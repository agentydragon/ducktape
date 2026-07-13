# haku mailbox (Stalwart)

Self-hosted mail for `allegedly.works`, existing so the operator can email
Haku (`haku@allegedly.works`) over an authenticated channel — contract in
<SPEC.md>, Haku-side usage in `haku/base/sources/mailbox.md`.

## Layout

| Path     | Role                                                                                               |
| -------- | -------------------------------------------------------------------------------------------------- |
| `db/`    | CNPG Postgres (OVH-HA profile) — Stalwart's data/blob/search/settings store                        |
| `app/`   | Declarative plan + init reconciliation, production server, certificate, Services, and HTTPRoute    |
| `image/` | Bazel repack of upstream Stalwart with `stalwart-cli` layered in (`ghcr.io/agentydragon/stalwart`) |

## Configuration model

Stalwart keeps almost all settings in its database; the file surface is
deliberately tiny (its documented declarative-deployments workflow):

- `app/config.json` — the DataStore object only (Postgres via the
  CNPG-generated `haku-mailbox-db-app` credentials; password injected as an
  env var).
- `app/mailbox-plan.ndjson` — everything else, as an idempotent
  `stalwart-cli apply` plan: domain, the `haku` account
  (pre-created so inbound RCPT resolves before first login — required for
  OIDC directories), the Authentik OIDC directory, the SPF-gated
  whitelist Sieve script wired to the SMTP DATA stage (SPF, not DMARC:
  the DATA-stage script runs before Stalwart's DKIM/DMARC analysis),
  a `SenderAuth` override enabling SPF/DMARC verification off port 25
  (the built-in defaults only verify on `local_port == 25`; the listener
  is on :2525), and the exact listener set
  (SMTP :2525 STARTTLS, HTTP :8080, IMAP :1143 — the latter two plaintext,
  cluster-internal only), and an `MtaStageAuth` override so the
  DNAT'ed :2525 listener accepts unauthenticated MX traffic (the upstream
  default demands SMTP AUTH on every port except 25), and the TLS
  certificate. Normal startup owns the version-matched built-in
  User/administrator roles and their permissions; the plan only points
  `Authentication.directoryId` at Authentik. The certificate is a `File`
  reference to the mounted cert-manager secret
  (`/tls/tls.{crt,key}`), so the plan is fully static and the upsert is
  idempotent across renewals — a renewal just restarts the pod (reloader)
  and the server re-reads the PEMs.
- `app/initialize.sh` runs in the Pod's init container on every creation. It
  starts Stalwart in normal mode with a temporary fallback admin, waits for
  the local API, applies the plan, then terminates and waits for that exact
  process. On an empty database, normal startup first creates SQL tables and
  upstream's version-matched safe defaults; on an existing database those
  count-gated inserts are no-ops. The production container starts only after
  reconciliation succeeds and never receives or mounts the fallback admin.
- Kustomize hashes the ConfigMap containing `initialize.sh`, `config.json`,
  and the plan. Any edit changes the Deployment Pod template; its `Recreate`
  strategy stops the old Pod before running the new init container. There is
  no bootstrap marker or recovery-mode process, and an interrupted partial
  apply is retried from the same idempotent plan.
- The in-repo image overlays the official release
  binary at build time with mode `0755`, removing upstream's unused
  `cap_net_bind_service` xattr so restricted pods can execute it. Certificate
  renewal still restarts production so Stalwart re-reads the mounted PEMs.
- The pod image is the in-repo repack from `image/BUILD.bazel` — upstream
  server + the pinned static `stalwart-cli` used by the init container (upstream
  ships the CLI only as a distroless image, not as an executable to layer into
  another container) plus the
  capability-free official server release binary. Published as
  `ghcr.io/agentydragon/stalwart` by the push-images workflow, tag tracked
  by Flux image automation. Upgrading Stalwart = bumping the `stalwart`
  `oci.pull` (tag + digest) and, on CLI releases, the `stalwart_cli`
  `http_archive` sha in `MODULE.bazel`.

**Deviation** from stock Stalwart: no setup wizard and no WebUI-managed state;
the plan is the source of truth. `STALWART_RECOVERY_ADMIN` is set only on the
short-lived normal-mode init process, as upstream's temporary fallback admin.
The production process has no fallback or permanent administrator credential.
Plan omission is not deletion: fields must be explicitly cleared and removed
objects must remain as filtered `destroy` operations. The listener collection
is deliberately exact-set reconciled with destroy + upsert because upstream
normal-mode defaults would otherwise remain alongside the desired listeners.

## Authentication

Haku's account authenticates exclusively via OIDC bearer tokens: the
directory points at the `stalwart-haku` Authentik provider
(`tf/gitops/agent-machine-access`) with `requireAudience` pinned to its
client id, so tokens minted for any other Authentik app (e.g. the
kubectl-sandbox JWT) are rejected. The `authentik-jwt-rotation` CronJob
(`haku-mail` entry) mints the JWT for the `haku` service account
biweekly-ish, publishes it as the `haku-mail-token` Secret (flux-system),
and the `ClusterExternalSecret` in `app/` mirrors it into `haku-sandbox`.

## Traffic

- **Port 25**: `MX allegedly.works → mx.allegedly.works` (A records on the
  public OVH gateway roster, `tf/gitops/dns-records/`) → the
  `haku-mailbox-smtp` Service's `externalIPs` DNAT (Cilium KPR) → Stalwart's
  :2525 listener (STARTTLS with the cert-manager certificate). SMTP has its
  own Service because externalIPs exposes every port of a Service; the HTTP
  listener stays on a separate ClusterIP-only Service. If externalIPs turn
  out not to be programmed by the Cilium config, fall back to `hostPort` +
  node pinning (the scanner's SMB port uses that pattern).
- **HTTPS**: `haku-mailbox.allegedly.works` HTTPRoute → Stalwart's HTTP
  listener (JMAP + management API; management requires the admin credential
  Haku can't read).
- **IMAP (cluster-internal only)**: the `haku-mailbox` ClusterIP Service's
  :1143 — no route, no externalIPs. Consumed by himalaya from `haku-sandbox`
  (whose egress CCNP already allows cluster-internal traffic) with SASL
  OAUTHBEARER; the OIDC directory structurally rejects password (PLAIN)
  auth, so bearer tokens remain the only way in. Client setup:
  `haku/base/sources/mailbox.md`.

## Post-deploy verification

```bash
# STARTTLS on the public path:
openssl s_client -starttls smtp -connect mx.allegedly.works:25 -servername mx.allegedly.works </dev/null
# Whitelist: a mail from the operator address lands, a spoofed one is 550'd.
# JMAP with the rotated token:
TOK=$(kubectl -n haku-sandbox get secret haku-mail-token -o jsonpath='{.data.jwt}' | base64 -d)
curl -s -H "Authorization: Bearer $TOK" https://haku-mailbox.allegedly.works/.well-known/jmap | head
```

First-deploy item to watch: the plan's object shapes were authored against
the v0.16 docs — `stalwart-cli apply` errors name the offending field if the
schema drifts.

## Future

- **Outbound (haku → operator only)**: enable a submission listener +
  server-side recipient allowlist, update SPF (currently `-all`) and OVH
  rDNS. Tracked in `haku/PLAN.md`.
- **Backups**: CNPG cluster; wire into the standard pg_dump/backup strategy
  if mailbox contents become worth keeping.
