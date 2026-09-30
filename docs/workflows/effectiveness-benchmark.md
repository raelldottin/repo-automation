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
| `automation/benchmark/cleanroom.py` | Materializes ProgramBench's inference cleanroom into the cell workspace and proves what is in it | **Yes** (amd64) |
| `automation/benchmark/run.py` | CLI + orchestrator (`run` / `eval` / `score` / `all`) | only `eval`/`all`, and `--cleanroom` |

By default the agent rebuilds on the local filesystem and Docker is used only by the
`eval` step. With `--cleanroom` the agent's own tools run inside ProgramBench's cleanroom
image too — the inference environment the benchmark is supposed to measure, described
below. Either way the scoring, orchestration and adapter code is unit-tested with no
Docker and no LLM.

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

uv run python -m automation.benchmark all --run-dir out --all --cleanroom \
  --agent-env NVIDIA_API_KEY --agent-env NVIDIA_BASE_URL
```

The first four are read by `run_agent.sh` and inherited automatically. `NVIDIA_API_KEY` is
not, which is what `--agent-env` is for — see below.

### What a session inherits

A cell exists to run model-authored commands. It used to be launched with the whole
launcher environment, so every session was handed whatever the operator happened to have
exported: provider keys for other vendors, `GH_TOKEN`, cloud credentials, `SSH_AUTH_SOCK`.
None of that rebuilds a C repository.

A session now inherits exactly the variables `automation/supervisor/run_agent.sh` reads,
listed by literal name in `adapter.INHERITED_ENV_NAMES`:

```text
PATH  HOME  TMPDIR  LANG  LC_ALL
REPO_AUTOMATION_AGENT_RUNNER  REPO_AUTOMATION_CODEX_BIN  REPO_AUTOMATION_CLAUDE_BIN
REPO_AUTOMATION_CLAUDE_PERMISSION_MODE  REPO_AUTOMATION_HERMES_BIN  OWLORY_CODEX_BIN
HERMES_REVISION  HERMES_INFERENCE_PROVIDER  HERMES_INFERENCE_MODEL
CLAUDECODE  CLAUDE_CODE  CLAUDE_CODE_ENTRYPOINT
```

Names, never patterns. `REPO_AUTOMATION_*` would ship the next variable somebody adds under
that prefix whatever ends up in it; the property worth having is that a variable nobody
considered is **absent**. `TERMINAL_CWD` and `HERMES_HOME` are deliberately not on the list:
the runner sets both per invocation, and inheriting either would point a session at the
previous cell's state.

Anything else a session needs is authorized one name at a time:

```shell
--agent-env NVIDIA_API_KEY --agent-env NVIDIA_BASE_URL
```

`--agent-env` takes a **variable name**, never `NAME=value` — a value on the command line is
readable in any process listing on the host, and in CI it lands in the job log. A name that is
not set in the environment is refused before the first agent launches, because a run that
reached the provider unauthenticated would score the outage as a lane effect.

**No refusal repeats any part of what you supplied.** Every one of them names the offending
argument by its position — `--agent-env argument #2 is not a variable name` — and nothing
else. Two earlier versions tried to quote back something safe, first the part before `=` and
then the leading variable-name prefix, and each leaked in turn: a credential put where a name
belongs is indistinguishable from a name by shape alone. If you need to know which argument,
count it on your own command line. The flag is
repeatable and applies to `run`, `all`, `lanes` and `probe`; in `lanes` every lane gets the same
selection, so what a lane can reach is not one of the things that varies between lanes.

Programmatic callers pass the same thing as `SupervisorAgentAdapter(env=...)`. That mapping
is the caller's authorization, so it is forwarded as given and wins over an inherited value
of the same name.

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
| `TERMINAL_CWD=<workspace>` (env) | set by the wrapper | work landing outside the cell's workspace |
| `automation/supervisor/hermes-benchmark.yaml` | copied into that home | *kept*: the deployed Kanban posture, narrowed to one worker |

`TERMINAL_CWD` is not decoration. `--in DIR` changes the process directory, but the
terminal, file and code_execution tools resolve their own working directory and prefer
`TERMINAL_CWD` to it. Left unset, a session writes outside the checkout it was handed:
the cell archives an empty workspace, ProgramBench scores it `compile_failed`, and the
next cell opens on top of the previous one's files. That is a silent result, not an
error, which is why the preflight now makes the agent write a file and checks where it
landed before any budget is spent.

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

## The inference environment (`--cleanroom`)

ProgramBench is a *rebuild* benchmark: the model gets a compiled program and whatever
documentation ships beside it, and has to produce sources that compile to a binary passing
the program's own black-box tests. Everything about that task is destroyed by an agent that
can read the upstream repository, so the environment is part of the measurement, not
plumbing around it. `--cleanroom` makes the cell run inference in ProgramBench's own
`task_cleanroom_v6` image instead of an empty local directory, and refuses the cell if the
environment does not match the contract.

