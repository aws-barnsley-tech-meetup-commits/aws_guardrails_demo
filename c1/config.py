"""Single source of truth for the model ids and the guardrail.

Every code path that needs ``BOT_MODEL_ID``, ``BOT_MODEL_ID_PREMIUM``,
``JUDGE_MODEL_ID``, ``GUARDRAIL_ID`` or ``GUARDRAIL_VERSION`` resolves it here,
so changing a model or pointing at a different guardrail is a one-line edit in
``.env``.

There are deliberately no hardcoded model defaults. A default is worse than an
error here: it means a typo'd variable silently runs the demo on a different
model than the one in ``.env``, and you only notice from the bill or from the
bot behaving unlike the thing you were told to expect.

Where the value comes from, in precedence order:

1. A variable already in the environment. Exporting ``BOT_MODEL_ID=...`` for one
   run beats everything else, and in the deployed Lambda and AgentCore Runtime
   this is the only source: CDK injects both variables at deploy time.
2. ``.env`` at the repo root, if this is a checkout rather than a deployment
   bundle.

This module lives in both deployment bundles, where it sits flat next to
``main.py`` and ``judge.py``. Nothing here may import from elsewhere in ``c1``.
"""

from __future__ import annotations

import os
from pathlib import Path

BOT_MODEL_ID_VAR = "BOT_MODEL_ID"
BOT_MODEL_ID_PREMIUM_VAR = "BOT_MODEL_ID_PREMIUM"
JUDGE_MODEL_ID_VAR = "JUDGE_MODEL_ID"
GUARDRAIL_ID_VAR = "GUARDRAIL_ID"
GUARDRAIL_VERSION_VAR = "GUARDRAIL_VERSION"
API_KEY_VAR = "API_KEY"
API_KEY_PARAMETER_ARN_VAR = "API_KEY_PARAMETER_ARN"

#: Values accepted in the ``x-model-tier`` request header.
TIER_HIGH_VOLUME = "high_volume"
TIER_FRONTIER = "frontier"
MODEL_TIERS = (TIER_HIGH_VOLUME, TIER_FRONTIER)
DEFAULT_MODEL_TIER = TIER_HIGH_VOLUME

#: Which variable backs which tier.
_TIER_VARIABLES = {
    TIER_HIGH_VOLUME: BOT_MODEL_ID_VAR,
    TIER_FRONTIER: BOT_MODEL_ID_PREMIUM_VAR,
}

#: Values accepted in the ``x-guardrail`` request header.
GUARDRAIL_ON = "on"
GUARDRAIL_OFF = "off"
GUARDRAIL_STATES = (GUARDRAIL_ON, GUARDRAIL_OFF)
DEFAULT_GUARDRAIL_STATE = GUARDRAIL_OFF

#: A file that marks the repo root. Deployment bundles do not contain it.
_REPO_MARKER = "pyproject.toml"

#: The package directory this file lives in inside a checkout. In a bundle the
#: file is copied flat, so its parent is the bundle root and has a different
#: name. Checking this rather than just walking up for the marker matters
#: because staged bundles under .build/ are themselves inside the repo: without
#: it, testing a bundle locally would quietly read the repo .env and hide a
#: variable that was never injected.
_PACKAGE_DIR_NAME = "c1"


class ModelNotConfigured(RuntimeError):
    """A required model id variable is missing or empty."""


class GuardrailNotConfigured(RuntimeError):
    """A required guardrail variable is missing or empty."""


class UnknownModelTier(ValueError):
    """The requested model tier is not one of MODEL_TIERS."""


class ApiKeyUnavailable(RuntimeError):
    """The API key could not be resolved for a local script."""


def repo_root() -> Path | None:
    """The repo root, or None when running from a deployment bundle."""
    here = Path(__file__).resolve().parent
    if here.name != _PACKAGE_DIR_NAME:
        return None
    root = here.parent
    return root if (root / _REPO_MARKER).is_file() else None


def env_file() -> Path | None:
    """The repo ``.env``, if there is one to load."""
    root = repo_root()
    if root is None:
        return None
    candidate = root / ".env"
    return candidate if candidate.is_file() else None


def load_env_file() -> Path | None:
    """Load the repo ``.env`` into the environment. Returns the file used.

    A no-op in a deployment bundle: there is no ``.env`` there, and
    ``python-dotenv`` is not installed either, because the variables arrive
    injected. Existing environment variables are never overwritten.
    """
    found = env_file()
    if found is None:
        return None
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    load_dotenv(found)
    return found


# Import-time so that merely importing the agent or the judge is enough for the
# model ids to resolve, however the process was started.
load_env_file()


