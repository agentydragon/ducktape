---
name: cluster_health
description: Scan cluster health — Flux kustomizations, pod status, recurring crashes, node conditions, active Alertmanager alerts, CNPG databases, certificate expiry — and output an actionable summary with fix plan. Use when user asks "how's the cluster", "cluster health", "what's broken", "check the cluster", or similar.
---

# Cluster Health Check

Comprehensive cluster health scan. Collect results from all checks, then produce a
single structured report with an actionable fix plan.

Use the `haku-console` MCP server's connected Kubernetes passthrough tools for
read queries. Haku routes these through the operator-linked Kubernetes MCP
server and its approval policy; do not assume client-side MCP auto-approval
bypasses that boundary.

Fall back to `Bash(kubectl ...)` with `dangerouslyDisableSandbox: true` only for
operations the SA cannot do (e.g., writing resources outside `claude-sandbox`,
reading pod logs in namespaces without explicit rolebindings).

## What to Check

Run checks in parallel where possible.

### Flux GitOps

- All Flux Kustomizations — ready status, suspended, stalled
- HelmReleases — ready status, failed upgrades
- Terraform resources (tofu-controller) — ready and applied status
- Identify suspended kustomizations and cross-reference with `cluster/docs/decisions.md`
  "Suspended Kustomizations" to distinguish expected vs unexpected suspensions

### Alertmanager & Prometheus Alerts

- Alertmanager deployment health — Alertmanager CRs, pods, endpoints, and API
  status in `monitoring`
- Active firing Alertmanager alerts from the live API, not just PrometheusRule
  definitions. Query Alertmanager API v2 for active alerts and preserve the
  identifying labels (`alertname`, `severity`, `namespace`, `pod`, `job`,
  `service`), `startsAt`, `generatorURL`, and annotations.
- Separately note silenced or inhibited alerts if they explain why a firing
  condition is not paging. Do not treat silenced maintenance alerts as healthy
  without checking the silence reason, creator, matcher, and expiry.

Prefer the connected Haku Kubernetes tools for Kubernetes object reads. For the live Alertmanager API,
use the least-invasive available path: Kubernetes service proxy if exposed by the
MCP server, otherwise the `Bash(kubectl ...)` escape hatch outside the sandbox for
a short `kubectl port-forward` or an ephemeral curl pod in `claude-sandbox`.
The in-cluster service is typically
`http://monitoring-alertmanager.monitoring.svc.cluster.local:9093`; query
`/api/v2/status`, `/api/v2/alerts?active=true`, and `/api/v2/silences`.

### Pod & Workload Health

- Non-running pods (Pending, CrashLoopBackOff, ImagePullBackOff, Error, etc.)
- Flapping pods — containers with restart counts >3 **where restarts are recent**
- Recent OOMKill and Eviction events
- Failed jobs

**Important**: When checking restart counts, always check if restarts are _recent_.
The `kubectl get pods` output shows restart count AND time since last restart, e.g.
`1256 (41h ago)`. A high total count accumulated over many days means nothing if
the last restart was days ago. Focus on pods where restarts are happening _now_
(last restart within the last hour or so). Check the pod's AGE and the "(Xh ago)"
suffix to determine recency.

### Node Health

- Node conditions: Ready, MemoryPressure, DiskPressure, PIDPressure
- Resource usage (`kubectl top nodes`)
- **If any node is under pressure or >80% memory/disk usage**: break down the top
  pod consumers on that node to identify what's causing the pressure
- Active taints on all nodes (cordoned, NoSchedule, NoExecute, etc.)

### Databases & Certificates

- CNPG cluster health — instance count vs ready count, phase
- cert-manager certificates — ready status, approaching expiry
- Stuck ACME challenges

### Warning Events

- Recent Warning-type events, focusing on recurring patterns (high event counts)

## Investigation

When any check reveals an error (unhealthy CNPG cluster, CrashLoopBackOff pod,
failed HelmRelease, stuck Terraform, etc.), don't just report the status — dig into
the cause before moving on:

- Pod failures: check logs, previous container logs (`--previous`), describe output
  (events, conditions, scheduling failures)
- CNPG issues: check operator logs in `cnpg-system`, individual instance logs, cluster
  events
- Flux/Helm failures: check the controller logs, the kustomization/helmrelease events
- Alertmanager alerts: trace each firing alert to the owning PrometheusRule and
  underlying Kubernetes object; use `generatorURL` or Prometheus/Mimir queries
  when needed, then classify it as active breakage, stale/wedged alert state,
  expected maintenance, or already-resolved-but-not-yet-cleared
- Node problems: check node describe, kubelet conditions, recent events on the node
- Image pull failures: check if the registry (Harbor) is up, if the image tag exists,
  if pull secrets are configured

Include the root cause (or best hypothesis) in the report, not just "pod is failing."

### Image Pull Failures

When images fail to pull, trace the full pipeline:

- Check if the image exists in Harbor (`registry.allegedly.works`)
- Check if the CI build that produces the image succeeded — look at GitHub Actions
  workflows (`gh run list`), BuildBuddy invocations, or the relevant `buildbuddy.yaml`
  / `.github/workflows/` pipeline
- Check if the image push step succeeded (GitHub Actions logs, Harbor push events)
- Report where the pipeline broke (build failed, push failed, tag missing, auth issue)

### Anomaly Detection

Flag anything that looks out of place:

- Pods running in namespaces that should be empty (suspended services with leftover
  workloads not cleaned up)
- Stale errored/completed objects: failed Jobs not cleaned up, old ReplicaSets with
  no pods, orphaned PVCs for deleted workloads
- Resources that exist in the cluster but have no corresponding Flux kustomization
  managing them (drift from GitOps)
- Pods/deployments with no owner (not managed by a Deployment/StatefulSet/DaemonSet/Job)

## Report Format

After collecting all data, produce:

```markdown
# Cluster Health Report — <date>

## Summary

<one-line assessment: healthy / degraded / critical>
<count of issues by severity>

## Critical Issues

<broken kustomizations, CrashLoopBackOff pods, NotReady nodes, unhealthy CNPG
clusters, active high-severity Alertmanager alerts, expired certificates — each
with: what, impact, evidence, fix>

## Warnings

<high restart counts, unexpected suspensions,
active low-severity Alertmanager alerts, resource pressure approaching limits>

## Expected / Known

<intentional suspensions per plan.md, scaled-to-zero deployments, offline
roaming nodes>

## Fix Plan

<ordered actions to resolve critical + warnings, prioritized by impact and
dependency order>
```

## Cross-Reference

Check findings against:

- `cluster/docs/decisions.md` "Suspended Kustomizations" — don't flag expected suspensions
- `cluster/docs/plan.md` "Next Actions" — note if findings match known TODOs
- `cluster/docs/troubleshooting.md` — reference known fix procedures for matching symptoms
