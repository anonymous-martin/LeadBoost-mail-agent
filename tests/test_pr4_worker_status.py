"""PR4 -- worker liveness: the recorder (mailer_agent/worker_status.py) and /system/metrics/worker.

Deterministic (SQLite). Time is an injected clock for the writer and a pinned ``utcnow`` for the route,
so fresh / stale / stopped transitions are tested at exact boundaries without sleeping.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from mailer_agent.config import get_settings
from mailer_agent.models import WorkerCycleStatus, WorkerRuntimeStatus
from mailer_agent.operator_metrics import MAX_INSTANCES_LISTED, heartbeat_status
from mailer_agent.worker_status import (
    COUNT_KEYS,
    CYCLE_KINDS,
    KIND_EXTERNAL_DISPATCH,
    KIND_INBOUND_MAILBOX,
    OUTCOME_COMPLETED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    ROLE_SCHEDULER,
    WorkerStatusRecorder,
    clean_counts,
    safe_error_type,
)
from tests.pr4_support import NOW, ago

URL = "/system/metrics/worker"
STALE_AFTER = get_settings().worker_stale_after_seconds  # 120 by default


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def recorder(env, clock, role="worker", **kw):
    return WorkerStatusRecorder(role, session_factory=env.sm, clock=clock, **kw)


def rows(env):
    with env.sm() as s:
        return s.execute(select(WorkerRuntimeStatus)).scalars().all()


def one(env):
    r = rows(env)
    assert len(r) == 1
    return r[0]


def status_via_api(env):
    return env.get(URL).json()


# ------------------------------------------------------------------ defaults are what the PR documents
def test_documented_defaults():
    s = get_settings()
    assert (s.worker_heartbeat_interval_seconds, s.worker_stale_after_seconds) == (30, 120)
    assert s.worker_runtime_retention_hours == 72 and s.operator_inbound_window_hours == 24
    assert s.worker_stale_after_seconds >= 3 * s.worker_heartbeat_interval_seconds  # one missed write never flaps


# ------------------------------------------------------------------ heartbeat
def test_heartbeat_creates_then_refreshes_one_row_and_keeps_start_time(operator_env):
    clock = Clock(ago(100))
    rec = recorder(operator_env, clock)
    assert rec.heartbeat() is True
    first = one(operator_env)
    assert first.instance_id == rec.instance_id and first.role == "worker" and first.stopped_at is None
    started = first.started_at

    clock.t = ago(30)
    assert rec.heartbeat() is True
    again = one(operator_env)  # still exactly one row
    assert again.started_at == started and again.heartbeat_at.replace(tzinfo=None) == ago(30).replace(tzinfo=None)


def test_instance_ids_are_unique_even_with_the_same_worker_id_env_and_host(operator_env, monkeypatch):
    monkeypatch.setenv("WORKER_ID", "worker-SAMEHOST-1")
    a, b = recorder(operator_env, Clock(ago(5))), recorder(operator_env, Clock(ago(5)))
    assert a.instance_id != b.instance_id and len(a.instance_id) == 32
    assert a.heartbeat() and b.heartbeat()
    assert len(rows(operator_env)) == 2  # two live processes remain two observable rows
    summary = status_via_api(operator_env)["summary"]
    assert summary["fresh"] == 2 and summary["roles_with_multiple_fresh_instances"] == ["worker"]


def test_role_label_is_validated_not_free_text(operator_env):
    assert recorder(operator_env, Clock(NOW), role="worker").role == "worker"
    assert recorder(operator_env, Clock(NOW), role="api_embedded").role == "api_embedded"
    for bad in ("Worker; DROP TABLE", "", "x" * 40, "../etc", None, 7):
        assert recorder(operator_env, Clock(NOW), role=bad).role == ROLE_SCHEDULER


# ------------------------------------------------------------------ fresh -> stale -> stopped
@pytest.mark.parametrize(
    "heartbeat_age, expected",
    [(0, "fresh"), (30, "fresh"), (STALE_AFTER, "fresh"), (STALE_AFTER + 1, "stale"), (3600, "stale"), (-5, "fresh")],
)
def test_freshness_threshold_boundaries_through_the_endpoint(operator_env, heartbeat_age, expected):
    rec = recorder(operator_env, Clock(ago(heartbeat_age)))
    assert rec.heartbeat()
    body = status_via_api(operator_env)
    assert body["instances"][0]["heartbeat_status"] == expected
    assert body["summary"]["heartbeat_status"] == expected
    assert body["instances"][0]["heartbeat_age_seconds"] == max(0, heartbeat_age)


def test_heartbeat_status_is_pure_and_takes_the_threshold_as_an_argument():
    assert heartbeat_status(None, NOW - timedelta(seconds=10), NOW, 5) == "stale"
    assert heartbeat_status(None, NOW - timedelta(seconds=10), NOW, 10) == "fresh"
    assert heartbeat_status(NOW, NOW - timedelta(seconds=999), NOW, 5) == "stopped"  # stop wins over age
    assert heartbeat_status(None, None, NOW, 120) == "stale"


def test_one_process_walks_fresh_stale_stopped_with_controllable_time(operator_env, monkeypatch):
    from mailer_agent.api import system

    clock = Clock(ago(0))
    rec = recorder(operator_env, clock)
    assert rec.heartbeat()
    assert status_via_api(operator_env)["summary"]["heartbeat_status"] == "fresh"

    monkeypatch.setattr(system, "utcnow", lambda: NOW + timedelta(seconds=STALE_AFTER + 1))  # time passes
    body = status_via_api(operator_env)
    assert body["summary"]["heartbeat_status"] == "stale" and body["summary"]["stale"] == 1

    clock.t = NOW + timedelta(seconds=STALE_AFTER + 1)  # the process comes back to stop cleanly
    assert rec.mark_stopped() is True
    body = status_via_api(operator_env)
    assert body["summary"]["heartbeat_status"] == "stopped"
    assert body["instances"][0]["stopped_at"] is not None and body["instances"][0]["uptime_seconds"] is None


# ------------------------------------------------------------------ graceful stop vs forced termination
def test_graceful_stop_records_stopped_and_a_late_heartbeat_cannot_revive_or_touch_it(operator_env):
    clock = Clock(ago(60))
    rec = recorder(operator_env, clock)
    assert rec.heartbeat()
    clock.t = ago(10)
    assert rec.mark_stopped() is True and rec.stopped is True
    stopped = one(operator_env)
    stamp = (stopped.stopped_at, stopped.heartbeat_at)

    clock.t = ago(1)
    assert rec.heartbeat() is False  # in-process: a stopped process never beats again
    # ...and even a heartbeat from ANOTHER writer object for the same id (a racing in-flight job) cannot:
    ghost = recorder(operator_env, Clock(ago(1)), instance_id=rec.instance_id)
    assert ghost.heartbeat() is True  # its write is accepted by the caller...
    after = one(operator_env)
    assert (after.stopped_at, after.heartbeat_at) == stamp  # ...but the record is untouched (UPDATE is conditional)


def test_a_process_killed_without_cleanup_is_never_marked_stopped_and_ages_into_stale(operator_env):
    rec = recorder(operator_env, Clock(ago(STALE_AFTER + 30)))
    assert rec.heartbeat()  # ...then the process is SIGKILLed: no mark_stopped(), no further heartbeats
    row = one(operator_env)
    assert row.stopped_at is None
    body = status_via_api(operator_env)
    assert body["summary"]["heartbeat_status"] == "stale"
    assert body["instances"][0]["stopped_at"] is None


# ------------------------------------------------------------------ failure containment
def _boom_factory():
    raise RuntimeError("could not connect to postgresql://svc:SECRETPW@db.internal:5432/prod")


def test_recorder_failures_are_swallowed_and_logged_without_values(operator_env, caplog):
    caplog.set_level(logging.DEBUG)
    rec = WorkerStatusRecorder("worker", session_factory=_boom_factory, clock=Clock(NOW))
    assert rec.heartbeat() is False
    assert rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED, {"processed": 1}) is False
    assert rec.mark_stopped() is False  # all three return False; none raises
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged  # the class name is enough to diagnose
    for leak in ("SECRETPW", "postgresql://", "db.internal", "svc"):
        assert leak not in logged, f"{leak!r} leaked into the failure log"
    assert "stale" in logged  # tells the operator how the failure will present


def _fail_inserts_with_integrity_error(engine):
    """Make every INSERT into worker_runtime_status fail with an IntegrityError that is NOT a duplicate of
    this process's row (nothing is inserted by anyone)."""
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    def _boom(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO WORKER_RUNTIME_STATUS"):
            raise IntegrityError(statement, {"p": "SECRET-PARAM"}, Exception("constraint failed SECRET-DETAIL"))

    event.listen(engine, "before_cursor_execute", _boom)
    return lambda: event.remove(engine, "before_cursor_execute", _boom)


def test_an_unexpected_integrity_error_is_not_reported_as_a_registered_heartbeat(operator_env, caplog):
    caplog.set_level(logging.DEBUG)
    undo = _fail_inserts_with_integrity_error(operator_env.engine)
    try:
        rec = recorder(operator_env, Clock(ago(5)))
        assert rec.heartbeat() is False, "no row was written, so the heartbeat must not claim success"
    finally:
        undo()
    assert rows(operator_env) == []  # nothing recorded ...
    assert status_via_api(operator_env)["summary"]["heartbeat_status"] == "no_instances_recorded"  # ... and the API says so
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "IntegrityError" in logged and "heartbeat" in logged  # sanitized: operation + class name
    for leak in ("SECRET-PARAM", "SECRET-DETAIL", "worker_runtime_status", "INSERT"):
        assert leak not in logged, f"{leak!r} leaked into the failure log"
    # and the recorder recovers on the next successful attempt (the failure was not sticky)
    assert rec.heartbeat() is True and len(rows(operator_env)) == 1


def test_an_unexpected_integrity_error_also_skips_retention_housekeeping(operator_env):
    undo = _fail_inserts_with_integrity_error(operator_env.engine)
    try:
        rec = recorder(operator_env, Clock(NOW))
        assert rec.heartbeat() is False
    finally:
        undo()
    assert rec._last_cleanup is None  # the failed heartbeat did not count as a successful registration


def test_unknown_kind_or_outcome_is_refused_not_stored(operator_env):
    rec = recorder(operator_env, Clock(NOW))
    assert rec.record_cycle("made_up_kind", OUTCOME_COMPLETED) is False
    assert rec.record_cycle(KIND_EXTERNAL_DISPATCH, "sort_of_ok") is False
    with operator_env.sm() as s:
        assert s.execute(select(WorkerCycleStatus)).first() is None


# ------------------------------------------------------------------ cycle records
def test_counters_are_whitelisted_nonnegative_ints_so_nothing_sensitive_can_be_stored(operator_env):
    hostile = {
        "processed": 3, "smtp_accepted": 2, "email": "leak@example.com", "from_address": "x@y.z",
        "message_id": "<abc@leak>", "organization_id": "org-secret", "failed": True, "unknown": -4,
        "outcome_not_persisted": "7", "smtp_attempted": 1.5,
    }
    rec = recorder(operator_env, Clock(NOW))
    assert rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED, hostile)
    with operator_env.sm() as s:
        row = s.execute(select(WorkerCycleStatus)).scalar_one()
    assert set(row.last_counts) == set(COUNT_KEYS[KIND_EXTERNAL_DISPATCH])  # exactly the whitelist
    assert row.last_counts["processed"] == 3 and row.last_counts["smtp_accepted"] == 2
    # bool / negative / string / float values are not counters -> 0
    assert row.last_counts["failed"] == 0 and row.last_counts["unknown"] == 0
    assert row.last_counts["outcome_not_persisted"] == 0 and row.last_counts["smtp_attempted"] == 0
    dumped = repr((row.last_counts, row.total_counts))
    for leak in ("leak@example.com", "org-secret", "<abc@leak>", "x@y.z"):
        assert leak not in dumped
    assert clean_counts(KIND_INBOUND_MAILBOX, None).keys() == set(COUNT_KEYS[KIND_INBOUND_MAILBOX])


