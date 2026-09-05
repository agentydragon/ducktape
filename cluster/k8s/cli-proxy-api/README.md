# CLIProxyAPI

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) is the gateway that lets
**Claude Code run on ChatGPT/Codex subscription models** (GPT-6 Astra, GPT-5.6-sol, …). It speaks
Anthropic `/v1/messages` to Claude Code and the ChatGPT Codex backend upstream, and —
unlike LiteLLM's `/v1/messages` bridge (BerriAI/litellm#25429) and claude-code-router —
**translates tool calls correctly** (`function_call` → `tool_use`). It goes direct to
`chatgpt.com/backend-api/codex`, holding its own Claude and Codex OAuth sessions.

The `codex-claude` wrapper points Claude Code at the main LiteLLM proxy
(`litellm.allegedly.works`), which fronts CLIProxyAPI as its `codex-*` upstream
(see `cluster/k8s/litellm/app/test_litellm_config.py`). The laptop/agent-box/codex-pod
consumers authenticate to LiteLLM with a scoped `codex-clients` virtual key; the client
key below is now consumed only by the main LiteLLM pod (ESO-mirrored into `litellm`).

## Models

`/model` lists the Codex slugs via gateway discovery. Defaults in the wrapper:

- available flagship: `gpt-6-astra`
- main: `gpt-6-astra`
- background/Haiku tier: `gpt-5.6-luna` (the small 5.6 — `sol` is overkill for titles etc.)

Reasoning effort is driven by Claude Code's `effortLevel` setting and forwarded to Codex
`reasoning.effort` (not a model-slug suffix).

The deployed CLIProxyAPI v7.2.135 discovered `gpt-6-astra` and completed live
tool-call probes on both `/v1/responses` (`function_call`) and `/v1/messages`
(`tool_use`) on 2026-09-05. Its remote model catalog makes Astra usable even
though that binary predates the model's release.

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

## One-time setup: Claude OAuth login

Run this only when adding or re-authenticating the Claude subscription. Keep the
callback local: do not publish port `54545` through the Service or public route.

In one terminal, forward the callback port to the pod:

```bash
kubectl -n cli-proxy-api port-forward deploy/cli-proxy-api 54545:54545
```

In a second terminal, start the one-shot login process:

```bash
kubectl -n cli-proxy-api exec -it deploy/cli-proxy-api -- \
  ./CLIProxyAPI -claude-login -no-browser \
  -oauth-callback-port 54545 -config /config/config.yaml
```

Open the printed Anthropic authorization URL in the local browser and finish
the login. Anthropic redirects to `http://localhost:54545/callback`; the
port-forward delivers that callback to the process in the pod. CLIProxyAPI
writes the Claude auth file, including its refresh token, under `/data/auth`.
The normal server process watches that directory and then owns future refreshes.

Verify only the file names, never the credential contents:

```bash
kubectl -n cli-proxy-api exec deploy/cli-proxy-api -- \
  sh -c 'find /data/auth -maxdepth 1 -type f -printf "%f\\n"'
```

The existing SOPS-managed Claude setup token and its egress proxy remain in
place for the existing Haku Claude runner. AIQuota has no fallback token path;
this keeps the quota service from maintaining two Claude credential owners.

AIQuota uses the management API's opaque `auth_index` only because the current
`/api-call` contract requires it for `$TOKEN$` substitution. It never reads or
stores the auth file or either OAuth token.

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

CLIProxyAPI holds and refreshes **dedicated** Claude and Codex OAuth sessions. LiteLLM owns no
subscription OAuth credential or auth PVC: its `chatgpt/*` routes proxy model traffic to
CLIProxyAPI with an API key, while its `anthropic-api/*` routes use the separate Anthropic API
key and its `anthropic-max20/*` routes use the Claude subscription session. This keeps AIQuota
from mounting or writing the CLIProxyAPI PVC.
