"""Shared-secret authorization for the demo API.

The API key lives in SSM Parameter Store and is referenced by ARN. The caller
sends the same value back in the ``Authorization`` header; this module fetches
the stored value and compares the two in constant time.

That is all this is: a single shared secret over TLS. It is not a signed
credential, there is no per-caller identity, and revoking means rotating the one
parameter. Good enough to keep a demo URL from being sprayed by strangers,
nowhere near good enough for anything real.

Fails closed. If the parameter ARN is not configured the API refuses every
request rather than quietly serving traffic with no authorization at all.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from typing import Any, Mapping

logger = logging.getLogger("c1.auth")

#: Header the caller puts the key in. A bare value or "Bearer <key>" both work.
AUTH_HEADER = "authorization"
_BEARER_SCHEME = "bearer"

#: How long a fetched key is reused before hitting Parameter Store again. Keeps
#: the hot path free of an SSM call while letting a rotation take effect without
#: a redeploy.
CACHE_TTL_SECONDS = 300

PARAMETER_ARN_VARIABLE = "API_KEY_PARAMETER_ARN"


class AuthNotConfigured(RuntimeError):
    """The API key parameter ARN is missing from the environment."""


class AuthUnavailable(RuntimeError):
    """The API key could not be read from Parameter Store."""


_cache: tuple[str, float] | None = None
_ssm_client: Any = None


def parameter_arn() -> str:
    arn = os.environ.get(PARAMETER_ARN_VARIABLE, "").strip()
    if not arn:
        raise AuthNotConfigured(
            f"{PARAMETER_ARN_VARIABLE} is not set, so no request can be authorized."
        )
    return arn


def _client() -> Any:
    global _ssm_client
    if _ssm_client is None:
        import boto3
        from botocore.config import Config

        _ssm_client = boto3.client(
            "ssm",
            config=Config(read_timeout=5, connect_timeout=3, retries={"max_attempts": 2}),
        )
    return _ssm_client


def expected_key(*, client: Any = None, now: float | None = None) -> str:
    """Return the stored API key, using the cached value when it is still fresh."""
    global _cache

    timestamp = time.monotonic() if now is None else now
    if _cache is not None and timestamp - _cache[1] < CACHE_TTL_SECONDS:
        return _cache[0]

    arn = parameter_arn()
    try:
        # GetParameter accepts a full parameter ARN in place of a name.
        response = (client or _client()).get_parameter(Name=arn, WithDecryption=True)
        value = response["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 - surfaced as 503, never as 401
        raise AuthUnavailable(f"could not read the API key parameter: {exc}") from exc

    if not value:
        raise AuthUnavailable("the API key parameter is empty")

    _cache = (value, timestamp)
    return value


def reset_cache() -> None:
    """Drop the cached key. Used by tests."""
    global _cache
    _cache = None


def extract_presented_key(headers: Mapping[str, str] | None) -> str | None:
    """Pull the key out of the Authorization header.

    API Gateway lowercases header names, but this does not rely on that.
    """
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() != AUTH_HEADER:
            continue
        stripped = (value or "").strip()
        if not stripped:
            return None
        # Split an optional "Bearer" scheme off the credential. A scheme with
        # nothing after it is not a key.
        parts = stripped.split(None, 1)
        if parts[0].lower() == _BEARER_SCHEME:
            if len(parts) == 1:
                return None
            return parts[1].strip() or None
        return stripped
    return None


def is_authorized(headers: Mapping[str, str] | None, *, client: Any = None) -> bool:
    """True when the caller presented the stored API key.

    Raises ``AuthNotConfigured`` or ``AuthUnavailable`` when the check itself
    cannot be performed. Those are not authorization failures and must not be
    reported as one.
    """
    presented = extract_presented_key(headers)
    expected = expected_key(client=client)
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
