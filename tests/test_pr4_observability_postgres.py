"""PR4 -- PostgreSQL evidence for the operator observability change.

Runs against a database THIS TEST CREATES AND DROPS (``mailer_migrate_test_pr4_<random>`` on the loopback server
named by POSTGRES_TEST_URL); it never touches any other database. With REQUIRE_PR4_OBSERVABILITY_PG=1
(set by the CI job) a missing or unreachable PostgreSQL is a hard FAILURE, never a skip.

Covers: migration 010 on a fresh and on an already-migrated (pre-PR4-shaped) database, idempotency,
read-only verify (no DDL), downgrade touching no business data, timestamp semantics of the aggregates
on real timestamptz/timestamp columns, agreement with the real lease-recovery sweep, and the query
plans that justify "no new dispatch index" and "one partial index on inbound messages".
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from mailer_agent.followup.work_claiming import recover_expired_external_dispatches
from mailer_agent.models import (
    Base,
    Campaign,
    Contact,
    ExternalDispatch,
    Message,
)
from mailer_agent.operator_metrics import (
    dispatch_metrics,
    inbound_metrics,
    worker_metrics,
)
from mailer_agent.worker_status import KIND_EXTERNAL_DISPATCH, WorkerStatusRecorder
from tests.dispatch_support import seed_dispatch

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "migrations" / "010_worker_runtime_status.py"
PREFLIGHT = ROOT / "scripts" / "migrate_preflight.py"
POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
_REQUIRED = os.environ.get("REQUIRE_PR4_OBSERVABILITY_PG") == "1"

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

IDX = "ix_messages_inbound_created_at"
NEW_TABLES = {"worker_runtime_status", "worker_cycle_status"}


def _admin():
    if not POSTGRES_TEST_URL:
        msg = "POSTGRES_TEST_URL is not set"
    else:
        eng = create_engine(POSTGRES_TEST_URL, isolation_level="AUTOCOMMIT")
        try:
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            return eng
        except Exception as exc:  # noqa: BLE001
            msg = f"PostgreSQL named by POSTGRES_TEST_URL is unreachable ({type(exc).__name__})"
    if _REQUIRED:
        pytest.fail(f"REQUIRE_PR4_OBSERVABILITY_PG=1 but {msg}")
    pytest.skip(msg)


class Db:
    def __init__(self, admin):
        self.name = f"mailer_migrate_test_pr4_{uuid.uuid4().hex[:10]}"  # the name the preflight guard accepts as disposable
        self._admin = admin
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{self.name}"'))
        self.url = make_url(POSTGRES_TEST_URL).set(database=self.name)
        self.dsn = self.url.set(drivername="postgresql").render_as_string(hide_password=False)
        self.engine = create_engine(self.url)
        self.sm = sessionmaker(bind=self.engine)

    def tables(self):
        with self.engine.connect() as c:
            return set(inspect(c).get_table_names())

    def index_names(self, table="messages"):
        with self.engine.connect() as c:
            return {i["name"] for i in inspect(c).get_indexes(table)}

    def sql(self, stmt):
        with self.engine.begin() as c:
            return c.execute(text(stmt))

    def count(self, table):
        with self.engine.connect() as c:
            return c.execute(text(f"SELECT count(*) FROM {table}")).scalar()

    def run_migration(self, *args, extra_env=None):
        env = {**os.environ, "DATABASE_URL": self.dsn, "PYTHONPATH": str(ROOT), **(extra_env or {})}
        return subprocess.run([sys.executable, str(MIGRATION), *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120, check=False)

    def preflight_status(self):
        env = {**os.environ, "DATABASE_URL": self.dsn, "MIGRATE_DISPOSABLE_TEST_DB": "yes", "PYTHONPATH": str(ROOT)}
        return subprocess.run([sys.executable, str(PREFLIGHT), "status"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120, check=False)

    def preflight_verify(self):
        env = {**os.environ, "DATABASE_URL": self.dsn, "MIGRATE_DISPOSABLE_TEST_DB": "yes", "PYTHONPATH": str(ROOT)}
        return subprocess.run([sys.executable, str(PREFLIGHT), "verify"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120, check=False)

    def drop(self):
        self.engine.dispose()
        with self._admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))


@pytest.fixture()
def pg():
    admin = _admin()
    db = Db(admin)
    try:
        yield db
    finally:
        db.drop()
        admin.dispose()


@pytest.fixture()
def pre_pr4(pg):
    """The schema as it was before PR4: every table from create_all MINUS the new tables and the new index,
    holding some business data so a later downgrade can prove it touches none of it."""
    Base.metadata.create_all(bind=pg.engine)
    with pg.sm() as s:
        seed_dispatch(s, state="sent", mailbox=False)
        s.add(Message(contact_id=1, direction="inbound", body="b"))
        s.commit()
    pg.sql(f"DROP INDEX {IDX}")
    pg.sql("DROP TABLE worker_cycle_status")
    pg.sql("DROP TABLE worker_runtime_status")
    assert not (NEW_TABLES & pg.tables()) and IDX not in pg.index_names()
    return pg


def _out(p):
    return p.stdout + p.stderr


# =============================================================== migration 010
def test_migration_on_a_pre_pr4_database_adds_tables_and_index_then_is_idempotent(pre_pr4):
    pg = pre_pr4
    first = pg.run_migration()
    assert first.returncode == 0, _out(first)
    assert NEW_TABLES <= pg.tables() and IDX in pg.index_names()
    assert "Migration 010 completed" in _out(first)
    second = pg.run_migration()  # already migrated: a no-op
    assert second.returncode == 0, _out(second)
    assert "already exists -- skipping" in _out(second) and "Created" not in _out(second)
    assert NEW_TABLES <= pg.tables() and IDX in pg.index_names()


def test_the_index_is_partial_on_inbound_and_the_tables_have_their_constraints(pre_pr4):
    pg = pre_pr4
    assert pg.run_migration().returncode == 0
    with pg.engine.connect() as c:
        definition = c.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": IDX}).scalar()
        assert re.search(r"\(created_at\) WHERE \(\(direction\)::text = 'inbound'::text\)", definition), definition
        uniq = {tuple(i["column_names"]) for i in inspect(c).get_indexes("worker_runtime_status") if i["unique"]}
        uniq |= {tuple(u["column_names"]) for u in inspect(c).get_unique_constraints("worker_runtime_status")}
        assert ("instance_id",) in uniq
        cyc = {tuple(u["column_names"]) for u in inspect(c).get_unique_constraints("worker_cycle_status")}
        cyc |= {tuple(i["column_names"]) for i in inspect(c).get_indexes("worker_cycle_status") if i["unique"]}
        assert ("instance_id", "kind") in cyc


def test_migration_on_a_fresh_create_all_database_is_a_safe_noop(pg):
    Base.metadata.create_all(bind=pg.engine)  # what init_db() does first in the release sequence
    assert NEW_TABLES <= pg.tables() and IDX in pg.index_names()
    p = pg.run_migration()
    assert p.returncode == 0, _out(p)
    assert "Created" not in _out(p)


def test_verify_module_function_is_read_only_and_reports_names_only(pre_pr4):
    pg = pre_pr4
    spec = importlib.util.spec_from_file_location("migration_010", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    statements = []

    def grab(conn, cur, stmt, params, ctx, many):
        statements.append(stmt)

    event.listen(pg.engine, "before_cursor_execute", grab)
    try:
        with Session(bind=pg.engine) as s:
            assert mod.verify(s) is False  # pre-PR4 shape: reports the gap
    finally:
        event.remove(pg.engine, "before_cursor_execute", grab)
    assert not [s for s in statements if re.match(r"\s*(create|alter|drop|insert|update|delete|truncate)", s, re.IGNORECASE)], \
        "verify must execute no DDL/DML"
    pg.run_migration()
    with Session(bind=pg.engine) as s:
        assert mod.verify(s) is True


def test_startup_verify_mode_flags_the_new_tables_until_010_is_applied(pre_pr4):
    pg = pre_pr4
    before = pg.preflight_verify()
    assert before.returncode == 5, _out(before)  # a service in SCHEMA_INIT_MODE=verify would refuse to start
    assert "missing table worker_runtime_status" in _out(before)
    assert "missing table worker_cycle_status" in _out(before)
    assert not (NEW_TABLES & pg.tables()), "verify must not create anything"
    assert pg.run_migration().returncode == 0
    after = pg.preflight_verify()
    assert after.returncode == 0, _out(after)


def test_downgrade_removes_only_operator_telemetry_and_no_business_data(pre_pr4):
    pg = pre_pr4
    assert pg.run_migration().returncode == 0
    business = {t: pg.count(t) for t in ("campaigns", "contacts", "messages", "external_dispatches")}
    assert all(v > 0 for v in business.values())
    d = pg.run_migration("--downgrade")
    assert d.returncode == 0, _out(d)
    assert not (NEW_TABLES & pg.tables()) and IDX not in pg.index_names()
    assert {t: pg.count(t) for t in business} == business  # not one business row touched
    assert pg.run_migration().returncode == 0  # and it can be re-applied
    assert NEW_TABLES <= pg.tables() and IDX in pg.index_names()


def test_the_migration_never_alters_existing_business_columns(pre_pr4):
    pg = pre_pr4

    def shape():
        with pg.engine.connect() as c:
            return {t: [(col["name"], str(col["type"]), col["nullable"]) for col in inspect(c).get_columns(t)]
                    for t in ("campaigns", "contacts", "messages", "external_dispatches", "mailboxes")}

    before = shape()
    assert pg.run_migration().returncode == 0
    assert shape() == before


# =============================================================== write-blocking safeguards (review item A)
@contextlib.contextmanager
def _open_writer_on_messages(pg):
    """A genuine uncommitted writer on `messages` (RowExclusiveLock), which conflicts with the index build's SHARE lock."""
    conn = pg.engine.connect()
    tx = conn.begin()
    conn.execute(text("INSERT INTO messages (contact_id, direction, body) VALUES (1, 'inbound', 'blocker')"))
    try:
        yield conn
    finally:
        tx.rollback()
        conn.close()


