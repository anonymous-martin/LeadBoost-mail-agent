"""
Safety gate for PostgreSQL tests that WRITE (additive DDL and inserts/deletes of their own rows).

``assess_pg_target`` is a pure function: it parses the URL and inspects environment values, and never
connects. A test module calls it before opening any connection, and refuses on anything but "run".

Verdicts:
  run          every condition below holds.
  unavailable  nothing is configured (no URL, or no explicit opt-in). A skip locally; a hard failure when
               the CI job sets its REQUIRE_* variable, so a missing database can never look like a pass.
  refuse       something is configured but is not the intended local/CI test database. Always a hard
               failure, whatever REQUIRE_* says, and always before any connection is made.

Conditions for "run":
  * MAILER_PG_TEST_OPT_IN=1 (an explicit statement that this target may be written to);
  * MAILER_PG_TEST_EXPECTED_DB is set, contains "test", and equals the URL's database name exactly;
  * the URL is a PostgreSQL URL whose host is loopback (localhost, 127.0.0.1, ::1) or absent (local
    socket) and carries no ?host= / ?hostaddr= / ?service= / ?dbname= / ?options= override.
Reasons never include the URL or any credential.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
FORBIDDEN_QUERY_KEYS = frozenset({"host", "hostaddr", "service", "dbname", "options"})


@dataclass(frozen=True)
class PgTargetDecision:
    verdict: str  # "run" | "unavailable" | "refuse"
    reason: str


def assess_pg_target(url: str | None, *, opt_in: str | None, expected_db: str | None) -> PgTargetDecision:
    if not url:
        return PgTargetDecision("unavailable", "POSTGRES_TEST_URL is not set")
    if opt_in != "1":
        return PgTargetDecision("unavailable", "MAILER_PG_TEST_OPT_IN=1 is not set (explicit opt-in required)")
    if not expected_db:
        return PgTargetDecision("refuse", "MAILER_PG_TEST_EXPECTED_DB is not set")
    if "test" not in expected_db.lower():
        return PgTargetDecision("refuse", "MAILER_PG_TEST_EXPECTED_DB does not contain 'test'")
    try:
        parsed = make_url(url)
    except ArgumentError:
        return PgTargetDecision("refuse", "POSTGRES_TEST_URL is not a valid database URL")
    if parsed.get_backend_name() != "postgresql":
        return PgTargetDecision("refuse", "POSTGRES_TEST_URL is not a PostgreSQL URL")
    if parsed.database != expected_db:
        return PgTargetDecision(
            "refuse", f"database name {parsed.database!r} is not the expected {expected_db!r}"
        )
    if parsed.host not in (None, "") and parsed.host not in LOOPBACK_HOSTS:
        return PgTargetDecision("refuse", "host is not loopback (localhost, 127.0.0.1, ::1)")
    overridden = sorted(FORBIDDEN_QUERY_KEYS & set(parsed.query))
    if overridden:
        return PgTargetDecision("refuse", f"URL query overrides the connection target: {', '.join(overridden)}")
    return PgTargetDecision("run", "ok")
