"""
PR 2 -- tenant authentication map integrity.

A credential must identify exactly ONE tenant. Before this change:

* ``json.loads`` silently collapsed a repeated literal key in ORG_KEY_MAP to its last value, and
* the three sources (ORG_KEY_MAP > ORG_KEYS_* > legacy API_KEY) were merged with silent overwrite,
  so a credential configured for two organizations quietly became whichever source ranked highest.

Now both are rejected exactly like a malformed ORG_KEY_MAP: the key map is left empty, the API
refuses to start, and every protected route fails closed with 503. Legitimate configurations are
unchanged: distinct keys per tenant, SEVERAL keys for one tenant (rollover), the same key repeated
for the SAME tenant, and the legacy key as organization "default".

Synthetic only: no database beyond an isolated SQLite file, no network, no real credentials. The
real authentication dependencies of the real ``app`` are used (none is overridden).
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mailer_agent.api import deps
from mailer_agent.api import main as api_main
from mailer_agent.api.main import app
from mailer_agent.config import get_settings
from mailer_agent.db import Base, get_db

KEY_A, KEY_B, KEY_C = "key-a-" + "a" * 24, "key-b-" + "b" * 24, "key-c-" + "c" * 24
LEGACY = "legacy-" + "l" * 24
SHARED = "shared-" + "s" * 24
ORG_A, ORG_B = "org-alpha", "org-bravo"
OP_KEY = "operator-" + "o" * 28

ALL_SECRET_VALUES = (KEY_A, KEY_B, KEY_C, LEGACY, SHARED, OP_KEY)
ALL_IDENTIFIERS = (ORG_A, ORG_B, "ORG_KEYS_orgalpha", "ORG_KEYS_orgbravo")


def load(env=None, legacy=""):
    return deps.load_auth_config(env or {}, legacy_api_key=legacy)


def map_json(mapping: dict) -> dict:
    return {"ORG_KEY_MAP": json.dumps(mapping)}


def assert_rejected(cfg):
    assert cfg.error, "expected the configuration to be rejected"
    assert cfg.key_map == {}  # whole config rejected: no partial map, no legacy fallback
    assert not cfg.legacy_key_active and not cfg.explicit_keys_active
    for value in ALL_SECRET_VALUES + ALL_IDENTIFIERS:
        assert value not in cfg.error, "a secret/identifier leaked into the error text"


@pytest.fixture(autouse=True)
def _real_auth_only():
    overridden = [
        d
        for d in app.dependency_overrides
        if d in {deps.require_api_key, deps.get_current_org_id, deps.get_integration_org_id, deps.get_authenticated_org_id}
    ]
    assert not overridden
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def sm(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'pr2.db'}", connect_args={"check_same_thread": False})
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


def activate(monkeypatch, cfg):
    """Install a loaded config as the process's effective authentication state."""
    monkeypatch.setattr(deps, "_AUTH_CONFIG", cfg)
    monkeypatch.setattr(deps, "_KEY_MAP", cfg.key_map)
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", cfg.error)
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", False)
    monkeypatch.setattr(get_settings(), "system_operator_api_key", SecretStr(""))


# ---------------------------------------------------------------------------
# Valid configurations are unchanged
# ---------------------------------------------------------------------------


def test_valid_single_tenant_map():
    cfg = load(map_json({KEY_A: ORG_A}))
    assert cfg.error is None and cfg.key_map == {KEY_A: ORG_A} and cfg.explicit_keys_active


def test_distinct_credentials_for_multiple_tenants_across_all_sources():
    env = {**map_json({KEY_A: ORG_A}), "ORG_KEYS_orgbravo": KEY_B}
    cfg = load(env, legacy=LEGACY)
    assert cfg.error is None
    assert cfg.key_map == {LEGACY: "default", KEY_B: "orgbravo", KEY_A: ORG_A}
    assert cfg.legacy_key_active and cfg.explicit_keys_active


