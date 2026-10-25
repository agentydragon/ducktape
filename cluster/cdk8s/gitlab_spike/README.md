# GitLab replacement spike

This component deploys a separate GitLab Free evaluation instance through the normal
cdk8s → ArtifactGenerator → Flux path. Merging enables its Kustomizations. The generated
outputs live under `cluster/generated/gitlab-spike/`.

[Live findings and benchmark evidence, 2026-10-25](../../debug/gitlab_spike/2026-10-25-live.md)
record the deployed proofs, resource costs and remaining replacement gates.

- Web: `https://gitlab-spike.allegedly.works`
- Registry: `registry-gitlab-spike.allegedly.works`
- SSH: `gitlab-spike.allegedly.works:2223`
- Provisioned project: `spike/acceptance`

The five Flux stages are prerequisites (namespaces, ESO credentials, databases, Valkey and S3) → GitLab →
bootstrap API token → `tf/gitops/gitlab-spike` → Runner, delivery and benchmark resources.
Authentik provisioning is part of `tf/gitops/sso-providers`. Terraform creates the human
account and links its current Authentik UID before login; automatic account creation is off.
The human account receives the existing Forgejo SSH public keys for `atlas`, `iguana`,
`rugged`, and `wyrm2`.

Provider constraints in `tf/gitops/gitlab-spike/versions.tf.json` are generated from its
`BUILD.bazel`: run `bb run //tf/gitops/gitlab-spike:gen-tf-versions` after changing them.

Praefect manages three separate Gitaly PVCs with one local HDD copy each on distinct OVH
workers. GitLab and Praefect use separate databases and login roles in one three-instance
CNPG cluster, with 25 GiB SSD storage per instance and one coordinated CNPG backup stream.
Gitaly cgroup isolation is disabled pending validation of the writable cgroup mount on Talos.
The experimental Praefect chart and beta Kubernetes topology remain evaluation constraints.

Sidekiq scales from the chart minimum of one Pod to a maximum of two, with concurrency
10 per Pod. Evaluate memory alongside queue depth and job latency before reducing it further.

ESO owns the root password and bootstrap-token Secret. The bootstrap Job installs that
token through Toolbox without printing it. Its PAT expires after 90 days; recreating the
bootstrap Job renews it. Terraform owns and rotates the project-scoped proof and Flux tokens.
The standalone GitLab benchmark PAT expires on `2027-01-22`.

The benchmark CronJobs are suspended. The failure policy selects no pod until the guarded
proof script labels the chosen spike Gitaly primary. Deployment does not start benchmarks
or inject failures. PVCs, buckets and stateful Flux inventories retain/orphan their data;
teardown needs an inventory review.

Acceptance commands, evidence requirements and replacement gaps are in
[`cluster/x/gitlab_spike/README.md`](../../x/gitlab_spike/README.md). No live acceptance
result is implied by successful synthesis, chart rendering or CI.

## Recorded performance references

Median seconds from the [July investigation](../../debug/git_storage_latency_and_ha.md):

| July topology on SeaweedFS SSD       | Version | Contents read | Contents write | Tiny push |
| ------------------------------------ | ------: | ------------: | -------------: | --------: |
| Forgejo, two replicas sharing RWX    |   0.349 |         0.499 |          3.356 |     1.630 |
| GitLab/Praefect, three Gitaly copies |   0.044 |         0.099 |          0.885 |     1.155 |

The [October HDD canary](../../debug/piraeus_migration/benchmarks/README.md) measured
single-writer Forgejo at `0.752 s` for contents writes / `1.140 s` for pushes on local
storage and `0.840 s` / `1.130 s` on two-copy Piraeus. That canary did not test failover.

Keep these historical rows separate from the new runs. Repeat balanced runs of both
suspended benchmark arms and report sample counts, errors, median, p95, client/app image
digests, placement and storage topology. The new comparison measures deployable product
topologies: shared RWX Forgejo versus Gitaly replication over independent local volumes.
It does not isolate application code from storage architecture. Record idle and loaded
resource use, clone/fetch behavior and observed recovery alongside request latency.
