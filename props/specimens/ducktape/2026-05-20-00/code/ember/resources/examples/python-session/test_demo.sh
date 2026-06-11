#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEMO="$SCRIPT_DIR/demo.sh"

LOG_FILE=$(mktemp)
trap 'rm -f "$LOG_FILE"' EXIT

if ! bash "$DEMO" | tee "$LOG_FILE"; then
  echo "Python session demo failed" >&2
  exit 1
fi

expect() {
  local pattern=$1
  if ! grep -F "$pattern" "$LOG_FILE" >/dev/null; then
    echo "Expected output containing '$pattern'" >&2
    echo "--- demo output ---" >&2
    cat "$LOG_FILE" >&2
    echo "-------------------" >&2
    exit 1
  fi
}

expect "persisted_value=42"
expect "post_restart=None"
expect "heredoc_output=strings with 'quotes' and \$variables stay literal"
expect "module_value=Hello World"
expect "first"
expect "module_reloaded=second"
