# repo-automation roadmap

Status: **proposed reprioritization** (2026-10-11). This roadmap sequences new work; it does not claim the top layer is installed or authorize any autonomous dispatch.  
Governing architecture: [SPEC-001: Kanban, external memory and Swarm Wiki](architecture/control-plane.md).

## Ordering decision

The **Kanban / ByteRover / Swarm Wiki top layer is P0**. Complete its explicit acceptance gate before expanding the existing supervisor roadmap. Keep already-operational supervisor/CI functionality working during the transition. No speculative replacement of the Hermes dispatcher, second database, full agent framework or user-specific live board in this distribution.

## Milestones and dependencies

| Order | Milestone | Output | Exit evidence |
| --- | --- | --- | --- |
| P0.0 | Discover and pin | Inventory active boards, workers/profiles, status enum, source task IDs, queue owners, memory roots, wiki deployments, git/CI pins, current gaps and permissions | Reviewable inventory; unknown ownership blocks migration |
| P0.1 | Kanban adapter | Read-only, revision-aware board-to-slice projection; stable task/board IDs, dependency and source links; explicit one-writer cutover plan | Fixtures for valid/invalid status, stale revision, concurrent edit, blocked dependencies, foreign board; shadow diff versus existing JSON queue |
| P0.2 | ByteRover adapter | Per-project rooted, bounded and source-attributed memory retrieval; curated write verification, timeouts and access policy | Reproducible project/profile/worktree tests, provenance hashes, stored/recalled reviewed node, outage/no-leak tests |
| P0.3 | Swarm Wiki integration | Adopt audited pinned `hermes-swarm-protocol`; immutable evidence publication and deterministic wiki compilation | Independent byte-identical rebuild, `--check`, source digest and link lint, unresolved-claim tests; zero canonical-source mutations |
| P0.4 | Supervisor composition | Kanban-authorized intake, bounded memory in context, handoff evidence to canonical records; review-gated transition writer | End-to-end one-slice canary, independent validation, rollback and idempotent replay; runner selection unchanged |
| P0.5 | Fleet rollout and release | Canary each opted-in consumer/project/profile; migrate without losing histories or cross-project isolation | Signed-off config diff, exact commit pins, evidence receipts, CLI/API negative cases and rollback rehearsal |
| P1 | Resume supervisor roadmap | Consumer adoption/smoke proof; harden provenance, queue projections, handoff and validation contracts; CI/security/coverage and performance evidence | Tests on real pinned consumer snapshots; clean stops, no regression against registered paths |
| P2 | Resume effectiveness research | Revisit existing C-only model-pinned calibration and plan/startup visibility after P0; preserve provider/phase isolation | Verified actual per-phase artifacts and timings, no provider substitutions; explicit run authorization |
| P3 | Optional ontology | Only after stable board/memory/wiki + supervisor adoption: typed Project/Component/Capability/Risk/Policy/EvidenceType IDs and deterministic resolver, later optional JSON-LD/OWL/SHACL | Consumer-owned extensions; no ontology database in supervisor |

P0.1–P0.3 can be designed/tested independently; **P0.4 depends on all three**. P0.5 requires P0.4. The P1–P3 order describes feature expansion rather than forbidding urgent bounded bug/security fixes.

## First implementation slices (proposed, not started)

### RA-CP-001 — Control-plane inventory and gap audit

Read the existing `repo-automation` supervisor, Hermes Kanban and the deployed `hermes-swarm-protocol` contract. Record live/project-scoped ownership without copying confidential state into public source. Enumerate exact APIs, status names, board identities, memory roots, wiki input/output and currently supported auth. Flag any missing/ambiguous mapping. **No mutations.**

Acceptance: machine-checkable sanitized inventory template + reviewed risk register + sources and commit pins. No board migration.

### RA-CP-002 — Kanban projection contract

