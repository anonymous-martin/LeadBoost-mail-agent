"""
Scheduler registration and worker shutdown for ExternalDispatch.

Shutdown tests are deterministic (threading.Event choreography, no sleeps
standing in for synchronisation) except one real-subprocess test that proves
SIGTERM actually reaches the shutdown code -- the original Phase 8 release
logic never ran on SIGTERM.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import worker as worker_mod
from mailer_agent.config import get_settings
from mailer_agent.followup import scheduler as sched
from mailer_agent.followup.work_claiming import ClaimedDispatch, claim_due_contacts, make_worker_id
from mailer_agent.mail import external_dispatch_worker as w
from mailer_agent.models import Base, ContactStatus, ExternalDispatch, ExternalDispatchState as S, Message, MessageStatus
from tests.dispatch_support import FakeSender, TrackingFactory, age, seed_dispatch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def factory(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'s.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    Base.metadata.create_all(bind=eng)
    yield TrackingFactory(sessionmaker(bind=eng))
    eng.dispose()


def _state(factory, did):
    with factory.sm() as s:
        d = s.get(ExternalDispatch, did)
        m = s.get(Message, d.message_id)
        return d.state, d.claimed_by, d.error_message, m.status


# ==================================================================== SCHEDULER

@pytest.fixture()
def started(monkeypatch):
    """start_scheduler() without spawning APScheduler's thread."""
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(BackgroundScheduler, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(sched, "_scheduler", None)
    s = sched.start_scheduler()
    yield s
    monkeypatch.setattr(sched, "_scheduler", None)


def _secs(job):
    return job.trigger.interval.total_seconds()


def test_dedicated_dispatch_and_recovery_jobs_are_registered(started):
    st = get_settings()
    d = started.get_job("dispatch_external_dispatches")
    r = started.get_job("recover_external_dispatch_leases")
    assert d is not None and r is not None
    assert d.func is sched.dispatch_external_dispatches_job
    assert r.func is sched.recover_external_dispatch_leases_job
    assert _secs(d) == st.external_dispatch_poll_seconds == 15
    assert _secs(r) == st.external_dispatch_recovery_poll_seconds
    assert d.max_instances == 1 and r.max_instances == 1


def test_dispatch_job_does_not_use_the_300s_followup_cadence(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(BackgroundScheduler, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(sched, "_scheduler", None)
    monkeypatch.setattr(sched.settings, "followup_poll_seconds", 777)
    monkeypatch.setattr(sched.settings, "external_dispatch_poll_seconds", 17)
    s = sched.start_scheduler()
    try:
        assert _secs(s.get_job("dispatch_external_dispatches")) == 17
        assert _secs(s.get_job("dispatch_new_contacts")) == 777
        assert _secs(s.get_job("dispatch_followups")) == 777
    finally:
        monkeypatch.setattr(sched, "_scheduler", None)


def test_existing_jobs_are_unchanged(started):
    st = get_settings()
    assert _secs(started.get_job("dispatch_new_contacts")) == st.followup_poll_seconds == 300
    assert _secs(started.get_job("dispatch_followups")) == 300
    assert _secs(started.get_job("poll_replies")) == st.imap_poll_seconds
    assert _secs(started.get_job("health_check")) == 300
    assert started.get_job("dispatch_new_contacts").func is sched.dispatch_new_contacts_job


def test_dispatch_job_delegates_to_worker_and_never_to_followup_drafting(monkeypatch):
    called = []
    monkeypatch.setattr(w, "run_external_dispatch_cycle", lambda **kw: called.append("cycle") or [])
    monkeypatch.setattr(sched, "run_followup_cycle",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("follow-up engine must not run")))
    sched.dispatch_external_dispatches_job()
    assert called == ["cycle"]


def test_jobs_swallow_errors_so_the_scheduler_keeps_ticking(monkeypatch):
    monkeypatch.setattr(w, "run_external_dispatch_cycle", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(w, "run_external_dispatch_lease_recovery", lambda **kw: (_ for _ in ()).throw(RuntimeError("y")))
    sched.dispatch_external_dispatches_job()
    sched.recover_external_dispatch_leases_job()


def test_integration_contacts_never_enter_followup_claiming(factory, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    monkeypatch.setattr(w, "send_email", FakeSender())
    with factory.sm() as s:
        did = seed_dispatch(s).id
    for stage in ("queued", "after dispatch"):
        with factory.sm() as s:
            for status in (ContactStatus.NEW.value, ContactStatus.ACTIVE.value):
                assert claim_due_contacts(s, make_worker_id("t"), status=status) == [], stage
            s.commit()
        if stage == "queued":
            w.process_next_external_dispatch(session_factory=factory, worker_id="w", runtime=w.DispatchRuntime())
    assert _state(factory, did)[0] == S.SENT.value


# ===================================================================== SHUTDOWN

def _claim_of(factory, did) -> ClaimedDispatch:
    with factory.sm() as s:
        d = s.get(ExternalDispatch, did)
        return ClaimedDispatch(d.id, d.organization_id, d.claimed_by, d.claimed_at)


def test_shutdown_resolves_an_owned_inflight_dispatch_to_unknown_not_queued(factory):
    with factory.sm() as s:
        did = seed_dispatch(s, state=S.SENDING.value, claimed_by="w-1", claimed_at=age(5)).id
    rt = w.DispatchRuntime()
    rt.register(_claim_of(factory, did))

    report = w.drain_external_dispatches_on_shutdown(session_factory=factory, runtime=rt, drain_seconds=0.05)

    assert report.drained is False and report.abandoned_unknown == (did,)
    state, claimed_by, err, msg_status = _state(factory, did)
    assert state == S.UNKNOWN.value and claimed_by is None
    assert msg_status == MessageStatus.UNKNOWN.value
    assert "worker_shutdown" in err and "NOT be retried" in err


def test_shutdown_never_sweeps_by_worker_id_alone(factory):
    """Sibling process with the SAME worker_id string (e.g. a shared WORKER_ID
    env), another worker's row, and a QUEUED row -- none may be touched."""
    with factory.sm() as s:
        mine = seed_dispatch(s, email="m@example.com", state=S.SENDING.value, claimed_by="w-1", claimed_at=age(5)).id
        sibling = seed_dispatch(s, email="s@example.com", state=S.SENDING.value, claimed_by="w-1", claimed_at=age(7)).id
        other = seed_dispatch(s, email="o@example.com", state=S.SENDING.value, claimed_by="w-2", claimed_at=age(5)).id
        queued = seed_dispatch(s, email="q@example.com").id
    rt = w.DispatchRuntime()
    rt.register(_claim_of(factory, mine))                     # only this process's own in-flight id

    report = w.drain_external_dispatches_on_shutdown(session_factory=factory, runtime=rt, drain_seconds=0.01)

    assert report.abandoned_unknown == (mine,)
    assert _state(factory, mine)[0] == S.UNKNOWN.value
    assert _state(factory, sibling)[0] == S.SENDING.value and _state(factory, sibling)[1] == "w-1"
    assert _state(factory, other)[0] == S.SENDING.value and _state(factory, other)[1] == "w-2"
    assert _state(factory, queued)[0] == S.QUEUED.value


def test_shutdown_leaves_a_row_alone_when_the_fence_no_longer_matches(factory):
    with factory.sm() as s:
        did = seed_dispatch(s, state=S.SENDING.value, claimed_by="w-1", claimed_at=age(5)).id
    stale = ClaimedDispatch(did, "org-a", "w-1", age(999))     # not the live token
    rt = w.DispatchRuntime()
    rt.register(stale)
    report = w.drain_external_dispatches_on_shutdown(session_factory=factory, runtime=rt, drain_seconds=0.01)
    assert report.abandoned_unknown == () and report.still_owned_elsewhere == (did,)
    assert _state(factory, did)[0] == S.SENDING.value


def test_shutdown_stops_new_claims_and_leaves_queued_rows_queued(factory, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    f = FakeSender()
    monkeypatch.setattr(w, "send_email", f)
    with factory.sm() as s:
        ids = [seed_dispatch(s, email=f"l{i}@example.com").id for i in range(3)]
    rt = w.DispatchRuntime()
    w.begin_external_dispatch_shutdown(rt)

    assert w.run_external_dispatch_cycle(session_factory=factory, worker_id="w", runtime=rt) == []
    assert w.process_next_external_dispatch(session_factory=factory, worker_id="w", runtime=rt) is None
    assert f.calls == []
    assert [_state(factory, i)[0] for i in ids] == [S.QUEUED.value] * 3
    assert all(_state(factory, i)[1] is None for i in ids)


def test_shutdown_requested_mid_transaction_b_rolls_back_to_queued_without_sending(factory, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    f = FakeSender()
    monkeypatch.setattr(w, "send_email", f)
    rt = w.DispatchRuntime()
    real_gen = w.generate_message_id
    monkeypatch.setattr(w, "generate_message_id", lambda e: (rt.request_shutdown(), real_gen(e))[1])
    with factory.sm() as s:
        did = seed_dispatch(s).id

    assert w.process_next_external_dispatch(session_factory=factory, worker_id="w", runtime=rt) is None

    state, claimed_by, _, msg_status = _state(factory, did)
    assert (state, claimed_by, msg_status) == (S.QUEUED.value, None, MessageStatus.DRAFT.value)
    with factory.sm() as s:
        assert s.get(Message, s.get(ExternalDispatch, did).message_id).message_id_header is None
    assert f.calls == [] and rt.inflight() == []


def test_active_dispatch_that_finishes_inside_the_drain_window_is_left_alone(factory, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    in_smtp, release = threading.Event(), threading.Event()
    f = FakeSender(on_call=lambda kw: (in_smtp.set(), release.wait(10)))
    monkeypatch.setattr(w, "send_email", f)
    with factory.sm() as s:
        did = seed_dispatch(s).id
    rt = w.DispatchRuntime()
    t = threading.Thread(target=w.process_next_external_dispatch,
                         kwargs=dict(session_factory=factory, worker_id="w-1", runtime=rt))
    t.start()
    assert in_smtp.wait(10)
    assert [c.dispatch_id for c in rt.inflight()] == [did]
    w.begin_external_dispatch_shutdown(rt)

    threading.Timer(0.2, release.set).start()                    # SMTP completes during the drain
    report = w.drain_external_dispatches_on_shutdown(session_factory=factory, runtime=rt, drain_seconds=10)
    t.join(10)

    assert report.drained is True and report.abandoned_unknown == ()
    assert _state(factory, did)[0] == S.SENT.value               # real outcome preserved
    assert len(f.calls) == 1


def test_active_dispatch_past_the_deadline_becomes_unknown_and_a_late_success_cannot_overwrite(factory, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    in_smtp, release = threading.Event(), threading.Event()
    f = FakeSender(on_call=lambda kw: (in_smtp.set(), release.wait(10)))
    monkeypatch.setattr(w, "send_email", f)
    with factory.sm() as s:
        did = seed_dispatch(s).id
    rt = w.DispatchRuntime()
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", w.process_next_external_dispatch(
        session_factory=factory, worker_id="w-1", runtime=rt)))
    t.start()
    assert in_smtp.wait(10)
    w.begin_external_dispatch_shutdown(rt)

    report = w.drain_external_dispatches_on_shutdown(session_factory=factory, runtime=rt, drain_seconds=0.05)
    assert report.drained is False and report.abandoned_unknown == (did,)
    assert _state(factory, did)[0] == S.UNKNOWN.value
    release.set()                                                 # SMTP now "succeeds", late
    t.join(10)

    state, _, err, msg_status = _state(factory, did)
    assert state == S.UNKNOWN.value and msg_status == MessageStatus.UNKNOWN.value
    assert box["r"].outcome == "sent" and box["r"].persisted is False
    assert "late_outcome_after_lease_loss" in err
    assert len(f.calls) == 1                                      # never resent


def test_two_workers_shutdown_independently(factory, monkeypatch):
    """Worker 1 shuts down with one in-flight dispatch; worker 2's in-flight
    dispatch (different runtime, different row) is unaffected."""
    with factory.sm() as s:
        a = seed_dispatch(s, email="a@example.com", state=S.SENDING.value, claimed_by="w-1", claimed_at=age(3)).id
        b = seed_dispatch(s, email="b@example.com", state=S.SENDING.value, claimed_by="w-2", claimed_at=age(3)).id
    r1, r2 = w.DispatchRuntime(), w.DispatchRuntime()
    r1.register(_claim_of(factory, a))
    r2.register(_claim_of(factory, b))

    w.begin_external_dispatch_shutdown(r1)
    w.drain_external_dispatches_on_shutdown(session_factory=factory, runtime=r1, drain_seconds=0.01)

    assert _state(factory, a)[0] == S.UNKNOWN.value
    assert _state(factory, b)[0] == S.SENDING.value and r2.stopping is False


# ----------------------------------------------------------- SIGTERM behaviour

def test_graceful_shutdown_order(monkeypatch):
    order = []
    monkeypatch.setattr(worker_mod, "begin_external_dispatch_shutdown", lambda: order.append("stop_claiming"))
    monkeypatch.setattr(worker_mod, "stop_scheduler", lambda: order.append("stop_scheduler"))
    monkeypatch.setattr(worker_mod, "drain_external_dispatches_on_shutdown",
                        lambda: order.append("drain_and_resolve") or w.ShutdownReport(True, (), ()))

    class _Scope:
        def __enter__(self): order.append("release_contact_claims"); return object()
        def __exit__(self, *a): return False

    monkeypatch.setattr(worker_mod, "session_scope", lambda: _Scope())
    monkeypatch.setattr(worker_mod, "release_all_claims", lambda db, wid: 0)
    worker_mod.graceful_shutdown("w-1")
    assert order == ["stop_claiming", "stop_scheduler", "drain_and_resolve", "release_contact_claims"]


def test_a_failing_shutdown_step_does_not_skip_the_next(monkeypatch):
    order = []
    monkeypatch.setattr(worker_mod, "begin_external_dispatch_shutdown", lambda: (_ for _ in ()).throw(RuntimeError("a")))
    monkeypatch.setattr(worker_mod, "stop_scheduler", lambda: (_ for _ in ()).throw(RuntimeError("b")))
    monkeypatch.setattr(worker_mod, "drain_external_dispatches_on_shutdown", lambda: (_ for _ in ()).throw(RuntimeError("c")))
    monkeypatch.setattr(worker_mod, "session_scope", lambda: (_ for _ in ()).throw(RuntimeError("d")))
    worker_mod.graceful_shutdown("w-1")           # must not raise


def test_sigterm_handler_raises_systemexit_once_and_ignores_repeats(monkeypatch):
    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(worker_mod, "_terminating", False)
    try:
        worker_mod.install_signal_handlers()
        with pytest.raises(SystemExit):
            signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)       # second one: ignored, no exception
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_real_worker_process_runs_graceful_shutdown_on_sigterm(tmp_path):
    env = {**os.environ, "PYTHONPATH": ROOT, "DATABASE_URL": f"sqlite:///{tmp_path/'p.db'}",
           "LIVE_SENDING_ENABLED": "false"}
    proc = subprocess.Popen([sys.executable, "worker.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    started = threading.Event()

    def pump():
        for line in proc.stdout:
            lines.append(line)
            if "Unified scheduler started" in line:
                started.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        assert started.wait(60), "worker did not start:\n" + "".join(lines)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(30) == 0
        reader.join(5)
    finally:
        if proc.poll() is None:
            proc.kill()
    text = "".join(lines)
    assert "Shutting down background worker" in text, text
    assert "external_dispatch: shutdown requested" in text, text
    assert text.index("shutdown requested") < text.index("Scheduler stopped"), text



# PR4 shutdown liveness regression tests
def test_shutdown_marks_stopped_only_after_successful_drain(monkeypatch):
    order = []
    monkeypatch.setattr(
        worker_mod, "begin_external_dispatch_shutdown",
        lambda: order.append("stop_claiming"),
    )
    monkeypatch.setattr(
        worker_mod, "stop_scheduler",
        lambda: order.append("stop_scheduler"),
    )
    monkeypatch.setattr(
        worker_mod, "drain_external_dispatches_on_shutdown",
        lambda: order.append("drain") or w.ShutdownReport(True, (), ()),
    )

    class _Scope:
        def __enter__(self):
            order.append("release_claims")
            return object()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(worker_mod, "session_scope", lambda: _Scope())
    monkeypatch.setattr(worker_mod, "release_all_claims", lambda db, wid: 0)
    monkeypatch.setattr(
        worker_mod, "mark_runtime_stopped",
        lambda: order.append("mark_stopped"),
    )

    worker_mod.graceful_shutdown("w-1")

    assert order == [
        "stop_claiming", "stop_scheduler", "drain",
        "release_claims", "mark_stopped",
    ]


def test_shutdown_timeout_does_not_mark_worker_stopped(monkeypatch):
    stopped = []
    monkeypatch.setattr(worker_mod, "begin_external_dispatch_shutdown", lambda: None)
    monkeypatch.setattr(worker_mod, "stop_scheduler", lambda: None)
    monkeypatch.setattr(
        worker_mod, "drain_external_dispatches_on_shutdown",
        lambda: w.ShutdownReport(False, (123,), ()),
    )

    class _Scope:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(worker_mod, "session_scope", lambda: _Scope())
    monkeypatch.setattr(worker_mod, "release_all_claims", lambda db, wid: 0)
    monkeypatch.setattr(
        worker_mod, "mark_runtime_stopped", lambda: stopped.append(True)
    )

    worker_mod.graceful_shutdown("w-1")

    assert stopped == []


def test_shutdown_drain_exception_does_not_mark_worker_stopped(monkeypatch):
    stopped = []
    monkeypatch.setattr(worker_mod, "begin_external_dispatch_shutdown", lambda: None)
    monkeypatch.setattr(worker_mod, "stop_scheduler", lambda: None)
    monkeypatch.setattr(
        worker_mod, "drain_external_dispatches_on_shutdown",
        lambda: (_ for _ in ()).throw(RuntimeError("simulated drain failure")),
    )

    class _Scope:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(worker_mod, "session_scope", lambda: _Scope())
    monkeypatch.setattr(worker_mod, "release_all_claims", lambda db, wid: 0)
    monkeypatch.setattr(
        worker_mod, "mark_runtime_stopped", lambda: stopped.append(True)
    )

    worker_mod.graceful_shutdown("w-1")

    assert stopped == []


def test_dispatch_runtime_resumes_only_when_idle():
    runtime = w.DispatchRuntime()
    w.begin_external_dispatch_shutdown(runtime)

    assert runtime.stopping is True
    assert runtime.resume_if_idle() is True
    assert runtime.stopping is False
    assert runtime.inflight() == []


def test_dispatch_runtime_does_not_discard_inflight_work_on_resume():
    runtime = w.DispatchRuntime()
    claim = ClaimedDispatch(999001, "org-test", "worker-test", age(5))
    runtime.register(claim)
    w.begin_external_dispatch_shutdown(runtime)

    assert runtime.resume_if_idle() is False
    assert runtime.stopping is True
    assert runtime.inflight() == [claim]


def test_start_scheduler_resumes_idle_runtime(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    runtime = w.DispatchRuntime()
    w.begin_external_dispatch_shutdown(runtime)

    # Isolate the singleton so this test cannot affect other tests.
    monkeypatch.setattr(w, "default_runtime", runtime)
    monkeypatch.setattr(BackgroundScheduler, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(sched, "_scheduler", None)
    monkeypatch.setattr(sched, "_runtime_recorder", None)

    started = sched.start_scheduler()

    assert started is sched._scheduler
    assert runtime.stopping is False

    # BackgroundScheduler.start was stubbed, so no scheduler thread was created.
    sched._scheduler = None
    sched._runtime_recorder = None


def test_start_scheduler_refuses_runtime_with_inflight_work(monkeypatch):
    runtime = w.DispatchRuntime()
    claim = ClaimedDispatch(999002, "org-test", "worker-test", age(5))
    runtime.register(claim)
    w.begin_external_dispatch_shutdown(runtime)

    monkeypatch.setattr(w, "default_runtime", runtime)
    monkeypatch.setattr(sched, "_scheduler", None)
    monkeypatch.setattr(sched, "_runtime_recorder", None)

    with pytest.raises(RuntimeError, match="remain in flight"):
        sched.start_scheduler()

    assert runtime.stopping is True
    assert runtime.inflight() == [claim]

def test_shutdown_drain_waits_for_claim_attempt_before_registration(
    factory, monkeypatch
):
    import threading

    claim_entered = threading.Event()
    release_claim = threading.Event()
    thread_errors = []
    runtime = w.DispatchRuntime()

    def paused_claim(db, worker_id):
        claim_entered.set()
        if not release_claim.wait(timeout=5):
            raise AssertionError("Timed out waiting to release simulated claim")
        return None

    monkeypatch.setattr(w, "claim_next_external_dispatch", paused_claim)

    def run_claim():
        try:
            w.process_next_external_dispatch(
                session_factory=factory,
                worker_id="w-claim-attempt",
                runtime=runtime,
            )
        except BaseException as exc:
            thread_errors.append(exc)

    thread = threading.Thread(target=run_claim, daemon=True)
    thread.start()

    try:
        assert claim_entered.wait(timeout=5)
        w.begin_external_dispatch_shutdown(runtime)

        report = w.drain_external_dispatches_on_shutdown(
            session_factory=factory,
            runtime=runtime,
            drain_seconds=0,
        )

        assert report.drained is False
        assert runtime.resume_if_idle() is False
    finally:
        release_claim.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert thread_errors == []
    assert runtime.resume_if_idle() is True
