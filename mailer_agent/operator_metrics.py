"""
PR4: read-only, process-wide aggregates for the three operator-only metrics routes
(``/system/metrics/dispatch``, ``/system/metrics/worker``, ``/system/metrics/inbound``).

These are DEPLOYMENT-wide operational numbers, not tenant analytics: they span every organization
on purpose, which is exactly why they sit behind the operator credential and never on the
tenant router. Nothing here is per tenant, and nothing here can name one.

Hard rules (each has a regression test in tests/test_pr4_*):
  * SQL-side aggregation only. No query loads dispatch/message rows into Python.
  * Output is counts, ages, fixed labels, timestamps and an opaque random process id. Never a
    tenant/organization id, contact id, address, Message-ID, idempotency key, message content, public
    reference, credential, or raw error text. Free-text columns (``error_message``) are never selected.
  * Time is injected (``now``) so tests control it and every age is computed from persisted
    timestamps against one reference instant.
  * Facts are kept apart: a fresh heartbeat is not a successful cycle; a successful cycle is not
    SMTP acceptance; SMTP acceptance (``sent``) is not recipient receipt.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, and_, case, func, or_, select, type_coerce
from sqlalchemy.orm import Session

from mailer_agent.models import (
    ExternalDispatch,
    ExternalDispatchState,
    Message,
    MessageDirection,
    WorkerCycleStatus,
    WorkerRuntimeStatus,
)
from mailer_agent.utils.datetime_utils import as_utc
from mailer_agent.worker_status import (
    CYCLE_KINDS,
    KIND_INBOUND_LEGACY,
    KIND_INBOUND_MAILBOX,
    clean_counts,
    safe_error_type,
)

# Most-recent process rows returned. Rows are already bounded by retention; this bounds the response.
MAX_INSTANCES_LISTED = 20

_S = ExternalDispatchState
_STATE_ORDER = (_S.QUEUED, _S.GENERATING, _S.SENDING, _S.SENT, _S.FAILED, _S.UNKNOWN)
_STATE_VALUES = tuple(s.value for s in _STATE_ORDER)
_NON_TERMINAL = (_S.QUEUED.value, _S.GENERATING.value, _S.SENDING.value)
_DT = DateTime(timezone=True)

DISPATCH_DEFINITIONS = {
    "scope": (
        "Process-wide, across every organization. Operational counts for the operator; "
        "not tenant analytics and not customer-facing."
    ),
    "queued": "Accepted and waiting. See `queue` for the split between awaiting generation and ready to send.",
    "generating": "Claimed by the generation worker; the message is being drafted. Nothing has been sent.",
    "sending": "Claimed by the dispatch worker with an unexpired or expired lease; an SMTP attempt may be in flight.",
    "sent": (
        "SMTP ACCEPTED: a mail server accepted the message for delivery. This is NOT confirmation that "
        "the recipient's mailbox received it; bounces, filtering and delivery are not observable here."
    ),
    "failed": "Definitively not sent (nothing reached SMTP, or SMTP refused). Never retried automatically.",
    "unknown": (
        "AMBIGUOUS outcome: an SMTP attempt may or may not have been accepted. Requires operator "
        "reconciliation. Never retried automatically, because a retry could duplicate an email."
    ),
    "oldest_queued_age": (
        "Seconds since the oldest queued dispatch was ACCEPTED (created_at), not since it was last "
        "re-queued. It can only over-state waiting, never hide it. null when nothing is queued."
    ),
    "stale_sending": (
        "`sending` rows whose lease has expired (claimed_at is NULL or older than the lease), using the "
        "same predicate as the lease-recovery sweep. They are resolved to `unknown` by that sweep; "
        "a non-zero value that persists means recovery is not running."
    ),
}


# --------------------------------------------------------------------------- helpers


def _iso(value: datetime | None) -> str | None:
    v = as_utc(value)
    return v.isoformat() if v else None


def _age_seconds(then: datetime | None, now: datetime) -> int | None:
    t = as_utc(then)
    if t is None:
        return None
    return max(0, int((as_utc(now) - t).total_seconds()))


def _naive_utc(value: datetime) -> datetime:
    return as_utc(value).replace(tzinfo=None)


def _int(value: Any) -> int:
    return int(value or 0)


# --------------------------------------------------------------------------- dispatch


def dispatch_metrics(db: Session, *, now: datetime, lease_seconds: int) -> dict[str, Any]:
    """Durable ExternalDispatch counts, queue depth/age and ambiguous outcomes.

    Two statements, both aggregating in the database:
      1. ``SELECT state, count(*) ... GROUP BY state`` over the whole table (served by
         ix_external_dispatches_state_claimed_at, an index-only scan on PostgreSQL; terminal rows
         grow without bound, so this is the one O(table) read and it never leaves the index).
      2. conditional aggregates restricted to the three NON-terminal states, which are the
         working set and stay small by construction.
    """
    per_state = {state: 0 for state in _STATE_VALUES}
    unrecognized = 0
    for state, n in db.execute(
        select(ExternalDispatch.state, func.count()).group_by(ExternalDispatch.state)
    ).all():
        if state in per_state:
            per_state[state] = _int(n)
        else:
            unrecognized += _int(n)

    queued = ExternalDispatch.state == _S.QUEUED.value
    awaiting_generation = and_(queued, ExternalDispatch.message_id.is_(None))
    ready_to_send = and_(queued, ExternalDispatch.message_id.is_not(None))
    # Same predicate as followup.work_claiming.recover_expired_external_dispatches.
    lease_cutoff = _naive_utc(now) - timedelta(seconds=int(lease_seconds))
    stale_sending = and_(
        ExternalDispatch.state == _S.SENDING.value,
        or_(ExternalDispatch.claimed_at.is_(None), ExternalDispatch.claimed_at < lease_cutoff),
    )
    created = ExternalDispatch.created_at
    row = db.execute(
        select(
            func.sum(case((awaiting_generation, 1), else_=0)),
            func.sum(case((ready_to_send, 1), else_=0)),
            type_coerce(func.min(case((queued, created))), _DT),
            type_coerce(func.min(case((awaiting_generation, created))), _DT),
            type_coerce(func.min(case((ready_to_send, created))), _DT),
            func.sum(case((stale_sending, 1), else_=0)),
        ).where(ExternalDispatch.state.in_(_NON_TERMINAL))
    ).one()
    n_awaiting, n_ready, oldest_q, oldest_aw, oldest_rd, n_stale = row

    sending_total = per_state[_S.SENDING.value]
    return {
        "generated_at": _iso(now),
        "definitions": DISPATCH_DEFINITIONS,
        "states": per_state,
        "total": sum(per_state.values()) + unrecognized,
        "unrecognized_state_rows": unrecognized,
        "queue": {
            "depth": per_state[_S.QUEUED.value],
            "awaiting_generation": _int(n_awaiting),
            "ready_to_send": _int(n_ready),
            "oldest_queued_age_seconds": _age_seconds(oldest_q, now),
            "oldest_awaiting_generation_age_seconds": _age_seconds(oldest_aw, now),
            "oldest_ready_to_send_age_seconds": _age_seconds(oldest_rd, now),
        },
        "sending": {
            "in_flight_or_unexpired": max(0, sending_total - _int(n_stale)),
            "stale_lease_expired": _int(n_stale),
            "lease_seconds": int(lease_seconds),
        },
        "outcomes": {
            "smtp_accepted": per_state[_S.SENT.value],
            "failed_not_sent": per_state[_S.FAILED.value],
            "unknown_needs_operator_reconciliation": per_state[_S.UNKNOWN.value],
        },
    }


# --------------------------------------------------------------------------- worker


def heartbeat_status(
    stopped_at: datetime | None, heartbeat_at: datetime | None, now: datetime, stale_after_seconds: int
) -> str:
    """'stopped' (graceful stop recorded) | 'fresh' | 'stale'. Pure: time is an argument.

    A heartbeat exactly ``stale_after_seconds`` old is still fresh; older is stale. A heartbeat
    slightly in the future (clock skew between hosts) counts as fresh.
    """
    if stopped_at is not None:
        return "stopped"
    age = _age_seconds(heartbeat_at, now)
    if age is None or age > int(stale_after_seconds):
        return "stale"
    return "fresh"


WORKER_DEFINITIONS = {
    "heartbeat": (
        "`fresh` means the process wrote its liveness row recently and its scheduler is ticking. It "
        "says NOTHING about SMTP, IMAP or LLM connectivity, or whether any job is succeeding."
    ),
    "stale": (
        "No heartbeat within the stale threshold: the process died without cleanup, is wedged, or "
        "cannot reach the database. These cannot be told apart from here."
    ),
    "stopped": "The process recorded a graceful stop (shutdown ran to its end).",
    "cycle": (
        "`last_outcome` describes the latest run of one scheduled job in that process. `completed` = ran "
        "to its normal end with no failure the job could observe; it does NOT mean the queue was empty "
        "or that mail was sent. `degraded` = ran to the end but an item/mailbox failed. `failed` = did "
        "not run to its end (raised, caught a database error and stopped, or could not list mailboxes)."
    ),
    "counts": (
        "`last_cycle_counts` are for the latest cycle only. `counts_since_instance_start` accumulate "
        "within ONE process lifetime and restart at zero with each new process; they are not summed "
        "across processes, because retention removes old processes."
    ),
    "clock": "Ages compare timestamps written by the worker host with the API host's clock; skew of "
    "more than a few seconds would distort them.",
}


def _cycle_view(row: WorkerCycleStatus, now: datetime) -> dict[str, Any]:
    return {
        "last_cycle_at": _iso(row.last_cycle_at),
        "last_cycle_age_seconds": _age_seconds(row.last_cycle_at, now),
        "last_outcome": row.last_outcome,
        "last_completed_at": _iso(row.last_completed_at),
        "consecutive_unhealthy_cycles": int(row.consecutive_unhealthy_cycles or 0),
        "cycles_recorded_since_instance_start": int(row.cycles_recorded or 0),
        "last_error_type": safe_error_type(row.last_error_type),
        # Whitelisted AGAIN at read time: only known integer counters can ever be emitted.
        "last_cycle_counts": clean_counts(row.kind, row.last_counts),
        "counts_since_instance_start": clean_counts(row.kind, row.total_counts),
    }


def worker_metrics(
    db: Session,
    *,
    now: datetime,
    stale_after_seconds: int,
    heartbeat_interval_seconds: int,
    retention_hours: int,
) -> dict[str, Any]:
    """Per scheduler-process liveness plus each process's latest cycle outcomes."""
    fetched = (
        db.execute(
            select(WorkerRuntimeStatus)
            .order_by(WorkerRuntimeStatus.heartbeat_at.desc(), WorkerRuntimeStatus.id.desc())
            .limit(MAX_INSTANCES_LISTED + 1)
        )
        .scalars()
        .all()
    )
    truncated = len(fetched) > MAX_INSTANCES_LISTED
    runtimes = fetched[:MAX_INSTANCES_LISTED]

    cycles_by_instance: dict[str, dict[str, Any]] = {}
    if runtimes:
        for c in db.execute(
            select(WorkerCycleStatus).where(
                WorkerCycleStatus.instance_id.in_([r.instance_id for r in runtimes])
            )
        ).scalars():
            if c.kind in CYCLE_KINDS:
                cycles_by_instance.setdefault(c.instance_id, {})[c.kind] = _cycle_view(c, now)

    instances = []
    tally = {"fresh": 0, "stale": 0, "stopped": 0}
    fresh_by_role: dict[str, int] = {}
    for r in runtimes:
        status = heartbeat_status(r.stopped_at, r.heartbeat_at, now, stale_after_seconds)
        tally[status] += 1
        if status == "fresh":
            fresh_by_role[r.role] = fresh_by_role.get(r.role, 0) + 1
        instances.append(
            {
                "instance_id": r.instance_id,
                "role": r.role,
                "heartbeat_status": status,
                "started_at": _iso(r.started_at),
                "uptime_seconds": None if status == "stopped" else _age_seconds(r.started_at, now),
                "last_heartbeat_at": _iso(r.heartbeat_at),
                "heartbeat_age_seconds": _age_seconds(r.heartbeat_at, now),
                "stopped_at": _iso(r.stopped_at),
                "cycles": cycles_by_instance.get(r.instance_id, {}),
            }
        )

    if not runtimes:
        overall = "no_instances_recorded"
    elif tally["fresh"]:
        overall = "fresh"
    elif tally["stale"]:
        overall = "stale"
    else:
        overall = "stopped"

    return {
        "generated_at": _iso(now),
        "definitions": WORKER_DEFINITIONS,
        "thresholds": {
            "heartbeat_interval_seconds": int(heartbeat_interval_seconds),
            "stale_after_seconds": int(stale_after_seconds),
            "retention_hours": int(retention_hours),
        },
        "summary": {
            "heartbeat_status": overall,
            "fresh": tally["fresh"],
            "stale": tally["stale"],
            "stopped": tally["stopped"],
            # More than one fresh process of one role is worth a look (the intended topology is one
            # worker); it is also normal for a few seconds during a rolling deploy.
            "roles_with_multiple_fresh_instances": sorted(r for r, n in fresh_by_role.items() if n > 1),
        },
        "instances": instances,
        "instances_truncated": truncated,
    }


