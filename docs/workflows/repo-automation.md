# Reusable Repo Automation

This workflow defines how the reusable slice harness is owned and how a repository consumes it.

## Ownership

`repo-automation` is the canonical source for all reusable automation. Every consumer — Owlory included — vendors a pinned snapshot of it.

```text
repo-automation (canonical) -> consumer's vendored snapshot, at one pinned commit
```

This is the explicit ownership flip that the earlier revision of this document reserved. Owlory was the initial source of truth while the harness was being extracted; it no longer is, and that statement is retired rather than left standing alongside this one.

A consumer never writes to `repo-automation`. Changes to reusable automation are made here, published here, and then imported into the consumer at an exact commit. A consumer that discovers a needed harness change makes it upstream first.

The pin is what makes the snapshot reviewable: a consumer records the canonical repository identity and the full commit SHA it imported, so a reviewer can tell exactly which `repo-automation` commit the vendored files came from. A moving unpinned checkout is not a canonical identity.

### Why the direction matters

The flip was forced by measurement, not preference. While Owlory was still declared the source, its manifest claimed `automation/schemas/` and `automation/benchmark/` with `delete_stale: true`. Those paths had since moved forward here — the Pydantic contract models and the benchmark lanes were added upstream — so the one-way "update" resolved to **568 insertions against 1937 deletions**, destroying five committed upstream files:

```text
automation/schemas/models.py      automation/benchmark/lanes.py
automation/schemas/generate.py    automation/benchmark/strategies.py
automation/schemas/__init__.py
```

A full inventory of the reusable boundary at that moment found 18 files identical, 15 ahead upstream, 5 upstream-only and exactly 1 diverged — **no file was ahead in the consumer**. The inversion was structural: a consumer's older inventory was deciding what the canonical repository should contain.

The rule that replaces it: the canonical source's inventory decides the content of canonical-owned destination files, and nothing else. An import may never delete a consumer's own files, a consumer-specific path, or a file simply because an older snapshot did not list it.

## Goals

- Reuse the supervised slice harness in other repositories without copying Owlory product state.
- Keep each consumer's vendored snapshot current with the canonical repository, at a pin a reviewer can check.
- Make reusable assets manifest-owned so sync behavior is deterministic and reviewable.
- Preserve Owlory-specific release, localization, UI proof, product, and SecondBrain history inside Owlory.

## Reusable Inventory

These assets are reusable or intended to become reusable with light parameterization:

- `automation/supervisor/`: queue selection, policy checks, validation ownership, diff-budget checks, and fresh-run launching.
- `automation/context/build_context.py`: compact slice context bundle generation.
- `automation/prompts/`: base, slice, and review prompt fragments.
- `automation/schemas/`: the Pydantic contract models (`models.py`) and the JSON Schemas generated from them.
- `automation/examples/`: starter queue and handoff payloads for new repositories.
- `automation/README.md`: harness behavior and operator model.
- `automation/tests/test_harness.py`: core supervisor and context-builder regression coverage.
- `Tools/clean-stop-check.py`: reusable after repository name and queue path are configurable.
- `Tools/agent-handoff.sh`: reusable after repository name, read order, and validation shortcuts are configurable.
- `pyproject.toml` `[tool.ruff]` and `[tool.ty]` sections: reusable lint and type-check configuration; a consumer copies the blocks into its own `pyproject.toml` and adjusts paths.
- Make targets for `handoff`, `clean-stop`, `automation-check`, and the lint/type checks (`uv run ruff check .`, `uv run ty check`): reusable after app-specific targets are excluded.

## Owlory-Specific Exclusions

These are consumer-owned. They are never canonical-owned, never imported, and an import may never delete them:

