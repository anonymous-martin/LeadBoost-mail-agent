"""
Exact-message execution boundary for externally-authorized outreach (C5).

LeadBoost hands the Mailer Agent an *already-authorized* subject and
body. For this integration path those two strings are the single source
of truth: they are persisted once (as one ``Message`` row) and must
reach ``send_email()`` unchanged. This module is the one place that
contract is expressed in code, so a future change cannot quietly route
the path through drafting, personalization, or a "helpful" fallback.

The authoritative values are ``Message.subject`` / ``Message.body``.
Nothing here keeps a second mutable copy: ``ExactSendInput`` is a frozen
value built from the persisted Message at the moment of hand-off.

What lives here
---------------
  create_authorized_message()
      Builds -- does NOT add/flush/commit -- the one Message for an
      operation. It takes no session, so it cannot create a second row:
      ONE authorized operation -> ONE Message is structural. (The name
      says "create" in the domain sense; nothing is persisted here.)

  build_exact_send_input() -> ExactSendInput
      The value a later stage (C7) passes to send_email().

  evaluate_exact_message_grounding() -> ExactGroundingDecision
      The established hard-block safety net, reusable by C7. Read-only.

Purity: no I/O of its own
-------------------------
No session is created, used or committed here, and there is no network,
SMTP or LLM call. Precisely: the functions *read attributes* of the ORM
objects they are handed; on an expired instance that read may lazy-load
through the caller's session. They never write, flush, or commit.

Allowed imports (enforced by tests/test_exact_message_boundary.py, as an
allowlist plus a fresh-interpreter runtime check):

  * ``mailer_agent.models``          -- Message/Contact/Campaign + enums
  * ``mailer_agent.llm.grounding``   -- validate_grounding (regex only)
  * ``mailer_agent.semantic_models`` -- GroundingValidation (result type)
  * stdlib: ``__future__``, ``dataclasses``

Deliberately NOT imported, even lazily -- each one loads the LLM layer
and/or SMTP (verified by importing them in a fresh interpreter):

  * ``llm.agent`` / ``draft_message``  -- message generation.
  * ``mail.sender`` / ``smtplib``      -- sending belongs to a dispatcher
    that *consumes* ExactSendInput (C7), not to this module.
  * ``memory.store``      -- ``build_conversation_context`` lives there,
    but that module imports ``llm.provider``.
  * ``followup.engine``   -- home of ``is_suppressed``, but it imports
    ``llm.agent`` and ``mail.sender``.

Subject semantics
-----------------
``subject=None`` (permitted by the request schema) is handed off as
``""`` because ``send_email(subject: str)`` requires a str. This is a
transport representation, not a new subject: at the MIME layer ``None``
and ``""`` both serialize to an empty ``Subject:`` header (verified), and
``Message.subject`` itself stays None. A subject is NEVER invented -- not
from ``campaign.sender_org`` (what the legacy approve path does), the
campaign name, a template, or an LLM.

Grounding inputs and known limitations
--------------------------------------
Same validator, same fields as the legacy approval gate, except the
transcript: see ``evaluate_exact_message_grounding``. This is NOT full
parity with the legacy gate. Also, the integration campaign has no
operator-approved proof_points, so metric/dollar/headcount/time claims
hard-block until an operator supplies grounding evidence (fail-closed).

Not done here (later stages) and what C7 will need
--------------------------------------------------
SMTP, worker claiming, lease recovery, Message-ID persistence,
reconciliation -- and:

  * Message-ID: ``send_email()`` mints its own ID internally
    (``make_msgid``) and only *returns* it. A Message-ID persisted before
    the SMTP call would therefore NOT be the one transmitted. C7 needs a
    backward-compatible optional ``message_id_header`` parameter on
    ``send_email()`` and a matching field on ExactSendInput. Neither is
    added here: nothing can consume them yet, and ``sender.py`` is out of
    scope for C5.
  * The suppression check and the ``live_sending_enabled`` gate that
    ``approve_and_send_draft`` applies (in dry-run ``send_email()`` returns
    SENT without sending). Both need an LLM-free home.

Future-stage invariant (nothing here contradicts it): an expired
ExternalDispatch SENDING lease resolves to UNKNOWN, never back to QUEUED
and never an automatic resend -- see ``models.resolve_expired_sending_lease``.
This module changes no state.
"""

