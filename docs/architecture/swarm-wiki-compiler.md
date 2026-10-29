# Swarm Wiki compiler pin (RA-CP-004)

Status: **pinned and exercised in CI with synthetic inputs only. No Hindsight, Kanban board or wiki on a real host was read or written.** The deployed plugin and wiki are unchanged.

Code: `automation/context/swarm_wiki.py`. Tests: `automation/tests/test_swarm_wiki.py`. CI: `.github/workflows/swarm-wiki-compiler.yml`. Snapshot export: [memory interface, RA-CP-004 section](memory-interface.md#snapshot-export-for-the-swarm-wiki-ra-cp-004).

## The pin

| | |
| --- | --- |
| Repository | `raelldottin/hermes-swarm-protocol` |
| Commit | `b7ab59c097be475f7674c746120c762544cc4b65` (`main` after raelldottin/hermes-swarm-protocol#7) |
| Entry point | `ops/build_wiki.py` |
| Compiler dependency | `ruamel.yaml` 0.19.1, for ByteRover frontmatter, installed in the compiler's own interpreter |

This commit adds authorized Hindsight snapshot ingestion and keeps ByteRover `.brv/context-tree` input. The pin lives in two places: `COMPILER_COMMIT` in the module and `ref` in the workflow. A test fails if they differ, so a new revision moves both in one reviewed change.

## Build and check

```bash
python -m automation.context.swarm_wiki build --compiler <checkout> --python <compiler interpreter> \
  --evidence <evidence.json> -- --board <board> --project <project> --memory <snapshot dir> \
  --memory-bank <bank> [other ops/build_wiki.py arguments]
python -m automation.context.swarm_wiki check --compiler <checkout> ... -- <the same arguments>
```

- **Only the pinned bytes.** Before it runs anything, the wrapper requires `<checkout>` to be a git checkout whose `HEAD` is the pinned commit, with no modified or untracked files. Anything else is refused, and the compiler is never started.
- **Arguments pass through.** Everything after `--` goes to `ops/build_wiki.py` unchanged. The subcommand chooses `--check`, so the operator may not pass it.
- **Evidence.** The output is JSON with:
  - the pin;
  - the mode and arguments;
  - the compiler's exit code;
  - its JSON report (`pages`, `findings`, `unresolved_findings`, `matches`);
  - each finding it printed, and any other error lines.

  The wrapper exits with the compiler's own code: `0` clean, `1` findings unresolved or the published wiki doesn't match, `2` an operational error. A refusal from the wrapper is also `2`, printed as `stop: …`.
- **Read-only.** The wrapper writes nothing except the evidence file. The compiler reads the board, tree and snapshots and publishes only to its `--out`.
- **Not coupled to agent execution.** Nothing in the supervisor, `run_next` or the context bundle imports it. An operator runs it, and so does CI.

## What CI demonstrates

The `Pinned Swarm Wiki compiler` job checks out the pinned commit and runs `automation/tests/test_swarm_wiki.py` against it. The fixture is synthetic: one Kanban task, one ByteRover node, one source file, and a snapshot written by `memory.write_snapshot`. The job shows that:

- a Hindsight-backed build publishes a memory page labelled `UNVERIFIED`, and keeps the ByteRover knowledge page;
- `check` against the published wiki matches;
- the snapshot files are unchanged afterwards;
- a memory with missing provenance is reported as `invalid-provenance`;
- an edited snapshot is reported as `changed-snapshot`, and its text is not projected.

The synthetic ByteRover node is linked to no Kanban work, so the compiler reports it as `orphan-topic` and exits `1`. The test expects exactly that finding and requires no finding against the snapshot. The job then runs the compiler's own wiki acceptance tests at the pin.

The job is not one of the required checks on `main`. Whether to make it one is the owner's decision.

## Performance evidence

This is not a performance-critical path. An operator or CI runs it by hand. It sits on no supervisor, policy or context-building path, and it adds no code to any registered path.

## Not done here

A live demonstration on real Hindsight data. That needs Hindsight installed, which is an owner decision, followed by a capture with `python -m automation.context.memory snapshot` and a build with this wrapper.