def test_last_cycle_counts_overwrite_while_totals_accumulate_within_one_process(operator_env):
    clock = Clock(ago(50))
    rec = recorder(operator_env, clock)
    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED, {"processed": 2, "smtp_accepted": 2})
    clock.t = ago(20)
    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED, {"processed": 1, "smtp_accepted": 1})
    with operator_env.sm() as s:
        row = s.execute(select(WorkerCycleStatus)).scalar_one()  # still ONE row per (process, kind)
    assert row.last_counts["processed"] == 1 and row.total_counts["processed"] == 3
    assert row.cycles_recorded == 2 and row.last_cycle_at.replace(tzinfo=None) == ago(20).replace(tzinfo=None)


def test_outcome_bookkeeping_last_completed_and_consecutive_unhealthy(operator_env):
    clock = Clock(ago(100))
    rec = recorder(operator_env, clock)

    def row():
        with operator_env.sm() as s:
            return s.execute(select(WorkerCycleStatus)).scalar_one()

    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED)
    ok_at = row().last_completed_at
    clock.t = ago(80)
    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_FAILED, error_type="OperationalError")
    clock.t = ago(60)
    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_DEGRADED)
    r = row()
    assert (r.last_outcome, r.consecutive_unhealthy_cycles) == (OUTCOME_DEGRADED, 2)
    assert r.last_completed_at == ok_at  # a failure never advances "last completed"
    assert r.last_error_type is None  # degraded without a type clears the stale one
    clock.t = ago(10)
    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED)
    r = row()
    assert (r.last_outcome, r.consecutive_unhealthy_cycles, r.last_error_type) == (OUTCOME_COMPLETED, 0, None)
    assert r.last_completed_at != ok_at


