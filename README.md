# repo-automation

[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://codspeed.io/raelldottin/repo-automation?utm_source=badge)

Reusable supervised automation for bounded agent work.

`repo-automation` packages a slice-based supervisor harness: queue records, context building, prompt fragments, validation ownership, handoff schemas, and stop rules for keeping AI-assisted repository work scoped and reviewable.

## What It Provides

- A machine-readable queue model for pre-classified implementation slices.
- A context builder that gives each run only the slice metadata and nearby evidence it needs.
- Supervisor policy checks for dependencies, allowed paths, diff budgets, and validation ownership.
- JSON handoff contracts that preserve what changed, what passed, and what risk remains.
- Prompt fragments for fresh slice runs and manual review.
- Sync tooling for keeping this reusable package current while extraction from Owlory continues.

## What It Does Not Provide

- Repo-specific product rules, build commands, or release policy.
- A live work queue for your project.
- Silent commits or remote pushes for consumer repositories.
- Recursive self-spawning from inside an agent run.

Each consuming repository owns its queue, validation commands, domain docs, handoff history, and local safety policy. The reusable policy expects each task to finish with a clean GitHub stop: all touched repositories committed, pushed to their GitHub upstreams, locally clean, and even with upstream. Consumers may decide how to automate that requirement, but they should not claim a clean stop until `git status --short` is empty and `git rev-list --left-right --count HEAD...@{u}` returns `0 0`.

## Layout

- `automation/README.md` - detailed harness model and operating rules.
- `automation/context/` - bounded context bundle generation.
- `automation/examples/` - example queue and handoff payloads.
- `automation/prompts/` - base, slice, and review prompt fragments.
- `automation/schemas/` - JSON schemas for queues and handoffs.
- `automation/supervisor/` - queue selection, stop policy, validation replay, and agent launch orchestration.
- `automation/tests/` - harness regression tests.
- `docs/workflows/repo-automation.md` - extraction, sync, and consumer-adoption contract.
- `Tools/repo-automation-sync.sh` - manifest-owned sync/check/update tool.

## Quick Start

Clone the package:

```bash
git clone https://github.com/raelldottin/repo-automation.git
cd repo-automation
```

Run the standalone type check:

```bash
pyright
```

Build context for the example queue:

```bash
python3 automation/context/build_context.py \
  --queue automation/examples/example-slices.json \
  --slice-id today-continue-ui-regression-coverage
```

For real adoption, copy the example queue shape into your repository, replace the example slices with project-specific work, and set validation commands that actually exist in that repository.

## Adoption Checklist

1. Add `automation/queue/slices.json` for your repository.
2. Add `automation/handoffs/` for run artifacts.
3. Define allowed paths, validation commands, and proof levels for each slice.
4. Point `agent_command_template` at your local agent launcher.
5. Run `python3 automation/supervisor/run_next.py --dry-run` before launching a real slice.

## Continuous Integration

The repository ships with seven GitHub Actions workflows in `.github/`:

- **CodSpeed** (`.github/workflows/codspeed.yml`) — runs `pytest-codspeed`
  benchmarks defined in `automation/tests/test_benchmarks.py` on every push
  and pull request. Uses OIDC authentication with the CodSpeed GitHub App.
- **Pylint** (`.github/workflows/pylint.yml`) — recursive lint across
  `automation/` on Python 3.11 and 3.12. Pylint configuration lives in
  `pyproject.toml`.
- **Coverage** (`.github/workflows/coverage.yml`) — runs `test_harness.py`
  under `pytest --cov`, deselects two Owlory-specific tests that depend on
  live queue state, and comments the report on pull requests via
  `MishaKav/pytest-coverage-comment`.
- **Dependency Check** (`.github/workflows/dependency-check.yml`) —
  `pypa/gh-action-pip-audit` against `requirements-dev.txt` on push, PR, and
  a weekly schedule. Fails when a known CVE matches a pinned dev dependency.
- **Dependency Graph Review** (`.github/workflows/dependency-review.yml`) —
  `actions/dependency-review-action` runs on pull requests and fails the PR
  if it introduces a high-severity vulnerability.
- **Publish Documentation** (`.github/workflows/publish-docs.yml`) — builds
  the MkDocs Material site from `mkdocs.yml` + `docs/` on every push to
  `main` (or on `docs/` changes). Deploy step probes the Pages API and
  skips with a notice until Pages is enabled.
- **Dependabot** (`.github/dependabot.yml`) — weekly updates for the
  `github-actions` and `pip` ecosystems with labeled PRs.

### Manual setup that remains

The build artifacts on `main` are green by default once these files land,
but one integration requires a repository-settings action before it goes
live end-to-end:

1. **GitHub Pages**: under **Settings -> Pages**, set **Source** to
   **GitHub Actions**. The publish-docs workflow will then deploy the
   MkDocs site on the next push.

## Status

This repository is the reusable automation distribution extracted from Owlory. The core harness is present; consumer-repository smoke proof is the next hardening step. Treat project-specific examples as templates, not as production configuration.
