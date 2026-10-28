## Performance Evidence and CodSpeed

Performance testing is an evidence requirement, not a repository-wide benchmark-count target.

### Hard Rule

Every **registered performance-critical path** MUST have representative CodSpeed benchmark coverage.

A change that affects a registered performance-critical path MUST produce applicable performance evidence before the work is considered complete.

**100% CodSpeed coverage means 100% coverage of the registered performance-critical paths. It does not mean 100% of source files, functions, methods, branches, or lines must have microbenchmarks.**

Do not create benchmarks solely to increase a coverage percentage.

### Performance-Critical Path Definition

A path is performance-critical when a material regression could affect the responsiveness, throughput, resource consumption, scalability, or operating cost of the automation harness.

Treat a path as performance-critical when any of the following applies:

- It is explicitly listed in the Performance-Critical Path Registry below.
- It is on a frequently executed supervisor, policy, validation, context-building, serialization, parsing, or queue-processing path.
- It performs work whose cost grows materially with queue size, handoff count, document size, repository size, or another expected workload dimension.
- It is identified by profiling, CodSpeed, production evidence, or an incident as a meaningful CPU, memory, I/O, or latency hot spot.
- It has previously caused or contributed to a performance regression.
- The task, issue, specification, or acceptance criteria explicitly establish a performance requirement.

Agents MUST NOT remove a path from this classification merely because satisfying the performance requirement is inconvenient.

### Performance-Critical Path Registry

The registry defines the denominator for the project's CodSpeed coverage requirement.

Current registered paths:

| Critical path | Current benchmark |
| --- | --- |
| Queue loading through `policy.load_queue` | `test_load_queue_benchmark` |
| Next-slice selection through `policy.select_next_slice` | `test_select_next_slice_benchmark` |
| Handoff schema validation through `policy.validate_document` | `test_validate_handoff_benchmark` |
| Context-bundle construction through `build_context_bundle` | `test_build_context_bundle_benchmark` |
| Kanban board projection through `kanban_projection.read_board` and `kanban_projection.project` | `test_project_kanban_board_benchmark` |
| Memory recall shaping and provenance verification through `memory.shape_recall` | `test_shape_memory_recall_benchmark` |

The registry MUST remain at **100% benchmark coverage**.

If a new performance-critical path is introduced or discovered, the same change MUST:

1. add that path to this registry;
2. add or extend a representative CodSpeed benchmark for it; and
3. establish enough baseline evidence for future regressions to be detected.

A path MUST NOT be omitted from the registry solely because no benchmark exists for it yet.

### Change-Impact Rule

Before completing a code change, determine whether the change can affect any registered performance-critical path.

Consider the path affected when the change modifies, directly or indirectly:

- algorithms or control flow;
- iteration or traversal behavior;
- parsing, serialization, or schema-validation work;
- data structures, caching, or memoization;
- I/O, subprocess, or filesystem access reached from that path; or
- a dependency whose own cost sits on that path.

A change that touches only documentation, comments, or code with no registered path downstream is not affected, and MUST NOT be held back for performance evidence. Deciding a change is unaffected is a judgement that MUST be made deliberately, not by omission.

### Performance Evidence

Applicable evidence is a CodSpeed run covering the affected registered paths:

```bash
python -m pytest automation/tests/test_benchmarks.py --codspeed
```

`.github/workflows/codspeed.yml` runs exactly that on every pull request and on pushes to `main`, so a pull request's own CodSpeed check is the evidence a reviewer reads. A local run is for the author's own iteration and is not a substitute, because only the CI run is measured against the recorded baseline.

Evidence is sufficient when the affected registered paths were measured and the result was read. A measured regression MUST be explained before the work is considered complete, and one of the following MUST hold:

- the regression is removed;
- the regression is accepted deliberately, with its reason and its cost recorded in the change; or
- the measurement is shown not to reflect the change.

An unexplained regression is not evidence of success, and a benchmark that was never run is not evidence at all.

## Control-Plane Architecture and Roadmap Priority

Before architecture or new feature work, read [SPEC-001](docs/architecture/control-plane.md) and [the roadmap](docs/roadmap.md). The proposed top layer is **Hermes Kanban**, **self-hosted Hindsight memory** and a **generated Swarm Wiki**, with distinct canonical owners and the deterministic repo-automation supervisor beneath it. **This is a proposed target, not an installed integration.**

