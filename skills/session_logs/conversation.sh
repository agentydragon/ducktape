#!/usr/bin/env bash

# Print every human/user turn and the two preceding assistant turns from a
# Claude Code or Codex CLI transcript. The source JSONL is read in full; a
# compaction marker is an informational boundary, not a reason to stop.

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage() {
  echo "usage: $0 [claude|codex] [TRANSCRIPT.jsonl]" >&2
  exit 2
}

HARNESS=
SESSION_FILE=
case $# in
  0)
    SESSION_FILE=$("$SCRIPT_DIR/find-current-session.sh")
    ;;
  1)
    case "$1" in
      claude | codex)
        HARNESS=$1
        SESSION_FILE=$("$SCRIPT_DIR/find-current-session.sh" "$HARNESS")
        ;;
      *)
        SESSION_FILE=$1
        ;;
    esac
    ;;
  2)
    HARNESS=$1
    SESSION_FILE=$2
    [[ "$HARNESS" == claude || "$HARNESS" == codex ]] || usage
    ;;
  *)
    usage
    ;;
esac

[[ -f "$SESSION_FILE" ]] || {
  echo "Error: transcript file not found: $SESSION_FILE" >&2
  exit 1
}

if [[ -z "$HARNESS" ]]; then
  if jq -e 'select(.type == "session_meta")' "$SESSION_FILE" >/dev/null 2>&1; then
    HARNESS=codex
  else
    HARNESS=claude
  fi
fi

case "$HARNESS" in
  claude)
    COMPACTIONS=$(jq -sr '[.[] | select(.type == "system" and .subtype == "compact_boundary")] | length' "$SESSION_FILE")
    ;;
  codex)
    COMPACTIONS=$(jq -sr '[.[] | select(.type == "event_msg" and .payload.type == "context_compacted")] | length' "$SESSION_FILE")
    ;;
  *)
    usage
    ;;
esac

printf 'Transcript: %s\nHarness: %s\nCompaction markers: %s\n' \
  "$SESSION_FILE" "$HARNESS" "$COMPACTIONS"
printf '%s\n' 'The complete JSONL is scanned; continue after every compaction marker.'

jq -sr --arg harness "$HARNESS" '
  def claude_user_text:
    if (.message.content | type) == "string" then
      .message.content
    elif (.message.content | type) == "array" then
      [.message.content[]? | select(.type == "text") | .text // ""] | join("\n")
    else
      ""
    end;

  def codex_user_text:
    [.payload.content[]? |
      select(.type == "input_text" or .type == "text") | .text // ""] | join("\n");

  def user_text($entry):
    if $harness == "claude" then ($entry | claude_user_text)
    else ($entry | codex_user_text)
    end;

  def is_user($entry):
    if $harness == "claude" then
      ($entry.type == "user" and (($entry | claude_user_text) | length > 0)
       and (($entry | claude_user_text | startswith("<task-notification>") | not))
       and (($entry | claude_user_text | startswith("<system-reminder>") | not)))
    else
      ($entry.type == "response_item" and $entry.payload.type == "message"
       and $entry.payload.role == "user" and (($entry | codex_user_text) | length > 0))
    end;

  def assistant_text($entry):
    if $harness == "claude" then
      [.message.content[]? |
        if .type == "text" then "[text]\n" + (.text // "")
        elif .type == "thinking" then "[thinking]\n" + (.thinking // "")
        elif .type == "tool_use" then "[tool_use: " + (.name // "unknown") + "]"
        else empty
        end] | join("\n")
    else
      [.payload.content[]? | select(.type == "output_text" or .type == "text") |
        .text // ""] | join("\n")
    end;

  def is_assistant($entry):
    if $harness == "claude" then $entry.type == "assistant"
    else ($entry.type == "response_item" and $entry.payload.type == "message"
          and $entry.payload.role == "assistant")
    end;

  def is_compaction($entry):
    if $harness == "claude" then
      ($entry.type == "system" and $entry.subtype == "compact_boundary")
    else
      ($entry.type == "event_msg" and $entry.payload.type == "context_compacted")
    end;

  def render_recent:
    if length == 0 then
      "(no preceding assistant message in transcript)\n"
    else
      to_entries |
      map("--- preceding assistant message \(.key + 1) @ \(.value.timestamp) ---\n\(.value.text)\n") |
      join("\n")
    end;

  foreach .[] as $entry
    ({recent: [], user_count: 0};
     .emitted = null |
     if is_compaction($entry) then
       .emitted = "\n### Compaction marker @ " + ($entry.timestamp // "unknown") +
         " — keep scanning; earlier JSONL entries remain part of this conversation.\n"
     elif is_assistant($entry) then
       .recent += [{timestamp: ($entry.timestamp // "unknown"), text: ($entry | assistant_text($entry))}] |
       .recent = .recent[-2:]
     elif is_user($entry) then
       .user_count += 1 |
       ($entry | user_text($entry)) as $text |
       .emitted =
         ("\n## User message " + (.user_count | tostring) + " @ " + ($entry.timestamp // "unknown") + "\n" +
          (.recent | render_recent) +
          "--- user message ---\n" + $text + "\n")
     else
       .
     end;
     .emitted // empty)
' "$SESSION_FILE"
