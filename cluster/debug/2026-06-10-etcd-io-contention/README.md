# etcd I/O contention on control-plane disks

Investigation started 2026-06-10 after repeated controller leader-election
timeouts and high apiserver lease latency on the OVH Kimsufi control planes.

This directory is the shared home for incidents where ordinary pod I/O on a
control-plane node starved etcd on the same physical disk:

- [image-automation-controller-etcd-io-starvation.md](image-automation-controller-etcd-io-starvation.md)
- [promtail-page-cache-etcd-starvation.md](promtail-page-cache-etcd-starvation.md)

## Current state

Live sample at 2026-06-10 14:54 PDT, after the Flux upgrade:

- `image-automation-controller` is no longer the dominant current writer, but
  still averaged about 205 KB/s writes over 30m on `ovh-ns103711`.
- `kustomize-controller` is the dominant recent Flux writer:
  - 30m average: about 2.56 MB/s and 89 filesystem writes/s.
  - 10m average: about 237 KB/s.
  - Pod: `flux-system/kustomize-controller-55d7f989dc-m7gbq`.
  - Node: `ovh-ns103711`, which is also a control-plane node.
- `/dev/sda` busy over 10m:
  - `ovh-ns103711` (`10.42.0.14`): 59%.
  - `ovh-ns103656` (`10.42.0.13`): 56%.
  - `ovh-ns102453` (`10.42.0.15`): 25%.
- apiserver `PUT leases` p99 over 10m:
  - `10.42.0.14:6443`: 20s.
  - `10.42.0.13:6443`: 4.45s.
  - `10.42.0.15:6443`: 0.93s.

The symptom path is therefore still present: control-plane disks are busy enough
that the leader-election/node-lease write path can hit multi-second p99 latency.

## What kustomize-controller is doing

`cluster/k8s/flux-system/kustomization.yaml` had this local patch:

- `--concurrent=16`
- `--requeue-dependency=5s`

Recent `kustomize-controller` logs over 20m:

- 1413 `Dependencies do not meet ready condition, retrying in 5s`
- 407 `server-side apply completed`
- 317 `All dependencies are ready, proceeding with reconciliation`

The dependency retry storm was concentrated:

| Kustomization                 | Retry count over 20m |
| ----------------------------- | -------------------: |
| `openclaw-gateway-agent-rbac` |                  236 |
| `matrix-agent-rbac`           |                  236 |
| `docker-ci-agent-rbac`        |                  236 |
| `augur-agent-rbac`            |                  236 |
| `arc-secrets`                 |                  236 |
| `proxmox-proxy-agent-rbac`    |                  235 |

Current non-healthy roots and notable followers:

- `harbor-db`: `HealthCheckFailed`, `Cluster/harbor/harbor-db` `NotFound`.
- `augur`: `HealthCheckFailed`, `Deployment/augur/augur` `Failed`.
- `proxmox-proxy`: `HealthCheckFailed`, deployment still `InProgress`.
- `listing-monitor-smoke`: dry-run failed calling the external-secrets webhook.
- Multiple dependency/revision followers remain behind those roots.

The I/O appears to be spent on two classes of work:

1. Legitimate post-source-revision fan-out: artifact unpack/build/apply for many
   Kustomizations after cluster-relevant commits.
2. Pathological retry churn: unhealthy dependency followers waking every 5s,
   keeping the controller hot after the normal wave should settle.

Mechanically, a Kustomization that passes dependency checks is not a cheap
"compare a few paths" operation. In kustomize-controller v1.8.5 it:

1. reads the referenced source object and artifact URL;
2. creates a fresh temp dir inside the controller container filesystem;
3. downloads and untars the source-controller artifact into that temp dir;
4. generates `kustomization.yaml` if needed;
5. runs `kustomize build`, including decryption and post-build substitution
   where configured;
6. parses the rendered YAML into unstructured Kubernetes objects;
7. for every rendered object, gets the live object, performs a server-side
   dry-run apply, compares dry-run output to live state, and only then skips the
   real apply if unchanged;
8. applies drifted/missing objects, updates inventory/status/history, prunes
   stale inventory, and optionally waits on health checks.

So an "unchanged" object avoids a real apply/resourceVersion bump, but still
costs live object GET + server-side dry-run apply. With one source artifact
currently about 8 MB compressed, hundreds of Kustomizations after a source
revision can easily produce GB-scale temp-dir writes from repeated artifact
fetch/untar/build work.

## Source artifact scope

The main `flux-system` `GitRepository` is the source for most of the graph:

- 254 of 263 live Flux Kustomizations use `GitRepository/flux-system`.
- 18 live Flux Terraform resources use the same source.
- All live Kustomization paths using that source are under `cluster/k8s/`.
- All live Terraform paths using that source are under `tf/gitops/`.

The previous sparse checkout included all of `cluster/` and all of `tf/`.
Locally, that pulled in several MiB of non-apply content such as
`cluster/debug/` and `cluster/docs/` that every Kustomization reconcile then had
to download and unpack. Narrow it to:

- `cluster/k8s/`
- `tf/gitops/`

This reduces artifact size and temp-dir write amplification. It does not remove
all broad fan-out: the `GitRepository` still tracks branch `devel`, and Flux
uses the branch HEAD SHA as the source revision. A non-cluster commit can still
create a new source revision even if the sparse artifact contents are almost
unchanged. Avoiding that class of fan-out requires a source that only advances
for deployable content, for example a generated deploy branch or OCI artifact.

