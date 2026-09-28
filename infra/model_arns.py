"""Turns a model id into the resource ARNs a ``bedrock:InvokeModel*`` grant needs.

Three shapes of model id show up in ``.env``, and each needs a different set of
resource ARNs:

``anthropic.claude-...``
    A plain foundation model. One regional FM ARN.

``us.anthropic.claude-...`` (also ``eu.``, ``apac.``, ``jp.``, ``au.``, ``us-gov.``)
    A geographic cross-Region inference profile. Needs the profile ARN in the
    calling Region plus the FM in any Region the profile may route to.

``global.anthropic.claude-...``
    A global cross-Region inference profile, which can route to supported
    commercial Regions worldwide. AWS requires three resources: the profile in
    the calling Region, the FM in the calling Region, and a Region-and-account
    agnostic FM ARN. That third one, ``arn:aws:bedrock:::foundation-model/...``,
    has empty region and account fields on purpose; it is what authorizes the
    cross-Region hop, and without it every routed request fails AccessDenied.

    See "IAM policy requirements for global cross-Region inference":
    https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html

Note on newer model ids: Anthropic dropped the ``-v1:0`` suffix starting with
Claude Sonnet 4.6, so ``global.anthropic.claude-haiku-4-5-20251001-v1:0`` is a complete and
valid id despite having no date or version. Older ids such as
``global.anthropic.claude-haiku-4-5-20251001-v1:0`` keep theirs.
"""

from __future__ import annotations

_GLOBAL_PREFIX = "global."

#: Prefixes AWS uses for geographic cross-Region inference profiles.
_GEO_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "us-gov.")

_ALL_PREFIXES = (_GLOBAL_PREFIX, *_GEO_PREFIXES)


def is_global_profile_id(model_id: str) -> bool:
    return model_id.startswith(_GLOBAL_PREFIX)


def is_geo_profile_id(model_id: str) -> bool:
    return model_id.startswith(_GEO_PREFIXES)


def is_inference_profile_id(model_id: str) -> bool:
    return model_id.startswith(_ALL_PREFIXES)


def foundation_model_id(model_id: str) -> str:
    """Strip an inference-profile prefix to get the underlying model id."""
    for prefix in _ALL_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


def model_invoke_resources(model_id: str, *, region: str, account: str) -> list[str]:
    """Resource ARNs to attach to a ``bedrock:InvokeModel*`` statement."""
    model = foundation_model_id(model_id)

    if is_global_profile_id(model_id):
        return [
            # 1. the global profile, in the Region we call from
            f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}",
            # 2. the model in the Region we call from
            f"arn:aws:bedrock:{region}::foundation-model/{model}",
            # 3. the Region-agnostic model, which permits worldwide routing.
            #    Empty region and account are required, not a mistake.
            f"arn:aws:bedrock:::foundation-model/{model}",
        ]

    if is_geo_profile_id(model_id):
        return [
            f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}",
            # Any Region inside the geography the profile routes within.
            f"arn:aws:bedrock:*::foundation-model/{model}",
        ]

    return [f"arn:aws:bedrock:{region}::foundation-model/{model}"]
