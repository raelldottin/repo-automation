"""Watch the container the agent was actually given, while the agent still has it.

``cleanroom.py`` proves the image and the workspace before the cell starts. That is a
different fact from the one this module records: a Hermes that reuses a container from an
earlier process, or attaches one to a bridge network, leaves the preflight receipt entirely
true and the inference contaminated anyway. Only the running container answers it.

So the observation happens while the session is alive and is written the moment the
container appears. A cell killed at its budget ceiling takes its whole process group with
it, and a witness that summarized at the end would have nothing to summarize.

What is never recorded: the container's environment. The inspect format below does not ask
for ``.Config.Env``, so the provider key cannot reach this process, let alone the artifact.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from .cleanroom import NETWORK_MODE, WORKSPACE_DIR, DockerRun, _subprocess_docker

# Hermes labels every container it owns. Baseline on the label alone and not on the image:
# a container running the wrong image is exactly one of the things this has to catch, and
# filtering it out of the listing would hide it.
HERMES_LABEL = "hermes-agent=1"
# The agent's first tool call is what creates the container, so the wait is the model's
# first turn - seconds to a minute - and the poll is cheap.
POLL_SECONDS = 0.5
WITNESS_SUFFIX = ".sandbox.json"
_CONTROLS_SUFFIX = ".controls.json"
# Exactly the fields the receipt keeps. `docker inspect` without a format would hand back
# the container's whole configuration, environment included.
_INSPECT_FORMAT = (
    '{"container_id":{{json .Id}},"created_at":{{json .Created}},'
    '"image_ref":{{json .Config.Image}},"image_id":{{json .Image}},'
    '"network_mode":{{json .HostConfig.NetworkMode}},'
    '"mounts":{{json .Mounts}},"labels":{{json .Config.Labels}}}'
)


def _hermes_containers(run: DockerRun) -> set[str]:
    try:
        returncode, stdout, _ = run(
            ["docker", "ps", "-a", "--no-trunc", "--filter", f"label={HERMES_LABEL}", "--format", "{{.ID}}"]
        )
    except OSError:
        # No Docker on this host: a local cell, or a unit test. Nothing to witness, and the
        # receipt says so rather than failing a run that never asked for a container.
        return set()
    if returncode != 0:
        return set()
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _violations(record: dict[str, object], image_id: str, workspace: Path) -> list[str]:
    """What, in the container the agent got, disqualifies the cell."""
    failures = []
    if record.get("image_id") != image_id:
        failures.append(f"the agent's container runs {record.get('image_id')}, not the cleanroom image {image_id}")
    if record.get("network_mode") != NETWORK_MODE:
        failures.append(f"the agent's container is on NetworkMode={record.get('network_mode')!r}, not {NETWORK_MODE!r}")
    mount = record.get("workspace_mount")
    if not isinstance(mount, dict):
        failures.append(f"the agent's container has nothing mounted at {WORKSPACE_DIR}")
        return failures
    source = mount.get("source")
    # Resolved on both sides: the harness's own temp root is a symlink on some hosts, and
    # Docker answers with the path it was handed, not with the one we would have written.
    if not source or Path(str(source)).resolve() != workspace:
        failures.append(f"{WORKSPACE_DIR} comes from {source!r}, not this cell's workspace {str(workspace)!r}")
    if not mount.get("rw"):
        failures.append(f"{WORKSPACE_DIR} is mounted read-only; the agent cannot produce a submission in it")
    return failures


class SandboxWitness:
    """One record per agent session of the container Docker actually handed it."""

    def __init__(
        self,
        sessions_dir: Path,
        workspace: Path,
        image: str,
        image_id: str,
        *,
        run: Optional[DockerRun] = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._sessions_dir = Path(sessions_dir)
        self._workspace = Path(workspace).resolve()
        self._image = image
        self._image_id = image_id
        self._run = run or _subprocess_docker
        self._poll_seconds = poll_seconds
        self.records: list[dict[str, object]] = []
        self.violations: list[str] = []

    def wrap(self, runner):
        """The session runner, with the witness watching for as long as the session runs."""

        def run_session(command: str, workspace: Path, env, timeout: int) -> int:
            with self.watching():
                return runner(command, workspace, env, timeout)

        return run_session

    @contextmanager
    def watching(self) -> Generator[None, None, None]:
        baseline = _hermes_containers(self._run)
        stop = threading.Event()
        thread = threading.Thread(target=self._watch, args=(baseline, stop), daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def _watch(self, baseline: set[str], stop: threading.Event) -> None:
        while True:
            if self._observe(baseline):
                return
            if stop.wait(self._poll_seconds):
                # One last look before giving up: `docker ps -a` still lists a container
                # that has exited, so a session shorter than one poll interval is not a
                # session whose container went unobserved.
                if not self._observe(baseline):
                    # Reuse is invisible from the outside - it leaves no new container - so
                    # a session that created none while one of Hermes's was already lying
                    # around is the shape of it.
                    violations = (
                        [f"no container was created and {len(baseline)} Hermes container(s) were already present"]
                        if baseline
                        else []
                    )
                    self._write(
                        {"observed": False, "existing_containers": sorted(baseline), "violations": violations},
                        violations,
                    )
                return

    def _observe(self, baseline: set[str]) -> bool:
        appeared = sorted(_hermes_containers(self._run) - baseline)
        if not appeared:
            return False
        returncode, stdout, _ = self._run(["docker", "inspect", "--format", _INSPECT_FORMAT, appeared[0]])
        if returncode != 0:
            return False
        raw = json.loads(stdout.strip() or "{}")
        mount = next(
            (entry for entry in raw.get("mounts") or [] if entry.get("Destination") == WORKSPACE_DIR),
            None,
        )
        record: dict[str, object] = {
            "observed": True,
            "container_id": raw.get("container_id"),
            "created_at": raw.get("created_at"),
            "requested_image": self._image,
            "image_ref": raw.get("image_ref"),
            "image_id": raw.get("image_id"),
            "network_mode": raw.get("network_mode"),
            "workspace_mount": (
                {"source": mount.get("Source"), "destination": mount.get("Destination"), "rw": mount.get("RW")} if mount else None
            ),
            "labels": {key: value for key, value in (raw.get("labels") or {}).items() if key.startswith("hermes")},
            "existed_in_baseline": False,
        }
        violations = _violations(record, self._image_id, self._workspace)
        record["violations"] = violations
        self._write(record, violations)
        return True

    def _write(self, record: dict[str, object], violations: list[str]) -> None:
        self.records.append(record)
        self.violations.extend(violations)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        path = self._sessions_dir / f"{self._session_stem()}{WITNESS_SUFFIX}"
        # Written whole or not at all: the process this runs in outlives the session, but
        # the run it belongs to does not outlive the job, and a half-written receipt reads
        # as an observation that was never made.
        temporary = path.with_name(path.name + ".partial")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _session_stem(self) -> str:
        """The session this container belongs to.

        ``run_agent.sh`` names the session itself and writes its controls report before it
        launches the agent, so by the time any container exists the newest of those names
        the session that will create it.
        """
        controls = sorted(
            self._sessions_dir.glob(f"*{_CONTROLS_SUFFIX}"),
            key=lambda path: path.stat().st_mtime,
        )
        if not controls:
            return "unattributed"
        return controls[-1].name[: -len(_CONTROLS_SUFFIX)]
