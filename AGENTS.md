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

### Adding a Benchmark

A benchmark for a registered path belongs in `automation/tests/test_benchmarks.py`, carries `@pytest.mark.benchmark`, and exercises the bundled fixtures in `automation/examples/` rather than consumer repository state.

Every benchmark MUST assert the result it measured. A benchmark that stops exercising its path should fail, not report a faster number.
