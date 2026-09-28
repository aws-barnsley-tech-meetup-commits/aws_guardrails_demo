"""Assembles deployment bundles at synth time, without Docker.

Two bundles:

* the AgentCore Runtime code zip, which needs the agent module, the shared
  system prompt and every third-party dependency pre-installed for the runtime's
  platform (Linux arm64);
* the Lambda bundle, which needs the API modules plus ``checks.json``. Its only
  third-party dependency is boto3, already present in the Lambda runtime.

Both are staged under ``.build/`` and skipped when nothing relevant changed, so
repeated ``cdk synth`` runs stay quick.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / ".build"

# Dependencies vendored into the AgentCore code zip. Keep in step with the
# matching pins in pyproject.toml so local runs and deployed runs agree.
AGENT_RUNTIME_DEPENDENCIES = (
    "strands-agents==1.56.0",
    "bedrock-agentcore==1.23.1",
)

# AgentCore Runtime executes the code zip on Amazon Linux, arm64.
TARGET_PLATFORM = "aarch64-manylinux2014"
TARGET_PYTHON_VERSION = "3.13"

AGENT_SOURCE = ROOT / "c1" / "agent" / "main.py"
SYSTEM_PROMPT_SOURCE = ROOT / "shared" / "system_prompt.txt"

#: Shared modules. These go into both bundles, flat beside the code that imports
#: them, and must not import anything else from c1.
SHARED_SOURCES = (
    ROOT / "c1" / "config.py",
    ROOT / "c1" / "guardrails.py",
)

#: Grounding source for the contextual grounding check. The agent needs it for
#: the inline guarded Converse call; the Lambda needs it for the OUTPUT stage of
#: /c1/guardrail-check?mode=full. One copy in the repo, copied at build time.
POLICY_SOURCE = ROOT / "c1" / "docs" / "policy.txt"

API_SOURCES = (
    ROOT / "c1" / "api" / "handler.py",
    ROOT / "c1" / "api" / "auth.py",
    ROOT / "c1" / "api" / "judge.py",
    ROOT / "c1" / "api" / "response.py",
    ROOT / "c1" / "api" / "guardrail_check.py",
)
CHECKS_SOURCE = ROOT / "c1" / "fixtures" / "checks.json"
PROMPTS_SOURCE = ROOT / "c1" / "fixtures" / "prompts.json"

#: The false-positive fixture, so /c1/guardrail-check?fixture=benign works
#: against the deployed API and not only locally.
BENIGN_SOURCE = ROOT / "c1" / "fixtures" / "benign.json"

def _stamp_path(target: Path) -> Path:
    """Stamp lives beside the bundle, not inside it, to keep the zip clean."""
    return target.with_suffix(".stamp")


def _fingerprint(files: tuple[Path, ...], extra: tuple[str, ...] = ()) -> str:
    digest = hashlib.sha256()
    for token in extra:
        digest.update(token.encode("utf-8"))
        digest.update(b"\0")
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _is_current(target: Path, fingerprint: str) -> bool:
    stamp = _stamp_path(target)
    return (
        target.is_dir()
        and stamp.is_file()
        and stamp.read_text(encoding="utf-8").strip() == fingerprint
    )


def _reset(target: Path) -> None:
    _stamp_path(target).unlink(missing_ok=True)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def _strip_bytecode(target: Path) -> None:
    """Drop __pycache__ trees. Local bytecode targets the wrong platform."""
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _install_dependencies(target: Path) -> None:
    command = [
        "uv",
        "pip",
        "install",
        "--quiet",
        "--target",
        str(target),
        "--python-platform",
        TARGET_PLATFORM,
        "--python-version",
        TARGET_PYTHON_VERSION,
        # Wheels only: a source build here would produce macOS binaries.
        "--only-binary",
        ":all:",
        *AGENT_RUNTIME_DEPENDENCIES,
    ]
    print(f"[bundle] installing agent dependencies for {TARGET_PLATFORM}", file=sys.stderr)
    subprocess.run(command, check=True, cwd=ROOT)


def build_agent_bundle() -> Path:
    """Stage the AgentCore Runtime code zip contents and return the directory."""
    target = BUILD_DIR / "c1-agent"
    sources = (AGENT_SOURCE, SYSTEM_PROMPT_SOURCE, POLICY_SOURCE, *SHARED_SOURCES)
    fingerprint = _fingerprint(
        sources,
        (TARGET_PLATFORM, TARGET_PYTHON_VERSION, *AGENT_RUNTIME_DEPENDENCIES),
    )
    if _is_current(target, fingerprint):
        return target

    _reset(target)
    _install_dependencies(target)
    shutil.copy2(AGENT_SOURCE, target / "main.py")
    for source in SHARED_SOURCES:
        shutil.copy2(source, target / source.name)
    # The prompt and the policy live in exactly one place in the repo; these are
    # build-time copies.
    shutil.copy2(SYSTEM_PROMPT_SOURCE, target / "system_prompt.txt")
    shutil.copy2(POLICY_SOURCE, target / POLICY_SOURCE.name)
    _strip_bytecode(target)
    _stamp_path(target).write_text(fingerprint, encoding="utf-8")
    return target


def build_api_bundle() -> Path:
    """Stage the Lambda bundle contents and return the directory."""
    target = BUILD_DIR / "c1-api"
    sources = (
        *API_SOURCES,
        *SHARED_SOURCES,
        CHECKS_SOURCE,
        PROMPTS_SOURCE,
        BENIGN_SOURCE,
        POLICY_SOURCE,
    )
    fingerprint = _fingerprint(sources)
    if _is_current(target, fingerprint):
        return target

    _reset(target)
    for source in sources:
        # Flat: judge.load_checks() and friends look next to the code first.
        shutil.copy2(source, target / source.name)
    _strip_bytecode(target)
    _stamp_path(target).write_text(fingerprint, encoding="utf-8")
    return target