What each cell does before its first agent turn (`automation/benchmark/cleanroom.py`):

1. Resolves the official image for the instance —
   `programbench/INSTANCE:task_cleanroom_v6`, where `INSTANCE` is the instance id with
   its `__` rewritten as `_1776_` (Docker repository names cannot hold `__`) — and records
   the image id, so `run.json` names the bytes that were used, not a tag that can move.
2. Copies the image's entire `/workspace` into a cell workspace that must be **empty**
   first: the reference `./executable`, the bundled documentation, and the one-commit Git
   repository the image ships, nothing curated, nothing added. A local cell gets a
   `git init` because `run_agent.sh` requires a worktree at the repo root; a cleanroom
   cell does not, and is refused if the image ships none. The harness does not create the
   environment it is supposed to be measuring.
3. Runs a probe container from that same image, on that same workspace, with
   `--network=none`, and reads back what it found: the executable is executable, the
   workspace is writable and a worktree, and `curl`, `wget`, `git ls-remote` and
   `getent hosts` all fail.
   The network mode is then read off the container that ran the probes rather than
   trusted from the flags that were passed to it.
4. Refuses the cell — `CleanroomError`, before any tokens are spent — on a non-empty
   destination, a missing reference binary, absent documentation, a missing Git worktree,
   a container that is not `NetworkMode=none`, any egress attempt that *succeeded*, or an
   image too thin for any probe to run at all.
   A cleanroom that could not be tested is not a tested cleanroom.

The receipt lands next to the submission as `cleanroom.json` and inside each lane cell's
record, so a scored result can be checked afterwards against the environment that produced
it.

The agent's session is then held in the same image. `run_agent.sh` appends
`automation/supervisor/hermes-cleanroom.yaml` to the Hermes profile and pins
`terminal.docker_image` to the instance's cleanroom image; at the pinned revision the
terminal, file and `code_execution` tools all route through one `docker exec` container,
so the three tool families the model is given share a single air-gapped workspace by
construction. `docker_network: false` is `--network=none`.

`docker_persist_across_processes: false` is what actually keeps one cell out of another
cell's container. At the pinned revision Hermes only forces per-session containers when a
session key is present, and a CLI session has none — its task id stays `default`, so the
configured value reaches `DockerEnvironment`. Reuse is then matched on task, profile and
egress labels plus the network mode, never on the image and never on the bind mount, and
an existing `--network=none` container is deliberately *kept* under `docker_network:
false`. Without this key a cell could attach to the previous cell's container, still
holding the previous instance's image and workspace mount. The key is absent from the
pinned `DEFAULT_CONFIG` but honoured through `TERMINAL_CONFIG_ENV_MAP`, which the CI
config check now knows about.

Configured is still not proved, so a **sandbox witness** watches from outside while the
session runs. It baselines the `hermes-agent=1` containers before the cell, polls for
every container that appears during it, and the moment one does writes
`agent-sessions/<session>.sandbox.json`. One record per container — task id, container id,
creation time, requested image, actual image id, `NetworkMode`, the `/workspace` mount's
source and rw flag, Hermes's own labels, `existed_in_baseline`, and `removed_after_exit` —
under one receipt:

```json
{"observed": true, "default_backend_verified": true, "session_backend_verified": true,
 "default_backend_removed_after_exit": true, "existing_containers": [],
 "containers": [{"task_id": "default", "...": "..."}]}
```

The witness also *removes* what a killed session left behind. A cell killed at its budget
ceiling takes Hermes's own cleanup with it, so the container it was working in survives,
along with whatever its last tool call started inside — a compile still burning the
runner's CPU while the next phase is measured on it. At the exit of **each** agent
invocation, not at the end of the lane, the witness issues `docker rm -f` against the
container ids **it saw**, never against a `hermes-agent=1` query: a broad sweep would also
take containers that were present before the cell and containers another cell is using.
Both facts survive in the record, since `removed_after_exit` is the only direct evidence
of `docker_persist_across_processes: false` there is:

| Hermes | `removed_after_exit` | `cleanup_attempted` | `removed_after_cleanup` |
| --- | --- | --- | --- |
| exited normally | `true` | `false` | `null` |
| was budget-killed | `false` | `true` | `true` |
| left something unremovable | `false` | `true` | `false` → **cell refused** |

