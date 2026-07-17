# Forgejo → GitLab migration considerations

Pre-think for replacing the in-cluster Forgejo with GitLab. Motivated by the git-write
latency + HA investigation (<../../../debug/git*storage_latency_and_ha.md>): GitLab's
**Gitaly Cluster (Praefect)** gives fast git writes \_and* no single-node dependency,
which the Forgejo multi-replica RWX share cannot. This note scopes what a migration
touches and — importantly — what is actually in **Free/CE** vs paid tiers.

Not a decision to migrate; a map of the surface and the gotchas.

## What Forgejo does for us today (the replacement surface)

From `cluster/k8s/forgejo/*` and `tf/gitops/*`:

- **Private git hosting** — `ducktape` (pull-mirror of GitHub), `gaffer-private`,
  `cpap-data`, `budget-ledger`, `augur-evidence`, `props`, `haku-state`.
- **Declarative access via the `svalabs/forgejo` Terraform provider**: 24 users, 30
  collaborators, 8 SSH keys, 14 repos, 4 Actions secrets, 2 webhooks, 2 branch-protection
  rules.
- **Forgejo Actions CI** — notably `haku-ci`: a warm, **egress-fenced, in-cluster-only**
  runner that builds Haku's UI image from `haku-state` and pushes to the in-cluster
  registry, and must **never** touch BuildBuddy/RBE or external CI (see
  `cluster/k8s/haku-ci/README.md`). Plus CI in props / budget-ledger / augur-evidence /
  cpap-data. Runner tokens are fetched from the API by TF because the Forgejo provider has
  no runner-token resource.
- **In-cluster container registry** (`forgejo-images`) + **Flux image automation** reading
  Forgejo registry tags.
- **Authentik SSO**, **RWX git PVC**, a **seaweedfs LFS bucket**.
- **Not touched:** Flux's GitOps source is **GitHub** (`gotk-sync.yaml`), not Forgejo.
  Forgejo hosts only secondary/private repos; that split stays.

## Tooling parity

**Terraform provider — yes, and strictly nicer.** Official `gitlabhq/gitlab` provider,
mature. Our resources map cleanly, and the runner-token wiring gets _simpler_:

| Forgejo (`svalabs/forgejo`)         | GitLab (`gitlabhq/gitlab`)                               |
| ----------------------------------- | -------------------------------------------------------- |
| `forgejo_repository`                | `gitlab_project`                                         |
| `forgejo_user`                      | `gitlab_user`                                            |
| `forgejo_collaborator`              | `gitlab_project_membership`                              |
| `forgejo_ssh_key`                   | `gitlab_user_sshkey`                                     |
| `forgejo_repository_action_secret`  | `gitlab_project_variable`                                |
| `forgejo_repository_webhook`        | `gitlab_project_hook`                                    |
| `forgejo_branch_protection`         | `gitlab_branch_protection`                               |
| _(API-fetch hack for runner token)_ | **`gitlab_user_runner`** — mints the auth token natively |

**Operator — yes.** The official **GitLab Operator** wraps the cloud-native chart with a
`GitLab` CR and sequences the version-upgrade DB migrations (the ~13-min ones observed in
the bench) safely. Heavier + assumes nginx-ingress; on our Talos/Cilium/Gateway-API stack
we'd disable that and wire an `HTTPRoute` (the bench already used the ingress-off shape).
For our scale a **Flux `HelmRelease` on the chart directly** may be simpler; the operator's
real value is upgrade safety.

**GitLab Agent for Kubernetes (`agentk`/KAS) — optional.** Native secure CI→cluster access
without exposing the kube API. We'd likely keep Flux-from-GitHub for GitOps, but `agentk`
is worth it for CI that needs cluster access.

## What changes operationally

- **CI rewrite is the real labor.** `.forgejo/workflows/*` (a GitHub-Actions clone) →
  `.gitlab-ci.yml` (different model). GitLab CI is far more capable; the **Kubernetes
  executor** runs each job as a pod, which maps directly onto the `haku-ci` containment
  model (dedicated egress-fenced namespace + NetworkPolicy, no external CI, push only to the
  in-cluster registry). Requirements transfer; the YAML does not.
- **Registry / packages / mirroring / SSO** are built in: Container + Package registry,
  Dependency Proxy (Docker Hub cache — could cut external pulls), Authentik via OmniAuth
  OIDC. Flux image automation reads GitLab's OCI registry fine.
