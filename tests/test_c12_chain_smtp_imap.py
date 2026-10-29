"""
C12 -- the complete LeadBoost-driven lifecycle on the Mailer side, in ONE flow,
with real protocols at every external boundary and the production code on every
Mailer path. Nothing in the chain is seeded straight into the database.

    POST /mailboxes (SMTP only, as LeadBoost provisions)           real HTTP handler, real auth
    PATCH /mailboxes/{ref} (IMAP set -- the approved STAGING-ONLY   real handler
        operator step) + LeadBoost-style SMTP/activation PATCH
    POST /integrations/leadboost/outreach-requests                 real handler, real X-API-Key auth
    run_generation_cycle                                           real worker (LLM = the existing fake)
    run_external_dispatch_cycle                                    real worker -> sender.send_email
        -> smtplib over STARTTLS + AUTH to a disposable aiosmtpd   real SMTP wire, real credential use
    reply APPENDed to a disposable Dovecot mailbox                 real IMAP server (implicit TLS)
    mailbox_inbound.poll_all_mailboxes                             real M3 poll (UID FETCH BODY.PEEK/STORE)
    GET .../outreach-actions/{key}/conversation                    real C9.3 endpoint

What it adds over the existing tests: test_m2b has the accept->generate->send
chain but with a fake sender; test_smtp_local_integration drives sender.send_email
alone; test_m3_imap_e2e injects mail that Mailer never sent. None connected the
Message-ID Mailer persisted and put on the wire to the In-Reply-To of a reply that
M3 correlated through a real mailbox and C9.3 then returned. Only the LLM is
faked (deterministic generation, no production LLM call), as everywhere else.

Skipped unless the `dovecot` binary exists and the process is root (the same
condition as test_m3_imap_e2e, whose Dovecot fixture and trust setup are reused;
importing that module is what skips this one). Runs on PostgreSQL when
POSTGRES_TEST_URL is set (then C9.3 runs in its REPEATABLE READ / READ ONLY
transaction and the "no open DB transaction during SMTP" check is real),
otherwise on a SQLite file.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import uuid
from email import message_from_bytes
from types import SimpleNamespace

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from mailer_agent.api import deps
from mailer_agent.api.main import app
from mailer_agent.config import get_settings
from mailer_agent.db import get_db
from mailer_agent.mail import external_dispatch_worker as dispatch_worker
from mailer_agent.mail import mailbox_inbound
from mailer_agent.mail import outreach_generation_worker as generation_worker
from mailer_agent.models import (
    Base,
    Contact,
    ExternalDispatch,
    ExternalDispatchState,
    Mailbox,
    Message,
)
from tests.fake_llm_provider import install_fake_llm_provider
from tests.m3_support import raw_email
from tests.test_m3_imap_e2e import (
    dovecot,  # noqa: F401  (reused fixture; importing it also skips this module when dovecot/root are absent)
)

pytestmark = [pytest.mark.integration, pytest.mark.imap_e2e]

ORG, OTHER_ORG = "org-c12", "org-c12-other"
KEY, OTHER_KEY = "c12-key-org-a-0001", "c12-key-org-b-0002"
IDEM = "c12-idem-chain-1"
RECIPIENT = "jane@acme.example.com"
MAILBOX_EMAIL = "outreach@c12-sender.example.org"
SMTP_USER, SMTP_PASSWORD = "c12-smtp-user", "c12-SMTP-secret-7d41"
VP = "We cut manual invoice reconciliation time by 40% for finance teams."
BODY = (
    "Hi Jane,\n\nWe cut manual invoice reconciliation time by 40% for finance teams.\n\n"
    "Worth a quick chat?\n\nBest,\nC12 Sender"
)
SUBJECT = "Quick question"
REPLY_TOKEN = f"C12-REPLY-{uuid.uuid4().hex}"
REPLY_BODY = f"Yes, happy to talk. <script>alert('c12')</script> {REPLY_TOKEN}"
REPLY_SUBJECT = f"Re: {SUBJECT} {REPLY_TOKEN}"

CONVERSATION_KEYS = {"action", "messages", "has_more"}
ACTION_KEYS = {
    "accepted",
    "state",
    "mailing_agent_reference",
    "created_at",
    "updated_at",
    "mailbox_reference",
}
MESSAGE_KEYS = {
    "direction",
    "message_type",
    "subject",
    "body",
    "body_truncated",
    "created_at",
    "delivery_state",
    "mailing_agent_reference",
    "mailbox_reference",
    "message_reference",  # C9.4: inbound only; not opaque (the message row id)
}


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def db(tmp_path):
    """PostgreSQL when POSTGRES_TEST_URL is set, else a SQLite file.

    Unlike the older fixtures, an explicitly configured PostgreSQL that is NOT
    reachable fails the test instead of silently degrading to SQLite: this is
    evidence for the real database, and a quiet fallback would claim it falsely."""
    url = os.environ.get("POSTGRES_TEST_URL")
    if url:
        if os.environ.get("C12_ALLOW_DESTRUCTIVE_TEST_DB") != "1":
            pytest.fail(
                "Refusing destructive C12 PostgreSQL test without explicit opt-in",
                pytrace=False,
            )

        parsed = make_url(url)
        if (
            not parsed.drivername.startswith("postgresql")
            or parsed.host not in {"localhost", "127.0.0.1", "::1"}
            or parsed.database != "mailer_agent_test"
        ):
            pytest.fail(
                "C12 requires a loopback PostgreSQL database named mailer_agent_test",
                pytrace=False,
            )

        eng = create_engine(url)
        with eng.connect() as c:  # raises if unreachable -- deliberately
            c.execute(text("SELECT 1"))
    else:
        eng = create_engine(
            f"sqlite:///{tmp_path / 'c12.db'}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )
    Base.metadata.create_all(bind=eng)
    if eng.dialect.name == "postgresql":
        with eng.connect() as c:
            c.execute(
                text(
                    "TRUNCATE messages, external_dispatches, contacts, campaigns, mailboxes, suppression_list CASCADE"
                )
            )
            c.commit()
    yield sessionmaker(bind=eng, autoflush=False)
    eng.dispose()


class SmtpSink:
    """Disposable STARTTLS-required, AUTH-required SMTP server (aiosmtpd). Records
    what a real MTA would see; `on_data` runs while the message is mid-flight, i.e.
    while Mailer's send is still in progress."""

    def __init__(self, ctx: ssl.SSLContext, expected_login: tuple[str, str]):
        self.received: list[SimpleNamespace] = []
        self.auth_attempts: list[tuple[str, bool]] = []
        self.on_data = None
        sink = self

        class Handler:
            async def handle_DATA(self, server, session, envelope):
                snap = sink.on_data() if sink.on_data else None
                sink.received.append(
                    SimpleNamespace(
                        mail_from=envelope.mail_from,
                        rcpt_tos=list(envelope.rcpt_tos),
                        raw=envelope.content,
                        tls=getattr(session, "ssl", None) is not None,
                        authenticated=bool(session.authenticated),
                        during=snap,
                    )
                )
                return "250 OK queued"

        def authenticator(server, session, envelope, mechanism, auth_data):
            ok = (
                isinstance(auth_data, LoginPassword)
                and auth_data.login.decode() == expected_login[0]
                and auth_data.password.decode() == expected_login[1]
            )
            sink.auth_attempts.append((mechanism, ok))
            return AuthResult(success=ok)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self._controller = Controller(
            Handler(),
            hostname="127.0.0.1",
            port=self.port,
            tls_context=ctx,
            require_starttls=True,
            authenticator=authenticator,
            auth_required=True,
            auth_require_tls=True,
        )

    def start(self):
        self._controller.start()

    def stop(self):
        self._controller.stop()


