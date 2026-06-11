#!/usr/bin/env bash
# Wrapper script to automatically copy LLM API keys from environment to VM

set -euo pipefail

# Auto-export LLM API keys if they exist
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  export TF_VAR_openai_api_key="$OPENAI_API_KEY"
  echo "✓ Detected OPENAI_API_KEY - will be copied to VM"
fi

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  export TF_VAR_anthropic_api_key="$ANTHROPIC_API_KEY"
  echo "✓ Detected ANTHROPIC_API_KEY - will be copied to VM"
fi

# Run terraform with all arguments passed through
terraform "$@"
