"""The session diagnosis, against what the pinned Hermes really leaves on disk.

Each case builds the files one session would leave - controls receipt, log, usage report,
stream, witness receipt - plus a ``state.db`` with the three tables the diagnosis reads, at
the columns pinned Hermes ``b6a87db`` declares them with. The cases are the failure modes the
plan phase could be in: never reaching the model, waiting on a call that never completed,
answering without acting, a tool that never returned, working until the budget, exiting
without the artifact, and succeeding.
"""

from __future__ import annotations

import contextlib
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
    api_call_count INTEGER DEFAULT 0, last_activity_at REAL, last_activity_description TEXT,
    last_activity_provenance TEXT);
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

    def ledger(
        self,
        *,
        row: bool = True,
        calls: int = 0,
        messages: tuple[tuple[str, float, bool], ...] = (),
        activity: Optional[tuple[float, str]] = None,
    ) -> None:
        """``messages`` are (role, seconds after launch, carries tool_calls); ``activity`` is
        (seconds after launch, the persisted activity description)."""
        at, description = activity or (3, None)
        with sqlite3.connect(self.home / "state.db") as conn:
            conn.executescript(_SCHEMA)
            if row:
                conn.execute(
                    "INSERT INTO sessions (id, source, started_at, api_call_count, last_activity_at,"
                    " last_activity_description, last_activity_provenance) VALUES (?,?,?,?,?,?,?)",
                    ("s1", "cli", LAUNCH + 3, calls, LAUNCH + at, description, "unknown" if description else None),
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

    def test_an_empty_ledger_is_not_read_as_a_startup_stall(self) -> None:
        """Hermes swallows a failed turn-start write, so an empty store has two readings."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=False)
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.NO_TURN_START_RECORDED, record["verdict"])
        self.assertIn("never started", record["explanation"])
        self.assertIn("persistence failed", record["explanation"])
        self.assertNotIn("never reached the model", record["explanation"])
        self.assertTrue(record["terminated"]["by_budget"])

    def test_a_failed_turn_start_write_with_later_evidence_is_not_a_startup_stall(self) -> None:
        """The session row was swallowed, but a completed call and a tool call were persisted."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=False, calls=2, messages=(("assistant", 60, True), ("tool", 70, False)))
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(0, record["observed"]["session_rows"])
        self.assertEqual(diagnosis.ACTIVE_UNTIL_BUDGET, record["verdict"])

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
        self.assertEqual(diagnosis.NO_TOOL_CALL_RECORDED, record["verdict"])

    def test_a_killed_oneshot_with_completed_calls_and_no_messages_claims_no_inactivity(self) -> None:
        """A tool requested and stalled before Hermes flushed its message leaves exactly this."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=1)
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.NO_TOOL_CALL_RECORDED, record["verdict"])
        self.assertEqual((1, 0), (record["observed"]["completed_model_calls"], record["observed"]["messages"]))
        self.assertIn("stalling", record["explanation"])
        self.assertNotIn("never called", record["explanation"])

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


class ActivityDescriptionTests(unittest.TestCase):
    """The last persisted activity label, verbatim from pinned Hermes ``b6a87db``'s streaming path."""

    def activity(self, description: str, at: float = 1025.0) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), transport="stream-json")
            session.ledger(row=True, calls=0, activity=(at, description))
            return diagnose(session, phase(124, 1080.0, False))["last_persisted_activity"]

    def test_a_stream_still_delivering_chunks_reads_as_receiving(self) -> None:
        record = self.activity("receiving stream response")
        self.assertEqual("receiving_chunks", record["state"])
        self.assertEqual(1025.0, record["at_seconds"])
        self.assertEqual("receiving stream response", record["description"])

    def test_a_stream_that_never_sent_a_chunk_reads_as_awaiting_the_first(self) -> None:
        for description in (
            "waiting for provider response (streaming)",
            "waiting for stream response (120s, first_chunk)",
            "⏳ waiting on z-ai/glm-5.3 — 60s waiting for the first stream chunk (auto-reconnect: stream stale watchdog in 120s)",
        ):
            with self.subTest(description=description):
                self.assertEqual("awaiting_first_chunk", self.activity(description)["state"])

    def test_a_stream_that_went_quiet_after_chunks_is_told_apart(self) -> None:
        for description in (
            "waiting for stream response (90s, post_chunk)",
            "⏳ waiting on z-ai/glm-5.3 — stream open; 60s without stream output",
        ):
            with self.subTest(description=description):
                self.assertEqual("silent_after_chunks", self.activity(description)["state"])

    def test_a_stale_stream_or_retry_reads_as_reconnecting(self) -> None:
        for description in (
            "stale stream detected after 181s, reconnecting",
            "stream retry 2/5 after ReadTimeout",
            "⚠ no output from provider for 181s — reconnecting...",
        ):
            with self.subTest(description=description):
                self.assertEqual("reconnecting", self.activity(description)["state"])

    def test_a_backoff_between_attempts_reads_as_retry_backoff(self) -> None:
        for description in (
            "retry backoff (2/3), 30s remaining",
            "error retry backoff (1/3), 45s remaining",
            "empty response retry backoff (1/2), 12s remaining",
            "auto-recovery wait (1/4), 270s remaining",
            "API error recovery (attempt 2/3)",
            "⏳ waiting on provider — retrying in 60s (attempt 2/3)",
        ):
            with self.subTest(description=description):
                self.assertEqual("retry_backoff", self.activity(description)["state"])

    def test_a_non_streaming_call_is_told_apart(self) -> None:
        for description in ("waiting for non-streaming API response", "stale non-streaming call killed after 300s"):
            with self.subTest(description=description):
                self.assertEqual("non_streaming", self.activity(description)["state"])

    def test_no_chunks_yet_is_not_read_as_no_chunk_ever(self) -> None:
        """Hermes writes it under 60s since the last chunk or the stream's opening, so it settles nothing."""
        self.assertEqual("other", self.activity("waiting for stream response (30s, no chunks yet)")["state"])
        self.assertEqual("other", self.activity("starting API call #1")["state"])

    def test_a_description_never_persisted_is_not_one_that_was_cleared(self) -> None:
        """NULL: nothing was ever written. '': the turn's finally - SIGTERM's too - wiped it."""
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True)
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.ACTIVITY_NEVER_PERSISTED, record["last_persisted_activity"]["state"])
        self.assertEqual("state_db_after_exit", record["last_persisted_activity"]["source"])
        self.assertFalse(any(item.startswith("activity description") for item in record["missing_evidence"]))
        self.assertEqual(diagnosis.ACTIVITY_CLEARED, self.activity("")["state"])

    def test_the_pre_sigterm_snapshot_wins_over_the_cleared_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), transport="stream-json")
            session.ledger(row=True, activity=(1080.0, ""))
            row = {
                "last_activity_at": LAUNCH + 1025,
                "last_activity_description": "receiving stream response",
                "last_activity_provenance": "unknown",
            }
            session.file(".activity.json", json.dumps({"captured_at": LAUNCH + 1080, "row": row}))
            record = diagnose(session, phase(124, 1080.0, False))
        activity = record["last_persisted_activity"]
        self.assertEqual(
            ("pre_sigterm_snapshot", "receiving_chunks", 1025.0), (activity["source"], activity["state"], activity["at_seconds"])
        )
        self.assertFalse(any(item.startswith("activity snapshot") for item in record["missing_evidence"]))

    def test_a_budget_kill_without_a_snapshot_says_the_label_may_be_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True, activity=(1080.0, ""))
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.ACTIVITY_CLEARED, record["last_persisted_activity"]["state"])
        self.assertTrue(any("SIGTERM may have cleared" in item for item in record["missing_evidence"]))

    def test_a_failed_snapshot_is_named_and_the_row_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True, activity=(1080.0, ""))
            session.file(".activity.json", json.dumps({"captured_at": LAUNCH + 1080, "error": "OperationalError: locked"}))
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual("state_db_after_exit", record["last_persisted_activity"]["source"])
        self.assertIn("activity snapshot: OperationalError: locked", record["missing_evidence"])

    def test_a_store_without_the_column_is_named_missing_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            with sqlite3.connect(session.home / "state.db") as conn:
                conn.execute(
                    "CREATE TABLE sessions (id TEXT, started_at REAL, ended_at REAL, end_reason TEXT,"
                    " api_call_count INTEGER, message_count INTEGER, tool_call_count INTEGER, last_activity_at REAL)"
                )
                conn.execute(
                    "CREATE TABLE messages (id INTEGER, role TEXT, timestamp REAL, tool_name TEXT,"
                    " finish_reason TEXT, tool_calls TEXT)"
                )
                conn.execute(
                    "CREATE TABLE session_model_usage (model TEXT, billing_provider TEXT, api_call_count INTEGER,"
                    " reasoning_tokens INTEGER, output_tokens INTEGER, first_seen REAL, last_seen REAL)"
                )
                conn.execute("INSERT INTO sessions VALUES ('s1', ?, NULL, NULL, 0, 1, 0, ?)", (LAUNCH + 3, LAUNCH + 3))
            record = diagnose(session, phase(124, 1080.0, False))
        self.assertEqual(diagnosis.NO_COMPLETED_MODEL_CALL, record["verdict"])
        self.assertEqual(diagnosis.ACTIVITY_UNAVAILABLE, record["last_persisted_activity"]["state"])
        self.assertIn(
            "activity description: this state.db predates sessions.last_activity_description", record["missing_evidence"]
        )

    def test_the_summary_prints_the_label_beside_its_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp), transport="stream-json")
            session.ledger(row=True, activity=(1025.0, "receiving stream response"))
            lines = diagnosis.summarize(diagnose(session, phase(124, 1080.0, False)))
        self.assertIn("last activity receiving_chunks at 1025.0s (state_db_after_exit): 'receiving stream response'", lines)