def test_a_blocked_index_build_fails_fast_and_changes_nothing_then_succeeds_when_the_writer_is_gone(pre_pr4):
    pg = pre_pr4
    with _open_writer_on_messages(pg):
        t0 = time.monotonic()
        p = pg.run_migration(extra_env={"MIGRATION_010_LOCK_TIMEOUT_MS": "500"})
        elapsed = time.monotonic() - t0
        assert p.returncode != 0, _out(p)
        assert elapsed < 30, f"must give up quickly, took {elapsed:.1f}s"
        assert "could not take the lock" in _out(p) and "NOTHING was changed" in _out(p)
        # PostgreSQL DDL is transactional: the new TABLES were rolled back together with the failed index
        assert not (NEW_TABLES & pg.tables()) and IDX not in pg.index_names()
        # and the blocker was never harmed: the writer's own transaction is still alive and unaffected
    ok = pg.run_migration(extra_env={"MIGRATION_010_LOCK_TIMEOUT_MS": "500"})
    assert ok.returncode == 0, _out(ok)
    assert NEW_TABLES <= pg.tables() and IDX in pg.index_names()


def _run_wrapper(pg, extra_env=None):
    env = {**os.environ, "DATABASE_URL": pg.dsn, "MIGRATE_DISPOSABLE_TEST_DB": "yes", "PYTHONPATH": str(ROOT),
           **(extra_env or {})}
    return subprocess.run(["bash", str(ROOT / "scripts" / "migrate_release.sh")], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=300, check=False)


