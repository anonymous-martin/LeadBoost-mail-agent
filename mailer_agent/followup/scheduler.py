"""
Unified background job scheduling with integrated components.

This module now uses the integrated engine with semantic intelligence
and conversation-aware scheduling as the single source of truth.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from mailer_agent.config import get_settings
from mailer_agent.db import session_scope
from mailer_agent.followup.engine import run_followup_cycle
from mailer_agent.mail.imap_reader import fetch_unseen_replies
from mailer_agent.mail.mailbox_inbound import poll_all_mailboxes
from mailer_agent.mail.reply_handler import process_inbound_email
from mailer_agent.models import Contact

logger = logging.getLogger("mailer_agent.scheduler")
settings = get_settings()

_scheduler: BackgroundScheduler | None = None


def poll_replies_job() -> None:
    """
    Poll for replies and process with full semantic intelligence.
    """
    emails = fetch_unseen_replies()
    if not emails:
        return
    
    logger.info(f"Fetched {len(emails)} unseen email(s)")
    
    with session_scope() as db:
        for email_in in emails:
            try:
                # Legacy deployment-global IMAP poll: trusted internal input, single-tenant by
                # design -> explicitly unscoped (organization_id=None). Never a fallback for the
                # per-mailbox path (poll_mailbox_inboxes_job).
                result = process_inbound_email(db, email_in, organization_id=None)
                action = result.get("action", "unknown")
                logger.info(f"Processed inbound from {email_in.from_email}: {action}")
            except Exception as e:
                logger.exception(f"Failed to process inbound email from {email_in.from_email}: {e}")


def poll_mailbox_inboxes_job() -> None:
    """
    M3: poll every eligible Mailer Mailbox with its own IMAP identity (see
    mail/mailbox_inbound.py). Independent of poll_replies_job, which remains the
    deployment-global legacy path and never acts as a fallback for this one.
    poll_all_mailboxes never raises; per-mailbox failures are in its outcomes.
    """
    outcomes = poll_all_mailboxes()
    if not outcomes:
        return
    handled = sum(o.processed for o in outcomes)
    failed = [o for o in outcomes if o.status not in ("ok", "skipped_ineligible")]
    logger.info(
        "Mailbox inbound cycle: %d mailbox(es), %d message(s) handled, %d mailbox failure(s)",
        len(outcomes), handled, len(failed),
    )


def dispatch_new_contacts_job() -> None:
    """
    Dispatch initial outreach to NEW contacts that have next_action_at set.

    This handles durable campaign starts -- when POST /campaigns/{id}/start
    is called it sets next_action_at on all NEW contacts, and this job
    picks them up.

    Work claiming (Phase 8)
    -----------------------
    Uses ``claim_due_contacts`` with status=NEW so concurrent workers
    never double-send the initial outreach to the same contact.  An
    expired-lease sweep runs at the top of each call to recover any
    contacts abandoned by a previously-crashed worker.
    """
    from mailer_agent.followup.work_claiming import (
        claim_due_contacts,
        make_worker_id,
        recover_expired_claims,
        release_claim,
    )
    from mailer_agent.followup.engine import send_initial_outreach, stagger_sleep
    from mailer_agent.models import Campaign, ContactStatus

    worker_id = make_worker_id()

    with session_scope() as db:
        # Sweep any leases left by a crashed worker (covers both NEW and ACTIVE).
        recover_expired_claims(db)

        due_new_contacts = claim_due_contacts(
            db,
            worker_id=worker_id,
            status=ContactStatus.NEW.value,
            limit=settings.max_sends_per_cycle,
        )

        if not due_new_contacts:
            return

        # Reload with Campaign join so relationship is available.
        contact_ids = [c.id for c in due_new_contacts]
        due_new_contacts = (
            db.query(Contact)
            .join(Campaign)
            .filter(
                Contact.id.in_(contact_ids),
                Campaign.is_active.is_(True),
            )
            .all()
        )

        results = []
        for i, contact in enumerate(due_new_contacts):
            if i > 0:
                stagger_sleep()
            try:
                result = send_initial_outreach(db, contact)
                results.append(result)
                db.commit()
            except Exception as e:
                logger.exception(f"Failed to send initial outreach to contact {contact.id}: {e}")
                db.rollback()
            finally:
                # Release regardless of outcome so the contact is not
                # stuck behind a lease after a send error.
                release_claim(db, contact)

        sent = sum(1 for r in results if r.get("action") == "sent")
        failed = sum(1 for r in results if r.get("action") == "failed")
        skipped = len(results) - sent - failed

        logger.info(
            f"Initial outreach cycle (worker={worker_id}): {len(results)} contacts processed "
            f"(Sent: {sent}, Failed: {failed}, Skipped: {skipped})"
        )


def dispatch_followups_job() -> None:
    """
    Dispatch follow-ups using conversation-aware logic.

    Delegates to ``run_followup_cycle`` which internally uses safe work
    claiming (Phase 8) -- no in-memory locking needed here.
    """
    from mailer_agent.followup.work_claiming import make_worker_id

    worker_id = make_worker_id()
    with session_scope() as db:
        results = run_followup_cycle(db, worker_id=worker_id)
        if results:
            sent = sum(1 for r in results if r.get("action") == "sent")
            failed = sum(1 for r in results if r.get("action") == "failed")
            skipped = len(results) - sent - failed

            logger.info(
                f"Follow-up cycle (worker={worker_id}): {len(results)} contacts processed "
                f"(Sent: {sent}, Failed: {failed}, Skipped: {skipped})"
            )


def health_check_job() -> None:
    """
    Periodic health check and metrics logging.
    """
    try:
        from mailer_agent.llm.provider_v2 import llm_metrics
        metrics = llm_metrics.get_stats()
        logger.info(f"System health check - LLM metrics: {metrics}")
    except Exception as e:
        logger.error(f"Health check failed: {e}")


def dispatch_external_dispatches_job() -> None:
    """
    LeadBoost async dispatch: claim + send queued ExternalDispatch rows.

    Its own job on its own cadence (settings.external_dispatch_poll_seconds,
    default 15 s) -- NOT followup_poll_seconds (300 s), and it never touches
    the follow-up drafting engine or Contact rows. The heavy lifting lives in
    mail/external_dispatch_worker.py, which is imported lazily so this
    module's import graph does not change.

    max_instances=1 (below) only stops this job overlapping ITSELF inside one
    process. Protection between worker processes is the database claim.
    """
    from mailer_agent.mail.external_dispatch_worker import run_external_dispatch_cycle

    try:
        results = run_external_dispatch_cycle()
    except Exception:
        logger.exception("dispatch_external_dispatches_job failed")
        return
    if results:
        counts: dict[str, int] = {}
        for r in results:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        logger.info("External dispatch cycle processed %d: %s", len(results), counts)


def generate_external_dispatches_job() -> None:
    """
    M2-B: draft the message for accepted generated-outreach dispatches
    (QUEUED, no Message yet) behind the durable acceptance. Its own job so a
    slow LLM call never delays sending; the LLM-free dispatch job above then
    sends what this job finishes. Imported lazily like its sibling.
    """
    from mailer_agent.mail.outreach_generation_worker import run_generation_cycle

    try:
        results = run_generation_cycle()
    except Exception:
        logger.exception("generate_external_dispatches_job failed")
        return
    if results:
        counts: dict[str, int] = {}
        for r in results:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        logger.info("External dispatch generation cycle processed %d: %s", len(results), counts)


def recover_external_generation_leases_job() -> None:
    """
    Separate sweep: expired GENERATING -> QUEUED (nothing was sent; the row is
    simply regenerated). Distinct job so it fires while generation is blocked
    inside a slow LLM call.
    """
    from mailer_agent.mail.outreach_generation_worker import run_generation_lease_recovery

    try:
        n = run_generation_lease_recovery()
    except Exception:
        logger.exception("recover_external_generation_leases_job failed")
        return
    if n:
        logger.warning("External dispatch generation lease recovery requeued %d row(s)", n)


def recover_external_dispatch_leases_job() -> None:
    """
    Separate sweep: expired SENDING -> UNKNOWN (never QUEUED, no SMTP).
    A distinct job so it still fires while dispatch_external_dispatches_job
    is blocked inside a slow SMTP call.
    """
    from mailer_agent.mail.external_dispatch_worker import run_external_dispatch_lease_recovery

    try:
        n = run_external_dispatch_lease_recovery()
    except Exception:
        logger.exception("recover_external_dispatch_leases_job failed")
        return
    if n:
        logger.warning("External dispatch lease recovery resolved %d row(s) to UNKNOWN", n)


def start_scheduler() -> BackgroundScheduler:
    """
    Start the unified scheduler.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    
    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={
            "misfire_grace_time": 60,
            "coalesce": True,
        },
    )
    
    # Reply polling
    scheduler.add_job(
        poll_replies_job,
        "interval",
        seconds=settings.imap_poll_seconds,
        id="poll_replies",
        max_instances=1  # Prevent overlapping runs
    )
    
    # M3: per-Mailbox inbound (each Mailbox's own IMAP identity, tenant bound
    # from Mailbox.organization_id). Same cadence setting as the legacy poll.
    scheduler.add_job(
        poll_mailbox_inboxes_job,
        "interval",
        seconds=settings.imap_poll_seconds,
        id="poll_mailbox_inboxes",
        max_instances=1
    )

    # Initial outreach dispatch (NEW contacts with next_action_at set)
    scheduler.add_job(
        dispatch_new_contacts_job,
        "interval",
        seconds=settings.followup_poll_seconds,
        id="dispatch_new_contacts",
        max_instances=1
    )
    
    # Follow-up dispatch (ACTIVE contacts)
    scheduler.add_job(
        dispatch_followups_job,
        "interval",
        seconds=settings.followup_poll_seconds,
        id="dispatch_followups",
        max_instances=1
    )
    
    # LeadBoost async dispatch (ExternalDispatch QUEUED -> SENDING -> outcome)
    scheduler.add_job(
        dispatch_external_dispatches_job,
        "interval",
        seconds=settings.external_dispatch_poll_seconds,
        id="dispatch_external_dispatches",
        max_instances=1
    )

    # M2-B: durable generation of generated-outreach messages (QUEUED, no
    # Message -> GENERATING -> QUEUED with Message / FAILED)
    scheduler.add_job(
        generate_external_dispatches_job,
        "interval",
        seconds=settings.external_dispatch_poll_seconds,
        id="generate_external_dispatches",
        max_instances=1
    )

    scheduler.add_job(
        recover_external_generation_leases_job,
        "interval",
        seconds=settings.external_dispatch_recovery_poll_seconds,
        id="recover_external_generation_leases",
        max_instances=1
    )

    # Expired-lease recovery for ExternalDispatch (SENDING -> UNKNOWN)
    scheduler.add_job(
        recover_external_dispatch_leases_job,
        "interval",
        seconds=settings.external_dispatch_recovery_poll_seconds,
        id="recover_external_dispatch_leases",
        max_instances=1
    )

    # Health check (every 5 minutes)
    scheduler.add_job(
        health_check_job,
        "interval",
        seconds=300,
        id="health_check",
        max_instances=1
    )
    
    scheduler.start()
    _scheduler = scheduler
    
    logger.info(
        f"Unified scheduler started:\n"
        f"  Reply poll: every {settings.imap_poll_seconds}s\n"
        f"  Initial outreach dispatch: every {settings.followup_poll_seconds}s\n"
        f"  Follow-up dispatch: every {settings.followup_poll_seconds}s\n"
        f"  External dispatch: every {settings.external_dispatch_poll_seconds}s "
        f"(lease recovery every {settings.external_dispatch_recovery_poll_seconds}s)\n"
        f"  Health check: every 300s"
    )
    
    return scheduler


def stop_scheduler() -> None:
    """Stop the scheduler."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
