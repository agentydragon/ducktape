# SeaweedFS crash-loops: descheduler evictions + self-FQDN DNS race

**Date:** 2026-06-19
**Status:** Root cause confirmed. Eviction-protection layers A–C implemented (PR:
`seaweedfs-eviction-protection`); D handled by the props fix (PR #2395); E (DNS
init-wait) deferred.
**Severity:** Intermittent — brief raft re-elections / S3 read aborts, no data loss.

## Symptom

SeaweedFS master/volume/filer StatefulSet pods periodically restart with
`exitCode: 255`, `reason: Error` (NOT OOMKilled — pods have no memory limits and
tiny usage). The crash log is always a startup DNS failure on the pod's **own**
headless-service FQDN:

```text
F master.go:205 Master startup error: listen tcp: lookup
  seaweedfs-master-2.seaweedfs-master-peer.seaweedfs on 10.96.0.10:53: no such host
F metrics.go:849 listen tcp: lookup
  seaweedfs-volume-0.seaweedfs-volume-peer.seaweedfs on 10.96.0.10:53: no such host
```

`startedAt == finishedAt` (±1s): the process dies the instant it tries to bind.
It recovers ~12s later once the EndpointSlice for the new pod IP is reconciled and
CoreDNS serves the A record. Observed restart counts (2026-06-19): filer-0 9×,
master-1 7×, master-0 3×, master-2 2×, volume-0 2×.

## Root cause (two layers)

### Trigger: the descheduler evicts BestEffort pods off the OVER-utilized node every 15 minutes

`cluster/k8s/descheduler/helmrelease.yaml` runs descheduler as a CronJob
(`*/15 * * * *`) with the `LowNodeUtilization` balance plugin
(`thresholds cpu/mem 20`, `targetThresholds cpu 50 / mem 70`) and
`metricsUtilization.source: KubernetesMetrics` (so it balances on **actual CPU/mem
usage**, not requests).

**Direction (important — easy to get backwards):** `LowNodeUtilization` evicts pods
**from OVER-utilized nodes** (usage above `targetThresholds`) and reschedules them
onto **under-utilized** ones (below `thresholds`). `ovh-ns104952` is the node being
evicted _from_ because it is classified **over-utilized**, not under-utilized. From
the descheduler's own classification log (2026-06-19 09:00 run):

```text
lownodeutilization.go:215 "Node has been classified" category="underutilized" node="ovh-ns104963" usagePercentage={"cpu":14,...}
lownodeutilization.go:215 "Node has been classified" category="overutilized"  node="ovh-ns104952" usagePercentage={"cpu":51,...}   # > 50% targetThreshold
nodeutilization.go:193   "Evicting pods from node" node="ovh-ns104952"
nodeutilization.go:216   "Evicting pods based on priority, if they have same priority, they'll be evicted based on QoS tiers"
evictions.go:558 "Evicted pod" pod="seaweedfs/seaweedfs-master-2" strategy="LowNodeUtilization" node="ovh-ns104952"
evictions.go:558 "Evicted pod" pod="seaweedfs/seaweedfs-volume-0" strategy="LowNodeUtilization" node="ovh-ns104952"
evictions.go:558 "Evicted pod" pod="wayback-cache/wayback-cache-filler-workers-…"  (totalEvicted=3)
```

So the eviction direction is **correct/by-design**. The actual fragility is two-fold:

1. **Why the node is over-utilized:** because utilization is measured on _actual CPU_,
   transient bursts flip the node over the 50% line. A large contributor is the props
   grader crash-loop (BestEffort, no resource requests, real CPU) — see the separate
   matchability poison-pill fix. When that runaway is quiet (or the node is calm), runs
   evict 0 (`"No node is underutilized, nothing to do here, you might tune your
thresholds further"`).
2. **Why SeaweedFS specifically gets picked:** the descheduler evicts **BestEffort
   pods first** (QoS tiers). The SeaweedFS master/volume/filer pods have **no resource
   requests/limits at all**, so they are BestEffort — prime eviction targets — and they
   carry **no descheduler-exempt annotation**. Stateful raft-quorum pods being
   BestEffort is the underlying anti-pattern.

The matching `LowNodeUtilization` Events confirm `master-2` and `volume-0` crashed in
the exact eviction windows (e.g. finishedAt `08:45:10Z` / `08:45:34Z`).

### Structural cause: operator binds to the self-FQDN, which doesn't resolve yet

The seaweedfs-operator hardcodes the bind address to the pod's own headless-service
FQDN (not `$POD_IP`):

```text
weed ... master ... -ip=$(POD_NAME).seaweedfs-master-peer.seaweedfs ...
weed ... volume ... -ip=$(POD_NAME).seaweedfs-volume-peer.seaweedfs ...
```

On a freshly (re)started pod, that A record does not exist until kube-controller-
manager reconciles the new EndpointSlice — a multi-second window. SeaweedFS treats a
DNS miss on its own `-ip` as fatal and exits. `publishNotReadyAddresses: true` on the
peer Services (already set) only helps _other_ pods resolve this one; it does not make
the record exist in the sub-second window between pod start and EndpointSlice write,
and it does nothing for the pod resolving **itself**.

CoreDNS deepens the race: its Corefile has
`cache 30 { disable success cluster.local; disable denial cluster.local }`, so
cluster.local answers are never cached — every self-lookup hits the kubernetes plugin
live, which returns NXDOMAIN until the EndpointSlice exists.

## Ruled out

