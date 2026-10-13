"""The session diagnosis, against what the pinned Hermes really leaves on disk.

Each case builds the files one session would leave - controls receipt, log, usage report,
stream, witness receipt - plus a ``state.db`` with the three tables the diagnosis reads, at
the columns pinned Hermes ``b6a87db`` declares them with. The cases are the failure modes the
plan phase could be in: never reaching the model, waiting on a call that never completed,
answering without acting, a tool that never returned, working until the budget, exiting
without the artifact, and succeeding.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

from automation.benchmark import diagnosis
from automation.benchmark.adapter import AGENT_SESSIONS_DIR, SubmissionResult
from automation.benchmark.instances import TaskSpec
from automation.benchmark.strategies import PhaseResult, StrategyResult

# 2026-10-12T12:56:44Z, the launch second of run 38248084765's plan session.
LAUNCH = 1791809804.0
STEM = "20261012T125644Z-42025"
RESEARCH_STEM = "20261012T122043Z-3852"

_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
    ended_at REAL, end_reason TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    api_call_count INTEGER DEFAULT 0, last_activity_at REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
    finish_reason TEXT);
CREATE TABLE session_model_usage (session_id TEXT NOT NULL, model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    first_seen REAL, last_seen REAL);
"""


def phase(returncode: int, seconds: float, artifact_valid: Optional[bool], name: str = "plan") -> dict[str, Any]:
    return PhaseResult(
        phase=name, returncode=returncode, seconds=seconds, prompt_chars=1, artifact_valid=artifact_valid
    ).to_dict()


class Session:
    """One session's leftovers, written the way run_agent.sh and Hermes write them."""

    def __init__(self, root: Path, stem: str = STEM, phase_name: str = "plan", transport: str = "oneshot") -> None:
        self.dir = root / AGENT_SESSIONS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stem = stem
        self.home = root / f"hermes-home-{stem}"
        self.home.mkdir()
        controls = {
            "runner": "hermes",
            "transport": transport,
            "slice_id": f"task-1a2b:{phase_name}",
            "hermes_home": str(self.home),
        }
        self.file(".controls.json", json.dumps(controls))
        self.file(".log", "")

    def file(self, suffix: str, text: str) -> None:
        (self.dir / f"{self.stem}{suffix}").write_text(text, encoding="utf-8")

    def ledger(self, *, row: bool = True, calls: int = 0, messages: tuple[tuple[str, float, bool], ...] = ()) -> None:
        """``messages`` are (role, seconds after launch, carries tool_calls)."""
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.executescript(_SCHEMA)
            if row:
                conn.execute(
                    "INSERT INTO sessions (id, source, started_at, api_call_count, last_activity_at) VALUES (?,?,?,?,?)",
                    ("s1", "cli", LAUNCH + 3, calls, LAUNCH + 3),
                )
            if calls:
                conn.execute(
                    "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
                    ("s1", "z-ai/glm-5.3", "nvidia", calls, 900, 40, LAUNCH + 60, LAUNCH + 60 * calls),
                )
            for role, at, has_calls in messages:
                conn.execute(
                    "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?,?,?,?)",
                    ("s1", role, '[{"name": "terminal"}]' if has_calls else None, LAUNCH + at),
                )

    def witness(self, *created_at: str) -> None:
        containers = [{"task_id": f"t{i}", "container_id": f"c{i}", "created_at": at} for i, at in enumerate(created_at)]
        self.file(".sandbox.json", json.dumps({"observed": True, "containers": containers}))


def diagnose(session: Session, result: Optional[dict[str, Any]]) -> dict[str, Any]:
    return diagnosis.diagnose_session(session.dir, session.stem, result)


