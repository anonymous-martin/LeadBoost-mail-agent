"""
Async execution of queued ExternalDispatch rows (LeadBoost integration,
C6-C8).

The HTTP endpoint (api/integrations.py) has already committed one
``Message`` (DRAFT) and one ``ExternalDispatch`` (QUEUED) and answered
"accepted". Everything here happens later, in the worker process, and NEVER
inside an HTTP request.

What this is not
----------------
It does not claim exactly-once delivery. Raw SMTP cannot give that across a
process crash. It guarantees only: no *automatic* second attempt after an
ambiguous outcome, a Message-ID that is durable before transmission, and
honest states (queued / sending / sent / failed / unknown are never
interchangeable).

Transactions -- explicit, and never open across SMTP
----------------------------------------------------
  Transaction B  (own session; ONE commit)
      claim QUEUED -> SENDING (FOR UPDATE SKIP LOCKED on PostgreSQL)
      load Campaign / Contact / Message with tenant-scoped predicates
      validate wiring -> mailbox (M2-A: present, same org, ACTIVE) ->
        suppression -> grounding -> LIVE_SENDING_ENABLED
      decrypt the mailbox SMTP password for this operation (M2-A);
        an unset/invalid MAILBOX_ENCRYPTION_KEY rolls back and leaves the row
        QUEUED (deployment problem); an undecryptable token is FAILED
      mint + persist Message.message_id_header, Message.status = SENDING
      COMMIT, then the session is CLOSED.
      Any gate failure ends here as FAILED (QUEUED -> SENDING -> FAILED
      inside this one transaction, so a gate-failed row is never visibly
      SENDING). A crash/rollback anywhere before this commit leaves the row
      QUEUED and untouched -- SMTP has not happened.
  --- no session, no transaction, no lock is held from here ---
  SMTP           send_email(..., message_id_header=<the persisted value>)
  --- ---
  Transaction C  (new session; ONE commit)
      ownership-fenced write of SENT / FAILED / UNKNOWN and clear
      claimed_by/claimed_at (work_claiming.finish_external_dispatch).

Consequence: a *committed* SENDING row always has every gate passed and the
Message-ID durable, and is indistinguishable, after a crash, between "died
before SMTP", "died during SMTP" and "SMTP accepted, died before C". That is
exactly why an expired SENDING lease resolves to UNKNOWN and never QUEUED
(models.resolve_expired_sending_lease).

Failure matrix (state left behind -> what happens next)
-------------------------------------------------------
  crash before B commits ......... QUEUED; claimable again.
  crash after B, before/during SMTP, or after SMTP accepts, before C
                                   SENDING (stale) -> lease sweep -> UNKNOWN.
  SMTP ambiguous ................. UNKNOWN. Never resent.
  SMTP definite failure .......... FAILED. Not auto-requeued (see below).
  send_email() raises ............ UNKNOWN (we cannot know what it did).
  C commit fails repeatedly ...... row stays SENDING -> lease sweep -> UNKNOWN.
  lease lost, then SMTP succeeds . row is already UNKNOWN; C's fence matches
                                   nothing and writes nothing; the observed
                                   outcome is logged and appended to
                                   error_message. State is NOT rewritten.

FAILED is terminal here. sender.SendOutcome.FAILED means "did not send", but
this layer only receives a free-text error, not a positively classified
retry-safe cause, so it does not requeue. A retry is a new, explicit decision
(a later stage / the caller with a new idempotency key), not a loop here.

Import boundary (enforced by tests/test_external_dispatch_boundary.py)
----------------------------------------------------------------------
No message generation and no LLM: nothing here imports ``llm.agent``,
``llm.provider*``, ``memory.store``, ``followup.engine*`` or any LLM client.
It may import ``mail.sender`` (it *is* the sender's caller) and
``llm.grounding`` (regex only, via exact_message). The suppression lookup
lives in the sender-free ``mailer_agent.suppression``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mailer_agent.config import get_settings
from mailer_agent.followup.work_claiming import (
    ClaimedDispatch,
    annotate_unknown_dispatch,
    claim_next_external_dispatch,
    finish_external_dispatch,
    make_worker_id,
    recover_expired_external_dispatches,
)
from mailer_agent.mail.exact_message import (
    ExactMessageError,
    build_exact_send_input,
    evaluate_exact_message_grounding,
)
from mailer_agent.mail.sender import SendOutcome, SmtpConfig, generate_message_id, send_email
from mailer_agent.mailbox_secrets import (
    MailboxDecryptionError,
    MailboxEncryptionUnavailable,
    decrypt_secret,
)
from mailer_agent.models import (
    Campaign,
    Contact,
    ExternalDispatch,
    ExternalDispatchState as S,
    Mailbox,
    MailboxStatus,
    Message,
    MessageStatus,
)
from mailer_agent.suppression import is_email_suppressed
from mailer_agent.worker_status import CycleReport

logger = logging.getLogger("mailer_agent.mail.external_dispatch_worker")
settings = get_settings()

SessionFactory = Callable[[], Session]

_TXN_C_ATTEMPTS = 3
_TXN_C_RETRY_DELAY_SECONDS = 0.2


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchResult:
    dispatch_id: int
    organization_id: str
    outcome: str                 # "sent" | "failed" | "unknown"
    reason: Optional[str]
    smtp_attempted: bool
    persisted: bool              # False: the outcome could NOT be written (fence lost / C failed)
    message_id_header: Optional[str] = None


@dataclass(frozen=True)
class ShutdownReport:
    drained: bool
    abandoned_unknown: tuple[int, ...]
    still_owned_elsewhere: tuple[int, ...]


# ---------------------------------------------------------------------------
# Shutdown bookkeeping (a courtesy, NOT a safety mechanism)
# ---------------------------------------------------------------------------

class DispatchRuntime:
    """
    Process-local record of which dispatches THIS process is working on, plus
    a "stop claiming" flag. It exists only so a graceful shutdown can (a)
    stop taking new work, (b) wait a bounded time for in-flight work, and
    (c) resolve exactly the dispatches this process still owns -- by ID and
    claim token, never by sweeping on worker_id. All correctness guarantees
    stay in the database (claim, fence, lease recovery); if this state is lost
    (SIGKILL) nothing is unsafe, the lease sweep handles it.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._inflight: dict[int, ClaimedDispatch] = {}
        self._claim_attempts = 0
        self._stopping = threading.Event()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def request_shutdown(self) -> None:
        with self._cond:
            self._stopping.set()
            self._cond.notify_all()

    def begin_claim_attempt(self) -> bool:
        """Admit a claim attempt atomically with respect to shutdown."""
        with self._cond:
            if self._stopping.is_set():
                return False
            self._claim_attempts += 1
            return True

    def end_claim_attempt(self) -> None:
        """Release a claim attempt that did not transfer into in-flight work."""
        with self._cond:
            if self._claim_attempts <= 0:
                raise RuntimeError(
                    "No active external-dispatch claim attempt to release"
                )
            self._claim_attempts -= 1
            self._cond.notify_all()

    def register_claim_attempt(self, claim: ClaimedDispatch) -> None:
        """Atomically transfer one active attempt into the in-flight set."""
        with self._cond:
            if self._claim_attempts <= 0:
                raise RuntimeError(
                    "External-dispatch claim attempt was not admitted"
                )
            self._inflight[claim.dispatch_id] = claim
            self._claim_attempts -= 1
            self._cond.notify_all()

    def resume_if_idle(self) -> bool:
        """Resume only when no prior dispatch or claim attempt remains active."""
        with self._cond:
            if self._inflight or self._claim_attempts:
                return False
            self._stopping.clear()
            return True

    def register(self, claim: ClaimedDispatch) -> None:
        with self._cond:
            self._inflight[claim.dispatch_id] = claim

    def unregister(self, dispatch_id: int) -> None:
        with self._cond:
            self._inflight.pop(dispatch_id, None)
            self._cond.notify_all()

    def inflight(self) -> list[ClaimedDispatch]:
        with self._cond:
            return list(self._inflight.values())

    def drain(self, timeout: float) -> bool:
        """Wait until nothing is in flight; False if `timeout` elapsed first."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._inflight or self._claim_attempts:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def reset(self) -> None:
        """Test helper: forget everything and allow claiming again."""
        with self._cond:
            self._inflight.clear()
        self._stopping.clear()


default_runtime = DispatchRuntime()


def _default_session_factory() -> SessionFactory:
    from mailer_agent.db import SessionLocal  # lazy: keeps import side-effect free

    return SessionLocal


# ---------------------------------------------------------------------------
# Internal control-flow helpers
# ---------------------------------------------------------------------------

class _Gate(Exception):
    """A deterministic pre-SMTP refusal. Becomes FAILED; SMTP is never reached."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Deferred(Exception):
    """Shutdown was requested before B committed: roll back, leave QUEUED."""


