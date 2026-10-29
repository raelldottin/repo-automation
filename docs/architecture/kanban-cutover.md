# Kanban write-back contract (RA-CP-005)

Status: **wired into `run_next` behind an opt-in flag, never run against a real board.** Without `--kanban-board` the supervisor reads and writes the JSON queue exactly as before, and that queue stays the operational authority. No board under `~/.hermes` was read or written to build this.

Code: `automation/supervisor/kanban_writer.py`, `automation/supervisor/run_next.py` (`KanbanIntake`). Tests: `automation/tests/test_kanban_writer.py`, `automation/tests/test_run_next_kanban.py`. Read side: [the Kanban projection](kanban-projection.md).

## One writer, through Hermes

SPEC-001 forbids raw SQLite writes, so every write is a `hermes kanban --board <slug>` command. Hermes then keeps its own locks, events and guards. The adapter reads the board read-only (the projection's `mode=ro`, `query_only` connection) to decide whether a write may happen, and again afterwards to check that it did.

| Step | Hermes command | Guard before | Check after |
| --- | --- | --- | --- |
| `claim` | `claim <task> --ttl <s>` | The board is still at the projected `revision` (`require_revision`). The task is `ready` and has no assignee. | The task is `running` with a `current_run_id`, which becomes the supervisor's run. |
| `record` `done` | `request-review <task> --summary … --metadata {slice_id, handoff_sha256}` | The supervisor's run is still `running` and still the task's current run. | That run's outcome is `review_requested`. |
| `record` `blocked` | `block <task> --kind needs_input "<reason> (handoff sha256 …)"` | Same | Outcome `blocked` |
| `record` `failed` | `block <task> --kind transient "<reason> (handoff sha256 …)"` | Same | Outcome `blocked` |

- **The claim is the lock.** The Hermes gateway only dispatches `ready` tasks, so a `running` task belongs to whoever claimed it. An assigned task is refused, because the gateway owns it. If the gateway claims first, Hermes refuses our claim and nothing runs.
- **Writes are conditional on our run.** `record` sets `HERMES_KANBAN_TASK` and `HERMES_KANBAN_RUN_ID`, which Hermes passes as `expected_run_id`. Hermes refuses the write if that run is no longer current, even if the board changed after our own check. Any ambient worker scope in the environment is dropped first.
- **`done` is never written.** A supervisor `done` becomes Kanban `review`. A person approves it in Kanban, which is the explicit approval of canonical state that the roadmap asks for.
- **Idempotent.** If the run already has the outcome a verdict produces, `record` returns `False` and calls nothing. A run that ended any other way (for example `reclaimed`) is refused.
- **Fail closed.** A Hermes refusal, a timeout, a missing `hermes`, or a write Hermes reported that the board doesn't show all raise `KanbanWriteError`. Nothing is retried or guessed.

## Supervisor intake

```bash
python3 automation/supervisor/run_next.py --kanban-board ~/.hermes/kanban/boards/<slug> \
  --kanban-overlay <approval overlay> [--hermes "hermes"] [--dry-run]
```

- **Both flags or neither.** With them, `run_next` takes slices from the board's projection instead of `--queue`. Without them, nothing changes: `JsonQueueIntake` performs the same queue reads and writes as before.
- **Each pass projects again.** The projected queue is written to a temporary directory outside the repository, only because `build_context_bundle` reads a queue file. It is a working copy. The board stays the authority, and the JSON queue isn't read or written.
- **Claim, then run.** `claim` runs at the revision projected in the same pass, after the context is built and before the agent starts. If the board changed in between, or Hermes refuses, `run_next` stops with `stop: kanban: …` and exit code 2, and the agent never starts.
- **Every outcome is recorded.** The verdict from `evaluate_completion` goes through `record`. So does every failure that happens after the claim: the agent exits non-zero, the handoff is missing or invalid, or the slice id doesn't match. Each of those becomes `failed` (Kanban `block --kind transient`). A failure before any handoff exists is recorded with `handoff sha256 absent`. A `done` verdict always cites the handoff it was judged on.
- **A retry is a human unblock.** A blocked task returns to `ready` only through Kanban. The next run claims a fresh Hermes run and can close only that one.
- **Other work on the board stops it.** If any approved task is already `running`, `run_next` refuses to start, as it does for an `in_progress` slice in the JSON queue.
- `--dry-run` projects and reports `kanban_revision`. It never claims.

## Known limits

- **The claim can't be renewed.** Hermes locks a claim to the claiming process (`host:pid`), and a CLI claim's process exits at once. So the TTL must cover the whole slice. After it expires Hermes reclaims the task, and `record` fails closed. The default is two hours. If slices outgrow that, the upgrade is an in-process claim through `hermes_cli.kanban_db`, which couples to Hermes internals.
- **Goal-mode tasks.** For a task with `goal_mode`, Hermes runs a goal judge before `request-review` and may refuse. The adapter reports that refusal; it doesn't work around it.
- **Tested against a stand-in.** The stand-in `hermes` applies the guards of `claim_task`, `request_review` and `block_task` as they are in the local Hermes build `31bde0d`. The real CLI hasn't been run, because its launcher can't start with a throwaway `HERMES_HOME`.

## Performance evidence

The writer runs once or twice per slice and is bounded by a subprocess, so it is not a performance-critical path. It shares `read_only` with `kanban_projection.read_board`, which is registered. The pull request's CodSpeed run covers that path.

## Not done here

1. A canary on a real, dedicated test board with the real CLI, including a rollback rehearsal. This needs the owner's approval of the board and the run.
2. Hindsight installed for the memory half of the canary. This is an owner decision, as it was for RA-CP-003 and RA-CP-004.
