"""
Pytest configuration and shared fixtures.
"""

import pytest

from tests.fake_llm_provider import FakeLLMProvider, install_fake_llm_provider
from tests.pr4_support import operator_env  # noqa: F401  (PR4 fixture, shared by tests/test_pr4_*.py)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch, request):
    """
    Fixture that provides a fake LLM provider for deterministic testing.
    
    Automatically applied to ALL tests unless explicitly disabled with 
    @pytest.mark.integration marker.
    
    This prevents tests from calling the live Groq API, avoiding:
    - Rate limits
    - API costs
    - Flaky network-dependent tests
    - Non-deterministic LLM outputs

    The fake never infers a response from prompt content -- it only
    returns whatever was explicitly queued via fake_llm.queue_response(...).
    A test that calls code which reaches the LLM provider without queuing
    a response first will get a clear AssertionError, not a guess.

    Usage:
        def test_something(fake_llm):
            fake_llm.queue_response({...})
            result = classify_prospect_reply(...)
            ...

    Usage (explicitly disable for integration test):
        @pytest.mark.integration
        def test_with_real_llm():
            # This test uses actual Groq API
    """
    # Skip patching for integration tests
    if "integration" in request.keywords:
        yield None
        return

    yield install_fake_llm_provider(monkeypatch)


@pytest.fixture(autouse=True)
def force_test_dry_run(monkeypatch):
    from mailer_agent.mail import sender

    # Tests must never inherit LIVE_SENDING_ENABLED=true from .env.
    # Tests that specifically exercise SMTP use their own localhost
    # SMTP fixture and explicitly re-enable sending there.
    monkeypatch.setattr(sender.settings, "live_sending_enabled", False)


@pytest.fixture(autouse=True, scope="session")
def mailbox_encryption_key():
    """M2-A: dispatch execution decrypts mailbox credentials, so the whole
    test session gets a valid throwaway Fernet key (never a real one).
    Session-scoped on purpose: several worker tests call monkeypatch.undo()
    mid-test, which must not remove it. Tests that exercise a missing or
    invalid key override it themselves with their own monkeypatch."""
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from mailer_agent.config import get_settings

    mp = pytest.MonkeyPatch()
    mp.setattr(get_settings(), "mailbox_encryption_key", SecretStr(Fernet.generate_key().decode()))
    yield
    mp.undo()


@pytest.fixture
def use_real_llm(monkeypatch):
    """
    Fixture to explicitly enable real LLM for integration tests.
    
    Usage:
        @pytest.mark.integration
        def test_with_real_llm(use_real_llm):
            # This test will use actual Groq API
    """
    # Don't patch anything - use real implementation
    yield


@pytest.fixture(autouse=True)
def _isolate_worker_runtime_recorder():
    """PR4: ``scheduler._runtime_recorder`` is process-global state created by ``start_scheduler()``.
    Some tests start a (stubbed) scheduler without ever stopping it; without this reset the recorder
    would leak into later tests that call job functions directly and make them write liveness rows
    to whatever the default database is. Resets only if the scheduler module was imported at all."""
    yield
    import sys

    scheduler = sys.modules.get("mailer_agent.followup.scheduler")
    if scheduler is not None:
        scheduler._runtime_recorder = None
