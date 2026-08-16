# Cluster Backups to Google Drive (2 TB Google One)

**Date**: 2026-06-20
**Status**: design — nothing applied yet.
**Scope**: small high-value PVCs (Grocy SF/Vallejo config, Tana-MCP state, app
config DBs). Large media is explicitly out of scope for now.

## Goal

Use the existing personal Google One 2 TB plan (`agentydragon@gmail.com`) as an
**offsite** copy of cluster backups, driven from the VolSync that's already
deployed (<../../k8s/volsync/helmrelease.yaml>, chart 0.15.0).

## The constraint that shapes everything

VolSync **cannot write to Google Drive directly**. Its movers are
`rsync(TLS)`, `restic`, `rclone`, `syncthing`. Today we only use `rsyncTLS`
(PVC→PVC, e.g. <../../k8s/grocy/sf/app/volsync-backup.yaml>). Drive is reachable
only via the `restic` mover (restic repo on an `rclone:` backend) or the `rclone`
mover (dumb file mirror).

Two Drive properties dominate the design:

- **Personal @gmail My Drive can't use a service account.** Service accounts get
  their own separate 15 GB drive and don't share the Google One quota, and can't
  write to My Drive without Workspace domain-wide delegation. So the credential
  **must be a user OAuth token**. We don't hand-mint it — **airlock already brokers
  Google OAuth** (acquire + store + auto-refresh), so a new airlock provider entry
  owns the Drive credential and the backup job just consumes a short-lived access
  token (see Component 4). (Workspace would allow a service account + shared drive;
  we don't have that.)
- **Drive stores plaintext Google can read, hates many small files, and
  rate-limits** (≈ a few requests/s, 750 GB/day upload cap). So we want to send
  **few, large, already-encrypted blobs** — exactly what restic produces and
  exactly what a raw file mirror does not.

For our scope (small DBs) the rate limits are a non-issue, but the
**encryption** and **point-in-time history** arguments still make restic the
right mover, not the rclone mirror.

## Chosen architecture: two-stage (restic → SeaweedFS S3 → rclone copy → Drive)

```text
app PVC ──VolSync restic mover──▶ SeaweedFS S3 bucket ──rclone CronJob──▶ gdrive:
         (encryption, dedup,        (native S3, no rclone    (opaque encrypted
          retention, snapshots)      in the mover path)        restic packs only)
```

Why two stages instead of pointing VolSync's restic mover straight at
`rclone:gdrive:`:

- VolSync's restic mover talks **native S3** to SeaweedFS — no dependency on
  whether the mover image ships the `rclone` binary on PATH (it may not; that's
  the unverified risk in the direct approach).
- restic does the hard part (encryption, dedup, prune/retention, point-in-time
  restore). Google only ever sees **ciphertext** in large immutable pack files.
- Clean 3-2-1: hot PVC + in-cluster S3 copy (OVH) + offsite Drive copy. The
  fragile Drive/OAuth/rate-limit concerns are isolated in **one dumb CronJob**,
  decoupled from backup integrity.
- Stage A reuses the SeaweedFS Bucket CR + per-tenant identity + ESO pattern we
  already run (e.g. <../../k8s/seaweedfs/langfuse-bucket/bucket.yaml>,
  <../../k8s/seaweedfs/secrets/secretstore.yaml>).

This also **supersedes the ad-hoc rsyncTLS PVC→PVC backups** (Grocy, Tana-MCP):
restic-to-S3 gives retention + offsite in one mechanism, where rsyncTLS only
gives a single in-cluster restore point. Migrating those is optional and can be
done per-app after the pattern is proven.

## Components

VolSync `ReplicationSource` must live in the **same namespace as the source
PVC**, so the restic sources and their config Secrets are per-app. The bucket,
the identity, and the rclone copy job are shared.

### 1. Shared SeaweedFS backup bucket + identity

One bucket `volsync-backups`, one prefix per app (`/grocy-sf`, `/grocy-vallejo`,
`/tana-mcp`, …). Per the [SeaweedFS Bucket co-location convention](../../k8s/seaweedfs/),
put the Bucket CR in the backup tooling's own flux-kustomization dir, not under
`seaweedfs/`. One identity `s3-identity-volsync` with `Read,Write,List,Tagging`
on the bucket, modeled on <../../k8s/seaweedfs/secrets/identities/langfuse.sops.yaml>.

Deliver `accessKey`/`secretKey` into each consuming namespace via ESO the same
way other tenants do (per-namespace `ExternalSecret` reading the seaweedfs
identity through a `ClusterSecretStore`).

### 2. Per-app restic config Secret (in the app namespace)

VolSync's restic mover reads these env-style keys:

```yaml
stringData:
  RESTIC_REPOSITORY: s3:http://seaweedfs-s3.seaweedfs.svc:8333/volsync-backups/grocy-sf
  RESTIC_PASSWORD: <per-repo encryption passphrase — generate, store in SOPS>
  AWS_ACCESS_KEY_ID: <from s3-identity-volsync>
  AWS_SECRET_ACCESS_KEY: <from s3-identity-volsync>
  AWS_DEFAULT_REGION: us-east-1 # SeaweedFS ignores it; the S3 SDK wants it set
```

The `RESTIC_PASSWORD` is the backup encryption key — **losing it means the
backups are unrecoverable**. Store it in SOPS alongside everything else; consider
also keeping a copy in the password manager.

### 3. Per-app VolSync ReplicationSource (restic mover)

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationSource
metadata:
  name: grocy-config-ovh-restic
  namespace: grocy-sf
spec:
  sourcePVC: grocy-config-ovh
  trigger:
    schedule: "30 3 * * *" # nightly; stagger per app
  restic:
    repository: restic-config-grocy-sf # the Secret from step 2
    copyMethod: Direct # no VolumeSnapshotClass installed (same as rsyncTLS backups)
    pruneIntervalDays: 7
    retain:
      daily: 7
      weekly: 4
      monthly: 6
    cacheCapacity: 1Gi
    moverSecurityContext: # mirror the existing rsyncTLS backups
      runAsUser: 1000
      runAsGroup: 1000
      fsGroup: 1000
      seccompProfile:
        type: RuntimeDefault
    moverAffinity: # keep movers on OVH where the source PVCs live
      nodeAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          nodeSelectorTerms:
            - matchExpressions:
                - key: topology.kubernetes.io/zone
                  operator: In
                  values: ["hil-ovh"]
```

`copyMethod: Direct` carries the same caveat as the current Grocy backups: the
mover bind-mounts the live path, so a backup may occasionally catch a torn SQLite
write — acceptable for crash-consistent restore.

### 4. Google Drive token — airlock provisions it (no SOPS token, no `rclone authorize`)

**Airlock is already an OAuth token broker**, so it — not a hand-minted SOPS
secret — owns the Drive credential. Its `oauth.providers` list
(<../../k8s/agents/airlock/config.yaml>) declares providers (oura, google, bsc);
a background `token_refresh_loop` (<../../../airlock/oauth/refresh.py>) keeps
each access token fresh and writes two k8s Secrets per provider:

- `refresh_secret` — **all** fields incl. `refresh_token` (the durable credential).
- `access_secret` — only `access_token`, `token_type`, `expires_at`, `scope`
  (a short-lived ~1 h access token, kept valid by the refresh loop).

The existing `google` provider is **read-only** (`drive.readonly`). Add a **new,
separate** provider for backup writes — separate, not extended, so it gets its own
least-privilege token and doesn't broaden the agent-facing read-only Google token:

```yaml
# add to oauth.providers in cluster/k8s/agents/airlock/config.yaml
- name: google_drive_backup
  provider_type: oauth2
  display_name: Google Drive (cluster backup write)
  authorize_url: https://accounts.google.com/o/oauth2/v2/auth
  token_url: https://oauth2.googleapis.com/token
  scopes:
    - https://www.googleapis.com/auth/drive.file # write, but only files this app creates
  redirect_uri: https://airlock.allegedly.works/oauth/callback/google_drive_backup
  refresh_secret:
    name: google-drive-backup-tokens
  access_secret:
    name: google-drive-backup-access-token
  refresh_margin_seconds: 600 # keep the access token comfortably fresh at job start
  extra_auth_params:
    access_type: offline # required to get a refresh_token
    prompt: consent
```

It reuses the same `google-client-credentials` OAuth client airlock already loads
(`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`), and the consent screen is already
published (google-workspace-mcp proves it), so there is **no GCP console work and
no off-cluster `rclone authorize`** — acquisition is one browser visit to the
callback URL (see runbook).

**The backup job consumes the `access_secret`, never the refresh token.** Airlock
stays the _sole_ refresher — no two-systems-rotating-the-same-`refresh_token`
hazard — and a compromised backup job leaks at most a ~1 h access token, not the
durable credential. rclone is handed a token with only `access_token` + `expiry`;
since airlock keeps it fresh and small-PVC runs finish in seconds-to-minutes (well
under the ~1 h access-token lifetime), rclone never needs to refresh mid-run.

`scope: drive.file` is the safe choice: the credential can only see and overwrite
files **the app itself created**, never the rest of the user's Drive.

### 5. rclone copy CronJob (in the `airlock` namespace)

Runs in `airlock` so it can mount `google-drive-backup-access-token` directly
(no cross-namespace mirror). It needs the SeaweedFS S3 reader identity too
(delivered via ESO, like other tenants). A tiny shim maps airlock's `expires_at`
to rclone's `expiry` and assembles the Drive remote's token JSON:

```yaml
# runs after the nightly restic sources have finished
schedule: "30 5 * * *"
restartPolicy: Never
image: rclone/rclone:<pinned>
env:
  # SeaweedFS S3 source (from the reader identity)
  - { name: RCLONE_CONFIG_SEAWEEDFS_TYPE, value: s3 }
  - { name: RCLONE_CONFIG_SEAWEEDFS_PROVIDER, value: Other }
  - { name: RCLONE_CONFIG_SEAWEEDFS_ENDPOINT, value: "http://seaweedfs-s3.seaweedfs.svc:8333" }
  - {
      name: RCLONE_CONFIG_SEAWEEDFS_ACCESS_KEY_ID,
      valueFrom: { secretKeyRef: { name: s3-identity-volsync-reader, key: accessKey } },
    }
  - {
      name: RCLONE_CONFIG_SEAWEEDFS_SECRET_ACCESS_KEY,
      valueFrom: { secretKeyRef: { name: s3-identity-volsync-reader, key: secretKey } },
    }
  # Google Drive target — Drive type/scope are static; the access token is injected fresh
  - { name: RCLONE_CONFIG_GDRIVE_TYPE, value: drive }
  - { name: RCLONE_CONFIG_GDRIVE_SCOPE, value: drive.file }
  - { name: ACCESS_TOKEN, valueFrom: { secretKeyRef: { name: google-drive-backup-access-token, key: access_token } } }
  - { name: EXPIRES_AT, valueFrom: { secretKeyRef: { name: google-drive-backup-access-token, key: expires_at } } }
command:
  - sh
  - -c
  - |
    export RCLONE_CONFIG_GDRIVE_TOKEN="$(printf '{"access_token":"%s","token_type":"Bearer","expiry":"%s"}' "$ACCESS_TOKEN" "$EXPIRES_AT")"
    exec rclone sync seaweedfs:volsync-backups gdrive:cluster-restic-backups \
      --fast-list --transfers=4 --drive-chunk-size=64M \
      --drive-stop-on-upload-limit   # bail cleanly if the 750 GB/day cap is hit
```

`rclone sync` mirrors (including prune-driven deletions); use `rclone copy` if you
want Drive to retain blobs restic has already pruned. Restic packs are large and
immutable, so this transfers incrementally and is Drive-friendly.

## Credential setup runbook (one-time)

No GCP console work, no `rclone authorize`, no SOPS token — airlock handles
acquisition, storage, and refresh. The OAuth client and a published consent screen
already exist (google-workspace-mcp uses them).

1. **Land the airlock provider entry** (step 4) so airlock exposes
   `/oauth/callback/google_drive_backup`.
2. **Authorize once in a browser as the personal Gmail account**: visit airlock's
   authorize endpoint for the new provider (the same flow used for oura/google/bsc;
   the Airlock UI surfaces the link). Approve the
   `drive.file` scope. Airlock writes `google-drive-backup-tokens` and
   `google-drive-backup-access-token` into the `airlock` namespace and refreshes
   them thereafter.
3. **SOPS secrets that remain** are only the non-Google ones: the per-app restic
   config secrets (step 2, with a fresh `RESTIC_PASSWORD` per repo) and the
   SeaweedFS backup identity (step 1). The Drive credential never touches SOPS.

## Restore path

- **From SeaweedFS S3 (fast, normal case):** a VolSync `ReplicationDestination`
  with the restic mover against the same repo Secret, or run `restic restore`
  directly with the same env vars.
- **From Drive (S3 lost):** `rclone copy gdrive:cluster-restic-backups seaweedfs:volsync-backups`
  to rehydrate the bucket, then restore as above — or point restic straight at
  `rclone:gdrive:cluster-restic-backups` from a machine with an rclone Drive token
  (e.g. the airlock `google-drive-backup` access token, or a fresh `rclone authorize`
  for the disaster-recovery case) and the `RESTIC_PASSWORD`.

Document and **actually run a restore drill** before relying on this (same
discipline the Study Casino DB backup TODO calls for).

## Open decisions

- **One bucket + prefixes vs one bucket per app.** Prefixes are simpler and let a
  single identity + single rclone job cover everything; per-app buckets give
  tighter blast-radius isolation. Leaning prefixes for this scope.
- **Migrate the existing rsyncTLS Grocy/Tana backups to restic, or run both?**
  Restic supersedes them functionally; suggest cutting over per-app once a restore
  drill passes, then deleting the rsyncTLS `ReplicationSource` + backup PVC.
- **Where the rclone CronJob's S3 identity lives** — reuse `s3-identity-volsync`
  (read+write) or mint a read-only `s3-identity-volsync-reader` for the copy job
  (least privilege). Prefer the reader.

## Caveats

- Personal-Gmail OAuth tokens can still be revoked by account events
  (password change, security review); airlock's refresh loop will start failing for
  the `google_drive_backup` provider and it needs a one-time re-authorize through
  airlock's callback. Wire a Gatus/alert on both airlock refresh failures and the
  CronJob.
- **Access-token-only consumption assumes short runs.** The job uses airlock's
  ~1 h access token with no `refresh_token`, so a single run must finish inside the
  token lifetime — true for small DBs. If scope grows to large media (multi-hour
  syncs), switch the job to consume the `refresh_secret` (+ client creds) so rclone
  can refresh mid-run — accepting that it then co-refreshes with airlock, so pick
  one owner (e.g. drop airlock's refresh for that provider).
- Initial seed of the small config PVCs is trivial; the 750 GB/day cap only bites if
  scope later grows to large media (then it stretches the first seed over days —
  restic dedup helps, but plan for it).
- If offsite backup is the real goal rather than "use the 2 TB I already pay
  for," **Backblaze B2 (~$6/TB/mo) or Cloudflare R2** are natively supported by
  the VolSync restic mover with zero rclone and no rate-limit drama — the rclone
  CronJob (stage 2) would just target B2/R2 instead of Drive, or be dropped if
  restic writes there directly.