from __future__ import annotations

from dataclasses import dataclass

from mailer_agent.llm.grounding import validate_grounding
from mailer_agent.models import (
    Campaign,
    Contact,
    Message,
    MessageDirection,
    MessageStatus,
    MessageType,
)
from mailer_agent.semantic_models import GroundingValidation


class ExactMessageError(ValueError):
    """The boundary refused to build a send input. Always a programming or
    wiring error (wrong/foreign/already-sent Message), never a property of
    the caller's content -- content is never rejected or rewritten here."""


# ---------------------------------------------------------------------------
# Construction: the ONE Message per operation
# ---------------------------------------------------------------------------

def create_authorized_message(
    *, contact_id: int, subject: str | None, body: str
) -> Message:
    """
    Build the single, pre-send Message snapshot for one authorized
    operation. ``subject`` and ``body`` are stored exactly as given: no
    strip, no newline/unicode normalization, no defaulting.

    Returns an unattached ``Message`` (status DRAFT). Deliberately does not
    take a Session: adding/flushing/committing stays with the caller, whose
    atomic Message+ExternalDispatch commit is what actually guards against
    duplicates (uq_external_dispatches_org_idempotency_key).
    """
    return Message(
        contact_id=contact_id,
        direction=MessageDirection.OUTBOUND.value,
        message_type=MessageType.INITIAL.value,
        subject=subject,
        body=body,
        status=MessageStatus.DRAFT.value,
    )


# ---------------------------------------------------------------------------
# Handoff shape for the (later) send stage
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExactSendInput:
    """
    Exactly what a dispatcher may pass to ``send_email()``. ``body_text``
    is ``Message.body`` verbatim. ``subject`` is ``Message.subject``
    verbatim, except that an absent subject (None) becomes ``""``:
    send_email() requires a ``str``, and an empty Subject is the faithful
    rendering of "no subject was authorized". The legacy fallback
    (``campaign.sender_org`` as a subject) is an *invention* and is never
    used on this path. ``Message.subject`` itself is left as None.

    In-Reply-To / References are carried ONLY for a message that was stored with a parent
    (a LeadBoost-authorized reply, see api/integrations_generated.py); initial outreach has
    none and its send kwargs are exactly what they were before.
    """
    to_email: str
    from_email: str
    from_name: str
    subject: str
    body_text: str
    reply_to: str | None
    in_reply_to_header: str | None = None
    references_header: str | None = None

    def as_send_email_kwargs(self) -> dict:
        """Keyword arguments for ``mail.sender.send_email`` (its names)."""
        kwargs = {
            "to_email": self.to_email,
            "from_email": self.from_email,
            "from_name": self.from_name,
            "subject": self.subject,
            "body_text": self.body_text,
            "reply_to": self.reply_to,
        }
        if self.in_reply_to_header:
            kwargs["in_reply_to_header"] = self.in_reply_to_header
            if self.references_header:
                kwargs["references_header"] = self.references_header
        return kwargs


def _assert_wired(message: Message, contact: Contact, campaign: Campaign) -> None:
    if message.direction != MessageDirection.OUTBOUND.value:
        raise ExactMessageError(f"message {message.id} is not outbound")
    if message.status != MessageStatus.DRAFT.value:
        # A message that already has an outcome must never be re-sent
        # through this boundary (sent/failed/unknown are all final here).
        raise ExactMessageError(
            f"message {message.id} is not pre-send (status={message.status})"
        )
    if message.contact_id != contact.id:
        raise ExactMessageError(
            f"message {message.id} does not belong to contact {contact.id}"
        )
    if contact.campaign_id != campaign.id:
        raise ExactMessageError(
            f"contact {contact.id} does not belong to campaign {campaign.id}"
        )


