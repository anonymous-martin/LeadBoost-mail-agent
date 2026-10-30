"""
Migration 012: messages.reply_reference -- the opaque public handle of a message.

Before this migration the LeadBoost conversation view identified an inbound message to reply to by the
decimal form of ``messages.id`` -- a guessable internal key (the PR #27 review finding). Code that includes
this migration hands out ``messages.reply_reference`` instead: 32 lowercase hex characters (a random UUID4),
unique, generated once when the row is inserted and never rewritten.

What this does, in this order (every step is skipped when already done, so re-running is always safe):

  1. ADD COLUMN messages.reply_reference VARCHAR(32) NULL        (committed: the ACCESS EXCLUSIVE lock is brief)
  2. BACKFILL every row whose reply_reference IS NULL            (batches of BATCH_SIZE, one commit per batch)
  3. CREATE UNIQUE INDEX uq_messages_reply_reference             (after the backfill, so it cannot fail on NULLs;
                                                                 NULLs never collide in a unique index on
                                                                 PostgreSQL or SQLite)
  4. verify

The backfill only touches rows that are still NULL (``WHERE id = :id AND reply_reference IS NULL``), so a row
that already has a token -- from the new code, from an earlier run, or from a concurrent writer -- is never
overwritten: tokens are stable. Inbound and outbound rows are both filled (outbound tokens are never exposed;
filling them keeps the column uniform).

The column stays NULLABLE on purpose. Code from before this change (a rollback, or a worker not yet restarted)
inserts rows without a token and must not fail; those rows have no public handle until this migration is run
again, which backfills them. The index is a plain unique index (no WHERE clause) so one DDL works on both
PostgreSQL and SQLite.

Locking: like 009, the index is built with a plain CREATE UNIQUE INDEX, which blocks writes to ``messages``
while it builds. This is a release step run by an operator through scripts/migrate_release.sh (which can set
MIGRATE_LOCK_TIMEOUT_MS so it gives up instead of queueing behind writers); it is not meant to be run
concurrently with traffic on a very large table.

Run:        python migrations/012_message_reply_reference.py
Downgrade:  python migrations/012_message_reply_reference.py --downgrade
            DROPS the column and every token. Any reference LeadBoost already stored
            (outreach_actions.reply_to_message_reference) stops resolving (404) and a re-run of 012 mints
            DIFFERENT tokens. Code from before this change simply ignores the column, so rolling the CODE back
            does not require the downgrade.
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import inspect, text

from mailer_agent.db import session_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TABLE = "messages"
COLUMN = "reply_reference"
INDEX = "uq_messages_reply_reference"
BATCH_SIZE = 1000


def _dialect(db) -> str:
    return db.get_bind().dialect.name


def _column_exists(db) -> bool:
    return any(c["name"] == COLUMN for c in inspect(db.connection()).get_columns(TABLE))


def _index_exists(db) -> bool:
    return any(i["name"] == INDEX for i in inspect(db.connection()).get_indexes(TABLE))


def add_column(db) -> bool:
    if _column_exists(db):
        logger.info("Column %s.%s already exists -- skipping", TABLE, COLUMN)
        return False
    db.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(32) NULL"))
    logger.info("✓ Added %s.%s", TABLE, COLUMN)
    return True


def backfill(db) -> int:
    """Give every NULL row a fresh token. Commits per batch; never overwrites an existing token."""
    total = 0
    while True:
        ids = [
            r[0]
            for r in db.execute(
                text(f"SELECT id FROM {TABLE} WHERE {COLUMN} IS NULL ORDER BY id LIMIT :n"),
                {"n": BATCH_SIZE},
            ).fetchall()
        ]
        if not ids:
            break
        db.execute(
            text(f"UPDATE {TABLE} SET {COLUMN} = :token WHERE id = :id AND {COLUMN} IS NULL"),
            [{"token": uuid.uuid4().hex, "id": i} for i in ids],
        )
        db.commit()
        total += len(ids)
        logger.info("  backfilled %d row(s) so far", total)
    logger.info("✓ Backfill complete (%d row(s) filled this run)", total)
    return total


def create_index(db) -> bool:
    if _index_exists(db):
        logger.info("Index %s already exists -- skipping", INDEX)
        return False
    db.execute(text(f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} ({COLUMN})"))
    logger.info("✓ Created %s", INDEX)
    return True


def _index_valid_postgres(db) -> bool:
    row = db.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :n"
        ),
        {"n": INDEX},
    ).fetchone()
    return bool(row and row[0])


def verify(db) -> bool:
    insp = inspect(db.connection())
    if not any(c["name"] == COLUMN for c in insp.get_columns(TABLE)):
        logger.error("✗ %s.%s missing", TABLE, COLUMN)
        return False
    idx = [i for i in insp.get_indexes(TABLE) if i["name"] == INDEX]
    if not idx or not idx[0].get("unique") or list(idx[0]["column_names"]) != [COLUMN]:
        logger.error("✗ %s missing or not a UNIQUE index on (%s)", INDEX, COLUMN)
        return False
    if _dialect(db) == "postgresql" and not _index_valid_postgres(db):
        logger.error("✗ %s exists but is INVALID (a failed concurrent build?) -- drop it and re-run", INDEX)
        return False
    nulls = db.execute(text(f"SELECT COUNT(*) FROM {TABLE} WHERE {COLUMN} IS NULL")).scalar()
    if nulls:
        logger.error("✗ %d row(s) still have no %s (written by older code during the run?) -- re-run", nulls, COLUMN)
        return False
    logger.info("✓ %s.%s present, unique index valid, no row without a token", TABLE, COLUMN)
    return True


def run() -> bool:
    logger.info("Starting migration 012: messages.reply_reference")
    with session_scope() as db:
        try:
            add_column(db)
            db.commit()
            backfill(db)
            create_index(db)
            db.commit()
            ok = verify(db)
            logger.info("✅ Migration 012 completed" if ok else "⚠️ Migration 012 verification failed")
            return ok
        except Exception:
            logger.exception("Migration 012 failed")
            db.rollback()
            raise


def downgrade() -> bool:
    logger.info("Starting migration 012 DOWNGRADE")
    with session_scope() as db:
        try:
            if _index_exists(db):
                db.execute(text(f"DROP INDEX {INDEX}"))
                logger.info("✓ Dropped %s", INDEX)
            if _column_exists(db):
                logger.warning("Dropping %s.%s: every opaque reply reference is discarded", TABLE, COLUMN)
                db.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"))
                logger.info("✓ Dropped %s.%s", TABLE, COLUMN)
            db.commit()
            return True
        except Exception:
            logger.exception("Migration 012 downgrade failed")
            db.rollback()
            raise


if __name__ == "__main__":
    try:
        ok = downgrade() if "--downgrade" in sys.argv[1:] else run()
        sys.exit(0 if ok else 1)
    except Exception:
        logger.exception("Unhandled error during migration 012")
        sys.exit(1)
