"""
Authentication and authorization dependencies.

Multi-tenancy model (simple, no OAuth):
- Each organization gets one API key, set as ORG_KEYS_<KEY>=<org_id> in the environment,
  or as a JSON mapping in ORG_KEY_MAP (see config.py).
- The single legacy API_KEY, if set, is ALWAYS merged into the key map as org "default"
  (it is valid on every route, including the integration routes). It is kept for
  backward compatibility only; see "Legacy API_KEY" below.
- When a key resolves to an org, the org_id is injected into the request via
  get_current_org_id(); every data-access query must filter by it.

Key-to-org resolution order (highest priority wins when the same key string repeats):
  1. ORG_KEY_MAP env var (JSON object: {"<key>": "<org_id>", ...})  <- preferred for prod
  2. Individual ORG_KEYS_<n> env vars: ORG_KEYS_acme=api_key_xxx
  3. Legacy API_KEY -> org_id "default"

FAIL CLOSED (C15.1)
-------------------
Authentication is required on every protected route unless the operator explicitly
opts out with ALLOW_INSECURE_NO_AUTH=true (local development only):

* No effective keys (nothing configured, or every source empty): protected routes answer
  503 "API authentication is not configured" -- never implicit access as org "default".
* ORG_KEY_MAP set but not a valid JSON object of non-empty string pairs: the WHOLE
  authentication configuration is rejected (recorded in _AUTH_CONFIG_ERROR, key map left
  empty). Nothing is silently ignored and the legacy key is not used as a fallback, so a
  typo can never leave another route open or half-configured. The error text never
  contains any value from the variable.
* The API refuses to START (validate_auth_configuration(), called from the lifespan) in
  either case. The per-request 503 remains as the second layer for any code path that
  does not run the lifespan.
* ALLOW_INSECURE_NO_AUTH only has an effect when there are no keys at all. It is ignored
  (with a warning) when keys are configured, and the API refuses to start when it is
  combined with LIVE_SENDING_ENABLED=true (an unauthenticated sending API).
* get_integration_org_id / get_authenticated_org_id never honour ALLOW_INSECURE_NO_AUTH:
  the integration and mailbox (credential-bearing) routes are fail-closed in every mode.

Legacy API_KEY
--------------
Effective permissions are unchanged by C15.1: when set it is accepted on every route as
organization "default" (including integration and mailbox routes). If an ORG_KEY_MAP /
ORG_KEYS_* entry also maps an organization named "default", the legacy key acts as that
tenant. Remove it from the environment once no operator tooling depends on it; whether
that is safe on a given deployment is a deployment fact this repository cannot see.

Usage in endpoints:
    @router.get("/campaigns")
    def list_campaigns(
        org_id: str = Depends(get_current_org_id),
        db: Session = Depends(get_db),
    ):
        return db.query(Campaign).filter(Campaign.organization_id == org_id).all()
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from mailer_agent.config import get_settings

logger = logging.getLogger("mailer_agent.api.deps")

settings = get_settings()


class AuthConfigurationError(RuntimeError):
    """The API's authentication configuration is missing or invalid. Raised at startup so a
    misconfigured deployment fails to start instead of serving. The message never contains a
    key, an organization id or any other value taken from the environment."""


# ---------------------------------------------------------------------------
# Build the key->org_id lookup table at import time (cheap, done once).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthConfig:
    key_map: dict[str, str]
    error: str | None = None
    legacy_key_active: bool = False
    explicit_keys_active: bool = False  # ORG_KEY_MAP and/or ORG_KEYS_* contributed at least one key


def _parse_org_key_map(raw: str) -> tuple[dict[str, str], str | None]:
    """Strictly parse ORG_KEY_MAP. Returns (mapping, error). On any problem returns ({}, error);
    the error describes the shape problem only and never echoes content."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return {}, "ORG_KEY_MAP is set but is not valid JSON"
    if not isinstance(parsed, dict):
        return {}, 'ORG_KEY_MAP must be a JSON object of the form {"<api_key>": "<org_id>"}'
    mapping: dict[str, str] = {}
    for key, org_id in parsed.items():
        valid_org = (isinstance(org_id, str) and org_id.strip()) or (
            isinstance(org_id, int) and not isinstance(org_id, bool)
        )
        if not (isinstance(key, str) and key.strip() and valid_org):
            return {}, "ORG_KEY_MAP entries must map a non-empty string key to a non-empty string organization id"
        mapping[key] = str(org_id)
    return mapping, None


