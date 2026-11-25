# CLIProxyAPI

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) is the gateway that lets
**Claude Code run on ChatGPT/Codex subscription models** (GPT-6 Astra, GPT-5.6-sol, …). It speaks
Anthropic `/v1/messages` to Claude Code and the ChatGPT Codex backend upstream, and —
unlike LiteLLM's `/v1/messages` bridge (BerriAI/litellm#25429) and claude-code-router —
**translates tool calls correctly** (`function_call` → `tool_use`). It goes direct to
`chatgpt.com/backend-api/codex`, holding its own Claude and Codex OAuth sessions.

The `codex-claude` wrapper points Claude Code at the main LiteLLM proxy
(`litellm.allegedly.works`), which fronts CLIProxyAPI as its `codex-*` upstream
(see `cluster/cdk8s/litellm/test_config.py`). The laptop and agent-box consumers
authenticate to LiteLLM with the shared `codex-clients` virtual key; codex-pod has a
separate GPT-6-only Messages key. The client key below is now consumed only by the main
LiteLLM pod (ESO-mirrored into `litellm`).

## Models

`/model` lists the Codex slugs via gateway discovery. Defaults in the wrapper:

- available flagship: `gpt-6-astra`
- main: `gpt-6-astra`
- background/Haiku tier: `gpt-6-luna` (the small 6 — `sol` is overkill for titles etc.)

Reasoning effort is driven by Claude Code's `effortLevel` setting and forwarded to Codex
`reasoning.effort` (not a model-slug suffix).

On 2026-09-05, CLIProxyAPI v7.2.135 discovered `gpt-6-astra` and completed live
tool-call probes on both `/v1/responses` (`function_call`) and `/v1/messages`
(`tool_use`): its remote model catalog made Astra usable although that binary
predated the model's release.

## Remote recovery (SSO-gated web UI)

Use the [management UI](https://cli-proxy-api-admin.allegedly.works/management.html)
to add or renew a Codex, Claude or Antigravity OAuth session from a browser.

1. Choose **Sign in with SSO**, then open **OAuth Login**.
2. Choose **Start Codex Login**, **Start Anthropic Login** or **Start Antigravity Login**.
   Open the authorization link and complete sign-in in the provider's tab.
3. When the provider redirects to a localhost URL, copy that full URL from the address
   bar back into the management UI and choose **Submit Callback URL**.
4. Wait for authentication to succeed, then verify the account appears in **Auth Files**.

The Gateway routes the admin host directly to CLIProxyAPI. Authentik policy and the
backend subject allowlist restrict sign-in to `agentydragon`. The browser session
authenticates same-origin `/v8/management` requests. The
[OIDC configuration](../../../third_party/cli_proxy_api/README.md#oidc-configuration)
documents session expiry, logout and management-key authentication.

The confidential provider and reflected `cli-proxy-api-admin-oidc` Secret are owned
by [the Authentik Terraform configuration](../../../tf/gitops/sso-providers/provider_cli_proxy_api_admin.tf).
It uses the owner's numeric Authentik ID as the subject and registers
`https://cli-proxy-api-admin.allegedly.works/v8/management/auth/oidc/callback`
for SSO sign-in. The provider redirect pasted in step 3 belongs to the separate
subscription OAuth flow.

AIQuota calls the in-cluster `/v0/management` API with `MANAGEMENT_PASSWORD`. Its
`/api-call` requests use the opaque `auth_index` for `$TOKEN$` substitution.
The `cli-proxy-api.allegedly.works` hostname only routes `/v1` model requests.

## Login with kubectl

Use these commands when you have cluster access and need to log in from the running
pod. The server watches `/data/auth` on the state PVC and refreshes the saved
credentials. Verify the account in **Auth Files**. The packaged image is
distroless, so shell commands such as `sh` and `find` are unavailable.

### Codex device login

```bash
kubectl -n cli-proxy-api exec -it deploy/cli-proxy-api -- \
  ./CLIProxyAPI -codex-device-login -no-browser -config /config/config.yaml
```

Open the printed URL, enter the device code and approve with the ChatGPT account.

### Claude OAuth login

Forward the callback port locally. Keep port `54545` off the Service and public route.

```bash
kubectl -n cli-proxy-api port-forward deploy/cli-proxy-api 54545:54545
```

In a second terminal, start the one-shot login process.

```bash
kubectl -n cli-proxy-api exec -it deploy/cli-proxy-api -- \
  ./CLIProxyAPI -claude-login -no-browser \
  -oauth-callback-port 54545 -config /config/config.yaml
```

Open the printed Anthropic authorization URL in the local browser and finish login.
The port-forward delivers the provider redirect at `http://localhost:54545/callback`
to the login process in the pod.

## Patched image

`@ducktape_cli_proxy_api//:image` is built from `third_party/cli_proxy_api`; its OIDC forks,
patches, tests and settings are in <../../../third_party/cli_proxy_api/README.md>. CI
publishes `git.allegedly.works/ducktape-ci/cli-proxy-api`; Flux tracks its `devel-*` tags.

The namespace's `forgejo-images-creds` pull Secret belongs to the aiquota chart
(`cluster/cdk8s/aiquota.py`). A Forgejo outage prevents uncached replacement
image pulls but does not interrupt a running process.

## Secrets

- `client-key.sops.yaml` — SSOT of the client key. ESO renders it into
  `cli-proxy-api-config/config.yaml` for CLIProxyAPI and mirrors it into `litellm` as
  `CLIPROXY_CLIENT_KEY` for the `codex-*` upstream. Laptop and agent-box use the scoped
  `codex-clients` LiteLLM virtual key; codex-pod uses a GPT-6-only key.
- The `cli-proxy-api-config` ExternalSecret (`cluster/cdk8s/cli_proxy_api/cli_proxy_api.py`) —
  plaintext CLIProxyAPI configuration template. It includes three bounded
  stream bootstrap retries, which retry a failed upstream stream only before any response bytes
  have been sent to the caller.
- `management-key.sops.yaml` — SOPS-managed key shared only by CLIProxyAPI's
  management endpoint and the in-cluster aiquota CLIProxyAPI integration.

Rotate the client key: generate a new value and update `client-key.sops.yaml` only, then push.

## Session ownership (rotation)

CLIProxyAPI holds and refreshes **dedicated** Claude and Codex OAuth sessions. LiteLLM owns no
subscription OAuth credential or auth PVC: its `chatgpt-sub/*` routes proxy model traffic to
CLIProxyAPI with an API key, while its `anthropic-api/*` routes use the separate Anthropic API
key and its `anthropic-max20/*` routes use the Claude subscription session. AIQuota uses
the management client for quota reads and holds no provider OAuth credentials.
