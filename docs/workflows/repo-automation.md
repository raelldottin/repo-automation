# Reusable Repo Automation

This workflow defines how Owlory's repo automation becomes reusable in other repositories while the current Owlory checkout remains the initial source of truth.

## Target Home

The reusable automation distribution target is:

```text
/Users/raelldottin/Documents/Personal/repo-automation
```

`/Users/raelldottin/Personal` does not exist on this machine. The active Personal workspace is `/Users/raelldottin/Documents/Personal`.

Until an explicit ownership flip is documented, updates flow one way:

```text
Owlory reusable automation source -> /Users/raelldottin/Documents/Personal/repo-automation
```

The external folder is a reusable distribution target, not a second source of truth. Reverse edits from `repo-automation` back into Owlory require an explicit migration or patch slice.

## Goals

- Reuse the supervised slice harness in other repositories without copying Owlory product state.
- Keep the external `repo-automation` folder current whenever Owlory's reusable automation changes.
- Make reusable assets manifest-owned so sync behavior is deterministic and reviewable.
- Preserve Owlory-specific release, localization, UI proof, product, and SecondBrain history inside Owlory.

## Reusable Inventory

These assets are reusable or intended to become reusable with light parameterization:

- `automation/supervisor/`: queue selection, policy checks, validation ownership, diff-budget checks, and fresh-run launching.
- `automation/context/build_context.py`: compact slice context bundle generation.
- `automation/prompts/`: base, slice, and review prompt fragments.
- `automation/schemas/`: JSON contracts for slices and handoffs.
- `automation/examples/`: starter queue and handoff payloads for new repositories.
- `automation/README.md`: harness behavior and operator model.
- `automation/tests/test_harness.py`: core supervisor and context-builder regression coverage.
- `Tools/clean-stop-check.py`: reusable after repository name and queue path are configurable.
- `Tools/agent-handoff.sh`: reusable after repository name, read order, and validation shortcuts are configurable.
- `pyrightconfig.json`: reusable for Python automation type-checking after include paths are generated or documented per consumer repository.
- Make targets for `handoff`, `clean-stop`, `automation-check`, and `pyright`: reusable after app-specific targets are excluded.

## Owlory-Specific Exclusions

The sync manifest must not copy these into the reusable automation distribution unless a later slice explicitly extracts and generalizes them:

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

- source path in Owlory
- destination path under `repo-automation`
- file or directory kind
- whether executable mode should be preserved
- whether stale destination files under that owned path may be deleted
- whether the entry is reusable now or copied as a template for consumer customization

The sync tool rejects paths outside the repository root and outside the target root. It does not follow symlinks into untracked locations.

## Sync Contract

Use `Tools/repo-automation-sync.sh` for manifest-owned sync:

- `--check`: report drift between Owlory reusable sources and the external folder without changing files.
- `--sync`: update the external folder to match the manifest.
- `--auto-update`: for validation and pre-push use only; require the target to be an existing clean Git repository, sync manifest-owned files, then verify `--check` passes.
- `--target <path>`: override the target path for tests and future consumers.
- `--source <path>` and `--manifest <path>`: test hooks for temp repositories and alternate manifests.

The tool must be idempotent. Running `--sync` twice against the same source should produce no second change. Running `--check` after a successful sync should pass.

Stale-file deletion is allowed only under manifest-owned destination paths. The tool must not clean arbitrary files in `repo-automation`.

## Automatic Update Contract

Owlory wires repo-automation currentness into the normal local automation path:

- `make repo-automation-check` runs `Tools/repo-automation-sync.sh --check --target /Users/raelldottin/Documents/Personal/repo-automation`.
- `make repo-automation-update` runs `Tools/repo-automation-sync.sh --auto-update --target /Users/raelldottin/Documents/Personal/repo-automation`.
- `.githooks/pre-push` detects whether the pending push touches manifest-owned reusable automation sources. If so, it runs `make repo-automation-update` before allowing the push.

Expected behavior:

- If a commit changes manifest-owned reusable automation, the pre-push path syncs `/Users/raelldottin/Documents/Personal/repo-automation` locally and verifies the target is current.
- If the external target is missing, `--auto-update` fails and points to the bootstrap path.
- If the external target is not a Git repository, `--auto-update` fails rather than creating an untracked copy.
- If the external target has local dirt before the update, `--auto-update` fails rather than overwriting external work.
- Automatic update means the external folder contents are updated locally. External Git commit or remote push remains explicit unless a later slice adds documented opt-in behavior.

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

## Next Slice Boundary

`repo-automation-consumer-adoption-smoke` owns the next implementation step:

- prove a non-Owlory repository can consume the reusable automation package
- exercise bootstrap instructions and required local configuration in a temp repo where practical
- document remaining manual steps for real repositories

It must not migrate another real repository. Consumer adoption proof is a separate slice.