@pytest.fixture
def smtp_sink(dovecot):  # noqa: F811
    # Reuse Dovecot's throw-away certificate (SAN: localhost + 127.0.0.1). The
    # dovecot fixture already points SSL_CERT_FILE at it, which is how Mailer's
    # own ssl.create_default_context() trusts it -- no production code change.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(dovecot.root / "cert.pem"), str(dovecot.root / "key.pem"))
    sink = SmtpSink(ctx, (SMTP_USER, SMTP_PASSWORD))
    sink.start()
    try:
        yield sink
    finally:
        sink.stop()


@pytest.fixture
def api(db, monkeypatch):
    """The real Mailer app: real X-API-Key auth (two orgs' keys), real routers.
    Only get_db is pointed at the test database."""

    def _get_db():
        s = db()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(deps, "_KEY_MAP", {KEY: ORG, OTHER_KEY: OTHER_ORG})
    app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def live(monkeypatch):
    """LIVE_SENDING_ENABLED=true for THIS test only (conftest forces false per test),
    deployment integration sender set, and deterministic generation (existing fake LLM).
    """
    s = get_settings()
    monkeypatch.setattr(s, "live_sending_enabled", True)
    monkeypatch.setattr(s, "leadboost_integration_sender_email", MAILBOX_EMAIL)
    monkeypatch.setattr(s, "leadboost_integration_sender_name", "C12 Sender")
    monkeypatch.setattr(s, "leadboost_integration_sender_org", "C12 Org")
    return install_fake_llm_provider(monkeypatch)


