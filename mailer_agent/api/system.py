"""
API routes for system monitoring and integration validation.

Multi-tenancy: every route here that returns tenant data is scoped to the
calling organization via get_current_org_id(), exactly like
campaigns/contacts/messages. Aggregate counts (contact-state distribution,
message counts, classification rates) are computed with SQL-side
GROUP BY / COUNT so they scale with an index scan instead of loading every
row into Python -- and, critically, so one organization's dashboard can
never reveal another organization's volume.

Access model (PR3). Two routers, each route explicitly in exactly one:

* TENANT routes (``tenant_router``): /system/health, /system/metrics/classification,
  /system/metrics/states. Authenticated with a tenant key (X-API-Key) and scoped to that
  key's organization. They return no process-wide data.
* OPERATOR routes (``operator_router``): /system/integration/validate, /system/metrics/llm,
  /system/config, and (PR4) /system/metrics/dispatch, /system/metrics/worker,
  /system/metrics/inbound. They report process-wide booleans/counters/configuration (is this
  deployment wired up, provider retry stats, API-process settings) -- not any tenant's data --
  so they are intentionally unscoped, and therefore are NOT reachable with a tenant key. They
  require the separate operator credential (X-System-Operator-Key, SYSTEM_OPERATOR_API_KEY; see
  deps.require_system_operator) and no tenant key at all. The tenant router's tenant-key dependency
  is router-level, which is why the operator routes live on their own router instead of sharing it.

``router`` (what main.py includes) is the two combined; a new /system route must be added to one of
the two on purpose (tests/test_pr3_system_operator_boundary.py pins the inventory).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from mailer_agent.api.deps import get_current_org_id, require_api_key, require_system_operator
from mailer_agent.config import get_settings
from mailer_agent.db import get_db
from mailer_agent.integration import mailer_agent
from mailer_agent.models import Campaign, Contact, ContactStatus, Message, MessageDirection, MessageStatus
from mailer_agent.operator_metrics import dispatch_metrics, inbound_metrics, worker_metrics
from mailer_agent.utils.datetime_utils import utcnow

tenant_router = APIRouter(prefix="/system", tags=["system"], dependencies=[Depends(require_api_key)])
operator_router = APIRouter(
    prefix="/system", tags=["system-operator"], dependencies=[Depends(require_system_operator)]
)


@tenant_router.get("/health")
def get_system_health(
    org_id: str = Depends(get_current_org_id),
    db: Session = Depends(get_db),
):
    """
    Health metrics for the CALLING ORGANIZATION ONLY.

    Returns contact-state distribution, message send stats and classification
    success rate. All counts are computed with a single grouped SQL query each,
    scoped to org_id. Process-wide data (LLM provider metrics, API-process
    settings) is deliberately NOT part of this response: it is available to the
    operator only, via /system/metrics/llm, /system/config and the PR4 /system/metrics/{dispatch,worker,inbound}.
    """
    # --- Contact state distribution: one GROUP BY, not one query per enum ---
    state_rows = (
        db.query(Contact.status, func.count(Contact.id))
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(Campaign.organization_id == org_id)
        .group_by(Contact.status)
        .all()
    )
    contact_states = {status.value: 0 for status in ContactStatus}
    for status_value, count in state_rows:
        contact_states[status_value] = count

    # --- Message stats: single aggregate query, not .all() + Python loop ---
    msg_row = (
        db.query(
            func.count(Message.id),
            func.sum(case((Message.status == MessageStatus.SENT.value, 1), else_=0)),
            func.sum(case((Message.status == MessageStatus.FAILED.value, 1), else_=0)),
            func.sum(case((Message.status == MessageStatus.UNKNOWN.value, 1), else_=0)),
        )
        .join(Contact, Message.contact_id == Contact.id)
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(Campaign.organization_id == org_id)
        .one()
    )
    total_messages, sent_messages, failed_messages, unknown_messages = (
        (v or 0) for v in msg_row
    )

    # --- Classification success rate: aggregate over inbound only ---
    class_row = (
        db.query(
            func.count(Message.id),
            func.sum(case((Message.classification_success.is_(True), 1), else_=0)),
        )
        .join(Contact, Message.contact_id == Contact.id)
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(
            Campaign.organization_id == org_id,
            Message.direction == MessageDirection.INBOUND.value,
        )
        .one()
    )
    classification_total, classification_success = ((v or 0) for v in class_row)

    return {
        "organization_id": org_id,
        "contact_states": contact_states,
        "messages": {
            "total": total_messages,
            "sent": sent_messages,
            "failed": failed_messages,
            "unknown": unknown_messages,
            "success_rate": (sent_messages / total_messages) if total_messages else 0,
        },
        "classification": {
            "success": classification_success,
            "total": classification_total,
            "success_rate": (
                classification_success / classification_total if classification_total else 0
            ),
        },
    }


@operator_router.get("/integration/validate")
def validate_integration():
    """
    OPERATOR ONLY. Validate that all components are properly integrated.

    Checks: LLM availability, configuration, module imports, database
    connectivity. Deployment-wide booleans (and, on failure, the raw error
    text for the operator) -- no tenant data -- so this intentionally is not
    org-scoped, and is not reachable with a tenant key.
    """
    return mailer_agent.validate_integration()


@operator_router.get("/metrics/llm")
def get_llm_metrics():
    """
    OPERATOR ONLY. Process-wide LLM provider metrics (total calls, success rate, rate
    limits, timeouts, malformed outputs). Not org-scoped: the underlying
    provider client and its retry/metrics counters are shared by the
    whole process, not partitioned per tenant.
    """
    return mailer_agent.get_llm_metrics()


@tenant_router.get("/metrics/classification")
def get_classification_metrics(
    org_id: str = Depends(get_current_org_id),
    db: Session = Depends(get_db),
):
    """Semantic classification metrics for the calling organization."""
    total_row = (
        db.query(
            func.count(Message.id),
            func.sum(case((Message.classification_success.is_(True), 1), else_=0)),
        )
        .join(Contact, Message.contact_id == Contact.id)
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(
            Campaign.organization_id == org_id,
            Message.direction == MessageDirection.INBOUND.value,
        )
        .one()
    )
    total, success = ((v or 0) for v in total_row)

    if total == 0:
        return {"total": 0, "success": 0, "success_rate": 0, "failure_reasons": {}}

    failure_rows = (
        db.query(Message.classification_failure_reason, func.count(Message.id))
        .join(Contact, Message.contact_id == Contact.id)
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(
            Campaign.organization_id == org_id,
            Message.direction == MessageDirection.INBOUND.value,
            Message.classification_success.is_(False),
            Message.classification_failure_reason.isnot(None),
        )
        .group_by(Message.classification_failure_reason)
        .all()
    )
    failure_reasons = {reason: count for reason, count in failure_rows}

    return {
        "total": total,
        "success": success,
        "success_rate": success / total,
        "failed": total - success,
        "failure_reasons": failure_reasons,
    }


@tenant_router.get("/metrics/states")
def get_state_distribution(
    org_id: str = Depends(get_current_org_id),
    db: Session = Depends(get_db),
):
    """Contact-state distribution for the calling organization (single GROUP BY query)."""
    rows = (
        db.query(Contact.status, func.count(Contact.id))
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(Campaign.organization_id == org_id)
        .group_by(Contact.status)
        .all()
    )
    distribution = {status.value: 0 for status in ContactStatus}
    for status_value, count in rows:
        distribution[status_value] = count

    total = sum(distribution.values())

    return {
        "distribution": distribution,
        "total": total,
        "percentages": {
            state: (count / total * 100 if total > 0 else 0)
            for state, count in distribution.items()
        },
    }


@operator_router.get("/config")
def get_system_config():
    """
    OPERATOR ONLY. Safe (non-secret) subset of the API process's configuration.
    Not org-scoped -- these are deployment-wide settings, not per-tenant data --
    and they describe THIS (API) process, not the separate worker.
    """
    settings = get_settings()

    return {
        "live_sending_enabled": settings.live_sending_enabled,
        "auto_reply_enabled": settings.auto_reply_enabled,
        "llm_model": settings.llm_model,
        "llm_temperature": settings.llm_temperature,
        "llm_max_tokens": settings.llm_max_tokens,
        "max_sends_per_cycle": settings.max_sends_per_cycle,
        "send_delay_seconds": settings.send_delay_seconds,
        "imap_poll_seconds": settings.imap_poll_seconds,
        "followup_poll_seconds": settings.followup_poll_seconds,
        "run_scheduler_in_process": settings.run_scheduler_in_process,
        "database_type": "postgresql" if "postgresql" in settings.database_url else "sqlite",
    }


# --------------------------------------------------------------------------- PR4: operator observability
# Read-only, process-wide, operator-credential only (router-level require_system_operator). They sit on
# operator_router and NOT on tenant_router, and they are deliberately not folded into /system/health,
# /system/metrics/states or /system/metrics/classification, which are tenant-scoped. See
# mailer_agent/operator_metrics.py for exactly what each number means and what it does not.


@operator_router.get("/metrics/dispatch")
def get_dispatch_metrics(db: Session = Depends(get_db)):
    """Durable ExternalDispatch counts by state, queue depth/age, stale `sending`, `unknown` outcomes.

    Process-wide across all organizations. `sent` means SMTP ACCEPTED, not recipient delivery.
    """
    return dispatch_metrics(db, now=utcnow(), lease_seconds=get_settings().external_dispatch_lease_seconds)


@operator_router.get("/metrics/worker")
def get_worker_metrics(db: Session = Depends(get_db)):
    """Heartbeat freshness (fresh / stale / stopped) per scheduler process, plus each process's latest
    cycle outcomes. A fresh heartbeat is process liveness only -- not SMTP delivery, not job health."""
    cfg = get_settings()
    return worker_metrics(
        db,
        now=utcnow(),
        stale_after_seconds=cfg.worker_stale_after_seconds,
        heartbeat_interval_seconds=cfg.worker_heartbeat_interval_seconds,
        retention_hours=cfg.worker_runtime_retention_hours,
    )


@operator_router.get("/metrics/inbound")
def get_inbound_metrics(
    window_hours: int | None = Query(default=None, ge=1, le=168, description="Look-back for persisted inbound rows."),
    db: Session = Depends(get_db),
):
    """Mailbox-poll cycle evidence, the legacy global-IMAP path (reported separately), and a windowed
    count of PERSISTED inbound messages. Unmatched/ambiguous mail is not stored as a Message, so the
    persisted count is not 'emails received'."""
    cfg = get_settings()
    return inbound_metrics(
        db,
        now=utcnow(),
        window_hours=window_hours if window_hours is not None else cfg.operator_inbound_window_hours,
        stale_after_seconds=cfg.worker_stale_after_seconds,
    )


# What main.py includes. Built LAST on purpose: include_router copies the routes registered on a router
# at the moment it is called, so this must come after every route above is defined.
router = APIRouter()
router.include_router(tenant_router)
router.include_router(operator_router)
