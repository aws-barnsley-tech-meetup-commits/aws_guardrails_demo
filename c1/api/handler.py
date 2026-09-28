"""Lambda handler behind the shared HTTP API.

``GET  /health``              -> {"ok": true}
``GET  /c1/prompts``          -> the attack prompt fixture, for the UI dropdown
``GET  /c1/guardrail-check``  -> run every fixture prompt past the guardrail
``POST /c1/attack``           -> invoke the bot, judge the reply, report the verdict

``POST /c1/attack`` reads two request headers:

``x-model-tier: high_volume | frontier``
    Which model answers. Defaults to ``high_volume``.
``x-guardrail: on | off``
    Whether to attach the Bedrock guardrail. Defaults to ``off``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import boto3
from botocore.config import Config

from auth import AuthNotConfigured, AuthUnavailable, is_authorized
from judge import judge_reply, load_checks
from response import (
    FEEDBACK_JUDGE_ERROR,
    RESULT_ERROR,
    build_response,
)

from guardrail_check import (
    MODE_FULL,
    load_fixture,
    load_prompts,
    normalise_fixture,
    normalise_mode,
    run_check,
)

try:  # checkout
    from c1.config import (
        UnknownModelTier,
        guardrail_configured,
        guardrail_id,
        guardrail_version,
        normalise_guardrail_state,
        normalise_model_tier,
    )
except ImportError:  # deployment bundle, flat
    from config import (
        UnknownModelTier,
        guardrail_configured,
        guardrail_id,
        guardrail_version,
        normalise_guardrail_state,
        normalise_model_tier,
    )

#: Request headers that choose the model and the guardrail. Lowercase: API
#: Gateway lowercases header names before handing them over.
HEADER_MODEL_TIER = "x-model-tier"
HEADER_GUARDRAIL = "x-guardrail"

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Loaded once per container: neither file changes at runtime.
CHECKS = load_checks()
PROMPTS = load_prompts()

_agentcore_client = None
_bedrock_runtime_client = None


def _agentcore():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client(
            "bedrock-agentcore",
            config=Config(
                read_timeout=22, connect_timeout=5, retries={"max_attempts": 1}
            ),
        )
    return _agentcore_client


def _bedrock_runtime():
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        _bedrock_runtime_client = boto3.client(
            "bedrock-runtime",
            config=Config(
                read_timeout=15,
                connect_timeout=5,
                # guardrail_check does its own jittered backoff, so botocore
                # retrying underneath would multiply the waits.
                retries={"max_attempts": 1},
                max_pool_connections=16,
            ),
        )
    return _bedrock_runtime_client


def _json(
    status: int,
    body: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {"content-type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, ensure_ascii=False),
    }


def _authorize(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return an error response when the caller may not proceed, else None."""
    try:
        authorized = is_authorized(event.get("headers"))
    except AuthNotConfigured:
        logger.error("API key parameter ARN is not configured; refusing the request")
        return _json(503, {"message": "Authorization is not configured on this API."})
    except AuthUnavailable:
        logger.exception("could not read the API key parameter")
        return _json(503, {"message": "Authorization is temporarily unavailable."})

    if not authorized:
        return _json(
            401,
            {"message": "Send the API key in the Authorization header."},
            {"www-authenticate": "Bearer"},
        )
    return None


def _route_key(event: dict[str, Any]) -> str:
    """Normalise an API Gateway HTTP API v2 event into ``"METHOD /path"``."""
    route_key = event.get("routeKey")
    if isinstance(route_key, str) and route_key != "$default":
        return route_key
    http = event.get("requestContext", {}).get("http", {})
    return f"{http.get('method', '')} {http.get('path', '')}"


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("body must be a JSON object")
    return parsed


def _header(event: dict[str, Any], name: str) -> str | None:
    """Case-insensitive header lookup. API Gateway lowercases, clients vary."""
    headers = event.get("headers") or {}
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def request_options(event: dict[str, Any]) -> tuple[str, str]:
    """The model tier and guardrail state this request asked for."""
    tier = normalise_model_tier(_header(event, HEADER_MODEL_TIER))
    guardrail = normalise_guardrail_state(_header(event, HEADER_GUARDRAIL))
    return tier, guardrail