# ------------------------------------------------------------------- helpers


def _h(key=KEY):
    return {"X-API-Key": key}


def _conversation(api, key=KEY, idem=IDEM):
    return api.get(
        f"/integrations/leadboost/outreach-actions/{idem}/conversation", headers=_h(key)
    )


def _walk_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_keys(v)


def _only_mailer_records(caplog):
    # Third-party test servers (aiosmtpd) log under their own names; the claim being
    # checked is about Mailer's logging.
    return [r for r in caplog.records if r.name.split(".")[0] == "mailer_agent"]


# ---------------------------------------------------------------------- test


def test_complete_lifecycle_provision_generate_send_reply_poll_conversation(
    dovecot, smtp_sink, db, api, live, monkeypatch, caplog  # noqa: F811
):
    caplog.set_level(logging.DEBUG)
    imap_user, imap_password = dovecot.new_account()

    # --- A. mailbox: provisioned SMTP-only (as LeadBoost does), then the approved
    #        STAGING-ONLY operator step adds the disposable IMAP set via the existing API.
    created = api.post(
        "/mailboxes",
        headers=_h(),
        json={
            "email_address": MAILBOX_EMAIL,
            "smtp_host": "127.0.0.1",
            "smtp_port": smtp_sink.port,
            "smtp_use_tls": True,
            "smtp_username": SMTP_USER,
            "smtp_password": SMTP_PASSWORD,
        },
    )
    assert created.status_code == 201, created.text
    mailbox_ref = created.json()["public_reference"]
    assert created.json()["imap_host"] is None and created.json()["status"] == "active"

    patched = api.patch(
        f"/mailboxes/{mailbox_ref}",
        headers=_h(),
        json={
            "imap_host": "localhost",
            "imap_port": dovecot.port,
            "imap_username": imap_user,
            "imap_password": imap_password,
        },
    )
    assert patched.status_code == 200, patched.text
    assert (
        patched.json()["imap_host"] == "localhost"
        and patched.json()["imap_port"] == dovecot.port
    )

    # LeadBoost later re-syncs/activates with SMTP fields only: IMAP must survive it.
    resync = api.patch(
        f"/mailboxes/{mailbox_ref}",
        headers=_h(),
        json={
            "status": "active",
            "smtp_host": "127.0.0.1",
            "smtp_port": smtp_sink.port,
            "smtp_use_tls": True,
            "smtp_username": SMTP_USER,
            "smtp_password": SMTP_PASSWORD,
        },
    )
    assert resync.status_code == 200, resync.text
    assert (
        resync.json()["imap_host"] == "localhost"
        and resync.json()["imap_username"] == imap_user
    )
    for r in (created, patched, resync):  # credentials are write-only
        assert SMTP_PASSWORD not in r.text and imap_password not in r.text

    # --- E. accept a generated-outreach request through the real endpoint + real auth.
    live.queue_response({"subject": SUBJECT, "body": BODY, "reasoning": "c12"})
    accepted = api.post(
        "/integrations/leadboost/outreach-requests",
        headers=_h(),
        json={
            "external_action_id": "481",
            "idempotency_key": IDEM,
            "recipient": {
                "email": RECIPIENT,
                "name": "Jane Doe",
                "title": "VP Finance",
                "company": "Acme",
            },
            "context": {"value_proposition": VP, "recipient_facts": []},
        },
    )
    assert accepted.status_code == 202, accepted.text
    with db() as s:
        d = s.query(ExternalDispatch).filter_by(idempotency_key=IDEM).one()
        mailbox_pk = s.query(Mailbox).filter_by(public_reference=mailbox_ref).one().id
        assert (d.state, d.message_id, d.organization_id, d.mailbox_id) == (
            ExternalDispatchState.QUEUED.value,
            None,
            ORG,
            mailbox_pk,
        )  # durable, not yet generated
    before = _conversation(api)
    assert before.status_code == 200 and before.json()["action"]["state"] == "queued"

    # --- F. real generation cycle: Message created, nothing sent.
    gen = generation_worker.run_generation_cycle(
        session_factory=db, worker_id="c12-gen"
    )
    assert [g.outcome for g in gen] == ["generated"]
    assert smtp_sink.received == []

    # --- G. real dispatch cycle -> real SMTP (STARTTLS + AUTH). While the message is
    #        mid-flight, observe the database from a separate connection.
    def during_smtp():
        with db() as s:
            row = s.query(ExternalDispatch).filter_by(idempotency_key=IDEM).one()
            msg = s.get(Message, row.message_id)
            idle_in_tx = None
            if s.get_bind().dialect.name == "postgresql":
                idle_in_tx = s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                    )
                ).scalar()
            return SimpleNamespace(
                state=row.state,
                persisted_message_id=msg.message_id_header,
                idle_in_tx=idle_in_tx,
            )

    smtp_sink.on_data = during_smtp
    sent = dispatch_worker.run_external_dispatch_cycle(
        session_factory=db, worker_id="c12-snd"
    )
    assert [r.outcome for r in sent] == ["sent"]

    # --- H. what the SMTP server actually saw.
    assert len(smtp_sink.received) == 1
    wire = smtp_sink.received[0]
    wire_msg = message_from_bytes(wire.raw)
    wire_message_id = wire_msg["Message-ID"]
    assert (
        wire.tls
        and wire.authenticated
        and smtp_sink.auth_attempts
        and all(ok for _, ok in smtp_sink.auth_attempts)
    )
    assert wire.mail_from == MAILBOX_EMAIL and wire.rcpt_tos == [
        RECIPIENT
    ]  # mailbox identity, not a global sender
    assert (
        MAILBOX_EMAIL in wire_msg["From"]
        and wire_msg["To"] == RECIPIENT
        and wire_msg["Subject"] == SUBJECT
    )
    wire_body = (
        wire_msg.get_payload(decode=True).decode()
        if not wire_msg.is_multipart()
        else wire_msg.get_payload()[0].get_payload(decode=True).decode()
    )
    assert wire_body.replace("\r\n", "\n").strip() == BODY.strip()  # SMTP carries CRLF
    # C6-C8, observed for real: at the moment SMTP held the message the Message-ID was
    # already committed and equal to the wire header, the row was SENDING, and (PostgreSQL)
    # no session was sitting in a transaction.
    assert wire.during.state == ExternalDispatchState.SENDING.value
    assert wire.during.persisted_message_id == wire_message_id
    assert wire.during.idle_in_tx in (None, 0)
    with db() as s:
        d = s.query(ExternalDispatch).filter_by(idempotency_key=IDEM).one()
        out_msg = s.get(Message, d.message_id)
        assert d.state == ExternalDispatchState.SENT.value
        assert (
            out_msg.message_id_header == wire_message_id
            and out_msg.body.strip() == BODY.strip()
        )
        contact_id = d.contact_id

    # C9.3 after delivery, before any reply: outbound only, state from ExternalDispatch.
    mid = _conversation(api).json()
    assert mid["action"]["state"] == "sent" and [
        m["direction"] for m in mid["messages"]
    ] == ["outbound"]

    # --- I. the recipient replies: injected into the disposable IMAP mailbox with
    #        In-Reply-To/References pointing at the Message-ID that actually went out.
    dovecot.deliver(
        imap_user,
        raw_email(
            from_addr=RECIPIENT,
            to_addr=MAILBOX_EMAIL,
            subject=REPLY_SUBJECT,
            body=REPLY_BODY,
            message_id=f"<c12-reply-{uuid.uuid4().hex}@prospect.example>",
            in_reply_to=wire_message_id,
            references=[wire_message_id],
        ),
    )
    assert len(dovecot.unseen(imap_user)) == 1

    # Inbound classification takes its deterministic fallback path (LLM unavailable), as in
    # test_m3_imap_e2e; generation above already used the fake LLM.
    import mailer_agent.llm.provider_v2 as provider_module

    monkeypatch.setattr(provider_module, "is_llm_available", lambda: False)
    # Auto-reply is switched ON. A reply to a LeadBoost-managed contact must be stored and shown, never
    # handled by Mailer-native automation. NOTE what proves that: with the LLM unavailable, "nothing was
    # sent / drafted" holds on the native path too, so those assertions alone do not discriminate. The
    # discriminating one is the contact's next_action_at below (pinned to None by the integration guard
    # in reply_handler_v2; the native path would reschedule a follow-up) -- checked by removing the guard.
    monkeypatch.setattr(get_settings(), "auto_reply_enabled", True)

    # --- J/K. real M3 poll -> mailbox-bound inbound Message.
    smtp_sink.received.clear()
    results = {r.mailbox_id: r for r in mailbox_inbound.poll_all_mailboxes(db)}
    assert results[mailbox_pk].status == mailbox_inbound.STATUS_OK
    assert dovecot.unseen(imap_user) == []  # marked Seen only after commit
    with db() as s:
        inbound = s.query(Message).filter(Message.direction == "inbound").all()
        assert len(inbound) == 1
        row = inbound[0]
        assert row.mailbox_id == mailbox_pk and row.contact_id == contact_id
        assert (
            row.in_reply_to_header == wire_message_id and row.body.strip() == REPLY_BODY
        )
        assert s.get(Mailbox, row.mailbox_id).organization_id == ORG
        inbound_message_id = row.message_id_header
        snapshot = {
            "dispatch": [
                (d.id, d.state, d.updated_at) for d in s.query(ExternalDispatch).all()
            ],
            "messages": [
                (m.id, m.status, m.direction, m.body)
                for m in s.query(Message).order_by(Message.id).all()
            ],
        }
    assert (
        smtp_sink.received == []
    )  # inbound handling sent nothing, even with auto-reply on
    with db() as s:
        assert (
            s.query(Message).filter(Message.direction == "outbound").count() == 1
        )  # and drafted nothing
        # The guard's own, database-visible effect: the contact is pinned out of Mailer-native
        # scheduling. On the native path the same reply reschedules a follow-up (next_action_at set).
        assert s.get(Contact, contact_id).next_action_at is None

    # --- L/M. real C9.3 endpoint.
    resp = _conversation(api)
    assert resp.status_code == 200, resp.text
    conv = resp.json()
    assert conv["action"]["state"] == "sent" and conv["has_more"] is False
    assert [(m["direction"], m["delivery_state"]) for m in conv["messages"]] == [
        ("outbound", "sent"),
        ("inbound", None),
    ]
    out_pub, in_pub = conv["messages"]
    assert out_pub["body"].strip() == BODY.strip() and out_pub["subject"] == SUBJECT
    assert (
        in_pub["body"] == REPLY_BODY and in_pub["subject"] == REPLY_SUBJECT
    )  # verbatim data, never sanitised away
    assert (
        in_pub["mailbox_reference"]
        == mailbox_ref
        == conv["action"]["mailbox_reference"]
    )

    # --- P. nothing internal or secret in the public response.
    raw = resp.text
    assert set(conv) == CONVERSATION_KEYS and set(conv["action"]) <= ACTION_KEYS
    assert all(set(m) <= MESSAGE_KEYS for m in conv["messages"])
    assert not {
        "id",
        "message_id",
        "in_reply_to",
        "references",
        "error_message",
        "organization_id",
        "contact_id",
        "mailbox_id",
        "email_address",
    } & set(_walk_keys(conv))
    for forbidden in (
        SMTP_PASSWORD,
        imap_password,
        SMTP_USER,
        imap_user,
        MAILBOX_EMAIL,
        "smtp_password",
        "imap_password",
        wire_message_id,
        wire_message_id.strip("<>"),
        inbound_message_id,
        inbound_message_id.strip("<>"),
        "In-Reply-To",
        ORG,
        KEY,
    ):
        assert forbidden not in raw, forbidden
    with db() as s:
        for mb in s.query(Mailbox).all():
            assert mb.smtp_password_enc not in raw and mb.imap_password_enc not in raw

    # Reading mutates nothing (twice, to include repeat reads).
    _conversation(api), _conversation(api)
    with db() as s:
        assert snapshot == {
            "dispatch": [
                (d.id, d.state, d.updated_at) for d in s.query(ExternalDispatch).all()
            ],
            "messages": [
                (m.id, m.status, m.direction, m.body)
                for m in s.query(Message).order_by(Message.id).all()
            ],
        }

    # --- N/O. the server offers the same message again: no duplicate, conversation unchanged.
    dovecot.forget_seen(imap_user)
    assert len(dovecot.unseen(imap_user)) == 1
    mailbox_inbound.poll_all_mailboxes(db)
    assert dovecot.unseen(imap_user) == []
    with db() as s:
        assert s.query(Message).filter(Message.direction == "inbound").count() == 1
    again = _conversation(api).json()
    assert [m["direction"] for m in again["messages"]] == ["outbound", "inbound"]

    # Another organization's key sees nothing of this conversation: the same 404 as an unknown action.
    foreign = _conversation(api, key=OTHER_KEY)
    unknown = _conversation(api, idem="c12-no-such-action")
    assert (
        foreign.status_code == unknown.status_code == 404
        and foreign.json() == unknown.json()
    )
    assert _conversation(api, key="not-a-key").status_code in (401, 403)

    # --- Q. logs: no credentials, no ciphertext, no attacker-controlled message text.
    logged = "\n".join(r.getMessage() for r in _only_mailer_records(caplog))
    assert logged, "expected Mailer to have logged something during the chain"
    secrets = [
        SMTP_PASSWORD,
        imap_password,
        str(get_settings().mailbox_encryption_key.get_secret_value()),
    ]
    with db() as s:
        for mb in s.query(Mailbox).all():
            secrets += [mb.smtp_password_enc, mb.imap_password_enc]
    for secret in secrets:
        assert secret and secret not in logged
    assert REPLY_TOKEN not in logged and "alert('c12')" not in logged
