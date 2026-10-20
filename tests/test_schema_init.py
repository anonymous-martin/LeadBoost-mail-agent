"""
PR 3 -- concurrent schema initialization.

Defect: ``init_db()`` was a bare ``Base.metadata.create_all``. That call is check-then-create, so on
PostgreSQL several processes starting together against an empty database (two Gunicorn workers + the
worker process, per the documented topology) both saw "no table" and both issued CREATE TABLE; one
failed with ``UniqueViolation ... pg_type_typname_nsp_index`` (the duplicate ``campaigns_id_seq``-style
error seen once on hosted infrastructure) or ``DuplicateTable``.

Fix under test: ``init_db()`` serializes on a transaction-scoped PostgreSQL advisory lock; services can
opt into ``SCHEMA_INIT_MODE=verify`` (read-only, zero DDL) so the guarded migration wrapper is the only
thing that creates or changes a deployed schema.

Two groups:
* SQLite / pure tests -- always run (ordinary deterministic suite).
* PostgreSQL tests -- REAL separate OS processes released at the same instant against a database this
  module creates and drops itself (``mailer_migrate_test_<random>`` on the server named by
  POSTGRES_TEST_URL). With REQUIRE_SCHEMA_INIT_PG=1 (set by the CI job) a missing PostgreSQL is a hard
  failure, never a skip. The concurrency test deliberately calls ``init_db`` in the children so the very
  same test fails on the pre-fix code.
"""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from pydantic import ValidationError

from mailer_agent import db as mailer_db
from mailer_agent.config import Settings, get_settings
from mailer_agent.models import Base

ROOT = Path(__file__).resolve().parent.parent
_REQUIRED = os.environ.get("REQUIRE_SCHEMA_INIT_PG") == "1"
POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
EXPECTED_TABLES = set(Base.metadata.tables)
DDL_OR_WRITE = ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "GRANT")