- **Heavier stack.** ~10 pods, external **PostgreSQL 16** (CNPG), Redis, and object storage
  (seaweedfs S3 / MinIO); ~13-min fresh migrations; slow Puma boot. A real step up from a
  single Go binary on el-cheapo OVH — the central "does it earn its footprint?" question.

## CE (Free) vs paid — verified against GitLab docs (2026-07)

Everything the current Forgejo setup depends on is **Free**. The paid gates only bite
"nice to have" workflow polish. Verified per-feature (`docs.gitlab.com`):

| Capability                                                              | Tier                                                       |
| ----------------------------------------------------------------------- | ---------------------------------------------------------- |
| Git hosting, projects, groups, SSH/deploy keys                          | **Free**                                                   |
| **Gitaly Cluster (Praefect)** — the HA win                              | **Free** (self-managed; GitLab _support_ is Premium+ only) |
| GitLab CI + GitLab Runner (Kubernetes executor)                         | **Free**                                                   |
| Merge-request pipelines                                                 | **Free**                                                   |
| Container Registry, Package Registry                                    | **Free**                                                   |
| **Dependency Proxy** (Docker Hub cache)                                 | **Free**                                                   |
| GitLab Pages                                                            | **Free**                                                   |
| CI/CD components / Catalog, `agentk`/KAS                                | **Free**                                                   |
| **Push mirroring** (GitLab → elsewhere)                                 | **Free**                                                   |
| Optional (non-blocking) MR approvals                                    | **Free**                                                   |
| Secret Detection analyzer (runs in CI)                                  | **Free**                                                   |
| Security-scan analyzers (SAST/container/dep) run in CI                  | **Free** (jobs run)                                        |
| **Pull mirroring** (GitHub → GitLab)                                    | **Premium** ⚠️                                             |
| **Enforced approval rules** (required approvers, prevent self-approval) | **Premium**                                                |
| **Code-owner _required_ approval**                                      | **Premium** (a `CODEOWNERS` file displays in Free)         |
| Merged-results pipelines                                                | **Premium**                                                |
| **Merge trains**                                                        | **Premium**                                                |
| MR / code-review analytics                                              | **Premium**                                                |
| Security Dashboard, MR vulnerability widgets, vulnerability management  | **Ultimate**                                               |

### The one gotcha that hits our current setup

**Pull mirroring is Premium.** Our `ducktape` on Forgejo is a _pull_ mirror of the GitHub
repo. On GitLab CE that mirror direction is gated. Options on Free: keep GitHub as the
source and don't mirror into GitLab; or run a tiny scheduled CI job that `git fetch`es
GitHub and `git push`es into the GitLab project (a poor-man's pull mirror). **Push**
mirroring (GitLab → GitHub) is Free but is the wrong direction for this case.

## Nice things GitLab would let us add (Free-tier)

- **Gitaly Cluster** — the motivating HA/latency win, data-backed.
- **Dependency Proxy** — cache Docker Hub pulls (complements the RBE image caching).
- **GitLab Pages** — could host the website/docs.
- **Package Registry** — npm/pypi/etc. in-cluster (we already use seaweedfs S3 for some).
- Richer CI (DAG pipelines, child pipelines, CI/CD components, scheduled pipelines,
  environments) — all Free.

Paid-only polish we'd forgo on CE: merge trains, enforced approval rules, security
dashboards. For a low-contention personal/agent forge, MR pipelines + branch protection
already cover the 99% case, so the Premium gates are mostly cosmetic here.

## Migration order (sketch, if we do it)

1. Stand up GitLab (Gitaly Cluster + CNPG PG16 + Redis + seaweedfs-S3 object storage +
   `HTTPRoute` + Authentik OIDC), no ingress, bench-shaped values as a base.
2. Port `tf/gitops` Forgejo resources to the GitLab provider (users/projects/membership/
   variables/hooks/runners) — mostly mechanical.
3. Migrate repos (push or import) + LFS + registry images.
4. Rewrite `.forgejo/workflows` → `.gitlab-ci.yml`; rebuild `haku-ci` as a contained
   GitLab Runner (egress-fenced namespace, in-cluster registry only).
5. Repoint Flux image automation at the GitLab registry; repoint the `ducktape` mirror
   (scheduled-job or drop it on CE).
6. Cut SSO over; decommission Forgejo.

## Open questions

- Does GitLab earn its operational weight on this hardware vs. Forgejo + single-writer git
  - app-layer mirror (the other arm of the benchmark's recommendation)?
- Object storage: reuse seaweedfs S3 gateway for GitLab (LFS/registry/artifacts) or MinIO?
- Is losing pull-mirror-on-Free acceptable, or worth the scheduled-job workaround?