def test_integer_org_ids_still_accepted():
    cfg = load({"ORG_KEY_MAP": '{"k1": "acme", "k2": 7}'})
    assert cfg.error is None and cfg.key_map == {"k1": "acme", "k2": "7"}


# ---------------------------------------------------------------------------
# Supported rotation: several distinct credentials for ONE tenant
# ---------------------------------------------------------------------------


def test_rollover_two_keys_for_one_tenant_inside_org_key_map():
    cfg = load(map_json({KEY_A: ORG_A, KEY_B: ORG_A, KEY_C: ORG_B}))
    assert cfg.error is None
    assert cfg.key_map == {KEY_A: ORG_A, KEY_B: ORG_A, KEY_C: ORG_B}


def test_rollover_old_key_in_map_new_key_in_org_keys_for_the_same_tenant():
    cfg = load({**map_json({KEY_A: "orgalpha"}), "ORG_KEYS_orgalpha": KEY_B})
    assert cfg.error is None and cfg.key_map == {KEY_A: "orgalpha", KEY_B: "orgalpha"}


def test_rollover_keys_both_authenticate_as_the_same_tenant_and_nobody_else(monkeypatch):
    activate(monkeypatch, load(map_json({KEY_A: ORG_A, KEY_B: ORG_A, KEY_C: ORG_B})))
    assert deps._resolve_org(KEY_A) == ORG_A
    assert deps._resolve_org(KEY_B) == ORG_A
    assert deps._resolve_org(KEY_C) == ORG_B
    assert deps.get_current_org_id(KEY_A) == deps.get_current_org_id(KEY_B) == ORG_A
    assert deps.get_current_org_id(KEY_C) == ORG_B  # tenant isolation unchanged


def test_removing_the_old_key_completes_the_rollover(monkeypatch):
    activate(monkeypatch, load(map_json({KEY_B: ORG_A})))
    assert deps._resolve_org(KEY_B) == ORG_A and deps._resolve_org(KEY_A) is None


def test_same_key_repeated_for_the_same_tenant_is_harmless():
    # across sources
    assert load({**map_json({SHARED: "orgalpha"}), "ORG_KEYS_orgalpha": SHARED}).error is None
    # legacy key explicitly mapped to its own org "default"
    assert load(map_json({LEGACY: "default"}), legacy=LEGACY).error is None
    # ORG_KEYS_ names differing only in case collapse to the same (lower-cased) tenant
    assert load({"ORG_KEYS_Acme": SHARED, "ORG_KEYS_ACME": SHARED}).error is None
    # integer and string spellings of one org id are the same tenant
    assert load({**map_json({SHARED: 7}), "ORG_KEYS_7": SHARED}).error is None


# ---------------------------------------------------------------------------
# Duplicate literal JSON keys in ORG_KEY_MAP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        f'{{"{SHARED}": "{ORG_A}", "{SHARED}": "{ORG_B}"}}',  # conflicting tenants: last used to win silently
        f'{{"{SHARED}": "{ORG_A}", "{SHARED}": "{ORG_A}"}}',  # even an identical repeat is ambiguous/typo-prone
        f'{{"{KEY_A}": "{ORG_A}", "{SHARED}": "{ORG_A}", "{KEY_B}": "{ORG_B}", "{SHARED}": "{ORG_B}"}}',
    ],
)
def test_duplicate_literal_json_keys_are_rejected(raw):
    cfg = load({"ORG_KEY_MAP": raw}, legacy=LEGACY)
    assert_rejected(cfg)  # not even the legacy key survives
    assert "more than once" in cfg.error


def test_json_loads_alone_would_have_hidden_the_duplicate():
    """Documents the defect being closed: the stdlib silently keeps the last duplicate."""
    assert json.loads(f'{{"{SHARED}": "{ORG_A}", "{SHARED}": "{ORG_B}"}}') == {SHARED: ORG_B}


# ---------------------------------------------------------------------------
# Same credential, different tenants, via different sources
# ---------------------------------------------------------------------------


