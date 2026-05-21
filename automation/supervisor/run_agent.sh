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

Launch a fresh Codex run for one supervisor-selected slice.
USAGE
}

repo_root=""
prompt_file=""
context_file=""
handoff_file=""
slice_id=""

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

if [[ -z "$repo_root" || -z "$prompt_file" || -z "$context_file" || -z "$handoff_file" || -z "$slice_id" ]]; then
  echo "Missing required supervisor agent launch argument." >&2
  usage
  exit 64
fi

if [[ ! -d "$repo_root/.git" ]]; then
  echo "Repo root is not a Git checkout: $repo_root" >&2
  exit 66
fi

if [[ ! -f "$prompt_file" ]]; then
  echo "Prompt file does not exist: $prompt_file" >&2
  exit 66
fi

if [[ ! -f "$context_file" ]]; then
  echo "Context file does not exist: $context_file" >&2
  exit 66
fi

if [[ -e "$handoff_file" ]]; then
  echo "Refusing to overwrite existing handoff file: $handoff_file" >&2
  exit 73
fi

codex_bin="${OWLORY_CODEX_BIN:-codex}"
if ! command -v "$codex_bin" >/dev/null 2>&1; then
  echo "Codex CLI not found. Set OWLORY_CODEX_BIN or install codex." >&2
  exit 69
fi

export OWLORY_SUPERVISOR_CONTEXT_FILE="$context_file"
export OWLORY_SUPERVISOR_HANDOFF_FILE="$handoff_file"
export OWLORY_SUPERVISOR_SLICE_ID="$slice_id"

cd "$repo_root"
exec "$codex_bin" --ask-for-approval never exec \
  -C "$repo_root" \
  --sandbox workspace-write \
  - < "$prompt_file"