Every container, not the first: Hermes builds one backend per `task_id`, and there are
three kinds. The system prompt's own probe gets `prompt-backend-probe`. The CLI parent's
tool calls get `default`. Every tool call *inside an agent turn* gets a session-scoped id,
because `container_persistent: false` keys the backend by session — and that third kind
reaches the mount logic by a different path, which is why the receipt scores it
separately. First-container-wins recorded the system prompt's and hid both others.
Written mid-session on purpose — a
cell killed at its budget ceiling takes its process group with it, and the container is
gone once it exits. The container's environment is never inspected, so the provider key
cannot reach the receipt.

The `default` backend is created by the model's *first tool call*, so a cell that spends
its budget before making one proves nothing about it. A **pre-budget default-backend
probe** asks directly instead, with no model in the loop: `run_agent.sh --sandbox-probe`
composes the same `HERMES_HOME/config.yaml`, exports the same `TERMINAL_CWD` and the same
sandbox image the session will get, then runs, under Hermes's own interpreter,
`apply_terminal_config_to_env()` followed by
`terminal_tool("pwd && test -x ./executable && test -f README.md", ...)` twice: once with
`task_id=None`, once with a fresh `probe-<uuid>`, so both the `default` backend and the
session-scoped backend an agent turn uses are exercised. Going through the config file is
the point: that bridge is where `docker_persist_across_processes` takes effect. Its
receipt and output land in `agent-sessions/sandbox-probe.sandbox.json` and
`agent-sessions/sandbox-probe.log`.

The cell is refused before the budget is spent unless both probes exit 0 and the witness
saw, for each of the two backends, a container with the cleanroom image id,
`NetworkMode=none`, `/workspace` bound rw from this cell's own `pb-*` workspace, and
`existed_in_baseline: false` — and unless every container it saw is **gone** from `docker
ps -a --filter label=hermes-agent=1` once the probe process exits. That last check is the
runtime reading of `docker_persist_across_processes: false`: a container the daemon still
lists is one the next process attaches to by label, whatever the config file says.

Probing both backends is not belt-and-braces. Run 36485906813 passed every `default` check
and still handed the model an empty tmpfs: `docker_mount_cwd_to_workspace` derives the
bind from `TERMINAL_CWD`, and the pinned revision's `_resolve_task_host_cwd` deliberately
refuses to do that for a session-scoped container, since the env var outlives the session
that set it and reusing it could leak the previous session's directory. `run_agent.sh`
therefore appends an explicit `docker_volumes: ["<workspace>:/workspace"]` to the composed
config, which applies to every task id, and the probe now covers the path that caught it.

The same refusal applies to the session's own witness: a container running another image,
not `NetworkMode=none`, with `/workspace` from somewhere other than that cell's workspace
or mounted read-only, or no container at all while one of Hermes's was already present —
the shape of a reused container. A session that simply never called a tool creates none,
which is a fact about the model's turn rather than the sandbox: that records
`{"observed": false}` and scores normally.

Credentials stay host-side: `NVIDIA_API_KEY` is read by the Hermes process, which runs
*outside* the sandbox, and `docker_forward_env: []` / `docker_env: {}` keep the container's
environment empty of it. The controls receipt records `credentials_forwarded: []` for this
reason — the model-facing tools never see the key, and `run.json` never records it.

Submissions are archived from the workspace afterwards, host-side, with `.git`, the
harness's own `rpi/` directory and the reference `executable` excluded: the binary the
agent was given back is not part of what it built. Excluding `.git` matches ProgramBench's
own submission packer, and matters for scoring: the evaluator seeds a synthetic repository
with fixed identity and fixed dates *only if the submission shipped none*, so a submission
carrying its own `.git` would make build scripts that embed a commit SHA produce a
different binary hash on every run. Evaluation is unchanged — the official
ProgramBench evaluator, in the official `task_v6` eval image.

Requirements: Docker and an x86_64 host (the images are linux/amd64 only). Without
`--cleanroom` the agent runs against a local checkout, which is useful for harness
development and is not a ProgramBench inference result.

One instance's environment can be checked on its own, without spending any agent budget:

```shell
uv run python -m automation.benchmark.cleanroom \
  --instance abishekvashok__cmatrix.5c082c6 \
  --workspace /tmp/cleanroom --receipt /tmp/cleanroom.json
```

## Upstream identity the harness adds

The cleanroom stops the agent *fetching* the upstream source. It does not stop the agent
*recalling* it, and a model that has read `abishekvashok/cmatrix` does not need to fetch
anything. So the harness adds no upstream repository identity, instance identity, commit,
or implementation-language hint of its own. Nothing agent-facing that we author carries
them:

- `TaskSpec.objective` — "Rebuild the program in this workspace from scratch…". The
  language went with the name: the submission only has to produce a `compile.sh` that
  builds an `./executable`, so the original language was never a requirement, only a hint
  about the upstream source.
