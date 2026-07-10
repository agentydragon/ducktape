# Forgejo git write latency — attribution bench (2026-07-10)

Status: measured 2026-07-10, post the PVC move to `seaweedfs-ovh-ssd`. Companion to
<sqlite_storage_bench/seaweedfs_latency_forensics.md>, which established the mechanism
(per-operation FUSE→filer→volume round-trips dominate; disk class barely matters).
This note extends the result to git workloads and to Forgejo end-to-end, answering
"is it Forgejo being slow or SeaweedFS being slow?"

## Question

haku-ui feedback writes (single small file via Forgejo contents API) take seconds and
sometimes fail. Forgejo's git data (`forgejo/forgejo-git-rwx-ssd`, RWX 50Gi) sits on
`seaweedfs-ovh-ssd`; its DB is on `local-path-ovh-ssd`. Attribute the latency:
transport vs Forgejo app vs storage.

## Method

1. End-to-end from a Claude Code web container (egress via agent proxy → public
   gateway): timed `/api/v1/version` (floor), contents-API reads, contents-API writes,
   and `git push` of tiny commits against a fresh throwaway private repo
   (`haku/bench-scratch-2026-07-10`, created for this bench, deleted after).
2. Storage A/B: identical git-shaped workload in `haku-sandbox` Jobs on the SAME node
   (`ovh-ns104952`), one on a `local-path-ovh-ssd` PVC, one on a `seaweedfs-ovh-ssd`
   PVC. Workload: 200 × 4KB `dd conv=fsync` files; 50 tiny `git commit`s; local
   `git clone`. Image `alpine/git`; timing via `/proc/uptime` (BusyBox `date` has no
   `%N` — first attempt produced all-zero timings).

## Results

End-to-end (fresh empty repo, so no repo-size or CI confounders):

| Operation                       | Time (n samples)            | Increment over floor |
| ------------------------------- | --------------------------- | -------------------- |
| `/api/v1/version` (floor)       | ~700ms (3)                  | —                    |
| contents API read               | 0.8–1.6s (3)                | +0.1–0.9s            |
| `git push`, tiny commit         | 2.1–3.4s, median ~2.3s (8)  | **+~1.6s**           |
| contents API write (small file) | 3.3–5.8s, median ~4.4s (12) | **+~3.7s**           |

Floor includes the web container's agent-proxy hop; in-cluster floor will be lower.

Storage A/B, same node, same workload:

| Phase                 | local-path-ovh-ssd | seaweedfs-ovh-ssd     | Slowdown |
| --------------------- | ------------------ | --------------------- | -------- |
| 200 × 4KB fsync files | 340ms (1.7ms/op)   | 5,360ms (27ms/op)     | **16×**  |
| 50 git commits        | 870ms (17ms each)  | 26,650ms (533ms each) | **31×**  |
| local `git clone`     | 20ms               | 5,090ms               | **254×** |

## Attribution

- A plain `git commit` costs **533ms on seaweedfs-ovh-ssd vs 17ms on local SSD**.
  Forgejo's contents-API write internally performs several commits' worth of git
  object/ref/lockfile operations → the observed ~3.7s over floor is storage
  round-trips, not Forgejo app time. `git push` (receive-pack, fewer ops) costing
  ~2s less than the API path is consistent.
- Moving the PVC to the SSD storage class did not and cannot fix this: the cost is
  per-operation network/FUSE round-trips (see the SQLite forensics' link RTT tables),
  and git is a many-tiny-ops workload. Nothing is misconfigured — this is the known
  reason production forges keep repos on node-local disk with application-level
  replication (GitHub Spokes, GitLab Gitaly Cluster; both explicitly moved off
  network filesystems). No mainstream forge stores git objects in S3/Postgres
  (JGit's DFS backend, used by Google's Gerrit, is the exception and a JVM stack).

## Incidental findings

- **CSI scheduling landmine**: `seaweedfs-csi-driver-mount` runs only on the five
  `ovh-*` nodes. A pod with a seaweedfs PVC that schedules elsewhere (hit: `wyrm2`)
  wedges in `ContainerCreating` with `CSINode ... does not contain driver` — no
  fail-fast. Consider adding a matching nodeAffinity via the storage class's
  `allowedTopologies` or documenting the constraint.
- Volume attach on a CSI-capable node still took >60s.
- BusyBox `date +%s%N` silently yields seconds-only — bench scripts in alpine images
  need `/proc/uptime` or coreutils.

## Recommendations (ranked)

1. **Move `forgejo-git-rwx-ssd` to `local-path-ovh-ssd`** (RWO; Forgejo is
   single-replica anyway). Expected: contents-API writes drop from ~4.4s to ~1s
   (floor + app). Replication story moves to the application layer: scheduled
   `git bundle`/mirror to object storage or a second Forgejo, mirroring how the
   SQLite forensics resolved (node-local hot + async replicated cold).
2. **Independently of storage: take feedback writes off the synchronous path** —
   haku-ui write-behind (local clone + instant ack + async push + sync badge), and
   path-filter `responses/**` out of full CI. Designed in haku-state
   `plans/feedback-write-latency.md`.
3. SeaweedFS remains fine for what it's good at — blob/object workloads via its S3
   gateway (media uploads, bundles, LFS backend). The anti-pattern is specifically
   POSIX-FUSE-mounting it under many-tiny-ops workloads (git, SQLite).

## Repro

```bash
# A/B jobs (pin to a CSI-capable node!): see haku-sandbox Jobs gitbench2-local /
# gitbench3-seaweed in kubectl history, or re-create: 200x dd conv=fsync + 50 git
# commits + clone on a PVC of each class, alpine/git, /proc/uptime timing.
# End-to-end: fresh private repo via API, then timed contents-API PUTs and git pushes.
```
