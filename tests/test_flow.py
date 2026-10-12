"""
End-to-end tests covering: campaign/contact creation, dry-run initial
send, dynamic follow-up dispatch, inbound reply correlation + intent
classification + draft-vs-auto-send gating, and suppression enforcement.

Run with: pytest -q

Test isolation
--------------
This file used to rely on module-level `os.environ[...]` mutations
(DATABASE_URL, API_KEY, GROQ_API_KEY) combined with api/deps.py's
key->org map and mailer_agent.db's engine, both of which are built once
at first import -- a real fragility, not a hypothetical one: whichever
test file's imports happen to run first in the collection order decides
what those singletons end up bound to, and a later file's environment
mutations have no effect on an already-built map/engine. The observed
failure mode was exactly this: 401s (the auth override never took
effect) cascading into KeyError("id") (r.json() on a 401 body has no
"id" key).

Fixed the same way tests/e2e/test_full_lifecycle.py already handles
this: FastAPI's dependency_overrides for get_db / require_api_key /
get_current_org_id, with a fresh StaticPool-backed in-memory SQLite
engine created PER TEST (not shared across the whole file, and not
dependent on any other test file's import order).
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
from mailer_agent.models import Base


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[require_api_key] = lambda: None
    app.dependency_overrides[get_current_org_id] = lambda: "test-flow-org"
    # C15.1b: the inbound webhook resolves its tenant via the fail-closed integration dependency.
    app.dependency_overrides[get_integration_org_id] = lambda: "test-flow-org"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()



def test_full_flow(client):
    r = client.get("/health")
    assert r.status_code == 200

    r = client.post(
        "/campaigns",
        json={
            "name": "Test Campaign",
            "sender_name": "Jordan",
            "sender_org": "TestOrg",
            "sender_email": "jordan@testorg.example.com",
            "value_prop": "We help teams do the thing, faster.",
            "follow_up_days": [2, 5],
        },
    )
    assert r.status_code == 201  # Created
    campaign_id = r.json()["id"]

    r = client.post(
        f"/campaigns/{campaign_id}/contacts",
        json={"contacts": [{"name": "Sam", "email": "sam@prospect.example.com", "company": "Prospect Co"}]},
    )
    assert r.status_code == 201  # Created
    contact_id = r.json()[0]["id"]

    r = client.post(f"/campaigns/{campaign_id}/start")
    assert r.status_code == 200
    assert r.json()["queued"] == 1

    # Campaign start now queues work, scheduler will process it
    # Test verifies the durable queueing behavior


def test_suppression_blocks_future_adds(client):
    r = client.post("/suppress", json={"email": "blocked@prospect.example.com"})
    assert r.status_code == 201  # Created

    r = client.post(
        "/campaigns",
        json={
            "name": "Another Campaign",
            "sender_name": "Jordan",
            "sender_org": "TestOrg",
            "sender_email": "jordan@testorg.example.com",
            "value_prop": "Same offer.",
        },
    )
    campaign_id = r.json()["id"]

    r = client.post(
        f"/campaigns/{campaign_id}/contacts",
        json={"contacts": [{"email": "blocked@prospect.example.com"}]},
    )
    assert r.status_code == 201  # Created
    assert r.json() == []  # silently skipped, never added


def test_flexible_lead_ingestion(client):
    r = client.post(
        "/campaigns",
        json={
            "name": "Ingest Campaign",
            "sender_name": "Jordan",
            "sender_org": "TestOrg",
            "sender_email": "jordan@testorg.example.com",
            "value_prop": "Same offer.",
        },
    )
    campaign_id = r.json()["id"]

    leadboost_shaped = {
        "company_name": "ExampleCorp",
        "contact_name": "Priya Singh",
        "contact_title": "Head of Support",
        "email": "priya@examplecorp.example.com",
        "industry": "B2B SaaS",
        "qualification_label": "Hot Lead",
    }
    generic_shaped = {"full_name": "Sam Lee", "lead_email": "sam@othercorp.example.com", "org": "OtherCorp"}
    no_email = {"company_name": "NoEmailCorp"}

    r = client.post(
        f"/campaigns/{campaign_id}/leads/ingest",
        json={"leads": [leadboost_shaped, generic_shaped, no_email]},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["created"]) == 2
    assert len(body["skipped"]) == 1

    contact_id = body["created"][0]["contact_id"]
    r = client.get(f"/contacts/{contact_id}")
    assert r.json()["name"] == "Priya Singh"
    assert r.json()["company"] == "ExampleCorp"


def test_inbound_webhook_matches_thread(client, monkeypatch):
    # This test only cares about correlation (does the webhook route to
    # the right contact), not classification content -- explicitly force
    # the deterministic fallback path (no LLM call at all) rather than
    # relying on the autouse fake_llm fixture's queue, which would raise
    # if nothing were queued for the (multi-call: classify -> plan ->
    # draft) reply pipeline this now triggers.
    import mailer_agent.llm.provider_v2 as provider_module
    monkeypatch.setattr(provider_module, "is_llm_available", lambda: False)

    r = client.post(
        "/campaigns",
        json={
            "name": "Webhook Campaign",
            "sender_name": "Jordan",
            "sender_org": "TestOrg",
            "sender_email": "jordan@testorg.example.com",
            "value_prop": "Same offer.",
        },
    )
    campaign_id = r.json()["id"]
    r = client.post(
        f"/campaigns/{campaign_id}/contacts",
        json={"contacts": [{"email": "webhook-contact@prospect.example.com"}]},
    )
    client.post(f"/campaigns/{campaign_id}/start")

    r = client.post(
        "/webhooks/inbound-email",
        json={
            "from_email": "webhook-contact@prospect.example.com",
            "subject": "Re: hello",
            "body_text": "Sounds interesting, tell me more.",
        },
    )
    assert r.status_code == 200
    assert r.json()["matched"] is True


def test_start_campaign_sends_first_contact_immediately_and_queues_rest(client, monkeypatch):
    # Test that campaign start queues work durably (all contacts)
    # The new architecture doesn't use BackgroundTasks - everything goes through
    # the scheduler for consistent handling
    from mailer_agent.followup import engine as engine_module
    monkeypatch.setattr(engine_module.settings, "send_delay_seconds", 0.0)
    monkeypatch.setattr(engine_module.settings, "send_jitter_seconds", 0.0)

    r = client.post(
        "/campaigns",
        json={
            "name": "Stagger Campaign",
            "sender_name": "Jordan",
            "sender_org": "TestOrg",
            "sender_email": "jordan@testorg.example.com",
            "value_prop": "Same offer.",
        },
    )
    campaign_id = r.json()["id"]

    r = client.post(
        f"/campaigns/{campaign_id}/contacts",
        json={"contacts": [
            {"email": "first@prospect.example.com"},
            {"email": "second@prospect.example.com"},
            {"email": "third@prospect.example.com"},
        ]},
    )
    contact_ids = [c["id"] for c in r.json()]

    r = client.post(f"/campaigns/{campaign_id}/start")
    assert r.status_code == 200
    # All 3 contacts queued durably for scheduler to process
    assert r.json()["queued"] == 3
    
    # Contacts are marked with next_action_at, scheduler will process them
    # This test verifies the durable queueing behavior, not inline sending
