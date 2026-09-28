"""The container the agent got, not the container we asked for.

Every test drives the witness against a stand-in for Docker, because the fact under test is
what the witness *concludes* from what the daemon reports - and what it has to conclude
about a reused, bridged or wrongly mounted container is a refusal.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from automation.benchmark.witness import WITNESS_SUFFIX, SandboxWitness

IMAGE = "programbench/abishekvashok_1776_cmatrix.5c082c6:task_cleanroom_v6"
IMAGE_ID = "sha256:4ef6d754"
CONTAINER = "c0ffee" * 10


class FakeDocker:
    """A Docker whose container list the test changes while the session 'runs'."""

    def __init__(self, *, containers: list[str], inspect: dict) -> None:
        self.containers = containers
        self.inspect = inspect
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[1] == "ps":
            return 0, "".join(f"{container}\n" for container in self.containers), ""
        if argv[1] == "inspect":
            return 0, json.dumps(self.inspect) + "\n", ""
        raise AssertionError(f"unexpected docker call: {argv}")


def inspected(workspace: Path, **overrides) -> dict:
    raw = {
        "container_id": CONTAINER,
        "created_at": "2026-09-28T12:14:57.1Z",
        "image_ref": IMAGE,
        "image_id": IMAGE_ID,
        "network_mode": "none",
        "mounts": [{"Destination": "/workspace", "Source": str(workspace), "RW": True, "Type": "bind"}],
        "labels": {"hermes-agent": "1", "hermes-task-id": "default", "org.opencontainers.image.title": "cmatrix"},
    }
    raw.update(overrides)
    return raw


class WitnessTests(unittest.TestCase):
    def watch(self, docker: FakeDocker, workspace: Path, sessions: Path, *, creates: bool = True) -> SandboxWitness:
        """Run one 'session' that creates its container the way Hermes does: partway in."""
        witness = SandboxWitness(sessions, workspace, IMAGE, IMAGE_ID, run=docker, poll_seconds=0.01)

        def session(command, workspace_arg, env, timeout) -> int:
            if creates:
                docker.containers.append(CONTAINER)
            return 0

        witness.wrap(session)("hermes", workspace, {}, 300)
        return witness

    def written(self, sessions: Path) -> dict:
        receipts = list(sessions.glob(f"*{WITNESS_SUFFIX}"))
        self.assertEqual(1, len(receipts), f"expected one witness receipt, found {receipts}")
        return json.loads(receipts[0].read_text(encoding="utf-8"))

    def test_the_container_the_agent_was_given_is_recorded_while_it_still_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "agent-sessions"
            workspace.mkdir()
            sessions.mkdir()
            (sessions / "20260928T121455Z-3087.controls.json").write_text("{}", encoding="utf-8")
            docker = FakeDocker(containers=[], inspect=inspected(workspace))

            witness = self.watch(docker, workspace, sessions)

            self.assertEqual([], witness.violations)
            # Named for the session that created it: run_agent.sh writes its controls
            # report before it launches the agent, so the newest one names this session.
            record = json.loads((sessions / f"20260928T121455Z-3087{WITNESS_SUFFIX}").read_text(encoding="utf-8"))
            self.assertTrue(record["observed"])
            self.assertFalse(record["existed_in_baseline"])
            self.assertEqual(CONTAINER, record["container_id"])
            self.assertEqual(IMAGE_ID, record["image_id"])
            self.assertEqual("none", record["network_mode"])
            self.assertEqual({"source": str(workspace), "destination": "/workspace", "rw": True}, record["workspace_mount"])
            self.assertEqual({"hermes-agent": "1", "hermes-task-id": "default"}, record["labels"])

    def test_the_container_environment_is_never_asked_for(self) -> None:
        # The provider key lives in the agent's environment. The witness reads the container
        # from outside, and a receipt beside the submission is the last place it may surface.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace))
            self.watch(docker, workspace, sessions)
            inspect = next(argv for argv in docker.calls if argv[1] == "inspect")
            self.assertNotIn("Env", inspect[3])
            self.assertNotIn("Env", json.dumps(self.written(sessions)))

    def test_a_container_on_a_bridge_network_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace, network_mode="bridge"))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("NetworkMode='bridge'", witness.violations[0])
            self.assertTrue(self.written(sessions)["observed"])

    def test_a_container_running_another_image_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace, image_id="sha256:something-else"))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("not the cleanroom image", witness.violations[0])

    def test_a_workspace_from_somewhere_other_than_this_cell_is_a_hard_failure(self) -> None:
        # A container reused across cells keeps the *first* cell's bind mount, so the second
        # cell's agent would be writing into the first cell's workspace.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            stale = [{"Destination": "/workspace", "Source": "/tmp/pb-another-cell", "RW": True}]
            docker = FakeDocker(containers=[], inspect=inspected(workspace, mounts=stale))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("not this cell's workspace", witness.violations[0])

    def test_a_read_only_workspace_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            frozen = [{"Destination": "/workspace", "Source": str(workspace), "RW": False}]
            docker = FakeDocker(containers=[], inspect=inspected(workspace, mounts=frozen))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("read-only", witness.violations[0])

    def test_nothing_mounted_at_the_workspace_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace, mounts=[]))
            witness = self.watch(docker, workspace, sessions)

            self.assertIn("nothing mounted at /workspace", witness.violations[0])

    def test_a_session_that_created_no_container_says_so_rather_than_inventing_one(self) -> None:
        # An agent that never calls a tool never gets a container. That is a fact about the
        # model's turn, not about the sandbox, so it is recorded and not refused.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect={})
            witness = self.watch(docker, workspace, sessions, creates=False)

            self.assertEqual([], witness.violations)
            self.assertEqual({"observed": False, "existing_containers": [], "violations": []}, self.written(sessions))

    def test_a_session_that_reused_an_existing_container_is_a_hard_failure(self) -> None:
        # Reuse leaves no new container, so it looks exactly like "no container" except for
        # what was already lying around - which is the only evidence there is.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=["leftover-from-an-earlier-process"], inspect={})
            witness = self.watch(docker, workspace, sessions, creates=False)

            self.assertIn("no container was created and 1 Hermes container(s) were already present", witness.violations[0])
            self.assertEqual(["leftover-from-an-earlier-process"], self.written(sessions)["existing_containers"])

    def test_a_container_that_was_already_running_is_not_mistaken_for_this_cell_s(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=["someone-elses"], inspect=inspected(workspace))
            witness = self.watch(docker, workspace, sessions)

            record = self.written(sessions)
            self.assertEqual(CONTAINER, record["container_id"])
            self.assertEqual([], witness.violations)


if __name__ == "__main__":
    unittest.main()
