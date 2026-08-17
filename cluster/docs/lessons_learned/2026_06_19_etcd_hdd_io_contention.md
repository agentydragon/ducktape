# etcd lease-PUT latency: control-plane etcd on rotational HDDs + workload I/O contention

**Date:** 2026-06-19
**Status:** Root cause confirmed. Immediate mitigations applied (etcd defrag; flux
controllers pinned off the control-plane nodes). Structural fix (etcd on NVMe) and the
remaining workload pins (tofu runners, augur) are tracked below.
**Severity:** Intermittent — `ControlPlaneLeasePutLatencyCritical` fires under load
bursts; etcd health checks occasionally fail `context deadline exceeded`. No data loss,
no quorum loss observed. **Recurred 2026-06-28 as a full outage** (two control planes
NotReady, Forgejo 500s) — see the dated section below.

## 2026-08-17 follow-up — isolate `ovh-ns103656` while measuring the effect

`ovh-ns103656` again flapped `NotReady`; the two SSD-backed control planes preserved
etcd quorum. Treat this as an active reliability incident, not a clean drain.

- Replaced four _verified non-primary_ local-PV CNPG replicas pinned to this node:
  Haku Console (2 GiB), Haku Mailbox (10 GiB), LiteLLM (5 GiB), and Matrix (10 GiB).
  Each was removed only after confirming a healthy primary, then reseeded onto worker
  `ovh-ns102453`; its cluster returned to the expected healthy replication count before
  continuing. Plaid MCP and other local state were left in place.
- Set `allowSchedulingOnControlPlanes = false` in the Talos config for this node only,
  and added `node-role.kubernetes.io/control-plane:NoSchedule` to its existing Node as
  an immediate bridge. The taint blocks new ordinary placements; it does not evict
  existing Pods. The declarative change must remain merged so future registration keeps
  the policy.
- Kept SeaweedFS on its separate `/dev/sdb` data volume. Public Coder, Haku OpenClaw,
  and Alertmanager remain temporary, explicit control-plane exceptions pending their own
  migration decisions.

Compare subsequent `NotReady` frequency/duration, lease-PUT and apiserver latency, and
`/dev/sda` utilization/queue/bytes against this baseline. Only restore the default
control-plane taint on every CP after each intentionally permitted CP workload has a
named toleration and owner.

## 2026-06-28 recurrence — escalated to a real outage

The same mechanism recurred and this time **flapped two control planes NotReady**
(`ovh-ns103656`, `ovh-ns103711`) for ~10–15 min around 03:06–03:11Z, throwing Forgejo
500s and restarting `authentik-server`. etcd raft apply latency hit **60 s**
(`apply request took too long: 59.9s`); `talosctl service etcd` showed `Fail` on `.13`.
Quorum held (leader `.15` + `.14` stayed in sync), so no data loss — but the API-server
brownout cascaded. Hosts were alive throughout (ping + Talos `apid`/`kubelet` healthy);
the failure was purely etcd fsync starvation, not hardware.

**Data-driven trigger** (Mimir `container_fs_writes_bytes_total` per pod, increase over
the 15 min into the stall, on the two CP nodes) — one batch job dominated by an order of
magnitude:

| pod                    | namespace             | node | MB written / 15 min |
| ---------------------- | --------------------- | ---- | ------------------- |
| `agent-box-img-…`      | `vm-images-publisher` | .14  | **15014**           |
| `mimir-ingester-1`     | monitoring            | .14  | 916                 |
| `mimir-ingester-0`     | monitoring            | .13  | 814                 |
| `attic-db-3`           | nix-cache             | .13  | 270                 |
| `loki-write-0`         | loki                  | .13  | 214                 |
| `seaweedfs-filer-db-3` | seaweedfs             | .13  | 213                 |

The `vm-images-publisher` CronJob (NixOS VM-image build → qcow2, ~15 GB written to the
node overlay/emptyDir) carried only `nodeSelector: region=hil` and landed on a CP HDD
node, saturating `sda5` (I/O-pressure `full avg10 ≈ 35 %`, queue depth ~28) and starving
etcd. The chronic stateful writers (mimir/attic/loki/seaweedfs/CNPG) are real but each
< ~1 MB/s sustained; the acute trigger was this single ~17 MB/s batch build.

### Mitigation applied (2026-06-28)

- **Stateful tier pinned soft-off the control plane** — `preferred` nodeAffinity
  `node-role.kubernetes.io/control-plane DoesNotExist` added to all ~22 hil-ovh stateful
  workloads (9 CNPG clusters, 8 Valkey, Loki, Mimir, langfuse + its clickhouse/zookeeper,
  gatus, forgejo). Soft, to keep the CP nodes as overflow capacity if both workers are
  down.
- **VM-image builder pinned hard-off the control plane** — `required` nodeAffinity on the
  `vm-images-publisher` CronJob jobTemplate. A batch build has no availability need and
  belongs on the NVMe workers; this is the workload that caused the outage.