# ===========================================================================
# SQLite / pure tests (always run)
# ===========================================================================
@pytest.fixture()
def sqlite_engine(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'schema.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(mailer_db, "engine", eng)
    yield eng
    eng.dispose()


def _capture_statements(eng):
    seen: list[str] = []

    @event.listens_for(eng, "before_cursor_execute")
    def _grab(_c, _cur, statement, *_a):
        seen.append(statement.strip().split(None, 1)[0].upper() if statement.strip() else "")

    return seen


def test_schema_problems_lists_every_table_on_an_empty_database(sqlite_engine):
    problems = mailer_db.schema_problems()
    assert {p.removeprefix("missing table ") for p in problems} == EXPECTED_TABLES
    assert all(p.startswith("missing table ") for p in problems)


def test_schema_problems_is_empty_once_init_db_has_run_and_init_db_is_idempotent(sqlite_engine):
    mailer_db.init_db()
    mailer_db.init_db()
    assert mailer_db.schema_problems() == []
    assert EXPECTED_TABLES <= set(inspect(sqlite_engine).get_table_names())


def test_a_missing_column_is_named(sqlite_engine):
    mailer_db.init_db()
    with sqlite_engine.begin() as c:
        c.execute(text("ALTER TABLE messages DROP COLUMN references_header"))
    assert mailer_db.schema_problems() == ["missing column messages.references_header"]


def test_verify_refuses_and_names_what_is_missing_without_creating_anything(sqlite_engine):
    with pytest.raises(mailer_db.SchemaNotReadyError) as exc:
        mailer_db.verify_schema()
    msg = str(exc.value)
    assert "SCHEMA_INIT_MODE=verify" in msg and "No DDL was attempted" in msg
    assert "scripts/migrate_release.sh" in msg
    assert all(f"missing table {t}" in msg for t in EXPECTED_TABLES)
    assert set(inspect(sqlite_engine).get_table_names()) == set()


def test_a_long_problem_list_is_truncated_in_the_message_but_counted_in_full(monkeypatch):
    monkeypatch.setattr(mailer_db, "schema_problems", lambda bind=None: [f"missing column t.c{i}" for i in range(13)])
    with pytest.raises(mailer_db.SchemaNotReadyError) as exc:
        mailer_db.verify_schema()
    msg = str(exc.value)
    assert "13 problem(s)" in msg and "missing column t.c9" in msg
    assert "missing column t.c10" not in msg and "and 3 more" in msg


def test_verify_issues_no_ddl_and_no_writes(sqlite_engine):
    mailer_db.init_db()
    statements = _capture_statements(sqlite_engine)
    mailer_db.verify_schema()  # passes on a complete schema
    with pytest.raises(mailer_db.SchemaNotReadyError):
        with sqlite_engine.begin() as c:
            c.execute(text("ALTER TABLE messages DROP COLUMN references_header"))
        statements.clear()
        mailer_db.verify_schema()
    assert not [s for s in statements if s.startswith(DDL_OR_WRITE)], statements


def test_prepare_schema_create_mode_creates_and_verify_mode_does_not(sqlite_engine, monkeypatch):
    monkeypatch.setattr(mailer_db.settings, "schema_init_mode", "verify")
    with pytest.raises(mailer_db.SchemaNotReadyError):
        mailer_db.prepare_schema()
    assert set(inspect(sqlite_engine).get_table_names()) == set()

    monkeypatch.setattr(mailer_db.settings, "schema_init_mode", "create")
    mailer_db.prepare_schema()
    assert EXPECTED_TABLES <= set(inspect(sqlite_engine).get_table_names())

    monkeypatch.setattr(mailer_db.settings, "schema_init_mode", "verify")
    mailer_db.prepare_schema()  # now complete: starts


def test_default_mode_is_create_so_an_unchanged_deployment_starts_exactly_as_before():
    assert Settings(_env_file=None).schema_init_mode == "create"
    assert get_settings().schema_init_mode in {"create", "verify"}


def test_mode_is_read_from_the_environment_and_a_bad_value_fails_closed(monkeypatch):
    monkeypatch.setenv("SCHEMA_INIT_MODE", "verify")
    assert Settings(_env_file=None).schema_init_mode == "verify"
    monkeypatch.setenv("SCHEMA_INIT_MODE", "skip")  # a typo must never silently mean "do nothing"
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_sqlite_path_takes_no_advisory_lock(sqlite_engine):
    statements = _capture_statements(sqlite_engine)
    mailer_db.init_db()
    assert not any("advisory" in s.lower() for s in statements)


def test_lock_key_is_a_stable_signed_64_bit_integer():
    assert -(2**63) <= mailer_db.SCHEMA_INIT_LOCK_KEY < 2**63
    # Pinned on purpose: processes of different releases must contend on the SAME lock during a rolling
    # deploy, so this value must never change by accident.
    assert mailer_db.SCHEMA_INIT_LOCK_KEY == -4164649664000867161
    assert mailer_db.SCHEMA_INIT_LOCK_TIMEOUT_MS < 60_000  # below gunicorn's --timeout 60


# --- wiring: the API lifespan and the worker both go through prepare_schema -----------------------
def test_lifespan_in_verify_mode_refuses_to_start_on_an_empty_database(sqlite_engine, monkeypatch):
    from fastapi.testclient import TestClient

    from mailer_agent.api import main as api_main

    monkeypatch.setattr(api_main, "validate_auth_configuration", lambda: None)
    monkeypatch.setattr(mailer_db.settings, "schema_init_mode", "verify")
    monkeypatch.setattr(get_settings(), "run_scheduler_in_process", False)
    with pytest.raises(mailer_db.SchemaNotReadyError), TestClient(api_main.app):
        pass
    assert set(inspect(sqlite_engine).get_table_names()) == set()


def test_lifespan_in_create_mode_creates_the_schema_and_serves(sqlite_engine, monkeypatch):
    from fastapi.testclient import TestClient

    from mailer_agent.api import main as api_main

    monkeypatch.setattr(api_main, "validate_auth_configuration", lambda: None)
    monkeypatch.setattr(mailer_db.settings, "schema_init_mode", "create")
    monkeypatch.setattr(get_settings(), "run_scheduler_in_process", False)
    with TestClient(api_main.app) as c:
        assert c.get("/health").status_code == 200
    assert EXPECTED_TABLES <= set(inspect(sqlite_engine).get_table_names())


class _StopHere(Exception):
    pass


def test_worker_process_start_goes_through_prepare_schema(monkeypatch):
    """Run worker.py as __main__ with prepare_schema replaced by a sentinel: the worker must call it
    first, before installing signal handlers or starting the scheduler (so verify mode really gates it)."""

    def _stop():
        raise _StopHere()

    monkeypatch.setattr(mailer_db, "prepare_schema", _stop)
    with pytest.raises(_StopHere):
        runpy.run_path(str(ROOT / "worker.py"), run_name="__main__")


# --- scripts/check_config.py reports how this process treats the schema at start -------------------
def test_preflight_reports_schema_init_mode_create_as_a_calm_warning_and_verify_as_a_pass(tmp_path):
    from tests.test_check_config import run

    rc, out = run(tmp=tmp_path)
    assert rc == 0, out
    assert "[WARN] SCHEMA_INIT_MODE: create (default)" in out and "migrate_preflight.py verify" in out

    rc, out = run(tmp=tmp_path, SCHEMA_INIT_MODE="verify")
    assert rc == 0, out
    assert "[PASS] SCHEMA_INIT_MODE: verify" in out


def test_preflight_does_not_silently_accept_an_invalid_schema_init_mode(tmp_path):
    from tests.test_check_config import run

    # A typo must stop the preflight (settings fail to load) rather than quietly mean "do nothing".
    rc, out = run(tmp=tmp_path, SCHEMA_INIT_MODE="skip")
    assert rc != 0, out
    assert "[PASS] SCHEMA_INIT_MODE" not in out and "[WARN] SCHEMA_INIT_MODE" not in out


# ===========================================================================
# PostgreSQL tests: real, separate, simultaneous processes
# ===========================================================================
def _admin_engine():
    if not POSTGRES_TEST_URL:
        msg = "POSTGRES_TEST_URL is not set"
    else:
        try:
            eng = create_engine(POSTGRES_TEST_URL, isolation_level="AUTOCOMMIT")
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            return eng
        except Exception as exc:  # noqa: BLE001
            msg = f"PostgreSQL named by POSTGRES_TEST_URL is unreachable ({type(exc).__name__})"
    if _REQUIRED:
        pytest.fail(f"REQUIRE_SCHEMA_INIT_PG=1 but {msg}")
    pytest.skip(msg)


class _FreshDb:
    """A brand-new, empty database this test created and will drop."""

    def __init__(self, admin):
        self.name = f"mailer_migrate_test_{uuid.uuid4().hex[:12]}"
        self._admin = admin
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{self.name}"'))
        self.sa_url = make_url(POSTGRES_TEST_URL).set(database=self.name)
        self.url = self.sa_url.render_as_string(hide_password=False)  # env only, never printed
        self.engine = create_engine(self.sa_url)

    def tables(self) -> set[str]:
        with self.engine.connect() as c:
            return set(inspect(c).get_table_names())

    def shape(self) -> dict:
        """Tables, columns, indexes and unique constraints: a structural fingerprint."""
        out = {}
        with self.engine.connect() as c:
            insp = inspect(c)
            for t in sorted(insp.get_table_names()):
                out[t] = (
                    sorted(col["name"] for col in insp.get_columns(t)),
                    sorted(i["name"] for i in insp.get_indexes(t)),
                    sorted(u["name"] for u in insp.get_unique_constraints(t)),
                )
        return out

    def drop(self):
        self.engine.dispose()
        with self._admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))