Define an adapter boundary that consumes supported Hermes Kanban read APIs/snapshots and emits existing validated slice records. Preserve the legacy JSON queue as current operational authority until an approved, rollback-tested cutover. Distinguish task provenance links from hard dependencies. Define guarded reconciliation and expected-revision semantics before any write adapter.

Acceptance: isolated contract fixtures and negative tests; deterministic repeat projections; source task and handoff traceability. No worker starts from an unapproved card.

### RA-CP-003 — Memory interface

Implement an adapter interface for ByteRover retrieval in the existing bounded context builder; configure explicit project-scoped roots, budgets and source digests. Provide no-model/offline fixture tests and error states. Curation acceptance requires actual persisted changes and reviewed origin, not exit status alone. Test ephemeral cwd changes and timeout killing both subprocess and post-processing.

Acceptance: bounded context payload and per-project isolation with reproducible fixture evidence.

### RA-CP-004 — Wiki source/publish integration

Integrate existing versioned Swarm Wiki source/publication contracts into the output and audit workflow. Do not regenerate from fabricated records or add raw DB mutation. Reuse its single-writer compiler through a pinned audited revision and expose build/check evidence without coupling it to agent execution.

Acceptance: deterministic independent read-only wiki generation; invalid provenance and unverified claims visible.

### RA-CP-005 — End-to-end authorization + cutover canary

Wire read-only projections, scoped memory and immutable wiki evidence into supervisor handoffs. Run one test task with verified scope and replayed validation, then explicitly approve canonical Kanban state update. Test blocked, retried, stale and unauthorized transitions. Reconcile ownership and perform rollout only after reviewer approval.

Acceptance: normal and failure paths verified; clean Git provenance, idempotent writes and successful rollback.

## Existing work retained

- **Supervisor:** `policy.load_queue`, `policy.select_next_slice`, schema validation, context building, bounded diffs, independent validation replay, clean Git stop and fresh worker processes remain. Only the supervisor decides continuation; no agent can recursively start new slices.
- **Runner:** preserve PR #68's nearest-enclosing-harness behavior and explicit `REPO_AUTOMATION_AGENT_RUNNER=hermes` in headless CI. Do not silently fall back to another installed CLI.
- **Performance:** maintain 100% benchmark coverage of **registered performance-critical paths** and independent correctness tests. Introduce CodSpeed coverage for new hot paths before calling them complete.
- **Effectiveness calibration:** retain pinned NVIDIA `z-ai/glm-5.3`, instances, workflow input configuration and experimental isolation until a **separate** change/dispatch is authorized. Run [37775851881](https://github.com/raelldottin/repo-automation/actions/runs/37775851881) has an overall workflow-success result but not a valid full C-lane treatment: user-reported research completed in **983.8s** under the configured **2160s** timeout (provisional **1800s** ceiling), while plan produced no `plan.json` before its **1080s** kill, and implement was not attempted. There is no measured plan or implement duration. Do not treat the workflow check as proof that lane C completed.
- **Provider availability:** preflight/cap failures due to NVIDIA 404/403 or stalls are infrastructure failures, not zero effectiveness scores; do not silently swap provider or model.
- **Consumer adoption:** continue exact-pin, manifest-owned snapshot distribution, domain-owned queues/credentials, independent proof ladder and consumer smoke testing.
- **Ontology:** defer until the Kanban/memory/wiki factory and supervisor interfaces are stable; it must not become a competing task store.

## Stop and review rules

Do not enable a background worker, alter a consumer board, curate project memory, publish restricted raw evidence, run a paid benchmark or merge a runtime PR as a side effect of this **specification** change. Each future implementation slice requires explicit scope, allowed paths, diff budget, validation ownership, proof level and reviewed merge/deployment authorization.

The first actionable next step after this spec PR is approved: **RA-CP-001, read-only inventory**. Subsequent slices are proposals until scheduled by the canonical Kanban owner and admitted by supervisor policy.
