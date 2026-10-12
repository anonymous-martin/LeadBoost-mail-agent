"""
C9.2 -- POST /integrations/leadboost/outreach-requests.

Per-request sessions on a file-backed SQLite (as the other dispatch API tests):
the HTTP layer and the dispatch worker see each other's COMMITTED state. The
LLM is the shared deterministic fake (tests/fake_llm_provider.py); nothing here
touches the network or SMTP. Race-sensitive behaviour is covered separately
against PostgreSQL in test_leadboost_generated_outreach_postgres.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from mailer_agent.api import deps
from mailer_agent.api import integrations_generated as gen
from mailer_agent.api.deps import get_current_org_id, get_integration_org_id, require_api_key
from mailer_agent.api.main import app
from mailer_agent.config import get_settings
from mailer_agent.db import get_db
from mailer_agent.llm.provider import LLMUnavailableError
from mailer_agent.mail import external_dispatch_worker as w
from mailer_agent.mail import outreach_generation_worker as gw
from mailer_agent.mail.exact_message import evaluate_exact_message_grounding
from mailer_agent.models import (
    Base,
    Campaign,
    Contact,
    ContactStatus,
    ExternalDispatch,
    ExternalDispatchState as S,
    Message,
    MessageStatus,
)
from tests.dispatch_support import FakeSender, TrackingFactory, seed_org_mailboxes

ORG_A, ORG_B = "org-a", "org-b"
URL = "/integrations/leadboost/outreach-requests"
EXACT_URL = "/integrations/leadboost/outreach-actions"

VP_A = "We cut manual invoice reconciliation time by 40% for finance teams."
FACTS_A = ["Acme just opened a second finance office in Austin."]
BODY_A = (
    "Hi Jane,\n\nWe cut manual invoice reconciliation time by 40% for finance teams, "
    "and I saw Acme just opened a second finance office in Austin.\n\n"
    "Worth a quick chat?\n\nBest,\nTest Sender"
)
VP_B = "We help support teams cut ticket backlog by 65% with automatic triage."
FACTS_B = ["Globex runs a 24/7 support desk."]
BODY_B = (
    "Hi Sam,\n\nWe help support teams cut ticket backlog by 65% with automatic triage, and I saw "
    "Globex runs a 24/7 support desk.\n\nWorth a quick chat?\n\nBest,\nTest Sender"
)


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _sender_identity(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "leadboost_integration_sender_email", "outreach@mailer.example.com")
    monkeypatch.setattr(s, "leadboost_integration_sender_name", "Test Sender")
    monkeypatch.setattr(s, "leadboost_integration_sender_org", "Test Org")


@pytest.fixture()
def sm(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path/'gen.db'}", connect_args={"check_same_thread": False, "timeout": 15}
    )
    Base.metadata.create_all(bind=eng)
    sm_ = sessionmaker(bind=eng)
    with sm_() as s:
        seed_org_mailboxes(s)
    yield sm_
    eng.dispose()


def _override_db(sm):
    def _get_db():
        s = sm()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db


class _Org:
    org_id = ORG_A


@pytest.fixture()
def org():
    return _Org()


@pytest.fixture()
def client(sm, org):
    _override_db(sm)
    app.dependency_overrides[require_api_key] = lambda: None
    app.dependency_overrides[get_integration_org_id] = lambda: org.org_id
    app.dependency_overrides[get_current_org_id] = lambda: org.org_id   # exact-message / reconciliation routes
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def real_auth_client(sm, monkeypatch):
    """Only get_db is overridden; the real API-key dependencies run."""
    monkeypatch.setattr(deps, "_KEY_MAP", {"key-a": ORG_A, "key-b": ORG_B})
    _override_db(sm)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def send_spy(monkeypatch):
    """Any SMTP attempt from any route/worker this test touches is recorded."""
    spy = FakeSender()
    monkeypatch.setattr(w, "send_email", spy)
    return spy


def _req(**over):
    base = {
        "external_action_id": "481",
        "idempotency_key": "idem-1",
        "correlation_id": "corr-1",
        "recipient": {"email": "jane@acme.example.com", "name": "Jane Doe",
                      "title": "VP Finance", "company": "Acme"},
        "context": {"value_proposition": VP_A, "recipient_facts": list(FACTS_A)},
    }
    base.update(over)
    return base


def _queue_draft(fake_llm, body=BODY_A, subject="Quick question about reconciliation"):
    fake_llm.queue_response({"subject": subject, "body": body, "reasoning": "test"})


def _generate(sm, n=5):
    """Run the M2-B generation worker (the only place the LLM is called after acceptance)."""
    return gw.run_generation_cycle(
        session_factory=TrackingFactory(sm), worker_id="gen-test", runtime=gw.DispatchRuntime(), max_items=n
    )


def _counts(sm):
    with sm() as s:
        return {
            "campaigns": s.query(Campaign).count(),
            "contacts": s.query(Contact).count(),
            "messages": s.query(Message).count(),
            "dispatches": s.query(ExternalDispatch).count(),
        }


EMPTY = {"campaigns": 0, "contacts": 0, "messages": 0, "dispatches": 0}


# ---------------------------------------------------------------- schema

FORBIDDEN_TOP = {
    "organization_id": "org-evil", "subject": "S", "body": "B", "sender_email": "x@y.com",
    "sender_name": "X", "sender_org": "X", "reply_to": "x@y.com", "smtp_host": "smtp.x.com",
    "smtp_port": 587, "smtp_username": "u", "smtp_password": "p", "credential": "p",
    "credential_type": "password", "imap_host": "imap.x.com", "mailbox_id": 1,
    "mailbox_reference": "m-1", "proof_points": "100% uptime", "tone": "casual",
    "action_type": "followup", "campaign_id": 1, "follow_up_days": [1, 2],
    "contact_id": 9, "conversation_transcript": "fake history",
}


@pytest.mark.parametrize("field,value", sorted(FORBIDDEN_TOP.items()))
def test_unknown_top_level_field_is_rejected_before_any_work(client, sm, fake_llm, field, value):
    r = client.post(URL, json={**_req(), field: value})
    assert r.status_code == 422
    assert fake_llm.call_count == 0 and _counts(sm) == EMPTY


@pytest.mark.parametrize("field", ["smtp_host", "sender_email", "mailbox_id", "subject", "body", "tone"])
def test_unknown_field_inside_recipient_or_context_is_rejected(client, sm, fake_llm, field):
    req = _req()
    req["recipient"][field] = "x"
    assert client.post(URL, json=req).status_code == 422
    req = _req()
    req["context"][field] = "x"
    assert client.post(URL, json=req).status_code == 422
    assert fake_llm.call_count == 0 and _counts(sm) == EMPTY


def test_recipient_facts_are_bounded_to_eight(client, sm, fake_llm):
    ok = _req(context={"value_proposition": VP_A, "recipient_facts": [f"fact {i}" for i in range(8)]})
    too_many = _req(context={"value_proposition": VP_A, "recipient_facts": [f"fact {i}" for i in range(9)]})
    assert client.post(URL, json=too_many).status_code == 422
    assert fake_llm.call_count == 0 and _counts(sm) == EMPTY
    _queue_draft(fake_llm, body="Hi Jane,\n\nWorth a quick chat?\n\nBest")
    assert client.post(URL, json=ok).status_code == 202


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("external_action_id"),
    lambda r: r.pop("idempotency_key"),
    lambda r: r.pop("recipient"),
    lambda r: r.pop("context"),
    lambda r: r["context"].pop("value_proposition"),
    lambda r: r["context"].__setitem__("value_proposition", ""),
    lambda r: r["recipient"].__setitem__("email", "not-an-email"),
    lambda r: r.__setitem__("external_action_id", ""),
])
def test_missing_or_invalid_required_fields_are_rejected(client, sm, fake_llm, mutate):
    req = _req()
    mutate(req)
    assert client.post(URL, json=req).status_code == 422
    assert fake_llm.call_count == 0 and _counts(sm) == EMPTY


# ---------------------------------------------------------------- auth / tenancy

def test_missing_and_unknown_api_key_are_401_and_do_nothing(real_auth_client, sm, fake_llm):
    assert real_auth_client.post(URL, json=_req()).status_code == 401
    assert real_auth_client.post(URL, json=_req(), headers={"X-API-Key": "nope"}).status_code == 401
    assert fake_llm.call_count == 0 and _counts(sm) == EMPTY


def test_integration_route_fails_closed_when_no_keys_are_configured(sm, monkeypatch, fake_llm):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    _override_db(sm)
    try:
        c = TestClient(app)
        for headers in ({}, {"X-API-Key": "anything"}):
            r = c.post(URL, json=_req(), headers=headers)
            assert r.status_code == 503
        # C15.1: the legacy exact-message reconciliation route is no longer open in an
        # unconfigured deployment (this assertion used to expect 404, i.e. pinned the fail-open
        # behaviour). It now fails closed like the rest of the integration surface.
        assert c.get(f"{EXACT_URL}/does-not-exist").status_code == 503
    finally:
        app.dependency_overrides.clear()
    assert fake_llm.call_count == 0 and _counts(sm) == EMPTY


def test_tenant_comes_from_the_api_key_only(real_auth_client, sm, fake_llm):
    _queue_draft(fake_llm)
    r = real_auth_client.post(URL, json=_req(), headers={"X-API-Key": "key-b"})
    assert r.status_code == 202
    with sm() as s:
        assert [c.organization_id for c in s.query(Campaign)] == [ORG_B]
        assert [d.organization_id for d in s.query(ExternalDispatch)] == [ORG_B]
    # a body-supplied tenant is not a thing:
    r = real_auth_client.post(URL, json={**_req(idempotency_key="i2"), "organization_id": ORG_A},
                              headers={"X-API-Key": "key-b"})
    assert r.status_code == 422


def test_same_key_in_two_tenants_are_independent_and_not_discoverable(real_auth_client, sm, fake_llm):
    _queue_draft(fake_llm)
    _queue_draft(fake_llm)
    ra = real_auth_client.post(URL, json=_req(), headers={"X-API-Key": "key-a"})
    rb = real_auth_client.post(URL, json=_req(), headers={"X-API-Key": "key-b"})
    assert ra.status_code == rb.status_code == 202
    assert ra.json()["mailing_agent_reference"] != rb.json()["mailing_agent_reference"]
    c = _counts(sm)
    assert c["campaigns"] == 2 and c["dispatches"] == 2 and c["messages"] == 0   # accepted, not yet generated
    assert len(_generate(sm)) == 2 and _counts(sm)["messages"] == 2
    # org B cannot see org A's dispatch through reconciliation
    ref_a_key = "idem-1"
    assert real_auth_client.get(f"{EXACT_URL}/{ref_a_key}", headers={"X-API-Key": "key-b"}).status_code == 200
    assert real_auth_client.get(f"{EXACT_URL}/nope", headers={"X-API-Key": "key-b"}).status_code == 404


# ---------------------------------------------------------------- happy path / persistence shape

def test_accepts_without_llm_or_message_then_generation_creates_exactly_one(client, sm, fake_llm, send_spy):
    _queue_draft(fake_llm)
    r = client.post(URL, json=_req())
    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] is True and body["mailing_agent_reference"]

    # durable acceptance: no LLM call, no Message, nothing sent
    assert fake_llm.call_count == 0 and send_spy.calls == []
    assert _counts(sm) == {"campaigns": 1, "contacts": 1, "messages": 0, "dispatches": 1}
    with sm() as s:
        d = s.query(ExternalDispatch).one()
        assert d.public_reference == body["mailing_agent_reference"]
        assert d.state == S.QUEUED.value and d.message_id is None and d.mailbox_id is not None
        assert d.organization_id == ORG_A and d.external_action_id == "481"
        assert d.correlation_id == "corr-1"

    (res,) = _generate(sm)
    assert (res.outcome, res.persisted) == ("generated", True) and fake_llm.call_count == 1
    assert _counts(sm) == {"campaigns": 1, "contacts": 1, "messages": 1, "dispatches": 1}
    with sm() as s:
        d = s.query(ExternalDispatch).one()
        m = s.query(Message).one()
        assert d.state == S.QUEUED.value and d.message_id == m.id and d.claimed_by is None
        # the generated artifact is exactly what the dispatch worker will transmit
        assert m.status == MessageStatus.DRAFT.value
        assert m.subject == "Quick question about reconciliation" and m.body == BODY_A
    assert send_spy.calls == [] and fake_llm.call_count == 1


def test_sender_identity_is_the_deployment_integration_sender(client, sm, fake_llm):
    _queue_draft(fake_llm)
    assert client.post(URL, json=_req()).status_code == 202
    with sm() as s:
        c = s.query(Campaign).one()
        assert c.integration_source == "leadboost"
        assert c.sender_email == "outreach@mailer.example.com"
        assert (c.sender_name, c.sender_org) == ("Test Sender", "Test Org")


def test_request_context_is_not_persisted_on_shared_campaign_or_contact(client, sm, fake_llm):
    _queue_draft(fake_llm)
    assert client.post(URL, json=_req()).status_code == 202
    with sm() as s:
        c = s.query(Campaign).one()
        k = s.query(Contact).one()
        # shared rows carry none of this request's context
        assert VP_A not in (c.value_prop or "") and c.proof_points is None
        assert k.context_notes is None and k.title is None and k.company is None
        # identity fields only; no native scheduling
        assert (k.email, k.name) == ("jane@acme.example.com", "Jane Doe")
        assert k.next_action_at is None and k.status == ContactStatus.NEW.value
        # the one durable copy is the per-dispatch snapshot
        snap = s.query(ExternalDispatch).one().grounding_context
        assert snap["version"] == 2 and snap["value_prop"] == VP_A
        assert "Acme just opened a second finance office in Austin." in snap["context_notes"]
        assert snap["conversation_transcript"]            # Mailer-owned history (first-contact placeholder)
        assert snap["recipient"] == {"name": "Jane Doe", "title": "VP Finance", "company": "Acme"}


def test_generation_is_fed_this_requests_context_through_existing_prompt_stack(client, sm, fake_llm):
    _queue_draft(fake_llm)
    assert client.post(URL, json=_req()).status_code == 202
    _generate(sm)
    system_prompt, human_prompt = fake_llm.last_prompts[-1]
    prompt = system_prompt + "\n" + human_prompt
    for needle in (VP_A, FACTS_A[0], "Jane Doe", "VP Finance", "Acme", "Test Sender"):
        assert needle in prompt


def test_prompt_does_not_leak_the_placeholder_campaign_text(client, sm, fake_llm):
    _queue_draft(fake_llm)
    assert client.post(URL, json=_req()).status_code == 202
    _generate(sm)
    prompt = "\n".join(fake_llm.last_prompts[-1])
    assert "LeadBoost-authorized outreach" not in prompt


def test_existing_contact_conversation_history_is_used_not_caller_supplied(client, sm, fake_llm):
    _queue_draft(fake_llm)
    assert client.post(URL, json=_req()).status_code == 202
    _generate(sm)
    with sm() as s:   # a real prior message in Mailer's own table
        k = s.query(Contact).one()
        s.add(Message(contact_id=k.id, direction="inbound", subject="Re: hi",
                      body="Please send details about onboarding.", status="received",
                      message_type="reply"))
        s.commit()
    _queue_draft(fake_llm, body="Hi Jane,\n\nWorth a quick chat?\n\nBest")
    assert client.post(URL, json=_req(idempotency_key="idem-2", external_action_id="482")).status_code == 202
    _generate(sm)
    human = fake_llm.last_prompts[-1][1]
    assert "Please send details about onboarding." in human
    assert _counts(sm)["contacts"] == 1                            # reused, not duplicated


# ---------------------------------------------------------------- idempotency

def test_replay_returns_same_operation_without_llm_or_mutation(client, sm, fake_llm):
    _queue_draft(fake_llm)
    first = client.post(URL, json=_req())
    before = _counts(sm)
    # a safe retry may even carry refined/changed descriptive context: same operation
    retry = _req(context={"value_proposition": VP_B, "recipient_facts": []},
                 correlation_id="corr-other")
    second = client.post(URL, json=retry)
    assert second.status_code == 202
    assert second.json() == first.json()
    assert fake_llm.call_count == 0 and _counts(sm) == before
    with sm() as s:
        assert s.query(ExternalDispatch).one().grounding_context["value_prop"] == VP_A   # not overwritten


@pytest.mark.parametrize("mutate", [
    lambda r: r.__setitem__("external_action_id", "999"),
    lambda r: r["recipient"].__setitem__("email", "other@acme.example.com"),
])
def test_same_key_different_operation_is_409_and_mutates_nothing(client, sm, fake_llm, mutate):
    assert client.post(URL, json=_req()).status_code == 202
    before = _counts(sm)
    conflicting = _req()
    mutate(conflicting)
    r = client.post(URL, json=conflicting)
    assert r.status_code == 409
    assert fake_llm.call_count == 0 and _counts(sm) == before


def test_key_first_used_on_the_exact_message_route_conflicts_here(client, sm, fake_llm):
    r = client.post(EXACT_URL, json={
        "external_action_id": "481", "idempotency_key": "idem-1", "correlation_id": None,
        "recipient": {"email": "jane@acme.example.com", "name": "Jane Doe"},
        "message": {"subject": "Hi", "body": "Hello there"},
    })
    assert r.status_code == 202
    r = client.post(URL, json=_req())
    assert r.status_code == 409
    assert fake_llm.call_count == 0 and _counts(sm)["dispatches"] == 1


def test_fingerprint_depends_only_on_operation_identity():
    f = gen._compute_generated_request_fingerprint
    assert f(external_action_id="1", recipient_email="a@b.com") == f(external_action_id="1", recipient_email="a@b.com")
    assert f(external_action_id="1", recipient_email="a@b.com") != f(external_action_id="2", recipient_email="a@b.com")
    assert f(external_action_id="1", recipient_email="a@b.com") != f(external_action_id="1", recipient_email="c@b.com")


# ---------------------------------------------------------------- generation failure / grounding

def test_provider_failure_after_acceptance_is_failed_without_message_or_send(client, sm, fake_llm, send_spy):
    fake_llm.queue_error(RuntimeError("provider exploded"))
    assert client.post(URL, json=_req()).status_code == 202        # acceptance never depended on the LLM
    (res,) = _generate(sm)
    assert (res.outcome, res.persisted) == ("failed", True)
    with sm() as s:
        d = s.query(ExternalDispatch).one()
        assert d.state == S.FAILED.value and d.message_id is None and d.claimed_by is None
        assert d.error_message.startswith("generation_failed")
        assert s.query(Message).count() == 0
    assert send_spy.calls == [] and _generate(sm) == []              # never regenerated or retried
    # a retry is a NEW idempotency key (same rule as any FAILED dispatch); the same key replays the failure
    assert client.post(URL, json=_req()).json()["mailing_agent_reference"] == _ref(sm, "idem-1")
    _queue_draft(fake_llm)
    assert client.post(URL, json=_req(idempotency_key="idem-new")).status_code == 202
    assert [r.outcome for r in _generate(sm)] == ["generated"]


def _ref(sm, key):
    with sm() as s:
        return s.query(ExternalDispatch).filter_by(idempotency_key=key).one().public_reference


def test_llm_unavailable_uses_the_existing_deterministic_fallback_with_this_requests_offer(client, sm, fake_llm):
    # draft_message's normal initial-outreach fallback, unchanged: it is built from
    # the transient campaign's value_prop, i.e. THIS request's, not the placeholder.
    fake_llm.queue_error(LLMUnavailableError("down"))
    assert client.post(URL, json=_req()).status_code == 202
    assert [r.outcome for r in _generate(sm)] == ["generated"]
    with sm() as s:
        m = s.query(Message).one()
        assert "LeadBoost-authorized outreach" not in m.body
        assert s.query(ExternalDispatch).one().grounding_context["value_prop"] == VP_A


def test_draft_that_grounding_hard_blocks_is_failed_with_no_message(client, sm, fake_llm, send_spy):
    # claims a figure that neither the offer nor the facts support
    _queue_draft(fake_llm, body="Hi Jane,\n\nWe saved Acme-sized teams $90,000 last year.\n\nBest")
    r = client.post(URL, json=_req(context={"value_proposition": "We help finance teams.", "recipient_facts": []}))
    assert r.status_code == 202
    (res,) = _generate(sm)
    assert res.outcome == "failed"
    with sm() as s:
        d = s.query(ExternalDispatch).one()
        assert d.state == S.FAILED.value and d.message_id is None
        assert d.error_message.startswith("generation_grounding_blocked")
        assert s.query(Message).count() == 0
    assert send_spy.calls == []


def test_a_claim_supported_by_the_requests_own_facts_is_generated(client, sm, fake_llm):
    _queue_draft(fake_llm, body="Hi Jane,\n\nCongrats on raising $50,000 in seed funding.\n\nBest")
    r = client.post(URL, json=_req(context={
        "value_proposition": "We help finance teams.", "recipient_facts": ["Acme raised $50,000 in seed funding."]}))
    assert r.status_code == 202
    assert [x.outcome for x in _generate(sm)] == ["generated"]


# ---------------------------------------------------------------- contact handling

def test_new_contact_has_no_native_follow_up_and_existing_one_is_not_clobbered(client, sm, fake_llm):
    assert client.post(URL, json=_req()).status_code == 202
    with sm() as s:
        k = s.query(Contact).one()
        assert k.next_action_at is None
        s.execute(text("UPDATE contacts SET status='active', next_action_at=:t"), {"t": "2030-01-01 00:00:00"})
        s.commit()
    r = client.post(URL, json=_req(idempotency_key="idem-2", external_action_id="482"))
    # the existing active-follow-up conflict guard is preserved: nothing mutated
    assert r.status_code == 409
    with sm() as s:
        assert s.query(Contact).one().next_action_at is not None
        assert s.query(ExternalDispatch).count() == 1


# ---------------------------------------------------------------- immutable per-dispatch grounding

def _post(client, **over):
    return client.post(URL, json=_req(**over))


def test_request_a_and_b_keep_their_own_grounding_snapshots(client, sm, fake_llm):
    _queue_draft(fake_llm, body=BODY_A)
    _queue_draft(fake_llm, body=BODY_B, subject="Support triage")
    ra = _post(client, idempotency_key="A", external_action_id="1")
    rb = _post(client, idempotency_key="B", external_action_id="2",
               recipient={"email": "sam@globex.example.com", "name": "Sam"},
               context={"value_proposition": VP_B, "recipient_facts": FACTS_B})
    assert ra.status_code == rb.status_code == 202
    assert [r.outcome for r in _generate(sm)] == ["generated", "generated"]   # oldest first: A then B
    with sm() as s:
        da = s.query(ExternalDispatch).filter_by(idempotency_key="A").one()
        db_ = s.query(ExternalDispatch).filter_by(idempotency_key="B").one()
        ma, mb = s.get(Message, da.message_id), s.get(Message, db_.message_id)
        ca, cb, camp = s.get(Contact, da.contact_id), s.get(Contact, db_.contact_id), s.get(Campaign, da.campaign_id)
        assert da.grounding_context["value_prop"] == VP_A and db_.grounding_context["value_prop"] == VP_B
        assert FACTS_A[0] in da.grounding_context["context_notes"]
        assert FACTS_B[0] in db_.grounding_context["context_notes"]
        # A's message is grounded by A's snapshot and NOT by B's (and vice versa) ...
        assert not evaluate_exact_message_grounding(ma, ca, camp, da.grounding_context).blocked
        assert not evaluate_exact_message_grounding(mb, cb, camp, db_.grounding_context).blocked
        assert evaluate_exact_message_grounding(ma, ca, camp, db_.grounding_context).blocked
        assert evaluate_exact_message_grounding(mb, cb, camp, da.grounding_context).blocked
        # ... and the shared rows could not have supplied either context
        assert camp.proof_points is None and ca.context_notes is None and cb.context_notes is None


def test_worker_grounds_against_the_snapshot_not_later_shared_row_changes(client, sm, fake_llm, send_spy, monkeypatch):
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    _queue_draft(fake_llm, body=BODY_A)
    assert _post(client, idempotency_key="A").status_code == 202
    assert [r.outcome for r in _generate(sm)] == ["generated"]
    # "request B" arrives later and (were context shared) would have replaced it:
    with sm() as s:
        s.query(Campaign).update({"value_prop": VP_B, "proof_points": "Totally different proof."})
        s.query(Contact).update({"context_notes": "Unrelated notes entirely."})
        s.commit()
    res = w.process_next_external_dispatch(session_factory=TrackingFactory(sm), worker_id="w-1",
                                           runtime=w.DispatchRuntime())
    assert res.outcome == "sent" and len(send_spy.calls) == 1
    assert send_spy.calls[0]["body_text"] == BODY_A


def test_worker_ignores_shared_row_support_when_the_snapshot_does_not_have_it(client, sm, fake_llm, send_spy, monkeypatch):
    """Converse: shared Campaign/Contact rows must never rescue a message the
    dispatch's own snapshot does not ground."""
    monkeypatch.setattr(w.settings, "live_sending_enabled", True)
    _queue_draft(fake_llm, body=BODY_A)
    assert _post(client, idempotency_key="A").status_code == 202
    assert [r.outcome for r in _generate(sm)] == ["generated"]
    with sm() as s:
        # Core UPDATE (bypasses the ORM write-once guard) to simulate a corrupted snapshot
        s.execute(text("UPDATE external_dispatches SET grounding_context = :g"),
                  {"g": '{"version": 1, "value_prop": "We help.", "context_notes": null, "conversation_transcript": null}'})
        s.query(Campaign).update({"value_prop": VP_A})
        s.query(Contact).update({"context_notes": FACTS_A[0]})
        s.commit()
    res = w.process_next_external_dispatch(session_factory=TrackingFactory(sm), worker_id="w-1",
                                           runtime=w.DispatchRuntime())
    assert res.outcome == "failed" and send_spy.calls == []


