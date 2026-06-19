#!/bin/bash
# Environment setup script for Haku's Claude Code web "home".
#
# Wire it as the web environment's setup script (the setup command runs from the
# parent of the repo checkout, hence the ducktape/ prefix):
#   bash ducktape/haku/claude_web_env/setup.sh
# paired with these environment settings:
#   DUCKTAPE_CLAUDE_HOOKS_PROFILE=haku/claude_web_env/profile.yaml
#   SOPS_AGE_KEY=<the haku age key from secrets/haku-age-key.sops.yaml>
#
# It delegates to the shared web setup (devtools, claude-hook daemon, certs, git
# remotes); the haku profile above supplies the haku-specific kubeconfig + env so
# Haku comes up as group `haku` against the `haku-sandbox` namespace.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Install Haku's agent closure (.#agent-haku), which composes the shared
# `.#devtools` and adds the fastmcp MCP-client CLI — so Haku can talk to
# in-cluster MCP facades (tana-mcp-ro) turnkey-ly via
# `fastmcp call <url> --auth <bearer>`, no runtime pip install. Claude web
# installs the lean default `.#devtools`.
export DUCKTAPE_WEB_SETUP_OUTPUT=agent-haku
exec bash "${repo_root}/devinfra/claude/web_setup.sh"
