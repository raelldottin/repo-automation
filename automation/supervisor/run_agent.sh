#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: automation/supervisor/run_agent.sh \
  --repo-root PATH \
  --prompt-file PATH \
  --context-file PATH \
  --handoff-file PATH \
  --slice-id ID

automation/supervisor/run_agent.sh --repo-root PATH --sandbox-probe

Launch a fresh agent run for one supervisor-selected slice, or - with
--sandbox-probe - make one no-model terminal call through the composed Hermes
config to prove what the tools' default backend is, and exit.

Runner selection:
  REPO_AUTOMATION_AGENT_RUNNER=auto|codex|claude|hermes (default: auto)
  REPO_AUTOMATION_CODEX_BIN overrides the Codex executable.
  REPO_AUTOMATION_CLAUDE_BIN overrides the Claude Code executable.
  REPO_AUTOMATION_CLAUDE_PERMISSION_MODE overrides the Claude permission mode.
  REPO_AUTOMATION_HERMES_BIN overrides the Hermes executable.
  REPO_AUTOMATION_HERMES_USAGE_DIR collects a per-session usage and controls report.
  HERMES_REVISION is recorded in that report; set it to the pinned Hermes commit.
  HERMES_INFERENCE_PROVIDER and HERMES_INFERENCE_MODEL choose what Hermes talks to.

Legacy OWLORY_CODEX_BIN remains supported for Codex executable overrides.
USAGE
}

process_tree_contains() {
  local needle="$1"
  local pid="${PPID:-}"
  local command=""
  local args=""

  while [[ -n "$pid" && "$pid" != "0" ]]; do
    command="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
    args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
    if [[ "$command" == *"$needle"* || "$args" == *"$needle"* ]]; then
      return 0
    fi
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  done

  return 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

# sha256sum on Linux, shasum on macOS. Both CI and a developer laptop run this script.
sha256_of() {
  if command_exists sha256sum; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

running_under_claude_code() {
  [[ -n "${CLAUDECODE:-}" || -n "${CLAUDE_CODE:-}" || -n "${CLAUDE_CODE_ENTRYPOINT:-}" ]] ||
    process_tree_contains "claude"
}

select_agent_runner() {
  local requested_runner="$1"
  local codex_bin="$2"
  local claude_bin="$3"
  local hermes_bin="$4"

  case "$requested_runner" in
    auto)
      if running_under_claude_code && command_exists "$claude_bin"; then
        printf 'claude\n'
      elif command_exists "$codex_bin"; then
        printf 'codex\n'
      elif command_exists "$claude_bin"; then
        printf 'claude\n'
      elif command_exists "$hermes_bin"; then
        printf 'hermes\n'
      else
        echo "No supported agent CLI found. Install Codex, Claude Code or Hermes, or set REPO_AUTOMATION_AGENT_RUNNER with a matching binary override." >&2
        return 69
      fi
      ;;
    codex|claude|hermes)
      printf '%s\n' "$requested_runner"
      ;;
    *)
      echo "Unsupported REPO_AUTOMATION_AGENT_RUNNER: $requested_runner" >&2
      echo "Expected one of: auto, codex, claude, hermes." >&2
      return 64
      ;;
  esac
}

repo_root=""
prompt_file=""
context_file=""
handoff_file=""
slice_id=""
sandbox_probe=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      repo_root="${2:-}"
      shift 2
      ;;
    --prompt-file)
      prompt_file="${2:-}"
      shift 2
      ;;
    --context-file)
      context_file="${2:-}"
      shift 2
      ;;
    --handoff-file)
      handoff_file="${2:-}"
      shift 2
      ;;
    --slice-id)
      slice_id="${2:-}"
      shift 2
      ;;
    --sandbox-probe)
      sandbox_probe=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 64
      ;;
  esac
done

# The probe launches no session, so it needs none of a session's inputs - only the
# workspace whose sandbox it is asking about.
if [[ -z "$repo_root" ]] || [[ "$sandbox_probe" == "0" &&
  (-z "$prompt_file" || -z "$context_file" || -z "$handoff_file" || -z "$slice_id") ]]; then
  echo "Missing required supervisor agent launch argument." >&2
  usage
  exit 64
fi

if [[ ! -d "$repo_root/.git" ]]; then
  echo "Repo root is not a Git checkout: $repo_root" >&2
  exit 66
