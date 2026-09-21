# Forgejo Flux ownership handoff

Pending: fold `forgejo-namespace` and `forgejo-db` into `forgejo`, leaving
`forgejo-cache` separate. Delete this note after the live handoff checks pass.

1. Merge protection and verify both retiring owners have `prune: false`,
   `deletionPolicy: Orphan`, and Ready at their current generation.
2. Capture Namespace, CNPG Cluster, PVC/volume bindings, Secret, HelmRelease,
   Bucket, and S3Credentials UIDs; record ready app/database/cache endpoints.
3. Suspend only `forgejo-namespace` and `forgejo-db` immediately before merging
   consolidation. Verify their protection fields remain set.
4. After reconciliation, verify both retired owners are absent and `forgejo`
   owns the combined inventory. All captured UIDs and volume bindings must match.
5. Verify Forgejo HTTP/SSH, CNPG read/write/read-only, and Valkey master/replica
   EndpointSlices have ready backends; application and database each remain 2/2,
   HelmRelease/ExternalSecret/Bucket/S3Credentials are Ready, and Forgejo's
   `/api/healthz` reports success. Verify the live cache CR still has
   `kustomize.toolkit.fluxcd.io/name: forgejo-cache`.

Preflight: all four Flux owners Ready; app, CNPG, and Valkey each have two ready
pods; all five PVCs Bound. CNPG and Forgejo Services select stable application
labels. Valkey Services select Flux ownership labels, so its owner and CR stay
unchanged. The existing Bucket, S3Credentials, HelmRelease, and git PVC stay owned
by `forgejo`; only the Namespace, CNPG Cluster, and encrypted DB Secret move.

Rollback of an adopted Namespace/database is another protected ownership transfer:
do not simply remove their bases from a pruning owner.