def test_the_real_release_wrapper_fails_fast_under_contention_when_the_lock_timeout_is_set(pg):
    """Existing steps re-run CREATE INDEX IF NOT EXISTS on `messages`, which needs the table's SHARE lock even
    when the index exists: without a timeout the wrapper WAITS behind an active writer (observed: it hangs at
    step 1/12). MIGRATE_LOCK_TIMEOUT_MS is the opt-in guard; prove it reaches every step via PGOPTIONS."""
    first = _run_wrapper(pg)
    assert first.returncode == 0, _out(first)
    assert "(12/13) migrations/010_worker_runtime_status.py" in _out(first)
    assert "NOTE: no MIGRATE_LOCK_TIMEOUT_MS set" in _out(first)  # the default is loud about the hazard
    pg.sql(f"DROP INDEX {IDX}")  # back to "migrated through 009, 010's index not yet built"
    pg.sql("INSERT INTO campaigns (name, organization_id, sender_name, sender_org, sender_email, value_prop) "
           "VALUES ('c','org-x','n','o','s@e.org','vp')")
    pg.sql("INSERT INTO contacts (campaign_id, email) VALUES (1, 'c@e.org')")
    with _open_writer_on_messages(pg):
        t0 = time.monotonic()
        blocked = _run_wrapper(pg, {"MIGRATE_LOCK_TIMEOUT_MS": "500"})
        elapsed = time.monotonic() - t0
    out = _out(blocked)
    assert blocked.returncode != 0 and elapsed < 120, out
    assert "lock_timeout=500ms applied to every statement" in out
    assert "lock timeout" in out and "FAILED at step" in out and "Later steps were NOT run" in out
    assert IDX not in pg.index_names(), "the index must not have been built while a writer held the table"
    ok = _run_wrapper(pg, {"MIGRATE_LOCK_TIMEOUT_MS": "500"})  # writer gone: the same guarded run completes
    assert ok.returncode == 0, _out(ok)
    assert NEW_TABLES <= pg.tables() and IDX in pg.index_names()


