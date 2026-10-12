"""
C15.1b -- the inbound path must never cross a tenant boundary.

At the base SHA the webhook authenticated a key but then correlated the inbound e-mail
GLOBALLY: an org-A key could post a reply that referenced org B's Message-ID, have it
persisted into B's thread, classify it (an "unsubscribe" suppressed B's contact) and receive B's
``contact_id`` back; the duplicate check and the post-IntegrityError recovery path also re-queried
globally and returned the other tenant's identifiers.

Everything here runs through the REAL webhook route with the REAL auth dependencies (no
override of require_api_key / get_current_org_id / get_integration_org_id), on an isolated SQLite
file; the classifier is forced onto its deterministic path, nothing contacts an LLM, SMTP or IMAP.
The unique index ``uq_messages_message_id_no_mailbox`` is the real one created from the models.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import mailer_agent.mail.reply_handler_v2 as rh
from mailer_agent.api import deps
from mailer_agent.api.main import app
from mailer_agent.db import Base, get_db
from mailer_agent.followup import scheduler
from mailer_agent.mail.imap_reader import InboundEmail
from mailer_agent.mail.reply_handler import process_inbound_email
from mailer_agent.models import Campaign, Contact, Message, SuppressionEntry

KEY_A, KEY_B = "key-a-" + "a" * 24, "key-b-" + "b" * 24
ORG_A, ORG_B = "org-a", "org-b"
URL = "/webhooks/inbound-email"
AUTH_DEPS = {deps.require_api_key, deps.get_current_org_id, deps.get_integration_org_id, deps.get_authenticated_org_id}

OUT_A = rh._normalize_message_id("<a-out-1@a.example>")
OUT_B = rh._normalize_message_id("<b-out-1@b.example>")
IN_X = rh._normalize_message_id("<shared-inbound-x@prospect.example>")


@pytest.fixture(autouse=True)
def _deterministic_classifier_and_real_auth(monkeypatch):
    import mailer_agent.llm.provider_v2 as provider_module

    monkeypatch.setattr(provider_module, "is_llm_available", lambda: False)
    overridden = [d for d in app.dependency_overrides if d in AUTH_DEPS]
    assert not overridden, f"auth dependencies overridden: {overridden}"
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def sm(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'scope.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    sm_ = sessionmaker(bind=eng)

    def _get_db():
        s = sm_()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    yield sm_
    eng.dispose()


@pytest.fixture()
def keys(monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {KEY_A: ORG_A, KEY_B: ORG_B})


@pytest.fixture()
def world(sm):
    """Two tenants. Each has a campaign, one contact and one outbound message with a known
    Message-ID. The two contacts deliberately share the same e-mail address."""
    ids = {}
    with sm() as s:
        for tag, org in (("a", ORG_A), ("b", ORG_B)):
            camp = Campaign(name=f"camp-{tag}", organization_id=org, sender_name=tag, sender_org=f"{tag} inc",
                            sender_email=f"sales@{tag}.example", value_prop="vp")
            s.add(camp)
            s.flush()
            ct = Contact(campaign_id=camp.id, email="prospect@shared.example", status="active")
            s.add(ct)
            s.flush()
            s.add(Message(contact_id=ct.id, direction="outbound", subject="hello", body=f"outbound {tag}",
                          message_id_header=OUT_A if tag == "a" else OUT_B))
            ids[f"contact_{tag}"] = ct.id
        s.commit()
    return ids


def _hold_inbound(sm, contact_id, message_id=IN_X):
    """Pre-existing inbound row owned by a tenant (it holds the globally-unique Message-ID)."""
    with sm() as s:
        m = Message(contact_id=contact_id, direction="inbound", subject="earlier", body="earlier reply",
                    status="received", message_id_header=message_id)
        s.add(m)
        s.commit()
        return m.id


def _snapshot(sm, contact_id):
    """Everything about a tenant's contact that a foreign request must not be able to change."""
    with sm() as s:
        c = s.get(Contact, contact_id)
        msgs = s.query(Message).filter(Message.contact_id == contact_id).order_by(Message.id).all()
        return {
            "status": c.status,
            "updated_at": c.updated_at,
            "messages": [(m.id, m.direction, m.subject, m.body, m.status, m.message_id_header) for m in msgs],
            "suppression_rows": s.query(SuppressionEntry).count(),
        }


