#!/bin/bash
# Setup script for Claude Code web sessions.
#
# Installs:
#   1. claude-hooks wheel (hook dispatcher, statusline, ducktape-precommit, pre-commit)
#   2. Skills tarball (AI agent skills deployed to ~/.claude/skills/)
#
# Usage (Claude Code web UI setup command):
#   curl -fsSL https://raw.githubusercontent.com/agentydragon/ducktape/main/devinfra/claude/web_setup.sh | bash
#
# Override release tags:
#   DUCKTAPE_HOOKS_TAG=claude-hooks-abc12345 DUCKTAPE_SKILLS_TAG=skills-abc12345 bash web_setup.sh
set -euo pipefail

LOG_FILE="/tmp/web-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

HOOKS_TAG="${DUCKTAPE_HOOKS_TAG:-claude-hooks-latest}"
SKILLS_TAG="${DUCKTAPE_SKILLS_TAG:-skills-latest}"
BASE_URL="https://github.com/agentydragon/ducktape/releases/download"

echo "Installing claude-hooks wheel (tag: ${HOOKS_TAG})..."
uv tool install --force \
  --with-executables-from pre-commit \
  "${BASE_URL}/${HOOKS_TAG}/claude_hooks-0.1.0-py3-none-any.whl"

echo "Installing skills (tag: ${SKILLS_TAG})..."
mkdir -p ~/.claude/skills
curl -fsSL "${BASE_URL}/${SKILLS_TAG}/skills.tar" | tar xf - -C ~/.claude/skills/

echo "Setup complete. Log: ${LOG_FILE}"
