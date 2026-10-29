# Kanban projection contract (RA-CP-002)

Status: **read-only adapter.** `run_next --kanban-board … --kanban-overlay …` uses it as the queue ([write-back contract](kanban-cutover.md)); without those flags the JSON queue (`automation/queue/slices.json`) stays the operational authority until the reviewed cutover in RA-CP-005. Nothing here writes to a board, starts a worker, or replaces `policy.load_queue`.

Code: `automation/supervisor/kanban_projection.py`. Tests: `automation/tests/test_kanban_projection.py`. Inputs and gaps: [the control-plane inventory](control-plane-inventory.md).

## Boundary

```text
~/.hermes/kanban/boards/<slug>/kanban.db ──read_board──▶ BoardSnapshot ─┐
repo-automation approval overlay (JSON) ──load_overlay──▶ KanbanOverlay ─┴─project──▶ Projection
                                                                                    (queue, provenance, revision)
```

- **Read path.** `read_board` opens `kanban.db` with `?mode=ro`, sets `PRAGMA query_only=ON` and reads every table inside one transaction, the same pattern as the Swarm Wiki compiler's `snapshot`. It never calls `hermes kanban`, because Hermes's `connect()` creates directories, takes the init lock and migrates the schema. A test makes the board directory and file read-only and checks the bytes are unchanged afterwards.
- **Schema check.** Kanban's schema is unversioned (`user_version` 0). The adapter checks for the columns it reads (`REQUIRED_COLUMNS`) and refuses the board if any are missing.
- **Output.** `Projection.queue` is a `SliceQueue` document. It must pass `policy.validate_document` and `policy.validate_queue_integrity` before it is returned, so `policy.load_queue` accepts it unchanged. A test checks that round trip.
- **CLI.** `python -m automation.supervisor.kanban_projection --board-dir <dir> --overlay <json>` prints the projection, its provenance and its revision to stdout, for a shadow comparison with the JSON queue. It writes no files.

## Approval overlay

Kanban has no source for `domain`, `allowed_paths`, `required_validations`, `max_files_changed` or the proof level. repo-automation owns these in an overlay, keyed by Kanban task id (`KanbanOverlay`; example in `automation/examples/kanban-overlay.json`):

```json
{"version": 1, "board": "<slug>", "policy": {<QueuePolicy>}, "approved": {"t_xxxxxxxx": {<policy fields>}}}
```

**Being listed in the overlay is the approval.** A task that isn't listed is never projected, so `select_next_slice` can never return it: no worker starts from a card nobody admitted. The overlay is admitted the way any queue change is admitted today, through a reviewed pull request. Overlays hold task ids and policy, never task text.

## Mapping

| Slice field | Source | Rule |
| --- | --- | --- |
| `slice_id` | board slug + `tasks.id` | `kanban.<slug>.<task_id>`. Task ids are only unique within a board, and slugs (`^[a-z0-9][a-z0-9\-_]{0,63}$`) contain no dots, so `parse_slice_id` gets the board and task back from a slice id or a handoff's `slice_id`. The id has no `/`, so handoff file names stay valid. |
| `title` | `tasks.title` | Stays in the projection. Never committed. |
| `status` | `tasks.status` | See the status table below. |
| `priority` | `tasks.priority` | Inverted to `max(priority) - priority` across projected tasks. Slices are emitted in Kanban dispatch order (`priority DESC, created_at ASC`, then id), so `select_next_slice` (lowest priority, then earliest index) picks the task Kanban would dispatch first. |
| `depends_on` | `task_links` | See the next section. |
| policy fields | overlay | Copied as written. |

| Kanban status | Slice status | Entry condition if the overlay gives none |
| --- | --- | --- |
| `ready` | `queued` | — |
| `running` | `in_progress` | — |
| `done` | `done` | — |
| `blocked` | `blocked` | Kanban task is blocked. |
| `todo` | `blocked` | Waiting on open parents. Kanban, not the projection, promotes `todo`. |
| `review` | `blocked` | Waiting for review. |
| `triage`, `scheduled` | `deferred` | Still in triage / scheduled for later. |
| `archived` | not projected | — |
| anything else | refused | `ProjectionError` |