- Prioritize the P0 adapter/inventory/canary acceptance gate before expanding the supervisor roadmap.
- **Benchmark exemption.** Benchmark and effectiveness work is exempt from the P0 ordering by owner decision. Do not defer, pause or refuse authorized benchmark work because P0 is incomplete. Each run, model, provider or budget change still needs its own authorization.
- Do not add a parallel queue, task store, memory database, coordination daemon or wiki write authority without a separate explicit design decision.
- Today the file-based slice queue remains operational authority; do not silently migrate or dual-write it and Kanban. A future cutover requires explicit approval, independent verification, provenance and rollback.
- Never treat wiki/memory summaries as independent live verification; do not allow workers to self-chain or bypass supervisor gates.
- **Delegation.** Agent-native delegation (a runner such as Claude Code launching its own sub-agents within one slice run) is available today. Supervisor-controlled delegation is proposed in SPEC-001 and not implemented; do not describe or rely on it as current behavior. No worker or sub-agent may start the next slice.
- The deployed Swarm Wiki currently reads ByteRover-specific `.brv/context-tree` files. Do not claim the wiki is Hindsight-compatible until a separately reviewed upstream migration adapter preserves source hashes and evidence.
- **Open-source production profile (owner gate).** A fully open-source production profile is a target the owner decides, not an instruction for agents to act on. Agents MUST NOT choose, add or change a license. Record license gaps and dependency or model license risks for the owner instead. Proprietary development assistants and the existing hosted benchmark sit outside that production profile.
- Keep benchmarks and pinned experiment settings isolated from live control-plane integrations.

## Adding a CodSpeed Benchmark

A benchmark for a registered path belongs in `automation/tests/test_benchmarks.py`, carries `@pytest.mark.benchmark`, and exercises the bundled fixtures in `automation/examples/` rather than consumer repository state.

Every benchmark MUST assert the result it measured. A benchmark that stops exercising its path should fail, not report a faster number.

## GitOps: Changes, Deployment, and Releases

`main` is the only source of deployed or released state. Git history on `main` is the record of what was shipped and why.

### Hard Rule

Every change MUST reach `main` through a pull request whose required checks have passed. Nothing deploys or releases except a workflow running from `main` or from a tag whose commit is on `main`.

### Changes to `main`

The `main` ruleset requires a pull request, the six required checks (`make reusable-check (3.11)`, `make reusable-check (3.12)`, `Tests + coverage comment`, `Run CodSpeed benchmarks`, `No agent attribution trailers`, `Review dependency graph diff`), and linear history, and it forbids force-pushes and deletion. Pull requests merge by squash or rebase. Approving reviews are not required yet.

Agents MUST NOT:

- push directly to `main`, force-push it, or delete it;
- disable, bypass, or weaken the ruleset or a required check, including by editing a workflow so a check passes without doing its work;
- merge a pull request whose required checks have not passed.

### Documentation Deployment

The documentation site deploys to GitHub Pages only from `.github/workflows/publish-docs.yml` running on a push to `main`. Agents MUST NOT publish the site any other way, including a manual dispatch from another branch.

### Releases

A release is a `vMAJOR.MINOR.PATCH` tag (optionally `-prerelease`) on a commit already on `main`, built and published by `.github/workflows/release.yml`, which reruns the required gate and publishes the archive and its `.sha256`. The maintainer creates the tag after the change merges.

Agents MUST NOT hand-build, upload, or edit a release asset, and MUST NOT create or move a release tag, unless the maintainer has explicitly authorized that action for that specific tag. Until `release.yml` exists on `main`, no release may be published.

### Consumer Repositories

Consumers receive changes only through `Tools/repo-automation-sync.sh`, driven by `automation/reusable-manifest.json`, from a released tag or a pinned commit. The result lands through a pull request in the consumer repository.

Agents MUST NOT commit or push to a consumer repository on this repository's behalf. Automatic consumer updates, including Owlory, are not implemented.

### Out of Scope

Runtime deployment, including the Hermes runtime and benchmark execution environments, is not covered by this policy.
