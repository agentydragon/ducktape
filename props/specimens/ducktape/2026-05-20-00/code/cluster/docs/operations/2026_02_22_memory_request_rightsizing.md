# Memory Request Right-Sizing (2026-02-22)

## Context

Cluster nodes were hitting scheduling limits despite low actual memory usage. The
scheduler refuses to place pods when the sum of memory **requests** exceeds node
allocatable, even if actual consumption is well below capacity.

Analysis of `kubectl top pods` vs configured requests revealed widespread
over-provisioning: many pods requested 5-50x their actual usage, wasting ~4GB of
scheduling budget across the cluster.

## Methodology

Compared actual memory usage (`kubectl top pods -A`) against configured requests for
all pods. Right-sized requests to ~1.5-2x observed usage, preserving headroom for
spikes while reclaiming wasted scheduling budget. Limits were left unchanged (they
only matter at OOM-kill time).

## Observed Usage vs New Requests

| Component            | Actual Usage   | Old Request     | New Request | Savings |
| -------------------- | -------------- | --------------- | ----------- | ------- |
| openclaw (main)      | 589Mi          | 1Gi             | 768Mi       | 256Mi   |
| openclaw (chromium)  | included above | 512Mi           | 256Mi       | 256Mi   |
| vault (x3 instances) | 66-329Mi       | 256Mi each      | 128Mi each  | 384Mi   |
| harbor-trivy         | 4Mi            | 512Mi           | 64Mi        | 448Mi   |
| harbor-portal        | 6Mi            | 256Mi           | 32Mi        | 224Mi   |
| harbor-redis         | 16Mi           | 256Mi           | 64Mi        | 192Mi   |
| harbor-jobservice    | 22Mi           | 256Mi           | 64Mi        | 192Mi   |
| harbor-core          | 55Mi           | 256Mi           | 128Mi       | 128Mi   |
| harbor-database      | 59Mi           | 256Mi           | 128Mi       | 128Mi   |
| attic (nix cache)    | 2Mi            | 256Mi           | 32Mi        | 224Mi   |
| matrix-synapse-pg    | 52Mi           | 256Mi (default) | 128Mi       | 128Mi   |
| props-postgresql     | 53Mi           | 256Mi (default) | 128Mi       | 128Mi   |

**Total reclaimed: ~2.7GB of request budget.**

## Outpost Consolidation (2026-02-23)

Consolidated 9 single-replica Authentik proxy outposts into 1 shared outpost with 2
replicas. Outpost pods have no memory requests configured (Authentik default), so
request budget is unchanged, but actual memory footprint decreased:

| Before                                       | After                                                |
| -------------------------------------------- | ---------------------------------------------------- |
| 9 outpost pods (~10-18Mi each, ~112Mi total) | 2 shared outpost pods (~30Mi each est., ~60Mi total) |

Net saving: ~50Mi actual memory and 7 fewer pods (reduced scheduling overhead, API server
watch connections, and kubelet bookkeeping). Primary benefit is HA — rolling restarts keep
at least 1 replica serving.

## Future Work

Consider deploying [Kubernetes VPA](https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler)
in `Off` (recommendation-only) mode to continuously monitor and suggest right-sized
requests, replacing manual analysis. See `docs/plan.md` for tracking.