@pytest.fixture()
def fresh_db():
    admin = _admin_engine()
    db = _FreshDb(admin)
    try:
        yield db
    finally:
        db.drop()
        admin.dispose()


_CHILD = """
import sys
from mailer_agent.db import {fn}
print("READY", flush=True)
sys.stdin.readline()
try:
    {fn}()
    print("OK", flush=True)
except Exception as e:
    print("FAIL", type(e).__name__, str(e).splitlines()[0][:240], flush=True)
    sys.exit(1)
"""


def _start_together(db: _FreshDb, nprocs: int = 6, fn: str = "init_db", **env) -> list[str]:
    """Launch nprocs real processes, wait until every one has IMPORTED the app, then release them all at
    the same instant so they hit CREATE TABLE together. Returns each process's final output line."""
    full = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "SCHEMA_INIT_MODE")}
    full["DATABASE_URL"] = db.url
    full.update(env)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD.format(fn=fn)],
            cwd=ROOT,
            env=full,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for _ in range(nprocs)
    ]
    try:
        for p in procs:
            assert p.stdout.readline().strip() == "READY"
        for p in procs:
            p.stdin.write("go\n")
            p.stdin.flush()
        results = []
        for p in procs:
            out, _ = p.communicate(timeout=120)
            lines = out.strip().splitlines()
            results.append(lines[-1] if lines else "NO OUTPUT")
        return results
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()


