"""Watch the containers the agent was actually given, while the agent still has them.

``cleanroom.py`` proves the image and the workspace before the cell starts. That is a
different fact from the one this module records: a Hermes that reuses a container from an
earlier process, or attaches one to a bridge network, leaves the preflight receipt entirely
true and the inference contaminated anyway. Only the running container answers it.

So the observation happens while the session is alive and is written the moment each
container appears. A cell killed at its budget ceiling takes its whole process group with
it, and a witness that summarized at the end would have nothing to summarize.

Every new container, not the first one. Hermes builds one backend per ``task_id``: the
system prompt's own probe gets ``prompt-backend-probe``, and the terminal, file and
code_execution tools the model calls get ``default``. First-container-wins recorded the
probe's and hid the one that matters.

One thing it does rather than records: a container it saw that is still listed once the
session is gone gets removed by id, because a budget kill leaves Hermes no chance to. See
``_reap``.

What is never recorded: the container's environment. The inspect format below does not ask
for ``.Config.Env``, so the provider key cannot reach this process, let alone the artifact.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

from .cleanroom import NETWORK_MODE, WORKSPACE_DIR, DockerRun, _subprocess_docker

# Hermes labels every container it owns. Baseline on the label alone and not on the image:
# a container running the wrong image is exactly one of the things this has to catch, and
# filtering it out of the listing would hide it.
HERMES_LABEL = "hermes-agent=1"
# The label Hermes writes its backend key to, and the key every ordinary tool call resolves
# to. `terminal_tool(..., task_id=None)` -> `_resolve_container_task_id` -> "default".
TASK_ID_LABEL = "hermes-task-id"
DEFAULT_TASK_ID = "default"
# The backend the system prompt builds for its own `uname`/`pwd`/`whoami` probe. Neither
# this nor `default` is what a tool call inside an agent turn resolves to: under
# `container_persistent: false` that is a session-scoped id, and it reaches the mount logic
# by a different path. Run 36485906813 had the first two right and the third on a tmpfs.
PROMPT_PROBE_TASK_ID = "prompt-backend-probe"
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


def _is_session_scoped(record: dict[str, object]) -> bool:
    """A container keyed by a session id rather than by one of Hermes's fixed backends."""
    task_id = record.get("task_id")
    return bool(task_id) and task_id not in (DEFAULT_TASK_ID, PROMPT_PROBE_TASK_ID)


