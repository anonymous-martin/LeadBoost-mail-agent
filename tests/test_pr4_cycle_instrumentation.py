"""PR4 -- job-cycle outcomes recorded by the scheduler jobs, and the lifecycle of the liveness row.

The point of these tests: a cycle function that CATCHES ITS OWN database error and returns normally
must be recorded as failed, not as success; and no failure to record anything may ever re-send a
mail or change a persisted dispatch outcome.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import worker as worker_mod
from mailer_agent.followup import scheduler as sched
from mailer_agent.mail import external_dispatch_worker as w
from mailer_agent.mail import outreach_generation_worker as og
from mailer_agent.mail.mailbox_inbound import (
    STATUS_CREDENTIAL_UNAVAILABLE,
    STATUS_IMAP_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_UNEXPECTED,
    MailboxCycleReport,
    MailboxPollOutcome,
    summarize_mailbox_cycle,
)
from mailer_agent.models import (
    Base,
    ExternalDispatch,
    WorkerCycleStatus,
    WorkerRuntimeStatus,
)
from mailer_agent.models import ExternalDispatchState as S
from mailer_agent.operator_metrics import heartbeat_status
from mailer_agent.worker_status import (
    COUNT_KEYS,
    KIND_EXTERNAL_DISPATCH,
    KIND_GENERATION,
    KIND_INBOUND_LEGACY,
    KIND_INBOUND_MAILBOX,
    WorkerStatusRecorder,
    summarize_dispatch_results,
    summarize_generation_results,
)
from tests.dispatch_support import FakeSender, TrackingFactory, seed_dispatch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = "w-pr4"


@pytest.fixture()
def factory(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'i.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    Base.metadata.create_all(bind=eng)
    yield TrackingFactory(sessionmaker(bind=eng))
    eng.dispose()


@pytest.fixture()
def runtime():
    return w.DispatchRuntime()


@pytest.fixture()
def rec(factory, monkeypatch):
    """A real recorder on the test DB, installed as this process's recorder."""
    r = WorkerStatusRecorder("worker", session_factory=factory.sm)
    monkeypatch.setattr(sched, "_runtime_recorder", r)
    return r


def cycle_row(factory, kind):
    with factory.sm() as s:
        return s.execute(select(WorkerCycleStatus).where(WorkerCycleStatus.kind == kind)).scalar_one_or_none()


def bind_dispatch_job(monkeypatch, factory, runtime):
    real = w.run_external_dispatch_cycle
    monkeypatch.setattr(w, "run_external_dispatch_cycle",
                        lambda **kw: real(session_factory=factory, worker_id=WORKER, runtime=runtime, **kw))


def bind_generation_job(monkeypatch, factory, runtime):
    real = og.run_generation_cycle
    monkeypatch.setattr(og, "run_generation_cycle",
                        lambda **kw: real(session_factory=factory, worker_id=WORKER, runtime=runtime, **kw))


def _db_error(*_a, **_k):
    raise OperationalError("SELECT secret FROM t", {"p": "SECRET-PARAM"}, Exception("server closed the connection"))


# =============================================================== dispatch job
def test_a_swallowed_database_error_is_recorded_as_FAILED_not_success(factory, runtime, rec, monkeypatch):
    bind_dispatch_job(monkeypatch, factory, runtime)
    monkeypatch.setattr(w, "claim_next_external_dispatch", _db_error)  # the cycle catches + logs this itself
    # prove the premise: the cycle function itself returns normally with an EMPTY list
    assert w.run_external_dispatch_cycle() == []
    sched.dispatch_external_dispatches_job()  # the job does not raise either
    row = cycle_row(factory, KIND_EXTERNAL_DISPATCH)
    assert row.last_outcome == "failed" and row.last_completed_at is None
    assert row.last_error_type == "DatabaseErrorCaughtByCycle"
    assert "SECRET" not in repr((row.last_error_type, row.last_counts))


