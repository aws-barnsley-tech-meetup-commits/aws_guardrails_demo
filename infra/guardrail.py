"""The Bedrock guardrail, built from ``guardrail-config.json``.

The JSON file stays the single source of truth. It is written in the shape the
Bedrock API takes, which is the same shape ``aws bedrock create-guardrail
--cli-input-json`` accepts, so you can still tune the guardrail against
``scripts/smoke_guardrail.py --local`` and then deploy the identical config.

CloudFormation wants the same tree with PascalCase keys, and that is the whole
translation: ``topicPolicyConfig`` becomes ``TopicPolicyConfig``,
``inputStrength`` becomes ``InputStrength``, all the way down. Doing it
mechanically rather than field by field means adding a new policy to the JSON
needs no change here.

Why the escape hatch rather than ``CfnGuardrail``: the typed L1 needs each nested
level constructed as a jsii struct, so feeding it a parsed JSON tree means
hand-mapping every property class and re-mapping them whenever the config grows.
``CfnResource`` takes the tree as-is and CloudFormation validates it, which keeps
this file short and keeps the JSON authoritative.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aws_cdk import CfnResource, RemovalPolicy
from constructs import Construct

ROOT = Path(__file__).resolve().parents[1]

#: The guardrail definition. Also what you would pass to the CLI by hand.
CONFIG_PATH = ROOT / "guardrail-config.json"

RESOURCE_TYPE = "AWS::Bedrock::Guardrail"

#: Keys the Bedrock API accepts but that belong to the stack, not the config
#: file. Tags are CDK's job, and a client token is meaningless here.
_IGNORED_KEYS = frozenset({"tags", "clientRequestToken"})

#: Required by the API. Checked at synth time so a malformed config fails before
#: the deploy rather than twelve seconds into it.
_REQUIRED_KEYS = ("name", "blockedInputMessaging", "blockedOutputsMessaging")


class GuardrailConfigInvalid(RuntimeError):
    """``guardrail-config.json`` is missing or unusable."""


def _pascal(key: str) -> str:
    """``inputStrength`` -> ``InputStrength``.

    Only the first character changes. Capitalising the whole word would break
    acronyms CloudFormation spells as-is, such as ``PiiEntitiesConfig``.
    """
    return key[:1].upper() + key[1:]


def to_cloudformation(value: Any) -> Any:
    """Re-key a parsed API-shaped config tree for CloudFormation.

    Dict keys are PascalCased at every level. Values are left alone, so filter
    strengths, topic definitions and regex patterns pass through untouched.
    """
    if isinstance(value, dict):
        return {_pascal(k): to_cloudformation(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_cloudformation(v) for v in value]
    return value


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read and sanity check the guardrail config."""
    source = path or CONFIG_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GuardrailConfigInvalid(f"{source} is missing.") from exc
    except json.JSONDecodeError as exc:
        raise GuardrailConfigInvalid(f"{source} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise GuardrailConfigInvalid(f"{source} should contain a JSON object.")

    missing = [key for key in _REQUIRED_KEYS if not str(raw.get(key, "")).strip()]
    if missing:
        raise GuardrailConfigInvalid(
            f"{source} is missing required keys: {', '.join(missing)}"
        )

    return {k: v for k, v in raw.items() if k not in _IGNORED_KEYS}


class DemoGuardrail(Construct):
    """The guardrail the demo attaches when a request sends ``x-guardrail: on``.

    Deleted with the stack. It holds no state worth keeping and the config it was
    built from is in the repo, so ``cdk deploy`` recreates it exactly.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config_path: Path | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        config = load_config(config_path)

        self.resource = CfnResource(
            self,
            "Resource",
            type=RESOURCE_TYPE,
            properties=to_cloudformation(config),
        )
        self.resource.apply_removal_policy(RemovalPolicy.DESTROY)

    @property
    def guardrail_id(self) -> str:
        return self.resource.get_att("GuardrailId").to_string()

    @property
    def guardrail_arn(self) -> str:
        return self.resource.get_att("GuardrailArn").to_string()

    @property
    def version(self) -> str:
        """The version CloudFormation created.

        ``AWS::Bedrock::Guardrail`` only ever manages the working version, so
        this resolves to ``DRAFT``. Reading it from the resource rather than
        hardcoding the string means the agent and the Lambda stay correct if a
        published version is ever added alongside it.
        """
        return self.resource.get_att("Version").to_string()
