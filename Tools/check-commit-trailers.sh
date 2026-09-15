#!/usr/bin/env bash
# Fail if any commit in a range carries agent attribution trailers.
#
# Usage: Tools/check-commit-trailers.sh [<range>]
#   default range: origin/main..HEAD
#
# Rejected trailers (case-insensitive):
#   Co-Authored-By: ... Claude ... | ... @anthropic.com
#   Claude-Session: ...
# Prose mentioning Claude, and non-Claude co-authors, are left alone.
set -euo pipefail

range="${1:-origin/main..HEAD}"
pattern='^[[:space:]]*(co-authored-by:.*(claude|@anthropic\.com)|claude-session:)'

offenders=()
while read -r sha; do
  [ -n "$sha" ] || continue
  if git log -1 --format=%B "$sha" | grep -qiE "$pattern"; then
    offenders+=("$(git log -1 --format='%h %s' "$sha")")
  fi
done < <(git rev-list "$range")

if [ "${#offenders[@]}" -gt 0 ]; then
  {
    echo "Agent attribution trailers found in ${#offenders[@]} commit(s) in $range:"
    printf '  %s\n' "${offenders[@]}"
    echo
    echo "Remove the 'Co-Authored-By: Claude ...' / 'Claude-Session: ...' trailers"
    echo "(git rebase --autosquash, or git commit --amend for the tip commit) and force-push."
    echo "To stop them being added: set attribution.commit and attribution.pr to \"\" in ~/.claude/settings.json."
  } >&2
  exit 1
fi

echo "No agent attribution trailers in $range."