## Kustomization count

Having a few hundred Kustomizations is not inherently wrong. The tradeoff is
mostly operational:

- Good: smaller ownership units, clearer status per app/component, bounded
  prune scope, explicit dependency edges, and easier selective reconcile.
- Bad: each Kustomization is a reconcile unit with its own source artifact
  fetch/unpack/build, API dry-run apply drift check, health wait, status update,
  events, and dependency polling.

The expensive combination here is not just "many Kustomizations"; it is many
Kustomizations sharing one branch-tracking source plus a large dependency graph
with unhealthy roots. Splitting the Git repository only helps if it also splits
the source revision stream. Moving the same manifests into another monorepo
layout but still advancing one shared `GitRepository` would not materially
change kustomize-controller fan-out.

Prefer this order before any repo split:

1. Keep the main source artifact narrow (`cluster/k8s/`, `tf/gitops/`).
2. Fix or suspend unhealthy roots so dependency followers stop polling.
3. Move static/cold Kustomizations to longer drift intervals, such as 1h.
4. Merge only tiny static units where the separate status/prune boundary is not
   useful.
5. If non-cluster commits still cause expensive deploy waves, introduce a
   deploy-only source stream rather than a human-facing repo split.

Parallelism is inside one active controller process today:

- live `kustomize-controller` replicas: 1;
- `--concurrent=16` means up to 16 Kustomization reconciles in parallel inside
  that one pod;
- `--concurrent-ssa` defaults to 4 and controls concurrent drift-detection
  goroutines inside one Kustomization reconcile;
- extra replicas would not automatically share work because leader election is
  enabled. Active-active spreading would require separate controller
  Deployments with disjoint watch label selectors and separate leader-election
  IDs, plus labels on Kustomizations.

The live Flux source revision at the time was
`devel@sha1:f6662c850a9330076edaf83aa9cefab62dbc4f89`. Comparing it to the prior
live root Kustomization revision (`7daf37a9e11b5261d89ac71a61523e565478eb62`)
does include `cluster/` changes, so the latest broad fan-out was not a pure
non-cluster no-op. Separately, the root `GitRepository` tracks branch HEAD, so
any devel commit can become a new Flux source revision even though sparse
checkout limits artifact contents to `cluster/` and `tf/`.

## Immediate mitigation

Patch `kustomize-controller` to keep moderate fan-out but stop the 5s dependency
retry multiplier:

- `--concurrent=16` -> `--concurrent=8`
- `--requeue-dependency=5s` -> `--requeue-dependency=30s`
- `GitRepository/flux-system` sparse checkout -> `cluster/k8s/` and
  `tf/gitops/`

This intentionally does not make Flux worker-only. The cluster currently allows
regular workloads on the three control-plane nodes, and that is useful capacity.
The immediate goal is to reduce burst pressure while keeping the scheduling model
intact.

## I/O isolation

Kubernetes `PriorityClass` and `system-cluster-critical` do not give etcd higher
block I/O priority. They affect scheduling and preemption, not the disk scheduler.

Linux has mechanisms such as `ionice` and cgroup v2 `io.weight` / `io.max`, but
there is no simple Kubernetes PodSpec field that says "this pod has low disk I/O
priority", and Talos manages etcd as part of the control-plane machine config.
Any cgroup I/O controller solution would need node/runtime-level work and a
careful Talos validation pass.

The reliable isolation boundary is physical or block-device separation:

- Today the Kimsufi control planes install Talos on `/dev/sda`.
- Talos EPHEMERAL lives on that system disk and contains container runtime data,
  logs, downloaded images, and etcd data.
- `/dev/sdb` is already consumed as a Talos `UserVolumeConfig` named
  `seaweedfs-data`, mounted at `/var/mnt/seaweedfs-data` and used by
  `local-path-ovh`.
- A generated Talos v1.12 control-plane config did not expose an obvious
  `cluster.etcd.dataDir` field.

So a hard guarantee that pod I/O cannot starve etcd likely means changing the
control-plane storage layout, not only Kubernetes scheduling:

- add or reserve a dedicated device/partition for etcd, if Talos supports a
  supported mount/data-path pattern for it;
- or move container runtime / EPHEMERAL pressure away from the etcd device;
- or reprovision with a disk layout that separates system, etcd, and pod/PV I/O.

## Next steps

1. Deploy the kustomize-controller throttle above and watch:
   - dependency retry log rate;
   - `container_fs_writes_bytes_total` and `container_fs_writes_total` for Flux;
   - `/dev/sda` busy time on the three control-plane nodes;
   - apiserver `PUT leases` p99.
2. Fix the real unhealthy Kustomization roots: `harbor-db`, `augur`,
   `proxmox-proxy`, and `listing-monitor-smoke`.
3. Add alerts for:
   - control-plane `/dev/sda` busy > 50% for 10m;
   - any non-system pod on a control-plane node writing > 1 MB/s for 10m;
   - apiserver `PUT leases` p99 > 500ms and > 2s;
   - kustomize-controller dependency retry rate above a small steady-state
     threshold.
4. Investigate Talos-supported storage isolation for etcd versus EPHEMERAL. Do
   not rely on Kubernetes priority classes for this.
5. Keep control-plane scheduling available, but treat high-write workloads as a
   separate class. Use soft placement preferences or workload labels for
   bursty/background controllers only after the lower-level disk isolation plan
   is clear.
