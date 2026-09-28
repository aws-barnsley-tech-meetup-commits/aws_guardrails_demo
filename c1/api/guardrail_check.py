"""``GET /c1/guardrail-check``: does the guardrail actually catch these prompts?

Runs every prompt in ``prompts.json`` past the guardrail and reports what fired,
so you can tune ``guardrail-config.json`` without burning a live demo slot.

Two modes:

``mode=input`` (default)
    One ``ApplyGuardrail`` call per prompt with ``source=INPUT``. No model is
    called, so it is cheap and fast, and it tells you which prompts never reach
    the bot at all.

``mode=full``
    Additionally, for prompts that survive the input check, invoke the bot and
    run ``ApplyGuardrail`` with ``source=OUTPUT``, passing the policy as the
    grounding source and the prompt as the query. This is the only way to catch
    a reply that is perfectly on-topic but factually invented, such as a made-up
    refund timing.

Requests run five at a time with exponential backoff on throttling. Five is a
deliberate ceiling rather than a tuned number: the Lambda has 29 seconds and
``mode=full`` spends most of that waiting on the model.
"""

from __future__ import annotations

import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:  # checkout
    from c1 import guardrails as _guardrails
except ImportError:  # deployment bundle, flat
    import guardrails as _guardrails

logger = logging.getLogger("c1.guardrail_check")

MODE_INPUT = "input"
MODE_FULL = "full"
MODES = (MODE_INPUT, MODE_FULL)
DEFAULT_MODE = MODE_INPUT

#: Which fixture to run. The attack prompts are the default; the benign set is
#: the false-positive check.
FIXTURE_ATTACKS = "attacks"
FIXTURE_BENIGN = "benign"
FIXTURES = (FIXTURE_ATTACKS, FIXTURE_BENIGN)
DEFAULT_FIXTURE = FIXTURE_ATTACKS

#: How many prompts are in flight at once.
CONCURRENCY = 5

#: Retry budget for throttling. Four attempts with jittered backoff is about
#: 3.5 seconds of waiting in the worst case, which the 29s Lambda can absorb.
MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 0.25

#: Errors worth retrying. Anything else is a config problem and retrying it just
#: burns the request budget.
RETRYABLE_ERROR_CODES = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException",
     "InternalServerException"}
)

ACTION_INTERVENED = _guardrails.INTERVENED
ACTION_NONE = "NONE"

_HERE = Path(__file__).resolve().parent

_PROMPTS_CANDIDATES = (
    _HERE / "prompts.json",
    _HERE.parent / "fixtures" / "prompts.json",
)
_BENIGN_CANDIDATES = (
    _HERE / "benign.json",
    _HERE.parent / "fixtures" / "benign.json",
)
_POLICY_CANDIDATES = (
    _HERE / "policy.txt",
    _HERE.parent / "docs" / "policy.txt",
)


def _first_existing(candidates: Sequence[Path], what: str) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"{what} not found. Looked in: {searched}")


def load_prompts(path: str | Path | None = None) -> list[dict[str, Any]]:
    """The attack prompt fixture, as served by ``GET /c1/prompts``."""
    target = Path(path) if path else _first_existing(_PROMPTS_CANDIDATES, "prompts.json")
    return json.loads(target.read_text(encoding="utf-8"))


