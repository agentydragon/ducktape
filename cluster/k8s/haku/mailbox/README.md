# haku mailbox (Stalwart)

Self-hosted mail for `allegedly.works`, existing so the operator can email
Haku (`haku@allegedly.works`) over an authenticated channel — contract in
<SPEC.md>, Haku-side usage in `haku/base/sources/mailbox.md`.

## Layout

| Path     | Role                                                                                               |
| -------- | -------------------------------------------------------------------------------------------------- |
| `db/`    | CNPG Postgres (OVH-HA profile) — Stalwart's data/blob/search/settings store; no PVC on the app     |
| `app/`   | Stalwart Deployment + provisioning plan + Service/HTTPRoute/Certificate/secrets                    |
| `image/` | Bazel repack of upstream Stalwart with `stalwart-cli` layered in (`ghcr.io/agentydragon/stalwart`) |

## Configuration model

Stalwart keeps almost all settings in its database; the file surface is
deliberately tiny (its documented declarative-deployments workflow):

- `app/config.json` — the DataStore object only (Postgres via the
  CNPG-generated `haku-mailbox-db-app` credentials; password injected as an
  env var).
- `app/mailbox-plan.ndjson` — everything else, as an idempotent
  `stalwart-cli apply` plan of `upsert`s: domain, the `haku` account
  (pre-created so inbound RCPT resolves before first login — required for
  OIDC directories), the Authentik OIDC directory, the DMARC-gated
  whitelist Sieve script wired to the SMTP DATA stage, the two listeners
  (SMTP :2525 STARTTLS, HTTP :8080), an `MtaStageAuth` override so the
  DNAT'ed :2525 listener accepts unauthenticated MX traffic (the upstream
  default requires SMTP AUTH off port 25), and the TLS certificate. The
  certificate is a `File` reference to the mounted cert-manager secret
  (`/tls/tls.{crt,key}`), so the plan is fully static and the upsert is
  idempotent across renewals — a renewal just restarts the pod (reloader)
  and the server re-reads the PEMs.
- `app/bootstrap-and-run.sh` — pod entrypoint: applies the plan against a
  temporary recovery-mode instance, then execs the normal server. Every pod
  start reconciles config; the reloader annotation makes cert-manager
  renewals trigger exactly such a restart.
- The pod image is the in-repo repack from `image/BUILD.bazel` — upstream
  server + the pinned static `stalwart-cli` (upstream ships the CLI only as
  a distroless image, unusable from the pod). Published as
  `ghcr.io/agentydragon/stalwart` by the push-images workflow, tag tracked
  by Flux image automation. Upgrading Stalwart = bumping the `stalwart`
  `oci.pull` (tag + digest) and, on CLI releases, the `stalwart_cli`
  `http_archive` sha in `MODULE.bazel`.

**Deviation** from stock Stalwart: no setup wizard, no WebUI-managed state —
the plan is the single source of truth. Interactive admin (rarely needed)
goes through Stalwart recovery mode: scale the deployment to keep a pod,
`kubectl -n haku-mailbox exec` is blocked for Haku but available to the
operator, or temporarily set `STALWART_RECOVERY_MODE=1` and port-forward
:8080 with the `haku-mailbox-admin` password.

## Authentication

Haku's account authenticates exclusively via OIDC bearer tokens: the
directory points at the `stalwart-haku` Authentik provider
(`tf/gitops/agent-machine-access`) with `requireAudience` pinned to its
client id, so tokens minted for any other Authentik app (e.g. the
kubectl-sandbox JWT) are rejected. The `authentik-jwt-rotation` CronJob
(`haku-mail` entry) mints the JWT for the `haku` service account
biweekly-ish, publishes it as the `haku-mail-token` Secret (flux-system),
and the `ClusterExternalSecret` in `app/` mirrors it into `haku-sandbox`.

Bootstrap note: `app/haku-mail-token.sops.yaml` is a placeholder seed until
the tofu provider lands and the rotation CronJob first runs.

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
- **TODO: replace the recovery-mode bootstrap dance** (`bootstrap-and-run.sh`:
  background recovery server → retried `stalwart-cli apply` → kill → exec).
  It is upstream's documented headless path and the ugliness is irreducible
  today — config lives in the DB, the management API is the only config
  surface, and an empty DB serves no API — but revisit when any of these
  gates opens: (a) a Stalwart tofu provider covers `SieveSystemScript`,
  `MtaStageData`, the `Authentication`/`SystemSettings` singletons, and
  `Certificate` (as of 2026-07, `flungo/stalwart` v0.1.0 covers
  accounts/domains/directories/listeners only); (b) upstream grows a
  file-based/declarative bootstrap.
