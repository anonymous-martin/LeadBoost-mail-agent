"""
Durable generation of LeadBoost generated-outreach messages (M2-B).

POST /integrations/leadboost/outreach-requests commits an ExternalDispatch
(QUEUED, message_id NULL, request context in the immutable grounding_context
snapshot) and answers 202 without any LLM call. This worker, in the worker
process and never inside an HTTP request, turns that row into the ONE Message
the existing C6-C8 dispatch worker then sends:

    QUEUED (message_id NULL) --claim--> GENERATING --LLM--> QUEUED (message set)
                                                    \\-----> FAILED (no Message)

Transactions -- never open across the LLM call
  G1 (own session, ONE commit)  claim -> GENERATING, load Campaign/Contact with
                                tenant-scoped predicates, read the snapshot.
                                Session CLOSED. A crash before this commit
                                leaves the row QUEUED.
  --- no session, no transaction, no lock from here ---
  LLM                           llm.agent.draft_message() (which also runs the
                                existing grounding validator), fed ONLY the
                                snapshot, on transient never-persisted
                                Campaign/Contact objects.
  G2 (new session, ONE commit)  insert Message + fenced GENERATING -> QUEUED
                                (message_id set), or fenced GENERATING ->
                                FAILED. Fence = id, org, state, claimed_by,
                                exact claimed_at, message_id NULL: a worker
                                that lost its lease rolls its Message back, so
                                at most one Message ever exists per dispatch.

Failure semantics
  LLM/provider error, or a draft the grounding validator hard-blocks -> FAILED,
  no Message, nothing sent (a retry is a new idempotency key, as for any FAILED
  dispatch). The same body is validated again at send time by the dispatch
  worker. Crash or lease expiry during generation -> the sweep returns the row
  to QUEUED and it is regenerated: safe because generation has no external
  side effect (contrast SENDING, which resolves to UNKNOWN).

Sending, mailbox use and every C6-C8 delivery/lease invariant stay in
mail/external_dispatch_worker.py, which still imports no LLM code. This module
is the only place the generated-outreach LLM call happens after acceptance.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mailer_agent.config import get_settings
from mailer_agent.followup.work_claiming import (
    ClaimedDispatch,
    claim_next_generation_dispatch,
    complete_generation,
    fail_generation,
    make_worker_id,
    recover_expired_generations,
)
from mailer_agent.llm.agent import draft_message
from mailer_agent.mail.exact_message import create_authorized_message
from mailer_agent.mail.external_dispatch_worker import DispatchRuntime, default_runtime
from mailer_agent.models import Campaign, Contact, ExternalDispatch, MessageType
from mailer_agent.worker_status import CycleReport

logger = logging.getLogger("mailer_agent.mail.outreach_generation_worker")
settings = get_settings()

SessionFactory = Callable[[], Session]

_TXN_G2_ATTEMPTS = 3
_TXN_G2_RETRY_DELAY_SECONDS = 0.2


@dataclass(frozen=True)
class GenerationResult:
    dispatch_id: int
    organization_id: str
    outcome: str                  # "generated" | "failed" | "lease_lost"
    reason: Optional[str]
    persisted: bool               # False: the outcome could NOT be written (fence lost / G2 failed)


@dataclass(frozen=True)
class _Prepared:
    """Plain values only -- nothing lazy-loads through a closed session."""
    claim: ClaimedDispatch
    contact_id: int
    email: str
    sender: dict
    value_prop: str
    context_notes: Optional[str]
    transcript: Optional[str]
    recipient_name: Optional[str]
    recipient_title: Optional[str]
    recipient_company: Optional[str]
    # Threading facts snapshotted at acceptance for a reply (None -> initial outreach).
    reply_parent: Optional[dict] = None


class _Refusal(Exception):
    """A deterministic pre-LLM refusal. Becomes FAILED; the LLM is never called."""


_REPLY_OBJECTIVE = (
    "Reply to the prospect's most recent message in this conversation. Answer only what they "
    "wrote, using verified facts. Do not state or imply that any meeting time is booked, "
    "available or confirmed; if they proposed times, acknowledge them and say the sender will "
    "confirm."
)


def _valid_reply_parent(value) -> Optional[dict]:
    """None for an absent snapshot; a refusal (never a silent downgrade to initial outreach)
    for a present but malformed one."""
    if value is None:
        return None
    if not (isinstance(value, dict) and isinstance(value.get("message_id"), int)
            and isinstance(value.get("subject"), str) and value["subject"]
            and isinstance(value.get("in_reply_to_header"), str) and value["in_reply_to_header"]):
        raise _Refusal("generation_context_invalid: reply parent snapshot is malformed")
    return value


def _default_session_factory() -> SessionFactory:
    from mailer_agent.db import SessionLocal  # lazy: keeps import side-effect free

    return SessionLocal


# ---------------------------------------------------------------------------
# Transaction G1
# ---------------------------------------------------------------------------

def _prepare(db: Session, claim: ClaimedDispatch) -> _Prepared:
    dispatch = db.get(ExternalDispatch, claim.dispatch_id, populate_existing=True)
    snap = dispatch.grounding_context
    if not isinstance(snap, dict) or not isinstance(snap.get("value_prop"), str) or not snap["value_prop"]:
        raise _Refusal("generation_context_missing: dispatch has no usable grounding snapshot")
    recipient = snap.get("recipient") or {}
    if not isinstance(recipient, dict):
        raise _Refusal("generation_context_invalid: recipient snapshot is malformed")

    campaign = db.execute(
        select(Campaign).where(
            Campaign.id == dispatch.campaign_id,
            Campaign.organization_id == dispatch.organization_id,
        )
    ).scalar_one_or_none()
    if campaign is None:
        raise _Refusal("tenant_mismatch: campaign not found for this organization")
    contact = db.execute(
        select(Contact).where(Contact.id == dispatch.contact_id, Contact.campaign_id == campaign.id)
    ).scalar_one_or_none()
    if contact is None:
        raise _Refusal("tenant_mismatch: contact does not belong to the dispatch's campaign")

    return _Prepared(
        claim=claim,
        contact_id=contact.id,
        email=contact.email,
        sender={
            "sender_name": campaign.sender_name,
            "sender_org": campaign.sender_org,
            "sender_email": campaign.sender_email,
            "sender_title": campaign.sender_title,
            "tone": campaign.tone,
        },
        value_prop=snap["value_prop"],
        context_notes=snap.get("context_notes"),
        transcript=snap.get("conversation_transcript"),
        recipient_name=recipient.get("name"),
        recipient_title=recipient.get("title"),
        recipient_company=recipient.get("company"),
        reply_parent=_valid_reply_parent(snap.get("reply_parent")),
    )


def _transaction_g1(
    factory: SessionFactory, worker_id: str, runtime: DispatchRuntime
) -> "None | GenerationResult | _Prepared":
    db = factory()
    try:
        if runtime.stopping:
            return None
        claim = claim_next_generation_dispatch(db, worker_id)
        if claim is None:
            db.rollback()
            return None
        try:
            prepared = _prepare(db, claim)
        except _Refusal as refusal:
            won = fail_generation(db, claim, str(refusal))
            db.commit()
            logger.warning(
                "external_dispatch generation refused dispatch_id=%d org=%s reason=%s",
                claim.dispatch_id, claim.organization_id, refusal,
            )
            return GenerationResult(claim.dispatch_id, claim.organization_id, "failed", str(refusal), won)
        if runtime.stopping:
            db.rollback()          # leave it QUEUED; do not start an LLM call during shutdown
            return None
        db.commit()                # <- GENERATING durable
        return prepared
    finally:
        db.close()                 # nothing survives past G1


# ---------------------------------------------------------------------------
# LLM (no session, no transaction)
# ---------------------------------------------------------------------------

def _generate(prepared: _Prepared):
    """Returns (draft, None) or (None, failure_reason)."""
    gen_campaign = Campaign(
        name="LeadBoost generated outreach (transient)",
        value_prop=prepared.value_prop,
        proof_points=None,
        **prepared.sender,
    )
    gen_contact = Contact(
        name=prepared.recipient_name,
        email=prepared.email,
        title=prepared.recipient_title,
        company=prepared.recipient_company,
        context_notes=prepared.context_notes,
        follow_up_index=0,
    )
    try:
        draft = draft_message(
            campaign=gen_campaign,
            contact=gen_contact,
            action_type="reply" if prepared.reply_parent else "initial_outreach",
            context_transcript=prepared.transcript,
            **({"planner_objective": _REPLY_OBJECTIVE} if prepared.reply_parent else {}),
        )
    except Exception as exc:  # noqa: BLE001 -- provider outage etc.; nothing was queued for send
        logger.exception("external_dispatch generation failed dispatch_id=%d", prepared.claim.dispatch_id)
        return None, f"generation_failed: {type(exc).__name__}"
    if draft.grounding is None or draft.grounding.hard_block:
        notes = draft.grounding.validation_notes if draft.grounding else "no grounding result"
        return None, f"generation_grounding_blocked: {notes}"
    return draft, None


# ---------------------------------------------------------------------------
# Transaction G2
# ---------------------------------------------------------------------------

def _transaction_g2(factory: SessionFactory, prepared: _Prepared, draft, failure: Optional[str]) -> Optional[bool]:
    """Fenced outcome write. True = written, False = fence lost, None = DB failed."""
    claim = prepared.claim
    for attempt in range(1, _TXN_G2_ATTEMPTS + 1):
        db = factory()
        try:
            if failure is not None:
                won = fail_generation(db, claim, failure)
            else:
                message = create_authorized_message(
                    contact_id=prepared.contact_id, subject=draft.subject, body=draft.body
                )
                if prepared.reply_parent:
                    # Subject and threading are protocol facts from the stored parent, never the model's.
                    p = prepared.reply_parent
                    message.message_type = MessageType.REPLY.value
                    message.subject = p["subject"]
                    message.in_reply_to_header = p["in_reply_to_header"]
                    message.references_header = p.get("references_header")
                db.add(message)
                db.flush()
                won = complete_generation(db, claim, message.id)
            if not won:
                db.rollback()      # a lost fence discards the Message insert: no second Message, ever
                return False
            db.commit()
            return True
        except SQLAlchemyError:
            db.rollback()
            logger.exception(
                "external_dispatch Transaction G2 failed (attempt %d/%d) dispatch_id=%d",
                attempt, _TXN_G2_ATTEMPTS, claim.dispatch_id,
            )
            if attempt < _TXN_G2_ATTEMPTS:
                time.sleep(_TXN_G2_RETRY_DELAY_SECONDS)
        finally:
            db.close()
    return None


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def process_next_generation(
    *,
    session_factory: Optional[SessionFactory] = None,
    worker_id: Optional[str] = None,
    runtime: Optional[DispatchRuntime] = None,
) -> Optional[GenerationResult]:
    """Claim and fully process ONE accepted-but-ungenerated dispatch; None if there was none."""
    factory = session_factory or _default_session_factory()
    worker_id = worker_id or make_worker_id()
    runtime = runtime or default_runtime

    prepared = _transaction_g1(factory, worker_id, runtime)
    if prepared is None or isinstance(prepared, GenerationResult):
        return prepared

    claim = prepared.claim
    draft, failure = _generate(prepared)          # NO session or transaction is open here
    written = _transaction_g2(factory, prepared, draft, failure)

    if written:
        outcome, reason = ("failed", failure) if failure else ("generated", None)
        logger.info(
            "external_dispatch generation %s dispatch_id=%d org=%s%s",
            outcome, claim.dispatch_id, claim.organization_id, f" reason={failure}" if failure else "",
        )
        return GenerationResult(claim.dispatch_id, claim.organization_id, outcome, reason, True)
    logger.error(
        "external_dispatch generation OUTCOME NOT PERSISTED dispatch_id=%d org=%s (%s); "
        "lease recovery will return it to QUEUED and it will be regenerated",
        claim.dispatch_id, claim.organization_id,
        "lease lost" if written is False else "database failure",
    )
    return GenerationResult(
        claim.dispatch_id, claim.organization_id, "lease_lost", failure, False
    )


def run_generation_cycle(
    *,
    session_factory: Optional[SessionFactory] = None,
    worker_id: Optional[str] = None,
    runtime: Optional[DispatchRuntime] = None,
    max_items: Optional[int] = None,
    report: Optional[CycleReport] = None,
) -> list[GenerationResult]:
    """One polling cycle: claim -> generate, one dispatch at a time. A database error ends the cycle.

    The error is caught and logged here, so the list alone cannot tell "no work" from "aborted";
    a caller that needs the difference passes a ``CycleReport`` (``report.aborted``). Behaviour and
    return value are unchanged either way.
    """
    runtime = runtime or default_runtime
    limit = max_items if max_items is not None else settings.external_dispatch_max_per_cycle
    results: list[GenerationResult] = []
    for _ in range(limit):
        if runtime.stopping:
            break
        try:
            result = process_next_generation(
                session_factory=session_factory, worker_id=worker_id, runtime=runtime
            )
        except SQLAlchemyError:
            logger.exception("external_dispatch generation cycle aborted by a database error")
            if report is not None:
                report.aborted = True
            break
        if result is None:
            break
        results.append(result)
    return results


def run_generation_lease_recovery(*, session_factory: Optional[SessionFactory] = None) -> int:
    """Expired GENERATING -> QUEUED sweep. Never sends, never calls the LLM."""
    factory = session_factory or _default_session_factory()
    db = factory()
    try:
        recovered = recover_expired_generations(db)
        db.commit()
        return len(recovered)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("external_dispatch generation lease recovery failed")
        return 0
    finally:
        db.close()