fi

if [[ "$sandbox_probe" == "1" ]]; then
  # A Hermes sandbox is the only thing there is to probe; no other runner has one.
  agent_runner_override="hermes"
fi

if [[ "$sandbox_probe" == "0" ]] && [[ ! -f "$prompt_file" ]]; then
  echo "Prompt file does not exist: $prompt_file" >&2
  exit 66
fi

if [[ "$sandbox_probe" == "0" ]] && [[ ! -f "$context_file" ]]; then
  echo "Context file does not exist: $context_file" >&2
  exit 66
fi

if [[ "$sandbox_probe" == "0" ]] && [[ -e "$handoff_file" ]]; then
  echo "Refusing to overwrite existing handoff file: $handoff_file" >&2
  exit 73
fi

codex_bin="${REPO_AUTOMATION_CODEX_BIN:-${OWLORY_CODEX_BIN:-codex}}"
claude_bin="${REPO_AUTOMATION_CLAUDE_BIN:-claude}"
hermes_bin="${REPO_AUTOMATION_HERMES_BIN:-hermes}"
agent_runner="${agent_runner_override:-$(select_agent_runner "${REPO_AUTOMATION_AGENT_RUNNER:-auto}" "$codex_bin" "$claude_bin" "$hermes_bin")}"

export REPO_AUTOMATION_SUPERVISOR_CONTEXT_FILE="$context_file"
export REPO_AUTOMATION_SUPERVISOR_HANDOFF_FILE="$handoff_file"
export REPO_AUTOMATION_SUPERVISOR_SLICE_ID="$slice_id"
export OWLORY_SUPERVISOR_CONTEXT_FILE="$context_file"
export OWLORY_SUPERVISOR_HANDOFF_FILE="$handoff_file"
export OWLORY_SUPERVISOR_SLICE_ID="$slice_id"

cd "$repo_root"

