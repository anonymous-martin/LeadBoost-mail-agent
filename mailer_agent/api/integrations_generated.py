"""
LeadBoost generated-outreach intake (C9.2, durable since M2-B).

    POST /integrations/leadboost/outreach-requests

LeadBoost has already authorized the outreach action. Mailer validates the
request, resolves the organization's single ACTIVE mailbox (M2-A), durably
records ONE ExternalDispatch carrying the request context, and answers 202.
It makes NO LLM call and creates NO Message here. The generation worker
(mail/outreach_generation_worker.py) later drafts the message with the
existing stack (llm.agent.draft_message + grounding validator) and creates the
one Message; the C6-C8 worker (mail/external_dispatch_worker.py) then sends it
through the mailbox, and the C9.1 reconciliation GET observes the whole chain.

Why a separate module from api/integrations.py
----------------------------------------------
integrations.py holds the exact-message route and the C9.1 read-only
reconciliation handler, and a frozen C9.1 test asserts that module imports
nothing from the LLM / follow-up / sender stack. This route reads Mailer's own
conversation history (memory.store, which imports the LLM provider module), so
it lives here. It reuses the campaign/contact get-or-create helpers and the
mailbox resolver from integrations.py rather than duplicating them.

What this endpoint is NOT
-------------------------
Not a second campaign API and not a way to drive Mailer-native behaviour: no
sender/SMTP/mailbox/credential, subject/body, tone, proof_points, action type,
cadence or tenant field exists in the request (schemas.py, extra="forbid").
The sending identity is the organization's one ACTIVE mailbox.

Durable generation context
--------------------------
The offer (value_proposition), recipient_facts and recipient name/title/company
are never written to the shared integration Campaign or Contact rows (so
concurrent requests cannot see each other's context). Their only durable copy
is ExternalDispatch.grounding_context, an immutable per-dispatch snapshot (it
is the generation input AND what the dispatch worker grounds the finished
message against):

    Request A context -> Dispatch A -> (generation) Message A -> grounding A

Conversation history is Mailer's own: build_conversation_context() over the
Message table (a plain DB read, snapshotted at acceptance; empty for a new
contact). Nothing is accepted from the caller.

Failure visibility
------------------
Because generation happens after the 202, an LLM outage or a draft the
grounding validator hard-blocks surfaces as a FAILED dispatch (no Message, no
send) via the reconciliation GET, not as an HTTP error. A retry is a new
idempotency key, as for any FAILED dispatch.

Concurrency and idempotency
---------------------------
Identity is (organization_id, idempotency_key) -- DB unique constraint -- and
the operation fingerprint is built only from the caller's external_action_id
and recipient email. A replay of an accepted key returns the existing
operation without touching anything. Two identical concurrent requests: one
commit wins, the loser converges on the winner's dispatch.

Transaction boundaries: Campaign and Contact are reusable get-or-create rows
committed on their own; the ExternalDispatch (with its snapshot) is ONE final
commit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mailer_agent.api.deps import get_integration_org_id, require_api_key
from mailer_agent.api.integrations import (
    _get_or_create_integration_campaign,
    _get_or_create_integration_contact,
    _reject_if_contact_has_active_followup,
    _resolve_sole_active_mailbox,
)
from mailer_agent.db import get_db
from mailer_agent.mail.imap_reader import _usable_message_id, as_reply_subject, is_synthetic_id
from mailer_agent.mail.reply_handler_v2 import _build_references_header
from mailer_agent.memory.store import build_conversation_context
from mailer_agent.models import (
    Campaign,
    Contact,
    ExternalDispatch,
    ExternalDispatchState,
    Mailbox,
    Message,
    MessageDirection,
    campaign_is_integration_managed,
)
from mailer_agent.schemas import (
    LeadBoostOutreachActionAccepted,
    LeadBoostOutreachRequestIn,
)

logger = logging.getLogger("mailer_agent.api.integrations_generated")

router = APIRouter(
    prefix="/integrations/leadboost",
    tags=["integrations"],
    dependencies=[Depends(require_api_key)],
)

GROUNDING_CONTEXT_VERSION = 2

_IDEMPOTENCY_CONFLICT_DETAIL = (
    "idempotency_key has already been used with a different request. "
    "Use a new idempotency_key for a genuinely new action."
)


def _compute_generated_request_fingerprint(
    *, external_action_id: str, recipient_email: str, reply_to_message_reference: str | None = None
) -> str:
    """
    SHA-256 over the canonical JSON of the operation's identity: which
    LeadBoost action, for which recipient. Stable across safe retries.

    Deliberately excludes everything generated or descriptive (subject, body,
    value_proposition, recipient_facts, name/title/company, correlation_id):
    the same logical operation must keep the same fingerprint however its
    message was worded or its context refined on a retry. The "kind" tag keeps
    these fingerprints disjoint from the exact-message route's, so a key
    first used there can never read as a replay here.
    """
    identity = {
        "kind": "leadboost.generated_outreach.v1",
        "external_action_id": external_action_id,
        "recipient_email": recipient_email,
    }
    if reply_to_message_reference is not None:
        # Only present for a reply, so every pre-existing (non-reply) fingerprint is unchanged
        # and a key first used for a plain request can never read as a replay of a reply. The
        # opaque public reference is what the caller sent, so it is what is bound (never a row id).
        identity["reply_to_message_reference"] = reply_to_message_reference
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_dispatch(db: Session, org_id: str, idempotency_key: str) -> ExternalDispatch | None:
    # Tenant predicate lives in the query itself, never fetch-then-check.
    return (
        db.query(ExternalDispatch)
        .filter(
            ExternalDispatch.organization_id == org_id,
            ExternalDispatch.idempotency_key == idempotency_key,
        )
        .first()
    )


def _accepted(dispatch: ExternalDispatch) -> LeadBoostOutreachActionAccepted:
    return LeadBoostOutreachActionAccepted(
        accepted=True, mailing_agent_reference=dispatch.public_reference
    )


def _replay_or_conflict(
    existing: ExternalDispatch, fingerprint: str
) -> LeadBoostOutreachActionAccepted:
    if existing.request_fingerprint == fingerprint:
        return _accepted(existing)
    raise HTTPException(status_code=409, detail=_IDEMPOTENCY_CONFLICT_DETAIL)


def _format_recipient_facts(facts: list[str]) -> str | None:
    """Same free-text shape the existing prompt already presents as
    "Verified facts about this contact/company" (Contact.context_notes)."""
    if not facts:
        return None
    return "\n".join(f"- {fact}" for fact in facts)


def _resolve_reply_parent(
    db: Session, org_id: str, message_reference: str, recipient_email: str
) -> tuple[Message, Contact, Campaign]:
    """
    The stored inbound Message this reply answers, resolved with the tenant predicate in the
    query itself: the message must be INBOUND, belong to a contact of this organization's
    campaign, and have arrived through a Mailbox this organization owns (the same ownership
    rule as the C9.3 conversation view -- webhook / legacy-poll mail has no provable owner).
    Anything else -- unknown, another tenant's, outbound -- is an indistinguishable 404.

    409 (nothing created) when the parent's contact is not an integration-managed contact, or
    its address is not the recipient (a reply goes to the person who wrote, nobody else), or
    it carries a native follow-up schedule (same rule as every other integration dispatch).
    """
    row = (
        db.query(Message, Contact, Campaign)
        .join(Contact, Message.contact_id == Contact.id)
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .join(Mailbox, Message.mailbox_id == Mailbox.id)
        .filter(
            Message.reply_reference == message_reference,
            Message.direction == MessageDirection.INBOUND.value,
            Campaign.organization_id == org_id,
            Mailbox.organization_id == org_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Reply parent message not found.")
    parent, contact, campaign = row
    if not campaign_is_integration_managed(campaign):
        raise HTTPException(
            status_code=409, detail="The parent message does not belong to a LeadBoost-managed contact."
        )
    if (contact.email or "").strip().lower() != recipient_email.strip().lower():
        raise HTTPException(
            status_code=409, detail="recipient does not match the sender of the parent message."
        )
    _reject_if_contact_has_active_followup(contact)
    return parent, contact, campaign


def _real_message_id(value: str | None) -> str | None:
    """A Message-ID that may be written into an outgoing RFC header: present, well formed
    (``_usable_message_id``) and NOT one of Mailer's own ``synthetic:`` dedupe identities, which are
    storage keys for messages that had no Message-ID and mean nothing to a mail client."""
    usable = _usable_message_id(value)
    return None if usable is None or is_synthetic_id(usable) else usable


def _reply_parent_snapshot(parent: Message) -> dict:
    """The deterministic threading facts, fixed at acceptance (grounding_context is write-once).
    Subject and headers are protocol facts derived from the stored row -- never from the LLM.

    409 (nothing created yet) when the parent offers nothing real to thread to: no usable
    Message-ID of its own and no usable In-Reply-To of its own to fall back on. Accepting it would
    queue a dispatch the generation worker can only refuse later. Synthetic identities are dropped
    from every header, including each entry of the parent's own References chain."""
    parent_mid = _real_message_id(parent.message_id_header)
    parent_irt = _real_message_id(parent.in_reply_to_header)
    thread_to = parent_mid or parent_irt
    if thread_to is None:
        raise HTTPException(
            status_code=409,
            detail="The parent message has no usable Message-ID to thread a reply to.",
        )
    chain = [m for m in (parent.references_header or "").split() if _real_message_id(m)]
    return {
        "message_id": parent.id,
        "subject": as_reply_subject(parent.subject),
        "in_reply_to_header": thread_to,
        "references_header": _build_references_header(chain, parent_irt, parent_mid),
    }


