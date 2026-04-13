# Grocy Machine Auth Investigation

**Goal**: Agents in the cluster can call the Grocy API at `grocy.allegedly.works`
through Airlock, reusing existing human OIDC SSO.

## Current architecture

```
Airlock backend registration "grocy"
        │  streamable HTTP over JSON-RPC
        ▼
Service: grocy-mcp.grocy-mcp.svc.cluster.local:3000
        │
        ▼
Pod: grocy-mcp (two-container sidecar pattern)
 ┌──────────────────────────────────────────────────────────────────┐
 │  grocy-mcp (container, port 3000)                                │
 │  ghcr.io/saya6k/mcp-grocy-api                                    │
 │  Off-the-shelf MCP server — 40+ Grocy tools.                     │
 │  Sends GROCY-API-KEY on every request to GROCY_BASE_URL.         │
 │                                                                  │
 │                      │  HTTP, localhost:8080, GROCY-API-KEY=dummy│
 │                      ▼                                           │
 │                                                                  │
 │  grocy-auth-proxy (container, port 8080)                         │
 │  envoyproxy/envoy:distroless-v1.32-latest                        │
 │  Envoy with envoy.filters.http.credential_injector + the         │
 │  oauth2 extension. Strips GROCY-API-KEY and Authorization via    │
 │  RouteConfiguration.request_headers_to_remove. Fetches a Bearer  │
 │  JWT from Authentik via the standard OAuth2 client_credentials   │
 │  grant (client_id + client_secret_b64), caches it, forwards to   │
 │  upstream with Authorization: Bearer <jwt>.                      │
 └──────────────────────────────────────────────────────────────────┘
        │  HTTPS, Bearer JWT
        ▼
https://grocy.allegedly.works   ← Gateway API / Cilium Envoy
        │
        ▼
Authentik embedded proxy outpost (runs in authentik-server pods)
 validates the JWT, injects X-authentik-username: grocy-machine,
 forwards to the upstream.
        │
        ▼
http://grocy.grocy.svc.cluster.local:80
 Grocy's ReverseProxyAuthMiddleware reads X-authentik-username.
```

Grocy uses `ReverseProxyAuthMiddleware` trusting `X-authentik-username`.
The proxy outpost assumes it is the sole source of that header; the auth
proxy sidecar strips any client-supplied `Authorization` before injecting
its Bearer JWT so clients can't bypass the M2M flow.

## Pieces in the repo

| Path                                                         | Purpose                                                                                                                                           |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cluster/terraform/gitops/agent-machine-access/main.tf`      | Authentik proxy provider + application + policy bindings + service account + app password + K8s secret + flux-system `grocy-envoy-vars` ConfigMap |
| `cluster/k8s/agents/grocy-mcp/envoy.yaml`                    | Envoy bootstrap config (credential_injector + oauth2 extension), wrapped into a ConfigMap by `configMapGenerator` in `kustomization.yaml`         |
| `cluster/k8s/agents/grocy-mcp/deployment.yaml`               | Two-container pod (Envoy auth proxy sidecar + MCP server)                                                                                         |
| `cluster/k8s/agents/grocy-mcp/service.yaml`                  | ClusterIP on port 3000 for Airlock to reach                                                                                                       |
| `cluster/k8s/agents/grocy-mcp/flux-kustomization.yaml`       | Flux wiring; depends on `agent-machine-access-tf` + `reflector`; `postBuild.substituteFrom` `grocy-envoy-vars` for `${CLIENT_ID}`                 |
| `cluster/k8s/agents/airlock/config.yaml`                     | Registers `grocy` backend at `http://grocy-mcp.grocy-mcp.svc.cluster.local:3000/mcp`                                                              |
| `cluster/k8s/grocy/settingoverrides.yaml`                    | `AUTH_CLASS=ReverseProxyAuthMiddleware`, `REVERSE_PROXY_AUTH_HEADER=X-authentik-username`                                                         |
| `cluster/k8s/authentik/app/blueprints/embedded-outpost.yaml` | `!Find [authentik_providers_proxy.proxyprovider, [name, grocy]]` binds grocy to the shared embedded outpost                                       |
| `cluster/k8s/authentik/proxy-routes/grocy-httproute.yaml`    | Gateway API HTTPRoute: `grocy.allegedly.works → authentik-server:80`                                                                              |

