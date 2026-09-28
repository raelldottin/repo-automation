"""The containers the agent got, not the container we asked for.

Every test drives the witness against a stand-in for Docker, because the fact under test is
what the witness *concludes* from what the daemon reports - and what it has to conclude
about a reused, bridged or wrongly mounted container is a refusal.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from automation.benchmark.witness import WITNESS_SUFFIX, SandboxWitness

IMAGE = "programbench/abishekvashok_1776_cmatrix.5c082c6:task_cleanroom_v6"
IMAGE_ID = "sha256:4ef6d754"
CONTAINER = "c0ffee" * 10
PROBE_CONTAINER = "decade" * 10
SESSION_CONTAINER = "bedead" * 10
# What `container_persistent: false` keys an agent turn's own tool calls by.
SESSION_TASK_ID = "32c58fde-13c6-4448-bbb5-b90482b19290"


class FakeDocker:
    """A Docker whose container list the test changes while the session 'runs'."""

    def __init__(self, *, containers: list[str], inspect: dict, unremovable: tuple[str, ...] = ()) -> None:
        self.containers = containers
        # One record per container id, or one record for whatever is asked about.
        self.inspect = inspect
        # Containers `docker rm -f` cannot get rid of: a wedged runtime, a shutting-down
        # daemon. The cell has to notice rather than hand the next phase a busy runner.
        self.unremovable = unremovable
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[1] == "ps":
            return 0, "".join(f"{container}\n" for container in self.containers), ""
        if argv[1] == "inspect":
            record = self.inspect.get(argv[-1], self.inspect) if self.inspect else {}
            return 0, json.dumps(record) + "\n", ""
        if argv[1] == "rm":
            container = argv[-1]
            if container in self.unremovable:
                return 1, "", f"Error response from daemon: cannot remove {container}\n"
            self.containers.remove(container)
            return 0, f"{container}\n", ""
        raise AssertionError(f"unexpected docker call: {argv}")


def inspected(workspace: Path, *, container: str = CONTAINER, task_id: str = "default", **overrides) -> dict:
    raw = {
        "container_id": container,
        "created_at": "2026-09-28T12:14:57.1Z",
        "image_ref": IMAGE,
        "image_id": IMAGE_ID,
        "network_mode": "none",
        "mounts": [{"Destination": "/workspace", "Source": str(workspace), "RW": True, "Type": "bind"}],
        "labels": {"hermes-agent": "1", "hermes-task-id": task_id, "org.opencontainers.image.title": "cmatrix"},
    }
    raw.update(overrides)
    return raw


class WitnessTests(unittest.TestCase):
    def watch(
        self,
        docker: FakeDocker,
        workspace: Path,
        sessions: Path,
        *,
        creates: tuple[str, ...] = (CONTAINER,),
        removes: bool = False,
    ) -> SandboxWitness:
        """Run one 'session' that creates its containers the way Hermes does: partway in."""
        witness = SandboxWitness(sessions, workspace, IMAGE, IMAGE_ID, run=docker, poll_seconds=0.01)

        def session(command, workspace_arg, env, timeout) -> int:
            docker.containers.extend(creates)
            if removes:
                # What `docker_persist_across_processes: false` does at exit, once the
                # witness has seen what it is there to see.
                deadline = time.monotonic() + 5
                while len(witness.containers) < len(creates) and time.monotonic() < deadline:
                    time.sleep(0.01)
                for container in creates:
                    docker.containers.remove(container)
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

            witness = self.watch(docker, workspace, sessions, removes=True)

            self.assertEqual([], witness.violations)
            # Named for the session that created it: run_agent.sh writes its controls
            # report before it launches the agent, so the newest one names this session.
            receipt = json.loads((sessions / f"20260928T121455Z-3087{WITNESS_SUFFIX}").read_text(encoding="utf-8"))
            self.assertTrue(receipt["observed"])
            self.assertTrue(receipt["default_backend_verified"])
            self.assertTrue(receipt["default_backend_removed_after_exit"])
            record = receipt["containers"][0]
            self.assertEqual("default", record["task_id"])
            self.assertFalse(record["existed_in_baseline"])
            self.assertEqual(CONTAINER, record["container_id"])
            self.assertEqual(IMAGE_ID, record["image_id"])
            self.assertEqual("none", record["network_mode"])
            self.assertEqual({"source": str(workspace), "destination": "/workspace", "rw": True}, record["workspace_mount"])
            self.assertEqual({"hermes-agent": "1", "hermes-task-id": "default"}, record["labels"])

    def test_every_backend_the_session_built_is_recorded_not_just_the_first(self) -> None:
        # Hermes builds one container per task id: the system prompt's own probe gets
        # `prompt-backend-probe`, and the tools the model calls get `default`. The probe's
        # container appears first, so first-container-wins recorded the one that proves
        # nothing about the backend serving terminal, file and code_execution.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(
                containers=[],
                inspect={
                    PROBE_CONTAINER: inspected(workspace, container=PROBE_CONTAINER, task_id="prompt-backend-probe"),
                    CONTAINER: inspected(workspace),
                },
            )

            witness = self.watch(docker, workspace, sessions, creates=(PROBE_CONTAINER, CONTAINER))

            receipt = self.written(sessions)
            self.assertEqual(
                ["prompt-backend-probe", "default"], sorted((r["task_id"] for r in receipt["containers"]), reverse=True)
            )
            self.assertTrue(receipt["default_backend_verified"])
            self.assertEqual([], witness.violations)

    def test_a_session_that_only_built_the_prompt_probe_has_not_proved_the_default_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            probe = inspected(workspace, container=PROBE_CONTAINER, task_id="prompt-backend-probe")
            docker = FakeDocker(containers=[], inspect={PROBE_CONTAINER: probe})

            self.watch(docker, workspace, sessions, creates=(PROBE_CONTAINER,))

            receipt = self.written(sessions)
            self.assertTrue(receipt["observed"])
            self.assertFalse(receipt["default_backend_verified"])
            self.assertIsNone(receipt["default_backend_removed_after_exit"])

    def test_the_session_scoped_backend_is_scored_apart_from_the_default_one(self) -> None:
        # Under `container_persistent: false` a tool call inside an agent turn is keyed by
        # session id, not by "default", and reaches the mount logic by a different path.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(
                containers=[],
                inspect={
                    CONTAINER: inspected(workspace),
                    SESSION_CONTAINER: inspected(workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID),
                },
            )

            witness = self.watch(docker, workspace, sessions, creates=(CONTAINER, SESSION_CONTAINER))

            receipt = self.written(sessions)
            self.assertTrue(receipt["default_backend_verified"])
            self.assertTrue(receipt["session_backend_verified"])
            self.assertEqual([], witness.violations)

    def test_a_session_backend_on_a_tmpfs_is_caught_while_the_default_one_looks_sound(self) -> None:
        # Run 36485906813 exactly: `docker_mount_cwd_to_workspace` bound /workspace for the
        # CLI parent's "default" backend, and `_resolve_task_host_cwd` refused to derive it
        # for the session-scoped one, which got an empty tmpfs instead.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            tmpfs = inspected(workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID, mounts=[])
            docker = FakeDocker(containers=[], inspect={CONTAINER: inspected(workspace), SESSION_CONTAINER: tmpfs})

            witness = self.watch(docker, workspace, sessions, creates=(CONTAINER, SESSION_CONTAINER))

            receipt = self.written(sessions)
            self.assertTrue(receipt["default_backend_verified"])
            self.assertFalse(receipt["session_backend_verified"])
            self.assertEqual(["the agent's container has nothing mounted at /workspace"], witness.violations)

    def test_a_container_that_outlives_its_process_is_reaped_without_losing_that_it_did(self) -> None:
        # Two facts, both kept. `removed_after_exit` is the runtime reading of
        # `docker_persist_across_processes: false` - a container the daemon still lists is
        # one the next process attaches to by label - and rewriting it to say the harness
        # tidied up would erase the only direct evidence there is.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace))

            witness = self.watch(docker, workspace, sessions, removes=False)

            receipt = self.written(sessions)
            self.assertFalse(receipt["default_backend_removed_after_exit"])
            self.assertEqual(
                {
                    "removed_after_exit": False,
                    "cleanup_attempted": True,
                    "removed_by_harness": True,
                    "removed_after_cleanup": True,
                },
                {
                    key: receipt["containers"][0][key]
                    for key in ("removed_after_exit", "cleanup_attempted", "removed_by_harness", "removed_after_cleanup")
                },
            )
            self.assertEqual([["docker", "rm", "-f", CONTAINER]], [c for c in docker.calls if c[1] == "rm"])
            self.assertEqual([], witness.violations)
            self.assertEqual([], docker.containers)

    def test_a_phase_is_reaped_at_its_own_exit_not_at_the_end_of_the_lane(self) -> None:
        # C-E run several agent invocations through one witness. A timed-out Research
        # container left running while Plan is measured on the same runner is the thing the
        # reap exists to stop, so it happens per invocation - and the second invocation must
        # not re-answer whether the first one's container outlived it.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(
                containers=[],
                inspect={
                    CONTAINER: inspected(workspace),
                    SESSION_CONTAINER: inspected(workspace, container=SESSION_CONTAINER, task_id=SESSION_TASK_ID),
                },
            )
            witness = SandboxWitness(sessions, workspace, IMAGE, IMAGE_ID, run=docker, poll_seconds=0.01)

            def phase(container: str):
                def session(command, workspace_arg, env, timeout) -> int:
                    docker.containers.append(container)
                    return 0

                return witness.wrap(session)

            phase(CONTAINER)("hermes", workspace, {}, 300)
            self.assertEqual([], docker.containers, "the first phase's container is still running during the second")
            phase(SESSION_CONTAINER)("hermes", workspace, {}, 300)

            records = {record["container_id"]: record for record in self.written(sessions)["containers"]}
            self.assertEqual([False, False], [records[c]["removed_after_exit"] for c in (CONTAINER, SESSION_CONTAINER)])
            self.assertEqual([True, True], [records[c]["removed_after_cleanup"] for c in (CONTAINER, SESSION_CONTAINER)])
            self.assertEqual(2, len([call for call in docker.calls if call[1] == "rm"]))
            self.assertEqual([], witness.violations)

    def test_a_container_the_harness_cannot_remove_refuses_the_cell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace), unremovable=(CONTAINER,))

            witness = self.watch(docker, workspace, sessions, removes=False)

            receipt = self.written(sessions)
            record = receipt["containers"][0]
            self.assertTrue(record["cleanup_attempted"])
            self.assertFalse(record["removed_by_harness"])
            self.assertFalse(record["removed_after_cleanup"])
            self.assertEqual([f"the container {CONTAINER} outlived its session and could not be removed"], witness.violations)

    def test_a_session_that_ended_cleanly_is_not_reaped(self) -> None:
        # Hermes removed its own container, so there is nothing to clean up and the receipt
        # should not claim the harness did anything.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace))

            self.watch(docker, workspace, sessions, removes=True)

            record = self.written(sessions)["containers"][0]
            self.assertTrue(record["removed_after_exit"])
            self.assertFalse(record["cleanup_attempted"])
            self.assertIsNone(record["removed_after_cleanup"])

    def test_containers_that_were_here_before_the_cell_are_never_reaped(self) -> None:
        # The reap works off this witness's own ids. A `hermes-agent=1` sweep would also
        # take a container another cell is still working in.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[PROBE_CONTAINER], inspect=inspected(workspace))

            self.watch(docker, workspace, sessions, removes=False)

            self.assertEqual([["docker", "rm", "-f", CONTAINER]], [c for c in docker.calls if c[1] == "rm"])
            self.assertIn(PROBE_CONTAINER, docker.containers)

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
            receipt = self.written(sessions)
            self.assertTrue(receipt["observed"])
            self.assertFalse(receipt["default_backend_verified"])

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
            witness = self.watch(docker, workspace, sessions, creates=())

            self.assertEqual([], witness.violations)
            self.assertEqual(
                {
                    "observed": False,
                    "default_backend_verified": False,
                    "session_backend_verified": False,
                    "default_backend_removed_after_exit": None,
                    "existing_containers": [],
                    "containers": [],
                },
                self.written(sessions),
            )

    def test_a_session_that_reused_an_existing_container_is_a_hard_failure(self) -> None:
        # Reuse leaves no new container, so it looks exactly like "no container" except for
        # what was already lying around - which is the only evidence there is.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=["leftover-from-an-earlier-process"], inspect={})
            witness = self.watch(docker, workspace, sessions, creates=())

            self.assertIn("no container was created and 1 Hermes container(s) were already present", witness.violations[0])
            self.assertEqual(["leftover-from-an-earlier-process"], self.written(sessions)["existing_containers"])

    def test_a_container_that_was_already_running_is_not_mistaken_for_this_cell_s(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=["someone-elses"], inspect=inspected(workspace))
            witness = self.watch(docker, workspace, sessions)

            receipt = self.written(sessions)
            self.assertEqual([CONTAINER], [record["container_id"] for record in receipt["containers"]])
            self.assertEqual([], witness.violations)

    def test_the_probe_names_its_own_receipt_rather_than_borrowing_a_session_s(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace, sessions = Path(temp_dir) / "ws", Path(temp_dir) / "s"
            workspace.mkdir()
            docker = FakeDocker(containers=[], inspect=inspected(workspace))
            witness = SandboxWitness(sessions, workspace, IMAGE, IMAGE_ID, run=docker, poll_seconds=0.01, stem="sandbox-probe")

            def session(command, workspace_arg, env, timeout) -> int:
                docker.containers.append(CONTAINER)
                return 0

            witness.wrap(session)("probe", workspace, {}, 300)

            self.assertTrue((sessions / f"sandbox-probe{WITNESS_SUFFIX}").is_file())


if __name__ == "__main__":
    unittest.main()