@pytest.mark.parametrize("bad", ["0", "50", "999999", "abc", "5 0", "-1"])
def test_the_wrapper_rejects_a_bad_lock_timeout_before_touching_the_database(bad):
    env = {**os.environ, "DATABASE_URL": "postgresql://x:y@127.0.0.1:1/mailer_migrate_test_none",
           "MIGRATE_DISPOSABLE_TEST_DB": "yes", "MIGRATE_LOCK_TIMEOUT_MS": bad}
    p = subprocess.run(["bash", str(ROOT / "scripts" / "migrate_release.sh")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60, check=False)
    assert p.returncode == 2 and "MIGRATE_LOCK_TIMEOUT_MS must be" in _out(p), _out(p)
    assert "preflight" not in _out(p).lower(), "validation must run before any guard or database work"


def test_the_lock_timeout_can_not_be_switched_off_by_setting_it_to_zero(pre_pr4):
    pg = pre_pr4
    with _open_writer_on_messages(pg):
        t0 = time.monotonic()
        p = pg.run_migration(extra_env={"MIGRATION_010_LOCK_TIMEOUT_MS": "0"})  # PostgreSQL reads 0 as "wait forever"
        assert p.returncode != 0 and time.monotonic() - t0 < 30, _out(p)
        assert "could not take the lock" in _out(p)


def test_the_default_lock_timeout_applies_when_nothing_is_configured(pre_pr4):
    pg = pre_pr4
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIGRATION_010_")}
    with _open_writer_on_messages(pg):
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, str(MIGRATION)], cwd=ROOT, capture_output=True, text=True, timeout=120,
                           check=False, env={**env, "DATABASE_URL": pg.dsn, "PYTHONPATH": str(ROOT)})
        elapsed = time.monotonic() - t0
    assert p.returncode != 0 and "NOTHING was changed" in _out(p)
    assert 4 <= elapsed < 30, f"default is 5000 ms, took {elapsed:.1f}s"


def test_lock_timeout_does_not_get_in_the_way_when_there_is_no_contention(pre_pr4):
    p = pre_pr4.run_migration(extra_env={"MIGRATION_010_LOCK_TIMEOUT_MS": "100"})
    assert p.returncode == 0, _out(p)


def test_statement_timeout_is_optional_and_a_cancelled_build_also_changes_nothing(pre_pr4):
    pg = pre_pr4
    # a large inbound set so the build has real work to do; the 1 s floor is the smallest cap the migration accepts
    pg.sql("INSERT INTO messages (contact_id, direction, body, created_at) "
           "SELECT 1, 'inbound', 'b', now() - (g||' seconds')::interval FROM generate_series(1, 400000) g")
    p = pg.run_migration(extra_env={"MIGRATION_010_STATEMENT_TIMEOUT_MS": "1000"})  # floor is 1 s
    if p.returncode == 0:  # a very fast machine can build 400k rows inside 1 s; the contract is "if cancelled, nothing changed"
        assert NEW_TABLES <= pg.tables() and IDX in pg.index_names()
    else:
        assert "NOTHING was changed" in _out(p)
        assert not (NEW_TABLES & pg.tables()) and IDX not in pg.index_names()


