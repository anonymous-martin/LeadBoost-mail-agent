"""
PR4: durable worker liveness and job-cycle outcomes, for the operator-only metrics routes.

Why this is a database record
-----------------------------
The HTTP API and the scheduler (``worker.py``) are separate processes. Anything the worker
keeps in memory is invisible to the API, and no existing table says "this process is alive"
(``claimed_by`` / ``claimed_at`` exist only while work is claimed). A recent dispatch proves
nothing either: the queue may simply have been empty for hours. So each scheduler-bearing
process writes a heartbeat row, and each instrumented job overwrites one row with the outcome
of its latest cycle. See ``models.WorkerRuntimeStatus`` / ``models.WorkerCycleStatus``.

Four different facts that must never be conflated (the endpoints keep them separate)
------------------------------------------------------------------------------------
* heartbeat fresh   -- the process wrote its liveness row recently. Nothing more.
* cycle completed   -- a scheduled job ran to its normal end without a failure the job itself
                       could observe. It is NOT "the queue is empty" and NOT "mail was sent".
* SMTP accepted     -- a mail server accepted a message (``ExternalDispatch.state == 'sent'``).
* recipient receipt -- never observable from here.

What a recording failure may do: nothing
----------------------------------------
Every public method of ``WorkerStatusRecorder`` swallows its own exceptions and returns a bool.
Recording always happens AFTER the work it describes, in its own short session, outside any
dispatch/claim transaction, so a failed write cannot trigger a second send attempt and cannot
alter an already persisted dispatch outcome. The only visible consequence of lost database
liveness is that the heartbeat goes stale -- which is exactly the signal the operator needs.
Failure logs carry the operation name and the exception CLASS name, never its text (driver
messages can echo connection details or row values).

What is stored: opaque process id, role, timestamps, fixed outcome labels, whitelisted integer
counters. Nothing else can reach the table: counters outside ``COUNT_KEYS`` are dropped at write
time, and error text is never accepted (only a sanitised class name).
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mailer_agent.config import get_settings
from mailer_agent.models import WorkerCycleStatus, WorkerRuntimeStatus
from mailer_agent.utils.datetime_utils import as_utc, utcnow

logger = logging.getLogger("mailer_agent.worker_status")

# --------------------------------------------------------------------------- vocabulary

ROLE_WORKER = "worker"                 # python worker.py
ROLE_API_EMBEDDED = "api_embedded"     # API process with RUN_SCHEDULER_IN_PROCESS=true
ROLE_SCHEDULER = "scheduler"           # default when a caller names no role

KIND_EXTERNAL_DISPATCH = "external_dispatch"
KIND_GENERATION = "generation"
KIND_INBOUND_MAILBOX = "inbound_mailbox"   # mailbox-bound IMAP polling (M3)
KIND_INBOUND_LEGACY = "inbound_legacy"     # deployment-global IMAP polling (kept distinct on purpose)
CYCLE_KINDS = (KIND_EXTERNAL_DISPATCH, KIND_GENERATION, KIND_INBOUND_MAILBOX, KIND_INBOUND_LEGACY)

# What each outcome asserts -- and what it does not:
#   completed  the cycle ran to its normal end and nothing it could observe went wrong. Per-item
#              business outcomes (a suppressed recipient, a refused SMTP) are counted separately in
#              the counters; they are not cycle failures. Zero items processed does NOT prove the
#              queue was empty (see docs/OPERATOR_OBSERVABILITY.md, "What 'completed' does not mean").
#   degraded   the cycle ran to its normal end, but at least one item or mailbox hit a failure the
#              job could observe (an outcome that could not be persisted, a mailbox poll error,
#              a message left unseen).
#   failed     the cycle did NOT run to its normal end: the job raised, or the cycle function caught
#              a database error itself and stopped early (reported through ``CycleReport``), or the
#              mailbox list could not be read.
OUTCOME_COMPLETED = "completed"
OUTCOME_DEGRADED = "degraded"
OUTCOME_FAILED = "failed"
OUTCOMES = (OUTCOME_COMPLETED, OUTCOME_DEGRADED, OUTCOME_FAILED)

# Inbound handler result buckets (see classify_inbound_result).
BUCKET_PERSISTED = "persisted"
BUCKET_DUPLICATE = "duplicate"
BUCKET_UNMATCHED = "unmatched"
BUCKET_AMBIGUOUS = "ambiguous"
BUCKET_UNRECOGNIZED = "unrecognized"

_DISPATCH_KEYS = (
    "processed", "smtp_attempted", "smtp_accepted", "failed", "unknown", "outcome_not_persisted",
)
_GENERATION_KEYS = ("processed", "generated", "failed", "outcome_not_persisted")
_MAILBOX_KEYS = (
    "mailboxes_listed", "mailboxes_ok", "mailboxes_skipped_ineligible",
    "mailboxes_credential_unavailable", "mailboxes_imap_error", "mailboxes_unexpected_error",
    "messages_marked_seen", "messages_left_unseen",
    "handled_persisted", "handled_duplicate", "handled_unmatched", "handled_ambiguous",
    "handled_unrecognized", "classification_succeeded", "classification_failed",
)
_LEGACY_KEYS = (
    "emails_fetched", "handler_failures",
    "handled_persisted", "handled_duplicate", "handled_unmatched", "handled_unrecognized",
    "classification_succeeded", "classification_failed",
)
# The ONLY counters that can ever be stored, per kind.
COUNT_KEYS: dict[str, tuple[str, ...]] = {
    KIND_EXTERNAL_DISPATCH: _DISPATCH_KEYS,
    KIND_GENERATION: _GENERATION_KEYS,
    KIND_INBOUND_MAILBOX: _MAILBOX_KEYS,
    KIND_INBOUND_LEGACY: _LEGACY_KEYS,
}

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ERROR_TYPE_STRIP = re.compile(r"[^A-Za-z0-9_.]")
_CLEANUP_EVERY = timedelta(hours=1)


@dataclass
class CycleReport:
    """Out-parameter for cycle functions that catch and log their own database errors.

    ``run_external_dispatch_cycle`` / ``run_generation_cycle`` swallow ``SQLAlchemyError`` (log, stop
    the loop, return what they have), so an empty or short result list cannot be told apart from
    "no work". A caller that passes a ``CycleReport`` learns the difference; the return value and
    every existing behaviour are unchanged.
    """

    aborted: bool = False


# --------------------------------------------------------------------------- pure helpers


def safe_error_type(value: Any) -> str | None:
    """A sanitised exception CLASS name (never message text), or None."""
    if value is None:
        return None
    name = value if isinstance(value, str) else type(value).__name__
    cleaned = _ERROR_TYPE_STRIP.sub("", name)[:64]
    return cleaned or None


def clean_counts(kind: str, counts: Mapping[str, Any] | None) -> dict[str, int]:
    """Whitelisted, non-negative integer counters for ``kind``; every whitelisted key is present."""
    allowed = COUNT_KEYS[kind]
    src = counts or {}
    out: dict[str, int] = {}
    for key in allowed:
        value = src.get(key, 0)
        out[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    return out


def classify_inbound_result(result: Any) -> str:
    """Bucket ONE inbound-handler result by what the handler actually returned.

    Real shapes (mail/reply_handler_v2.py):
      * mailbox-bound   {"matched": True,  "action": "skipped_duplicate", ...}   -> duplicate
                        {"matched": False, "action": "unmatched_<reason>", "reason": "no_match"} -> unmatched
                        {"matched": False, "action": "unmatched_<reason>", "reason": "ambiguous"} -> ambiguous
                        {"matched": True,  "classification_success": bool, "action": ...}       -> persisted
      * legacy/webhook  {"matched": False, "from_email": ...}  (NO reason: ambiguous cannot be told
                        apart from unmatched on that path)                                       -> unmatched
    Anything else is ``unrecognized`` -- never guessed into another bucket. Only the bucket name
    leaves this function; the result dict (which may carry an address) is not retained.
    """
    if not isinstance(result, dict):
        return BUCKET_UNRECOGNIZED
    if result.get("action") == "skipped_duplicate":
        return BUCKET_DUPLICATE
    matched = result.get("matched")
    if matched is False:
        return BUCKET_AMBIGUOUS if result.get("reason") == "ambiguous" else BUCKET_UNMATCHED
    if matched is True:
        return BUCKET_PERSISTED
    return BUCKET_UNRECOGNIZED


def classification_flag(result: Any) -> bool | None:
    """The handler's own ``classification_success`` for a PERSISTED result, else None."""
    if classify_inbound_result(result) != BUCKET_PERSISTED:
        return None
    flag = result.get("classification_success")
    return flag if isinstance(flag, bool) else None