def test_an_empty_cycle_with_no_error_is_completed_and_distinguishable_from_an_aborted_one(factory, runtime, rec, monkeypatch):
    bind_dispatch_job(monkeypatch, factory, runtime)
    sched.dispatch_external_dispatches_job()
    row = cycle_row(factory, KIND_EXTERNAL_DISPATCH)
    assert row.last_outcome == "completed" and row.last_counts["processed"] == 0 and row.last_completed_at is not None


def test_an_exception_escaping_the_cycle_is_recorded_failed_with_its_class_only(factory, rec, monkeypatch):
    monkeypatch.setattr(w, "run_external_dispatch_cycle", lambda **kw: (_ for _ in ()).throw(KeyError("tenant-secret")))
    sched.dispatch_external_dispatches_job()
    row = cycle_row(factory, KIND_EXTERNAL_DISPATCH)
    assert (row.last_outcome, row.last_error_type) == ("failed", "KeyError")
    assert "tenant-secret" not in repr(row.last_counts)


def test_a_real_send_cycle_records_counts_and_completed(factory, runtime, rec, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    fake = FakeSender()
    monkeypatch.setattr(w, "send_email", fake)
    for i in range(2):
        with factory.sm() as s:
            seed_dispatch(s, email=f"l{i}@example.com")
    bind_dispatch_job(monkeypatch, factory, runtime)
    sched.dispatch_external_dispatches_job()
    row = cycle_row(factory, KIND_EXTERNAL_DISPATCH)
    assert row.last_outcome == "completed"
    assert (row.last_counts["processed"], row.last_counts["smtp_attempted"], row.last_counts["smtp_accepted"]) == (2, 2, 2)
    assert row.last_counts["failed"] == row.last_counts["unknown"] == 0 and len(fake.calls) == 2


def test_an_outcome_that_could_not_be_persisted_degrades_the_cycle(factory, rec, monkeypatch):
    results = [SimpleNamespace(outcome="unknown", smtp_attempted=True, persisted=False),
               SimpleNamespace(outcome="sent", smtp_attempted=True, persisted=True)]
    monkeypatch.setattr(w, "run_external_dispatch_cycle", lambda **kw: results)
    sched.dispatch_external_dispatches_job()
    row = cycle_row(factory, KIND_EXTERNAL_DISPATCH)
    assert row.last_outcome == "degraded"
    assert (row.last_counts["unknown"], row.last_counts["smtp_accepted"], row.last_counts["outcome_not_persisted"]) == (1, 1, 1)


def test_dispatch_and_generation_summaries_count_only_what_the_results_say():
    d = summarize_dispatch_results([
        SimpleNamespace(outcome="sent", smtp_attempted=True, persisted=True),
        SimpleNamespace(outcome="failed", smtp_attempted=False, persisted=True),   # a pre-SMTP gate refusal
        SimpleNamespace(outcome="unknown", smtp_attempted=True, persisted=True),
    ])
    assert d == {"processed": 3, "smtp_attempted": 2, "smtp_accepted": 1, "failed": 1, "unknown": 1,
                 "outcome_not_persisted": 0}
    g = summarize_generation_results([
        SimpleNamespace(outcome="generated", persisted=True), SimpleNamespace(outcome="failed", persisted=True),
        SimpleNamespace(outcome="lease_lost", persisted=False),
    ])
    assert g == {"processed": 3, "generated": 1, "failed": 1, "outcome_not_persisted": 1}


# =============================================================== generation job
def test_generation_swallowed_database_error_is_recorded_failed(factory, runtime, rec, monkeypatch):
    bind_generation_job(monkeypatch, factory, runtime)
    monkeypatch.setattr(og, "claim_next_generation_dispatch", _db_error)
    assert og.run_generation_cycle() == []
    sched.generate_external_dispatches_job()
    row = cycle_row(factory, KIND_GENERATION)
    assert (row.last_outcome, row.last_error_type) == ("failed", "DatabaseErrorCaughtByCycle")


def test_generation_empty_cycle_is_completed(factory, runtime, rec, monkeypatch):
    bind_generation_job(monkeypatch, factory, runtime)
    sched.generate_external_dispatches_job()
    assert cycle_row(factory, KIND_GENERATION).last_outcome == "completed"


# =============================================================== recording can never touch a send
def _boom_factory():
    raise RuntimeError("postgresql://svc:SECRETPW@db/prod")


def test_a_failing_status_write_never_causes_a_second_send_or_changes_the_persisted_outcome(factory, runtime, monkeypatch, caplog):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    fake = FakeSender()
    monkeypatch.setattr(w, "send_email", fake)
    with factory.sm() as s:
        did = seed_dispatch(s).id
    # every status write fails -- heartbeat, cycle record and stop
    monkeypatch.setattr(sched, "_runtime_recorder",
                        WorkerStatusRecorder("worker", session_factory=_boom_factory))
    bind_dispatch_job(monkeypatch, factory, runtime)
    sched.dispatch_external_dispatches_job()   # must not raise
    sched.worker_heartbeat_job()
    sched.dispatch_external_dispatches_job()   # a later cycle, still failing to record
    assert len(fake.calls) == 1, "a status-write failure must never cause another SMTP attempt"
    with factory.sm() as s:
        d = s.get(ExternalDispatch, did)
        assert d.state == S.SENT.value
    assert "SECRETPW" not in caplog.text


def test_a_recorder_that_itself_raises_cannot_break_a_job_or_a_send(factory, runtime, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    fake = FakeSender()
    monkeypatch.setattr(w, "send_email", fake)
    with factory.sm() as s:
        did = seed_dispatch(s).id

    class Exploding:
        def record_cycle(self, *a, **k):
            raise RuntimeError("recorder bug")

    monkeypatch.setattr(sched, "_runtime_recorder", Exploding())
    bind_dispatch_job(monkeypatch, factory, runtime)
    sched.dispatch_external_dispatches_job()
    sched.dispatch_external_dispatches_job()
    assert len(fake.calls) == 1
    with factory.sm() as s:
        assert s.get(ExternalDispatch, did).state == S.SENT.value


def test_recording_happens_after_the_work_in_its_own_session_never_inside_a_claim_transaction(factory, runtime, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    monkeypatch.setattr(w, "send_email", FakeSender())
    with factory.sm() as s:
        seed_dispatch(s)
    seen_open = []

    class Probe(WorkerStatusRecorder):
        def record_cycle(self, *a, **k):
            seen_open.append(factory.open_transactions())  # dispatch sessions still open at record time
            return super().record_cycle(*a, **k)

    monkeypatch.setattr(sched, "_runtime_recorder", Probe("worker", session_factory=factory.sm))
    bind_dispatch_job(monkeypatch, factory, runtime)
    sched.dispatch_external_dispatches_job()
    assert seen_open == [[]], "no dispatch/claim session may be open when the cycle outcome is written"


def test_calling_a_job_with_no_scheduler_started_records_nothing(factory, runtime, monkeypatch):
    monkeypatch.setattr(sched, "_runtime_recorder", None)
    bind_dispatch_job(monkeypatch, factory, runtime)
    sched.dispatch_external_dispatches_job()
    with factory.sm() as s:
        assert s.execute(select(WorkerCycleStatus)).first() is None


# =============================================================== mailbox-bound polling job
def _outcome(status, **kw):
    return MailboxPollOutcome(mailbox_id=1, status=status, **kw)


def test_mailbox_cycle_outcome_matrix():
    assert summarize_mailbox_cycle(MailboxCycleReport())[0] == "completed"          # nothing eligible: a successful listing
    assert summarize_mailbox_cycle(MailboxCycleReport(listing_failed=True))[0:3:2] == ("failed", "MailboxListingFailed")
    ok = summarize_mailbox_cycle(MailboxCycleReport([_outcome(STATUS_OK, processed=2), _outcome(STATUS_SKIPPED)]))
    assert ok[0] == "completed" and ok[1]["messages_marked_seen"] == 2 and ok[1]["mailboxes_skipped_ineligible"] == 1
    for status, key in ((STATUS_IMAP_ERROR, "mailboxes_imap_error"),
                        (STATUS_CREDENTIAL_UNAVAILABLE, "mailboxes_credential_unavailable"),
                        (STATUS_UNEXPECTED, "mailboxes_unexpected_error")):
        got = summarize_mailbox_cycle(MailboxCycleReport([_outcome(STATUS_OK), _outcome(status, error_type="X")]))
        assert got[0] == "degraded" and got[1][key] == 1 and got[2] == "X"
    left = summarize_mailbox_cycle(MailboxCycleReport([_outcome(STATUS_OK, left_unseen=1)]))
    assert left[0] == "degraded" and left[1]["messages_left_unseen"] == 1  # retry/left-unseen is not "completed"


def test_mailbox_counter_keys_match_the_recorder_whitelist_exactly():
    keys = set(summarize_mailbox_cycle(MailboxCycleReport())[1])
    assert keys == set(COUNT_KEYS[KIND_INBOUND_MAILBOX])  # a drift would silently drop a counter


def test_mailbox_job_records_what_the_report_says(factory, rec, monkeypatch):
    report = MailboxCycleReport([
        _outcome(STATUS_OK, processed=3, handled={"persisted": 1, "duplicate": 1, "unmatched": 1},
                 classification_succeeded=1),
        _outcome(STATUS_CREDENTIAL_UNAVAILABLE, error_type="MailboxEncryptionUnavailable"),
    ])
    monkeypatch.setattr(sched, "poll_all_mailboxes_report", lambda: report)
    sched.poll_mailbox_inboxes_job()
    row = cycle_row(factory, KIND_INBOUND_MAILBOX)
    assert row.last_outcome == "degraded" and row.last_error_type == "MailboxEncryptionUnavailable"
    c = row.last_counts
    assert (c["handled_persisted"], c["handled_duplicate"], c["handled_unmatched"], c["classification_succeeded"]) == (1, 1, 1, 1)
    assert (c["mailboxes_listed"], c["mailboxes_credential_unavailable"], c["messages_marked_seen"]) == (2, 1, 3)


def test_mailbox_job_listing_failure_is_failed_not_an_empty_success(factory, rec, monkeypatch):
    monkeypatch.setattr(sched, "poll_all_mailboxes_report", lambda: MailboxCycleReport(listing_failed=True))
    sched.poll_mailbox_inboxes_job()
    assert cycle_row(factory, KIND_INBOUND_MAILBOX).last_outcome == "failed"


# =============================================================== legacy global IMAP path (kept distinct)
def _legacy_run(monkeypatch, emails, results, session_exc=None):
    monkeypatch.setattr(sched, "fetch_unseen_replies", lambda: emails)
    it = iter(results)

    def process(db, email_in, **kw):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(sched, "process_inbound_email", process)

    @contextlib.contextmanager
    def scope():
        yield object()
        if session_exc:
            raise session_exc

    monkeypatch.setattr(sched, "session_scope", scope)


def _mail():
    return SimpleNamespace(from_email="sender@example.com")


def test_legacy_poll_is_recorded_under_its_own_kind_with_bucketed_results(factory, rec, monkeypatch):
    results = [
        {"matched": True, "action": "skipped_duplicate"},
        {"matched": False, "from_email": "sender@example.com"},          # real legacy shape: no reason
        {"matched": True, "action": "replied", "classification_success": True},
        {"matched": True, "action": "x", "classification_success": False},
    ]
    _legacy_run(monkeypatch, [_mail()] * 4, results)
    sched.poll_replies_job()
    row = cycle_row(factory, KIND_INBOUND_LEGACY)
    c = row.last_counts
    assert row.last_outcome == "completed"
    assert (c["emails_fetched"], c["handled_duplicate"], c["handled_unmatched"], c["handled_persisted"]) == (4, 1, 1, 2)
    assert (c["classification_succeeded"], c["classification_failed"], c["handler_failures"]) == (1, 1, 0)
    assert cycle_row(factory, KIND_INBOUND_MAILBOX) is None, "legacy results must never land in the mailbox-bound record"
    assert "sender@example.com" not in repr((c, row.last_error_type))


def test_legacy_handler_exception_degrades_the_cycle(factory, rec, monkeypatch):
    _legacy_run(monkeypatch, [_mail(), _mail()], [{"matched": True, "action": "x"}, RuntimeError("boom sender@example.com")])
    sched.poll_replies_job()
    row = cycle_row(factory, KIND_INBOUND_LEGACY)
    assert row.last_outcome == "degraded" and row.last_counts["handler_failures"] == 1


def test_legacy_empty_fetch_records_nothing_because_it_is_not_evidence(factory, rec, monkeypatch):
    monkeypatch.setattr(sched, "fetch_unseen_replies", list)
    sched.poll_replies_job()
    assert cycle_row(factory, KIND_INBOUND_LEGACY) is None


def test_legacy_commit_failure_is_recorded_failed_and_still_raises_as_before(factory, rec, monkeypatch):
    _legacy_run(monkeypatch, [_mail()], [{"matched": True, "action": "x"}], session_exc=OperationalError("c", {}, Exception("x")))
    with pytest.raises(OperationalError):
        sched.poll_replies_job()  # existing behaviour preserved
    assert cycle_row(factory, KIND_INBOUND_LEGACY).last_outcome == "failed"


# =============================================================== scheduler wiring
@pytest.fixture()
def stubbed_scheduler(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(BackgroundScheduler, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(BackgroundScheduler, "shutdown", lambda self, *a, **k: None)
    monkeypatch.setattr(sched, "_scheduler", None)
    yield
    monkeypatch.setattr(sched, "_scheduler", None)


def test_start_scheduler_registers_the_heartbeat_job_and_labels_the_role(stubbed_scheduler):
    s = sched.start_scheduler(role="worker")
    job = s.get_job("worker_heartbeat")
    assert job is not None and job.func is sched.worker_heartbeat_job and job.max_instances == 1
    assert job.trigger.interval.total_seconds() == 30
    assert job.next_run_time is not None  # first beat is immediate, not after one full interval
    assert sched._runtime_recorder.role == "worker" and len(sched._runtime_recorder.instance_id) == 32


def test_total_job_count_fits_the_default_thread_pool_so_the_heartbeat_cannot_be_starved(stubbed_scheduler):
    s = sched.start_scheduler()
    assert len(s.get_jobs()) == 10  # APScheduler's default pool is 10 threads; every job has max_instances=1
    assert all(j.max_instances == 1 for j in s.get_jobs())


def test_starting_the_scheduler_itself_writes_nothing(stubbed_scheduler, factory, monkeypatch):
    sched.start_scheduler()
    # the recorder exists but has written no row: only the heartbeat JOB writes
    with factory.sm() as s:
        assert s.execute(select(WorkerRuntimeStatus)).first() is None


def test_a_misconfigured_zero_interval_is_floored_not_fatal(stubbed_scheduler, monkeypatch):
    monkeypatch.setattr(sched.settings, "worker_heartbeat_interval_seconds", 0)
    assert sched.start_scheduler().get_job("worker_heartbeat").trigger.interval.total_seconds() == 5


# =============================================================== graceful shutdown marks the row LAST
def _patch_shutdown(monkeypatch, order):
    monkeypatch.setattr(worker_mod, "begin_external_dispatch_shutdown", lambda: order.append("stop_claiming"))
    monkeypatch.setattr(worker_mod, "stop_scheduler", lambda: order.append("stop_scheduler"))
    monkeypatch.setattr(worker_mod, "drain_external_dispatches_on_shutdown",
                        lambda: order.append("drain") or w.ShutdownReport(True, (), ()))

    class _Scope:
        def __enter__(self):
            order.append("release_claims")
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(worker_mod, "session_scope", lambda: _Scope())
    monkeypatch.setattr(worker_mod, "release_all_claims", lambda db, wid: 0)


def test_graceful_shutdown_marks_stopped_as_the_very_last_step(factory, monkeypatch):
    order = []
    _patch_shutdown(monkeypatch, order)

    class Probe(WorkerStatusRecorder):
        def mark_stopped(self):
            order.append("mark_stopped")
            return super().mark_stopped()

    r = Probe("worker", session_factory=factory.sm)
    r.heartbeat()
    monkeypatch.setattr(sched, "_runtime_recorder", r)
    worker_mod.graceful_shutdown("w-1")
    assert order == ["stop_claiming", "stop_scheduler", "drain", "release_claims", "mark_stopped"]
    with factory.sm() as s:
        row = s.execute(select(WorkerRuntimeStatus)).scalar_one()
    assert row.stopped_at is not None
    assert sched._runtime_recorder is None  # nothing more is recorded once stopped


def test_graceful_shutdown_still_completes_when_the_database_is_unreachable(monkeypatch):
    order = []
    _patch_shutdown(monkeypatch, order)
    monkeypatch.setattr(sched, "_runtime_recorder", WorkerStatusRecorder("worker", session_factory=_boom_factory))
    worker_mod.graceful_shutdown("w-1")  # must not raise
    assert order == ["stop_claiming", "stop_scheduler", "drain", "release_claims"]


def test_the_worker_module_labels_its_scheduler_as_the_worker_role():
    src = (Path(ROOT) / "worker.py").read_text()
    assert "start_scheduler(role=ROLE_WORKER)" in src


# =============================================================== REAL worker processes: stop vs kill
def _spawn(tmp_path):
    db = tmp_path / "proc.db"
    env = {**os.environ, "PYTHONPATH": ROOT, "DATABASE_URL": f"sqlite:///{db}", "LIVE_SENDING_ENABLED": "false",
           "WORKER_HEARTBEAT_INTERVAL_SECONDS": "5"}
    proc = subprocess.Popen([sys.executable, "worker.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    started = threading.Event()

    def pump():
        for line in proc.stdout:
            lines.append(line)
            if "Unified scheduler started" in line:
                started.set()

    threading.Thread(target=pump, daemon=True).start()
    assert started.wait(60), "worker did not start:\n" + "".join(lines)
    return proc, db, lines


def _read_runtime(db):
    con = sqlite3.connect(db, timeout=10)
    try:
        return con.execute("SELECT role, started_at, heartbeat_at, stopped_at FROM worker_runtime_status").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _wait_for(predicate, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        got = predicate()
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def test_real_worker_sigterm_records_a_graceful_stop(tmp_path):
    proc, db, lines = _spawn(tmp_path)
    try:
        rows = _wait_for(lambda: _read_runtime(db), 30, "first heartbeat")
        assert len(rows) == 1 and rows[0][0] == "worker" and rows[0][3] is None  # running: not stopped
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    rows = _read_runtime(db)
    assert len(rows) == 1 and rows[0][3] is not None, "graceful SIGTERM must record stopped_at\n" + "".join(lines)
    assert heartbeat_status(rows[0][3], None, None, 120) == "stopped"


def test_real_worker_sigkill_leaves_no_stop_marker_and_the_heartbeat_stops_advancing(tmp_path):
    proc, db, _lines = _spawn(tmp_path)
    try:
        first = _wait_for(lambda: _read_runtime(db), 30, "first heartbeat")[0]
        # the heartbeat really ticks while the process lives (5 s interval)
        advanced = _wait_for(lambda: (r := _read_runtime(db)) and r[0][2] != first[2] and r[0], 20, "a second heartbeat")
        proc.kill()  # SIGKILL: no handler, no shutdown path
        proc.wait(15)
    finally:
        if proc.poll() is None:
            proc.kill()
    time.sleep(6.5)  # longer than one heartbeat interval: a live process would have beaten again
    after = _read_runtime(db)[0]
    assert after[3] is None, "a killed process must NOT be marked stopped"
    assert after[2] == advanced[2], "a killed process must stop heartbeating"
    # ...so it ages into stale purely by the passage of time
    from datetime import datetime

    from mailer_agent.worker_status import aware

    hb = aware(datetime.fromisoformat(after[2]))
    assert heartbeat_status(None, hb, hb + timedelta(seconds=121), 120) == "stale"
    assert heartbeat_status(None, hb, hb + timedelta(seconds=60), 120) == "fresh"