# --------------------------------------------------------------------------- inbound

INBOUND_DEFINITIONS = {
    "scope": "Process-wide, across every organization. Aggregate counters only; no addresses, subjects, "
    "bodies, Message-IDs or tenant identifiers are recorded or returned.",
    "mailbox_polling": (
        "Mailbox-bound polling (each Mailbox's own IMAP identity). Evidence is the cycle snapshot written "
        "by the worker after each poll, per process. `handled_*` are the handler's actual results, "
        "counted when it returns -- after its commit, before the message is marked Seen. They are "
        "handler outcomes, not distinct emails: a message whose Seen flag fails to store is offered "
        "again and counted again as a duplicate."
    ),
    "unmatched_and_ambiguous": (
        "Unmatched and ambiguous mail is deliberately NOT stored as a Message row. Only the aggregate "
        "counters in the cycle snapshots record it (per process lifetime), so there is no durable "
        "historical total for it, and persisted Message rows must not be read as 'emails received'."
    ),
    "persisted_inbound_messages": (
        "Inbound Message ROWS created in the window, from every channel (mailbox polling, webhook, "
        "legacy poll). It is a count of stored messages only: it excludes duplicates, unmatched and "
        "ambiguous mail, so it is NOT the number of emails received."
    ),
    "legacy_global_imap": (
        "The deployment-global IMAP path, instrumented separately and never a fallback for mailbox "
        "polling. Its fetch helper returns an empty list for 'not configured', 'nothing new' and "
        "'the poll failed' alike, so an empty fetch records nothing: there is NO evidence of fetch "
        "success or failure, and `last_cycle_at` is only the last time it fetched at least one email. "
        "It also reports no reason for an unmatched result, so ambiguous cannot be separated from "
        "unmatched (counted as unmatched)."
    ),
}