def load_auth_config(environ: dict[str, str] | None = None, legacy_api_key: str | None = None) -> AuthConfig:
    """
    Build the effective {api_key: org_id} map from configuration.

    Three sources, merged in priority order (highest first):
      1. ORG_KEY_MAP - JSON object in one env var, easiest for secrets managers.
      2. ORG_KEYS_<org_id>=<key> - individual per-org vars, easier in .env files.
      3. Legacy API_KEY - single key -> org "default".

    A malformed ORG_KEY_MAP yields an EMPTY map plus an error: the configuration is rejected as a
    whole rather than partly honoured.
    """
    env = os.environ if environ is None else environ
    legacy = settings.api_key if legacy_api_key is None else legacy_api_key
    mapping: dict[str, str] = {}

    # 3. Legacy single key (lowest priority, may be overridden)
    if legacy:
        mapping[legacy] = "default"

    # 2. ORG_KEYS_<org_id>=<key> - scan environment
    explicit = False
    for env_key, env_val in env.items():
        if env_key.upper().startswith("ORG_KEYS_") and env_val.strip():
            mapping[env_val.strip()] = env_key[len("ORG_KEYS_"):].lower()
            explicit = True

    # 1. ORG_KEY_MAP JSON (highest priority, overwrites anything above)
    raw = env.get("ORG_KEY_MAP", "").strip()
    if raw:
        parsed, error = _parse_org_key_map(raw)
        if error:
            return AuthConfig(key_map={}, error=error)
        mapping.update(parsed)
        explicit = explicit or bool(parsed)

    return AuthConfig(
        key_map=mapping,
        legacy_key_active=bool(legacy) and legacy in mapping,
        explicit_keys_active=explicit,
    )


_AUTH_CONFIG: AuthConfig = load_auth_config()
# Module-level singletons, read at request time (tests and scripts/check_config.py inspect them).
_KEY_MAP: dict[str, str] = _AUTH_CONFIG.key_map
_AUTH_CONFIG_ERROR: str | None = _AUTH_CONFIG.error


def validate_auth_configuration() -> None:
    """Startup gate (called from the FastAPI lifespan). Raises AuthConfigurationError rather than
    letting the API serve with missing/invalid authentication. Never logs a secret."""
    if _AUTH_CONFIG_ERROR:
        raise AuthConfigurationError(
            f"Invalid authentication configuration: {_AUTH_CONFIG_ERROR}. Refusing to start."
        )
    if _KEY_MAP:
        if settings.allow_insecure_no_auth:
            logger.warning(
                "ALLOW_INSECURE_NO_AUTH is set but ignored: API keys are configured, so authentication "
                "is enforced. Remove ALLOW_INSECURE_NO_AUTH."
            )
        if _AUTH_CONFIG.legacy_key_active and _AUTH_CONFIG.explicit_keys_active:
            logger.warning(
                "Legacy API_KEY is active alongside ORG_KEY_MAP/ORG_KEYS_*: it is accepted on every route, "
                "including the integration and mailbox routes, as organization 'default'. Unset API_KEY "
                "once no operator tooling depends on it."
            )
        return
    if settings.allow_insecure_no_auth:
        if settings.live_sending_enabled:
            raise AuthConfigurationError(
                "ALLOW_INSECURE_NO_AUTH=true cannot be combined with LIVE_SENDING_ENABLED=true "
                "(that would be an unauthenticated API that can send email). Refusing to start."
            )
        logger.warning(
            "INSECURE: ALLOW_INSECURE_NO_AUTH=true and no API keys are configured. Every protected "
            "route is open as organization 'default'. LOCAL DEVELOPMENT ONLY - never expose this process."
        )
        return
    raise AuthConfigurationError(
        "No API keys are configured (API_KEY, ORG_KEY_MAP and ORG_KEYS_* are all unset or empty). "
        "Set at least one key, or ALLOW_INSECURE_NO_AUTH=true for local development only. Refusing to start."
    )


