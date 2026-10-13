"""
PR2 -- public API surface: interactive documentation is opt-in, and ``/health`` is liveness only.

* ``/docs``, ``/redoc`` and ``/openapi.json`` must be ABSENT (404) unless ``API_DOCS_ENABLED`` is
  deliberately true, and all three must be present together when it is.
* ``GET /health`` keeps its path and HTTP 200 (Render's ``healthCheckPath``) and returns exactly
  ``{"status": "ok"}`` -- no sending / auto-reply flags.

The documentation URLs are fixed when ``mailer_agent.api.main`` builds the module-level ``app`` at
import time, from a cached settings object. Monkeypatching ``get_settings()`` in-process therefore
proves nothing, and re-importing the module would disturb every other test that holds ``app``. So the
docs tests run the real application in a FRESH interpreter with the real environment variable:
the value is parsed by ``Settings`` and used by the real ``FastAPI(...)`` construction. The lifespan is
not started (``TestClient`` is not used as a context manager), so no database work happens; the
working directory is a temp dir so no ``.env`` file can influence the result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mailer_agent.api.main import app
from mailer_agent.config import Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")

_PROBE = r"""
import json
from fastapi.testclient import TestClient
from mailer_agent.api.main import app

c = TestClient(app)
status, bodies = {}, {}
for p in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
    r = c.get(p)
    status[p] = r.status_code
    bodies[p] = r.text if r.status_code == 200 else ""
schema = json.loads(bodies["/openapi.json"]) if bodies["/openapi.json"] else {}
print("PROBE:" + json.dumps({
    "status": status,
    "app_urls": [app.docs_url, app.redoc_url, app.openapi_url],
    "doc_like_routes": sorted(
        r.path for r in app.routes if any(w in r.path for w in ("docs", "redoc", "openapi"))
    ),
    "swagger_page": "swagger-ui" in bodies["/docs"],
    "redoc_page": "redoc" in bodies["/redoc"].lower(),
    "schema_title": (schema.get("info") or {}).get("title"),
    "schema_has_health": "/health" in (schema.get("paths") or {}),
}))
"""


def _probe(tmp_path, api_docs_enabled):
    """Build the real app in a fresh process with API_DOCS_ENABLED set to the given value (None = unset)."""
    env = {k: v for k, v in os.environ.items() if k != "API_DOCS_ENABLED"}
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'probe.db'}"
    env["PYTHONPATH"] = str(REPO_ROOT)
    if api_docs_enabled is not None:
        env["API_DOCS_ENABLED"] = api_docs_enabled
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,  # outside the repo: a developer's .env must not leak into the result
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("PROBE:")]
    assert len(lines) == 1, done.stdout[-1000:]
    return json.loads(lines[0][len("PROBE:") :])


# --------------------------------------------------------------------------- documentation endpoints
def test_docs_setting_defaults_to_disabled_when_the_variable_is_missing(monkeypatch):
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
    assert Settings(_env_file=None).api_docs_enabled is False


@pytest.mark.parametrize("value", [None, "false"], ids=["unset", "explicit-false"])
def test_documentation_endpoints_are_absent_by_default(tmp_path, value):
    out = _probe(tmp_path, value)
    for path in DOC_PATHS:
        assert out["status"][path] == 404, (path, out["status"][path])
    assert out["status"]["/docs/oauth2-redirect"] == 404  # the Swagger helper route goes with /docs
    assert out["app_urls"] == [None, None, None]
    assert out["doc_like_routes"] == []  # and no alternative path serves them


def test_documentation_endpoints_are_served_only_with_explicit_opt_in(tmp_path):
    out = _probe(tmp_path, "true")
    for path in DOC_PATHS:
        assert out["status"][path] == 200, (path, out["status"][path])
    assert out["app_urls"] == ["/docs", "/redoc", "/openapi.json"]
    assert out["swagger_page"] and out["redoc_page"]
    assert out["schema_title"] == "Mailer Agent" and out["schema_has_health"]


# --------------------------------------------------------------------------- /health
def test_health_is_exactly_status_ok_with_http_200_and_needs_no_key():
    r = TestClient(app).get("/health")  # no X-API-Key: Render's health check sends none
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_does_not_publish_the_safety_toggles(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "live_sending_enabled", True)
    monkeypatch.setattr(s, "auto_reply_enabled", True)
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert "live_sending_enabled" not in r.text and "auto_reply_enabled" not in r.text