def test_legacy_dispatch_without_snapshot_still_grounds_against_shared_rows(sm):
    from tests.dispatch_support import seed_dispatch
    with sm() as s:
        d = seed_dispatch(s, proof_points="We cut costs by 40%.", body="We cut costs by 40%.")
        assert d.grounding_context is None
        m, k, c = s.get(Message, d.message_id), s.get(Contact, d.contact_id), s.get(Campaign, d.campaign_id)
        assert not evaluate_exact_message_grounding(m, k, c).blocked
        assert not evaluate_exact_message_grounding(m, k, c, None).blocked


def test_grounding_context_is_write_once_at_the_orm_level(client, sm, fake_llm):
    assert _post(client).status_code == 202
    with sm() as s:
        d = s.query(ExternalDispatch).one()
        d.grounding_context = {"version": 1, "value_prop": "rewritten"}
        with pytest.raises(ValueError, match="immutable"):
            s.commit()
        s.rollback()
        # ordinary lifecycle updates leave it alone and are allowed
        d = s.query(ExternalDispatch).one()
        d.state = S.SENDING.value
        s.commit()
        assert s.query(ExternalDispatch).one().grounding_context["value_prop"] == VP_A


# ---------------------------------------------------------------- integration message cannot be approved natively

def test_message_created_by_the_endpoint_cannot_be_approved_natively(client, sm, fake_llm, send_spy, monkeypatch):
    from mailer_agent.api import messages as messages_module
    legacy = FakeSender()
    monkeypatch.setattr(messages_module, "send_email", legacy)
    _queue_draft(fake_llm)
    assert _post(client).status_code == 202
    assert [r.outcome for r in _generate(sm)] == ["generated"]
    with sm() as s:
        mid = s.query(Message).one().id
    r = client.post(f"/messages/{mid}/approve")
    assert r.status_code == 409 and "external integration" in r.json()["detail"]
    assert legacy.calls == [] and send_spy.calls == []
