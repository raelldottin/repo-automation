"""What one agent session left behind, on a single clock, and what that evidence can decide.

Runs 37775851881 and 38248084765 both lost lane C's plan phase the same way: killed at its
1080-second ceiling, no ``plan.json``, a 0-byte log, no usage report. The lane-wide sandbox
witness held one container, created twenty minutes into research, which the plan session's
receipt copied byte for byte - so the only reading of "the plan session" was research's
evidence under plan's name. ``--oneshot`` discards the turn's output and writes its usage
report only when the turn returns, so nothing the lane kept could say whether Hermes ever
reached the model, the model ever answered, or a tool ever ran.

Hermes did keep something. Every session gets its own throwaway ``HERMES_HOME``, and its
``state.db`` is written while the turn runs, on every transport: the ``sessions`` row at turn
start, before the first model call (``agent/turn_context.py`` ``_ensure_session_row``);
``session_model_usage`` once per completed API call, with ``first_seen`` and ``last_seen``;
``messages`` after each tool result (``agent/tool_executor.py``), because a tool can kill
the process before turn-end persistence. A budget kill leaves all of it on disk.

So this module reads, never runs: the controls receipt, the usage report if there is one,
the stream-json events if the transport left any, the session's ``state.db`` read-only by
URI, and the witness's containers - keeping only those created inside this session's own
window, so one phase is never credited with another's container. It adds no model call and
changes no prompt, budget or flag. Every absent source is named as absent, because "nothing
was recorded" and "nothing happened" are different findings and only one of them is ever
supported.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

CONTROLS_SUFFIX = ".controls.json"
USAGE_SUFFIX = ".usage.json"
LOG_SUFFIX = ".log"
STREAM_SUFFIX = ".stream.jsonl"
SANDBOX_SUFFIX = ".sandbox.json"
SESSION_DB_FILENAME = "state.db"
AUTH_STORE_FILENAME = "auth.json"
TIMEOUT_RETURNCODE = 124

# run_agent.sh names every session `<UTC launch second>-<its own pid>`, before it execs Hermes.
_STEM = re.compile(r"^(\d{8}T\d{6}Z)-(\d+)$")

# The verdicts, in the order the evidence is checked. Each names only what was observed.
COMPLETED = "completed"
EVIDENCE_UNAVAILABLE = "evidence_unavailable"
NO_TURN_START_RECORDED = "no_turn_start_recorded"
NO_COMPLETED_MODEL_CALL = "no_completed_model_call"
NO_TOOL_CALL_RECORDED = "no_tool_call_recorded"
TOOL_CALL_UNANSWERED = "tool_call_without_recorded_result"
ACTIVE_UNTIL_BUDGET = "active_until_budget"
EXITED_WITHOUT_ARTIFACT = "exited_without_artifact"
FAILED = "failed"

# The one thing no source here can split, said wherever it applies rather than once in a doc.
_UNSPLITTABLE = (
    "a provider that never answered and a model reasoning through one unfinished call leave the "
    "same evidence: the usage row is written only when a call completes, and neither transport "
    "reports reasoning in progress"
)

# What Hermes was doing the last time it persisted ``sessions.last_activity_description``, from
# the labels pinned ``b6a87db`` stamps on the chat-completions path. The write is rate-limited to
# once per 60s, overwrites, and clamps to 120 characters, so this is one late sample, never a
# history. "waiting for stream response (Ns, no chunks yet)" is deliberately ``other``: despite
# its wording, the stream monitor writes it whenever it has been under 60s since the last chunk
# or since the stream opened, so it cannot say whether a chunk ever arrived.
#
# NULL and '' are different findings. NULL: no description was ever persisted. '': one was, and
# was then cleared - the turn's ``finally`` clears it on every exit, including the one a SIGTERM
# causes, and a blank wait notice writes '' too. That is why the adapter snapshots the row just
# before it signals the session (``snapshot_activity``), and why the diagnosis prefers it.
ACTIVITY_UNAVAILABLE = "unavailable"
ACTIVITY_NEVER_PERSISTED = "never_persisted"
ACTIVITY_CLEARED = "cleared"
ACTIVITY_OTHER = "other"
ACTIVITY_SUFFIX = ".activity.json"
# A read-only open that cannot hold up the budget kill it precedes.
SNAPSHOT_TIMEOUT_SECONDS = 0.5
_ACTIVITY_STATES = (
    ("receiving_chunks", re.compile(r"^receiving stream response$")),
    (
        "awaiting_first_chunk",
        re.compile(
            r"^waiting for provider response \(streaming\)$"
            r"|^waiting for stream response \(\d+s, first_chunk\)$"
            r"|\d+s waiting for the first stream chunk"
        ),
    ),
    (
        "silent_after_chunks",
        re.compile(r"^waiting for stream response \(\d+s, post_chunk\)$|stream open; \d+s without stream output"),
    ),
    (
        "reconnecting",
        re.compile(
            r"^stale stream detected after \d+s, reconnecting$|^stream retry \d+/\d+ after "
            r"|no output from provider for \d+s — reconnecting"
        ),
    ),
    (
        "retry_backoff",
        re.compile(
            r"^(?:error |empty response )?retry backoff \(\d+/\d+\), \d+s remaining$"
            r"|^auto-recovery wait \(\d+/\d+\), \d+s remaining$"
            r"|^API error recovery \(attempt \d+/\d+\)$"
            r"|retrying in \d+s \(attempt \d+/\d+\)"
        ),
    ),
    (
        "non_streaming",
        re.compile(r"^waiting for non-streaming API response$|^stale non-streaming call killed after \d+s$"),
    ),
)


def _parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds from Docker's RFC 3339, whose nanoseconds fromisoformat will not take."""
    if not isinstance(value, str) or not value:
        return None
    match = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", value)
    if not match:
        return None
    whole, fraction, zone = match.groups()
    try:
        moment = datetime.fromisoformat(whole + ("+00:00" if zone in (None, "Z") else zone))
    except ValueError:
        return None
    return moment.timestamp() + (float(f"0.{fraction}") if fraction else 0.0)


