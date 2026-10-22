"""PR4 -- inbound observability: handler-outcome tallies and /system/metrics/inbound.

Drives the REAL mailbox poller and the REAL inbound handler through the in-memory fake IMAP world
(tests/m3_support.py). The tallies are compared with (a) what the handler actually returned for every
message and (b) what actually landed in the database, so the numbers cannot drift from reality.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from mailer_agent.api.main import app
from mailer_agent.db import get_db
from mailer_agent.followup import scheduler as sched
from mailer_agent.mail import imap_reader, mailbox_inbound
from mailer_agent.mail.mailbox_inbound import summarize_mailbox_cycle
from mailer_agent.models import (
    Campaign,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    WorkerCycleStatus,
)
from mailer_agent.worker_status import (
    BUCKET_AMBIGUOUS,
    BUCKET_DUPLICATE,
    BUCKET_PERSISTED,
    BUCKET_UNMATCHED,
    BUCKET_UNRECOGNIZED,
    KIND_INBOUND_MAILBOX,
    WorkerStatusRecorder,
    classification_flag,
    classify_inbound_result,
)
from tests.m3_support import (
    FakeImapWorld,
    inbound_rows,
    make_session_factory,
    raw_email,
    seed_org_with_imap_mailbox,
)
from tests.pr4_support import NOW, ago, capture_sql, install_operator_app

URL = "/system/metrics/inbound"
HOST, USER, PW = "imap.a.example", "user-a@a.example", "PwA-Imap-Secret-1111"
OUT_A = "<out-a@mailer.a>"
LEAK_FROM = "leaky.sender@leak-example.test"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    sm, eng = make_session_factory(tmp_path)
    world = FakeImapWorld()
    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL", world.connection_factory())
    import mailer_agent.llm.provider_v2 as provider_module

    monkeypatch.setattr(provider_module, "is_llm_available", lambda: False)  # deterministic classification
    world.add_account(HOST, USER, PW)
    with sm() as db:
        mb, ct, _ = seed_org_with_imap_mailbox(
            db, org="org-a", host=HOST, imap_user=USER, imap_password=PW,
            contact_email="lead@example.com", outbound_message_id=OUT_A)
        ids = SimpleNamespace(mailbox=mb.id, contact=ct.id, campaign=ct.campaign_id)
    client, _headers, get = install_operator_app(monkeypatch, sm)
    rec = WorkerStatusRecorder("worker", session_factory=sm)
    monkeypatch.setattr(sched, "_runtime_recorder", rec)
    monkeypatch.setattr(sched, "poll_all_mailboxes_report", lambda: mailbox_inbound.poll_all_mailboxes_report(sm))
    try:
        yield SimpleNamespace(sm=sm, world=world, ids=ids, client=client, get=get, rec=rec)
    finally:
        app.dependency_overrides.pop(get_db, None)
        eng.dispose()


def add_ambiguity(env):
    """A second contact with the SAME address in another campaign of the same org."""
    with env.sm() as db:
        camp = Campaign(name="second", organization_id="org-a", sender_name="n", sender_org="o",
                        sender_email="s2@example.org", value_prop="vp")
        db.add(camp)
        db.flush()
        db.add(Contact(campaign_id=camp.id, email="lead@example.com"))
        db.commit()


def capture_handler_results(monkeypatch):
    """Record, verbatim, every dict the real inbound handler returns."""
    seen: list[dict] = []
    real = mailbox_inbound._persist_inbound

    def spy(factory, target, email_in):
        result = real(factory, target, email_in)
        seen.append(result)
        return result

    monkeypatch.setattr(mailbox_inbound, "_persist_inbound", spy)
    return seen


def inject(env, **kw):
    return env.world.inject(HOST, USER, raw_email(**kw))


def cycle_row(env, kind=KIND_INBOUND_MAILBOX):
    with env.sm() as s:
        return s.execute(select(WorkerCycleStatus).where(WorkerCycleStatus.kind == kind)).scalar_one_or_none()


def stored_inbound(env):
    with env.sm() as s:
        return inbound_rows(s)


# ------------------------------------------------------------------ the pure bucketing function
def test_classify_inbound_result_matches_every_real_handler_shape():
    assert classify_inbound_result({"matched": True, "action": "skipped_duplicate", "message_id": 1}) == BUCKET_DUPLICATE
    assert classify_inbound_result({"matched": False, "action": "unmatched_no_match", "reason": "no_match"}) == BUCKET_UNMATCHED
    assert classify_inbound_result({"matched": False, "action": "unmatched_ambiguous", "reason": "ambiguous"}) == BUCKET_AMBIGUOUS
    assert classify_inbound_result({"matched": True, "action": "replied", "classification_success": True}) == BUCKET_PERSISTED
    assert classify_inbound_result({"matched": False, "from_email": "a@b.c"}) == BUCKET_UNMATCHED  # legacy: no reason
    for junk in (None, [], "x", 3, {}, {"matched": "yes"}):
        assert classify_inbound_result(junk) == BUCKET_UNRECOGNIZED  # never guessed into another bucket
    assert classification_flag({"matched": True, "classification_success": True}) is True
    assert classification_flag({"matched": True, "classification_success": False}) is False
    assert classification_flag({"matched": True}) is None
    assert classification_flag({"matched": True, "action": "skipped_duplicate", "classification_success": True}) is None
    assert classification_flag({"matched": False, "classification_success": True}) is None


# ------------------------------------------------------------------ tallies agree with the REAL handler
def test_tallies_agree_with_what_the_real_handler_returned_and_what_the_database_holds(env, monkeypatch):
    add_ambiguity(env)
    results = capture_handler_results(monkeypatch)
    # 1 threaded reply -> persisted ; the same Message-ID twice -> persisted + duplicate ;
    # unknown sender -> unmatched ; known address with no thread refs but two candidates -> ambiguous
    inject(env, message_id="<r1@p>", in_reply_to=OUT_A)
    inject(env, message_id="<r1@p>", in_reply_to=OUT_A)
    inject(env, from_addr="stranger@nowhere.example", message_id="<s1@p>", in_reply_to=None)
    inject(env, from_addr="lead@example.com", message_id="<amb@p>", in_reply_to=None)

    report = mailbox_inbound.poll_all_mailboxes_report(env.sm)
    outcome, counts, _err = summarize_mailbox_cycle(report)
    assert len(results) == 4

    # expected, derived INDEPENDENTLY from the handler's documented result contract
    expected = Counter()
    for r in results:
        if r.get("action") == "skipped_duplicate":
            expected["handled_duplicate"] += 1
        elif r["matched"] is False and r.get("reason") == "ambiguous":
            expected["handled_ambiguous"] += 1
        elif r["matched"] is False:
            expected["handled_unmatched"] += 1
        else:
            expected["handled_persisted"] += 1
            expected["classification_succeeded" if r.get("classification_success") else "classification_failed"] += 1
    for key in ("handled_persisted", "handled_duplicate", "handled_unmatched", "handled_ambiguous",
                "classification_succeeded", "classification_failed"):
        assert counts[key] == expected[key], (key, counts, results)
    assert counts["handled_unrecognized"] == 0
    # the scenario really exercised every outcome (not a vacuous pass)
    assert counts["handled_persisted"] == 1 and counts["handled_duplicate"] == 1
    assert counts["handled_unmatched"] == 1 and counts["handled_ambiguous"] == 1

    # ...and with the database: ONLY the persisted bucket produced a Message row
    rows = stored_inbound(env)
    assert len(rows) == counts["handled_persisted"] == 1
    assert sum(1 for r in rows if r.classification_success is True) == counts["classification_succeeded"]
    assert sum(1 for r in rows if r.classification_success is False) == counts["classification_failed"]
    # every message handled and marked Seen (approved behaviour: also unmatched/ambiguous/duplicate)
    assert counts["messages_marked_seen"] == 4 and counts["messages_left_unseen"] == 0
    assert not env.world.unseen_uids(HOST, USER)
    assert outcome == "completed"


def test_handler_that_raises_leaves_the_message_unseen_and_is_not_counted_as_handled(env, monkeypatch):
    real = mailbox_inbound._persist_inbound
    calls = {"n": 0}

    def flaky(factory, target, email_in):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db error")
        return real(factory, target, email_in)

    monkeypatch.setattr(mailbox_inbound, "_persist_inbound", flaky)
    inject(env, message_id="<a@p>", in_reply_to=OUT_A)
    inject(env, message_id="<b@p>", in_reply_to=OUT_A)
    report = mailbox_inbound.poll_all_mailboxes_report(env.sm)
    outcome, c, _ = summarize_mailbox_cycle(report)
    assert c["messages_left_unseen"] == 1 and c["messages_marked_seen"] == 1
    assert sum(c[k] for k in ("handled_persisted", "handled_duplicate", "handled_unmatched", "handled_ambiguous")) == 1
    assert outcome == "degraded"  # retry/left-unseen is never reported as a clean cycle
    assert len(env.world.unseen_uids(HOST, USER)) == 1  # Seen only after the handler returned: preserved


def test_a_redelivered_message_is_counted_again_as_a_duplicate_not_hidden(env):
    inject(env, message_id="<once@p>", in_reply_to=OUT_A)
    first = summarize_mailbox_cycle(mailbox_inbound.poll_all_mailboxes_report(env.sm))[1]
    assert first["handled_persisted"] == 1
    inject(env, message_id="<once@p>", in_reply_to=OUT_A)  # offered again (e.g. a lost Seen flag)
    second = summarize_mailbox_cycle(mailbox_inbound.poll_all_mailboxes_report(env.sm))[1]
    assert (second["handled_duplicate"], second["handled_persisted"]) == (1, 0)
    assert len(stored_inbound(env)) == 1


def test_tally_failure_can_never_turn_a_handled_message_into_a_retry(env, monkeypatch):
    monkeypatch.setattr(mailbox_inbound, "classify_inbound_result", lambda r: (_ for _ in ()).throw(ValueError("x")))
    inject(env, message_id="<a@p>", in_reply_to=OUT_A)
    report = mailbox_inbound.poll_all_mailboxes_report(env.sm)
    assert report.outcomes[0].processed == 1 and report.outcomes[0].left_unseen == 0
    assert len(stored_inbound(env)) == 1 and not env.world.unseen_uids(HOST, USER)


# ------------------------------------------------------------------ listing failure / credential failure evidence
def test_listing_failure_is_reported_not_mistaken_for_no_mailboxes(env, monkeypatch):
    monkeypatch.setattr(mailbox_inbound, "list_pollable_mailbox_ids",
                        lambda sf=None: (_ for _ in ()).throw(RuntimeError("db down")))
    report = mailbox_inbound.poll_all_mailboxes_report(env.sm)
    assert report.listing_failed is True and report.outcomes == []
    assert mailbox_inbound.poll_all_mailboxes(env.sm) == []  # the legacy return shape is unchanged
    assert summarize_mailbox_cycle(report)[0] == "failed"
    monkeypatch.setattr(sched, "poll_all_mailboxes_report", lambda: report)
    sched.poll_mailbox_inboxes_job()
    assert cycle_row(env).last_outcome == "failed"


def test_credential_unavailable_is_counted_and_degrades_the_cycle(env, monkeypatch):
    from mailer_agent.mailbox_secrets import MailboxEncryptionUnavailable

    def no_key(_blob):
        raise MailboxEncryptionUnavailable("no key")

    monkeypatch.setattr(mailbox_inbound, "decrypt_secret", no_key)
    outcome, c, err = summarize_mailbox_cycle(mailbox_inbound.poll_all_mailboxes_report(env.sm))
    assert (outcome, c["mailboxes_credential_unavailable"], err) == ("degraded", 1, "MailboxEncryptionUnavailable")


# ------------------------------------------------------------------ the endpoint
def test_endpoint_before_any_cycle_has_no_evidence_and_zero_rows(env):
    body = env.get(URL).json()
    assert body["mailbox_polling"] == {"last_completed_cycle_at": None, "last_completed_cycle_age_seconds": None, "instances": []}
    assert body["legacy_global_imap"]["instances"] == []
    assert body["persisted_inbound_messages"]["stored_message_rows"] == 0


def test_endpoint_distinguishes_persisted_rows_from_unpersisted_mail(env):
    add_ambiguity(env)
    inject(env, message_id="<p@p>", in_reply_to=OUT_A)                                    # persisted
    inject(env, from_addr="stranger@nowhere.example", message_id="<u1@p>", in_reply_to=None)  # unmatched
    inject(env, from_addr="stranger@nowhere.example", message_id="<u2@p>", in_reply_to=None)  # unmatched
    inject(env, from_addr="lead@example.com", message_id="<amb@p>", in_reply_to=None)     # ambiguous
    sched.poll_mailbox_inboxes_job()
    body = env.get(URL).json()

    persisted = body["persisted_inbound_messages"]
    assert persisted["stored_message_rows"] == 1 and persisted["stored_via_mailbox_polling"] == 1

    inst = body["mailbox_polling"]["instances"][0]
    c = inst["last_cycle_counts"]
    assert (c["handled_persisted"], c["handled_unmatched"], c["handled_ambiguous"]) == (1, 2, 1)
    handled = sum(c[k] for k in ("handled_persisted", "handled_duplicate", "handled_unmatched", "handled_ambiguous"))
    assert handled == 4 and handled > persisted["stored_message_rows"], \
        "rows are NOT 'emails received': unmatched/ambiguous mail is deliberately not stored"
    assert inst["last_outcome"] == "completed" and inst["instance_heartbeat_status"] in ("fresh", "stale", "unknown")
    assert body["mailbox_polling"]["last_completed_cycle_at"] is not None
    assert "NOT the number of emails received" in body["definitions"]["persisted_inbound_messages"]
    assert "deliberately NOT stored" in body["definitions"]["unmatched_and_ambiguous"]
    assert "NO evidence of fetch" in body["definitions"]["legacy_global_imap"]


def test_since_instance_start_totals_accumulate_across_cycles(env):
    inject(env, message_id="<a@p>", in_reply_to=OUT_A)
    sched.poll_mailbox_inboxes_job()
    inject(env, from_addr="stranger@nowhere.example", message_id="<b@p>", in_reply_to=None)
    sched.poll_mailbox_inboxes_job()
    inst = env.get(URL).json()["mailbox_polling"]["instances"][0]
    assert inst["last_cycle_counts"]["handled_unmatched"] == 1 and inst["last_cycle_counts"]["handled_persisted"] == 0
    assert inst["counts_since_instance_start"]["handled_persisted"] == 1
    assert inst["counts_since_instance_start"]["handled_unmatched"] == 1
    assert inst["cycles_recorded_since_instance_start"] == 2


def test_legacy_global_imap_is_reported_separately_from_mailbox_polling(env, monkeypatch):
    monkeypatch.setattr(sched, "fetch_unseen_replies", lambda: [SimpleNamespace(from_email=LEAK_FROM)])
    monkeypatch.setattr(sched, "process_inbound_email",
                        lambda db, e, **kw: {"matched": False, "from_email": LEAK_FROM})

    @contextlib.contextmanager
    def scope():
        yield object()

    monkeypatch.setattr(sched, "session_scope", scope)
    sched.poll_replies_job()
    body = env.get(URL).json()
    assert body["mailbox_polling"]["instances"] == []
    legacy = body["legacy_global_imap"]["instances"][0]
    assert legacy["last_cycle_counts"]["handled_unmatched"] == 1 and legacy["last_cycle_counts"]["emails_fetched"] == 1
    assert "handled_ambiguous" not in legacy["last_cycle_counts"]  # that path cannot tell; it is not claimed
    assert LEAK_FROM not in env.get(URL).text


def test_no_addresses_subjects_bodies_message_ids_or_references_are_stored_or_returned(env):
    inject(env, from_addr=LEAK_FROM, message_id="<LEAK-MSGID-777@leak.test>", in_reply_to="<LEAK-REF-888@leak.test>",
           subject="LEAK-SUBJECT-999", body="LEAK-BODY-1010 hello")
    inject(env, from_addr="lead@example.com", message_id="<LEAK-OK-1111@leak.test>", in_reply_to=OUT_A,
           subject="LEAK-SUBJECT-1212", body="LEAK-BODY-1313")
    sched.poll_mailbox_inboxes_job()
    text = env.get(URL).text
    with env.sm() as s:  # also nothing sensitive in the durable aggregate itself
        stored = repr([(r.last_counts, r.total_counts, r.last_error_type, r.instance_id)
                       for r in s.execute(select(WorkerCycleStatus)).scalars()])
    for blob in (text, stored):
        for leak in (LEAK_FROM, "leak-example.test", "LEAK-MSGID", "LEAK-REF", "LEAK-SUBJECT", "LEAK-BODY",
                     "LEAK-OK", "lead@example.com", "org-a", "imap.a.example", USER, PW, "outreach@"):
            assert leak not in blob, f"{leak!r} leaked"
    assert "@" not in text


# ------------------------------------------------------------------ the persisted-inbound window
def _add_inbound(env, *, created, success, mailbox=True, direction=MessageDirection.INBOUND.value):
    with env.sm() as s:
        s.add(Message(contact_id=env.ids.contact, direction=direction, body="b", subject="s",
                      status=MessageStatus.RECEIVED.value, classification_success=success,
                      mailbox_id=env.ids.mailbox if mailbox else None, created_at=created))
        s.commit()


def test_window_counts_only_inbound_rows_inside_the_window_by_classification(env):
    _add_inbound(env, created=ago(3600), success=True)
    _add_inbound(env, created=ago(7200), success=True, mailbox=False)       # webhook-style: no mailbox
    _add_inbound(env, created=ago(10), success=False)
    _add_inbound(env, created=ago(20), success=None)                         # classification not recorded
    _add_inbound(env, created=ago(25 * 3600), success=True)                  # outside the 24h window
    _add_inbound(env, created=ago(60), success=True, direction=MessageDirection.OUTBOUND.value)  # not inbound
    p = env.get(URL).json()["persisted_inbound_messages"]
    assert p["window_hours"] == 24 and p["window_start"] == (NOW - timedelta(hours=24)).isoformat()
    assert (p["stored_message_rows"], p["stored_via_mailbox_polling"]) == (4, 3)
    assert (p["classification_succeeded"], p["classification_failed"], p["classification_not_recorded"]) == (2, 1, 1)


def test_window_boundary_and_override(env):
    _add_inbound(env, created=ago(24 * 3600), success=True)       # exactly at the boundary: included (>=)
    _add_inbound(env, created=ago(24 * 3600 + 1), success=True)   # one second older: excluded
    assert env.get(URL).json()["persisted_inbound_messages"]["stored_message_rows"] == 1
    _add_inbound(env, created=ago(30 * 60), success=True)
    assert env.get(URL, params={"window_hours": 1}).json()["persisted_inbound_messages"]["stored_message_rows"] == 1
    assert env.get(URL, params={"window_hours": 48}).json()["persisted_inbound_messages"]["stored_message_rows"] == 3


@pytest.mark.parametrize("bad", ["0", "-1", "169", "abc", "1.5"])
def test_window_override_is_bounded(env, bad):
    assert env.get(URL, params={"window_hours": bad}).status_code == 422


def test_window_query_is_one_sql_side_aggregate_that_never_reads_content_or_identifiers(env):
    _add_inbound(env, created=ago(10), success=True)
    with capture_sql(env.sm.kw["bind"]) as statements:
        env.get(URL)
    msg_selects = [s.lower() for s in statements if "from messages" in s.lower()]
    assert len(msg_selects) == 1
    stmt = msg_selects[0]
    assert "count(" in stmt and "sum(" in stmt
    for forbidden in ("messages.body", "messages.subject", "from_email", "message_id_header", "in_reply_to",
                      "contact_id", "messages.id,", "references"):
        assert forbidden not in stmt, forbidden
