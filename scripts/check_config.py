#!/usr/bin/env python3
"""Read-only configuration preflight for a Mailer service. Prints NO secret values.

Run it in the shell of the service you are checking (or locally with that
service's environment exported):

    python scripts/check_config.py --role api
    python scripts/check_config.py --role worker

It reports only presence, validity and shape: booleans, counts, organization
IDs (identifiers, not credentials) and a redacted ``host:port/database``. API
keys, the encryption key and the DSN password are never printed.

It reuses the application's own parsing (``Settings``, ``deps._KEY_MAP``,
``mailbox_secrets``) so it cannot drift from what the service really does. It
checks the *environment of the process it runs in*; it cannot see another
service's dashboard, so run it once per service. It does not prove the
LeadBoost-side ``MAILING_AGENT_ORG_API_KEYS`` matches -- that is verified by an
authenticated request in C14.

Exit status: 1 if any FAIL, else 0 (WARN does not fail).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from migrate_preflight import Refused, validate_environment

MIN_KEY_LEN = 32
_results: list[tuple[str, str, str]] = []


def _r(level: str, name: str, detail: str) -> None:
    _results.append((level, name, detail))
    print(f"[{level:<4}] {name}: {detail}", flush=True)


def check_database() -> None:
    try:
        t = validate_environment(apply=False)
        _r("PASS", "DATABASE_URL", f"PostgreSQL target {t.redacted} (set in environment)")
    except Refused as exc:
        _r("FAIL", "DATABASE_URL", str(exc))


def check_encryption_key() -> None:
    from mailer_agent import mailbox_secrets as ms

    try:
        ms._fernet()
        _r("PASS", "MAILBOX_ENCRYPTION_KEY", "present and a valid Fernet key (value not shown)")
    except Exception as exc:  # noqa: BLE001
        _r("FAIL", "MAILBOX_ENCRYPTION_KEY", f"missing or invalid ({type(exc).__name__}); stored mailbox credentials cannot be read")
    _r("INFO", "MAILBOX_ENCRYPTION_KEY", "must be the SAME value on the API and the worker, and stable across restarts/rollbacks")


def check_flags(settings) -> None:
    _r("INFO", "LIVE_SENDING_ENABLED", f"{settings.live_sending_enabled} (consumed by the worker's dispatch path; set per service)")
    _r("INFO", "AUTO_REPLY_ENABLED", f"{settings.auto_reply_enabled}")
    if settings.live_sending_enabled or settings.auto_reply_enabled:
        _r("WARN", "sending flags", "real sending and/or auto-reply is ENABLED in this process; confirm that is intended")
    _r(
        "PASS" if settings.groq_api_key else "WARN",
        "GROQ_API_KEY",
        "set" if settings.groq_api_key else "empty (generation cycles will fail)",
    )


def check_api(settings) -> None:
    if settings.run_scheduler_in_process:
        _r("FAIL", "RUN_SCHEDULER_IN_PROCESS", "true on the API: every Gunicorn worker would start its own scheduler beside the worker service (the code default is true; set it to false)")
    else:
        _r("PASS", "RUN_SCHEDULER_IN_PROCESS", "false (the separate worker owns the scheduler)")

    try:
        # deps builds the key map at import time (module-level _KEY_MAP / _AUTH_CONFIG_ERROR).
        # Importing never raises for a bad ORG_KEY_MAP any more (C15.1), but stay defensive:
        # report, don't crash.
        from mailer_agent.api import deps

        mapping = deps._KEY_MAP
        config_error = deps._AUTH_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001
        _r("FAIL", "auth key map", f"the API would fail to start: auth configuration raised {type(exc).__name__} at import")
        return

    if config_error:
        _r("FAIL", "ORG_KEY_MAP", f"{config_error}; the API REFUSES TO START and every protected route would answer 503 (nothing is ignored or partly honoured)")
        return
    if not mapping:
        if settings.allow_insecure_no_auth:
            _r("FAIL", "auth key map", "EMPTY with ALLOW_INSECURE_NO_AUTH=true: the human-operated routes are UNAUTHENTICATED (local-dev opt-out). Never on a deployed service; integration/mailbox routes still return 503")
        else:
            _r("FAIL", "auth key map", "EMPTY: the API refuses to start and every protected route answers 503 (fail closed). Set ORG_KEY_MAP (or ORG_KEYS_*; legacy API_KEY)")
        return
    if settings.allow_insecure_no_auth:
        _r("WARN", "ALLOW_INSECURE_NO_AUTH", "set but ignored because keys are configured; unset it")
    orgs = sorted(set(mapping.values()))
    _r("PASS", "auth key map", f"{len(mapping)} key(s) mapping to org id(s): {', '.join(orgs)}")
    short = sum(1 for k in mapping if len(k) < MIN_KEY_LEN)
    if short:
        _r("WARN", "API key strength", f"{short} key(s) shorter than {MIN_KEY_LEN} characters")
    if settings.api_key:
        _r("WARN", "API_KEY (legacy)", "set: it is ALWAYS merged into the key map as org 'default' and is valid on every route including the integration routes. Unset it when ORG_KEY_MAP is used unless intended")
    if "default" in orgs and not settings.api_key:
        _r("WARN", "org 'default'", "an ORG_KEYS_*/ORG_KEY_MAP entry maps to org 'default'")

    check_operator_key(settings)

    sender = (settings.leadboost_integration_sender_email or "").strip()
    if "@" in sender:
        _r("PASS", "LEADBOOST_INTEGRATION_SENDER_EMAIL", "set (deployment-wide sender persona; known C15 multi-tenant limitation)")
    else:
        _r("WARN", "LEADBOOST_INTEGRATION_SENDER_EMAIL", "empty/invalid: generated-outreach requests return 503")


def check_operator_key(settings) -> None:
    """SYSTEM_OPERATOR_API_KEY (PR3): presence and distinctness only; the value is never printed."""
    from mailer_agent.api import deps

    key = settings.system_operator_api_key.get_secret_value().strip()
    if not key:
        _r("INFO", "SYSTEM_OPERATOR_API_KEY", "not set: /system/config, /system/metrics/llm and /system/integration/validate answer 503 (fail closed). Set it only if an operator needs those process-wide routes")
        return
    if deps._matches_tenant_key(key):
        _r("FAIL", "SYSTEM_OPERATOR_API_KEY", "equals a tenant API key (API_KEY / ORG_KEY_MAP / ORG_KEYS_*): the API REFUSES TO START. Use a distinct value")
        return
    _r("PASS", "SYSTEM_OPERATOR_API_KEY", "set and distinct from every tenant key (value not shown); presented as the X-System-Operator-Key header")
    if len(key) < MIN_KEY_LEN:
        _r("WARN", "SYSTEM_OPERATOR_API_KEY", f"shorter than {MIN_KEY_LEN} characters")


def check_worker() -> None:
    _r("INFO", "scheduler", "worker.py always starts the scheduler: run exactly ONE worker instance")
    if os.environ.get("ORG_KEY_MAP") or os.environ.get("API_KEY"):
        _r("INFO", "ORG_KEY_MAP/API_KEY", "set on the worker but unused there (consumed by the API only)")
    if os.environ.get("SYSTEM_OPERATOR_API_KEY"):
        _r("WARN", "SYSTEM_OPERATOR_API_KEY", "set on the worker but unused there (API only): remove it from the worker's environment")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--role", choices=["api", "worker"], required=True)
    args = ap.parse_args(argv)
    print(f"Mailer configuration preflight (role={args.role}) -- no secret values are printed")
    check_database()
    # Importing settings never connects; DATABASE_URL validity was checked above.
    from mailer_agent.config import get_settings

    settings = get_settings()
    check_encryption_key()
    check_flags(settings)
    if args.role == "api":
        check_api(settings)
    else:
        check_worker()
    fails = sum(1 for lvl, *_ in _results if lvl == "FAIL")
    warns = sum(1 for lvl, *_ in _results if lvl == "WARN")
    print(f"\nresult: {fails} FAIL, {warns} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
