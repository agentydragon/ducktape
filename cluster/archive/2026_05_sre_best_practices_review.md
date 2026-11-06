# SRE Best Practices Review: Cluster Assessment

**Review date:** 2026-02-14
**Scope:** Cluster architecture compared with Kubernetes/SRE practices at the time.

> **Historical note (2026-04-19):** Vault has since been decommissioned; see
> <../docs/decisions.md> § "Secrets: SOPS SSOT". Vault references and the Vault SLO
> below describe the pre-migration architecture and no longer apply.

## Post-review status

- Ingress-NGINX retirement was completed 2026-02-16; traffic moved to Cilium Gateway API.
- An undated archived update records the Terraform roots consolidated under
  `cluster/terraform/main/` with a PostgreSQL backend (`tofu-state-db`, schema `main`, two
  replicas on VPS `local-path`). It records a CronJob writing `pg_dump` to
  `proxmox-csi-retain` every six hours and `prevent_destroy` on persistent-auth resources.
- The backup reduced routine risk, but both state and its backup remained in-cluster. If
  both the cluster and backup PVC were lost, state would need reconstruction. The review
  proposed an additional off-cluster encrypted copy (for example, rclone to cloud storage
  or S3 with [OpenTofu state encryption](https://opentofu.org/docs/language/state/encryption/)).
- The Flux webhook receiver was later marked done in the action ledger; no completion date
  was recorded there.

## Prioritized action ledger

### P0 — Do now (blocking/urgent)

| #   | Action                                                              | Risk                      | Effort   | Status                                                  |
| --- | ------------------------------------------------------------------- | ------------------------- | -------- | ------------------------------------------------------- |
| 1   | ~~Migrate off ingress-nginx~~                                       | ~~Service disruption~~    | ~~Done~~ | Done 2026-02-16                                         |
| 2   | Back up persistent-auth tofu state (PG backend + `pg_dump` CronJob) | Unrecoverable secret loss | Done     | Done; date not recorded. Off-cluster copy remained open |

### P1 — Do soon (high value)

| #   | Action                                           | Risk                           | Effort |
| --- | ------------------------------------------------ | ------------------------------ | ------ |
| 3   | Deploy etcd backup (`talos-backup` CronJob)      | Cluster state loss             | Small  |
| 4   | Default-deny network policies                    | Lateral movement in compromise | Medium |
| 5   | Pod Security Standards on application namespaces | Container escape risk          | Small  |
| 6   | Restrict API server access in Hetzner firewall   | Unauthorized cluster access    | Small  |
| 7   | Alertmanager-to-ntfy bridge                      | Silent failures                | Small  |

### P2 — Do when convenient (good practice)

| #   | Action                                   | Risk                          | Effort |
| --- | ---------------------------------------- | ----------------------------- | ------ |
| 8   | Kyverno Audit → Enforce                  | Configuration drift           | Small  |
| 9   | SLO definitions (Pyrra/Sloth)            | Alert fatigue / missed issues | Medium |
| 10  | Velero for PVC backup                    | Application data loss         | Medium |
| 11  | ResourceQuota + LimitRange per namespace | Resource contention           | Small  |
| 12  | Golden Signals dashboards                | Visibility gaps               | Small  |
| 13  | ~~Flux webhook receiver~~                | ~~Reconciliation delay~~      | Done   |
| 14  | Image registry allowlist via Kyverno     | Supply-chain risk             | Small  |

### P3 — Future consideration

| #   | Action                                             | Benefit                 | Effort |
| --- | -------------------------------------------------- | ----------------------- | ------ |
| 15  | Evaluate Crossplane as tofu-controller replacement | Operational stability   | Large  |
| 16  | Grafana Tempo for distributed tracing              | Debug velocity          | Medium |
| 17  | Capacitor for Flux DAG visualization               | Operational visibility  | Small  |
| 18  | Flagger for progressive delivery                   | Safer deployments       | Medium |
| 19  | Tetragon for runtime security                      | Kernel-level protection | Medium |
| 20  | cosign image signing + verification                | Supply-chain integrity  | Medium |

## Operational notes retained from the review

### Backup and recovery (items 2, 3, 10)

The archived review and later updates record no etcd or application PVC backups (Harbor,
Gitea, Loki, and Postgres); a two-of-three control-plane failure could lose cluster state.
The undated update above records the PG/`pg_dump` state backup. The review considered the
Git repository mirrored to GitHub and GitLab adequate. It proposed:

- [talos-backup](https://github.com/siderolabs/talos-backup) as a CronJob taking etcd
  snapshots through the Talos API, encrypting with age, and uploading to S3-compatible
  storage; use the [Talos disaster-recovery guide](https://docs.siderolabs.com/talos/v1.9/build-and-extend-talos/cluster-operations-and-maintenance/disaster-recovery)
  when planning recovery.
- [Velero](https://velero.io/) schedules for PVCs: hourly for critical services (then Vault
  and Authentik), daily for standard services (Gitea, Harbor, Loki), with seven days of
  hourly and 30 days of daily retention. The review noted Proxmox CSI snapshots and
  Hetzner CSI integration.
- Quarterly restore drills and a `scripts/test-restore.sh` integrity check; an un-restored
  backup is unverified.

### Network policy rollout (item 4)

The review recorded no Cilium NetworkPolicies, so pods could communicate freely, and
Hubble at `hubble.allegedly.works`. It proposed default-deny in every namespace. Observe
flows with `hubble observe`, derive a baseline, deploy the policy in audit mode, and enforce
only after checking that legitimate traffic still works. The proposed allow rules covered DNS,
Cilium gateway-to-backend traffic, ESO-to-Vault secret sync, Prometheus scraping, and
application-to-Authentik SSO. See [Cilium zero-trust networking](https://cilium.io/outcomes/zero-trust/).

### Observability (items 7, 9, 12, 16, 17)

The review recorded kube-prometheus-stack/Grafana SSO, Loki on Proxmox storage, ntfy
notifications for Flux reconciliation, and Hubble UI. It found no SLO/SLI definitions and
said the ntfy integration lacked an alerting chain. Proposed changes:

- Route Prometheus alerts through Alertmanager and an ntfy bridge to ntfy.sh; candidates
  were [alertmanager-ntfy](https://github.com/alexbakker/alertmanager-ntfy) and
  [ntfy-alertmanager](https://hub.xenrox.net/~xenrox/ntfy-alertmanager/).
- Define SLOs with [Pyrra](https://github.com/pyrra-dev/pyrra) or
  [Sloth](https://github.com/slok/sloth). Proposed targets, all over seven days: ingress,
  99.5% of requests return non-5xx; DNS, 99.9% of queries answered; Vault, 99.9% of
  secret reads succeed (historical; retired with Vault).
- Give each service a dashboard for latency, traffic, errors, and saturation—the
  [four golden signals](https://sre.google/sre-book/monitoring-distributed-systems/);
  [dashboard 21073](https://grafana.com/grafana/dashboards/21073-monitoring-golden-signals/)
  was suggested as a starting point.
- Consider [Grafana Tempo](https://grafana.com/oss/tempo/) for traces correlated with
  Loki logs, and [Capacitor](https://fluxcd.io/blog/2024/02/introducing-capacitor/) for a
  Flux reconciliation and dependency-DAG view.

### Security and resource controls (items 5, 6, 8, 11, 14, 19, 20)

- Apply the [Restricted Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/)
  to application namespaces (`warn=restricted`, `audit=restricted`, `enforce=restricted`).
  Begin with `warn`, then enforce; the review called out `kube-system`, `csi-proxmox`,
  and `cilium` as needing `privileged`.
- The review recorded Kyverno Audit mode and a `require-gitops` ClusterPolicy. Promote it
  to Enforce after validation to block manual `kubectl apply` of GitOps-owned resources.
- No image signing or verification existed. Sign CI images with
  [cosign](https://github.com/sigstore/cosign) using GitHub Actions
  OIDC, verify them with Kyverno `verifyImages`, and attach Trivy/Syft SBOMs. A pragmatic
  first step was allowing only `ghcr.io`, `docker.io`, and `registry.allegedly.works`.
- The review recorded API port 6443 open to `0.0.0.0/0` in Hetzner firewall rules. Restrict
  it to admin Nebula IPs, inter-node CIDRs, and CI runner IPs if used.
  Consider [Tetragon](https://tetragon.io/) for runtime detection and blocking.
- Most deployments already had requests/limits; no namespace `ResourceQuota` or `LimitRange`
  existed. Add `LimitRange`s for defaults and `ResourceQuota`s for contention control;
  use VPA recommendation mode or [Goldilocks](https://github.com/FairwindsOps/goldilocks)
  for right-sizing. The review framed this as contention prevention on fixed-cost hosts.

### Operations and documentation (items 13, 15, 18)

- `docs/troubleshooting.md` was useful but could be split into one runbook per alert/issue,
  with severity, symptoms, diagnosis, resolution, and prevention. The review described
  the seven `lessons_learned/` entries as serving the postmortem role and proposed a
  consistent timeline, root cause, detection gap, fix, and prevention structure.
- Consider branch protection requiring PR reviews for infrastructure changes on `devel`;
  the Flux `Receiver` + GitHub webhook (to replace one-minute polling with push-triggered
  reconciliation) was marked done in the ledger. Evaluate
  [Flagger](https://github.com/fluxcd/flagger) for Prometheus-analyzed canaries and
  automatic rollback on error-rate spikes.
- The recorded change process was edit → commit → push → Flux reconcile, with no review
  gate. Extract inline `plan.md` decisions into numbered ADRs, generate a Mermaid Flux DAG and
  update it in CI or by script, and include expected bootstrap timings in `bootstrap.py`
  output using the AGENTS.md timing reference.

### tofu-controller alternatives and migration path (item 15)

The review recorded 62 Terraform modules managed in-cluster and three failure mechanisms:
TLS secret-cache desynchronization when startup garbage collection deletes secrets, runner
pod crashes followed by state loss, and Authentik token overwrites after state regeneration.
It attributed the fragility to ephemeral Terraform runner pods and in-cluster state.

| Option                                                          | Architecture                                         | Trade-off                              |
| --------------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------- |
| [Crossplane](https://spacelift.io/blog/crossplane-vs-terraform) | Kubernetes-native CRDs and continuous reconciliation | Significant rework; fewer providers    |
| Atlantis                                                        | PR-based plan/apply                                  | External server; not GitOps-native     |
| Terraform in bootstrap                                          | Move GitOps modules into bootstrap layers            | Simpler; no continuous reconciliation  |
| Keep tofu-controller                                            | Status quo                                           | Known bugs with documented workarounds |

The proposed path was to keep tofu-controller short-term and set `cas = 0` on write-once
`vault_kv_secret_v2` resources to prevent silent overwrites (Vault-specific and no longer
applicable); evaluate Crossplane for new modules in the medium term, including replacing
the SSO blueprint pattern with Compositions for continuous reconciliation without
Terraform state-file management; and migrate as its provider ecosystem matured (then-listed
Authentik, PowerDNS, and Vault providers). The review linked the
[tofu-controller project](https://github.com/flux-iac/tofu-controller).
