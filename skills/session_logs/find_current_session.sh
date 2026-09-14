#!/usr/bin/env bash

# Find the current Claude Code or Codex CLI transcript.
#
# With Codex CLI, approval flows can leave two JSONL files with the same thread
# id. The larger file is the parent conversation; the smaller file is the
# approval-review transcript. Select the parent so callers do not duplicate
# user messages or mistake an approval request for the conversation.

set -euo pipefail

usage() {
  echo "usage: $0 [claude|codex]" >&2
  exit 2
}

if [[ $# -gt 1 ]]; then
  usage
fi

HARNESS=${1:-}
if [[ -z "$HARNESS" ]]; then
  if [[ -n "${CODEX_THREAD_ID:-}" || -n "${CODEX_SESSION_ID:-}" ]]; then
    HARNESS=codex
  else
    # Claude does not expose a consistently available session-id environment
    # variable in all launch modes. Its project directory is the safer
    # default when the caller did not specify a harness.
    HARNESS=claude
  fi
fi

case "$HARNESS" in
  claude | codex) ;;
  *) usage ;;
esac

choose_most_recent() {
  local candidates="$1"
  local selected
  selected=$(printf '%s\n' "$candidates" | sort -t ' ' -k1,1nr | cut -d ' ' -f2- | awk 'NF && !seen[$0]++ { print; exit }')
  if [[ -z "$selected" ]]; then
    echo "Error: no $HARNESS transcript found" >&2
    exit 1
  fi
  printf '%s\n' "$selected"
}

find_claude() {
  local projects_root="${HOME:?}/.claude/projects"
  local session_id="${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"
  local matches

  if [[ ! -d "$projects_root" ]]; then
    echo "Error: Claude transcript directory not found: $projects_root" >&2
    exit 1
  fi

  if [[ -n "$session_id" ]]; then
    matches=$(find "$projects_root" -type f -name "$session_id.jsonl" \
      ! -path '*/subagents/*' -print 2>/dev/null || true)
    if [[ -n "$matches" ]]; then
      printf '%s\n' "$matches" | awk 'NF && !seen[$0]++ { print; exit }'
      return
    fi
  fi

  local project_dir="${PWD//\//-}"
  local project_root="$projects_root/$project_dir"
  local candidates=""
  if [[ -d "$project_root" ]]; then
    candidates=$(find "$project_root" -maxdepth 1 -type f -name '*.jsonl' \
      ! -path '*/subagents/*' -printf '%T@ %p\n' 2>/dev/null || true)
  fi
  if [[ -z "$candidates" ]]; then
    candidates=$(find "$projects_root" -type f -name '*.jsonl' \
      ! -path '*/subagents/*' -mmin -120 -printf '%T@ %p\n' 2>/dev/null || true)
  fi
  choose_most_recent "$candidates"
}

find_codex() {
  local sessions_root="${HOME:?}/.codex/sessions"
  local thread_id="${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-}}"
  local matches

  if [[ ! -d "$sessions_root" ]]; then
    echo "Error: Codex transcript directory not found: $sessions_root" >&2
    exit 1
  fi

  if [[ -n "$thread_id" ]]; then
    # The canonical Codex filename ends in the thread id. This avoids scanning
    # the potentially multi-gigabyte history store with rg. A paired approval
    # file may have a different suffix but is not the parent transcript.
    matches=$(find "$sessions_root" -type f -name "*$thread_id.jsonl" \
      -print 2>/dev/null || true)
    if [[ -n "$matches" ]]; then
      local largest_file=""
      local largest_lines=0
      local file lines
      while IFS= read -r file; do
        [[ -f "$file" ]] || continue
        lines=$(wc -l <"$file")
        if ((lines > largest_lines)); then
          largest_lines=$lines
          largest_file=$file
        fi
      done <<<"$matches"
      if [[ -n "$largest_file" ]]; then
        printf '%s\n' "$largest_file"
        return
      fi
    fi
  fi

  local candidates
  candidates=$(find "$sessions_root" -type f -name 'rollout-*.jsonl' -mmin -120 \
    -printf '%T@ %p\n' 2>/dev/null || true)
  choose_most_recent "$candidates"
}

case "$HARNESS" in
  claude) find_claude ;;
  codex) find_codex ;;
esac
