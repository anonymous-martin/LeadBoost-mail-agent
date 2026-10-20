"""
PR4 -- a non-ASCII presented tenant API key is an ordinary invalid credential, never a server error.

Defect: ``deps._resolve_org`` compared with ``secrets.compare_digest(str, str)``, which raises
``TypeError`` when either argument contains a non-ASCII character, so a malformed ``X-API-Key``
produced an HTTP 500 instead of a 401. ``_resolve_org`` now uses ``_ct_equal`` (constant-time
comparison of UTF-8 bytes), the helper PR3 already uses for operator-credential comparisons.

This is an input-handling defect, not an authentication bypass: the tests also pin that valid keys
still resolve to the right organization and that the operator credential is unaffected.

HTTP-level note: the real stack is exercised. httpx refuses a non-ASCII ``str`` header value
client-side, so the header is sent as raw UTF-8 / Latin-1 *bytes*; Starlette decodes request header
bytes as Latin-1, which hands the application a ``str`` containing non-ASCII characters -- exactly the
input that used to raise. Nothing here overrides an authentication dependency.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mailer_agent.api import deps
from mailer_agent.api.main import app
from mailer_agent.config import get_settings
from mailer_agent.db import Base, get_db

KEY_A, KEY_B, LEGACY = "key-a-" + "a" * 20, "key-b-" + "b" * 20, "legacy-" + "l" * 20
ORG_A, ORG_B = "org-a", "org-b"
OP_KEY = "operator-" + "o" * 28

# Raw header values (bytes): UTF-8 text, a lone Latin-1 byte, and a long multi-script value.
NON_ASCII_HEADERS = [
    pytest.param("clé-ü".encode("utf-8"), id="utf8-latin"),
    pytest.param("密钥".encode("utf-8"), id="utf8-cjk"),
    pytest.param(b"key-\xe9-" + b"x" * 20, id="latin1-byte"),
    pytest.param(("ключ-" * 20).encode("utf-8"), id="utf8-long-cyrillic"),
]
# The same idea as already-decoded strings, for the resolver called directly (incl. a lone surrogate).
NON_ASCII_STRINGS = ["clé-ü", "密钥", "ключ", "key-\u00e9-" + "x" * 20, "\ud800"]

AUTH_DEPS = (
    deps.require_api_key,
    deps.get_current_org_id,
    deps.get_integration_org_id,
    deps.get_authenticated_org_id,
)


@pytest.fixture(autouse=True)
def _real_auth_only():
    overridden = [d for d in app.dependency_overrides if d in AUTH_DEPS + (deps.require_system_operator,)]
    assert not overridden, f"auth dependencies overridden: {overridden}"
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def sm(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'pr4.db'}", connect_args={"check_same_thread": False})
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
    monkeypatch.setattr(deps, "_KEY_MAP", {KEY_A: ORG_A, KEY_B: ORG_B, LEGACY: "default"})
    monkeypatch.setattr(deps, "_AUTH_CONFIG_ERROR", None)
    monkeypatch.setattr(get_settings(), "allow_insecure_no_auth", False)


def _direct_calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _direct_calls(d)


def _route_for(dep):
    """First route whose dependency tree contains ``dep`` (so every tenant dependency is covered)."""
    for r in app.routes:
        if isinstance(r, APIRoute) and dep in set(_direct_calls(r.dependant)):
            method = sorted(r.methods - {"HEAD", "OPTIONS"})[0]
            import re

            return method, re.sub(r"\{[^}]+\}", "1", r.path)
    raise AssertionError(f"no route uses {dep.__name__}")


# --------------------------------------------------------------------------- resolver, directly
@pytest.mark.parametrize("presented", NON_ASCII_STRINGS)
def test_resolver_returns_no_match_for_non_ascii_key(keys, presented):
    assert deps._resolve_org(presented) is None  # used to raise TypeError


def test_resolver_still_maps_valid_keys_to_their_organization(keys):
    assert deps._resolve_org(KEY_A) == ORG_A
    assert deps._resolve_org(KEY_B) == ORG_B
    assert deps._resolve_org(LEGACY) == "default"
    assert deps._resolve_org(KEY_A + "x") is None
    assert deps._resolve_org("") is None


def test_resolver_for_a_key_repeated_for_the_same_organization_is_unchanged(monkeypatch):
    """One dict entry per key string. Repeating a key for the SAME organization across sources is
    harmless and still resolves to it. (Repeating it for DIFFERENT organizations used to be settled
    silently by precedence; PR 2 rejects that configuration -- see test_pr2_key_map_collisions.py.)"""
    cfg = deps.load_auth_config(
        environ={"ORG_KEY_MAP": '{"shared-key-xxxxxxxx": "default"}', "ORG_KEYS_default": "shared-key-xxxxxxxx"},
        legacy_api_key="shared-key-xxxxxxxx",
    )
    assert cfg.error is None
    monkeypatch.setattr(deps, "_KEY_MAP", cfg.key_map)
    assert deps._resolve_org("shared-key-xxxxxxxx") == "default"


# --------------------------------------------------------------------------- HTTP, real dependencies
@pytest.mark.parametrize("raw", NON_ASCII_HEADERS)
@pytest.mark.parametrize("dep", AUTH_DEPS, ids=lambda d: d.__name__)
def test_non_ascii_x_api_key_is_401_not_500(dep, raw, keys, sm):
    method, path = _route_for(dep)
    c = TestClient(app, raise_server_exceptions=False)  # a TypeError would surface as a 500, not raise
    r = c.request(method, path, headers={"X-API-Key": raw})
    assert r.status_code == 401, (dep.__name__, method, path, r.status_code, r.text)
    # Same body as any other unknown key: nothing about the credential or an exception is echoed.
    text = r.text
    assert r.json()["detail"] in {"Invalid API key", "Invalid or missing API key"}
    for leak in (raw.decode("latin-1"), raw.decode("utf-8", "replace"), "TypeError", "Traceback", "compare_digest"):
        assert leak not in text


@pytest.mark.parametrize("dep", AUTH_DEPS, ids=lambda d: d.__name__)
def test_non_ascii_response_equals_an_ordinary_wrong_key_response(dep, keys, sm):
    method, path = _route_for(dep)
    c = TestClient(app, raise_server_exceptions=False)
    ordinary = c.request(method, path, headers={"X-API-Key": "wrong-key-" + "w" * 20})
    non_ascii = c.request(method, path, headers={"X-API-Key": "clé-ü".encode("utf-8")})
    assert (non_ascii.status_code, non_ascii.json()) == (ordinary.status_code, ordinary.json()) == (401, ordinary.json())


def test_valid_tenant_keys_still_authenticate_over_http(keys, sm):
    method, path = _route_for(deps.get_current_org_id)
    c = TestClient(app, raise_server_exceptions=False)
    for key in (KEY_A, KEY_B, LEGACY):
        assert c.request(method, path, headers={"X-API-Key": key}).status_code != 401
    assert c.request(method, path).status_code == 401  # missing key: existing contract unchanged


# --------------------------------------------------------------------------- operator unchanged
def test_operator_authentication_is_unchanged(keys, sm, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_operator_api_key", SecretStr(OP_KEY))
    c = TestClient(app, raise_server_exceptions=False)
    url = "/system/config"
    assert c.get(url, headers={"X-System-Operator-Key": OP_KEY}).status_code == 200
    # A tenant key never opens an operator route; the operator key never opens a tenant route.
    assert c.get(url, headers={"X-API-Key": KEY_A}).status_code == 401
    assert c.get(url, headers={"X-System-Operator-Key": KEY_A}).status_code == 401
    method, path = _route_for(deps.get_current_org_id)
    assert c.request(method, path, headers={"X-API-Key": OP_KEY}).status_code == 401
    # Non-ASCII operator header: already a 401 via _ct_equal; pinned so it stays that way.
    bad = c.get(url, headers={"X-System-Operator-Key": "clé-ü".encode("utf-8")})
    assert bad.status_code == 401 and OP_KEY not in bad.text
