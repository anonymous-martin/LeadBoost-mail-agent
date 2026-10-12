from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mailer_agent.api import (
    campaigns,
    contacts,
    integrations,
    integrations_conversation,
    integrations_generated,
    mailboxes,
    messages,
    system,
    webhooks,
)
from mailer_agent.api.deps import validate_auth_configuration
from mailer_agent.config import get_settings
from mailer_agent.db import init_db
from mailer_agent.followup.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mailer_agent")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # C15.1: fail closed. Missing/invalid authentication configuration stops the API from
    # starting at all (before any database work) instead of serving unauthenticated routes.
    validate_auth_configuration()
    init_db()
    logger.info(
        "Mailer Agent starting | live_sending_enabled=%s auto_reply_enabled=%s run_scheduler_in_process=%s",
        settings.live_sending_enabled, settings.auto_reply_enabled, settings.run_scheduler_in_process,
    )
    if not settings.live_sending_enabled:
        logger.warning(
            "DRY RUN MODE: LIVE_SENDING_ENABLED is not set to true. "
            "Messages will be generated and logged but NOT actually emailed."
        )
    if settings.run_scheduler_in_process:
        start_scheduler()
    else:
        logger.info(
            "RUN_SCHEDULER_IN_PROCESS=false -- this process will only serve the API. "
            "Make sure `python worker.py` is running somewhere as a separate process, "
            "or scheduled sends/replies will never be picked up."
        )
    yield
    if settings.run_scheduler_in_process:
        stop_scheduler()


app = FastAPI(
    title="Mailer Agent",
    description=(
        "Standalone AI sales-outreach mailer: intelligent drafting, dynamic "
        "follow-up cadence, reply detection, and memory -- exposed as a REST "
        "API for any caller (e.g. LeadBoost) to integrate with."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(campaigns.router)
app.include_router(contacts.router)
app.include_router(messages.router)
app.include_router(webhooks.router)
app.include_router(system.router)
app.include_router(integrations.router)
app.include_router(integrations_generated.router)
app.include_router(integrations_conversation.router)
app.include_router(mailboxes.router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "live_sending_enabled": settings.live_sending_enabled,
        "auto_reply_enabled": settings.auto_reply_enabled,
    }