## Authentik M2M flow (what the auth proxy actually does)

Authentik's proxy providers don't export a `client_secret` through the
Terraform provider schema, so we can't use the standard OAuth2
`client_id` + `client_secret` grant. Authentik's intended M2M flow for
proxy providers is different: a service account is identified by
`client_id`, but authenticated by **username + app_password**.

TF provisions:

- `authentik_user.grocy_machine_sa` — `username = "grocy-machine"`,
  `type = "service_account"`
- `authentik_token.grocy_machine_app_password` — `intent = "app_password"`,
  `expiring = true`, `retrieve_key = true`
- `authentik_policy_binding` — binds the SA to the grocy application so it
  passes the outpost's authorization check
- `kubernetes_secret.grocy_machine_credentials` — written to
  `claude-sandbox/grocy-machine-credentials` with Reflector annotations
  mirroring to `openclaw-sandbox,grocy-mcp`; data:
  - `client_id` (from `authentik_provider_proxy.grocy.client_id`, read-only)
  - `client_secret_b64` = `base64("<username>:<app_password>")`

Token exchange (performed by Envoy's oauth2 credential injector):

```
POST https://auth.allegedly.works/application/o/token/
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=<provider client_id>
&client_secret=<base64("grocy-machine:<app_password>")>
```

Response: `{"access_token": "<jwt>", "token_type": "Bearer", "expires_in": 86400, ...}`

The JWT's `iss` is `https://auth.allegedly.works/application/o/grocy/` and
`aud` matches the proxy provider's `client_id`.

Authentik accepts `client_secret = base64("<username>:<app_password>")` as
an equivalent encoding of the M2M app-password credential, which lets the
flow plug into Envoy's native `client_credentials` shape without any custom
auth logic.

## grocy-mcp container quirks

The `ghcr.io/saya6k/mcp-grocy-api` image is primarily a Home Assistant
add-on. Two quirks that required deployment-level workarounds:

1. **s6-overlay run script reads from HA supervisor config.** The image's
   `rootfs/etc/s6-overlay/app/run` overwrites every relevant env var via
   `bashio::config 'grocy_base_url'` etc., which returns empty without an
   HA supervisor. `set -e` then kills the container. **Workaround**:
   bypass s6-overlay entirely by setting `command: ["/bin/sh", "-c"]` +
   `args: ["sleep infinity | node build/index.js"]` and `workingDir: /app`.

2. **`src/index.ts::run()` always starts stdio transport first, before HTTP.**
   Without a TTY in Kubernetes the container's stdin EOFs immediately,
   the stdio transport tears down, and the Node process exits before the
   HTTP server on port 3000 finishes binding. **Workaround**: the
   `sleep infinity | node ...` pipeline keeps a write end of node's stdin
   open forever, so the stdio reader blocks harmlessly and the HTTP server
   starts normally.

Both workarounds are documented inline in `cluster/k8s/agents/grocy-mcp/deployment.yaml`.

## Machine auth options considered

### Option A — Authentik M2M via app password (**implemented**)

The flow described above. Pros: consistent with Authentik's intended M2M
path for proxy providers; all creds managed by TF; audit trail at the
Authentik layer; per-SA revocation; uses stock Envoy (no custom code to
maintain). Cons: requires the Envoy sidecar translator because off-the-
shelf Grocy MCP servers only speak the native `GROCY-API-KEY` header.

### Option B — Grocy native API key

Provision a long-lived API key in Grocy's UI for a dedicated machine
user, store it in a K8s secret, send it as `GROCY-API-KEY` directly.
Simpler (no token exchange, no auth proxy), but Grocy has no API to
programmatically create users or API keys (UI-only bootstrap), keys are
long-lived, and there's no Authentik audit trail. Would pair with
`skip_path_regex = "^/api/"` on the proxy provider to let `/api/*`
bypass the outpost entirely.

### Option C — Direct header injection, no Authentik

An in-cluster client with network access to `grocy.grocy.svc.cluster.local:80`
could set `X-authentik-username` itself. Rejected: `ReverseProxyAuthMiddleware`
assumes a trusted proxy strips any client-supplied copy of the header, so
direct injection is only safe behind a tight network policy, and the
`grocy` namespace's CiliumNetworkPolicy currently restricts ingress to
the Authentik outpost + Gatus only.

### Option D — Disable auth entirely

Set `DISABLE_AUTH=true` and rely on network-level ACLs. Rejected — far
too broad; any in-cluster pod that can reach Grocy becomes admin.

## Historical attempts that didn't work

The first three approaches tried before landing on the current M2M app
password flow:

### 1. Authentik API token as Bearer (failed)

The `agent-bearer-token` K8s secret contains an Authentik API token
(`intent = "api"`). The proxy outpost doesn't accept API tokens — it
expects a browser session cookie or a JWT issued by its own provider.

### 2. Separate OAuth2 provider with client_credentials (failed)

Tried creating a standalone `grocy-machine` OAuth2 provider in TF and
exchanging `client_credentials` for a JWT. Got a valid JWT but the
proxy outpost rejected it because the `azp`/`aud` claim didn't match
its own provider's `client_id`. **Cross-provider JWTs don't work with
proxy outposts** — the outpost only accepts JWTs from its own provider.

### 3. Proxy provider with client_id + client_secret (failed)

Moved the `grocy` proxy provider into the same TF module and tried
reading `client_secret` from `authentik_provider_proxy.grocy.client_secret`.
**`client_secret` is not exported** by the `goauthentik/authentik` TF
provider for `authentik_provider_proxy` — only `client_id` is. The
underlying Authentik API does return it (proxy providers inherit from
`OAuth2Provider`), but the TF resource schema omits it.

This is what forced the switch to Option A's M2M app password flow,
which only needs `client_id` (exported) + a separately-managed
`authentik_token` for the service account.

## Verified current state (2026-04-13)

End-to-end test, reading live credentials from
`claude-sandbox/grocy-machine-credentials` and hitting the public ingress:

```bash
# 1. M2M token exchange against Authentik
curl -X POST https://auth.allegedly.works/application/o/token/ \
    -d grant_type=client_credentials \
    -d client_id=<client_id> \
    -d client_secret=<client_secret_b64>
# → {"token_type":"Bearer","expires_in":86400,"access_token":"..."}  ✓

# 2. Outpost reachability (no auth)
curl -I https://grocy.allegedly.works/outpost.goauthentik.io/auth/nginx
# → HTTP/2 302
#   location: .../outpost.goauthentik.io/start?rd=...
#   set-cookie: authentik_proxy_328f0ca8=...   (provider-specific cookie)   ✓

# 3. Full path with Bearer JWT
curl -H "Authorization: Bearer $JWT" https://grocy.allegedly.works/api/system/info
# → HTTP/2 500 (from Grocy, not Authentik)
#   {"error_message":"ReverseProxyAuthMiddleware: X-authentik-username header is missing or invalid"}
```

### What works

- **M2M client_credentials** exchange returns a valid Bearer JWT (≈673 bytes,
  24h validity) when you encode `client_secret = base64("<username>:<app_password>")`
  — this form is Authentik's accepted encoding for M2M app-password auth.
- **Outpost binding to the grocy provider exists**: `/outpost.goauthentik.io/auth/nginx`
  302s into the authorize flow and issues a provider-specific session cookie.
  This confirms the embedded outpost's `providers` M2M includes the current
  `authentik_provider_proxy.grocy` PK.
- **The JWT is accepted by the outpost**: the request is forwarded to `internal_host`
  (i.e. the Grocy container at `grocy.grocy.svc.cluster.local:80`) — we know it
  reached Grocy because the response body is a PHP stack trace from
  `/app/www/middleware/ReverseProxyAuthMiddleware.php`.

### What doesn't work — the real current blocker

**Authentik's embedded outpost does not inject `X-authentik-username` (or any
`X-authentik-*` header) on the forwarded request when the caller authenticated
via a Bearer JWT.** Grocy's `ReverseProxyAuthMiddleware` therefore rejects the
request with HTTP 500 because the header it's configured to trust is missing.

