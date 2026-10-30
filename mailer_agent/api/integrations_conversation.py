"""
C9.3 -- LeadBoost conversation read.

    GET /integrations/leadboost/outreach-actions/{idempotency_key}/conversation?limit=20

"What is the actual conversation for the outreach I submitted?" LeadBoost's own
OutreachAction.subject/body is only its authorization snapshot; the message the
Mailer generated and sent exists nowhere but here. This is the one read that
lets LeadBoost show it. The Mailer stays the source of truth; LeadBoost only
displays what this returns.

THIS ENDPOINT IS STRICTLY READ-ONLY. It runs SELECTs only: no INSERT / UPDATE /
DELETE, no lock (no FOR UPDATE), no claim, no lease recovery, no retry, no
state "healing", no queue item, and no call into SMTP, IMAP or an LLM. It
imports nothing from llm / followup / sender / worker (an AST test enforces it).
It is deliberately a separate module: the C9.1 reconciliation GET in
api/integrations.py is frozen and is neither imported nor modified here.

ROOT. The lookup is rooted on the idempotency_key -- the same root as C9.1: it
is unique per organization, LeadBoost stores it, and it still works when the
POST response that would have carried the public reference was lost. Rooting
on the key does NOT make the conversation per action. There is no Thread table:
a conversation is one Contact plus its Message rows, and the Contact is
get-or-created per (organization's integration Campaign, recipient email), so
several actions to the same address share one conversation. The dispatch's
contact_id is the durable link to that Contact (set at acceptance, never
guessed at read time). Subject matching, recipient-address matching and any
cross-organization search are deliberately not used.

TENANCY. org_id comes only from the authenticated API key
(get_integration_org_id -- the FAIL-CLOSED variant: with no keys configured it
is a 503, never an implicit "default" organization). Nothing from the request
body or query string names a tenant. The organization is part of the SQL
predicate of every query, never fetch-then-check. An unknown key, another
organization's key, and an integrity failure are one indistinguishable 404.

WHICH MESSAGES. Of the contact's rows, only:
  * outbound  -- rows owned by an ExternalDispatch of THIS organization for
                 THIS contact (every outbound row on an integration contact is
                 dispatch-created: native sends refuse integration campaigns);
  * inbound   -- rows with mailbox_id NOT NULL whose Mailbox belongs to THIS
                 organization (M3, bound from the polled Mailbox row, never from
                 message content).
Inbound rows with mailbox_id NULL (the webhook and the legacy deployment-global
IMAP poll) have no provable owner -- the webhook path resolves a Message-ID
without an organization filter -- so they are excluded, not tagged. Fixing that
provenance gap is a separate hardening item (C15); this endpoint only refuses
to turn it into customer-visible data.

STATE. action.state and each outbound message's delivery_state come from
ExternalDispatch.state (authoritative), in C9.1's public vocabulary
(GENERATING -> queued, UNKNOWN stays unknown). Message.status is never read:
it is a mirror written in the dispatch transaction, and presenting it would
create a second, competing source of truth. A message's existence never implies
a delivery state.

WINDOW. The most recent `limit` messages (default 20, max 50), returned oldest
first. limit + 1 rows are fetched to compute has_more; there is no cursor and
no per-message public id. Bodies are cut in SQL at BODY_CAP_CHARS (substr, so an
oversized stored body is never loaded whole) and flagged body_truncated.
Inbound text is attacker-controlled; it is returned verbatim as data and must be
rendered as plain text by the consumer.

CONSISTENCY. On PostgreSQL the read runs in one REPEATABLE READ, READ ONLY
transaction, so the dispatch state and every message come from a single
snapshot (a worker finishing a dispatch mid-request can't produce a torn view).
A snapshot read takes no row locks and cannot block FOR UPDATE SKIP LOCKED
claims. SQLite has no equivalent and runs as-is.

EXPOSED, inbound messages only: ``message_reference`` -- the opaque Message.reply_reference token
(32 lowercase hex characters, not derived from any id) that LeadBoost passes back to reply.

NOT EXPOSED: Message.id or any database id, RFC Message-ID / In-Reply-To /
References (so synthetic ids never appear), error_message (raw diagnostics),
detected_intent, semantic_analysis, grounding context, claim/lease fields,
mailbox addresses or credentials, the organization.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, aliased

from mailer_agent.api.deps import get_integration_org_id
from mailer_agent.db import get_db
from mailer_agent.models import (
    Campaign,
    Contact,
    ExternalDispatch,
    ExternalDispatchState,
    Mailbox,
    Message,
    MessageDirection,
)
from mailer_agent.schemas import (
    LeadBoostConversation,
    LeadBoostConversationAction,
    LeadBoostConversationMessage,
)

logger = logging.getLogger("mailer_agent.api.integrations_conversation")

# No router-level require_api_key: unlike the human-operated routes it would be
# open when no keys are configured, and the endpoint's own dependency
# (get_integration_org_id) already authenticates and fails closed.
router = APIRouter(prefix="/integrations/leadboost", tags=["integrations"])

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
BODY_CAP_CHARS = 20_000

# The public delivery vocabulary -- identical to C9.1's. GENERATING is an
# internal step of "accepted, not yet completed by a worker" and reads as queued.
_PUBLIC_STATES = frozenset(
    s.value for s in ExternalDispatchState if s is not ExternalDispatchState.GENERATING
)


def _public_state(internal: str) -> str | None:
    """Map ExternalDispatch.state to the public vocabulary (None if it is not a
    recognised value -- treated by the caller as an integrity failure)."""
    if internal == ExternalDispatchState.GENERATING.value:
        return ExternalDispatchState.QUEUED.value
    return internal if internal in _PUBLIC_STATES else None


def _begin_consistent_read(db: Session) -> None:
    """On PostgreSQL, pin this request to one REPEATABLE READ, READ ONLY snapshot.

    Must run before the session's first statement (isolation can't change inside
    a transaction): the request's session is fresh from get_db and no dependency
    touches the database before the handler. If a transaction is somehow already
    open, the read simply proceeds on it -- degraded to the session's existing
    isolation, never an error. SQLite (tests, local dev) has no equivalent.
    """
    if db.get_bind().dialect.name != "postgresql" or db.in_transaction():
        return
    db.connection(
        execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
    )


def _not_found() -> HTTPException:
    # One body for unknown key, other-organization key and integrity failure --
    # identical to C9.1's 404, so none of them is distinguishable.
    return HTTPException(status_code=404, detail="Outreach action not found")


@router.get(
    "/outreach-actions/{idempotency_key:path}/conversation",
    response_model=LeadBoostConversation,
)
def get_leadboost_outreach_conversation(
    idempotency_key: str,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    org_id: str = Depends(get_integration_org_id),
    db: Session = Depends(get_db),
):
    """See the module docstring. `:path` on the key is only so that a key that
    itself contains "/" (LeadBoost's are caller-suppliable, percent-encoded by
    its client) still reaches this handler after the server decodes the URL; the
    key is only ever used as a bound parameter of an equality predicate."""
    _begin_consistent_read(db)

    mailbox_for_action = aliased(Mailbox)
    root = (
        db.query(
            ExternalDispatch.contact_id,
            ExternalDispatch.public_reference,
            ExternalDispatch.state,
            ExternalDispatch.created_at,
            ExternalDispatch.updated_at,
            mailbox_for_action.public_reference.label("mailbox_reference"),
        )
        # Integrity: the dispatch's contact must exist and live in a Campaign of
        # this organization. An inner join, so a violation yields no row (404).
        .join(Contact, Contact.id == ExternalDispatch.contact_id)
        .join(
            Campaign,
            and_(Campaign.id == Contact.campaign_id, Campaign.organization_id == org_id),
        )
        .outerjoin(
            mailbox_for_action,
            and_(
                mailbox_for_action.id == ExternalDispatch.mailbox_id,
                mailbox_for_action.organization_id == org_id,
            ),
        )
        .filter(
            ExternalDispatch.organization_id == org_id,
            ExternalDispatch.idempotency_key == idempotency_key,
        )
        .first()
    )
    if root is None:
        raise _not_found()

    action_state = _public_state(root.state)
    if action_state is None:
        # An unrecognised stored state is never guessed at or passed through.
        logger.error("C9.3 read refused: unrecognised ExternalDispatch.state")
        raise _not_found()

    dispatch_for_message = aliased(ExternalDispatch)
    inbound_mailbox = aliased(Mailbox)
    outbound_mailbox = aliased(Mailbox)
    body_head = func.substr(Message.body, 1, BODY_CAP_CHARS + 1)

    rows = (
        db.query(
            Message.reply_reference.label("reply_reference"),
            Message.direction,
            Message.message_type,
            Message.subject,
            body_head.label("body_head"),
            Message.created_at,
            dispatch_for_message.state.label("dispatch_state"),
            dispatch_for_message.public_reference.label("dispatch_reference"),
            inbound_mailbox.public_reference.label("inbound_mailbox_reference"),
            outbound_mailbox.public_reference.label("outbound_mailbox_reference"),
        )
        # Outbound ownership: a dispatch of THIS organization for THIS contact
        # that produced this message.
        .outerjoin(
            dispatch_for_message,
            and_(
                dispatch_for_message.message_id == Message.id,
                dispatch_for_message.organization_id == org_id,
                dispatch_for_message.contact_id == root.contact_id,
            ),
        )
        # Inbound ownership: the receiving mailbox belongs to THIS organization.
        .outerjoin(
            inbound_mailbox,
            and_(
                inbound_mailbox.id == Message.mailbox_id,
                inbound_mailbox.organization_id == org_id,
            ),
        )
        .outerjoin(
            outbound_mailbox,
            and_(
                outbound_mailbox.id == dispatch_for_message.mailbox_id,
                outbound_mailbox.organization_id == org_id,
            ),
        )
        .filter(
            Message.contact_id == root.contact_id,
            or_(
                and_(
                    Message.direction == MessageDirection.OUTBOUND.value,
                    dispatch_for_message.id.isnot(None),
                ),
                and_(
                    Message.direction == MessageDirection.INBOUND.value,
                    Message.mailbox_id.isnot(None),
                    inbound_mailbox.id.isnot(None),
                ),
            ),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit + 1)
        .all()
    )

    has_more = len(rows) > limit
    window = list(reversed(rows[:limit]))  # most recent `limit`, oldest first

    messages: list[LeadBoostConversationMessage] = []
    for r in window:
        outbound = r.direction == MessageDirection.OUTBOUND.value
        delivery_state = None
        if outbound:
            delivery_state = _public_state(r.dispatch_state)
            if delivery_state is None:
                logger.error("C9.3 read refused: unrecognised ExternalDispatch.state on a message")
                raise _not_found()
        body = r.body_head or ""
        truncated = len(body) > BODY_CAP_CHARS
        messages.append(
            LeadBoostConversationMessage(
                direction=r.direction,
                message_type=r.message_type,
                subject=r.subject,
                body=body[:BODY_CAP_CHARS],
                body_truncated=truncated,
                created_at=r.created_at,
                delivery_state=delivery_state,
                mailing_agent_reference=r.dispatch_reference if outbound else None,
                mailbox_reference=(
                    r.outbound_mailbox_reference if outbound else r.inbound_mailbox_reference
                ),
                message_reference=None if outbound else r.reply_reference,
            )
        )

    return LeadBoostConversation(
        action=LeadBoostConversationAction(
            state=action_state,
            mailing_agent_reference=root.public_reference,
            created_at=root.created_at,
            updated_at=root.updated_at,
            mailbox_reference=root.mailbox_reference,
        ),
        messages=messages,
        has_more=has_more,
    )
