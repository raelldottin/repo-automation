# Effectiveness Benchmark

The effectiveness benchmark measures how good the harness is at producing *working*
code — not how fast its functions run (that is the CodSpeed micro-benchmark). It uses
[ProgramBench](https://github.com/facebookresearch/ProgramBench): the harness rebuilds
real programs from scratch and is scored by ProgramBench's black-box test suites.

## What it measures

For each ProgramBench instance the harness attempts a rebuild; ProgramBench then runs
the program's hidden test suite. The report (`effectiveness-report.json`) aggregates:

- **resolve rate** — fraction of instances where every test passed (`pass_fraction >= 1.0`).
- **near-resolve rate** — fraction with `pass_fraction >= 0.95`.
- **mean pass fraction** — average test pass fraction across instances.
- **error counts** — ProgramBench `error_code`s (e.g. `copy_executable_failed`), per instance.

## Architecture

| Component | Role | Containers? |
|-----------|------|-------------|
| `automation/benchmark/adapter.py` | Drives the supervisor loop (`run_next` + `run_agent.sh`) to rebuild a program in a local workspace and archive it as `submission.tar.gz` | No |
| `automation/benchmark/evalrunner.py` | `ProgramBenchEvalRunner` shells out to `programbench eval` (authoritative test run) | **Yes** (amd64) |
| `automation/benchmark/scoring.py` | Reduces eval JSON to the effectiveness report | No |
| `automation/benchmark/run.py` | CLI + orchestrator (`run` / `eval` / `score` / `all`) | only `eval`/`all` |

The agent rebuilds on the local filesystem; ProgramBench's cleanroom Docker images are
used only by the `eval` step, so everything except the authoritative test run is
container-free and unit-tested with no Docker or LLM.

## Running it

### Score existing eval output (no Docker)

```shell
uv run python -m automation.benchmark score --run-dir path/to/run-dir
```

### Full run (rebuild → eval → score)

`eval` needs Docker and the amd64 cleanroom images, so run it on a native x86_64 Linux
host (or in CI). Point the agent at an OpenAI-compatible LLM — e.g. NVIDIA:

```shell
export REPO_AUTOMATION_AGENT_RUNNER=codex
export OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1
export OPENAI_API_KEY="$NVIDIA_API_KEY"

uv pip install programbench
uv run python -m automation.benchmark all --run-dir out --all          # full set
uv run python -m automation.benchmark all --run-dir out --instances abishekvashok__cmatrix.5c082c6
```

> On Apple Silicon the cleanroom images run only under slow amd64 emulation; prefer an
> x86_64 host for real runs.

## In production (CI)

`.github/workflows/effectiveness-benchmark.yml` runs the full set on `ubuntu-latest`
(native x86_64) on a nightly schedule and on manual dispatch. It requires the
repository secret **`NVIDIA_API_KEY`** and uploads `effectiveness-report.json` plus the
per-instance `*.eval.json` files as a build artifact. The workflow is repo-specific and
is not part of the reusable sync manifest.

## Reading the report

`effectiveness-report.json` holds the aggregate metrics and a per-instance breakdown.
The `score` / `all` commands also print a summary table:

```
Harness Effectiveness (ProgramBench)
  instances        : 1
  resolved         : 0 (0.0%)
  near-resolved    : 0 (0.0%)
  mean pass frac.  : 0.0%
  errors           : copy_executable_failed=1
```

## The A–E lane experiment

The benchmark above answers "how good is the harness?". The lane experiment answers the
question that actually decides what we build next: **which context-engineering strategy
makes the agent better?**

Five lanes attempt the *same* instances. Lanes differ only in how context and workflow
are treated — the model, tool authority, sandbox, workspace, agent command, total time
budget, ProgramBench evaluator and scoring code are identical across all of them.

| Lane | Treatment |
|---|---|
| **A** | One session, raw objective. No harness context, no phases. |
| **B** | The shipped harness: one bounded slice, `base.md` + `slice.md`. **Control.** |
| **C** | Fresh Research → Plan → Implement sessions, passing typed JSON artifacts. |
| **D** | C, with each artifact intentionally compacted before the next phase. |
| **E** | D, with the canonical J-Space skill administered inside each phase. |

Because each lane adds exactly one treatment to the one before it, the differences
decompose:

```text
B - A  = value of the harness's bounded context
C - B  = value of RPI
D - C  = value of intentional compaction
E - D  = value of J-Space
```

Only adjacent lanes are compared. `E - A` would measure four changes at once.

### Lane E: the canonical J-Space artifact

`E - D` is only a claim about J-Space if lane E receives J-Space itself rather than our
summary of it, so the skill text is never vendored into this repository or paraphrased in
`strategies.py`. `automation/benchmark/jspace.lock.json` pins the source repository, the
revision, the artifact path and the SHA-256 of its bytes; the operator supplies a checkout:

```shell
git clone https://github.com/Tiger3807861189/J-Space-Cognition-Suite /path/to/j-space
git -C /path/to/j-space checkout <revision-from-jspace.lock.json>
export JSPACE_ROOT=/path/to/j-space
```

At lane-E construction the harness verifies that `JSPACE_ROOT` is a Git checkout sitting
at the pinned revision and that the artifact hashes to the pinned digest, then injects
that text verbatim — binding only the two names the skill leaves open, `<skill-root>` and
`<python-command>`. The resolved identity is written into each cell's `run.json` under
`strategy.jspace` and echoed in `lane-comparison.md`, so every result states which bytes
produced it.

Resolution is fail-closed. A missing `JSPACE_ROOT`, a wrong revision, a moved artifact or
a hash mismatch skips every lane-E cell with the reason recorded in `run.json`; lane E
never silently degrades into lane D under E's name. Lanes A–D are unaffected and still run.

Updating the pin is a deliberate act: change the revision and hash together in the lock
file, in a commit that says why. Results produced under different pins are not comparable.

### Running it

```shell
# Small first: the default smoke instance, every lane, twice. No Docker.
uv run python -m automation.benchmark lanes --run-dir out --repeats 2

# With authoritative scoring (Docker, amd64):
uv run python -m automation.benchmark lanes --run-dir out --repeats 2 --eval

# Score/compare a matrix that was produced elsewhere:
uv run python -m automation.benchmark compare --run-dir out --repeats 2
```

Cost scales as `lanes × repeats × instances` agent runs, and C–E use three to five agent
sessions each. Start with a few instances before spending money on the full matrix.

### Layout

```text
out/<lane>/r<repeat>/<instance>/submission.tar.gz   graded artefact
out/<lane>/r<repeat>/<instance>/run.json            provenance + process metrics
out/<lane>/r<repeat>/<instance>/rpi/                phase artifacts (C–E)
out/<lane>/r<repeat>/effectiveness-report.json      ProgramBench score for that cell
out/lane-comparison.json | lane-comparison.md       the comparison
```

Each `<lane>/r<repeat>` directory is exactly the shape `programbench eval` already
expects, so scoring is the same code the single-lane benchmark uses.

### What is held fixed, and why it matters

Lanes are interleaved per instance (rotated deterministically) rather than run
lane-by-lane, so provider load or time of day cannot masquerade as a lane effect.

The time budget is per *instance*, not per session: a three-session lane must not get
three times the wall clock of lane A, or it wins on budget rather than on treatment.

Phase artifacts live in `<workspace>/.rpi/` and are excluded from the submission archive,
so what ProgramBench grades is the same kind of thing in every lane.

### Reading the comparison

`lane-comparison.md` reports four groups. **The primary metric is ProgramBench
correctness.** A lane that saves context but loses resolve rate is worse.

- **primary** — resolve rate, near-resolve rate, mean pass fraction
- **efficiency** — wall clock, agent invocations
- **process** — phase failures, non-zero exits, compression ratios
- **stability** — standard deviation across repeats

One repeat per cell is too noisy to interpret; use at least two while developing the
instrumentation and at least three before believing a result.

### Known limits

- **ProgramBench is greenfield.** The workspace starts empty, so lane C's Research phase
  studies the program's observable contract rather than an existing codebase. `C - B`
  therefore measures RPI's value for requirements analysis, *not* for brownfield code
  exploration, which is the case RPI was designed for. A brownfield fixture is needed
  before generalising the result.
- **Tokens are not observable.** The agent runner returns an exit code, so efficiency is
  measured in wall clock and agent invocations only.
- **Lane E needs a J-Space checkout to run at all.** It is skipped, not approximated,
  when the pinned artifact is unavailable (see below). A skipped lane has no outcome; do
  not read its zeroes as a treatment effect.
- **No propose-only lane.** A pure-agent lane would change execution authority and Git
  semantics, not just context treatment, so it would confound this experiment. Test it
  against whichever of A–E wins.
