#!/usr/bin/env python3
"""Check the guardrail config against the demo prompts before you present.

Answers the two questions that decide whether the guardrail is ready:

1. Does every attack prompt get caught?
2. Does a normal customer still get through?

The second one matters more. A guardrail that blocks all eight attacks and also
blocks "how long does standard delivery take" is worse than no guardrail, and
that failure is invisible if you only ever test attacks.

    uv run python scripts/smoke_guardrail.py --local
    uv run python scripts/smoke_guardrail.py --local --mode full
    uv run python scripts/smoke_guardrail.py --url "$API_URL"

``--local`` calls ``ApplyGuardrail`` directly with your own credentials and needs
no deploy, which is the mode to use while tuning topic definitions in
``guardrail-config.json``. ``--url`` hits ``GET /c1/guardrail-check`` on the
deployed API instead.

``--mode input`` (the default) only checks prompts, so no model is called.
``--mode full`` additionally asks the bot for a reply to every prompt that
survived the input check and grades it against ``c1/docs/policy.txt``. That second
stage is the only way to catch p6, whose question is perfectly legitimate and
whose reply is where the invented refund timing shows up.

Exits non-zero if an attack was missed, a benign prompt was blocked, or any check
errored. An errored check is counted as a failure on purpose: not knowing is not
the same as passing.
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
load_dotenv(ENV_PATH)

from c1 import config  # noqa: E402
from c1 import local_env  # noqa: E402

# GUARDRAIL_ID and the API key are outputs of the deployed stack, not .env
# values. Fill in whatever is not already set.
local_env.hydrate()

from c1.api.guardrail_check import (  # noqa: E402
    ACTION_INTERVENED,
    DEFAULT_FIXTURE,
    FAILING_VERDICTS,
    FIXTURE_ATTACKS,
    FIXTURE_BENIGN,
    MODE_FULL,
    MODE_INPUT,
    VERDICT_EXPECTED_PASS,
    VERDICT_OK,
    judge_row,
    load_fixture,
    run_check,
)

LABEL_COLUMN_WIDTH = 34
FILTER_COLUMN_WIDTH = 46
REQUEST_TIMEOUT_SECONDS = 120

NOTHING = "-"


def resolve_key(explicit: str) -> tuple[str, str]:
    """The API key plus a short description of where it came from."""
    if explicit:
        return explicit, "--api-key"
    if os.environ.get(config.API_KEY_VAR, "").strip():
        return config.resolve_api_key(), f"${config.API_KEY_VAR}"
    key = config.resolve_api_key()
    return key, f"read from {config.api_key_parameter_arn()}"


# --- transports -------------------------------------------------------------


def check_via_api(
    base_url: str, api_key: str, *, mode: str, fixture: str, tier: str
) -> dict[str, Any]:
    url = (
        f"{base_url.rstrip('/')}/c1/guardrail-check"
        f"?mode={mode}&fixture={fixture}"
    )
    request = urllib.request.Request(
        url,
        headers={"authorization": f"Bearer {api_key}", "x-model-tier": tier},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def check_locally(*, mode: str, fixture: str, tier: str) -> dict[str, Any]:
    """Run the endpoint's own logic in this process, against Bedrock directly."""
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "bedrock-runtime",
        config=Config(
            read_timeout=30,
            connect_timeout=5,
            # run_check does its own jittered backoff; botocore retrying
            # underneath would multiply the waits.
            retries={"max_attempts": 1},
            max_pool_connections=16,
        ),
    )

    def bot(prompt: str) -> dict[str, Any]:
        # Guardrail off: this grades the raw reply with ApplyGuardrail, so letting
        # the model apply the guardrail too would block it first and leave no
        # output to assess.
        from c1.agent.main import run_bot

        return run_bot(prompt, model_tier=tier, guardrail=config.GUARDRAIL_OFF)

    return run_check(
        client=client,
        guardrail_id=config.guardrail_id(),
        guardrail_version=config.guardrail_version(),
        mode=mode,
        fixture=fixture,
        invoke_bot=bot if mode == MODE_FULL else None,
    )


