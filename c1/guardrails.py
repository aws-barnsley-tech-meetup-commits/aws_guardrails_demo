"""Reading Bedrock guardrail assessments.

Two callers, two response shapes, one vocabulary:

* the agent, which gets a ``GuardrailTraceAssessment`` back from Converse when
  ``guardrailConfig`` is set (``inputAssessment`` plus ``outputAssessments``);
* the ``/c1/guardrail-check`` endpoint, which calls ``ApplyGuardrail`` directly
  and gets a flat ``assessments`` list.

Both reduce to the same ``GuardrailAssessment`` object, so both end up in
``fired_filters()``, which renders ``policy:name`` strings the UI can label:

    topicPolicy:CompetitorComparison
    contentPolicy:PROMPT_ATTACK
    wordPolicy:QC-STAFF-4471
    sensitiveInformationPolicy:CREDIT_DEBIT_CARD_NUMBER
    contextualGroundingPolicy:GROUNDING

Field names and action enums here come from the bedrock-runtime service model,
not from guesswork. Getting one wrong fails silently as an empty filter list, so
the unit tests assert every policy type against a recorded payload shape.

This module is copied flat into both deployment bundles. It must not import
anything else from ``c1``.
"""

from __future__ import annotations

from statistics import median
from typing import Any, Iterable, Mapping, Sequence

#: Guardrail actions that count as the policy having done something. NONE means
#: the policy looked and let it through. ANONYMIZED only appears on the
#: sensitive information policy, where a PII entity was masked rather than
#: blocked: the filter did fire, so it belongs in the list.
ACTED = frozenset({"BLOCKED", "ANONYMIZED"})

#: Top-level action on the response.
INTERVENED = "GUARDRAIL_INTERVENED"

STAGE_INPUT = "input"
STAGE_OUTPUT = "output"


def _acted(entry: Mapping[str, Any]) -> bool:
    """Whether one filter entry actually fired.

    ``action`` is authoritative when present. ``outputScope=FULL`` returns
    non-detected entries too, so falling back to ``detected`` keeps those out
    when a payload omits ``action``.
    """
    action = entry.get("action")
    if isinstance(action, str):
        return action.upper() in ACTED
    return bool(entry.get("detected"))


def _labels(assessment: Mapping[str, Any]) -> list[str]:
    """Render one GuardrailAssessment as policy:name strings."""
    out: list[str] = []

    topics = (assessment.get("topicPolicy") or {}).get("topics") or []
    for topic in topics:
        if _acted(topic) and topic.get("name"):
            out.append(f"topicPolicy:{topic['name']}")

    filters = (assessment.get("contentPolicy") or {}).get("filters") or []
    for entry in filters:
        if _acted(entry) and entry.get("type"):
            out.append(f"contentPolicy:{entry['type']}")

    word_policy = assessment.get("wordPolicy") or {}
    for entry in word_policy.get("customWords") or []:
        if _acted(entry) and entry.get("match"):
            out.append(f"wordPolicy:{entry['match']}")
    for entry in word_policy.get("managedWordLists") or []:
        # Managed lists have a type (PROFANITY); prefer it over the matched word
        # so the label does not leak the profanity itself into the UI.
        if _acted(entry):
            name = entry.get("type") or entry.get("match")
            if name:
                out.append(f"wordPolicy:{name}")

    sensitive = assessment.get("sensitiveInformationPolicy") or {}
    for entry in sensitive.get("piiEntities") or []:
        if _acted(entry) and entry.get("type"):
            out.append(f"sensitiveInformationPolicy:{entry['type']}")
    for entry in sensitive.get("regexes") or []:
        # Report the regex's configured name, never the matched text: the match
        # is the staff code itself.
        if _acted(entry) and entry.get("name"):
            out.append(f"sensitiveInformationPolicy:{entry['name']}")

    grounding = (assessment.get("contextualGroundingPolicy") or {}).get("filters") or []
    for entry in grounding:
        if _acted(entry) and entry.get("type"):
            out.append(f"contextualGroundingPolicy:{entry['type']}")

    return out


def _dedupe(labels: Iterable[str]) -> list[str]:
    """Stable de-duplication. A word can match more than once per assessment."""
    seen: dict[str, None] = {}
    for label in labels:
        seen.setdefault(label, None)
    return list(seen)