def _payload(**kw):
    base = {"from_email": "prospect@shared.example", "body_text": "Please unsubscribe me, remove me from your list."}
    base.update(kw)
    return base


def _post(key, **kw):
    headers = {"X-API-Key": key} if key else {}
    return TestClient(app).post(URL, json=_payload(**kw), headers=headers)


def _no_foreign_ids(body: dict):
    assert body == {"matched": False, "from_email": "prospect@shared.example"}, body


# ------------------------------------------------------------------ webhook authentication (real route)
def test_webhook_route_uses_the_fail_closed_integration_dependency():
    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == URL)

    def calls(d):
        for x in d.dependencies:
            yield x.call
            yield from calls(x)

    assert deps.get_integration_org_id in set(calls(route.dependant))


def test_webhook_empty_key_map_is_503_never_open(sm, world, monkeypatch):
    monkeypatch.setattr(deps, "_KEY_MAP", {})
    before = _snapshot(sm, world["contact_b"])
    for key in (None, "anything"):
        assert _post(key, in_reply_to=OUT_B).status_code == 503
    assert _snapshot(sm, world["contact_b"]) == before


def test_webhook_missing_and_invalid_key_are_401_and_change_nothing(sm, world, keys):
    before = (_snapshot(sm, world["contact_a"]), _snapshot(sm, world["contact_b"]))
    assert _post(None, in_reply_to=OUT_A).status_code == 401
    assert _post("not-a-key", in_reply_to=OUT_A).status_code == 401
    assert _post("", in_reply_to=OUT_A).status_code == 401
    assert (_snapshot(sm, world["contact_a"]), _snapshot(sm, world["contact_b"])) == before


def test_webhook_valid_tenant_keys_each_reach_only_their_own_thread(sm, world, keys):
    ra = _post(KEY_A, in_reply_to=OUT_A, message_id="<reply-a@prospect.example>")
    assert ra.status_code == 200 and ra.json()["matched"] is True and ra.json()["contact_id"] == world["contact_a"]
    rb = _post(KEY_B, in_reply_to=OUT_B, message_id="<reply-b@prospect.example>")
    assert rb.status_code == 200 and rb.json()["matched"] is True and rb.json()["contact_id"] == world["contact_b"]


# ------------------------------------------------------------------ cross-tenant threading
@pytest.mark.parametrize("how", ["in_reply_to", "references"])
def test_cross_tenant_threading_reference_is_unmatched_and_mutates_nothing(sm, world, keys, how):
    before_b, before_a = _snapshot(sm, world["contact_b"]), _snapshot(sm, world["contact_a"])
    kw = {"in_reply_to": OUT_B} if how == "in_reply_to" else {"references": [OUT_B]}
    r = _post(KEY_A, from_email="someone-else@elsewhere.example", **kw)
    assert r.status_code == 200
    assert r.json() == {"matched": False, "from_email": "someone-else@elsewhere.example"}
    assert str(world["contact_b"]) not in r.text or "contact_id" not in r.json()
    assert _snapshot(sm, world["contact_b"]) == before_b  # not persisted into B, B not suppressed
    assert _snapshot(sm, world["contact_a"]) == before_a


def test_cross_tenant_to_email_cannot_reach_the_other_tenants_inbox(sm, world, keys):
    before_b = _snapshot(sm, world["contact_b"])
    r = _post(KEY_A, to_email="sales@b.example")  # B's sender inbox, A's key
    _no_foreign_ids(r.json())
    assert _snapshot(sm, world["contact_b"]) == before_b


def test_email_fallback_resolves_inside_the_authenticated_tenant_only(sm, world, keys):
    """Intended behaviour change: with the tenant known from the key, an address that exists in
    two tenants is no longer 'ambiguous' for the webhook -- it resolves to the caller's own
    contact and the other tenant's contact is untouched."""
    before_b = _snapshot(sm, world["contact_b"])
    r = _post(KEY_A, body_text="Thanks, sounds interesting, tell me more.")
    assert r.json()["matched"] is True and r.json()["contact_id"] == world["contact_a"]
    assert _snapshot(sm, world["contact_b"]) == before_b


