#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec bash ../run_ollama_bench.sh ollama-bench-gemma-followup
