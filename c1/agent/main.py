"""QuickCart support bot for challenge C1, "Break the Bot".

By default nothing stands between the presenter and a policy violation except
the system prompt in ``shared/system_prompt.txt``. A request can ask for a
Bedrock guardrail instead, which is the before-and-after the demo is built on.

Single turn, no tools, no memory.

Per-request knobs, both chosen by the API from request headers:

* ``modelTier`` picks which model answers: ``high_volume`` (``BOT_MODEL_ID``) or
  ``frontier`` (``BOT_MODEL_ID_PREMIUM``). The cheaper tier usually folds first.
* ``guardrail`` attaches ``GUARDRAIL_ID`` at ``GUARDRAIL_VERSION``.

Two entry paths:

* AgentCore Runtime invokes ``python main.py`` with no arguments, which starts
  the ``BedrockAgentCoreApp`` HTTP server and serves ``/invocations``.
* A developer runs ``uv run python c1/agent/main.py "<prompt>"``, which prints
  the same JSON payload the runtime would return. ``--frontier`` and
  ``--guardrail`` switch the knobs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

# This file runs from two layouts: flat at the root of the AgentCore code zip,
# and at c1/agent/main.py in a checkout. Running the script directly puts only
# c1/agent on sys.path, so add the repo root when there is one.
for _candidate in Path(__file__).resolve().parents:
    if (_candidate / "pyproject.toml").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

try:  # checkout
    from c1 import config as _config
    from c1 import guardrails as _guardrails
except ImportError:  # deployment bundle, flat
    import config as _config
    import guardrails as _guardrails

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("c1.agent")

_HERE = Path(__file__).resolve().parent


def _resolve(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"file not found. Looked in: {searched}")


def system_prompt_path() -> Path:
    """Flat in the deployment bundle, under shared/ in a checkout."""
    return _resolve(
        _HERE / "system_prompt.txt",
        _HERE.parents[1] / "shared" / "system_prompt.txt",
    )


def policy_path() -> Path:
    """The grounding source for the contextual grounding check."""
    return _resolve(
        _HERE / "policy.txt",
        _HERE.parent / "docs" / "policy.txt",
    )


SYSTEM_PROMPT = system_prompt_path().read_text(encoding="utf-8")

#: Re-exported so callers resolve models through one code path.
bot_model_id = _config.bot_model_id
bot_model_id_premium = _config.bot_model_id_premium


def policy_text() -> str:
    """Read the grounding policy lazily: only the guarded path needs it."""
    return policy_path().read_text(encoding="utf-8")


class _GuardrailTracingBedrockModel(BedrockModel):
    """A BedrockModel that keeps the last guardrail trace it saw.

    Strands reads the Converse guardrail trace internally to decide redaction,
    but does not put it on ``AgentResult``. It does emit it as a metadata stream
    event, so tapping ``stream`` is the supported way to get at it. Without the
    trace there is no way to say which filters fired or whether the guardrail
    caught the prompt or the reply.
    """

    def __init__(self, **model_config: Any) -> None:
        super().__init__(**model_config)
        self.last_guardrail_trace: dict[str, Any] | None = None

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        async for event in super().stream(*args, **kwargs):
            if isinstance(event, dict):
                trace = (event.get("metadata") or {}).get("trace") or {}
                guardrail = trace.get("guardrail")
                if guardrail:
                    self.last_guardrail_trace = guardrail
            yield event


def _response_text(result: Any) -> str:
    """Pull the assistant text out of a Strands AgentResult."""
    message = getattr(result, "message", None) or {}
    blocks = message.get("content") or []
    text = "".join(block["text"] for block in blocks if isinstance(block, dict) and "text" in block)
    return text.strip() or str(result).strip()


def _build_model(tier: str, guardrail: str) -> _GuardrailTracingBedrockModel:
    config: dict[str, Any] = {"model_id": _config.model_id_for_tier(tier)}

    if guardrail == _config.GUARDRAIL_ON:
        config.update(
            guardrail_id=_config.guardrail_id(),
            guardrail_version=_config.guardrail_version(),
            guardrail_trace="enabled",
            # Leave the blocked text exactly as the guardrail wrote it. Strands
            # would otherwise swap in its own placeholder, and the guardrail's
            # own message is what the demo shows the audience.
            guardrail_redact_input=False,
            guardrail_redact_output=False,
        )

    return _GuardrailTracingBedrockModel(**config)


def run_bot(
    prompt: str,
    *,
    model_tier: str | None = None,
    guardrail: str | None = None,
) -> dict[str, Any]:
    """Answer one prompt and report what happened.

    A new ``Agent`` per call is what keeps this single turn with no memory:
    nothing carries over between invocations.
    """
    tier = _config.normalise_model_tier(model_tier)
    guardrail_state = _config.normalise_guardrail_state(guardrail)
    model = _build_model(tier, guardrail_state)

    if guardrail_state == _config.GUARDRAIL_ON:
        # Wrapping the turn in guardContent scopes the guardrail to it: Converse
        # then evaluates only guardContent blocks, so the system prompt is never
        # assessed. The qualifiers switch on the contextual grounding check.
        turn: Any = _guardrails.grounding_content_blocks(policy_text(), prompt)
    else:
        turn = prompt

    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[],
        # Suppress the default streaming printer so stdout stays clean for the
        # local CLI, where the only output should be the JSON payload.
        callback_handler=None,
    )

    started = time.perf_counter()
    result = agent(turn)
    latency_ms = int((time.perf_counter() - started) * 1000)

    payload: dict[str, Any] = {
        "botResponse": _response_text(result),
        "latencyMs": latency_ms,
        "modelTier": tier,
        "modelId": model.get_config()["model_id"],
        "guardrail": guardrail_state,
        "blocked": False,
        "firedFilters": [],
        "guardrailStage": None,
    }

    if guardrail_state != _config.GUARDRAIL_ON:
        return payload

    fired, stage = _guardrails.trace_filters(model.last_guardrail_trace)
    blocked = getattr(result, "stop_reason", None) == "guardrail_intervened"
    payload["blocked"] = blocked
    payload["firedFilters"] = fired
    # stop_reason is the reliable signal; the trace is what names the filters.
    # If the trace never arrived, still report the block rather than pretend the
    # bot answered.
    payload["guardrailStage"] = stage if blocked else None
    if blocked and not fired:
        logger.warning("guardrail intervened but no trace arrived; filters unknown")

    return payload


app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """AgentCore Runtime entry point.

    Input ``{"prompt": "...", "modelTier": "...", "guardrail": "on|off"}``.
    """
    payload = payload or {}
    prompt = (payload.get("prompt") or "").strip()
    tier = _config.normalise_model_tier(payload.get("modelTier"))
    guardrail = _config.normalise_guardrail_state(payload.get("guardrail"))

    if not prompt:
        return {
            "botResponse": "",
            "latencyMs": 0,
            "modelTier": tier,
            "modelId": _config.model_id_for_tier(tier),
            "guardrail": guardrail,
            "blocked": False,
            "firedFilters": [],
            "guardrailStage": None,
        }

    logger.info("tier=%s guardrail=%s prompt=%s", tier, guardrail, prompt)
    return run_bot(prompt, model_tier=tier, guardrail=guardrail)


def _hydrate_from_stack() -> None:
    """Fill GUARDRAIL_ID and friends from the deployed stack, for local runs only.

    Only reachable from the CLI path. In the deployment bundle the import fails
    (there is no ``c1`` package there) and every variable is injected anyway.
    """
    try:
        from c1 import local_env
    except ImportError:
        return
    local_env.hydrate()


def _main(argv: list[str]) -> int:
    _hydrate_from_stack()
    tier = _config.TIER_FRONTIER if "--frontier" in argv else _config.TIER_HIGH_VOLUME
    guardrail = _config.GUARDRAIL_ON if "--guardrail" in argv else _config.GUARDRAIL_OFF
    words = [a for a in argv if not a.startswith("--")]
    if not words:
        app.run()
        return 0
    result = run_bot(" ".join(words), model_tier=tier, guardrail=guardrail)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