case "$agent_runner" in
  codex)
    if ! command_exists "$codex_bin"; then
      echo "Codex CLI not found. Set REPO_AUTOMATION_CODEX_BIN, OWLORY_CODEX_BIN, or install codex." >&2
      exit 69
    fi
    exec "$codex_bin" --ask-for-approval never exec \
      -C "$repo_root" \
      --sandbox workspace-write \
      - < "$prompt_file"
    ;;
  claude)
    if ! command_exists "$claude_bin"; then
      echo "Claude Code CLI not found. Set REPO_AUTOMATION_CLAUDE_BIN or install claude." >&2
      exit 69
    fi
    exec "$claude_bin" --print \
      --input-format text \
      --no-session-persistence \
      --permission-mode "${REPO_AUTOMATION_CLAUDE_PERMISSION_MODE:-bypassPermissions}" \
      --add-dir "$repo_root" \
      < "$prompt_file"
    ;;
  hermes)
    if ! command_exists "$hermes_bin"; then
      echo "Hermes CLI not found. Set REPO_AUTOMATION_HERMES_BIN or install hermes." >&2
      exit 69
    fi
    # Reproduce the deployed Hermes posture, then subtract what would contaminate a lane.
    #
    # NOT --safe-mode. That flag sets three independent controls at once, and one of them,
    # HERMES_IGNORE_USER_CONFIG, discards config.yaml and falls back to Hermes' built-in
    # defaults - 10 concurrent delegation children, an enabled orchestrator, background
    # review on. A benchmark that ran on those defaults would not be measuring the harness
    # anyone operates. So set the other two directly and leave the config loading:
    #
    #   HERMES_SAFE_MODE=1   plugins, MCP servers, outbound webhooks, shell hooks
    #   --ignore-rules       AGENTS.md, SOUL.md, .cursorrules, memory, preloaded skills
    #   config.yaml          honoured - this is the posture under test
    export HERMES_SAFE_MODE=1
    # Where the tools actually run. --in changes the process directory, but the terminal,
    # file and code_execution tools resolve their own working directory and prefer
    # TERMINAL_CWD to it. Left unset, a session edits files outside the checkout it was
    # handed: a benchmark cell then archives an empty workspace, scores compile_failed,
    # and the next cell inherits the last one's files from wherever the tools defaulted to.
    export TERMINAL_CWD="$repo_root"
    # Harmless, and kept for that reason alone - it does not make a killed session
    # readable. Under --oneshot, Hermes' own hermes_cli/oneshot.py redirects stdout AND
    # stderr to /dev/null for the whole turn and writes the final response to the real
    # stdout only after the turn returns, so a session killed at its phase ceiling archives
    # a 0-byte log whatever the buffering is. That is what runs 35715428932, 35736569601
    # and 35788842735 recorded. The stream-json transport below is the way to see inside.
    export PYTHONUNBUFFERED=1
    # Pin the toolset: the default CLI set hands the model delegate_task, memory,
    # session_search and the skills tools, so a session could spawn a second agent, keep
    # state for the next one, or load a skill of its own choosing - none of which is the
    # treatment the caller administered.
    hermes_toolsets="terminal,file,code_execution,todo"
    # One throwaway HERMES_HOME per invocation. sessions/ and memories/ are per-home, so a
    # shared one would let a later session read what an earlier one saw - and a measurement
    # of what compaction drops is worthless if the dropped context can be recalled.
    # ponytail: left for the OS to reap, like the workspace dirs the adapter makes.
    HERMES_HOME="$(mktemp -d "${TMPDIR:-/tmp}/repo-automation-hermes-XXXXXX")"
    export HERMES_HOME
    # Diagnostic transport, not a benchmark setting. --oneshot is what every A-E run has
    # been measured on and stays the default; stream-json routes through `hermes chat`
    # instead, which is a different execution path in the agent, so a session produced this
    # way answers "what did the turn do before it died" and is never a lane result.
    hermes_transport="${REPO_AUTOMATION_HERMES_TRANSPORT:-oneshot}"
    case "$hermes_transport" in
      oneshot | stream-json) ;;
      *)
        echo "Unsupported REPO_AUTOMATION_HERMES_TRANSPORT: $hermes_transport" >&2
        echo "Expected one of: oneshot, stream-json." >&2
        exit 64
        ;;
    esac

    hermes_config="$script_dir/hermes-benchmark.yaml"
    if [[ ! -f "$hermes_config" ]]; then
      echo "Hermes benchmark config not found: $hermes_config" >&2
      exit 69
    fi
    cp "$hermes_config" "$HERMES_HOME/config.yaml"
    # The model-facing sandbox, when the caller built one. The adapter materializes the
    # ProgramBench cleanroom into the workspace and names the image here; TERMINAL_CWD
    # above is that workspace, which Hermes bind-mounts at /workspace inside the image.
    # Unset, the session runs on the local filesystem exactly as it always has.
    sandbox_image="${REPO_AUTOMATION_HERMES_SANDBOX_IMAGE:-}"
    sandbox_image_id=""
    sandbox_backend="local"
    if [[ -n "$sandbox_image" ]]; then
      hermes_sandbox_config="$script_dir/hermes-cleanroom.yaml"
      if [[ ! -f "$hermes_sandbox_config" ]]; then
        echo "Hermes cleanroom config not found: $hermes_sandbox_config" >&2
        exit 69
      fi
      # A `-v` spec is colon-separated, so a workspace path containing one would bind
      # something else entirely. mktemp never produces one; refuse rather than guess.
      case "$repo_root" in
        *:*)
          echo "Workspace path contains a colon and cannot be bind-mounted: $repo_root" >&2
          exit 78
          ;;
      esac
      {
        printf '\n'
        cat "$hermes_sandbox_config"
        printf '  docker_image: "%s"\n' "$sandbox_image"
        # Per-cell, so it is appended here rather than kept in the fragment. Explicit
        # because `docker_mount_cwd_to_workspace` only binds the CLI parent's "default"
        # backend: a session-scoped task id - which is what every tool call in an agent
        # turn carries - is refused the TERMINAL_CWD-derived mount and would get a tmpfs.
        # An entry ending in :/workspace is honoured for every task id.
        printf '  docker_volumes: ["%s:/workspace"]\n' "$repo_root"
      } >> "$HERMES_HOME/config.yaml"
      sandbox_backend="docker"
      # Which bytes ran, not which tag was requested: a tag is repointed, a digest is not.
      sandbox_image_id="$(docker image inspect --format '{{.Id}}' "$sandbox_image" 2>/dev/null || true)"
    fi
    if [[ "$sandbox_probe" == "1" ]]; then
      # The pre-budget proof, with no model in the loop: one terminal call through the
      # config composed immediately above, so what it exercises is the configuration the
      # next invocation gets rather than a restatement of it.
      #
      # Hermes builds one backend per task id, and the benchmark posture gives the agent
      # two of them: the CLI parent's "default" backend (task_id=None), and - because
      # `container_persistent: false` turns on per-session isolation - a session-scoped one
      # that every tool call inside an agent turn resolves to. They take different paths
      # through the mount logic, so both are probed. Run 36485906813 is why: the default
      # backend had the cleanroom at /workspace and the session-scoped one had a tmpfs.
      if [[ -z "$sandbox_image" ]]; then
        echo "--sandbox-probe needs REPO_AUTOMATION_HERMES_SANDBOX_IMAGE; there is no sandbox to probe." >&2
        exit 78
      fi
      # Hermes' own interpreter, not this shell's: the tools are importable only from the
      # venv it was installed into, and importing them is what routes through config.yaml.
      probe_python="${REPO_AUTOMATION_HERMES_PYTHON:-$(dirname "$(command -v "$hermes_bin")")/python}"
      if [[ ! -x "$probe_python" ]]; then
        echo "Hermes interpreter not found: $probe_python. Set REPO_AUTOMATION_HERMES_PYTHON." >&2
        exit 69
      fi
      probe_log="/dev/null"
      if [[ -n "${REPO_AUTOMATION_HERMES_USAGE_DIR:-}" ]]; then
        mkdir -p "$REPO_AUTOMATION_HERMES_USAGE_DIR"
        probe_log="$REPO_AUTOMATION_HERMES_USAGE_DIR/sandbox-probe.log"
      fi
      # Reads the cleanroom the way a rebuild would: where am I, is the reference binary
      # here, is the documentation here. Three facts, one exit status, no model tokens.
      export REPO_AUTOMATION_SANDBOX_PROBE_COMMAND="${REPO_AUTOMATION_SANDBOX_PROBE_COMMAND:-pwd && test -x ./executable && test -f README.md}"
      set +e
      "$probe_python" - <<'PROBE' 2>&1 | tee "$probe_log"
