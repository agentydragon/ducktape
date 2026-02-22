# Programmatic Service Authentication via Authentik

How to let code authenticate once against Authentik and access multiple
services — the way a human uses SSO with one password.

## Mechanism: OAuth2 Client Credentials with a Service Account

Authentik's **client credentials grant** is the standard OAuth2 flow for this.

**Setup (one-time):**

1. Create a service account in Authentik (Directory > Users > Create Service
   Account). This produces a username + **app password** token.
2. Add the service account to the same groups as the human user (e.g.,
   `authentik Admins`) so it gets the same access policies.

**Usage (from code):**

```http
POST https://auth.allegedly.works/application/o/token/
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=<app_client_id>
&username=<service_account_username>
&password=<app_password_token>
&scope=openid profile email
```

Returns a signed JWT `access_token` carrying the service account's identity,
groups, and entitlements.

## Current Service Integration Patterns

### Already proxy-based (bearer token works today)

These services sit behind an Authentik proxy outpost. Send
`Authorization: Bearer <jwt>` using the proxy provider's `client_id`:

| Service     | `client_id`   | URL                           |
| ----------- | ------------- | ----------------------------- |
| Grocy       | `grocy`       | `grocy.allegedly.works`       |
| Loki        | `loki`        | `loki.allegedly.works`        |
| Gatus       | `gatus`       | `status.allegedly.works`      |
| Filebrowser | `filebrowser` | `filebrowser.allegedly.works` |
| Headlamp    | `headlamp`    | `headlamp.allegedly.works`    |
| Hubble      | `hubble`      | `hubble.allegedly.works`      |
| OpenClaw    | `openclaw`    | `openclaw.allegedly.works`    |
| Alloy OTLP  | `alloy-otlp`  | `alloy-otlp.allegedly.works`  |
| Kagent      | `kagent`      | `kagent.allegedly.works`      |

### Native OAuth2/OIDC (cannot use bearer token directly)

These services use OIDC for the browser login flow, then issue their own
sessions/tokens. A raw Authentik JWT is not accepted by their APIs.

| Service        | `client_id` | URL                         |
| -------------- | ----------- | --------------------------- |
| Gitea          | `gitea`     | `git.allegedly.works`       |
| Grafana        | `grafana`   | `grafana.allegedly.works`   |
| Harbor         | `harbor`    | `registry.allegedly.works`  |
| Matrix/Synapse | `matrix`    | `matrix.allegedly.works`    |
| Vault          | `vault`     | `vault.allegedly.works`     |
| InvenTree      | `inventree` | `inventree.allegedly.works` |
| Headscale      | `headscale` | `headscale.allegedly.works` |

## Can These OAuth2 Services Switch to Proxy?

### Yes: Gitea, Grafana, InvenTree

All three have native reverse-proxy header auth support. They can read
identity from headers like `X-authentik-username` that the Authentik proxy
outpost sets.

- **Gitea**: `ENABLE_REVERSE_PROXY_AUTHENTICATION = true` in `app.ini`.
  Reads `X-WEBAUTH-USER` (configurable). Auto-creates users.
- **Grafana**: `[auth.proxy]` section in `grafana.ini`. Reads
  `X-WEBAUTH-USER` (configurable). Supports group/role mapping from headers.
  Auto-creates users.
- **InvenTree**: `INVENTREE_REMOTE_LOGIN=true`,
  `INVENTREE_REMOTE_LOGIN_HEADER=X-Forwarded-User`. Django
  `RemoteUserMiddleware`.

### No: Matrix, Vault, Headscale, Harbor

| Service            | Why proxy auth won't work                                                                                                                                                                                                                        |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Matrix/Synapse** | Matrix clients (Element, etc.) must obtain Matrix access tokens via the Synapse login flow. The entire client-server API is bearer-token-based. A proxy can't give Element the tokens it needs. OIDC is the only SSO path.                       |
| **Vault**          | Vault's security model is built on its own token system. Every API call requires a Vault token with policies, TTLs, and audit trails. **However**, Vault's OIDC/JWT auth method already accepts Authentik JWTs natively — no switch needed.      |
| **Harbor**         | Has `http_auth` mode, but `docker push`/`docker pull` requires Harbor to issue its own registry v2 JWT tokens via its token service. The web UI would work behind a proxy, but the Docker CLI flow would break. Poorly documented, known issues. |
| **Headscale**      | OIDC is used for the device registration flow (`tailscale up`). This is a machine-level protocol where the Tailscale client itself completes the OIDC dance. The admin API uses API keys. Proxy auth maps to neither use case.                   |

## Reasons NOT to Switch (Gitea, Grafana, InvenTree)

### Security: weaker trust model

Native OIDC uses **cryptographic verification** — the service validates a
signed JWT against Authentik's JWKS endpoint. An attacker cannot forge a
valid token without Authentik's signing key.

Proxy header auth uses **network topology trust** — the service trusts
whatever value is in `X-authentik-username`. If anything can reach the
backend service bypassing the proxy, it can impersonate any user by setting
that header.

Authentik itself has had CVEs in this area:

- **CVE-2023-36456**: `X-Forwarded-For`/`X-Real-IP` spoofing — anyone could
  bypass IP-based policies.