class _Unavailable(Exception):
    """A deployment-level precondition is missing (MAILBOX_ENCRYPTION_KEY
    unset/invalid). Not a property of this dispatch, so it is NOT failed:
    roll back, leave QUEUED, and retry once the deployment is fixed."""


@dataclass(frozen=True)
class _Ctx:
    """Correlation identifiers for logging. Never credentials, never bodies."""
    dispatch_id: int
    organization_id: str
    external_action_id: Optional[str]
    correlation_id: Optional[str]
    public_reference: str
    worker_id: str

    def __str__(self) -> str:
        return (
            f"dispatch_id={self.dispatch_id} org={self.organization_id} "
            f"external_action_id={self.external_action_id} "
            f"correlation_id={self.correlation_id} "
            f"public_reference={self.public_reference} worker_id={self.worker_id}"
        )


@dataclass(frozen=True)
class _Prepared:
    """Everything SMTP + Transaction C need. Plain values only -- no ORM
    objects, so nothing can lazy-load through a session after B closes."""
    claim: ClaimedDispatch
    ctx: _Ctx
    send_kwargs: dict
    message_id: str


def _ctx_from(dispatch: ExternalDispatch, worker_id: str) -> _Ctx:
    return _Ctx(
        dispatch_id=dispatch.id,
        organization_id=dispatch.organization_id,
        external_action_id=dispatch.external_action_id,
        correlation_id=dispatch.correlation_id,
        public_reference=dispatch.public_reference,
        worker_id=worker_id,
    )


