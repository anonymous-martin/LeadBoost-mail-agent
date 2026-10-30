"""
Data model.

Design notes:
- A Campaign holds *sender identity + offer + cadence* -- the things a
  user configures once per outreach effort. Nothing industry-specific
  is hardcoded here; `value_prop` / `tone` / `follow_up_days` are all
  user-supplied data, not code branches.
- A Contact is one prospect inside a campaign. `follow_up_days` on the
  campaign is a JSON list of integers (e.g. [3, 7, 14]) -- "wait 3 days,
  then 7 more, then 14 more" -- read and applied dynamically by the
  scheduler; nothing about cadence is baked into logic.
- A Message is one email in either direction. The full ordered set of
  Messages for a Contact *is* the conversation memory -- there is no
  separate "memory blob" that can drift from what was actually sent/
  received. For long threads, `Contact.memory_summary` holds an
  LLM-generated rolling summary of everything older than the last few
  messages, so prompts stay bounded without ever discarding the
  underlying record.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase, relationship

from mailer_agent.utils.datetime_utils import utcnow


class Base(DeclarativeBase):
    pass


class ContactStatus(str, enum.Enum):
    """
    Relationship state in the sales conversation.
    
    Enhanced to reflect B2B buying stages and conversation state.
    """
    NEW = "new"                        # created, nothing sent yet
    ACTIVE = "active"                  # in sequence, awaiting next action or reply
    REPLIED = "replied"                # last inbound message needs a reply
    ENGAGED = "engaged"                # positive interest shown, active conversation
    QUALIFYING = "qualifying"          # asking questions, gathering info
    EVALUATING = "evaluating"          # comparing solutions, commercial discussion
    MEETING_REQUESTED = "meeting_requested"  # prospect wants to meet
    MEETING_SCHEDULED = "meeting_scheduled"  # meeting confirmed
    NEGOTIATING = "negotiating"        # discussing terms, pricing, contract
    NURTURE = "nurture"                # interested but not ready (future opportunity)
    SEQUENCE_COMPLETE = "sequence_complete"  # ran out of follow-ups, no reply
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"
    SUPPRESSED = "suppressed"          # opted out / bounced -- never message again
    PAUSED = "paused"                  # human paused it manually
    NEEDS_REVIEW = "needs_review"      # requires human attention


class MessageDirection(str, enum.Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class MessageType(str, enum.Enum):
    INITIAL = "initial_outreach"
    FOLLOW_UP = "follow_up"
    REPLY = "reply"
    CLOSING = "closing"


class MessageStatus(str, enum.Enum):
    """
    Message send/receive state with explicit lifecycle.
    
    Enhanced to track send lifecycle and ambiguous outcomes.

    Verified-in-use vs reserved: DRAFT, SENT, FAILED, UNKNOWN, and
    RECEIVED are actually set by the current send paths (api/messages.py,
    followup/engine_v2.py, mail/reply_handler_v2.py) -- send is
    synchronous within one request/job, so a message goes straight from
    DRAFT to a terminal outcome. APPROVED, PENDING, and SENDING are
    defined but currently unused by any code path: they describe an
    asynchronous approve -> queue -> in-flight pipeline this system
    doesn't currently have (approval triggers a synchronous send in the
    same call). Kept in the enum as forward-reserved states for if that
    changes, not because anything sets them today -- don't assume a
    message will ever be observed in one of these three states via the
    current API.
    """
    DRAFT = "draft"          # generated, not sent (live_sending_enabled=False, or awaiting approval)
    APPROVED = "approved"    # reserved, not currently set -- see class docstring
    PENDING = "pending"      # reserved, not currently set -- see class docstring
    SENDING = "sending"      # reserved, not currently set -- see class docstring
    SENT = "sent"            # successfully sent and confirmed
    FAILED = "failed"        # send failed permanently
    UNKNOWN = "unknown"      # provider may have sent, but we don't know (crash window)
    RECEIVED = "received"    # inbound messages are always "received", never draft/sent


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        # Phase C (LeadBoost integration): at most one campaign per
        # organization may carry a given integration_source value.
        # integration_source is NULL for every ordinary, human-created
        # campaign, and both PostgreSQL and SQLite treat NULL as
        # distinct-from-itself in a unique index -- so this constraint is
        # inert for all existing/ordinary campaigns and only bites once a
        # second campaign for the same org tries to claim the same
        # non-NULL integration_source (e.g. two concurrent "first use"
        # requests both trying to create the one leadboost campaign for
        # an org -- see api/integrations.py's get-or-create, which relies
        # on this exact constraint as its race backstop, the same
        # "app checks, DB enforces" pattern already used by
        # uq_contacts_campaign_email below). A plain unique INDEX (not a
        # named UNIQUE CONSTRAINT) so the equivalent migration DDL
        # (CREATE UNIQUE INDEX ...) works unchanged on both PostgreSQL and
        # SQLite -- SQLite has no ALTER TABLE ... ADD CONSTRAINT.
        Index(
            "uq_campaigns_org_integration_source",
            "organization_id",
            "integration_source",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    
    # Multi-tenancy: Organization ownership
    # Each campaign belongs to exactly one organization (LeadBoost customer)
    organization_id = Column(String, nullable=True, index=True)  # Nullable for migration compatibility

    # Phase C (LeadBoost integration): which external integration this
    # campaign is the fixed, deterministic home for -- e.g. "leadboost".
    # NULL for every ordinary, human-created campaign; never set or read
    # by any pre-existing code path. See uq_campaigns_org_integration_source
    # above and api/integrations.py's get-or-create for how exactly one
    # such campaign per organization is guaranteed.
    #
    # No index=True here (Batch 1.1 cleanup): a standalone single-column
    # index on this column alone would be redundant today --
    # uq_campaigns_org_integration_source above already covers any query
    # that filters on (organization_id, integration_source) together,
    # which is every query that currently exists (grep confirms nothing
    # in this codebase filters by integration_source without
    # organization_id also in the predicate). A standalone index here
    # would only add write overhead with no query-planning benefit for
    # any access pattern that exists today. If a future admin/reporting
    # need arises to query across all organizations by integration_source
    # alone, add it back then, deliberately, for that need.
    integration_source = Column(String, nullable=True)

    # Sender identity -- who this campaign is "from"
    sender_name = Column(String, nullable=False)
    sender_org = Column(String, nullable=False)
    sender_email = Column(String, nullable=False)
    reply_to_email = Column(String, nullable=True)
    sender_title = Column(String, nullable=True)
    sender_signature_extra = Column(Text, nullable=True)  # e.g. phone / calendly link

    # The offer -- what this campaign is trying to sell / get agreement on.
    # This is the single most important field for avoiding generic output:
    # it's real, user-written business content, not a template category.
    value_prop = Column(Text, nullable=False)
    # Optional: specific proof points, case studies, pricing notes the
    # agent is allowed to reference. Kept separate from value_prop so the
    # prompt can clearly mark it as "verified facts you may cite".
    proof_points = Column(Text, nullable=True)
    tone = Column(String, default="professional, direct, concise")

    # Cadence -- dynamic, per campaign, not fixed in code.
    follow_up_days = Column(JSON, default=lambda: [3, 7, 14])
    max_follow_ups = Column(Integer, nullable=True)  # defaults to len(follow_up_days)
    
    # Timezone for campaign scheduling
    # Used for interpreting "business hours" and prospect local time
    # Format: IANA timezone string (e.g., "America/New_York", "UTC", "Asia/Kolkata")
    timezone = Column(String, default="UTC")

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    contacts = relationship("Contact", back_populates="campaign")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        # Enforce idempotency: same email cannot appear twice in one campaign.
        # This is the DB-level backstop; the application checks first to give
        # a friendly error, but this constraint prevents races.
        UniqueConstraint("campaign_id", "email", name="uq_contacts_campaign_email"),
    )

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)

    name = Column(String, nullable=True)
    email = Column(String, nullable=False, index=True)
    title = Column(String, nullable=True)
    company = Column(String, nullable=True)

    # Real, structured facts about this contact/company the agent may
    # use -- e.g. "raised Series A in March", "posted 4 open SDR roles".
    # Deliberately freeform text supplied by the caller (or a future
    # enrichment step) rather than a fixed schema, but always presented
    # to the LLM as "verified context" so it never has to invent facts
    # to sound personalized.
    context_notes = Column(Text, nullable=True)

    status = Column(String, default=ContactStatus.NEW.value)
    follow_up_index = Column(Integer, default=0)  # how many follow-ups sent so far
    next_action_at = Column(DateTime, nullable=True)  # when the scheduler should act next
    
    # Enhanced relationship tracking
    last_reply_at = Column(DateTime, nullable=True)  # When prospect last replied
    last_outbound_at = Column(DateTime, nullable=True)  # When we last sent
    buying_stage = Column(String, nullable=True)  # From semantic analysis
    engagement_score = Column(Float, default=0.0)  # 0-1 score based on interactions

    # Rolling LLM-generated summary of older messages, used to keep
    # long threads' prompts bounded. See memory/store.py.
    #
    # Data-lineage note: memory/store.py's summarization used to include
    # every Message regardless of status (DRAFT/FAILED/UNKNOWN outbound
    # rows, not just genuinely-sent/received ones) when folding older
    # messages into this field -- fixed to use only confirmed
    # conversational evidence (see _conversational_evidence in that
    # module). That fix is forward-looking only: a memory_summary value
    # persisted before the fix may have been generated from a window
    # that included a draft/failed message's content, and this text
    # column has no per-fact provenance to selectively correct. No
    # migration was written for this deliberately -- there's no schema
    # change involved (the column is unchanged), and automatically
    # re-summarizing every existing contact would mean an unreviewed LLM
    # call per contact with its own risk of introducing new errors,
    # which isn't obviously safer than a stale field. If a specific
    # contact's summary is suspected of being contaminated (e.g. it
    # mentions a figure that doesn't appear in any SENT/RECEIVED message
    # for that contact), the safe fix is to clear that one
    # memory_summary value -- it will regenerate correctly (now
    # provenance-filtered) once the thread crosses SUMMARIZE_THRESHOLD
    # again.
    memory_summary = Column(Text, nullable=True)

    # Distributed work claiming (Phase 8 — safe work claiming).
    # When a scheduler worker picks up a contact for outreach/follow-up
    # it writes its worker_id here and sets claimed_at to now().
    # Only the claiming worker should then process this contact.
    # Lease expiry: if claimed_at < now() - CLAIM_LEASE_SECONDS (default 5 min)
    # the record is considered abandoned and can be re-claimed by any worker.
    # These fields are NULL when the contact is not currently being processed.
    claimed_by = Column(String, nullable=True, index=True)    # worker identity string
    claimed_at = Column(DateTime, nullable=True)              # when the lease was taken

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    campaign = relationship("Campaign", back_populates="contacts")
    messages = relationship(
        "Message", back_populates="contact", order_by="Message.created_at"
    )


def new_reply_reference() -> str:
    """A fresh opaque public handle for a Message: 32 lowercase hex characters (128 random bits).

    This -- never Message.id -- is what the LeadBoost conversation view hands out for an inbound
    message and accepts back as ``in_reply_to.message_reference``. It carries no sequence, tenant or
    time information, so it cannot be enumerated; every use is still resolved tenant- and
    mailbox-scoped (api/integrations_generated.py::_resolve_reply_parent)."""
    return uuid.uuid4().hex


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)

    direction = Column(String, nullable=False)  # MessageDirection
    message_type = Column(String, nullable=True)  # MessageType (null for inbound)
    subject = Column(String, nullable=True)
    body = Column(Text, nullable=False)

    status = Column(String, default=MessageStatus.DRAFT.value)

    # Threading headers -- required to correlate an inbound reply back
    # to the right contact/thread instead of guessing from subject text.
    message_id_header = Column(String, nullable=True, index=True)   # our own Message-ID when sent
    in_reply_to_header = Column(String, nullable=True, index=True)  # for outbound: prior Message-ID
    references_header = Column(Text, nullable=True)

    # Only set for inbound messages, by the reply classifier.
    detected_intent = Column(String, nullable=True)  # interested/objection/question/not_interested/oos/unsubscribe/neutral
    intent_confidence = Column(Float, nullable=True)
    
    # Enhanced semantic analysis (JSON stored)
    semantic_analysis = Column(JSON, nullable=True)  # Full SemanticIntent as JSON
    classification_success = Column(Boolean, nullable=True)  # Did classification succeed?
    classification_failure_reason = Column(String, nullable=True)  # If failed, why?

    error_message = Column(Text, nullable=True)  # populated when status=failed

    # M3: the Mailer Mailbox an inbound message was received through. Set ONLY
    # by mailbox-bound IMAP polling (mail/mailbox_inbound.py), from the polled
    # Mailbox row -- never from message content. NULL for every outbound row
    # and for inbound rows that arrived through the webhook or the legacy
    # deployment-global IMAP poll. It scopes the inbound dedupe identity below.
    mailbox_id = Column(Integer, ForeignKey("mailboxes.id", ondelete="RESTRICT"), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow)

    # Opaque public handle (see new_reply_reference). Generated once, at INSERT, and never rewritten, so a
    # retry or re-read of the same row always shows the same token. Nullable at the database level ON PURPOSE:
    # migration 012 backfills every existing row, but code from before this change (a rollback, or a worker
    # that has not been restarted yet) inserts rows without it and must not fail; such a row simply has no
    # public handle until the next 012 run backfills it. Uniqueness is a plain unique index (NULLs never
    # collide on PostgreSQL or SQLite). Core ``insert(Message)`` paths set it explicitly as well.
    reply_reference = Column(String(32), nullable=True, default=new_reply_reference)

    contact = relationship("Contact", back_populates="messages")

    __table_args__ = (
        # Message-ID dedupe identity (M3 split of the former global constraint).
        #
        # Rows WITHOUT a mailbox (all outbound rows -- our own Message-IDs are
        # globally unique by construction -- plus webhook / legacy-global-IMAP
        # inbound) keep exactly the original rule: one row per non-NULL
        # Message-ID, globally.
        #
        # Rows WITH a mailbox (mailbox-bound IMAP inbound) are unique per
        # (mailbox_id, Message-ID): the same RFC Message-ID can legitimately
        # arrive in two organizations' mailboxes (shared CC, mailing list) and
        # each organization must get its own record. A global rule made the
        # second organization's copy look like a "duplicate" of the first's.
        #
        # NULL Message-IDs remain unconstrained in both indexes. The
        # application-level check-then-insert in mail/reply_handler_v2.py is
        # kept for the common path; these indexes are what make it correct under
        # concurrency (see tests/test_postgresql_concurrency.py and
        # tests/test_m3_inbound_postgres.py). Partial indexes (not named
        # UNIQUE CONSTRAINTs) so one DDL works on PostgreSQL and SQLite.
        Index(
            "uq_messages_message_id_no_mailbox",
            "message_id_header",
            unique=True,
            postgresql_where=text("mailbox_id IS NULL"),
            sqlite_where=text("mailbox_id IS NULL"),
        ),
        Index(
            "uq_messages_mailbox_message_id",
            "mailbox_id",
            "message_id_header",
            unique=True,
            postgresql_where=text("mailbox_id IS NOT NULL"),
            sqlite_where=text("mailbox_id IS NOT NULL"),
        ),
        # PR4: serves ONE query -- the operator-only inbound aggregate over a recent window
        # (mailer_agent/operator_metrics.py::inbound_metrics: WHERE direction = 'inbound' AND
        # created_at >= :since). Partial on inbound rows so it stays small (outbound rows, the
        # majority, are never indexed) and adds a write only to inbound inserts. Not unique and
        # not used by any request path. Created for existing databases by migration 010.
        Index(
            "ix_messages_inbound_created_at",
            "created_at",
            postgresql_where=text("direction = 'inbound'"),
            sqlite_where=text("direction = 'inbound'"),
        ),
        # Opaque reply handle: unique, looked up by equality on every reply request. Created for existing
        # databases by migration 012 (after it backfills the column).
        Index("uq_messages_reply_reference", "reply_reference", unique=True),
    )


class ExternalDispatchState(str, enum.Enum):
    """
    Dispatch-operation lifecycle for an externally-authorized outreach
    action (Phase C / LeadBoost integration).

    Deliberately NOT the same state machine as LeadBoost's own
    OutreachAction (PENDING_REVIEW -> APPROVED -> DISPATCHING ->
    SUBMITTED): that lifecycle is LeadBoost's authorization/handoff
    record; this one tracks what *this* service has done with the
    dispatch operation after accepting it. See models.py module docstring
    context and mailer_agent/api/integrations.py.

    QUEUED  -- durably accepted (the endpoint's final commit -- Message
      + ExternalDispatch created together, see
      api/integrations.py::create_leadboost_outreach_action's
      transaction-boundary docstring for why that's the one commit that
      matters -- has landed), not yet claimed by a worker. This is what
      accepted=true maps to over the wire -- see api/integrations.py's
      response contract.
    SENDING -- claimed by a worker, about to/currently calling
      send_email(). Short-lived. Introduced in a later phase (worker
      claiming); not set anywhere in this batch. See
      resolve_expired_sending_lease() below for the one, corrected rule
      governing what an *expired* SENDING lease recovers to -- read that
      docstring before implementing any lease-recovery code (C6+).
    SENT / FAILED / UNKNOWN -- terminal-ish outcomes mirroring
      mail/sender.py's SendOutcome exactly. Introduced in a later phase;
      not set anywhere in this batch.

    This batch (C2-C4) only ever creates rows in QUEUED. The other four
    values are defined now so the column's full vocabulary is fixed
    before any code writes to it, but nothing in this batch sets them.

    GENERATING (M2-B) -- a generation worker owns a generated-outreach row
      whose Message does not exist yet (message_id IS NULL). Internal only:
      the reconciliation read reports it as "queued". Generation has no
      external side effect, so -- unlike SENDING -- an expired GENERATING
      lease is safely returned to QUEUED (see
      work_claiming.recover_expired_generations). A QUEUED row with
      message_id NULL is "accepted, awaiting generation"; the send claim
      only ever takes QUEUED rows whose message_id is set.
    """
    QUEUED = "queued"
    GENERATING = "generating"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


def resolve_expired_sending_lease() -> "ExternalDispatchState":
    """
    DESIGN CORRECTION, made explicit before any worker/lease-recovery
    code exists (C6+ -- this function is not called by anything in
    Batch 1; it exists to pin the rule down before that code is written).

    An earlier design draft (the Phase B.2 reconciliation report's
    crash/failure matrix) treated two situations differently:

      (a) worker crashes after Transaction B commits but BEFORE
          send_email() is ever called -- described there as safe to
          requeue and retry, since "SMTP never happened";
      (b) worker crashes DURING or AFTER the SMTP call -- correctly
          described there (and in the governing project brief) as
          UNKNOWN, never auto-resent.

    That distinction is real in principle but is NOT SAFE to act on,
    because this schema records no signal that distinguishes them.
    ExternalDispatch.claimed_at records only when a worker claimed the
    row, not whether that worker had reached send_email() yet. A
    lease-expiry recovery sweep sees the identical row shape --
    state=SENDING, claimed_at older than CLAIM_LEASE_SECONDS -- whether
    the crash happened in case (a), mid-SMTP, or just after SMTP
    accepted but before Transaction C committed. Since the recovery code
    cannot tell these apart after the fact, it must not act as if it
    could -- and must not default to the most optimistic of the
    indistinguishable possibilities.

    CORRECTED RULE, and the only one any future worker/lease-recovery
    implementation may follow: an expired SENDING lease ALWAYS resolves
    to UNKNOWN. It is never automatically moved back to QUEUED and never
    automatically resent. This makes case (a) and case (b) above resolve
    identically, which is the only choice consistent with not being able
    to tell them apart.

    The only way an expired SENDING row could safely become QUEUED again
    is via a *separate*, positively persisted signal that specifically
    proves send_email() was never invoked for that claim -- e.g. a
    durably-recorded "SMTP call started" marker, written in its own
    small transaction distinct from the claim itself, checked before
    SMTP is attempted. No such signal exists in this schema today. This
    function takes no arguments and has no conditional branch because
    that signal does not exist yet; if a later phase deliberately adds
    one, this function's contract -- and this file's tests -- are the
    place to update, not a new ad hoc check inside the worker loop.

    This mirrors mail/sender.py's own AmbiguousSendError philosophy
    (never silently retried) and the "never blind auto-resend" language
    the project brief already applies to the mid-SMTP and
    post-SMTP-pre-Transaction-C cases -- this function removes the one
    case that had drifted from that principle, so all three now agree.
    """
    return ExternalDispatchState.UNKNOWN


class ExternalDispatch(Base):
    """
    One durable record of a single externally-authorized outreach
    dispatch operation (Phase C / LeadBoost integration, and designed to
    be reusable for any future external caller, not LeadBoost-specific
    at the schema level).

    This is the actual idempotency backstop for the integration endpoint
    (mailer_agent/api/integrations.py): UNIQUE(organization_id,
    idempotency_key) is enforced by the database, not only checked in
    application code, so two concurrent identical requests can only ever
    produce one row (the loser's IntegrityError is caught, and the
    winner's row is re-read and returned -- see
    api/integrations.py::create_leadboost_outreach_action).

    request_fingerprint lets a replay of the same idempotency_key be
    distinguished from caller misuse: same key + same fingerprint is a
    safe replay (return the existing operation, no new send); same key +
    different fingerprint is rejected (409, zero mutation) rather than
    silently overwriting what the key already refers to.

    organization_id is populated *only* from the authenticated
    X-API-Key -> org_id resolution (api/deps.py::get_current_org_id),
    never from the request body -- see api/integrations.py. Every query
    against this table must filter on organization_id in the query
    predicate itself (WHERE organization_id = ... AND ...), never
    fetch-then-check, so a key from one org can never read or mutate
    another org's dispatch even if it somehow guesses a valid
    idempotency_key or public_reference.

    external_action_id is the caller's own correlation id (e.g.
    LeadBoost's OutreachAction.id) -- stored for observability only and
    NEVER used to resolve a tenant, campaign, or contact. Confusing
    "correlation data" with "authorization data" is exactly the bug this
    column's docstring exists to prevent (see the inbound webhook's
    to_email-based tenant resolution in api/webhooks.py for what that
    mistake actually looks like in this codebase today -- a separate,
    already-tracked gap this table's design deliberately does not
    repeat).

    public_reference is the opaque identifier actually handed back to
    the caller (as mailing_agent_reference) and used for the
    reconciliation lookup -- never the internal sequential `id`, so the
    external API surface doesn't leak enumerable row counts across the
    tenant boundary.

    claimed_by / claimed_at follow the exact same lease shape as
    Contact's own work-claiming fields (see followup/work_claiming.py) --
    introduced now so the column exists, but not written to by anything
    in this batch; a later phase generalizes work_claiming.py's claim
    logic to this table.
    """

    __tablename__ = "external_dispatches"
    __table_args__ = (
        # THE idempotency guarantee (see class docstring) -- DB-enforced,
        # not an in-memory or best-effort application check.
        UniqueConstraint(
            "organization_id", "idempotency_key",
            name="uq_external_dispatches_org_idempotency_key",
        ),
        # public_reference is handed out externally as the sole
        # reconciliation handle -- it must never collide across
        # organizations either.
        UniqueConstraint(
            "public_reference", name="uq_external_dispatches_public_reference",
        ),
        # Supports the expired-lease sweep a later phase adds (same
        # reasoning as idx_contacts_claimed_by in
        # migrations/002_work_claiming.py).
        Index("ix_external_dispatches_claimed_by", "claimed_by"),
        # Covers the work-claim query a later phase adds: something like
        # WHERE state = 'queued' OR (state = 'sending' AND claimed_at < cutoff).
        Index("ix_external_dispatches_state_claimed_at", "state", "claimed_at"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Tenancy -- see class docstring. NOT NULL: every dispatch belongs to
    # exactly one authenticated organization from the moment it's created.
    organization_id = Column(String, nullable=False, index=True)

    idempotency_key = Column(String, nullable=False)

    # Correlation only -- see class docstring. Never used for tenant/
    # campaign/contact resolution.
    external_action_id = Column(String, nullable=True)
    correlation_id = Column(String, nullable=True)

    # RESTRICT, not CASCADE (Batch 1.1 correction -- see
    # tests/test_external_dispatch_fk_durability.py). Contact.campaign_id
    # and Message.contact_id above both cascade, and this file does not
    # touch that pre-existing behavior: no code path anywhere in this
    # repository currently deletes a Campaign, Contact, or Message (no
    # DELETE endpoint, no db.delete() call exists today), so changing
    # those established relationships isn't warranted by anything found
    # in this review. But ExternalDispatch is different in kind from an
    # ordinary child row: it is the durable idempotency/reconciliation
    # record itself (see class docstring), and this codebase's schema
    # cannot rule out a parent row being deleted by something outside
    # this application's own request handlers -- a future admin tool, a
    # GDPR/data-deletion process, or direct operator SQL. If that ever
    # happens to a Campaign/Contact/Message that still has an
    # ExternalDispatch pointing at it, CASCADE would silently delete the
    # dispatch record along with it. That specifically breaks the
    # UNIQUE(organization_id, idempotency_key) guarantee this whole
    # design depends on: with the row gone, a retried request bearing
    # the same idempotency_key would find nothing and create a brand new
    # dispatch (and could trigger a genuinely duplicate send), silently
    # violating "no duplicate send for a replayed request" -- and would
    # separately erase reconciliation history for an operation LeadBoost
    # may still be asking about. RESTRICT here makes any such deletion
    # attempt fail loudly (an integrity error) instead of silently
    # discarding dispatch history, for any of the three FKs below,
    # whether the deletion is attempted directly against that row or
    # arrives indirectly via the Contact->Campaign / Message->Contact
    # cascade chain above (a cascading delete is one atomic operation;
    # if any RESTRICT anywhere in that chain would be violated, the
    # whole delete fails, not just the one row this FK is on).
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False)
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False)
    # Exact-message route: set in the same final commit that creates this row
    # (see api/integrations.py::create_leadboost_outreach_action).
    # M2-B generated-outreach route: NULL from acceptance until the generation
    # worker creates the ONE Message and sets this in a single fenced commit
    # (state GENERATING -> QUEUED). Nothing sends a row whose message_id is
    # NULL. NULL on a FAILED row means generation failed and no Message exists.
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="RESTRICT"), nullable=True)

    # M2-A: the Mailer-owned sending identity this dispatch executes through.
    # Resolved from the authenticated organization at acceptance (exactly one
    # ACTIVE mailbox) and re-validated by the worker at claim time (tenant,
    # ACTIVE, decryptable) -- never trusted from the caller. NULL only on rows
    # accepted before M2-A; the worker fails those pre-SMTP as no_mailbox.
    # Credentials are NOT copied here: they are decrypted per operation from
    # the Mailbox row. RESTRICT: a mailbox with dispatch history cannot be
    # deleted out from under it.
    mailbox_id = Column(Integer, ForeignKey("mailboxes.id", ondelete="RESTRICT"), nullable=True)

    # SHA-256 hex digest over a canonical JSON serialization of
    # {external_action_id, recipient_email, recipient_name, subject,
    # body} -- see api/integrations.py::_compute_request_fingerprint.
    request_fingerprint = Column(String, nullable=False)

    # C9.2: immutable per-dispatch grounding snapshot. NULL for dispatches
    # created by the exact-message route (POST /outreach-actions), whose
    # grounding inputs are the integration Campaign/Contact fields exactly
    # as before. For a dispatch created by POST /outreach-requests this
    # holds the grounding material THIS dispatch's Message was generated
    # and validated against:
    #     {"version": 2,
    #      "value_prop": str,                    # request's value_proposition
    #      "context_notes": str | None,          # request's recipient_facts
    #      "conversation_transcript": str | None,
    #      "recipient": {"name", "title", "company"}}   # request-local (M2-B)
    # (version 1, pre-M2-B, had no "recipient": its Message already existed.)
    # Written once, in the commit that accepts this row (M2-B: before any
    # Message exists, so it is also the durable generation input), and never
    # updated (see _forbid_grounding_context_update below).
    # The worker grounds against THIS value, never against the shared
    # integration Campaign/Contact rows, which other requests may touch:
    # request A's message must never be re-validated against request B's
    # context. Not a place for sender/SMTP/mailbox data and not exposed
    # through any API.
    grounding_context = Column(JSON, nullable=True)

    # Opaque external handle -- see class docstring. uuid4 hex, generated
    # at creation time, never the internal sequential `id`.
    public_reference = Column(String, nullable=False)

    # Not index=True here -- ix_external_dispatches_state_claimed_at
    # below already covers this column: state is the LEADING column of
    # that composite index, so any state-only query is already served by
    # it via the standard leftmost-prefix rule (Batch 1.1 cleanup --
    # index=True here was redundant from the start, since the composite
    # index was always going to exist for the worker claim query).
    state = Column(String, nullable=False, default=ExternalDispatchState.QUEUED.value)

    # Not index=True here -- ix_external_dispatches_claimed_by above
    # already covers this column; index=True would create a duplicate.
    claimed_by = Column(String, nullable=True)
    claimed_at = Column(DateTime, nullable=True)

    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    campaign = relationship("Campaign")
    contact = relationship("Contact")
    message = relationship("Message")


def campaign_is_integration_managed(campaign: "Campaign | None") -> bool:
    """
    True for a Campaign that is the structural home of an external
    integration's work (integration_source is non-NULL, today only
    "leadboost"). Authorization for everything sent under such a campaign
    belongs to the integration (LeadBoost's OutreachAction), not to Mailer's
    native campaign automation, so every native outbound path (campaign
    start, initial/follow-up schedulers, force-followup, manual approval,
    automatic reply) must refuse it. Deliberately "is not None" rather than
    "== 'leadboost'": any future integration_source is equally not a native
    campaign, and the safe default for an unknown source is to refuse.
    """
    return campaign is not None and campaign.integration_source is not None


def contact_is_integration_managed(contact: "Contact | None") -> bool:
    """See campaign_is_integration_managed. Reads contact.campaign (lazy)."""
    return contact is not None and campaign_is_integration_managed(contact.campaign)


@event.listens_for(ExternalDispatch, "before_update")
def _forbid_grounding_context_update(mapper, connection, target) -> None:
    """
    grounding_context is write-once. Every legitimate update to an
    ExternalDispatch (claiming, finishing, annotating) leaves it untouched;
    an ORM update that changes it is a bug, so fail loudly instead of
    letting a later request rewrite what an accepted message is grounded
    against. (Core/bulk UPDATE statements bypass ORM events; nothing in this
    code base issues one against this column.)
    """
    if sa_inspect(target).attrs.grounding_context.history.has_changes():
        raise ValueError(
            "ExternalDispatch.grounding_context is immutable once the "
            "dispatch has been accepted"
        )


class SuppressionEntry(Base):
    __tablename__ = "suppression_list"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, nullable=False, index=True)
    # Org-scoped: an unsubscribe in org "acme" should not suppress the same
    # address in org "leadboost" unless they share mailboxes.
    # The unique constraint is therefore on (email, organization_id).
    organization_id = Column(String, nullable=True, index=True)
    reason = Column(String, nullable=True)  # unsubscribed / bounced / manual
    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("email", "organization_id", name="uq_suppression_email_org"),
    )


class MailboxStatus(str, enum.Enum):
    ACTIVE = "active"      # operator-enabled; says nothing about credential validity
    DISABLED = "disabled"


def _new_public_reference() -> str:
    return uuid.uuid4().hex


class Mailbox(Base):
    """
    A Mailer-owned sending identity (M1). Organization-scoped, with SMTP and
    optional IMAP credentials stored only as Fernet ciphertext (see
    mailer_agent/mailbox_secrets.py). Nothing in the send/receive paths reads
    this table yet; M2/M3 wire it in.

    organization_id comes only from the authenticated API key. public_reference
    is the only identifier exposed externally. Uniqueness is per organization:
    two organizations may register the same address (M3 must decide how that
    interacts with inbound routing -- see migrations/README.md).
    """
    __tablename__ = "mailboxes"

    id = Column(Integer, primary_key=True)
    public_reference = Column(String, nullable=False, default=_new_public_reference)
    organization_id = Column(String, nullable=False)
    email_address = Column(String, nullable=False)  # trimmed + lowercased before persistence

    smtp_host = Column(String, nullable=False)
    smtp_port = Column(Integer, nullable=False)
    smtp_use_tls = Column(Boolean, nullable=False)
    smtp_username = Column(String, nullable=False)
    smtp_password_enc = Column(Text, nullable=False)

    # IMAP is all-or-none (enforced in the request schema).
    imap_host = Column(String, nullable=True)
    imap_port = Column(Integer, nullable=True)
    imap_username = Column(String, nullable=True)
    imap_password_enc = Column(Text, nullable=True)

    status = Column(String, nullable=False, default=MailboxStatus.ACTIVE.value)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("public_reference", name="uq_mailboxes_public_reference"),
        UniqueConstraint("organization_id", "email_address", name="uq_mailboxes_org_email"),
    )

    def __repr__(self) -> str:
        return f"<Mailbox {self.public_reference}>"


class UnmatchedInboundMessage(Base):
    """
    Durable retention of mailbox-polled inbound mail that could not be attached to exactly one
    contact (reason ``no_match`` or ``ambiguous``).

    Why a separate table: ``messages.contact_id`` is NOT NULL, and an unmatched or ambiguous message
    must never be attached to a contact it may not belong to. Nothing in the conversation, transcript
    or classification paths reads this table -- it is an operator-facing holding area only.

    ``organization_id`` and ``mailbox_id`` come from the polled Mailbox row, never from the message.
    ``message_id_header`` is the same dedupe identity Message rows use (the RFC Message-ID, or
    ``synthetic:<sha256>``), so a re-offered message converges on one row.

    The row is written in the same transaction that precedes the IMAP \\Seen flag
    (mail/mailbox_inbound.py::_persist_inbound), which is what makes that flag safe to set.
    ``resolved_at`` is set if the same message is later attached to a contact (an operator marks it
    Unseen again after fixing the cause); the retained copy is then kept for audit, not deleted.
    """
    __tablename__ = "unmatched_inbound_messages"

    id = Column(Integer, primary_key=True)
    mailbox_id = Column(Integer, ForeignKey("mailboxes.id", ondelete="RESTRICT"), nullable=False)
    organization_id = Column(String, nullable=False)
    message_id_header = Column(String, nullable=False)
    reason = Column(String, nullable=False)  # "no_match" | "ambiguous"
    from_email = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    body = Column(Text, nullable=False)
    in_reply_to_header = Column(String, nullable=True)
    references_header = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("mailbox_id", "message_id_header", name="uq_unmatched_inbound_mailbox_message_id"),
    )

    def __repr__(self) -> str:
        return f"<UnmatchedInboundMessage {self.id} mailbox={self.mailbox_id} {self.reason}>"


# ---------------------------------------------------------------------------
# PR4: worker runtime status (operator observability)
# ---------------------------------------------------------------------------
# Two deliberately small tables. They exist because the HTTP API and the scheduler
# (worker.py) are separate processes: nothing in-process in the worker can be read by the API,
# and no existing table records "this process is alive" (Contact / ExternalDispatch
# claimed_by/claimed_at exist only while work is claimed, and say nothing about an idle worker).
#
# They hold NO business data: no tenant, contact, address, Message-ID, idempotency key,
# message content, credential or exception text -- only an opaque random process id, a role,
# timestamps, fixed outcome labels and whitelisted integer counters
# (mailer_agent/worker_status.py enforces the whitelist at write time).
#
# Growth is bounded: one row per scheduler PROCESS (a new random id each start) and at most
# one row per (process, job kind). Rows of processes whose last heartbeat is older than
# WORKER_RUNTIME_RETENTION_HOURS are deleted by a running scheduler process. There is no
# event log.

class WorkerRuntimeStatus(Base):
    """One scheduler-bearing process (worker.py, or an API with an in-process scheduler)."""

    __tablename__ = "worker_runtime_status"
    __table_args__ = (
        # The process identity. Random per start, so two workers on one host -- or two with
        # the same WORKER_ID -- are still two rows.
        UniqueConstraint("instance_id", name="uq_worker_runtime_status_instance_id"),
        # Retention sweep and the operator listing both order/filter by recency.
        Index("ix_worker_runtime_status_heartbeat_at", "heartbeat_at"),
    )

    id = Column(Integer, primary_key=True)
    instance_id = Column(String(64), nullable=False)      # uuid4 hex; never a hostname or WORKER_ID
    role = Column(String(32), nullable=False)             # "worker" | "api_embedded" | "scheduler"
    started_at = Column(DateTime(timezone=True), nullable=False)
    heartbeat_at = Column(DateTime(timezone=True), nullable=False)
    stopped_at = Column(DateTime(timezone=True), nullable=True)   # set only by a graceful stop


class WorkerCycleStatus(Base):
    """Latest outcome of one scheduled job kind, for one process. Overwritten, never appended."""

    __tablename__ = "worker_cycle_status"
    __table_args__ = (
        UniqueConstraint("instance_id", "kind", name="uq_worker_cycle_status_instance_kind"),
    )

    id = Column(Integer, primary_key=True)
    instance_id = Column(String(64), nullable=False)      # matches WorkerRuntimeStatus.instance_id
    kind = Column(String(32), nullable=False)             # fixed vocabulary, see worker_status.CYCLE_KINDS
    last_cycle_at = Column(DateTime(timezone=True), nullable=False)
    # completed | degraded | failed -- see worker_status for exactly what each one asserts.
    last_outcome = Column(String(16), nullable=False)
    last_completed_at = Column(DateTime(timezone=True), nullable=True)
    consecutive_unhealthy_cycles = Column(Integer, nullable=False, default=0)
    cycles_recorded = Column(Integer, nullable=False, default=0)
    last_error_type = Column(String(64), nullable=True)   # exception CLASS name only, never its text
    last_counts = Column(JSON, nullable=True)             # whitelisted ints, last cycle
    total_counts = Column(JSON, nullable=True)            # whitelisted ints, since this process started