# ---- an INVALID index (what a cancelled CREATE INDEX CONCURRENTLY leaves behind) must never be accepted
def _make_index_invalid(pg):
    pg.sql(f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '{IDX}'::regclass")  # disposable DB, superuser


def test_an_invalid_index_is_refused_by_the_migration_status_and_verify(pre_pr4):
    pg = pre_pr4
    pg.sql(f"CREATE INDEX {IDX} ON messages (created_at) WHERE direction = 'inbound'")
    _make_index_invalid(pg)
    p = pg.run_migration()
    assert p.returncode != 0 and "INVALID" in _out(p), _out(p)
    # the GUARD (not merely the later verify step) must stop the run before anything is committed
    assert "Refusing to treat it as present" in _out(p)
    assert not (NEW_TABLES & pg.tables()), "a refused run must leave no partial state (no tables created)"
    st = _out(pg.preflight_status())
    idx_line = next(ln for ln in st.splitlines() if "ix_messages_inbound_created_at" in ln)
    assert "ABSENT" in idx_line and "must be VALID" in idx_line, "an invalid index is not 'present' in --status"
    pg.sql(f"DROP INDEX {IDX}")
    assert pg.run_migration().returncode == 0
    after = _out(pg.preflight_status())
    lines_010 = [ln for ln in after.splitlines() if "010 " in ln and ("[present]" in ln or "[ABSENT" in ln)]
    assert len(lines_010) == 3 and all("[present]" in ln for ln in lines_010), lines_010


def test_an_operator_built_concurrent_index_is_accepted_and_skipped(pre_pr4):
    """The documented alternative for a large table: build the index by hand, outside the wrapper."""
    pg = pre_pr4
    with pg.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f"CREATE INDEX CONCURRENTLY {IDX} ON messages (created_at) WHERE direction = 'inbound'"))
    p = pg.run_migration()
    assert p.returncode == 0, _out(p)
    assert "already exists -- skipping" in _out(p)  # the index step was skipped; tables were still created
    assert NEW_TABLES <= pg.tables()
    spec = importlib.util.spec_from_file_location("migration_010", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with Session(bind=pg.engine) as s:
        assert mod.verify(s) is True


# ---- the read-only inspection script
INSPECT = ROOT / "scripts" / "migrate_010_inspect.sql"


def _inspect_statements():
    body = "\n".join(ln for ln in INSPECT.read_text().splitlines() if not ln.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def test_inspection_script_contains_only_selects_and_the_read_only_setting():
    stmts = _inspect_statements()
    assert stmts[0].upper() == "SET DEFAULT_TRANSACTION_READ_ONLY = ON"
    assert all(s.split()[0].upper() == "SELECT" for s in stmts[1:])

    # The full-table count is deliberately opt-in: psql -f must not execute it.
    source_lines = INSPECT.read_text(encoding="utf-8").splitlines()
    optional_count = "SELECT count(*) FILTER (WHERE direction = 'inbound')"
    assert any(
        line.lstrip().startswith("-- " + optional_count)
        for line in source_lines
    ), "the expensive full-table count must remain documented but commented out"
    assert not any(
        line.lstrip().startswith(optional_count)
        for line in source_lines
    ), "the expensive full-table count must not execute by default"

    body = "\n".join(stmts)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|VACUUM|ANALYZE|COPY|LOCK)\b",
                         body, re.IGNORECASE)
    assert not re.search(r"\bquery\b\s*(,|FROM)|\.query\b", body, re.IGNORECASE), "must never select pg_stat_activity.query"