def test_error_type_is_a_sanitised_class_name_never_message_text():
    assert safe_error_type(ValueError("password=hunter2 host=db")) == "ValueError"
    assert safe_error_type("Bad Name!<script>alert(1)") == "BadNamescriptalert1"
    assert safe_error_type("x" * 200) == "x" * 64
    assert safe_error_type("!!!") is None and safe_error_type(None) is None


# ------------------------------------------------------------------ retention
def test_retention_deletes_old_processes_and_their_cycles_keeps_recent_and_self(operator_env):
    retention_h = get_settings().worker_runtime_retention_hours
    old = recorder(operator_env, Clock(NOW - timedelta(hours=retention_h + 1)))
    old.heartbeat()
    old.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED)
    old_stopped = recorder(operator_env, Clock(NOW - timedelta(hours=retention_h + 5)))
    old_stopped.heartbeat()
    old_stopped.mark_stopped()
    recent = recorder(operator_env, Clock(NOW - timedelta(hours=retention_h - 1)))
    recent.heartbeat()
    recent.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED)
    # an orphan cycle row (its process row never existed) that is itself old
    orphan = recorder(operator_env, Clock(NOW - timedelta(hours=retention_h + 2)))
    orphan.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED)

    me = recorder(operator_env, Clock(NOW))
    assert me.heartbeat()  # first heartbeat also runs the sweep
    ids = {r.instance_id for r in rows(operator_env)}
    assert ids == {recent.instance_id, me.instance_id}
    with operator_env.sm() as s:
        cycle_ids = {c.instance_id for c in s.execute(select(WorkerCycleStatus)).scalars()}
    assert cycle_ids == {recent.instance_id}  # old + orphan cycles gone, recent kept


