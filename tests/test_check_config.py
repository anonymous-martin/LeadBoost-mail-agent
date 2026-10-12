"""scripts/check_config.py: shape/presence checks, never prints secret values."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_config.py"
KEY = "K" * 40  # a stand-in API key; must never be echoed
FERNET = Fernet.generate_key().decode()
PW = "SUPERSECRETPW"

_BASE = {
    "DATABASE_URL": f"postgresql://u:{PW}@127.0.0.1:5432/mailer_x",
    "MAILBOX_ENCRYPTION_KEY": FERNET,
    "ORG_KEY_MAP": f'{{"{KEY}": "org1"}}',
    "RUN_SCHEDULER_IN_PROCESS": "false",
    "LEADBOOST_INTEGRATION_SENDER_EMAIL": "ops@example.org",
    "GROQ_API_KEY": "gsk_stand_in",
}


def run(role="api", tmp=None, drop=(), **over):
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(_BASE)
    env.update(over)
    for k in drop:
        env.pop(k, None)
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "--role", role], cwd=tmp, env=env, check=False, capture_output=True, text=True, timeout=120
    )
    out = p.stdout + p.stderr
    for secret in (KEY, FERNET, PW, "gsk_stand_in"):
        assert secret not in out, "a secret value leaked into the preflight output"
    return p.returncode, out


def test_good_api_config_passes(tmp_path):
    rc, out = run(tmp=tmp_path)
    assert rc == 0, out
    assert "org1" in out


def test_good_worker_config_passes(tmp_path):
    rc, out = run("worker", tmp=tmp_path, drop=("ORG_KEY_MAP",))
    assert rc == 0, out


def test_scheduler_default_true_on_api_fails(tmp_path):
    rc, out = run(tmp=tmp_path, drop=("RUN_SCHEDULER_IN_PROCESS",))
    assert rc == 1 and "RUN_SCHEDULER_IN_PROCESS" in out and "FAIL" in out


@pytest.mark.parametrize("bad", ["{not json", '["a","b"]', "[]"])
def test_malformed_org_key_map_fails_and_never_crashes(bad, tmp_path):
    rc, out = run(tmp=tmp_path, ORG_KEY_MAP=bad)
    assert rc == 1, out
    assert "ORG_KEY_MAP" in out or "auth key map" in out
    assert "Traceback" not in out


def test_no_keys_at_all_fails_closed_and_is_reported_as_a_failure(tmp_path):
    rc, out = run(tmp=tmp_path, drop=("ORG_KEY_MAP",))
    assert rc == 1 and "refuses to start" in out and "503" in out
    assert "UNAUTHENTICATED" not in out


def test_insecure_opt_out_with_no_keys_is_a_failure_in_a_deployment_preflight(tmp_path):
    rc, out = run(tmp=tmp_path, drop=("ORG_KEY_MAP",), ALLOW_INSECURE_NO_AUTH="true")
    assert rc == 1 and "UNAUTHENTICATED" in out and "Never on a deployed service" in out


def test_insecure_opt_out_alongside_real_keys_warns_it_is_ignored(tmp_path):
    rc, out = run(tmp=tmp_path, ALLOW_INSECURE_NO_AUTH="true")
    assert rc == 0, out
    assert "ALLOW_INSECURE_NO_AUTH" in out and "ignored" in out


def test_legacy_api_key_alongside_org_key_map_warns(tmp_path):
    rc, out = run(tmp=tmp_path, API_KEY="L" * 40)
    assert rc == 0, out
    assert "WARN" in out and "API_KEY (legacy)" in out
    assert "L" * 40 not in out


def test_missing_or_sqlite_database_url_fails(tmp_path):
    rc, out = run(tmp=tmp_path, drop=("DATABASE_URL",))
    assert rc == 1 and "DATABASE_URL" in out
    rc, out = run(tmp=tmp_path, DATABASE_URL="sqlite:///./mailer_agent.db")
    assert rc == 1


def test_invalid_encryption_key_fails(tmp_path):
    rc, out = run(tmp=tmp_path, MAILBOX_ENCRYPTION_KEY="not-a-fernet-key")
    assert rc == 1 and "MAILBOX_ENCRYPTION_KEY" in out


def test_short_key_and_missing_sender_warn_only(tmp_path):
    rc, out = run(tmp=tmp_path, ORG_KEY_MAP='{"short": "org1"}', LEADBOOST_INTEGRATION_SENDER_EMAIL="")
    assert rc == 0, out
    assert out.count("WARN") >= 2