def launch_time(stem: str) -> tuple[Optional[float], Optional[int]]:
    """When run_agent.sh launched this session, and the pid it ran as."""
    match = _STEM.match(stem)
    if not match:
        return None, None
    moment = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return moment.timestamp(), int(match.group(2))


def _load_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stream_events(path: Path) -> Optional[list[dict[str, Any]]]:
    """Whole lines only: a killed session's last line can be half-written. None if no file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def read_ledger(hermes_home: Optional[str]) -> dict[str, Any]:
    """The session's own record in its throwaway HERMES_HOME, read-only, timestamps only.

    Message content is never copied: the question is when things happened, and a transcript
    would carry the model's prose and tool output into a record uploaded as an artifact.
    """
    if not hermes_home:
        return {"available": False, "reason": "controls receipt names no hermes_home"}
    database = Path(hermes_home) / SESSION_DB_FILENAME
    if not Path(hermes_home).is_dir():
        # Read somewhere other than the runner the session ran on, or after it was reaped.
        return {"available": False, "reason": f"{hermes_home} is gone, so its {SESSION_DB_FILENAME} cannot be read"}
    if not database.is_file():
        # Hermes opens its store best-effort before the turn, so this leans towards a session
        # that never got that far - but a store that failed to open looks the same.
        return {"available": False, "reason": f"{hermes_home} exists but Hermes never created {SESSION_DB_FILENAME} in it"}
    try:
        with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            # Older stores predate the activity description; name that absence rather than fail.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            activity = [c for c in ("last_activity_description", "last_activity_provenance") if c in columns]
            sessions = [
                dict(row)
                for row in conn.execute(
                    "SELECT "
                    + ", ".join(
                        [
                            "id",
                            "started_at",
                            "ended_at",
                            "end_reason",
                            "api_call_count",
                            "message_count",
                            "tool_call_count",
                            "last_activity_at",
                            *activity,
                        ]
                    )
                    + " FROM sessions ORDER BY started_at"
                )
            ]
            messages = [
                dict(row)
                for row in conn.execute(
                    "SELECT role, timestamp, tool_name, finish_reason, "
                    "tool_calls IS NOT NULL AND tool_calls NOT IN ('', '[]', 'null') AS has_tool_calls "
                    "FROM messages ORDER BY timestamp, id"
                )
            ]
            usage = [
                dict(row)
                for row in conn.execute(
                    "SELECT model, billing_provider, api_call_count, reasoning_tokens, output_tokens, "
                    "first_seen, last_seen FROM session_model_usage"
                )
            ]
    except sqlite3.Error as error:
        return {"available": False, "reason": f"{type(error).__name__}: {error}"}
    return {
        "available": True,
        "sessions": sessions,
        "messages": messages,
        "usage": usage,
        "has_activity_description": "last_activity_description" in columns,
    }


def read_credential_pool(hermes_home: Optional[str]) -> Optional[list[dict[str, Any]]]:
    """Which keys the session's credential pool held and what each went through.

    Hermes seeds ``NVIDIA_API_KEY_2`` alongside ``NVIDIA_API_KEY`` and rotates to it when the
    first key is rate-limited twice in a row, so without this a session one key served and a
    session two keys served look the same. A key that was throttled reads ``exhausted`` with
    its status code. ``source`` is the variable's name (``env:NAME``); nothing else is copied -
    not the label, which Hermes can fill from a token's claims, never a token field, and not
    ``request_count``, which counts selections rather than calls and reaches disk only on the
    next rotation. None where the store or its pool is absent.
    """
    if not hermes_home:
        return None
    store = _load_json(Path(hermes_home) / AUTH_STORE_FILENAME)
    pool = store.get("credential_pool") if isinstance(store, dict) else None
    if not isinstance(pool, dict):
        return None
    return [
        {
            "provider": provider,
            "source": entry.get("source"),
            "last_status": entry.get("last_status"),
            "last_error_code": entry.get("last_error_code"),
        }
        for provider, entries in sorted(pool.items())
        if isinstance(entries, list)
        for entry in entries
        if isinstance(entry, dict)
    ]


def _first(values: list[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _offset(moment: Optional[float], origin: Optional[float]) -> Optional[float]:
    if moment is None or origin is None:
        return None
    return round(moment - origin, 1)


def _containers_in_window(
    receipt: Any, start: Optional[float], end: Optional[float]
) -> tuple[Optional[list[dict[str, Any]]], int]:
    """This session's containers out of a witness that spans the whole lane.

    One witness watches every phase and every session's ``.sandbox.json`` is a copy of its
    running total, so a plan receipt lists research's containers too. A container belongs to
    the session whose launch-to-termination window its ``created_at`` falls in. The window's
    start is the launch second, which can be up to a second late, so it is widened by one.
    """
    if not isinstance(receipt, dict) or start is None:
        return None, 0
    own, others = [], 0
    for container in receipt.get("containers") or []:
        created = _parse_iso(container.get("created_at"))
        if created is not None and created >= start - 1 and (end is None or created <= end):
            own.append(
                {
                    "task_id": container.get("task_id"),
                    "container_id": container.get("container_id"),
                    "created_at": container.get("created_at"),
                    "at_seconds": _offset(created, start),
                }
            )
        else:
            others += 1
    return own, others


def diagnose_session(
    sessions_dir: Path,
    stem: str,
    phase_result: Optional[dict[str, Any]] = None,
    window_end: Optional[float] = None,
) -> dict[str, Any]:
    """One session's evidence on one clock, the sources that were missing, and a verdict.

    ``phase_result`` is the strategy's record of the phase this session ran (returncode,
    seconds, artifact validity); without it the session can be described but not judged.
    ``window_end`` bounds container attribution when there is no phase result to compute it
    from - the next session's launch, say.
    """
    sessions_dir = Path(sessions_dir)
    launched, pid = launch_time(stem)
    controls = _load_json(sessions_dir / f"{stem}{CONTROLS_SUFFIX}")
    usage_report = _load_json(sessions_dir / f"{stem}{USAGE_SUFFIX}")
    log_path = sessions_dir / f"{stem}{LOG_SUFFIX}"
    log_bytes = log_path.stat().st_size if log_path.is_file() else None
    events = _stream_events(sessions_dir / f"{stem}{STREAM_SUFFIX}")
    ledger = read_ledger((controls or {}).get("hermes_home"))
    transport = (controls or {}).get("transport")
    slice_id = (controls or {}).get("slice_id") or ""

    if phase_result is not None and launched is not None:
        window_end = launched + float(phase_result.get("seconds") or 0) + float(phase_result.get("grace_seconds") or 0)
    containers, foreign = _containers_in_window(_load_json(sessions_dir / f"{stem}{SANDBOX_SUFFIX}"), launched, window_end)

    missing: list[str] = []
    if controls is None:
        missing.append("controls receipt: run_agent.sh never reached the Hermes launch")
    if log_bytes is None:
        missing.append("session log: absent")
    elif log_bytes == 0:
        missing.append(
            "session log: 0 bytes" + (" - --oneshot writes nothing until the turn returns" if transport == "oneshot" else "")
        )
    if usage_report is None:
        missing.append(
            "usage report: absent"
            + (" - --oneshot writes it only when the turn returns" if transport == "oneshot" else " - this transport writes none")
        )
    if events is None:
        if transport == "stream-json":
            missing.append("event stream: absent although the transport was stream-json")
    elif not events:
        missing.append("event stream: empty - not even system/init was flushed")
    if not ledger["available"]:
        missing.append(f"session ledger: {ledger['reason']}")
    if containers is None:
        missing.append("sandbox observation: no witness receipt, or no launch time to place it")
    if ledger["available"] and not ledger.get("has_activity_description"):
        missing.append("activity description: this state.db predates sessions.last_activity_description")
    snapshot = _load_json(sessions_dir / f"{stem}{ACTIVITY_SUFFIX}")
    if snapshot is not None and snapshot.get("error"):
        missing.append(f"activity snapshot: {snapshot['error']}")
    elif snapshot is None and phase_result is not None and phase_result.get("returncode") == TIMEOUT_RETURNCODE:
        missing.append("activity snapshot: none taken before the budget kill, so SIGTERM may have cleared the label")

    rows = ledger.get("sessions") or []
    messages = ledger.get("messages") or []
    usage_rows = ledger.get("usage") or []
    assistant = [m for m in messages if m.get("role") == "assistant"]
    tool_calls = [m for m in assistant if m.get("has_tool_calls")]
    tool_results = [m for m in messages if m.get("role") == "tool"]
    api_calls = sum(int(row.get("api_call_count") or 0) for row in usage_rows)
    if not usage_rows and rows:
        api_calls = sum(int(row.get("api_call_count") or 0) for row in rows)

    stream = events or []
    kinds = [e.get("type") for e in stream]
    stamp = lambda e: (e.get("timestamp") or 0) / 1000 or None  # noqa: E731 - ms on the wire
    stream_tool_uses = [e for e in stream if e.get("type") == "tool_use"]
    stream_tool_results = [e for e in stream if e.get("type") == "tool_result"]

    moments = {
        "turn_started": _first([row.get("started_at") for row in rows]),
        "first_stream_event": _first([stamp(e) for e in stream]),
        "first_completed_model_call": _first([row.get("first_seen") for row in usage_rows]),
        "last_completed_model_call": max(
            [row.get("last_seen") for row in usage_rows if row.get("last_seen") is not None], default=None
        ),
        "first_model_output": _first(
            [m.get("timestamp") for m in assistant] + [stamp(e) for e in stream if e.get("type") == "text"]
        ),
        "first_tool_call": _first([m.get("timestamp") for m in tool_calls] + [stamp(e) for e in stream_tool_uses]),
        "first_tool_result": _first([m.get("timestamp") for m in tool_results] + [stamp(e) for e in stream_tool_results]),
        "last_activity": max(
            [
                value
                for value in [m.get("timestamp") for m in messages]
                + [row.get("last_activity_at") for row in rows]
                + [row.get("last_seen") for row in usage_rows]
                + [stamp(e) for e in stream]
                if value is not None
            ],
            default=None,
        ),
        "first_container": _first([_parse_iso(c["created_at"]) for c in containers or []]),
    }
    timeline = {name: _offset(moment, launched) for name, moment in moments.items()}

    observed = {
        "session_rows": len(rows),
        "usage_rows": len(usage_rows),
        "messages": len(messages),
        "completed_model_calls": api_calls,
        "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in usage_rows) if usage_rows else None,
        "assistant_messages": len(assistant),
        "tool_calls": max(len(tool_calls), len(stream_tool_uses)),
        "tool_results": max(len(tool_results), len(stream_tool_results)),
        "stream_events": len(stream) if events is not None else None,
        "stream_kinds": sorted({k for k in kinds if k}),
        "terminal_result": "result" in kinds,
        "end_reason": next((row.get("end_reason") for row in rows if row.get("end_reason")), None),
    }

    verdict, explanation = _verdict(phase_result, ledger["available"], events, observed, timeline)
    return {
        "session": stem,
        "phase": slice_id.rsplit(":", 1)[-1] if ":" in slice_id else None,
        "pid": pid,
        "transport": transport,
        "launched_at": datetime.fromtimestamp(launched, timezone.utc).isoformat() if launched is not None else None,
        "terminated": (
            {
                "returncode": phase_result.get("returncode"),
                "at_seconds": _offset(window_end, launched),
                "by_budget": phase_result.get("returncode") == TIMEOUT_RETURNCODE,
                "artifact_valid": phase_result.get("artifact_valid"),
            }
            if phase_result is not None
            else None
        ),
        "timeline_seconds": timeline,
        "observed": observed,
        "credential_pool": read_credential_pool((controls or {}).get("hermes_home")),
        "last_persisted_activity": _last_persisted_activity(rows, launched, snapshot),
        "containers": containers,
        "containers_from_other_sessions": foreign,
        "missing_evidence": missing,
        "verdict": verdict,
        "explanation": explanation,
    }


def classify_activity(description: Optional[str]) -> str:
    """Name the state a persisted activity description shows: NULL and '' are their own states."""
    if description is None:
        return ACTIVITY_NEVER_PERSISTED
    if description == "":
        return ACTIVITY_CLEARED
    return next((state for state, pattern in _ACTIVITY_STATES if pattern.search(description)), ACTIVITY_OTHER)


def controls_stems(sessions_dir: Path) -> frozenset[str]:
    """The sessions that have announced themselves so far, to tell the next one apart."""
    return frozenset(path.name[: -len(CONTROLS_SUFFIX)] for path in sessions_dir.glob(f"*{CONTROLS_SUFFIX}"))


def snapshot_activity(sessions_dir: Path, before: frozenset[str]) -> None:
    """Keep the activity row of the one session launched since ``before``, ahead of its SIGTERM.

    Hermes clears the description in the turn's ``finally``, so a graceful stop erases the very
    label a budget kill should explain. One read-only open with a short timeout; any failure is
    written down for the diagnosis to name, and nothing here ever raises into the kill.
    """
    try:
        new = sorted(controls_stems(sessions_dir) - before)
        if len(new) != 1:
            return
        stem = new[0]
        snapshot: dict[str, Any] = {"captured_at": datetime.now(timezone.utc).timestamp()}
        try:
            home = (_load_json(sessions_dir / f"{stem}{CONTROLS_SUFFIX}") or {}).get("hermes_home")
            if not home:
                raise FileNotFoundError("controls receipt names no HERMES_HOME")
            database = Path(home) / SESSION_DB_FILENAME
            if not database.is_file():
                raise FileNotFoundError(f"{database} does not exist")
            uri = f"file:{database}?mode=ro"
            with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=SNAPSHOT_TIMEOUT_SECONDS)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT last_activity_at, last_activity_description, last_activity_provenance "
                    "FROM sessions ORDER BY last_activity_at DESC LIMIT 1"
                ).fetchone()
            snapshot["row"] = dict(row) if row is not None else None
        except (OSError, sqlite3.Error, ValueError) as error:
            snapshot["error"] = f"{type(error).__name__}: {error}"
        (sessions_dir / f"{stem}{ACTIVITY_SUFFIX}").write_text(json.dumps(snapshot) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - observation must never stand between a budget and its kill
        return


def _last_persisted_activity(
    rows: list[dict[str, Any]], launched: Optional[float], snapshot: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """One activity stamp, up to 60s old: the pre-SIGTERM snapshot when there is one, else the row."""
    if snapshot is not None and snapshot.get("row"):
        row, source = snapshot["row"], "pre_sigterm_snapshot"
    else:
        row = max(rows, key=lambda r: r.get("last_activity_at") or 0, default={})
        source = "state_db_after_exit" if row else None
    present = "last_activity_description" in row
    description = row.get("last_activity_description")
    return {
        "source": source,
        "at_seconds": _offset(row.get("last_activity_at"), launched),
        "description": description,
        "provenance": row.get("last_activity_provenance"),
        "state": classify_activity(description) if present else ACTIVITY_UNAVAILABLE,
    }


def _verdict(
    phase_result: Optional[dict[str, Any]],
    ledger_available: bool,
    events: Optional[list[dict[str, Any]]],
    observed: dict[str, Any],
    timeline: dict[str, Optional[float]],
) -> tuple[str, str]:
    """The furthest stage the evidence proves the session reached, and nothing past it."""
    if phase_result is not None and phase_result.get("artifact_valid") is not False and not phase_result.get("returncode"):
        return COMPLETED, "the phase exited 0 and owed no artifact it failed to leave"
    # Any later write proves the turn started, even when the turn-start row itself is missing.
    started = observed["session_rows"] > 0 or observed["usage_rows"] > 0 or observed["messages"] > 0 or bool(events)
    if not ledger_available and not events:
        return EVIDENCE_UNAVAILABLE, ("no session ledger and no event stream, so nothing here can say how far the session got")
    if not started:
        # Not "never reached the model": _ensure_session_row goes through _persist_under_lock,
        # which logs and swallows a failed write, so an empty ledger is also what a turn whose
        # persistence failed would leave.
        return NO_TURN_START_RECORDED, (
            "the ledger exists but records nothing: Hermes writes a session row at turn start, before "
            "the first model call, but logs and swallows a failed write - so either the turn never "
            "started or its persistence failed"
        )
    if observed["completed_model_calls"] == 0 and observed["assistant_messages"] == 0 and not observed["tool_calls"]:
        return NO_COMPLETED_MODEL_CALL, f"the turn started but no model call ever completed; {_UNSPLITTABLE}"
    if observed["tool_calls"] == 0:
        # Not "never called a tool": the tool-call message is flushed after the tool makes
        # progress, so a tool requested and then stalled before that flush leaves this too.
        return NO_TOOL_CALL_RECORDED, (
            "a model call completed but no tool call was recorded; Hermes persists a tool call only after "
            "the tool makes progress, so the model answering without tools and a requested tool stalling "
            "before that flush leave the same evidence"
        )
    if observed["tool_results"] < observed["tool_calls"]:
        return TOOL_CALL_UNANSWERED, (
            f"{observed['tool_calls']} tool calls recorded, {observed['tool_results']} results: "
            "no result was recorded for the last one"
        )
    if phase_result is None:
        return FAILED, "the session called tools; without its phase result there is no termination to judge"
    if phase_result.get("returncode") == TIMEOUT_RETURNCODE:
        last = timeline.get("last_activity")
        banked = "after" if phase_result.get("artifact_valid") else "before"
        return ACTIVE_UNTIL_BUDGET, (
            f"tools ran and returned; the budget ended it (last recorded activity at {last}s) {banked} a valid artifact"
        )
    if phase_result.get("artifact_valid") is False:
        return EXITED_WITHOUT_ARTIFACT, "the session exited on its own without leaving a valid artifact"
    return FAILED, f"exited {phase_result.get('returncode')}"


def diagnose_sessions(sessions_dir: Path, phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every controls-bearing session in the directory, each judged against its own phase.

    Sessions are matched to phases by the ``:phase`` suffix run_phase writes into the slice id,
    not by order, so a skipped or missing session cannot shift every later phase's evidence
    onto the wrong session. A session with no matching phase is still described.
    """
    sessions_dir = Path(sessions_dir)
    stems = sorted(path.name[: -len(CONTROLS_SUFFIX)] for path in sessions_dir.glob(f"*{CONTROLS_SUFFIX}"))
    by_phase = {phase.get("phase"): phase for phase in phases}
    records = []
    for index, stem in enumerate(stems):
        slice_id = (_load_json(sessions_dir / f"{stem}{CONTROLS_SUFFIX}") or {}).get("slice_id") or ""
        phase = by_phase.get(slice_id.rsplit(":", 1)[-1]) if ":" in slice_id else None
        next_launch = launch_time(stems[index + 1])[0] if index + 1 < len(stems) else None
        records.append(diagnose_session(sessions_dir, stem, phase, window_end=next_launch))
    return records


def summarize(record: dict[str, Any]) -> list[str]:
    """Operator lines for one session: verdict first, then the clock, then what was missing."""
    timeline = {k: v for k, v in record["timeline_seconds"].items() if v is not None}
    lines = [
        f"session       {record['session']} pid {record['pid']} ({record['phase'] or 'unknown phase'}, {record['transport']})",
        f"verdict       {record['verdict']}: {record['explanation']}",
        "timeline      " + (", ".join(f"{k} {v}s" for k, v in timeline.items()) or "nothing timestamped"),
        f"containers    {len(record['containers'] or [])} in this session's window,"
        f" {record['containers_from_other_sessions']} from other sessions",
    ]
    activity = record["last_persisted_activity"]
    if activity["source"]:
        lines.append(
            f"last activity {activity['state']} at {activity['at_seconds']}s ({activity['source']}): {activity['description']!r}"
        )
    lines += [f"missing       {item}" for item in record["missing_evidence"]]
    return lines