Hypotheses (not yet verified against Authentik source):

1. Proxy-mode header injection (`X-authentik-username`, `-email`, `-groups`,
   `-uid`, `-meta-*`) may only fire on the browser session path (i.e. after
   a successful authorize-flow redirect), not on the Bearer-JWT short-circuit
   that reuses the same provider.
2. Service-account users might be skipped by the header-injection step, even
   though they carry a `username` — e.g. because they lack a primary email or
   a human `name`.
3. The `proxy` provider mode expects forward-auth upstream semantics for
   header injection; with a direct JWT call the outpost might bypass the
   header-building step entirely.

This supersedes the earlier "stale outpost binding" theory. The binding is fine;
header injection on the JWT path is the problem.

### What to investigate next

- Read Authentik's source (`authentik/providers/proxy/api/proxyprovider.py` and
  the outpost's Go proxy code in `outpost/proxyv2/application/`) to find the
  header-injection branch and confirm whether it fires on JWT-authenticated
  requests.
- Try setting `X-authentik-username: grocy-machine` directly from inside the
  cluster bypassing the outpost (would require a CiliumNetworkPolicy exception).
  That's Option C in the options survey above — currently rejected but might
  be the cleanest path if the outpost can't be coerced into injecting headers
  for JWT callers.
- Alternatively: reshape the flow so the Envoy sidecar obtains a browser-style
  session cookie via the authorize flow (requires a headless login, which for
  service accounts is possible through the token endpoint). The outpost DOES
  inject headers on session-authenticated requests.

