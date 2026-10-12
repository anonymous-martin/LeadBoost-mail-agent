"""
Deterministic end-to-end suite (spec sections 40-42).

What "deterministic E2E" means here, concretely:
  - REAL: FastAPI HTTP layer (fastapi.testclient.TestClient against the
    actual app and its actual routers), real Pydantic request/response
    validation, real SQLAlchemy persistence, and the real worker-side
    functions (send_initial_outreach, process_inbound_email) -- not
    reimplementations of them.
  - FAKE: the LLM provider (tests/fake_llm_provider.py, via the autouse
    `fake_llm` fixture from conftest.py) and the SMTP transport (achieved
    by leaving LIVE_SENDING_ENABLED at its default False, which is
    mail/sender.py's own built-in deterministic dry-run path -- it
    returns a real, correctly-shaped SendResult without touching a
    socket, rather than needing a separate hand-rolled fake SMTP server).

What's deliberately NOT exercised here: the real APScheduler background
threads (main.py's lifespan would start those against whatever
mailer_agent.db.engine happens to be bound to, which is a session-wide
singleton this file has no reliable control over -- see
test_tenant_correlation.py's module docstring for the same issue). This
file constructs its own isolated engine/session per test and overrides
FastAPI's get_db, then calls the actual worker functions
(send_initial_outreach, process_inbound_email) directly with that
session, simulating "a scheduler tick fired" deterministically instead
of waiting on a real timer. This is what section 42 asks for
("real... scheduler/worker BEHAVIOR") without the flakiness of actually
running a background thread inside a test.

Auth: the app's require_api_key / get_current_org_id dependencies are
overridden directly rather than relying on environment variables, since
api/deps.py builds its key->org map once at import time (another
session-wide singleton) -- overriding the dependency callables is the
robust way to control auth in tests regardless of import order.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mailer_agent.api.deps import get_current_org_id, get_integration_org_id, require_api_key
from mailer_agent.api.main import app
from mailer_agent.db import get_db
from mailer_agent.followup.engine import send_initial_outreach
from mailer_agent.mail.reply_handler import process_inbound_email
from mailer_agent.mail.imap_reader import InboundEmail
from mailer_agent.models import Base, Contact


@pytest.fixture
def e2e_session():
    # StaticPool is load-bearing here, not just a nice-to-have: without
    # it, sqlite:///:memory: hands out a SEPARATE, empty in-memory
    # database to each new connection the pool opens (SQLite's
    # :memory: databases are connection-local by nature). This session's
    # own direct use of `engine` (Base.metadata.create_all, and this
    # fixture's own session) might get one connection/database, while
    # TestClient's request handling -- which can dispatch through a
    # different thread/connection depending on the ASGI transport --
    # could get handed a DIFFERENT, table-less one, surfacing as
    # `sqlite3.OperationalError: no such table: campaigns` despite
    # create_all() having genuinely run moments earlier. StaticPool
    # forces the whole engine to share exactly one underlying
    # connection, so every consumer -- this fixture, the FastAPI
    # dependency override, and any cross-thread access from TestClient --
    # sees the same actual database. check_same_thread=False is required
    # alongside it, since StaticPool means that one connection genuinely
    # can be used from more than one thread.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(e2e_session):
    """
    TestClient against the real app, real routers, real HTTP layer --
    with get_db/auth dependencies overridden to point at this test's
    isolated session, and NOT used as a context manager (`with
    TestClient(app) as c`) specifically so the app's lifespan
    (init_db + start_scheduler) never runs. No real background
    scheduler thread, no dependency on mailer_agent.db's module-level
    engine singleton.
    """
    app.dependency_overrides[get_db] = lambda: e2e_session
    app.dependency_overrides[require_api_key] = lambda: None
    app.dependency_overrides[get_current_org_id] = lambda: "e2e-test-org"
    # C15.1b: the inbound webhook resolves its tenant through the fail-closed integration
    # dependency; this lifecycle test (not an auth test) pins it to the same org.
    app.dependency_overrides[get_integration_org_id] = lambda: "e2e-test-org"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _draft_response(subject: str, body: str) -> dict:
    return {"subject": subject, "body": body, "reasoning": "test fixture draft"}


def _planner_response(**overrides) -> dict:
    base = {
        "action_type": "provide_requested_information",
        "objective": "Answer what they asked based on approved information.",
        "reason": "test fixture planner proposal",
        "required_information": [],
        "confidence": 0.85,
        "requires_human_review": False,
    }
    base.update(overrides)
    return base


def _classification_response(**overrides) -> dict:
    base = {
        "intents": ["information_request"],
        "sentiment": "positive",
        "buying_stage": "considering",
        "urgency": "no_timeline",
        "requested_information": ["pricing"],
        "questions_asked": [],
        "confidence": 0.85,
        "reasoning": "test fixture classification",
        "requires_human_review": False,
    }
    base.update(overrides)
    return base


class TestFullCampaignLifecycle:
    """
    campaign create -> contact add -> start -> initial send -> inbound
    reply (via the webhook HTTP endpoint) -> classification -> draft
    reply held for approval -> approve -> second send -> conversation
    thread reflects all three messages in order.

    This is section 63's "final local live business flow" in
    deterministic form (fake LLM + dry-run SMTP instead of real Groq
    and real mailboxes, which this sandbox cannot reach -- see
    docs/FINAL_PRODUCTION_READINESS.md for the real-provider commands
    the user runs separately).
    """

    def test_full_lifecycle(self, client, e2e_session, fake_llm):
        # 1. Create campaign -- real HTTP + real Pydantic validation.
        create_resp = client.post(
            "/campaigns",
            json={
                "name": "Footwear Outreach Q3",
                "sender_name": "Raj Kumar",
                "sender_org": "Kumar Footwear Distributors",
                "sender_email": "raj@kumarfootwear.example.com",
                "value_prop": "We supply premium footwear brands to established retail chains.",
                "proof_points": "Partnered with 50 retail chains across North India.",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        campaign_id = create_resp.json()["id"]

        # 2. Add a contact.
        contacts_resp = client.post(
            f"/campaigns/{campaign_id}/contacts",
            json={"contacts": [{"name": "Priya Sharma", "email": "priya@mochishoes.example.com", "company": "Mochi Shoes"}]},
        )
        assert contacts_resp.status_code == 201, contacts_resp.text
        contact_id = contacts_resp.json()[0]["id"]

        # 3. Start the campaign -- marks the contact due, doesn't send yet.
        start_resp = client.post(f"/campaigns/{campaign_id}/start")
        assert start_resp.status_code == 200, start_resp.text
        assert start_resp.json()["queued"] == 1

        # 4. Simulate the worker tick: call the real send function directly
        #    against our isolated session (see module docstring for why).
        fake_llm.queue_response(_draft_response(
            subject="Quick question for Mochi Shoes",
            body="Hi Priya, we distribute premium footwear brands to retail "
                 "chains like yours -- worth a quick chat?",
        ))
        contact = e2e_session.query(Contact).get(contact_id)
        send_result = send_initial_outreach(e2e_session, contact)
        e2e_session.commit()
        assert send_result["action"] == "sent", send_result

        # 5. Verify via the real HTTP layer that the message landed.
        thread_resp = client.get(f"/contacts/{contact_id}/thread")
        assert thread_resp.status_code == 200, thread_resp.text
        thread = thread_resp.json()
        assert len(thread["messages"]) == 1
        assert thread["messages"][0]["status"] == "sent"
        assert thread["messages"][0]["direction"] == "outbound"

        # 6. Prospect replies -- via the real webhook HTTP endpoint, the
        #    same path a real inbound-parse provider would hit.
        #    Three LLM calls happen for a reply that isn't auto-sent:
        #    (a) classify_prospect_reply, (b) plan_next_action (the
        #    Planner), (c) draft_message inside
        #    _draft_and_maybe_send_reply (the Responder) -- all three
        #    fixtures must be queued up front since the fake consumes
        #    them strictly in order.
        fake_llm.queue_response(_classification_response(
            intents=["information_request", "pricing_request"],
            requires_human_review=True,
        ))
        fake_llm.queue_response(_planner_response(
            action_type="escalate",
            objective="Have a human review this pricing question before responding.",
            requires_human_review=True,
            review_reason="Pricing question -- needs verified data",
        ))
        fake_llm.queue_response(_draft_response(
            subject="Re: Quick question for Mochi Shoes",
            body="Thanks for your interest! I'll get pricing details together "
                 "for your 12 stores and follow up shortly.",
        ))
        webhook_resp = client.post(
            "/webhooks/inbound-email",
            json={
                "from_email": "priya@mochishoes.example.com",
                "to_email": "raj@kumarfootwear.example.com",
                "subject": "Re: Quick question for Mochi Shoes",
                "body_text": "This looks interesting -- can you send pricing for our 12 stores?",
                "message_id": "<reply-1@mochishoes.example.com>",
                "in_reply_to": None,
                "references": [],
            },
        )
        assert webhook_resp.status_code == 200, webhook_resp.text
        assert webhook_resp.json()["matched"] is True

        # 7. auto_reply_enabled defaults to False, so the reply produces a
        #    DRAFT held for approval, not an auto-send. Confirm the thread
        #    now has 2 inbound+draft messages and find the draft.
        thread_resp = client.get(f"/contacts/{contact_id}/thread")
        messages = thread_resp.json()["messages"]
        assert len(messages) == 3, messages  # initial outbound + inbound reply + draft reply
        draft_msgs = [m for m in messages if m["status"] == "draft"]
        assert len(draft_msgs) == 1, messages
        draft_id = draft_msgs[0]["id"]

        # 8. Approve the draft -- exercises the full outbound safety gate
        #    (suppression + grounding recheck + live-sending gate) via
        #    the real HTTP layer. live_sending_enabled is True only for
        #    this call so approval passes the new gate in api/messages.py;
        #    send_email is faked (not left real) because with the flag
        #    True, mail/sender.py's own dry-run branch no longer applies
        #    and it would attempt a real SMTP connection this sandbox
        #    can't reach -- this fake mirrors that same dry-run
        #    simulation instead (a real, correctly-shaped SendResult, no
        #    socket touched), consistent with this file's stated design
        #    of never touching a real socket.
        import mailer_agent.api.messages as messages_api
        from email.utils import make_msgid

        from mailer_agent.config import get_settings
        from mailer_agent.mail.sender import SendOutcome, SendResult

        def fake_send_email(*, to_email, from_email, from_name, subject, body_text,
                             reply_to=None, in_reply_to_header=None, references_header=None):
            domain = from_email.split("@")[-1] if "@" in from_email else "localhost"
            return SendResult(success=True, message_id=make_msgid(domain=domain), outcome=SendOutcome.SENT)

        settings = get_settings()
        original_live_sending = settings.live_sending_enabled
        original_send_email = messages_api.send_email
        settings.live_sending_enabled = True
        messages_api.send_email = fake_send_email
        try:
            approve_resp = client.post(f"/messages/{draft_id}/approve")
        finally:
            settings.live_sending_enabled = original_live_sending
            messages_api.send_email = original_send_email
        assert approve_resp.status_code == 200, approve_resp.text
        assert approve_resp.json()["status"] == "sent"

        # 9. Final thread state: 2 outbound (sent) + 1 inbound.
        final_thread = client.get(f"/contacts/{contact_id}/thread").json()
        statuses = sorted(m["status"] for m in final_thread["messages"])
        assert statuses == ["received", "sent", "sent"], final_thread["messages"]

    def test_suppressed_contact_is_skipped_at_add_time(self, client, e2e_session):
        """A contact already on the suppression list is skipped when added,
        never queued for outreach in the first place."""
        from mailer_agent.models import SuppressionEntry
        e2e_session.add(SuppressionEntry(email="blocked@example.com", organization_id="e2e-test-org", reason="unsubscribed"))
        e2e_session.commit()

        create_resp = client.post(
            "/campaigns",
            json={
                "name": "Test Campaign",
                "sender_name": "Sam",
                "sender_org": "TestCo",
                "sender_email": "sam@testco.example.com",
                "value_prop": "Test value prop.",
            },
        )
        campaign_id = create_resp.json()["id"]

        contacts_resp = client.post(
            f"/campaigns/{campaign_id}/contacts",
            json={"contacts": [{"email": "blocked@example.com"}]},
        )
        assert contacts_resp.status_code == 201
        assert contacts_resp.json() == []  # skipped, not created

    def test_suppressed_contact_reply_is_not_auto_sent(self, client, e2e_session, fake_llm):
        """
        Regression test: mail.reply_handler_v2's auto-send path used to
        skip the suppression check that every other send path has
        (initial outreach, follow-up, manual approval) -- a contact could
        unsubscribe and still receive an auto-reply if a later inbound
        message classified as auto-sendable. Fixed to check immediately
        before deciding to auto-send, same as every other send path.

        Uses auto_reply_enabled=True (monkeypatched) specifically to
        reach the auto-send branch this bug lived in -- with the default
        False, this path is never exercised at all, which is exactly how
        the gap went unnoticed.
        """
        from mailer_agent.config import get_settings
        from mailer_agent.models import SuppressionEntry

        create_resp = client.post(
            "/campaigns",
            json={
                "name": "Auto Reply Test",
                "sender_name": "Sam",
                "sender_org": "TestCo",
                "sender_email": "sam@testco.example.com",
                "value_prop": "Test value prop.",
            },
        )
        campaign_id = create_resp.json()["id"]
        contact_id = client.post(
            f"/campaigns/{campaign_id}/contacts",
            json={"contacts": [{"email": "prospect@example.com"}]},
        ).json()[0]["id"]

        fake_llm.queue_response(_draft_response("Intro", "intro body"))
        from mailer_agent.models import Contact as _Contact
        contact = e2e_session.query(_Contact).get(contact_id)
        send_initial_outreach(e2e_session, contact)
        e2e_session.commit()

        # Contact unsubscribes AFTER the initial send -- suppression must
        # still be honored on the very next inbound-triggered reply.
        e2e_session.add(SuppressionEntry(email="prospect@example.com", organization_id="e2e-test-org", reason="unsubscribed"))
        e2e_session.commit()

        settings = get_settings()
        original = settings.auto_reply_enabled
        settings.auto_reply_enabled = True
        try:
            fake_llm.queue_response(_classification_response(
                intents=["question"], confidence=0.95, requires_human_review=False,
            ))
            fake_llm.queue_response(_planner_response(
                action_type="answer",
                objective="Answer their question directly.",
                confidence=0.9,
                requires_human_review=False,
            ))
            fake_llm.queue_response(_draft_response("Re: Intro", "auto-reply body"))
            resp = client.post(
                "/webhooks/inbound-email",
                json={
                    "from_email": "prospect@example.com",
                    "to_email": "sam@testco.example.com",
                    "body_text": "Quick question for you.",
                    "message_id": "<r1@example.com>",
                },
            )
        finally:
            settings.auto_reply_enabled = original

        assert resp.status_code == 200, resp.text
        thread = client.get(f"/contacts/{contact_id}/thread").json()
        outbound_messages = [m for m in thread["messages"] if m["direction"] == "outbound"]
        # Exactly 2 outbound messages should exist: the initial send (sent)
        # and the reply attempt, which must be held as a draft -- not sent
        # -- because the contact is suppressed. No ordering assumption
        # needed: just confirm one of each status, never two "sent".
        assert sorted(m["status"] for m in outbound_messages) == ["draft", "sent"], (
            f"Suppressed contact's reply must be held as a draft, never "
            f"auto-sent. Outbound messages: {outbound_messages}"
        )


class TestContextContaminationAcrossCampaigns:
    """
    Spec section 8: the same process must handle multiple unrelated
    campaigns without seller/prospect context leaking between them.

    Rather than inspecting generated message *text* (which, with a fake
    LLM that returns exactly what's queued, wouldn't actually prove
    anything about contamination -- the fake doesn't derive content from
    the prompt), this inspects the actual PROMPTS sent to the provider
    (captured by fake_llm.last_prompts) for each contact, interleaved
    across two unrelated campaigns, and confirms each contact's prompt
    contains only its own campaign's seller identity/value prop and
    never the other campaign's.
    """

    def test_two_campaigns_interleaved_no_context_crossover(self, client, e2e_session, fake_llm):
        footwear = client.post(
            "/campaigns",
            json={
                "name": "Footwear Campaign",
                "sender_name": "Raj Kumar",
                "sender_org": "Kumar Footwear Distributors",
                "sender_email": "raj@kumarfootwear.example.com",
                "value_prop": "We supply premium footwear brands to retail chains.",
                "proof_points": "Partnered with 50 retail chains.",
            },
        ).json()

        logistics = client.post(
            "/campaigns",
            json={
                "name": "Logistics Campaign",
                "sender_name": "Dana Lee",
                "sender_org": "Swift Logistics Partners",
                "sender_email": "dana@swiftlogistics.example.com",
                "value_prop": "We provide same-day freight logistics for manufacturers.",
                "proof_points": "Handles 200 shipments per week across the region.",
            },
        ).json()

        contact_a = client.post(
            f"/campaigns/{footwear['id']}/contacts",
            json={"contacts": [{"email": "buyer@shoeretailer.example.com", "company": "Shoe Retailer Co"}]},
        ).json()[0]
        contact_b = client.post(
            f"/campaigns/{logistics['id']}/contacts",
            json={"contacts": [{"email": "ops@manufacturer.example.com", "company": "Acme Manufacturing"}]},
        ).json()[0]

        client.post(f"/campaigns/{footwear['id']}/start")
        client.post(f"/campaigns/{logistics['id']}/start")

        # Interleave processing order deliberately: B, then A, then B again's
        # reply -- not the order the campaigns were created in.
        fake_llm.queue_response(_draft_response("Intro", "Freight logistics intro"))
        b = e2e_session.query(Contact).get(contact_b["id"])
        send_initial_outreach(e2e_session, b)
        e2e_session.commit()

        fake_llm.queue_response(_draft_response("Intro", "Footwear intro"))
        a = e2e_session.query(Contact).get(contact_a["id"])
        send_initial_outreach(e2e_session, a)
        e2e_session.commit()

        assert len(fake_llm.last_prompts) == 2
        prompt_for_b, prompt_for_a = fake_llm.last_prompts[0][1], fake_llm.last_prompts[1][1]

        # B's prompt (logistics) must contain only logistics context.
        assert "Swift Logistics Partners" in prompt_for_b
        assert "same-day freight logistics" in prompt_for_b
        assert "Kumar Footwear Distributors" not in prompt_for_b
        assert "premium footwear brands" not in prompt_for_b

        # A's prompt (footwear) must contain only footwear context.
        assert "Kumar Footwear Distributors" in prompt_for_a
        assert "premium footwear brands" in prompt_for_a
        assert "Swift Logistics Partners" not in prompt_for_a
        assert "same-day freight logistics" not in prompt_for_a