def _resolve_org(api_key: str) -> str | None:
    """
    Resolve an API key to its org_id.  Returns None if the key is unknown.
    Uses constant-time comparison for every candidate to prevent timing attacks.
    """
    matched_org: str | None = None
    for known_key, org_id in _KEY_MAP.items():
        # compare_digest always compares full strings - safe against timing.
        if secrets.compare_digest(api_key, known_key):
            matched_org = org_id
    return matched_org


def _not_configured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="API authentication is not configured on this deployment",
    )


def _keys_required() -> bool:
    """True: callers must present a valid key. False: the operator explicitly opted out for local
    development (no keys at all AND ALLOW_INSECURE_NO_AUTH=true). Raises 503 when authentication is
    unavailable (nothing configured, or the configuration was rejected as invalid)."""
    if _AUTH_CONFIG_ERROR:
        logger.error("Request refused: the authentication configuration is invalid; protected routes fail closed.")
        raise _not_configured()
    if _KEY_MAP:
        return True
    if settings.allow_insecure_no_auth:
        return False
    logger.error("Request refused: no API keys are configured; protected routes fail closed.")
    raise _not_configured()


# ---------------------------------------------------------------------------
# FastAPI dependency functions
# ---------------------------------------------------------------------------

def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """
    Auth check for routes that do not need the org id. 401 for a missing/unknown key, 503 when
    authentication is not configured (fail closed). Open only under the explicit
    ALLOW_INSECURE_NO_AUTH development opt-in.
    """
    if not _keys_required():
        return
    if not x_api_key or _resolve_org(x_api_key) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def get_current_org_id(x_api_key: str | None = Header(default=None)) -> str:
    """
    Resolve the calling organization from the X-API-Key header.

    Returns the org_id string (e.g. "acme", "leadboost", "default"). 503 when authentication is
    not configured; only under the explicit ALLOW_INSECURE_NO_AUTH development opt-in does it
    return "default" without a key.
    """
    if not _keys_required():
        return "default"
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )
    org_id = _resolve_org(x_api_key)
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return org_id


def get_integration_org_id(x_api_key: str | None = Header(default=None)) -> str:
    """
    Fail-closed variant of get_current_org_id for the external-integration boundary
    (POST /integrations/leadboost/outreach-requests, GET .../conversation).

    Unlike get_current_org_id it NEVER honours ALLOW_INSECURE_NO_AUTH: an unconfigured or invalid
    key map is a 503, and the tenant is always the one the presented key maps to -- never anything
    from the request body. 401 for a missing/unknown key (same bodies as get_current_org_id, so
    which keys exist is not distinguishable).
    """
    if _AUTH_CONFIG_ERROR or not _KEY_MAP:
        logger.error(
            "Integration request refused: no usable API keys are configured "
            "(API_KEY / ORG_KEY_MAP / ORG_KEYS_*); the integration boundary fails closed."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Integration authentication is not configured on this deployment",
        )
    return get_current_org_id(x_api_key)


def get_authenticated_org_id(x_api_key: str | None = Header(default=None)) -> str:
    """
    Fail-closed organization resolution for credential-bearing routes (mailboxes). Never honours
    ALLOW_INSECURE_NO_AUTH: an unconfigured or invalid key map is a 503, never an implicit
    "default" org. 401 for a missing/unknown key (same bodies as get_current_org_id).
    """
    if _AUTH_CONFIG_ERROR or not _KEY_MAP:
        logger.error("Request refused: no usable API keys are configured; credential routes fail closed.")
        raise _not_configured()
    return get_current_org_id(x_api_key)
