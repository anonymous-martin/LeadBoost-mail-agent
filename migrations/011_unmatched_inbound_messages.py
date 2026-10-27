"""
Migration 011: unmatched_inbound_messages table (durable retention of unmatched/ambiguous mailbox mail).

Before this migration, mailbox-polled inbound mail that could not be attached to exactly one contact was
neither stored nor guessed -- and the IMAP message was still marked \\Seen, so the poller never offered it
again: the message existed only on the provider side, where any other client could delete it. Code that
includes this migration writes such a message here, in the transaction that precedes the \\Seen flag.

Creates `unmatched_inbound_messages` via UnmatchedInboundMessage.__table__.create(checkfirst=True) -- the
same pattern as 004/006/010, so the DDL cannot drift from models.py and coexists with
db.py::init_db()'s create_all (whichever runs first wins; the other no-ops). The wrapper runs init_db()
first, so on the canonical path this step normally finds the table already present and only verifies it.

Purely additive: no existing table or column is altered and nothing is dropped. Nothing existing reads this
table; it is an operator-facing holding area (rows keep the message's From, Subject, body and threading
headers, exactly like `messages` does, plus the reason and the polled mailbox/organization).
`mailbox_id` references `mailboxes.id` ON DELETE RESTRICT, like `messages.mailbox_id`.

Code from before this change simply ignores the table, so rolling the CODE back does not require running
the downgrade.

Run:        python migrations/011_unmatched_inbound_messages.py
Downgrade:  python migrations/011_unmatched_inbound_messages.py --downgrade
            DESTRUCTIVE: drops the table and every retained unmatched message. Those messages may exist
            nowhere else (they are marked Seen on the provider side), so export first if any are
            unresolved:  SELECT * FROM unmatched_inbound_messages WHERE resolved_at IS NULL;
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import inspect, text

from mailer_agent.db import session_scope
from mailer_agent.models import UnmatchedInboundMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TABLE = UnmatchedInboundMessage.__tablename__


def _table_exists(db) -> bool:
    return inspect(db.connection()).has_table(TABLE)


def create_table(db) -> bool:
    if _table_exists(db):
        logger.info("Table %s already exists -- skipping", TABLE)
        return False
    UnmatchedInboundMessage.__table__.create(bind=db.connection(), checkfirst=True)
    logger.info("✓ Created %s", TABLE)
    return True


def drop_table(db) -> bool:
    if not _table_exists(db):
        logger.info("Table %s does not exist -- skipping", TABLE)
        return False
    count = db.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
    unresolved = db.execute(text(f"SELECT COUNT(*) FROM {TABLE} WHERE resolved_at IS NULL")).scalar()
    logger.warning(
        "Dropping %s: discarding %s retained message(s), %s of them unresolved", TABLE, count, unresolved
    )
    UnmatchedInboundMessage.__table__.drop(bind=db.connection(), checkfirst=True)
    logger.info("✓ Dropped %s", TABLE)
    return True


def verify(db) -> bool:
    insp = inspect(db.connection())
    if not insp.has_table(TABLE):
        logger.error("✗ %s missing", TABLE)
        return False
    uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints(TABLE)}
    uniques |= {tuple(i["column_names"]) for i in insp.get_indexes(TABLE) if i.get("unique")}
    ok = ("mailbox_id", "message_id_header") in uniques
    logger.info("✓ %s present with the expected unique constraint" if ok else "✗ %s unique constraint missing", TABLE)
    return ok


def run() -> bool:
    logger.info("Starting migration 011: unmatched_inbound_messages")
    with session_scope() as db:
        try:
            create_table(db)
            db.commit()
            ok = verify(db)
            logger.info("✅ Migration 011 completed" if ok else "⚠️ Migration 011 verification failed")
            return ok
        except Exception:
            logger.exception("Migration 011 failed")
            db.rollback()
            raise


def downgrade() -> bool:
    logger.info("Starting migration 011 DOWNGRADE")
    with session_scope() as db:
        try:
            drop_table(db)
            db.commit()
            return True
        except Exception:
            logger.exception("Migration 011 downgrade failed")
            db.rollback()
            raise


if __name__ == "__main__":
    try:
        ok = downgrade() if "--downgrade" in sys.argv[1:] else run()
        sys.exit(0 if ok else 1)
    except Exception:
        logger.exception("Unhandled error during migration 011")
        sys.exit(1)
