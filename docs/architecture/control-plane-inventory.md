# RA-CP-001: Control-plane inventory and license audit

Status: **read-only inventory, observed 2026-10-27 on the owner's workstation.** It is not a migration plan and authorizes no board, memory or wiki change.
Slice: RA-CP-001 in the [roadmap](../roadmap.md#ra-cp-001-control-plane-inventory-and-gap-audit); governing spec [SPEC-001](control-plane.md).
Next consumer: RA-CP-002, the Kanban projection contract.

## How this was gathered

Nothing was written to a board, a memory store, the wiki or `~/.hermes`.

- Board databases were opened with SQLite's `immutable=1` URI flag. With that flag SQLite takes no locks and never touches the `-wal` or `-shm` files, so the read cannot change the board. Each board's `-wal` file was 0 bytes at read time, so the read saw committed state. The reader is in [Appendix A](#appendix-a-the-reader).
- No `hermes kanban` subcommand other than `--help` was run. The CLI read path is not write-free; see [Read surfaces](#read-surfaces-a-projection-could-consume).
- Hermes behaviour is cited from source at the installed checkout, `~/.hermes/hermes-agent` @ `31bde0d74ba8`. Line numbers refer to that commit.
- Upstream facts were read with `gh api` at the pinned commits named below.
- Only ids, counts, column names and payload key names were read. No task titles, bodies, comments or run summaries went into this document. Chat-platform ids in `config.yaml` were seen while reading the `kanban:` block and left out.
- No credential, `.env` file or `auth/` directory was opened.

Every claim carries an evidence tag `[E#]`. [Appendix B](#appendix-b-evidence) lists the command or file for each tag.

## Component pins

| Component | Where it runs | Pin observed | Evidence |
| --- | --- | --- | --- |
| Hermes Agent (Kanban, dispatcher, gateway) | `~/.hermes/hermes-agent`, `hermes` v0.22.2+2408.g31bde0d (2026.9.24) | `31bde0d74ba88f1252fcc7ad4a2470f5028f870f`, detached. **33 commits not on `NousResearch/hermes-agent` `origin/main`** (`085f562a…`). It is a local fork build, not an upstream release. | [E1] |
| hermes-swarm-protocol (deployed plugin and wiki compiler) | `~/.hermes/plugins/swarm-protocol` → `~/.hermes/releases/swarm-protocol/f341d77d…/hermes_swarm_protocol` | Release `f341d77d5a1acdc52a9aefdf1571ac0e215eb558`, plugin `version: 1.2.0`. GitHub `main` is `834b2f6a…`, 2 commits ahead and docs-only. `wiki_compiler.py` is byte-identical between them (sha256 `43e8d38c…`). | [E14] |
| hermes-swarm-protocol local checkout | `~/Documents/Personal/hermes-swarm-protocol` | Branch `wiki/design-contract-phase0` @ `855a0194…` with uncommitted edits. **Not the deployed source.** | [E14] |
| Hindsight Hermes integration | Hermes plugin catalog entry, not installed | `vectorize-io/hindsight` @ `d952b6e655fccab4581ee0722b51c5f103873d12`, subdir `hindsight-integrations/hermes`, version `1.2.3` | [E15] |
| ByteRover CLI (`brv`) | `~/.brv-cli/bin/brv` | `byterover-cli/3.16.1` | [E13] |
| repo-automation | this repository | `main` @ `d1e788526990340f044a7f71b37d64c5e2a00d20` | [E18] |

## Hermes Kanban

### Boards

Each board is a directory `~/.hermes/kanban/boards/<slug>/` holding `board.json`, a per-board `kanban.db` (WAL mode), `kanban.db.dispatch.lock`, `kanban.db.init.lock`, `attachments/`, `logs/` and `workspaces/` [E2]. The keys of `board.json` are `archived, color, created_at, default_workdir, description, icon, name, project_id, slug` [E2].

| Board slug | `project_id` | Tasks by status | Runs (tasks with ≥1 run) | Max `task_events.id` |
| --- | --- | --- | --- | --- |
| `alepes` | `p_a8207c70` | archived 135, done 22, running 1, todo 10 | 350 (139) | 20666 |
| `home-lab` | — | archived 13, done 21 | 86 (34) | 9448 |
| `hsp` | — | archived 1, done 12 | 18 (13) | 456 |
| `life-achievements` | — | archived 118, blocked 1, done 8 | 709 (125) | 26030 |
| `opnory` | `p_c977883d` | archived 89, done 26, todo 8, triage 1 | 354 (115) | 20406 |
| `opnory-iac` | — | archived 31, done 1 | 50 (32) | 3199 |
| `tachikoma` | — | archived 67, done 14, todo 2, triage 1 | 303 (81) | 9144 |
| `tunory` | — | archived 43, done 20, todo 6, triage 2 | 163 (60) | 17886 |

Sources: [E2], [E3]. The counts are a snapshot to size a shadow diff, not live state.

Observed anomalies, recorded and not fixed:

- `~/.hermes/kanban/current` contains `tachikoma`. A `hermes kanban` command run without `--board` acts on `tachikoma`, so a projection must always name its board [E2].
- Zero-byte files `~/.hermes/kanban/kanban.db`, `boards/alepes.db`, `boards/life-achievements.db` and `boards/tachikoma.db` sit beside the real per-board databases. `boards/_archived/` holds four archived test boards [E2].
- Every board reports `PRAGMA user_version` 0. Schema changes are additive `ALTER TABLE` migrations (`_migrate_add_optional_columns`), not versioned [E3], [E5].
- The board `project_id` is set on only two boards. The task-level `tasks.project_id` and `tasks.tenant` columns are populated unevenly: `tenant` is set on 83 of 84 `tachikoma` tasks and on no `tunory` task [E3].

### Worker profiles

There are 43 profile directories under `~/.hermes/profiles/`, plus `.deleted/` [E4]. Most are named `<board-or-project>-<role>`:

| Prefix | Profiles |
| --- | --- |
| `alepes` | `alepes`, `-adversary`, `-builder`, `-invariants`, `-verifier` |
| `home-lab` | `home-lab`, `-adversary`, `-builder`, `-invariants`, `-verifier` |
| `hsp` | `-builder`, `-researcher`, `-security`, `-verifier` |
| `la` (board `life-achievements`) | `la`, `-architect`, `-builder`, `-database`, `-domain`, `-integrations`, `-moderation`, `-product-spec`, `-qa`, `-security`, `-verifier` |
| `opnory` | `opnory`, `-architect`, `-builder`, `-executor`, `-proof-verifier`, `-security`, `-verifier` |
| `tachikoma` | `tachikoma`, `-analyst`, `-architect`, `-protocol`, `-researcher`, `-safety`, `-verifier` |
| `tunory` | `tunory`, `-builder`, `-verifier` |
| other | `discord-second` |

Cross-checking `tasks.assignee` on every board against those directories [E4]:

- Three assignees have no profile directory: `default` (the root `HERMES_HOME`), `builder` and `tunory-auditor`.
- Seven profiles were never assigned a task: `discord-second`, `la-database`, `la-domain`, `la-integrations`, `la-moderation`, `la-qa` and `la-security`.
- Some assignees cross boards. For example, `opnory-builder` and `tachikoma-verifier` hold tasks on `alepes`. Profile name is therefore not a reliable proxy for board.

### Dispatcher owner

- `kanban.dispatch_in_gateway` defaults to `True` (`hermes_cli/config_defaults.py:1911`). The `kanban:` block of `~/.hermes/config.yaml` does not set it, so the default holds [E5].
- Effective owner: **the Hermes gateway process.** One was running at read time. `hermes kanban daemon` is deprecated in favour of the gateway (CLI help). `kanban_ops.py:197` warns that a second dispatcher means "two dispatchers racing for claims" [E5].
- Each tick takes a per-board `kanban.db.dispatch.lock` (`_dispatch_tick_lock`, `kanban_db_connect.py:161`) [E5].
- Dispatch limits set in `config.yaml`: `max_in_progress: 4`, `max_in_progress_per_profile: 1`, `max_spawn: 1`, `failure_limit: 2`, `auto_decompose: true` [E5].

repo-automation must not run its own dispatcher against these boards. SPEC-001 already forbids that.

### Read surfaces a projection could consume

| Surface | What it returns | Write behaviour | Fit for RA-CP-002 | Evidence |
| --- | --- | --- | --- | --- |
| `hermes kanban show <task> --json` | `task` (all columns), `latest_summary`, `parents`, `children`, `comments` (author, body, created_at), `events` (kind, payload, created_at, run_id), `runs` (id, profile, step_key, status, outcome, summary, error, metadata, worker_pid, started_at, ended_at) | **Not write-free.** It goes through `connect()`, which can `mkdir` the board directory, take the cross-process init lock, run `SCHEMA_SQL` and additive migrations once per process, and enable WAL. Only a "fenced" descendant path opens `mode=ro` (`kanban_db_connect.py:716-786`). | Good for spot-checking one task. It reads one task per call, so it gives no board-wide consistent snapshot. Its output carries confidential text (body, comments, summaries) that the projection must drop. | [E6] |
| `hermes kanban list` / `boards list --json` | Task and board listings | Same `connect()` path | Same caveats | [E6] |
| `kanban_list`, `kanban_show` (agent tools) | Structured tool results | — | **No.** They are registered only for dispatcher-spawned workers or profiles with the `kanban` toolset (`tools/kanban_tools.py:1-7`). They are a worker surface, not a supervisor one. | [E7] |
| Per-board SQLite `~/.hermes/kanban/boards/<slug>/kanban.db` opened `mode=ro` with `PRAGMA query_only=ON` inside one `BEGIN` | Every table, as one consistent snapshot | None. This is the existing precedent: `wiki_compiler.snapshot` (`hermes_swarm_protocol/wiki_compiler.py:114-135`) | **Best fit.** It is the only surface found that gives a board-wide consistent read with no init writes. It couples to an unversioned schema, so the adapter must check required columns as `wiki_compiler.REQUIRED` does (lines 24-30). | [E3], [E14] |
| `task_runs`, `task_events` (via SQLite, or `hermes kanban runs` / `log` / `tail`) | One row per attempt; append-only event log | — | Execution-identity source; see the next section | [E3] |
| `tasks.completion_contract` | `local-only`, `OWNER/REPO`, or an exact PR URL (`validate_contract`, `kanban_pr_acceptance.py:23`) | — | Source for PR evidence only. Observed across all boards: 568 `local-only`, 85 null, **0** repo or PR contracts. The feature exists but is unused. | [E9], [E3] |

Tables present on every populated board: `tasks, task_runs, task_events, task_links, task_comments, task_attachments, kanban_notify_subs, sqlite_sequence` [E3]. Task ids have the shape `t_xxxxxxxx` (8 hex characters) and are unique only within one board database [E3].

**Revision gap.** No table has a board revision or row-version column [E3]. RA-CP-002 needs expected-revision semantics. The nearest candidate is `max(task_events.id)` as a board watermark, but only if every task mutation appends an event. That is unverified, so record it as a gap for RA-CP-002 to test.

### Mapping Kanban onto `SliceRecord`

`SliceRecord` is defined at `automation/schemas/models.py:64-78` [E16].

| `SliceRecord` field | Kanban source | Notes |
| --- | --- | --- |
| `slice_id` | `tasks.id` | Board-local, so it must be namespaced with the board slug to be unique (for example `<slug>/<task_id>`). RA-CP-002 decides the format. |
| `title` | `tasks.title` | Confidential text. The projection may carry it locally but must never commit it to this repository. |
| `status` | `tasks.status` | Kanban has `triage, todo, scheduled, ready, running, blocked, review, done, archived` (`kanban_workflow.py`, `DEFAULT_STATUSES`) [E10]. `SliceStatus` has `queued, in_progress, blocked, deferred, done, failed`. Proposed mapping: `ready`→`queued`, `running`→`in_progress`, `blocked`→`blocked`, `done`→`done`, `archived`→excluded. **No clean source** for `triage`, `todo` (parents still open), `scheduled` (possibly `deferred`) or `review`. **Kanban has no `failed` task status:** `crashed`, `gave_up` and `timed_out` are run outcomes, not task states. |
| `priority` | `tasks.priority` | **Opposite order.** Kanban dispatches `ORDER BY priority DESC` (`kanban_db.py:1467`, `kanban_db_dispatch.py:2290`) [E11]. `select_next_slice` sorts ascending (`automation/supervisor/policy.py:313-326`) [E16]. Observed values are only `0` and `1` on `opnory` [E3]. |
| `domain` | none | **No source.** The board slug or `project_id` is a candidate, but neither is a domain. |
| `allowed_paths` (required, non-empty) | none | **No source.** Kanban records `workspace_kind`, `workspace_path` and `branch_name`, but no path allow-list. |
| `required_validations` | none | **No source.** `completion_contract` names remote required CI checks to read at completion. It does not name local commands for the supervisor to replay, so the two are not interchangeable. |
| `depends_on` | `task_links (parent_id, child_id)` | Present: 134 links on `opnory`, for example [E3]. Kanban treats parents as promotion gates (`dependency_wait` events). Links made by swarm/decompose may be provenance rather than hard dependencies. RA-CP-002 must tell them apart, as the roadmap already requires. |
| `max_files_changed` (≥1) | none | **No source.** |
| `notes` | none safe | `body` and `latest_summary` exist but are confidential free text. |
| `entry_condition`, `recommended_unblocker` | none | `tasks.block_kind` and `last_failure_error` exist but do not match these meanings. |
| `manual_proof`, `required_proof_level` | none | **No source.** |

**Conclusion for RA-CP-002.** Kanban can supply identity, title, status (lossy), priority (inverted) and dependencies. It cannot supply the engineering-policy fields `allowed_paths`, `required_validations`, `max_files_changed`, `domain` or the proof level. Those must come from a repo-automation-owned source keyed by Kanban task id, or from a structured block in the task that RA-CP-002 specifies. Either way repo-automation keeps policy authority, as SPEC-001 intends.

### Execution identity Kanban already records

These facts already exist. repo-automation should read them and not rebuild them:

- **One run row per attempt.** `task_runs` has one row per claim, with `profile, step_key, status, claim_lock, claim_expires, worker_pid, worker_started_at, last_heartbeat_at, started_at, ended_at, outcome, summary, metadata, error`. On `alepes`, 350 runs cover 139 tasks [E3]. `claimed` events carry `lock, run_id, retry_of, source, source_status, actor, expires` [E3].
- **`worker_bound`.** Its payload is `{claim_lock, pid, started_at}`. `bound_interpreter_gone` uses it to decide whether the bound interpreter for the current run is dead (`kanban_owner_recovery.py:36-46`). `worker_result` (`{claim_lock, exit_code, last_output, pid}`) counts for a run only under the same claim lock and pid (`managed_run_exit_code`, lines 49-70) [E8], [E3].
- **Unknown admissions are never re-executed.** `owner_admitted` (`{birth, db, pid, request_id, session_id}`) binds a run to an owner-side session admission. `owner_reclaim_paused` (`kanban_owner_recovery.py:9-33`, docstring: "Missing/ambiguous owner receipts never authorize another attempt") pauses reclaim in four cases: the admission row is missing, it is `queued` or `unknown`, it is terminal with outcome `interrupted`, or any read error occurs. It also pauses when the owner process has exited, or when its pid now belongs to a process with a different birth time [E8].
- **Reclaim and crash events.** The log holds `crashed` (`exit_code, exit_kind, pid, retry_status, terminal_provider, worker_output`), `reclaimed` (`reason, stale_lock, prev_lock, prev_pid, heartbeat_stale, manual, terminated, termination_attempted, sigkill, …`), `timed_out`, `gave_up`, `protocol_violation`, `spawn_failed`, `respawn_guarded` and `terminal_worker_reaped` [E3].
- **Lifecycle attribution.** An unidentified actor is recorded as `"unknown"`, never as a guessed name (`kanban_db_respawn.py:_attribution`) [E8].
- **Resume guard.** A Kanban worker transcript cannot resume as a write-capable ordinary session (`kanban_resume_guard.py`). This blocks a second writer in the same workspace [E8].
- **PR completion contracts.** `collect_acceptance` reads the PR's `headRefOid`, the branch-protection and ruleset required checks, and classifies each check against the exact head sha. It runs `gh` as the assignee profile's login, not the ambient one (`kanban_pr_acceptance.py:107-160`). `prepare_acceptance` binds a published PR once, so a retry cannot swap in a green sibling PR (`kanban_pr_acceptance_store.py`) [E9].

Caveat: `task_runs.metadata` and the `completed` event payload are worker-authored free-form JSON with no fixed key set [E3]. A projection must not treat them as a contract.

## Memory: ByteRover today, Hindsight not deployed

### Existing ByteRover `.brv/context-tree` roots

Found with `find … -name context-tree -path '*/.brv/*'` at depth ≤6 [E12]. Paths under `/worktrees/` or `/workspaces/` were left out.

| Root | Markdown files | Notes |
| --- | --- | --- |
| `~/.hermes/byterover/.brv/context-tree` | 2308 | Global tree; has `_index.md`, `_manifest.json`, `_archived/` |
| `~/.hermes/byterover-projects/alepes/.brv/context-tree` | 668 | `_index.md`, `_manifest.json` |
| `~/.hermes/byterover-projects/life-achievements/.brv/context-tree` | 893 | same |
| `~/.hermes/byterover-projects/tunory/.brv/context-tree` | 493 | same |
| `~/.hermes/byterover-projects/opnory/.brv/context-tree` | 334 | same |
| `~/.hermes/byterover-projects/tachikoma/.brv/context-tree` | 118 | same |
| `~/.hermes/byterover-projects/hermes-swarm-protocol/.brv/context-tree` | 69 | same |
| `~/.hermes/byterover-projects/home-lab/.brv/context-tree` | 0 | contains only `.git/` and `.gitignore` |
| `~/Documents/Personal/life_achievements/.brv/context-tree` | 9 | repo-local |
| `~/Documents/Personal/{Gymphant,alepes,opnory/opnory}/.brv/context-tree` | 0 | empty repo-local trees |

There are no `byterover-projects` trees for `hsp` or `opnory-iac`, and no tree in this repository [E12]. The active Hermes memory provider is `memory.provider: byterover` (`~/.hermes/config.yaml`, `memory:` block) [E13].

### Hindsight: no bank or tenant identities exist yet

- No Hindsight state was found: `~/.hindsight` and `~/.hermes/hindsight` are absent [E13].
- The plugin is listed in `plugin-catalog/hindsight.yaml` but not installed under `plugins/memory/` [E15].
- No `bank_id` exists to inventory. Bank and tenant identities are future RA-CP-003 decisions.

### Can the Hindsight Hermes plugin run fully on local models?

**Per upstream configuration, yes, but none of the defaults are local, and this was not run.** Evidence [E15], all at `vectorize-io/hindsight@d952b6e6`:

- **The default setups need a hosted-LLM key.**
    - The README Docker quick start exports `OPENAI_API_KEY` and passes it as `HINDSIGHT_API_LLM_API_KEY` (`README.md:71-77`).
    - The external-Postgres compose fails to start unless `HINDSIGHT_API_LLM_API_KEY` is set (`docker/docker-compose/external-pg/docker-compose.yaml:42`, `${…:?Please set…}`).
    - `.env.example` defaults to `HINDSIGHT_API_LLM_PROVIDER=openai` and `HINDSIGHT_API_LLM_MODEL=gpt-4o-mini` (lines 6-8).
    - Strictly, the required variable is `HINDSIGHT_API_LLM_API_KEY`; `OPENAI_API_KEY` is only the README's way of filling it.
    - Ambiguity: the README's `cd docker/docker-compose && docker compose up` (lines 87-90) names a directory that holds no compose file at this commit. Each variant lives in its own subdirectory.
- **A fully local variant exists.** `docker/docker-compose/local-llm/` runs a `llama.cpp` server with `bartowski/google_gemma-4-E2B-it-GGUF` (`Q4_K_M`) and points Hindsight's `openai` provider at it with `HINDSIGHT_API_LLM_API_KEY: not-needed`. Its README says it is "fully offline, with no external API key required".
- **Embeddings and reranking default to local models.** Embeddings default to `local` / `BAAI/bge-small-en-v1.5` (`.env.example:382-386`). The reranker defaults to `local` / `cross-encoder/ms-marco-MiniLM-L-6-v2` (lines 557, 574).
- **Hermes plugin modes.**
    - The plugin offers `cloud` (the default), `local_embedded` and `local_external` (integration `README.md:214`).
    - `local_external` can point at the local-llm stack above.
    - `local_embedded` "Requires an LLM API key" (line 177), but its `llm_provider` list includes `ollama`, `lmstudio` and `openai_compatible` (line 320). Whether those run without a key was not verified.
- **Egress at first start, so "offline" needs pre-staging.**
    - Images are tagged `:latest`.
    - `llama.cpp` downloads its model from Hugging Face (`LLAMA_ARG_HF_REPO`).
    - `local_embedded` may fetch `hindsight-api` with `uvx`.
    - `hermes memory setup` fetches bank templates from `raw.githubusercontent.com` (`vectorize-io/hindsight` `main`) (catalog disclosure).
    - Embedding and reranker weights are named by Hugging Face id, so presumably they download on first use unless cached. Not verified.
    - All of this is pre-staging and pinning work for RA-CP-003 and P0.5. It does not block local operation.

## Swarm Wiki compiler inputs

From `ops/build_wiki.py:36-48` and `hermes_swarm_protocol/wiki_compiler.py` at deployed release `f341d77d` / `main` `834b2f6a` [E14]:

| Input | Default path | How it is read |
| --- | --- | --- |
| Kanban board DB | `~/.hermes/kanban/boards/<board>/kanban.db`. Deliberately ignores a worker's `HERMES_HOME`. | `mode=ro`, `PRAGMA query_only=ON`, one transaction. Required columns are checked for `tasks, task_links, task_comments, task_events, task_runs` (`REQUIRED`, lines 24-30). |
| ByteRover knowledge tree | `~/.hermes/byterover-projects/<project>/.brv/context-tree` | `stable_inventory(tree, markdown=True)`. The tree is re-read until two passes match, otherwise it fails "input changed during snapshot". YAML frontmatter is parsed with `ruamel.yaml` (`node`). |
| Raw artifacts | `<workspace>/.swarm/raw` | `stable_inventory(…, ignore_staging=True)` |
| Trusted rules | `<workspace>/.swarm/SWARM_WIKI.md` | Read twice; must match and be non-empty |
| Hermes statuses | `--hermes-source` (default `~/.hermes/hermes-agent`) | Imports `hermes_cli.kanban_db.VALID_STATUSES` |
| Repository HEAD | `--repo` (default workspace) | `git rev-parse HEAD` |
| Output | `<workspace>/.swarm/wiki` | `publish(…, check)`; `--check` compares without writing |

The compiler reads no Hindsight input. This confirms the RA-CP-004 premise.

## License register

SPDX identifiers come from the GitHub license API unless noted [E17], [E19].

| Component | Role | SPDX | Risk |
| --- | --- | --- | --- |
| `raelldottin/repo-automation` | supervisor (this repo) | **none** (no root `LICENSE`, GitHub `license: null`) | **Owner-approved license decision needed.** Agents do not choose one. |
| `raelldottin/hermes-swarm-protocol` | wiki compiler and swarm plugin | **none** (no `LICENSE` in the `main` tree, GitHub `license: null`) | **Owner-approved license decision needed.** The deployed plugin is therefore unlicensed code. |
| `NousResearch/hermes-agent` | Kanban, dispatcher, gateway | MIT (local `LICENSE`: "Copyright (c) 2025 Nous Research") | Low. The deployed build is a 33-commit local fork; its added commits' provenance belongs in a P0.5 SBOM. |
| `vectorize-io/hindsight` (incl. `hindsight-integrations/hermes`) | target memory | MIT | Low |
| `campfirein/byterover-cli` (`brv` 3.16.1) | current memory | **Elastic-2.0** (GitHub reports `NOASSERTION`; the `LICENSE` file is "Elastic License 2.0 (ELv2)") | **Not OSI-approved.** It is a dependency of today's memory and of the wiki's input format, so it cannot be part of a fully open-source production profile. Historic `.brv` Markdown can still be read as data. |
| `ggml-org/llama.cpp` | local inference in Hindsight's `local-llm` compose | MIT | Low |
| `pgvector/pgvector` | vector store in Hindsight's Postgres composes | PostgreSQL (GitHub reports `NOASSERTION`; `LICENSE` is the PostgreSQL license text) | Low |
| `BAAI/bge-small-en-v1.5` | default local embeddings | MIT (Hugging Face tag), rev `5c38ec7c…` | Tags are self-declared; check the model card before P0.5 |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | default local reranker | Apache-2.0 (Hugging Face tag), rev `233902d2…` | Same caveat. The MS MARCO training-data terms are not reviewed here. |
| `bartowski/google_gemma-4-E2B-it-GGUF` | local LLM in the `local-llm` compose | Apache-2.0 (Hugging Face tag), rev `81012ba3…` | A third-party quantization; confirm the base model's license against Google's model card |
| `jessfraz/agent` | compared coordinator (essay), not adopted | MIT, `main` @ `cee60dc6…` | n/a |
| `earendil-works/pi` (Pi Durable) | substrate of `jessfraz/agent`, not adopted | MIT, @ `85c2ee3e…` | n/a |
| Superlogical Rex | session layer of `jessfraz/agent`, not adopted | **no source license published** | See below |

**Rex status** [E20]:

- **Self-hostable.** Superlogical's docs describe a Linux/FreeBSD server: a shared server, a per-account login server and server modes.
- **Testers only for now.** The docs say "Install Linux is open to testers only for now", and the installer needs a `REX_PACKAGE_TOKEN`.
- **Binary only.** The Nix package "packages the release binary rather than building Rex from source".
- **No public source.** The GitHub user `superlogical` has 0 public repositories, and no source license was found.
- **No license checks.** The privacy page states there are "no account or license checks".
- **Price unconfirmed.** This inventory found no Superlogical statement on price, so "free" is unconfirmed here.
- **Not open source** under the roadmap's [open-source release gate](../roadmap.md#open-source-release-gate). That does not matter while Rex stays outside this stack.

## Gaps for RA-CP-002 and later

1. **Policy fields have no Kanban source.** `allowed_paths`, `required_validations`, `max_files_changed`, `domain` and the proof level need a repo-automation-owned source keyed by Kanban task id.
2. **Status is lossy, and there is no `failed` state.** Priority order is inverted.
3. **No board revision column.** Expected-revision semantics need a tested watermark.
4. **The CLI read path writes on init.** Use a `mode=ro` / `query_only` snapshot as the wiki compiler does, or confirm with Hermes upstream that a supported read-only snapshot API exists.
5. **The schema is unversioned** (`user_version` 0). Check required columns like the compiler's `REQUIRED`, and pin the Hermes commit.
6. **Link semantics.** Dependency links and provenance links share one `task_links` table.
7. **Not delivered here: the machine-checkable inventory template.** The roadmap's RA-CP-001 acceptance asks for one. This document is the reviewed prose register. A schema-validated template can follow once RA-CP-002 fixes which fields it must carry.
8. **License decisions** for `repo-automation` and `hermes-swarm-protocol`, and the ByteRover Elastic-2.0 dependency, block the P0.5 open-source claim.
9. **The Hermes fork drift** (33 local commits) needs provenance before P0.5 pins it.

## Appendix A: the reader

```python
# Read-only: immutable=1 means no locks and no -wal/-shm access. Prints shapes and counts only.
import json, os, sqlite3

root = os.path.expanduser("~/.hermes/kanban/boards")
for slug in sorted(os.listdir(root)):
    db = os.path.join(root, slug, "kanban.db")
    if not os.path.isfile(db) or os.path.getsize(db) == 0:
        continue
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    print(
        slug,
        {t: con.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables},
        dict(con.execute("select status, count(*) from tasks group by status")),
        con.execute("select max(id) from task_events").fetchone()[0],
    )
```

Payload key sets were gathered the same way, with `json.loads(payload).keys()` per event `kind`. Values were never printed.

## Appendix B: evidence

| Tag | Command or file |
| --- | --- |
| E1 | `hermes --version`; `git -C ~/.hermes/hermes-agent rev-parse HEAD`; `git -C ~/.hermes/hermes-agent rev-list --count origin/main..HEAD` → 33; `git … merge-base --is-ancestor HEAD origin/main` → false |
| E2 | `ls -la ~/.hermes/kanban ~/.hermes/kanban/boards ~/.hermes/kanban/boards/hsp`; `cat ~/.hermes/kanban/current`; `board.json` key names and `slug` / `project_id` per board |
| E3 | [Appendix A](#appendix-a-the-reader) reader plus `pragma table_info`, `pragma user_version`, per-kind `task_events` counts, `task_runs.status` / `outcome` counts, payload key sets, `completion_contract` shape counts, optional-column population counts |
| E4 | `ls ~/.hermes/profiles`; distinct `tasks.assignee` across boards compared with the profile directory names |
| E5 | `hermes_cli/config_defaults.py:1911`; `hermes_cli/kanban.py:102-126`; `hermes_cli/kanban_ops.py:164-197`; `hermes_cli/kanban_db_connect.py:161`; `~/.hermes/config.yaml` `kanban:` block keys; `pgrep -fl gateway`; `hermes kanban --help` |
| E6 | `hermes_cli/kanban_db_connect.py:716-786` (`connect`); `hermes_cli/kanban.py:481-507` (`_cmd_show`); `hermes_cli/kanban_output.py:23-26` (`_SHOW_RUN_FIELDS`); `hermes_cli/kanban_parser.py:98-101` |
| E7 | `tools/kanban_tools.py:1-7`, `84-99`; tool names from `tools/kanban_tools_schemas.py` |
| E8 | `hermes_cli/kanban_owner_recovery.py:1-70`; `hermes_cli/kanban_db_respawn.py` (`_attribution`); `hermes_cli/kanban_resume_guard.py:1-20` |
| E9 | `hermes_cli/kanban_pr_acceptance.py:1-160`; `hermes_cli/kanban_pr_acceptance_store.py:1-40` |
| E10 | `hermes_cli/kanban_workflow.py` (`DEFAULT_WORKFLOW` columns, `DEFAULT_STATUSES`); `hermes_cli/kanban_db.py:32` |
| E11 | `hermes_cli/kanban_db.py:1467`; `hermes_cli/kanban_db_dispatch.py:2290` |
| E12 | `find ~/Documents ~/.hermes/byterover-projects ~/.hermes/byterover ~/.hermes/kanban/workspaces -maxdepth 6 -type d -name context-tree -path '*/.brv/*'`; `find <root> -name '*.md' \| wc -l` |
| E13 | `which brv; brv --version`; `~/.hermes/config.yaml` `memory:` block; `ls ~/.hindsight ~/.hermes/hindsight` (absent) |
| E14 | `gh api repos/raelldottin/hermes-swarm-protocol/git/trees/834b2f6a…?recursive=1`; `…/contents/ops/build_wiki.py`, `…/hermes_swarm_protocol/wiki_compiler.py`, `…/plugin.yaml` at `834b2f6a`; `ls -la ~/.hermes/plugins/swarm-protocol`; `shasum -a 256` of both compilers; `gh api …/compare/f341d77d…834b2f6a`; `git -C ~/Documents/Personal/hermes-swarm-protocol rev-parse HEAD; git status --short` |
| E15 | `~/.hermes/hermes-agent/plugin-catalog/hindsight.yaml`; `git ls-tree origin/main plugins/memory/`; `gh api repos/vectorize-io/hindsight/contents/<path>?ref=d952b6e6…` for `README.md`, `.env.example`, `docker/docker-compose/local-llm/{docker-compose.yaml,README.md}`, `docker/docker-compose/external-pg/docker-compose.yaml`, `hindsight-integrations/hermes/README.md`; tree listing at that sha |
| E16 | `automation/schemas/models.py:20-78`; `automation/supervisor/policy.py:313-326`; `automation/README.md:23,87` |
| E17 | `gh api repos/<owner>/<repo> --jq .license.spdx_id` for each GitHub component; `gh api repos/campfirein/byterover-cli/contents/LICENSE`; `gh api repos/pgvector/pgvector/contents/LICENSE`; `head -3 ~/.hermes/hermes-agent/LICENSE`; `gh repo view raelldottin/{repo-automation,hermes-swarm-protocol} --json licenseInfo` |
| E18 | `git ls-remote https://github.com/raelldottin/repo-automation refs/heads/main` |
| E19 | `https://huggingface.co/api/models/<id>` (`license:` tags and `sha`) for the four models listed |
| E20 | `https://www.superlogical.com/rex/docs/linux/installation`, `…/linux/server-modes`, `…/reference/privacy`; `gh api users/superlogical --jq .public_repos` → 0; `jessfraz/agent` `README.md:5-7` |
