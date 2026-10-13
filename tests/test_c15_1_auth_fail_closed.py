"""
C15.1 -- authentication must FAIL CLOSED.

Every test here goes through the REAL authentication dependencies of the REAL ``app``. Nothing in
this module overrides ``require_api_key`` / ``get_current_org_id`` / ``get_integration_org_id`` /
``get_authenticated_org_id`` (an autouse guard fails the test if any override is present), and the
only dependency replaced is ``get_db`` (an isolated SQLite file). Nothing contacts an SMTP/IMAP
server, an LLM provider or a real database.

The base-SHA defect these pin down: with no effective keys, ``require_api_key`` accepted every
request and ``get_current_org_id`` resolved to org "default", so every campaign / contact /
message / webhook / system / legacy-integration route was open, and an unparseable ORG_KEY_MAP was
only logged and ignored. The previous suite even asserted the open behaviour
(test_integration_route_fails_closed_when_no_keys_are_configured used to expect a 404 from the
unauthenticated legacy route).
"""

from __future__ import annotations

import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mailer_agent.api import deps
from mailer_agent.api import main as api_main
from mailer_agent.api.main import app
from mailer_agent.config import Settings, get_settings
from mailer_agent.db import Base, get_db
from mailer_agent.models import Campaign

AUTH_DEPS = {
    deps.require_api_key,
    deps.get_current_org_id,
    deps.get_integration_org_id,
    deps.get_authenticated_org_id,
}
# The ONLY routes allowed to be reachable without a key. Anything new must be added here on purpose.
PUBLIC_PATHS = {"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
# PR3: process-wide routes that are deliberately NOT tenant-key routes (a tenant key must not open them).
# They use require_system_operator instead; tests/test_pr3_system_operator_boundary.py covers their access.
OPERATOR_PATHS = {"/system/config", "/system/metrics/llm", "/system/integration/validate"}

KEY_A, KEY_B, LEGACY = "key-a-" + "a" * 20, "key-b-" + "b" * 20, "legacy-" + "l" * 20
ORG_A, ORG_B = "org-a", "org-b"


@pytest.fixture(autouse=True)
def _real_auth_only():
    """Guard (spec #14): no auth dependency may be overridden while these tests run."""
    overridden = [d for d in app.dependency_overrides if d in AUTH_DEPS]
    assert not overridden, f"auth dependencies overridden: {overridden}"
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def sm(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'auth.db'}", connect_args={"check_same_thread": False})
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


@pytest.fixture()
def no_keys(monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", False)


def _routes():
    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                out.append((m, r))
    return out


def _concrete(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "1", path)


def _all_calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _all_calls(d)


PROTECTED = [(m, r) for m, r in _routes() if r.path not in PUBLIC_PATHS and r.path not in OPERATOR_PATHS]
IDS = [f"{m} {r.path}" for m, r in PROTECTED]


# --------------------------------------------------------------------------- route inventory
def test_every_route_carries_a_real_auth_dependency_unless_explicitly_public():
    """A new route that forgets authentication fails here, instead of shipping open."""
    missing = [
        f"{m} {r.path}"
        for m, r in _routes()
        if r.path not in PUBLIC_PATHS and r.path not in OPERATOR_PATHS and not (set(_all_calls(r.dependant)) & AUTH_DEPS)
    ]
    assert not missing, f"routes without an authentication dependency: {missing}"
    # PR3: the operator routes must carry the operator dependency and NO tenant dependency.
    operator = [(m, r) for m, r in _routes() if r.path in OPERATOR_PATHS]
    assert {r.path for _, r in operator} == OPERATOR_PATHS
    for m, r in operator:
        calls = set(_all_calls(r.dependant))
        assert deps.require_system_operator in calls and not (calls & AUTH_DEPS), (m, r.path)
    assert {r.path for _, r in _routes()} & PUBLIC_PATHS == {"/health"}  # only /health is an APIRoute


# --------------------------------------------------------------------------- spec 1/2/3/12
@pytest.mark.parametrize("method,route", PROTECTED, ids=IDS)
def test_empty_key_map_is_not_open_on_any_protected_route(method, route, sm, no_keys):
    c = TestClient(app)
    for headers in ({}, {"X-API-Key": "anything"}):
        r = c.request(method, _concrete(route.path), headers=headers, json={})
        assert r.status_code == 503, (method, route.path, r.status_code)
        assert "not configured" in r.json()["detail"]


@pytest.mark.parametrize("method,route", PROTECTED, ids=IDS)
def test_missing_and_wrong_key_rejected_on_every_protected_route(method, route, sm, keys):
    c = TestClient(app)
    assert c.request(method, _concrete(route.path), json={}).status_code == 401
    assert c.request(method, _concrete(route.path), headers={"X-API-Key": "wrong"}, json={}).status_code == 401
    assert c.request(method, _concrete(route.path), headers={"X-API-Key": ""}, json={}).status_code == 401


@pytest.mark.parametrize("method,route", PROTECTED, ids=IDS)
def test_valid_key_passes_authentication_on_every_protected_route(method, route, sm, keys):
    """Authentication succeeds: whatever comes back is NOT the auth rejection (a 404/422/200 from the
    route itself is fine -- these requests carry no meaningful body)."""
    r = TestClient(app).request(method, _concrete(route.path), headers={"X-API-Key": KEY_A}, json={})
    assert r.status_code not in (401, 503), (method, route.path, r.status_code, r.text[:200])


# --------------------------------------------------------------------------- spec 4: tenant mapping
def test_valid_key_resolves_to_its_own_organization(sm, keys):
    assert deps.get_current_org_id(KEY_A) == ORG_A
    assert deps.get_current_org_id(KEY_B) == ORG_B
    with sm() as s:
        for org in (ORG_A, ORG_B):
            s.add(Campaign(name=f"c-{org}", organization_id=org, sender_name="n", sender_org="o",
                           sender_email="s@example.org", value_prop="vp"))
        s.commit()
    c = TestClient(app)
    a = c.get("/campaigns", headers={"X-API-Key": KEY_A}).json()
    b = c.get("/campaigns", headers={"X-API-Key": KEY_B}).json()
    assert [x["name"] for x in a] == [f"c-{ORG_A}"] and [x["name"] for x in b] == [f"c-{ORG_B}"]


# --------------------------------------------------------------------------- spec 5/6: config parsing
@pytest.mark.parametrize(
    "raw",
    [
        "{not json", "[]", '["a","b"]', '"a string"', "123", "null", "true",
        '{"": "org"}', '{"k": ""}', '{"k": "  "}', '{"k": null}', '{"k": true}', '{"k": ["x"]}',
        '{"k": {"nested": 1}}', '{"k": 1.5}',
    ],  # an EMPTY value is "unset", not an error -- covered separately below
)
def test_malformed_org_key_map_is_rejected_and_never_falls_back(raw):
    cfg = deps.load_auth_config({"ORG_KEY_MAP": raw}, legacy_api_key=LEGACY)
    assert cfg.error, raw
    assert cfg.key_map == {}  # not even the legacy key survives: no permissive/half-configured state


def test_empty_org_key_map_is_treated_as_unset_not_as_an_error():
    for raw in ("", "   "):
        cfg = deps.load_auth_config({"ORG_KEY_MAP": raw}, legacy_api_key="")
        assert cfg.error is None and cfg.key_map == {}


def test_explicit_empty_object_is_valid_but_yields_no_keys():
    cfg = deps.load_auth_config({"ORG_KEY_MAP": "{}"}, legacy_api_key="")
    assert cfg.error is None and cfg.key_map == {}


def test_config_error_text_never_contains_secret_material():
    secret = "SUPER-SECRET-KEY-VALUE-123"
    for raw in (f'{{"{secret}": ', f'["{secret}"]', f'{{"{secret}": ""}}'):
        err = deps.load_auth_config({"ORG_KEY_MAP": raw}, legacy_api_key="").error
        assert err and secret not in err


def test_valid_org_key_map_accepts_string_and_integer_org_ids():
    cfg = deps.load_auth_config({"ORG_KEY_MAP": '{"k1": "acme", "k2": 7}'}, legacy_api_key="")
    assert cfg.error is None and cfg.key_map == {"k1": "acme", "k2": "7"}


def test_invalid_config_makes_every_route_503_even_with_a_legacy_key_presented(sm, monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", "ORG_KEY_MAP is set but is not valid JSON")
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", True)  # not even the dev opt-out rescues it
    c = TestClient(app)
    for m, r in PROTECTED:
        resp = c.request(m, _concrete(r.path), headers={"X-API-Key": LEGACY}, json={})
        assert resp.status_code == 503, (m, r.path)


# --------------------------------------------------------------------------- spec 7: legacy API_KEY contract
def test_legacy_key_alone_maps_to_org_default():
    cfg = deps.load_auth_config({}, legacy_api_key=LEGACY)
    assert cfg.key_map == {LEGACY: "default"} and cfg.legacy_key_active and not cfg.explicit_keys_active


def test_legacy_key_is_merged_alongside_org_key_map_and_never_overrides_it():
    env = {"ORG_KEY_MAP": f'{{"{KEY_A}": "{ORG_A}"}}', "ORG_KEYS_acme": KEY_B}
    cfg = deps.load_auth_config(env, legacy_api_key=LEGACY)
    assert cfg.key_map == {LEGACY: "default", KEY_B: "acme", KEY_A: ORG_A}
    assert cfg.legacy_key_active and cfg.explicit_keys_active
    # the same key string configured both ways: the explicit tenant mapping wins, as before
    clash = deps.load_auth_config({"ORG_KEY_MAP": f'{{"{LEGACY}": "{ORG_A}"}}'}, legacy_api_key=LEGACY)
    assert clash.key_map == {LEGACY: ORG_A}


def test_legacy_key_is_valid_on_integration_and_mailbox_routes_as_org_default(sm, monkeypatch):
    """Documented, unchanged compatibility contract (C15.1 does not break existing single-key
    deployments): the legacy key authenticates everywhere as org 'default'."""
    monkeypatch.setattr(deps, "_KEY_MAP", {LEGACY: "default", KEY_A: ORG_A})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    assert deps.get_integration_org_id(LEGACY) == "default"
    assert deps.get_authenticated_org_id(LEGACY) == "default"
    assert deps.get_current_org_id(KEY_A) == ORG_A


# --------------------------------------------------------------------------- spec 8/9/10/11 (named routes)
def test_legacy_exact_message_routes_have_no_authentication_bypass(sm, no_keys):
    c = TestClient(app)
    base = "/integrations/leadboost/outreach-actions"
    assert c.post(base, json={}).status_code == 503
    assert c.get(f"{base}/some-key").status_code == 503


def test_legacy_exact_message_routes_reject_missing_and_wrong_keys(sm, keys):
    c = TestClient(app)
    base = "/integrations/leadboost/outreach-actions"
    assert c.post(base, json={}).status_code == 401
    assert c.post(base, json={}, headers={"X-API-Key": "bad"}).status_code == 401
    assert c.get(f"{base}/some-key").status_code == 401
    assert c.get(f"{base}/some-key", headers={"X-API-Key": "bad"}).status_code == 401


def test_integration_and_mailbox_routes_stay_fail_closed_with_a_503_never_default(sm, no_keys):
    c = TestClient(app)
    assert c.post("/integrations/leadboost/outreach-requests", json={}).status_code == 503
    assert c.get("/integrations/leadboost/outreach-actions/k/conversation").status_code == 503
    assert c.get("/mailboxes").status_code == 503
    assert c.post("/mailboxes", json={}).status_code == 503


# --------------------------------------------------------------------------- spec 13: startup validation
def test_startup_refuses_when_no_keys_are_configured(no_keys):
    with pytest.raises(deps.AuthConfigurationError, match="No API keys are configured"):
        deps.validate_auth_configuration()


def test_startup_refuses_an_invalid_configuration(monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", "ORG_KEY_MAP must be a JSON object")
    with pytest.raises(deps.AuthConfigurationError, match="Invalid authentication configuration"):
        deps.validate_auth_configuration()


def test_startup_accepts_a_valid_configuration(keys):
    deps.validate_auth_configuration()


def test_lifespan_refuses_to_start_before_touching_the_database(no_keys, monkeypatch):
    called = []
    monkeypatch.setattr(api_main, "init_db", lambda: called.append("init_db"))
    monkeypatch.setattr(get_settings(), "run_scheduler_in_process", False)
    with pytest.raises(deps.AuthConfigurationError), TestClient(app):
        pass
    assert called == []


def test_lifespan_starts_with_valid_keys(keys, monkeypatch):
    called = []
    monkeypatch.setattr(api_main, "init_db", lambda: called.append("init_db"))
    monkeypatch.setattr(get_settings(), "run_scheduler_in_process", False)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
    assert called == ["init_db"]


# --------------------------------------------------------------------------- spec 15: dev opt-out
def test_production_default_is_secure_when_the_variable_is_missing(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_NO_AUTH", raising=False)
    assert Settings(_env_file=None).allow_insecure_no_auth is False


def test_insecure_opt_out_opens_only_human_routes_and_only_with_no_keys(sm, monkeypatch, caplog):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    s = get_settings()
    monkeypatch.setattr(s, "allow_insecure_no_auth", True)
    monkeypatch.setattr(s, "live_sending_enabled", False)
    with caplog.at_level(logging.WARNING, logger="mailer_agent.api.deps"):
        deps.validate_auth_configuration()
    assert "INSECURE" in caplog.text
    c = TestClient(app)
    assert c.get("/campaigns").status_code == 200  # dev: open as org "default"
    # the integration / credential boundary never honours the opt-out
    assert c.post("/integrations/leadboost/outreach-requests", json={}).status_code == 503
    assert c.get("/integrations/leadboost/outreach-actions/k/conversation").status_code == 503
    assert c.get("/mailboxes").status_code == 503


def test_insecure_opt_out_is_ignored_when_any_key_is_configured(sm, keys, monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", True)
    with caplog.at_level(logging.WARNING, logger="mailer_agent.api.deps"):
        deps.validate_auth_configuration()
    assert "ignored" in caplog.text
    c = TestClient(app)
    assert c.get("/campaigns").status_code == 401
    assert c.get("/campaigns", headers={"X-API-Key": KEY_A}).status_code == 200


def test_insecure_opt_out_cannot_start_with_live_sending(no_keys, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allow_insecure_no_auth", True)
    monkeypatch.setattr(s, "live_sending_enabled", True)
    with pytest.raises(deps.AuthConfigurationError, match="LIVE_SENDING_ENABLED"):
        deps.validate_auth_configuration()


def test_legacy_plus_explicit_keys_warns_at_startup(monkeypatch, caplog):
    monkeypatch.setattr(deps, "_KEY_MAP", {LEGACY: "default", KEY_A: ORG_A})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    monkeypatch.setattr(deps, "_AUTH_CONFIG", deps.AuthConfig(
        key_map={LEGACY: "default", KEY_A: ORG_A}, legacy_key_active=True, explicit_keys_active=True))
    with caplog.at_level(logging.WARNING, logger="mailer_agent.api.deps"):
        deps.validate_auth_configuration()
    assert "Legacy API_KEY is active" in caplog.text
    assert LEGACY not in caplog.text and KEY_A not in caplog.text