- The prompt envelope every phase of every lane repeats — one `- Task:` line.
- The phase headings (`# Research:`, `# Plan:`, `# Implement:`, `# Rebuild`).
- `build_slice_record`'s `slice_id` and `title`, which `summarize_slice` renders into the
  prompt for the lanes that get harness context.
- The cell's workspace directory name, which is the agent's own cwd outside a cleanroom.
- The `--slice-id` handed to `run_agent.sh`, which it exports as
  `REPO_AUTOMATION_SUPERVISOR_SLICE_ID` and `OWLORY_SUPERVISOR_SLICE_ID`. The pinned Hermes
  path renders neither into a prompt, but the worker process has no use for the real name.

What they carry instead is `TaskSpec.public_id`: `task-` plus twelve hex characters of
`sha256(instance_id)`. Stable, so the same task is recognisable across lanes and repeats
in a transcript.

**Opaque in a prompt, not anonymous.** ProgramBench's instance catalogue is public and
small; anyone holding it can hash every entry and invert this in a second. It is not an
anti-memorization measure — that would need a run-scoped random or HMAC id, which would
also cost cross-run comparability. It is the harness declining to put the name in front of
the model.

`instance_id` is unchanged everywhere the agent does not read — run directories, `run.json`,
`cleanroom.json`, the scoring report, and the image name the cleanroom is built from. The
mapping stays in the receipts; it just never reaches the model.

### What the benchmark itself still supplies

This control covers what the harness adds, and stops there. The cleanroom bundles the
program's own documentation, because rebuilding from observable behaviour and docs is the
task — and for `abishekvashok__cmatrix.5c082c6` that documentation is a `README.md`
beginning "CMatrix" and linking `https://github.com/abishekvashok/cmatrix`, plus a
`cmatrix.1` man page naming the program. An agent that reads them knows what it is
rebuilding.

Sanitizing them is not on the table: it would stop this being ProgramBench's inference
environment and make it a new benchmark variant. What the preflight does instead is
**record** the other provenance signal it can see. The probe reads the image's own Git
worktree and writes any remotes into `cleanroom.json`:

```json
"git_remotes": [{"name": "remote.origin.url", "url": "https://github.com/…"}]
```

Read, never acted on — the worktree is ProgramBench's, and the harness does not edit an
environment it is administering. An empty list is a real answer, not a missing one.

## In production (CI)

`.github/workflows/effectiveness-benchmark.yml` runs the full set on `ubuntu-latest`
(native x86_64) on a nightly schedule and on manual dispatch. It requires the
repository secret **`NVIDIA_API_KEY`** and uploads `effectiveness-report.json` plus the
per-instance `*.eval.json` and `cleanroom.json` files as a build artifact. Benchmark and
lane runs pass `--cleanroom`. Three things are proved before any agent budget is spent:
every key in both Hermes profiles resolves against the pinned revision's own defaults
(Hermes ignores keys it does not know, and a silently ignored `docker_network` is an agent
with internet access), one trivial agent session completes and writes where it was told to,
and one instance's cleanroom materializes and fails all four egress probes. That last check
writes its receipt to `out/preflight/cleanroom.json`, so the artifact carries it even when
no cell ran.

The model id is pinned in the job environment, not read straight off `inputs.model`: the
`inputs` context is populated for `workflow_dispatch` only, so a scheduled run does not
inherit the dispatch input's default and would otherwise start with an empty model id.

