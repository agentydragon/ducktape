# Secrets & SSO Architecture

Part of <plan.md>.

## Current Setup (Vault + tofu-controller + ESO)

TF generates random secrets -> Vault KV -> ESO creates K8s Secrets ->
Authentik reads via `!Env` blueprint tags. Apps read from K8s Secrets.

3 operators (Vault, ESO, tofu-controller). Good rotation story, but
complex for ~10 static OAuth2 secrets.

## Option A: SOPS + age (recommended simplest)

Encrypt secrets in git with age/SOPS. Flux decrypts natively — zero
additional operators.

- Recovery: age private key (one string, fits in password manager) + git
- No cluster-bound state (unlike SealedSecrets whose key is cluster-specific)
- Same Authentik `!Env` integration, no changes needed
- Rotation: manual re-encrypt and commit
- Already on the cluster TODO list (see `plan.md`)

## Option B: SealedSecrets only (current bootstrap path)

Already have SealedSecrets for bootstrap. Could use it for everything.

- Need Reflector to mirror secrets across namespaces
- 2 operators (SealedSecrets, Reflector)
- Signing key is cluster-bound — must back it up (currently in TF state)
- Recovery: need signing key + git

## Option C: Authentik as SSOT

Let Authentik generate client secrets natively (omit `client_secret` in
blueprints). Extract via API into K8s Secrets with a Job.

- 0 extra operators, but needs custom extraction Job (~30 lines)
- Bootstrap ordering problem: apps need secrets before Authentik is ready
- Secrets tied to Authentik DB — DB loss = full SSO rebuild anyway
- Not portable to a different SSO provider

## Option D: Keep Vault, drop tofu-controller

Vault as pure KV store. Populate with a simple script/Job instead of TF.
ESO reads from Vault unchanged.

- 2 operators (Vault, ESO) — still running Vault for ~10 static strings
- Best automatic rotation story (write to Vault, ESO picks up)
- Vault needs unsealing, storage, HA — heavyweight for the use case

## Option E: K8s Secrets as SSOT (generate in cluster)

Job generates secrets, creates K8s Secrets. Reflector mirrors.

- 1 operator (Reflector)
- Secrets only in etcd — cluster wipe = new secrets, full SSO rebuild
- Worst durability

## Comparison

| Option                 | Operators         | Wipe Recovery           | Rotation     | Complexity  |
| ---------------------- | ----------------- | ----------------------- | ------------ | ----------- |
| Current (Vault+TF+ESO) | 3                 | Vault backup + TF state | Auto (TF)    | High        |
| A. SOPS+age            | 0 (Flux built-in) | age key + git           | Manual       | Lowest      |
| B. SealedSecrets       | 2                 | Signing key + git       | Manual       | Low         |
| C. Authentik SSOT      | 0 + Job           | Authentik DB            | Manual (API) | Medium      |
| D. Vault sans TF       | 2                 | Vault backup            | Auto (ESO)   | Medium-high |
| E. K8s Secrets         | 1                 | New secrets             | Manual       | Low         |

Decision: **SOPS + age** for secrets. See tofu-controller audit below.

## Tofu-Controller Audit

47 Terraform resources managed by tofu-controller. Breakdown:

**Replaceable by SOPS (29 resources)** — pure secret generation
(`random_password` → Vault KV). No external API calls:

- SSO client secrets: Gitea, Grafana, Harbor, Matrix, Vault, Inventree,
  Headlamp, Gatus, OpenClaw (9 resources in `sso-secrets` module)
- App admin passwords: Harbor, Gitea, Grafana, Inventree, Authentik,
  Props, Atuin, Matrix, Langfuse (9 resources)
- API keys/tokens: PowerDNS API key, Ollama API key, Ollama direct
  token, Alloy OTLP bearer token, Authentik API token, Authentik
  secret_key, Grafana Flux token, user passwords (11 resources)

**Must remain Terraform (7 resources)** — call external APIs:

| Resource             | API Called              | Purpose                                      |
| -------------------- | ----------------------- | -------------------------------------------- |
| `dns-records`        | AWS Route 53 + PowerDNS | NS delegation + lighthouse records           |
| `harbor-proxy-cache` | Harbor API              | Proxy cache projects (Docker Hub, GHCR, etc) |
| `harbor-props`       | Harbor API              | Props project + robot account                |
| `harbor-ci`          | Harbor API              | CI project + robots + webhook tokens         |
| `harbor-webhook`     | Harbor API              | Flux receiver webhook                        |
| `harbor-oidc-config` | Harbor API              | OIDC auth configuration                      |
| `vault-oidc-auth`    | Vault API               | OIDC auth backend (goes away if Vault drops) |

**Implication**: Can't fully drop tofu-controller — still need it for
DNS and Harbor API management. But scope drops from 47 → 7 resources
(85% reduction). If we also drop Vault (Authelia path), `vault-oidc-auth`
goes away → 6 resources. The 5 Harbor resources go away only if Harbor
is replaced.

