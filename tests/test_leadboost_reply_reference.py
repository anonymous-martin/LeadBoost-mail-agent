"""
v4 -- the opaque reply reference (Message.reply_reference) and the reply-parent checks that depend on it.

Covers: token generation and stability, both inbound persistence paths, the conversation view (inbound-only
exposure), request validation, tenant / mailbox / direction / recipient scoping, refusal BEFORE acceptance
when the parent cannot be threaded (synthetic id and no usable In-Reply-To), synthetic ids never reaching
RFC headers, idempotency binding, and migration 012 on SQLite (backfill, stability, idempotence).

SQLite only: nothing here needs PostgreSQL. (The PostgreSQL migration wrapper test lives in
tests/test_migrate_release.py and is deselected unless POSTGRES_TEST_URL is set.)
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, insert, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mailer_agent.api.deps import get_current_org_id, get_integration_org_id, require_api_key
from mailer_agent.api.main import app
from mailer_agent.config import get_settings
from mailer_agent.db import get_db
from mailer_agent.mail import reply_handler_v2 as rh
from mailer_agent.mail.imap_reader import InboundEmail
from mailer_agent.models import (
    Base,
    Contact,
    ExternalDispatch,
    Mailbox,
    Message,
    UnmatchedInboundMessage,
)
from tests.dispatch_support import seed_dispatch as _seed_dispatch, seed_org_mailboxes

ORG_A, ORG_B = "org-a", "org-b"
HEX32 = re.compile(r"^[0-9a-f]{32}$")
REQ_URL = "/integrations/leadboost/outreach-requests"
CONV = "/integrations/leadboost/outreach-actions/{key}/conversation"
LEAD = "lead@example.com"
REAL_OUT = "<out-1@sender.example>"
REAL_IN = "<in-1@prospect.example>"
SYN = "synthetic:" + "ab" * 32


# ------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def _sender_identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "leadboost_integration_sender_email", "outreach@mailer.example.com")
    monkeypatch.setattr(s, "leadboost_integration_sender_name", "Test Sender")
    monkeypatch.setattr(s, "leadboost_integration_sender_org", "Test Org")


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    session = sessionmaker(bind=eng)()
    seed_org_mailboxes(session)
    try:
        yield session
    finally:
        session.close()
        eng.dispose()


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_api_key] = lambda: None
    app.dependency_overrides[get_integration_org_id] = lambda: ORG_A
    app.dependency_overrides[get_current_org_id] = lambda: ORG_A
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def seed_dispatch(db, **kw):
    """dispatch_support.seed_dispatch, bound to the org's ALREADY-seeded mailbox (seed_org_mailboxes) so each
    org keeps exactly one ACTIVE mailbox -- the precondition of POST /outreach-requests."""
    kw.setdefault("sender_email", "outreach@mailer.example.com")
    return _seed_dispatch(db, **kw)


def _mailbox_id(db, org=ORG_A) -> int:
    return db.query(Mailbox).filter(Mailbox.organization_id == org).one().id


def _inbound(db, contact_id, *, org=ORG_A, mailbox=True, direction="inbound", message_id=REAL_IN,
             in_reply_to=REAL_OUT, references=None, subject="Hello") -> Message:
    m = Message(
        contact_id=contact_id, direction=direction, message_type=None, subject=subject, body="Tell me more.",
        status="received", mailbox_id=_mailbox_id(db, org) if mailbox else None,
        message_id_header=message_id, in_reply_to_header=in_reply_to, references_header=references,
    )
    db.add(m)
    db.commit()
    return m


def _world(db, **inbound_kw):
    d = seed_dispatch(db, org=ORG_A, idem="seed-1", email=LEAD)
    return d, _inbound(db, d.contact_id, **inbound_kw)


def _req(ref, *, idem="reply-1", email=LEAD, action="481"):
    body = {
        "external_action_id": action,
        "idempotency_key": idem,
        "recipient": {"email": email, "name": "Jane", "title": None, "company": None},
        "context": {"value_proposition": "We cut reconciliation time by 40%.", "recipient_facts": []},
    }
    if ref is not None:
        body["in_reply_to"] = {"message_reference": ref}
    return body


def _count(db, model):
    return db.query(func.count(model.id)).scalar()


# ------------------------------------------------------------------ token generation and stability
def test_orm_created_messages_get_unique_32_hex_tokens(db):
    d = seed_dispatch(db, org=ORG_A, idem="t1", email=LEAD)
    a, b = _inbound(db, d.contact_id, message_id="<a@x>"), _inbound(db, d.contact_id, message_id="<b@x>")
    assert HEX32.fullmatch(a.reply_reference) and HEX32.fullmatch(b.reply_reference)
    assert a.reply_reference != b.reply_reference
    assert a.reply_reference != str(a.id)
    # the outbound seeded by seed_dispatch was created through the ORM too
    assert HEX32.fullmatch(db.get(Message, d.message_id).reply_reference)


def test_token_is_stable_across_reads_and_updates(db):
    _, m = _world(db)
    first = m.reply_reference
    m.status = "classified"
    db.commit()
    db.expire_all()
    assert db.get(Message, m.id).reply_reference == first


def test_core_insert_without_an_explicit_value_still_gets_the_column_default(db):
    d = seed_dispatch(db, org=ORG_A, idem="t2", email=LEAD)
    new_id = db.execute(
        insert(Message).values(contact_id=d.contact_id, direction="inbound", body="x", status="received")
        .returning(Message.id)
    ).scalar_one()
    assert HEX32.fullmatch(db.get(Message, new_id).reply_reference)


def test_unmatched_inbound_storage_has_no_reply_reference_column():
    assert "reply_reference" not in UnmatchedInboundMessage.__table__.c


def test_unique_index_exists_on_the_model_and_rejects_a_duplicate(db):
    names = {i["name"]: i for i in inspect(db.get_bind()).get_indexes("messages")}
    assert names["uq_messages_reply_reference"]["unique"]
    d = seed_dispatch(db, org=ORG_A, idem="t3", email=LEAD)
    m = _inbound(db, d.contact_id)
    dup = Message(contact_id=d.contact_id, direction="inbound", body="y", status="received",
                  reply_reference=m.reply_reference)
    db.add(dup)
    with pytest.raises(Exception) as ei:
        db.flush()
    assert "UNIQUE" in str(ei.value).upper()
    db.rollback()


# ------------------------------------------------------------------ both inbound persistence paths
def test_process_inbound_email_v2_persists_a_token(db, monkeypatch):
    import mailer_agent.llm.provider_v2 as provider_module
    monkeypatch.setattr(provider_module, "is_llm_available", lambda: False)
    d = seed_dispatch(db, org=ORG_A, idem="p1", email=LEAD)
    db.get(Message, d.message_id).message_id_header = rh._normalize_message_id(REAL_OUT)
    db.commit()
    mail = InboundEmail(from_email=LEAD, subject="Re: hi", body_text="Thanks, tell me more.",
                        message_id="<v2-1@prospect.example>", in_reply_to=REAL_OUT, references=[], to_email=None)
    r = rh.process_inbound_email_v2(db, mail, organization_id=None)
    db.commit()
    assert r["matched"] is True
    stored = db.query(Message).filter(Message.direction == "inbound").one()
    assert HEX32.fullmatch(stored.reply_reference)


def test_insert_correlated_message_sets_a_token_and_a_duplicate_keeps_the_original(db):
    d = seed_dispatch(db, org=ORG_A, idem="p2", email=LEAD)
    mb = _mailbox_id(db)
    mail = InboundEmail(from_email=LEAD, subject="Re: hi", body_text="b", message_id="<c-1@prospect.example>",
                        in_reply_to=REAL_OUT, references=[], to_email=None)
    kw = dict(contact_id=d.contact_id, identity="<c-1@prospect.example>", mailbox_id=mb, organization_id=ORG_A)
    first, inserted = rh._insert_correlated_message(db, mail, **kw)
    db.commit()
    assert inserted and HEX32.fullmatch(first.reply_reference)
    token = first.reply_reference
    again, inserted2 = rh._insert_correlated_message(db, mail, **kw)
    db.commit()
    assert not inserted2 and again.id == first.id and again.reply_reference == token  # retry never regenerates


# ------------------------------------------------------------------ conversation view
def test_view_exposes_the_token_for_inbound_only_and_never_the_id(client, db):
    d, m = _world(db)
    r = client.get(CONV.format(key=d.idempotency_key))
    assert r.status_code == 200
    msgs = r.json()["messages"]
    inbound = [x for x in msgs if x["direction"] == "inbound"]
    outbound = [x for x in msgs if x["direction"] == "outbound"]
    assert [x["message_reference"] for x in inbound] == [m.reply_reference]
    assert HEX32.fullmatch(inbound[0]["message_reference"]) and inbound[0]["message_reference"] != str(m.id)
    assert outbound and all(x["message_reference"] is None for x in outbound)


# ------------------------------------------------------------------ request validation
@pytest.mark.parametrize("bad", ["2", "00000000000000000002", "A" * 32, "g" * 32, "a" * 31, "a" * 33, " " + "a" * 31, ""])
def test_malformed_references_are_rejected_before_any_work(client, db, bad):
    _world(db)
    before = _count(db, ExternalDispatch)
    r = client.post(REQ_URL, json=_req(bad))
    assert r.status_code == 422
    assert _count(db, ExternalDispatch) == before


def test_extra_fields_inside_in_reply_to_are_rejected(client, db):
    _, m = _world(db)
    body = _req(m.reply_reference)
    body["in_reply_to"]["in_reply_to_header"] = "<forged@x>"
    assert client.post(REQ_URL, json=body).status_code == 422


# ------------------------------------------------------------------ scoping
def test_valid_reference_is_accepted_and_snapshots_real_threading(client, db):
    d, m = _world(db, references="<root@x> " + REAL_OUT)
    before = _count(db, ExternalDispatch)
    r = client.post(REQ_URL, json=_req(m.reply_reference))
    assert r.status_code == 202, r.text
    assert _count(db, ExternalDispatch) == before + 1
    snap = db.query(ExternalDispatch).filter_by(idempotency_key="reply-1").one().grounding_context["reply_parent"]
    assert snap["message_id"] == m.id
    assert snap["in_reply_to_header"] == REAL_IN
    assert snap["subject"] == "Re: Hello"
    assert snap["references_header"].split() == ["<root@x>", REAL_OUT, REAL_IN]


def test_unknown_outbound_foreign_tenant_and_null_mailbox_are_the_same_404(client, db):
    d, inbound = _world(db)
    outbound = db.get(Message, d.message_id)
    foreign_d = seed_dispatch(db, org=ORG_B, idem="b-1", email=LEAD)
    foreign = _inbound(db, foreign_d.contact_id, org=ORG_B, message_id="<b-in@x>")
    webhook = _inbound(db, d.contact_id, mailbox=False, message_id="<wh@x>")
    # An OUTBOUND row that (unrealistically) carries this org's mailbox: only the direction predicate stops it,
    # so the direction check is proven on its own and not by the mailbox join happening to exclude outbound rows.
    outbound_in_mailbox = _inbound(db, d.contact_id, direction="outbound", message_id="<o-mb@x>")
    before = _count(db, ExternalDispatch)
    bodies = set()
    for ref in ("f" * 32, outbound.reply_reference, outbound_in_mailbox.reply_reference,
                foreign.reply_reference, webhook.reply_reference):
        r = client.post(REQ_URL, json=_req(ref, idem=f"k-{ref[:4]}"))
        assert r.status_code == 404, (ref, r.text)
        bodies.add(r.text)
    assert len(bodies) == 1, "every refusal must be indistinguishable"
    assert _count(db, ExternalDispatch) == before


def test_recipient_must_be_the_sender_of_the_parent(client, db):
    _, m = _world(db)
    before = _count(db, ExternalDispatch)
    r = client.post(REQ_URL, json=_req(m.reply_reference, email="someone-else@example.com"))
    assert r.status_code == 409
    assert _count(db, ExternalDispatch) == before


# ------------------------------------------------------------------ unthreadable parents are refused BEFORE acceptance
def test_synthetic_id_and_no_in_reply_to_is_409_and_creates_nothing(client, db):
    _, m = _world(db, message_id=SYN, in_reply_to=None)
    counts = (_count(db, ExternalDispatch), _count(db, Message), _count(db, Contact))
    r = client.post(REQ_URL, json=_req(m.reply_reference))
    assert r.status_code == 409
    assert (_count(db, ExternalDispatch), _count(db, Message), _count(db, Contact)) == counts


@pytest.mark.parametrize("irt", [SYN, "has whitespace", "x" * 1000])
def test_unusable_in_reply_to_fallbacks_are_also_409(client, db, irt):
    _, m = _world(db, message_id=SYN, in_reply_to=irt)
    before = _count(db, ExternalDispatch)
    assert client.post(REQ_URL, json=_req(m.reply_reference)).status_code == 409
    assert _count(db, ExternalDispatch) == before


def test_synthetic_parent_with_a_real_in_reply_to_threads_to_it_and_no_header_carries_a_synthetic_value(client, db):
    _, m = _world(db, message_id=SYN, in_reply_to=REAL_OUT, references=f"<root@x> {SYN} {REAL_OUT}")
    r = client.post(REQ_URL, json=_req(m.reply_reference))
    assert r.status_code == 202, r.text
    snap = db.query(ExternalDispatch).filter_by(idempotency_key="reply-1").one().grounding_context["reply_parent"]
    assert snap["in_reply_to_header"] == REAL_OUT
    assert "synthetic:" not in snap["in_reply_to_header"] + snap["references_header"]
    assert snap["references_header"].split() == ["<root@x>", REAL_OUT]


# ------------------------------------------------------------------ idempotency
def test_same_key_same_reference_replays_and_a_different_reference_conflicts(client, db):
    d, m1 = _world(db)
    m2 = _inbound(db, d.contact_id, message_id="<in-2@prospect.example>")
    first = client.post(REQ_URL, json=_req(m1.reply_reference))
    assert first.status_code == 202
    n = _count(db, ExternalDispatch)
    replay = client.post(REQ_URL, json=_req(m1.reply_reference))
    assert replay.status_code == 202 and replay.json() == first.json()
    assert _count(db, ExternalDispatch) == n
    conflict = client.post(REQ_URL, json=_req(m2.reply_reference))
    assert conflict.status_code == 409
    assert _count(db, ExternalDispatch) == n


# ------------------------------------------------------------------ migration 012 (SQLite)
def _load_migration():
    path = Path(__file__).resolve().parent.parent / "migrations" / "012_message_reply_reference.py"
    spec = importlib.util.spec_from_file_location("migration_012", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def legacy_db():
    """A messages table as it was before 012: no reply_reference column, no index, with rows."""
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("needs SQLite >= 3.35 for ALTER TABLE DROP COLUMN")
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    s = sessionmaker(bind=eng)()
    seed_org_mailboxes(s)
    d = seed_dispatch(s, org=ORG_A, idem="m-1", email=LEAD)
    for i in range(7):
        _inbound(s, d.contact_id, message_id=f"<legacy-{i}@x>")
    s.execute(text("DROP INDEX uq_messages_reply_reference"))
    s.execute(text("ALTER TABLE messages DROP COLUMN reply_reference"))
    s.commit()
    assert "reply_reference" not in {c["name"] for c in inspect(s.connection()).get_columns("messages")}
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _tokens(s):
    return dict(s.execute(text("SELECT id, reply_reference FROM messages")).fetchall())


def test_migration_012_backfills_unique_tokens_creates_the_index_and_is_idempotent(legacy_db):
    mig = _load_migration()
    s = legacy_db
    n_rows = s.execute(text("SELECT COUNT(*) FROM messages")).scalar()
    mig.BATCH_SIZE = 3  # force several batches

    assert mig.add_column(s) is True
    s.commit()
    assert mig.backfill(s) == n_rows
    assert mig.create_index(s) is True
    s.commit()
    assert mig.verify(s) is True

    first = _tokens(s)
    assert len(first) == n_rows and all(t and HEX32.fullmatch(t) for t in first.values())
    assert len(set(first.values())) == n_rows, "tokens must be unique"

    # a second full run changes nothing: tokens are stable
    assert mig.add_column(s) is False
    assert mig.backfill(s) == 0
    assert mig.create_index(s) is False
    assert mig.verify(s) is True
    assert _tokens(s) == first


def test_migration_012_never_overwrites_an_existing_token_and_fills_late_nulls(legacy_db):
    mig = _load_migration()
    s = legacy_db
    mig.add_column(s)
    s.commit()
    some_id = s.execute(text("SELECT MIN(id) FROM messages")).scalar()
    keep = "0123456789abcdef0123456789abcdef"
    s.execute(text("UPDATE messages SET reply_reference = :t WHERE id = :i"), {"t": keep, "i": some_id})
    s.commit()
    mig.backfill(s)
    mig.create_index(s)
    s.commit()
    assert _tokens(s)[some_id] == keep

    # older code inserts a row WITHOUT a token after the index exists: NULL is allowed...
    s.execute(text("INSERT INTO messages (contact_id, direction, body, status) "
                   "SELECT contact_id, 'inbound', 'late', 'received' FROM messages LIMIT 1"))
    s.commit()
    assert mig.verify(s) is False          # ...but verify flags it
    assert mig.backfill(s) == 1            # ...and a re-run fills exactly that row
    assert mig.verify(s) is True
    assert _tokens(s)[some_id] == keep


def test_migration_012_verify_rejects_a_non_unique_index(legacy_db):
    mig = _load_migration()
    s = legacy_db
    mig.add_column(s)
    mig.backfill(s)
    s.execute(text("CREATE INDEX uq_messages_reply_reference ON messages (reply_reference)"))  # wrong: not UNIQUE
    s.commit()
    assert mig.verify(s) is False


def test_migration_012_downgrade_removes_index_and_column(legacy_db):
    mig = _load_migration()
    s = legacy_db
    mig.add_column(s)
    mig.backfill(s)
    mig.create_index(s)
    s.commit()
    assert mig._index_exists(s) and mig._column_exists(s)
    s.execute(text(f"DROP INDEX {mig.INDEX}"))
    s.execute(text(f"ALTER TABLE {mig.TABLE} DROP COLUMN {mig.COLUMN}"))
    s.commit()
    assert not mig._index_exists(s) and not mig._column_exists(s)
