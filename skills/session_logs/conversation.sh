#!/usr/bin/env bash

# Print every human/user turn and the two preceding assistant turns from a
# Claude Code or Codex CLI transcript. The source JSONL is read in full; a
# compaction marker is an informational boundary, not a reason to stop.

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage() {
  echo "usage: $0 [--max-display-text-length N] [claude|codex] [TRANSCRIPT.jsonl]" >&2
  exit 2
}

MAX_DISPLAY_TEXT_LENGTH=1000
POSITIONAL=()
while (($# > 0)); do
  case "$1" in
    --max-display-text-length)
      (($# >= 2)) || usage
      MAX_DISPLAY_TEXT_LENGTH=$2
      shift 2
      ;;
    --max-display-text-length=*)
      MAX_DISPLAY_TEXT_LENGTH=${1#*=}
      shift
      ;;
    --)
      shift
      POSITIONAL+=("$@")
      break
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [[ ! "$MAX_DISPLAY_TEXT_LENGTH" =~ ^[0-9]+$ ]] || ((MAX_DISPLAY_TEXT_LENGTH < 100)); then
  echo "Error: --max-display-text-length must be an integer of at least 100" >&2
  exit 2
fi

set -- "${POSITIONAL[@]}"

HARNESS=
SESSION_FILE=
case $# in
  0)
    SESSION_FILE=$("$SCRIPT_DIR/find_current_session.sh")
    ;;
  1)
    case "$1" in
      claude | codex)
        HARNESS=$1
        SESSION_FILE=$("$SCRIPT_DIR/find_current_session.sh" "$HARNESS")
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

jq -sr \
  --arg harness "$HARNESS" \
  --argjson max_display_text_length "$MAX_DISPLAY_TEXT_LENGTH" \
  -f "$SCRIPT_DIR/conversation.jq" \
  "$SESSION_FILE"
