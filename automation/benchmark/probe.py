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

Everything *else* about the environment is the cell's, because a diagnostic run against a
different environment answers a question nobody asked. It was not: this module used to
``git init`` an empty directory and let Hermes pick its own local backend, which is the
pre-cleanroom environment runs 35715428932 onward were administered in and which PR #55
retired. So the workspace is now ProgramBench's own ``task_cleanroom_v6`` worktree, the
image is named to run_agent.sh, the two sandbox backends are proved before the model turn,
and the session runs under the same witness - including its exact-id reap of a container a
killed session leaves behind. The transport is the one difference, and it is the point.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .adapter import (
    AGENT_SESSIONS_DIR,
    CLEANROOM_RECEIPT_FILENAME,
    SANDBOX_IMAGE_ENV,
    CommandRunner,
    _default_command_template,
    _subprocess_runner,
    audited_inherited_environment,
    probe_sandbox,
)
from .cleanroom import prepare as prepare_cleanroom
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
from .witness import SandboxWitness

STREAM_JSON_SUFFIX = ".stream.jsonl"
USAGE_SUFFIX = ".usage.json"
CONTROLS_SUFFIX = ".controls.json"
# The two transports this diagnostic can administer. stream-json shows the shape of a turn
# and is blind to reasoning; oneshot shows the accounting and is blind to everything until
# the turn ends. Neither is a lane.
TRANSPORTS = ("stream-json", "oneshot")
# The turn cap is only real on one of them. At the pinned revision `--oneshot` builds its
# AIAgent without passing max_iterations at all (hermes_cli/oneshot.py:574), so the default
# in run_agent.py:256 stands: sys.maxsize. Neither the flag, nor agent.max_turns in config,
# nor HERMES_MAX_ITERATIONS reaches it - every one of those is read in cli_init_mixin, on
# the chat path. Capping a oneshot session is not possible here without forking the pin.
CAPPABLE_TRANSPORT = "stream-json"
# Hermes' own per-session token accounting, inside the throwaway HERMES_HOME. Written
# through update_token_counts, the chokepoint every per-API-call delta flows through on
# every path, so a `chat -q` session lands here even though --usage-file does not.
SESSION_DB_FILENAME = "state.db"
LEDGER_TABLE = "session_model_usage"
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


def terminal_result(events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """What the final ``result`` event exposes - the only accounting this transport gives.

    There is no ``--usage-file`` off anything but ``-z/--oneshot``, so a stream session
    leaves no usage receipt. The terminal event does carry the exit code, the turn's
    duration and its token counts, which is exactly the timing evidence this probe is for.

    It is not provenance. Nothing here names the model or the provider, so no field of it may
    be read as evidence of which model served the turn - that remains the job-level preflight,
    which compares served against asked and fails the run on a mismatch.
    """
    finals = [event for event in events if event.get("type") == "result"]
    if not finals:
        return None
    final = finals[-1]
    return {key: final.get(key) for key in ("exit_code", "duration_ms", "tokens", "session_id")}


def stream_session_id(events: list[dict[str, Any]]) -> Optional[str]:
    """The session id the stream names, which is the key into Hermes' own accounting."""
    for event in events:
        session_id = event.get("session_id")
        if session_id:
            return str(session_id)
    return None


def ledger_usage(hermes_home: Path, session_id: str) -> Optional[dict[str, Any]]:
    """The session's row in Hermes' own usage table, normalised to the usage-report keys.

    This is the accounting source for the capped transport. ``--usage-file`` is written only
    by ``--oneshot``, and ``--oneshot`` cannot be capped, so the one path that honours
    ``--max-turns`` has to be read somewhere else - and ``session_model_usage`` carries the
    field the whole diagnostic turns on, ``reasoning_tokens``, per session and per route.

    Read-only, by URI: this is somebody else's database and the harness is reading evidence
    out of it, not administering it. A session that never reached an API call leaves no row,
    which is reported as no accounting rather than as zeros.
    """
    database = Path(hermes_home) / SESSION_DB_FILENAME
    if not database.exists():
        return None
    columns = (
        "model",
        "billing_provider",
        "api_call_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "estimated_cost_usd",
    )
    try:
        with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in conn.execute(f"SELECT {', '.join(columns)} FROM {LEDGER_TABLE} WHERE session_id = ?", (session_id,))
            ]
    except sqlite3.Error as error:
        return {"error": f"{type(error).__name__}: {error}"}
    if not rows:
        return None

    # One row per (model, provider) route. A fallback mid-session would give two, and summing
    # the counters while naming only the busiest route would report a blend as if it were one
    # model - so the route count travels with the numbers.
    busiest = max(rows, key=lambda row: row.get("api_call_count") or 0)
    totals = {
        key: sum(int(row.get(key) or 0) for row in rows)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
    }
    return {
        **totals,
        "api_calls": sum(int(row.get("api_call_count") or 0) for row in rows),
        "estimated_cost_usd": sum(float(row.get("estimated_cost_usd") or 0.0) for row in rows),
        "model": busiest.get("model"),
        "provider": busiest.get("billing_provider"),
        "session_id": session_id,
        "routes": len(rows),
    }


