# SeaweedFS: one consolidated public S3 gateway

**Date**: 2026-06-09
**Status**: Done. `public-s3` replaces the per-purpose `drivefs-artifacts-s3`
and `vm-images-s3` gateways and adds a read-only `claude-reader` identity.

## What changed

Previously each externally-reachable S3 consumer got its own gateway
Deployment + Service + HTTPRoute + config ExternalSecret
(`drivefs-artifacts-s3` at `drivefs-s3.allegedly.works`, `vm-images-s3` at
`vm-images-s3.allegedly.works`). Adding Claude as a reader would have been a
third.

Now there is a single public gateway, `cluster/k8s/seaweedfs/public-s3/`,
served at **`s3.allegedly.works`**. Its config is the union of all
externally-facing identities:

- `drivefs-artifacts-writer` / `drivefs-artifacts-reader`
- `vm-images-ci-writer` / `vm-images-cdi-reader`
- `claude-reader` — Read+List on `attic`, `drivefs-artifacts`, `vm-images`,
  `augur-assets`, `listing-monitor-captures`

The operator-managed `seaweedfs-s3` Service stays ClusterIP + all-tenant
(it contains `admin` and every write key) for in-cluster workloads. It must
never get a public route.

## Why this is safe (and why a separate process is still required)

- A `weed s3` process authenticates **every identity in the config it
  loaded** — there is no per-route or per-identity filtering. So the only way
  to expose a _subset_ of identities publicly is a separate process with a
  trimmed `-config`. That's why `public-s3` is its own Deployment and not just
  a route onto `seaweedfs-s3`.
- Co-hosting identities on one gateway is safe: SeaweedFS scopes actions to the
  identity, so one identity's credentials never reach another's buckets even
  when they share a process. The drivefs/vm-images write identities were
  already publicly reachable (on their own hostnames) before this change — the
  only net-new public exposure is the read-only `claude-reader`.

## SOPS recipient split (can't co-mingle creds in one file)

SOPS encrypts a whole file to one recipient set, so the three credential
classes stay in **separate** files even though one gateway consumes all of
them:

- `public-s3/drivefs-artifacts-credentials.sops.yaml` → admin, cluster-secrets,
  all user keys, ci (users upload; gaffer CI reads).
- `public-s3/vm-images-credentials.sops.yaml` → admin, cluster-secrets
  (in-cluster only).
- `public-s3/claude-reader-credentials.sops.yaml` → admin, cluster-secrets,
  **claude-web** (Claude Code web sessions decrypt it directly via
  `SOPS_AGE_KEY`).

The Secret **names** (`drivefs-artifacts-s3-credentials`,
`vm-images-s3-credentials`) are unchanged so the cross-namespace ESO consumers
(gecko's CDI reader, the vm-images-publisher writer) keep resolving them. The
merged config ExternalSecret pulls all three via `dataFrom.extract`; the key
names are collision-free (`writer*`/`reader*` vs `ciWriter*`/`cdiReader*` vs
`claudeReader*`).

## Cutover (no alias hostnames)

The old hostnames were dropped, not aliased, so every consumer was repointed in
the same change:

- gecko `DataVolume.url` → `https://s3.allegedly.works/...`
- `vm-images-publisher/publish.sh` `S3_ENDPOINT` → internal
  `http://public-s3.seaweedfs.svc.cluster.local:8333`
- gaffer-private `nix-attic-push.yml` endpoint → `https://s3.allegedly.works`
  (and the moved drivefs cred path)
- `github-secrets-sync` `dependsOn` → `seaweedfs-public-s3`

**Cross-repo caveat**: the gaffer-private workflow edit and the ducktape
gateway move are in separate repos, so the cutover is not atomic across them.
Merge the gaffer-private change together with or before the ducktape change so
gaffer CI never hits a dead `drivefs-s3` host.

## Adding a future external identity

No new Deployment/Service/Route. Append to the one gateway:

1. Add a credential Secret file under `public-s3/` (its own `.sops.yaml`
   recipient set), random `accessKey`/`secretKey`.
2. Add a `dataFrom.extract` for it and a new identity block (bucket-scoped
   `Read`/`Write`/`List`/`Tagging` actions) to
   `public-s3/s3-config-externalsecret.yaml`.
3. Add the file to `public-s3/kustomization.yaml`. Commit + push; ESO
   reassembles, Reloader rolls the deployment.

In-cluster-only tenants still use the per-tenant `secrets/identities/*.sops.yaml`
path that assembles into the main `seaweedfs-s3` gateway — see
<2026_05_27_seaweedfs_eso_identities.md>.
