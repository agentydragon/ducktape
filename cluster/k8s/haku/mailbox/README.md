# haku mailbox (Stalwart)

Self-hosted mail for `allegedly.works`, existing so the operator can email
Haku (`haku@allegedly.works`) over an authenticated channel — contract in
<SPEC.md>, Haku-side usage in `haku/base/sources/mailbox.md`.

## Layout

| Path         | Role                                                                                               |
| ------------ | -------------------------------------------------------------------------------------------------- |
| `db/`        | CNPG Postgres (OVH-HA profile) — Stalwart's data/blob/search/settings store                        |
| `bootstrap/` | Certificate, recovery credential, canonical plan, and one-shot empty-database bootstrap Job        |
| `app/`       | Normal Stalwart Deployment + Service/HTTPRoute and Haku mailbox-token publication                  |
| `reconcile/` | Authentik machine-token mirror and steady-state plan reconciler Job                                |
| `image/`     | Bazel repack of upstream Stalwart with `stalwart-cli` layered in (`ghcr.io/agentydragon/stalwart`) |

## Configuration model

Stalwart keeps almost all settings in its database; the file surface is
deliberately tiny (its documented declarative-deployments workflow):

- `bootstrap/config.json` — the DataStore object only (Postgres via the
  CNPG-generated `haku-mailbox-db-app` credentials; password injected as an
  env var).
- `bootstrap/mailbox-plan.ndjson` — everything else, as an idempotent
  `stalwart-cli apply` plan of `upsert`s: domain, the `haku` account
  (pre-created so inbound RCPT resolves before first login — required for
  OIDC directories), the Authentik OIDC directory, the SPF-gated
  whitelist Sieve script wired to the SMTP DATA stage (SPF, not DMARC:
  the DATA-stage script runs before Stalwart's DKIM/DMARC analysis),
  a `SenderAuth` override enabling SPF/DMARC verification off port 25
  (the built-in defaults only verify on `local_port == 25`; the listener
  is on :2525), the three listeners
  (SMTP :2525 STARTTLS, HTTP :8080, IMAP :1143 — the latter two plaintext,
  cluster-internal only), a built-in-User-role alias +
  `Authentication.defaultUserRoleIds` grant (externally-authenticated
  principals get ONLY these default roles — the set ships empty, which
  403s every JMAP/IMAP request even for a valid token),
  an `MtaStageAuth` override so the
  DNAT'ed :2525 listener accepts unauthenticated MX traffic (the upstream
  default demands SMTP AUTH on every port except 25), and the TLS
  certificate. The
  certificate is a `File` reference to the mounted cert-manager secret
  (`/tls/tls.{crt,key}`), so the plan is fully static and the upsert is
  idempotent across renewals — a renewal just restarts the pod (reloader)
  and the server re-reads the PEMs.
- `bootstrap/bootstrap-job.yaml` + `bootstrap/bootstrap.sh` — one-shot empty-database
  bootstrap. It first gives an existing production server a chance to become
  healthy (cluster-resource recreation over a retained database needs no
  bootstrap), then uses upstream's serialized recovery-mode + apply workflow.
  Its stable ConfigMap name deliberately prevents ordinary plan changes from
  rerunning recovery mode.
- `reconcile/reconciler-job.yaml` + `reconcile/reconcile.sh` — steady-state reconciliation
  against the running management API. Kustomize hashes the plan ConfigMap;
  plan changes make Flux replace the completed Job. It authenticates as the
  dedicated `stalwart-reconciler` Authentik machine principal, whose JWT is
  minted automatically by `authentik-jwt-rotation` and mirrored only into the
  trusted `haku-mailbox` namespace. It never starts another Stalwart server.
- `bootstrap/run.sh` — the production pod entrypoint only strips the upstream
  binary's unused file capability and starts normal Stalwart. It never applies
  configuration. Runtime and reconciler ConfigMaps are separate, so changing
  only the plan does not restart the mailserver. Certificate renewal still
  restarts the production pod so Stalwart re-reads the mounted PEM files.
- The pod image is the in-repo repack from `image/BUILD.bazel` — upstream
  server + the pinned static `stalwart-cli` used by the reconciler (upstream
  ships the CLI only as a distroless image, unusable from the Job). Published as
  `ghcr.io/agentydragon/stalwart` by the push-images workflow, tag tracked
  by Flux image automation. Upgrading Stalwart = bumping the `stalwart`
  `oci.pull` (tag + digest) and, on CLI releases, the `stalwart_cli`
  `http_archive` sha in `MODULE.bazel`.

**Deviation** from stock Stalwart: no setup wizard, no WebUI-managed state —
the plan is the single source of truth. The recovery admin credential is
mounted only into the one-shot bootstrap Job, never the production container
or steady-state reconciler.
Interactive admin (rarely needed) goes through Stalwart recovery mode: scale
the deployment to keep a pod,
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
- **Future: replace the recovery-mode bootstrap** when a fully native IaC
  surface exists. The one-shot Job follows upstream's documented headless
  path because config lives in the DB, the management API is the only config
  surface, and an empty DB serves no API. Revisit when either gate opens:
  (a) a Stalwart tofu provider covers `SieveSystemScript`,
  `MtaStageData`, the `Authentication`/`SystemSettings` singletons, and
  `Certificate` (as of 2026-07, `flungo/stalwart` v0.1.0 covers
  accounts/domains/directories/listeners only); (b) upstream grows a
  file-based/declarative bootstrap.