- **OOM:** all exits are 255/Error, never 137/OOMKilled; pods have no memory limits.
- **CoreDNS → apiserver watch failure:** CoreDNS replica `coredns-…-mtkf4` (on
  ovh-ns104952) DID log `Failed to watch … dial tcp 10.96.0.1:443: i/o timeout` and
  HINFO timeouts to upstream `169.254.116.108:53` — but **all timestamped 2026-06-08
  ~09:01**, a one-off node/network blip. The pod has `restarts=0`, started
  2026-06-08T09:00:31Z, `ready=true`, and served normal queries as recently as
  2026-06-18. A `--tail` grep still counts those old lines because CoreDNS is quiet at
  INFO when healthy. This was **historical, not the ongoing cause.** There is no
  NodeLocal DNSCache daemonset; `169.254.116.108` is just a node upstream resolver
  from `/etc/resolv.conf` (CoreDNS `forward . /etc/resolv.conf`).

## Remediation (ranked, NOT yet applied)

> **Correction (do not use the evict:false annotation).** An earlier draft suggested
> `descheduler.alpha.kubernetes.io/evict: "false"`. **That annotation does not exist** —
> the descheduler annotation only _enables_ eviction (`evict: "true"`); there is no
> "false"/block form. The real levers are PDB respect, pod Priority/`priorityThreshold`,
> the storage/PVC filters, and resource requests (QoS). See the layered fix below.

This is a **cluster-wide BestEffort-hygiene** problem, not SeaweedFS-specific: every
operator-managed stateful workload with no resource config is BestEffort — **all CNPG
database pods** (props-db, langfuse-db, seaweedfs-filer-db, authentik, forgejo, grafana,
matrix, …), the CNPG operator itself, and SeaweedFS master/volume/filer/s3. CNPG DBs
survive only because CNPG **auto-creates a PDB** per cluster (`<name>-primary`,
minAvailable 1) that the descheduler honors; **SeaweedFS master/volume/filer has no PDB
at all**, which is why it is the one that actually gets evicted.

Three independent axes protect against different failure modes — the principled fix is a
**layering**, not one switch (the descheduler only governs _voluntary_ eviction; node
OOM / kubelet node-pressure eviction are _involuntary_ and ignore PDBs):

### A. Resource requests on the stateful pods (foundational) — IMPLEMENTED

Set `requests` (cpu+mem; optionally limits, but avoid full Guaranteed to dodge
container-OOM-on-own-limit) on master/volume/filer/s3 via the Seaweed CR
(`spec.{master,volume,filer,s3}.requests`/`.limits` — flat siblings, not under
`resources:`). This is the only lever that touches the **involuntary** axes: it moves
pods out of BestEffort (kernel `oom_score_adj` drops, so they're not the first OOM
victim) and below their memory request (kubelet node-pressure eviction ranks
over-request pods first), and it deprioritizes them in the descheduler's QoS tiebreak.
Same gap exists on every CNPG cluster — worth a cluster-wide convention.

### B. PodDisruptionBudget for the raft/quorum sets (the real descheduler lever) — IMPLEMENTED

The descheduler honors PDBs unconditionally. SeaweedFS master is a 3-node raft set →
add a hand-written PDB `minAvailable: 2` (the operator CRD has **no** PDB field; select
`app.kubernetes.io/{name=seaweedfs,component=master}`). Volume servers similar. The
filer is a singleton — a `maxUnavailable: 0` PDB would **wedge node drains/upgrades**,
so protect it via A+C instead and accept brief downtime on planned drains. This mirrors
the house CNPG auto-PDB pattern.

### C. Custom (non-system) PriorityClass on infra — IMPLEMENTED

A class value ~1,000,000 (no `globalDefault`, below `system-cluster-critical`) makes the
pods last in the descheduler's primary priority sort, defers them under node-pressure,
and shields them from scheduler preemption. Do **not** reuse `system-*-critical`.

### D. Stop the over-utilization at the source

The node flips over-utilized largely because of runaway **BestEffort** CPU (the props
grader matchability crash-loop — fixed in PR #2395). Removing it keeps `ovh-ns104952`
under the 50% target on most runs, so the descheduler finds nothing to evict. A
contributing trigger, not the structural cause — but the two incidents are linked
through this node's CPU.

### E. Make SeaweedFS survive a transient self-FQDN miss (defense in depth — independent of the eviction fix)

The operator hardcodes `-ip=<FQDN>`, so we cannot switch to `-ip=$POD_IP` through the
CR. Inject an initContainer that blocks until the self-FQDN resolves, via
`spec.master/volume/filer.sidecars` (the CRD exposes `sidecars`; confirm it accepts
initContainers in the operator version, else fall back to a pod-template patch through
a kustomize `patches` on the operator-rendered StatefulSet):

```yaml
# initContainer pseudo-spec
- name: wait-self-dns
  image: busybox:1.37
  command: ["sh", "-c", "until nslookup $(POD_NAME).seaweedfs-master-peer.seaweedfs; do sleep 1; done"]
  env: [{ name: POD_NAME, valueFrom: { fieldRef: { fieldPath: metadata.name } } }]
```

This makes A. less load-bearing: even a future eviction or node-drain restart can't
FATAL the pod.

### F. Underlying DNS path (low priority — already healthy)

No action required now; CoreDNS is healthy. If the June-8-style apiserver-watch blip
recurs, cycle the affected CoreDNS replica (`kubectl -n kube-system delete pod
coredns-…`) to force a fresh apiserver watch. Consider deploying NodeLocal DNSCache
only if upstream/apiserver DNS flakiness becomes recurrent.

### G. Langfuse ingestion retry hardening (optional)

Each master crash forces a brief raft re-election during which S3 reads abort
(`reader_at.go … context canceled`, langfuse `Failed to download file from S3 …
aborted`). Ingestion is currently healthy. Fixing A.+B. removes the crash windows;
no langfuse change is strictly required. If desired, raise langfuse ingestion
worker retry/backoff so a single S3 abort doesn't dead-letter the trace.
