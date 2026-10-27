"""
Enhanced reply handler with semantic intelligence.

Improvements over original:
1. Uses multi-dimensional semantic classification
2. Explicit failure handling (provider failures ≠ semantic neutrality)
3. State machine-driven transitions
4. Message deduplication
5. Proper timestamp tracking
"""

from __future__ import annotations

import logging
from dataclasses import replace

from sqlalchemy import func, text
from sqlalchemy.dialects import postgresql as _pg_dialect
from sqlalchemy.dialects import sqlite as _sqlite_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mailer_agent.config import get_settings
from mailer_agent.llm.agent import ContextualFallbackUnavailable, draft_message
from mailer_agent.mail.imap_reader import InboundEmail, as_reply_subject, is_synthetic_id
from mailer_agent.mail.sender import SendOutcome, send_email
from mailer_agent.memory.store import build_conversation_context, maybe_summarize_older_messages
from mailer_agent.policy.guardrails import authorize_action
from mailer_agent.policy.next_action import PLANNER_PROMPT_VERSION, plan_next_action
from mailer_agent.models import (
    Campaign,
    Contact,
    ContactStatus,
    ExternalDispatch,
    Message,
    MessageDirection,
    MessageStatus,
    MessageType,
    SuppressionEntry,
    UnmatchedInboundMessage,
    contact_is_integration_managed,
)
from mailer_agent.semantic.classifier import classify_prospect_reply
from mailer_agent.semantic_models import IntentType, attach_fact_provenance, serialize_semantic_intent
from mailer_agent.state_machine import (
    StateTransitionEvent,
    infer_event_from_semantic_intent,
    transition_contact_state,
)
from mailer_agent.utils.datetime_utils import utcnow

logger = logging.getLogger("mailer_agent.mail.reply_handler_v2")
settings = get_settings()