# ------------------------------------------------------------------ duplicate Message-IDs
def test_cross_tenant_duplicate_message_id_reveals_no_foreign_identifiers(sm, world, keys):
    held = _hold_inbound(sm, world["contact_b"])
    before_b = _snapshot(sm, world["contact_b"])
    r = _post(KEY_A, message_id=IN_X, from_email="someone-else@elsewhere.example")
    assert r.status_code == 200
    assert r.json() == {"matched": False, "from_email": "someone-else@elsewhere.example"}
    assert "skipped_duplicate" not in r.text and str(held) not in r.json().values()
    assert _snapshot(sm, world["contact_b"]) == before_b


def test_same_tenant_duplicate_message_id_is_still_skipped_as_duplicate(sm, world, keys):
    held = _hold_inbound(sm, world["contact_a"])
    r = _post(KEY_A, message_id=IN_X, in_reply_to=OUT_A)
    assert r.json() == {"matched": True, "contact_id": world["contact_a"], "action": "skipped_duplicate",
                        "message_id": held}


# ------------------------------------------------------------------ post-IntegrityError recovery path
def test_post_integrity_error_recovery_cross_tenant_returns_no_foreign_identifiers(sm, world, keys, caplog):
    """THE disclosure path. B holds the (globally unique) Message-ID. A's request is correctly
    correlated to A's OWN contact, so Step 1 (scoped) misses, the insert violates the global
    unique index, and the recovery re-query used to return B's contact_id / message id."""
    held = _hold_inbound(sm, world["contact_b"])
    before_a, before_b = _snapshot(sm, world["contact_a"]), _snapshot(sm, world["contact_b"])
    with caplog.at_level("INFO", logger="mailer_agent.mail.reply_handler_v2"):
        r = _post(KEY_A, message_id=IN_X, in_reply_to=OUT_A)
    assert "Concurrent duplicate detected" in caplog.text, "the IntegrityError recovery path was not exercised"
    assert "outside this organization" in caplog.text
    assert r.status_code == 200
    assert r.json() == {"matched": False, "from_email": "prospect@shared.example"}
    assert held not in r.json().values() and world["contact_b"] not in r.json().values()
    assert _snapshot(sm, world["contact_b"]) == before_b  # foreign record untouched
    assert _snapshot(sm, world["contact_a"]) == before_a  # A's insert was rolled back, nothing leaked in
    with sm() as s:
        assert s.query(Message).filter(Message.message_id_header == IN_X).count() == 1  # still B's single row


def test_global_message_id_uniqueness_constraint_is_preserved(sm, world):
    _hold_inbound(sm, world["contact_b"])
    with sm() as s:
        idx = {i["name"]: i for i in sa.inspect(s.get_bind()).get_indexes("messages")}
    assert idx["uq_messages_message_id_no_mailbox"]["unique"]
    with pytest.raises(sa.exc.IntegrityError), sm() as s:
        s.add(Message(contact_id=world["contact_a"], direction="inbound", body="x", message_id_header=IN_X))
        s.commit()


def _simulate_lost_race(monkeypatch):
    """Make ONLY the first (Step 1) duplicate check miss, as if another transaction had not yet
    committed -- so the insert, not the pre-check, hits the unique index."""
    real, n = rh._scope_messages_to_org, {"calls": 0}

    def racing(query, organization_id):
        n["calls"] += 1
        return query.filter(sa.false()) if n["calls"] == 1 else real(query, organization_id)

    monkeypatch.setattr(rh, "_scope_messages_to_org", racing)


def test_post_integrity_error_recovery_same_tenant_still_reports_its_own_duplicate(sm, world, keys, monkeypatch, caplog):
    held = _hold_inbound(sm, world["contact_a"])
    _simulate_lost_race(monkeypatch)
    with caplog.at_level("INFO", logger="mailer_agent.mail.reply_handler_v2"):
        r = _post(KEY_A, message_id=IN_X, in_reply_to=OUT_A)
    assert "Concurrent duplicate detected" in caplog.text
    assert r.json() == {"matched": True, "contact_id": world["contact_a"], "action": "skipped_duplicate",
                        "message_id": held}


