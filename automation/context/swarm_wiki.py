"""Run the pinned Swarm Wiki compiler and record build or check evidence (RA-CP-004).

The compiler is ``ops/build_wiki.py`` in ``raelldottin/hermes-swarm-protocol``, pinned below to
the reviewed revision that ingests Hindsight snapshots. The contract is
``docs/architecture/swarm-wiki-compiler.md``.

This wrapper runs only an exact, clean checkout of the pinned commit, passes the operator's
compiler arguments through unchanged, and returns what the compiler reported. It never edits the
checkout, the canonical stores or the output; the compiler owns all of that. Nothing in the
supervisor or the context workflow calls it: an operator or CI runs it on its own.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from automation.supervisor import policy

COMPILER_REPOSITORY = "raelldottin/hermes-swarm-protocol"
COMPILER_COMMIT = "d8cdc741a6d89ff827de4bf3a91dba0c71bd72d2"
COMPILER_ENTRY = "ops/build_wiki.py"
# Flags this wrapper owns; the operator chooses build or check with the subcommand instead.
WRAPPER_FLAGS = frozenset({"--check"})
DEFAULT_TIMEOUT_SECONDS = 600


class CompilerPinError(policy.ConfigError):
    """The checkout is not the pinned compiler, or the request cannot be run as given."""


def _git(checkout: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise CompilerPinError(f"{checkout} is not a readable git checkout") from error
    return result.stdout.strip()


def verify_compiler(checkout: Path) -> Path:
    """Return the compiler entry point if ``checkout`` is the pinned commit with no local changes."""
    head = _git(checkout, "rev-parse", "HEAD")
    if head != COMPILER_COMMIT:
        raise CompilerPinError(f"compiler checkout is at {head}, not the pinned {COMPILER_COMMIT}")
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise CompilerPinError("compiler checkout has local changes; evidence needs the pinned bytes")
    entry = checkout / COMPILER_ENTRY
    if not entry.is_file():
        raise CompilerPinError(f"{COMPILER_ENTRY} is missing from the compiler checkout")
    return entry


def run(
    checkout: Path,
    compiler_args: list[str],
    check: bool,
    python: str = sys.executable,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the pinned compiler once and return its evidence; the compiler's exit code is ``exit_code``."""
    owned = WRAPPER_FLAGS.intersection(compiler_args)
    if owned:
        raise CompilerPinError(f"use the check subcommand instead of passing {', '.join(sorted(owned))}")
    entry = verify_compiler(checkout)
    argv = [*compiler_args, *(["--check"] if check else [])]
    try:
        result = subprocess.run([python, str(entry), *argv], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CompilerPinError(f"compiler did not finish within {timeout} seconds") from None
    except OSError as error:
        raise CompilerPinError(f"cannot run {python}: {error.strerror}") from None
    findings, errors = [], []
    for line in result.stderr.splitlines():
        parsed = _json(line)
        (findings if isinstance(parsed, dict) else errors).append(parsed if isinstance(parsed, dict) else line)
    report = _json(result.stdout.strip())
    return {
        "compiler": {"repository": COMPILER_REPOSITORY, "commit": COMPILER_COMMIT, "entry": COMPILER_ENTRY},
        "mode": "check" if check else "build",
        "argv": argv,
        "exit_code": result.returncode,
        "report": report if isinstance(report, dict) else None,
        "findings": findings,
        "errors": errors,
    }


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build or check a Swarm Wiki with the pinned compiler.")
    parser.add_argument("mode", choices=["build", "check"])
    parser.add_argument("--compiler", type=Path, required=True, help="Checkout of the pinned compiler commit.")
    parser.add_argument("--python", default=sys.executable, help="Interpreter with the compiler's dependencies.")
    parser.add_argument("--evidence", type=Path, help="Write the evidence JSON here instead of stdout.")
    parser.epilog = "Arguments after -- go to ops/build_wiki.py unchanged."
    argv = sys.argv[1:] if argv is None else argv
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    compiler_args = argv[split + 1 :]
    try:
        evidence = run(args.compiler, compiler_args, args.mode == "check", args.python)
    except CompilerPinError as error:
        print(f"stop: {error}", file=sys.stderr)
        return 2
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    if args.evidence:
        args.evidence.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return evidence["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