def test_inspection_script_runs_on_real_postgres_in_a_read_only_session_and_reports_the_blocker(pre_pr4):
    pg = pre_pr4
    stmts = _inspect_statements()
    with _open_writer_on_messages(pg), pg.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(stmts[0]))  # the script's own read-only setting
        with pytest.raises(Exception, match="read-only"):  # ... which really is enforced
            c.execute(text("DELETE FROM messages"))
        results = [c.execute(text(s)).all() for s in stmts[1:]]

    # The default script omits the optional full-table COUNT, so seven query
    # results follow the initial read-only SET statement.
    assert len(results) == 7
    settings, sizes, counters, active, locks, index_state, tables = results
    assert settings[0].lock_timeout_default is not None and sizes and counters
    assert any(r.transaction_age is not None for r in active), "the open writer must be visible as an active transaction"
    assert any(r.mode == "RowExclusiveLock" and r.granted for r in locks), "and as a RowExclusiveLock on messages"
    assert index_state == []  # pre-PR4 shape: no index yet
    assert tables[0].worker_runtime_status is None and tables[0].worker_cycle_status is None


# =============================================================== aggregates on real PostgreSQL types
NOW = datetime.now(UTC)


def _naive(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).replace(tzinfo=None)


def test_dispatch_aggregates_use_timestamptz_created_at_and_naive_claimed_at_correctly(pg):
    Base.metadata.create_all(bind=pg.engine)
    with pg.sm() as s:
        ids = {}
        for n, (state, kw) in enumerate([
            ("queued", {}), ("queued", {}), ("sending", {"claimed_at": _naive(1000)}),   # expired (lease 900)
            ("sending", {"claimed_at": _naive(100)}), ("unknown", {}), ("sent", {}), ("failed", {}),
        ]):
            d = seed_dispatch(s, state=state, mailbox=False, email=f"p{n}@example.com",
                              claimed_by="w" if kw else None, **kw)
            ids[n] = d.id
        d0 = s.get(ExternalDispatch, ids[0])
        d0.created_at = NOW - timedelta(seconds=300)
        d1 = s.get(ExternalDispatch, ids[1])
        d1.created_at = NOW - timedelta(seconds=50)
        d1.message_id = None
        s.commit()
        m = dispatch_metrics(s, now=NOW, lease_seconds=900)
    assert m["states"] == {"queued": 2, "generating": 0, "sending": 2, "sent": 1, "failed": 1, "unknown": 1}
    assert (m["queue"]["awaiting_generation"], m["queue"]["ready_to_send"]) == (1, 1)
    assert m["queue"]["oldest_queued_age_seconds"] == 300
    assert m["queue"]["oldest_awaiting_generation_age_seconds"] == 50
    assert (m["sending"]["stale_lease_expired"], m["sending"]["in_flight_or_unexpired"]) == (1, 1)


def test_stale_sending_count_agrees_with_the_real_recovery_sweep_on_postgres(pg):
    Base.metadata.create_all(bind=pg.engine)
    with pg.sm() as s:
        for n, ago in enumerate((10, 899, 901, 5000, None)):
            seed_dispatch(s, state="sending", mailbox=False, email=f"q{n}@example.com",
                          claimed_at=None if ago is None else _naive(ago), claimed_by="w")
        counted = dispatch_metrics(s, now=NOW, lease_seconds=900)["sending"]["stale_lease_expired"]
        recovered = recover_expired_external_dispatches(s, now=NOW, lease_seconds=900)  # FOR UPDATE SKIP LOCKED path
        s.commit()
    assert counted == len(recovered) == 3


def test_inbound_window_on_postgres_counts_inside_the_window_only(pg):
    Base.metadata.create_all(bind=pg.engine)
    with pg.sm() as s:
        seed_dispatch(s, state="sent", mailbox=False)
        for age, ok, direction in ((60, True, "inbound"), (120, False, "inbound"), (180, None, "inbound"),
                                   (25 * 3600, True, "inbound"), (30, True, "outbound")):
            s.add(Message(contact_id=1, direction=direction, body="b", classification_success=ok,
                          created_at=NOW - timedelta(seconds=age)))
        s.commit()
        p = inbound_metrics(s, now=NOW, window_hours=24, stale_after_seconds=120)["persisted_inbound_messages"]
    assert (p["stored_message_rows"], p["classification_succeeded"], p["classification_failed"],
            p["classification_not_recorded"]) == (3, 1, 1, 1)