def summarize_dispatch_results(results: Iterable[Any]) -> dict[str, int]:
    """Counters from ``DispatchResult`` objects (``outcome`` is 'sent'|'failed'|'unknown')."""
    c = dict.fromkeys(_DISPATCH_KEYS, 0)
    for r in results:
        c["processed"] += 1
        c["smtp_attempted"] += 1 if getattr(r, "smtp_attempted", False) else 0
        outcome = getattr(r, "outcome", None)
        if outcome == "sent":
            c["smtp_accepted"] += 1
        elif outcome == "failed":
            c["failed"] += 1
        elif outcome == "unknown":
            c["unknown"] += 1
        if getattr(r, "persisted", True) is False:
            c["outcome_not_persisted"] += 1
    return c


def summarize_generation_results(results: Iterable[Any]) -> dict[str, int]:
    """Counters from ``GenerationResult`` objects (``outcome``: 'generated'|'failed'|'lease_lost')."""
    c = dict.fromkeys(_GENERATION_KEYS, 0)
    for r in results:
        c["processed"] += 1
        outcome = getattr(r, "outcome", None)
        if outcome == "generated":
            c["generated"] += 1
        elif outcome == "failed":
            c["failed"] += 1
        if getattr(r, "persisted", True) is False:
            c["outcome_not_persisted"] += 1
    return c


