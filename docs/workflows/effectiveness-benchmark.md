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
host (or in CI).

The benchmark drives Hermes, which reaches NVIDIA directly: provider and model are
first-class flags, so nothing has to be translated through a second vendor's config file
or wire protocol. `REPO_AUTOMATION_AGENT_RUNNER=codex|claude` still work; they are just
more setup for the same thing.

```shell
export REPO_AUTOMATION_AGENT_RUNNER=hermes
export HERMES_INFERENCE_PROVIDER=nvidia
export HERMES_INFERENCE_MODEL=moonshotai/kimi-k3
export NVIDIA_API_KEY="$NVIDIA_API_KEY"
```

The wrapper reproduces the deployed Hermes posture and then subtracts what would
contaminate a lane. It deliberately does **not** pass `--safe-mode`. That flag sets three
independent controls at once, and one of them — `HERMES_IGNORE_USER_CONFIG` — discards
`config.yaml` and falls back to Hermes' built-in defaults. A benchmark run on those
defaults measures shipped Hermes, not the harness anyone operates:

| control | how | what it removes |
|---|---|---|
| `HERMES_SAFE_MODE=1` (env) | set by the wrapper | plugins, MCP servers, outbound webhooks, shell hooks |
| `--ignore-rules` | flag | `AGENTS.md`, `SOUL.md`, `.cursorrules`, memory, preloaded skills |
| `--toolsets` | flag | 51 of the 59 default tools, including `delegate_task`, `memory`, `session_search` |
| throwaway `HERMES_HOME` | per invocation | prior sessions and memories |
| `automation/supervisor/hermes-benchmark.yaml` | copied into that home | *kept*: the deployed Kanban posture, narrowed to one worker |

`HERMES_IGNORE_USER_CONFIG` is left unset, so the config profile loads. Its identity is
recorded as `config_profile` and `config_sha256` in each session's controls report.

A lane that spawned child agents would not be running the session the lane administered,
and a lane that wrote memories would hand the next cell context the treatment never gave
it. For the same reason each invocation gets a throwaway `HERMES_HOME`: sessions and
memories are per-home, and a measurement of what compaction drops is worthless if the
dropped context can be recalled.

Because the home is thrown away, it carries no `.env` — **credentials must come from the
environment**, not from `~/.hermes/.env`.

Hermes ignores config keys it does not recognise, so a key renamed upstream would leave a
control at its default while `run.json` claimed otherwise. The workflow checks every key in
the profile against the pinned revision's own `DEFAULT_CONFIG` before spending any budget.

Declared controls are not evidence, so each agent session also writes a usage report next
to the submission, under `agent-sessions/`, and every cell's `run.json` records both:

```text
agent_sessions[].usage      model and provider that served each turn, tokens, api_calls
agent_sessions[].controls   revision, config profile + hash, toolset, HERMES_HOME
```

`usage.auxiliary.api_calls` is the one that catches a second model answering alongside the
lane. The profile turns off background review and the title-generation model upgrade, so
a one-turn session reports zero — which makes it evidence rather than an assumption.

Check the agent before spending a matrix on it — a provider it cannot talk to turns every
cell into an agent error that looks like a harness failure. Use the agent itself rather
than a hand-written HTTP probe, which only proves whatever protocol the probe chose; one
such probe returned 200 while every agent session was failing:

```shell
REPO_AUTOMATION_HERMES_USAGE_DIR=/tmp/preflight automation/supervisor/run_agent.sh \
  --repo-root /tmp/scratch-checkout --prompt-file /tmp/prompt.md \
  --context-file /tmp/context.json --handoff-file /tmp/handoff.json --slice-id preflight
```

Drive the wrapper rather than `hermes` directly: that is what a cell runs, so it exercises
the config profile, the toolset pin and the throwaway home along with transport and auth.

`--usage-file` names the model and provider that actually served the turn. Compare it with
what you asked for: a silent substitution turns a lane comparison into a comparison between
two different models.

```shell
uv pip install programbench
uv run python -m automation.benchmark all --run-dir out --all          # full set
uv run python -m automation.benchmark all --run-dir out --instances abishekvashok__cmatrix.5c082c6
```

> On Apple Silicon the cleanroom images run only under slow amd64 emulation; prefer an
> x86_64 host for real runs.

Eval containers are given the host's CPU count, capped at 10. Docker refuses a container
that asks for more CPUs than exist, which fails every container and produces no results,
so `--docker-cpus` only ever lowers that default.

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

Verify a checkout before spending a run on it:

```shell
uv run python -c "from automation.benchmark import jspace; print(jspace.resolve().provenance())"
```

The benchmark workflow does this for itself: when a dispatch includes lane E it clones the
pin out of the lock file, exports `JSPACE_ROOT`, and fails the job if the artifact does not
resolve — so a misconfigured run stops before any agent budget is spent rather than
producing an A–D experiment labelled A–E.

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
out/<lane>/r<repeat>/<instance>/agent-sessions/     per-session usage + controls
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

A session that runs the budget out is killed, with its tool subprocesses, and the phase is
recorded with returncode 124 — the same code `timeout(1)` reports. The cell counts as a
failure and the matrix continues; one slow cell does not take the finished lanes with it.
Repeated 124s mean the budget is too small for the instance, not that the lane lost.

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
- **Tokens are recorded but not compared.** Hermes sessions write token counts into
  `run.json`; the comparison still reports wall clock and agent invocations only, because
  the reported metric has to mean the same thing for every runner the seam accepts.
- **Lane E needs a J-Space checkout to run at all.** It is skipped, not approximated,
  when the pinned artifact is unavailable (see below). A skipped lane has no outcome; do
  not read its zeroes as a treatment effect.
- **No propose-only lane.** A pure-agent lane would change execution authority and Git
  semantics, not just context treatment, so it would confound this experiment. Test it
  against whichever of A–E wins.
