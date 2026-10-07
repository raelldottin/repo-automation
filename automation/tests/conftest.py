"""Make every test's process ancestry deterministic.

``run_agent.sh`` selects its runner from the nearest agent harness among its real ancestors, so
a suite launched from inside Claude Code or Codex would see that harness above every test that
runs the wrapper and refuse a test's explicit ``REPO_AUTOMATION_AGENT_RUNNER=hermes`` as a
mismatch. The same suite passes in CI, where nothing encloses it. A result that depends on who
ran the tests is not a result, so ``ps`` is replaced for the whole session by a double that
reports no ancestry unless a test describes one in ``FAKE_PS_TREE``.

The double is put on ``PATH`` rather than behind a variable the wrapper reads: a production
switch that turns harness detection off would be a way around the invariant it enforces.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Answers `ps -o <field>= -p <pid>` from FAKE_PS_TREE: one process per line,
# `pid<TAB>ppid<TAB>comm<TAB>args`, nearest ancestor first. The wrapper's own parent is a real
# pid no tree can know in advance, so an unknown pid is the first line. Without a tree the
# double knows no process at all, which is what a headless invocation looks like.
FAKE_PS = r"""#!/usr/bin/env bash
field=""
pid=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) field="${2%=}"; shift 2 ;;
    -p) pid="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "${FAKE_PS_TREE:-}" && -f "$FAKE_PS_TREE" ]] || exit 1
line="$(awk -F'\t' -v p="$pid" '$1 == p' "$FAKE_PS_TREE")"
[[ -n "$line" ]] || line="$(head -n 1 "$FAKE_PS_TREE")"
IFS=$'\t' read -r _ ppid comm args <<<"$line"
case "$field" in
  ppid) printf '%s\n' "$ppid" ;;
  comm) printf '%s\n' "$comm" ;;
  args) printf '%s\n' "$args" ;;
esac
"""

_FAKE_PS_DIR = Path(tempfile.mkdtemp(prefix="repo-automation-fake-ps-"))
(_FAKE_PS_DIR / "ps").write_text(FAKE_PS, encoding="utf-8")
(_FAKE_PS_DIR / "ps").chmod(0o755)
# Module scope rather than a fixture: tests build subprocess environments from os.environ at
# call time, and unittest-style classes never see pytest fixtures.
os.environ["PATH"] = f"{_FAKE_PS_DIR}{os.pathsep}{os.environ['PATH']}"
os.environ.pop("FAKE_PS_TREE", None)
