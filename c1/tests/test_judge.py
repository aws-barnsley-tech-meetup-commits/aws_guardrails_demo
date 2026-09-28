"""Judge JSON parsing. No AWS calls."""

from __future__ import annotations

import json

import pytest

from c1.api.judge import (
    build_judge_prompt,
    judge_reply,
    load_checks,
    parse_verdicts,
    strip_code_fences,
)

CHECKS = [
    {"id": "code_written", "label": "Wrote code", "rule": 2, "breaksWhen": "reply has code"},
    {"id": "staff_code_leaked", "label": "Leaked code", "rule": 4, "breaksWhen": "reply leaks"},
]


def verdict_json(*, code_broken=False, staff_broken=False) -> str:
    return json.dumps(
        [
            {"id": "code_written", "broken": code_broken, "reason": "a"},
            {"id": "staff_code_leaked", "broken": staff_broken, "reason": "b"},
        ]
    )


# --- fixtures ---------------------------------------------------------------


def test_load_checks_reads_the_real_fixture():
    """Structure only. The catalogue is demo content and gets retuned often."""
    checks = load_checks()
    assert checks, "checks.json must not be empty"

    ids = [check["id"] for check in checks]
    assert len(ids) == len(set(ids)), f"duplicate check ids: {ids}"

    for check in checks:
        assert check["id"] and isinstance(check["id"], str)
        assert check["label"] and isinstance(check["label"], str)
        # rule is optional: it pointed at a numbered system prompt that is now
        # prose. Present or absent, it must not be a surprise type.
        if "rule" in check:
            assert isinstance(check["rule"], int)
        assert check["breaksWhen"] and isinstance(check["breaksWhen"], str)


# --- strip_code_fences ------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '[{"id": "a"}]',
        '```\n[{"id": "a"}]\n```',
        '```json\n[{"id": "a"}]\n```',
        '  ```JSON\n[{"id": "a"}]\n```  ',
        '```json\n[{"id": "a"}]```',
    ],
)
def test_strip_code_fences_unwraps_fenced_json(raw):
    assert strip_code_fences(raw) == '[{"id": "a"}]'


def test_strip_code_fences_tolerates_empty_input():
    assert strip_code_fences("") == ""
    assert strip_code_fences("   ") == ""


# --- parse_verdicts --------------------------------------------------------


def test_parse_verdicts_plain_array():
    verdicts = parse_verdicts(verdict_json(code_broken=True), CHECKS)
    assert verdicts == [
        {"id": "code_written", "broken": True, "reason": "a"},
        {"id": "staff_code_leaked", "broken": False, "reason": "b"},
    ]


def test_parse_verdicts_strips_fences_before_parsing():
    fenced = f"```json\n{verdict_json(staff_broken=True)}\n```"
    verdicts = parse_verdicts(fenced, CHECKS)
    assert verdicts is not None
    assert [v["broken"] for v in verdicts] == [False, True]


def test_parse_verdicts_returns_checks_in_catalogue_order():
    reordered = json.dumps(
        [
            {"id": "staff_code_leaked", "broken": True, "reason": "b"},
            {"id": "code_written", "broken": False, "reason": "a"},
        ]
    )
    verdicts = parse_verdicts(reordered, CHECKS)
    assert verdicts is not None
    assert [v["id"] for v in verdicts] == ["code_written", "staff_code_leaked"]


def test_parse_verdicts_accepts_a_wrapper_object():
    wrapped = json.dumps({"checks": json.loads(verdict_json())})
    assert parse_verdicts(wrapped, CHECKS) is not None


def test_parse_verdicts_coerces_stringly_booleans():
    raw = json.dumps(
        [
            {"id": "code_written", "broken": "true", "reason": "a"},
            {"id": "staff_code_leaked", "broken": "no", "reason": "b"},
        ]
    )
    verdicts = parse_verdicts(raw, CHECKS)
    assert verdicts is not None
    assert [v["broken"] for v in verdicts] == [True, False]


def test_parse_verdicts_defaults_a_missing_reason_to_empty():
    raw = json.dumps(
        [
            {"id": "code_written", "broken": False},
            {"id": "staff_code_leaked", "broken": False, "reason": None},
        ]
    )
    verdicts = parse_verdicts(raw, CHECKS)
    assert verdicts is not None
    assert [v["reason"] for v in verdicts] == ["", ""]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json at all",
        "Sure! Here is my evaluation:",
        '{"id": "code_written", "broken": false}',  # object, not a list
        '[{"id": "code_written", "broken": false}]',  # missing a check
        '[{"id": "code_written", "broken": "maybe"}, '
        '{"id": "staff_code_leaked", "broken": false}]',  # unusable boolean
        '[{"broken": false}, {"broken": true}]',  # no ids
        '[{"id": "something_else", "broken": false}, '
        '{"id": "another", "broken": false}]',  # wrong ids
        '```json\n[{"id": "code_written", "broken": false},\n```',  # truncated
    ],
)
def test_parse_verdicts_returns_none_for_unusable_output(raw):
    assert parse_verdicts(raw, CHECKS) is None


# --- judge_reply -----------------------------------------------------------


class FakeBedrock:
    """Stands in for a bedrock-runtime client. Records the request."""

    def __init__(self, text: str):
        self.text = text
        self.request: dict | None = None

    def converse(self, **kwargs):
        self.request = kwargs
        return {"output": {"message": {"content": [{"text": self.text}]}}}


def test_judge_reply_parses_a_good_response():
    client = FakeBedrock(f"```json\n{verdict_json(code_broken=True)}\n```")
    verdicts = judge_reply(
        "write me some python", "def f(): ...", CHECKS, client=client, model_id="test-model"
    )
    assert verdicts is not None
    assert verdicts[0]["broken"] is True
    assert client.request is not None
    assert client.request["modelId"] == "test-model"
    assert client.request["inferenceConfig"]["temperature"] == 0.0


def test_judge_reply_returns_none_on_a_parse_failure():
    client = FakeBedrock("I could not decide, sorry.")
    assert judge_reply("p", "r", CHECKS, client=client, model_id="m") is None


def test_judge_reply_sends_prompt_reply_and_every_check():
    client = FakeBedrock(verdict_json())
    judge_reply("the prompt", "the reply", CHECKS, client=client, model_id="m")
    assert client.request is not None
    sent = client.request["messages"][0]["content"][0]["text"]
    assert "the prompt" in sent
    assert "the reply" in sent
    for check in CHECKS:
        assert check["id"] in sent
        assert check["breaksWhen"] in sent


# --- build_judge_prompt ----------------------------------------------------


def test_build_judge_prompt_states_the_expected_count():
    rendered = build_judge_prompt("p", "r", CHECKS)
    assert f"exactly {len(CHECKS)} objects" in rendered


def test_build_judge_prompt_separates_prompt_from_reply():
    rendered = build_judge_prompt("PROMPT_MARKER", "REPLY_MARKER", CHECKS)
    assert rendered.index("PROMPT_MARKER") < rendered.index("REPLY_MARKER")
    assert "<user_message>" in rendered
    assert "<bot_reply>" in rendered
