# Kanban write-back contract (RA-CP-005)

Status: **wired into `run_next` behind explicit flags, never run against a real board.** The JSON queue stays the default and the operational authority. No board under `~/.hermes` was read or written to build this.

Code: `automation/supervisor/kanban_writer.py`, and `KanbanQueue` in `automation/supervisor/run_next.py`. Tests: `automation/tests/test_kanban_writer.py`, `automation/tests/test_run_next_kanban.py`. Read side: [the Kanban projection](kanban-projection.md).

## Running the supervisor on a board

```bash
python automation/supervisor/run_next.py --kanban-board ~/.hermes/kanban/boards/<slug> \
  --kanban-overlay <overlay.json> [--hermes "hermes"] [--agent-cmd …]
```

- **The board is the queue.** `--queue` is ignored and no JSON queue is read or written, so nothing is dual-written. Each loop reads a fresh projection.
- **Claim before the agent.** Setting a slice `in_progress` claims its task at the revision just projected. A refused claim stops the run (`stop: …`, exit 2) before the agent starts.
- **Record after the decision.** Every later status, including each failure path, is recorded on that claim. A failure before the agent wrote a handoff is recorded with `handoff sha256 none`; a `done` verdict always has its handoff.
- **Claim TTL.** The claim lasts `handoff_timeout_seconds` from the overlay's policy plus 30 minutes (`CLAIM_MARGIN_SECONDS`), so it outlasts the agent and the validation replay.
- **Continuation.** A slice the supervisor finished is in Kanban `review`, so the projection shows it as blocked. Its Kanban children stay `todo` until a person approves it, and the loop can't run ahead of that approval.

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

## Known limits

- **The claim can't be renewed.** Hermes locks a claim to the claiming process (`host:pid`), and a CLI claim's process exits at once. So the TTL must cover the whole slice. After it expires Hermes reclaims the task, and `record` fails closed. The default is two hours. If slices outgrow that, the upgrade is an in-process claim through `hermes_cli.kanban_db`, which couples to Hermes internals.
- **Goal-mode tasks.** For a task with `goal_mode`, Hermes runs a goal judge before `request-review` and may refuse. The adapter reports that refusal; it doesn't work around it.
- **Tested against a stand-in.** The stand-in `hermes` applies the guards of `claim_task`, `request_review` and `block_task` as they are in the local Hermes build `31bde0d`. The real CLI hasn't been run, because its launcher can't start with a throwaway `HERMES_HOME`.

## Performance evidence

The writer runs once or twice per slice and is bounded by a subprocess, so it is not a performance-critical path. It shares `read_only` with `kanban_projection.read_board`, which is registered. `build_context_bundle`, also registered, gained an optional `queue_data` argument that skips its own `load_queue` when a caller already holds the queue. Each pull request's CodSpeed run covers those paths.

## Not done here

1. A canary on a real, dedicated test board with the real CLI, including a rollback rehearsal. This needs the owner's approval of the board and the run.
2. Hindsight installed for the memory half of the canary. This is an owner decision, as it was for RA-CP-003 and RA-CP-004.