def test_worker_endpoint_logic_round_trips_on_postgres_timestamptz(pg):
    Base.metadata.create_all(bind=pg.engine)
    clock_now = datetime.now(UTC)
    rec = WorkerStatusRecorder("worker", session_factory=pg.sm, clock=lambda: clock_now - timedelta(seconds=30))
    assert rec.heartbeat() and rec.record_cycle(KIND_EXTERNAL_DISPATCH, "completed", {"processed": 2})
    with pg.sm() as s:
        m = worker_metrics(s, now=clock_now, stale_after_seconds=120, heartbeat_interval_seconds=30, retention_hours=72)
    inst = m["instances"][0]
    assert inst["heartbeat_status"] == "fresh" and inst["heartbeat_age_seconds"] == 30
    assert inst["cycles"][KIND_EXTERNAL_DISPATCH]["last_cycle_counts"]["processed"] == 2
    assert rec.mark_stopped()
    with pg.sm() as s:
        assert worker_metrics(s, now=clock_now, stale_after_seconds=120, heartbeat_interval_seconds=30,
                              retention_hours=72)["summary"]["heartbeat_status"] == "stopped"


def test_a_racing_insert_of_the_same_instance_row_is_benign_and_verified_on_postgres(pg):
    """A REAL unique violation: another transaction commits this process's row between the UPDATE (0 rows)
    and the INSERT. The recorder must confirm the row exists, refresh it, and report success."""
    Base.metadata.create_all(bind=pg.engine)
    when = datetime.now(UTC)
    rec = WorkerStatusRecorder("worker", session_factory=pg.sm, clock=lambda: when)
    raced = {"done": False}

    def race(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO WORKER_RUNTIME_STATUS") and not raced["done"]:
            raced["done"] = True
            with pg.engine.begin() as other:  # a second, independent connection commits first
                other.execute(
                    text("INSERT INTO worker_runtime_status (instance_id, role, started_at, heartbeat_at) "
                         "VALUES (:i, 'worker', :t, :t)"),
                    {"i": rec.instance_id, "t": when - timedelta(hours=1)},
                )

    event.listen(pg.engine, "before_cursor_execute", race)
    try:
        assert rec.heartbeat() is True
    finally:
        event.remove(pg.engine, "before_cursor_execute", race)
    assert raced["done"] and pg.count("worker_runtime_status") == 1
    with pg.engine.connect() as c:
        hb = c.execute(text("SELECT heartbeat_at FROM worker_runtime_status")).scalar()
    assert abs((hb - when).total_seconds()) < 1, "the benign race must still refresh the heartbeat"


def test_a_racing_insert_that_is_already_stopped_is_never_revived_on_postgres(pg):
    Base.metadata.create_all(bind=pg.engine)
    when = datetime.now(UTC)
    rec = WorkerStatusRecorder("worker", session_factory=pg.sm, clock=lambda: when)
    stopped_at = when - timedelta(minutes=5)
    raced = {"done": False}

    def race(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO WORKER_RUNTIME_STATUS") and not raced["done"]:
            raced["done"] = True
            with pg.engine.begin() as other:
                other.execute(
                    text("INSERT INTO worker_runtime_status (instance_id, role, started_at, heartbeat_at, stopped_at) "
                         "VALUES (:i, 'worker', :s, :s, :s)"),
                    {"i": rec.instance_id, "s": stopped_at},
                )

    event.listen(pg.engine, "before_cursor_execute", race)
    try:
        rec.heartbeat()
    finally:
        event.remove(pg.engine, "before_cursor_execute", race)
    with pg.engine.connect() as c:
        row = c.execute(text("SELECT heartbeat_at, stopped_at FROM worker_runtime_status")).one()
    assert row.stopped_at is not None and abs((row.heartbeat_at - stopped_at).total_seconds()) < 1


def test_the_recorder_is_safe_with_two_processes_writing_concurrently(pg):
    Base.metadata.create_all(bind=pg.engine)
    import threading

    recs = [WorkerStatusRecorder("worker", session_factory=pg.sm) for _ in range(6)]
    errors = []

    def work(r):
        try:
            for _ in range(5):
                assert r.heartbeat()
                assert r.record_cycle(KIND_EXTERNAL_DISPATCH, "completed", {"processed": 1})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(r,)) for r in recs]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors
    assert pg.count("worker_runtime_status") == 6 and pg.count("worker_cycle_status") == 6
    with pg.sm() as s:
        totals = {c[0] for c in s.execute(text("SELECT total_counts->>'processed' FROM worker_cycle_status")).all()}
    assert totals == {"5"}  # each process's own row accumulated exactly its own 5 cycles


