#!/usr/bin/env python3
"""CDK entry point. Loads .env, then creates the one stack.

The only thing ``.env`` has to supply is which models to run on. The guardrail
and the API key are created by the stack itself, so there is nothing to set up by
hand and nothing to keep in sync.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aws_cdk as cdk  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from c1.config import (  # noqa: E402
    ModelNotConfigured,
    bot_model_id,
    bot_model_id_premium,
    judge_model_id,
)
from infra.shared_stack import SharedStack  # noqa: E402

STACK_NAME = os.environ.get("STACK_NAME", "GuardrailsDemo")


def model_ids() -> tuple[str, str, str]:
    """Resolve every model id, or stop the deploy with a readable reason.

    Same resolver the agent and the judge use, so what you deploy is what a
    local run uses.
    """
    try:
        return bot_model_id(), bot_model_id_premium(), judge_model_id()
    except ModelNotConfigured as exc:
        raise SystemExit(str(exc)) from exc


def main() -> None:
    app = cdk.App()

    bot_model, bot_model_premium, judge_model = model_ids()

    SharedStack(
        app,
        STACK_NAME,
        bot_model_id=bot_model,
        bot_model_id_premium=bot_model_premium,
        judge_model_id=judge_model,
        env=cdk.Environment(
            account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
            region=os.environ.get("CDK_DEFAULT_REGION"),
        ),
        description="Guardrails demo. C1: Break the Bot.",
    )

    app.synth()


if __name__ == "__main__":
    main()
