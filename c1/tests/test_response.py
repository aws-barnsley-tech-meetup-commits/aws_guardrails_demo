"""Response building. No AWS calls."""

from __future__ import annotations

from c1.api.judge import load_checks
from c1.api.response import (
    FEEDBACK_HELD,
    FEEDBACK_JUDGE_ERROR,
    REASON_NOT_EVALUATED,
    RESULT_BLOCKED,
    broken_phrase,
    build_response,
)

CHECKS = load_checks()


def verdicts(**broken_by_id: bool) -> list[dict]:
    return [
        {
            "id": check["id"],
            "broken": broken_by_id.get(check["id"], False),
            "reason": f"reason for {check['id']}",
        }
        for check in CHECKS
    ]


def build(**kwargs):
    defaults = {
        "prompt": "a prompt",
        "bot_response": "a reply",
        "latency_ms": 1234,
        "checks": CHECKS,
    }
    return build_response(**{**defaults, **kwargs})


def test_held_when_nothing_is_broken():
    body = build(verdicts=verdicts())
    assert body["result"] == "held"
    assert body["feedback"] == FEEDBACK_HELD
    assert all(check["broken"] is False for check in body["checks"])


def test_broken_when_one_check_breaks():
    target = CHECKS[1]
    body = build(verdicts=verdicts(**{target["id"]: True}))
    assert body["result"] == "broken"
    assert body["feedback"] == broken_phrase({**target, "rule": target.get("rule")})


def test_broken_joins_every_broken_check_on_one_line():
    first, second = CHECKS[0], CHECKS[-1]
    body = build(verdicts=verdicts(**{first["id"]: True, second["id"]: True}))
    assert body["result"] == "broken"
    assert body["feedback"] == (
        f"{broken_phrase({**first, 'rule': first.get('rule')})} "
        f"{broken_phrase({**second, 'rule': second.get('rule')})}"
    )
    assert "\n" not in body["feedback"]


def test_error_when_the_judge_failed():
    body = build(verdicts=None)
    assert body["result"] == "error"
    assert body["feedback"] == FEEDBACK_JUDGE_ERROR
    assert all(check["broken"] is False for check in body["checks"])
    assert all(check["reason"] == REASON_NOT_EVALUATED for check in body["checks"])


def test_labels_and_rules_come_from_checks_not_the_judge():
    lying_verdicts = [
        {
            "id": check["id"],
            "broken": False,
            "reason": "",
            "label": "JUDGE MADE THIS UP",
            "rule": 99,
        }
        for check in CHECKS
    ]
    body = build(verdicts=lying_verdicts)
    for row, check in zip(body["checks"], CHECKS, strict=True):
        assert row["label"] == check["label"]
        assert row["rule"] == check.get("rule")


def test_checks_keep_catalogue_order_even_when_verdicts_are_shuffled():
    shuffled = list(reversed(verdicts()))
    body = build(verdicts=shuffled)
    assert [row["id"] for row in body["checks"]] == [check["id"] for check in CHECKS]


def test_verdicts_missing_a_check_default_to_not_broken():
    first = CHECKS[0]
    partial = [verdicts(**{first["id"]: True})[0]]
    body = build(verdicts=partial)
    assert body["result"] == "broken"
    assert body["checks"][0]["broken"] is True
    assert body["checks"][1]["broken"] is False
    assert body["checks"][1]["reason"] == REASON_NOT_EVALUATED


def test_response_shape_matches_the_spec():
    body = build(verdicts=verdicts())
    assert set(body) == {
        "result",
        "feedback",
        "checks",
        "prompt",
        "botResponse",
        "latencyMs",
        # Added by the addendum so the UI can label which model and guardrail
        # produced the result.
        "modelTier",
        "modelId",
        "guardrail",
        "firedFilters",
        "guardrailStage",
    }
    assert body["prompt"] == "a prompt"
    assert body["botResponse"] == "a reply"
    assert body["latencyMs"] == 1234
    for row in body["checks"]:
        assert set(row) == {"id", "label", "rule", "broken", "reason"}


