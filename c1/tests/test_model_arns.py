"""Bedrock resource ARNs for each shape of model id. No AWS calls."""

from __future__ import annotations

import pytest

from infra.model_arns import (
    foundation_model_id,
    is_geo_profile_id,
    is_global_profile_id,
    is_inference_profile_id,
    model_invoke_resources,
)

REGION = "eu-west-2"
ACCOUNT = "154443798753"

# Deliberately synthetic. These exercise the two id shapes that matter: one with
# a version suffix and one without. Do not swap these for whatever .env happens
# to name today, or this file goes stale every time the models change.
SUFFIXED = "anthropic.claude-haiku-4-5-20251001-v1:0"
UNSUFFIXED = "anthropic.claude-sonnet-5"

GLOBAL_SUFFIXED = f"global.{SUFFIXED}"
GLOBAL_UNSUFFIXED = f"global.{UNSUFFIXED}"


def resources(model_id: str) -> list[str]:
    return model_invoke_resources(model_id, region=REGION, account=ACCOUNT)


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    "model_id", [GLOBAL_SUFFIXED, GLOBAL_UNSUFFIXED, "global.amazon.nova-pro-v1:0"]
)
def test_global_ids_are_recognised(model_id):
    assert is_global_profile_id(model_id)
    assert is_inference_profile_id(model_id)
    assert not is_geo_profile_id(model_id)


@pytest.mark.parametrize(
    "model_id",
    [
        "us.anthropic.claude-sonnet-5",
        "eu.anthropic.claude-sonnet-5",
        "apac.anthropic.claude-sonnet-5",
        "au.anthropic.claude-sonnet-5",
        "jp.anthropic.claude-sonnet-5",
        "us-gov.anthropic.claude-sonnet-5",
    ],
)
def test_geo_ids_are_recognised(model_id):
    assert is_geo_profile_id(model_id)
    assert is_inference_profile_id(model_id)
    assert not is_global_profile_id(model_id)


@pytest.mark.parametrize(
    "model_id", ["anthropic.claude-sonnet-5", "anthropic.claude-haiku-4-5-20251001-v1:0"]
)
def test_plain_model_ids_are_not_profiles(model_id):
    assert not is_inference_profile_id(model_id)
    assert not is_global_profile_id(model_id)
    assert not is_geo_profile_id(model_id)


# --- prefix stripping ------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        (GLOBAL_UNSUFFIXED, UNSUFFIXED),
        (GLOBAL_SUFFIXED, SUFFIXED),
        (f"us.{UNSUFFIXED}", UNSUFFIXED),
        (f"us-gov.{UNSUFFIXED}", UNSUFFIXED),
        (UNSUFFIXED, UNSUFFIXED),
    ],
)
def test_foundation_model_id_strips_only_the_prefix(model_id, expected):
    assert foundation_model_id(model_id) == expected


def test_stripping_does_not_touch_a_version_suffix():
    assert foundation_model_id(GLOBAL_SUFFIXED) == SUFFIXED
    assert foundation_model_id(GLOBAL_SUFFIXED).endswith("-v1:0")


def test_stripping_handles_an_id_with_no_version_suffix():
    """Anthropic dropped -v1:0 from Sonnet 4.6 onward; both shapes are valid."""
    assert foundation_model_id(GLOBAL_UNSUFFIXED) == UNSUFFIXED


# --- global profiles -------------------------------------------------------


@pytest.mark.parametrize(
    ("profile_id", "model"),
    [(GLOBAL_UNSUFFIXED, UNSUFFIXED), (GLOBAL_SUFFIXED, SUFFIXED)],
)
def test_global_profile_gets_all_three_documented_arns(profile_id, model):
    assert resources(profile_id) == [
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{profile_id}",
        f"arn:aws:bedrock:{REGION}::foundation-model/{model}",
        f"arn:aws:bedrock:::foundation-model/{model}",
    ]


@pytest.mark.parametrize(
    ("profile_id", "model"),
    [(GLOBAL_UNSUFFIXED, UNSUFFIXED), (GLOBAL_SUFFIXED, SUFFIXED)],
)
def test_global_profile_includes_the_region_agnostic_model_arn(profile_id, model):
    """Without this ARN every cross-Region hop fails AccessDenied."""
    assert f"arn:aws:bedrock:::foundation-model/{model}" in resources(profile_id)


def test_the_region_agnostic_arn_has_empty_region_and_account():
    global_arn = next(
        a for a in resources(GLOBAL_UNSUFFIXED) if a.startswith("arn:aws:bedrock:::")
    )
    _, _, _, region, account, _ = global_arn.split(":", 5)
    assert region == ""
    assert account == ""


def test_global_profile_arn_uses_the_calling_region_and_account():
    profile_arn = next(
        a for a in resources(GLOBAL_UNSUFFIXED) if ":inference-profile/" in a
    )
    assert profile_arn == (
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{GLOBAL_UNSUFFIXED}"
    )


def test_global_profile_does_not_use_a_wildcard_region():
    assert not any("bedrock:*:" in arn for arn in resources(GLOBAL_UNSUFFIXED))


@pytest.mark.parametrize("profile_id", [GLOBAL_SUFFIXED, GLOBAL_UNSUFFIXED])
def test_a_global_profile_resolves_to_exactly_three_arns(profile_id):
    assert len(resources(profile_id)) == 3


# --- geo profiles ----------------------------------------------------------


def test_geo_profile_gets_profile_plus_wildcard_region_model():
    assert resources(f"us.{UNSUFFIXED}") == [
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/us.{UNSUFFIXED}",
        f"arn:aws:bedrock:*::foundation-model/{UNSUFFIXED}",
    ]


# --- plain models ----------------------------------------------------------


def test_plain_model_gets_one_regional_arn():
    assert resources(UNSUFFIXED) == [
        f"arn:aws:bedrock:{REGION}::foundation-model/{UNSUFFIXED}"
    ]


def test_a_plain_model_never_gets_an_inference_profile_arn():
    """deepseek.v3-v1:0 and friends have no profile to grant."""
    assert not any(":inference-profile/" in arn for arn in resources("deepseek.v3-v1:0"))


# --- general ---------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [GLOBAL_SUFFIXED, GLOBAL_UNSUFFIXED, "us.anthropic.x", "anthropic.x", "deepseek.v3-v1:0"],
)
def test_no_arn_is_fully_wildcarded(model_id):
    """Every grant must name a specific model, never bedrock:*."""
    for arn in resources(model_id):
        assert arn.endswith(foundation_model_id(model_id)) or ":inference-profile/" in arn
        assert not arn.endswith("/*")
        assert arn != "*"
