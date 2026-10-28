# repo-automation roadmap

Status: **proposed reprioritization** (2026-10-11). This roadmap sequences new work; it does not claim the top layer is installed or authorize any autonomous dispatch.  
Governing architecture: [SPEC-001: Kanban, external memory and Swarm Wiki](architecture/control-plane.md).

## Ordering decision

The **Kanban / Hindsight / Swarm Wiki top layer is P0**. Complete its explicit acceptance gate before expanding the existing supervisor roadmap.

**Benchmark exemption (owner decision).** Benchmark and effectiveness work is exempt from this ordering. It continues in parallel with P0 under explicit owner authorization and is not paused until the P0 gate passes. The exemption covers the work, not its controls: each run, model, provider or budget change still needs its own authorization, and benchmark experiments stay isolated from live boards, memory stores and wiki infrastructure. Current benchmark status and run diagnostics live in [the effectiveness benchmark workflow](workflows/effectiveness-benchmark.md), not in this roadmap. Keep already-operational supervisor/CI functionality working during the transition. No speculative replacement of the Hermes dispatcher, second database, full agent framework or user-specific live board in this distribution.

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
| P2 | Effectiveness research (parallel, exempt from P0 ordering) | Continue model-pinned calibration and plan/startup visibility under explicit owner authorization; preserve provider/phase isolation | Verified actual per-phase artifacts and timings, no provider substitutions; explicit run authorization |
| P3 | Optional ontology | Only after stable board/memory/wiki + supervisor adoption: typed Project/Component/Capability/Risk/Policy/EvidenceType IDs and deterministic resolver, later optional JSON-LD/OWL/SHACL | Consumer-owned extensions; no ontology database in supervisor |

P0.1–P0.3 can be designed/tested independently; **P0.4 depends on all three**. P0.5 requires P0.4. The P1 and P3 order describes feature expansion rather than forbidding urgent bounded bug/security fixes; P2 runs in parallel under the benchmark exemption above.

## First implementation slices (proposed, not started)

**Admission while the JSON queue is authoritative.** Kanban is not wired into the supervisor yet, so these slices cannot be admitted from a board. Until the reviewed cutover in RA-CP-005, each RA-CP slice is admitted the same way as any other work today: either as a record in `automation/queue/slices.json` that passes `policy.validate_document` and is selected by `policy.select_next_slice`, or as an ordinary reviewed pull request with explicit scope, allowed paths and validation. No slice is admitted by being written on a board, in memory or in the wiki.

### RA-CP-001 — Control-plane inventory and gap audit

Read the existing `repo-automation` supervisor, Hermes Kanban and deployed `hermes-swarm-protocol`. Record project-scoped ownership without copying confidential state into public source. Enumerate APIs/status, board IDs, existing ByteRover tree roots, future Hindsight bank and local model identities, authorization, persisted evidence, wiki dependencies and OSI/SPDX licenses. The Hindsight upstream core is MIT. As of 2026-10-11, neither `repo-automation` nor `hermes-swarm-protocol` had a GitHub-detected root license; re-check, and record that both need an **owner-approved license decision** (agents do not choose licenses). Flag ambiguity. **No mutations.**

Acceptance: machine-checkable sanitized inventory template + reviewed OSS/licensing risk register, component/model source revisions and commit/image pins; no board or memory migration.

### RA-CP-002 — Kanban projection contract

Define an adapter boundary that consumes supported Hermes Kanban read APIs/snapshots and emits existing validated slice records. Preserve the legacy JSON queue as current operational authority until an approved, rollback-tested cutover. Distinguish task provenance links from hard dependencies. Define guarded reconciliation and expected-revision semantics before any write adapter.

Acceptance: isolated contract fixtures and negative tests; deterministic repeat projections; source task and handoff traceability. No worker starts from an unapproved card. Because the projection feeds `policy.load_queue` and `policy.select_next_slice`, both registered performance-critical paths, the change MUST carry CodSpeed evidence for `test_load_queue_benchmark` and `test_select_next_slice_benchmark` per the Performance Evidence rules in `AGENTS.md`, and register any new hot path (for example the projection itself) with its own benchmark.

### RA-CP-003 — Memory interface

Implement an adapter for self-hosted Hindsight `retain`, `recall`, and `reflect` in bounded context workflows; configure explicit project-to-`bank_id` maps, endpoint, access rules, budgets and source digests. Hindsight bank IDs do not replace authorization controls. Use an offline/local-model profile with locally licensed inference, embeddings and reranker. Require original evidence references and independent durable recall/backup verification; API success or generated reflection alone does not count as source proof. Test ephemeral cwd, changed source, timeouts, egress denial and unauthorized cross-bank access.