def build_exact_send_input(
    message: Message, contact: Contact, campaign: Campaign
) -> ExactSendInput:
    """Pure, read-only. Raises ExactMessageError on mis-wired/non-pre-send
    input; never alters or rejects the subject/body content itself."""
    _assert_wired(message, contact, campaign)
    return ExactSendInput(
        to_email=contact.email,
        from_email=campaign.sender_email,
        from_name=campaign.sender_name,
        subject=message.subject if message.subject is not None else "",
        body_text=message.body,
        reply_to=campaign.reply_to_email,
        in_reply_to_header=message.in_reply_to_header,
        references_header=message.references_header,
    )


# ---------------------------------------------------------------------------
# Grounding safety net (hard-block only) -- consumed by C7
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExactGroundingDecision:
    """
    ``blocked`` mirrors the legacy approval gate exactly: True iff
    ``hard_block``. ``review_required`` (pricing/availability wording) is
    surfaced but NOT blocking -- here, as on the approval endpoint,
    LeadBoost's own APPROVED state is the authorization. Callers that want
    a stricter policy can read the flag; this function does not decide it.
    ``error_message`` is ready to store on ExternalDispatch when blocked.
    """
    blocked: bool
    review_required: bool
    error_message: str | None
    validation: GroundingValidation


def evaluate_exact_message_grounding(
    message: Message,
    contact: Contact,
    campaign: Campaign,
    grounding_context: dict | None = None,
) -> ExactGroundingDecision:
    """
    ``grounding_context`` (C9.2): the ExternalDispatch.grounding_context
    snapshot, when the dispatch has one. If given it REPLACES the
    Campaign/Contact grounding sources described below (see the branch in the
    body); if None -- every exact-message dispatch -- behaviour is unchanged.

    Run the existing ``validate_grounding`` against the exact stored body
    -- same function, same source fields as api/messages.py::
    approve_and_send_draft (campaign.proof_points, contact.context_notes,
    campaign.value_prop). Read-only: never mutates or rewrites the message,
    never touches ExternalDispatch state (a later stage maps ``blocked`` to
    FAILED and skips SMTP).

    Transcript: passed as None, not built. The legacy gate builds one via
    memory.store.build_conversation_context, which would import the LLM
    provider into this path (see module docstring). Consequences:
      * For a contact with no history the result matches the legacy gate
        (its first-contact placeholder has no digits or guarantee/warranty
        text) -- covered by a parity test.
      * With prior conversation evidence the legacy gate can ground claims
        this one cannot. validate_grounding builds its approved corpus as a
        union of sources, so dropping the transcript can only shrink the
        corpus; today that means this path cannot unblock anything the
        legacy gate blocks. That is a property of the current
        implementation, locked by tests -- not a general guarantee.
      * Restoring parity would need an LLM-free conversation-context
        module: a separate refactor, deliberately not done here.

    Known consequence (measured, see tests): the integration campaign has
    no operator-approved proof_points and a placeholder value_prop, so a
    message containing a metric, dollar figure, headcount or time claim
    hard-blocks unless the operator supplies matching campaign.proof_points.
    Fail-closed by design; the message is never edited to pass.
    """
    _assert_wired(message, contact, campaign)
    if grounding_context is None:
        validation = validate_grounding(
            message.body,
            proof_points=campaign.proof_points,
            context_notes=contact.context_notes,
            conversation_transcript=None,
            value_prop=campaign.value_prop,
        )
    else:
        # C9.2: the dispatch carries its own immutable snapshot of what its
        # message was generated and validated against. Use ONLY that -- never
        # the shared Campaign/Contact fields, which another request may have
        # changed since this one was accepted. No operator proof_points exist
        # for generated outreach (the contract has none), so None.
        validation = validate_grounding(
            message.body,
            proof_points=None,
            context_notes=grounding_context.get("context_notes"),
            conversation_transcript=grounding_context.get("conversation_transcript"),
            value_prop=grounding_context.get("value_prop"),
        )
    blocked = validation.hard_block
    error_message = None
    if blocked:
        error_message = (
            "Message failed grounding safety check and was not sent: "
            f"{validation.validation_notes}. Unsupported/fabricated claims: "
            f"{validation.unsupported_claims}."
        )
    return ExactGroundingDecision(
        blocked=blocked,
        review_required=validation.review_required,
        error_message=error_message,
        validation=validation,
    )
