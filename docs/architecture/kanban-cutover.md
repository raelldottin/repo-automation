# Kanban write-back contract (RA-CP-005)

Status: **wired into `run_next` behind explicit flags, and run once against a dedicated canary board with the real `hermes` CLI ([canary](#canary-2026-10-29)).** The JSON queue stays the default and the operational authority. No board other than the two canary boards was written.

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
- **CI uses a stand-in.** The unit tests' stand-in `hermes` applies the guards of `claim_task`, `request_review` and `block_task` from the local Hermes build `31bde0d`. The real CLI is covered by an opt-in test; see the next section.

## Real Hermes CLI evidence

`test_the_real_hermes_cli_honours_the_contract` runs the real `hermes kanban` code on a throwaway `HERMES_HOME`. It is skipped unless `HERMES_AGENT_PYTHON` names an interpreter with Hermes's dependencies and `HERMES_AGENT_SOURCE` a `hermes-agent` checkout, because the installed launcher can't start with a throwaway home and CI has no Hermes:

```bash
HERMES_AGENT_PYTHON=~/.hermes/hermes-agent/venv/bin/python HERMES_AGENT_SOURCE=~/.hermes/hermes-agent \
  python -m pytest automation/tests/test_kanban_writer.py -k real_hermes
```

On 2026-10-29 it passed against the local build `31bde0d`. The board, its three tasks and every write lived in the throwaway home; `~/.hermes/kanban` was not modified. It showed:

- a claim at a stale revision is refused before Hermes is called;
- `claim` then `record done` leaves the task in `review` with outcome `review_requested` and the `slice_id` and handoff digest in the run's metadata; recording again is a no-op;
- `record failed` without a handoff leaves the task `blocked` with `block_kind` `transient`;
- after an operator `reclaim` and a new claim, `record` refuses the superseded run, and **Hermes itself** refuses a `request-review` that names it, leaving the newer claim running.

This is evidence about the CLI contract, not the canary: no real board, gateway or agent was involved.

## Performance evidence

The writer runs once or twice per slice and is bounded by a subprocess, so it is not a performance-critical path. It shares `read_only` with `kanban_projection.read_board`, which is registered. `build_context_bundle`, also registered, gained an optional `queue_data` argument that skips its own `load_queue` when a caller already holds the queue. Each pull request's CodSpeed run covers those paths.

## Canary (2026-10-29)

Run with the owner's approval on a dedicated board, `ra-cp-canary`. It used the real CLI (Hermes Agent `31bde0d`) while the gateway was running. The supervisor was repo-automation `843077f`, run from a throwaway consumer repository holding that commit's `automation/` tree. The agent was a script, not a model: it appends the slice id to `canary.txt`, commits, and writes a handoff. The overlay approved two of the board's three unassigned `ready` tasks, both with `allowed_paths: ["canary.txt"]` and `required_validations: ["git diff --check"]`.

**Setup.**

- **Gateway.** With `kanban.default_assignee` empty, the gateway doesn't dispatch unassigned tasks (`_resolve_default_assignee` and `skipped_unassigned` in `hermes_cli/kanban_db_dispatch.py`). It never claimed a canary task. Every run on the board is the supervisor's.
- **Current board.** `~/.hermes/kanban/current` stayed `tachikoma` throughout.

**Runs.**

| Step | Result on the board |
| --- | --- |
| Dry run | Selected task A. No run was created and every task stayed `ready`. |
| Normal path, A | Claimed as run 1. The agent committed, and the supervisor replayed `git diff --check` (exit 0). Verdict `done` was recorded as `request-review`: A is `review`, and run 1 is `review_requested` with `{"slice_id", "handoff_sha256"}`. That sha256 matches the handoff file byte for byte. |
| Failure, B | The agent exited 3 before writing a handoff. B was recorded as `block --kind transient`, with reason `… (handoff sha256 none)`. B is `blocked`, and run 2 is `blocked`. |
| Retry, B | Unblocked with `hermes kanban unblock`, as a person would. The next pass claimed a fresh run 3 and recorded `review_requested`. Run 2 is unchanged. |
| Unapproved, C | Never claimed. Once A and B were in review, the next pass stopped with `no eligible queued slice found` while C was still `ready`. |
| Idempotent write | Recording run 1's `done` again, or run 2's `failed` again, returned `False` with no Hermes call. |
| Stale and foreign transitions | These were refused before Hermes was called: a claim of A while it was in `review` ("not ready"), a claim at the pre-canary revision (`StaleRevisionError`), and a `done` on the ended run 2 ("not ours to close"). |
| Rollback rehearsal | Before any run, the board was exported (`hermes kanban boards export`), then imported afterwards as `ra-cp-canary-rollback`. Its projection, recomputed under the original slug, has exactly the pre-canary revision `ad70a7ed…3af8`: three `ready` tasks and no runs. |

**Git provenance.** The consumer repository ended clean, with one commit per completed slice, and each handoff names its slice.

**Not exercised by the canary.**

- Hermes's own `expected_run_id` refusal. Our pre-check refuses first, so the canary never reached it. The real CLI's refusal is covered by `test_the_real_hermes_cli_honours_the_contract` ([Real Hermes CLI evidence](#real-hermes-cli-evidence)).
- A claim racing the gateway.
- Goal-mode tasks.

**Left for the owner.**

- A and B are in `review`. Approving them in Kanban is the explicit approval of canonical state that RA-CP-005 asks for.
- The two canary boards remain until you archive them (`hermes kanban boards rm <slug>` archives by default).

## Not done here

1. Hindsight installed for the memory half of the canary. This is an owner decision, as it was for RA-CP-003 and RA-CP-004.
