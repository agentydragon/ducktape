#!/bin/bash
# web_setup_hook.sh — Claude Code Setup hook entry point.
#
# Runs web_setup.sh on web sessions to ensure the claude-hooks wheel is fresh
# before SessionStart fires. This closes the "stale wheel on resume-cached
# sessions" gap: the init_script (web_setup.sh) that normally re-installs
# devtools is NOT sent by Anthropic's backend for resume-cached sessions, so
# without this hook the installed wheel can lag behind npins/sources.json by
# many hours (however long the Firecracker VM stays alive between new sessions).
#
# Unlike the init_script invocation of web_setup.sh (which runs BEFORE claude
# and does not see user-UI env vars), this Setup hook fires as a subprocess of
# `claude` and inherits the full user-UI env — including
# DUCKTAPE_CLAUDE_HOOK_IMPL, which selects Python or Rust claude-hook. Set
# DUCKTAPE_CLAUDE_HOOK_IMPL=rust in the Claude Code web UI to switch impls
# without branching or modifying settings.json. web_setup.sh reads the var,
# removes the stale devtools profile, and reinstalls the selected flake output.
#
# No-ops on CLI sessions (CLAUDE_CODE_REMOTE unset) — web_setup.sh is
# web-only (Nix flake install, git remote, etc.) and must not run on NixOS.
#
# IMPORTANT: this script must NOT call claude-hook. The Setup hook fires with
# the new post-compaction session ID while SessionStart uses the old ID.
# Starting the daemon here would create a daemon for the wrong session ID and
# trigger a second daemon start when SessionStart arrives. A raw bash script
# that never calls claude-hook is safe.

set -euo pipefail

# Append to the same log file web_setup.sh uses so one file covers both.
LOG_FILE="/tmp/web-setup.log"
exec >>"$LOG_FILE" 2>&1

# Read stdin first — Claude Code hooks always receive JSON on stdin; consuming
# it avoids a broken pipe if Claude closes the write end before we exit.
# Logged verbatim so we can observe what (if anything) Setup hooks receive.
STDIN_DATA=$(cat)
echo "[$(date -Iseconds)] web_setup_hook.sh: cwd=$(pwd) BASH_SOURCE=${BASH_SOURCE[0]}"
echo "[$(date -Iseconds)] web_setup_hook.sh: CLAUDE_CODE_REMOTE=${CLAUDE_CODE_REMOTE:-<unset>} IS_SANDBOX=${IS_SANDBOX:-<unset>} DUCKTAPE_CLAUDE_HOOK_IMPL=${DUCKTAPE_CLAUDE_HOOK_IMPL:-<unset>}"
echo "[$(date -Iseconds)] web_setup_hook.sh: stdin=${STDIN_DATA:-(empty)}"

if [ -z "${CLAUDE_CODE_REMOTE:-}" ]; then
  echo "[$(date -Iseconds)] web_setup_hook.sh: CLI session (CLAUDE_CODE_REMOTE unset) — skipping"
  exit 0
fi

echo "[$(date -Iseconds)] web_setup_hook.sh: web session detected — delegating to web_setup.sh"
exec bash "$(dirname "${BASH_SOURCE[0]}")/web_setup.sh"
