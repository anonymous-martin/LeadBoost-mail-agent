"""
PR3 -- /system/*: organization-scoped monitoring vs process-wide operator diagnostics.

* TENANT routes (``/system/health``, ``/system/metrics/classification``, ``/system/metrics/states``)
  keep tenant-key authentication and per-organization scoping, and no longer return any process-wide
  data (``/system/health`` used to add ``llm_metrics_process_wide`` and the API's ``settings`` flags).
* OPERATOR routes (``/system/config``, ``/system/metrics/llm``, ``/system/integration/validate``) need
  the separate ``X-System-Operator-Key`` credential (SYSTEM_OPERATOR_API_KEY). A tenant key -- or the
  legacy API_KEY -- never opens them, they need no tenant key, and they fail closed.

Like tests/test_c15_1_auth_fail_closed.py, every request goes through the REAL authentication
dependencies of the REAL ``app``; only ``get_db`` is replaced (an isolated SQLite file). Nothing contacts
SMTP/IMAP, an LLM provider or a real database; the one handler that would touch the engine
(``validate_integration``) is stubbed in the tests that reach it.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mailer_agent.api import deps, system
from mailer_agent.api.main import app
from mailer_agent.config import get_settings
from mailer_agent.db import Base, get_db
from mailer_agent.models import (
    Campaign,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
)

OP_KEY = "operator-" + "o" * 28
KEY_A, KEY_B, LEGACY = "key-a-" + "a" * 20, "key-b-" + "b" * 20, "legacy-" + "l" * 20
ORG_A, ORG_B = "org-a", "org-b"
OP_HEADER = "X-System-Operator-Key"

TENANT_PATHS = {"/system/health", "/system/metrics/classification", "/system/metrics/states"}
OPERATOR_PATHS = ["/system/config", "/system/metrics/llm", "/system/integration/validate"]
SECRETS = (OP_KEY, KEY_A, KEY_B, LEGACY)


def _no_secret_in(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, "a credential value leaked into a response or log"


@pytest.fixture(autouse=True)
def _real_auth_only(caplog):
    """No auth dependency (tenant or operator) may be overridden; all logs are captured for the secrecy
    checks and every test ends by asserting that no credential reached a log line."""
    guarded = {
        deps.require_api_key, deps.get_current_org_id, deps.get_integration_org_id,
        deps.get_authenticated_org_id, deps.require_system_operator,
    }
    overridden = [d for d in app.dependency_overrides if d in guarded]
    assert not overridden, f"auth dependencies overridden: {overridden}"
    caplog.set_level(logging.DEBUG)
    yield
    app.dependency_overrides.pop(get_db, None)
    # NOTE: pytest swaps caplog's handler per phase, so caplog.text at teardown would only hold
    # teardown-phase records. Read the CALL-phase records (the test body's requests) explicitly.
    _no_secret_in("\n".join(r.getMessage() for r in caplog.get_records("call")))


@pytest.fixture()
def sm(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'pr3.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    sm_ = sessionmaker(bind=eng)

    def _get_db():
        s = sm_()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    yield sm_
    eng.dispose()


@pytest.fixture()
def keys(monkeypatch):
    """Two tenants configured the normal way; insecure opt-out explicitly off."""
    monkeypatch.setattr(deps, "_KEY_MAP", {KEY_A: ORG_A, KEY_B: ORG_B})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", False)


def _set_operator_key(monkeypatch, value: str) -> None:
    monkeypatch.setattr(get_settings(), "system_operator_api_key", SecretStr(value))


@pytest.fixture()
def operator(monkeypatch):
    _set_operator_key(monkeypatch, OP_KEY)


@pytest.fixture()
def no_operator(monkeypatch):
    _set_operator_key(monkeypatch, "")


@pytest.fixture()
def stub_validate(monkeypatch):
    """validate_integration() opens a connection on the module-level engine; the auth tests do not need it."""
    monkeypatch.setattr(system.mailer_agent, "validate_integration", lambda: {"integration_valid": True})


def _seed(sm):
    """Org A: 2 contacts, 3 outbound (2 sent, 1 failed) + 1 classified inbound. Org B: 3 contacts, 5 outbound sent."""
    with sm() as s:
        for org, n_contacts in ((ORG_A, 2), (ORG_B, 3)):
            camp = Campaign(name=f"c-{org}", organization_id=org, sender_name="n", sender_org="o",
                            sender_email="s@example.org", value_prop="vp")
            s.add(camp)
            s.flush()
            contacts = [Contact(campaign_id=camp.id, email=f"{org}-{i}@example.org") for i in range(n_contacts)]
            s.add_all(contacts)
            s.flush()
            c0 = contacts[0]
            outbound = [MessageStatus.SENT] * 2 + [MessageStatus.FAILED] if org == ORG_A else [MessageStatus.SENT] * 5
            for st in outbound:
                s.add(Message(contact_id=c0.id, direction=MessageDirection.OUTBOUND.value, body="hi", status=st.value))
            if org == ORG_A:
                s.add(Message(contact_id=c0.id, direction=MessageDirection.INBOUND.value, body="re",
                              status=MessageStatus.RECEIVED.value, classification_success=True))
        s.commit()


# --------------------------------------------------------------------------- inventory
def _system_routes():
    return [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/system")]


def _calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _calls(d)


def test_every_system_route_is_classified_on_purpose_and_carries_exactly_one_kind_of_auth():
    """A new /system route must be added to TENANT_PATHS or OPERATOR_PATHS here, deliberately."""
    routes = _system_routes()
    assert {r.path for r in routes} == TENANT_PATHS | set(OPERATOR_PATHS)
    tenant_deps = {deps.require_api_key, deps.get_current_org_id}
    for r in routes:
        calls = set(_calls(r.dependant))
        if r.path in TENANT_PATHS:
            assert deps.require_api_key in calls and deps.require_system_operator not in calls, r.path
        else:  # operator route: operator credential ONLY (no tenant key requirement, no tenant key acceptance)
            assert deps.require_system_operator in calls, r.path
            assert not (calls & (tenant_deps | {deps.get_integration_org_id, deps.get_authenticated_org_id})), r.path


# --------------------------------------------------------------------------- tenant /system/health
def test_tenant_health_is_organization_scoped_and_has_no_process_wide_fields(sm, keys):
    _seed(sm)
    c = TestClient(app)
    a = c.get("/system/health", headers={"X-API-Key": KEY_A})
    b = c.get("/system/health", headers={"X-API-Key": KEY_B})
    assert a.status_code == b.status_code == 200
    assert set(a.json()) == {"organization_id", "contact_states", "messages", "classification"}
    # org A sees only org A's rows, org B only org B's
    assert a.json()["organization_id"] == ORG_A and b.json()["organization_id"] == ORG_B
    assert a.json()["contact_states"]["new"] == 2 and b.json()["contact_states"]["new"] == 3
    assert {k: a.json()["messages"][k] for k in ("total", "sent", "failed", "unknown")} == {
        "total": 4, "sent": 2, "failed": 1, "unknown": 0}
    assert {k: b.json()["messages"][k] for k in ("total", "sent", "failed", "unknown")} == {
        "total": 5, "sent": 5, "failed": 0, "unknown": 0}
    assert (a.json()["classification"]["total"], a.json()["classification"]["success"]) == (1, 1)
    assert (b.json()["classification"]["total"], b.json()["classification"]["success"]) == (0, 0)
    for r in (a, b):  # nothing process-wide, under any name
        assert "llm_metrics_process_wide" not in r.text and "live_sending_enabled" not in r.text
        assert "auto_reply_enabled" not in r.text and '"settings"' not in r.text


def test_tenant_health_does_not_publish_process_wide_data_even_when_the_flags_are_on(sm, keys, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "live_sending_enabled", True)
    monkeypatch.setattr(s, "auto_reply_enabled", True)
    body = TestClient(app).get("/system/health", headers={"X-API-Key": KEY_A}).json()
    assert set(body) == {"organization_id", "contact_states", "messages", "classification"}


@pytest.mark.parametrize("path", sorted(TENANT_PATHS))
def test_tenant_routes_still_require_a_tenant_key_and_reject_the_operator_key(path, sm, keys, operator):
    c = TestClient(app)
    assert c.get(path).status_code == 401
    assert c.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get(path, headers={OP_HEADER: OP_KEY}).status_code == 401  # operator credential is not a tenant key
    assert c.get(path, headers={"X-API-Key": OP_KEY}).status_code == 401  # ...in either header
    assert c.get(path, headers={"X-API-Key": KEY_A}).status_code == 200


@pytest.mark.parametrize("path", sorted(TENANT_PATHS))
def test_tenant_routes_fail_closed_with_no_keys_even_if_an_operator_key_is_configured(path, sm, operator, monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", False)
    assert TestClient(app).get(path, headers={OP_HEADER: OP_KEY}).status_code == 503


# --------------------------------------------------------------------------- operator routes
@pytest.mark.parametrize("path", OPERATOR_PATHS)
def test_a_tenant_key_alone_cannot_reach_an_operator_route(path, sm, keys, operator):
    c = TestClient(app)
    for tenant_key in (KEY_A, KEY_B):
        assert c.get(path, headers={"X-API-Key": tenant_key}).status_code == 401
    assert c.get(path, headers={OP_HEADER: KEY_A}).status_code == 401  # a tenant key in the operator header
    assert c.get(path, headers={"X-API-Key": KEY_A, OP_HEADER: KEY_A}).status_code == 401


@pytest.mark.parametrize("path", OPERATOR_PATHS)
def test_legacy_api_key_is_not_an_operator_credential(path, sm, monkeypatch, operator):
    monkeypatch.setattr(deps, "_KEY_MAP", {LEGACY: "default", KEY_A: ORG_A})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    c = TestClient(app)
    assert c.get(path, headers={"X-API-Key": LEGACY}).status_code == 401
    assert c.get(path, headers={OP_HEADER: LEGACY}).status_code == 401


@pytest.mark.parametrize("path", OPERATOR_PATHS)
def test_missing_empty_and_wrong_operator_keys_are_rejected(path, sm, keys, operator):
    c = TestClient(app)
    for headers in ({}, {OP_HEADER: ""}, {OP_HEADER: "wrong"}, {OP_HEADER: OP_KEY + "x"}, {OP_HEADER: OP_KEY[:-1]}):
        r = c.get(path, headers=headers)
        assert r.status_code == 401, (path, headers, r.status_code)
        assert r.json()["detail"] == "Invalid or missing operator key"  # missing vs wrong not distinguishable
    # the operator key is only accepted in its own header
    assert c.get(path, headers={"X-API-Key": OP_KEY}).status_code == 401


@pytest.mark.parametrize("path", OPERATOR_PATHS)
def test_correct_operator_key_needs_no_tenant_key(path, sm, keys, operator, stub_validate):
    c = TestClient(app)
    r = c.get(path, headers={OP_HEADER: OP_KEY})  # no X-API-Key at all
    assert r.status_code == 200, r.text
    _no_secret_in(r.text)
    # an unrelated/invalid tenant header neither helps nor hurts: the two credentials are independent
    assert c.get(path, headers={OP_HEADER: OP_KEY, "X-API-Key": "garbage"}).status_code == 200


def test_operator_routes_return_their_existing_payloads(sm, keys, operator, stub_validate):
    c = TestClient(app)
    cfg = c.get("/system/config", headers={OP_HEADER: OP_KEY}).json()
    assert {"live_sending_enabled", "auto_reply_enabled", "run_scheduler_in_process", "database_type"} <= set(cfg)
    assert isinstance(c.get("/system/metrics/llm", headers={OP_HEADER: OP_KEY}).json(), dict)
    assert c.get("/system/integration/validate", headers={OP_HEADER: OP_KEY}).json() == {"integration_valid": True}


@pytest.mark.parametrize("path", OPERATOR_PATHS)
@pytest.mark.parametrize("configured", ["", "   "])
def test_operator_routes_fail_closed_when_no_operator_key_is_configured(path, configured, sm, keys, monkeypatch):
    _set_operator_key(monkeypatch, configured)
    c = TestClient(app)
    for headers in ({}, {OP_HEADER: ""}, {OP_HEADER: configured}, {OP_HEADER: OP_KEY}, {"X-API-Key": KEY_A},
                    {OP_HEADER: KEY_A, "X-API-Key": KEY_A}):
        r = c.get(path, headers=headers)
        assert r.status_code == 503, (path, repr(configured), headers, r.status_code)  # an empty key never matches
        assert r.json()["detail"] == "Operator authentication is not configured on this deployment"


@pytest.mark.parametrize("path", OPERATOR_PATHS)
def test_insecure_dev_opt_out_never_opens_operator_routes(path, sm, no_operator, monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", True)
    monkeypatch.setattr(get_settings(), "live_sending_enabled", False)
    c = TestClient(app)
    assert c.get("/system/metrics/states").status_code == 200  # the dev opt-out still opens tenant routes (unchanged)
    assert c.get(path).status_code == 503  # ...and not these
    _set_operator_key(monkeypatch, OP_KEY)
    assert c.get(path).status_code == 401  # a configured key is still required in dev mode


@pytest.mark.parametrize("path", OPERATOR_PATHS)
def test_invalid_tenant_auth_configuration_also_fails_operator_routes_closed(path, sm, operator, monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", "ORG_KEY_MAP is set but is not valid JSON")
    assert TestClient(app).get(path, headers={OP_HEADER: OP_KEY}).status_code == 503


@pytest.mark.parametrize("path", OPERATOR_PATHS)
def test_an_operator_key_equal_to_a_tenant_key_is_not_a_distinct_credential(path, sm, monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {LEGACY: "default", KEY_A: ORG_A})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    for colliding in (KEY_A, LEGACY):
        _set_operator_key(monkeypatch, colliding)
        c = TestClient(app)
        # even the holder of that key, presenting it as the operator key, is refused (503: misconfigured)
        assert c.get(path, headers={OP_HEADER: colliding}).status_code == 503


def test_non_ascii_operator_header_is_a_401_not_a_server_error(sm, keys, operator):
    r = TestClient(app).get("/system/config", headers={OP_HEADER: "caf\xe9".encode("latin-1")})
    assert r.status_code == 401


# --------------------------------------------------------------------------- startup validation
def test_startup_refuses_an_operator_key_that_equals_a_tenant_key(monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {LEGACY: "default", KEY_A: ORG_A})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    for colliding in (KEY_A, LEGACY):
        _set_operator_key(monkeypatch, colliding)
        with pytest.raises(deps.AuthConfigurationError, match="SYSTEM_OPERATOR_API_KEY") as exc:
            deps.validate_auth_configuration()
        _no_secret_in(str(exc.value))


def test_startup_accepts_unset_or_distinct_operator_keys_including_non_ascii(keys, monkeypatch):
    for value in ("", OP_KEY, "op-caf\xe9-" + "x" * 20):
        _set_operator_key(monkeypatch, value)
        deps.validate_auth_configuration()  # must not raise (and must not TypeError on non-ASCII)


def test_the_operator_key_setting_defaults_to_empty_and_is_never_shown_in_repr(monkeypatch):
    monkeypatch.delenv("SYSTEM_OPERATOR_API_KEY", raising=False)
    from mailer_agent.config import Settings

    assert Settings(_env_file=None).system_operator_api_key.get_secret_value() == ""
    shown = repr(Settings(_env_file=None, system_operator_api_key=OP_KEY))
    assert OP_KEY not in shown