def _inbound_cycle_rows(db: Session, kind: str, now: datetime, stale_after_seconds: int) -> dict[str, Any]:
    rows = db.execute(
        select(WorkerCycleStatus, WorkerRuntimeStatus)
        .join(
            WorkerRuntimeStatus,
            WorkerRuntimeStatus.instance_id == WorkerCycleStatus.instance_id,
            isouter=True,
        )
        .where(WorkerCycleStatus.kind == kind)
        .order_by(WorkerCycleStatus.last_cycle_at.desc(), WorkerCycleStatus.id.desc())
        .limit(MAX_INSTANCES_LISTED)
    ).all()
    instances = []
    last_completed: datetime | None = None
    for cyc, rt in rows:
        view = _cycle_view(cyc, now)
        view["instance_id"] = cyc.instance_id
        view["instance_heartbeat_status"] = (
            heartbeat_status(rt.stopped_at, rt.heartbeat_at, now, stale_after_seconds)
            if rt is not None else "unknown"
        )
        instances.append(view)
        done = as_utc(cyc.last_completed_at)
        if done is not None and (last_completed is None or done > last_completed):
            last_completed = done
    return {
        "last_completed_cycle_at": _iso(last_completed),
        "last_completed_cycle_age_seconds": _age_seconds(last_completed, now),
        "instances": instances,
    }