def _build_references_header(
    parent_references: list[str], parent_in_reply_to: str | None, parent_message_id: str | None
) -> str | None:
    """
    RFC 5322 References for a reply: the parent's own References chain
    (its ancestors), then the parent's own In-Reply-To, then the
    parent's own Message-ID -- in order, with no duplicates.

    In-Reply-To is included as well as References because real mail
    clients don't always populate References even when they do set
    In-Reply-To (Gmail's first reply in a thread, in particular, sends
    In-Reply-To with References empty) -- using References alone would
    silently drop the immediate ancestor from the chain in exactly that
    case. The immediate parent (parent_message_id) stays the last entry
    here (and is separately set as In-Reply-To by the caller) -- this
    just makes sure the rest of the ancestor chain isn't dropped on the
    next hop.

    Deterministic and protocol-level: never derived from anything the
    LLM wrote.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for msg_id in (*parent_references, parent_in_reply_to, parent_message_id):
        if msg_id and msg_id not in seen:
            seen.add(msg_id)
            ordered.append(msg_id)
    return " ".join(ordered) if ordered else None


def _normalize_message_id(value: str | None) -> str | None:
    """
    Canonicalize a Message-ID-like header value to RFC 5322's
    angle-bracketed form, e.g. "abc@x" and "<abc@x>" both become
    "<abc@x>".

    Why this matters: IMAP polling extracts the raw `Message-ID` header
    via Python's email module, which normally keeps the angle brackets
    intact. A webhook payload has no such guarantee -- different
    inbound-parse providers (and hand-rolled payload templates) may or
    may not include them, and may add stray whitespace. Without
    normalizing at the one point both channels converge
    (process_inbound_email_v2), the *same physical email* delivered via
    webhook and then again via IMAP fallback (or vice versa) could be
    dedup-mismatched purely due to string formatting, and get processed
    -- and possibly auto-replied to -- twice.
    """
    if not value:
        return value
    v = value.strip()
    if not v:
        return None
    if not v.startswith("<"):
        v = f"<{v}"
    if not v.endswith(">"):
        v = f"{v}>"
    return v


def _scope_messages_to_org(query, organization_id: str | None):
    """
    Restrict a ``Message`` query to rows owned by ``organization_id`` (Message -> Contact ->
    Campaign.organization_id). ``organization_id=None`` returns the query UNCHANGED: that is
    the deliberate, trusted-internal behaviour of the legacy deployment-global IMAP poller
    only. Any caller acting on behalf of an authenticated tenant must pass that tenant's id.
    """
    if organization_id is None:
        return query
    return (
        query.join(Contact, Message.contact_id == Contact.id)
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(Campaign.organization_id == organization_id)
    )


def process_inbound_email_v2(db: Session, email_in: InboundEmail, *, organization_id: str | None) -> dict:
    """
    Enhanced inbound email processing with semantic intelligence.
    
    Returns dict with processing result.

    ``organization_id`` is REQUIRED (keyword-only, no default) so that no caller can be
    unscoped by omission:
      * a ``str`` -- the authenticated tenant (webhook): every lookup (duplicate check,
        threading-header correlation, e-mail fallback, post-IntegrityError recovery) is
        restricted to that tenant's campaigns. Something that belongs to another tenant
        behaves exactly like "not found": ``{"matched": False, ...}`` with no foreign
        identifier in the result, and no foreign record is read into the response or modified.
      * ``None`` -- the legacy deployment-global IMAP poller only (trusted internal input,
        single-tenant by design): unscoped, exactly the behaviour before C15.1b.

    This is the one convergence point for both inbound channels
    (api/webhooks.py and the IMAP poll job in followup/scheduler.py both
    call through mail.reply_handler.process_inbound_email, which is a
    thin pass-through to this function) -- so Message-ID normalization
    happens here, once, rather than duplicated in each channel's own
    parsing code.
    """
    # Normalize threading identifiers before anything else touches them,
    # so dedup (Step 1) and threading correlation (Step 2) both compare
    # apples to apples regardless of which channel this arrived through.
    normalized_message_id = _normalize_message_id(email_in.message_id)
    normalized_in_reply_to = _normalize_message_id(email_in.in_reply_to)
    normalized_references = [
        r for r in (_normalize_message_id(ref) for ref in email_in.references) if r
    ]
    email_in = replace(
        email_in,
        message_id=normalized_message_id,
        in_reply_to=normalized_in_reply_to,
        references=normalized_references,
    )

    # Step 1: Deduplicate by Message-ID
    if email_in.message_id:
        existing = _scope_messages_to_org(
            db.query(Message).filter(
                Message.message_id_header == email_in.message_id,
                Message.direction == MessageDirection.INBOUND.value
            ),
            organization_id,
        ).first()
        
        if existing:
            logger.info(
                f"Duplicate inbound email detected (Message-ID: {email_in.message_id}), skipping"
            )
            return {
                "matched": True,
                "contact_id": existing.contact_id,
                "action": "skipped_duplicate",
                "message_id": existing.id
            }
    
    # Step 2: Correlate to contact
    # An explicit thread reference that cannot be resolved in this tenant
    # must not fall back to matching a shared sender address.
    thread_reference_present = bool(
        email_in.in_reply_to or email_in.references
    )
    contact = _find_contact_by_message_id(
        db, email_in, organization_id
    )
    if contact is None and not thread_reference_present:
        contact = _find_contact_by_email(
            db, email_in, organization_id
        )
    
    if not contact:
        logger.info(
            "No matching contact for inbound email from %s (to=%s) -- "
            "either genuinely unknown or ambiguous correlation (see warning "
            "above if this was an ambiguous-email-match case); skipped, not persisted.",
            email_in.from_email, email_in.to_email,
        )
        return {"matched": False, "from_email": email_in.from_email}
    
    campaign = contact.campaign
    
    # Step 3: Persist inbound message
    inbound_msg = Message(
        contact_id=contact.id,
        direction=MessageDirection.INBOUND.value,
        message_type=None,
        subject=email_in.subject,
        body=email_in.body_text,
        status=MessageStatus.RECEIVED.value,
        message_id_header=email_in.message_id,
        in_reply_to_header=email_in.in_reply_to,
        references_header=" ".join(email_in.references) if email_in.references else None,
    )
    db.add(inbound_msg)
    try:
        db.flush()
    except IntegrityError:
        # The Step 1 check-then-insert dedup check above has a race
        # window under true concurrency: two transactions can both see
        # "not found" before either commits (e.g. the same email
        # arriving via webhook and IMAP nearly simultaneously). The
        # uq_messages_message_id_no_mailbox index (see models.py,
        # migrations/003_message_id_unique_constraint.py) is what
        # actually closes that race -- this except clause is what makes
        # hitting it a graceful "someone else already handled this"
        # instead of an unhandled 500. Roll back this transaction (now
        # aborted after the IntegrityError) and re-query for the row the
        # winning transaction inserted.
        db.rollback()
        logger.info(
            "Concurrent duplicate detected for Message-ID %s (lost the "
            "race to insert, another transaction won) -- treating as "
            "already-handled, not an error.",
            email_in.message_id,
        )
        existing = _scope_messages_to_org(
            db.query(Message).filter(
                Message.message_id_header == email_in.message_id,
                Message.direction == MessageDirection.INBOUND.value
            ),
            organization_id,
        ).first()
        if organization_id is not None and existing is None:
            # The global uniqueness constraint fired because the Message-ID is already held by a
            # row this tenant cannot see (another tenant's). Behave as "not found": no foreign
            # contact_id / message id in the result, and nothing of theirs was touched.
            logger.info("Inbound Message-ID already recorded outside this organization; treated as unmatched.")
            return {"matched": False, "from_email": email_in.from_email}
        return {
            "matched": True,
            "contact_id": existing.contact_id if existing else contact.id,
            "action": "skipped_duplicate",
            "message_id": existing.id if existing else None,
        }
    
    # Steps 4-6 (classification, state transition, reply drafting, memory) are
    # shared with the mailbox-bound path -- see _classify_and_apply_inbound.
    return _classify_and_apply_inbound(db, email_in, contact, campaign, inbound_msg)


def _classify_and_apply_inbound(
    db: Session,
    email_in: InboundEmail,
    contact: Contact,
    campaign,
    inbound_msg: Message,
) -> dict:
    """
    Everything after the inbound Message row exists and its contact is known:
    semantic classification, state-machine transition, (gated) reply drafting,
    memory summarization. Extracted unchanged from process_inbound_email_v2 so
    the webhook/legacy path and the M3 mailbox-bound path run the SAME
    classification and prompt-injection-hardened machinery. The caller owns the
    transaction; this never commits.
    """
    # Update contact timestamps
    contact.last_reply_at = utcnow()
    
    # Step 4: Semantic classification
    context_transcript = build_conversation_context(db, contact)
    
    classification_result = classify_prospect_reply(
        campaign=campaign,
        contact=contact,
        inbound_body=email_in.body_text,
        conversation_context=context_transcript,
        auto_submitted=email_in.auto_submitted,
    )
    
    # Store classification result in message
    inbound_msg.classification_success = classification_result.success
    
    if classification_result.success and classification_result.semantic_intent:
        intent = classification_result.semantic_intent

        # Deterministic provenance stamp (spec section 14) -- must happen
        # here, not in the classifier: this is the one place that
        # actually knows the inbound Message's real database id and
        # observation time. The LLM only ever fills `supersedes` (see
        # semantic/classifier.py's SUPERSESSION rule) -- it has no
        # visibility into row ids or wall-clock time and must never be
        # asked to invent them.
        attach_fact_provenance(
            intent,
            source_message_id=inbound_msg.id,
            observed_at=utcnow().isoformat(),
        )

        # Native dict into the JSON column -- semantic_analysis is
        # Column(JSON), and SQLAlchemy's JSON type handles
        # serialization itself. Passing json.dumps(...) here used to
        # double-encode it (a JSON string *containing* a JSON string),
        # which breaks any JSON-path query against the column and means
        # any future reader has to json.loads() a value that should
        # already be a dict.
        inbound_msg.semantic_analysis = serialize_semantic_intent(intent)
        
        # Backward compatibility: set detected_intent to primary intent
        inbound_msg.detected_intent = intent.intents[0].value if intent.intents else "neutral"
        inbound_msg.intent_confidence = intent.confidence
        
        # Update contact buying stage
        contact.buying_stage = intent.buying_stage.value
        
    else:
        # Classification failed
        inbound_msg.classification_failure_reason = (
            classification_result.failure_reason.value if classification_result.failure_reason else "unknown"
        )
        inbound_msg.detected_intent = "unknown"
        inbound_msg.intent_confidence = 0.0
    
    result = {
        "matched": True,
        "contact_id": contact.id,
        "classification_success": classification_result.success,
    }
    
    # Step 5: Handle based on classification
    if not classification_result.success:
        # Classification failed - require human review
        logger.warning(
            f"Classification failed for contact {contact.id}: "
            f"{classification_result.failure_reason} - routing to human review"
        )
        
        transition_contact_state(
            contact,
            StateTransitionEvent.NEEDS_HUMAN,
            reason=f"Classification failed: {classification_result.failure_reason}"
        )
        contact.next_action_at = None
        result["action"] = "needs_review_classification_failed"
        
    elif not classification_result.semantic_intent:
        # Should not happen if success=True, but handle gracefully
        logger.error(f"Classification success but no semantic_intent for contact {contact.id}")
        transition_contact_state(contact, StateTransitionEvent.NEEDS_HUMAN, reason="Missing semantic intent")
        result["action"] = "needs_review_missing_intent"
        
    else:
        # Successful classification
        intent = classification_result.semantic_intent
        result["primary_intent"] = intent.intents[0].value if intent.intents else "neutral"
        result["confidence"] = intent.confidence
        
        # Infer state transition event
        event = infer_event_from_semantic_intent(intent)
        
        # Handle terminal intents
        if IntentType.UNSUBSCRIBE in intent.intents:
            _handle_unsubscribe(db, contact, result)
            
        elif IntentType.NOT_INTERESTED in intent.intents:
            transition_contact_state(contact, StateTransitionEvent.NOT_INTERESTED)
            contact.next_action_at = None
            result["action"] = "closed_lost"
            
        elif IntentType.OUT_OF_OFFICE in intent.intents:
            # No state change, no action - just wait
            result["action"] = "ignored_oos"
            
        elif contact_is_integration_managed(contact):
            # LeadBoost-owned contact. The reply is stored, classified and
            # state-tracked above/here, but Mailer-native automation must not
            # turn it into an outbound email nobody authorized: no native
            # follow-up rescheduling, no drafting, and in particular no
            # automatic send -- AUTO_REPLY_ENABLED does not apply to this
            # contact. next_action_at is pinned to None so the contact can
            # never become native-scheduler-eligible. The bilateral reply
            # contract is C9.3.
            transition_contact_state(contact, event, reason=f"Semantic: {intent.reasoning[:100]}")
            contact.next_action_at = None
            result["action"] = "recorded_integration_managed"

        else:
            # Regular reply - transition state
            transition_contact_state(contact, event, reason=f"Semantic: {intent.reasoning[:100]}")
            
            # Reschedule follow-up based on conversation context
            from mailer_agent.followup.conversation_aware import FollowUpScheduler
            scheduler = FollowUpScheduler()
            contact.next_action_at = scheduler.reschedule_after_reply(
                contact, campaign, intent
            )
            
            # Draft reply
            _draft_and_maybe_send_reply(
                db, contact, campaign, email_in, intent, result,
                classification_source=classification_result.source,
                classification_model_used=classification_result.model_used,
                classification_used_fallback=classification_result.used_fallback,
                classification_response_mode=classification_result.response_mode,
                inbound_message_id=inbound_msg.id,
            )
    
    db.add(contact)
    db.add(inbound_msg)
    db.flush()
    
    # Step 6: Update conversation memory
    maybe_summarize_older_messages(db, contact)
    
    return result


# ---------------------------------------------------------------------------
# M3: mailbox-bound inbound
# ---------------------------------------------------------------------------

_UNMATCHED = "no_match"
_AMBIGUOUS = "ambiguous"


def process_mailbox_inbound(
    db: Session,
    email_in: InboundEmail,
    *,
    mailbox_id: int,
    organization_id: str,
) -> dict:
    """
    Inbound processing for mail received through ONE Mailer Mailbox (M3).

    `mailbox_id` and `organization_id` come from the polled Mailbox row, never
    from the message: the tenant is bound before any content is turned into
    application state, and every lookup below is parameterized by it. Nothing in
    the message (From, To, Subject, Message-ID, body) selects or widens the tenant.

    Differences from process_inbound_email_v2 (the webhook / legacy-global-IMAP
    path, which is unchanged):
      * dedupe identity is (mailbox_id, Message-ID) -- or (mailbox_id,
        "synthetic:<sha256>") for a message with no usable Message-ID -- not a
        global Message-ID, so another organization's copy of the same email is
        never mistaken for a duplicate (and never leaks its contact id);
      * correlation is organization- and mailbox-scoped (see
        _correlate_mailbox_inbound); no To:-header resolution;
      * unmatched or ambiguous mail is NOT guessed and is NOT stored as a Message: it is
        retained, unattached to any contact, in ``unmatched_inbound_messages`` (see
        _retain_unmatched) and returns an unmatched result carrying only a reason. The caller's
        commit therefore makes it durable BEFORE the IMAP message is marked Seen, so a Seen
        message is never one that exists nowhere in the database.

    A correlated message is inserted with a conflict-safe INSERT whose only absorbed outcome is
    "this exact message is already stored" (see _insert_correlated_message); any other database
    failure propagates, and a duplicate is reported only after the existing row has been verified.

    The caller owns the transaction: it must commit before marking the IMAP
    message Seen. This function never commits and never rolls back.
    """
    email_in = replace(
        email_in,
        message_id=_normalize_message_id(email_in.message_id),
        in_reply_to=_normalize_message_id(email_in.in_reply_to),
        references=[
            r for r in (_normalize_message_id(ref) for ref in email_in.references) if r
        ],
    )
    identity = email_in.message_id or email_in.synthetic_id
    if not identity:
        # parse_inbound_bytes always provides one of the two; refuse rather than
        # persist an un-dedupable row.
        raise ValueError("mailbox inbound requires a Message-ID or synthetic identity")

    existing = _find_inbound_message(db, mailbox_id=mailbox_id, identity=identity, organization_id=organization_id)
    if existing:
        logger.info("Duplicate inbound for mailbox %s (already stored), skipping", mailbox_id)
        return {
            "matched": True, "contact_id": existing.contact_id,
            "action": "skipped_duplicate", "message_id": existing.id,
        }

    contact, reason = _correlate_mailbox_inbound(
        db, email_in, mailbox_id=mailbox_id, organization_id=organization_id
    )
    if contact is None:
        _retain_unmatched(
            db, email_in, identity=identity, mailbox_id=mailbox_id,
            organization_id=organization_id, reason=reason,
        )
        # Metadata only: no addresses, subject or body in the log line.
        logger.info(
            "Inbound for mailbox %s not attached to any contact (%s); retained for review, not attached",
            mailbox_id, reason,
        )
        return {"matched": False, "action": f"unmatched_{reason}", "reason": reason}

    campaign = contact.campaign
    inbound_msg, inserted = _insert_correlated_message(
        db, email_in, contact_id=contact.id, identity=identity,
        mailbox_id=mailbox_id, organization_id=organization_id,
    )
    if not inserted:
        # Lost a race against a concurrent poll of the same mailbox. This is reported as a duplicate
        # ONLY because _insert_correlated_message verified the exact existing row (this tenant, this
        # mailbox, this Message-ID, inbound); anything it could not verify has already raised.
        logger.info("Concurrent duplicate for mailbox %s resolved by unique index (existing row verified)", mailbox_id)
        return {
            "matched": True,
            "contact_id": inbound_msg.contact_id,
            "action": "skipped_duplicate",
            "message_id": inbound_msg.id,
        }

    # If this exact message was retained earlier as unmatched/ambiguous and has now been attached
    # (an operator fixed the cause and marked it Unseen), record that; the retained copy stays.
    db.query(UnmatchedInboundMessage).filter(
        UnmatchedInboundMessage.mailbox_id == mailbox_id,
        UnmatchedInboundMessage.message_id_header == identity,
        UnmatchedInboundMessage.resolved_at.is_(None),
    ).update({"resolved_at": utcnow()}, synchronize_session=False)

    return _classify_and_apply_inbound(db, email_in, contact, campaign, inbound_msg)


class MessagePersistenceNotConfirmedError(RuntimeError):
    """A correlated inbound message could not be confirmed as stored (an insert conflict whose existing
    row is not this tenant's inbound message for this mailbox and Message-ID). Raised so the handler
    never reports success without a durable row: the poller then leaves the IMAP message Unseen."""


def _dialect_insert(db: Session):
    """The dialect-specific INSERT constructor that supports ON CONFLICT. Only PostgreSQL and SQLite
    are supported (the only dialects this service runs on); anything else fails closed."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return _pg_dialect.insert
    if dialect == "sqlite":
        return _sqlite_dialect.insert
    raise RetentionNotConfirmedError(f"conflict-safe inbound persistence is not supported on dialect {dialect!r}")


def _find_inbound_message(db: Session, *, mailbox_id: int, identity: str, organization_id: str) -> Message | None:
    """The inbound Message stored for exactly this (mailbox_id, Message-ID) AND owned by this tenant
    (Message -> Contact -> Campaign.organization_id). Tenant scoping is mandatory here: passing None
    would silently return an unscoped query, so it is refused."""
    if organization_id is None:
        raise ValueError("a tenant (organization_id) is required to look up a mailbox inbound message")
    return _scope_messages_to_org(
        db.query(Message).filter(
            Message.mailbox_id == mailbox_id,
            Message.message_id_header == identity,
            Message.direction == MessageDirection.INBOUND.value,
        ),
        organization_id,
    ).first()


def _insert_correlated_message(
    db: Session,
    email_in: InboundEmail,
    *,
    contact_id: int,
    identity: str,
    mailbox_id: int,
    organization_id: str,
) -> tuple[Message, bool]:
    """
    Insert the inbound Message for a correlated reply. Returns ``(message, inserted)``.

    The arbiter is the partial unique index ``uq_messages_mailbox_message_id`` --
    UNIQUE (mailbox_id, message_id_header) WHERE mailbox_id IS NOT NULL (models.Message.__table_args__,
    migration 009) -- so the conflict target repeats that exact predicate. ``ON CONFLICT ... DO
    NOTHING`` absorbs only a uniqueness conflict on that one index. Every other failure (NOT NULL,
    foreign key, a different constraint) raises IntegrityError out of this function; nothing is caught,
    rolled back or hidden here.

    If the insert was absorbed, the existing row is looked up with the SAME tenant-scoped query as
    the duplicate pre-check (this tenant, this mailbox, this Message-ID, inbound). Only a row found
    that way is a duplicate (``inserted=False``). Any other conflicting row -- another tenant's, or an
    outbound row -- is not accepted: MessagePersistenceNotConfirmedError is raised.
    """
    stmt = (
        _dialect_insert(db)(Message)
        .values(
            contact_id=contact_id,
            mailbox_id=mailbox_id,
            direction=MessageDirection.INBOUND.value,
            message_type=None,
            subject=email_in.subject,
            body=email_in.body_text,
            status=MessageStatus.RECEIVED.value,
            message_id_header=identity,
            in_reply_to_header=email_in.in_reply_to,
            references_header=" ".join(email_in.references) if email_in.references else None,
        )
        .on_conflict_do_nothing(
            index_elements=["mailbox_id", "message_id_header"],
            index_where=text("mailbox_id IS NOT NULL"),
        )
        .returning(Message.id)
    )
    new_id = db.execute(stmt).scalar_one_or_none()
    if new_id is not None:
        return db.get(Message, new_id), True
    existing = _find_inbound_message(db, mailbox_id=mailbox_id, identity=identity, organization_id=organization_id)
    if existing is None:
        raise MessagePersistenceNotConfirmedError(
            f"mailbox {mailbox_id}: the insert conflicted but no inbound message for this tenant, mailbox "
            f"and Message-ID exists"
        )
    return existing, False


class RetentionNotConfirmedError(RuntimeError):
    """Retention could not be confirmed. Raised so the handler never reports success without a durable
    row: the poller then leaves the IMAP message Unseen and the next poll retries it."""


def _retained_row_exists(db: Session, *, mailbox_id: int, identity: str, organization_id: str) -> bool:
    """True only if a row with this EXACT identity -- (mailbox_id, message_id_header) and the same
    organization -- exists in the current transaction's view."""
    return (
        db.query(UnmatchedInboundMessage.id)
        .filter(
            UnmatchedInboundMessage.mailbox_id == mailbox_id,
            UnmatchedInboundMessage.message_id_header == identity,
            UnmatchedInboundMessage.organization_id == organization_id,
        )
        .first()
        is not None
    )


def _retain_unmatched(
    db: Session,
    email_in: InboundEmail,
    *,
    identity: str,
    mailbox_id: int,
    organization_id: str,
    reason: str,
) -> None:
    """
    Persist one unmatched/ambiguous mailbox message so it can be marked Seen without being lost.

    Returns only when a row with this exact (mailbox_id, identity, organization) exists in the caller's
    transaction; in every other case it raises, so a retention failure can never look like success.

    The insert is ``INSERT ... ON CONFLICT (mailbox_id, message_id_header) DO NOTHING``. That clause
    absorbs exactly one thing -- a uniqueness conflict on that one key, i.e. the message is already
    retained (re-offered after a failed Seen, an operator re-offer, or a concurrent poll). Every other
    constraint failure (NOT NULL, foreign key, a different unique key, a check) is NOT absorbed: it
    raises IntegrityError out of this function, nothing is rolled back or hidden here, and the caller
    rolls the transaction back. Nothing is caught: no exception is used for control flow, and no
    rollback of the caller's transaction is ever issued from here.

    After the insert the row is CONFIRMED to exist (``_retained_row_exists``) whether it was inserted or
    the insert was a no-op. A conflict whose row is not visible or does not match the exact identity
    (for example under REPEATABLE READ, or a row recorded for another organization) is not accepted as a
    duplicate: it raises RetentionNotConfirmedError. ``organization_id`` / ``mailbox_id`` are the polled
    Mailbox's, never the message's. Nothing is attached to a contact and ``messages`` is not touched.
    This function never commits (the caller does, before the IMAP Seen flag).

    Only PostgreSQL and SQLite are supported (see _dialect_insert); any other fails closed rather than
    falling back to a broader exception handler.
    """
    stmt = (
        _dialect_insert(db)(UnmatchedInboundMessage)
        .values(
            mailbox_id=mailbox_id,
            organization_id=organization_id,
            message_id_header=identity,
            reason=reason,
            from_email=email_in.from_email,
            subject=email_in.subject,
            body=email_in.body_text,
            in_reply_to_header=email_in.in_reply_to,
            references_header=" ".join(email_in.references) if email_in.references else None,
        )
        .on_conflict_do_nothing(index_elements=["mailbox_id", "message_id_header"])
    )
    inserted = db.execute(stmt).rowcount == 1

    if not _retained_row_exists(db, mailbox_id=mailbox_id, identity=identity, organization_id=organization_id):
        raise RetentionNotConfirmedError(
            f"mailbox {mailbox_id}: no retained row with the exact identity exists after the insert"
        )
    if not inserted:
        logger.info("Inbound for mailbox %s already retained (confirmed existing row); not stored twice", mailbox_id)


def _correlate_mailbox_inbound(
    db: Session, email_in: InboundEmail, *, mailbox_id: int, organization_id: str
) -> tuple[Contact | None, str | None]:
    """
    Safe correlation inside ONE organization, in this order:

      1. RFC threading: In-Reply-To / References matched against Mailer-stored
         messages of this organization. A stored message only counts if it
         belongs to this mailbox: an inbound row must carry this mailbox_id; an
         outbound row sent through the Mailer mailbox path must have its
         ExternalDispatch.mailbox_id == this mailbox. An outbound row with no
         ExternalDispatch (the native, deployment-global-SMTP path) has no
         mailbox to compare, so it is accepted on organization scope alone.
         If the eligible matches span more than one contact the result is
         AMBIGUOUS -- never "pick the newest".
      2. Only if no eligible thread match exists: the sender address within this
         organization, and only when exactly one non-suppressed contact has it.

    No subject matching and no To:-header use. Synthetic identities are never
    threading identifiers. Returns (contact, None) or (None, reason).
    """
    candidate_ids = {
        v for v in (email_in.in_reply_to, *email_in.references)
        if v and not is_synthetic_id(v)
    }
    if candidate_ids:
        rows = (
            db.query(Message)
            .join(Contact, Message.contact_id == Contact.id)
            .join(Campaign, Contact.campaign_id == Campaign.id)
            .filter(
                Campaign.organization_id == organization_id,
                Message.message_id_header.in_(candidate_ids),
                ~Message.message_id_header.like("synthetic:%"),
            )
            .all()
        )
        contact_ids: set[int] = set()
        for m in rows:
            if m.direction == MessageDirection.INBOUND.value:
                eligible = m.mailbox_id == mailbox_id
            else:
                dispatch_mailbox = (
                    db.query(ExternalDispatch.mailbox_id)
                    .filter(
                        ExternalDispatch.message_id == m.id,
                        ExternalDispatch.organization_id == organization_id,
                    )
                    .first()
                )
                eligible = dispatch_mailbox is None or dispatch_mailbox[0] == mailbox_id
            if eligible:
                contact_ids.add(m.contact_id)
        if len(contact_ids) > 1:
            return None, _AMBIGUOUS
        if len(contact_ids) == 1:
            return db.get(Contact, next(iter(contact_ids))), None

    if not email_in.from_email:
        return None, _UNMATCHED
    candidates = (
        db.query(Contact)
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(
            Campaign.organization_id == organization_id,
            func.lower(Contact.email) == email_in.from_email,
            Contact.status != ContactStatus.SUPPRESSED.value,
        )
        .all()
    )
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, _AMBIGUOUS
    return None, _UNMATCHED


def _find_contact_by_message_id(
    db: Session, email_in: InboundEmail, organization_id: str | None
) -> Contact | None:
    """Find contact by threading headers (reliable method). Restricted to ``organization_id``'s
    campaigns when it is not None (see process_inbound_email_v2)."""
    candidate_ids = set()
    if email_in.in_reply_to:
        candidate_ids.add(email_in.in_reply_to)
    candidate_ids.update(email_in.references)
    
    if not candidate_ids:
        return None
    
    msg = (
        _scope_messages_to_org(
            db.query(Message).filter(Message.message_id_header.in_(candidate_ids)),
            organization_id,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    
    return msg.contact if msg else None


def _find_contact_by_email(
    db: Session, email_in: InboundEmail, organization_id: str | None
) -> Contact | None:
    """
    Find contact by email address (fallback method, used only when
    threading-header correlation in _find_contact_by_message_id fails).

    Tenant-safe by construction, not by convention:
    - If to_email is known (webhook told us which inbox received this,
      or IMAP polled a specific mailbox), resolution is deterministic:
      joined to Campaign.sender_email == to_email, which scopes to
      exactly one organization before any row is even considered.
    - If to_email is NOT known, this never guesses across multiple
      candidates. If more than one contact anywhere matches from_email,
      correlation is genuinely ambiguous (could be two different
      organizations that both have a contact with this address) and
      this returns None -- unresolved -- rather than silently picking
      the most-recently-updated one, which is what this function used
      to do and which risked attributing a reply to the wrong tenant.
      The caller (process_inbound_email_v2) treats None the same as "no
      match": the inbound email is not persisted against any contact,
      logged, and left for human/ops follow-up instead of guessed at.
    """
    query = db.query(Contact).filter(
        Contact.email == email_in.from_email,
        Contact.status != ContactStatus.SUPPRESSED.value,
    )

    if organization_id is not None or email_in.to_email:
        query = query.join(Campaign, Contact.campaign_id == Campaign.id)
    if organization_id is not None:
        # Authenticated tenant: only its own campaigns are ever candidates, so the ambiguity
        # check below is evaluated within that tenant.
        query = query.filter(Campaign.organization_id == organization_id)

    if email_in.to_email:
        # to_email scopes this to (at most) one organization before we
        # pick among its contacts, so a most-recently-updated tiebreaker
        # here is choosing among that ONE org's own re-contacts of the
        # same address (e.g. a re-run campaign) -- not guessing across
        # tenants.
        query = query.filter(Campaign.sender_email == email_in.to_email)
        return query.order_by(Contact.updated_at.desc()).first()

    # No to_email available: we cannot resolve to a specific
    # organization ourselves. Only proceed if the match is already
    # unambiguous (exactly one contact anywhere has this email) --
    # otherwise this is exactly the cross-tenant collision scenario
    # that must not be guessed at.
    candidates = query.all()
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "Ambiguous inbound correlation: %d contacts match from_email=%s "
            "with no to_email or thread-header to disambiguate -- treating "
            "as unresolved rather than guessing which organization owns it.",
            len(candidates), email_in.from_email,
        )
    return None


def _handle_unsubscribe(db: Session, contact: Contact, result: dict):
    """Handle unsubscribe request."""
    # Add to suppression list if not already there
    # CRITICAL: Include organization_id to properly scope suppression
    from mailer_agent.models import Campaign
    campaign = db.query(Campaign).filter_by(id=contact.campaign_id).first()
    org_id = campaign.organization_id if campaign else None
    
    existing = (
        db.query(SuppressionEntry)
        .filter_by(email=contact.email, organization_id=org_id)
        .first()
    )
    
    if not existing:
        db.add(SuppressionEntry(
            email=contact.email,
            organization_id=org_id,
            reason="unsubscribed"
        ))
    
    # Transition to suppressed
    transition_contact_state(contact, StateTransitionEvent.UNSUBSCRIBED)
    contact.next_action_at = None
    
    result["action"] = "suppressed"


def _build_known_facts_summary(intent, contact: Contact, campaign) -> str:
    """
    Facts for the planner prompt: what's actually APPROVED to draw from
    (the campaign's value_prop/proof_points and the contact's
    context_notes -- the same sources llm/agent.py's Responder and
    grounding.py's validator treat as authoritative) plus accumulated
    conversation-wide facts (memory.store.build_known_facts_context,
    pulled from every prior classified inbound message's stored
    semantic_analysis) plus anything new from THIS turn's intent that
    hasn't been persisted yet at the point this runs.

    The approved-content block matters specifically for
    PLANNER_SYSTEM_PROMPT's rule 2 ("only propose
    provide_requested_information if it's the kind of thing that could
    plausibly be answered from approved campaign materials") -- without
    it, the planner was being asked to judge plausibility with no view
    of what's actually approved, so it could only guess. It now sees the
    same approved facts the Responder and grounding gate already use, so
    its own instruction has something to check itself against instead
    of assuming.

    Deduplicated by simple text match -- this-turn facts are appended
    after the accumulated ones so the planner sees the most current
    information last.
    """
    from mailer_agent.memory.store import build_known_facts_context

    approved_lines = [
        f"value proposition: {campaign.value_prop}",
        "proof points: "
        + (campaign.proof_points or "(none approved -- nothing beyond the value proposition above is available)"),
        "approved facts about this contact: " + (contact.context_notes or "(none recorded)"),
    ]
    approved = "Approved campaign/contact content (the only source of business facts a reply may draw from):\n" + "\n".join(
        approved_lines
    )

    accumulated = build_known_facts_context(contact)
    this_turn_lines = []
    if intent.current_solution:
        this_turn_lines.append(
            f"current_solution (this message): {intent.current_solution.value} "
            f"(certainty={intent.current_solution.certainty.value})"
        )
    for fact in intent.new_facts:
        this_turn_lines.append(f"fact (this message): {fact.value} (certainty={fact.certainty.value})")
    if intent.contradicted_facts:
        this_turn_lines.append(f"contradicts earlier (this message): {intent.contradicted_facts}")

    parts = [p for p in (approved, accumulated, "\n".join(this_turn_lines)) if p]
    return "\n".join(parts)


def _draft_and_maybe_send_reply(
    db: Session,
    contact: Contact,
    campaign,
    email_in: InboundEmail,
    intent,
    result: dict,
    *,
    classification_source: str = "unknown",
    classification_model_used: str | None = None,
    classification_used_fallback: bool | None = None,
    classification_response_mode: str | None = None,
    inbound_message_id: int | None = None,
):
    """
    Plan, draft, and (if authorized) send a reply.

    Pipeline: Planner (LLM proposes what to do) -> Guardrails
    (deterministic authorization of that proposal) -> Responder
    (llm.agent.draft_message writes the actual wording, grounded in the
    planner's objective) -> suppression/grounding gates (independent,
    unconditional, checked regardless of what the planner/guardrails
    decided) -> send or hold for approval.

    This replaced a single inline line (`action_type = "closing" if
    POSITIVE_INTEREST and confidence >= 0.7 else "reply"`) plus a fixed
    ALWAYS_REQUIRE_APPROVAL_INTENTS set-membership check. Both are gone
    now, not because they were exactly wrong, but because they were the
    seed of exactly the kind of hardcoded, intent-keyed branching this
    architecture is supposed to avoid -- see policy/next_action.py and
    policy/guardrails.py for where that reasoning now lives instead.
    """
    context_transcript = build_conversation_context(db, contact)

    # --- Planner: what should this message accomplish? ---
    proposal = plan_next_action(
        intent=intent,
        context_transcript=context_transcript,
        known_facts=_build_known_facts_summary(intent, contact, campaign),
    )

    # --- Guardrails: is that proposal eligible for auto-send? ---
    authorized = authorize_action(proposal, intent=intent, auto_reply_enabled=settings.auto_reply_enabled)
    action_type = authorized.draft_action_type

    logger.info(
        "Contact %s planner proposal: action_type=%s objective=%r "
        "confidence=%.2f source=%s | guardrail: can_auto_send=%s reason=%r",
        contact.id, proposal.action_type.value, proposal.objective,
        proposal.confidence, proposal.source,
        authorized.can_auto_send, authorized.review_reason,
    )

    # --- Responder: write the actual wording, grounded in the plan. ---
    # draft_message() raises ContextualFallbackUnavailable instead of
    # returning a draft when the LLM is unavailable/failed -- a reply
    # has to respond to something specific the prospect said, and there
    # is no safe deterministic way to fabricate that (see
    # llm/agent.py's module docstring). No draft body is invented here:
    # route straight to human review instead of persisting a generic,
    # not-actually-responsive message that a reviewer might approve
    # without noticing it doesn't address what was asked.
    try:
        draft = draft_message(
            campaign=campaign,
            contact=contact,
            action_type=action_type,
            context_transcript=context_transcript,
            planner_objective=proposal.objective,
            planner_reason=proposal.reason,
        )
    except ContextualFallbackUnavailable as e:
        logger.warning(
            "Contact %s reply requires human drafting -- no LLM-generated "
            "content available: %s", contact.id, e,
        )
        transition_contact_state(
            contact,
            StateTransitionEvent.NEEDS_HUMAN,
            reason=f"LLM unavailable for contextual reply (action_type={action_type})",
        )
        result["action"] = "reply_needs_manual_draft"
        result["approval_reason"] = (
            "LLM was unavailable, so no automated draft could be safely "
            "generated for this reply -- a human must write it from scratch."
        )
        return result

    # Grounding gate: if unsupported claims found, force draft status
    # regardless of the planner/guardrail decision. Independent of, and
    # downstream of, everything above -- a well-authorized action can
    # still produce an ungrounded draft if the model invents a detail
    # while executing an otherwise-fine plan.
    grounding_blocked = draft.grounding and not draft.grounding.is_safe_to_send
    if grounding_blocked:
        logger.info(
            "Contact %s reply held for grounding review: %s",
            contact.id, draft.grounding.validation_notes,
        )

    # Suppression gate: every other send path (initial outreach,
    # follow-up, and the manual-approval endpoint) checks suppression
    # immediately before the external send call -- checked here too,
    # immediately before deciding whether to auto-send, independent of
    # the planner/guardrail decision above.
    from mailer_agent.followup.engine import is_suppressed
    suppressed = is_suppressed(db, contact.email, org_id=campaign.organization_id)
    if suppressed:
        logger.info(
            "Contact %s reply held for review instead of auto-sent: address is suppressed",
            contact.id,
        )

    can_auto_send = authorized.can_auto_send and not grounding_blocked and not suppressed

    # Subject and threading are transport/application-layer facts, not
    # wording -- the responder proposes draft.subject for content it
    # originates (initial outreach, follow-ups), but a contextual reply
    # stays in the prospect's existing thread deterministically. Letting
    # an LLM-generated subject replace it would silently start a new
    # thread in the recipient's mail client even though In-Reply-To/
    # References still point at the right parent (see docs for the
    # observed live-E2E case this fixes). Do not use draft.subject here.
    reply_subject = as_reply_subject(email_in.subject)
    reply_references = _build_references_header(
        email_in.references, email_in.in_reply_to, email_in.message_id
    )

    # Create reply message
    reply_msg = Message(
        contact_id=contact.id,
        direction=MessageDirection.OUTBOUND.value,
        message_type=MessageType.REPLY.value if action_type == "reply" else MessageType.CLOSING.value,
        subject=reply_subject,
        body=draft.body,
        status=MessageStatus.DRAFT.value,
        in_reply_to_header=email_in.message_id,
        references_header=reply_references,
    )

    if can_auto_send:
        # Auto-send
        send_result = send_email(
            to_email=contact.email,
            from_email=campaign.sender_email,
            from_name=campaign.sender_name,
            subject=reply_subject,
            body_text=draft.body,
            reply_to=campaign.reply_to_email,
            in_reply_to_header=email_in.message_id,
            references_header=reply_references,
        )

        # Status mirrors send_result.outcome directly (sent/failed/unknown)
        # -- an ambiguous SMTP outcome must never be recorded as a plain
        # "failed" that a caller might treat as safe to blindly retry.
        reply_msg.status = send_result.outcome.value
        reply_msg.message_id_header = send_result.message_id
        reply_msg.error_message = send_result.error

        if send_result.success:
            contact.last_outbound_at = utcnow()
        elif send_result.outcome == SendOutcome.UNKNOWN:
            # Ambiguous delivery -- escalate instead of leaving the
            # contact in a state where an automatic follow-up cycle
            # could duplicate a reply that may have already gone out.
            transition_contact_state(
                contact,
                StateTransitionEvent.NEEDS_HUMAN,
                reason=f"Auto-reply send outcome unknown: {send_result.error}",
            )
            contact.next_action_at = None

        result["action"] = {
            "sent": "auto_replied",
            "failed": "auto_reply_failed",
            "unknown": "auto_reply_unknown",
        }[send_result.outcome.value]
        result["message_id"] = reply_msg.id
        result["planner_action_type"] = proposal.action_type.value

    else:
        # Requires human approval
        result["action"] = "reply_drafted_awaiting_approval"
        result["message_id"] = reply_msg.id
        result["planner_action_type"] = proposal.action_type.value
        if suppressed:
            result["approval_reason"] = "Contact is on the suppression list"
        elif grounding_blocked and draft.grounding:
            result["approval_reason"] = (
                f"Grounding: {draft.grounding.validation_notes}"
            )
        else:
            result["approval_reason"] = authorized.review_reason or "Requires review"

    db.add(reply_msg)

    # Full structured trace of this turn -- see observability.py. Logged
    # here rather than at each intermediate step so it captures the
    # actual final outcome, not just the plan.
    from mailer_agent.observability import build_and_log_turn_trace
    build_and_log_turn_trace(
        contact_id=contact.id,
        campaign_id=campaign.id,
        inbound_body=email_in.body_text,
        inbound_message_id=inbound_message_id,
        outbound_message_id=reply_msg.id,
        classification_source=classification_source,
        classification_success=True,
        semantic_intent_dict=serialize_semantic_intent(intent),
        classifier_model_used=classification_model_used,
        classifier_used_fallback=classification_used_fallback,
        classifier_response_mode=classification_response_mode,
        prompt_version_planner=PLANNER_PROMPT_VERSION,
        planner_action_type=proposal.action_type.value,
        planner_objective=proposal.objective,
        planner_confidence=proposal.confidence,
        planner_source=proposal.source,
        planner_model_used=proposal.model_used,
        planner_used_fallback=proposal.used_fallback,
        planner_response_mode=proposal.response_mode,
        guardrail_can_auto_send=authorized.can_auto_send,
        guardrail_review_reason=authorized.review_reason,
        draft_source=draft.source,
        grounding_safe=draft.grounding.is_safe_to_send if draft.grounding else None,
        grounding_notes=draft.grounding.validation_notes if draft.grounding else None,
        draft_model_used=draft.model_used,
        draft_used_fallback=draft.used_fallback,
        draft_response_mode=draft.response_mode,
        final_action=result.get("action"),
    )
