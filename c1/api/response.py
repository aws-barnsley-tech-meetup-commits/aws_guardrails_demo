"""Builds the ``POST /c1/attack`` response body.

Kept separate from the Lambda handler so it can be unit tested and reused by the
local smoke runner without touching AWS.
"""

from __future__ import annotations

from typing import Any, Sequence

FEEDBACK_HELD = (
    "The bot held. Try another angle: role-play, translation, or a format trick."
)
FEEDBACK_JUDGE_ERROR = "Judge could not evaluate this reply."
REASON_NOT_EVALUATED = "Not evaluated."
REASON_NOT_REACHED = "Not evaluated: the guardrail blocked this turn."

RESULT_HELD = "held"
RESULT_BROKEN = "broken"
RESULT_BLOCKED = "blocked"
RESULT_ERROR = "error"

#: Every value ``result`` can take.
RESULTS = (RESULT_HELD, RESULT_BROKEN, RESULT_BLOCKED, RESULT_ERROR)


def _check_rows(
    checks: Sequence[dict[str, Any]],
    verdicts: Sequence[dict[str, Any]] | None,
    *,
    missing_reason: str,
) -> list[dict[str, Any]]:
    """One row per check, in catalogue order.

    ``label`` and ``rule`` always come from ``checks.json``; the judge only ever
    supplies ``broken`` and ``reason``.
    """
    by_id = {verdict["id"]: verdict for verdict in (verdicts or [])}
    rows: list[dict[str, Any]] = []
    for check in checks:
        verdict = by_id.get(check["id"])
        rows.append(
            {
                "id": check["id"],
                "label": check["label"],
                # Optional: the checks used to carry a rule number pointing at a
                # numbered system prompt. The prompt is prose now, so the field
                # may be absent. None rather than omitted keeps the shape stable.
                "rule": check.get("rule"),
                "broken": bool(verdict["broken"]) if verdict else False,
                "reason": (verdict.get("reason") or "") if verdict else missing_reason,
            }
        )
    return rows


def broken_phrase(row: dict[str, Any]) -> str:
    """One clause of the broken feedback line.

    Names the rule number when the catalogue still has one, otherwise just the
    label. Writing "Broke rule None" would be worse than dropping the number.
    """
    rule = row.get("rule")
    if rule in (None, ""):
        return f"Broke: {row['label']}."
    return f"Broke rule {rule}: {row['label']}."


def blocked_feedback(fired_filters: Sequence[str], stage: str | None) -> str:
    """One line naming where the guardrail fired and which filters did it."""
    where = f" on {stage}" if stage else ""
    if fired_filters:
        return f"Blocked by the guardrail{where}: {', '.join(fired_filters)}."
    # stop_reason said intervened but no trace arrived. Say so rather than
    # implying nothing fired.
    return f"Blocked by the guardrail{where}. Filter detail unavailable."


def build_response(
    *,
    prompt: str,
    bot_response: str,
    latency_ms: int,
    checks: Sequence[dict[str, Any]],
    verdicts: Sequence[dict[str, Any]] | None,
    model_tier: str = "",
    model_id: str = "",
    guardrail: str = "off",
    blocked: bool = False,
    fired_filters: Sequence[str] = (),
    guardrail_stage: str | None = None,
) -> dict[str, Any]:
    """Assemble the ``POST /c1/attack`` body.

    Three outcomes, in precedence order:

    1. ``blocked`` - the guardrail stopped the turn, so the judge never ran and
       ``botResponse`` is the guardrail's own message.
    2. ``verdicts is None`` - the judge failed.
    3. otherwise the judge's verdicts decide ``broken`` or ``held``.
    """
    if blocked:
        result = RESULT_BLOCKED
        feedback = blocked_feedback(fired_filters, guardrail_stage)
        rows = _check_rows(checks, None, missing_reason=REASON_NOT_REACHED)
    elif verdicts is None:
        result = RESULT_ERROR
        feedback = FEEDBACK_JUDGE_ERROR
        rows = _check_rows(checks, None, missing_reason=REASON_NOT_EVALUATED)
    else:
        rows = _check_rows(checks, verdicts, missing_reason=REASON_NOT_EVALUATED)
        broken_rows = [row for row in rows if row["broken"]]
        if broken_rows:
            result = RESULT_BROKEN
            feedback = " ".join(broken_phrase(row) for row in broken_rows)
        else:
            result = RESULT_HELD
            feedback = FEEDBACK_HELD

    return {
        "result": result,
        "feedback": feedback,
        "checks": rows,
        "prompt": prompt,
        "botResponse": bot_response,
        "latencyMs": latency_ms,
        # Which model and guardrail produced this, so the UI can label it.
        "modelTier": model_tier,
        "modelId": model_id,
        "guardrail": guardrail,
        # Always present so the UI does not have to branch on shape.
        "firedFilters": list(fired_filters),
        "guardrailStage": guardrail_stage,
    }
