"""
Tenant-safe inbound correlation (spec section 17).

Exercises the real inbound-processing path (mail.reply_handler_v2.
process_inbound_email_v2) against a fully isolated, in-process SQLite
database seeded with two organizations that both have a contact using
the *same* email address -- the scenario where cross-tenant leakage
would actually show up. This is not a code-inspection assertion; it
persists real rows and calls the real function end to end.

Isolation note: this file does NOT import mailer_agent.db (whose engine
is a module-level singleton bound once, at first import, to whatever
DATABASE_URL happened to be set by whichever test file's import ran
first in the session -- a real fragility other test files here work
around with `os.environ.setdefault(...)` before their first
mailer_agent import). Instead it builds its own SQLAlchemy engine/session
directly and passes that Session into process_inbound_email_v2, which
takes a Session as a plain parameter. That sidesteps the shared-singleton
problem entirely rather than depending on import order.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mailer_agent.mail.imap_reader import InboundEmail
from mailer_agent.mail.reply_handler_v2 import process_inbound_email_v2
from mailer_agent.models import Base, Campaign, Contact


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _force_deterministic_fallback(monkeypatch):
    """
    This test is about tenant correlation, not classification content --
    force the deterministic (non-LLM) classification path so the test
    doesn't depend on fake_llm fixtures at all.
    """
    import mailer_agent.llm.provider_v2 as provider_module
    monkeypatch.setattr(provider_module, "is_llm_available", lambda: False)


def _seed_two_orgs_with_colliding_contact_email(db_session):
    """
    Two different organizations, each running their own campaign, each
    with a contact using the identical email address -- the exact
    scenario where a naive "look up contact by email" fallback would
    misattribute an inbound reply to the wrong organization.
    """
    campaign_a = Campaign(
        name="Org A Campaign",
        organization_id="org-a",
        sender_name="Alice",
        sender_org="Org A Inc",
        sender_email="alice@org-a.example.com",
        value_prop="Org A's value prop",
    )
    campaign_b = Campaign(
        name="Org B Campaign",
        organization_id="org-b",
        sender_name="Bob",
        sender_org="Org B Inc",
        sender_email="bob@org-b.example.com",
        value_prop="Org B's value prop",
    )
    db_session.add_all([campaign_a, campaign_b])
    db_session.flush()

    # Same prospect email address, contacted by both organizations
    # independently (a realistic collision -- e.g. a shared inbox, or
    # simply the same person being prospected by two different sellers).
    contact_a = Contact(
        campaign_id=campaign_a.id,
        name="Prospect (Org A's contact)",
        email="prospect@shared-prospect.example.com",
        status="active",
    )
    contact_b = Contact(
        campaign_id=campaign_b.id,
        name="Prospect (Org B's contact)",
        email="prospect@shared-prospect.example.com",
        status="active",
    )
    db_session.add_all([contact_a, contact_b])
    db_session.commit()

    return campaign_a, campaign_b, contact_a, contact_b


def test_inbound_reply_with_known_inbox_resolves_to_correct_org(db_session):
    """
    When the webhook/IMAP source tells us which inbox (to_email) received
    the reply, that must be used to resolve the correct organization even
    when another organization has a contact with the identical email
    address.
    """
    campaign_a, campaign_b, contact_a, contact_b = _seed_two_orgs_with_colliding_contact_email(db_session)

    email_in = InboundEmail(
        from_email="prospect@shared-prospect.example.com",
        subject="Re: intro",
        body_text="Thanks, tell me more.",
        message_id=None,
        in_reply_to=None,
        references=[],
        to_email="alice@org-a.example.com",  # received at Org A's inbox
    )

    result = process_inbound_email_v2(db_session, email_in, organization_id=None)

    assert result["matched"] is True
    assert result["contact_id"] == contact_a.id, (
        "Reply received at Org A's inbox must attribute to Org A's contact, "
        "never Org B's, even though both contacts share an email address."
    )

    # And the reverse: the same prospect replying to Org B's inbox
    # attributes to Org B's contact.
    email_in_b = InboundEmail(
        from_email="prospect@shared-prospect.example.com",
        subject="Re: intro",
        body_text="Thanks, tell me more.",
        message_id=None,
        in_reply_to=None,
        references=[],
        to_email="bob@org-b.example.com",
    )
    result_b = process_inbound_email_v2(db_session, email_in_b, organization_id=None)
    assert result_b["contact_id"] == contact_b.id


def test_inbound_reply_correlates_by_thread_header_regardless_of_email_collision(db_session):
    """
    The threading-header correlation path (In-Reply-To / References
    matching a prior outbound Message-ID) is inherently tenant-safe: a
    Message-ID is unique to one persisted outbound message, which belongs
    to exactly one contact/campaign/org. It should win even without
    to_email, and even with a colliding contact email on another org.
    """
    campaign_a, campaign_b, contact_a, contact_b = _seed_two_orgs_with_colliding_contact_email(db_session)

    from mailer_agent.models import Message, MessageDirection, MessageStatus, MessageType

    prior_outbound = Message(
        contact_id=contact_a.id,
        direction=MessageDirection.OUTBOUND.value,
        message_type=MessageType.INITIAL.value,
        subject="Intro",
        body="Hi there",
        status=MessageStatus.SENT.value,
        message_id_header="<abc123@org-a.example.com>",
    )
    db_session.add(prior_outbound)
    db_session.commit()

    email_in = InboundEmail(
        from_email="prospect@shared-prospect.example.com",
        subject="Re: Intro",
        body_text="Thanks, tell me more.",
        message_id=None,
        in_reply_to="<abc123@org-a.example.com>",
        references=["<abc123@org-a.example.com>"],
        to_email=None,  # deliberately absent -- threading header must still resolve correctly
    )

    result = process_inbound_email_v2(db_session, email_in, organization_id=None)

    assert result["matched"] is True
    assert result["contact_id"] == contact_a.id


def test_ambiguous_correlation_is_not_guessed(db_session, caplog):
    """
    Spec requirement (section 33): "Never globally choose an arbitrary
    contact only because the email address matches. If correlation
    remains ambiguous: mark unresolved or human review."

    Two organizations both have a contact with the same email address.
    The inbound email has NO thread-header match (first-ever reply from
    this address, or headers stripped by some relay) AND no to_email
    (e.g. a webhook payload that doesn't tell us which inbox received
    it). There is genuinely no reliable signal for which organization
    this belongs to -- the correct behavior is to leave it unresolved,
    never to pick one by a tiebreaker like "most recently updated".
    """
    import logging
    campaign_a, campaign_b, contact_a, contact_b = _seed_two_orgs_with_colliding_contact_email(db_session)

    email_in = InboundEmail(
        from_email="prospect@shared-prospect.example.com",
        subject="Hello",
        body_text="Following up on your message.",
        message_id=None,
        in_reply_to=None,
        references=[],
        to_email=None,
    )

    with caplog.at_level(logging.WARNING):
        result = process_inbound_email_v2(db_session, email_in, organization_id=None)

    assert result["matched"] is False, (
        "Ambiguous correlation must be left unresolved, not guessed at -- "
        f"got: {result}"
    )
    assert any("ambiguous" in r.message.lower() for r in caplog.records), (
        "Ambiguous correlation should be logged clearly for ops follow-up"
    )

    # And confirm nothing was persisted against either contact as a side effect.
    from mailer_agent.models import Message
    assert db_session.query(Message).filter(Message.contact_id.in_([contact_a.id, contact_b.id])).count() == 0


def test_single_unambiguous_email_match_without_to_email_still_resolves(db_session):
    """
    The ambiguity guard must not become overly conservative: if only ONE
    contact anywhere has this from_email (the common case -- most email
    addresses are not simultaneously being prospected by two different
    organizations), correlation should still succeed even without
    to_email or a thread-header match.
    """
    campaign = Campaign(
        name="Solo Org Campaign",
        organization_id="org-solo",
        sender_name="Casey",
        sender_org="Solo Org Inc",
        sender_email="casey@solo-org.example.com",
        value_prop="Solo org's value prop",
    )
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        name="Unique Prospect",
        email="unique-prospect@example.com",
        status="active",
    )
    db_session.add(contact)
    db_session.commit()

    email_in = InboundEmail(
        from_email="unique-prospect@example.com",
        subject="Hello",
        body_text="Following up.",
        message_id=None,
        in_reply_to=None,
        references=[],
        to_email=None,
    )

    result = process_inbound_email_v2(db_session, email_in, organization_id=None)

    assert result["matched"] is True
    assert result["contact_id"] == contact.id


def test_same_email_via_webhook_then_imap_is_deduplicated_despite_format_difference(db_session):
    """
    Spec section 35: the same message arriving via webhook and then via
    IMAP polling (a realistic double-delivery: e.g. the webhook fires,
    but the reply also sits unseen in the mailbox and gets picked up by
    the next poll before whatever suppression the provider offers kicks
    in) must produce exactly one logical inbound message -- not two.

    The subtlety this test targets specifically: IMAP preserves the raw
    `Message-ID` header, which normally includes angle brackets
    (e.g. "<abc123@prospect.example.com>"). A webhook JSON payload has
    no such guarantee -- a provider or a hand-rolled payload template
    might send the bare id without brackets. If the two channels'
    representations of the *same* Message-ID aren't normalized to a
    common form before the dedup check runs, this scenario would
    silently create two inbound messages (and could trigger two separate
    auto-replies) instead of being caught as a duplicate.
    """
    campaign = Campaign(
        name="Dedup Test Campaign",
        organization_id="org-dedup",
        sender_name="Dana",
        sender_org="Dedup Test Inc",
        sender_email="dana@dedup-test.example.com",
        value_prop="Dedup test value prop",
    )
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        name="Reply Sender",
        email="replier@example.com",
        status="active",
    )
    db_session.add(contact)
    db_session.commit()

    # First delivery: webhook payload, bare Message-ID with no angle brackets
    # (a realistic shape for a provider/template that strips them).
    email_via_webhook = InboundEmail(
        from_email="replier@example.com",
        subject="Re: intro",
        body_text="Sounds good, tell me more.",
        message_id="dupe-check-123@prospect.example.com",  # no angle brackets
        in_reply_to=None,
        references=[],
        to_email="dana@dedup-test.example.com",
    )
    result1 = process_inbound_email_v2(db_session, email_via_webhook, organization_id=None)
    assert result1["matched"] is True
    assert result1["action"] != "skipped_duplicate"

    # Second delivery: the identical email, now arriving via IMAP, whose
    # raw header parsing yields the angle-bracketed form of the SAME id.
    email_via_imap = InboundEmail(
        from_email="replier@example.com",
        subject="Re: intro",
        body_text="Sounds good, tell me more.",
        message_id="<dupe-check-123@prospect.example.com>",  # with brackets
        in_reply_to=None,
        references=[],
        to_email="dana@dedup-test.example.com",
    )
    result2 = process_inbound_email_v2(db_session, email_via_imap, organization_id=None)

    assert result2["matched"] is True
    assert result2["action"] == "skipped_duplicate", (
        "Same Message-ID in a different (but equivalent) string format "
        f"must be recognized as a duplicate, not processed again. Got: {result2}"
    )

    from mailer_agent.models import Message, MessageDirection
    inbound_count = (
        db_session.query(Message)
        .filter(Message.contact_id == contact.id, Message.direction == MessageDirection.INBOUND.value)
        .count()
    )
    assert inbound_count == 1, "Exactly one inbound message should be persisted, not two"
