"""PR4 -- /system/metrics/dispatch: durable ExternalDispatch aggregates for the operator.

Deterministic (SQLite). Time is injected. The stale-`sending` count is cross-checked against the REAL
lease-recovery sweep so the two can never disagree about what "expired" means.
"""

from __future__ import annotations

import itertools
import json

import pytest

from mailer_agent.followup.work_claiming import recover_expired_external_dispatches
from mailer_agent.models import ExternalDispatch
from mailer_agent.models import ExternalDispatchState as S
from mailer_agent.operator_metrics import dispatch_metrics
from tests.dispatch_support import seed_dispatch
from tests.pr4_support import NOW, ago, capture_sql

LEASE = 900
URL = "/system/metrics/dispatch"
_seq = itertools.count(1)  # seed_dispatch defaults to ONE email; UNIQUE(campaign_id, email) needs a fresh one


def _seed(sm, state, *, created=None, claimed_at=None, message=True, org="org-a", **kw):
    """One dispatch in ``state``; ``message=False`` makes it 'awaiting generation' (message_id NULL)."""
    with sm() as s:
        d = seed_dispatch(s, state=state.value, org=org, mailbox=False, claimed_at=claimed_at,
                          claimed_by="w" if claimed_at is not None else None,
                          email=f"lead{next(_seq)}@example.com", **kw)
        if not message:
            d.message_id = None
        if created is not None:
            d.created_at = created
        s.commit()
        return d.id


def _metrics(sm):
    with sm() as s:
        return dispatch_metrics(s, now=NOW, lease_seconds=LEASE)


# ------------------------------------------------------------------ zero work / every state
def test_zero_work_is_all_zero_with_null_ages(operator_env):
    m = _metrics(operator_env.sm)
    assert m["states"] == {"queued": 0, "generating": 0, "sending": 0, "sent": 0, "failed": 0, "unknown": 0}
    assert m["total"] == 0 and m["unrecognized_state_rows"] == 0
    assert m["queue"] == {
        "depth": 0, "awaiting_generation": 0, "ready_to_send": 0, "oldest_queued_age_seconds": None,
        "oldest_awaiting_generation_age_seconds": None, "oldest_ready_to_send_age_seconds": None,
    }
    assert m["sending"] == {"in_flight_or_unexpired": 0, "stale_lease_expired": 0, "lease_seconds": LEASE}
    assert m["outcomes"] == {"smtp_accepted": 0, "failed_not_sent": 0, "unknown_needs_operator_reconciliation": 0}


def test_every_lifecycle_state_is_counted_and_unknown_is_called_out(operator_env):
    sm = operator_env.sm
    plan = {S.QUEUED: 2, S.GENERATING: 1, S.SENDING: 3, S.SENT: 4, S.FAILED: 5, S.UNKNOWN: 2}
    for state, n in plan.items():
        for _ in range(n):
            _seed(sm, state, claimed_at=ago(10, naive=True) if state in (S.SENDING, S.GENERATING) else None)
    m = _metrics(sm)
    assert m["states"] == {s.value: n for s, n in plan.items()}
    assert m["total"] == sum(plan.values())
    assert m["outcomes"]["unknown_needs_operator_reconciliation"] == 2
    assert m["outcomes"]["smtp_accepted"] == 4  # `sent` is SMTP acceptance
    assert "NOT confirmation" in m["definitions"]["sent"] and "reconciliation" in m["definitions"]["unknown"]


def test_a_row_with_an_unexpected_state_is_flagged_not_silently_dropped(operator_env):
    sm = operator_env.sm
    _seed(sm, S.SENT)
    with sm() as s:
        d = seed_dispatch(s, state="mystery", mailbox=False, email=f"lead{next(_seq)}@example.com")
        assert d.state == "mystery"
    m = _metrics(sm)
    assert m["unrecognized_state_rows"] == 1 and m["total"] == 2 and m["states"]["sent"] == 1


# ------------------------------------------------------------------ queue split and oldest age
def test_queue_splits_awaiting_generation_from_ready_to_send(operator_env):
    sm = operator_env.sm
    _seed(sm, S.QUEUED, message=False)
    _seed(sm, S.QUEUED, message=False)
    _seed(sm, S.QUEUED, message=True)
    _seed(sm, S.GENERATING, claimed_at=ago(5, naive=True), message=False)  # in-flight: NOT queue depth
    q = _metrics(sm)["queue"]
    assert (q["depth"], q["awaiting_generation"], q["ready_to_send"]) == (3, 2, 1)


def test_oldest_queued_age_uses_created_at_not_updated_at_or_claim_time(operator_env):
    sm = operator_env.sm
    old_ready = _seed(sm, S.QUEUED, message=True, created=ago(500))
    _seed(sm, S.QUEUED, message=False, created=ago(120))
    _seed(sm, S.QUEUED, message=True, created=ago(40))
    # A recent updated_at / stale claim timestamp on the OLD row must not change its age.
    with sm() as s:
        row = s.get(ExternalDispatch, old_ready)
        row.updated_at = ago(1)
        row.claimed_at = ago(2, naive=True)
        s.commit()
    q = _metrics(sm)["queue"]
    assert q["oldest_queued_age_seconds"] == 500
    assert q["oldest_ready_to_send_age_seconds"] == 500
    assert q["oldest_awaiting_generation_age_seconds"] == 120


def test_terminal_and_inflight_rows_never_count_toward_oldest_queued(operator_env):
    sm = operator_env.sm
    _seed(sm, S.SENT, created=ago(99999))
    _seed(sm, S.SENDING, claimed_at=ago(5, naive=True), created=ago(88888))
    _seed(sm, S.QUEUED, created=ago(30))
    assert _metrics(sm)["queue"]["oldest_queued_age_seconds"] == 30


