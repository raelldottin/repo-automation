# repo-automation roadmap

Status: **proposed reprioritization** (2026-10-11). This roadmap sequences new work; it does not claim the top layer is installed or authorize any autonomous dispatch.  
Governing architecture: [SPEC-001: Kanban, external memory and Swarm Wiki](architecture/control-plane.md).

## Ordering decision

The **Kanban / Hindsight / Swarm Wiki top layer is P0**. Complete its explicit acceptance gate before expanding the existing supervisor roadmap. Keep already-operational supervisor/CI functionality working during the transition. No speculative replacement of the Hermes dispatcher, second database, full agent framework or user-specific live board in this distribution.

## Milestones and dependencies

| Order | Milestone | Output | Exit evidence |
| --- | --- | --- | --- |
| P0.0 | Discover, license and pin | Inventory boards/workers/status, old ByteRover files, Hindsight bank/tenant identities, wiki compiler input, local LLM/embeddings/reranker, model/software licenses, git/CI pins, gaps and permissions | Reviewed ownership/OSS inventory; any missing license or unresolved source blocks release |
| P0.1 | Kanban adapter | Read-only, revision-aware board-to-slice projection; stable task/board IDs, dependency and source links; explicit one-writer cutover plan | Fixtures for valid/invalid status, stale revision, concurrent edit, blocked dependencies, foreign board; shadow diff versus existing JSON queue |
| P0.2 | Self-hosted Hindsight adapter | Strict project/board-to-bank map, bounded source-attributed retain/recall/reflect, persist/backup tests, authenticated API boundary, local OSS inference/embeddings/reranking | Deterministic fixture tests, denied cross-bank access, reviewed retained+recalled facts and snapshots, egress-denied integration |
| P0.3 | Swarm Wiki Hindsight compatibility | Review upstream `hermes-swarm-protocol` migration from `.brv/context-tree` Markdown to authenticated, stable Hindsight snapshots while retaining immutable publications and historic ByteRover evidence | Upstream reviewed PR, typed adapter, source hash/provenance, independent byte-identical rebuild and `--check`, no canonical mutations |
| P0.4 | Supervisor composition | Kanban-authorized intake, bounded memory in context, handoff evidence to canonical records; review-gated transition writer | End-to-end one-slice canary, independent validation, rollback and idempotent replay; runner selection unchanged |
| P0.5 | Fleet rollout and open-source acceptance | Canary each opted-in consumer/project/profile; migrate without losing ByteRover history or cross-bank isolation; license and local-stack audit | Exact pins, owner-approved project licenses, SBOM/third-party notices, closed-egress self-hosted proof, rollback rehearsal |
| P1 | Resume supervisor roadmap | Consumer adoption/smoke proof; harden provenance, queue projections, handoff and validation contracts; CI/security/coverage and performance evidence | Tests on real pinned consumer snapshots; clean stops, no regression against registered paths |
| P2 | Resume effectiveness research | Revisit existing C-only model-pinned calibration and plan/startup visibility after P0; preserve provider/phase isolation | Verified actual per-phase artifacts and timings, no provider substitutions; explicit run authorization |
| P3 | Optional ontology | Only after stable board/memory/wiki + supervisor adoption: typed Project/Component/Capability/Risk/Policy/EvidenceType IDs and deterministic resolver, later optional JSON-LD/OWL/SHACL | Consumer-owned extensions; no ontology database in supervisor |

P0.1–P0.3 can be designed/tested independently; **P0.4 depends on all three**. P0.5 requires P0.4. The P1–P3 order describes feature expansion rather than forbidding urgent bounded bug/security fixes.

## First implementation slices (proposed, not started)

### RA-CP-001 — Control-plane inventory and gap audit

