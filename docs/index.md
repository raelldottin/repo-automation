# repo-automation

Reusable supervised-slice automation distribution. This repository is the
publication target for the manifest-owned slice harness in
[raelldottin/owlory](https://github.com/raelldottin/owlory); changes flow one
way from Owlory into this repo via `Tools/repo-automation-sync.sh`.

## What lives here

- `automation/supervisor/` — queue selection, policy checks, validation
  ownership, diff-budget checks, and fresh-run launching.
- `automation/context/build_context.py` — compact slice context bundle
  generation.
- `automation/prompts/` — base, slice, and review prompt fragments
  (consumer-customizable).
- `automation/schemas/` — JSON contracts for slices and handoffs.
- `automation/examples/` — starter queue and handoff payloads for new
  repositories.
- `automation/tests/test_harness.py` — core supervisor and context-builder
  regression coverage.
- `Tools/repo-automation-sync.sh` — manifest-owned sync between Owlory and
  this distribution target.
- `docs/workflows/repo-automation.md` — full workflow contract including
  consumer adoption bootstrap.

## Consumer adoption

See [Workflow — Repo Automation](workflows/repo-automation.md) for the full
adoption sequence, known failure modes, and manual steps that remain for a
real consumer.

## Reporting issues

Bug reports and adoption questions go to
[github.com/raelldottin/repo-automation/issues](https://github.com/raelldottin/repo-automation/issues).