def _hermes_home(sessions_dir: Path) -> Optional[Path]:
    """Where the session's throwaway HERMES_HOME was, as its own controls receipt recorded it."""
    receipts = sorted(sessions_dir.glob(f"*{CONTROLS_SUFFIX}"))
    if not receipts:
        return None
    try:
        controls = json.loads(receipts[0].read_text(encoding="utf-8"))
    except ValueError:
        return None
    home = controls.get("hermes_home")
    return Path(home) if home else None


def session_accounting(sessions_dir: Path) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """The session id the stream named, and what Hermes recorded against it.

    The pair the capped transport is read through, and the one the CI cap-consumption proof
    calls: if the accounting source ever moves, the proof fails in a minute instead of a
    30-minute diagnostic reporting no tokens and being read as a stalled provider.
    """
    streams = sorted(sessions_dir.glob(f"*{STREAM_JSON_SUFFIX}"))
    session_id = stream_session_id(_events(streams[0])) if streams else None
    home = _hermes_home(sessions_dir)
    if not (session_id and home):
        return session_id, None
    return session_id, ledger_usage(home, session_id)


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
    cleanroom: bool = True,
    transport: str = "stream-json",
    max_turns: Optional[int] = None,
) -> dict[str, Any]:
    """Administer the research phase once and keep everything it left behind."""
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r}; expected one of {', '.join(TRANSPORTS)}")
    if max_turns is not None and max_turns < 1:
        raise ValueError(f"max_turns must be at least 1, got {max_turns}")
    if max_turns is not None and transport != CAPPABLE_TRANSPORT:
        # Refused rather than ignored. The pinned --oneshot path would run to the wall budget
        # while the receipt said max_turns=1, and a measurement labelled as one turn that was
        # not one turn is worse than no measurement.
        raise ValueError(
            f"max_turns is not honoured on the {transport!r} transport at the pinned Hermes revision; "
            f"use --transport {CAPPABLE_TRANSPORT}"
        )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    task = task_spec(instance_id)
    _refuse_pre_existing(out_dir / PROBE_RPI_DIRNAME)
    _refuse_pre_existing(out_dir / PROBE_FILENAME)

    # Same workspace construction as a cell, down to the name: the agent's shell prompt shows
    # its own working directory, so `pb-probe-abishekvashok__cmatrix.5c082c6-x` would hand back
    # the instance id the envelope stopped naming.
    workspace = Path(tempfile.mkdtemp(prefix=f"pb-probe-{task.public_id}-"))
    if not cleanroom:
        # run_agent.sh refuses a non-git repo root. A cleanroom workspace gets its worktree
        # from the image; this branch is the synthetic repo, and nothing it measures describes
        # the environment a cell runs in.
        subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    control_dir = Path(tempfile.mkdtemp(prefix=f"pb-probe-control-{task.public_id}-"))

    sessions_dir = (out_dir / AGENT_SESSIONS_DIR).resolve()
    # A diagnostic reaches the same agent through the same shell as a lane, so it inherits
    # what a lane inherits and nothing else. Explicit `env` is the caller's own authorization
    # and is not filtered through that boundary.
    environment = audited_inherited_environment()
    if env:
        environment.update(env)
    environment["REPO_AUTOMATION_HERMES_USAGE_DIR"] = str(sessions_dir)
    environment["REPO_AUTOMATION_HERMES_TRANSPORT"] = transport
    if max_turns is not None:
        environment["REPO_AUTOMATION_HERMES_MAX_TURNS"] = str(max_turns)

    session_runner = runner or _subprocess_runner
    receipt = None
    witness = None
    if cleanroom:
        # The cell's own inference environment, in the cell's own order: materialize, name the
        # image to run_agent.sh, keep the receipt, then prove the two backends the model's
        # tools would get. The proof raises before the budget is touched - a diagnostic run in
        # the wrong sandbox costs 600 seconds and answers about the wrong sandbox.
        receipt = prepare_cleanroom(task.instance_id, workspace, task.repository)
        environment[SANDBOX_IMAGE_ENV] = receipt.image
        (out_dir / CLEANROOM_RECEIPT_FILENAME).write_text(json.dumps(receipt.to_dict(), indent=2) + "\n", encoding="utf-8")
        probe_sandbox(Path(repo_root), session_runner, workspace, environment, sessions_dir, receipt)
        witness = SandboxWitness(sessions_dir, workspace, receipt.image, receipt.image_id)
        session_runner = witness.wrap(session_runner)

    ctx = ExecutionContext(
        repo_root=Path(repo_root),
        task=task,
        workspace=workspace,
        control_dir=control_dir,
        command_template=_default_command_template(Path(repo_root)),
        env=environment,
        timeout_seconds=budget_seconds,
        runner=session_runner,
    )
    prompt = research_prompt(task)
    # Measured here because the phase result does not carry it, and a token count without the
    # time it took cannot say whether a long turn was spent generating or spent waiting.
    started = time.monotonic()
    result = run_phase(ctx, PROBE_PHASE, prompt, {"objective": task.objective}, budget_seconds)
    elapsed_seconds = round(time.monotonic() - started, 1)

    data, text = read_artifact(workspace, RESEARCH_ARTIFACT)
    copied: list[str] = []
    skipped_links: list[str] = []
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
                # One predicate, because the two cases differ only in which call reveals
                # them: a hard link is not a symlink and is_file() is True for it, so it
                # would be copied out and listed under `copied` as though the session had
                # written it. That launders host bytes into the evidence - the record would
                # assert a clean collection of something the session never produced.
                # `is_file()` guards the link count because a directory's own entries give
                # it st_nlink >= 2; only a regular file with more than one name is a hard link.
                if artifact.is_symlink() or (artifact.is_file() and artifact.lstat().st_nlink > 1):
                    skipped_links.append(artifact.name)
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
    # Two transports, two accounting sources, because each path leaves exactly one. Recorded
    # with the source named: the numbers mean the same thing, but how much of the run they
    # cover does not - the report is the whole -z call, the ledger row is the session.
    usage_reports = sorted(sessions_dir.glob(f"*{USAGE_SUFFIX}"))
    usage_source: Optional[str] = None
    usage = None
    if usage_reports:
        usage = json.loads(usage_reports[0].read_text(encoding="utf-8"))
        usage_source = USAGE_SUFFIX.lstrip(".")
    else:
        _session_id, usage = session_accounting(sessions_dir)
        if usage is not None:
            usage_source = f"{SESSION_DB_FILENAME}:{LEDGER_TABLE}"

    record = {
        "instance_id": task.instance_id,
        "phase": PROBE_PHASE,
        "transport": transport,
        "max_turns": max_turns,
        # Wall time beside the token counts, because the ratio is the finding: ten minutes
        # spent emitting a large hidden reasoning trace and ten minutes spent waiting to
        # start a small one look identical in a duration alone.
        "elapsed_seconds": elapsed_seconds,
        "usage_source": usage_source,
        # reasoning_tokens, which is the field the latency question turns on, and which the
        # stream protocol drops. Off --oneshot it comes from --usage-file, and there it also
        # carries `turn_exit_reason`; off the capped transport it comes from Hermes' own
        # session_model_usage row, which has no exit reason - the stream's terminal `result`
        # says how the session ended there.
        "usage": usage,
        # The environment, recorded rather than assumed: a probe.json that does not say which
        # sandbox it ran in cannot be told apart from the synthetic-repo ones that preceded it.
        "cleanroom": receipt.to_dict() if receipt is not None else None,
        # Recorded, not raised on. A cell refuses, because a violated sandbox makes its score
        # not a ProgramBench result; this run has no score, and its timeline is the deliverable.
        # Throwing the evidence away over a finding about the environment would lose both.
        "sandbox": {
            "observation": witness.receipt if witness is not None else None,
            "violations": list(witness.violations) if witness is not None else [],
        },
        "budget_seconds": budget_seconds,
        "prompt_chars": len(prompt),
        "phase_result": result.to_dict(),
        # A skip reads as a finding about the session; a silent omission would read as
        # "the agent wrote nothing", which is the question the probe exists to answer.
        "rpi": {
            "copied": copied,
            "skipped_links": skipped_links,
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
            "terminal_result": terminal_result(events),
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


def _rendered(names: list[str]) -> str:
    """Agent-chosen names, escaped at the point they are printed.

    ``probe.json`` is already safe - ``json.dumps`` escapes - but the summary joined these
    raw, so a name carrying a newline wrote whole lines of its own into the operator's
    answer, including lines that read exactly like the probe's own verdict. Escaping here
    rather than at collection keeps the real name in the record, where it is evidence.
    """
    return ", ".join(repr(name) for name in names)


def summarize(record: dict[str, Any]) -> str:
    """The four questions, answered in the order the decision tree asks them."""
    stream, artifact, phase = record["stream"], record["artifact"], record["phase_result"]
    lines = [
        f"instance      {record['instance_id']}",
        f"phase         {record['phase']} via {record['transport']}"
        + (f", max {record['max_turns']} turn(s)" if record.get("max_turns") else "")
        + f", {record['prompt_chars']} prompt chars",
        f"ended         rc {phase['returncode']} ({phase['state']}) after {phase['seconds']:.0f}s of {record['budget_seconds']}s",
        f"stream        {stream['events']} events ({stream['text_deltas']} text deltas) in {stream['file'] or 'no stream file'}",
        f"artifact      {'present' if artifact['present'] else 'ABSENT'}"
        f" ({artifact['chars']} chars, schema {artifact['schema_errors'] or 'valid'})",
    ]
    cleanroom = record.get("cleanroom")
    lines.append(
        f"sandbox       {cleanroom['image']} on NetworkMode={cleanroom['network_mode']}"
        if cleanroom
        else "sandbox       NO CLEANROOM: synthetic repo, local backend - not a cell's environment"
    )
    final = stream.get("terminal_result")
    if final:
        lines.append(
            f"session       exit {final.get('exit_code')} after {(final.get('duration_ms') or 0) / 1000:.0f}s,"
            f" tokens {(final.get('tokens') or {}).get('total')} (this transport names no model)"
        )
    usage = record.get("usage")
    elapsed = record.get("elapsed_seconds")
    if usage:
        # Tokens and time on adjacent lines, because the question is a ratio: a large hidden
        # reasoning trace and a slow start to a small one are the same wall clock.
        lines.append(
            f"accounting    in {usage.get('input_tokens')} / out {usage.get('output_tokens')}"
            f" / reasoning {usage.get('reasoning_tokens')} tokens over {usage.get('api_calls')} api calls"
        )
        lines.append(f"              {usage.get('model')} on {usage.get('provider')}, per {record.get('usage_source')}")
        if usage.get("turn_exit_reason") is not None or usage.get("completed") is not None:
            # --usage-file only. The ledger row records what was spent, not why it stopped.
            lines.append(
                f"              completed={usage.get('completed')} partial={usage.get('partial')}"
                f" interrupted={usage.get('interrupted')} exit_reason={usage.get('turn_exit_reason')}"
            )
        if (usage.get("routes") or 1) > 1:
            lines.append(f"              {usage['routes']} ROUTES summed: the counts are a blend, not one model's")
        if elapsed is not None:
            per_call = elapsed / (usage.get("api_calls") or 1)
            lines.append(f"              {elapsed:.0f}s wall, {per_call:.0f}s per api call")
    else:
        lines.append("accounting    NO USAGE REPORT: no report and no ledger row, so no token counts to read")
    for violation in record.get("sandbox", {}).get("violations", []):
        lines.append(f"sandbox flag  {violation}")
    rpi = record["rpi"]
    # Printed, not just recorded: these are the lines that stop a reader concluding the agent
    # wrote nothing when it wrote something the probe declined to follow.
    if rpi["container_skipped"]:
        lines.append(f"skipped links {RPI_DIR} itself is a symlink, so nothing under it was collected")
    if rpi["skipped_links"]:
        lines.append(f"skipped links {_rendered(rpi['skipped_links'])} (links in {RPI_DIR}, not followed)")
    if rpi["skipped_non_files"]:
        lines.append(f"skipped dirs  {_rendered(rpi['skipped_non_files'])} (not regular files in {RPI_DIR})")
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
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default="stream-json",
        help="stream-json shows the shape of a turn, can be capped with --max-turns, and "
        "reports its accounting from Hermes' session ledger; oneshot is what a lane runs, "
        "shows nothing until the turn ends, writes a --usage-file report, and cannot be capped.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Stop the session after N tool-calling iterations. stream-json only - the "
        "pinned --oneshot path honours no turn cap at all. Use 1 to read the accounting as "
        "soon as one provider turn completes instead of waiting out the whole phase.",
    )
    parser.add_argument(
        "--no-cleanroom",
        dest="cleanroom",
        action="store_false",
        help="Run in a synthetic git repo on the local filesystem instead of ProgramBench's "
        "cleanroom image. For exercising this module without Docker; the timings it produces "
        "describe no environment a cell runs in and are not comparable to a lane's.",
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
        cleanroom=args.cleanroom,
        transport=args.transport,
        max_turns=args.max_turns,
    )
    print(summarize(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
