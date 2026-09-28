"""Reading guardrail assessments. No AWS calls.

Field names here mirror the bedrock-runtime service model. If the parser stops
matching them it fails silently as an empty filter list, which is exactly the bug
these tests exist to catch.
"""

from __future__ import annotations

import pytest

from c1.guardrails import (
    INTERVENED,
    STAGE_INPUT,
    STAGE_OUTPUT,
    fired_filters,
    grounding_content_blocks,
    guardrail_arn,
    guardrail_message,
    intervened,
    median_latency_ms,
    trace_filters,
)

GUARDRAIL = "ttez4741pmc6"


def blocked(**extra):
    return {"action": "BLOCKED", "detected": True, **extra}


def allowed(**extra):
    return {"action": "NONE", "detected": False, **extra}


# --- one label per policy type ---------------------------------------------


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        ({"topicPolicy": {"topics": [blocked(name="CompetitorComparison")]}},
         "topicPolicy:CompetitorComparison"),
        ({"contentPolicy": {"filters": [blocked(type="PROMPT_ATTACK")]}},
         "contentPolicy:PROMPT_ATTACK"),
        ({"wordPolicy": {"customWords": [blocked(match="QC-STAFF-4471")]}},
         "wordPolicy:QC-STAFF-4471"),
        ({"wordPolicy": {"managedWordLists": [blocked(match="damn", type="PROFANITY")]}},
         "wordPolicy:PROFANITY"),
        ({"sensitiveInformationPolicy": {
            "piiEntities": [blocked(match="4111", type="CREDIT_DEBIT_CARD_NUMBER")]}},
         "sensitiveInformationPolicy:CREDIT_DEBIT_CARD_NUMBER"),
        ({"sensitiveInformationPolicy": {
            "regexes": [blocked(name="StaffOverridePattern", match="QC-STAFF-4471")]}},
         "sensitiveInformationPolicy:StaffOverridePattern"),
        ({"contextualGroundingPolicy": {
            "filters": [blocked(type="GROUNDING", threshold=0.7, score=0.1)]}},
         "contextualGroundingPolicy:GROUNDING"),
    ],
)
def test_each_policy_type_renders_its_label(assessment, expected):
    assert fired_filters([assessment]) == [expected]


def test_every_policy_at_once():
    assessment = {
        "topicPolicy": {"topics": [blocked(name="StaffOverrideCode")]},
        "contentPolicy": {"filters": [blocked(type="PROMPT_ATTACK")]},
        "wordPolicy": {"customWords": [blocked(match="staff override code")]},
        "sensitiveInformationPolicy": {
            "piiEntities": [{"match": "a@b.c", "type": "EMAIL", "action": "ANONYMIZED"}]
        },
        "contextualGroundingPolicy": {"filters": [blocked(type="RELEVANCE")]},
    }
    assert fired_filters([assessment]) == [
        "topicPolicy:StaffOverrideCode",
        "contentPolicy:PROMPT_ATTACK",
        "wordPolicy:staff override code",
        "sensitiveInformationPolicy:EMAIL",
        "contextualGroundingPolicy:RELEVANCE",
    ]


# --- what counts as fired --------------------------------------------------


def test_action_none_is_not_reported():
    assessment = {
        "topicPolicy": {"topics": [allowed(name="CompetitorComparison")]},
        "contentPolicy": {"filters": [allowed(type="HATE")]},
    }
    assert fired_filters([assessment]) == []


def test_anonymized_counts_as_fired():
    """A masked PII entity did act, even though nothing was blocked."""
    assessment = {"sensitiveInformationPolicy": {
        "piiEntities": [{"match": "x", "type": "NAME", "action": "ANONYMIZED"}]}}
    assert fired_filters([assessment]) == ["sensitiveInformationPolicy:NAME"]


def test_detected_is_the_fallback_when_action_is_absent():
    assert fired_filters([{"contentPolicy": {"filters": [{"type": "HATE", "detected": True}]}}]) == [
        "contentPolicy:HATE"
    ]
    assert fired_filters([{"contentPolicy": {"filters": [{"type": "HATE", "detected": False}]}}]) == []


def test_duplicate_matches_are_collapsed():
    assessment = {"wordPolicy": {"customWords": [blocked(match="QC-STAFF"), blocked(match="QC-STAFF")]}}
    assert fired_filters([assessment]) == ["wordPolicy:QC-STAFF"]


# --- never leak the matched secret -----------------------------------------


def test_regex_label_uses_the_name_not_the_match():
    """The regex match is the staff code itself, so it must not be echoed."""
    labels = fired_filters([{"sensitiveInformationPolicy": {
        "regexes": [blocked(name="StaffOverridePattern", match="QC-STAFF-4471")]}}])
    assert labels == ["sensitiveInformationPolicy:StaffOverridePattern"]
    assert not any("4471" in label for label in labels)


def test_pii_label_uses_the_type_not_the_match():
    labels = fired_filters([{"sensitiveInformationPolicy": {
        "piiEntities": [blocked(match="4111 1111 1111 1111", type="CREDIT_DEBIT_CARD_NUMBER")]}}])
    assert not any("4111" in label for label in labels)