class VerdictTests(unittest.TestCase):
    def test_a_plan_that_wrote_its_artifact_is_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=3, messages=(("assistant", 50, True), ("tool", 55, False), ("assistant", 120, False)))
            session.file(".usage.json", json.dumps({"model": "z-ai/glm-5.3", "api_calls": 3}))
            record = diagnose(session, phase(0, 130.0, True))
        self.assertEqual(diagnosis.COMPLETED, record["verdict"])
        self.assertEqual("plan", record["phase"])
        self.assertEqual(42025, record["pid"])
        self.assertEqual(3.0, record["timeline_seconds"]["turn_started"])
        self.assertEqual(50.0, record["timeline_seconds"]["first_tool_call"])
        self.assertEqual(900, record["observed"]["reasoning_tokens"])

    def test_a_session_whose_hermes_never_opened_its_store_did_not_start(self) -> None:
        """The home exists, run_agent.sh made it; no state.db means Hermes never got that far."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.EVIDENCE_UNAVAILABLE, record["verdict"])
        self.assertTrue(any("never created state.db" in item for item in record["missing_evidence"]))

    def test_a_store_without_a_session_row_is_a_startup_stall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=False)
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.STARTUP_INCOMPLETE, record["verdict"])
        self.assertTrue(record["terminated"]["by_budget"])

    def test_a_turn_with_no_completed_call_names_what_it_cannot_split(self) -> None:
        """A provider stall and one long reasoning call look the same, and the verdict says so."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True, calls=0)
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.NO_COMPLETED_MODEL_CALL, record["verdict"])
        self.assertIn("reasoning", record["explanation"])
        self.assertIn("never answered", record["explanation"])
        self.assertEqual(3.0, record["timeline_seconds"]["turn_started"])
        self.assertIsNone(record["timeline_seconds"]["first_completed_model_call"])

    def test_a_provider_refusal_still_reads_as_no_completed_call(self) -> None:
        """The refusal itself is the lane's provider classifier's job; this says how far it got."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True, calls=0)
            session.file(".log", "❌ API failed after 3 retries — Connection error.\n")
            record = diagnose(session, phase(1, 40.0, False))
        self.assertEqual(diagnosis.NO_COMPLETED_MODEL_CALL, record["verdict"])
        self.assertFalse(any(item.startswith("session log") for item in record["missing_evidence"]))

    def test_a_model_that_answered_without_tools_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=2, messages=(("assistant", 200, False),))
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.NO_TOOL_CALL, record["verdict"])

    def test_a_tool_call_that_never_returned_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=2, messages=(("assistant", 60, True), ("tool", 70, False), ("assistant", 90, True)))
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.TOOL_CALL_UNANSWERED, record["verdict"])
        self.assertEqual((2, 1), (record["observed"]["tool_calls"], record["observed"]["tool_results"]))

    def test_a_session_working_when_the_budget_ran_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(
                calls=9, messages=(("assistant", 60, True), ("tool", 70, False), ("assistant", 1070, True), ("tool", 1075, False))
            )
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.ACTIVE_UNTIL_BUDGET, record["verdict"])
        self.assertEqual(1075.0, record["timeline_seconds"]["last_activity"])
        self.assertEqual(1080.0, record["terminated"]["at_seconds"])

    def test_an_incomplete_artifact_on_exit_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=4, messages=(("assistant", 60, True), ("tool", 70, False)))
            record = diagnose(session, phase(0, 300.0, False))
        self.assertEqual(diagnosis.EXITED_WITHOUT_ARTIFACT, record["verdict"])


class MissingEvidenceTests(unittest.TestCase):
    def test_an_oneshot_kill_names_every_absent_source_and_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            record = diagnose(session, phase(124, 1080.0, False))
        missing = "\n".join(record["missing_evidence"])
        self.assertIn("session log: 0 bytes - --oneshot writes nothing until the turn returns", missing)
        self.assertIn("usage report: absent - --oneshot writes it only when the turn returns", missing)
        self.assertIn("sandbox observation", missing)

    def test_an_empty_event_stream_is_reported_rather_than_read_as_silence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), transport="stream-json")
            session.file(".stream.jsonl", "")
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(0, record["observed"]["stream_events"])
        self.assertIn("event stream: empty - not even system/init was flushed", record["missing_evidence"])
        self.assertEqual(diagnosis.EVIDENCE_UNAVAILABLE, record["verdict"])

    def test_a_stream_that_only_initialised_has_started_but_completed_no_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), transport="stream-json")
            session.file(
                ".stream.jsonl",
                json.dumps({"type": "system", "subtype": "init", "timestamp": (LAUNCH + 6) * 1000}) + '\n{"type": "te',
            )
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(1, record["observed"]["stream_events"])
        self.assertEqual(6.0, record["timeline_seconds"]["first_stream_event"])
        self.assertEqual(diagnosis.NO_COMPLETED_MODEL_CALL, record["verdict"])

    def test_a_home_read_off_its_runner_is_said_to_be_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.home.rmdir()
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertTrue(any("is gone" in item for item in record["missing_evidence"]))
        self.assertEqual(diagnosis.EVIDENCE_UNAVAILABLE, record["verdict"])

    def test_without_a_phase_result_the_session_is_described_not_judged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=2, messages=(("assistant", 60, True), ("tool", 70, False)))
            record = diagnose(session, None)
        self.assertIsNone(record["terminated"])
        self.assertEqual(diagnosis.FAILED, record["verdict"])


class ContainerProvenanceTests(unittest.TestCase):
    def test_research_containers_are_not_credited_to_plan(self) -> None:
        """Run 38248084765: one lane-wide witness, its research container copied into plan's receipt."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.witness("2026-10-12T12:21:57.317312761Z")
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual([], record["containers"])
        self.assertEqual(1, record["containers_from_other_sessions"])
        self.assertIsNone(record["timeline_seconds"]["first_container"])

    def test_a_container_created_inside_the_window_is_this_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.witness("2026-10-12T12:21:57.317312761Z", "2026-10-12T12:57:30.5Z", "2026-10-12T13:20:00Z")
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(["c1"], [c["container_id"] for c in record["containers"]])
        self.assertEqual(46.5, record["containers"][0]["at_seconds"])
        self.assertEqual(2, record["containers_from_other_sessions"])

    def test_sessions_are_matched_to_phases_by_slice_id_not_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            research = Session(Path(tmp), stem=RESEARCH_STEM, phase_name="research")
            plan = Session(Path(tmp))
            for session in (research, plan):
                session.witness("2026-10-12T12:21:57.317312761Z")
            records = diagnosis.diagnose_sessions(
                research.dir, [phase(124, 1080.0, False), phase(124, 2160.0, True, name="research")]
            )
        self.assertEqual(["research", "plan"], [r["phase"] for r in records])
        self.assertEqual(2160.0, records[0]["terminated"]["at_seconds"])
        self.assertEqual(1, len(records[0]["containers"]))
        self.assertEqual(0, len(records[1]["containers"]))


