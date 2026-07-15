# CLIProxyAPI

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) is the gateway that lets
**Claude Code run on ChatGPT/Codex subscription models** (GPT-5.6-sol, …). It speaks
Anthropic `/v1/messages` to Claude Code and the ChatGPT Codex backend upstream, and —
unlike LiteLLM's `/v1/messages` bridge (BerriAI/litellm#25429) and claude-code-router —
**translates tool calls correctly** (`function_call` → `tool_use`). It goes direct to
`chatgpt.com/backend-api/codex`, holding its own Codex OAuth session.

The `codex-claude` laptop wrapper (<../../../../nix/home/claude_code/codex-claude.nix>)
points Claude Code at `https://cli-proxy-api.allegedly.works`.

## Models

`/model` lists the Codex slugs via gateway discovery. Defaults in the wrapper:

- main: `gpt-5.6-sol`
- background/Haiku tier: `gpt-5.6-luna` (the small 5.6 — `sol` is overkill for titles etc.)

Reasoning effort is driven by Claude Code's `effortLevel` setting and forwarded to Codex
`reasoning.effort` (not a model-slug suffix).

## One-time setup: Codex OAuth login

CLIProxyAPI needs its own Codex session. Once per PVC (the token is refreshed in place
afterward), run the device login against the running pod:

```bash
kubectl -n cli-proxy-api exec -it deploy/cli-proxy-api -- \
  ./CLIProxyAPI -codex-device-login -no-browser -config /config/config.yaml
```

Open the printed URL, enter the code, approve with the ChatGPT account. CLIProxyAPI writes
`/data/auth/auth.json` (PVC) and its file watcher loads it without a restart. The PVC
persists the token across pod restarts; the auto-refresh worker (15m) keeps it valid.

## Secrets

- `config.sops.yaml` — `cli-proxy-api-config` Secret holding `config.yaml` (port, auth-dir,
  `api-keys` client key). Flux decrypts via `sops-age-cluster-secrets`.
- `client-key.sops.yaml` — SSOT of the **same** client key, consumed by laptops and
  `codex@agent-box` via `ducktape.sopsEnv` (`CLIPROXY_CLIENT_KEY`) and reflected into
  `codex-pod` for its `codex-claude` wrapper. Keep it in sync with `config.sops.yaml`
  on rotation.

Rotate the client key: generate a new value, update both files (`sops -e -i` each), push.

## Session ownership (rotation)

CLIProxyAPI holds and refreshes a **dedicated** Codex OAuth session, independent of
LiteLLM's. This avoids the shared-token refresh race (LiteLLM's own deployment warns of it
for its replicas). LiteLLM keeps its separate session for its own consumers. If we ever
want a single rotation owner across both, that's a follow-up.
