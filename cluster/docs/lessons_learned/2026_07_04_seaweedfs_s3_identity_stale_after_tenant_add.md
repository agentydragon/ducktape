# SeaweedFS S3 gateway serves stale identities after adding a tenant

**Recurring.** Seen 2026-06-08 (`attic`) and again 2026-07-04 (`registry-cache`,
the oci-cache Zot mirror). Symptom is `InvalidAccessKeyId` on S3 **writes** for a
newly added tenant.

**Superseded for operator-managed buckets (2026-08-17).** The installed operator
does provide `S3Identity` and `S3Credentials` CRDs backed by filer IAM. Use those
with the `Bucket.spec.access` policy instead of adding new identities to the
static JSON. The restart behavior below still applies to legacy static identities.

## Symptom

A new SeaweedFS S3 tenant (identity added under
`cluster/k8s/seaweedfs/secrets/identities/`) can't write:

```text
s3aws: InvalidAccessKeyId: The access key ID you provided does not exist in our records. (403)
```

Reads/lists may still succeed (anonymous, on a public bucket), so it looks half-working.
If only **some** gateway replicas are stale it is **intermittent** — one request 200s,
the next 403s — because the `seaweedfs-s3` Service load-balances across pods that loaded
the config at different times.

## Root cause

`weed s3` reads its identities from the **static `-s3.config` file** — the
`seaweedfs-s3-config` Secret, wired via the CR's `spec.s3.configSecret` — **only at
startup**. Adding/rotating a tenant makes ESO reassemble that Secret (see
`cluster/k8s/seaweedfs/secrets/externalsecret-s3-config.yaml`), but a running gateway does
**not** hot-reload it.

Reloader is supposed to cover this (`autoReloadAll: true`, see
`cluster/k8s/reloader/reloader.yaml`) and **does fire** — reloader logs show
`Changes detected in 'seaweedfs-s3-config' … updated 'seaweedfs-s3'`. But the roll does
**not stick**: the SeaweedFS **operator owns the `seaweedfs-s3` Deployment** and
reconciles it back to its CR-derived pod template, **reverting Reloader's patch**. On
2026-07-04 the ReplicaSet history showed exactly this — a fresh RS created at the reload
timestamp, then scaled back to 0 while the old RS returned to 2/2. Any pod that never
cycled keeps serving the pre-tenant identity set.

The `reloader.stakater.com/auto: "true"` annotation on `spec.s3.annotations` is **inert**:
it lands on the pod template, but Reloader reads the annotation off the workload's own
metadata, which the operator controls and we can't set via the CR.

## Diagnose

```bash
# reloader saw the change and tried to roll?
kubectl -n kube-system logs -l app=reloader-reloader --tail=2000 | grep seaweedfs-s3
# transient RS created then scaled to 0 = operator reverted the roll:
kubectl -n seaweedfs get rs | grep seaweedfs-s3
# pod ages vs. when the tenant merged — an older pod predates the identity:
kubectl -n seaweedfs get pods -l app.kubernetes.io/component=s3
# consumer side (e.g. Zot): InvalidAccessKeyId on blob commit
kubectl -n oci-cache logs deploy/zot | grep -i InvalidAccessKeyId
```

## Fix (now)

Force the pods to reload the current config. **Delete the pods** rather than patching the
Deployment — the operator does not revert a pod deletion; the ReplicaSet recreates them and
they read the current Secret at startup:

```bash
kubectl -n seaweedfs delete pod -l app.kubernetes.io/component=s3
```

`kubectl -n seaweedfs rollout restart deployment/seaweedfs-s3` also works (a prior manual
restart's `restartedAt` annotation has survived operator reconciliation), but pod deletion
is the surest bet given the revert behavior above.

**This restart is a required manual step whenever adding/rotating a legacy static
SeaweedFS S3 tenant.** Operator-managed filer IAM identities hot-reload instead.

## Durable options (none is a drop-in)

The operator is already on the latest (v1.0.30 / chart 0.1.33) — there is no newer-version
fix. The static-`configSecret` model is the constraint.

1. **Reloader `reloadStrategy: annotations`** — cheapest and now validated. When
   Reloader fired with its default _env-var_ patch, we watched the operator revert the
   container-spec drift (fresh RS scaled to 0), yet a `restartedAt` _annotation_ (not in
   the CR) survived on the same pod template. A 2026-07-04 throwaway Seaweed experiment
   confirmed the annotation strategy works: changing a disposable `seaweedfs-s3-config`
   Secret made Reloader add `reloader.stakater.com/last-reloaded-from` to the generated
   S3 Deployment's pod template, Kubernetes rolled to a new ReplicaSet, and forced
   Seaweed operator reconciles preserved the annotation and active RS. The one-line
   durable fix lives in `cluster/k8s/reloader/reloader.yaml`. Caveat: `reloadStrategy`
   is a **global** Reloader setting (affects every managed workload), but `annotations`
   is the less invasive patch mode for operator-owned workloads because it avoids
   mutating container specs.
2. **Automate the restart** (the guaranteed fallback if A doesn't survive the operator). A
   small reconciler in `cluster/provisioners/` (matching grocy/inventree/matrix) that hashes
   `seaweedfs-s3-config` and, on change, runs `kubectl delete pod -l
app.kubernetes.io/component=s3` — pod deletion is _not_ reverted by the operator. Isolated
   to seaweedfs; costs a new moving part + up to one poll-interval of lag.
3. **Filer-backed dynamic identities** — now the preferred path for operator-managed
   buckets. The installed operator exposes `S3Identity`, `S3Credentials`, `S3Policy`,
   and `S3PolicyBinding`; `Bucket.spec.access` supplies the bucket-scoped actions.
   A 2026-08-17 `wayback-archive` canary proved that credentials remain valid after
   restarting the internal S3 gateway and are accepted by both internal and public
   gateways. Public reachability is therefore an authentication boundary, not a
   credential-isolation boundary: keep every key confidential and rely on bucket policy
   for authorization.

**Recommended order:** migrate operator-managed buckets to (3). Keep (1)/(2) only for
legacy identities that still require the assembled static JSON.
