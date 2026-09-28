#!/usr/bin/env bash
# codex_turn.sh — drive ONE orchestrator->worker turn (new session or resume) and
# print the worker's reply. Encapsulates the flag-ordering gotcha (exec options must
# precede the `resume` subcommand) and the thread-id/final-message extraction.
#
# Usage:  codex_turn.sh <workdir> <new|SESSION_ID> <prompt>
# Env:
#   CODEX_BIN         codex binary (default: `codex` on PATH)
#   LITELLM_API_KEY   the LiteLLM virtual key (or set LITELLM_KEY_FILE to a file holding it)
#   LITELLM_KEY_FILE  optional path to read the key from when LITELLM_API_KEY is unset
#   CODEX_FLEET_HOME  CODEX_HOME for the worker (default: $HOME/.cache/codex-fleet).
#                     Resume needs the SAME value across invocations (session history lives here).
#   CODEX_WORKER_MODEL  model slug (default: chatgpt/oai-responses/gpt-5.6-luna)
# Output (stdout): first line  THREAD=<uuid>,  then the worker's final message text.
set -euo pipefail
CODEX_BIN="${CODEX_BIN:-codex}"
export CODEX_HOME="${CODEX_FLEET_HOME:-$HOME/.cache/codex-fleet}"
mkdir -p "$CODEX_HOME"
if [ -z "${LITELLM_API_KEY:-}" ] && [ -n "${LITELLM_KEY_FILE:-}" ]; then
  LITELLM_API_KEY="$(cat "$LITELLM_KEY_FILE")"
fi
: "${LITELLM_API_KEY:?set LITELLM_API_KEY or LITELLM_KEY_FILE}"
export LITELLM_API_KEY
model="${CODEX_WORKER_MODEL:-chatgpt/oai-responses/gpt-5.6-luna}"
# Ensure a LiteLLM/Responses config exists (idempotent). Points wire_api=responses at the
# cluster LiteLLM; env_key names the var holding the virtual key.
if [ ! -f "$CODEX_HOME/config.toml" ]; then
  cat >"$CODEX_HOME/config.toml" <<CFG
model = "$model"
model_provider = "litellm"
approval_policy = "never"
sandbox_mode = "danger-full-access"
model_reasoning_effort = "${CODEX_WORKER_EFFORT:-low}"
model_context_window = 372000
[model_providers.litellm]
name = "Cluster LiteLLM"
base_url = "${LITELLM_BASE_URL:-https://litellm.allegedly.works/v1}"
env_key = "LITELLM_API_KEY"
wire_api = "responses"
CFG
fi
workdir="$1"
sid="$2"
prompt="$3"
mkdir -p "$workdir"
out="$(mktemp)"
# GOTCHA: exec options must come BEFORE the `resume` subcommand; `</dev/null` or exec hangs on stdin.
if [ "$sid" = "new" ]; then
  "$CODEX_BIN" exec --json --skip-git-repo-check -C "$workdir" "$prompt" </dev/null >"$out" 2>/dev/null || true
else
  "$CODEX_BIN" exec --json --skip-git-repo-check -C "$workdir" resume "$sid" "$prompt" </dev/null >"$out" 2>/dev/null || true
fi
python3 - "$out" <<'PY'
import json, sys
sid = final = None
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    try:
        o = json.loads(line)
    except Exception:
        continue
    if o.get("type") == "thread.started":
        sid = o.get("thread_id")
    it = o.get("item", {}) if isinstance(o.get("item"), dict) else {}
    if it.get("type") in ("agent_message", "assistant_message"):
        final = it.get("text")
print(f"THREAD={sid}")
print(final if final is not None else "(no final message)")
PY
rm -f "$out"
