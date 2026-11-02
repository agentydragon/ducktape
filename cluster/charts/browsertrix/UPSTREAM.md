# Vendored Browsertrix chart

This chart is based on the `chart/` directory from Webrecorder Browsertrix
`v1.24.2`:

- Repository: <https://github.com/webrecorder/browsertrix>
- Release tag: `v1.24.2`
- Upstream commit: `380ed775c9d5b759624ec4b04711e42e3450d413`
- Immutable source tree:
  <https://github.com/webrecorder/browsertrix/tree/380ed775c9d5b759624ec4b04711e42e3450d413/chart>
- Snapshot taken: 2026-08-06
- License: AGPL-3.0; see `LICENSE.browsertrix`

The initial snapshot copied upstream `chart/`, excluding its development-only
`test/` and `examples/` directories. The production-only removals and local
changes below were then applied.

Ducktape carries a narrow downstream patch because the upstream chart:

- grants its Kubernetes controller permissions to both the namespace's
  `default` ServiceAccount and `system:anonymous`;
- has no way to assign those permissions to a dedicated ServiceAccount;
- couples the application's public origin to creation of an Ingress; and
- only enables the frontend's same-origin object-storage proxy for bundled
  MinIO.

The downstream patch removes anonymous access, gives the backend and generated
background jobs a dedicated controller ServiceAccount, prevents unrelated
workloads from mounting that token, supports arbitrary node selectors and a
Gateway API deployment through `external_url`, and permits proxying the existing
in-cluster SeaweedFS S3 bucket. Mutable crawler and rclone image tags are pinned
for reproducible deployments.

This production-only fork intentionally deletes the upstream Ingress and
bundled MinIO templates, the optional Elasticsearch/Kibana/Fluentd logging
subchart, the unused crawler egress-proxy helper subchart, and
local-development NodePort support. The decommissioned trial published
Browsertrix through its Cilium Gateway, stored archives in SeaweedFS, and used
the cluster's existing logging stack. The chart and parked manifests remain a
revival package. When refreshing from a newer upstream tag, do not restore
`templates/ingress.yaml`,
`templates/minio.yaml`, `admin/logging/`, `proxies/`, the WACZ signer, or the frontend/email NodePort values
and template branches; retain the Gateway-only origin and external storage
proxy changes in `configmap.yaml` and `frontend.yaml`.

## Updating from upstream

1. Resolve the desired Browsertrix release tag to its immutable commit and
   update the provenance fields above.
2. Compare that commit's `chart/` directory with this directory. Copy updates
   only for retained files; continue excluding `test/`, `examples/`, Ingress,
   bundled MinIO, the admin logging subchart, local NodePort support, and optional WACZ signing.
3. Reapply and review the dedicated-ServiceAccount, token-automount, node
   selector, Gateway origin, SeaweedFS proxy, image-pin, and crawler-network
   policy changes described above.
4. Run `helm dependency build`, `helm lint`, render using the Ducktape
   HelmRelease values, parse/render the runtime Jinja templates, and run the
   cluster Flux/SOPS validation targets before deploying.
