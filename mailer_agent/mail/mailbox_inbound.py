"""
M3: mailbox-bound inbound polling.

Each eligible Mailer Mailbox is polled with ITS OWN IMAP configuration and ITS OWN
encrypted credential, and everything it receives is bound to that Mailbox's
organization before any message content is interpreted.

    for each eligible mailbox:
        fresh DB read  ->  (mailbox_id, organization_id, imap config, ciphertext)
        decrypt password, connect, LOGIN         <- the only place a credential exists
        UID SEARCH UNSEEN -> UID FETCH (BODY.PEEK[]) -> parse
        per message:  own session -> process_mailbox_inbound -> COMMIT -> UID STORE Seen flag

Separate from the legacy deployment-global poll (mail/imap_reader.fetch_unseen_replies
+ followup/scheduler.poll_replies_job), which is kept for non-integrated campaigns.
Nothing here ever reads settings.imap_host/imap_username/imap_password: the global
IMAP identity is NOT a fallback for mailbox-bound work. Do not configure the same
physical account both as the global IMAP identity and as a Mailbox's IMAP identity --
the two paths dedupe independently.

Eligibility (re-evaluated from the database immediately before each mailbox is
polled, so a mailbox disabled or re-credentialed mid-cycle is honoured, and no
credential is ever cached across cycles): status ACTIVE, imap_host / imap_port /
imap_username / imap_password_enc all present. Anything else is skipped without a
connection attempt.

Isolation: every mailbox runs in its own try/except and every message in its own
database session/transaction, so one bad mailbox, credential or message never stops
another. No database transaction is held across any IMAP network call. A mailbox's
failure is reported through its MailboxPollOutcome and a WARNING that carries the
mailbox reference and an exception TYPE only (never a password, address, subject or
body).

Concurrency: one logical poll per mailbox at a time comes from the existing topology
(one scheduler process, max_instances=1). If two pollers ever overlap, correctness
still holds because inbound persistence is idempotent on (mailbox_id, Message-ID).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session

from mailer_agent.config import get_settings
from mailer_agent.mail.imap_reader import InboundEmail, drain_unseen, open_authenticated_connection
from mailer_agent.mail.reply_handler_v2 import process_mailbox_inbound
from mailer_agent.mailbox_secrets import (
    MailboxDecryptionError,
    MailboxEncryptionUnavailable,
    decrypt_secret,
)
from mailer_agent.models import Mailbox, MailboxStatus
from mailer_agent.worker_status import (
    BUCKET_AMBIGUOUS,
    BUCKET_DUPLICATE,
    BUCKET_PERSISTED,
    BUCKET_UNMATCHED,
    BUCKET_UNRECOGNIZED,
    OUTCOME_COMPLETED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    classification_flag,
    classify_inbound_result,
)

logger = logging.getLogger("mailer_agent.mail.mailbox_inbound")

SessionFactory = Callable[[], Session]

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped_ineligible"
STATUS_CREDENTIAL_UNAVAILABLE = "credential_unavailable"
STATUS_IMAP_ERROR = "imap_error"
STATUS_UNEXPECTED = "unexpected_error"


@dataclass(frozen=True)
class MailboxImapTarget:
    """What one poll needs. The password exists only as ciphertext here."""

    mailbox_id: int
    organization_id: str
    public_reference: str
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password_enc: str = field(repr=False)


@dataclass
class MailboxPollOutcome:
    mailbox_id: int
    public_reference: str | None = None
    status: str = STATUS_OK
    processed: int = 0   # messages durably handled and marked Seen
    left_unseen: int = 0  # messages that will be offered again next poll
    error_type: str | None = None
    # PR4 -- what the handler ACTUALLY returned for each message it was given, by bucket
    # (persisted / duplicate / unmatched / ambiguous / unrecognized), counted when the handler
    # returns -- i.e. after its COMMIT and before the Seen flag. So these are handler outcomes, not
    # a count of distinct emails: a message whose Seen flag then fails to store is offered again
    # next poll and counted again, as a duplicate. Unmatched/ambiguous mail is deliberately NOT
    # persisted as a Message row; it is retained in unmatched_inbound_messages, and this tally is
    # its aggregate, address-free count.
    handled: dict[str, int] = field(default_factory=dict)
    classification_succeeded: int = 0  # persisted messages whose handler result said success
    classification_failed: int = 0     # persisted messages whose handler result said failure


def _default_session_factory() -> Session:
    from mailer_agent.db import SessionLocal

    return SessionLocal()


def _eligible(mailbox: Mailbox) -> bool:
    return bool(
        mailbox.status == MailboxStatus.ACTIVE.value
        and mailbox.imap_host and mailbox.imap_host.strip()
        and mailbox.imap_port
        and mailbox.imap_username and mailbox.imap_username.strip()
        and mailbox.imap_password_enc
    )


def list_pollable_mailbox_ids(session_factory: SessionFactory | None = None) -> list[int]:
    db = (session_factory or _default_session_factory)()
    try:
        return [m.id for m in db.query(Mailbox).order_by(Mailbox.id).all() if _eligible(m)]
    finally:
        db.close()


def load_pollable_target(
    mailbox_id: int, session_factory: SessionFactory | None = None
) -> MailboxImapTarget | None:
    """Fresh read of one mailbox; None if it is no longer eligible."""
    db = (session_factory or _default_session_factory)()
    try:
        mailbox = db.get(Mailbox, mailbox_id)
        if mailbox is None or not _eligible(mailbox):
            return None
        return MailboxImapTarget(
            mailbox_id=mailbox.id,
            organization_id=mailbox.organization_id,
            public_reference=mailbox.public_reference,
            imap_host=mailbox.imap_host.strip(),
            imap_port=mailbox.imap_port,
            imap_username=mailbox.imap_username.strip(),
            imap_password_enc=mailbox.imap_password_enc,
        )
    finally:
        db.close()  # no transaction or session is held while we talk IMAP


def _persist_inbound(
    session_factory: SessionFactory, target: MailboxImapTarget, email_in: InboundEmail
) -> dict:
    """One message, one session, one transaction. Returns only after COMMIT."""
    db = session_factory()
    try:
        result = process_mailbox_inbound(
            db,
            email_in,
            mailbox_id=target.mailbox_id,
            organization_id=target.organization_id,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _tally_handler_result(outcome: MailboxPollOutcome, result: object) -> None:
    """Fold one handler result into ``outcome`` as bucket counts. Never raises, keeps no content.

    Only the bucket NAME and a boolean survive; the result dict (which can carry addresses or
    reasons) is dropped here.
    """
    try:
        bucket = classify_inbound_result(result)
        outcome.handled[bucket] = outcome.handled.get(bucket, 0) + 1
        flag = classification_flag(result)
        if flag is True:
            outcome.classification_succeeded += 1
        elif flag is False:
            outcome.classification_failed += 1
    except Exception as exc:  # noqa: BLE001 - observability must never turn a handled message into a retry
        logger.debug("inbound tally skipped (%s)", type(exc).__name__)


def poll_mailbox(
    target: MailboxImapTarget, session_factory: SessionFactory | None = None
) -> MailboxPollOutcome:
    """Poll one mailbox. Never raises: every failure becomes the outcome."""
    factory = session_factory or _default_session_factory
    outcome = MailboxPollOutcome(mailbox_id=target.mailbox_id, public_reference=target.public_reference)
    conn = None
    try:
        # --- the credential boundary: decrypt immediately before LOGIN ---------
        try:
            password = decrypt_secret(target.imap_password_enc)
        except (MailboxEncryptionUnavailable, MailboxDecryptionError) as e:
            outcome.status = STATUS_CREDENTIAL_UNAVAILABLE
            outcome.error_type = type(e).__name__
            logger.warning(
                "Mailbox %s not polled: IMAP credential unavailable (%s)",
                target.public_reference, outcome.error_type,
            )
            return outcome

        try:
            conn = open_authenticated_connection(
                target.imap_host, target.imap_port, target.imap_username, password,
                get_settings().imap_timeout_seconds,
            )
        finally:
            password = None  # noqa: F841 - drop the only plaintext reference
        # ----------------------------------------------------------------------

        def _handle(email_in: InboundEmail) -> dict:
            # Unchanged contract: persist, COMMIT, return -- only then does drain_unseen mark Seen.
            # The tally happens strictly after that return value exists and cannot raise.
            result = _persist_inbound(factory, target, email_in)
            _tally_handler_result(outcome, result)
            return result

        stats = drain_unseen(conn, _handle)
        outcome.processed = stats.seen_marked
        outcome.left_unseen = stats.left_unseen
    except Exception as e:  # noqa: BLE001 - isolate this mailbox; type name only
        outcome.status = STATUS_IMAP_ERROR
        outcome.error_type = type(e).__name__
        logger.warning(
            "Mailbox %s poll failed (%s); other mailboxes are unaffected",
            target.public_reference, outcome.error_type,
        )
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
    return outcome


@dataclass
class MailboxCycleReport:
    """One mailbox cycle: the per-mailbox outcomes, plus whether the mailbox LIST could be read.

    ``poll_all_mailboxes`` returns ``[]`` both when no mailbox is eligible and when listing them
    failed, so the list alone cannot tell "nothing to do" from "could not look".
    """

    outcomes: list[MailboxPollOutcome] = field(default_factory=list)
    listing_failed: bool = False


def poll_all_mailboxes_report(session_factory: SessionFactory | None = None) -> MailboxCycleReport:
    """One cycle over every currently eligible mailbox, with the listing result. Never raises."""
    report = MailboxCycleReport()
    try:
        ids = list_pollable_mailbox_ids(session_factory)
    except Exception as e:  # noqa: BLE001
        logger.error("Mailbox inbound cycle could not list mailboxes (%s)", type(e).__name__)
        report.listing_failed = True
        return report
    for mailbox_id in ids:
        try:
            target = load_pollable_target(mailbox_id, session_factory)  # fresh: disabled/rotated mid-cycle
            if target is None:
                report.outcomes.append(MailboxPollOutcome(mailbox_id=mailbox_id, status=STATUS_SKIPPED))
                continue
            report.outcomes.append(poll_mailbox(target, session_factory))
        except Exception as e:  # noqa: BLE001
            logger.error("Mailbox %s poll crashed unexpectedly (%s)", mailbox_id, type(e).__name__)
            report.outcomes.append(
                MailboxPollOutcome(mailbox_id=mailbox_id, status=STATUS_UNEXPECTED, error_type=type(e).__name__)
            )
    return report


def poll_all_mailboxes(session_factory: SessionFactory | None = None) -> list[MailboxPollOutcome]:
    """One cycle over every currently eligible mailbox. Never raises. (Outcomes only.)"""
    return poll_all_mailboxes_report(session_factory).outcomes


def summarize_mailbox_cycle(report: MailboxCycleReport) -> tuple[str, dict[str, int], str | None]:
    """(cycle outcome, whitelisted counters, error type) for the worker status record.

    failed    the mailbox list could not be read -- nothing was polled.
    degraded  the cycle ran, but at least one mailbox could not be polled (credential unavailable,
              IMAP error, unexpected error) or at least one message was left unseen.
    completed every eligible mailbox was polled and nothing was left unseen. (Also true when there
              were no eligible mailboxes: that is the honest result of a successful listing.)
    """
    c = dict.fromkeys(
        (
            "mailboxes_listed", "mailboxes_ok", "mailboxes_skipped_ineligible",
            "mailboxes_credential_unavailable", "mailboxes_imap_error", "mailboxes_unexpected_error",
            "messages_marked_seen", "messages_left_unseen",
            "handled_persisted", "handled_duplicate", "handled_unmatched", "handled_ambiguous",
            "handled_unrecognized", "classification_succeeded", "classification_failed",
        ),
        0,
    )
    status_key = {
        STATUS_OK: "mailboxes_ok",
        STATUS_SKIPPED: "mailboxes_skipped_ineligible",
        STATUS_CREDENTIAL_UNAVAILABLE: "mailboxes_credential_unavailable",
        STATUS_IMAP_ERROR: "mailboxes_imap_error",
        STATUS_UNEXPECTED: "mailboxes_unexpected_error",
    }
    bucket_key = {
        BUCKET_PERSISTED: "handled_persisted", BUCKET_DUPLICATE: "handled_duplicate",
        BUCKET_UNMATCHED: "handled_unmatched", BUCKET_AMBIGUOUS: "handled_ambiguous",
        BUCKET_UNRECOGNIZED: "handled_unrecognized",
    }
    first_error: str | None = None
    for o in report.outcomes:
        c["mailboxes_listed"] += 1
        key = status_key.get(o.status)
        if key:
            c[key] += 1
        c["messages_marked_seen"] += o.processed
        c["messages_left_unseen"] += o.left_unseen
        for bucket, n in o.handled.items():
            if bucket in bucket_key:
                c[bucket_key[bucket]] += n
        c["classification_succeeded"] += o.classification_succeeded
        c["classification_failed"] += o.classification_failed
        if first_error is None and o.status != STATUS_OK and o.status != STATUS_SKIPPED:
            first_error = o.error_type
    if report.listing_failed:
        return OUTCOME_FAILED, c, "MailboxListingFailed"
    unhealthy = (
        c["mailboxes_credential_unavailable"] + c["mailboxes_imap_error"]
        + c["mailboxes_unexpected_error"] + c["messages_left_unseen"]
    )
    return (OUTCOME_DEGRADED if unhealthy else OUTCOME_COMPLETED), c, first_error
