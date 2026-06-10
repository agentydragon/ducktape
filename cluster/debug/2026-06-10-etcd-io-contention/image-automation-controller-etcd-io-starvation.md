# image-automation-controller I/O starving etcd (2026-06-10)

## Symptoms

- `seaweedfs-csi-driver-controller` crash-looping every ~8 minutes with:
  ```text
  error retrieving resource lock: Get "https://10.96.0.1:443/...": context deadline exceeded
  stopped leading
  ```
- `cnpg-cloudnative-pg` operator restarting ~daily with leader election timeout
- `kyverno-cleanup-controller`, `node-feature-discovery-master`, `seaweedfs-operator`
  all showing recent restart bursts (~12 restarts, last "12 minutes ago")
- Baseline apiserver `PUT leases` p99 latency **~800ms–1s** cluster-wide, with
  periodic spikes to **4–9s** roughly every few minutes

All affected controllers ran on or accessed `ovh-ns102453` (a Talos KS-5 control plane
node). The CSI provisioner has a 10s `renewDeadline`; CNPG uses 5s — both too short for
the spike magnitude.

## Root Cause

**`image-automation-controller` (v1.0.4) was doing a full git clone of the
`ducktape` repo (~540 MB) every 5 minutes, writing ~9 MB/s in 60–90 second
bursts, directly onto `sda` of `ovh-ns102453`.**

`ovh-ns102453` is a control plane node where etcd also writes its WAL to `sda`.
The burst writes saturated the disk's IOPS queue, delaying etcd WAL fsyncs, which
cascaded into apiserver slowness on all three CP nodes simultaneously.

Confirmed via Mimir metrics:

- `container_fs_writes_bytes_total` for `image-automation-controller` pod: **544 KB/s
  average** over 30 min, with burst profile of **0 KB/s → 9 MB/s → 0 KB/s** every
  5 minutes (matching the `interval: 5m` in `ImageUpdateAutomation`).
- `histogram_quantile(0.99, rate(apiserver_request_duration_seconds_bucket{verb="PUT",
resource="leases"}[2m]))`: baseline 0.8–1s on all three CP nodes, spikes to 4–9s
  at timestamps correlating exactly with controller crashes.
- `node_disk_io_time_seconds_total` on `ovh-ns102453 sda`: 20–78% utilization
  continuously.

Secondary contributors: multiple active `tf-runner` pods (tofu-controller Terraform
reconciliations) each writing ~100 KB/s to `sda` overlayfs.

## Why v1.0.4 did a full clone

`image-automation-controller` v1.0.4 clones the git repository into its own tmpdir
in the container filesystem (on the host's `sda` via overlayfs) for every reconcile
cycle. It has no `depth` (shallow clone) option in its API and does not reuse the
source-controller's existing git checkout. Each clone of the `ducktape` monorepo
writes ~540 MB.

The controller had not been upgraded with the rest of Flux: as of this incident, all
other controllers were at v1.4–1.7 (the v2.7.5 bundle) while image-automation and
image-reflector were still at v1.0.4 (the initial v1 release, ~2 years behind).

## Fix

Upgraded all Flux controllers to v2.8.8 (PR #1991). In v1.1.x, image-automation-
controller reuses the source-controller's artifact cache rather than performing an
independent full clone, eliminating the burst I/O pattern.

The upgrade also resolved the `loki/promtail` HelmRelease stall (PR #1990, added
`upgrade.disableWait: true` for roaming-node DaemonSet pods).

## Investigation Path

1. Cluster health check surfaced `seaweedfs-csi` crash-loop and several recent
   controller restarts.
2. `kube-apiserver-ovh-ns102453` logs showed VPA webhook `operation not permitted`
   (EPERM — Cilium returning immediately for no-endpoint service, log noise only)
   and intermittent etcd `context deadline exceeded` on lease updates.
3. Mimir query of `apiserver_request_duration_seconds_bucket{verb="PUT",resource="leases"}`
   revealed the 0.8–1s baseline and 4–9s spikes cluster-wide, ruling out a
   single-node issue and pointing to etcd write latency.
4. `container_fs_writes_bytes_total` topk query on `ovh-ns102453` identified
   `image-automation-controller` at 544 KB/s average as the largest writer to `sda`,
   far exceeding the kube-apiserver's expected etcd traffic (151 KB/s).
5. Write rate time-series revealed the burst pattern matching the automation interval.
6. `flux version` showed image-automation-controller 2+ years behind the rest of Flux.

## Lessons

- **image-automation and image-reflector controllers must not run on CP nodes.**
  Even after the v1.1 improvement, their git operations should not compete with etcd.
  Add a worker-only `nodeAffinity` to these controllers (TODO).
- **Monitor `container_fs_writes_bytes_total` by namespace on CP nodes.** Any
  workload writing >50 KB/s sustained to the CP system disk is a risk for etcd.
- **Track Flux component versions independently.** The `gotk-components.yaml` header
  version can drift from individual component images if the file was partially
  hand-edited. The `flux version` CLI is the ground truth.
- **Baseline apiserver lease PUT p99 > 500ms is a warning sign.** Under normal
  conditions this should be well under 100ms. The 800ms baseline here indicated a
  persistent (not just transient) etcd write problem worth investigating even without
  a crash.
- **VPA at 0/0 replicas with its MutatingWebhookConfiguration still registered** causes
  every admission request to attempt a call to the dead webhook service. Cilium returns
  EPERM immediately (no timeout), so it's log noise only, but worth cleaning up.
  See `cluster/k8s/vpa/`.
