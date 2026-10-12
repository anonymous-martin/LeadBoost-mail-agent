"""
Webhook-based reply intake.

Why this exists alongside IMAP polling: research into production email
agents in 2026 turned up two consistent findings that matter here --
(1) Gmail's IDLE support is unreliable in practice and polling a Gmail
inbox on an automated cadence has led to real accounts being flagged
for "suspicious automated activity" and suspended, and (2) IMAP polling
intervals mean 1-15+ minutes of latency before a reply is even seen.
A transactional email provider's inbound-parse webhook (Postmark
Inbound, Mailgun Routes, SendGrid Inbound Parse) avoids both: no
polling, no personal-inbox automation risk, and replies arrive within
seconds of being received.

This endpoint deliberately accepts one normalized shape rather than
trying to parse every provider's native payload -- map your provider's
webhook to this shape using its own payload-template feature (Postmark
and Mailgun both support this) or a tiny transform in front of this
endpoint. See the README for the exact field mapping per provider.

IMAP polling (mail/imap_reader.py) is left in place as the zero-setup
default for local development and low-volume use -- this webhook is
the recommended upgrade once you're sending real production volume.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from mailer_agent.api.deps import get_integration_org_id, require_api_key
from mailer_agent.db import get_db
from mailer_agent.mail.imap_reader import InboundEmail
from mailer_agent.mail.reply_handler import process_inbound_email

logger = logging.getLogger("mailer_agent.api.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"], dependencies=[Depends(require_api_key)])


class InboundEmailWebhook(BaseModel):
    from_email: str
    to_email: str | None = None  # The inbox this was sent to (for tenant resolution)
    subject: str | None = None
    body_text: str
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = []
    # Optional: pass through if your inbound-parse provider surfaces the
    # Auto-Submitted header or an equivalent "this is an autoresponder"
    # flag (Postmark and Mailgun both expose raw headers you can map
    # this from). Defaults to False, meaning "signal not available" --
    # NOT "confirmed not an autoresponder" -- so OOO detection falls
    # through to semantic classification when this isn't provided.
    auto_submitted: bool = False


@router.post("/inbound-email")
def receive_inbound_email(
    payload: InboundEmailWebhook,
    # C15.1b: the tenant is the one the presented key maps to (fail-closed variant: 503 when
    # no keys are configured, 401 for a missing/unknown key) -- never anything in the payload.
    org_id: str = Depends(get_integration_org_id),
    db: Session = Depends(get_db),
):
    email_in = InboundEmail(
        from_email=payload.from_email.strip().lower(),
        subject=payload.subject or "",
        body_text=payload.body_text,
        message_id=payload.message_id,
        in_reply_to=payload.in_reply_to,
        references=payload.references,
        to_email=payload.to_email,  # Pass through for tenant resolution
        auto_submitted=payload.auto_submitted,
    )
    result = process_inbound_email(db, email_in, organization_id=org_id)
    db.commit()
    logger.info("Webhook inbound processed for %s: %s", email_in.from_email, result)
    return result