# ------------------------------------------------------------------ legacy global IMAP poller: unchanged
def _email(**kw):
    base = {"from_email": "prospect@shared.example", "subject": "Re: hello",
            "body_text": "Thanks, sounds interesting, tell me more.", "message_id": None, "in_reply_to": None,
            "references": [], "to_email": None}
    base.update(kw)
    return InboundEmail(**base)


def test_unscoped_none_threading_is_unchanged_global_correlation(sm, world):
    with sm() as s:
        r = process_inbound_email(s, _email(in_reply_to=OUT_B, message_id="<x1@p.example>"), organization_id=None)
        s.commit()
    assert r["matched"] is True and r["contact_id"] == world["contact_b"]


def test_unscoped_none_duplicate_check_is_unchanged_global(sm, world):
    held = _hold_inbound(sm, world["contact_b"])
    with sm() as s:
        r = process_inbound_email(s, _email(message_id=IN_X), organization_id=None)
    assert r == {"matched": True, "contact_id": world["contact_b"], "action": "skipped_duplicate", "message_id": held}


def test_unscoped_none_recovery_path_is_unchanged_global(sm, world, monkeypatch):
    held = _hold_inbound(sm, world["contact_b"])
    _simulate_lost_race(monkeypatch)
    with sm() as s:
        r = process_inbound_email(s, _email(in_reply_to=OUT_A, message_id=IN_X), organization_id=None)
    assert r == {"matched": True, "contact_id": world["contact_b"], "action": "skipped_duplicate", "message_id": held}


def test_legacy_poller_passes_organization_id_none_explicitly(monkeypatch):
    seen = []
    monkeypatch.setattr(scheduler, "fetch_unseen_replies", lambda: [_email()])
    monkeypatch.setattr(scheduler, "process_inbound_email",
                        lambda db, e, **kw: seen.append(kw) or {"action": "x"})

    @contextlib.contextmanager
    def fake_scope():
        yield object()

    monkeypatch.setattr(scheduler, "session_scope", fake_scope)
    scheduler.poll_replies_job()
    assert seen == [{"organization_id": None}]


# ------------------------------------------------------------------ explicitness (no unscoped-by-omission)
def test_organization_id_is_a_required_keyword_for_wrapper_and_implementation(sm):
    with sm() as s:
        with pytest.raises(TypeError):
            process_inbound_email(s, _email())
        with pytest.raises(TypeError):
            rh.process_inbound_email_v2(s, _email())
        with pytest.raises(TypeError):
            process_inbound_email(s, _email(), None)  # not positional either


def test_every_production_call_site_passes_organization_id_explicitly():
    root = pathlib.Path(rh.__file__).resolve().parents[1]
    names = {"process_inbound_email", "process_inbound_email_v2", "_find_contact_by_message_id", "_find_contact_by_email"}
    sites = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                if name in names:
                    explicit = any(k.arg == "organization_id" for k in node.keywords) or (
                        name.startswith("_find") and len(node.args) == 3)
                    sites.append((path.relative_to(root).as_posix(), name, explicit))
    assert {s[0] for s in sites} >= {"api/webhooks.py", "followup/scheduler.py", "mail/reply_handler.py",
                                     "mail/reply_handler_v2.py"}
    assert all(s[2] for s in sites), [s for s in sites if not s[2]]


@pytest.mark.parametrize("how", ["in_reply_to", "references"])
def test_foreign_thread_reference_with_shared_sender_fails_closed(
    sm, world, keys, how
):
    """A foreign thread reference must not fall back to a shared sender address.

    The default payload has the same sender address in both tenants and contains
    unsubscribe language. The authenticated tenant must not infer a local thread
    merely from that shared sender when the explicit thread reference is unknown
    within the authenticated tenant.
    """
    before_a = _snapshot(sm, world["contact_a"])
    before_b = _snapshot(sm, world["contact_b"])

    kwargs = (
        {"in_reply_to": OUT_B}
        if how == "in_reply_to"
        else {"references": [OUT_B]}
    )

    r = _post(KEY_A, **kwargs)

    assert r.status_code == 200
    _no_foreign_ids(r.json())
    assert _snapshot(sm, world["contact_a"]) == before_a
    assert _snapshot(sm, world["contact_b"]) == before_b