def _violations(record: dict[str, object], image_id: str, workspace: Path) -> list[str]:
    """What, in a container the agent got, disqualifies the cell."""
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
    """Every container Docker handed one agent session, recorded as that session runs."""

    def __init__(
        self,
        sessions_dir: Path,
        workspace: Path,
        image: str,
        image_id: str,
        *,
        run: Optional[DockerRun] = None,
        poll_seconds: float = POLL_SECONDS,
        stem: Optional[str] = None,
    ) -> None:
        self._sessions_dir = Path(sessions_dir)
        self._workspace = Path(workspace).resolve()
        self._image = image
        self._image_id = image_id
        self._run = run or _subprocess_docker
        self._poll_seconds = poll_seconds
        self._stem = stem
        self._baseline: set[str] = set()
        self._seen: set[str] = set()
        self.containers: list[dict[str, object]] = []
        self.violations: list[str] = []

    @property
    def receipt(self) -> dict[str, Any]:
        """One structure, whatever happened: no container, one, or one per backend."""
        default = [record for record in self.containers if record.get("task_id") == DEFAULT_TASK_ID]
        session = [record for record in self.containers if _is_session_scoped(record)]
        removed = [record.get("removed_after_exit") for record in default]
        return {
            "observed": bool(self.containers),
            # The backend every ordinary terminal, file and code_execution call resolves to.
            # A session that only ever created the system prompt's probe backend has not
            # shown that the tools the model calls run in the cleanroom.
            "default_backend_verified": bool(default) and not any(record["violations"] for record in default),
            # The backend an agent turn's own tool calls resolve to under per-session
            # isolation. A different path through the mount logic, so a separate fact.
            "session_backend_verified": bool(session) and not any(record["violations"] for record in session),
            # The direct runtime reading of `docker_persist_across_processes: false`: the
            # container is gone once the process that made it is, so nothing can attach to
            # it next time. Null until the session has exited.
            "default_backend_removed_after_exit": all(removed) if removed and None not in removed else None,
            "existing_containers": sorted(self._baseline),
            "containers": self.containers,
        }

    def wrap(self, runner):
        """The session runner, with the witness watching for as long as the session runs."""

        def run_session(command: str, workspace: Path, env, timeout: int) -> int:
            with self.watching():
                return runner(command, workspace, env, timeout)

        return run_session

    @contextmanager
    def watching(self) -> Generator[None, None, None]:
        self._baseline = _hermes_containers(self._run)
        stop = threading.Event()
        thread = threading.Thread(target=self._watch, args=(stop,), daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
            self._after_exit()

    def _watch(self, stop: threading.Event) -> None:
        while True:
            self._observe()
            if stop.wait(self._poll_seconds):
                # One last look before giving up: `docker ps -a` still lists a container
                # that has exited, so a session shorter than one poll interval is not a
                # session whose container went unobserved.
                self._observe()
                return

    def _observe(self) -> None:
        for container in sorted(_hermes_containers(self._run) - self._baseline - self._seen):
            self._seen.add(container)
            returncode, stdout, _ = self._run(["docker", "inspect", "--format", _INSPECT_FORMAT, container])
            if returncode != 0:
                # Gone between the listing and the inspect. Not seen again, and not claimed.
                continue
            self._record(json.loads(stdout.strip() or "{}"))

    def _record(self, raw: dict[str, Any]) -> None:
        mounts = raw.get("mounts") or []
        mount = next((entry for entry in mounts if entry.get("Destination") == WORKSPACE_DIR), None)
        labels = {key: value for key, value in (raw.get("labels") or {}).items() if key.startswith("hermes")}
        record: dict[str, object] = {
            "task_id": labels.get(TASK_ID_LABEL),
            "container_id": raw.get("container_id"),
            "created_at": raw.get("created_at"),
            "requested_image": self._image,
            "image_ref": raw.get("image_ref"),
            "image_id": raw.get("image_id"),
            "network_mode": raw.get("network_mode"),
            "workspace_mount": (
                {"source": mount.get("Source"), "destination": mount.get("Destination"), "rw": mount.get("RW")} if mount else None
            ),
            "labels": labels,
            "existed_in_baseline": False,
            "removed_after_exit": None,
            "cleanup_attempted": False,
            "removed_by_harness": False,
            "removed_after_cleanup": None,
            "violations": [],
        }
        violations = _violations(record, self._image_id, self._workspace)
        record["violations"] = violations
        self.containers.append(record)
        self.violations.extend(violations)
        self._flush()

    def _after_exit(self) -> None:
        """What survived the session that made it.

        A container still listed once its process is gone is one the next process can
        attach to - `_find_reusable_container` filters on these labels, and a stopped
        container is started again, not rebuilt. So presence, not running, is the reading.
        """
        if not self.containers:
            if self._baseline:
                # Reuse is invisible from the outside - it leaves no new container - so a
                # session that created none while one of Hermes's was already lying around
                # is the shape of it.
                self.violations.append(
                    f"no container was created and {len(self._baseline)} Hermes container(s) were already present"
                )
            self._flush()
            return
        remaining = _hermes_containers(self._run)
        for record in self.containers:
            # Answered once, at the exit of the invocation that made it. One witness spans
            # every phase of a lane, and a later phase must not re-answer an earlier
            # phase's question - least of all to say "gone" about a container this reaped.
            if record["removed_after_exit"] is None:
                record["removed_after_exit"] = record["container_id"] not in remaining
        self._reap()
        self._flush()

    def _reap(self) -> None:
        """Remove what a killed session left behind - and only what this witness saw.

        A cell killed at its budget ceiling takes Hermes's own cleanup down with it, so the
        container it was working in survives, along with whatever its last tool call started
        inside. A fresh session id keeps the next cell from attaching to it, so this is not
        contamination; it is a compile running on the runner's CPU while the next phase is
        being measured on it.

        The target list is this witness's own container ids, never a `hermes-agent=1` query:
        a broad sweep would also take containers that were here before the cell and
        containers another cell is still using. `removed_after_exit` keeps its original
        reading - the runtime proof of `docker_persist_across_processes: false` is a fact
        about Hermes, and rewriting it to say the harness cleaned up would erase it.
        """
        leaked = [record for record in self.containers if not record["removed_after_exit"] and not record["cleanup_attempted"]]
        if not leaked:
            return
        for record in leaked:
            record["cleanup_attempted"] = True
            try:
                returncode, _, _ = self._run(["docker", "rm", "-f", str(record["container_id"])])
            except OSError:
                returncode = 1
            record["removed_by_harness"] = returncode == 0
        remaining = _hermes_containers(self._run)
        for record in leaked:
            record["removed_after_cleanup"] = record["container_id"] not in remaining
            if not record["removed_after_cleanup"]:
                # Refused rather than carried forward: the next phase would be measured
                # against a runner this one is still using.
                self.violations.append(f"the container {record['container_id']} outlived its session and could not be removed")

    def _flush(self) -> None:
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        path = self._sessions_dir / f"{self._stem or self._session_stem()}{WITNESS_SUFFIX}"
        # Written whole or not at all: the process this runs in outlives the session, but
        # the run it belongs to does not outlive the job, and a half-written receipt reads
        # as an observation that was never made.
        temporary = path.with_name(path.name + ".partial")
        temporary.write_text(json.dumps(self.receipt, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _session_stem(self) -> str:
        """The session these containers belong to.

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
