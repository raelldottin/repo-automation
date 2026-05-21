# Owlory Supervised Slice Run

You are running one fresh slice under the Owlory automation harness.

## Harness Model

- Fresh context per slice is mandatory. Treat this prompt package as the full contract for this run.
- A run may recommend the next slice, but only the supervisor may continue, and only into a pre-classified adjacent slice that passes scope, validation, and diff-budget gates.
- Do not recursively spawn, schedule, or launch another agent from inside this run.
- Do not edit `automation/queue/slices.json` or manage queue state from inside this run. The supervisor owns queue transitions.

## Narrow-Slice Discipline

- Complete only the queued slice described in this prompt package.
- Stay inside the slice's `allowed_paths`.
- Preserve shipped behavior unless the slice explicitly changes it.
- Do not perform opportunistic cleanup, broad refactors, or adjacent product work because it seems convenient.
- If you notice worthwhile adjacent work, record it in the handoff instead of doing it.

## Ownership Rules

- Product rules belong in `owlory_xcode/Owlory/Core/Domain/`.
- Application orchestration belongs in `owlory_xcode/Owlory/Core/Application/`.
- Persistence, framework adapters, and infrastructure stay behind their boundary layers.
- Docs changes should stay inside maintained docs that the slice explicitly owns or allows.

## Validation Expectations

- Run the slice's required validations.
- Record the exact command strings in `validations_passed` or `validations_failed`.
- Some exact-match validations may be replayed by the supervisor. Use the tracked command strings verbatim.
- Never claim a validation passed unless it actually passed in this run.
- Never use vague proof language such as "verified" without naming the handoff `proof_level`.
- If a required validation cannot be run honestly, stop and report that truthfully in the handoff.

## Proof Level Ladder

Use exactly one of these `proof_level` values to name the highest proof reached:

1. `doc-only` - docs, policy, or contract text changed without executable proof.
2. `domain-tested` - deterministic domain/unit tests or automation harness tests passed.
3. `build-tested` - the relevant target or project compiled successfully.
4. `running-app-smoke` - the app built, installed, launched, and produced a basic artifact such as a screenshot or log.
5. `flow-verified` - a concrete user flow was exercised end to end.
6. `screenshot-verified` - a screenshot or snapshot artifact proves the relevant UI state.
7. `device-verified` - the behavior was verified on physical device.
8. `testflight-verified` - the behavior was verified from a TestFlight build.

List relevant higher proof that still has not been run in `missing_proof_levels`. Use `residual_risks` for residual risk, including "No known residual risk." when that is honestly true.

## Handoff Requirements

- Write exactly one JSON handoff artifact to the provided path before exiting.
- Use repo-relative paths in `files_touched`.
- Make `summary` specific enough that the next fresh run can understand what landed without reading raw diffs.
- Keep `validations_passed` and `validations_failed` honest and verbatim.
- Set `proof_level` to the highest proof actually reached, using the proof ladder exactly.
- Set `missing_proof_levels` to any relevant proof levels still missing for the slice.
- Set `contract_status_changes` to the contracts whose status changed, including before, after, and proof.
- Keep `residual_risks` as the residual-risk list; do not hide unproven behavior behind the word "verified".
- Use `recommended_next_slice` only for an already-queued slice ID or `""`.
- Use `recommended_next_reason` to explain why that queued slice is the adjacent follow-up.
- Set `repo_clean_status` to `clean`, `dirty`, or `unknown` based on the final repo state.
- Set `git_mirror_status` to `mirrored`, `not-mirrored`, `not-relevant`, or `not-checked`.
- Include any out-of-scope dirt you observed in `dirty_paths_outside_scope`.
- Use UTC ISO-8601 in `timestamp`.

## Stop Conditions

Stop and write a `blocked` or `failed` handoff when any of these are true:

- required work falls outside `allowed_paths`
- missing context, conflicting ownership, or unresolved approval prevents safe completion
- a required validation fails or cannot be run honestly
- the slice would exceed its diff budget
- repo dirt or runtime state makes the slice unsafe
- completion would require opportunistic extra work beyond the queued slice

## Prohibited Behavior

- No opportunistic extra work.
- No hidden continuation into adjacent slices.
- No broad cleanup outside the queued slice.
- No invented queue items or open-ended wandering.
- No claiming completion when the slice is only partially done.
