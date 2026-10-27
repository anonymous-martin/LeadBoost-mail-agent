"""
Real-PostgreSQL tests for mailbox inbound persistence in mail/reply_handler_v2.py:
  * unmatched/ambiguous retention (``_retain_unmatched``), and
  * correlated-message insertion (``_insert_correlated_message``, arbiter: the PARTIAL unique index
    ``uq_messages_mailbox_message_id`` ... WHERE mailbox_id IS NOT NULL).

What SQLite cannot show and these do: a genuinely concurrent insert of the same message (the second INSERT
blocks on the first transaction's uncommitted row, and the duplicate is accepted only once the committed row
has been verified), what happens when the first transaction rolls back instead of committing, that
``ON CONFLICT ... DO NOTHING`` absorbs ONLY the duplicate (NOT NULL and foreign-key violations still raise,
each with its own SQLSTATE), that PostgreSQL accepts the partial-index conflict target, that a conflicting row
which is not this tenant's inbound message is refused, and what REPEATABLE READ does.

Safety (tests/pg_target_guard.py; the logic is unit-tested without a database in test_pg_target_guard.py):
this module writes -- additive CREATE TABLE IF NOT EXISTS plus inserts/deletes of its OWN rows -- so it only
runs when ALL hold: MAILER_PG_TEST_OPT_IN=1; MAILER_PG_TEST_EXPECTED_DB names the database and contains "test";
POSTGRES_TEST_URL is a PostgreSQL URL for exactly that database on a loopback host with no host override; and
the server itself reports that database name before anything is written. A configured-but-wrong target FAILS
(before any connection); an unconfigured or unreachable one skips locally and FAILS when
REQUIRE_UNMATCHED_INBOUND_PG=1 (set by the CI job), so it can never "pass" by skipping there.
It never TRUNCATEs, drops or resets; every row it writes lives under a unique per-run organization and only
those rows are deleted at the end.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from mailer_agent.mail import reply_handler_v2 as rh
from mailer_agent.mail.imap_reader import InboundEmail
from mailer_agent.models import Base, Campaign, Contact, Mailbox, Message
from tests.pg_target_guard import assess_pg_target

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")
_REQUIRED = os.environ.get("REQUIRE_UNMATCHED_INBOUND_PG") == "1"


def _unavailable(reason: str):
    if _REQUIRED:
        pytest.fail(f"REQUIRE_UNMATCHED_INBOUND_PG=1 but the PostgreSQL target is not usable: {reason}")
    pytest.skip(reason)


@pytest.fixture
def pg():
    expected = os.environ.get("MAILER_PG_TEST_EXPECTED_DB")
    decision = assess_pg_target(
        POSTGRES_TEST_URL, opt_in=os.environ.get("MAILER_PG_TEST_OPT_IN"), expected_db=expected)
    if decision.verdict == "refuse":                          # before any connection is opened
        pytest.fail(f"refusing to run against this PostgreSQL target: {decision.reason}")
    if decision.verdict == "unavailable":
        _unavailable(decision.reason)
    eng = create_engine(POSTGRES_TEST_URL, pool_size=10)
    try:
        with eng.connect() as c:
            actual = c.execute(text("SELECT current_database()")).scalar()
    except Exception as exc:  # noqa: BLE001 - any connection failure means "not usable"
        eng.dispose()
        _unavailable(f"PostgreSQL is unreachable ({type(exc).__name__})")
    if actual != expected:                                     # the server's own answer, before any write
        eng.dispose()
        pytest.fail(f"refusing to run: the server reports database {actual!r}, expected {expected!r}")

    Base.metadata.create_all(bind=eng)                         # additive only
    sm = sessionmaker(bind=eng, autoflush=False)
    run = uuid.uuid4().hex[:10]
    org, other_org = f"org-unmatched-pg-{run}", f"org-unmatched-pg-{run}-other"
    with sm() as db:
        mb = Mailbox(
            public_reference=f"ref-{run}", organization_id=org, email_address=f"mb-{run}@test.example",
            smtp_host="smtp.test.example", smtp_port=587, smtp_use_tls=True, smtp_username="u",
            smtp_password_enc="x",
        )
        camp = Campaign(name="pg", organization_id=org, sender_name="n", sender_org="o",
                        sender_email=f"s-{run}@test.example", value_prop="v")
        camp_other = Campaign(name="pg-other", organization_id=other_org, sender_name="n", sender_org="o",
                              sender_email=f"s2-{run}@test.example", value_prop="v")
        db.add_all([mb, camp, camp_other])
        db.flush()
        contact = Contact(campaign_id=camp.id, email=f"lead-{run}@test.example")
        other_contact = Contact(campaign_id=camp_other.id, email=f"other-{run}@test.example")
        db.add_all([contact, other_contact])
        db.commit()
        ns = SimpleNamespace(
            eng=eng, sm=sm, run=run, org=org, other_org=other_org, mailbox_id=mb.id,
            campaign_ids=(camp.id, camp_other.id), contact_id=contact.id, contact_email=contact.email,
            other_contact_id=other_contact.id,
        )
    yield ns
    with eng.begin() as c:                                    # only this run's own rows
        p = {"o": org, "o2": other_org, "m": ns.mailbox_id, "c1": ns.contact_id, "c2": ns.other_contact_id,
             "k1": ns.campaign_ids[0], "k2": ns.campaign_ids[1]}
        c.execute(text("DELETE FROM unmatched_inbound_messages WHERE organization_id IN (:o, :o2) OR mailbox_id = :m"), p)
        c.execute(text("DELETE FROM messages WHERE contact_id IN (:c1, :c2) OR mailbox_id = :m"), p)
        c.execute(text("DELETE FROM contacts WHERE id IN (:c1, :c2)"), p)
        c.execute(text("DELETE FROM campaigns WHERE id IN (:k1, :k2)"), p)
        c.execute(text("DELETE FROM mailboxes WHERE id = :m AND organization_id = :o"), p)
    eng.dispose()


def _email(identity: str, body: str = "BODY") -> InboundEmail:
    return InboundEmail(
        from_email="stranger@nowhere.test", subject="Re: x", body_text=body,
        message_id=identity, in_reply_to=None, references=[],
    )


def _retain(db, pg, identity, *, reason="no_match", mailbox_id=None, organization_id=None, body="BODY"):
    rh._retain_unmatched(
        db, _email(identity, body), identity=identity,
        mailbox_id=pg.mailbox_id if mailbox_id is None else mailbox_id,
        organization_id=pg.org if organization_id is None else organization_id, reason=reason,
    )


def _count(pg, identity) -> int:
    with pg.eng.connect() as c:
        return c.execute(
            text("SELECT count(*) FROM unmatched_inbound_messages WHERE mailbox_id = :m AND message_id_header = :i"),
            {"m": pg.mailbox_id, "i": identity},
        ).scalar()


def _wait_until_another_backend_waits_on_a_lock(pg, timeout=15.0) -> None:
    """Prove the second INSERT is really blocked behind the first (uncommitted) one."""
    end = time.time() + timeout
    while time.time() < end:
        with pg.eng.connect() as c:
            waiting = c.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()")).scalar()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("the second insert never blocked on the first transaction's row")


def _second_writer(pg, identity, result):
    s = pg.sm()
    try:
        _retain(s, pg, identity)
        s.commit()
        result["ok"] = True
    except Exception as e:  # noqa: BLE001 - the test inspects exactly what the writer raised
        s.rollback()
        result["err"] = e
    finally:
        s.close()


def test_concurrent_retention_of_the_same_message_stores_one_row_and_neither_writer_fails(pg):
    identity = f"<conc-{pg.run}@p>"
    first = pg.sm()
    _retain(first, pg, identity)                              # uncommitted row held by transaction 1
    result: dict = {}
    t = threading.Thread(target=_second_writer, args=(pg, identity, result))
    t.start()
    _wait_until_another_backend_waits_on_a_lock(pg)           # transaction 2 is blocked on it
    first.commit()
    first.close()
    t.join(20)
    assert not t.is_alive()
    assert result == {"ok": True}                             # the duplicate was confirmed, not an error
    assert _count(pg, identity) == 1                          # one row per (mailbox, Message-ID)


def test_when_the_first_writer_rolls_back_the_second_one_really_inserts_the_row(pg):
    """The duplicate is only ever accepted if a row exists: with no winner, the loser must store it."""
    identity = f"<rb-{pg.run}@p>"
    first = pg.sm()
    _retain(first, pg, identity)
    result: dict = {}
    t = threading.Thread(target=_second_writer, args=(pg, identity, result))
    t.start()
    _wait_until_another_backend_waits_on_a_lock(pg)
    first.rollback()                                          # the "winner" never became durable
    first.close()
    t.join(20)
    assert not t.is_alive() and result == {"ok": True}
    assert _count(pg, identity) == 1                          # present because the second writer stored it


def test_not_null_violation_propagates_with_its_own_sqlstate_and_stores_nothing(pg):
    identity = f"<nn-{pg.run}@p>"
    db = pg.sm()
    with pytest.raises(IntegrityError) as ei:
        _retain(db, pg, identity, reason=None)
    assert ei.value.orig.pgcode == "23502"                    # not_null_violation -- not 23505 (the duplicate)
    db.rollback()
    db.close()
    assert _count(pg, identity) == 0


def test_foreign_key_violation_propagates_with_its_own_sqlstate_and_stores_nothing(pg):
    identity = f"<fk-{pg.run}@p>"
    db = pg.sm()
    with pytest.raises(IntegrityError) as ei:
        _retain(db, pg, identity, mailbox_id=-1)              # no such mailbox
    assert ei.value.orig.pgcode == "23503"                    # foreign_key_violation
    db.rollback()
    db.close()
    with pg.eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM unmatched_inbound_messages WHERE mailbox_id = -1")).scalar() == 0


def test_a_conflicting_row_for_another_organization_is_not_accepted_as_a_duplicate(pg):
    identity = f"<org-{pg.run}@p>"
    seed = pg.sm()
    _retain(seed, pg, identity, organization_id=f"{pg.org}-other", body="SEEDED")
    seed.commit()
    seed.close()
    db = pg.sm()
    with pytest.raises(rh.RetentionNotConfirmedError):
        _retain(db, pg, identity)
    db.rollback()
    db.close()
    with pg.eng.connect() as c:
        rows = c.execute(text("SELECT organization_id, body FROM unmatched_inbound_messages "
                              "WHERE mailbox_id = :m AND message_id_header = :i"),
                         {"m": pg.mailbox_id, "i": identity}).all()
    assert rows == [(f"{pg.org}-other", "SEEDED")]            # untouched, no second row


def test_under_repeatable_read_an_invisible_conflict_raises_and_is_never_taken_for_a_duplicate(pg):
    """REPEATABLE READ only (the service runs READ COMMITTED). A conflicting row committed after this
    transaction's snapshot is invisible to it; PostgreSQL then refuses the insert with a serialization
    failure (SQLSTATE 40001) instead of skipping it silently. Either way the handler must not report
    success, and here it propagates: the poller leaves the message Unseen and the next poll retries."""
    identity = f"<rr-{pg.run}@p>"
    rr = pg.eng.connect().execution_options(isolation_level="REPEATABLE READ")
    try:
        rr.execute(text("SELECT 1")).scalar()                 # take the snapshot first
        other = pg.sm()
        _retain(other, pg, identity)
        other.commit()                                        # committed AFTER the snapshot
        other.close()
        db = pg.sm(bind=rr)
        with pytest.raises(OperationalError) as ei:
            _retain(db, pg, identity)
        assert ei.value.orig.pgcode == "40001"                # serialization_failure
        db.close()
    finally:
        rr.rollback()
        rr.close()
    assert _count(pg, identity) == 1                          # the other writer's row; nothing doubled


# ---------------------------------------------------------------------------------------------
# Correlated replies: Message insert on the partial unique index uq_messages_mailbox_message_id
# ---------------------------------------------------------------------------------------------

def _reply(pg, identity, body="REPLY"):
    return InboundEmail(from_email=pg.contact_email, subject="Re: hello", body_text=body,
                        message_id=identity, in_reply_to=None, references=[])


def _handle(db, pg, identity):
    return rh.process_mailbox_inbound(db, _reply(pg, identity), mailbox_id=pg.mailbox_id, organization_id=pg.org)


def _insert(db, pg, identity, *, contact_id=None, email=None):
    return rh._insert_correlated_message(
        db, email or _reply(pg, identity), contact_id=pg.contact_id if contact_id is None else contact_id,
        identity=identity, mailbox_id=pg.mailbox_id, organization_id=pg.org)


def _message_ids(pg, identity) -> list[int]:
    with pg.eng.connect() as c:
        return [r[0] for r in c.execute(
            text("SELECT id FROM messages WHERE mailbox_id = :m AND message_id_header = :i ORDER BY id"),
            {"m": pg.mailbox_id, "i": identity})]


def _second_handler(pg, identity, result):
    s = pg.sm()
    try:
        result["res"] = _handle(s, pg, identity)
        s.commit()
    except Exception as e:  # noqa: BLE001 - the test inspects exactly what the handler raised
        s.rollback()
        result["err"] = e
    finally:
        s.close()


def test_correlated_insert_conflict_target_matches_the_partial_unique_index_on_postgresql(pg):
    ident = f"<tgt-{pg.run}@p>"
    db = pg.sm()
    msg, inserted = _insert(db, pg, ident)
    msg_id = msg.id
    assert inserted and msg_id
    again, inserted_again = _insert(db, pg, ident)            # same transaction, same key: conflict absorbed
    assert not inserted_again and again.id == msg_id          # ... and the exact row verified
    db.commit()
    db.close()
    assert _message_ids(pg, ident) == [msg_id]


def test_concurrent_correlated_duplicate_is_accepted_only_after_the_committed_row_is_verified(pg):
    ident = f"<cc-{pg.run}@p>"
    first = pg.sm()
    r1 = _handle(first, pg, ident)                            # inserted and classified; NOT committed yet
    assert r1.get("action") != "skipped_duplicate"
    result: dict = {}
    t = threading.Thread(target=_second_handler, args=(pg, ident, result))
    t.start()
    _wait_until_another_backend_waits_on_a_lock(pg)           # the second INSERT is blocked on the first
    first.commit()
    first.close()
    t.join(20)
    assert not t.is_alive() and "err" not in result, result.get("err")
    ids = _message_ids(pg, ident)
    assert len(ids) == 1                                      # one row per (mailbox, Message-ID)
    assert result["res"]["action"] == "skipped_duplicate" and result["res"]["matched"] is True
    assert result["res"]["message_id"] == ids[0]              # the verified, committed row


def test_when_the_first_correlated_writer_rolls_back_the_second_really_stores_the_message(pg):
    ident = f"<crb-{pg.run}@p>"
    first = pg.sm()
    _handle(first, pg, ident)
    result: dict = {}
    t = threading.Thread(target=_second_handler, args=(pg, ident, result))
    t.start()
    _wait_until_another_backend_waits_on_a_lock(pg)
    first.rollback()                                          # the "winner" never became durable
    first.close()
    t.join(20)
    assert not t.is_alive() and "err" not in result, result.get("err")
    assert result["res"].get("action") != "skipped_duplicate"  # it was NOT reported as a duplicate ...
    assert len(_message_ids(pg, ident)) == 1                  # ... because it stored the row itself


def test_correlated_not_null_violation_propagates_with_its_own_sqlstate_and_stores_nothing(pg):
    ident = f"<cnn-{pg.run}@p>"
    db = pg.sm()
    with pytest.raises(IntegrityError) as ei:                 # through the handler: it must not turn this into a result
        rh.process_mailbox_inbound(
            db, dataclasses.replace(_reply(pg, ident), body_text=None),
            mailbox_id=pg.mailbox_id, organization_id=pg.org)
    assert ei.value.orig.pgcode == "23502"                    # not_null_violation, not 23505 (the duplicate)
    db.rollback()
    db.close()
    assert _message_ids(pg, ident) == []


def test_correlated_foreign_key_violation_propagates_with_its_own_sqlstate_and_stores_nothing(pg):
    ident = f"<cfk-{pg.run}@p>"
    db = pg.sm()
    with pytest.raises(IntegrityError) as ei:
        _insert(db, pg, ident, contact_id=-1)                 # no such contact
    assert ei.value.orig.pgcode == "23503"                    # foreign_key_violation
    db.rollback()
    db.close()
    assert _message_ids(pg, ident) == []


@pytest.mark.parametrize("seed", ["other_tenants_contact", "outbound_row"])
def test_a_conflicting_row_that_is_not_this_tenants_inbound_message_is_never_a_duplicate(pg, seed):
    ident = f"<cx-{seed}-{pg.run}@p>"
    contact_id = pg.other_contact_id if seed == "other_tenants_contact" else pg.contact_id
    direction = "inbound" if seed == "other_tenants_contact" else "outbound"
    with pg.sm() as db:
        db.add(Message(contact_id=contact_id, mailbox_id=pg.mailbox_id, direction=direction,
                       body="SEEDED", status="received", message_id_header=ident))
        db.commit()
    db = pg.sm()
    result, raised = None, None
    try:
        result = _handle(db, pg, ident)                       # through the handler
    except Exception as exc:  # noqa: BLE001 - asserted precisely below
        raised = exc
    db.rollback()
    db.close()
    assert raised is not None, f"a conflicting row that is not this tenant's inbound message was accepted: {result!r}"
    assert type(raised).__name__ == "MessagePersistenceNotConfirmedError", repr(raised)
    assert len(_message_ids(pg, ident)) == 1                  # only the seeded row; nothing doubled or replaced