class ActivitySnapshotTests(unittest.TestCase):
    """The read the adapter takes just before SIGTERM, which Hermes answers by clearing the label."""

    def test_the_session_launched_since_is_snapshotted_beside_its_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True, activity=(1025.0, "receiving stream response"))
            diagnosis.snapshot_activity(session.dir, frozenset())
            snapshot = json.loads((session.dir / f"{STEM}.activity.json").read_text(encoding="utf-8"))
        self.assertEqual("receiving stream response", snapshot["row"]["last_activity_description"])
        self.assertNotIn("error", snapshot)

    def test_sessions_announced_before_launch_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(row=True)
            diagnosis.snapshot_activity(session.dir, frozenset({STEM}))
            self.assertFalse((session.dir / f"{STEM}.activity.json").exists())

    def test_a_missing_store_is_written_down_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            diagnosis.snapshot_activity(session.dir, frozenset())
            snapshot = json.loads((session.dir / f"{STEM}.activity.json").read_text(encoding="utf-8"))
        self.assertIn("does not exist", snapshot["error"])

    def test_the_budget_kill_snapshots_before_it_signals(self) -> None:
        """The real kill path, with a child that clears its label on SIGTERM the way Hermes does.

        The child's handler blanks ``last_activity_description`` and then drops a marker, and the
        test's ``killpg`` waits for that marker after delivering SIGTERM. So a snapshot taken after
        the signal reliably reads ``''``, and only one taken before it keeps the label.
        """
        import os
        import signal
        import sys
        import time
        from unittest import mock

        from automation.benchmark import adapter

        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            database = home / "state.db"
            with sqlite3.connect(database) as conn:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT INTO sessions (id, source, started_at, last_activity_at, last_activity_description)"
                    " VALUES ('s1', 'cli', 1, 2, 'stream retry 1/5 after ReadTimeout')"
                )
            cleared = Path(tmp) / "cleared"
            pid_file = Path(tmp) / "child.pid"
            controls = sessions / f"{STEM}.controls.json"

            def reap() -> None:
                """Never leave the child's group running, whichever assertion fails."""
                with contextlib.suppress(OSError, ValueError):
                    os.killpg(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)

            child = Path(tmp) / "child.py"
            child.write_text(
                "import json, os, signal, sqlite3, sys, time\n"
                "def clear(*_):\n"
                f"    with sqlite3.connect({str(database)!r}) as conn:\n"
                "        conn.execute(\"UPDATE sessions SET last_activity_description = '',"
                " last_activity_provenance = 'unknown'\")\n"
                f"    open({str(cleared)!r}, 'w').close()\n"
                "    sys.exit(143)\n"
                "signal.signal(signal.SIGTERM, clear)\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                f"open({str(controls)!r}, 'w').write(json.dumps({{'hermes_home': {str(home)!r}}}))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            real_killpg = os.killpg

            def killpg_then_await_clear(pgid: int, signum: int) -> None:
                real_killpg(pgid, signum)
                deadline = time.monotonic() + 5
                while signum == signal.SIGTERM and not cleared.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)

            env = {"PATH": "/usr/bin:/bin", "REPO_AUTOMATION_HERMES_USAGE_DIR": str(sessions)}
            try:
                with mock.patch.object(adapter.os, "killpg", killpg_then_await_clear):
                    returncode = adapter._subprocess_runner(f"exec {sys.executable} {child}", Path(tmp), env, 2)
                snapshot = json.loads((sessions / f"{STEM}.activity.json").read_text(encoding="utf-8"))
                handler_ran = cleared.exists()
                with contextlib.closing(sqlite3.connect(database)) as conn:
                    (after_exit,) = conn.execute("SELECT last_activity_description FROM sessions").fetchone()
            finally:
                reap()
        self.assertEqual(124, returncode)
        self.assertTrue(handler_ran, "the child's SIGTERM handler never ran")
        self.assertEqual("", after_exit)
        self.assertEqual("stream retry 1/5 after ReadTimeout", snapshot["row"]["last_activity_description"])


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