# ---------------------------------------------------------------------------
# Transaction B
# ---------------------------------------------------------------------------

def _load_tenant_scoped(db: Session, dispatch: ExternalDispatch):
    """
    Load the dispatch's Campaign/Contact/Message and prove they belong
    together AND to the dispatch's organization. Every predicate is in the
    query itself (never fetch-then-check), and nothing is trusted from
    external_action_id. Any mismatch is a hard, non-retried refusal.
    """
    campaign = db.execute(
        select(Campaign).where(
            Campaign.id == dispatch.campaign_id,
            Campaign.organization_id == dispatch.organization_id,
        )
    ).scalar_one_or_none()
    if campaign is None:
        raise _Gate("tenant_mismatch: campaign not found for this organization")
    contact = db.execute(
        select(Contact).where(
            Contact.id == dispatch.contact_id,
            Contact.campaign_id == campaign.id,
        )
    ).scalar_one_or_none()
    if contact is None:
        raise _Gate("tenant_mismatch: contact does not belong to the dispatch's campaign")
    message = db.execute(
        select(Message).where(
            Message.id == dispatch.message_id,
            Message.contact_id == contact.id,
        )
    ).scalar_one_or_none()
    if message is None:
        raise _Gate("tenant_mismatch: message does not belong to the dispatch's contact")
    return campaign, contact, message


def _load_active_mailbox(db: Session, dispatch: ExternalDispatch) -> Mailbox:
    """
    M2-A: the dispatch's Mailer-owned mailbox, proven to belong to the
    dispatch's organization (predicate in the query) and to be ACTIVE *now*.
    ACTIVE is evaluated here, at claim time; a mailbox disabled after
    acceptance fails the dispatch before SMTP. Once B has committed SENDING,
    disabling a mailbox does not interrupt that in-flight send. A missing,
    foreign or disabled mailbox is a hard, non-retried refusal.
    """
    if dispatch.mailbox_id is None:
        raise _Gate("no_mailbox: dispatch has no mailbox; not sent")
    mailbox = db.execute(
        select(Mailbox).where(
            Mailbox.id == dispatch.mailbox_id,
            Mailbox.organization_id == dispatch.organization_id,
        )
    ).scalar_one_or_none()
    if mailbox is None:
        raise _Gate("tenant_mismatch: mailbox not found for this organization")
    if mailbox.status != MailboxStatus.ACTIVE.value:
        raise _Gate("mailbox_disabled: mailbox is not ACTIVE; not sent")
    return mailbox


def _smtp_config_for(mailbox: Mailbox) -> SmtpConfig:
    """Decrypt the SMTP password for THIS operation only (IMAP is not touched)."""
    try:
        password = decrypt_secret(mailbox.smtp_password_enc)
    except MailboxEncryptionUnavailable as exc:
        raise _Unavailable(str(exc)) from exc
    except MailboxDecryptionError as exc:
        raise _Gate(
            "mailbox_credentials_undecryptable: stored SMTP credential cannot be "
            "decrypted with the configured key; not sent"
        ) from exc
    return SmtpConfig(
        host=mailbox.smtp_host,
        port=mailbox.smtp_port,
        use_tls=mailbox.smtp_use_tls,
        username=mailbox.smtp_username,
        password=password,
    )