def test_org_key_map_versus_org_keys_conflict_is_rejected():
    cfg = load({**map_json({SHARED: ORG_A}), "ORG_KEYS_orgbravo": SHARED})
    assert_rejected(cfg)
    assert "ORG_KEY_MAP" in cfg.error and "ORG_KEYS_*" in cfg.error and "between" in cfg.error


def test_two_org_keys_variables_with_the_same_value_conflict_is_rejected():
    cfg = load({"ORG_KEYS_orgalpha": SHARED, "ORG_KEYS_orgbravo": SHARED})
    assert_rejected(cfg)
    assert "within ORG_KEYS_*" in cfg.error


def test_org_key_map_conflict_between_entries_whose_keys_differ_only_by_whitespace():
    # An HTTP header value is whitespace-trimmed in transit, so these are one credential.
    cfg = load(map_json({SHARED: ORG_A, f"  {SHARED}\t": ORG_B}))
    assert_rejected(cfg)
    assert "within ORG_KEY_MAP" in cfg.error


def test_org_keys_value_with_surrounding_whitespace_is_compared_trimmed():
    assert_rejected(load({**map_json({SHARED: ORG_A}), "ORG_KEYS_orgbravo": f"  {SHARED}  "}))


def test_org_ids_differing_only_by_case_are_different_tenants_and_conflict():
    # Tenant filters compare organization ids exactly, so "Acme" and "acme" are two tenants.
    assert_rejected(load(map_json({SHARED: "Acme", KEY_A: "acme"}) | {"ORG_KEYS_acme": SHARED}))


# ---------------------------------------------------------------------------
# Legacy API_KEY collisions
# ---------------------------------------------------------------------------


def test_legacy_key_equal_to_a_tenant_key_in_org_key_map_is_rejected():
    # Used to be silently re-assigned to ORG_A (ORG_KEY_MAP "won"); the legacy key then stopped
    # working as org "default" with no warning.
    cfg = load(map_json({LEGACY: ORG_A}), legacy=LEGACY)
    assert_rejected(cfg)
    assert "API_KEY (legacy)" in cfg.error and "ORG_KEY_MAP" in cfg.error


def test_legacy_key_equal_to_a_tenant_key_in_org_keys_is_rejected():
    cfg = load({"ORG_KEYS_orgalpha": LEGACY}, legacy=LEGACY)
    assert_rejected(cfg)
    assert "API_KEY (legacy)" in cfg.error and "ORG_KEYS_*" in cfg.error


def test_three_way_collision_is_rejected():
    cfg = load({**map_json({SHARED: ORG_A}), "ORG_KEYS_orgbravo": SHARED}, legacy=SHARED)
    assert_rejected(cfg)


def test_legacy_key_alone_and_alongside_distinct_tenants_still_works_as_default():
    assert load({}, legacy=LEGACY).key_map == {LEGACY: "default"}
    cfg = load(map_json({KEY_A: ORG_A}), legacy=LEGACY)
    assert cfg.error is None and cfg.key_map == {LEGACY: "default", KEY_A: ORG_A}


def test_legacy_key_may_share_the_default_org_with_an_explicit_default_tenant_key():
    cfg = load(map_json({KEY_A: "default"}), legacy=LEGACY)  # two keys, one tenant "default": documented
    assert cfg.error is None and cfg.key_map == {LEGACY: "default", KEY_A: "default"}


# ---------------------------------------------------------------------------
# Malformed / missing / blank configuration (pre-existing contracts, re-pinned)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["{not json", "[]", '"s"', "123", "null", '{"k": ""}', '{"": "o"}', '{"k": null}'])
def test_malformed_map_is_still_rejected_whole_even_with_a_legacy_key(raw):
    cfg = load({"ORG_KEY_MAP": raw}, legacy=LEGACY)
    assert cfg.error and cfg.key_map == {}


@pytest.mark.parametrize("env", [{}, {"ORG_KEY_MAP": ""}, {"ORG_KEY_MAP": "   "}, {"ORG_KEYS_x": "  "}])
def test_missing_or_blank_configuration_is_not_a_collision_error_but_yields_no_keys(env, monkeypatch):
    cfg = load(env)
    assert cfg.error is None and cfg.key_map == {}
    activate(monkeypatch, cfg)
    with pytest.raises(deps.AuthConfigurationError, match="No API keys are configured"):
        deps.validate_auth_configuration()


