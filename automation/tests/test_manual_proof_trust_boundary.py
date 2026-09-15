"""Trust-boundary regressions for policy.verify_manual_proofs.

Every case starts from one known-good manual proof and mutates exactly one
property, so a failure names the property that stopped being enforced.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from automation.supervisor import policy

HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40
SLICE_ID = "today-prove-the-thing"
PROOF_BYTES = b"screenshot bytes"

Mutate = Callable[[dict[str, Any], dict[str, Any], Path], None]


@contextlib.contextmanager
def workspace():
    """A repo root containing one valid proof, with cwd pointed at it.

    verify_manual_proofs anchors the approved proof root at cwd, so the chdir
    is part of the fixture rather than incidental.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir).resolve()
        proof_dir = root / "automation" / "proofs" / SLICE_ID
        proof_dir.mkdir(parents=True)
        proof_file = proof_dir / "screenshot.png"
        proof_file.write_bytes(PROOF_BYTES)

        slice_record = {
            "slice_id": SLICE_ID,
            "manual_proof": ["screenshot"],
            "required_proof_level": "screenshot-verified",
            "allowed_paths": ["docs/product/domains/today.md"],
            "required_validations": ["git diff --check"],
            "max_files_changed": 5,
        }
        handoff = {
            "slice_id": SLICE_ID,
            "proof_level": "screenshot-verified",
            "verified_commit_sha": HEAD_SHA,
            "provided_proofs": [
                {
                    "proof_type": "screenshot",
                    "path": str(proof_file),
                    "commit_sha": HEAD_SHA,
                    "created_at": "2026-09-14T00:00:00Z",
                    "slice_id": SLICE_ID,
                    "sha256": hashlib.sha256(PROOF_BYTES).hexdigest(),
                }
            ],
        }
        with contextlib.chdir(root):
            yield root, slice_record, handoff


def completion_fields() -> dict[str, Any]:
    """The non-proof handoff keys evaluate_completion reads."""
    return {
        "status": "done",
        "validations_passed": ["git diff --check"],
        "validations_failed": [],
        "files_touched": ["docs/product/domains/today.md"],
        "dirty_paths_outside_scope": [],
        "recommended_next_slice": "",
        "recommended_next_reason": "",
    }