# --- reporting --------------------------------------------------------------


def truncate(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "\u2026"


def rows_with_verdicts(report: dict[str, Any], *, mode: str, benign: bool) -> list[dict[str, Any]]:
    """Attach the expectation comparison to every result row."""
    by_id = {f["id"]: f for f in load_fixture(FIXTURE_BENIGN if benign else FIXTURE_ATTACKS)}
    rows = []
    for row in report.get("results", []):
        # The deployed endpoint echoes expectGuardrail, but fall back to the local
        # fixture so an older deploy still gets compared.
        merged = dict(row)
        fixture = by_id.get(row.get("id"), {})
        merged.setdefault("expectGuardrail", fixture.get("expectGuardrail"))
        verdict, expectation = judge_row(merged, mode=mode, benign=benign)
        merged["verdict"] = verdict
        merged["expectation"] = expectation
        rows.append(merged)
    return rows


def render_table(rows: list[dict[str, Any]]) -> str:
    headers = ("", "id", "label", "expected", "actual", "filters that fired", "ms")
    table = [headers]
    for row in rows:
        blocked = row["action"] == ACTION_INTERVENED
        actual = "blocked" if blocked else ("error" if row["action"] == "ERROR" else "passed")
        if blocked:
            actual = f"{actual} at {row.get('stage', '?')}"
        fired = ", ".join(row.get("firedFilters") or []) or NOTHING
        table.append(
            (
                "" if row["verdict"] in (VERDICT_OK, VERDICT_EXPECTED_PASS) else "!=",
                row.get("id", ""),
                truncate(row.get("label", ""), LABEL_COLUMN_WIDTH),
                row["expectation"],
                actual,
                truncate(fired, FILTER_COLUMN_WIDTH),
                str(row.get("latencyMs", 0)),
            )
        )

    widths = [max(len(str(cell)) for cell in column) for column in zip(*table, strict=True)]
    lines = []
    for index, cells in enumerate(table):
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def print_report(report: dict[str, Any], rows: list[dict[str, Any]], *, benign: bool) -> None:
    kind = "benign prompts" if benign else "attack prompts"
    print(f"{kind}: {len(rows)}")
    print()
    print(render_table(rows))

    odd = [r for r in rows if r["verdict"] not in (VERDICT_OK, VERDICT_EXPECTED_PASS)]
    if odd:
        print()
        for row in odd:
            print(f"  {row['verdict']:>14}  {row.get('id')}  {row.get('label')}")
    print()


def summarise(
    attacks: list[dict[str, Any]] | None,
    benign: list[dict[str, Any]] | None,
    *,
    mode: str,
    latencies: list[int],
) -> tuple[str, bool]:
    """The block that decides whether the guardrail is ready. Returns (text, ok)."""
    from c1.guardrails import median_latency_ms

    lines: list[str] = []
    ok = True

    if attacks is not None:
        # p6 is expected to survive the input check, so it is not a candidate
        # unless the reply is being graded too.
        candidates = [r for r in attacks if r["verdict"] != VERDICT_EXPECTED_PASS]
        caught = [r for r in candidates if r["action"] == ACTION_INTERVENED]
        deferred = len(attacks) - len(candidates)
        note = f"   ({deferred} expected to pass input, use --mode full)" if deferred else ""
        lines.append(f"  attacks blocked   {len(caught)}/{len(candidates)}{note}")
        if any(r["verdict"] in FAILING_VERDICTS for r in attacks):
            ok = False

    if benign is not None:
        false_positives = [r for r in benign if r["action"] == ACTION_INTERVENED]
        marker = "" if not false_positives else "   <-- real customers turned away"
        lines.append(f"  false positives   {len(false_positives)}/{len(benign)}{marker}")
        if any(r["verdict"] in FAILING_VERDICTS for r in benign):
            ok = False
    else:
        lines.append("  false positives   not checked (run with --benign)")

    if attacks is None:
        lines.insert(0, "  attacks blocked   not checked (run without --benign)")

    lines.append(f"  median latency    {median_latency_ms(latencies)} ms")
    lines.append(f"  mode              {mode}")
    return "\n".join(lines), ok


# --- entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="Base URL of the deployed HTTP API.")
    group.add_argument(
        "--local",
        action="store_true",
        help="Call ApplyGuardrail directly with your own credentials, no deploy.",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API key for the Authorization header. Defaults to $API_KEY, then to the SSM parameter in API_KEY_PARAMETER_ARN.",
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_INPUT, MODE_FULL),
        default=MODE_INPUT,
        help="input checks prompts only (no model). full also grades each reply.",
    )
    parser.add_argument(
        "--model-tier",
        choices=("high_volume", "frontier"),
        default="high_volume",
        help="Which model answers in --mode full. Ignored in input mode.",
    )
    fixtures = parser.add_mutually_exclusive_group()
    fixtures.add_argument(
        "--benign",
        action="store_true",
        help="Run c1/fixtures/benign.json only: the false-positive check.",
    )
    fixtures.add_argument(
        "--attacks",
        action="store_true",
        help="Run c1/fixtures/prompts.json only. Default runs both.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw reports as JSON, for piping into jq.",
    )
    args = parser.parse_args(argv)

    api_key = ""
    key_source = ""
    if args.url:
        try:
            api_key, key_source = resolve_key(args.api_key)
        except config.ApiKeyUnavailable as exc:
            parser.error(f"--url needs an API key. {exc}")

    if not config.guardrail_configured():
        parser.error(
            "GUARDRAIL_ID and GUARDRAIL_VERSION are not set. Put them in .env, or "
            "export them for one run."
        )

    # Default covers both questions; the flags narrow it to one.
    wanted = [FIXTURE_ATTACKS, FIXTURE_BENIGN]
    if args.benign:
        wanted = [FIXTURE_BENIGN]
    elif args.attacks:
        wanted = [FIXTURE_ATTACKS]

    target = "local ApplyGuardrail" if args.local else args.url
    print(f"Guardrail check against {target}")
    if args.url:
        print(f"  api key      {key_source}")
    print(f"  guardrail    {config.guardrail_id()} at {config.guardrail_version()}")
    print(f"  mode         {args.mode}")
    if args.mode == MODE_FULL:
        print(f"  model tier   {args.model_tier}")
    print()

    reports: dict[str, dict[str, Any]] = {}
    verdicts: dict[str, list[dict[str, Any]]] = {}
    latencies: list[int] = []

    for fixture in wanted:
        print(f"[{fixture}] running", file=sys.stderr)
        try:
            if args.local:
                report = check_locally(
                    mode=args.mode, fixture=fixture, tier=args.model_tier
                )
            else:
                report = check_via_api(
                    args.url,
                    api_key,
                    mode=args.mode,
                    fixture=fixture,
                    tier=args.model_tier,
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            print(f"\nHTTP {exc.code} from the API: {detail}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - a broken run must not traceback
            print(f"\n{fixture} check failed: {exc}", file=sys.stderr)
            return 2

        rows = rows_with_verdicts(report, mode=args.mode, benign=fixture == FIXTURE_BENIGN)
        reports[fixture] = report
        verdicts[fixture] = rows
        latencies.extend(r.get("latencyMs", 0) for r in rows)

        print_report(report, rows, benign=fixture == FIXTURE_BENIGN)

    summary, ok = summarise(
        verdicts.get(FIXTURE_ATTACKS),
        verdicts.get(FIXTURE_BENIGN),
        mode=args.mode,
        latencies=latencies,
    )
    print(summary)
    print()
    print("guardrail looks ready" if ok else "guardrail is NOT ready, see the != rows above")

    if args.json:
        print()
        print(json.dumps(reports, indent=2, ensure_ascii=False))

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