Kanban has no `failed` task status (`crashed`, `gave_up` and `timed_out` are run outcomes), so the projection never emits `failed`. Kanban allows several `running` tasks, but the queue allows at most one `in_progress` slice. If more than one approved task is running, the projection is refused instead of being trimmed to one.

## Dependencies and provenance

Every `task_links` row is a hard dependency in Kanban. `recompute_ready` promotes a child only when all of its parents are `done` or `archived`, and `link_tasks` demotes a `ready` child to `todo` behind an open parent. The table has no link-kind column, so there is no provenance-only link to filter out.

Provenance is recorded separately: `creator_task_id` in the task's `created` event, which `inherit_creator_origin` uses to copy session and subscriptions without adding a dependency. The projection reports it as `provenance[slice_id].spawned_by` and never as `depends_on`.

For each parent link of a projected task:

- **Parent projected:** added to `depends_on`.
- **Parent `done` or `archived` and not projected:** dropped, because it's already satisfied.
- **Parent open and not approved:** dropped from `depends_on` (the queue can't see it). A child that would otherwise be `queued` becomes `blocked`, with an entry condition naming the parent.

## Revision and guarded reconciliation

There's no board revision column. `BoardSnapshot.revision` is the SHA-256 of the canonical JSON of what the projection reads: each task's `id, title, status, priority, created_at`, every link, and every creator. Equal boards give equal revisions, and repeat projections are byte-identical.

`max(task_events.id)` is reported as `event_watermark` and is never compared. The inventory couldn't show that every mutation appends an event, and many events (heartbeats, for example) don't change the projection. A test shows such an event moves the watermark but not the revision.

Expected-revision semantics come before any write adapter:

1. A writer acts on a projection and keeps its `revision`.
2. Before writing, it takes a fresh snapshot and calls `require_revision(fresh, expected)`. A mismatch raises `StaleRevisionError`.
3. On a mismatch it writes nothing, re-projects and re-decides. It never merges its intent into a newer board.
4. The snapshot check and the write aren't atomic across processes. A write adapter must also make the write conditional on the row state it read (for example `UPDATE … WHERE id = ? AND status = ?`), as Hermes's own transitions do. RA-CP-005 owns that adapter and its one-writer cutover: see [the write-back contract](kanban-cutover.md). Hermes's `expected_run_id` is that condition. This slice adds no write path.

## Evidence

| Roadmap fixture | Test |
| --- | --- |
| valid status | `test_projection_maps_status_priority_and_dependencies` |
| invalid status | `test_unknown_kanban_status_is_refused`, `test_two_running_tasks_cannot_become_one_queue` |
| stale revision | `test_stale_revision_is_refused` |
| concurrent edit | `test_concurrent_edit_leaves_the_snapshot_intact_and_moves_the_revision` |
| blocked dependencies | `test_unapproved_card_is_never_projected` (also the unapproved-card rule) |
| foreign board | `test_overlay_for_a_foreign_board_is_refused`, `test_overlay_approving_a_task_from_another_board_is_refused` |
| deterministic repeat | `test_repeat_projection_is_byte_identical` |
| traceability | `test_slice_id_traces_back_to_board_and_task`, `test_creator_is_provenance_not_a_dependency` |

The projection is a registered performance-critical path, benchmarked by `test_project_kanban_board_benchmark` on the bundled fixture. This slice leaves `load_queue` and `select_next_slice` unchanged. The pull request's CodSpeed run is their evidence.

## Not in this slice

- A shadow diff against a live JSON queue. Kanban slice ids and queue slice ids are different namespaces, so a diff needs an id map that only a cutover plan can define (P0.1 exit evidence, RA-CP-005).
- Any write adapter, any supervisor wiring, and multi-board projection.
