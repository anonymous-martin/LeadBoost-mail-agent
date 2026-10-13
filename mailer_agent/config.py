"""
Central configuration for the Mailer Agent.

Every tunable value (SMTP/IMAP creds, LLM model/key, poll intervals,
default follow-up cadence, safety toggles) lives here as an environment
variable with a sane default. No module outside this file should read
os.environ directly -- that is what makes the rest of the codebase free
of hardcoded behavior: change behavior by changing env vars / API
payloads, not by editing source.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database -----------------------------------------------------
    database_url: str = "sqlite:///./mailer_agent.db"

    # --- LLM ------------------------------------------------------------
    groq_api_key: str = ""
    # llama-3.3-70b-versatile was deprecated by Groq in June 2026 (see
    # .env.example) -- this default must track whatever .env.example
    # currently recommends, since anyone who doesn't set LLM_MODEL
    # explicitly gets THIS value, not the .env.example comment. If you
    # change the recommendation in .env.example, change it here too.
    llm_model: str = "openai/gpt-oss-120b"
    llm_temperature: float = 0.4
    llm_max_tokens: int = 500
    # Comma-separated, tried in order if llm_model fails with an
    # eligible-for-failover error (rate limit, timeout, provider 5xx,
    # model-not-found on a fallback, or malformed output after
    # strict+lenient parsing both failed on the primary -- see
    # llm/provider_v2.py's _FAILOVER_ELIGIBLE_TRANSIENT / ModelUnavailableError).
    # NOT tried for authentication failures or genuine request/schema
    # validation errors -- see the same module's routing logic and its
    # docstring for why.
    #
    # Default is openai/gpt-oss-20b only -- both gpt-oss models are
    # Groq PRODUCTION-tier (console.groq.com/docs/models). qwen/qwen3.8-27b
    # is currently a Groq PREVIEW model: Groq's own docs say preview
    # models are for evaluation and can be discontinued at short notice,
    # so it is deliberately NOT in the default production fallback chain
    # even though it has the same JSON Schema Mode + reasoning_effort
    # capabilities as the gpt-oss models (see MODEL_CAPABILITIES in
    # llm/provider_v2.py) and remains a fine explicit opt-in:
    # LLM_FALLBACK_MODELS=openai/gpt-oss-20b,qwen/qwen3.8-27b
    llm_fallback_models: str = "openai/gpt-oss-20b"
    # GPT-OSS models reject "none" outright (400) -- their valid values
    # are low/medium/high, defaulting server-side to "medium" if omitted.
    # qwen/qwen3.8-27b additionally supports low/medium/high (default
    # "none"). Only ever sent to a model whose capability entry in
    # llm/provider_v2.py's MODEL_CAPABILITIES says it supports this
    # value -- never sent blindly. See /docs/api-reference.
    llm_reasoning_effort: str = "low"

    # --- Outbound mail (SMTP) -------------------------------------------
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    # --- Inbound mail (IMAP, for reply detection) ------------------------
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_poll_seconds: int = 120
    # M3: socket timeout for a mailbox-bound IMAP connection, so one hung server
    # cannot stall every other mailbox's poll. Applies to the per-Mailbox path only.
    imap_timeout_seconds: int = 30

    # --- Follow-up scheduler ---------------------------------------------
    followup_poll_seconds: int = 300
    # Used only if a campaign is created without an explicit cadence.
    # Campaigns should normally set their own follow_up_days_after_send.
    default_followup_days: list[int] = [3, 7, 14]
    # When running as a single process (local dev, or a single Render Web
    # Service), the scheduler starts in-process via the FastAPI lifespan.
    # In production with multiple gunicorn workers, set this False and run
    # `python worker.py` as a separate process (a Render Background Worker)
    # instead -- otherwise every worker process would start its own
    # scheduler and you'd get duplicate sends/duplicate reply polls.
    run_scheduler_in_process: bool = True

    # --- Safety toggles ----------------------------------------------------
    # When False, generated sends/replies are written to the DB as
    # status="draft" instead of actually being emailed -- lets you
    # inspect agent output before it touches a real inbox.
    live_sending_enabled: bool = False
    # When False, incoming replies are classified and a reply is drafted,
    # but never auto-sent -- someone has to call POST /threads/{id}/approve.
    auto_reply_enabled: bool = False
    # Hard ceiling regardless of campaign/contact count, per poll cycle.
    max_sends_per_cycle: int = 20
    # Real prospects landing seconds apart from the same sender reads as
    # bot activity (and hammers your SMTP/IMAP rate limits). This is the
    # base gap between consecutive sends within one dispatch batch;
    # send_jitter_seconds adds a random 0-N second variation on top so
    # the cadence doesn't look mechanically fixed either.
    send_delay_seconds: float = 8.0
    send_jitter_seconds: float = 7.0

    # --- API / Multi-tenancy --------------------------------------------
    # Single legacy key (maps to org "default"). For multi-tenant use,
    # prefer ORG_KEY_MAP (JSON) or individual ORG_KEYS_<org_id>=<key> vars
    # -- see mailer_agent/api/deps.py for the full resolution order.
    api_key: str = ""  # if set, accepted as `X-API-Key` (org "default") on all routes
    # C15.1: authentication fails CLOSED. With no keys at all (API_KEY, ORG_KEY_MAP and ORG_KEYS_*
    # all empty) the API refuses to start and every protected route answers 503. This is the ONLY
    # opt-out: LOCAL DEVELOPMENT ONLY, ignored when any key is configured, refused at startup
    # together with LIVE_SENDING_ENABLED=true, and never honoured by the integration or mailbox
    # routes. Default off; never set it on a deployed service.
    allow_insecure_no_auth: bool = False
    # PR2: the interactive API documentation (/docs, /redoc and /openapi.json) is OFF by default so a
    # deployed API does not publish its full route inventory. Setting API_DOCS_ENABLED=true is a
    # deliberate operator choice: when enabled, those three routes are served WITHOUT an API key
    # (they sit outside the authenticated routers). Read once at API start-up; the worker ignores it.
    api_docs_enabled: bool = False
    # PR3: credential for the PROCESS-WIDE operator routes (/system/config, /system/metrics/llm,
    # /system/integration/validate), presented as the `X-System-Operator-Key` header. It is NOT a
    # tenant key: no value of API_KEY / ORG_KEY_MAP / ORG_KEYS_* grants operator access, and the API
    # refuses to start if this value equals one of them. Empty (the default) means the operator routes
    # answer 503 for everyone (fail closed). API service only -- the worker never reads it. Never log it.
    system_operator_api_key: SecretStr = SecretStr("")

    # --- Mailbox credential encryption (M1) --------------------------------
    # Fernet key (urlsafe-base64, 32 bytes) used to encrypt mailbox SMTP/IMAP
    # passwords at rest. Deployment configuration only: never stored in the
    # database, never auto-generated. Unset/invalid => mailbox credential
    # operations fail closed (503); the rest of the app is unaffected.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    mailbox_encryption_key: SecretStr = SecretStr("")

    # --- LeadBoost integration (Phase C) ----------------------------------
    # KNOWN OPEN ITEM, recorded explicitly rather than guessed around (see
    # Batch 1 report): the LeadBoost integration's request contract
    # (mailer_agent/api/integrations.py) deliberately carries no `sender.*`
    # block -- LeadBoost's own OutreachAction already stopped sending SMTP
    # credentials over the wire, and this integration goes further and
    # sends no sender identity at all, per request. But Campaign.sender_name
    # / sender_org / sender_email are NOT NULL columns (see models.py), and
    # the one, fixed, per-organization "leadboost" integration Campaign
    # (get-or-created by api/integrations.py) still has to satisfy that
    # constraint at creation time -- there is no way around this without
    # either (a) making those columns nullable (a real schema change to an
    # existing table used by every other campaign, explicitly out of scope
    # for this batch) or (b) putting a sender.* block back in the request
    # body (explicitly rejected by the brief this batch was built against).
    #
    # The smallest valid solution that adds no new architecture and
    # changes no existing table's nullability: a fixed, deployment-level
    # sender identity for the LeadBoost integration specifically,
    # configured here exactly like every other tunable value in this file
    # (SMTP_HOST, IMAP_HOST, etc.) -- NOT a per-organization value, and
    # explicitly flagged as such. This mirrors the SMTP settings' own
    # pre-existing shape: this codebase already sends every ordinary,
    # non-integration campaign through one deployment-wide SMTP identity
    # (smtp_username/smtp_password above) regardless of which
    # organization's campaign it is, so a single deployment-wide sender
    # identity for the one LeadBoost-integration campaign per org is not a
    # new kind of limitation, just the same existing one applied to a new
    # campaign.
    #
    # Fails closed (see api/integrations.py's get-or-create): if
    # leadboost_integration_sender_email is empty, the *first* LeadBoost
    # dispatch for any organization returns 503 rather than silently
    # inserting a placeholder/garbage sender identity into a real Campaign
    # row. Once any organization's integration Campaign has been created,
    # subsequent requests for that org reuse the existing row and do not
    # re-check this setting.
    #
    # Real per-organization sender identity (e.g. derived from LeadBoost's
    # own verified EmailAccount, passed once at credential-provisioning
    # time rather than per-request) is an explicit open item for a later
    # phase (see the Batch 1 report's "SENDER IDENTITY DECISION" section),
    # not solved here.
    #
    # SCOPE OF THIS DECISION (Batch 1.1, stated explicitly per review):
    # one deployment-wide sender identity is acceptable for a controlled
    # staging environment, or for the current deployment topology where
    # one Mailer Agent instance serves a small, known set of LeadBoost
    # organizations under one operator's control. It is NOT a complete
    # per-organization sender architecture, and must not be treated as
    # production-ready for arbitrary multi-customer use: every
    # organization's LeadBoost-integration mail would go out under the
    # same From: identity, which is unacceptable once distinct customers
    # need their own sending domain/reputation. Do not remove this
    # caveat by quietly expanding scope in a later batch without
    # actually building the per-org mechanism described above.
    leadboost_integration_sender_email: str = ""
    leadboost_integration_sender_name: str = "LeadBoost Outreach"
    leadboost_integration_sender_org: str = "LeadBoost"

    # --- LeadBoost async dispatch worker (Phase C6-C8) ------------------------
    # ExternalDispatch rows are processed by a DEDICATED job on its own
    # cadence. Do not reuse followup_poll_seconds (300 s): that would add up
    # to five minutes of queue latency to every accepted LeadBoost dispatch.
    external_dispatch_poll_seconds: int = 15
    # Separate, independent recovery sweep for expired SENDING leases. It is
    # its own job so it still fires while the dispatch job is blocked inside
    # a (slow) SMTP call.
    external_dispatch_recovery_poll_seconds: int = 60
    # Upper bound on dispatches processed per poll cycle. Each is claimed
    # one at a time, immediately before it is processed, so a claim never
    # sits idle behind other sends and ages towards its lease.
    external_dispatch_max_per_cycle: int = 5
    # Lease for a SENDING ExternalDispatch. DELIBERATELY separate from
    # CLAIM_LEASE_SECONDS (300 s, contact claims -- unchanged). The SMTP
    # path in mail/sender.py has NO overall deadline: timeout=30 is a
    # per-socket-operation timeout, up to 3 attempts, and a blackholed
    # connect to a multi-address host can legitimately exceed 300 s. An
    # expired SENDING lease resolves to UNKNOWN (never QUEUED, never an
    # automatic resend -- see models.resolve_expired_sending_lease), so an
    # undersized lease does not cause a duplicate, but it does discard the
    # real outcome of a still-running send. 900 s clears the measured
    # worst realistic case with margin.
    external_dispatch_lease_seconds: int = 900
    # M2-B: lease for a GENERATING row (one LLM draft, which has the provider's
    # own retry/fallback). Unlike SENDING, an expired GENERATING lease is safe
    # to return to QUEUED -- generation has no external side effect.
    external_dispatch_generation_lease_seconds: int = 300
    # On SIGTERM: how long the worker waits for in-flight dispatches to
    # finish before marking the ones it still owns UNKNOWN. Keep it under
    # the platform's shutdown grace period.
    external_dispatch_shutdown_drain_seconds: float = 25.0

    @property
    def llm_fallback_models_list(self) -> list[str]:
        """llm_fallback_models parsed into an ordered list, empty entries
        and whitespace stripped. A plain property (not a field) so it's
        always derived fresh from the current llm_fallback_models value
        rather than parsed once and risking drift."""
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
