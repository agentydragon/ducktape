# Gatus Monitoring Plan

## Check levels

- **L1 — Availability**: Service responds at all (public or internal HTTP, no auth)
- **L2 — Functional**: Service returns expected typed data from an unauthenticated API
- **L3 — Authenticated**: API call with a dedicated monitoring credential (proves auth stack + service internals)

**Credential policy**: L3 checks use only credentials created specifically for monitoring,
stored at `kv/monitoring/<service>` in Vault. Existing infra-critical credentials (Authentik
Terraform admin token, PowerDNS API key used by cert-manager/external-dns, devbot agent
tokens, Langfuse project keys used by LiteLLM) are not reused for monitoring purposes.

**PowerDNS exception**: No check planned. The single API key is infra-critical (shared by
cert-manager, external-dns, powerdns-operator). Health is indirectly covered: DNS must work
for all other external service checks to pass.

---

## Phase 1 (done) — L1/L2 checks, no new credentials

Only `helmrelease.yaml` changed. All checks are unauthenticated.

Group structure:

```text
core:     Website, Vault, Gitea, Harbor (registry), Grafana
sso:      Authentik (login page + liveness), Matrix SSO, Harbor OIDC redirect
ai:       Ollama, LiteLLM (health + inference), Langfuse
comms:    Matrix/Synapse (versions), Element
cluster:  Loki (ready + labels), Prometheus, Hubble UI, Headlamp
services: InventTree, Headscale, Nix Cache, Atuin, Grocy, FileBrowser, OpenClaw
```

Changes from previous config:

| Service        | Before                       | After                                                  |
| -------------- | ---------------------------- | ------------------------------------------------------ |
| Langfuse       | `any(200,302)` on public URL | `langfuse-web.langfuse:3000/api/public/health` → 200   |
| Atuin          | `any(200,302)` on public URL | `atuin-server.atuin:8888` → 200                        |
| Hubble UI      | `any(200,302)` on public URL | `hubble-ui.kube-system:80` → 200                       |
| OpenClaw       | `any(200,302)` on public URL | `openclaw.openclaw:18789` → any(200,302)               |
| Authentik      | Login page only              | + internal liveness probe (no auth)                    |
| Matrix/Synapse | SSO login check only         | + `matrix-synapse.matrix:8008/_matrix/client/versions` |
| LiteLLM        | Inference only               | + `/health` L1 check (60s) alongside 30m inference     |
| Grocy          | not monitored                | Added L1 `/login` check                                |
| Headlamp       | not monitored                | Added L1 check                                         |
| FileBrowser    | not monitored                | Added L1 `/health` check                               |
| Loki           | not monitored                | Added L1 `/ready` + L2 `/loki/api/v1/labels`           |
| Prometheus     | not monitored                | Added L1 `/-/ready`                                    |

---

## Phase 2 (deferred) — L3 authenticated checks

Each requires creating a dedicated minimal-scope monitoring credential first.

### Approach: per-service native credentials, not Authentik proxy auth

Many services sit behind Authentik forward-auth (Traefik `forwardAuth` middleware). Using a
single Authentik service account token to authenticate through the proxy was considered and
rejected:

- **Forward-auth is session-cookie based.** The Traefik ↔ Authentik flow passes session
  cookies, not Bearer tokens. Making it accept tokens requires per-provider configuration and
  forces checks through external URLs (ingress → TLS → Authentik proxy → backend) rather than
  direct internal cluster endpoints.
- **Wrong failure mode.** An Authentik-proxied check proves the SSO proxy is routing correctly,
  not that the service's own auth stack (DB, token validation, secret rotation) is healthy.
  Service-internal auth can break while Authentik is perfectly fine.
- **Blast-radius dependency.** One Authentik outage would simultaneously fail all L3 checks,
  producing noise instead of signal.

All services in the table below have native API token/key support. Checks hit internal cluster
endpoints directly.

**FileBrowser exception**: FileBrowser has no native API auth mechanism. L3 is deferred until
either a native auth endpoint is identified or an Authentik token-passthrough check (hitting the
external URL) is set up as a best-effort alternative in the `sso` group.

### Workflow per service

1. Create credential (Terraform module or manual)
2. Store at `kv/monitoring/<service>` in Vault
3. Add entry to `gatus-secrets` ExternalSecret
4. Add endpoint to `config.yaml`

### Credentials and endpoints

| Service    | Credential                              | Scope             | Endpoint                                    | How to create        |
| ---------- | --------------------------------------- | ----------------- | ------------------------------------------- | -------------------- |
| Authentik  | Read-only service account token         | list users only   | `GET /api/v3/core/users/?page_size=1`       | Terraform            |
| Gitea      | Monitoring user + API token             | read repos only   | `GET /api/v1/repos/search?limit=1`          | Terraform            |
| Harbor     | Robot account                           | read-only         | `GET /api/v2/systeminfo`                    | Terraform            |
| Grafana    | Service account API key, Viewer role    | read-only         | `GET /api/datasources`                      | Terraform            |
| Langfuse   | Dedicated project API key pair          | read-only project | `GET /api/public/projects`                  | Manual in UI → Vault |
| Grocy      | API key (manual in UI → store in Vault) | read-only         | `GET /api/system/info` with `GROCY-API-KEY` | Manual in UI → Vault |
| InventTree | API token                               | read-only         | `GET /api/user/` with Bearer                | Manual in UI → Vault |

All four Terraform-managed credentials belong in a single new module
`cluster/terraform/gitops/monitoring-creds`, keeping monitoring credential creation as a coherent
unit rather than scattered across per-service modules.
