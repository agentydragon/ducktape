#!/bin/bash
# Start Ollama server with GPT-OSS-20B (host-native, no container)
#
# Hardware: RTX 5090 (32GB) - model ~14 GB + KV cache ~13 GB = fits on single GPU
# Model: gpt-oss (already pulled to /wyrmhdd/ollama-models)
#
# Ollama serves OpenAI-compatible API at http://localhost:11434/v1
#
# Requires: ollama binary on PATH (install via https://ollama.com/install.sh)
#
# Why not Podman? Podman 4.x CDI passthrough doesn't properly expose CUDA
# runtime to Ollama's GPU discovery, causing silent CPU fallback. Running
# on the host avoids this entirely.
#
# Usage:
#   ./start-ollama.sh                        # Use defaults
#   MODEL=gpt-oss:20b-32k ./start-ollama.sh  # Reduced context variant
#   PORT=11435 ./start-ollama.sh              # Custom port

set -euo pipefail

# Model to load after server starts
MODEL="${MODEL:-gpt-oss}"

# Port
PORT="${PORT:-11434}"

# Context window (tokens). 131072 = full model context.
# KV cache at 131K: ~13 GB (f16, 24 layers, 8 kv_heads, 128 dim).
NUM_CTX="${NUM_CTX:-131072}"

# Ollama models directory (pre-downloaded weights)
OLLAMA_MODELS="${OLLAMA_MODELS:-/wyrmhdd/ollama-models}"

# Ensure a recent-enough ollama is available
OLLAMA_MIN_VERSION="0.15.0"
OLLAMA_BIN="${OLLAMA_BIN:-/usr/local/bin/ollama}"

if [[ ! -x "$OLLAMA_BIN" ]]; then
  OLLAMA_BIN="$(command -v ollama 2>/dev/null || true)"
fi

needs_install=false
if [[ -z "$OLLAMA_BIN" ]]; then
  needs_install=true
elif ! version=$("$OLLAMA_BIN" --version 2>&1 | grep -oP '\d+\.\d+\.\d+'); then
  needs_install=true
elif printf '%s\n%s\n' "$OLLAMA_MIN_VERSION" "$version" | sort -V -C; then
  : # version >= min, ok
else
  echo "Installed ollama is $version (need >= $OLLAMA_MIN_VERSION)"
  needs_install=true
fi

if $needs_install; then
  echo "Installing/updating ollama..."
  curl -fsSL https://ollama.com/install.sh | sh
  OLLAMA_BIN="$(command -v ollama)"
fi

echo "Starting Ollama server..."
echo "  Binary: $OLLAMA_BIN ($("$OLLAMA_BIN" --version 2>&1 | grep -oP '\d+\.\d+\.\d+' || echo unknown))"
echo "  Model: $MODEL"
echo "  Models dir: $OLLAMA_MODELS"
echo "  Context: $NUM_CTX tokens"
echo "  API endpoint: http://localhost:$PORT/v1"
echo ""

# Stop any existing ollama (systemd or stale process)
pkill -f 'ollama serve' 2>/dev/null || true
sleep 1

# Launch ollama with a clean (non-Nix) environment so the system dynamic
# linker resolves libcuda.so.1 from /etc/ld.so.cache.  Nix's glibc uses
# its own cache that doesn't include the NVIDIA driver library, causing
# CUDA GPU discovery to silently fail (initial_count=0).
env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  HOME="$HOME" \
  OLLAMA_MODELS="$OLLAMA_MODELS" \
  OLLAMA_HOST="127.0.0.1:$PORT" \
  OLLAMA_DEBUG="${OLLAMA_DEBUG:-}" \
  "$OLLAMA_BIN" serve &
OLLAMA_PID=$!

# Wait for server to be ready
echo "Waiting for Ollama server (pid $OLLAMA_PID)..."
until curl -sf "http://localhost:$PORT/" >/dev/null 2>&1; do
  if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
    echo "ERROR: Ollama process exited unexpectedly"
    exit 1
  fi
  sleep 0.5
done
echo "Ollama server ready."

# Pre-load model (keep_alive=-1 keeps it loaded indefinitely)
echo "Loading $MODEL (num_ctx=$NUM_CTX)..."
curl -sf "http://localhost:$PORT/api/generate" \
  -d "{\"model\": \"$MODEL\", \"keep_alive\": -1, \"options\": {\"num_ctx\": $NUM_CTX}}" >/dev/null

# Show GPU status
echo ""
curl -s "http://localhost:$PORT/api/ps" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for m in data.get('models', []):
    vram = m.get('size_vram', 0)
    size = m.get('size', 0)
    gpu_pct = (vram / size * 100) if size else 0
    print(f\"  {m['name']}: {vram/1e9:.1f} GB VRAM / {size/1e9:.1f} GB total ({gpu_pct:.0f}% GPU)\")
" 2>/dev/null || true

echo ""
echo "Model loaded. Ctrl+C to stop server."
wait "$OLLAMA_PID"