@router.post(
    "/outreach-requests",
    response_model=LeadBoostOutreachActionAccepted,
    status_code=202,
)
def create_leadboost_generated_outreach(
    payload: LeadBoostOutreachRequestIn,
    org_id: str = Depends(get_integration_org_id),
    db: Session = Depends(get_db),
):
    """
    Accept an already-authorized LeadBoost outreach action and durably queue
    it for generation. Generates nothing and sends nothing.

    1. org_id from the API key only (fail-closed dependency); payload strictly
       validated (unknown fields -> 422).
    2. Idempotency lookup scoped to org_id: same fingerprint -> return the
       existing operation (no mutation); different -> 409.
    3. The org must have exactly one ACTIVE mailbox (none / several -> 409).
    4. get-or-create the integration Campaign, then the Contact (own short
       commits; a reused Contact with an active follow-up -> 409, nothing
       mutated). Request context is NOT written to either row.
    5. ONE commit creates the ExternalDispatch (state QUEUED, message_id NULL,
       mailbox_id, immutable generation/grounding snapshot). The unique
       (organization_id, idempotency_key) constraint is the race backstop.
    """
    email = str(payload.recipient.email)
    reply_ref = payload.in_reply_to.message_reference if payload.in_reply_to else None
    fingerprint = _compute_generated_request_fingerprint(
        external_action_id=payload.external_action_id, recipient_email=email,
        reply_to_message_reference=reply_ref,
    )

    existing = _find_dispatch(db, org_id, payload.idempotency_key)
    if existing is not None:
        logger.info(
            "Idempotent replay: org=%s idempotency_key=%s (no mutation)",
            org_id, payload.idempotency_key,
        )
        return _replay_or_conflict(existing, fingerprint)

    # A reply's parent is resolved (404/409, nothing created) before any row is written.
    reply_parent = None
    if payload.in_reply_to is not None:
        parent, parent_contact, parent_campaign = _resolve_reply_parent(
            db, org_id, payload.in_reply_to.message_reference, email
        )
        reply_parent = _reply_parent_snapshot(parent)

    # No mailbox / ambiguous mailbox -> 409 before any row is created.
    mailbox_id = _resolve_sole_active_mailbox(db, org_id)

    if reply_parent is not None:
        campaign, contact = parent_campaign, parent_contact      # the parent's own rows; nothing is created
    else:
        campaign = _get_or_create_integration_campaign(db, org_id)
        contact = _get_or_create_integration_contact(db, campaign.id, email, payload.recipient.name)
    campaign_id, contact_id = campaign.id, contact.id

    # Mailer's own conversation history (new contact -> the first-contact
    # placeholder). A plain DB read; never caller-supplied.
    transcript = build_conversation_context(db, contact)

    grounding = {
        "version": GROUNDING_CONTEXT_VERSION,
        "value_prop": payload.context.value_proposition,
        "context_notes": _format_recipient_facts(payload.context.recipient_facts),
        "conversation_transcript": transcript,
        "recipient": {
            "name": payload.recipient.name,
            "title": payload.recipient.title,
            "company": payload.recipient.company,
        },
    }
    if reply_parent is not None:
        grounding["reply_parent"] = reply_parent

    dispatch = ExternalDispatch(
        organization_id=org_id,
        idempotency_key=payload.idempotency_key,
        external_action_id=payload.external_action_id,
        correlation_id=payload.correlation_id,
        campaign_id=campaign_id,
        contact_id=contact_id,
        message_id=None,               # created later by the generation worker
        mailbox_id=mailbox_id,
        request_fingerprint=fingerprint,
        public_reference=uuid.uuid4().hex,
        state=ExternalDispatchState.QUEUED.value,
        grounding_context=grounding,
    )
    db.add(dispatch)

    try:
        db.commit()
    except IntegrityError:
        # Lost a concurrent race on (organization_id, idempotency_key):
        # converge on the winner's row.
        db.rollback()
        winner = _find_dispatch(db, org_id, payload.idempotency_key)
        if winner is None:
            raise
        logger.info(
            "Concurrent request converged on existing dispatch: org=%s idempotency_key=%s",
            org_id, payload.idempotency_key,
        )
        return _replay_or_conflict(winner, fingerprint)

    db.refresh(dispatch)
    logger.info(
        "Accepted LeadBoost generated outreach: org=%s external_action_id=%s "
        "correlation_id=%s -> dispatch=%s contact=%s mailbox=%s (generation pending)",
        org_id, payload.external_action_id, payload.correlation_id,
        dispatch.public_reference, contact_id, mailbox_id,
    )
    return _accepted(dispatch)
