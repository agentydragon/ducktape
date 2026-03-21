# SRE Best Practices Review: Cluster Assessment

**Date**: 2026-02-14
**Scope**: Full comparison of current cluster architecture against modern Kubernetes best practices

---

## Executive Summary

The cluster is well-architected for a personal/small-team infrastructure project. The
three-layer Terraform bootstrap, Flux GitOps DAG with 83 kustomizations, Vault-backed
secret management, and comprehensive validation tooling are all aligned with professional
SRE practices. The documentation is thorough and the lessons-learned corpus is valuable.

However, several areas need attention — one urgently. The sections below are ordered by
priority.

---

## ~~1. URGENT: Ingress-NGINX Retirement (March 2026)~~ — DONE

**Status**: Completed 2026-02-16. Migrated to Cilium Gateway API. See changelog.

---

## 2. Terraform State: Single Point of Failure

**Risk**: High
**Current state**: `persistent-auth/terraform.tfstate` is local-only, no backup

This file is the SSOT for the sealed-secrets keypair, Proxmox CSI tokens, Nix signing
key, and Flux deploy key. Losing it means:

- All SealedSecrets in git become undecryptable (5 sealed secrets)
- CSI storage tokens desynchronize with Proxmox
- Full secret re-generation and git commit churn required

### Current Best Practice

Remote state with encryption, versioning, and locking. For OpenTofu (which the cluster
uses), the options ranked by fit:

| Option                                   | Complexity | Cost                | Fit                              |
| ---------------------------------------- | ---------- | ------------------- | -------------------------------- |
| **S3 + native locking** (OpenTofu 1.11+) | Low        | ~$0.50/mo           | Best for infrastructure state    |
| **Encrypted git (git-crypt/SOPS)**       | Low        | Free                | Acceptable for small state files |
| **rclone to cloud storage**              | Low        | Free-$2/mo          | Simple backup, no locking        |
| **HCP Terraform / Spacelift**            | Medium     | Free tier available | Overkill for personal infra      |

### Recommendation

**Minimum viable**: rclone cron job pushing encrypted state to Google Drive or S3.
**Better**: Migrate to S3 backend with `use_lockfile = true` (OpenTofu native locking,
no DynamoDB needed). Enable S3 versioning for automatic rollback.