def _fail_in_b(db: Session, claim: ClaimedDispatch, ctx: _Ctx, reason: str) -> DispatchResult:
    won = finish_external_dispatch(db, claim, new_state=S.FAILED, error_message=reason)
    db.commit()
    logger.warning(
        "external_dispatch QUEUED -> SENDING -> FAILED (no SMTP) %s reason=%s", ctx, reason
    )
    return DispatchResult(
        dispatch_id=claim.dispatch_id,
        organization_id=claim.organization_id,
        outcome="failed",
        reason=reason,
        smtp_attempted=False,
        persisted=won,
    )


def _transaction_b(
    factory: SessionFactory, worker_id: str, runtime: DispatchRuntime
) -> "None | DispatchResult | _Prepared":
    claim: Optional[ClaimedDispatch] = None
    handed_off = False
    claim_attempt_open = False
    db: Optional[Session] = None
    try:
        # Count the attempt before any database operation. Shutdown must see
        # this attempt even when it has not yet registered a claimed dispatch.
        if not runtime.begin_claim_attempt():
            return None
        claim_attempt_open = True

        db = factory()
        if runtime.stopping:
            return None
        claim = claim_next_external_dispatch(db, worker_id)
        if claim is None:
            db.rollback()
            return None
        runtime.register_claim_attempt(claim)
        claim_attempt_open = False

        dispatch = db.get(ExternalDispatch, claim.dispatch_id, populate_existing=True)
        ctx = _ctx_from(dispatch, worker_id)
        logger.info("external_dispatch QUEUED -> SENDING (claimed) %s", ctx)

        try:
            campaign, contact, message = _load_tenant_scoped(db, dispatch)
            try:
                send_input = build_exact_send_input(message, contact, campaign)
            except ExactMessageError as exc:
                raise _Gate(f"wiring_error: {exc}") from exc

            mailbox = _load_active_mailbox(db, dispatch)
            # The mailbox is the sending identity: From (and the Message-ID
            # domain) is its address, not the deployment-level campaign sender.
            send_input = replace(send_input, from_email=mailbox.email_address)

            if is_email_suppressed(db, contact.email):
                raise _Gate("suppressed: recipient is on the suppression list; not sent")

            grounding = evaluate_exact_message_grounding(
                message, contact, campaign, dispatch.grounding_context
            )
            if grounding.blocked:
                raise _Gate(grounding.error_message or "grounding hard block; not sent")
            if grounding.review_required:
                # Same as the legacy approval gate: LeadBoost's APPROVED state
                # is the authorization; surfaced, not blocking, message untouched.
                logger.info("external_dispatch review_required content passed through %s", ctx)

            if not settings.live_sending_enabled:
                # send_email() in dry-run returns SENT without transmitting.
                # It must never be reached, or a simulated send would be
                # recorded as delivered.
                raise _Gate(
                    "live_sending_disabled: LIVE_SENDING_ENABLED is not true; "
                    "the message was NOT sent"
                )

            smtp_config = _smtp_config_for(mailbox)

            message_id = generate_message_id(send_input.from_email)
            message.message_id_header = message_id
            message.status = MessageStatus.SENDING.value
            if runtime.stopping:
                raise _Deferred()
            db.flush()
        except _Gate as gate:
            return _fail_in_b(db, claim, ctx, gate.reason)
        except (SQLAlchemyError, _Deferred, _Unavailable):
            raise
        except Exception as exc:  # noqa: BLE001 -- deterministic pre-SMTP bug/data error
            logger.exception("external_dispatch internal error before SMTP %s", ctx)
            return _fail_in_b(
                db, claim, ctx, f"internal_error_before_send: {type(exc).__name__}: {exc}"
            )

        db.commit()  # <- the single Transaction B commit: SENDING + Message-ID durable
        logger.info(
            "external_dispatch SENDING committed, message_id=%s %s", message_id, ctx
        )
        handed_off = True
        return _Prepared(
            claim=claim,
            ctx=ctx,
            send_kwargs={**send_input.as_send_email_kwargs(), "smtp_config": smtp_config},
            message_id=message_id,
        )
    except _Deferred:
        db.rollback()
        logger.info("external_dispatch deferred by shutdown; left QUEUED")
        return None
    except _Unavailable as exc:
        db.rollback()
        logger.error(
            "external_dispatch left QUEUED: deployment cannot decrypt mailbox credentials (%s)", exc
        )
        return None
    finally:
        if claim_attempt_open:
            runtime.end_claim_attempt()
        if claim is not None and not handed_off:
            runtime.unregister(claim.dispatch_id)
        if db is not None:
            db.close()  # nothing survives past B