- Complements the 2026-06-19 flux-controller pins. Still pending: the tofu-runner pins
  (item 3 below) and the structural etcd-on-NVMe move (item 5).

## Symptom

`ControlPlaneLeasePutLatencyHigh` (warning, p99 > 0.5s) firing continuously and
`ControlPlaneLeasePutLatencyCritical` (critical, p99 > 2s) firing under bursts. The
alert metric is apiserver-side:

```promql
histogram_quantile(0.99, sum by (instance,le) (
  rate(apiserver_request_duration_seconds_bucket{resource="leases",verb="PUT",scope="resource"}[10m])
)) > 2
```

`talosctl -n <cp> service etcd` shows `HEALTH OK` but with recurring
`Health check failed: context deadline exceeded` events, most frequent on
`ovh-ns103656` (the raft leader).

## Root cause: etcd fsync on spinning rust

etcd's write path (raft) commits every write to a quorum of members, and each member
must `fsync` its WAL before acking. Lease PUT latency is therefore gated by the
**slowest disk in the quorum**. The OVH Kimsufi KS-5 control-plane nodes have **only
rotational SATA HDDs** — both `sda` and `sdb` on all three are `ROTATIONAL true`:

| Node                 | role           | etcd disk (`sda`)               | WAL fsync p99 | backend commit p99 |
| -------------------- | -------------- | ------------------------------- | ------------- | ------------------ |
| `ovh-ns102453` (.15) | CP             | HGST HUS726T4TAL (4 TB 7200rpm) | 54 ms         | 59 ms              |
| `ovh-ns103656` (.13) | CP, **leader** | HGST HUS726020AL (2 TB 7200rpm) | 124 ms        | 226 ms             |
| `ovh-ns103711` (.14) | CP             | HGST HUS726020AL (2 TB 7200rpm) | 126 ms        | 240 ms             |

etcd's healthy targets are **WAL fsync p99 < 10 ms** and **backend commit p99 < 25 ms**.
These are ~10× over. Because raft needs a quorum fsync, the two slow followers gate the
cluster-wide lease PUT p99 to ~0.95 s (spiking > 2 s under load = the critical alert).
Raft itself is healthy (all members same index/term, no errors, no leader churn) — the
problem is purely disk latency.

**There is no SSD/NVMe on the control-plane nodes** to move etcd onto. The fast storage
is on the wrong nodes: the KS-GAME **worker** nodes `ovh-ns104952` / `ovh-ns104963`
(.16/.17) each have **two Intel NVMe SSDs** (used today for SeaweedFS volume data).

## What competes with etcd on the control-plane disks

The OVH control-plane nodes carry **no `NoSchedule` taint**, so the scheduler freely
lands general workloads on them, and their write I/O shares the etcd spindle. Node-level
ground truth (node-exporter, device `sda`):

| Node | write KiB/s | disk busy | write IOPS | avg write latency |
| ---- | ----------- | --------- | ---------- | ----------------- |
| .15  | 1714        | 24%       | 113        | 8.7 ms            |
| .14  | 789         | 49%       | 94         | 18.4 ms           |
| .13  | 460         | 46%       | 90         | 11.6 ms           |

Per-pod attribution (cAdvisor `container_fs_writes_bytes_total{device="/dev/sda"}` —
**approximate**; it over-counts logical vs coalesced device writes, so treat as relative,
not additive). etcd itself runs as a Talos host process, so it does **not** appear in
cAdvisor — its share is the node total minus the containers:

- **.15** (fast disk, copes): `flux-system/image-automation-controller` ~988 KiB/s,
  `source-controller` ~376, four `*-tf-runner` pods (tofu-controller) ~330+165+35,
  `kube-apiserver` ~275.
- **.14** (slow, worst etcd latency): `augur/augur-evidence-ingest` ~490 KiB/s,
  `airlock-oidc-proxy-tf-runner` ~306, `kube-apiserver` ~53.
- **.13** (slow, leader): `monitoring/alloy` ~122 KiB/s, `kube-apiserver` ~91, plus
  etcd's own leader fsync/commit load (~52% of the device, the non-container remainder).

The danger is not only current placement but that flux controllers and tofu runners can
land on **any** untainted node — so a big terraform apply or an image-automation cycle
can drop onto the leader (.13) at any time and spike its fsync queue.

## Remediation

### Done (2026-06-19)

1. **etcd defrag** — DB was ~76% fragmented (345–357 MB allocated, ~76 MB in use).
   `talosctl -n <member> etcd defrag`, followers first, leader last; each member
   345–357 MB → **76 MB** (100% utilization), no leader election, all `HEALTH OK`.
   Fewer bbolt pages per commit → less fsync work per write on the HDDs. Re-run
   periodically (etcd refragments as it churns); not a permanent fix.