# ------------------------------------------------------------------ stale sending == the recovery sweep's predicate
def test_stale_sending_uses_claimed_at_and_matches_the_real_recovery_sweep(operator_env):
    sm = operator_env.sm
    fresh = _seed(sm, S.SENDING, claimed_at=ago(LEASE - 60, naive=True))       # unexpired lease
    stale = _seed(sm, S.SENDING, claimed_at=ago(LEASE + 60, naive=True))       # expired lease
    boundary = _seed(sm, S.SENDING, claimed_at=ago(LEASE, naive=True))         # == cutoff: NOT expired (strict <)
    anomalous = _seed(sm, S.SENDING, claimed_at=None)                          # NULL claim can never be live
    _seed(sm, S.SENDING, claimed_at=ago(LEASE + 9999, naive=True), created=ago(5))  # created_at is irrelevant

    m = _metrics(sm)["sending"]
    assert m["stale_lease_expired"] == 3 and m["in_flight_or_unexpired"] == 2 and m["lease_seconds"] == LEASE

    with sm() as s:  # the SAME instant and lease given to the real sweep
        recovered = {r.dispatch_id for r in recover_expired_external_dispatches(s, now=NOW, lease_seconds=LEASE)}
        s.commit()
    assert len(recovered) == m["stale_lease_expired"]
    assert fresh not in recovered and boundary not in recovered and stale in recovered and anomalous in recovered


def test_metrics_after_the_sweep_move_stale_sending_into_unknown(operator_env):
    sm = operator_env.sm
    _seed(sm, S.SENDING, claimed_at=ago(LEASE + 1, naive=True))
    assert _metrics(sm)["sending"]["stale_lease_expired"] == 1
    with sm() as s:
        recover_expired_external_dispatches(s, now=NOW, lease_seconds=LEASE)
        s.commit()
    m = _metrics(sm)
    assert m["sending"]["stale_lease_expired"] == 0
    assert m["states"]["unknown"] == 1 and m["outcomes"]["unknown_needs_operator_reconciliation"] == 1


# ------------------------------------------------------------------ SQL shape: aggregated in the database
def test_aggregation_happens_in_sql_and_never_reads_row_level_or_free_text_columns(operator_env):
    sm = operator_env.sm
    for _ in range(5):
        _seed(sm, S.QUEUED)
    with capture_sql(operator_env.engine) as statements:
        _metrics(sm)
    selects = [s.lower() for s in statements if s.lstrip().lower().startswith("select")]
    assert 1 <= len(selects) <= 2, selects
    for stmt in selects:
        assert "count(" in stmt or "sum(" in stmt or "min(" in stmt
        for forbidden in ("error_message", "idempotency_key", "public_reference", "claimed_by",
                          "external_dispatches.id", "organization_id", "contact_id", "request_fingerprint",
                          "grounding_context", "correlation_id", "external_action_id"):
            assert forbidden not in stmt, f"{forbidden} must not be selected: {stmt}"


# ------------------------------------------------------------------ the endpoint
def test_endpoint_returns_the_aggregate_and_no_identifiers_raw_errors_or_content(operator_env):
    sm = operator_env.sm
    with sm() as s:
        d = seed_dispatch(
            s, state=S.UNKNOWN.value, org="ORG-LEAK-TENANT-77", idem="IDEM-LEAK-KEY-88",
            email="leak.person@leak-example.test", subject="SUBJECT-LEAK-99",
            body="BODY-LEAK-100 hello", mailbox=False,
        )
        d.error_message = "RAW-ERROR-LEAK smtp password=hunter2 worker=HOSTNAME-LEAK-1234"
        d.claimed_by = "worker-HOSTNAME-LEAK-1234-abcd"
        d.correlation_id = "CORR-LEAK-55"
        d.external_action_id = "EXTACT-LEAK-66"
        ref = d.public_reference
        s.commit()
    r = operator_env.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["states"]["unknown"] == 1 and body["outcomes"]["unknown_needs_operator_reconciliation"] == 1
    text = r.text
    for leak in ("ORG-LEAK-TENANT-77", "IDEM-LEAK-KEY-88", "leak.person", "leak-example.test", "SUBJECT-LEAK-99",
                 "BODY-LEAK-100", "RAW-ERROR-LEAK", "hunter2", "HOSTNAME-LEAK-1234", "CORR-LEAK-55",
                 "EXTACT-LEAK-66", ref, "@"):
        assert leak not in text, f"{leak!r} leaked into /system/metrics/dispatch"


def test_endpoint_is_read_only_and_leaves_every_row_untouched(operator_env):
    sm = operator_env.sm
    _seed(sm, S.SENDING, claimed_at=ago(LEASE + 500, naive=True))
    _seed(sm, S.UNKNOWN)
    _seed(sm, S.FAILED)
    def snap():
        with sm() as s:
            return json.dumps(
                [(d.id, d.state, str(d.claimed_at), str(d.updated_at), d.error_message)
                 for d in s.query(ExternalDispatch).order_by(ExternalDispatch.id)]
            )
    before = snap()
    for _ in range(3):
        assert operator_env.get(URL).status_code == 200
    assert snap() == before  # stale `sending` is REPORTED, never resolved, retried or resent by a GET


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_endpoint_accepts_only_get(operator_env, method):
    r = getattr(operator_env.client, method)(URL, headers=operator_env.headers)
    assert r.status_code == 405