def inbound_metrics(
    db: Session, *, now: datetime, window_hours: int, stale_after_seconds: int
) -> dict[str, Any]:
    """Mailbox-poll cycle evidence, legacy-path evidence (separate), and persisted inbound rows."""
    since = as_utc(now) - timedelta(hours=int(window_hours))
    inbound = Message.direction == MessageDirection.INBOUND.value
    total, ok, failed, via_mailbox = db.execute(
        select(
            func.count(),
            func.sum(case((Message.classification_success.is_(True), 1), else_=0)),
            func.sum(case((Message.classification_success.is_(False), 1), else_=0)),
            func.sum(case((Message.mailbox_id.is_not(None), 1), else_=0)),
        ).where(inbound, Message.created_at >= since)
    ).one()
    total, ok, failed, via_mailbox = _int(total), _int(ok), _int(failed), _int(via_mailbox)

    return {
        "generated_at": _iso(now),
        "definitions": INBOUND_DEFINITIONS,
        "mailbox_polling": _inbound_cycle_rows(db, KIND_INBOUND_MAILBOX, now, stale_after_seconds),
        "legacy_global_imap": _inbound_cycle_rows(db, KIND_INBOUND_LEGACY, now, stale_after_seconds),
        "persisted_inbound_messages": {
            "window_hours": int(window_hours),
            "window_start": _iso(since),
            "stored_message_rows": total,
            "stored_via_mailbox_polling": via_mailbox,
            "classification_succeeded": ok,
            "classification_failed": failed,
            "classification_not_recorded": max(0, total - ok - failed),
        },
    }