Read the existing `repo-automation` supervisor, Hermes Kanban and deployed `hermes-swarm-protocol`. Record project-scoped ownership without copying confidential state into public source. Enumerate APIs/status, board IDs, existing ByteRover tree roots, future Hindsight bank and local model identities, authorization, persisted evidence, wiki dependencies and OSI/SPDX licenses. The Hindsight upstream core is MIT, but both `repo-automation` and `hermes-swarm-protocol` currently lack a GitHub-detected root license and need an **owner-approved license decision**. Flag ambiguity. **No mutations.**

Acceptance: machine-checkable sanitized inventory template + reviewed OSS/licensing risk register, component/model source revisions and commit/image pins; no board or memory migration.

### RA-CP-002 — Kanban projection contract

Define an adapter boundary that consumes supported Hermes Kanban read APIs/snapshots and emits existing validated slice records. Preserve the legacy JSON queue as current operational authority until an approved, rollback-tested cutover. Distinguish task provenance links from hard dependencies. Define guarded reconciliation and expected-revision semantics before any write adapter.

Acceptance: isolated contract fixtures and negative tests; deterministic repeat projections; source task and handoff traceability. No worker starts from an unapproved card.

### RA-CP-003 — Memory interface

Implement an adapter for self-hosted Hindsight `retain`, `recall`, and `reflect` in bounded context workflows; configure explicit project-to-`bank_id` maps, endpoint, access rules, budgets and source digests. Hindsight bank IDs do not replace authorization controls. Use an offline/local-model profile with locally licensed inference, embeddings and reranker. Require original evidence references and independent durable recall/backup verification; API success or generated reflection alone does not count as source proof. Test ephemeral cwd, changed source, timeouts, egress denial and unauthorized cross-bank access.

Acceptance: bounded context payload, per-project bank isolation, locally inferred retention and provenance-preserving recall with reproducible fixture evidence.

### RA-CP-004 — Wiki source/publish integration

The current Swarm Wiki compiler **does not ingest Hindsight directly**; it reads ByteRover's `.brv/context-tree` and frontmatter. Prepare and review an upstream `hermes-swarm-protocol` PR adding deterministic, authorized Hindsight snapshot/export ingestion with typed provenance and historical ByteRover read compatibility. Keep original immutable evidence and versioned Swarm publications; no fabricated pages or raw DB mutation. Pin the audited revised compiler and expose build/check evidence without coupling it to agent execution.

Acceptance: demonstrated Hindsight-backed deterministic independent read-only wiki generation; historical ByteRover retention; invalid provenance, changed snapshots and unverified claims visible.

### RA-CP-005 — End-to-end authorization + cutover canary

Wire read-only projections, scoped memory and immutable wiki evidence into supervisor handoffs. Run one test task with verified scope and replayed validation, then explicitly approve canonical Kanban state update. Test blocked, retried, stale and unauthorized transitions. Reconcile ownership and perform rollout only after reviewer approval.

Acceptance: normal and failure paths verified; clean Git provenance, idempotent writes and successful rollback.

## Open-source release gate

Full-stack open source means **OSI-approved source licenses** for all required software and compatible **verified model-weight/runtime licenses**, with local inference, embeddings, reranking, storage and runtime. Hindsight is MIT; Hermes Agent is MIT. Repository visibility alone is not a license: confirm owner-approved license files for `repo-automation` and `hermes-swarm-protocol` before calling the stack fully open source. No required Hindsight Cloud, proprietary inference API, commercial-only orchestrator, proprietary Docker Desktop dependency or hosted model. A local stack still carries hardware and maintenance costs. External Claude Code/Codex/proprietary models may remain optional *development* tools; the separately pinned NVIDIA benchmark is not a production dependency. Audit and test these claims; do not infer them from upstream marketing.

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

The first actionable next step after this spec PR is approved: **RA-CP-001, read-only inventory and license audit**. The separate Swarm Wiki Hindsight-compatibility PR and source data migration need their own authorization. Subsequent slices are proposals until scheduled by the canonical Kanban owner and admitted by supervisor policy.
