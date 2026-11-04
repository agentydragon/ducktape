# Plan: move the cluster from Forgejo to GitLab

Status: draft proposal, 2026-11-03. Production Forgejo remains authoritative. User scope: migrate Git repositories and
current configuration; importing old Forgejo issues and pull requests is unnecessary. Inventory non-Git data and
consumers before retiring the old installation; do not assume it is disposable.

## Starting point and architecture

- Use the existing in-cluster GitLab spike as the starting point, not an automatic deployment of a new VM-based Git
  service. GitLab web, registry, Shell, Sidekiq and Toolbox are up; three Gitaly and three Praefect replicas are Ready.
  Its Flux stages and demo source are Ready at the time of the inventory. Decide separately whether to harden the spike
  in place or deploy a clean production instance: the former saves effort; the latter allows a clean bootstrap. Preserve
  existing spike data until inventoried.
- The spike has three independent 50 GiB Piraeus local-HDD Gitaly PVCs, one per OVH worker, and replication at the
  application level; database PVCs are local SSD. It is not using the SeaweedFS CSI mount for Gitaly, but GitLab object
  storage uses SeaweedFS S3.
- GitLab documents Praefect on Kubernetes as beta and not recommended for production; standalone Gitaly on Kubernetes is
  generally available but does not provide repository-level failover. The GitLab-recommended production-HA alternative
  is a hybrid deployment with Praefect/Gitaly outside Kubernetes. This is a support/maturity trade-off, not a technical
  requirement for VMs. The operator is comfortable proceeding with the in-cluster Praefect topology despite that
  maturity status; failure handling, upgrade behavior and the other migration gates still need evaluation.
  [GitLab deployment options](https://docs.gitlab.com/administration/gitaly/kubernetes/)
- Existing evidence: a private project, Authentik OIDC, managed user, Kubernetes Runner, push -> registry -> Flux ->
  app, and one controlled Gitaly primary partition were demonstrated. One push failed during that fault before a retry
  succeeded; the acknowledged commit survived. This does not prove general zero data loss or a production restore. Real
  private repo import, human SSH access, membership revocation, and existing agent workflows remain gates. The operator
  chose to proceed without a backup-restore rehearsal, so recoverability remains unverified. Synthetic Git/LFS and
  scoped SSH round trips passed; see the [preflight report](../../debug/gitlab_spike/2026-11-03-migration-preflight.md).
  The spike's initial Rails database seed needed manual recovery. See `cluster/debug/gitlab_spike/2026-10-25-live.md`
  and `cluster/x/gitlab_spike/README.md`.
- The spike measured 9.895 GiB namespace working set in a quiet period after tuning and 15.31 GiB under the earlier
  benchmark; the earlier Forgejo comparison was 1.31 GiB under load. These windows and configurations differ; neither is
  a capacity forecast. Rails startup showed long I/O stalls with container image files on a busy HDD. Measure current
  peak capacity and restart behavior, including application image storage, before committing production workloads.

## GitLab repository storage direction (2026-11-03)

The spike's Praefect cluster replicates repositories across Gitaly nodes. GitLab calls the
[Praefect Helm chart experimental and unsuitable for production](https://docs.gitlab.com/charts/charts/gitlab/praefect/)
and has de-prioritized Gitaly Cluster on Kubernetes in favor of a new backend. Standalone Gitaly on Kubernetes is
generally available but has no repository-level failover. The
[hybrid architecture](https://docs.gitlab.com/administration/gitaly/kubernetes/) runs Praefect and Gitaly outside
Kubernetes for repository-level HA. None of these maturity statements establishes our contractual support entitlement.

GitLab's [Scaling Git design](https://handbook.gitlab.com/handbook/engineering/architecture/design-documents/scaling-git/)
proposes authoritative Git repository data in object storage, with stateless Gitaly nodes caching data locally, a new Git
MVCC format for consistent reads and atomic writes, and a routing layer. GitLab's stated goal is resilience and
horizontal scaling on Kubernetes without Praefect or its separate PostgreSQL database. This concerns Git repositories,
not the separate object-storage categories already used by GitLab for features such as LFS and the registry.

The design is ongoing and explicitly not committed. The [engineering epic](https://gitlab.com/groups/gitlab-org/-/work_items/21711)
reported on 2026-10-22 that most MVCC-reference migration infrastructure had landed and the object-storage format
extension was in review; it does not establish a production release date. Public proof-of-concept changes exist for
[Git](https://gitlab.com/gitlab-org/git/-/merge_requests/551) and
[Gitaly](https://gitlab.com/gitlab-org/gitaly/-/merge_requests/8812), but are not a deployable production solution.
The design says the object-storage migration is for "enterprise customers" and an earlier local-storage phase is the
last stage for non-enterprise customers; the final edition and license eligibility are unclear.

SeaweedFS is named as an example object store, not verified for our deployment. The design requires read-after-write
consistency, conditional create-only writes and deletes, ordered prefix listing and ETag behavior. Verify those semantics,
durability and availability against the actual SeaweedFS version and configuration before considering it for Git's
authoritative data. Object storage would become a Git availability dependency. Recheck the upstream design, release
status, licensing and migration path from Praefect before treating it as an option; do not depend on its arrival for the
current cutover decision.

## Phase 1: inventory and contract

The [source inventory](../../debug/gitlab_spike/2026-11-04-source-inventory.md) records the nine repositories, all
advertised refs, default-branch trees, LFS metadata, registry version/manifest metadata, live Flux sources and workload
references, and known owners. It is a point-in-time snapshot; private detail is kept outside this public repository.
The remaining per-consumer dispositions, off-cluster clients and final cutover refresh are not yet complete.

1. Inventory every Forgejo repository by owner, default branch, visibility, branches/tags, wikis and LFS use, active
   writers, mirror direction, Git/HTTP/SSH clients, credentials, branch policies, hooks, and CI workflows. Extend the
   initial Forgejo API and Git-ref inventory with per-repo LFS manifests; distinguish private user data from disposable
   mirrors. Git repository metadata, issue/PR history, wikis, releases, and CI artifacts are separate categories; do not
   promise to import or delete them without an explicit disposition.
2. Inventory registry images by OCI repository, full tag set, manifest digest, platform/index, signatures/referrers
   where relevant, producers, deployed digest references, GitOps and rollback history, dormant configurations and
   external users. Record live Flux ImageRepositories, ImagePolicies, pull secrets, namespace secret stores, receivers,
   and writers. Current snapshot: nine private repositories were discoverable with the Forgejo admin API, and all nine
   advertised refs; 69 Flux ImageRepositories reference `git.allegedly.works`. Three private Flux GitRepository objects
   refer to Forgejo: `ducktape-flux/finance-agent`, `flux-system/haku-state` and `haku-sandbox/haku-state-write`.
3. Map Terraform/Crossplane ownership, secret rotation and ESO copies, Authentik identities, egress proxy credential
   injection, agent Git/Forgejo API calls, SSH keys, scheduled tasks, backup/restore dependencies, and bootstrap
   ordering. Include Haku's state writer, UI/Anki writers, Flux image automation and jobs. Preserve ownership of
   existing resources; never destroy old provider-managed repos by accidentally removing their state.
4. Agree on per-service write freeze duration, acceptable data loss and recovery objectives, which paths need stable
   URLs, and proof of a rollback. Do not claim RPO/RTO values until measured.

Gate: an owner and migration method for each data class, writer, reader, credential, workflow and image; no unknown
critical bootstrap dependency.

## Declarative resource mapping

Port each owner independently and verify its plan and credentials; these resource names are a starting point, not a
claim of equivalent security semantics.

| Forgejo resource or function                       | Candidate GitLab resource                                                                         |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Repository and owner                               | `gitlab_group`, `gitlab_project`, `gitlab_user`                                                   |
| Collaborators and SSH keys                         | `gitlab_project_membership`, `gitlab_group_membership`, `gitlab_user_sshkey`, `gitlab_deploy_key` |
| Actions secrets and workflows                      | `gitlab_project_variable`, GitLab CI and project-scoped `gitlab_user_runner`                      |
| Webhook and branch protection                      | `gitlab_project_hook`, `gitlab_branch_protection`                                                 |
| Repo-scoped service access and registry credential | Project/group access tokens or deploy tokens, with independently verified scopes and rotation     |

GitLab Free does not provide
[required MR approvals](https://docs.gitlab.com/user/project/merge_requests/approvals/#required-approvals). Check
whether any existing policy depends on them. GitLab evaluates
[overlapping protected-branch rules](https://docs.gitlab.com/user/project/repository/branches/protection_rules/) using
the most permissive rule for most settings, so test the resulting permissions rather than checking rules individually.

## Phase 2: make GitLab a safe destination

1. Produce separately reviewable declarative cdk8s/Flux, OpenTofu/Crossplane, Authentik, ESO and network changes for the
   chosen in-cluster topology. Decide whether to promote the spike or build a clean instance. Configure production
   DNS/TLS/SSH, PostgreSQL/Valkey, object buckets, backups, resource requests/placement, monitoring, quotas and upgrade
   sequencing. Keep container image and recovery dependencies outside a circular dependency on the new registry or old
   Forgejo.
2. Establish SSO and least-privilege users, groups, projects, keys, tokens, webhook secrets and branch protections.
   Exercise first login, repeat login, service credentials, revocation, rotation, Terraform no-drift and clean
   bootstrap, including interrupted database initialization recovery. GitLab's project and registry permissions must be
   tested rather than assumed equivalent to Forgejo.
3. Prove readiness and failure handling for each Gitaly failure domain, Praefect quorum, DB failover, pod and node loss,
   network interruption, recovery and replica verification. The prior single partition experiment is a starting point
   only. Inventory backups of enabled categories, the Rails secrets, registry objects and the separately backed-up
   Praefect metadata database. The six
   [skipped categories](../../debug/gitlab_spike/2026-11-03-migration-preflight.md#backup-preflight) were disabled in
   the spike; reassess backup coverage before enabling one. The operator chose not to rehearse a backup restore, so the
   resulting recovery procedure is unverified. Follow the chart's backup/restore guidance and account for separately
   managed object storage.
   [GitLab restore requirements](https://docs.gitlab.com/administration/backup_restore/restore_gitlab/)
4. Prove existing agents' least-privilege review proposal flow: Forgejo AGit `refs/for/*` and read-only source access
   cannot be assumed to work in GitLab. Prototype an appropriately scoped fork/MR or equivalent; test protected-branch
   enforcement, SSH, HTTP and injected credentials. Maintain Haku's allowed direct writes to its state while preserving
   branch protection.
5. Implement GitHub -> GitLab mirror for each needed mirror. The observed Forgejo pull mirror is `haku/ducktape`;
   determine the disposition of the separate `agentydragon/ducktape` copy. GitLab Free lacks built-in pull mirroring; either drop
   unused mirrors or run a narrowly credentialed, monitored fetch/push job. Define ref deletion and force-update
   behavior and prevent it from overwriting GitLab-owned refs. Native pull mirroring is Premium/Ultimate.
   [GitLab pull mirror](https://docs.gitlab.com/user/project/repository/mirror/pull/)

Gate: supported or consciously accepted deployment trade-off, successful clean bootstrap, inventoried backups,
threat-model parity and a tested production data path. Backup restoration is not a gate; it remains unverified. Operator
owns the HA/availability decision.

## Phase 3: prepare production integrations in parallel

1. Reproduce Haku CI on project-scoped GitLab Runner using the Kubernetes executor, preserving its actual trust
   contract: in-cluster execution; no BuildBuddy or external CI for private `haku-state`; fenced egress; per-job
   isolation; scoped Git/registry credentials; no job service-account token; no arbitrary cross-project jobs; node
   placement; and no scale-down deletion of running builds. Preserve existing necessary dependency-fetch allowlist,
   rather than assuming zero network access. The
   [preflight](../../debug/gitlab_spike/2026-11-03-migration-preflight.md#runner-manager-policy-gap) found that the
   spike manager's Cilium policy did not select its Pod. PR #14225 fixed the selector, Flux applied it, and Cilium
   reported egress enforcement; a synthetic runner job passed afterward. Runner-manager RBAC must be separate from
   job-pod credentials. Exercise a real representative build, concurrent/long builds, failures, revocation, cancellation
   and malicious egress tests; do not assume the demo's ko build covers Haku's Dockerfile. Assess privileged DIND risk
   and aim for a tested unprivileged build if compatible. [Runner security](https://docs.gitlab.com/runner/security/)
2. Port the remaining Forgejo Actions workflows to GitLab pipelines independently, using fixtures and appropriate secret
   boundaries. Mirror pipeline status and artifact behavior explicitly, not workflow YAML line for line.
3. Design GitLab registry namespace/project mapping before copying: GitLab registry images are tied to a project path,
   so `git.allegedly.works/ducktape-ci/<image>` cannot simply be assumed to map to the same authorization and namespace.
   Options include per-image projects or a shared image project with updated paths. Test scoped pushes, pulls, isolation
   and image-policy matching. [GitLab registry naming](https://docs.gitlab.com/user/packages/container_registry/)
4. Publish to the new registry alongside the old only where this does not create multiple authoritative writers. Copy
   referenced tags and digest-pinned versions, all platforms and required signatures/referrers, checking manifest
   digests and fresh pulls. Test new registry credentials, ESO fanout, namespace allowlist, CI image publishing, Flux
   tag scanning, selection and webhook -> reconciliation. Keep old registry alive for old pinned deployments and
   rollback until all consumers are accounted for.
5. Prepare GitLab private Flux sources, kubelet pull credentials, Haku/finance consumers and all Forgejo-specific API
   adapters and agent credential rotation in reviewable changes. Prove token rotation and repo-specific permissions.
   Preserve GitHub as the root Flux source; separately migrate its private secondary sources. Sequence Flux owners to
   avoid races or reconciliation against absent credentials.

Gate: representative end-to-end build and deployment, private source reconciliation, image digest pulls and denied
unauthorized access all work on the new stack without altering production ownership.

## Phase 4: migrate incrementally and cut over

1. For each repository seed GitLab using a method tested with private and protected refs. Compare all intended branch
   and tag object IDs; push/fetch LFS and compare object IDs; compare repository visibility, access and configuration.
   Preserve the source `refs/pull/*` in a backup and assess whether they retain otherwise unreachable commits; they are
   not automatically equivalent to GitLab merge-request refs. Do not blindly `--mirror` over protected or GitLab-owned
   refs. Historical issues/PRs are outside scope.
2. Pilot a low-impact repo with one owner and a tested rollback. For active repos, set one authoritative writer and use
   a bounded write freeze: stop or fence all application writers, CI pushes, automated mirrors and GitOps mutations;
   confirm no in-flight work; final sync; compare refs and LFS; enable GitLab permissions and integrations; switch
   readers and writers declaratively; run a representative end-to-end write and read. Record the cutover SHA and
   event/queue checkpoint. Avoid replaying uncertain writes; reconcile exact outcomes before retries.
3. Cut over Haku and other private state separately, explicitly identifying and fencing scan jobs, UI writes, scheduled
   agents and Flux image commits. Resume in a controlled order, then verify state updates, branch protections, CI,
   registry publish, Flux image update, deployed application and credentials. Inventory and switch finance's private
   Flux source separately. Verify local and outside-cluster clients.
4. Switch image publishers and consumers in small batches; ensure the destination contains both current and rollback
   digests before pointing workloads or Flux at it. Keep the old registry serving images while any image path or
   rollback depends on it. A separate GitLab hostname allows both services to coexist; changing `git.allegedly.works`
   also changes the existing registry entry point, so do not flip that hostname until all HTTP/SSH and OCI routing,
   image pulls and legacy clients are explicitly handled.
5. For rollback before GitLab accepts writes, re-enable Forgejo against the untouched source. After GitLab accepts
   writes, first fence GitLab writers, inventory and export all acknowledged commits plus relevant configuration or
   generated image/tag state, reconcile them into Forgejo with verified protection and no overwrite, and only then
   switch back. If safe reconciliation cannot be proven, hold writes and make an operator decision; DNS reversal alone
   loses/diverges data. Record each cutover and rollback checkpoint.

Gate: compare final ref/LFS inventories, permissions, SSH and API behavior, working CI, signed-in users, private Flux
revisions and images by digest, plus monitored failure-free observation for an agreed period. All differences need an
explicit disposition.

## Phase 5: retire Forgejo

Only after every consumer, dormant/rollback image, host reference, Forgejo-specific API client, secret rotator,
workflow, webhook, runner, root state owner and object-store dependency is migrated or explicitly retained: take and
retain an independent Forgejo backup; preserve user-authored repo and package data and all required cryptographic keys;
establish a retention period; and remove controllers/dependencies in a separately reviewed sequence. Prevent Terraform
or Flux prune from deleting resources or credentials still in use. Retire the SeaweedFS CSI Git PVC only after its data
and the old service's recovery obligations are satisfied. Do not delete volume replicas to resolve prior index
inconsistencies without separate evidence.

## Open decisions and immediate next work

- Proceed with in-cluster Praefect as the working topology, subject to the failure-handling and migration gates above.
  Track the proposed object-storage backend separately until its production readiness, license, SeaweedFS compatibility
  and migration path are established.
- Promote the spike versus reproduce it as a production instance; whether to retain Forgejo read-only temporarily after
  cutover; explicit outage and data-loss objectives; production Git and registry hostname layout; per-image or shared
  registry project layout; and mirror policy.
- First execution work: assign dispositions and destination owners from the source inventory, including off-cluster
  clients; rehearse a real private-repo/registry migration; prototype Haku's fenced runner and review flow; then
  sequence the resulting independent implementation PRs. Refresh source inventories under the cutover freeze. None of
  these authorize cutover by themselves.