- `automation/queue/slices.json`: live Owlory work queue and product history.
- `automation/handoffs/`: live Owlory handoff history.
- `automation/proofs/`: app, localization, TestFlight, screenshot, and design proof artifacts.
- `automation/smoke/`: currently tied to Owlory's Xcode app, simulator, localization, and screenshot proof paths.
- Owlory-specific automation tests such as release provenance, localization drift, localized screenshots, running app smoke, and version bump tests.
- Owlory product docs under `docs/product/`, `docs/runtime/`, and most `docs/workflows/` entries that describe app behavior rather than harness behavior.
- `SecondBrain/`: Owlory operational history.
- `localization/`, `owlory_xcode/`, app resources, and generated app artifacts.
- Release tooling such as `Tools/bump-version.sh`, `Tools/set-build-number.sh`, `Tools/generate-build-info.sh`, `Tools/verify-build-provenance.sh`, `Tools/release-preflight.sh`, and `.githooks/pre-push` as currently written.
- App-specific Make targets such as `fast`, `verify`, `test-domain`, `ui-smoke`, `ui-regression`, `build-provenance`, `release-preflight`, and localization targets.

## Manifest Contract

The tracked manifest lives at `automation/reusable-manifest.json` and owns the distribution file list.

The manifest is explicit rather than glob-heavy. Each entry identifies:

- source path in the canonical repository
- destination path in the consumer
- file or directory kind
- whether executable mode should be preserved
- whether stale destination files under that owned path may be deleted
- whether the entry is reusable now or copied as a template for consumer customization

The sync tool rejects paths outside the repository root and outside the target root. It does not follow symlinks into untracked locations.

The manifest does not name a destination. It says what is reusable, never where it lands — that is the caller's to state with `--target`, and a manifest carrying `default_target` is rejected. The field used to hold the canonical repository's own path, which was right while Owlory was the source and became a `--sync` away from writing outward the moment the direction flipped.

## Sync Contract

Use `Tools/repo-automation-sync.sh` for manifest-owned sync:

- `--check`: report drift between the canonical source and a consumer's vendored snapshot without changing files.
- `--sync`: update a consumer's vendored snapshot to match the manifest at the pinned canonical commit.
- `--import`: the explicit mutating path; require the canonical source to be clean and published at the pinned commit, import canonical-owned files inward, then verify `--check` passes. There is no outward `--auto-update`; it was the destructive direction and is removed.
- `--target <path>`: **required.** The consumer to write into. There is no default, and it may not be the canonical repository or another checkout of it.
- `--source <path>` and `--manifest <path>`: test hooks for temp repositories and alternate manifests.

### Canonical quality gate

`make reusable-check` is this repository's one named quality contract. It owns which tools run — currently `ruff check`, `ruff format --check`, `ty`, the JSON Schema drift check and the automation test suite — so consumers ask a single question instead of tracking the toolchain:

```bash
make reusable-check
```

The sync tool invokes it as the import preflight (protection 8). Adding or replacing a check here changes nothing on the consumer side.

### Declared runtime dependencies

Source provenance is identity, the quality gate is source quality, and this is consumer compatibility — three separate facts, checked in that order:

```text
canonical source: provenance ✓ + reusable-check ✓
consumer:         runtime dependency ✓
                        ↓
                  plan and write the import
```

```json
{
  "version": 1,
  "runtime": {
    "python": {
      "dependencies": [
        {
          "distribution": "pydantic",
          "import_name": "pydantic",
          "minimum_major": 2,
          "maximum_major_exclusive": 3
        }
      ]
    }
  },
  "entries": []
}
```

Majors only, on purpose: the check is `2 <= major < 3` read from `importlib.metadata.version()`, not a PEP 508 implementation, so no package-manager dependency appears inside the dependency checker.

The manifest stays at `version: 1`. A sync tool predating this block ignores it and enforces nothing, so runtime enforcement begins with a tool that understands it. Negotiating manifest/tool compatibility is a separate problem, worth solving only if heterogeneous consumer tool versions ever need to coexist.

The tool must be idempotent. Running `--sync` twice against the same source should produce no second change. Running `--check` after a successful sync should pass.

Stale-file deletion is allowed only under manifest-owned destination paths in the consumer, and only for files the canonical source does not track. The tool must not clean arbitrary files, and it must never delete anything in `repo-automation`.

## Import Contract

A consumer imports canonical files inward. It never updates this repository outward, and no ordinary `git push` in a consumer may rewrite either repository.

The operation is split so that reading is never coupled to mutating:

