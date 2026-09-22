"""One research phase, one session, one event stream: what the matrix cannot show.

Runs 35715428932, 35736569601 and 35788842735 all ended the same way in lanes C, D and E:
the research phase burned its whole 600-second ceiling and left no ``research.json``, and
the archived log was 0 bytes, so there was no way to tell whether the model never tried to
write the artifact, tried and failed, or tried too late. Under ``--oneshot`` there never
will be - Hermes redirects the turn's stdout and stderr to ``/dev/null`` and prints only
the final response - so this probe runs the same phase over the ``stream-json`` transport,
which flushes one JSON line per text delta, tool call and tool result.

This is a diagnostic, never a lane. ``hermes chat`` is a different execution path from
``hermes --oneshot``, and it carries no ``--usage-file`` receipt, so nothing it produces is
comparable with an A-E cell. It exists to answer four questions about the research phase:
what the model does first, whether it ever calls a tool to write the artifact, when, and
what the tool said back.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .adapter import AGENT_SESSIONS_DIR, CommandRunner, _default_command_template, _subprocess_runner
from .instances import task_spec
from .lanes import _agent_sessions
from .strategies import (
    PHASE_CEILING_SECONDS,
    RESEARCH_ARTIFACT,
    RESEARCH_SPEC,
    RPI_DIR,
    ExecutionContext,
    read_artifact,
    research_prompt,
    run_phase,
)

STREAM_JSON_SUFFIX = ".stream.jsonl"
PROBE_FILENAME = "probe.json"
# The phase the campaign is stuck on, at the ceiling the campaign gives it.
PROBE_PHASE = "research"
PROBE_BUDGET_SECONDS = PHASE_CEILING_SECONDS[PROBE_PHASE]


def _events(path: Path) -> list[dict[str, Any]]:
    """Parse the JSONL stream, keeping only whole lines.

    A killed session's last line can be a partial write. Dropping it loses nothing the
    earlier lines did not already establish; refusing to parse the file would lose the run.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _start(events: list[dict[str, Any]]) -> int:
    return min((event.get("timestamp") or 0) for event in events) if events else 0


def _entry(event: dict[str, Any], start: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "at_seconds": round(((event.get("timestamp") or start) - start) / 1000, 1),
        "type": event.get("type"),
    }
    for key in ("name", "tool", "tool_name", "is_error", "subtype"):
        if event.get(key) is not None:
            entry[key] = event[key]
    return entry


def timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool activity in order, with the offset from the first event.

    Text deltas are counted rather than kept: the question is what the session *did* and
    when, and a transcript of its prose would bury that under its own length.
    """
    start = _start(events)
    entries: list[dict[str, Any]] = []
    deltas = 0
    for event in events:
        if event.get("type") == "text":
            deltas += 1
            continue
        entries.append({**_entry(event, start), "text_deltas_before": deltas})
    return entries


def artifact_mentions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every event naming the required artifact - the write attempt, or its absence.

    Read off the events themselves, not off ``timeline``: that one drops text deltas, so
    pairing the two by position dated run 35799016896's write attempt five events late and
    lost every mention past the 36th. The archived JSONL was right; this reader was not.
    """
    start = _start(events)
    return [_entry(event, start) for event in events if RESEARCH_ARTIFACT in json.dumps(event)]