`dns-records` is the hardest to replace — it manages AWS Route 53
nameserver delegation. Could theoretically use ExternalDNS but that
doesn't handle registrar-level NS records.

## SSO Provider Options

### Current Authentik Usage (audit)

- 12 native OIDC apps (Grafana, Gitea, Harbor, Matrix, Vault, Headlamp,
  Gatus, Inventree, Airlock x2, OpenClaw Agent, Google Workspace MCP)
- 11 proxy-mode forward-auth apps (OpenClaw, Proxmox, Longhorn, Hubble,
  Loki, Grocy, Scanner, Goldilocks, Alloy OTLP, Google Workspace MCP,
  OpenClaw Mitmproxy)
- 2 public PKCE clients (Airlock SPA, Claude Code Airlock)
- 2 service accounts with bearer tokens / client_credentials
- 3 custom OAuth scopes (`airlock:propose`, `airlock:decide`,
  `airlock:read`)
- No LDAP/SCIM/SAML in use. MFA disabled (planned).
- 1 user (`agentydragon`), 2 groups

### Project Health

| Project   | Stars  | Latest Release | Date       | Active | Lang   |
| --------- | ------ | -------------- | ---------- | ------ | ------ |
| Keycloak  | 33,658 | 26.5.6         | 2026-03-19 | Yes    | Java   |
| Authelia  | 27,360 | v4.39.16       | 2026-03-14 | Yes    | Go     |
| Authentik | 20,749 | 2026.2.1       | 2026-03-03 | Yes    | Python |
| Zitadel   | 13,388 | v4.13.0        | 2026-03-23 | Yes    | Go     |
| Dex       | 10,690 | v2.45.1        | 2026-03-03 | Yes    | Go     |
| Kanidm    | 4,741  | v1.9.2         | 2026-03-13 | Yes    | Rust   |

All actively maintained with recent releases.

### Authelia Deep Dive (strongest lightweight candidate)

| Capability                    | Authelia           | Authentik (current)       |
| ----------------------------- | ------------------ | ------------------------- |
| OIDC Provider                 | Yes (certified)    | Yes                       |
| OIDC Consumer (upstream IdP)  | **No**             | Yes (Google, GitHub, etc) |
| Forward-auth / proxy SSO      | Yes (native)       | Yes (proxy outpost)       |
| Envoy ExtAuthz                | Yes (documented)   | Via outpost               |
| Cilium Gateway API            | Likely (via Envoy) | Not documented            |
| Client secrets in YAML        | Yes (hashed)       | Via `!Env` blueprints     |
| Custom OAuth scopes           | Yes                | Yes                       |
| Client credentials grant      | Yes                | Yes                       |
| PKCE public clients           | Yes                | Yes                       |
| TOTP / WebAuthn               | Yes                | Yes                       |
| Local user file (no LDAP)     | Yes (YAML file)    | Yes (DB)                  |
| Web UI for user mgmt          | **No**             | Yes                       |
| Single binary, no DB required | Yes (Go + SQLite)  | No (Django+PG+Redis)      |
| RAM footprint                 | ~20-50 MB          | ~500 MB - 1 GB+           |
| RAC (remote desktop/SSH/VNC)  | **No**             | Yes (Guacamole-based)     |
| SAML IdP                      | **No**             | Yes                       |
| SCIM provisioning             | **No**             | Yes                       |
| Application dashboard / UI    | **No**             | Yes                       |

**Key blocker**: Authelia **cannot federate upstream to Google/GitHub**.
It is provider-only — users must authenticate against Authelia's local
user file or LDAP. No "log in with Google". For a single-user personal
cluster this may be fine (just set a password in the YAML user file),
but it means no Google account integration.

**RAC**: Authentik feature for browser-based RDP/SSH/VNC to remote
machines (built on Guacamole). Not available in Authelia or any other
candidate. Would need Apache Guacamole separately if wanted.

### How Each Provider Handles Shared Secrets

The core problem: SSO provider and app must share a client secret. Who
generates it, and how does the other side get it?

**Config-file providers (Authelia, Dex)** largely eliminate the problem:

- Client secrets live in the provider's YAML config file
- Generate secret once, put it in SSO config + app secret YAML
- SOPS-encrypt both, commit to git, Flux decrypts — done
- No API extraction, no ESO, no Vault, no operators
- Secret rotation = edit two SOPS files, commit, push

**DB-backed providers (Authentik, Keycloak, Zitadel, Kanidm)**:

- Config lives in a database, not files
- Either (a) inject pre-set secrets into the DB via env/API/import, or
  (b) let the provider generate secrets and extract via API
- Both paths need a side-channel (env vars, API Jobs, Terraform)
- This is what makes the current Vault+TF+ESO chain necessary

**Implication**: If we switch to Authelia or Dex + SOPS, we can drop
Vault, ESO, tofu-controller, and Reflector entirely. The secret
management story becomes: SOPS-encrypted YAML in git, Flux decrypts.
No moving parts beyond Flux itself.

