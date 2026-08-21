# CLIProxyAPI

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) is the gateway that lets
**Claude Code run on ChatGPT/Codex subscription models** (GPT-5.6-sol, …). It speaks
Anthropic `/v1/messages` to Claude Code and the ChatGPT Codex backend upstream, and —
unlike LiteLLM's `/v1/messages` bridge (BerriAI/litellm#25429) and claude-code-router —
**translates tool calls correctly** (`function_call` → `tool_use`). It goes direct to
`chatgpt.com/backend-api/codex`, holding its own Codex OAuth session.

The `codex-claude` wrapper points Claude Code at the main LiteLLM proxy
(`litellm.allegedly.works`), which fronts CLIProxyAPI as its `codex-*` upstream
(see `cluster/k8s/litellm/app/test_litellm_config.py`). The laptop/agent-box/codex-pod
consumers authenticate to LiteLLM with a scoped `codex-clients` virtual key; the client
key below is now consumed only by the main LiteLLM pod (ESO-mirrored into `litellm`).

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

- `client-key.sops.yaml` — SSOT of the client key. ESO renders it into
  `cli-proxy-api-config/config.yaml` for CLIProxyAPI and mirrors it into `litellm` as
  `CLIPROXY_CLIENT_KEY` for the `codex-*` upstream. Laptops/agent-box/codex-pod use a scoped
  `codex-clients` LiteLLM virtual key instead.
- `config-eso.yaml` — plaintext CLIProxyAPI configuration template. It includes three bounded
  stream bootstrap retries, which retry a failed upstream stream only before any response bytes
  have been sent to the caller.
- `management-key.sops.yaml` — SOPS-managed key shared only by CLIProxyAPI's
  management endpoint and the in-cluster aiquota CLIProxyAPI integration.

Rotate the client key: generate a new value and update `client-key.sops.yaml` only, then push.

## Session ownership (rotation)

CLIProxyAPI holds and refreshes a **dedicated** Codex OAuth session, independent of
LiteLLM's. This avoids the shared-token refresh race (LiteLLM's own deployment warns of it
for its replicas). LiteLLM keeps its separate session for its own consumers. If we ever
want a single rotation owner across both, that's a follow-up.