OpenTofu now supports [native state encryption](https://opentofu.org/docs/language/state/encryption/)
— encrypt at rest without relying on the storage layer. This is an OpenTofu-exclusive
feature not available in Terraform.

**Note**: The infrastructure and flux layers are ephemeral (destroyed per bootstrap
cycle), so remote state matters less for them. `persistent-auth` is the critical one.

---

## 3. Backup and Disaster Recovery: None Exists

**Risk**: High
**Current state**: No etcd backups, no PVC backups, no Velero

### What Needs Backup

| Data                                 | Current Backup  | Risk                                |
| ------------------------------------ | --------------- | ----------------------------------- |
| etcd (cluster state)                 | None            | Full cluster loss on 2/3 CP failure |
| Terraform state (persistent-auth)    | None            | Secret re-generation                |
| PVCs (Harbor, Gitea, Loki, Postgres) | None            | Data loss                           |
| Git repo                             | GitHub + GitLab | Adequate                            |

### Recommendations

**etcd snapshots**: Deploy [talos-backup](https://github.com/siderolabs/talos-backup)
(official Siderolabs tool). Runs as a CronJob, takes etcd snapshots via Talos API,
encrypts with `age`, pushes to S3-compatible storage.

**Application data (PVCs)**: Deploy [Velero](https://velero.io/) with scheduled backups:

```yaml
# Critical: hourly (vault, authentik)
# Standard: daily (gitea, harbor, loki)
# Retention: 7 days hourly, 30 days daily
```

Velero integrates with both Proxmox CSI (via CSI snapshots) and Hetzner CSI.

**Backup testing**: Backups that have never been restored are hopes, not backups.
Schedule quarterly restore drills. A `scripts/test-restore.sh` that validates backup
integrity would be valuable.

---

## 4. Network Policies: Currently Open

**Risk**: Medium
**Current state**: No Cilium NetworkPolicies. All pods can communicate freely.

### Best Practice: Default-Deny with Identity-Based Policies

Every namespace should have a default-deny policy, with explicit allow rules for
required traffic. Cilium supports this natively with `CiliumNetworkPolicy` CRDs
that operate on workload identity rather than IP addresses.

### Implementation Path

1. **Enable Hubble** (already deployed at `hubble.allegedly.works`) to observe current
   traffic flows
2. **Generate baseline policies** from observed traffic using `hubble observe`
3. **Deploy default-deny** in audit mode first (Cilium supports policy audit mode)
4. **Enforce** after validating no legitimate traffic is blocked

### Key Policies to Create

| Policy           | Scope                        | Purpose             |
| ---------------- | ---------------------------- | ------------------- |
| Default deny all | Every namespace              | Zero-trust baseline |
| Allow DNS        | All pods → kube-dns          | CoreDNS resolution  |
| Allow ingress    | Cilium gateway → backend pods | HTTP routing       |
| Allow Vault      | ESO → Vault                  | Secret sync         |
| Allow monitoring | Prometheus → all pods        | Metric scraping     |
| Allow Authentik  | Apps → Authentik             | SSO/forward-auth    |

---

## 5. Observability Gaps

**Risk**: Medium
**Current state**: Prometheus + Grafana + Loki deployed. ntfy.sh notifications configured.

### What's Good

- kube-prometheus-stack deployed with Grafana SSO
- Loki for log aggregation on Proxmox storage
- ntfy.sh webhook for Flux reconciliation alerts
- Hubble UI for Cilium network observability

### What's Missing

#### 5a. Structured Alerting Pipeline

The ntfy.sh integration exists but lacks a proper alerting chain. Best practice:

```text
Prometheus → Alertmanager → ntfy-alertmanager bridge → ntfy.sh → phone
```

Deploy [alertmanager-ntfy](https://github.com/alexbakker/alertmanager-ntfy) or
[ntfy-alertmanager](https://hub.xenrox.net/~xenrox/ntfy-alertmanager/) as a bridge.
Supports priority levels, action buttons (create silence, open Prometheus), and
severity-based routing.

#### 5b. SLO-Based Alerting

Currently no SLO/SLI definitions. Adopt [Pyrra](https://github.com/pyrra-dev/pyrra)
or [Sloth](https://github.com/slok/sloth) to define SLOs declaratively. These tools
auto-generate multi-window, multi-burn-rate alerts (Google SRE methodology) from simple
SLO definitions.

Start with:

- **Ingress availability**: 99.5% of requests return non-5xx (7d window)
- **DNS availability**: 99.9% of DNS queries answered (7d window)
- **Vault availability**: 99.9% of secret reads succeed (7d window)

#### 5c. Golden Signals Dashboards

Every service should have a dashboard showing the [four golden signals](https://sre.google/sre-book/monitoring-distributed-systems/):
latency, traffic, errors, saturation. Import Grafana dashboard
[#21073](https://grafana.com/grafana/dashboards/21073-monitoring-golden-signals/) as a
starting point.

#### 5d. Distributed Tracing (Lower Priority)

Consider [Grafana Tempo](https://grafana.com/oss/tempo/) for distributed tracing —
natural fit with the existing Grafana + Loki stack. Object-storage backed, cheaper than
Jaeger. Enables correlated logs-to-traces via trace IDs.

#### 5e. Dependency Visualization

Deploy [Capacitor](https://fluxcd.io/blog/2024/02/introducing-capacitor/) — the official
Flux GUI. Provides a reconciliation graph showing how Flux objects relate and a dependency
view showing the `dependsOn` DAG. Lightweight single-pod deployment.

---

## 6. Security Hardening

### 6a. Pod Security Standards

**Current state**: No Pod Security Standards enforcement.

Apply namespace labels to enforce the [Restricted profile](https://kubernetes.io/docs/concepts/security/pod-security-standards/)
on all application namespaces:

```yaml
metadata:
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

System namespaces (`kube-system`, `csi-proxmox`, `cilium`) need `privileged`.
Start with `warn` mode on application namespaces, then promote to `enforce`.

### 6b. Kyverno: Audit → Enforce

The cluster has Kyverno deployed in Audit mode with a `require-gitops` ClusterPolicy
(already noted in `plan.md`). Switch to `Enforce` after validation. This prevents
manual `kubectl apply` of resources that should be GitOps-managed.

### 6c. Image Supply Chain

No image signing or verification exists. The modern stack:

1. **Sign images in CI** with [cosign](https://github.com/sigstore/cosign) (keyless via
   GitHub Actions OIDC)
2. **Verify in admission** with Kyverno `verifyImages` policies
3. **SBOM generation** with Trivy/Syft, attached to images in OCI registry

For the cluster's purposes, enforcing that all images come from known registries
(`ghcr.io`, `docker.io`, `registry.allegedly.works`) via Kyverno is a pragmatic
first step.

### 6d. Runtime Security (Lower Priority)

Consider [Tetragon](https://tetragon.io/) — Cilium's eBPF-based runtime security tool.
Can detect and block suspicious syscalls, file access, and network connections at the
kernel level. Natural complement to the existing Cilium stack.

### 6e. API Server Access Restriction

The Kubernetes API (port 6443) is currently open to `0.0.0.0/0` in Hetzner firewall
rules (noted in `plan.md` as TODO). Restrict to:

- Admin IPs (Nebula mesh)
- Inter-node CIDRs
- CI runner IPs (if any)

---

## 7. Resource Management

**Current state**: Most deployments have `requests` and `limits` set (good). No
`ResourceQuota` or `LimitRange` on namespaces.

### Resource Management Recommendations

- **LimitRange per namespace**: Set default requests/limits so pods without explicit
  resource specs don't consume unbounded resources
- **ResourceQuota per namespace**: Prevent any single namespace from consuming the
  entire cluster
- **VPA in recommendation mode**: Deploy the Vertical Pod Autoscaler or
  [Goldilocks](https://github.com/FairwindsOps/goldilocks) to get right-sizing
  recommendations. Given fixed-cost infrastructure (Hetzner VPS + Proxmox), this is
  more about contention prevention than cost optimization.

---

## 8. Operational Practices

### 8a. Runbooks

Alerts should link to runbooks. The `docs/troubleshooting.md` is good but could be
structured as individual runbook files (one per alert/issue) with standardized sections:

```markdown
## Alert: <name>

### Severity: <level>

### Symptoms

### Diagnosis Steps

### Resolution

### Prevention
```

### 8b. Incident Post-Mortems

The `lessons_learned/` directory serves this purpose well (7 entries). Formalize with a
template: timeline, root cause, detection gap, fix, prevention. Already mostly there.

### 8c. Change Management

Currently: edit → commit → push → Flux reconciles. No review gate.

Consider:

- **Branch protection** on `devel` with required PR reviews for infrastructure changes
- **Flux `Receiver`** + GitHub webhook for instant reconciliation on push (currently
  polling at 1-minute intervals, noted as TODO in `plan.md`)
- **Flagger** for progressive delivery of application changes (canary analysis with
  Prometheus metrics, auto-rollback on error rate spikes). Natural fit with the Flux
  ecosystem.

---

## 9. tofu-controller: Known Operational Fragility

**Current state**: 62 Terraform modules managed by tofu-controller in-cluster. Known
issues include the TLS secret cache desync bug (startup GC deletes all secrets),
runner pod crashes causing state loss, and Authentik token overwrites from state
regeneration.

### Assessment

tofu-controller is the weakest link in the operational chain. The three documented
bugs (TLS cache desync, Authentik token overwrite, runner crashes) all stem from the
same architectural issue: running Terraform as ephemeral pods with in-cluster state
is fragile.

### Alternatives to Evaluate

| Tool                              | Architecture                               | Trade-off                               |
| --------------------------------- | ------------------------------------------ | --------------------------------------- |
| **Crossplane**                    | K8s-native CRDs, continuous reconciliation | Significant rework, fewer providers     |
| **Atlantis**                      | PR-based plan/apply                        | External server, not GitOps-native      |
| **Direct Terraform in bootstrap** | Move gitops modules into bootstrap layers  | Simpler but not continuously reconciled |
| **Keep tofu-controller**          | Status quo                                 | Known bugs, but documented workarounds  |

### Tofu-Controller Recommendation

For the current scale (62 modules, single operator), the pragmatic path is:

1. **Short-term**: Keep tofu-controller with documented workarounds. Add `cas = 0`
   (check-and-set) on all write-once `vault_kv_secret_v2` resources to prevent
   silent overwrites.
2. **Medium-term**: Evaluate Crossplane for new modules. Crossplane Compositions
   could replace the SSO blueprint pattern with continuous reconciliation and no
   state file management.
3. **Long-term**: Migrate tofu-controller modules to Crossplane as the provider
   ecosystem matures (Authentik, PowerDNS, Vault providers exist).

---

## 10. Documentation and Process

### What's Excellent

- **Layered documentation** (bootstrap.md, operations.md, troubleshooting.md, plan.md,
  secrets.md) with clear separation of concerns
- **Lessons learned** corpus with root cause analysis
- **AGENTS.md** with detailed agent instructions, anti-patterns, and debugging processes
- **Validation tooling** (4 Python scripts, pre-commit hooks, Bazel integration)
- **Comprehensive troubleshooting** with diagnosis commands and known issues

### What Could Improve

- **Runbook-per-alert structure** (see 8a above)
- **Architecture decision records (ADRs)**: The `plan.md` has some architectural
  decisions inline. Consider extracting to numbered ADR files (`docs/adr/`) for
  easier reference and historical tracking
- **Dependency graph visualization**: Generate and commit a Mermaid diagram of the
  Flux kustomization DAG. Update automatically in CI or via a script.
- **Bootstrap timing documentation**: The timing reference table in AGENTS.md is
  valuable. Consider adding expected timing to bootstrap.py output so operators
  know when to worry.

---

## Summary: Prioritized Action Items

### P0 — Do Now (Blocking/Urgent)

| #   | Item                                                  | Risk                                | Effort |
| --- | ----------------------------------------------------- | ----------------------------------- | ------ |
| 1   | ~~**Migrate off ingress-nginx**~~ (DONE 2026-02-16)   | ~~Service disruption~~              | ~~Done~~ |
| 2   | **Back up persistent-auth tofu state**                | Unrecoverable secret loss           | Small  |

### P1 — Do Soon (High Value)

| #   | Item                                                 | Risk                           | Effort |
| --- | ---------------------------------------------------- | ------------------------------ | ------ |
| 3   | **Deploy etcd backup** (talos-backup CronJob)        | Cluster state loss             | Small  |
| 4   | **Default-deny network policies**                    | Lateral movement in compromise | Medium |
| 5   | **Pod Security Standards** on application namespaces | Container escape risk          | Small  |
| 6   | **Restrict API server access** in Hetzner firewall   | Unauthorized cluster access    | Small  |
| 7   | **Alertmanager → ntfy bridge**                       | Silent failures                | Small  |

### P2 — Do When Convenient (Good Practice)

| #   | Item                                                 | Risk                          | Effort |
| --- | ---------------------------------------------------- | ----------------------------- | ------ |
| 8   | **Kyverno Audit → Enforce**                          | Configuration drift           | Small  |
| 9   | **SLO definitions** (Pyrra/Sloth)                    | Alert fatigue / missed issues | Medium |
| 10  | **Velero for PVC backup**                            | Application data loss         | Medium |
| 11  | **ResourceQuota + LimitRange** per namespace         | Resource contention           | Small  |
| 12  | **Golden Signals dashboards**                        | Visibility gaps               | Small  |
| 13  | ~~**Flux webhook receiver**~~ (DONE)                 | ~~Reconciliation delay~~      | ~~Done~~ |
| 14  | **Image registry allowlist** via Kyverno             | Supply chain risk             | Small  |

### P3 — Future Consideration

| #   | Item                                                  | Benefit                 | Effort |
| --- | ----------------------------------------------------- | ----------------------- | ------ |
| 15  | Crossplane evaluation for tofu-controller replacement | Operational stability   | Large  |
| 16  | Grafana Tempo for distributed tracing                 | Debug velocity          | Medium |
| 17  | Capacitor for Flux DAG visualization                  | Operational visibility  | Small  |
| 18  | Flagger for progressive delivery                      | Safer deployments       | Medium |
| 19  | Tetragon for runtime security                         | Kernel-level protection | Medium |
| 20  | cosign image signing + verification                   | Supply chain integrity  | Medium |

---

## Sources

### Ingress/Gateway API

- [Ingress NGINX Retirement (kubernetes.io)](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/)
- [Cilium Gateway API Docs](https://docs.cilium.io/en/stable/network/servicemesh/gateway-api/gateway-api/)
- [Gateway API vs Ingress (Kong)](https://konghq.com/blog/engineering/gateway-api-vs-ingress)

### State Management

- [S3-Native State Locking (no DynamoDB)](https://medium.com/aws-specialists/dynamodb-not-needed-for-terraform-state-locking-in-s3-anymore-29a8054fc0e9)
- [OpenTofu State Encryption](https://opentofu.org/docs/language/state/encryption/)
- [Terraform Backend Configuration Guide 2025 (Scalr)](https://scalr.com/learning-center/terraform-backend-configuration-guide-choosing-the-right-state-management-solution/)

### Backup and DR

- [talos-backup (Siderolabs)](https://github.com/siderolabs/talos-backup)
- [Velero](https://velero.io/)
- [Talos Disaster Recovery Docs](https://docs.siderolabs.com/talos/v1.9/build-and-extend-talos/cluster-operations-and-maintenance/disaster-recovery)

### Observability

- [Pyrra (SLO management)](https://github.com/pyrra-dev/pyrra)
- [alertmanager-ntfy](https://github.com/alexbakker/alertmanager-ntfy)
- [Capacitor (Flux GUI)](https://fluxcd.io/blog/2024/02/introducing-capacitor/)
- [Grafana Tempo](https://grafana.com/oss/tempo/)
- [dotdc/grafana-dashboards-kubernetes](https://github.com/dotdc/grafana-dashboards-kubernetes)

### Security

- [Pod Security Standards (kubernetes.io)](https://kubernetes.io/docs/concepts/security/pod-security-standards/)
- [Cilium Zero Trust Networking](https://cilium.io/outcomes/zero-trust/)
- [Tetragon](https://tetragon.io/)
- [cosign/Sigstore](https://github.com/sigstore/cosign)

### GitOps

- [Flux Repository Structure Guide](https://fluxcd.io/flux/guides/repository-structure/)
- [Flagger (Progressive Delivery)](https://github.com/fluxcd/flagger)
- [ArgoCD vs Flux 2025](https://aws.plainenglish.io/argocd-vs-flux-in-2025-the-gitops-war-is-over-and-you-won-d22e084929a5)

### Secret Management

- [ESO + Vault Best Practices](https://external-secrets.io/latest/provider/hashicorp-vault/)
- [Kubernetes Secrets Management 2025 (Atmosly)](https://atmosly.com/blog/kubernetes-secrets-management-vault-vs-sealed-secrets-vs-external-secrets-2025/)

### IaC Alternatives

- [Crossplane vs Terraform (Spacelift)](https://spacelift.io/blog/crossplane-vs-terraform)
- [Flux tofu-controller](https://github.com/flux-iac/tofu-controller)
