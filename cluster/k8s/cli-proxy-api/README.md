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

## Remote recovery (SSO-gated web UI)

The Codex/Claude OAuth sessions above periodically die (OpenAI/Anthropic invalidate the
refresh token) and previously could only be recovered by the interactive `kubectl exec`
device-login flow, which needs a machine with cluster access — not a phone. CLIProxyAPI's
own [management API](https://help.router-for.me/management/api) already covers this:
`GET /v0/management/{codex,anthropic,antigravity}-auth-url` returns a provider login URL
plus a `state`; `GET /v0/management/get-auth-status?state=...` polls it; `GET`/`POST
/v0/management/oauth-callback` completes it (unauthenticated, since it only carries the
provider's redirect). The rest of the management API and the bundled web UI it serves at
`/management.html` on the same port ride along.

`https://cli-proxy-api-admin.allegedly.works` exposes this — Gateway → Authentik embedded
outpost (SSO, `agentydragon` only, `tf/gitops/sso-providers/provider_cli_proxy_api_admin.tf`)
→ this Service. Authentik does not replace the app's own auth: every management endpoint
still requires the `cli-proxy-api-management` key (`Authorization: Bearer <key>` or
`X-Management-Key: <key>`, config `remote-management.secret-key`, wired here via the
`MANAGEMENT_PASSWORD` env var) underneath. Enabled via `remote-management.allow-remote:
true` in `config-eso.yaml` — CLIProxyAPI's config format has no narrower scoping for
remote endpoints, so this lifts the localhost restriction for the whole management
surface (account pool included), not just the OAuth-login endpoints; the SSO gate in
front, scoped to the account owner, is the accepted mitigation.

The existing `cli-proxy-api.allegedly.works` hostname is unrelated and unchanged — it only
ever routes unauthenticated `/v1` model traffic.

## Native OIDC image and pending migration

The patched image is built by `@ducktape_cli_proxy_api//:image` and tested by
`//third_party/cli_proxy_api_tests:tests` (upstream patch tests plus an actual
container boot with synthetic API keys). The image roster in
`devinfra/ci/image_targets.json` publishes it to
`git.allegedly.works/ducktape-ci/cli-proxy-api`. Its backend and management frontend
patches live in `third_party/cli_proxy_api/` for eventual upstream submission.

This build does not activate OIDC in the cluster. The deployment still pins the
upstream image and uses the Authentik proxy described above. Switching the provider
before publishing and validating the patched image would interrupt management access.
After publication, make the following changes together in a deployment PR:

1. Replace the proxy provider in
   `tf/gitops/sso-providers/provider_cli_proxy_api_admin.tf` with a confidential
   `authentik_provider_oauth2`, using `client_id = "cli-proxy-api-admin"`,
   `issuer_mode = "per_provider"`, `sub_mode = "user_id"`, the existing signing
   certificate, and the OpenID scope mapping. Keep the application slug and its
   `cli_proxy_api_admin_owner_only` policy binding. Register exactly
   `https://cli-proxy-api-admin.allegedly.works/v0/management/callback` as a strict
   redirect URI.
2. Have that Terraform module own an OIDC Secret and distribute it only to
   `cli-proxy-api`, following its existing provider Secret pattern. Wire its entries
   into the container with `secretKeyRef`; do not copy credentials into Git or the
   browser. Required environment values are:

   | Environment variable               | Value                                                             |
   | ---------------------------------- | ----------------------------------------------------------------- |
   | `MANAGEMENT_OIDC_ISSUER`           | `https://auth.allegedly.works/application/o/cli-proxy-api-admin/` |
   | `MANAGEMENT_OIDC_CLIENT_ID`        | Provider's `client_id`                                            |
   | `MANAGEMENT_OIDC_CLIENT_SECRET`    | Provider's generated `client_secret`                              |
   | `MANAGEMENT_OIDC_REDIRECT_URL`     | The exact callback URI above                                      |
   | `MANAGEMENT_OIDC_ALLOWED_SUBJECTS` | `tostring(authentik_user.agentydragon.id)`                        |

   The allowlist is an additional backend authorization check, independent of the
   Authentik application policy. `user_id` makes its value the same numeric user ID
   Terraform manages; a username or email is not the subject. Preserve
   `MANAGEMENT_PASSWORD` for AIQuota's direct management API calls and the existing
   client key for model consumers.

3. Pin a successfully published image, add `forgejo-images-creds` to the Pod's
   `imagePullSecrets`, and configure its bundled management
   UI as described in the image README. The pull Secret already belongs to
   `aiquota/forgejo-images-creds-eso.yaml` in this namespace; do not create a second
   ExternalSecret owning it. Add a colocated ImageRepository/ImagePolicy under
   `flux-image-automation-forgejo/` and an image-policy marker only after the registry
   contains a usable `devel-*` tag.
4. Move the admin HTTPRoute from `authentik/proxy-routes/` into this directory and
   point it directly at `cli-proxy-api:8317`. Update both Kustomizations, remove the
   old provider's embedded-outpost assignment, and remove the obsolete Authentik
   ingress rule from this service's NetworkPolicy. The existing Gateway ingress and
   direct LiteLLM rules remain necessary.
5. Verify a fresh browser reaches the dashboard through OIDC without a management
   key; a different Authentik user is denied; logout invalidates the local session;
   and expired sessions offer login again. Check forged proxy headers and
   cross-origin mutations cannot authorize management requests. Then verify both
   AIQuota's management-key access and LiteLLM's authenticated model request path.

Forgejo is the repository default for new private images and already serves AIQuota
in this namespace. A Forgejo outage prevents replacement image pulls when the image
is not cached; it does not interrupt a running CLIProxyAPI process.

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