- `make repo-automation-check` is **read-only**. It reports whether the vendored snapshot still matches the pinned canonical commit, and exits non-zero on drift without changing a byte.
- `make repo-automation-import` is the only mutating path, and it is explicit. It imports canonical-owned files at the pinned commit.

A consumer's pre-push hook runs the read-only check, never the import. When the check fails it refuses the push and states the remediation in upstream-first order:

1. make the reusable change in `repo-automation`
2. publish it there
3. import that exact commit into the consumer
4. commit the updated pin together with the imported snapshot
5. retry the push

No automatic external mutation, and no hidden cross-repository commits.

### Import protections

These are enforced in `Tools/repo-automation-sync.sh` itself, not in a consumer's Makefile, so they hold however the tool is invoked. Each one refuses with a non-zero exit and changes nothing — the import is planned in full before a single file is written, so a refusal leaves the consumer byte-for-byte unchanged rather than half-imported.

1. **No outward mutation.** Importing into the canonical source, into anything inside it, or into anything containing it, is refused — and so is importing into any *other* checkout of the canonical repository, identified by a shared git directory or by an `origin` that matches the canonical remote. Path containment alone does not establish identity: two checkouts sitting side by side contain neither the other, so the destructive case looks like an ordinary import right up until `delete_stale` runs. `--auto-update` — the mode that once resolved to deleting 1937 lines of committed canonical work — has been removed, and no flag re-enables it. There is also no default destination: `--target` is required, and a manifest carrying `default_target` is rejected rather than ignored.
2. **The canonical commit is explicit.** `--sync` requires `--pin <full-sha>`.
3. **Repository identity is verified.** The source's `origin` must equal `--expect-remote`. A source with no `origin` fails; it cannot state what repository it is, which is the case the check exists for.
4. **The source must be published and clean.** Uncommitted content is unpublished by definition, and the source must be at the pinned commit, not merely near it.
5. **Consumer-owned destinations are never importable.** A manifest entry may not target `.githooks/`, `Makefile`, `automation/queue/`, `automation/handoffs/`, `automation/proofs/`, `automation/repo-automation.lock`, app code or release tooling — nor any *ancestor* of them, since a `delete_stale` directory entry rooted at `automation` would sweep the queue without ever naming it. Comparison is casefolded, because on APFS and NTFS a destination of `makefile` is the file being protected. Unlike the source-side check there is no manifest opt-out. The lock file is on this list because the consumer's Makefile reads it and passes the values to this tool: whoever writes the lock chooses what the consumer's `make` executes.
6. **Stale deletion stays inside what the canonical side owns.** Because (5) is enforced when the manifest is parsed, `delete_stale` can only ever reach canonical-owned destinations.
7. **Locally modified files are never clobbered.** A tracked destination file with uncommitted consumer edits stops the import; that work exists in neither history. Template entries are additionally not overwritten by an ordinary import. This check fails closed: a consumer that is not under version control has nothing to protect and imports normally, but any *other* Git failure — dubious ownership, a missing `git` — refuses the import rather than silently proceeding without the guard.

8. **The canonical snapshot must pass the canonical quality gate.** Provenance establishes identity, not quality: a source can sit at the pinned commit, on the right remote, with a clean worktree, and still be lint-red or schema-drifted — canonical `main` was exactly that for two commits on 2026-09-15. Before any file is read for import, the tool runs `--quality-command` (default `make reusable-check`) in the *source* checkout and refuses the import if it exits non-zero, quoting the tail of its output. The gate is named rather than enumerated: the tool never learns which linters exist this month, and adding a check to `reusable-check` needs no change on the consumer side. A gate that cannot be run at all is a refusal, not a pass.

9. **The consumer must be able to run what it vendors.** A manifest may declare the Python distributions the imported code needs in `runtime.python.dependencies`, each as a distribution name plus a supported major range. Before anything is planned or written, the tool asks the *consumer's* interpreter — `--runtime-python`, defaulting to the interpreter running the tool — what it has installed, via `importlib.metadata` only, so the dependency checker needs no dependency of its own. A missing or out-of-range distribution refuses the import with `missing_runtime_dependency: pydantic>=2,<3`, and `(found 1.10.15)` when a version is present but unsupported. A manifest that declares nothing enforces nothing.