def invoke_bot(prompt: str, *, model_tier: str, guardrail: str) -> dict[str, Any]:
    """Invoke the AgentCore Runtime once and normalise its payload."""
    response = _agentcore().invoke_agent_runtime(
        agentRuntimeArn=os.environ["AGENT_RUNTIME_ARN"],
        # AgentCore requires at least 33 characters. Fresh id per request keeps
        # every attack a clean single turn with no shared state.
        runtimeSessionId=uuid.uuid4().hex + uuid.uuid4().hex,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(
            {"prompt": prompt, "modelTier": model_tier, "guardrail": guardrail}
        ).encode("utf-8"),
    )

    body = response["response"].read()
    if isinstance(body, bytes):
        body = body.decode("utf-8")

    # Fall back to what the caller asked for if the runtime does not echo it.
    fallback = {
        "botResponse": "",
        "latencyMs": 0,
        "modelTier": model_tier,
        "modelId": "",
        "guardrail": guardrail,
        "blocked": False,
        "firedFilters": [],
        "guardrailStage": None,
    }

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {**fallback, "botResponse": str(body).strip()}

    if not isinstance(payload, dict):
        return {**fallback, "botResponse": str(payload)}

    fired = payload.get("firedFilters")
    return {
        "botResponse": str(payload.get("botResponse", "")),
        "latencyMs": int(payload.get("latencyMs") or 0),
        "modelTier": str(payload.get("modelTier") or model_tier),
        "modelId": str(payload.get("modelId") or ""),
        "guardrail": str(payload.get("guardrail") or guardrail),
        "blocked": bool(payload.get("blocked")),
        "firedFilters": [str(f) for f in fired] if isinstance(fired, list) else [],
        "guardrailStage": payload.get("guardrailStage") or None,
    }


def _attack(event: dict[str, Any]) -> dict[str, Any]:
    try:
        body = _parse_body(event)
    except (json.JSONDecodeError, ValueError) as exc:
        return _json(400, {"message": f"Invalid JSON body: {exc}"})

    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return _json(400, {"message": 'Body must include a non-empty "prompt".'})

    try:
        model_tier, guardrail = request_options(event)
    except (UnknownModelTier, ValueError) as exc:
        return _json(400, {"message": str(exc)})

    bot = invoke_bot(prompt, model_tier=model_tier, guardrail=guardrail)

    # A blocked turn never reached the model's own words, so there is nothing for
    # the judge to score. Skipping it also keeps the demo honest: the guardrail
    # gets credit for the stop, not the judge.
    if bot["blocked"]:
        verdicts: list[dict[str, Any]] | None = None
    else:
        try:
            verdicts = judge_reply(prompt, bot["botResponse"], CHECKS)
        except Exception:  # noqa: BLE001 - a judge failure is a reportable result
            logger.exception("judge invocation failed")
            verdicts = None

    return _json(
        200,
        build_response(
            prompt=prompt,
            bot_response=bot["botResponse"],
            latency_ms=bot["latencyMs"],
            checks=CHECKS,
            verdicts=verdicts,
            model_tier=bot["modelTier"],
            model_id=bot["modelId"],
            guardrail=bot["guardrail"],
            blocked=bot["blocked"],
            fired_filters=bot["firedFilters"],
            guardrail_stage=bot["guardrailStage"],
        ),
    )


def _prompts() -> dict[str, Any]:
    """Serve the fixture so the UI dropdown has one source of truth."""
    return _json(200, {"prompts": PROMPTS})


def _guardrail_check(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    try:
        mode = normalise_mode(params.get("mode"))
        fixture = normalise_fixture(params.get("fixture"))
        model_tier, _ = request_options(event)
    except (UnknownModelTier, ValueError) as exc:
        return _json(400, {"message": str(exc)})

    if not guardrail_configured():
        return _json(
            503,
            {"message": "GUARDRAIL_ID and GUARDRAIL_VERSION are not configured."},
        )

    def bot(prompt: str) -> dict[str, Any]:
        # Guardrail off: this path grades the raw reply with ApplyGuardrail, so
        # letting the runtime apply the guardrail too would block it first and
        # there would be no output left to assess.
        return invoke_bot(prompt, model_tier=model_tier, guardrail="off")

    return _json(
        200,
        run_check(
            client=_bedrock_runtime(),
            guardrail_id=guardrail_id(),
            guardrail_version=guardrail_version(),
            mode=mode,
            fixture=fixture,
            prompts=load_fixture(fixture),
            invoke_bot=bot if mode == MODE_FULL else None,
        ),
    )


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    route = _route_key(event)

    # Deliberately open: /health proves the deploy works and leaks nothing.
    if route.startswith("GET ") and route.endswith("/health"):
        return _json(200, {"ok": True})

    if route.startswith("GET ") and route.endswith("/c1/prompts"):
        denied = _authorize(event)
        if denied is not None:
            return denied
        return _prompts()

    if route.startswith("GET ") and route.endswith("/c1/guardrail-check"):
        denied = _authorize(event)
        if denied is not None:
            return denied
        try:
            return _guardrail_check(event)
        except Exception:  # noqa: BLE001
            logger.exception("guardrail check failed")
            return _json(502, {"message": "Guardrail check failed. See CloudWatch logs."})

    if route.startswith("POST ") and route.endswith("/c1/attack"):
        denied = _authorize(event)
        if denied is not None:
            return denied
        try:
            return _attack(event)
        except Exception:  # noqa: BLE001
            logger.exception("attack request failed")
            return _json(
                502,
                {
                    "result": RESULT_ERROR,
                    "feedback": FEEDBACK_JUDGE_ERROR,
                    "message": "Upstream failure. See CloudWatch logs.",
                },
            )

    return _json(404, {"message": f"No route for {route}"})
