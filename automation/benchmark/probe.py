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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .adapter import (
    AGENT_SESSIONS_DIR,
    CommandRunner,
    _default_command_template,
    _subprocess_runner,
    audited_inherited_environment,
)
from .instances import task_spec
from .lanes import _agent_sessions
from .run import resolve_agent_env
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
# Where the workspace's .rpi artifacts are kept once they belong to the run directory.
PROBE_RPI_DIRNAME = "rpi"
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


def _is_text(event: dict[str, Any]) -> bool:
    """The one place the skip rule lives: two readings of it is what misaligned the timeline."""
    return event.get("type") == "text"


def timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool activity in order, with the offset from the first event.

    Text deltas are counted rather than kept: the question is what the session *did* and
    when, and a transcript of its prose would bury that under its own length.
    """
    if not events:
        return []
    start = min(event.get("timestamp") or 0 for event in events)
    entries: list[dict[str, Any]] = []
    deltas = 0
    for event in events:
        kind = event.get("type")
        if _is_text(event):
            deltas += 1
            continue
        entry = {
            "at_seconds": round(((event.get("timestamp") or start) - start) / 1000, 1),
            "type": kind,
            "text_deltas_before": deltas,
        }
        for key in ("name", "tool", "tool_name", "is_error", "subtype"):
            if event.get(key) is not None:
                entry[key] = event[key]
        entries.append(entry)
    return entries


def artifact_mentions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every event naming the required artifact - the write attempt, or its absence."""
    named = [event for event in events if not _is_text(event)]
    return [entry for entry, event in zip(timeline(events), named) if RESEARCH_ARTIFACT in json.dumps(event)]


def _refuse_pre_existing(path: Path) -> None:
    """Ownership is established by creation: what this run did not make, it does not clear."""
    if path.exists():
        raise FileExistsError(f"refusing to overwrite pre-existing probe output: {path}")


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
    _refuse_pre_existing(out_dir / PROBE_RPI_DIRNAME)
    _refuse_pre_existing(out_dir / PROBE_FILENAME)

    # Same workspace construction as a cell: an agent that finds a different repo shape
    # answers a different question.
    workspace = Path(tempfile.mkdtemp(prefix=f"pb-probe-{task.instance_id}-"))
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    control_dir = Path(tempfile.mkdtemp(prefix=f"pb-probe-control-{task.instance_id}-"))

    sessions_dir = (out_dir / AGENT_SESSIONS_DIR).resolve()
    # A diagnostic reaches the same agent through the same shell as a lane, so it inherits
    # what a lane inherits and nothing else. Explicit `env` is the caller's own authorization
    # and is not filtered through that boundary.
    environment = audited_inherited_environment()
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
    copied: list[str] = []
    skipped_symlinks: list[str] = []
    skipped_non_files: list[str] = []
    rpi_source = workspace / RPI_DIR
    # `.rpi` is agent-created, container included. `is_dir()` follows links, so a session that
    # makes `.rpi` itself a link gets every entry of the target copied out as a real file -
    # the per-entry guard below never sees a symlink. Reach over the whole host filesystem is
    # not authority an evidence collector needs, at either level.
    container_skipped = rpi_source.is_symlink()
    if rpi_source.is_dir() and not container_skipped:
        destination = out_dir / PROBE_RPI_DIRNAME
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # Appeared after the pre-flight refusal looked: someone else's, either way.
            raise FileExistsError(f"refusing to overwrite pre-existing probe output: {destination}") from None
        try:
            for artifact in sorted(rpi_source.iterdir()):
                # Skipped rather than refused, both here and above: refusing would let a
                # session halt its own diagnostic by leaving a link or a directory behind.
                if artifact.is_symlink():
                    skipped_symlinks.append(artifact.name)
                    continue
                if not artifact.is_file():
                    # read_bytes() on a directory raised mid-loop, and the cleanup below then
                    # removed everything already collected - a session could destroy its own
                    # evidence with one `mkdir`.
                    skipped_non_files.append(artifact.name)
                    continue
                destination.joinpath(artifact.name).write_bytes(artifact.read_bytes())
                copied.append(artifact.name)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise

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
        # A skip reads as a finding about the session; a silent omission would read as
        # "the agent wrote nothing", which is the question the probe exists to answer.
        "rpi": {
            "copied": copied,
            "skipped_symlinks": skipped_symlinks,
            "skipped_non_files": skipped_non_files,
            "container_skipped": container_skipped,
        },
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
        },
        "agent_sessions": sessions,
    }
    # "x": a record written between the pre-flight refusal and here is still someone else's.
    record_path = out_dir / PROBE_FILENAME
    try:
        with record_path.open("x", encoding="utf-8") as record_file:
            record_file.write(json.dumps(record, indent=2) + "\n")
    except FileExistsError:
        raise FileExistsError(f"refusing to overwrite pre-existing probe output: {record_path}") from None
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
    rpi = record["rpi"]
    # Printed, not just recorded: these are the lines that stop a reader concluding the agent
    # wrote nothing when it wrote something the probe declined to follow.
    if rpi["container_skipped"]:
        lines.append(f"skipped links {RPI_DIR} itself is a symlink, so nothing under it was collected")
    if rpi["skipped_symlinks"]:
        lines.append(f"skipped links {', '.join(rpi['skipped_symlinks'])} (symlinks in {RPI_DIR}, not followed)")
    if rpi["skipped_non_files"]:
        lines.append(f"skipped dirs  {', '.join(rpi['skipped_non_files'])} (not regular files in {RPI_DIR})")
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


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--agent-env",
        action="append",
        metavar="VARIABLE_NAME",
        default=[],
        help="Forward this launcher variable to the probe session. Repeatable. Name only, never NAME=value.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    record = run_probe(
        args.instance,
        args.out_dir,
        repo_root=args.repo_root,
        budget_seconds=args.budget,
        env=resolve_agent_env(args.agent_env),
    )
    print(summarize(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