Protections 2, 3, 4 and 8 can be waived together with `--allow-unverified-source` for local development. It prints a warning, the consumer's `make` targets never pass it, and it cannot waive 1, 5, 6, 7 or 9. Protection 9 is deliberately outside that waiver: trusting an unpublished source is a different claim from being able to execute the code it carries.

Cleanliness is not provenance: the destructive 2026-09-14 run was against a clean worktree on the correct branch. What it lacked was any statement of which commit it was meant to be.

`automation/tests/test_repo_automation_sync.py` holds one test per protection. Consumers are expected to keep equivalents against their own vendored checkout.

## Consumer Repository Contract

A future repository should consume `repo-automation` as a reusable package or template, then own its repo-specific configuration:

- repository name and read order
- domain or work-area docs
- validation commands and proof levels
- local queue file
- handoff directory
- prompt fragments if local policy differs
- Make targets that map to that repository's build and test commands

New repositories must start from examples or templates, not Owlory's live queue, handoffs, proofs, or SecondBrain history.

## Clean GitHub Stop Contract

Reusable automation assumes every task ends with a clean GitHub stop across all touched repositories. A clean stop requires:

1. all changes committed in logical commits
2. the current branch pushed to its GitHub upstream
3. `git status --short` returning no output
4. `git rev-list --left-right --count HEAD...@{u}` returning `0 0`

This applies even in multi-agent workspaces. If another agent leaves dirt in a repo, inspect it, commit or deliberately preserve it, push the resulting branch, and report the exact state. If a branch has no upstream, credentials fail, or a push is rejected, record that blocker explicitly and do not call the stop clean.

## Agent Runner Selection

`automation/supervisor/run_agent.sh` is the reusable launch wrapper for fresh slice agents. Its default `auto` mode supports both Codex and Claude Code without changing `policy.agent_command_template` in every consumer queue.

The wrapper chooses Claude Code when it detects that the supervisor was invoked from a Claude Code process tree and a `claude` executable is available. Outside Claude Code it preserves the previous Codex-first behavior: use `codex` when present, then `claude`, then `hermes`. Operators can pin a runner with:

```bash
REPO_AUTOMATION_AGENT_RUNNER=claude python3 automation/supervisor/run_next.py
REPO_AUTOMATION_AGENT_RUNNER=codex python3 automation/supervisor/run_next.py
REPO_AUTOMATION_AGENT_RUNNER=hermes python3 automation/supervisor/run_next.py
```

Codex runs with the existing no-approval, workspace-write invocation. Claude Code runs non-interactively with prompt stdin, `--print`, `--input-format text`, `--no-session-persistence`, `--permission-mode bypassPermissions`, and `--add-dir <repo_root>`. Hermes runs one-shot with `--safe-mode` and `--in <repo_root>`, so a run carries no operator config, memory, plugins or MCP servers; it takes its provider and model from `HERMES_INFERENCE_PROVIDER` and `HERMES_INFERENCE_MODEL`.

Consumer repositories can override executable names with `REPO_AUTOMATION_CODEX_BIN` or `REPO_AUTOMATION_CLAUDE_BIN`. `OWLORY_CODEX_BIN` remains supported for existing Codex setups. Consumers that require a stricter Claude local policy can set `REPO_AUTOMATION_CLAUDE_PERMISSION_MODE` to another Claude Code permission mode.

## Bootstrap Status

As of 2026-05-21, `/Users/raelldottin/Documents/Personal/repo-automation` is initialized as a Git repository on `main`. The bootstrap commit is `6ab871bbf957df24e648b02ef002c0efa2d7c609`. Exact publication commits are recorded in Owlory handoffs so this manifest-synced workflow doc does not have to change for every external commit.

The bootstrap commit was populated only by `Tools/repo-automation-sync.sh --sync --target /Users/raelldottin/Documents/Personal/repo-automation`, then verified with:

