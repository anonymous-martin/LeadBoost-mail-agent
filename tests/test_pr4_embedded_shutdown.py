"""PR4: embedded API scheduler shutdown must report liveness conservatively."""

import asyncio

from mailer_agent.api import main as main_mod
from mailer_agent.mail.external_dispatch_worker import ShutdownReport


def _configure(monkeypatch, events, drain_result):
    monkeypatch.setattr(main_mod.settings, "run_scheduler_in_process", True)
    monkeypatch.setattr(main_mod.settings, "live_sending_enabled", True)
    monkeypatch.setattr(
        main_mod, "validate_auth_configuration", lambda: events.append("validate")
    )
    monkeypatch.setattr(
        main_mod, "prepare_schema", lambda: events.append("prepare_schema")
    )
    monkeypatch.setattr(
        main_mod, "start_scheduler", lambda role: events.append("start_scheduler")
    )
    monkeypatch.setattr(
        main_mod, "begin_external_dispatch_shutdown",
        lambda: events.append("stop_claiming"),
    )
    monkeypatch.setattr(
        main_mod, "stop_scheduler", lambda: events.append("stop_scheduler")
    )

    def drain():
        events.append("drain")
        if isinstance(drain_result, BaseException):
            raise drain_result
        return drain_result

    monkeypatch.setattr(main_mod, "drain_external_dispatches_on_shutdown", drain)
    monkeypatch.setattr(
        main_mod, "mark_runtime_stopped", lambda: events.append("mark_stopped")
    )


def _run_lifespan():
    async def run():
        async with main_mod.lifespan(None):
            pass

    asyncio.run(run())


def test_embedded_api_marks_stopped_after_successful_drain(monkeypatch):
    events = []
    _configure(monkeypatch, events, ShutdownReport(True, (), ()))

    _run_lifespan()

    assert events == [
        "validate", "prepare_schema", "start_scheduler",
        "stop_claiming", "stop_scheduler", "drain", "mark_stopped",
    ]


def test_embedded_api_does_not_mark_stopped_after_drain_timeout(monkeypatch):
    events = []
    _configure(monkeypatch, events, ShutdownReport(False, (123,), ()))

    _run_lifespan()

    assert events == [
        "validate", "prepare_schema", "start_scheduler",
        "stop_claiming", "stop_scheduler", "drain",
    ]
    assert "mark_stopped" not in events


def test_embedded_api_does_not_mark_stopped_if_drain_raises(monkeypatch):
    events = []
    _configure(monkeypatch, events, RuntimeError("simulated drain failure"))

    _run_lifespan()

    assert events == [
        "validate", "prepare_schema", "start_scheduler",
        "stop_claiming", "stop_scheduler", "drain",
    ]
    assert "mark_stopped" not in events
