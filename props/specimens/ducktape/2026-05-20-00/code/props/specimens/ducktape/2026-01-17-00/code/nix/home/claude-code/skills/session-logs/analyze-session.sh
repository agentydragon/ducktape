#!/bin/bash

# Analyze Claude Code Session Script
# Provides summary statistics and recent activity from a session log

set -euo pipefail

# Get session file (argument or auto-detect)
if [ $# -eq 1 ]; then
  SESSION_FILE="$1"
else
  SESSION_FILE=$(~/.claude/skills/session-logs/find-current-session.sh)
fi

if [ ! -f "$SESSION_FILE" ]; then
  echo "Error: Session file not found: $SESSION_FILE" >&2
  exit 1
fi

echo "=== Session Analysis: $(basename "$SESSION_FILE") ==="
echo ""

# Session metadata from last entry
LAST_ENTRY=$(tail -1 "$SESSION_FILE")
SESSION_ID=$(echo "$LAST_ENTRY" | jq -r '.sessionId // "unknown"')
CWD=$(echo "$LAST_ENTRY" | jq -r '.cwd // "unknown"')
BRANCH=$(echo "$LAST_ENTRY" | jq -r '.gitBranch // "unknown"')
LAST_TIMESTAMP=$(echo "$LAST_ENTRY" | jq -r '.timestamp // "unknown"')

echo "Session ID: $SESSION_ID"
echo "Working Directory: $CWD"
echo "Git Branch: $BRANCH"
echo "Last Activity: $LAST_TIMESTAMP"
echo ""

# Entry counts
TOTAL_ENTRIES=$(wc -l <"$SESSION_FILE")
TOOL_USES=$(grep -c '"type":"tool_use"' "$SESSION_FILE" || echo 0)
USER_MESSAGES=$(grep -c '"type":"user"' "$SESSION_FILE" || echo 0)
THINKING_BLOCKS=$(grep -c '"type":"thinking"' "$SESSION_FILE" || echo 0)

echo "=== Statistics ==="
echo "Total Entries: $TOTAL_ENTRIES"
echo "Tool Uses: $TOOL_USES"
echo "User Messages: $USER_MESSAGES"
echo "Thinking Blocks: $THINKING_BLOCKS"
echo ""

# Tool usage breakdown
echo "=== Tool Usage ==="
grep '"type":"tool_use"' "$SESSION_FILE" \
  | jq -r '.message.content[0].name // "unknown"' \
  | sort | uniq -c | sort -rn | head -10
echo ""

# Recent tool calls (last 10)
echo "=== Recent Tool Calls (last 10) ==="
grep '"type":"tool_use"' "$SESSION_FILE" | tail -10 \
  | jq -r '"\(.timestamp | split("T")[1] | split(".")[0]): \(.message.content[0].name)"'
echo ""

# Files modified
echo "=== Files Modified ==="
grep '"type":"tool_use"' "$SESSION_FILE" \
  | jq -r 'select(.message.content[0].name == "Edit" or .message.content[0].name == "Write") |
    .message.content[0].input.file_path' \
  | sort -u | head -20
echo ""

# Recent user messages (last 5)
echo "=== Recent User Messages (last 5) ==="
grep '"type":"user"' "$SESSION_FILE" | tail -5 \
  | jq -r '"\(.timestamp | split("T")[1] | split(".")[0]): \(.message.content[0].text[0:80])"'
echo ""

echo "=== Session file: $SESSION_FILE ==="