2. **Flux controllers pinned off the control-plane nodes** — soft (preferred)
   nodeAffinity `node-role.kubernetes.io/control-plane DoesNotExist`, applied to all
   six controller Deployments via a patch in
   <../../k8s/flux-system/kustomization.yaml>. The controllers use `emptyDir` for
   `/data` and `/tmp`, so their git-clone / artifact / image-automation writes land on
   the host disk; this keeps that I/O off the etcd spindles and onto the NVMe workers in
   normal operation. Soft (not required) because the controllers are
   `system-cluster-critical` and must retain a scheduling fallback if both workers are
   down.
3. **Throttled kustomize-controller's dependency retry-storm.** The controllers were
   also amplifying I/O directly: `--concurrent=16` + `--requeue-dependency=5s` made
   unhealthy dependency followers wake every 5s and re-run the full reconcile path
   (artifact fetch + untar + build + per-object server-side dry-run) for hundreds of
   Kustomizations after each source revision. Set `--concurrent=8` /
   `--requeue-dependency=30s` (`cluster/k8s/flux-system/gotk-components.yaml`) and
   narrowed the `flux-system` GitRepository sparse checkout to `cluster/k8s/` +
   `tf/gitops/` (was all of `cluster/` + `tf/`, dragging `debug/`/`docs/` into every
   artifact). Per-culprit retry-storm RCAs live under
   <../../debug/2026-06-10-etcd-io-contention/>.

### Pending

3. **Pin the tofu-controller runner pods off the control plane.** Each runner does
   `tofu plan/apply` (provider plugin downloads + working dirs) on the node's disk —
   ~165–330 KiB/s bursts, and they can land on the leader. The placement lever is
   `Terraform.spec.runnerPodTemplate.spec.{nodeSelector,affinity,tolerations}` (verified
   on the CRD). For ephemeral batch runners use **required** anti-affinity (no
   availability cost). **The blocker is that this is copy-pasted across ~22 hand-written
   `cluster/k8s/**/\*-tf.yaml`/`terraform.yaml`files** (each already repeats an
identical`runnerPodTemplate.spec.env`with the`tofu-state-db-credentials`
   PGPASSWORD). This config is purely cross-cutting (identical for every CR) and should
   be centralized before adding more to it — see "Centralizing the Terraform CR runner
   template" below.
4. **Move `augur-evidence-ingest` off the control plane** (~490 KiB/s sustained on .14
   during ingest; also the job that intermittently fails). Cross-repo — augur is
   reconciled from `gaffer-private`; the pin must be made there on the CronJob's pod
   template.
5. **Structural fix: etcd belongs on NVMe.** The KS-5 control planes have only HDDs while
   the KS-GAME workers (.16/.17) have NVMe. Either re-designate the NVMe nodes as
   control-plane (quorum migration via Talos machine config), or obtain SSD-backed OVH CP
   boxes. Large topology change — plan separately. Until then, defrag + keeping competing
   I/O off the CP spindles is the best available mitigation. Staged plan (Stage 2 promotes
   the NVMe KS-GAME nodes into the quorum): <../plans/ovh_storage_tiering.md>.

## Centralizing the Terraform CR runner template

The ~22 GitOps `Terraform` CRs each hand-repeat the same `runnerPodTemplate` (PGPASSWORD
env today; runner affinity/resources tomorrow). Options, best-first:

1. **Global default at the controller** — **ruled out (verified 2026-06-19, v0.16.1).**
   There is no cluster-wide default runner pod template. The controller's `runnerPodSpec`
   builder reads `NodeSelector`/`Affinity`/`Tolerations` _exclusively_ from
   `Terraform.spec.runnerPodTemplate.spec.*` with no fallback
   (`controllers/tf_controller_runner.go`); the only global runner knob is the
   `RUNNER_POD_IMAGE` env (image only). The chart's `runner:` block exposes only
   `image`/`grpc`/`creationTimeout`/`serviceAccount`, and the controller flags only
   `--runner-creation-timeout` / `--runner-grpc-max-message-size`. So every CR must keep
   its own `runnerPodTemplate` — use option 2 or 3.
2. **Shared kustomize component** (recommended) — put the `runnerPodTemplate` patch in one
   `components/` dir targeting `kind: Terraform`; each flux kustomization adds a single
   `components:` line. Content lives once; works today regardless of controller support.
3. **Bazel codegen** — generate the `Terraform` CRs from a minimal per-module spec with
   the runner template baked into the macro (fits the repo's `@rules_tf` codegen model;
   most upfront work, strongest SSOT).

## Cross-references

- SeaweedFS BestEffort / descheduler eviction RCA (same "stateful infra needs resource &
  scheduling hygiene" theme, different mechanism):
  <2026_06_19_seaweedfs_descheduler_dns_race_crashloop.md>
- Node hardware: <../../README.md> § Node Types / Storage.