def drop_proof(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"] = []


def wrong_slice_id(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"][0]["slice_id"] = "today-some-other-slice"


def stale_commit_sha(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"][0]["commit_sha"] = OTHER_SHA


def path_outside_proof_root(slice_record: dict, handoff: dict, root: Path) -> None:
    stray = root / "screenshot.png"
    stray.write_bytes(PROOF_BYTES)
    handoff["provided_proofs"][0]["path"] = str(stray)


def path_traversal(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"][0]["path"] = str(root / "automation" / "proofs" / SLICE_ID / ".." / ".." / ".." / "screenshot.png")


def symlink_escaping_proof_root(slice_record: dict, handoff: dict, root: Path) -> None:
    outside = root / "outside.png"
    outside.write_bytes(PROOF_BYTES)
    link = root / "automation" / "proofs" / SLICE_ID / "linked.png"
    link.symlink_to(outside)
    handoff["provided_proofs"][0]["path"] = str(link)


def file_absent(slice_record: dict, handoff: dict, root: Path) -> None:
    (root / "automation" / "proofs" / SLICE_ID / "screenshot.png").unlink()


def sha256_mismatch(slice_record: dict, handoff: dict, root: Path) -> None:
    (root / "automation" / "proofs" / SLICE_ID / "screenshot.png").write_bytes(b"swapped after recording")


def sha256_absent(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["provided_proofs"][0]["sha256"] = ""


def proof_level_below_required(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["proof_level"] = "flow-verified"


def proof_level_above_required(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["proof_level"] = "device-verified"


def verified_sha_absent(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff.pop("verified_commit_sha")


def verified_sha_null(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["verified_commit_sha"] = None


def verified_sha_mismatched(slice_record: dict, handoff: dict, root: Path) -> None:
    handoff["verified_commit_sha"] = OTHER_SHA


# name, mutation, expected fragment in missing_manual_proofs, expected missing_proof_level
CASES: tuple[tuple[str, Mutate | None, str | None, bool], ...] = (
    ("baseline_valid_proof", None, None, False),
    ("required_proof_not_provided", drop_proof, "screenshot", False),
    ("proof_recorded_for_another_slice", wrong_slice_id, "screenshot", False),
    ("proof_recorded_against_stale_commit", stale_commit_sha, "screenshot", False),
    ("proof_path_outside_proof_root", path_outside_proof_root, "outside approved proof root", False),
    ("proof_path_traverses_out_of_proof_root", path_traversal, "outside approved proof root", False),
    ("proof_path_symlinks_out_of_proof_root", symlink_escaping_proof_root, "outside approved proof root", False),
    ("proof_file_missing_on_disk", file_absent, "missing file", False),
    ("proof_file_changed_after_recording", sha256_mismatch, "sha256 mismatch", False),
    ("proof_metadata_has_no_sha256", sha256_absent, "missing sha256 in metadata", False),
    ("proof_level_below_required", proof_level_below_required, None, True),
    ("proof_level_above_required", proof_level_above_required, None, False),
    ("verified_commit_sha_absent", verified_sha_absent, "Missing verified_commit_sha", False),
    ("verified_commit_sha_null", verified_sha_null, "Missing verified_commit_sha", False),
    ("verified_commit_sha_mismatched", verified_sha_mismatched, "Invalid verified_commit_sha", False),
)


class ManualProofTrustBoundaryTest(unittest.TestCase):
    def test_one_mutation_per_case(self) -> None:
        for name, mutate, expected_fragment, expects_missing_level in CASES:
            with self.subTest(case=name), workspace() as (root, slice_record, handoff):
                slice_record, handoff = copy.deepcopy(slice_record), copy.deepcopy(handoff)
                if mutate is not None:
                    mutate(slice_record, handoff, root)

                missing_proofs, missing_level = policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA)

                if expected_fragment is None:
                    self.assertEqual([], missing_proofs)
                else:
                    self.assertTrue(
                        any(expected_fragment in entry for entry in missing_proofs),
                        f"{name}: expected {expected_fragment!r} in {missing_proofs}",
                    )
                self.assertEqual(expects_missing_level, missing_level, name)

    def test_proof_level_exactly_required_is_accepted(self) -> None:
        with workspace() as (_, slice_record, handoff):
            missing_proofs, missing_level = policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA)
        self.assertEqual(([], False), (missing_proofs, missing_level))

    def completion_decision(self, slice_record: dict, handoff: dict) -> policy.CompletionDecision:
        """Run the same proof through the supervisor completion boundary."""
        queue_data = {
            "policy": {"supervisor_owned_paths": ["automation/queue/slices.json", "automation/handoffs/"]},
            "slices": [slice_record],
        }
        return policy.evaluate_completion(
            queue_data=queue_data,
            slice_record=slice_record,
            handoff=handoff,
            dirty_paths_before_run=[],
            dirty_paths_after_run=handoff["files_touched"],
            completed_autonomous_runs=1,
            run_limit=2,
            post_run_commit_sha=HEAD_SHA,
        )

    def test_completion_rejects_done_handoff_without_verified_commit_sha(self) -> None:
        with workspace() as (_, slice_record, handoff):
            handoff.update(completion_fields())
            handoff.pop("verified_commit_sha")
            decision = self.completion_decision(slice_record, handoff)

        self.assertEqual("failed", decision.queue_status)
        self.assertEqual("stop_failed", decision.decision)
        self.assertFalse(decision.should_continue)
        self.assertTrue(
            any("Missing verified_commit_sha" in entry for entry in decision.missing_manual_proofs),
            decision.missing_manual_proofs,
        )

    def test_completion_accepts_done_handoff_with_valid_proof(self) -> None:
        """Control: the rejection above must come from the proof, not the rest of the handoff."""
        with workspace() as (_, slice_record, handoff):
            handoff.update(completion_fields())
            decision = self.completion_decision(slice_record, handoff)

        self.assertEqual("done", decision.queue_status)
        self.assertEqual([], decision.missing_manual_proofs)
        self.assertFalse(decision.missing_proof_level)

    def test_verified_commit_sha_not_required_when_no_manual_proofs(self) -> None:
        with workspace() as (_, slice_record, handoff):
            slice_record["manual_proof"] = []
            handoff.pop("verified_commit_sha")
            missing_proofs, missing_level = policy.verify_manual_proofs(slice_record, handoff, HEAD_SHA)
        self.assertEqual([], missing_proofs)


if __name__ == "__main__":
    unittest.main()