import json
import os
import sys
import uuid

# Before importing the tool: this is the bridge that turns the `terminal.*` keys of
# config.yaml into the TERMINAL_* env vars terminal_tool actually reads. Skipping it
# would run the probe on Hermes' defaults and prove nothing about the composed config.
from hermes_cli.config import apply_terminal_config_to_env

apply_terminal_config_to_env()

from tools.terminal_tool import terminal_tool

command = os.environ["REPO_AUTOMATION_SANDBOX_PROBE_COMMAND"]
failed = False
for task_id in (None, f"probe-{uuid.uuid4()}"):
    answer = json.loads(terminal_tool(command, task_id=task_id))
    print(json.dumps({"task_id": task_id or "default", "result": answer}))
    failed = failed or answer.get("exit_code") != 0
sys.exit(1 if failed else 0)
PROBE
      probe_status=${PIPESTATUS[0]}
      set -e
      exit "$probe_status"
    fi
    hermes_args=(--ignore-rules --in "$repo_root" --toolsets "$hermes_toolsets")
    if [[ "$hermes_transport" == "stream-json" ]]; then
      # `chat` is the only path that carries the JSONL emitter; --format implies --quiet and
      # requires -q, so the prompt moves off --oneshot onto --query.
      hermes_args=(chat "${hermes_args[@]}" --format stream-json)
      hermes_prompt_flag="--query"
    else
      hermes_prompt_flag="--oneshot"
    fi
    session_stem=""
    if [[ -n "${REPO_AUTOMATION_HERMES_USAGE_DIR:-}" ]]; then
      mkdir -p "$REPO_AUTOMATION_HERMES_USAGE_DIR"
      session_stem="$REPO_AUTOMATION_HERMES_USAGE_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$$"
      # --usage-file is documented as having no effect outside -z/--oneshot, so the
      # stream-json transport gets no usage report rather than an empty one that would read
      # like a session which spent nothing. The controls below record which path ran, so a
      # missing usage report on a probe is a stated property and not an unexplained gap.
      if [[ "$hermes_transport" == "oneshot" ]]; then
        hermes_args+=(--usage-file "$session_stem.usage.json")
      fi
      # The usage report says what the session spent; it does not say what the session was
      # allowed to do. Record the controls beside it, from the same variables that set them,
      # and hash the config so a cell states exactly which posture produced it. The hash is
      # of the file Hermes loaded, not of the template it was copied from: under a cleanroom
      # cell the sandbox block is appended after the copy, and a template hash would
      # describe a configuration that never ran.
      config_sha="$(sha256_of "$HERMES_HOME/config.yaml")"
      # Requested is not effective. `--toolsets` is what this invocation asked for; Hermes'
      # single-query mode refuses execute_code outright (the tool answers BLOCKED), while
      # the --oneshot path sets HERMES_YOLO_MODE=1 and has not been observed refusing it.
      # Neither is an observation of *this* session, so effective_tools stays null rather
      # than restating the request as though it had been confirmed.
      requested_toolsets="$(printf '%s' "$hermes_toolsets" | sed 's/[^,]*/"&"/g')"
      if [[ "$hermes_transport" == "stream-json" ]]; then
        refused_tools='["code_execution"]'
        tool_observation="single-query mode refuses execute_code; observed on this transport, not in this session"
      else
        refused_tools='[]'
        tool_observation="not observed: --oneshot leaves no tool-level transcript"
      fi
      cat > "$session_stem.controls.json" <<CONTROLS
{"runner":"hermes",
 "hermes_revision":"${HERMES_REVISION:-unknown}",
 "config_profile":"kanban-benchmark-v1",
 "config_sha256":"$config_sha",
 "safe_mode_env":true,
 "ignore_rules":true,
 "ignore_user_config":false,
 "requested_toolsets":[$requested_toolsets],
 "effective_tools":null,
 "refused_tools":$refused_tools,
 "tool_observation":"$tool_observation",
 "transport":"$hermes_transport",
 "provider":"${HERMES_INFERENCE_PROVIDER:-}",
 "model":"${HERMES_INFERENCE_MODEL:-}",
 "sandbox":{"backend":"$sandbox_backend",
            "image":"$sandbox_image",
            "image_id":"$sandbox_image_id",
            "network":"$([[ "$sandbox_backend" == docker ]] && echo none || echo host)",
            "workspace":"$([[ "$sandbox_backend" == docker ]] && echo /workspace || echo "$TERMINAL_CWD")",
            "credentials_forwarded":[]},
 "slice_id":"$slice_id",
 "hermes_home":"$HERMES_HOME",
 "terminal_cwd":"$TERMINAL_CWD"}