```bash
Tools/repo-automation-sync.sh --check --target /Users/raelldottin/Documents/Personal/repo-automation
```

## Remote Status

The external repository remote is:

```text
https://github.com/raelldottin/repo-automation.git
```

`main` tracks `origin/main`, and `git -C /Users/raelldottin/Documents/Personal/repo-automation rev-list --left-right --count HEAD...@{u}` returns `0 0`.

The GitHub repository also exposes the SSH URL `git@github.com:raelldottin/repo-automation.git`, but this machine does not currently have GitHub SSH key authentication configured. Publication used the GitHub CLI authenticated HTTPS path.

## Consumer Adoption Bootstrap

A non-Owlory repository can adopt the reusable automation package by syncing the
manifest-owned subset from `repo-automation` and then committing it into a fresh local Git
repository. The exact sequence proven by `RepoAutomationConsumerAdoptionSmokeTests`
in `automation/tests/test_repo_automation_sync.py` is:

1. Create the consumer directory and run

   ```bash
   Tools/repo-automation-sync.sh --sync --target <consumer-path>
   ```

   from inside Owlory. The manifest at `automation/reusable-manifest.json` decides
   what lands; Owlory product state (live queue, handoffs, proofs, SecondBrain,
   `owlory_xcode/`, localization, product/runtime docs, release tooling, Owlory
   pre-push hook) is rejected by the sync tool unless an entry explicitly opts
   in with `allow_owlory_specific: true`.

2. From the consumer directory:

   ```bash
   git init -b main
   git config user.email <consumer-email>
   git config user.name <consumer-name>
   git add -A
   git commit -m "Bootstrap reusable automation"
   ```

   The supervisor and `make repo-automation-import` both require a clean Git
   working tree. The very first sync produces many untracked files, so the
   bootstrap commit must happen before normal automation runs.

3. Provide the repo-specific local state the reusable assets expect:

   - `automation/queue/slices.json` — copy `automation/examples/example-slices.json`
     as a starting point and rewrite it for the consumer's own slices.
   - `automation/handoffs/` — create the directory; the supervisor writes
     handoff artifacts here.
   - `.gitignore` entry for `__pycache__/`, or invocations should set
     `PYTHONDONTWRITEBYTECODE=1`. Without one of those, supervisor runs
     leave pycache files that the supervisor's own dirty-tree check then
     refuses on the next invocation.

4. Smoke-verify by running the supervisor inside the consumer:

   ```bash
   PYTHONDONTWRITEBYTECODE=1 python3 automation/supervisor/run_next.py --dry-run
   ```

   The dry-run prints `selected_slice` plus a handoff path that resolves under
   the consumer repo (not Owlory). `automation/supervisor/run_next.py` derives
   `REPO_ROOT` from its own file location, so syncing the supervisor file tree
   into the consumer is what makes it operate on the consumer's queue.

### Known consumer-side failure modes

The smoke test asserts the friendly message shape so future changes to the
reusable supervisor do not silently regress it back into raw tracebacks. The
common consumer-side failure modes now exit with code 2 and a two-line
`stop: <reason>` + `hint: <fix>` shape:

- Missing `automation/queue/slices.json` (either via
  `automation/supervisor/run_next.py` or `automation/context/build_context.py`):

  ```text
  stop: queue file not found: <path>
  hint: copy automation/examples/example-slices.json to that path and edit it for this repository's slices.
  ```

- Running the supervisor outside a Git working tree:

  ```text
  stop: not a Git repository: <consumer path>
  hint: run 'git init -b main' in this directory, commit the bootstrap, then re-run.
  ```

- Running the supervisor on a dirty working tree returns the supervisor's own
  `stop: repo is dirty outside the next slice scope` message and a non-zero
  exit code. This path was already friendly before this slice.

The friendly messages flow through a `policy.ConfigError` exception raised by
`automation/supervisor/policy.py` (`load_json`, `load_queue`, `git_dirty_paths`)
and caught at the CLI entry points (`run_next.py:_cli_entry`,
`build_context.py:_cli_entry`).

### Manual steps that remain for a real consumer

These are not covered by the smoke test and require explicit per-repository
work:

