#!/usr/bin/env bash
set -euo pipefail
# Simple reproducer for seatbelt-wrapped Jupyter+MCP in stdio mode
# Usage:
#   ./run_one.sh /abs/path/to/.sandbox_jupyter.yaml /abs/workspace /abs/run_root [--port 0]
# Assumes control venv tools (jupyter, jupyter-mcp-server) are available on PATH, or use PATH override.

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 POLICY_YAML WORKSPACE RUN_ROOT [--port PORT]" >&2
  exit 2
fi

POLICY_YAML="$1"
shift
WORKSPACE="$1"
shift
RUN_ROOT="$1"
shift
PORT=0
if [[ "${1:-}" == "--port" ]]; then
  PORT="${2:-0}"
  shift 2
fi

mkdir -p "$RUN_ROOT/runtime" "$RUN_ROOT/data" "$RUN_ROOT/config" "$RUN_ROOT/mpl" "$RUN_ROOT/pycache" "$RUN_ROOT/tmp"

export SJ_DEBUG_DIAG=1
export SJ_POLICY_ECHO_DIR="$RUN_ROOT/tmp"
export JUPYTER_PLATFORM_DIRS=1

# Start wrapper in seatbelt mode over stdio
exec python3 -m sandboxed_jupyter.wrapper stdio \
  --policy-config "$POLICY_YAML" \
  --workspace "$WORKSPACE" \
  --run-root "$RUN_ROOT" \
  --mode seatbelt \
  --jupyter-port "${PORT}" \
  --trace-sandbox