# =============================================================== query-plan evidence (the index review)
def _explain(db, statement, params=None):
    with db.engine.connect() as c:
        return "\n".join(r[0] for r in c.exec_driver_sql("EXPLAIN (COSTS OFF) " + statement, params or ()).all())


@pytest.fixture()
def skewed(pg):
    """40k dispatches (99.8% terminal) and 60k messages (1.7% inbound): the shape that makes plans matter."""
    Base.metadata.create_all(bind=pg.engine)
    with pg.sm() as s:
        s.add(Campaign(name="c", organization_id="org-x", sender_name="n", sender_org="o",
                       sender_email="s@e.org", value_prop="vp"))
        s.flush()
        s.add(Contact(campaign_id=1, email="c@e.org"))
        s.commit()
    pg.sql("""INSERT INTO external_dispatches (organization_id, idempotency_key, campaign_id, contact_id,
                request_fingerprint, public_reference, state, created_at, updated_at)
              SELECT 'org-x','k'||g,1,1,repeat('f',64),'r'||g,
                     CASE WHEN g<=40 THEN 'queued' WHEN g<=50 THEN 'sending' WHEN g<=60 THEN 'unknown' ELSE 'sent' END,
                     now()-(g||' seconds')::interval, now()
              FROM generate_series(1,40000) g""")
    pg.sql("""INSERT INTO messages (contact_id, direction, body, status, created_at)
              SELECT 1, CASE WHEN g<=1000 THEN 'inbound' ELSE 'outbound' END,'b','sent',
                     now()-(g||' seconds')::interval FROM generate_series(1,60000) g""")
    with pg.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("VACUUM (ANALYZE) external_dispatches"))
        c.execute(text("VACUUM (ANALYZE) messages"))
    return pg


def test_plans_justify_no_new_dispatch_index_and_one_partial_inbound_index(skewed):
    pg = skewed
    # statements exactly as the endpoints issue them

    captured = []

    def grab(conn, cur, stmt, params, ctx, many):
        if "external_dispatches" in stmt or "FROM messages" in stmt:
            captured.append((stmt, params))

    event.listen(pg.engine, "before_cursor_execute", grab)
    try:
        with pg.sm() as s:
            dispatch_metrics(s, now=datetime.now(UTC), lease_seconds=900)
            inbound_metrics(s, now=datetime.now(UTC), window_hours=24, stale_after_seconds=120)
    finally:
        event.remove(pg.engine, "before_cursor_execute", grab)
    assert len(captured) == 3
    plans = [_explain(pg, stmt, params) for stmt, params in captured]
    group_by_state, working_set, inbound_window = plans

    # 1. counting by state never needs the heap: an index-only scan of the EXISTING (state, claimed_at) index
    assert "Index Only Scan using ix_external_dispatches_state_claimed_at" in group_by_state, group_by_state
    # 2. the non-terminal working set is an index scan, not a table scan, although 99.8% of rows are terminal
    assert "ix_external_dispatches_state_claimed_at" in working_set and "Seq Scan" not in working_set, working_set
    # 3. the inbound window uses the new partial index
    assert IDX in inbound_window and "Seq Scan" not in inbound_window, inbound_window


def test_without_the_partial_index_the_inbound_window_is_a_sequential_scan_which_is_why_it_exists(skewed):
    pg = skewed
    pg.sql(f"DROP INDEX {IDX}")
    plan = _explain(
        pg,
        "SELECT count(*) FROM messages WHERE direction = 'inbound' AND created_at >= now() - interval '24 hours'",
    )
    assert "Seq Scan on messages" in plan, plan