def load_benign(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Legitimate customer questions that must not be blocked.

    Checking these is the other half of the job: a guardrail that blocks every
    attack and also every real customer is worse than no guardrail.
    """
    target = Path(path) if path else _first_existing(_BENIGN_CANDIDATES, "benign.json")
    return json.loads(target.read_text(encoding="utf-8"))


def load_fixture(name: str) -> list[dict[str, Any]]:
    return load_benign() if name == FIXTURE_BENIGN else load_prompts()


def normalise_fixture(value: str | None) -> str:
    """Coerce the ``fixture`` query parameter. Unknown values are an error."""
    if value is None or not value.strip():
        return DEFAULT_FIXTURE
    fixture = value.strip().lower()
    if fixture not in FIXTURES:
        raise ValueError(
            f"unknown fixture {value!r}. Expected one of: {', '.join(FIXTURES)}"
        )
    return fixture


def load_policy(path: str | Path | None = None) -> str:
    """The grounding source for the contextual grounding check."""
    target = Path(path) if path else _first_existing(_POLICY_CANDIDATES, "policy.txt")
    return target.read_text(encoding="utf-8")


def normalise_mode(value: str | None) -> str:
    """Coerce the ``mode`` query parameter. Unknown values are an error."""
    if value is None or not value.strip():
        return DEFAULT_MODE
    mode = value.strip().lower()
    if mode not in MODES:
        raise ValueError(f"unknown mode {value!r}. Expected one of: {', '.join(MODES)}")
    return mode


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return str((response.get("Error") or {}).get("Code") or "")
    return exc.__class__.__name__


def call_with_backoff(
    operation: Callable[[], Any],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    base_delay: float = BASE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> Any:
    """Run ``operation``, retrying throttles with jittered exponential backoff.

    Jitter matters because five workers throttled by the same burst would
    otherwise retry in lockstep and throttle each other again.
    """
    last_error: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if _error_code(exc) not in RETRYABLE_ERROR_CODES:
                raise
            last_error = exc
            if attempt == max_attempts - 1:
                break
            delay = base_delay * (2**attempt) * (0.5 + rand())
            logger.warning(
                "throttled (attempt %d/%d), retrying in %.2fs", attempt + 1, max_attempts, delay
            )
            sleep(delay)
    assert last_error is not None
    raise last_error


def apply_guardrail(
    client: Any,
    *,
    guardrail_id: str,
    guardrail_version: str,
    source: str,
    content: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """One ApplyGuardrail call, retried on throttling."""
    return call_with_backoff(
        lambda: client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
            source=source,
            content=list(content),
        )
    )


def input_content(prompt: str) -> list[dict[str, Any]]:
    """Content blocks for an INPUT check: just the prompt."""
    return [{"text": {"text": prompt}}]


def output_content(policy: str, prompt: str, reply: str) -> list[dict[str, Any]]:
    """Content blocks for an OUTPUT check with contextual grounding.

    Three blocks: the policy the reply is graded against, the question it is
    graded for relevance to, and the reply itself. Only the unqualified block is
    the thing being assessed for content; the qualified two are the reference.
    """
    return [
        {"text": {"text": policy, "qualifiers": ["grounding_source"]}},
        {"text": {"text": prompt, "qualifiers": ["query"]}},
        {"text": {"text": reply}},
    ]


def _result_row(
    fixture: Mapping[str, Any],
    *,
    stage: str,
    response: Mapping[str, Any] | None,
    latency_ms: int,
    reply: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": fixture.get("id", ""),
        "label": fixture.get("label", ""),
        "stage": stage,
        "action": ACTION_NONE if response is None else response.get("action", ACTION_NONE),
        "firedFilters": _guardrails.fired_filters((response or {}).get("assessments")),
        "latencyMs": latency_ms,
    }
    # Carried through so a caller can compare without re-reading the fixture.
    if fixture.get("expectGuardrail"):
        row["expectGuardrail"] = fixture["expectGuardrail"]
    if reply is not None:
        row["botResponse"] = reply
    if error is not None:
        row["action"] = "ERROR"
        row["error"] = error
    return row


def check_one(
    fixture: Mapping[str, Any],
    *,
    client: Any,
    guardrail_id: str,
    guardrail_version: str,
    mode: str,
    policy: str,
    invoke_bot: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one prompt through the guardrail and, in full mode, the bot too."""
    prompt = fixture.get("prompt", "")
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        response = apply_guardrail(
            client,
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
            source="INPUT",
            content=input_content(prompt),
        )
    except Exception as exc:  # noqa: BLE001 - one bad prompt must not sink the run
        logger.exception("input check failed for %s", fixture.get("id"))
        return _result_row(
            fixture, stage=_guardrails.STAGE_INPUT, response=None,
            latency_ms=elapsed(), error=f"{_error_code(exc)}: {exc}",
        )

    # Stop here if the input was blocked, or if that is all that was asked for.
    if mode != MODE_FULL or _guardrails.intervened(response):
        return _result_row(
            fixture, stage=_guardrails.STAGE_INPUT, response=response, latency_ms=elapsed()
        )

    if invoke_bot is None:
        raise ValueError("mode=full needs an invoke_bot callable")

    try:
        bot = invoke_bot(prompt)
        reply = str(bot.get("botResponse", ""))
    except Exception as exc:  # noqa: BLE001
        logger.exception("bot invocation failed for %s", fixture.get("id"))
        return _result_row(
            fixture, stage=_guardrails.STAGE_OUTPUT, response=None,
            latency_ms=elapsed(), error=f"{_error_code(exc)}: {exc}",
        )

    try:
        out_response = apply_guardrail(
            client,
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
            source="OUTPUT",
            content=output_content(policy, prompt, reply),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("output check failed for %s", fixture.get("id"))
        return _result_row(
            fixture, stage=_guardrails.STAGE_OUTPUT, response=None, latency_ms=elapsed(),
            reply=reply, error=f"{_error_code(exc)}: {exc}",
        )

    return _result_row(
        fixture, stage=_guardrails.STAGE_OUTPUT, response=out_response,
        latency_ms=elapsed(), reply=reply,
    )


def run_check(
    *,
    client: Any,
    guardrail_id: str,
    guardrail_version: str,
    mode: str = DEFAULT_MODE,
    fixture: str = DEFAULT_FIXTURE,
    prompts: Sequence[Mapping[str, Any]] | None = None,
    policy: str | None = None,
    invoke_bot: Callable[[str], dict[str, Any]] | None = None,
    concurrency: int = CONCURRENCY,
) -> dict[str, Any]:
    """Check every prompt and summarise. Results keep fixture order."""
    fixtures = list(prompts if prompts is not None else load_fixture(fixture))
    grounding = policy if policy is not None else (load_policy() if mode == MODE_FULL else "")

    def work(fixture: Mapping[str, Any]) -> dict[str, Any]:
        return check_one(
            fixture,
            client=client,
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
            mode=mode,
            policy=grounding,
            invoke_bot=invoke_bot,
        )

    if fixtures:
        with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(fixtures)))) as pool:
            # executor.map preserves input order, which keeps the report stable.
            results = list(pool.map(work, fixtures))
    else:
        results = []

    blocked = sum(1 for row in results if row["action"] == ACTION_INTERVENED)
    errors = sum(1 for row in results if row["action"] == "ERROR")

    return {
        "guardrailId": guardrail_id,
        "guardrailVersion": guardrail_version,
        "mode": mode,
        "fixture": fixture,
        "blocked": blocked,
        "passed": len(results) - blocked - errors,
        "errors": errors,
        "medianLatencyMs": _guardrails.median_latency_ms([r["latencyMs"] for r in results]),
        "results": results,
    }