def test_explicit_empty_object_with_no_other_source_is_valid_but_empty():
    cfg = load({"ORG_KEY_MAP": "{}"})
    assert cfg.error is None and cfg.key_map == {}


# ---------------------------------------------------------------------------
# Fail closed at startup and per request
# ---------------------------------------------------------------------------

CONFLICTS = {
    "map-vs-keys": ({**map_json({SHARED: ORG_A}), "ORG_KEYS_orgbravo": SHARED}, ""),
    "legacy-vs-map": (map_json({LEGACY: ORG_A}), LEGACY),
    "duplicate-json": ({"ORG_KEY_MAP": f'{{"{SHARED}": "{ORG_A}", "{SHARED}": "{ORG_B}"}}'}, ""),
}


@pytest.mark.parametrize("name", CONFLICTS)
def test_startup_refuses_a_conflicting_configuration(name, monkeypatch):
    env, legacy = CONFLICTS[name]
    activate(monkeypatch, load(env, legacy))
    with pytest.raises(deps.AuthConfigurationError, match="Invalid authentication configuration") as exc:
        deps.validate_auth_configuration()
    assert "Refusing to start" in str(exc.value)
    for value in ALL_SECRET_VALUES + ALL_IDENTIFIERS:
        assert value not in str(exc.value)


@pytest.mark.parametrize("name", CONFLICTS)
def test_lifespan_refuses_to_start_before_touching_the_database(name, monkeypatch):
    env, legacy = CONFLICTS[name]
    activate(monkeypatch, load(env, legacy))
    called = []
    # The lifespan's schema step is `prepare_schema` once the concurrent-start fix is present, `init_db`
    # before it: patch whichever this tree has, so this test holds with either change merged first.
    schema_step = "prepare_schema" if hasattr(api_main, "prepare_schema") else "init_db"
    monkeypatch.setattr(api_main, schema_step, lambda: called.append(schema_step))
    monkeypatch.setattr(get_settings(), "run_scheduler_in_process", False)
    with pytest.raises(deps.AuthConfigurationError), TestClient(app):
        pass
    assert called == []


@pytest.mark.parametrize("name", CONFLICTS)
def test_every_protected_route_is_503_under_a_conflicting_configuration(name, sm, monkeypatch):
    env, legacy = CONFLICTS[name]
    activate(monkeypatch, load(env, legacy))
    c = TestClient(app, raise_server_exceptions=False)
    for presented in (SHARED, LEGACY, KEY_A, None):  # including the very key that collided
        headers = {"X-API-Key": presented} if presented else {}
        for path in ("/mailboxes", "/campaigns", "/integrations/leadboost/outreach-actions/k/conversation"):
            resp = c.get(path, headers=headers)
            assert resp.status_code == 503, (name, presented, path, resp.status_code)
            assert SHARED not in resp.text and LEGACY not in resp.text


def test_the_colliding_credential_authenticates_as_nobody(monkeypatch):
    activate(monkeypatch, load({**map_json({SHARED: ORG_A}), "ORG_KEYS_orgbravo": SHARED}))
    assert deps._resolve_org(SHARED) is None
    with pytest.raises(Exception) as exc:  # HTTPException 503: nothing configured, never a tenant
        deps.get_current_org_id(SHARED)
    assert getattr(exc.value, "status_code", None) == 503


def test_startup_log_and_error_text_never_contain_keys_ids_or_variable_names(monkeypatch, caplog):
    env = {
        **map_json({SHARED: ORG_A, KEY_A: ORG_B}),
        "ORG_KEYS_orgalpha": KEY_C,
        "ORG_KEYS_orgbravo": SHARED,
    }
    with caplog.at_level(logging.DEBUG):
        cfg = load(env, legacy=LEGACY)
        activate(monkeypatch, cfg)
        with pytest.raises(deps.AuthConfigurationError) as exc:
            deps.validate_auth_configuration()
        TestClient(app, raise_server_exceptions=False).get("/mailboxes", headers={"X-API-Key": SHARED})
    blob = caplog.text + cfg.error + str(exc.value)
    for value in ALL_SECRET_VALUES + ALL_IDENTIFIERS + (KEY_A + "x",):
        assert value not in blob