## Known issue — outpost providers list doesn't auto-refresh (resolved 2026-04-13)

The `embedded-outpost.yaml` blueprint hand-curates the outpost's providers list
via `!Find [authentik_providers_proxy.proxyprovider, [name, <x>]]` entries.
Authentik's blueprint file watcher only re-applies the blueprint when the
underlying ConfigMap's `resourceVersion` bumps (i.e. when the file content
changes in git) or when the periodic blueprint scheduler fires (~every 23h).

If a TF-managed proxy provider is created **after** the most recent blueprint
apply, `!Find` resolves it, but only on the next apply. Between "provider
created" and "next blueprint apply", the outpost's providers list has no
binding for that hostname, and Authentik returns its brand 404 for any
request to that host.

This is exactly what happened for grocy: the blueprint already had
`!Find [name, grocy]`, but no subsequent blueprint re-apply had occurred since
the grocy provider's most recent state change, so the outpost's `providers`
M2M never repopulated.

**Accidental fix**: commit `be28f6fac` (#1267) added
`!Find [name, authentik-mcp-poc-backend]` to the same blueprint file for a
completely unrelated reason. The file change bumped the ConfigMap's
`resourceVersion`, Authentik's blueprint watcher re-applied the blueprint,
`!Find` re-resolved **every** entry in the providers list, and grocy's binding
was restored as a side effect.

Direct verification after that commit landed:

```
grocy.allegedly.works/outpost.goauthentik.io/auth/nginx  → HTTP/2 302  ✓
```

This makes PR #1248 ("touch-embedded-outpost-blueprint") obsolete — that PR's
sole purpose was to force the exact re-apply that `be28f6fac` already forced.

### Architectural smell

TF owns the providers; the blueprint file owns the outpost→providers M2M.
There is **no automatic sync** between the two. Any time a provider is added,
renamed, or destroyed/recreated in TF, the blueprint must also be re-applied —
but the blueprint only re-applies on its own file change or the ~23h scheduler.
Until then, the new provider is invisible to the outpost.

Cleaner alternatives (not yet done):

1. **Move the outpost→providers binding into TF** alongside the provider
   itself. The goauthentik TF provider has `authentik_outpost` — the grocy
   module could append to the embedded outpost's providers list at apply time.
   Risk: the embedded-outpost.yaml blueprint is still the source of truth for
   most other providers, so TF would need to coexist with it without fighting.
2. **Kill the hand-curated list** and bind providers through an
   `authentik_policy_binding` or per-provider blueprint that registers itself
   with the outpost. Each new provider then drags its own outpost binding along.
3. **Drop the periodic scheduler dependency** by wiring a Flux
   `HelmRelease`/`Kustomization` post-apply hook that rolls the authentik-worker
   pods whenever `embedded-outpost.yaml` changes — but this doesn't help the
   "TF created a provider, blueprint didn't change" case, which is the actual
   failure mode.

Keep the inline note on the blueprint file in place so the next person hitting
this failure mode knows to touch the file as the quickest patch.
