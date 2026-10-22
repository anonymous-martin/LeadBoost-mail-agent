"""
Migration 010: worker runtime status (PR4, operator observability).

Creates, additively and idempotently:

  * `worker_runtime_status`  -- one row per scheduler-bearing process: opaque random instance id,
                                role, started/heartbeat/stopped timestamps.
  * `worker_cycle_status`    -- at most one row per (process, job kind): latest outcome label and
                                whitelisted integer counters.
  * `ix_messages_inbound_created_at` -- a PARTIAL index on messages(created_at) WHERE
                                direction = 'inbound', serving the operator-only inbound aggregate
                                (/system/metrics/inbound). It adds a write only to inbound inserts.

The tables are created with <Model>.__table__.create(checkfirst=True) -- the same pattern as 004/006,
so the DDL cannot drift from models.py and coexists with db.py::init_db()'s create_all (whichever
runs first wins; the other no-ops). The index is the part create_all can NOT do for an already
existing `messages` table, which is why this migration exists as a numbered step.

Nothing existing reads or writes the new tables other than the PR4 code; no business-data column is
altered and nothing is dropped. The tables hold NO tenant, contact, address, Message-ID, content,
credential or error text -- only an opaque process id, timestamps, fixed labels and counters.

Locking (PostgreSQL) -- read before running against a real database
--------------------------------------------------------------------
A plain CREATE INDEX takes a SHARE lock on `messages`: reads continue, but INSERT/UPDATE/DELETE wait
for the whole build. Suspending the worker is NOT enough -- the API also writes `messages`
(POST /webhooks/inbound-email, POST /messages/{id}/approve, POST /integrations/leadboost/outreach-actions).
Two guards are built in:

  * lock_timeout (default 5000 ms; env MIGRATION_010_LOCK_TIMEOUT_MS, clamped 100..300000). The build must
    first acquire its lock, and while it waits behind a long-running writer every NEW writer queues behind
    it. With a lock_timeout it gives up quickly instead: the transaction rolls back (PostgreSQL DDL is
    transactional, so the new tables are rolled back too), the script exits non-zero, and NOTHING has
    changed. Re-run in a quieter window.
  * optional statement_timeout (env MIGRATION_010_STATEMENT_TIMEOUT_MS, default: none) to cap how long
    writes may be blocked by the build itself. A cancelled build also changes nothing.

CREATE INDEX CONCURRENTLY is not used because the migration runner executes inside a transaction. If the
real table is large enough that blocking writes for the build is unacceptable, an operator can build the
index first, by hand and outside the wrapper, with exactly:
    CREATE INDEX CONCURRENTLY ix_messages_inbound_created_at ON messages (created_at) WHERE direction = 'inbound';
and this migration will then see it and skip. It checks the index is VALID (a cancelled CONCURRENTLY
build leaves an INVALID index that must be dropped and rebuilt) and refuses to accept an invalid one.
Measure the size, the write rate and the active transactions BEFORE choosing: scripts/migrate_010_inspect.sql
(read-only) and docs/C13_DEPLOYMENT_RUNBOOK.md section 9.1.1.

The release wrapper's EARLIER steps contend with writers too (CREATE INDEX IF NOT EXISTS on `messages` needs the
SHARE lock even when the index exists). Set MIGRATE_LOCK_TIMEOUT_MS for the wrapper run (see
scripts/migrate_release.sh); this script's own timeout above only covers step 12.

Run:        python migrations/010_worker_runtime_status.py
Downgrade:  python migrations/010_worker_runtime_status.py --downgrade
            Drops the two status tables and the index. This discards ONLY operator telemetry
            (heartbeats and latest-cycle snapshots, rebuilt automatically by a running worker); no
            business data. Code from before PR4 simply ignores these tables, so rolling the CODE back
            does not require running the downgrade.
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from mailer_agent.db import session_scope
from mailer_agent.models import Message, WorkerCycleStatus, WorkerRuntimeStatus

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MODELS = (WorkerRuntimeStatus, WorkerCycleStatus)
INDEX_NAME = "ix_messages_inbound_created_at"
_UNIQUES = {
    WorkerRuntimeStatus.__tablename__: {("instance_id",)},
    WorkerCycleStatus.__tablename__: {("instance_id", "kind")},
}


def _env_ms(name: str, default: int, lo: int, hi: int) -> int:
    """Integer milliseconds from the environment, clamped to [lo, hi]. Empty/invalid -> default.
    (0 is clamped UP to ``lo``: it must never silently disable a timeout, because PostgreSQL reads 0 as 'no limit'.)"""
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _apply_timeouts(db) -> int:
    """PostgreSQL only: fail fast instead of queueing behind (and then blocking) writers. Returns lock_timeout ms."""
    conn = db.connection()
    if conn.dialect.name != "postgresql":
        return 0
    lock_ms = _env_ms("MIGRATION_010_LOCK_TIMEOUT_MS", 5000, 100, 300000)
    conn.execute(text(f"SET LOCAL lock_timeout = {int(lock_ms)}"))  # int-formatted: no injection surface
    stmt_ms = _env_ms("MIGRATION_010_STATEMENT_TIMEOUT_MS", 0, 1000, 3600000) if os.environ.get(
        "MIGRATION_010_STATEMENT_TIMEOUT_MS", "").strip() else 0  # unset = no statement cap
    if stmt_ms:
        conn.execute(text(f"SET LOCAL statement_timeout = {int(stmt_ms)}"))
    return lock_ms


def _index_valid(db):
    """True/False on PostgreSQL (pg_index.indisvalid); None if the index does not exist; True elsewhere."""
    conn = db.connection()
    if conn.dialect.name != "postgresql":
        return True if _index_exists(db) else None
    return conn.execute(
        text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)"), {"n": INDEX_NAME}
    ).scalar()


def _inbound_index():
    return next(i for i in Message.__table__.indexes if i.name == INDEX_NAME)


def _index_exists(db) -> bool:
    return any(i["name"] == INDEX_NAME for i in inspect(db.connection()).get_indexes(Message.__tablename__))


def create_tables(db) -> list:
    created = []
    for model in MODELS:
        if inspect(db.connection()).has_table(model.__tablename__):
            logger.info("Table %s already exists -- skipping", model.__tablename__)
            continue
        model.__table__.create(bind=db.connection(), checkfirst=True)
        logger.info("✓ Created %s", model.__tablename__)
        created.append(model.__tablename__)
    return created


def create_index(db) -> bool:
    if _index_exists(db):
        if _index_valid(db) is False:
            raise RuntimeError(
                f"index {INDEX_NAME} exists but is INVALID (a cancelled CREATE INDEX CONCURRENTLY leaves one). "
                f"Refusing to treat it as present: DROP INDEX {INDEX_NAME}; then re-run."
            )
        logger.info("Index %s already exists -- skipping", INDEX_NAME)
        return False
    _inbound_index().create(bind=db.connection(), checkfirst=True)
    logger.info("✓ Created index %s", INDEX_NAME)
    return True


def drop_all(db) -> None:
    if _index_exists(db):
        _inbound_index().drop(bind=db.connection(), checkfirst=True)
        logger.info("✓ Dropped index %s", INDEX_NAME)
    for model in reversed(MODELS):
        if inspect(db.connection()).has_table(model.__tablename__):
            logger.warning("Dropping %s: discarding operator telemetry only", model.__tablename__)
            model.__table__.drop(bind=db.connection(), checkfirst=True)
            logger.info("✓ Dropped %s", model.__tablename__)


def verify(db) -> bool:
    """Read-only. Reports table / column / constraint / index NAMES only -- never row values."""
    insp = inspect(db.connection())
    ok = True
    for model in MODELS:
        table = model.__tablename__
        if not insp.has_table(table):
            logger.error("✗ %s missing", table)
            ok = False
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        missing = sorted({c.name for c in model.__table__.columns} - have)
        if missing:
            logger.error("✗ %s is missing columns: %s", table, ", ".join(missing))
            ok = False
        uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints(table)}
        uniques |= {tuple(i["column_names"]) for i in insp.get_indexes(table) if i.get("unique")}
        if not _UNIQUES[table] <= uniques:
            logger.error("✗ %s is missing its unique constraint", table)
            ok = False
        if not missing and _UNIQUES[table] <= uniques:
            logger.info("✓ %s present with expected columns and unique constraint", table)
    if not _index_exists(db):
        logger.error("✗ index %s missing", INDEX_NAME)
        ok = False
    elif _index_valid(db) is False:
        logger.error("✗ index %s exists but is INVALID (drop and rebuild it)", INDEX_NAME)
        ok = False
    else:
        logger.info("✓ index %s present and valid", INDEX_NAME)
    return ok


def run() -> bool:
    logger.info("Starting migration 010: worker runtime status")
    with session_scope() as db:
        try:
            lock_ms = _apply_timeouts(db)
            create_tables(db)
            create_index(db)
            db.commit()
            ok = verify(db)
            logger.info("✅ Migration 010 completed" if ok else "⚠️ Migration 010 verification failed")
            return ok
        except OperationalError as exc:
            db.rollback()
            code = getattr(getattr(exc, "orig", None), "pgcode", None)
            if code == "55P03":  # lock_not_available
                logger.error(
                    "Migration 010 could not take the lock it needs on `messages` within %d ms (another "
                    "transaction holds a conflicting lock). NOTHING was changed: the transaction was rolled "
                    "back, including the new tables. Re-run in a quieter window (see the migration docstring).",
                    lock_ms,
                )
                return False
            if code == "57014":  # query_canceled (statement_timeout)
                logger.error("Migration 010 hit MIGRATION_010_STATEMENT_TIMEOUT_MS. NOTHING was changed.")
                return False
            logger.exception("Migration 010 failed")
            raise
        except Exception:
            logger.exception("Migration 010 failed")
            db.rollback()
            raise


def downgrade() -> bool:
    logger.info("Starting migration 010 DOWNGRADE")
    with session_scope() as db:
        try:
            drop_all(db)
            db.commit()
            return True
        except Exception:
            logger.exception("Migration 010 downgrade failed")
            db.rollback()
            raise


if __name__ == "__main__":
    try:
        ok = downgrade() if "--downgrade" in sys.argv[1:] else run()
        sys.exit(0 if ok else 1)
    except Exception:
        logger.exception("Unhandled error during migration 010")
        sys.exit(1)
