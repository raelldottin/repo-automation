"""The cleanroom has to be proved, not configured.

Every test here drives ``cleanroom`` against a stand-in for Docker, because the thing
under test is what the module *concludes* from what a container reports - and the
conclusion that matters is the refusal. A run that scores a lane on a workspace with no
reference executable, or with a working network, is not a ProgramBench inference result,
and the only place that can be caught is before the first agent turn.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from automation.benchmark.cleanroom import (
    CleanroomError,
    image_for,
    prepare,
)

INSTANCE = "abishekvashok__cmatrix.5c082c6"
REPOSITORY = "abishekvashok/cmatrix"

# What a healthy cleanroom's probe container prints: a reference binary, a writable
# workspace, and four egress attempts that each got nowhere.
HEALTHY_PROBE = """reference_executable=yes
workspace_writable=yes
git_worktree=yes
egress=yes|6|curl -sS -m 5 https://github.com
egress=no||wget -q -T 5 -O - https://github.com
egress=yes|128|git ls-remote https://github.com/abishekvashok/cmatrix.git
egress=yes|2|getent hosts github.com
"""


class FakeDocker:
    """A Docker that reports whatever the test needs it to report."""

    def __init__(
        self,
        *,
        contents: dict[str, str],
        probe: str = HEALTHY_PROBE,
        network_mode: str = "none",
        executable: bool = True,
        pull_noise: str = "Unable to find image locally\nlatest: Pulling from programbench/x\n",
    ) -> None:
        self.contents = contents
        self.probe = probe
        self.network_mode = network_mode
        self.pull_noise = pull_noise
        self.executable = executable
        self.calls: list[list[str]] = []
        self._workspace: Path | None = None

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[1] == "create":
            container = "probe-container" if "--network=none" in argv else "copy-container"
            # What a cache miss looks like: `docker create` pulls, and the pull's progress
            # arrives on stderr in front of the container id.
            return 0, f"{self.pull_noise}{container}\n"
        if argv[1] == "cp":
            destination = Path(argv[3])
            for name, body in self.contents.items():
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            if self.executable:
                (destination / "executable").chmod(0o755)
            return 0, ""
        if argv[1] == "image":
            return 0, "sha256:d1ge57\n"
        if argv[1] == "start":
            return 0, self.probe
        if argv[1] == "inspect":
            return 0, self.network_mode + "\n"
        if argv[1] == "rm":
            return 0, ""
        raise AssertionError(f"unexpected docker call: {argv}")

    def call(self, verb: str) -> list[str]:
        return next(argv for argv in self.calls if argv[1] == verb)


def cleanroom_contents() -> dict[str, str]:
    # `.git` is part of what the official image ships: its /workspace is a one-commit
    # repository, built by cloning upstream and then replacing the history wholesale.
    return {
        "executable": "ELF",
        "README.md": "cmatrix - terminal rain",
        "doc/cmatrix.1": ".TH CMATRIX 1",
        ".git/HEAD": "ref: refs/heads/master\n",
    }


class ImageNamingTests(unittest.TestCase):
    def test_the_instance_id_becomes_the_official_image_name(self) -> None:
        # ProgramBench's own rule: Docker repository names cannot hold `__`.
        self.assertEqual(
            "programbench/abishekvashok_1776_cmatrix.5c082c6:task_cleanroom_v6",
            image_for(INSTANCE),
        )


class PrepareTests(unittest.TestCase):
    def prepare(self, docker: FakeDocker, workspace: Path):
        return prepare(INSTANCE, workspace, REPOSITORY, run=docker)

    def test_the_reference_executable_and_its_documentation_arrive_before_the_agent_does(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            receipt = self.prepare(docker, workspace)

            self.assertTrue((workspace / "executable").is_file())
            self.assertTrue(receipt.reference_executable)
            self.assertEqual(["README.md", "doc"], receipt.documentation)
            self.assertEqual("sha256:d1ge57", receipt.image_id)
            self.assertEqual("none", receipt.network_mode)
            # The whole image workspace, not a curated subset: what the image ships is the
            # specification the model is allowed to read.
            self.assertEqual([".git", "README.md", "doc", "executable"], receipt.workspace_entries)
            self.assertTrue(receipt.git_worktree)

    def test_a_pull_on_the_way_in_is_not_mistaken_for_the_container_id(self) -> None:
        # Run 36414921665: the image is never cached on a fresh runner, so `docker create`
        # pulled, and the pull log was passed on as the container id. Every later call
        # addressed a container that does not exist.
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            self.prepare(docker, Path(temp_dir) / "ws")
        self.assertEqual("copy-container:/workspace/.", docker.call("cp")[2])
        self.assertEqual("probe-container", docker.call("inspect")[-1])

    def test_the_probe_runs_air_gapped_in_the_same_image_and_the_same_workspace(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            self.prepare(docker, workspace)

            created = [argv for argv in docker.calls if argv[1] == "create"][-1]
            self.assertIn("--network=none", created)
            self.assertIn(f"{workspace.resolve()}:/workspace", created)
            self.assertIn(image_for(INSTANCE), created)
            # Created and inspected rather than `run --rm`: the network mode has to be read
            # off the container that ran the probes, not off the flags we believe we passed.
            self.assertEqual(
                ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", "probe-container"],
                docker.call("inspect"),
            )

    def test_a_reachable_network_is_refused(self) -> None:
        docker = FakeDocker(
            contents=cleanroom_contents(),
            probe=HEALTHY_PROBE.replace("egress=yes|6|curl", "egress=yes|0|curl"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("network egress succeeded", str(refusal.exception))

    def test_a_container_that_is_not_air_gapped_is_refused_even_when_nothing_reached_out(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), network_mode="bridge")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("NetworkMode='bridge'", str(refusal.exception))

    def test_an_image_too_thin_to_test_is_not_a_tested_cleanroom(self) -> None:
        # Every probe binary missing proves nothing about the network. Passing here would
        # mean the air gap was never checked, only assumed.
        absent = "\n".join(
            line.replace("=yes|6|", "=no||").replace("=yes|128|", "=no||").replace("=yes|2|", "=no||")
            for line in HEALTHY_PROBE.splitlines()
        )
        docker = FakeDocker(contents=cleanroom_contents(), probe=absent)
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("no egress probe could run", str(refusal.exception))

    def test_a_workspace_without_the_reference_binary_is_refused(self) -> None:
        docker = FakeDocker(
            contents={"README.md": "docs only", ".git/HEAD": "ref: x\n"},
            probe=HEALTHY_PROBE.replace("reference_executable=yes", "reference_executable=no"),
            executable=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("no executable reference", str(refusal.exception))

    def test_the_harness_never_creates_the_workspace_it_is_supposed_to_be_measuring(self) -> None:
        # A `git init` here, or a leftover from an earlier cell, would make the cell's
        # environment partly the harness's invention. run_agent.sh needs a worktree; the
        # image ships one, and that is the only one a cleanroom cell may run on.
        docker = FakeDocker(contents=cleanroom_contents())
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "ws"
            workspace.mkdir()
            (workspace / ".git").mkdir()
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, workspace)
        self.assertIn("is not empty", str(refusal.exception))
        self.assertEqual([], [argv for argv in docker.calls if argv[1] == "cp"])

    def test_an_image_that_ships_no_worktree_is_refused_rather_than_git_inited(self) -> None:
        docker = FakeDocker(
            contents={"executable": "ELF", "README.md": "docs"},
            probe=HEALTHY_PROBE.replace("git_worktree=yes", "git_worktree=no"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("ships no Git worktree", str(refusal.exception))

    def test_a_workspace_without_documentation_is_refused_and_says_what_it_found(self) -> None:
        # The task is "rebuild from the executable and the bundled documentation". Half of
        # that being absent changes the task, so it stops the run instead of scoring it.
        docker = FakeDocker(contents={"executable": "ELF", "data.bin": "\0", ".git/HEAD": "ref: x\n"})
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        self.assertIn("no bundled documentation", str(refusal.exception))
        self.assertIn(".git, data.bin, executable", str(refusal.exception))

    def test_a_refused_cleanroom_still_reports_everything_it_observed(self) -> None:
        docker = FakeDocker(contents=cleanroom_contents(), network_mode="bridge")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CleanroomError) as refusal:
                self.prepare(docker, Path(temp_dir) / "ws")
        receipt = refusal.exception.receipt
        assert receipt is not None
        self.assertTrue(receipt.reference_executable)
        self.assertEqual(4, len(receipt.egress_probes))
        self.assertEqual("bridge", receipt.to_dict()["network_mode"])


if __name__ == "__main__":
    unittest.main()