# ---------------------------------------------------------------------------
# Operator key separation is preserved
# ---------------------------------------------------------------------------


def _with_operator_key(monkeypatch, cfg, key):
    activate(monkeypatch, cfg)
    monkeypatch.setattr(get_settings(), "system_operator_api_key", SecretStr(key))


def test_operator_key_distinct_from_every_tenant_key_starts(monkeypatch):
    _with_operator_key(monkeypatch, load({**map_json({KEY_A: ORG_A}), "ORG_KEYS_orgbravo": KEY_B}, LEGACY), OP_KEY)
    deps.validate_auth_configuration()


@pytest.mark.parametrize(
    "env, legacy, collides_with",
    [
        (map_json({KEY_A: ORG_A}), "", KEY_A),
        ({"ORG_KEYS_orgbravo": KEY_B}, "", KEY_B),
        ({}, LEGACY, LEGACY),
        # a rollover key is still a tenant key
        (map_json({KEY_A: ORG_A, KEY_B: ORG_A}), "", KEY_B),
    ],
)
def test_operator_key_equal_to_any_tenant_credential_still_refuses_to_start(monkeypatch, env, legacy, collides_with):
    _with_operator_key(monkeypatch, load(env, legacy), collides_with)
    with pytest.raises(deps.AuthConfigurationError, match="SYSTEM_OPERATOR_API_KEY") as exc:
        deps.validate_auth_configuration()
    assert collides_with not in str(exc.value)


def test_a_tenant_key_collision_also_closes_the_operator_routes(sm, monkeypatch):
    _with_operator_key(monkeypatch, load({**map_json({SHARED: ORG_A}), "ORG_KEYS_orgbravo": SHARED}), OP_KEY)
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/system/config", headers={"X-System-Operator-Key": OP_KEY})
    assert resp.status_code == 503  # fail closed: nothing is partly honoured


# ---------------------------------------------------------------------------
# Existing tenant isolation and status-code contract (valid config)
# ---------------------------------------------------------------------------


def test_status_code_contract_for_a_valid_multi_tenant_configuration(sm, monkeypatch):
    activate(monkeypatch, load({**map_json({KEY_A: ORG_A}), "ORG_KEYS_orgbravo": KEY_B}))
    deps.validate_auth_configuration()  # starts
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/mailboxes", headers={"X-API-Key": KEY_A}).status_code == 200
    assert c.get("/mailboxes", headers={"X-API-Key": KEY_B}).status_code == 200
    assert c.get("/mailboxes", headers={"X-API-Key": "wrong-" + "w" * 24}).status_code == 401
    assert c.get("/mailboxes").status_code == 401
    assert deps.get_current_org_id(KEY_A) == ORG_A and deps.get_current_org_id(KEY_B) == "orgbravo"


def test_a_tenant_key_never_opens_operator_routes_and_the_operator_key_never_opens_tenant_routes(sm, monkeypatch):
    _with_operator_key(monkeypatch, load(map_json({KEY_A: ORG_A})), OP_KEY)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/system/config", headers={"X-System-Operator-Key": KEY_A}).status_code == 401
    assert c.get("/mailboxes", headers={"X-API-Key": OP_KEY}).status_code == 401


def test_every_tenant_authenticated_route_still_exists_in_the_openapi_surface():
    """Sanity: the change touched configuration loading only, not route wiring."""
    protected = [
        r
        for r in app.routes
        if isinstance(r, APIRoute)
        and any(getattr(d.call, "__name__", "") in {"require_api_key", "get_current_org_id"} for d in r.dependant.dependencies)
    ]
    assert len(protected) > 5