def test_reason_is_carried_through_from_the_judge():
    target = CHECKS[-1]
    body = build(verdicts=verdicts(**{target["id"]: True}))
    reasons = {row["id"]: row["reason"] for row in body["checks"]}
    assert reasons[target["id"]] == f"reason for {target['id']}"


# --- the guardrail blocked the turn ----------------------------------------


def test_blocked_beats_the_judge_verdicts():
    """A blocked turn never produced the bot's own words to judge."""
    body = build(
        verdicts=verdicts(**{CHECKS[0]["id"]: True}),
        blocked=True,
        fired_filters=["topicPolicy:StaffOverrideCode"],
        guardrail_stage="input",
    )
    assert body["result"] == RESULT_BLOCKED
    assert all(row["broken"] is False for row in body["checks"])


def test_blocked_feedback_names_the_stage_and_filters():
    body = build(
        verdicts=None,
        blocked=True,
        fired_filters=["topicPolicy:StaffOverrideCode", "contentPolicy:PROMPT_ATTACK"],
        guardrail_stage="input",
    )
    assert body["feedback"] == (
        "Blocked by the guardrail on input: "
        "topicPolicy:StaffOverrideCode, contentPolicy:PROMPT_ATTACK."
    )


def test_blocked_on_output_says_so():
    body = build(verdicts=None, blocked=True,
                 fired_filters=["contextualGroundingPolicy:GROUNDING"],
                 guardrail_stage="output")
    assert "on output" in body["feedback"]


def test_blocked_without_a_trace_admits_the_filters_are_unknown():
    """stop_reason said intervened but no trace arrived. Do not imply nothing fired."""
    body = build(verdicts=None, blocked=True, fired_filters=[], guardrail_stage=None)
    assert body["result"] == RESULT_BLOCKED
    assert "unavailable" in body["feedback"].lower()


def test_blocked_rows_are_marked_not_evaluated():
    body = build(verdicts=None, blocked=True, fired_filters=["x"], guardrail_stage="input")
    for row in body["checks"]:
        assert "guardrail blocked" in row["reason"]


def test_fired_filters_and_stage_are_echoed():
    body = build(verdicts=verdicts(), fired_filters=["a", "b"], guardrail_stage="output")
    assert body["firedFilters"] == ["a", "b"]
    assert body["guardrailStage"] == "output"


def test_fired_filters_default_to_an_empty_list_not_none():
    """The UI should not have to branch on shape."""
    body = build(verdicts=verdicts())
    assert body["firedFilters"] == []
    assert body["guardrailStage"] is None


# --- model and guardrail labelling -----------------------------------------


def test_model_and_guardrail_are_echoed_for_the_ui():
    body = build(
        verdicts=verdicts(),
        model_tier="frontier",
        model_id="global.anthropic.claude-sonnet-4-6",
        guardrail="on",
    )
    assert body["modelTier"] == "frontier"
    assert body["modelId"] == "global.anthropic.claude-sonnet-4-6"
    assert body["guardrail"] == "on"


def test_guardrail_defaults_to_off():
    assert build(verdicts=verdicts())["guardrail"] == "off"


# --- feedback without rule numbers ----------------------------------------


def test_broken_phrase_drops_the_number_when_there_is_no_rule():
    assert broken_phrase({"label": "Wrote code", "rule": None}) == "Broke: Wrote code."
    assert broken_phrase({"label": "Wrote code"}) == "Broke: Wrote code."


def test_broken_phrase_keeps_the_number_when_there_is_one():
    assert broken_phrase({"label": "Wrote code", "rule": 7}) == "Broke rule 7: Wrote code."


def test_feedback_never_says_rule_none():
    """checks.json dropped the rule field; the old format would read badly."""
    body = build(verdicts=verdicts(**{CHECKS[0]["id"]: True}))
    assert "None" not in body["feedback"]
