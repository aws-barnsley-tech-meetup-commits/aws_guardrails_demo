"""Fills in the values the deployed stack owns, for local tooling only.

Since the stack creates the guardrail and the API key, neither has an id anyone
can put in ``.env`` before the first deploy. The deployed Lambda and runtime get
both injected as environment variables, so they never need this. Local tooling
does: ``scripts/smoke_*.py`` and ``c1/agent/main.py --guardrail`` all resolve the
guardrail through ``c1.config``, which only reads the environment.

So this reads the stack outputs once and puts them in ``os.environ``, filling only
what is not already set. Precedence stays what it was:

1. an exported variable, which still wins for a one-off experiment
2. ``.env``, if you pinned something there
3. the deployed stack

Deliberately not part of ``infra/bundling.py``'s shared sources. This module must
never reach a deployment bundle, where a CloudFormation lookup would be both
impossible and wrong.

Fails soft. No credentials, no stack, wrong region: the caller carries on and hits
the normal "not configured" error from ``c1.config``, which says what to set.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

logger = logging.getLogger("c1.local_env")

#: Default stack name, matching infra/app.py.
DEFAULT_STACK_NAME = "GuardrailsDemo"

#: Stack output -> environment variable. Only these four are hydrated; they are
#: the ones with no sensible value before a deploy.
OUTPUT_TO_VARIABLE = {
    "GuardrailId": "GUARDRAIL_ID",
    "GuardrailVersion": "GUARDRAIL_VERSION",
    "ApiKeyParameterArn": "API_KEY_PARAMETER_ARN",
    # Saves the smoke scripts a second call to read the parameter they would
    # otherwise resolve through API_KEY_PARAMETER_ARN.
    "ApiKey": "API_KEY",
}

_cache: dict[str, str] | None = None


class StackOutputsUnavailable(RuntimeError):
    """The stack outputs could not be read."""


def stack_name() -> str:
    return os.environ.get("STACK_NAME", "").strip() or DEFAULT_STACK_NAME


def outputs(*, name: str | None = None, client: Any = None) -> Mapping[str, str]:
    """Every output of the deployed stack, keyed by output name.

    Cached for the life of the process: these do not change under a running
    script, and the scripts call this more than once.
    """
    global _cache
    if _cache is not None and client is None:
        return _cache

    target = name or stack_name()
    try:
        import boto3

        response = (client or boto3.client("cloudformation")).describe_stacks(
            StackName=target
        )
        stacks = response.get("Stacks") or []
        if not stacks:
            raise StackOutputsUnavailable(f"stack {target!r} was not found.")
        resolved = {
            entry["OutputKey"]: entry["OutputValue"]
            for entry in stacks[0].get("Outputs") or []
            if entry.get("OutputKey") and entry.get("OutputValue") is not None
        }
    except StackOutputsUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - reported with the stack name
        raise StackOutputsUnavailable(f"could not read stack {target!r}: {exc}") from exc

    if client is None:
        _cache = resolved
    return resolved


def hydrate(*, name: str | None = None, client: Any = None) -> dict[str, str]:
    """Fill absent variables from the stack outputs. Returns what it set.

    A no-op when everything is already set, so the CloudFormation call is skipped
    entirely in that case.
    """
    wanted = {
        output: variable
        for output, variable in OUTPUT_TO_VARIABLE.items()
        if not os.environ.get(variable, "").strip()
    }
    if not wanted:
        return {}

    try:
        resolved = outputs(name=name, client=client)
    except StackOutputsUnavailable as exc:
        logger.debug("not hydrating from the stack: %s", exc)
        return {}

    applied: dict[str, str] = {}
    for output, variable in wanted.items():
        value = (resolved.get(output) or "").strip()
        if value:
            os.environ[variable] = value
            applied[variable] = value
    return applied


def reset_cache() -> None:
    """Drop the cached outputs. Used by tests."""
    global _cache
    _cache = None
