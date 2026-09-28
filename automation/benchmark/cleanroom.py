"""Put ProgramBench's cleanroom in front of the agent, and prove it is there.

ProgramBench's inference contract is an environment, not a prompt: the agent is given a
reference ``./executable`` and its bundled documentation in ``/workspace``, no internet,
and no access to the upstream source. Run 35799016896 showed what happens when the
harness supplies none of that - the session spent 48 seconds cloning the upstream
repository, which is the one thing the benchmark forbids, and every earlier B-A delta was
measured over lanes that differed mainly in how fast they found that repository.

This module builds that environment from the official inference image
(``programbench/<instance>:task_cleanroom_v6``) and then *checks* it, in a container
started with the same image and the same ``--network=none`` the agent's tools get:

    reference executable   ``test -x ./executable`` inside the container
    documentation          what the image actually bundles, listed, never assumed
    writable workspace     create, read back, delete
    git worktree           shipped by the image (a one-commit repository), never created here
    no egress              curl / wget / git / getent must each fail, or be absent

A check that cannot run is a failure, not a pass. The whole point is that the receipt
records what was observed rather than what the config asked for.

Not proved here, and deliberately not claimed: that Hermes' own container is this
container. That one rests on the pinned revision routing terminal, file and
code_execution through ``docker exec`` into a single ``--network=none`` container, plus
its own refusal to attach to a networked container under ``docker_network: false``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

CLEANROOM_TAG = "task_cleanroom_v6"
CLEANROOM_REGISTRY = "programbench"
# Docker repository names cannot contain `__`; ProgramBench substitutes this for it.
INSTANCE_SEPARATOR = "__"
IMAGE_SEPARATOR = "_1776_"
WORKSPACE_DIR = "/workspace"
REFERENCE_EXECUTABLE = "executable"
NETWORK_MODE = "none"

# Anything the agent is allowed to read as a specification. Kept wide on purpose: the
# image decides what it bundles, and a rebuild briefed from an unread man page is the
# same failure as one briefed from no page at all.
_DOCUMENTATION_SUFFIXES = (".md", ".txt", ".rst", ".html", ".htm", ".pdf", ".info", ".org")
_DOCUMENTATION_STEMS = ("readme", "manual", "usage", "changelog", "news", "faq", "help", "tutorial")
_DOCUMENTATION_DIRS = ("doc", "docs", "man", "manual", "manpages", "share")

# (argv) -> (returncode, combined output)
DockerRun = Callable[[list[str]], tuple[int, str]]

# A pull inside `docker create` is the slow step; everything else is sub-second.
_DOCKER_TIMEOUT_SECONDS = 900


class CleanroomError(RuntimeError):
    """The inference environment is not the one the benchmark claims to administer."""

    def __init__(self, message: str, receipt: Optional["CleanroomReceipt"] = None) -> None:
        super().__init__(message)
        self.receipt = receipt


@dataclass
class CleanroomReceipt:
    """What was observed, in the order the acceptance contract asks for it."""

    instance_id: str
    image: str
    image_id: str = ""
    network_mode: str = ""
    reference_executable: bool = False
    workspace_writable: bool = False
    git_worktree: bool = False
    workspace_entries: list[str] = field(default_factory=list)
    documentation: list[str] = field(default_factory=list)
    egress_probes: list[dict[str, object]] = field(default_factory=list)
    probe_argv: list[str] = field(default_factory=list)

    def violations(self) -> list[str]:
        """Every acceptance item this environment fails, named the way the contract names it."""
        failures = []
        if self.network_mode != NETWORK_MODE:
            failures.append(f"model-facing tools are not air-gapped: NetworkMode={self.network_mode!r}, want {NETWORK_MODE!r}")
        if not self.reference_executable:
            failures.append(f"no executable reference at {WORKSPACE_DIR}/{REFERENCE_EXECUTABLE} before the first agent turn")
        if not self.documentation:
            failures.append(
                f"no bundled documentation in the image workspace; it holds only: {', '.join(self.workspace_entries) or '(nothing)'}"
            )
        if not self.workspace_writable:
            failures.append(f"{WORKSPACE_DIR} is not writable by the tools that have to rebuild in it")
        if not self.git_worktree:
            # The image builds its workspace as a one-commit repository, and run_agent.sh
            # requires a Git checkout at the repo root. The harness will not `git init`
            # the inference workspace to paper over an image that ships none: that would
            # be the harness inventing environment it is supposed to be measuring.
            failures.append(f"the image ships no Git worktree at {WORKSPACE_DIR}; the harness does not create one")
        reached = [probe["command"] for probe in self.egress_probes if probe.get("returncode") == 0]
        if reached:
            failures.append(f"network egress succeeded from inside the sandbox: {reached}")
        if not any(probe.get("present") for probe in self.egress_probes):
            # Every probe binary missing proves nothing about the network: it proves the
            # image is thin. A cleanroom that cannot be tested is not a tested cleanroom.
            failures.append("no egress probe could run: none of curl, wget, git or getent exist in the image")
        return failures

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def image_for(instance_id: str) -> str:
    """The official inference image for one instance, by ProgramBench's naming rule."""
    return f"{CLEANROOM_REGISTRY}/{instance_id.replace(INSTANCE_SEPARATOR, IMAGE_SEPARATOR)}:{CLEANROOM_TAG}"