def pool_entry(source: str, requests: int, status: Optional[str] = None, code: Optional[int] = None, **extra: Any) -> dict:
    """One ``credential_pool`` row in the shape pinned Hermes (b6a87db) writes to auth.json."""
    return {
        "id": source[-6:],
        "label": source.split(":", 1)[-1],
        "auth_type": "api_key",
        "priority": 0,
        "source": source,
        "access_token": None,
        "request_count": requests,
        "last_status": status,
        "last_error_code": code,
        **extra,
    }


class CredentialPoolTests(unittest.TestCase):
    """A session the second key served must not look like one the first key served."""

    def _record(self, store: Optional[dict]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(Path(tmp))
            session.ledger(calls=2)
            if store is not None:
                (session.home / diagnosis.AUTH_STORE_FILENAME).write_text(json.dumps(store), encoding="utf-8")
            return diagnose(session, phase(0, 130.0, True))

    def test_a_rotation_shows_the_throttled_key_and_the_one_that_took_over(self) -> None:
        store = {
            "credential_pool": {
                "nvidia": [
                    pool_entry("env:NVIDIA_API_KEY", 3, "exhausted", 429),
                    pool_entry("env:NVIDIA_API_KEY_2", 5),
                ]
            }
        }
        self.assertEqual(
            [
                {
                    "provider": "nvidia",
                    "source": "env:NVIDIA_API_KEY",
                    "last_status": "exhausted",
                    "last_error_code": 429,
                },
                {
                    "provider": "nvidia",
                    "source": "env:NVIDIA_API_KEY_2",
                    "last_status": None,
                    "last_error_code": None,
                },
            ],
            self._record(store)["credential_pool"],
        )

    def test_nothing_but_names_and_counters_is_copied(self) -> None:
        """Hermes can label a row from a token's claims; neither the label nor a token leaves."""
        entry = pool_entry(
            "env:NVIDIA_API_KEY", 1, label="someone@example.com", access_token="nvapi-SECRET", refresh_token="r-SECRET"
        )
        recorded = json.dumps(self._record({"credential_pool": {"nvidia": [entry]}})["credential_pool"])
        for value in ("SECRET", "someone@example.com"):
            self.assertNotIn(value, recorded)

    def test_no_store_or_no_pool_is_none_not_an_empty_pool(self) -> None:
        self.assertIsNone(self._record(None)["credential_pool"])
        self.assertIsNone(self._record({"providers": {}})["credential_pool"])


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
        self.assertEqual([diagnosis.NO_TURN_START_RECORDED], [d["verdict"] for d in provenance["session_diagnoses"]])


if __name__ == "__main__":
    unittest.main()
