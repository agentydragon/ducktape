# Grocy Machine Auth Investigation

Context: Grocy runs at `grocy.allegedly.works` behind an Authentik proxy outpost.
Goal: enable agent/machine access without a human browser session.

## Grocy Authentication Architecture

### AUTH_CLASS (single-valued)

Grocy supports exactly **one** active auth class at a time, set via `AUTH_CLASS` in config
or settingoverrides. There is no multi-provider mode. Available classes:

| Class                                         | Mechanism                                                        |
| --------------------------------------------- | ---------------------------------------------------------------- |
| `Grocy\Middleware\DefaultAuthMiddleware`      | Username + password (Grocy DB), Argon2ID hashing                 |
| `Grocy\Middleware\ReverseProxyAuthMiddleware` | Trusts a header (or env var) injected by a reverse proxy         |
| `Grocy\Middleware\LdapAuthMiddleware`         | LDAP bind: service account search → user bind to verify password |
| (custom)                                      | Any class implementing `Grocy\Middleware\AuthMiddleware`         |

Auth bypass is a config toggle, not a class: set `DISABLE_AUTH=true` (or `GROCY_DISABLE_AUTH`
env var). All requests use the default user (user ID 1). Also auto-enabled in dev/demo/prerelease
`GROCY_MODE` values.

**API keys are orthogonal**: `ApiKeyAuthMiddleware` is the base of every class above.
All middleware chains check the `GROCY-API-KEY` header (or query param) _first_, before
falling through to the class-specific mechanism. So API keys work alongside any AUTH_CLASS.

### ReverseProxyAuthMiddleware (current)

Config:

| Setting                      | Default                 | Our value                    |
| ---------------------------- | ----------------------- | ---------------------------- |
| `AUTH_CLASS`                 | `DefaultAuthMiddleware` | `ReverseProxyAuthMiddleware` |
| `REVERSE_PROXY_AUTH_HEADER`  | `REMOTE_USER`           | `X-authentik-username`       |
| `REVERSE_PROXY_AUTH_USE_ENV` | `false`                 | `false` (use header)         |

Behavior:

- Reads username from the named header. Throws an exception (→ 500) if the header is missing or
  empty — no graceful fallback to another auth method.
- If the user doesn't exist in Grocy's DB, **auto-creates** it (empty password, default permissions).
- Sets `GROCY_EXTERNALLY_MANAGED_AUTHENTICATION=true` internally, which hides password fields in
  the user management UI.
- Also accepts `GROCY-API-KEY` (checked first via the shared base).
- **Security assumption**: the reverse proxy strips any client-supplied copy of the header.
  Authentik's outpost enforces this — clients cannot inject `X-authentik-username` directly.

### Native Grocy API Keys

Independent of AUTH_CLASS. Managed per-user in the Grocy UI
(Settings → Manage API keys) or via the API itself.

- **Header**: `GROCY-API-KEY: <key>` (recommended)
- **Query param**: `?GROCY-API-KEY=<key>` (not recommended)
- Keys are 50-char random strings, stored in DB with `user_id`, expiry, description.
- Each key is scoped to one user.
- Work even when `AUTH_CLASS` is `ReverseProxyAuthMiddleware`.

### LdapAuthMiddleware (not used here)

Config: `LDAP_ADDRESS`, `LDAP_BASE_DN`, `LDAP_BIND_DN`, `LDAP_BIND_PW`,
`LDAP_USER_FILTER`, `LDAP_UID_ATTR` (Windows AD: `sAMAccountName`, OpenLDAP: `uid`).

Flow: service-account bind → search for user DN → bind as user to verify password →
create/reuse session. Also checks API keys first.

## Settingoverrides Mechanism

Grocy resolves settings in priority order (highest first):

1. `/data/settingoverrides/<SETTING_NAME>.txt` — file content (trailing newlines stripped)
2. `GROCY_<SETTING_NAME>` environment variable (trailing whitespace stripped)
3. `config-dist.php` defaults

In this cluster, the settingoverrides directory is mounted via ConfigMap `grocy-settingoverrides`
(`cluster/k8s/grocy/settingoverrides.yaml`).

## Current Cluster Setup

```
client → grocy.allegedly.works
       → Gateway (cilium)
       → Authentik proxy outpost (authentik namespace)
       → Grocy pod (grocy namespace, port 80)
```

The Authentik proxy outpost:

1. Checks the request is authenticated (valid session cookie or Bearer JWT).
2. Injects `X-authentik-username: <user>` before forwarding.
3. Grocy reads that header via `ReverseProxyAuthMiddleware`.

SSO resource management: `cluster/terraform/gitops/agent-machine-access/main.tf`
(proxy provider + application + policy bindings for humans and machine SA).

## Machine Auth Options Considered

### Option A: OAuth2 client_credentials (current)

**Flow**: agent reads `client_id`+`client_secret` from K8s secret
`grocy-machine-credentials` → POSTs to Authentik token endpoint
(`/application/o/<slug>/token/`) → receives JWT → presents as
`Authorization: Bearer <jwt>` to `grocy.allegedly.works` → outpost validates JWT,
injects `X-authentik-username: ak-grocy-client_credentials` → Grocy auto-creates
that user on first request.

Credentials location: `grocy-machine-credentials` secret in `claude-sandbox`
(reflected to `openclaw-sandbox` via Reflector).

**Pros**: consistent with how other Authentik-fronted services do machine auth;
SSO policy enforced by Authentik (can revoke at Authentik layer without touching Grocy).

**Cons**: token refresh needed (JWTs expire per `access_token_validity = "hours=24"`);
extra round-trip to Authentik token endpoint before each Grocy session.

### Option B: Grocy native API key

**Flow**: generate an API key in Grocy UI for a dedicated machine user → store in K8s
secret → agent sends `GROCY-API-KEY: <key>` header directly.

Works even with `ReverseProxyAuthMiddleware` active (API key check is a base-layer
bypass that runs before the proxy header check).

**Pros**: simpler — no token exchange, no expiry handling, no Authentik dependency in
the auth path; key can be rotated independently.

**Cons**: Grocy has no API to programmatically create users or API keys (must bootstrap
manually via UI); key is long-lived (until manually rotated); not reflected in Authentik
audit logs.

### Option C: Direct header injection (no Authentik)

Only viable for in-cluster clients that can route directly to the `grocy` service
bypassing the ingress. A client inside the cluster that can reach
`grocy.grocy.svc.cluster.local:80` could set `X-authentik-username: <user>` itself.

**Rejected**: CiliumNetworkPolicy (`cluster/k8s/grocy/networkpolicy.yaml`) restricts
which pods can reach the grocy service, and `ReverseProxyAuthMiddleware` assumes a
trusted proxy strips the header before it arrives — direct injection is only safe if
external access is blocked at the network level for all clients.

### Option D: Disable auth + network policy

Set `DISABLE_AUTH=true`, rely on network-level access control.

**Rejected**: too broad — any in-cluster pod with network access becomes admin.

## Active Implementation

Option A (client_credentials). The machine SA username inside Grocy is
`ak-grocy-client_credentials` (auto-created on first authenticated request).

Token endpoint: `https://auth.allegedly.works/application/o/grocy/token/`

```
POST /application/o/grocy/token/
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=<from grocy-machine-credentials secret>
&client_secret=<from grocy-machine-credentials secret>
```

Response: `{"access_token": "...", "token_type": "Bearer", "expires_in": 86400}`

Then: `GET https://grocy.allegedly.works/api/... -H "Authorization: Bearer <token>"`

Tokens are valid 24h. Refresh by repeating the POST.
