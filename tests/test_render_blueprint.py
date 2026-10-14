"""
Deployment guard: render.yaml must provision MAILBOX_ENCRYPTION_KEY for every
service that touches mailbox credentials.

Mailer M1/M2 deliberately fail closed when the key is missing: the API cannot
create/update a mailbox (503) and the dispatch worker cannot decrypt a
credential to send. A blueprint that omits the key for either service deploys
a system that accepts work it can never perform. Plain-text parsing on
purpose -- no YAML dependency is declared in requirements.txt.
"""

from __future__ import annotations

import re
from pathlib import Path

RENDER_YAML = Path(__file__).resolve().parents[1] / "render.yaml"


def _service_blocks() -> dict[str, str]:
    text = RENDER_YAML.read_text(encoding="utf-8")
    services_part = text.split("\nservices:", 1)[1]
    blocks = re.split(r"\n  - type: ", services_part)[1:]
    out = {}
    for block in blocks:
        name = re.search(r"^\s*name:\s*(\S+)", block, re.MULTILINE).group(1)
        out[name] = block
    return out


def test_blueprint_declares_both_services():
    assert {"mailer-agent-api", "mailer-agent-worker"} <= set(_service_blocks())


def test_mailbox_encryption_key_is_provisioned_for_api_and_worker_without_a_value():
    blocks = _service_blocks()
    for name in ("mailer-agent-api", "mailer-agent-worker"):
        block = blocks[name]
        m = re.search(r"- key: MAILBOX_ENCRYPTION_KEY\s*\n\s*sync: false", block)
        assert m, f"{name} must declare MAILBOX_ENCRYPTION_KEY with sync: false"
        # A secret must never be committed in the blueprint.
        assert not re.search(r"MAILBOX_ENCRYPTION_KEY\s*\n\s*value:", block)


# --- External-Supabase production profile -------------------------------------------------
# The production database is a dedicated Supabase project supplied out of band, so the
# Blueprint must create the two application services and nothing else.


def _env_block(block: str, key: str) -> str | None:
    """The lines of one `- key: KEY` env var entry (up to the next entry), or None."""
    entries: list[list[str]] = []
    for line in block.splitlines():
        if re.match(r"^\s*- key:\s*\S", line):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
    for lines in entries:
        if re.match(rf"^\s*- key:\s*{re.escape(key)}\s*(#.*)?$", lines[0]):
            return "\n".join(lines)
    return None


def _full_text() -> str:
    return RENDER_YAML.read_text(encoding="utf-8")


def _code_lines(text: str) -> str:
    """The manifest without comment-only lines, so prose in comments cannot satisfy or break a check."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_blueprint_provisions_no_render_database():
    code = _code_lines(_full_text())
    assert not re.search(r"^databases:", code, re.MULTILINE)
    assert "fromDatabase" not in code
    assert "mailer-agent-db" not in code


def test_blueprint_has_exactly_one_web_service_and_one_worker():
    code = _code_lines(_full_text())
    assert len(re.findall(r"^  - type: web\s*$", code, re.MULTILINE)) == 1
    assert len(re.findall(r"^  - type: worker\s*$", code, re.MULTILINE)) == 1
    assert len(_service_blocks()) == 2


def test_database_url_is_dashboard_supplied_on_both_services_and_never_in_yaml():
    blocks = _service_blocks()
    for name in ("mailer-agent-api", "mailer-agent-worker"):
        entry = _env_block(blocks[name], "DATABASE_URL")
        assert entry, f"{name} must declare DATABASE_URL"
        assert re.search(r"sync:\s*false", entry), f"{name} DATABASE_URL must be sync: false"
        assert not re.search(r"^\s*value:", entry, re.MULTILINE)
        assert "fromDatabase" not in entry
    # No connection string or credential-bearing URL anywhere in the manifest.
    assert not re.search(r"postgres(?:ql)?(?:\+\w+)?://", _full_text(), re.IGNORECASE)


def test_encryption_key_is_never_generated_per_service():
    # Independently generated values would make API and worker disagree.
    assert "generateValue" not in _code_lines(_full_text())


def test_api_auth_profile_uses_tenant_keys_and_a_separate_api_only_operator_key():
    blocks = _service_blocks()
    api, worker = blocks["mailer-agent-api"], blocks["mailer-agent-worker"]

    org_map = _env_block(api, "ORG_KEY_MAP")
    assert org_map and re.search(r"sync:\s*false", org_map)
    op = _env_block(api, "SYSTEM_OPERATOR_API_KEY")
    assert op and re.search(r"sync:\s*false", op)
    assert not re.search(r"^\s*value:", op, re.MULTILINE)

    # The legacy global API_KEY (org "default" on every route) is not prompted for in this profile.
    for block in (api, worker):
        assert _env_block(block, "API_KEY") is None
    # Tenant map and operator key are API-only.
    for key in ("ORG_KEY_MAP", "SYSTEM_OPERATOR_API_KEY"):
        assert _env_block(worker, key) is None


def test_api_flags_docs_scheduler_and_health_are_pinned():
    api = _service_blocks()["mailer-agent-api"]
    for key in ("API_DOCS_ENABLED", "RUN_SCHEDULER_IN_PROCESS", "LIVE_SENDING_ENABLED", "AUTO_REPLY_ENABLED"):
        entry = _env_block(api, key)
        assert entry and re.search(r'value:\s*"false"', entry), f"API {key} must be the literal string \"false\""
    assert re.search(r"healthCheckPath:\s*/health\s*$", api, re.MULTILINE)
    assert "gunicorn mailer_agent.api.main:app -k uvicorn.workers.UvicornWorker" in api


def test_worker_keeps_sending_disabled_start_command_and_shutdown_drain():
    worker = _service_blocks()["mailer-agent-worker"]
    for key in ("LIVE_SENDING_ENABLED", "AUTO_REPLY_ENABLED"):
        entry = _env_block(worker, key)
        assert entry and re.search(r'value:\s*"false"', entry), f"worker {key} must be the literal string \"false\""
    assert re.search(r"startCommand:\s*python worker\.py\s*$", worker, re.MULTILINE)
    assert re.search(r"maxShutdownDelaySeconds:\s*60\s*$", worker, re.MULTILINE)
    assert _env_block(worker, "API_DOCS_ENABLED") is None


def test_blueprint_pins_region_and_single_worker():
    blocks = _service_blocks()
    for name in ("mailer-agent-api", "mailer-agent-worker"):
        assert re.search(
            r"^\s*region:\s*singapore\s*$",
            blocks[name], re.MULTILINE
        ), name

    worker = blocks["mailer-agent-worker"]
    assert re.search(
        r"^\s*numInstances:\s*1\s*(?:#.*)?$",
        worker, re.MULTILINE
    )


def test_blueprint_does_not_prompt_for_legacy_process_mail_credentials():
    blocks = _service_blocks()
    for name in ("mailer-agent-api", "mailer-agent-worker"):
        for key in (
            "SMTP_USERNAME", "SMTP_PASSWORD", "IMAP_USERNAME", "IMAP_PASSWORD"
        ):
            assert _env_block(blocks[name], key) is None, (name, key)
