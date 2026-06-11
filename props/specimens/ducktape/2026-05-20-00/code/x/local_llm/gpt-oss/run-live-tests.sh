#!/bin/bash
# Run all live OpenAI API tests against a local Ollama instance.
#
# Prerequisites:
#   - Ollama running (./start-ollama.sh)
#   - gpt-oss model loaded
#
# Usage:
#   ./run-live-tests.sh                    # Use defaults (localhost:11434, gpt-oss)
#   OPENAI_MODEL=gpt-oss:20b-32k ./run-live-tests.sh

set -euo pipefail

export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://localhost:11434/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-unused}"
export OPENAI_MODEL="${OPENAI_MODEL:-gpt-oss}"

echo "Running live OpenAI API tests against Ollama"
echo "  Base URL: $OPENAI_BASE_URL"
echo "  Model:    $OPENAI_MODEL"
echo ""

bazelisk test -k \
  --spawn_strategy=local \
  --test_tag_filters=live_openai_api \
  --test_output=errors \
  --test_timeout=600 \
  --test_arg=-s \
  --test_arg=--log-cli-level=DEBUG \
  //...
