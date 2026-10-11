# SPEC-001: Kanban, external memory, and Swarm Wiki control plane

Status: **proposed architecture; not a claim of runtime implementation**  
Priority: **P0 prerequisite for subsequent repo-automation roadmap execution**  
Authority: This document defines the target deployment architecture. Existing supervisor queue behavior remains operative until a reviewed, tested cutover.

## Decision

The top-level project interface MUST provide three cooperating surfaces:

1. **Kanban board** — canonical human/agent-visible work state: tasks, assignment, priority, dependencies, progress, attempts, review and final disposition. Prefer the installed **Hermes Kanban** implementation; do not build a second scheduler or board database merely to satisfy the UI requirement.
2. **External memory** — curated, durable, per-project knowledge beyond an agent's chat or process lifetime. Use **ByteRover** as the initial reference integration (context tree, source-aware recall, human-reviewed curation and shared project roots). Memory is not the queue and never grants execution authority.
3. **Swarm Wiki** — navigable, versioned, rebuildable synthesis of decisions, claims, evidence, open questions, changes and history. Prefer the existing [hermes-swarm-protocol](https://github.com/raelldottin/hermes-swarm-protocol) compiler and published records rather than another wiki database. Wiki pages are generated projections, not authoritative work state or independent proof.

These are three **logical responsibilities**, not necessarily three servers. They sit above the existing deterministic repo-automation supervisor, which remains the sole authority for accepting runnable slices, enforcing scope/budgets, interpreting handoffs and deciding continuation.

```text
                    TOP LAYER: operator / agent navigation
              +----------------+-----------------------+
              | Kanban board   | Swarm Wiki (read-only)|
              | work authority | derived audit / docs  |
              +-------+--------+-----------+-----------+
                      |                    ^
              +-------v--------------------+-----------+
              | External memory: ByteRover             |
              | project-scoped curated knowledge       |
              +------------------+---------------------+
                                 |
            explicit task/knowledge/evidence references
                                 |
              +------------------v---------------------+
              | repo-automation deterministic supervisor|
              | queue projection, policy, context, gates|
              +------------------+---------------------+
                                 |
              fresh bounded runner (Hermes/Claude/Codex)
                                 |
              JSON handoff + immutable live evidence
```

The diagram depicts navigation and data flow, **not** permission for memory or the Wiki to schedule tasks. Canonical board records and evidence feed the wiki compiler; ByteRover provides knowledge inputs. There is no required live LLM call to render the wiki.

## Ownership and precedence

| Concern | Canonical owner | Not allowed |
| --- | --- | --- |
| Task identity, status, assignee, dependencies, lifecycle, dispatch | Hermes Kanban per-project board | Wiki/memory/worker privately changing statuses, inferred statuses, second scheduler |
| Curated requirements, design knowledge, prior solutions, uncertainties | ByteRover per-project context tree | Treating recalled prose as a verified test or executable instruction |
| Claim/evidence/decision publications and cross-agent request/verify/route | Swarm protocol records on canonical Kanban comments and immutable captures | Unverified conclusions or a parallel coordination database |
| Human-readable synthesis and searchable wiki pages | One deterministic Swarm Wiki compiler | Workers directly editing generated output or compiler mutating upstream stores |
| Slice eligibility, allowed paths, diff budgets, validation tiers, run/handoff verification, next-run decision | repo-automation supervisor | Recursive worker spawning, retrospective invented evidence, model-driven overriding of gates |
| Source code, tests, commit provenance | Consumer repository at pinned Git identity | Storing credentials or live source-of-truth artifacts in the generated wiki |

Precedence during an incident: **original live evidence and committed source + canonical Kanban records > reviewed ByteRover knowledge > generated wiki text**. A wiki discrepancy is a lint finding, never an automatic board mutation. A pass requires independent scope-matched non-memory live evidence. Keep `swarm_verify`'s inspect → evidence receipt → commit discipline and deterministic `swarm_route`; unresolved destinations fail closed.

## Required contracts

### A. Kanban and the existing slice queue

- Inventory existing Hermes boards, profile identities, status enums, project mappings, source cards and dependency links through supported read paths before making changes. Preserve every existing board and history.
- Each executable slice MUST have a stable canonical `board_id`, `task_id` and source/provenance reference in the proposed adapter contract. Preserve task relationships separately from execution dependencies.
- Existing `automation/queue/slices.json` and JSON schemas remain supported during migration. **Today they are the implementation's execution authority**; do not falsely mark Kanban as already wired into `run_next.py`.
- Target cutover: Kanban becomes the authoritative task state, and a deterministic, validated **read-only projection** produces supervisor-eligible slices. Existing supervisor selection and stop rules still decide execution; no board status implies authorization to run a slice.
- All transitions back to Kanban go through a separately authorized, idempotent adapter after handoff validation. Reject stale version/revision, invalid status, foreign board, ambiguous task, lost source reference and concurrent edit rather than guessing. Never update raw Kanban SQLite as a shortcut.
- In the transition, name one status writer explicitly and reconcile before switching ownership. Do not dual-write two active queues or fabricate historical board state.

### B. ByteRover memory

- Declare the project and exact absolute `memory.byterover.workdir` for each board/consumer; never let worker cwd or `HERMES_HOME` silently select a different knowledge tree.
- Separate read-mostly coordinator identities from authorized curators. Default `auto_extract: false` for coordinators; accept curation only after durable writes, trace/review where configured and verifiable node hashes. CLI exit 0 alone is not proof of storage.
- Context bundles retrieve **bounded, relevant, source-attributed** knowledge; include source revision, origin, hash, task scope and freshness. Never paste unbounded memory dumps onto Kanban cards or into prompts.
- Timeouts, unavailable model, missing source, empty result and stale knowledge are explicit states, not permission to substitute another model or invent a claim. Enforce subprocess and join deadlines at the integration boundary.
- Protect secrets/credentials, cross-project access and restricted raw artifacts. No automatic publication into a public repository or wiki.

### C. Swarm Wiki

- Reuse the reviewed `hermes-swarm-protocol` `swarm_capture`, `swarm_publish`, `swarm_verify` and `ops/build_wiki.py` concepts, preserving their role and project authorization. Pin an audited plugin commit at deployment, not a mutable checkout symlink.
- Keep raw immutable captures under a dedicated source path and maintenance rules under version control, outside the disposable generated `.swarm/wiki/` tree.
- Single-writer compiler reads a consistent read-only board snapshot and stable ByteRover/file inventories; records input fingerprints, checks source hashes/links, and atomically generates index, topics, decision history, evidence links, open issues and audit findings.
- The compiler cannot curate memory, dispatch workers, infer verification passes, alter task statuses, or reinterpret untrusted prose as instructions. Missing/conflicting/stale evidence must remain visible and fail the applicable acceptance check.
- Rebuild and `--check` must be deterministic; cleanup may remove only owned generated files, never canonical board, knowledge, raw captures or unrelated paths.

### D. Supervisor and worker boundary

- Preserve fresh-process workers and interchangeable `agent_command_template` runners. The originating-harness invariant from PR #68 remains binding; headless invocations must name a runner explicitly.
- Preserve independent validation replay, scope and diff gates, provenance, clean Git stops, and supervisor-only continuation. Kanban, ByteRover and wiki integrations are **adapters**, not a new agent framework.
- A worker may author code/evidence and recommend adjacent work; it cannot authorize its own next slice, mark itself independently verified, bypass a board lock, or route to a guessed identity.
- Consumer repositories own their board mappings, memory trees, wiki output, domain policy and queue/handoff artifacts. `repo-automation` ships reusable contracts/adapters and acceptance fixtures, not private live fleet state.

## Migration and compatibility

1. **Discovery only:** inventory board IDs, status vocabulary, configured profiles, memory roots, wiki deployments, consumer pins and current queue owners. Record any ambiguity as blocked.
2. **Read-only integration:** verify task projection + bounded memory recall + deterministic wiki compilation independently with fixture data. Do not mutate consumers or trigger a worker.
3. **Shadow compare:** compare projected slices and statuses against the live JSON queue; enumerate mismatches, unresolved IDs and dependencies. Do not claim dual canonical authority.
4. **Authorized cutover:** back up and pin state, verify idempotent one-writer status reconciliation, then enable board-driven intake per consumer, with rollback. The cutover is a separate explicitly reviewed PR/deployment.
5. **Canary and scale:** run a bounded scoped canary per project/profile using public Kanban/Swarm interfaces; independently verify memory and wiki, run negative permission/stale-state tests, and preserve existing history.
6. **Resume roadmap:** only after the top-layer acceptance gate passes, proceed to remaining supervisor hardening, consumer smoke proofs and effectiveness calibration as laid out in [the roadmap](../roadmap.md).

Existing CI/cleanroom benchmark experiments remain isolated: do not connect them to live boards, memory stores or wiki infrastructure. The pinned NVIDIA/`z-ai/glm-5.3` effectiveness experiment and phase-budget decisions are not changed by this specification.

## Acceptance gate (all required)

- Board: existing cards and history preserved; ID/revision-based projection is reproducible; no unauthorized worker launch or invalid lifecycle/status write; concurrency conflict fails closed.
- Memory: correct per-project root across profiles/worktrees; source-grounded read and persisted, reviewed write; explicit timeout/error handling; no cross-project leakage or credential exposure.
- Wiki: captured source hashes and provenance; deterministic independent rebuild and matching `--check`; unresolved contradictions remain visible; compiler has no write access to canonical stores.
- Integration: a complete task runs **only** through supervisor authorization, bounded context, independent live validation, immutable handoff, safe status commit and wiki update; failed or repeated operations are idempotent and traceable.
- Rollback: tested restoration of previous board mapping, profile configuration and plugin pin without deleting task history, source memory or originals.
- Release: CI checks, CodSpeed evidence for affected registered performance-critical paths, exact commit pins, change inventory and an operator-readable acceptance report.

## Source references

- [hermes-swarm-protocol README](https://github.com/raelldottin/hermes-swarm-protocol/blob/main/README.md)
- [Swarm Wiki design and ownership](https://github.com/raelldottin/hermes-swarm-protocol/blob/main/docs/swarm-wiki-design.md)
- [Swarm Wiki operations](https://github.com/raelldottin/hermes-swarm-protocol/blob/main/docs/swarm-wiki-usage.md)
- [Fleet rollout and canary gates](https://github.com/raelldottin/hermes-swarm-protocol/blob/main/docs/swarm-wiki-fleet-rollout.md)
- [ByteRover CLI](https://github.com/campfirein/byterover-cli) (reference implementation; repository archived September 2026, so assess support, exact revision and licensing before pinning)