def require_model_id(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if value:
        return value

    where = env_file() or (repo_root() / ".env" if repo_root() else None)
    hint = f"Set it in {where}" if where else "Set it in the environment"
    raise ModelNotConfigured(
        f"{variable} is not set. {hint}, or export it for one run:\n"
        f"  {variable}=global.anthropic.claude-sonnet-5 <your command>\n"
        "There is no default on purpose: a default would quietly run the demo on "
        "the wrong model."
    )


def bot_model_id() -> str:
    """The model the QuickCart bot runs on by default, the high-volume tier."""
    return require_model_id(BOT_MODEL_ID_VAR)


def bot_model_id_premium() -> str:
    """The frontier-tier model the bot runs on when asked for."""
    return require_model_id(BOT_MODEL_ID_PREMIUM_VAR)


def judge_model_id() -> str:
    """The model the judge runs on."""
    return require_model_id(JUDGE_MODEL_ID_VAR)


def normalise_model_tier(value: str | None) -> str:
    """Coerce an ``x-model-tier`` header value into a known tier.

    Absent or blank means the default. An unrecognised value is an error rather
    than a silent fall back to the cheap tier: if the UI asks for the frontier
    model and quietly gets the other one, the demo tells the audience something
    untrue.
    """
    if value is None or not value.strip():
        return DEFAULT_MODEL_TIER
    tier = value.strip().lower()
    if tier not in MODEL_TIERS:
        raise UnknownModelTier(
            f"unknown model tier {value!r}. Expected one of: {', '.join(MODEL_TIERS)}"
        )
    return tier


def model_id_for_tier(tier: str) -> str:
    """The model id backing a tier, resolved from the environment."""
    try:
        variable = _TIER_VARIABLES[tier]
    except KeyError:
        raise UnknownModelTier(
            f"unknown model tier {tier!r}. Expected one of: {', '.join(MODEL_TIERS)}"
        ) from None
    return require_model_id(variable)


def normalise_guardrail_state(value: str | None) -> str:
    """Coerce an ``x-guardrail`` header value into "on" or "off".

    Anything unrecognised is an error. Quietly treating a typo as "off" would
    show the audience an unguarded answer while the UI claims it was guarded.
    """
    if value is None or not value.strip():
        return DEFAULT_GUARDRAIL_STATE
    state = value.strip().lower()
    if state not in GUARDRAIL_STATES:
        raise ValueError(
            f"unknown guardrail state {value!r}. Expected one of: "
            f"{', '.join(GUARDRAIL_STATES)}"
        )
    return state


def _require_guardrail_value(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if value:
        return value
    where = env_file()
    hint = f"Set it in {where}" if where else "Set it in the environment"
    raise GuardrailNotConfigured(
        f"{variable} is not set, so the guardrail cannot be applied. {hint}."
    )


def guardrail_id() -> str:
    """Identifier of the Bedrock guardrail to apply."""
    return _require_guardrail_value(GUARDRAIL_ID_VAR)


def guardrail_version() -> str:
    """Version of the guardrail to apply, for example ``DRAFT``."""
    return _require_guardrail_value(GUARDRAIL_VERSION_VAR)


def api_key_parameter_arn() -> str | None:
    """ARN of the SSM parameter holding the API key, if configured."""
    return os.environ.get(API_KEY_PARAMETER_ARN_VAR, "").strip() or None


def resolve_api_key() -> str:
    """The API key the deployed API expects.

    Used by the local smoke scripts, never by the deployed Lambda: that reads the
    parameter itself through ``c1/api/auth.py``.

    ``$API_KEY`` wins when set. Otherwise this reads the same parameter the stack
    was deployed with, which is the only way to guarantee the client and the
    Lambda agree. Hand-copying the key into a second file is how you end up
    debugging a 401 that is really a typo.
    """
    direct = os.environ.get(API_KEY_VAR, "").strip()
    if direct:
        return direct

    arn = api_key_parameter_arn()
    if not arn:
        raise ApiKeyUnavailable(
            f"neither {API_KEY_VAR} nor {API_KEY_PARAMETER_ARN_VAR} is set, so the "
            "API key cannot be resolved."
        )

    try:
        import boto3

        response = boto3.client("ssm").get_parameter(Name=arn, WithDecryption=True)
        value = response["Parameter"]["Value"].strip()
    except Exception as exc:  # noqa: BLE001 - surfaced with the ARN for context
        raise ApiKeyUnavailable(f"could not read {arn}: {exc}") from exc

    if not value:
        raise ApiKeyUnavailable(f"{arn} is empty.")
    return value


def guardrail_configured() -> bool:
    """Whether both guardrail variables are present."""
    return bool(
        os.environ.get(GUARDRAIL_ID_VAR, "").strip()
        and os.environ.get(GUARDRAIL_VERSION_VAR, "").strip()
    )
