"""
C9.2 -- Mailer-native outbound automation must never act on a LeadBoost
integration Campaign / Contact / Message, and must keep behaving exactly as
before for ordinary campaigns.

Every test pairs the integration case with an ORDINARY-campaign control so a
guard that is too broad (or a test that passes vacuously) shows up.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mailer_agent.api import contacts as contacts_module
from mailer_agent.api.deps import get_current_org_id, require_api_key
from mailer_agent.api.main import app
from mailer_agent.db import get_db
from mailer_agent.followup import engine_v2 as engine_v2_module
from mailer_agent.followup import scheduler as scheduler_module
from mailer_agent.followup.engine import run_followup_cycle
from mailer_agent.followup.engine_v2 import integrated_engine
from mailer_agent.followup.work_claiming import claim_due_contacts
from mailer_agent.mail import reply_handler_v2 as rh
from mailer_agent.mail.imap_reader import InboundEmail
from mailer_agent.models import (
    Base,
    Campaign,
    Contact,
    ContactStatus,
    Message,
    SuppressionEntry,
)
from mailer_agent.semantic_models import (
    BuyingStage,
    ClassificationResult,
    IntentType,
    SemanticIntent,
    SentimentType,
)
from mailer_agent.utils.datetime_utils import utcnow
from tests.dispatch_support import seed_dispatch

ORG = "org-a"
PAST = (utcnow() - timedelta(hours=1)).replace(tzinfo=None)   # column is naive DateTime


@pytest.fixture()
def sm(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path/'iso.db'}", connect_args={"check_same_thread": False, "timeout": 15}
    )
    Base.metadata.create_all(bind=eng)
    yield sessionmaker(bind=eng)
    eng.dispose()


@pytest.fixture()
def client(sm):
    def _get_db():
        s = sm()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_api_key] = lambda: None
    app.dependency_overrides[get_current_org_id] = lambda: ORG
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _native(db, *, status=ContactStatus.NEW.value, next_action_at=None, email="native@example.com"):
    """An ordinary Mailer-native campaign + contact (control group)."""
    camp = Campaign(
        name="native", organization_id=ORG, sender_name="S", sender_org="O",
        sender_email="s@native.example.com", value_prop="We help teams.",
    )
    db.add(camp)
    db.flush()
    k = Contact(campaign_id=camp.id, name="Nat", email=email, status=status, next_action_at=next_action_at)
    db.add(k)
    db.flush()
    return camp, k


def _integration(db, *, status=ContactStatus.NEW.value, next_action_at=None):
    """The integration campaign/contact exactly as the dispatch seed builds them,
    then forced into whatever (invalid-by-design) native-schedulable state a
    regression or manual DB edit could produce."""
    d = seed_dispatch(db, org=ORG)
    k = db.get(Contact, d.contact_id)
    k.status, k.next_action_at = status, next_action_at
    db.commit()
    return db.get(Campaign, d.campaign_id), k, d


# ------------------------------------------------------------ campaign start

def test_integration_campaign_cannot_be_started_natively(client, sm):
    with sm() as s:
        camp, k, _ = _integration(s)
        cid, kid = camp.id, k.id
    r = client.post(f"/campaigns/{cid}/start")
    assert r.status_code == 409 and "external integration" in r.json()["detail"]
    with sm() as s:
        k = s.get(Contact, kid)
        assert k.next_action_at is None and k.status == ContactStatus.NEW.value


def test_ordinary_campaign_start_is_unchanged(client, sm):
    with sm() as s:
        camp, k = _native(s)
        s.commit()
        cid, kid = camp.id, k.id
    r = client.post(f"/campaigns/{cid}/start")
    assert r.status_code == 200 and r.json()["queued"] == 1
    with sm() as s:
        assert s.get(Contact, kid).next_action_at is not None


# ------------------------------------------------------------ initial scheduler

def test_claim_for_initial_outreach_excludes_integration_contacts(sm):
    with sm() as s:
        _, ik, _ = _integration(s, status=ContactStatus.NEW.value, next_action_at=PAST)
        _, nk = _native(s, next_action_at=PAST)
        s.commit()
        iid, nid = ik.id, nk.id
        claimed = claim_due_contacts(s, worker_id="w", status=ContactStatus.NEW.value, limit=10)
        assert [c.id for c in claimed] == [nid]
        s.commit()
        assert s.get(Contact, iid).claimed_by is None


def test_dispatch_new_contacts_job_never_sends_an_integration_contact(sm, monkeypatch):
    from mailer_agent.followup import engine as engine_module

    with sm() as s:
        _, ik, _ = _integration(s, status=ContactStatus.NEW.value, next_action_at=PAST)
        _, nk = _native(s, next_action_at=PAST)
        s.commit()
        iid, nid = ik.id, nk.id

    @contextmanager
    def _scope():
        s = sm()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    sent_to: list[int] = []
    monkeypatch.setattr(scheduler_module, "session_scope", _scope)
    monkeypatch.setattr(engine_module, "stagger_sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        engine_module, "send_initial_outreach", lambda db, c: sent_to.append(c.id) or {"action": "spy"}
    )

    scheduler_module.dispatch_new_contacts_job()

    assert sent_to == [nid] and iid not in sent_to           # control reached, integration did not


def test_engine_refuses_initial_outreach_for_an_integration_contact_even_if_called_directly(sm, fake_llm):
    with sm() as s:
        _, ik, _ = _integration(s, next_action_at=PAST)
        before = s.query(Message).count()
        res = integrated_engine.send_initial_outreach(s, ik)
        s.commit()
        assert res["action"] == "skipped_integration_managed"
        assert s.query(Message).count() == before and fake_llm.call_count == 0


# ------------------------------------------------------------ follow-up scheduler

def test_followup_claim_and_cycle_exclude_integration_contacts(sm, fake_llm, monkeypatch):
    with sm() as s:
        _, ik, _ = _integration(s, status=ContactStatus.ACTIVE.value, next_action_at=PAST)
        _, nk = _native(s, status=ContactStatus.ACTIVE.value, next_action_at=PAST)
        s.commit()
        iid, nid = ik.id, nk.id
        claimed = claim_due_contacts(s, worker_id="w", status=ContactStatus.ACTIVE.value, limit=10)
        assert [c.id for c in claimed] == [nid]                       # ordinary behaviour unchanged
        s.rollback()
    with sm() as s:
        before = s.query(Message).filter(Message.contact_id == iid).count()   # the seeded dispatch message
    sent = []
    monkeypatch.setattr(engine_v2_module, "send_email", lambda **kw: sent.append(kw["to_email"]))
    with sm() as s:
        # the full cycle never touches the integration contact (no draft, no send)
        run_followup_cycle(s, worker_id="w2")
        s.commit()
        assert s.query(Message).filter(Message.contact_id == iid).count() == before
        assert s.get(Contact, iid).claimed_by is None
    assert "lead@example.com" not in sent


def test_engine_refuses_followup_for_an_integration_contact_even_if_called_directly(sm, fake_llm):
    with sm() as s:
        _, ik, _ = _integration(s, status=ContactStatus.ACTIVE.value, next_action_at=PAST)
        before = s.query(Message).filter(Message.contact_id == ik.id, Message.direction == "outbound").count()
        res = integrated_engine.send_followup_if_due(s, ik)
        s.commit()
        assert res["action"] == "skipped_integration_managed"
        assert fake_llm.call_count == 0
        assert s.query(Message).filter(Message.contact_id == ik.id, Message.direction == "outbound").count() == before


# ------------------------------------------------------------ force-followup

def test_force_followup_is_rejected_for_integration_contacts_before_any_send(client, sm, monkeypatch):
    calls = []
    monkeypatch.setattr(contacts_module, "send_followup_if_due", lambda db, c: calls.append(c.id) or {"action": "spy"})
    with sm() as s:
        _, ik, _ = _integration(s, status=ContactStatus.ACTIVE.value, next_action_at=None)
        iid = ik.id
    r = client.post(f"/contacts/{iid}/force-followup")
    assert r.status_code == 409 and "external integration" in r.json()["detail"]
    assert calls == []
    with sm() as s:
        assert s.get(Contact, iid).next_action_at is None


def test_force_followup_for_an_ordinary_contact_is_unchanged(client, sm, monkeypatch):
    calls = []
    monkeypatch.setattr(contacts_module, "send_followup_if_due", lambda db, c: calls.append(c.id) or {"action": "spy"})
    # can_send_followup compares the (naive, from SQLite) column with an aware now();
    # that pre-existing clock mismatch is not what is under test, so only it is stubbed.
    monkeypatch.setattr(contacts_module, "can_send_followup", lambda c: True)
    with sm() as s:
        _, nk = _native(s, status=ContactStatus.ACTIVE.value)
        s.commit()
        nid = nk.id
    r = client.post(f"/contacts/{nid}/force-followup")
    assert r.status_code == 200 and calls == [nid]


# ------------------------------------------------------------ inbound replies

def _positive_intent():
    return SemanticIntent(
        intents=[IntentType.POSITIVE_INTEREST], sentiment=SentimentType.POSITIVE,
        buying_stage=BuyingStage.EVALUATING, confidence=0.95,
        requires_human_review=False, reasoning="Prospect is interested.",
    )


@pytest.fixture()
def inbound(monkeypatch):
    monkeypatch.setattr(
        rh, "classify_prospect_reply",
        lambda **kw: ClassificationResult(success=True, semantic_intent=_positive_intent()),
    )
    from tests.dispatch_support import FakeSender
    spy = FakeSender()
    monkeypatch.setattr(rh, "send_email", spy)
    return spy.calls


def _outbound_seed(db, contact, *, mid="<orig-1@sender.example.com>"):
    db.add(Message(contact_id=contact.id, direction="outbound", subject="Hello", body="Hi there",
                   status="sent", message_type="initial", message_id_header=mid))
    db.flush()
    return mid


def _reply(parent, email="lead@example.com"):
    return InboundEmail(
        from_email=email, subject="Re: Hello", body_text="Sounds great, let's talk.",
        message_id="<reply-1@lead.example.com>", in_reply_to=parent, references=[parent],
    )


def test_integration_reply_is_recorded_but_never_creates_native_automation(sm, inbound, fake_llm, monkeypatch):
    monkeypatch.setattr(rh.settings, "auto_reply_enabled", True)       # must NOT bypass the boundary
    with sm() as s:
        _, ik, _ = _integration(s, status=ContactStatus.ACTIVE.value, next_action_at=None)
        parent = _outbound_seed(s, ik)
        s.commit()
        iid = ik.id
        outbound_before = s.query(Message).filter_by(contact_id=iid, direction="outbound").count()

        result = rh.process_inbound_email_v2(s, _reply(parent, email="lead@example.com"), organization_id=None)
        s.commit()

        assert result["matched"] is True and result["action"] == "recorded_integration_managed"
        # observed and stored ...
        inbound_msgs = s.query(Message).filter_by(contact_id=iid, direction="inbound").all()
        assert len(inbound_msgs) == 1 and inbound_msgs[0].detected_intent == "positive_interest"
        # ... but nothing authorized, drafted or scheduled
        assert s.query(Message).filter_by(contact_id=iid, direction="outbound").count() == outbound_before
        assert inbound == [] and fake_llm.call_count == 0              # no send, no planner/responder LLM
        assert s.get(Contact, iid).next_action_at is None

    # and the native follow-up cycle still has nothing to send for it
    with sm() as s:
        run_followup_cycle(s, worker_id="w")
        s.commit()
        assert s.query(Message).filter_by(contact_id=iid, direction="outbound").count() == outbound_before
    assert inbound == []


def test_integration_unsubscribe_still_suppresses(sm, monkeypatch):
    unsub = SemanticIntent(
        intents=[IntentType.UNSUBSCRIBE], sentiment=SentimentType.NEGATIVE, buying_stage=BuyingStage.REJECTED,
        confidence=0.99, requires_human_review=False, reasoning="Asked to be removed.",
    )
    monkeypatch.setattr(rh, "classify_prospect_reply",
                        lambda **kw: ClassificationResult(success=True, semantic_intent=unsub))
    with sm() as s:
        _, ik, _ = _integration(s, status=ContactStatus.ACTIVE.value)
        parent = _outbound_seed(s, ik)
        s.commit()
        iid = ik.id
        rh.process_inbound_email_v2(s, _reply(parent), organization_id=None)
        s.commit()
        assert s.query(SuppressionEntry).count() >= 1
        assert s.get(Contact, iid).next_action_at is None


def test_ordinary_reply_behaviour_is_unchanged(sm, inbound, fake_llm, monkeypatch):
    """Control: with auto-reply on, a native contact still goes through the
    planner/responder pipeline and gets a native follow-up scheduled."""
    monkeypatch.setattr(rh.settings, "auto_reply_enabled", True)
    fake_llm.queue_response({
        "action_type": "answer", "objective": "Thank them and propose a call.",
        "reason": "Positive interest.", "confidence": 0.95, "requires_human_review": False,
    })
    fake_llm.queue_response({"subject": "Re: Hello", "body": "Hi,\n\nGreat -- would a short call work?\n\nBest",
                             "reasoning": "test"})
    with sm() as s:
        _, nk = _native(s, status=ContactStatus.ACTIVE.value, email="lead@example.com")
        parent = _outbound_seed(s, nk)
        s.commit()
        nid = nk.id
        result = rh.process_inbound_email_v2(s, _reply(parent), organization_id=None)
        s.commit()
        assert result["action"] != "recorded_integration_managed"
        assert fake_llm.call_count >= 1                                  # planner/responder ran
        assert s.query(Message).filter_by(contact_id=nid, direction="outbound").count() >= 2
