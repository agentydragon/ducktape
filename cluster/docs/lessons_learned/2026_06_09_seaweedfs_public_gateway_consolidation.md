# SeaweedFS public-s3 gateway

`cluster/k8s/seaweedfs/public-s3/` is the single public S3 gateway
(`s3.allegedly.works`). It runs its own `weed s3` process with a curated
multi-identity config because a gateway authenticates **every** identity in the
config it loads — there is no per-route scoping. So the all-tenant
`seaweedfs-s3` config (admin + every write key) must never be exposed publicly;
a public subset needs its own process. SeaweedFS scopes actions per identity, so
co-hosting identities on one gateway is safe.

## Adding an external identity

1. Add a credential Secret file under `public-s3/` with its own `.sops.yaml`
   recipient set and random `accessKey`/`secretKey`.
2. Add a `dataFrom.extract` for it plus a bucket-scoped identity block in
   `public-s3/s3-config-externalsecret.yaml`.
3. List the file in `public-s3/kustomization.yaml`, commit, push. ESO
   reassembles the config; Reloader rolls the deployment.

In-cluster-only tenants instead use `secrets/identities/*.sops.yaml`, which
assemble into the main `seaweedfs-s3` gateway — see
<2026_05_27_seaweedfs_eso_identities.md>.
