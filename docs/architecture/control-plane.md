# SPEC-001: Kanban, external memory, and Swarm Wiki control plane

Status: **proposed architecture; not a claim of runtime implementation**  
Priority: **P0 for the control-plane and supervisor roadmap.** Benchmark/effectiveness work is exempt from this ordering and continues in parallel under explicit owner authorization; see the [roadmap](../roadmap.md#ordering-decision).  
Authority: This document defines the target deployment architecture. Existing supervisor queue behavior remains operative until a reviewed, tested cutover.

## Decision

The top-level project interface MUST provide three cooperating surfaces:

1. **Kanban board** — canonical human/agent-visible work state: tasks, assignment, priority, dependencies, progress, attempts, review and final disposition. The target implementation is **Hermes Kanban**; this distribution does not ship or require an installed board today. Do not build a second scheduler or board database merely to satisfy the UI requirement.
2. **External memory** — self-hosted, durable and project-isolated agent knowledge beyond chat/process lifetimes. Use **[Hindsight](https://github.com/vectorize-io/hindsight)** (MIT) as the canonical target backend. Its retain, recall and reflect APIs operate on project-scoped memory banks. Memory is not the queue, an evidence registry or execution authority.
3. **Swarm Wiki** — navigable, versioned, rebuildable synthesis of decisions, claims, evidence, open questions, changes and history. Prefer the existing [hermes-swarm-protocol](https://github.com/raelldottin/hermes-swarm-protocol) compiler and published records rather than another wiki database. Wiki pages are generated projections, not authoritative work state or independent proof.

These are three **logical responsibilities**, not necessarily three servers. They sit above the existing deterministic repo-automation supervisor, which remains the sole authority for accepting runnable slices, enforcing scope/budgets, interpreting handoffs and deciding continuation.

```text
                    TOP LAYER: operator / agent navigation

  +----------------------+  +----------------------+  +----------------------+
  | Kanban board         |  | External memory:     |  | Swarm Wiki           |
  | canonical work state |  | Hindsight bank       |  | generated, read-only |
  +----------+-----------+  +----------+-----------+  +----------^-----------+
             |                         |                         |
     read-only slice         bounded, source-        board + memory snapshots,
     projection              attributed recall       immutable evidence
             |                         |                         |
  +----------v-------------------------v----------+              |
  | repo-automation deterministic supervisor      |              |
  | sole authority: eligibility, policy,          |              |
  | context, gates, continuation                  |              |
  +-----------------------+-----------------------+              |
                          |                                      |
     fresh bounded runner (Hermes/Claude/Codex)                  |
                          |                                      |
       JSON handoff + immutable live evidence -------------------+
```

The diagram depicts navigation and data flow, **not** permission for memory or the Wiki to schedule tasks. Kanban feeds the supervisor directly through the deterministic read-only slice projection defined in contract A; memory supplies bounded context only, and nothing flows from the Wiki back into the supervisor or the board. Canonical board records and immutable original evidence feed the wiki compiler. A reviewed Hindsight export/snapshot adapter supplies knowledge references; the compiler must not use unsourced model recollection as canonical proof. There is no required live LLM call to render the wiki.

## Ownership and precedence

| Concern | Canonical owner | Not allowed |
| --- | --- | --- |
| Task identity, status, assignee, dependencies, lifecycle, dispatch | Hermes Kanban per-project board | Wiki/memory/worker privately changing statuses, inferred statuses, second scheduler |
| Curated requirements, design knowledge, prior solutions, uncertainties | Hindsight per-project memory bank | Treating recalled prose as a verified test or executable instruction |
| Claim/evidence/decision publications and cross-agent request/verify/route | Swarm protocol records on canonical Kanban comments and immutable captures | Unverified conclusions or a parallel coordination database |
| Human-readable synthesis and searchable wiki pages | One deterministic Swarm Wiki compiler | Workers directly editing generated output or compiler mutating upstream stores |
| Slice eligibility, allowed paths, diff budgets, validation tiers, run/handoff verification, next-run decision | repo-automation supervisor | Workers self-chaining the next slice (supervisor-controlled sub-agents are proposed, not implemented), retrospective invented evidence, model-driven overriding of gates |
| Source code, tests, commit provenance | Consumer repository at pinned Git identity | Storing credentials or live source-of-truth artifacts in the generated wiki |

Precedence during an incident: **original live evidence and committed source + canonical Kanban records > reviewed Hindsight knowledge > generated wiki text**. A wiki discrepancy is a lint finding, never an automatic board mutation. A pass requires independent scope-matched non-memory live evidence. Keep `swarm_verify`'s inspect → evidence receipt → commit discipline and deterministic `swarm_route`; unresolved destinations fail closed.

## Required contracts

### A. Kanban and the existing slice queue

- Inventory existing Hermes boards, profile identities, status enums, project mappings, source cards and dependency links through supported read paths before making changes. Preserve every existing board and history.
- Each executable slice MUST have a stable canonical `board_id`, `task_id` and source/provenance reference in the proposed adapter contract. Preserve task relationships separately from execution dependencies.
- Existing `automation/queue/slices.json` and JSON schemas remain supported during migration. **Today they are the implementation's execution authority**; do not falsely mark Kanban as already wired into `run_next.py`.
- Target cutover: Kanban becomes the authoritative task state, and a deterministic, validated **read-only projection** produces supervisor-eligible slices. Existing supervisor selection and stop rules still decide execution; no board status implies authorization to run a slice.
- All transitions back to Kanban go through a separately authorized, idempotent adapter after handoff validation. Reject stale version/revision, invalid status, foreign board, ambiguous task, lost source reference and concurrent edit rather than guessing. Never update raw Kanban SQLite as a shortcut.
- In the transition, name one status writer explicitly and reconcile before switching ownership. Do not dual-write two active queues or fabricate historical board state.

### B. Hindsight memory

- Use a **self-hosted, pinned** version of [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight) (MIT). Its canonical memory interface is **retain / recall / reflect** over uniquely mapped project `bank_id` values, via an explicit server URL and supported Python/REST/MCP client. Do not pretend its banks are Hindsight-local Markdown trees or drop-in replacements for the prior `.brv/context-tree`.
- An operator-reviewed project-to-bank map binds canonical Kanban `board_id`, consumer repository identity, server endpoint, project `bank_id`, data-retention rules, and authorized profile/adapter identities. Use explicit configuration, never implicit cwd, a shared default bank, or environment-based guessing. A bank ID by itself is **not authentication**: restrict service reachability, credentials and authorized project access at the API/network boundary; avoid exposing unrestricted cross-bank MCP tools to worker profiles.
- Context generation retrieves **bounded, relevant, source-attributed** Hindsight recall results, with a recorded bank, task scope, collection/source IDs, time, underlying original-evidence references and stable digests where obtainable. Persist signed-off source/evidence linkage in canonical Kanban/Swarm records; never interpret generated reflection as direct proof or let a memory response make an execution decision.
- Retain/reflect are optional, explicitly authorized actions; they MUST NOT automatically ingest secrets, other projects' content or raw worker transcripts. Review and verify durable read-back of stored facts and changes; API success alone does not establish correct extraction, consolidation or provenance. Treat absent/conflicting/stale claims as uncertainty.
- Implement explicit timeout, authentication/network failure, empty recall, source deletion and changed-revision handling; bound retrieval volume and avoid injecting untrusted memory text as instructions into prompts. Preserve the memory bank and backup/snapshot across worker/container restarts.
- For the **fully open-source, self-hosted deployment profile**, run Hindsight and its database locally; configure an independently license-vetted local LLM (e.g. supported Ollama/LM Studio/OpenAI-compatible endpoint), local embedding provider and local reranker. Audit model **weights and inference terms** separately from software license. Never depend on a cloud default, paid API key, external embedding, remote reranker or telemetry. Pin image/build and model artifacts; verify outbound behavior with an egress-denied integration test after prefetching model assets. Bare-metal llama.cpp is an alternative where supported; do not assume the standard Docker image bundles it.
- Retain prior Hindsight-independent original evidence as the authority; memory and its generated views are fallible contextual aids. No model swap, provider fallback or memory backend change in the separate benchmark calibration without its own approval.

### C. Swarm Wiki

- Reuse the reviewed `hermes-swarm-protocol` `swarm_capture`, `swarm_publish`, `swarm_verify` and `ops/build_wiki.py` concepts, preserving their role and project authorization. Pin an audited plugin commit at deployment, not a mutable checkout symlink.
- Keep raw immutable captures under a dedicated source path and maintenance rules under version control, outside the disposable generated `.swarm/wiki/` tree.
- The **existing** hermes-swarm-protocol wiki compiler currently reads ByteRover-specific `.brv/context-tree` Markdown/frontmatter, metadata and hashes. It is **not** Hindsight-compatible today. Before rollout, a separately reviewed upstream migration PR MUST introduce a typed Hindsight snapshot/export adapter, mapped evidence IDs, deterministic source/content hashes and complete lineage. Do not synthesize pseudo-`.brv` pages and call them authoritative. Maintain backward-read support for existing ByteRover history until archived/verified and cutover is approved.
- The single-writer compiler reads a consistent read-only board snapshot and an explicitly captured, stable Hindsight snapshot with immutable evidence; records input fingerprints, checks hashes/links, and atomically generates index, topics, decision history, evidence links, open issues and audit findings. The wiki is a distinct projection, **not Hindsight's own knowledge-page UI**.
- The compiler cannot curate memory, dispatch workers, infer verification passes, alter task statuses, or reinterpret untrusted prose as instructions. Missing/conflicting/stale evidence must remain visible and fail the applicable acceptance check.
- Rebuild and `--check` must be deterministic; cleanup may remove only owned generated files, never canonical board, knowledge, raw captures or unrelated paths.

### D. Supervisor and worker boundary

- Preserve fresh-process workers and interchangeable `agent_command_template` runners. The originating-harness invariant from raelldottin/repo-automation#68 remains binding; headless invocations must name a runner explicitly.
- Preserve independent validation replay, scope and diff gates, provenance, clean Git stops, and supervisor-only continuation. Kanban, Hindsight and wiki integrations are **adapters**, not a new agent framework.
- Delegation has two kinds. *Agent-native delegation* is available today: a runner such as Claude Code may launch its own sub-agents inside one slice run, and the supervisor neither manages nor sees them. *Supervisor-controlled delegation* (the supervisor assigning sub-agents with explicit permissions, budgets, dependencies and verification) is proposed by this specification and not implemented. Neither kind may self-chain: no worker or sub-agent starts the next slice.
- A worker may author code/evidence and recommend adjacent work; it cannot authorize its own next slice, mark itself independently verified, bypass a board lock, or route to a guessed identity.
- Consumer repositories own their board mappings, bank isolation and snapshots, wiki output, domain policy and queue/handoff artifacts. `repo-automation` ships reusable contracts/adapters and acceptance fixtures, not private live fleet state.

## Open-source and self-hosted requirement

**Goal: all required production/runtime components must be auditable and redistributable under an OSI-approved open-source software license, with locally runnable model inference.** Free services, openly available weights and open-source client libraries alone do not establish this.

- Hindsight core is MIT licensed; Hermes Agent is MIT licensed. Verify the licenses and bundled component obligations of **all** pinned distributions, images, model runtimes, embedding models, rerankers, database extensions, UI assets and transitive dependencies.
- **As of 2026-10-11, `repo-automation` and `hermes-swarm-protocol` did not expose a root LICENSE file or GitHub-detected license.** Re-check before relying on this statement; it records a point-in-time observation, not a permanent property. Public source does not by itself grant open-source rights. Choosing a license is an **owner decision**; once the owner approves an SPDX/OSI license (MIT is compatible with the upstream Hindsight core) after attribution/contributor review, add it to each original project. `repo-automation` already records adapted third-party material in [`THIRD-PARTY-NOTICES.md`](https://github.com/raelldottin/repo-automation/blob/main/THIRD-PARTY-NOTICES.md); extend that file rather than starting a new one. Do not assert full-stack OSS completion until audited.
- The production reference deployment uses self-hosted Hindsight, an open-source database/container/runtime, verified open-license model weights and local inference/embeddings/reranking. No mandatory Hindsight Cloud, proprietary IDE, commercial agent model, managed inference endpoint, gated registry, paid proprietary telemetry or Docker Desktop-only dependency. Hardware/energy/operations may still cost money.
- Claude Code and proprietary hosted models are permitted as **optional external development tools**, never as mandatory components of the **fully open-source runtime profile**. The independently administered NVIDIA `z-ai/glm-5.3` effectiveness benchmark is outside that production profile; it is exempt from the P0 ordering and continues in parallel under explicit owner authorization, with any model, provider or budget change still needing its own approval. A source release and a fully self-hosted runtime are two different claims; verify both separately.
- Model licensing, export/import rights, data provenance, source-code license files, SBOM, dependency policy and egress tests are release gates, not assumptions inferred from a vendor pricing page.

## Migration and compatibility

1. **Discovery only:** inventory board IDs, status vocabulary, configured profiles, old ByteRover trees where in use, target Hindsight banks, wiki compiler's source format, consumer pins, model/OSS licenses and current queue owners. Record any ambiguity as blocked.
2. **Read-only integration:** verify task projection, source-attributed Hindsight recall from a test bank and a *modified* wiki compiler against recorded Hindsight snapshots. Demonstrate local OSS model/embeddings/reranker with remote inference blocked. Do not mutate live consumers or trigger a worker.
3. **Shadow compare:** compare projected slices against the live JSON queue; additionally snapshot prior ByteRover records, migrate test knowledge through the reviewed Hindsight retain API, audit counts, lineage, loss, conflicting timestamps and independently recall sampled evidence. Preserve ByteRover originals as read-only history during transition; no implicit one-to-one equivalence between knowledge nodes and memory records.
4. **Authorized cutover:** back up and pin state, approve upstream Swarm Wiki compatibility PR and source migration; verify idempotent one-writer status reconciliation before enabling board-driven intake and Hindsight as canonical memory per consumer. Roll back all three adapters independently and preserve historical data. The cutover is a separately reviewed PR/deployment.
5. **Canary and scale:** run a bounded scoped canary per project/profile using public Kanban/Swarm/Hindsight interfaces; independently verify retain/recall/reflect behavior and wiki snapshot export, forbidden cross-bank access, lossless backup/recovery, network outages and zero external inference. Preserve old history.
6. **Resume roadmap:** only after the top-layer acceptance gate passes, proceed to remaining supervisor hardening and consumer smoke proofs as laid out in [the roadmap](../roadmap.md). Effectiveness calibration is not gated on this step; it continues in parallel under explicit owner authorization.

Existing CI/cleanroom benchmark experiments remain isolated: do not connect them to live boards, memory stores or wiki infrastructure. The pinned NVIDIA/`z-ai/glm-5.3` effectiveness experiment and phase-budget decisions are not changed by this specification, and the experiment is not paused by it.

## Acceptance gate (all required)

- Board: existing cards and history preserved; ID/revision-based projection is reproducible; no unauthorized worker launch or invalid lifecycle/status write; concurrency conflict fails closed.
- Memory: correct per-project bank and server endpoint across profiles/worktrees; independent retain/recall/reflect evidence, preserved original provenance and persisted/backup-verified writes; explicit timeout/error handling; no cross-project leakage, unauthorized multi-bank visibility or credential exposure.
- Wiki: verified upstream compatibility with **Hindsight snapshots rather than ByteRover Markdown**; captured source hashes and provenance; deterministic independent rebuild and matching `--check`; unresolved contradictions remain visible; compiler has no write access to canonical stores.
- Integration: a complete task runs **only** through supervisor authorization, bounded context, independent live validation, immutable handoff, safe status commit and wiki update; failed or repeated operations are idempotent and traceable.
- Rollback: tested restoration of previous board mapping, profile configuration and plugin pin without deleting task history, source memory or originals.
- Release: CI checks, CodSpeed evidence for affected registered performance-critical paths, exact commit/model pins, **OSI/SPDX license and third-party notice audit**, local egress-denied deployment proof, change inventory and an operator-readable acceptance report.

## Source references

- [hermes-swarm-protocol README](https://github.com/raelldottin/hermes-swarm-protocol/blob/main/README.md)
- [Swarm Wiki design and ownership, including the current ByteRover coupling (migration source)](https://github.com/raelldottin/hermes-swarm-protocol/blob/main/docs/swarm-wiki-design.md)
- [Swarm Wiki operations](https://github.com/raelldottin/hermes-swarm-protocol/blob/main/docs/swarm-wiki-usage.md)
- [Fleet rollout and canary gates](https://github.com/raelldottin/hermes-swarm-protocol/blob/main/docs/swarm-wiki-fleet-rollout.md)
- [Hindsight repository and MIT license](https://github.com/vectorize-io/hindsight)
- [Hindsight API configuration (local Ollama, local embeddings and reranker)](https://github.com/vectorize-io/hindsight/blob/main/skills/hindsight-docs/references/developer/configuration.md)
- [Hindsight local MCP and bank mapping](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs-integrations/local-mcp.md)