# ---------------------------------------------------------------------------
# SMTP + Transaction C
# ---------------------------------------------------------------------------

def _transaction_c(
    factory: SessionFactory, prepared: _Prepared, new_state: S, error: Optional[str]
) -> bool:
    """Fenced outcome write. True = written. False = fence lost or DB failed."""
    claim = prepared.claim
    for attempt in range(1, _TXN_C_ATTEMPTS + 1):
        db = factory()
        try:
            won = finish_external_dispatch(db, claim, new_state=new_state, error_message=error)
            db.commit()
            return won
        except SQLAlchemyError:
            db.rollback()
            logger.exception(
                "external_dispatch Transaction C failed (attempt %d/%d) %s",
                attempt, _TXN_C_ATTEMPTS, prepared.ctx,
            )
            if attempt < _TXN_C_ATTEMPTS:
                time.sleep(_TXN_C_RETRY_DELAY_SECONDS)
        finally:
            db.close()
    return False


def _annotate_late_outcome(
    factory: SessionFactory, prepared: _Prepared, outcome: str, error: Optional[str]
) -> None:
    note = (
        f"late_outcome_after_lease_loss: worker {prepared.claim.worker_id} observed "
        f"SMTP outcome={outcome} message_id={prepared.message_id}"
        + (f" error={error}" if error else "")
        + "; state intentionally not changed"
    )
    db = factory()
    try:
        annotate_unknown_dispatch(
            db,
            dispatch_id=prepared.claim.dispatch_id,
            organization_id=prepared.claim.organization_id,
            note=note,
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("could not annotate late outcome %s", prepared.ctx)
    finally:
        db.close()


def _execute_and_finalize(factory: SessionFactory, prepared: _Prepared) -> DispatchResult:
    ctx = prepared.ctx
    smtp_error: Optional[str] = None
    try:
        # NO session or transaction is open here.
        result = send_email(**prepared.send_kwargs, message_id_header=prepared.message_id)
    except Exception as exc:  # noqa: BLE001
        # send_email() reports failures through SendResult; an exception means
        # we cannot know how far it got. Ambiguous -> UNKNOWN, never FAILED.
        outcome = SendOutcome.UNKNOWN
        smtp_error = f"send_email raised {type(exc).__name__}: {exc}"
        logger.exception("external_dispatch send_email raised %s", ctx)
    else:
        outcome = result.outcome
        smtp_error = result.error
        if result.message_id != prepared.message_id:
            # Should be impossible now; if it ever happens the stored and
            # transmitted IDs differ and an operator must know.
            logger.error(
                "external_dispatch MESSAGE-ID MISMATCH stored=%s returned=%s %s",
                prepared.message_id, result.message_id, ctx,
            )

    new_state = {
        SendOutcome.SENT: S.SENT,
        SendOutcome.FAILED: S.FAILED,
        SendOutcome.UNKNOWN: S.UNKNOWN,
    }[outcome]
    error = None if new_state is S.SENT else (smtp_error or f"smtp outcome {outcome.value}")

    persisted = _transaction_c(factory, prepared, new_state, error)
    if persisted:
        logger.info(
            "external_dispatch SENDING -> %s message_id=%s %s",
            new_state.value.upper(), prepared.message_id, ctx,
        )
    else:
        logger.error(
            "external_dispatch OUTCOME NOT PERSISTED outcome=%s message_id=%s %s -- claim was "
            "no longer owned (lease recovery/shutdown resolved it) or Transaction C failed; "
            "row remains/was resolved as UNKNOWN, never rewritten",
            new_state.value, prepared.message_id, ctx,
        )
        _annotate_late_outcome(factory, prepared, new_state.value, smtp_error)

    return DispatchResult(
        dispatch_id=prepared.claim.dispatch_id,
        organization_id=prepared.claim.organization_id,
        outcome=new_state.value,
        reason=error,
        smtp_attempted=True,
        persisted=persisted,
        message_id_header=prepared.message_id,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def process_next_external_dispatch(
    *,
    session_factory: Optional[SessionFactory] = None,
    worker_id: Optional[str] = None,
    runtime: Optional[DispatchRuntime] = None,
) -> Optional[DispatchResult]:
    """Claim and fully process ONE queued dispatch; None if there was none."""
    factory = session_factory or _default_session_factory()
    worker_id = worker_id or make_worker_id()
    runtime = runtime or default_runtime

    prepared = _transaction_b(factory, worker_id, runtime)
    if prepared is None or isinstance(prepared, DispatchResult):
        return prepared
    try:
        return _execute_and_finalize(factory, prepared)
    finally:
        runtime.unregister(prepared.claim.dispatch_id)


def run_external_dispatch_cycle(
    *,
    session_factory: Optional[SessionFactory] = None,
    worker_id: Optional[str] = None,
    runtime: Optional[DispatchRuntime] = None,
    max_items: Optional[int] = None,
    report: Optional[CycleReport] = None,
) -> list[DispatchResult]:
    """
    One polling cycle: claim -> process, one dispatch at a time, up to
    ``external_dispatch_max_per_cycle``. Claiming one row at a time (never a
    batch up front) means a claim never waits behind other sends and ages
    toward its lease. A database error ends the cycle; the next tick retries.

    The database error is caught and logged HERE, so the returned list alone cannot tell "no work"
    from "aborted by a database error". A caller that needs the difference (the scheduler job, for
    the operator worker metrics) passes a ``CycleReport``; ``report.aborted`` is set in that case.
    The return value and every behaviour are the same with or without it.
    """
    runtime = runtime or default_runtime
    limit = max_items if max_items is not None else settings.external_dispatch_max_per_cycle
    results: list[DispatchResult] = []
    for _ in range(limit):
        if runtime.stopping:
            break
        try:
            result = process_next_external_dispatch(
                session_factory=session_factory, worker_id=worker_id, runtime=runtime
            )
        except SQLAlchemyError:
            logger.exception("external_dispatch cycle aborted by a database error")
            if report is not None:
                report.aborted = True
            break
        if result is None:
            break
        results.append(result)
    return results


def run_external_dispatch_lease_recovery(
    *, session_factory: Optional[SessionFactory] = None
) -> int:
    """Expired SENDING -> UNKNOWN sweep. Never queues, never sends."""
    factory = session_factory or _default_session_factory()
    db = factory()
    try:
        recovered = recover_expired_external_dispatches(db)
        db.commit()
        return len(recovered)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("external_dispatch lease recovery failed")
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def begin_external_dispatch_shutdown(runtime: Optional[DispatchRuntime] = None) -> None:
    """Step 1: stop taking NEW claims. In-flight work is untouched."""
    (runtime or default_runtime).request_shutdown()
    logger.info("external_dispatch: shutdown requested; no new claims")


def drain_external_dispatches_on_shutdown(
    *,
    session_factory: Optional[SessionFactory] = None,
    runtime: Optional[DispatchRuntime] = None,
    drain_seconds: Optional[float] = None,
) -> ShutdownReport:
    """
    Step 2: give in-flight dispatches a bounded time to finish; then resolve
    the ones THIS process still owns to UNKNOWN (fenced, by dispatch ID and
    claim token). Anything the fence no longer matches is left alone. Nothing
    is ever moved to QUEUED, and nothing is swept by worker_id.
    """
    runtime = runtime or default_runtime
    factory = session_factory or _default_session_factory()
    timeout = settings.external_dispatch_shutdown_drain_seconds if drain_seconds is None else drain_seconds

    drained = runtime.drain(timeout)
    abandoned: list[int] = []
    elsewhere: list[int] = []
    for claim in runtime.inflight():
        reason = (
            "worker_shutdown: this worker was stopped while the dispatch was SENDING and it "
            "did not finish within the drain window. Whether SMTP accepted the message is "
            "unknown; it will NOT be retried automatically."
        )
        db = factory()
        try:
            won = finish_external_dispatch(db, claim, new_state=S.UNKNOWN, error_message=reason)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception("shutdown could not resolve dispatch_id=%d; lease sweep will", claim.dispatch_id)
            won = False
        finally:
            db.close()
        (abandoned if won else elsewhere).append(claim.dispatch_id)
        if won:
            logger.warning(
                "external_dispatch shutdown: SENDING -> UNKNOWN dispatch_id=%d org=%s worker_id=%s (no resend)",
                claim.dispatch_id, claim.organization_id, claim.worker_id,
            )
    return ShutdownReport(
        drained=drained,
        abandoned_unknown=tuple(abandoned),
        still_owned_elsewhere=tuple(elsewhere),
    )
