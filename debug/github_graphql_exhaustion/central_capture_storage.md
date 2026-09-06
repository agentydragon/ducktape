# Central capture storage observations

On 2026-09-06, 05:27–05:34Z, the mounted central capture PVC had no retained
`kubelet_volume_stats_{capacity,available}_bytes` series. Its node's kubelet
scrapes were healthy, and another storage driver's PVC had those metrics.
This was a driver capability gap, not a fresh-mount delay.

The deployed SeaweedFS CSI driver v1.4.30 does not advertise `GET_VOLUME_STATS`
or implement `NodeGetVolumeStats`. Kubernetes v1.35.1 checks that capability and
returns not-supported before requesting statistics. The previous
`GitHubProxyCaptureStorageLow` rule therefore had no inputs for this PVC.
Sources: [driver capabilities](https://github.com/seaweedfs/seaweedfs-csi-driver/blob/v1.4.30/pkg/driver/nodeserver.go),
[kubelet CSI metrics](https://github.com/kubernetes/kubernetes/blob/v1.35.1/pkg/volume/csi/csi_metrics.go).

## Native collection budget

The replacement rules live in
[the proxy PrometheusRule](../../cluster/k8s/github-api-proxy/app/prometheus-rule.yaml).
SeaweedFS 4.44 already exports
`SeaweedFS_volumeServer_total_disk_size{type="normal",collection="<PV name>"}`.
The driver's default collection is the basename of its filer path, which is the
PV name for this claim. `kube_persistentvolumeclaim_info.volumename` supplies the
PVC-to-collection join without hardcoding a generated PV identifier.
Sources: [collection selection](https://github.com/seaweedfs/seaweedfs-csi-driver/blob/v1.4.30/pkg/driver/mounter.go),
[metric definition](https://github.com/seaweedfs/seaweedfs/blob/4.44/weed/stats/metrics.go),
[per-server accounting](https://github.com/seaweedfs/seaweedfs/blob/4.44/weed/storage/store.go).

Each volume server is currently scraped through two jobs. The rule takes the
maximum by collection and instance before summing across servers: duplicate
scrapes must not double the budget, but real replicas count. The maximum also
conservatively accommodates slightly different scrape times. A live evaluation
at 05:33:44Z returned 41,832 reported bytes divided by the 100-GiB PVC request.

The recording rule emits a ratio only with matching collection bytes, the
named PVC's mapping, and a positive storage request. The budget warning fires
above 85% for five minutes; a separate warning fires when the ratio is absent
for five minutes. There is no zero-fill. An observed zero-byte collection is
different from an unobserved or not-yet-created collection.

## Limits and validation

- This is a **physical collection storage-budget warning**, not an exact
  free-space, logical capture-size, or authoritative CSI-quota measurement.
  Normal-volume data includes replicas but not every backend/index/EC allocation.
  Revisit the rule if the collection becomes shared or uses different accounting
  or erasure coding. A PVC request is the chosen operational budget, not proof
  that the backend can satisfy it.
- Complete loss of the collection metric, mapping, or positive request triggers
  the missing-input warning. Losing only some volume servers can lower the sum
  while leaving a ratio present. These rules do not establish an exhaustive
  server roster or prove collection completeness; check the storage scrape
  targets before interpreting a falling ratio as reclaimed storage.
- A simple application `statvfs` gauge would not make the signal authoritative.
  The pinned mount source caches filer statistics, caps capacity at the mount
  quota, and can return successful FUSE statistics with old values after a filer
  request fails. See [mount statistics](https://github.com/seaweedfs/seaweedfs/blob/79b87202136c/weed/mount/weedfs_stats.go).
- Capture-write failure remains a distinct, sticky evidence-loss alert. This
  change adds no application collector, automatic retention, deletion, or restart.
  Rule evaluation and notification delivery must still be verified after rollout.

[Promtool regression cases](../../cluster/validation/testdata/github_proxy_rules.yaml)
exercise the actual deployed expressions through
`bbr test //cluster/validation:test_github_proxy_rules`, including duplicate
scrapes, replicas, missing inputs, threshold duration and recovery, and total
collection scrape loss. This note contains only the central storage finding;
raw captures and private request data are not published.