def test_a_process_never_deletes_its_own_rows_even_if_its_clock_is_far_ahead(operator_env):
    me = recorder(operator_env, Clock(NOW))
    me.heartbeat()
    me.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED)
    assert me.cleanup_stale_records(now=NOW + timedelta(days=365)) == 0
    assert len(rows(operator_env)) == 1


def test_retention_sweep_runs_at_most_hourly(operator_env):
    clock = Clock(NOW)
    me = recorder(operator_env, clock)
    me.heartbeat()
    old = recorder(operator_env, Clock(NOW - timedelta(days=30)))
    old.heartbeat()  # inserted AFTER my first sweep
    clock.t = NOW + timedelta(minutes=30)
    me.heartbeat()
    assert old.instance_id in {r.instance_id for r in rows(operator_env)}  # not swept yet
    clock.t = NOW + timedelta(minutes=61)
    me.heartbeat()
    assert old.instance_id not in {r.instance_id for r in rows(operator_env)}


# ------------------------------------------------------------------ the endpoint
def test_endpoint_with_nothing_recorded(operator_env):
    body = status_via_api(operator_env)
    assert body["instances"] == [] and body["instances_truncated"] is False
    assert body["summary"] == {
        "heartbeat_status": "no_instances_recorded", "fresh": 0, "stale": 0, "stopped": 0,
        "roles_with_multiple_fresh_instances": [],
    }
    assert body["thresholds"] == {"heartbeat_interval_seconds": 30, "stale_after_seconds": 120, "retention_hours": 72}


