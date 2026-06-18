#!/bin/bash
# Environment setup script for Haku's Claude Code web "home".
#
# Wire it as the web environment's setup script:
#   bash haku/claude_web_env/setup.sh
# paired with these environment settings:
#   DUCKTAPE_CLAUDE_HOOK_IMPL=rust
#   DUCKTAPE_CLAUDE_HOOKS_PROFILE=haku/claude_web_env/profile.yaml
#   SOPS_AGE_KEY=<the haku age key from secrets/haku-age-key.sops.yaml>
#
# It delegates to the shared web setup (devtools, claude-hook daemon, certs, git
# remotes); the haku profile above supplies the haku-specific kubeconfig + env so
# Haku comes up as group `haku` against the `haku-sandbox` namespace.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec bash "${repo_root}/devinfra/claude/web_setup.sh"