Acceptance: bounded context payload, per-project bank isolation, locally inferred retention and provenance-preserving recall with reproducible fixture evidence. Because recall results enter `build_context_bundle`, a registered performance-critical path, the change MUST carry CodSpeed evidence for `test_build_context_bundle_benchmark` per the Performance Evidence rules in `AGENTS.md`, with the benchmark exercising bundled fixtures rather than a live Hindsight server.

### RA-CP-004 — Wiki source/publish integration

The current Swarm Wiki compiler **does not ingest Hindsight directly**; it reads ByteRover's `.brv/context-tree` and frontmatter. Prepare and review an upstream `hermes-swarm-protocol` PR adding deterministic, authorized Hindsight snapshot/export ingestion with typed provenance and historical ByteRover read compatibility. Keep original immutable evidence and versioned Swarm publications; no fabricated pages or raw DB mutation. Pin the audited revised compiler and expose build/check evidence without coupling it to agent execution.

Acceptance: demonstrated Hindsight-backed deterministic independent read-only wiki generation; historical ByteRover retention; invalid provenance, changed snapshots and unverified claims visible.

### RA-CP-005 — End-to-end authorization + cutover canary

Wire read-only projections, scoped memory and immutable wiki evidence into supervisor handoffs. Run one test task with verified scope and replayed validation, then explicitly approve canonical Kanban state update. Test blocked, retried, stale and unauthorized transitions. Reconcile ownership and perform rollout only after reviewer approval.

Acceptance: normal and failure paths verified; clean Git provenance, idempotent writes and successful rollback.

## Open-source release gate

Full-stack open source means **OSI-approved source licenses** for all required software and compatible **verified model-weight/runtime licenses**, with local inference, embeddings, reranking, storage and runtime. Hindsight is MIT; Hermes Agent is MIT. Repository visibility alone is not a license: confirm owner-approved license files for `repo-automation` and `hermes-swarm-protocol` before calling the stack fully open source, and extend the existing `THIRD-PARTY-NOTICES.md` for bundled notices. No required Hindsight Cloud, proprietary inference API, commercial-only orchestrator, proprietary Docker Desktop dependency or hosted model. A local stack still carries hardware and maintenance costs. External Claude Code/Codex/proprietary models may remain optional *development* tools; the separately pinned NVIDIA benchmark is not a production dependency. Audit and test these claims; do not infer them from upstream marketing.

## Existing work retained

- **Supervisor:** `policy.load_queue`, `policy.select_next_slice`, schema validation, context building, bounded diffs, independent validation replay, clean Git stop and fresh worker processes remain. Only the supervisor decides continuation; no agent or sub-agent can start the next slice. Agent-native delegation (a runner such as Claude Code launching its own sub-agents within a slice) is available today; supervisor-controlled delegation is proposed by SPEC-001 and not implemented.
- **Runner:** preserve raelldottin/repo-automation#68's nearest-enclosing-harness behavior and explicit `REPO_AUTOMATION_AGENT_RUNNER=hermes` in headless CI. Do not silently fall back to another installed CLI.
- **Performance:** maintain 100% benchmark coverage of **registered performance-critical paths** and independent correctness tests. Introduce CodSpeed coverage for new hot paths before calling them complete.
- **Effectiveness calibration:** continues in parallel under the benchmark exemption. Retain pinned NVIDIA `z-ai/glm-5.3-flash` (glm-5.3 reached end of life on 2026-10-28), instances, workflow input configuration and experimental isolation unless a **separate** change/dispatch is authorized. For current run status and diagnostics, see [the effectiveness benchmark workflow](workflows/effectiveness-benchmark.md); do not treat a workflow check as proof that a lane completed.
- **Provider availability:** preflight/cap failures due to NVIDIA 404/403 or stalls are infrastructure failures, not zero effectiveness scores; do not silently swap provider or model.
- **Consumer adoption:** continue exact-pin, manifest-owned snapshot distribution, domain-owned queues/credentials, independent proof ladder and consumer smoke testing.
- **Ontology:** defer until the Kanban/memory/wiki factory and supervisor interfaces are stable; it must not become a competing task store.

## Stop and review rules

Do not enable a background worker, alter a consumer board, curate project memory, publish restricted raw evidence, run a paid benchmark outside its own authorization or merge a runtime PR as a side effect of this **specification** change. Each future implementation slice requires explicit scope, allowed paths, diff budget, validation ownership, proof level and reviewed merge/deployment authorization.

The first actionable next step after this spec PR is approved: **RA-CP-001, read-only inventory and license audit**. The separate Swarm Wiki Hindsight-compatibility PR and source data migration need their own authorization. Subsequent slices are proposals until admitted through the JSON queue or an ordinary reviewed pull request, as described under First implementation slices; board-driven admission begins only after the reviewed cutover.