**Trade-off**: Config-file providers have no UI for managing clients.
Adding a new SSO app = edit YAML, commit. For a personal cluster with
~10 apps that rarely change, this is fine.

### What We'd Lose Switching to Authelia

1. **Google/GitHub upstream federation** — must use local password
2. **RAC** (remote desktop via browser) — need Guacamole separately.
   Guacamole works independently of Authentik: has native OIDC
   extension (works with Authelia), header-based auth (works behind
   forward-auth proxy), and its own local user DB. Only loss vs
   Authentik RAC is a separate web UI instead of embedded in the SSO
   dashboard.
3. **Web admin UI** — all config via YAML files
4. **SAML, SCIM** — not in use currently, but unavailable if needed
5. **Application dashboard** — no user-facing app launcher page

### What We'd Gain

1. **~10-20x less RAM** (~50 MB vs ~1 GB for Authentik stack)
2. **Drop PostgreSQL** for SSO (Authelia uses SQLite or no DB)
3. **Drop Vault, ESO, tofu-controller** — SOPS replaces all of them
4. **Config-as-code native** — no blueprints, no `!Env` hacks
5. **Simpler bootstrap** — no DB migrations, no worker pods
6. **Fewer failure modes** — no blueprint hash desync, no ESO
   password generator issues, no Vault token rotation bugs

### Dex as Alternative

Dex **can** federate to Google/GitHub (it's a connector/federator),
but it **cannot** do forward-auth/proxy-mode. So Dex alone won't
replace Authentik's proxy outpost for the 11 proxy-mode apps.

**Dex + Authelia combo**: Dex for upstream Google federation, Authelia
for forward-auth. But this adds complexity — two SSO components instead
of one.

### Decision Framework

Key question: **Is "log in with Google" a hard requirement?**

- If no: **Authelia + SOPS** is the clear winner. Drops 4 operators,
  saves ~1 GB RAM, config-as-code, simplest bootstrap.
- If yes: Stay with **Authentik** (simplest path) or evaluate
  **Zitadel** (Go, lighter than Authentik, has upstream federation
  \- Terraform provider). Dex+Authelia combo is possible but complex.

Decision: **Try Authelia.** Run in parallel with Authentik, migrate
app-by-app, turn off Authentik + Vault once fully migrated.

### Authelia Service Account / M2M Capabilities

| Capability                    | Authentik                           | Authelia                                    |
| ----------------------------- | ----------------------------------- | ------------------------------------------- |
| Client credentials grant      | Full, UI-managed                    | Supported in YAML config                    |
| JWT access tokens             | Default                             | Opaque by default; opt-in per client        |
| Service account tokens        | Native SA objects, API-managed      | Not supported; use client_credentials       |
| Token rotation                | Blueprints (delete+recreate)        | Standard OAuth expiry (cleaner)             |
| Per-client group restrictions | Policy bindings (expression engine) | `authorization_policies` (group/user match) |
| Custom token claims           | Property mappings (flexible)        | Limited `claims_policies`                   |

**Alloy OTLP** (currently: long-lived bearer token, rotated every 60 min
via Authentik blueprint): Switch to `client_credentials` grant. Alloy
calls Authelia's token endpoint, gets a JWT, uses it as Bearer. Standard
OAuth token expiry replaces the manual delete+recreate pattern. Cleaner.

**OpenClaw Agent** (currently: `client_credentials` grant): Works
directly — same grant type, same flow, just different issuer URL.

**Per-client access control**: Authelia `authorization_policies` support
`group:admins` / `user:agentydragon` matching. Less expressive than
Authentik's policy engine but covers our use case (admin-only access).

### Migration Strategy: Authelia + Authentik in Parallel

Deploy Authelia alongside Authentik. Migrate apps one at a time. Each
app gets a new client secret in Authelia's YAML config (SOPS-encrypted)
and a corresponding SOPS-encrypted K8s Secret in its namespace. The old
Authentik secret stays in Vault/ESO until Authentik is fully turned off.

**Dual-provider support per app:**

- Grafana, Matrix, Vault: support multiple OIDC providers simultaneously
  (safe rollback during migration)
- Harbor, Headlamp, Gatus, Inventree: single OIDC config (one provider
  at a time, must flip)
- Proxy-mode apps (11): the HTTPRoute decides which provider does
  forward-auth — flip the route, not the app

**Migration order:**

1. Deploy Authelia (tiny: single pod ~50 MB, optional Redis)
2. Native OIDC apps first (change issuer URL + client config):
   start with low-risk (Headlamp, Goldilocks, Gatus)
3. Proxy-mode apps second (rewire HTTPRoutes to Authelia ExtAuthz):
   Longhorn, Hubble, Scanner, etc.
4. Service accounts last (OpenClaw Agent, Alloy OTLP): verify
   client_credentials grant + JWT validation
5. Once all apps migrated: suspend Authentik, then Vault + ESO
6. After validation period: delete Authentik, Vault, ESO, and
   tofu-controller secret resources. Keep TF resources that call
   external APIs (Harbor, DNS).