# --------------------------------------------------------------------------- recorder


class WorkerStatusRecorder:
    """One scheduler-bearing process's liveness and cycle-outcome writer. Never raises."""

    def __init__(
        self,
        role: str = ROLE_SCHEDULER,
        *,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], datetime] | None = None,
        instance_id: str | None = None,
        settings: Any = None,
    ) -> None:
        # A fresh random identity for every process start. Independent of the hostname, the PID and
        # of WORKER_ID/make_worker_id() (which an operator may set to the same value on two
        # processes), so duplicate workers remain distinguishable.
        self.instance_id: str = instance_id or uuid.uuid4().hex
        self.role: str = role if isinstance(role, str) and _ROLE_RE.match(role) else ROLE_SCHEDULER
        self._factory = session_factory
        self._clock: Callable[[], datetime] = clock or utcnow
        self._settings = settings or get_settings()
        self.started_at: datetime = self._clock()
        self._lock = threading.Lock()
        self._stopped = False
        self._last_cleanup: datetime | None = None

    # ---- plumbing -------------------------------------------------------------------------
    def _new_session(self) -> Session:
        if self._factory is not None:
            return self._factory()
        from mailer_agent.db import (
            SessionLocal,  # lazy: importing this module builds no engine
        )

        return SessionLocal()

    def _log_failure(self, operation: str, exc: BaseException) -> None:
        # Operation + exception CLASS only. Never str(exc): driver messages can carry DSNs/values.
        logger.warning(
            "Worker status %s was not recorded (%s). Liveness will read as stale until the database "
            "is reachable again; no job outcome was changed.",
            operation, type(exc).__name__,
        )

    @property
    def stopped(self) -> bool:
        return self._stopped

    # ---- liveness ---------------------------------------------------------------------------
    def heartbeat(self) -> bool:
        """Write/refresh this process's liveness row. Creates it on first success.

        A stopped process never heartbeats again, and the UPDATE is conditional on
        ``stopped_at IS NULL`` so a heartbeat that races a graceful stop cannot touch the record.
        """
        if self._stopped:
            return False
        try:
            now = self._clock()
            db = self._new_session()
            try:
                updated = db.execute(
                    update(WorkerRuntimeStatus)
                    .where(
                        WorkerRuntimeStatus.instance_id == self.instance_id,
                        WorkerRuntimeStatus.stopped_at.is_(None),
                    )
                    .values(heartbeat_at=now)
                    .execution_options(synchronize_session=False)
                ).rowcount
                if updated == 0:
                    exists = db.execute(
                        select(WorkerRuntimeStatus.id).where(WorkerRuntimeStatus.instance_id == self.instance_id)
                    ).first()
                    if exists is None:
                        db.add(
                            WorkerRuntimeStatus(
                                instance_id=self.instance_id, role=self.role,
                                started_at=self.started_at, heartbeat_at=now,
                            )
                        )
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                # A duplicate on the unique instance_id (a racing insert of this process's own row) is
                # benign -- but ONLY if the row is actually there. Any other integrity error leaves no
                # row, and reporting success then would make the heartbeat lie about its own state.
                # Verify after the rollback; never assume.
                if not self._own_row_exists(db):
                    self._log_failure("heartbeat", exc)  # operation + exception CLASS only
                    return False
                db.execute(
                    update(WorkerRuntimeStatus)
                    .where(
                        WorkerRuntimeStatus.instance_id == self.instance_id,
                        WorkerRuntimeStatus.stopped_at.is_(None),
                    )
                    .values(heartbeat_at=now)
                    .execution_options(synchronize_session=False)
                )
                db.commit()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 -- recording must never break the scheduler
            self._log_failure("heartbeat", exc)
            return False
        self._maybe_cleanup(now)
        return True

    def _own_row_exists(self, db: Session) -> bool:
        """True iff this process's runtime row is present (read in a fresh transaction)."""
        return (
            db.execute(
                select(WorkerRuntimeStatus.id).where(WorkerRuntimeStatus.instance_id == self.instance_id)
            ).first()
            is not None
        )

    def mark_stopped(self) -> bool:
        """Graceful stop: no further heartbeats, and ``stopped_at`` recorded if the database answers.

        A process that is killed without cleanup never reaches this, so its heartbeat simply ages
        into "stale" -- the intended signal for an unclean death.
        """
        with self._lock:
            self._stopped = True
        try:
            now = self._clock()
            db = self._new_session()
            try:
                db.execute(
                    update(WorkerRuntimeStatus)
                    .where(
                        WorkerRuntimeStatus.instance_id == self.instance_id,
                        WorkerRuntimeStatus.stopped_at.is_(None),
                    )
                    .values(stopped_at=now)
                    .execution_options(synchronize_session=False)
                )
                db.commit()
            finally:
                db.close()
            return True
        except Exception as exc:  # noqa: BLE001
            self._log_failure("stop", exc)
            return False

    # ---- cycle outcomes ---------------------------------------------------------------------
    def record_cycle(
        self,
        kind: str,
        outcome: str,
        counts: Mapping[str, Any] | None = None,
        error_type: Any = None,
    ) -> bool:
        """Overwrite this process's latest-cycle row for ``kind``. Whitelisted counters only."""
        if kind not in CYCLE_KINDS or outcome not in OUTCOMES:
            logger.warning("Worker cycle record refused: unknown kind or outcome label")
            return False
        try:
            now = self._clock()
            clean = clean_counts(kind, counts)
            db = self._new_session()
            try:
                row = db.execute(
                    select(WorkerCycleStatus).where(
                        WorkerCycleStatus.instance_id == self.instance_id,
                        WorkerCycleStatus.kind == kind,
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = WorkerCycleStatus(
                        instance_id=self.instance_id, kind=kind, last_cycle_at=now,
                        last_outcome=outcome, consecutive_unhealthy_cycles=0, cycles_recorded=0,
                        total_counts=clean_counts(kind, None),
                    )
                    db.add(row)
                row.last_cycle_at = now
                row.last_outcome = outcome
                row.cycles_recorded = (row.cycles_recorded or 0) + 1
                if outcome == OUTCOME_COMPLETED:
                    row.last_completed_at = now
                    row.consecutive_unhealthy_cycles = 0
                    row.last_error_type = None
                else:
                    row.consecutive_unhealthy_cycles = (row.consecutive_unhealthy_cycles or 0) + 1
                    row.last_error_type = safe_error_type(error_type)
                row.last_counts = clean
                totals = clean_counts(kind, row.total_counts)
                for key, value in clean.items():
                    totals[key] += value
                row.total_counts = totals  # a NEW dict: in-place mutation of a JSON column is not tracked
                db.commit()
            finally:
                db.close()
            return True
        except Exception as exc:  # noqa: BLE001
            self._log_failure(f"cycle:{kind}", exc)
            return False

    # ---- retention --------------------------------------------------------------------------
    def _maybe_cleanup(self, now: datetime) -> None:
        if self._last_cleanup is not None and now - self._last_cleanup < _CLEANUP_EVERY:
            return
        self._last_cleanup = now
        try:
            self.cleanup_stale_records(now=now)
        except Exception as exc:  # noqa: BLE001 -- retention is housekeeping; never affects liveness
            self._log_failure("retention cleanup", exc)

    def cleanup_stale_records(self, *, now: datetime | None = None) -> int:
        """Delete rows of processes whose last heartbeat is older than the retention window.

        Policy (``WORKER_RUNTIME_RETENTION_HOURS``, default 72): a runtime row -- stopped or merely
        silent -- is deleted once its ``heartbeat_at`` is older than the window; cycle rows whose
        process row is gone (and that are themselves older than the window) go with it. This
        process's own rows are never deleted by itself. Returns the number of runtime rows deleted.
        """
        now = now or self._clock()
        cutoff = now - timedelta(hours=int(self._settings.worker_runtime_retention_hours))
        db = self._new_session()
        try:
            dead = select(WorkerRuntimeStatus.instance_id).where(
                WorkerRuntimeStatus.heartbeat_at < cutoff,
                WorkerRuntimeStatus.instance_id != self.instance_id,
            )
            db.execute(
                delete(WorkerCycleStatus)
                .where(WorkerCycleStatus.instance_id.in_(dead))
                .execution_options(synchronize_session=False)
            )
            removed = db.execute(
                delete(WorkerRuntimeStatus)
                .where(
                    WorkerRuntimeStatus.heartbeat_at < cutoff,
                    WorkerRuntimeStatus.instance_id != self.instance_id,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            # Orphans: a cycle row whose process row does not exist (its process never managed to
            # register, or was already purged) once it is itself older than the window.
            known = select(WorkerRuntimeStatus.instance_id)
            db.execute(
                delete(WorkerCycleStatus)
                .where(
                    WorkerCycleStatus.last_cycle_at < cutoff,
                    WorkerCycleStatus.instance_id != self.instance_id,
                    WorkerCycleStatus.instance_id.not_in(known),
                )
                .execution_options(synchronize_session=False)
            )
            db.commit()
            return int(removed or 0)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def aware(value: datetime | None) -> datetime | None:
    """Naive timestamps read back from SQLite are UTC; make every value timezone-aware."""
    return as_utc(value)