def test_endpoint_exposes_only_opaque_ids_and_never_hostnames_pids_or_worker_ids(operator_env, monkeypatch):
    monkeypatch.setenv("WORKER_ID", "worker-SECRETHOST-424242-deadbeef")
    monkeypatch.setenv("HOSTNAME", "SECRETHOST")
    rec = recorder(operator_env, Clock(ago(5)))
    rec.heartbeat()
    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_FAILED, {"processed": 1}, error_type=RuntimeError("secret text"))
    r = operator_env.get(URL)
    assert r.status_code == 200
    inst = r.json()["instances"][0]
    assert set(inst) == {"instance_id", "role", "heartbeat_status", "started_at", "uptime_seconds",
                         "last_heartbeat_at", "heartbeat_age_seconds", "stopped_at", "cycles"}
    assert inst["instance_id"] == rec.instance_id
    cycle = inst["cycles"][KIND_EXTERNAL_DISPATCH]
    assert cycle["last_outcome"] == "failed" and cycle["last_error_type"] == "RuntimeError"
    for leak in ("SECRETHOST", "424242", "deadbeef", "secret text"):
        assert leak not in r.text


def test_endpoint_lists_at_most_the_cap_and_says_when_truncated(operator_env):
    for i in range(MAX_INSTANCES_LISTED + 5):
        recorder(operator_env, Clock(ago(i + 1))).heartbeat()
    body = status_via_api(operator_env)
    assert len(body["instances"]) == MAX_INSTANCES_LISTED and body["instances_truncated"] is True
    ages = [i["heartbeat_age_seconds"] for i in body["instances"]]
    assert ages == sorted(ages)  # most recent heartbeat first


def test_heartbeat_fresh_is_not_reported_as_job_health_or_delivery(operator_env):
    rec = recorder(operator_env, Clock(ago(1)))
    rec.heartbeat()
    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_FAILED, error_type="OperationalError")
    body = status_via_api(operator_env)
    inst = body["instances"][0]
    # distinct signals: the process is fresh while its dispatch cycle FAILED
    assert inst["heartbeat_status"] == "fresh"
    assert inst["cycles"][KIND_EXTERNAL_DISPATCH]["last_outcome"] == "failed"
    assert "SMTP" in body["definitions"]["heartbeat"] and "NOTHING" in body["definitions"]["heartbeat"]
    assert "NOT mean the queue was empty" in body["definitions"]["cycle"]
    assert set(body["instances"][0]["cycles"]) <= set(CYCLE_KINDS)
    assert "healthy" not in str(body["summary"]).lower()  # we never call a heartbeat "healthy"


def test_read_cycle_counts_are_whitelisted_again_at_read_time(operator_env):
    rec = recorder(operator_env, Clock(ago(1)))
    rec.record_cycle(KIND_EXTERNAL_DISPATCH, OUTCOME_COMPLETED, {"processed": 1})
    with operator_env.sm() as s:  # a row tampered with directly in the database
        row = s.execute(select(WorkerCycleStatus)).scalar_one()
        row.last_counts = {"processed": 1, "leaked_email": "a@b.c", "failed": "oops"}
        s.commit()
    rec.heartbeat()
    cyc = operator_env.get(URL).json()["instances"][0]["cycles"][KIND_EXTERNAL_DISPATCH]
    assert set(cyc["last_cycle_counts"]) == set(COUNT_KEYS[KIND_EXTERNAL_DISPATCH])
    assert "a@b.c" not in str(cyc) and cyc["last_cycle_counts"]["failed"] == 0
