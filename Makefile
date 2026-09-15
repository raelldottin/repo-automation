# Canonical quality contract for the reusable harness.
#
# `reusable-check` is the single named gate a consumer's sync preflight invokes before
# importing this repository. It owns which tools run; the sync tool only asks whether the
# gate passed, so adding a check here needs no change on the consumer side.

UV ?= uv

.PHONY: reusable-check

reusable-check:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run ty check automation Tools
	$(UV) run python -m automation.schemas.generate --check
	$(UV) run python -m pytest automation/tests -q
