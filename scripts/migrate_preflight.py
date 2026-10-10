#!/usr/bin/env python3
"""Guard, status and bootstrap helper for ``scripts/migrate_release.sh``.

Why this exists
---------------
``mailer_agent.config.Settings.database_url`` silently defaults to
``sqlite:///./mailer_agent.db``, and every migration script reads that
setting. Run by hand without ``DATABASE_URL`` they "succeed" against a
throwaway local SQLite file. This helper refuses to let that happen and
refuses every other unsafe target *before* any migration runs.

Subcommands (all read-only except ``init``)
-------------------------------------------
``gate``          validate the target, prove it is reachable, run the migration
                  007 queue pre-check. ``--apply`` additionally requires the
                  backup acknowledgement (see below).
``status``        read-only report of which migration steps the live schema
                  already satisfies. Heuristic markers, NOT proof.
``init``          ``init_db()`` (``Base.metadata.create_all``) -- the very call
                  the API and worker make on every start. Creates missing
                  tables only; never alters or drops anything.

Guards (exit 2 = refused, 3 = unreachable, 4 = dispatch pre-check)
-------------------------------------------------------------------
* ``DATABASE_URL`` must be set in the *process environment*. A ``.env`` file or
  the built-in SQLite default is never accepted.
* The URL must be PostgreSQL (``postgres://``, ``postgresql://`` or
  ``postgresql+<driver>://``) with an explicit host and database name.
* ``--apply`` against anything that is not a declared disposable test database
  needs ``MIGRATE_CONFIRM_BACKUP=I_HAVE_A_VERIFIED_BACKUP_OF_<database name>``.
* ``MIGRATE_DISPOSABLE_TEST_DB=yes`` waives that acknowledgement ONLY for a
  loopback host AND a database whose name starts with ``mailer_migrate_test_``.
  If it is set and either condition fails, the run is refused (it does not
  fall back to anything).
* Migration 007: if ``external_dispatches.mailbox_id`` does not exist yet and
  non-terminal dispatches do, the run is refused (see migrations/README.md).

Secrets: only ``host:port/database`` is ever printed -- never the user name,
password, query string or the raw URL.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.engine import make_url

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNREACHABLE = 3
EXIT_DISPATCH_BLOCK = 4

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
DISPOSABLE_DB_PREFIX = "mailer_migrate_test_"
BACKUP_ACK_PREFIX = "I_HAVE_A_VERIFIED_BACKUP_OF_"
_PG_SCHEME = re.compile(r"^(postgres|postgresql)(\+[a-z0-9_]+)?://", re.IGNORECASE)

# Dispatch states that are not terminal (see ExternalDispatchState in models.py).
_NON_TERMINAL = ("queued", "generating", "sending")


def _say(msg: str) -> None:
    print(f"[preflight] {msg}", flush=True)


class Refused(Exception):
    def __init__(self, message: str, code: int = EXIT_REFUSED):
        super().__init__(message)
        self.code = code


class Target:
    def __init__(self, host: str, port: int | None, database: str):
        self.host = host
        self.port = port
        self.database = database

    @property
    def redacted(self) -> str:
        return f"{self.host}:{self.port or 5432}/{self.database}"

    @property
    def is_declared_disposable(self) -> bool:
        return self.host.lower() in LOOPBACK_HOSTS and self.database.startswith(DISPOSABLE_DB_PREFIX)


def validate_environment(*, apply: bool) -> Target:
    """Everything that can be decided from the environment alone (no network,
    no import of mailer_agent.db, which would build an engine)."""
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise Refused(
            "DATABASE_URL is not set in the process environment. Migrations never use a .env "
            "file or the built-in SQLite default (mailer_agent.db). Export the PostgreSQL URL "
            "of the database you intend to migrate."
        )
    if not _PG_SCHEME.match(raw):
        # Deliberately says nothing about what the value looked like.
        raise Refused("DATABASE_URL is not a PostgreSQL URL (SQLite and other databases are refused).")

    parse_form = re.sub(r"^postgres://", "postgresql://", raw, count=1, flags=re.IGNORECASE)
    try:
        url = make_url(parse_form)
    except Exception as exc:  # noqa: BLE001
        raise Refused(f"DATABASE_URL could not be parsed ({type(exc).__name__}).") from None
    if not url.host:
        raise Refused("DATABASE_URL has no explicit host (unix-socket/implicit targets are refused).")
    if not url.database:
        raise Refused("DATABASE_URL has no database name.")
    target = Target(url.host, url.port, url.database)

    disposable_requested = os.environ.get("MIGRATE_DISPOSABLE_TEST_DB", "").strip().lower() == "yes"
    if disposable_requested and not target.is_declared_disposable:
        raise Refused(
            "MIGRATE_DISPOSABLE_TEST_DB=yes is only honoured for a loopback host and a database "
            f"named {DISPOSABLE_DB_PREFIX}*; target {target.redacted} does not qualify. Refusing."
        )

    if apply and not disposable_requested:
        expected = BACKUP_ACK_PREFIX + target.database
        if os.environ.get("MIGRATE_CONFIRM_BACKUP", "") != expected:
            raise Refused(
                f"Refusing to apply migrations to {target.redacted} without an explicit backup "
                f"acknowledgement. Take and verify a backup first, then set "
                f"MIGRATE_CONFIRM_BACKUP={expected}"
            )
    return target


def _load_db(target: Target):
    """Import the application's own engine (so the scripts and this helper use
    the same connection path) and make sure it points at the validated target."""
    from mailer_agent import db as mailer_db
    from mailer_agent.config import get_settings

    effective = make_url(mailer_db.engine.url.render_as_string(hide_password=False))
    wanted_port = target.port or 5432
    if (
        (effective.host or "").lower() != target.host.lower()
        or (effective.port or 5432) != wanted_port
        or effective.database != target.database
        or not effective.get_backend_name().startswith("postgresql")
    ):
        raise Refused("The application's effective database settings differ from DATABASE_URL; refusing.")
    # settings object is only used to confirm the value was read from the environment
    get_settings()
    return mailer_db


def _connect_check(mailer_db, target: Target) -> str:
    from sqlalchemy import text

    try:
        with mailer_db.engine.connect() as conn:
            name, version = conn.execute(
                text("SELECT current_database(), current_setting('server_version')")
            ).one()
    except Exception as exc:  # noqa: BLE001 -- type only: driver messages can echo connection details
        raise Refused(
            f"Could not connect to {target.redacted} ({type(exc).__name__}).", EXIT_UNREACHABLE
        ) from None
    if name != target.database:
        raise Refused("Connected database name does not match DATABASE_URL; refusing.")
    return version


def _dispatch_precheck(mailer_db) -> None:
    """Migration 007 / README: dispatches still non-terminal when the
    ``mailbox_id`` column is introduced are failed pre-SMTP as ``no_mailbox``.
    Only relevant when the column does not exist yet."""
    from sqlalchemy import inspect, text

    with mailer_db.engine.connect() as conn:
        insp = inspect(conn)
        if "external_dispatches" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("external_dispatches")}
        states = ",".join(f"'{s}'" for s in _NON_TERMINAL)
        counts = dict(
            conn.execute(
                text(f"SELECT state, count(*) FROM external_dispatches WHERE state IN ({states}) GROUP BY state")
            ).all()
        )
    in_flight = sum(counts.values())
    if "mailbox_id" in cols:
        if in_flight:
            _say(
                f"NOTE: {in_flight} non-terminal dispatch(es) exist ({counts}). Suspend the Mailer worker "
                "and let it drain before applying schema changes (docs/C13_DEPLOYMENT_RUNBOOK.md)."
            )
        return
    if in_flight:
        raise Refused(
            f"external_dispatches.mailbox_id does not exist yet and {in_flight} non-terminal dispatch(es) "
            f"exist ({counts}). Migration 007 would make them fail pre-SMTP (no_mailbox). Suspend the "
            "worker, drain or resolve them, then re-run. Nothing was changed.",
            EXIT_DISPATCH_BLOCK,
        )


def cmd_gate(args: argparse.Namespace) -> int:
    target = validate_environment(apply=args.apply)
    _say(f"target: {target.redacted}")
    mailer_db = _load_db(target)
    version = _connect_check(mailer_db, target)
    _say(f"connected: PostgreSQL {version}; database name confirmed")
    _dispatch_precheck(mailer_db)
    _say("gate passed" + (" (apply authorised)" if args.apply else " (read-only check)"))
    return EXIT_OK


def cmd_init(_args: argparse.Namespace) -> int:
    target = validate_environment(apply=False)
    mailer_db = _load_db(target)
    mailer_db.init_db()
    _say("init_db(): missing tables created if any; nothing altered or dropped")
    return EXIT_OK


def _markers(conn):
    from sqlalchemy import inspect

    insp = inspect(conn)
    tables = set(insp.get_table_names())

    def col(table, name):
        return table in tables and any(c["name"] == name for c in insp.get_columns(table))

    def idx(table, name):
        return table in tables and (
            any(i["name"] == name for i in insp.get_indexes(table))
            or any(u["name"] == name for u in insp.get_unique_constraints(table))
        )

    def nullable(table, name):
        if table not in tables:
            return False
        for c in insp.get_columns(table):
            if c["name"] == name:
                return bool(c["nullable"])
        return False

    return [
        ("001  messages.send_attempt_count column", col("messages", "send_attempt_count")),
        ("002  uq_messages_inbound_message_id", idx("messages", "uq_messages_inbound_message_id")),
        ("002w idx_contacts_work_queue", idx("contacts", "idx_contacts_work_queue")),
        ("mdb  contacts.buying_stage column", col("contacts", "buying_stage")),
        (
            "003  message_id_header uniqueness (003 constraint, or its 009 replacement)",
            idx("messages", "uq_messages_message_id_header")
            or (
                idx("messages", "uq_messages_message_id_no_mailbox")
                and idx("messages", "uq_messages_mailbox_message_id")
            ),
        ),
        ("004  uq_campaigns_org_integration_source", idx("campaigns", "uq_campaigns_org_integration_source")),
        ("004  external_dispatches table", "external_dispatches" in tables),
        ("005  external_dispatches.grounding_context", col("external_dispatches", "grounding_context")),
        ("006  mailboxes table", "mailboxes" in tables),
        ("007  external_dispatches.mailbox_id", col("external_dispatches", "mailbox_id")),
        ("008  external_dispatches.message_id nullable", nullable("external_dispatches", "message_id")),
        ("009  messages.mailbox_id column", col("messages", "mailbox_id")),
        (
            "009  partial unique indexes (replace the global 003 constraint)",
            idx("messages", "uq_messages_message_id_no_mailbox") and idx("messages", "uq_messages_mailbox_message_id"),
        ),
    ]


def cmd_status(_args: argparse.Namespace) -> int:
    target = validate_environment(apply=False)
    mailer_db = _load_db(target)
    _connect_check(mailer_db, target)
    _say(f"schema markers for {target.redacted} (read-only; heuristic, not proof of a clean migration history):")
    with mailer_db.engine.connect() as conn:
        rows = _markers(conn)
    for label, ok in rows:
        print(f"  [{'present' if ok else 'ABSENT '}] {label}", flush=True)
    missing = [label for label, ok in rows if not ok]
    _say("all markers present" if not missing else f"{len(missing)} marker(s) absent -- migrations are still needed")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--apply", action="store_true")
    gate.set_defaults(fn=cmd_gate)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except Refused as r:
        _say(f"REFUSED: {r}")
        return r.code


if __name__ == "__main__":
    sys.exit(main())