- A consumer-specific `Makefile` with targets that map to the consumer's own
  build, test, and validation commands. The reusable tree does not ship a
  Makefile because Owlory's targets are app-specific.
- A consumer-specific `AGENTS.md` (or equivalent) that names the consumer's
  read order, allowed paths, and validation expectations.
- Optional `core.hooksPath` configuration if the consumer wants the same
  commit-msg or pre-push behavior as Owlory.
- Optional override of the prompt fragments under `automation/prompts/` if the
  consumer needs different policy language (see Customizing prompt fragments
  below).
- Optional update of the `[tool.ty]` paths in `pyproject.toml` if the consumer
  wants its own Python paths type-checked.
- A consumer-specific remote (e.g., `git remote add origin <url>`) and an
  initial push. The smoke test does not exercise remote publication.

### Customizing prompt fragments

`automation/supervisor/run_next.py:render_prompt` reads
`automation/prompts/base.md` and `automation/prompts/slice.md` from the
consumer's own `repo_root`, so a consumer can rewrite either file and the
supervisor will use the customized text for every subsequent run. This is
asserted by
`test_consumer_can_override_prompt_fragments` in
`automation/tests/test_repo_automation_sync.py`, which writes sentinel
markers into both files and verifies they appear in the rendered prompt.

Because those fragments are first-time-only template entries, a placeholder rename in
this repository does **not** reach a consumer that already customized the file. The
supervisor now substitutes `__EXECUTION_CONSTRAINTS__` (previously
`__ACCEPTANCE_CHECKS__`); a consumer holding an older `slice.md` renders the old token
literally and loses that section until it renames the placeholder in its own copy.

The consumer-side flow:

1. **Commit the override first.** The supervisor's dirty-tree check refuses
   to run when files outside the current slice's `allowed_paths` are dirty.
   A consumer who edits `automation/prompts/*.md` must `git add` and commit
   before running the supervisor; otherwise the run aborts with the existing
   `stop: repo is dirty outside the next slice scope` message.

2. **Overrides survive re-sync.** The manifest entries marked
   `template: true` (currently `automation/prompts/` and `automation/examples/`)
   are first-time-only: `Tools/repo-automation-sync.sh`
   copies them when the destination file does not yet exist and otherwise
   leaves them alone. Consumer-added files in those directories also survive
   (those entries set `delete_stale: false`).
   `test_consumer_prompt_override_survives_resync` and
   `test_consumer_added_prompt_file_survives_resync` cover both cases.

3. **Owlory updates to template files don't auto-propagate.** Because the
   sync skips existing template files, an upstream Owlory improvement to
   `base.md` or `slice.md` does not reach a consumer who already has those
   files. Non-template entries (the supervisor, context builder, schemas,
   harness test, docs) continue to be authoritative on every sync.

   To deliberately re-baseline template files to the current Owlory
   content, run:

   ```bash
   Tools/repo-automation-sync.sh --sync --force-templates --target <consumer>
   ```

   `--force-templates` bypasses the first-time-only guard so all template
   entries are rewritten to source content, replacing any local
   customizations. The flag intentionally does NOT remove
   consumer-added files in template directories — those still survive
   because the manifest entries set `delete_stale: false`. This is
   asserted by `test_force_templates_overwrites_consumer_override` and
   `test_force_templates_preserves_consumer_added_files` in
   `automation/tests/test_repo_automation_sync.py`.

### What the smoke test does not prove

- That any specific external repository has actually adopted the package.
- That Make targets, hooks, or prompt fragments composed into a real consumer
  Makefile produce a working CI integration.
- That the reusable supervisor handles consumer-side prompts or LLM
  invocations end-to-end. The smoke proof is limited to `--dry-run` slice
  selection.

## Next Slice Boundary

Consumer adoption proof is complete at the smoke level and the common failure
modes a consumer encounters now exit with friendly `stop:` + `hint:` messages.
The next implementation boundaries (not queued by this slice) are a real
third-party consumer migration when a specific repository is named, prompt
fragment override portability proof, and a consumer Makefile / hooks / CI
smoke.