CONTROLS
    fi
    if [[ -z "$session_stem" ]]; then
      exec "$hermes_bin" "${hermes_args[@]}" "$hermes_prompt_flag" "$(cat "$prompt_file")"
    fi
    if [[ "$hermes_transport" == "stream-json" ]]; then
      # stdout is the event stream and stderr is diagnostics plus the session id, so they go
      # to separate files: the JSONL stays byte for byte what Hermes emitted, and .log stays
      # the file the provider-failure classifier reads. The emitter flushes every line, so
      # unlike --oneshot a turn killed at its ceiling leaves everything it did up to then.
      set +e
      "$hermes_bin" "${hermes_args[@]}" "$hermes_prompt_flag" "$(cat "$prompt_file")" \
        2> "$session_stem.log" | tee "$session_stem.stream.jsonl"
      hermes_status=${PIPESTATUS[0]}
      set -e
      exit "$hermes_status"
    fi
    # Keep the session's own output beside its reports. The usage report says whether the
    # session failed, never why: a provider that refuses to serve (429, exhausted retries)
    # and an agent that simply did badly both land as a failed turn with no model. The
    # caller needs that difference to tell a treatment effect from a provider outage, and
    # the terminal line naming it is only ever printed. Teed, so the job log still streams
    # live, and not exec'd, so the tee has flushed the last block before the caller reads.
    set +e
    "$hermes_bin" "${hermes_args[@]}" --oneshot "$(cat "$prompt_file")" 2>&1 | tee "$session_stem.log"
    # The session's status, not the tee's: a logging failure is not a phase failure.
    hermes_status=${PIPESTATUS[0]}
    set -e
    exit "$hermes_status"
    ;;
esac