# --- comparing against what the fixture expected ---------------------------

VERDICT_OK = "ok"
VERDICT_MISS = "MISS"
VERDICT_FALSE_POSITIVE = "FALSE POSITIVE"
VERDICT_OTHER_FILTER = "other filter"
VERDICT_EXPECTED_PASS = "expected pass"
VERDICT_ERROR = "ERROR"

#: Verdicts that should fail the run. A miss and a false positive are the two
#: real failures; an error is included because an unverified check must not read
#: as green.
FAILING_VERDICTS = frozenset({VERDICT_MISS, VERDICT_FALSE_POSITIVE, VERDICT_ERROR})


def filter_matches(fired: Sequence[str], expected: Sequence[str]) -> bool:
    """Whether any fired filter satisfies the expectation.

    ``policy:*`` matches any rule inside that policy, which is how the fixtures
    say "caught by the word policy" without naming which word.
    """
    for want in expected:
        if want.endswith(":*"):
            prefix = want[:-1]
            if any(label.startswith(prefix) for label in fired):
                return True
        elif want in fired:
            return True
    return False


def judge_row(row: Mapping[str, Any], *, mode: str, benign: bool) -> tuple[str, str]:
    """Compare one result row against what the fixture expected.

    Returns ``(verdict, expectation)`` where expectation is a short label for the
    report, such as ``"blocked at input"`` or ``"not blocked"``.
    """
    action = row.get("action")
    fired = row.get("firedFilters") or []

    if action == VERDICT_ERROR:
        return VERDICT_ERROR, "not blocked" if benign else "blocked"

    if benign:
        # Anything blocked here is a real customer being turned away.
        if action == ACTION_INTERVENED:
            return VERDICT_FALSE_POSITIVE, "not blocked"
        return VERDICT_OK, "not blocked"

    expectation = row.get("expectGuardrail") or {}
    want_stage = expectation.get("stage") or _guardrails.STAGE_INPUT
    want_filters = expectation.get("anyOf") or []
    label = f"blocked at {want_stage}"

    # A prompt expected to break only on the reply cannot be caught by an input
    # check. In input mode that is the correct outcome, not a miss.
    if want_stage == _guardrails.STAGE_OUTPUT and mode != MODE_FULL:
        if action == ACTION_INTERVENED:
            return VERDICT_OTHER_FILTER, "passes input"
        return VERDICT_EXPECTED_PASS, "passes input"

    if action != ACTION_INTERVENED:
        return VERDICT_MISS, label
    if row.get("stage") != want_stage:
        return VERDICT_OTHER_FILTER, label
    if want_filters and not filter_matches(fired, want_filters):
        return VERDICT_OTHER_FILTER, label
    return VERDICT_OK, label