Dispatching with **`preflight_only`** runs the deterministic checks and stops: the config
check, the cleanroom materialization and its egress probes, and nothing that spends a model
turn - not the agent session check, not `benchmark all`, not `benchmark lanes`, not the
diagnostic probe. It is the cheap way to read what a given instance's cleanroom exposes,
including the `git_remotes` the preflight records (see
[What the benchmark itself still supplies](#what-the-benchmark-itself-still-supplies)).

The workflow is repo-specific and is not part of the reusable sync manifest.

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
uv run python -m automation.benchmark lanes --run-dir out --repeats 2 \
  --agent-env NVIDIA_API_KEY --agent-env NVIDIA_BASE_URL

# With authoritative scoring (Docker, amd64):
uv run python -m automation.benchmark lanes --run-dir out --repeats 2 --eval \
  --agent-env NVIDIA_API_KEY --agent-env NVIDIA_BASE_URL

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
out/<lane>/r<repeat>/<instance>/agent-sessions/     per-session usage + controls + log + sandbox
out/probe/probe.json | probe/cleanroom.json | probe/agent-sessions/*.stream.jsonl
out/probe/agent-sessions/*.usage.json                 --oneshot only; capped runs read state.db instead
                                                    diagnostic only, not a cell
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

Within a multi-phase lane the budget is divided by fixed ceilings, not spent from one
shared remainder, and the profile is recorded in `run.json` as `phase_budgets` because it
is part of the treatment:

| phase | seconds | C | D | E |
|---|---:|:-:|:-:|:-:|
| `research` | 600 | ✓ | ✓ | ✓ |
| `research_compact` | 300 | – | ✓ | ✓ |
| `plan` | 300 | ✓ | ✓ | ✓ |
| `plan_compact` | 300 | – | ✓ | ✓ |
| `implement` | 1800 | ✓ | ✓ | ✓ |

The ceilings are written against a 3300-second lane; a different `--timeout` scales them
proportionally. Lanes A and B are one session and get the whole instance budget. C spends
2700 of the 3300 and forfeits the 600 the compaction slots cost D and E.

This profile replaced a 2700-second one after run 35715428932, where it censored the
treatments it was supposed to measure: `research` hit its 420-second ceiling in all three
multi-phase lanes, `implement` hit its 1260-second ceiling in two of three, and lane E was
killed before it wrote `compile.sh` and scored `compile_failed`. The phases that finished
peaked near 229 seconds (`plan`) and 202 seconds (compaction), so 300 leaves both room;
`research` and `implement` get about 43% more than the ceilings they kept hitting.

Two things follow, and both were bought the hard way. Implement keeps its allowance no
matter how greedy research is: run 35650966066 handed each phase the whole remainder, lane
D spent 1799 of 1800 seconds researching, and no multi-phase lane submitted anything at
all. And C does not reclaim the 720 seconds D and E spend compacting — if it did, `D - C`
would measure compaction plus whatever C did with the extra time.

A session that runs the budget out is asked to stop (SIGTERM), given five seconds to flush
and exit, and then killed with its tool subprocesses (SIGKILL). The phase is recorded with
returncode 124 — the same code `timeout(1)` reports — and the grace is recorded separately
as `grace_seconds`, never charged to the phase. The cell counts as a failure and the matrix
continues; one slow cell does not take the finished lanes with it. Repeated 124s mean the
budget is too small for the instance, not that the lane lost.

A session killed at its ceiling archives a **0-byte log**, and no buffering setting
changes that. Under `--oneshot` Hermes redirects the turn's stdout *and* stderr to
`/dev/null` for the whole call tree and prints the final response only after the turn
returns, and it disables logging, so `$HERMES_HOME/logs/agent.log` holds nothing past
startup either. Runs 35715428932, 35736569601 and 35788842735 all recorded this;
`PYTHONUNBUFFERED=1` stayed in the Hermes branch of `run_agent.sh` only because it is
harmless. Where the evidence actually comes from is the diagnostic transport below.

### The diagnostic transport (`stream-json`)

`REPO_AUTOMATION_HERMES_TRANSPORT=stream-json` runs the session as
`hermes chat -q "$PROMPT" --format stream-json` instead of `hermes --oneshot "$PROMPT"`.
That path flushes one JSON object per event — `system/init`, text deltas, `tool_use`,
`tool_result` — so a session killed at its ceiling keeps everything it had already emitted.
The raw JSONL is archived untouched as `agent-sessions/<stem>.stream.jsonl`, with the
ordinary controls receipt beside it and `transport` recorded in it.

**This is a diagnostic, never a lane.** `hermes chat` is a different execution path inside
the agent, so a result produced on it is not comparable with an A–E cell measured on
`--oneshot`, and it carries no `--usage-file` receipt — that flag has no effect outside
`-z/--oneshot`, so the probe records no usage rather than an empty one that would read like
a session that spent nothing. What accounting the transport does give is the terminal
`result` event — exit code, wall duration and token counts — recorded as
`stream.terminal_result`. It is not provenance: that event's `model` field came back empty,
so nothing in a probe record may be read as evidence of which model served the turn. The
job-level preflight remains the check that compares served against asked. The default
transport stays `oneshot`; an unrecognised value is refused with exit 64 rather than
silently falling back.

```shell
uv run python -m automation.benchmark.probe \
  --instance abishekvashok__cmatrix.5c082c6 --out-dir out/probe \
  --agent-env NVIDIA_API_KEY --agent-env NVIDIA_BASE_URL
```

The probe reaches the same agent through the same shell as a lane, so it inherits the same
audited set of launcher variables and takes the rest one name at a time through the same
`--agent-env` resolver — a diagnostic does not get to hand an agent what a lane may not.
Being a diagnostic is why it needs the flag at all: with nothing selected it reaches the
provider unauthenticated.

It also runs **where a cell runs**. The probe materializes the same
`task_cleanroom_v6` workspace through `prepare()`, names the image in
`REPO_AUTOMATION_HERMES_SANDBOX_IMAGE`, runs the same `--sandbox-probe` proof of both
backends before it spends any budget, and wraps the session in the same `SandboxWitness`,
down to the exact-id cleanup on a timeout kill. Its workspace directory is named from
`public_id`, like a cell's, because the agent's shell prompt shows its own working
directory. The receipts land in `out/probe/cleanroom.json` and in the record's `cleanroom`
and `sandbox` keys. Without this the probe answered about a bare `git init` directory on
Hermes' local backend with no reference `./executable`, no bundled docs and no
`NetworkMode=none` — the environment PRs #55 and #56 retired — and a 600-second answer
about the wrong sandbox is worse than none.

Witness findings are **recorded, not raised**, which is the one place the probe departs
from a cell. A cell refuses on a violation because a violated sandbox makes its score not a
ProgramBench result; a probe has no score, and its timeline is the whole deliverable, so
discarding it over a finding about the environment would lose the finding too. The pre-turn
`--sandbox-probe` proof still refuses outright — that one costs nothing to repeat.
`--no-cleanroom` restores the old synthetic path for exercising the module without Docker;
its summary says `NO CLEANROOM` and its timings are not comparable to a lane's.

Unlike `--oneshot`, which sets `HERMES_YOLO_MODE=1` inside Hermes, the `chat -q` path takes
the unattended approval route, where `approvals.single_query_mode` defaults to `deny` and
`execute_code` answers `BLOCKED`. The launcher exports `HERMES_YOLO_MODE=1` for this
transport only, before the process starts — `tools/approval.py` freezes the value at import,
so a later assignment would not take — which removes that one avoidable difference. It does
not make the two transports identical. Catastrophic commands still stop at the hardline
floor, which runs ahead of the override, and the session is in a network-none disposable
container either way.

### First-turn accounting (`--max-turns`)

The transport above ruled execution latency out and cannot rule anything else in. Hermes'
stream-json emitter carries `system/init`, text deltas, `tool_use`, `tool_result` and the
terminal `result` and nothing else — its own `on_tool_progress` says reasoning progress is
not part of the protocol — and a reasoning model emits no text until it has finished
reasoning. So a phase that reasons through its whole ceiling and is killed before its first
visible token records exactly one event, which looks identical to a provider that never
answered. Run 36679174467 recorded that: init at ~6s, 599 seconds of silence, no tool call,
no artifact.

`reasoning_tokens` is what separates the two, and no single Hermes path hands over both the
cap and the count:

| | `--oneshot` (what a lane runs) | `chat -q --format stream-json` |
| --- | --- | --- |
| Turn cap | **ignored entirely** | `--max-turns`, consumed |
| `--usage-file` report | written on every exit path | not written |
| `reasoning_tokens` | in the report | in `state.db:session_model_usage` |

So the cap goes on the transport that consumes it, and the accounting is read from Hermes'
own session ledger.

```shell
uv run python -m automation.benchmark.probe \
  --instance abishekvashok__cmatrix.5c082c6 --out-dir out/probe \
  --transport stream-json --max-turns 1 --budget 1800 \
  --agent-env NVIDIA_API_KEY --agent-env NVIDIA_BASE_URL
```

**The pinned `--oneshot` path honours no turn cap at all**, so asking for one there is
refused rather than recorded. `hermes_cli/oneshot.py` constructs its `AIAgent` without
passing `max_iterations`, which leaves `run_agent.py`'s default of `sys.maxsize`; and all
three ways to set it — the `--max-turns` flag, `agent.max_turns` in `config.yaml`, and
`HERMES_MAX_ITERATIONS` — are read in `cli_init_mixin._init_turn_limits` on the chat path
that `--oneshot` bypasses ("Bypasses cli.py entirely"). A receipt reading `max_turns: 1` over
a session that ran to the wall clock is a mislabelled measurement, which is worse than none,
so both the launcher (exit 64) and the probe (`ValueError`) refuse the combination. On the
chat path the cap is passed as the flag, never written into `hermes-benchmark.yaml`, because
that file is the posture every A–E lane shares and a lane must not be capped at one turn.

Because the capped path writes no `--usage-file` report, the accounting comes from
`session_model_usage` in `$HERMES_HOME/state.db`, keyed on the session id the stream's own
`system/init` names. That table is written through `update_token_counts`, the chokepoint every
per-API-call delta flows through on every path, so a `chat -q` session lands there even
though the report does not. It is opened read-only, by URI: the harness is reading evidence
out of somebody else's database, not administering it. A fallback mid-session leaves a second
row; the counters are summed, the busiest route is named, and `routes` travels with them so a
blend is never read as one model's numbers. What the ledger does not carry is
`turn_exit_reason` — on this transport the stream's terminal `result` is what says how the
session ended.

Whichever source was used is recorded as `usage_source` and printed, next to
`elapsed_seconds` and a seconds-per-api-call line, because the finding is a ratio: ten
minutes spent emitting a large hidden reasoning trace and ten minutes spent waiting to start
a small one are the same wall clock. No accounting at all prints as `NO USAGE REPORT` rather
than being omitted — silence there would read as a turn that cost nothing.

Both mechanisms are proved before a probe run spends anything on them, in a CI step that
takes about a minute. It drives a prompt needing three sequential tool calls under
`--max-turns 1`: uncapped that is four or more API calls and two files, and capped it is the
one turn plus the single toolless summary call Hermes makes on the way out
(`agent/turn_finalizer.py:157`), with the third step never reached. So `api_calls <= 2` and a
missing second file are the cap being consumed rather than the model being brief, and a
ledger row carrying non-zero input and output tokens is the accounting source still being
where the probe reads it. Both have a silent failure mode worth a minute: a cap the transport
ignores produces a confident mislabel, and an accounting source that moved produces zero
tokens, which reads exactly like a stalled provider. The step is gated on the probe input, so
scheduled A–E runs are untouched.

One research phase at the lane's own 600-second ceiling. `out/probe/probe.json` holds the
phase result, the artifact and its schema errors, the tool-event timeline with offsets from
the first event, the terminal `result` accounting, the cleanroom and sandbox receipts, and
every event naming `research.json` at the offset of the event that named it; the printed
summary answers what
the model did first, whether it ever called a tool to write the artifact, when, and what the
tool said back. In CI it is the `probe` input on the workflow, which reuses the same Hermes
install and config-verification preflight and skips the matrix and eval entirely.

`out/probe/probe.json` and `out/probe/rpi/` are the probe's own outputs under the same
ownership rule as a phase artifact directory: it creates them or it refuses. A run directory
that already holds either is left byte-for-byte alone and the probe stops before spending the
phase budget, so a second probe into a directory that already answered the question cannot
destroy the answer it was run to re-check.

Symlinks in the agent's `.rpi` are **skipped, not followed** - including a `.rpi` that is
itself a symlink, which is checked before the directory is read at all. `Path.is_dir()`
follows links, so gating only on it and then filtering entries leaves the container
unguarded: every entry of the link target arrives as a real file and no per-entry check ever
sees a link. Entries that are not regular files are skipped too, which is the same rule and
also stops a directory in `.rpi` from aborting the copy and taking the already-collected
artifacts with it.

Hard links are skipped by the same predicate. A hard link is not a symlink and `is_file()`
is true for it, so neither existing check saw one: the host's bytes were copied out and
listed under `rpi.copied` as though the session had written them. It grants no read
authority the agent lacks - `HOME` is inherited either way - so what it cost was the
evidence, which claimed a clean collection of something the session never produced.

Each skip is named in `probe.json` under `rpi.skipped_links`, `rpi.skipped_non_files` and
`rpi.container_skipped`, and printed in the summary. The probe collects what a session
produced; dereferencing a link the session left would hand an evidence collector read
authority over the whole host filesystem, which is not what it is for. Skipping rather than
refusing keeps a session from halting its own diagnostic by leaving a link or a directory,
and naming the skip keeps it from reading as "the agent wrote nothing".

### The phase artifact contract

A phase that owes the next one a typed artifact must write it *as a checkpoint*, not as a
hand-in. The prompt asks for `research.json` / `plan.json` to be created immediately,
schema-valid, and updated as work proceeds, because the session can be stopped at any
moment and whatever is in the file then is what the phase produced.

If a required artifact is absent or schema-invalid, the lane **stops**. It does not
fabricate the handoff: the harness used to substitute a stub (`"research phase produced no
artifact"`), which is how six multi-phase cells across two runs came to be scored on a
treatment they never administered. Each phase therefore records its own state, keeping
administration apart from validity:

| state | what it means |
|---|---|
| `completed` | exit 0, and any required artifact arrived schema-valid |
| `censored` | killed at its ceiling (124), but the required artifact was there — a legitimate outcome |
| `treatment_invalid` | a required artifact was missing or schema-invalid — the treatment was not administered |
| `failed` | some other non-zero exit |

Phase artifacts live in `<workspace>/.rpi/` and are excluded from the submission archive,
so what ProgramBench grades is the same kind of thing in every lane. They are copied out to
`out/<lane>/<repeat>/<instance>/rpi/`, and that copy **creates** the directory rather than
replacing it: if one already exists the run stops with

```text
refusing to overwrite pre-existing phase artifact directory: out/.../rpi
```

`submission.tar.gz` and `submission.files.txt` follow the same rule and stop the same way:

```text
refusing to overwrite pre-existing submission output: out/.../submission.tar.gz
```

Both paths are claimed before either is written, so a refused re-run leaves neither a stray
submission nor a half-written manifest.

Re-running into a populated run directory used to delete what was there first — the evidence
of the attempt under investigation, destroyed by the attempt investigating it. Point
`--run-dir` somewhere new, or move the old output aside deliberately. A write that fails
part-way removes only what that call created, never what it found.

### Reading the comparison

`lane-comparison.md` reports four groups. **The primary metric is ProgramBench
correctness.** A lane that saves context but loses resolve rate is worse.

- **primary** — resolve rate, near-resolve rate, mean pass fraction
- **measurement** — branch-balanced pass fraction, executions, unique tests, censored phases,
  phases that produced no artifact
- **efficiency** — wall clock, agent invocations
- **process** — phase failures, non-zero exits, compression ratios
- **stability** — standard deviation across repeats

### Pooled score, branch-balanced check

`mean_pass_fraction` is ProgramBench's own score — passed over *executions*, pooled across
test branches — and stays the primary outcome. The denominator is not constant across
lanes: the suite runs once per test branch, and how many executions a branch contributes
depends on the submission. In run 35715428932 the same 769 test names produced 769
executions in lane E and 1649 in lane D.

So each lane also reports `branch_macro_pass_fraction`, the same results with every branch
weighted equally, plus `executions` and `unique_tests`. It is a sensitivity check, never
the score. On that run the pooled figures read A 83.3%, B 77.7%, C 76.7%, D 73.3% while
the balanced ones read A 84.2%, B 83.0%, C 75.4%, D 77.9% — B and D swap. A pooled delta
smaller than the gap between the two is a weighting artefact, not a treatment effect.

### Provider validity

A score only means something if the provider answered. Every cell is classified in
`run.json` as `provider_validity`, and a lane takes its worst cell:

| verdict | what happened |
|---|---|
| `valid` | every session was served, or ended for an experiment-local reason (its phase ceiling, a bad submission) |
| `provider_degraded` | some session was served and another was refused |
| `provider_unavailable` | nothing was ever served and the provider is on record refusing |

A non-valid lane keeps its score as a *diagnostic* and is dropped from the deltas: both
sides of a subtraction have to be outcomes the provider produced. Run 35701448042 is the
case — NVIDIA rate-limited every attempt in lanes A, B, D and most of E, and the report
rendered that as `D - C = -74.8% mean pass`.

The verdict needs the provider on record, in the session's own log:

```text
❌ Rate limited after 3 retries — HTTP 429 ...   → rate_limit
❌ API failed after 3 retries — Connection error  → unavailable
```

A missing usage report is **not** evidence. A session killed at its phase ceiling writes
none either, and returncode 124 stays an experiment outcome. The matched line is copied
into `run.json`, and the logs themselves are uploaded with the report, so a verdict can be
checked against the output that produced it.

### Treatment validity

A second, independent question about the same cell: the provider answered, but did the lane
administer the treatment its name claims? `run.json` records `treatment_validity`, and a
lane is `invalid` if any cell is.

A cell is invalid when a required phase artifact never arrived — including a compaction
artifact, since a D cell whose compaction did not land is lane C wearing D's name. Like a
refused cell, it keeps its score as a *diagnostic* and is dropped from `C - B`, `D - C` and
`E - D`; `run.json` names the reason (`required_phase_artifact_missing`) and the phase.

Being censored is not being invalid. A phase killed at its ceiling that checkpointed its
artifact ran its treatment and ran out of road, and stays in the comparison.

One repeat per cell is too noisy to interpret; use at least two while developing the
instrumentation and at least three before believing a result.

### Known limits

- **ProgramBench is greenfield.** The workspace holds a compiled reference and its
  documentation, never sources, so lane C's Research phase studies the program's
  observable contract rather than an existing codebase. `C - B` therefore measures RPI's
  value for requirements analysis, *not* for brownfield code exploration, which is the
  case RPI was designed for. A brownfield fixture is needed before generalising the
  result.
- **Tokens are recorded but not compared.** Hermes sessions write token counts into
  `run.json`; the comparison still reports wall clock and agent invocations only, because
  the reported metric has to mean the same thing for every runner the seam accepts.
- **Lane E needs a J-Space checkout to run at all.** It is skipped, not approximated,
  when the pinned artifact is unavailable (see below). A skipped lane has no outcome; do
  not read its zeroes as a treatment effect.
- **No propose-only lane.** A pure-agent lane would change execution authority and Git
  semantics, not just context treatment, so it would confound this experiment. Test it
  against whichever of A–E wins.