def test_managed_word_label_uses_the_type_not_the_word():
    labels = fired_filters([{"wordPolicy": {
        "managedWordLists": [blocked(match="somethingrude", type="PROFANITY")]}}])
    assert labels == ["wordPolicy:PROFANITY"]
    assert not any("somethingrude" in label for label in labels)


# --- defensive -------------------------------------------------------------


@pytest.mark.parametrize("value", [None, [], [{}], [None], ["nonsense"]])
def test_fired_filters_survives_junk(value):
    assert fired_filters(value) == []


def test_entries_missing_their_name_are_skipped():
    assert fired_filters([{"topicPolicy": {"topics": [blocked()]}}]) == []


# --- Converse trace --------------------------------------------------------


def test_trace_input_assessment_is_keyed_by_guardrail_id():
    trace = {"inputAssessment": {GUARDRAIL: {
        "topicPolicy": {"topics": [blocked(name="StaffOverrideCode")]},
        "invocationMetrics": {"guardrailProcessingLatency": 240},
    }}}
    assert trace_filters(trace) == (["topicPolicy:StaffOverrideCode"], STAGE_INPUT)


def test_trace_output_assessments_is_keyed_by_id_and_holds_a_list():
    trace = {"outputAssessments": {GUARDRAIL: [
        {"contextualGroundingPolicy": {"filters": [blocked(type="GROUNDING")]}}
    ]}}
    assert trace_filters(trace) == (["contextualGroundingPolicy:GROUNDING"], STAGE_OUTPUT)


def test_input_stage_wins_when_both_are_present():
    """Input is what stopped the turn, so it is the stage worth reporting."""
    trace = {
        "inputAssessment": {GUARDRAIL: {"contentPolicy": {"filters": [blocked(type="PROMPT_ATTACK")]}}},
        "outputAssessments": {GUARDRAIL: [{"topicPolicy": {"topics": [blocked(name="X")]}}]},
    }
    labels, stage = trace_filters(trace)
    assert stage == STAGE_INPUT
    assert labels == ["contentPolicy:PROMPT_ATTACK"]


@pytest.mark.parametrize(
    "trace",
    [None, {}, {"inputAssessment": {}}, {"inputAssessment": {GUARDRAIL: {"topicPolicy": {"topics": []}}}}],
)
def test_trace_filters_returns_no_stage_when_nothing_fired(trace):
    assert trace_filters(trace) == ([], None)


# --- small helpers ---------------------------------------------------------


def test_intervened_reads_the_top_level_action():
    assert intervened({"action": INTERVENED}) is True
    assert intervened({"action": "NONE"}) is False
    assert intervened(None) is False


def test_guardrail_message_joins_the_outputs():
    assert guardrail_message({"outputs": [{"text": "one"}, {"text": "two"}]}) == "one\ntwo"
    assert guardrail_message({}) == ""
    assert guardrail_message(None) == ""


@pytest.mark.parametrize(
    ("values", "expected"),
    [([], 0), ([7], 7), ([10, 20], 15), ([100, 120, 900], 120), ([1, "x", 3], 2)],
)
def test_median_latency(values, expected):
    assert median_latency_ms(values) == expected


def test_guardrail_arn_builds_from_an_id():
    assert guardrail_arn(region="eu-west-2", account="1234", identifier="abc") == (
        "arn:aws:bedrock:eu-west-2:1234:guardrail/abc"
    )


def test_guardrail_arn_passes_an_arn_through():
    arn = "arn:aws:bedrock:us-east-1:9:guardrail/xyz"
    assert guardrail_arn(region="eu-west-2", account="1", identifier=arn) == arn


# --- the guarded Converse turn ---------------------------------------------


def test_grounding_blocks_carry_the_right_qualifiers():
    blocks = grounding_content_blocks("THE POLICY", "THE PROMPT")
    assert [b["guardContent"]["text"]["qualifiers"] for b in blocks] == [
        ["grounding_source"],
        ["query", "guard_content"],
    ]
    assert blocks[0]["guardContent"]["text"]["text"] == "THE POLICY"
    assert blocks[1]["guardContent"]["text"]["text"] == "THE PROMPT"


def test_the_prompt_is_marked_guard_content_so_input_policies_see_it():
    """Without guard_content, "query" is evaluated only by contextual grounding.

    Every input-side policy would then have nothing to look at, and
    PROMPT_ATTACK (output-NONE by config) could never fire at all.
    """
    blocks = grounding_content_blocks("policy", "prompt")
    assert "guard_content" in blocks[1]["guardContent"]["text"]["qualifiers"]


def test_the_grounding_source_is_not_marked_guard_content():
    """Screening the policy would block the guardrail's own reference material."""
    assert blocks_qualifiers_of(grounding_content_blocks("policy", "prompt"))[0] == [
        "grounding_source"
    ]


def blocks_qualifiers_of(blocks):
    return [b["guardContent"]["text"]["qualifiers"] for b in blocks]


def test_every_block_is_wrapped_in_guardcontent():
    """An unwrapped block would be skipped by the guardrail entirely."""
    for block in grounding_content_blocks("p", "q"):
        assert set(block) == {"guardContent"}