def _subprocess_docker(argv: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _checked(run: DockerRun, argv: list[str]) -> str:
    returncode, output = run(argv)
    if returncode != 0:
        raise CleanroomError(f"{' '.join(argv[:3])} failed (rc {returncode}): {output.strip()}")
    return output.strip()


def _is_documentation(entry: Path) -> bool:
    name = entry.name.lower()
    if entry.is_dir():
        return name in _DOCUMENTATION_DIRS
    stem, _, suffix = name.rpartition(".")
    if f".{suffix}" in _DOCUMENTATION_SUFFIXES:
        return True
    # Man pages: foo.1 ... foo.9, and the extensionless conventional names.
    return (suffix.isdigit() and len(suffix) == 1 and bool(stem)) or name in _DOCUMENTATION_STEMS


# Run inside the container, not against the config that asked for it. `key=value` per
# line so a partial run is still readable, and every egress probe reports whether its
# binary existed - "failed" and "absent" are different claims about the network.
_PROBE_SCRIPT = r"""
set -u
cd {workspace} || exit 70
if [ -x ./{executable} ]; then echo "reference_executable=yes"; else echo "reference_executable=no"; fi
if [ -d .git ]; then echo "git_worktree=yes"; else echo "git_worktree=no"; fi
probe=".cleanroom-write-probe"
if echo ok > "$probe" && [ "$(cat "$probe")" = ok ] && rm -f "$probe" && [ ! -e "$probe" ]; then
  echo "workspace_writable=yes"
else
  echo "workspace_writable=no"
fi
try() {{
  name="$1"; shift
  if command -v "$name" >/dev/null 2>&1; then
    if "$@" >/dev/null 2>&1; then rc=0; else rc=$?; fi
    echo "egress=yes|$rc|$*"
  else
    echo "egress=no||$*"
  fi
}}
GIT_TERMINAL_PROMPT=0
export GIT_TERMINAL_PROMPT
try curl curl -sS -m 5 https://github.com
try wget wget -q -T 5 -O - https://github.com
try git git ls-remote https://github.com/{repository}.git
try getent getent hosts github.com
"""


def _parse_probe(output: str, receipt: CleanroomReceipt) -> None:
    for line in output.splitlines():
        key, _, value = line.strip().partition("=")
        if key == "reference_executable":
            receipt.reference_executable = value == "yes"
        elif key == "workspace_writable":
            receipt.workspace_writable = value == "yes"
        elif key == "git_worktree":
            receipt.git_worktree = value == "yes"
        elif key == "egress":
            present, _, rest = value.partition("|")
            returncode, _, command = rest.partition("|")
            receipt.egress_probes.append(
                {
                    "command": command,
                    "present": present == "yes",
                    "returncode": int(returncode) if returncode.isdigit() else None,
                }
            )


def materialize(instance_id: str, workspace: Path, *, run: Optional[DockerRun] = None) -> tuple[str, str]:
    """Copy the image's ``/workspace`` onto the host. Returns ``(image, image id)``.

    The contents move to the host rather than the agent moving into the image, because
    Hermes mounts *something* at ``/workspace`` in every configuration it has - a tmpfs, a
    sandbox directory or the host cwd - and the first two would mask exactly the reference
    executable and documentation this whole exercise exists to put in front of the model.
    The third one is a bind mount of the host cwd, so the host copy is what the container
    sees, and the harness keeps reading and archiving the workspace as it always has.
    """
    run = run or _subprocess_docker
    image = image_for(instance_id)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    # Nothing of the harness's own may already be here. The cell workspace has to be what
    # the image ships and only that, so anything pre-existing - a `git init`, a leftover
    # from a previous cell - is refused rather than merged into.
    existing = sorted(entry.name for entry in workspace.iterdir())
    if existing:
        raise CleanroomError(f"{workspace} is not empty; the cleanroom is not merged into existing state: {existing}")

    container = _checked(run, ["docker", "create", image, "true"])
    try:
        _checked(run, ["docker", "cp", f"{container}:{WORKSPACE_DIR}/.", str(workspace)])
    finally:
        run(["docker", "rm", "-f", container])
    image_id = _checked(run, ["docker", "image", "inspect", "--format", "{{.Id}}", image])
    return image, image_id


def verify(
    instance_id: str, workspace: Path, image: str, image_id: str, repository: str, *, run: Optional[DockerRun] = None
) -> CleanroomReceipt:
    """Check the environment from inside a container started the way the agent's will be."""
    run = run or _subprocess_docker
    workspace = Path(workspace).resolve()
    receipt = CleanroomReceipt(instance_id=instance_id, image=image, image_id=image_id)
    receipt.workspace_entries = sorted(entry.name for entry in workspace.iterdir())
    receipt.documentation = sorted(entry.name for entry in workspace.iterdir() if _is_documentation(entry))

    script = _PROBE_SCRIPT.format(workspace=WORKSPACE_DIR, executable=REFERENCE_EXECUTABLE, repository=repository)
    # Created, started and inspected rather than `docker run --rm`: the network mode has
    # to be read off the container that ran the probes, not off the flags we believe we
    # passed it. Item 2 of the acceptance contract is a fact about a container.
    receipt.probe_argv = [
        "docker",
        "create",
        f"--network={NETWORK_MODE}",
        "-v",
        f"{workspace}:{WORKSPACE_DIR}",
        "-w",
        WORKSPACE_DIR,
        image,
        "sh",
        "-c",
        script,
    ]
    container = _checked(run, receipt.probe_argv)
    try:
        _, output = run(["docker", "start", "--attach", container])
        _parse_probe(output, receipt)
        receipt.network_mode = _checked(run, ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", container])
    finally:
        run(["docker", "rm", "-f", container])
    return receipt


def prepare(instance_id: str, workspace: Path, repository: str, *, run: Optional[DockerRun] = None) -> CleanroomReceipt:
    """Materialize the cleanroom and refuse to hand over one that fails the contract."""
    image, image_id = materialize(instance_id, workspace, run=run)
    receipt = verify(instance_id, workspace, image, image_id, repository, run=run)
    violations = receipt.violations()
    if violations:
        raise CleanroomError(
            "the inference environment is not a ProgramBench cleanroom:\n  - " + "\n  - ".join(violations),
            receipt,
        )
    return receipt


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m automation.benchmark.cleanroom",
        description="Build one ProgramBench cleanroom workspace and prove what is in it.",
    )
    parser.add_argument("--instance", required=True, help="ProgramBench instance id.")
    parser.add_argument("--workspace", type=Path, required=True, help="Host directory to materialize into.")
    parser.add_argument("--receipt", type=Path, help="Write the receipt as JSON here.")
    args = parser.parse_args(argv)

    from .instances import task_spec

    task = task_spec(args.instance)
    try:
        receipt = prepare(args.instance, args.workspace, task.repository)
    except CleanroomError as failure:
        if failure.receipt is not None and args.receipt:
            args.receipt.write_text(json.dumps(failure.receipt.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"cleanroom preflight FAILED: {failure}", file=sys.stderr)
        return 1
    if args.receipt:
        args.receipt.write_text(json.dumps(receipt.to_dict(), indent=2) + "\n", encoding="utf-8")
    egress = "; ".join(
        f"{str(probe['command']).split()[0]} {'rc ' + str(probe['returncode']) if probe['present'] else 'absent'}"
        for probe in receipt.egress_probes
    )
    print(
        f"cleanroom ok   {receipt.image}\n"
        f"  image id     {receipt.image_id}\n"
        f"  network      {receipt.network_mode}\n"
        f"  reference    ./{REFERENCE_EXECUTABLE} executable\n"
        f"  git          worktree shipped by the image, not created here\n"
        f"  workspace    {len(receipt.workspace_entries)} entries, writable\n"
        f"  docs         {', '.join(receipt.documentation)}\n"
        f"  egress       {egress}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
