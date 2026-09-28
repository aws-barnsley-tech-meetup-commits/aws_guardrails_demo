#!/usr/bin/env python3
"""Run every prompt in c1/fixtures/prompts.json through the bot and report.

Two modes:

    uv run python scripts/smoke_c1.py --url https://abc123.execute-api.eu-west-2.amazonaws.com
    uv run python scripts/smoke_c1.py --local

``--url`` hits the deployed API. ``--local`` calls the agent and the judge
directly in this process, so you can sanity check the prompts and the checks
before spending a deploy. Both need Bedrock access; ``--local`` needs it from
your own credentials.

Two knobs, matching the request headers the API takes:

    --tier high_volume|frontier     which model answers
    --guardrail on|off              whether the Bedrock guardrail is attached

Running it twice, once with ``--guardrail off`` and once with ``on``, is the
before-and-after the demo is built on.

``.env`` is loaded on startup, so ``--local`` runs against the same model ids and
guardrail the stack deploys with. Under ``--url`` those values are ignored: the
deployed Lambda and runtime carry their own copies from deploy time.

Fixtures may carry an optional ``expect`` field. When present the result is
compared against it and mismatches are marked; when absent nothing is compared,
because these prompts are deliberately close calls against a language model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_PATH = ROOT / ".env"

# Same file infra/app.py deploys from, so a local run uses the same models.
# load_dotenv does not overwrite variables that are already set, which is the
# precedence we want: an exported BOT_MODEL_ID beats the one in .env.
load_dotenv(ENV_PATH)

from c1 import local_env  # noqa: E402

# The guardrail id and the API key belong to the deployed stack, so they are not
# in .env and cannot be. Fill them in from the stack outputs, without touching
# anything already set.
local_env.hydrate()

PROMPTS_PATH = ROOT / "c1" / "fixtures" / "prompts.json"

LABEL_COLUMN_WIDTH = 34
PROMPT_COLUMN_WIDTH = 44
REQUEST_TIMEOUT_SECONDS = 90

NOT_COMPARED = "-"


def resolve_key(explicit: str) -> str:
    """$API_KEY, or the SSM parameter the stack was deployed with."""
    if explicit:
        return explicit
    from c1 import config

    return config.resolve_api_key()


# --- transports -------------------------------------------------------------


def attack_via_api(
    base_url: str, prompt: str, api_key: str, *, tier: str, guardrail: str
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/c1/attack",
        data=json.dumps({"prompt": prompt}).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
            "x-model-tier": tier,
            "x-guardrail": guardrail,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return _transport_error(prompt, f"HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return _transport_error(prompt, str(exc))


def attack_locally(prompt: str, *, tier: str, guardrail: str) -> dict[str, Any]:
    from c1.agent.main import run_bot
    from c1.api.judge import judge_reply, load_checks
    from c1.api.response import build_response

    checks = load_checks()
    try:
        bot = run_bot(prompt, model_tier=tier, guardrail=guardrail)
    except Exception as exc:  # noqa: BLE001 - report, keep walking the fixture
        return _transport_error(prompt, f"agent failed: {exc}")

    if bot["blocked"]:
        verdicts = None  # the guardrail stopped the turn; nothing to judge
    else:
        try:
            verdicts = judge_reply(prompt, bot["botResponse"], checks)
        except Exception as exc:  # noqa: BLE001
            print(f"  judge failed: {exc}", file=sys.stderr)
            verdicts = None

    return build_response(
        prompt=prompt,
        bot_response=bot["botResponse"],
        latency_ms=bot["latencyMs"],
        checks=checks,
        verdicts=verdicts,
        model_tier=bot["modelTier"],
        model_id=bot["modelId"],
        guardrail=bot["guardrail"],
        blocked=bot["blocked"],
        fired_filters=bot["firedFilters"],
        guardrail_stage=bot["guardrailStage"],
    )


def _transport_error(prompt: str, message: str) -> dict[str, Any]:
    return {
        "result": "error",
        "feedback": message,
        "checks": [],
        "prompt": prompt,
        "botResponse": "",
        "latencyMs": 0,
        "modelTier": "",
        "modelId": "",
        "guardrail": "",
        "firedFilters": [],
        "guardrailStage": None,
    }


# --- reporting --------------------------------------------------------------


def truncate(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "\u2026"


def detail_column(body: dict[str, Any]) -> str:
    """What actually happened: fired guardrail filters, or broken checks."""
    fired = body.get("firedFilters") or []
    if fired:
        return ", ".join(fired)
    broken = [check["id"] for check in body.get("checks", []) if check.get("broken")]
    return ", ".join(broken) if broken else NOT_COMPARED


def local_config_summary() -> dict[str, str]:
    from c1 import config

    summary = {
        "high_volume": config.bot_model_id(),
        "frontier": config.bot_model_id_premium(),
        "judge": config.judge_model_id(),
    }
    if config.guardrail_configured():
        summary["guardrail"] = f"{config.guardrail_id()} at {config.guardrail_version()}"
    else:
        summary["guardrail"] = "not configured"
    return summary


def print_banner(args: argparse.Namespace, prompt_count: int) -> None:
    target = "local agent + judge" if args.local else args.url
    print(f"C1 smoke test against {target}: {prompt_count} prompts")
    print(f"  tier         {args.tier}")
    print(f"  guardrail    {args.guardrail}")

    if args.local:
        summary = local_config_summary()
        source = ".env" if ENV_PATH.is_file() else "environment"
        print(f"  config from  {source}")
        print(f"  bot model    {summary[args.tier]}")
        print(f"  judge model  {summary['judge']}")
        print(f"  guardrail id {summary['guardrail']}")
    else:
        # Naming the models from .env here would be a lie: the deployed Lambda
        # and runtime carry their own copies from deploy time.
        print("  models       whichever the deployed stack was built with")
    print()


def print_detail(row: dict[str, Any]) -> None:
    """Print one full exchange: prompt, verdict, the whole reply, and why.

    Nothing is truncated here. The point of this view is reading exactly what the
    bot said, and a leak can just as easily be in the last sentence.
    """
    body = row["body"]
    print("\u2500" * 78)
    print(f"ID        {row['id']}  {row['label']}")
    print(f"PROMPT    {row['prompt']}")
    if row["expect"] is not None:
        print(f"EXPECT    {row['expect']}")
    marker = "" if row["match"] else "   <-- differs from expect"
    print(f"RESULT    {row['result']}{marker}")
    print(f"MODEL     {body.get('modelTier') or '?'}  {body.get('modelId') or '?'}")
    print(f"GUARDRAIL {body.get('guardrail') or '?'}", end="")
    stage = body.get("guardrailStage")
    print(f"  (fired on {stage})" if stage else "")
    fired = body.get("firedFilters") or []
    if fired:
        print("FILTERS")
        for label in fired:
            print(f"  {label}")
    print(f"FEEDBACK  {body.get('feedback', '')}")
    print(f"LATENCY   {body.get('latencyMs', 0)} ms")

    reply = (body.get("botResponse") or "").strip()
    print("REPLY")
    for line in (reply.splitlines() or ["(empty)"]):
        print(f"  {line}")

    checks = body.get("checks") or []
    if checks:
        # When the guardrail blocked the turn or the judge failed, no check was
        # actually evaluated. Printing "held" for those would read on stage as
        # the bot having passed them.
        evaluated = row["result"] not in ("blocked", "error")
        print("CHECKS" if evaluated else "CHECKS (not evaluated)")
        for check in checks:
            if not evaluated:
                state = " n/a  "
            else:
                state = "BROKEN" if check.get("broken") else "held  "
            rule = check.get("rule")
            prefix = f"rule {rule}  " if rule not in (None, "") else ""
            print(f"  [{state}] {prefix}{check.get('id')}")
            reason = (check.get("reason") or "").strip()
            if reason and evaluated:
                print(f"           {reason}")
    print()


def render_table(rows: list[dict[str, Any]]) -> str:
    headers = ("", "id", "label", "prompt", "expect", "result", "fired / broken")
    table = [headers]
    for row in rows:
        table.append(
            (
                "" if row["match"] else "!=",
                row["id"],
                truncate(row["label"], LABEL_COLUMN_WIDTH),
                truncate(row["prompt"], PROMPT_COLUMN_WIDTH),
                NOT_COMPARED if row["expect"] is None else row["expect"],
                row["result"],
                row["detail"],
            )
        )

    widths = [max(len(str(cell)) for cell in column) for column in zip(*table, strict=True)]
    lines = []
    for index, cells in enumerate(table):
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def summarise(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    tally = "  ".join(f"{name}={count}" for name, count in sorted(counts.items()))

    compared = [row for row in rows if row["expect"] is not None]
    if not compared:
        return f"{len(rows)} prompts.  {tally}"
    matched = sum(1 for row in compared if row["match"])
    return (
        f"{len(rows)} prompts.  {tally}\n"
        f"{matched}/{len(compared)} matched expect. Rows marked != differ."
    )


# --- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="Base URL of the deployed HTTP API.")
    group.add_argument(
        "--local",
        action="store_true",
        help="Run the agent and judge in this process instead of calling the API.",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API key for the Authorization header. Defaults to $API_KEY, then to the SSM parameter in API_KEY_PARAMETER_ARN.",
    )
    parser.add_argument(
        "--tier",
        choices=("high_volume", "frontier"),
        default="high_volume",
        help="Which model answers. Sent as x-model-tier. Default high_volume.",
    )
    parser.add_argument(
        "--guardrail",
        choices=("on", "off"),
        default="off",
        help="Whether to attach the Bedrock guardrail. Sent as x-guardrail. "
        "Default off.",
    )
    parser.add_argument(
        "--show-replies",
        action="store_true",
        help="Print the full bot reply and the judge's reasoning for every check.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw API responses as a JSON array, for piping into jq.",
    )
    args = parser.parse_args(argv)

    if args.url:
        try:
            args.api_key = resolve_key(args.api_key)
        except Exception as exc:  # noqa: BLE001 - argparse reports it cleanly
            parser.error(f"--url needs an API key. {exc}")

    fixtures = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
    print_banner(args, len(fixtures))

    rows: list[dict[str, Any]] = []
    for index, fixture in enumerate(fixtures, start=1):
        prompt = fixture["prompt"]
        # Optional: the fixture may or may not declare what it expects.
        expect = fixture.get("expect")
        print(f"[{index}/{len(fixtures)}] {truncate(prompt, 60)}", file=sys.stderr)

        if args.local:
            body = attack_locally(prompt, tier=args.tier, guardrail=args.guardrail)
        else:
            body = attack_via_api(
                args.url, prompt, args.api_key, tier=args.tier, guardrail=args.guardrail
            )
        result = body.get("result", "error")

        rows.append(
            {
                "id": fixture.get("id", str(index)),
                "label": fixture.get("label", ""),
                "prompt": prompt,
                "expect": expect,
                "result": result,
                "detail": detail_column(body),
                # Nothing to mismatch against when the fixture says nothing.
                "match": expect is None or result == expect,
                "body": body,
            }
        )

    print()
    print(render_table(rows))
    print()
    print(summarise(rows))

    if args.show_replies:
        print()
        for row in rows:
            print_detail(row)

    if args.json:
        print()
        print(json.dumps([row["body"] for row in rows], indent=2, ensure_ascii=False))

    # A mismatch is information, not a build failure: the bot is a language
    # model and these prompts are meant to be close calls.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