- **CVE-2024-47070**: Malformed `X-Forwarded-For` value caused Authentik to
  skip password verification entirely.
- **CVE-2026-25748**: Malformed cookie in forward auth mode caused validation
  to **fail open** — request reached the backend without `X-Authentik-*`
  headers being set.

### Security: requires network isolation that we don't have yet

Proxy header auth is only safe if the backend service is **unreachable**
except through the proxy. Currently:

- **No default-deny NetworkPolicy exists.** All pods communicate freely.
  (Documented TODO in `plan.md` and `sre-best-practices-review.md`.)
- **Gitea, Grafana, InvenTree have direct HTTPRoutes** through the Cilium
  Gateway. They are directly reachable from the internet without any proxy
  layer.

Switching to proxy auth without first deploying NetworkPolicies that restrict
backend access to only the Authentik outpost pods would leave the services
**less secure than they are today** — anyone who can reach the service
directly could set `X-WEBAUTH-USER: admin` and get full access.

**Required before switching:**

1. Remove the direct HTTPRoutes (so external traffic only reaches the
   Authentik outpost).
2. Deploy `CiliumNetworkPolicy` per namespace with default-deny ingress,
   allowing only the Authentik outpost pods to reach the backend services.
3. Configure the services' built-in IP whitelists as defense-in-depth:
   - Gitea: `REVERSE_PROXY_TRUSTED_PROXIES`
   - Grafana: `[auth.proxy] whitelist`

### Gitea-specific: git-over-HTTPS breaks

Git CLI sends HTTP Basic Auth credentials, not custom `X-WEBAUTH-USER`
headers. With proxy auth enabled:

- `git clone https://git.allegedly.works/...` fails because the git client
  does not send the proxy header.
- Users must create personal access tokens via the web UI and use them for
  HTTPS git operations.
- There are reports of reverse proxy auth causing **unauthenticated
  clone/push** when misconfigured (Gitea issue #2427).

**Mitigation**: Use SSH for all git operations (`git clone
git@git.allegedly.works:...`). SSH bypasses the HTTP proxy entirely and
continues to work. Consider `DISABLE_HTTP_GIT = true` if SSH is viable for
all users.

### Session revocation gap

With OIDC, revoking a user's refresh token at Authentik ends their session
across all services.

With proxy auth, Grafana (and potentially others) issue their own session
cookies after the first proxy-authenticated request (`enable_login_token`
defaults to `true` in Grafana). Even if you revoke the Authentik session, the
Grafana session persists until it expires. You must revoke sessions in both
places.

### Existing user migration

Switching auth modes may orphan existing user accounts:

- **Gitea** ties users to their authentication source. If the username from
  the proxy header doesn't match exactly, duplicate accounts are created.
- **Grafana** is forgiving — it adopts existing users when email/login matches.
- **InvenTree** requires exact username match in the Django User table.

## Recommended Architecture

Given the tradeoffs, the pragmatic approach is to **keep native OIDC** for
the services that have it and use per-service adapters for the few that need
them:

```text
              One app password
                    |
             Client Credentials
                    |
              Authentik JWT
                    |
    +---------------+-------------------+
    |               |                   |
Proxy services   Vault              Gitea, Grafana,
(9 services)     (native JWT        Harbor, Matrix,
Bearer token     auth method)       InvenTree, Headscale
works directly   Works directly     Service-specific tokens
```

### Per-service access for the OAuth2 services

| Service       | Programmatic access path                                                                                                           |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| **Vault**     | `vault write auth/oidc/login jwt=<authentik_jwt>` — already works, no changes needed.                                              |
| **Gitea**     | Create a Gitea API token (personal access token) for the service account. The service account is auto-created on first OIDC login. |
| **Grafana**   | Create a Grafana service account + token. Or configure `auth.jwt` to validate Authentik JWTs directly against the JWKS endpoint.   |
| **Harbor**    | Create a Harbor robot account for programmatic access.                                                                             |
| **InvenTree** | Create a DRF API token for the service account after first OIDC login.                                                             |
| **Matrix**    | Use the Matrix client-server API login flow with the OIDC token.                                                                   |
| **Headscale** | Use `headscale apikeys create` for the admin API. Device registration is interactive by design.                                    |

### Score

- **12 of 16 services** accessible with a single bearer token (9 proxy + Vault + 2 that could be switched but shouldn't be).
- **4 services** need service-specific tokens (Gitea, Harbor, Matrix, Headscale) — but these are architecturally constrained.
- Grafana and InvenTree _could_ be switched to proxy to make it 14/16, but the security cost outweighs the convenience unless NetworkPolicies and the other prerequisites are deployed first.

### If you still want to switch later

The prerequisites, in order:

1. Deploy default-deny `CiliumNetworkPolicy` per namespace.
2. Allowlist only Authentik outpost pods as ingress to backend services.
3. Remove direct HTTPRoutes for the switched services.
4. Create proxy provider + outpost + HTTPRoute in Authentik blueprints (same
   pattern as existing Grocy/Loki blueprints).
5. Configure the service's header auth settings.
6. Configure the service's trusted proxy IP whitelist.
7. Test user migration (username matching between OIDC-created accounts and
   proxy header values).
8. For Gitea: decide on git-over-HTTPS policy (disable or require tokens).