class ReadOnlyTests(unittest.TestCase):
    def test_the_diagnosis_changes_nothing_it_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=1, messages=(("assistant", 60, True),))
            session.witness("2026-10-12T12:57:30Z")
            files = sorted(Path(tmp).rglob("*"))
            before = {path: path.read_bytes() for path in files if path.is_file()}
            diagnose(session, phase(124, 1080.0, False))
            after = {path: path.read_bytes() for path in sorted(Path(tmp).rglob("*")) if path.is_file()}
        self.assertEqual(before, after)

    def test_message_content_never_enters_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=1)
            with sqlite3.connect(session.home / "state.db") as conn:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'assistant', 'SECRET-PROSE', ?)",
                    (LAUNCH + 9,),
                )
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertNotIn("SECRET-PROSE", json.dumps(record))


class CellRecordTests(unittest.TestCase):
    def test_every_cell_records_its_session_diagnoses(self) -> None:
        from automation.benchmark import lanes as lanes_module

        class Adapter:
            def produce_submission(self, task: TaskSpec, out_tar: Path) -> SubmissionResult:
                Session(out_tar.parent).ledger(row=False)
                strategy = StrategyResult(
                    lane="C",
                    returncode=124,
                    phases=(PhaseResult(phase="plan", returncode=124, seconds=1080.0, prompt_chars=1, artifact_valid=False),),
                )
                return SubmissionResult(task.instance_id, out_tar, out_tar.parent, 124, strategy=strategy)

        with tempfile.TemporaryDirectory() as tmp:
            cell = lanes_module.Cell("C", 1, "abishekvashok__cmatrix.5c082c6")
            provenance = lanes_module.run_cell(cell, Path(tmp), Adapter(), Path(tmp))
        self.assertEqual([diagnosis.STARTUP_INCOMPLETE], [d["verdict"] for d in provenance["session_diagnoses"]])


if __name__ == "__main__":
    unittest.main()