def terminal_result(events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """What the final ``result`` event exposes - the only accounting this transport gives.

    There is no ``--usage-file`` off ``-z/--oneshot``, but the terminal event does carry the
    exit code, the turn's duration and its token counts. It does not name the model: the
    ``system/init`` event's ``model`` field came back empty, so nothing here may be read as
    evidence of which model or provider served the turn.
    """
    finals = [event for event in events if event.get("type") == "result"]
    if not finals:
        return None
    final = finals[-1]
    return {key: final.get(key) for key in ("exit_code", "duration_ms", "tokens", "session_id")}


def run_probe(
    instance_id: str,
    out_dir: Path,
    *,
    repo_root: Path,
    budget_seconds: int = PROBE_BUDGET_SECONDS,
    runner: Optional[CommandRunner] = None,
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Administer the research phase once and keep everything it left behind."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    task = task_spec(instance_id)

    # Same workspace construction as a cell: an agent that finds a different repo shape
    # answers a different question.
    workspace = Path(tempfile.mkdtemp(prefix=f"pb-probe-{task.instance_id}-"))
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    control_dir = Path(tempfile.mkdtemp(prefix=f"pb-probe-control-{task.instance_id}-"))

    sessions_dir = (out_dir / AGENT_SESSIONS_DIR).resolve()
    environment = dict(os.environ)
    if env:
        environment.update(env)
    environment["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(sessions_dir)
    environment["REPO_AUTOMATION_HERMES_TRANSPORT"] = "stream-json"

    ctx = ExecutionContext(
        repo_root=Path(repo_root),
        task=task,
        workspace=workspace,
        control_dir=control_dir,
        command_template=_default_command_template(Path(repo_root)),
        env=environment,
        timeout_seconds=budget_seconds,
        runner=runner or _subprocess_runner,
    )
    prompt = research_prompt(task)
    result = run_phase(ctx, PROBE_PHASE, prompt, {"objective": task.objective}, budget_seconds)

    data, text = read_artifact(workspace, RESEARCH_ARTIFACT)
    rpi_source = workspace / RPI_DIR
    if rpi_source.is_dir():
        destination = out_dir / "rpi"
        if destination.exists():
            for stale in destination.iterdir():
                stale.unlink()
        destination.mkdir(parents=True, exist_ok=True)
        for artifact in rpi_source.iterdir():
            destination.joinpath(artifact.name).write_bytes(artifact.read_bytes())

    sessions = _agent_sessions(out_dir)
    streams = sorted(sessions_dir.glob(f"*{STREAM_JSON_SUFFIX}"))
    events = _events(streams[0]) if streams else []

    record = {
        "instance_id": task.instance_id,
        "phase": PROBE_PHASE,
        "transport": "stream-json",
        "budget_seconds": budget_seconds,
        "prompt_chars": len(prompt),
        "phase_result": result.to_dict(),
        "artifact": {
            "filename": RESEARCH_ARTIFACT,
            "present": data is not None,
            "chars": len(text),
            "schema_errors": RESEARCH_SPEC.validate(data) if data is not None else ["absent"],
        },
        "stream": {
            "file": streams[0].name if streams else None,
            "events": len(events),
            "text_deltas": sum(1 for event in events if event.get("type") == "text"),
            "timeline": timeline(events),
            "artifact_mentions": artifact_mentions(events),
            "terminal_result": terminal_result(events),
        },
        "agent_sessions": sessions,
    }
    (out_dir / PROBE_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def summarize(record: dict[str, Any]) -> str:
    """The four questions, answered in the order the decision tree asks them."""
    stream, artifact, phase = record["stream"], record["artifact"], record["phase_result"]
    lines = [
        f"instance      {record['instance_id']}",
        f"phase         {record['phase']} via {record['transport']}, {record['prompt_chars']} prompt chars",
        f"ended         rc {phase['returncode']} ({phase['state']}) after {phase['seconds']:.0f}s of {record['budget_seconds']}s",
        f"stream        {stream['events']} events ({stream['text_deltas']} text deltas) in {stream['file'] or 'no stream file'}",
        f"artifact      {'present' if artifact['present'] else 'ABSENT'}"
        f" ({artifact['chars']} chars, schema {artifact['schema_errors'] or 'valid'})",
    ]
    final = stream.get("terminal_result")
    if final:
        lines.append(
            f"session       exit {final.get('exit_code')} after {(final.get('duration_ms') or 0) / 1000:.0f}s,"
            f" tokens {(final.get('tokens') or {}).get('total')} (no model identity on this transport)"
        )
    mentions = stream["artifact_mentions"]
    named = f"{len(mentions)} events name {RESEARCH_ARTIFACT}" if mentions else "none"
    lines.append(f"write attempt {named}")
    for entry in mentions[:10]:
        lines.append(f"  at {entry['at_seconds']:>7.1f}s  {entry.get('type')} {entry.get('name') or entry.get('tool') or ''}")
    if not mentions and stream["timeline"]:
        lines.append("first tool activity:")
        for entry in stream["timeline"][:10]:
            lines.append(f"  at {entry['at_seconds']:>7.1f}s  {entry.get('type')} {entry.get('name') or entry.get('tool') or ''}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m automation.benchmark.probe",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--instance", required=True, help="ProgramBench instance id to research.")
    parser.add_argument("--out-dir", type=Path, required=True, help="Where to write the probe record.")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2], help="Harness repo root.")
    parser.add_argument(
        "--budget",
        type=int,
        default=PROBE_BUDGET_SECONDS,
        help=f"Phase ceiling in seconds (default: {PROBE_BUDGET_SECONDS}, the lane's own).",
    )
    args = parser.parse_args(argv)

    record = run_probe(args.instance, args.out_dir, repo_root=args.repo_root, budget_seconds=args.budget)
    print(summarize(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
