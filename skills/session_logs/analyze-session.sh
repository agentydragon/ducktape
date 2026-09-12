#!/usr/bin/env bash

# Summarize a Claude Code or Codex CLI transcript.

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage() {
  echo "usage: $0 [claude|codex|TRANSCRIPT.jsonl]" >&2
  exit 2
}

if [[ $# -eq 0 ]]; then
  SESSION_FILE=$("$SCRIPT_DIR/find-current-session.sh")
elif [[ $# -eq 1 && ("$1" == claude || "$1" == codex) ]]; then
  SESSION_FILE=$("$SCRIPT_DIR/find-current-session.sh" "$1")
elif [[ $# -eq 1 ]]; then
  SESSION_FILE=$1
else
  usage
fi

if [[ ! -f "$SESSION_FILE" ]]; then
  echo "Error: transcript file not found: $SESSION_FILE" >&2
  exit 1
fi

if jq -e 'select(.type == "session_meta")' "$SESSION_FILE" >/dev/null 2>&1; then
  HARNESS=codex
else
  HARNESS=claude
fi

case "$HARNESS" in
  claude)
    read -r SESSION_ID CWD BRANCH LAST_TIMESTAMP < <(
      jq -sr '
        {
          session_id: ([.[] | select(.sessionId != null) | .sessionId][0] // "unknown"),
          cwd: ([.[] | select(.cwd != null) | .cwd][0] // "unknown"),
          branch: ([.[] | select(.gitBranch != null) | .gitBranch][0] // "unknown"),
          timestamp: ([.[] | select(.timestamp != null) | .timestamp][-1] // "unknown")
        } | [.session_id, .cwd, .branch, .timestamp] | @tsv
      ' "$SESSION_FILE"
    )
    USER_MESSAGES=$(jq -sr '
      [ .[] | select(.type == "user") |
        (if (.message.content | type) == "string" then .message.content
         elif (.message.content | type) == "array" then
           [.message.content[]? | select(.type == "text") | .text // ""] | join("\n")
         else "" end) |
        select(length > 0) |
        select(startswith("<task-notification>") | not) |
        select(startswith("<system-reminder>") | not)
      ] | length
    ' "$SESSION_FILE")
    AGENT_MESSAGES=$(jq -sr '[.[] | select(.type == "assistant")] | length' "$SESSION_FILE")
    COMPACTIONS=$(jq -sr '[.[] | select(.type == "system" and .subtype == "compact_boundary")] | length' "$SESSION_FILE")
    TOOL_USES=$(jq -sr '[.[] | select(.type == "assistant") | .message.content[]? | select(.type == "tool_use")] | length' "$SESSION_FILE")
    ;;
  codex)
    read -r SESSION_ID CWD BRANCH LAST_TIMESTAMP < <(
      jq -sr '
        (map(select(.type == "session_meta")) | last | .payload) as $meta |
        [$meta.session_id // "unknown", $meta.cwd // "unknown", ($meta.git.branch // "unknown"),
         ([.[] | select(.timestamp != null) | .timestamp][-1] // "unknown")] | @tsv
      ' "$SESSION_FILE"
    )
    USER_MESSAGES=$(jq -sr '[.[] | select(.type == "response_item" and .payload.type == "message" and .payload.role == "user")] | length' "$SESSION_FILE")
    AGENT_MESSAGES=$(jq -sr '[.[] | select(.type == "response_item" and .payload.type == "message" and .payload.role == "assistant")] | length' "$SESSION_FILE")
    COMPACTIONS=$(jq -sr '[.[] | select(.type == "event_msg" and .payload.type == "context_compacted")] | length' "$SESSION_FILE")
    TOOL_USES=$(jq -sr '[.[] | select(.type == "response_item" and (.payload.type == "function_call" or .payload.type == "custom_tool_call"))] | length' "$SESSION_FILE")
    ;;
esac

printf '=== Session analysis: %s ===\n' "$(basename "$SESSION_FILE")"
printf 'Harness: %s\nSession ID: %s\nWorking directory: %s\nGit branch: %s\nLast activity: %s\n' \
  "$HARNESS" "$SESSION_ID" "$CWD" "$BRANCH" "$LAST_TIMESTAMP"
printf 'Entries: %s\nUser messages: %s\nAgent messages: %s\nTool calls: %s\nCompactions: %s\n' \
  "$(wc -l <"$SESSION_FILE")" "$USER_MESSAGES" "$AGENT_MESSAGES" "$TOOL_USES" "$COMPACTIONS"
printf 'Transcript: %s\n' "$SESSION_FILE"
