# SeaweedFS S3 gateway serves stale identities after adding a tenant

**Recurring.** Seen 2026-06-08 (`attic`) and again 2026-07-04 (`registry-cache`,
the oci-cache Zot mirror). Symptom is `InvalidAccessKeyId` on S3 **writes** for a
newly added tenant.

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

**This restart is a required manual step whenever adding/rotating a SeaweedFS S3 tenant**
until one of the durable fixes below lands.

## Durable options (none is a drop-in)

The operator is already on the latest (v1.0.30 / chart 0.1.33) — there is no newer-version
fix. The static-`configSecret` model is the constraint.

1. **Reloader `reloadStrategy: annotations`.** The operator preserves pod-template
   _annotations_ (it kept a `restartedAt` from a manual restart) but strips Reloader's
   default _env-var_ patch. Switching Reloader's global strategy to `annotations` may make
   its reloads survive reconciliation. Global setting — validate against all Reloader-managed
   workloads before adopting.
2. **Filer-backed dynamic identities.** SeaweedFS hot-reloads identities managed via the
   filer (`weed shell s3.configure` / the embedded IAM API / credential manager) with no
   restart. This is the "proper" fix but an architecture change away from the static
   `configSecret`, with known upstream footguns
   ([#8331](https://github.com/seaweedfs/seaweedfs/issues/8331),
   [#6442](https://github.com/seaweedfs/seaweedfs/issues/6442),
   [#6130](https://github.com/seaweedfs/seaweedfs/issues/6130)).
3. **Automate the restart.** A small controller/Job that deletes `seaweedfs-s3` pods when
   `seaweedfs-s3-config` changes — closes the gap without the operator conflict, at the cost
   of a moving part.