@pytest.mark.integration
@pytest.mark.postgres
def test_concurrent_init_db_on_an_empty_database_every_process_succeeds():
    """THE regression test. Before the fix, several of these processes fail with UniqueViolation
    (pg_type_typname_nsp_index) or DuplicateTable on essentially every run. Three rounds, each against
    a brand-new empty database."""
    admin = _admin_engine()
    try:
        for round_no in range(3):
            db = _FreshDb(admin)
            try:
                results = _start_together(db, nprocs=6, fn="init_db")
                assert results == ["OK"] * 6, (round_no, results)
                assert EXPECTED_TABLES <= db.tables()
            finally:
                db.drop()
    finally:
        admin.dispose()


@pytest.mark.integration
@pytest.mark.postgres
def test_concurrent_prepare_schema_in_default_create_mode_every_process_succeeds(fresh_db):
    results = _start_together(fresh_db, nprocs=6, fn="prepare_schema", SCHEMA_INIT_MODE="create")
    assert results == ["OK"] * 6, results
    assert EXPECTED_TABLES <= fresh_db.tables()
    with fresh_db.engine.connect() as c:
        assert mailer_db.schema_problems(c) == []


@pytest.mark.integration
@pytest.mark.postgres
def test_start_against_an_already_migrated_database_changes_nothing(fresh_db):
    """Run the REAL guarded wrapper (all 11 steps) on the disposable database, then start several
    processes together in both modes: nothing may be created, altered or dropped."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to run scripts/migrate_release.sh")
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "MIGRATE_CONFIRM_BACKUP")}
    env.update(
        PYTHON=sys.executable,
        DATABASE_URL=fresh_db.sa_url.set(drivername="postgresql").render_as_string(hide_password=False),
        MIGRATE_DISPOSABLE_TEST_DB="yes",
    )
    run = subprocess.run(
        [bash, str(ROOT / "scripts" / "migrate_release.sh")], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300
    )
    assert run.returncode == 0, run.stdout[-800:] + run.stderr[-800:]
    migrated = fresh_db.shape()
    assert "external_dispatches" in migrated

    assert _start_together(fresh_db, nprocs=6, fn="prepare_schema", SCHEMA_INIT_MODE="create") == ["OK"] * 6
    assert fresh_db.shape() == migrated, "create-mode start altered a migrated schema"

    assert _start_together(fresh_db, nprocs=6, fn="prepare_schema", SCHEMA_INIT_MODE="verify") == ["OK"] * 6
    assert fresh_db.shape() == migrated, "verify-mode start altered a migrated schema"


@pytest.mark.integration
@pytest.mark.postgres
def test_verify_mode_on_an_empty_database_refuses_everywhere_and_creates_nothing(fresh_db):
    results = _start_together(fresh_db, nprocs=4, fn="prepare_schema", SCHEMA_INIT_MODE="verify")
    assert all(r.startswith("FAIL SchemaNotReadyError") for r in results), results
    assert fresh_db.tables() == set()
    assert all("password" not in r.lower() and fresh_db.url not in r for r in results)


@pytest.mark.integration
@pytest.mark.postgres
def test_verify_mode_issues_no_ddl_against_postgres(fresh_db, monkeypatch):
    monkeypatch.setattr(mailer_db, "engine", fresh_db.engine)
    mailer_db.init_db()
    statements = _capture_statements(fresh_db.engine)
    mailer_db.verify_schema()
    assert statements, "verify should have inspected the catalog"
    assert not [s for s in statements if s.startswith(DDL_OR_WRITE)], statements


def _advisory_locks(db: _FreshDb) -> int:
    with db.engine.connect() as c:
        return c.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND database = (SELECT oid FROM pg_database WHERE datname = current_database())")
        ).scalar()


@pytest.mark.integration
@pytest.mark.postgres
def test_the_lock_is_released_when_init_db_returns(fresh_db, monkeypatch):
    monkeypatch.setattr(mailer_db, "engine", fresh_db.engine)
    mailer_db.init_db()
    assert _advisory_locks(fresh_db) == 0
    mailer_db.init_db()  # and a second call neither blocks nor fails
    assert _advisory_locks(fresh_db) == 0


@pytest.mark.integration
@pytest.mark.postgres
def test_a_stuck_lock_holder_yields_a_clear_timeout_not_a_hang_and_nothing_is_created(fresh_db, monkeypatch):
    monkeypatch.setattr(mailer_db, "engine", fresh_db.engine)
    monkeypatch.setattr(mailer_db, "SCHEMA_INIT_LOCK_TIMEOUT_MS", 400)
    holder = fresh_db.engine.connect()
    try:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": mailer_db.SCHEMA_INIT_LOCK_KEY})
        started = time.monotonic()
        with pytest.raises(mailer_db.SchemaInitLockTimeout, match="schema-initialization lock"):
            mailer_db.init_db()
        assert time.monotonic() - started < 10
        assert fresh_db.tables() == set()
    finally:
        holder.execute(text("SELECT pg_advisory_unlock_all()"))
        holder.close()
    mailer_db.init_db()  # recovers as soon as the holder lets go
    assert EXPECTED_TABLES <= fresh_db.tables()


@pytest.mark.integration
@pytest.mark.postgres
def test_a_process_killed_while_holding_the_lock_cannot_block_later_starts(fresh_db, monkeypatch):
    """The lock is transaction-scoped: if the holder dies (here: its backend is terminated), PostgreSQL
    rolls its transaction back and releases the lock -- there is nothing to clean up."""
    monkeypatch.setattr(mailer_db, "engine", fresh_db.engine)
    victim = fresh_db.engine.connect()
    victim.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": mailer_db.SCHEMA_INIT_LOCK_KEY})
    pid = victim.execute(text("SELECT pg_backend_pid()")).scalar()

    def _kill_soon():
        time.sleep(0.5)
        with fresh_db.engine.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})

    threading.Thread(target=_kill_soon, daemon=True).start()
    started = time.monotonic()
    mailer_db.init_db()  # waits for the lock, which frees itself when the victim's backend dies
    assert time.monotonic() - started < 30
    assert EXPECTED_TABLES <= fresh_db.tables()
    try:
        victim.close()
    except Exception:  # noqa: BLE001 - its backend was terminated on purpose
        pass