def fired_filters(assessments: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """Render a flat ``assessments`` list, as ApplyGuardrail returns it."""
    labels: list[str] = []
    for assessment in assessments or []:
        if isinstance(assessment, Mapping):
            labels.extend(_labels(assessment))
    return _dedupe(labels)


def _assessment_map_values(value: Any) -> list[Mapping[str, Any]]:
    """Flatten a Converse assessment map.

    ``inputAssessment`` is ``{guardrailId: assessment}``. ``outputAssessments``
    is ``{guardrailId: [assessment, ...]}``. Either can arrive as a bare object
    or list depending on the caller, so handle all of it.
    """
    out: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        # A map keyed by guardrail id, or a single assessment.
        looks_like_assessment = any(
            key.endswith("Policy") or key == "invocationMetrics" for key in value
        )
        if looks_like_assessment:
            return [value]
        for nested in value.values():
            out.extend(_assessment_map_values(nested))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            out.extend(_assessment_map_values(nested))
    return out


def trace_filters(guardrail_trace: Mapping[str, Any] | None) -> tuple[list[str], str | None]:
    """Render a Converse ``trace.guardrail`` block.

    Returns the fired filter labels and the stage that fired, ``"input"`` or
    ``"output"``. Input wins when both are present, because the input assessment
    is what stopped the turn.
    """
    if not isinstance(guardrail_trace, Mapping):
        return [], None

    input_labels = _dedupe(
        label
        for assessment in _assessment_map_values(guardrail_trace.get("inputAssessment"))
        for label in _labels(assessment)
    )
    if input_labels:
        return input_labels, STAGE_INPUT

    output_labels = _dedupe(
        label
        for assessment in _assessment_map_values(guardrail_trace.get("outputAssessments"))
        for label in _labels(assessment)
    )
    if output_labels:
        return output_labels, STAGE_OUTPUT

    return [], None


def intervened(response: Mapping[str, Any] | None) -> bool:
    """Whether an ApplyGuardrail response reports an intervention."""
    return bool(response) and response.get("action") == INTERVENED


def guardrail_message(response: Mapping[str, Any] | None) -> str:
    """The guardrail's own replacement text from an ApplyGuardrail response."""
    outputs = (response or {}).get("outputs") or []
    texts = [
        block["text"]
        for block in outputs
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    ]
    return "\n".join(texts).strip()


def median_latency_ms(latencies: Sequence[int | float]) -> int:
    """Median of the per-prompt latencies, 0 when there are none."""
    values = [v for v in latencies if isinstance(v, (int, float))]
    if not values:
        return 0
    return int(median(values))


def guardrail_arn(*, region: str, account: str, identifier: str) -> str:
    """ARN for a guardrail id. Passes an ARN straight through."""
    if identifier.startswith("arn:"):
        return identifier
    return f"arn:aws:bedrock:{region}:{account}:guardrail/{identifier}"


def grounding_content_blocks(policy_text: str, prompt: str) -> list[dict[str, Any]]:
    """The user-turn content blocks for a guarded, grounded Converse call.

    Wrapping both blocks in ``guardContent`` scopes the guardrail to this turn:
    once any ``guardContent`` block is present, Converse evaluates only those
    blocks, so the system prompt is never assessed.

    The qualifiers are what switch on the contextual grounding check. The policy
    is the ``grounding_source`` the reply is scored against and the user's
    message is the ``query`` it is scored for relevance against.

    Note the model receives both blocks as ordinary content. There is no way to
    give Converse a grounding source the model cannot see, so with the guardrail
    on the bot also gets the policy text in front of it.
    """
    return [
        {"guardContent": {"text": {"text": policy_text, "qualifiers": ["grounding_source"]}}},
        # "query" alone would make this block visible to the contextual grounding
        # check and invisible to everything else, so the prompt would never be
        # screened on input and a filter like PROMPT_ATTACK (which is output-NONE
        # by config) could never fire at all. "guard_content" is what puts it in
        # front of the topic, word, PII and content policies as well.
        #
        # The grounding source above is deliberately left unguarded: it is
        # QuickCart's own policy text, and screening it would have the guardrail
        # block its own reference material on the refund wording it contains.
        {
            "guardContent": {
                "text": {"text": prompt, "qualifiers": ["query", "guard_content"]}
            }
        },
    ]
