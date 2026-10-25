# GitLab replacement spike

This directory contains disposable acceptance material for the GitLab spike. It does not
enable or reconcile the deployment. Current state: **not deployed; no live acceptance evidence
has been collected**.

| Boundary                            | Expected value                                                 |
| ----------------------------------- | -------------------------------------------------------------- |
| GitLab namespace                    | `gitlab-spike`                                                 |
| GitLab URL                          | `https://gitlab-spike.allegedly.works`                         |
| Registry                            | `registry-gitlab-spike.allegedly.works`                        |
| In-cluster webservice               | `http://gitlab-spike-webservice-default.gitlab-spike.svc:8181` |
| Disposable project                  | `spike/acceptance`                                             |
| Terraform CR                        | `flux-system/terraform.gitlab-spike`                           |
| CI runner namespace                 | `gitlab-spike-ci` (untagged jobs, spike runner only)           |
| Flux demo namespace and source path | `gitlab-spike-demo`, `./k8s`                                   |

The provisioner creates a low-privilege, non-admin `spike-benchmark` user's personal access
token in Secret `gitlab-spike-benchmark-token` (key `token`) for the existing 20-operation
benchmark. The account has no group membership or group-creation rights. Provisioning protects
`main` and configures a GitLab project webhook for push and pipeline events, authenticated
by the Flux Receiver's secret token. The pipeline
uses GitLab's predefined `CI_REGISTRY_USER` and `CI_REGISTRY_PASSWORD` values. Do not put
credentials in this directory or in evidence files.

## Before collecting evidence

The manifests and scripts are a reviewable acceptance recipe. Run it only after the reviewed
GitOps change has been committed, reconciled, and the namespace-specific objects are ready.
Capture evidence outside the repository, for example:

```bash
./cluster/x/gitlab_spike/scripts/capture-evidence.sh /tmp/gitlab-spike-evidence/$(date -u +%Y%m%dT%H%M%SZ)
```

The script is read-only. It captures resource status and image identifiers, not Kubernetes
Secrets or Terraform variables. The commands below that alter a project, registry, or Gitaly
replica are explicitly identified and limited to the disposable spike.

For every Kubernetes command, set KUBECONFIG explicitly to the approved cluster configuration.
This isolated checkout's local environment may otherwise select a generated placeholder path.

## Provisioning and revocation proof

Use the in-cluster tofu-controller CR at `flux-system/terraform.gitlab-spike`; do not run local
Terraform against its backend or alter its state. A green Flux Kustomization only proves that
the CR was applied. The provider result and GitLab API/UI state are the evidence.

1. **Provision:** after reconciliation, save the CR metadata and conditions, the controller
   runner logs for that run, and the GitLab API response showing the exact managed group,
   project, and membership. The only configured membership is the OIDC human account from
   `gitlab_user.agentydragon`, with Owner access to group `spike`; it is not a service account or
   a project-only membership. Redact all token fields. Confirm the only project in the group is
   the disposable `spike/acceptance` project.
2. **No-op:** wait for a completed tofu-controller run after configuration is unchanged.
   Record the plan summary (`0 to add, 0 to change, 0 to destroy`) and confirm that the same
   managed resources remain present in GitLab. A CR `Ready` condition alone is not a no-op
   proof.
3. **Drift:** unavailable with the current configuration. It has no disposable service-account
   membership to drift; the only membership is the intended OIDC human's Owner access to the
   entire private group. Do not remove or alter that membership for this test. Add an explicit,
   separately reviewed low-privilege service-account membership before claiming membership drift
   coverage.
4. **Revoke:** unavailable until that disposable service-account membership exists. Do not remove
   `gitlab_group_membership.agentydragon` or revoke the human Owner's group access as a substitute.
   Never delete the project or repository as a revoke test.

For each completed step, retain the exact source commit, CR generation and conditions, plan/run ID, UTC
timestamps, redacted GitLab API result, and a before/after resource inventory. Stop if the
controller proposes deleting the project, repository, or user. Check the generated readable
plan ConfigMap only if the module enables it and its provider marks every credential-bearing
field sensitive; do not copy raw plans or controller logs into this repository.

## Manual OIDC browser proof

OIDC is **pending manual proof**. It cannot be established from chart values or a healthy
callback route.

1. Open a private browser session at `https://gitlab-spike.allegedly.works` and choose the
   configured OpenID Connect login.
2. Complete Authentik authentication and verify GitLab lands on the intended account whose
   external subject is the expected Authentik user UID. Confirm the account has only the
   intended spike access.
3. Sign out of GitLab and Authentik, repeat the login, and confirm the same GitLab account is
   selected rather than a duplicate account.
4. Record UTC time, browser version, selected account identity (no token/claims dump), and
   redacted screenshots of the initial login, successful return, account identity, and second
   login. Capture server-side auth logs only after removing cookies, authorization headers,
   client secrets, and tokens.

## Push pipeline, image, and Flux chain

The sample files are intended for the private `spike/acceptance` repository. Seed its `k8s/`
tree from the cdk8s-generated demo manifests at
`cluster/generated/gitlab-spike/demo-seed/`; do not hand-author Kubernetes workload manifests
here. The generated root Kustomization references the image-pins component, whose native
component file lives at `cluster/x/gitlab_spike/seed/k8s/image-pins/kustomization.yaml` and is
copied into the seeded repository. After the spike Terraform project exists, seed it with the guarded
script below. Use the Maintainer username and password from the protected
`flux-system/gitlab-spike-git` Secret through an approved secure handoff. Do not use the
benchmark user or failure-proof token for this setup write.

```bash
export GIT_REMOTE_URL=https://gitlab-spike.allegedly.works/spike/acceptance.git
read -r -p 'GitLab project username: ' GIT_AUTH_USER
read -r -s -p 'GitLab project password: ' GIT_AUTH_TOKEN; printf '\n'
export GIT_AUTH_USER GIT_AUTH_TOKEN
ALLOW_SPIKE_PROJECT_WRITE=YES BENCH_TARGET=spike/acceptance \
  ./cluster/x/gitlab_spike/scripts/seed-project.sh
unset GIT_AUTH_USER GIT_AUTH_TOKEN
```

The script refuses any other project URL, requires both explicit spike guards, copies only
the generated demo seed and sample CI/app sources, and pushes a normal fast-forward commit to
`main`. Its local clone and authentication header are removed on exit. CI runs Go tests in the
pinned official ko builder image. On protected `main`, `ko build` compiles the app and pushes
the final selector-matching tag directly, without a Docker daemon, privileged
Docker-in-Docker, or a staging tag. Jobs run as nonprivileged UID 1000 with Go caches under
`$CI_PROJECT_DIR`; the app image uses ko's static runtime base and UID/GID 65532, matching the
generated demo Deployment.

The builder is pinned to the published GHCR image digest
`ghcr.io/ko-build/ko@sha256:8cc63fb82d48708a20ec5055adc41b12bdfb5709f82a49c1b12f25e259760e7f`.
That image contains Go 1.26.6; the fixture declares Go 1.26.0 in `go.mod`, and
`GOTOOLCHAIN=local` prevents a toolchain download. CI logs `ko version` and `go version` so
the toolchain and builder version are recorded with each run.

`ko`'s default runtime base is `cgr.dev/chainguard/static`, so the job needs HTTPS egress to
`cgr.dev` and `9236a389bd48b984df91adc1bc924620.r2.cloudflarestorage.com` for redirected image
blobs. The runner also fetches the pinned builder image from `ghcr.io`. See `ko`'s
[build reference](https://ko.build/reference/ko_build/), [base-image configuration](https://ko.build/configuration/),
and Chainguard's [registry network requirements](https://edu.chainguard.dev/chainguard/containers/registry/network-requirements/).

1. Run a pipeline on `spike/acceptance`. Save the pipeline URL/ID, commit SHA, test output,
   build log, pushed tag, and registry digest. The `publish` job runs only for protected
   `main` and pushes `CI_REGISTRY_IMAGE:devel-<UTC YYYYMMDDHHMMSS>-<7-char commit SHA>`; this
   matches the generated ImagePolicy's selector. GitLab's native pipeline webhook notifies
   Flux after publishing completes; retain its successful delivery status from the project's
   webhook history. Push events also notify Flux when Git source changes.
2. Confirm the tag is visible in the GitLab project's Container Registry and can be pulled
   using Secret `gitlab-spike-registry`. Do not record credentials.
3. Confirm the GitLab `GitRepository` source can read this private repository, image
   automation observes the new tag and writes the selected tag in the generated demo source,
   and Flux reconciles the demo Deployment in `gitlab-spike-demo`.
4. Record source artifact revision, ImageRepository/ImagePolicy/ImageUpdateAutomation status,
   the updated manifest commit, Flux Kustomization revision/conditions, Deployment rollout,
   Pod `imageID` (digest), and a successful `/healthz` response from the deployed app.
   These are separate links in the chain; a successful image push does not prove Flux pulled
   or deployed it.

This is an OCI image assembly path, not a Dockerfile or general-purpose container build
service. A production application that needs Dockerfile semantics still needs a separately
reviewed rootless builder or approved build service. The runner and ko build path have not
been exercised.

## Repeated 20-operation application benchmark

Reuse the existing scripts without modification:

- GitLab: `cluster/debug/gitlab_gitaly_storage_bench/gitlab-bench.sh`
- Forgejo control: `cluster/debug/piraeus_migration/benchmarks/forgejo-bench.sh`

Each script creates a uniquely named private disposable project, performs 20 samples each of
version, contents read, contents write, and tiny Git push, then deletes that project. Do not
point the scripts at any other project. The GitLab CronJob uses only the low-privilege PAT in
Secret `gitlab-spike-benchmark-token` (key `token`). The Forgejo CronJob uses the dedicated
low-privilege account in Secret `forgejo-spike-benchmark`; its target label is
`forgejo-current`. Both suspended CronJobs run their client on the same node,
`ovh-ns102453`.

Create one disposable Job from each CronJob, in sequence. Save the Job, Pod placement, and raw
CSV logs outside the checkout before deleting the one-off Job. Example for GitLab:

```bash
RUN_ID=$(date -u +%Y%m%d%H%M%S)
EVIDENCE_DIR=/tmp/gitlab-spike-benchmark-$RUN_ID
JOB=gitlab-spike-bench-$RUN_ID
mkdir -m 700 -p "$EVIDENCE_DIR"
sha256sum cluster/debug/gitlab_gitaly_storage_bench/gitlab-bench.sh >"$EVIDENCE_DIR/script.sha256"
kubectl -n gitlab-spike create job --from=cronjob/gitlab-spike-benchmark "$JOB"
kubectl -n gitlab-spike wait --for=condition=complete "job/$JOB" --timeout=30m
kubectl -n gitlab-spike get job "$JOB" -o yaml >"$EVIDENCE_DIR/job.yaml"
kubectl -n gitlab-spike get pods -l "job-name=$JOB" -o wide >"$EVIDENCE_DIR/pods.txt"
kubectl -n gitlab-spike logs "job/$JOB" --all-containers=true >"$EVIDENCE_DIR/gitlab-api-git.csv"
kubectl -n gitlab-spike delete job "$JOB"
```

Repeat for Forgejo with a new RUN_ID and Job name, CronJob `forgejo-spike-benchmark`, and log
file `forgejo-api-git.csv`. Capture each dedicated account's project inventory immediately
before and after the run; the inventories must match after the benchmark cleanup trap. Never
copy Secret data to a terminal or evidence file. Also preserve:

```bash
RUN_ID=$(date -u +%Y%m%d%H%M%S)
EVIDENCE_DIR=/tmp/forgejo-spike-benchmark-$RUN_ID
JOB=forgejo-spike-bench-$RUN_ID
mkdir -m 700 -p "$EVIDENCE_DIR"
sha256sum cluster/debug/piraeus_migration/benchmarks/forgejo-bench.sh >"$EVIDENCE_DIR/script.sha256"
kubectl -n gitlab-spike create job --from=cronjob/forgejo-spike-benchmark "$JOB"
kubectl -n gitlab-spike wait --for=condition=complete "job/$JOB" --timeout=30m
kubectl -n gitlab-spike get job "$JOB" -o yaml >"$EVIDENCE_DIR/job.yaml"
kubectl -n gitlab-spike get pods -l "job-name=$JOB" -o wide >"$EVIDENCE_DIR/pods.txt"
kubectl -n gitlab-spike logs "job/$JOB" --all-containers=true >"$EVIDENCE_DIR/forgejo-api-git.csv"
kubectl -n gitlab-spike delete job "$JOB"
```

- UTC start/end, exact script SHA, cluster context, node placement, and job/pod UID;
- GitLab chart/app version and Forgejo image digest;
- Gitaly PVC storage class, Praefect/Gitaly replica readiness, and concurrent load notes;
- benchmark target label, project namespace/account, and cleanup result.

Do not combine these new measurements with the historical result tables without labeling the
different GitLab chart, versions, cluster load, and storage configuration. Never create a
benchmark repository under a human or production project namespace.

## Representative clone and fetch benchmark

`scripts/bench-git-transfer.sh` measures fresh mirror clones and no-change fetches from an
explicitly selected repository. It is read-only on the remote and records raw per-operation
timings along with object/commit/ref counts and disk size. Run it against the GitLab
`spike/acceptance` repository and a fresh Forgejo control repository seeded from the same
commit tree. Record their commit, refs, object count and repository size; comparisons are only
valid when these inputs match. Credentials are supplied through environment variables and
are not written to the evidence directory. For GitLab use the project-scoped Developer
credential in Secret `gitlab-spike-proof`; for Forgejo use the dedicated account in
`forgejo-spike-benchmark`. Supply them through a secure handoff. Set `BENCH_LOCATION`, `BENCH_ROUTE`,
`CLUSTER_CONTEXT`, and `JOB_POD_UID`; the script records them with the remote URL, Git version,
sample counts, UTC times, and repository shape. Use `not-applicable` for the cluster fields
only when running outside Kubernetes.

This is an app-path comparison. It does not measure client WAN performance unless the script
is run from a client on that network path.

For a matched large-repository control, seed the same public source snapshot into two fresh
private destinations: one personal project under spike-benchmark and one Forgejo repository
under gitlab-spike-control. Use a unique name such as `git-transfer-TIMESTAMP`. Create
both through their APIs with private visibility and no auto-initialized README; save the
creation responses and IDs. The GitLab API uses the benchmark PAT from
`gitlab-spike-benchmark-token`; Forgejo uses `forgejo-spike-benchmark`. Do not create a group,
fork, or repository under any human or production owner.

Clone the public source once, then seed both destinations from that same local mirror. Record
the source commit, symbolic default branch, sorted refs, object count, pack size, and UTC time
before pushing. GitHub exposes read-only refs/pull/*; record them in the original snapshot,
then remove only those local pull-request refs before the mirror push. Keep all other branches
and tags identical in both controls. The source repository remains read-only.

```bash
RUN_ID=$(date -u +%Y%m%d%H%M%S)
CONTROL_REPO=git-transfer-$RUN_ID
EVIDENCE_DIR=/tmp/git-transfer-$RUN_ID
mkdir -m 700 -p "$EVIDENCE_DIR"
git clone --mirror https://github.com/agentydragon/ducktape.git "$EVIDENCE_DIR/source.git"
git --git-dir="$EVIDENCE_DIR/source.git" rev-parse HEAD >"$EVIDENCE_DIR/source-head.txt"
git --git-dir="$EVIDENCE_DIR/source.git" symbolic-ref HEAD >"$EVIDENCE_DIR/source-default-branch.txt"
git --git-dir="$EVIDENCE_DIR/source.git" show-ref --head | sort >"$EVIDENCE_DIR/source-refs-before-filter.txt"
git --git-dir="$EVIDENCE_DIR/source.git" count-objects -v >"$EVIDENCE_DIR/source-objects-before-filter.txt"
git --git-dir="$EVIDENCE_DIR/source.git" for-each-ref --format='delete %(refname)' refs/pull | git --git-dir="$EVIDENCE_DIR/source.git" update-ref --stdin
git --git-dir="$EVIDENCE_DIR/source.git" show-ref --head | sort >"$EVIDENCE_DIR/source-refs-to-push.txt"
git --git-dir="$EVIDENCE_DIR/source.git" count-objects -v >"$EVIDENCE_DIR/source-objects-to-push.txt"
```

Create the GitLab destination with POST /api/v4/projects, omitting namespace_id so the project
belongs to the benchmark PAT's personal namespace. Set path and name to CONTROL_REPO,
visibility to private, and initialize_with_readme to false. Do not set default_branch on this
request: GitLab only accepts it at creation when initializing with a README. Create the Forgejo
destination with POST /api/v1/user/repos as gitlab-spike-control, using the same name,
private=true, and auto_init=false. Use a mode-0600 temporary curl config for API credentials,
save each response outside the checkout, and verify the returned owner, path, and
project/repository ID before pushing.

Push the same local mirror to the two exact clone URLs returned by those API creations. Use
HTTP Basic authentication configured through the environment-only GIT_CONFIG_* variables, as
in the seed script; never put tokens in remote URLs or command arguments. Confirm `devel` exists
in the source snapshot and set it as the default branch after pushing (GitLab Projects API
`PUT /api/v4/projects/<id>` with `{"default_branch":"devel"}`; for Forgejo, use the repository
settings page). Verify both repositories expose the recorded refs and default branch before
measuring. Run
bench-git-transfer.sh once per remote with the same client, sample counts, and source snapshot;
record the route and actual job/pod UID or mark the run as outside Kubernetes. Preserve both
raw timing CSVs and metadata. After review, delete only the recorded GitLab project ID and the
Forgejo path gitlab-spike-control/CONTROL_REPO created in this run; first verify each API
response still names the expected owner and the unique git-transfer- prefix. Never delete the
public source or any repository that predates this run.

## Praefect replication and primary-failure proof

Start with a healthy baseline for spike/acceptance. The Rails project ID and hashed repository
relative path are different values; capture both for repeatability. Set KUBECONFIG explicitly
to the approved cluster configuration before using kubectl. Discover ready pods, then get the
project values from a ready Toolbox pod:

```bash
kubectl -n gitlab-spike get pods -o wide
kubectl -n gitlab-spike get pods -l app=toolbox,release=gitlab-spike
kubectl -n gitlab-spike get pods -l app=praefect,release=gitlab-spike
TOOLBOX_POD=$(kubectl -n gitlab-spike get pods -l app=toolbox,release=gitlab-spike \
  -o jsonpath='{.items[0].metadata.name}')
kubectl -n gitlab-spike exec "$TOOLBOX_POD" -c toolbox -- gitlab-rails runner \
  'p=Project.find_by_full_path("spike/acceptance"); abort "project missing" unless p; puts "project_id=#{p.id}"; puts "relative_path=#{p.repository.disk_path}"'
```

From a ready Praefect pod, run the check and metadata commands with the chart's config file:

```bash
PRAEFECT_POD=$(kubectl -n gitlab-spike get pods -l app=praefect,release=gitlab-spike \
  -o jsonpath='{.items[0].metadata.name}')
kubectl -n gitlab-spike exec "$PRAEFECT_POD" -c praefect -- \
  /usr/local/bin/praefect -config /etc/gitaly/config.toml check
kubectl -n gitlab-spike exec "$PRAEFECT_POD" -c praefect -- \
  /usr/local/bin/praefect -config /etc/gitaly/config.toml metadata \
  -virtual-storage default -relative-path '<relative_path>'
kubectl -n gitlab-spike exec "$TOOLBOX_POD" -c toolbox -- \
  gitlab-rake 'gitlab:praefect:replicas[<project_id>]'
```

The baseline must show the intended primary, all three assigned replicas healthy and at the
current generation, and matching checksums from the replica task. The official task prints the
primary and internal replica checksums; its source formats a table but does not compare the
values or return a mismatch status, so inspect the values. Capture the complete command output.
For the fault proof, map the metadata Primary value to its Gitaly Pod and storage name. Run these
baseline commands and inspect their full output before opting into the fault script; stop if the
checksums do not match. The script validates equal checksums across the baseline report, retries
the same commit and branch for up to three minutes while recording each attempt, then polls
Praefect metadata for up to three minutes after unisolation until all three replicas are healthy
at the repository generation. It validates equal checksums within the recovered replica report;
it does not compare before/after reports because the proof commit deliberately changes the repo.
The selected chart renders StatefulSet gitlab-spike-gitaly-default with storage names ending in
ordinals 0, 1, and 2, but verify the live Pod and metadata rather than assuming an ordinal. The
harness requires the supplied Pod name and storage name to match exactly.

The failure policy is a generated, dormant CiliumNetworkPolicy named
`gitlab-spike-fault-isolation`. It selects only Gitaly Pods labeled `app=gitaly`,
`release=gitlab-spike`, and `gitlab-spike.ducktape.io/fault=isolated`, and denies all ingress
and egress. The script validates this policy, the three-ready-replica StatefulSet, the exact
Praefect primary, the healthy baseline, and successful read-only TCP connections from the
Toolbox pod to the primary and one healthy peer's `grpc-gitaly` ports. It also requires ready
Kubernetes CiliumEndpoint records and read access to each Pod's node-local Cilium agent status
before touching the label. Immediately before adding the label it fetches the Pod again and sends
a JSON Patch that tests both the captured Pod UID and that read's `resourceVersion`; a
replacement or concurrent Pod update makes the patch fail without changing labels.

After labeling, the script waits up to 30 seconds for the Kubernetes CiliumEndpoint to retain the
captured Pod UID and for `cilium-dbg endpoint get` on that Pod's node-local agent to report the
isolated identity label, `ready` state, and realized ingress plus egress policy enforcement. It
then requires a new primary TCP connect to time out while a new connection to the healthy peer
still succeeds, before any Git push is attempted. Both probes use the Toolbox's Ruby socket
library and make no application writes. A missing status field or agent tool, an endpoint that is
not ready, a reachable primary port, an unreachable healthy peer, or any probe failure other
than the expected primary TCP timeout aborts the proof and restores the label. The
[Cilium Endpoint CRD](https://docs.cilium.io/en/stable/network/kubernetes/ciliumendpoint/)
mirrors the endpoint status from `cilium-dbg endpoint get`; Cilium's [endpoint lifecycle
documentation](https://docs.cilium.io/en/stable/security/policy/lifecycle/) defines `ready` as
successfully regenerated endpoint networking configuration. The gate still needs live
validation against the deployed Cilium version before the primary-failure proof is considered
available. The script removes the label on ordinary exit, interrupt, and termination; it never
deletes PVCs, drains nodes, or scales a StatefulSet.

The failure proof is opt-in. Prepare a rollback window and use the project-scoped Developer
credential in Secret gitlab-spike-proof (username and token); do not use the benchmark PAT,
Flux Maintainer credential, or an administrator token. The project is deliberately disposable.
The test leaves its uniquely named proof branch and commit in spike/acceptance for review. Run:

```bash
export GIT_REMOTE_URL=http://gitlab-spike-webservice-default.gitlab-spike.svc:8181/spike/acceptance.git
read -r -p 'Metadata primary Gitaly Pod: ' GITALY_PRIMARY_POD
read -r -p 'Matching Praefect Primary storage name: ' PRAEFECT_PRIMARY_STORAGE
read -r -p 'Ready Praefect Pod: ' PRAEFECT_POD
read -r -p 'Ready Toolbox Pod: ' TOOLBOX_POD
read -r -p 'GitLab project ID: ' GITLAB_PROJECT_ID
read -r -p 'Praefect relative path: ' PRAEFECT_RELATIVE_PATH
read -r -p 'Project token username: ' GIT_AUTH_USER
read -r -s -p 'Project token: ' GIT_AUTH_TOKEN; printf '\n'
export GITALY_PRIMARY_POD PRAEFECT_PRIMARY_STORAGE PRAEFECT_POD TOOLBOX_POD
export GITLAB_PROJECT_ID PRAEFECT_RELATIVE_PATH GIT_AUTH_USER GIT_AUTH_TOKEN
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
ALLOW_GITLAB_SPIKE_FAILURE=YES BENCH_TARGET=spike/acceptance \
  EVIDENCE_DIR=/tmp/gitlab-spike-primary-failure-$RUN_ID \
  ./cluster/x/gitlab_spike/scripts/inject-primary-failure.sh
unset GIT_AUTH_USER GIT_AUTH_TOKEN
```

Once the Cilium status, isolated-primary timeout, and healthy-peer positive-control gates pass,
the script confirms the isolation selector matches only the target primary and pushes one new
commit to a unique branch through GitLab. It
retries the same commit and branch for up to three minutes, recording each attempt and the elapsed
time to the first acknowledgement. It then checks the exact branch SHA is readable through
GitLab during the fault, runs client Git fsck, and polls Praefect metadata for a different primary.
After removing the label it polls for all three replicas to reach the repository generation,
checks that the recovered replica checksums agree, verifies the old refs are unchanged and the
acknowledged branch still points to its exact SHA, then clones and runs fsck again. If an
enforcement precondition fails, the script aborts before pushing. If no push is acknowledged
within the retry window or a check fails, the run is a failed proof; it does not claim
acknowledged write durability. Review the captured metadata, endpoint status, probe results, and
checksum outputs as well as the summary.

One acknowledged push in one isolated repository is evidence for that run only. It does not
establish a general RPO of zero or characterize failures outside this exact primary-isolation
case. Do not run the script if the baseline is unhealthy, the primary mapping is ambiguous, the
policy is absent, or the target is not the named spike Gitaly StatefulSet. The harness is never
run by CI. GitLab documents Praefect primary election as lazy, triggered by a request after the
current primary becomes unhealthy. See the [Praefect troubleshooting guide](https://docs.gitlab.com/administration/gitaly/praefect/troubleshooting/)
and [replica checksum task](https://docs.gitlab.com/administration/raketasks/praefect/).

The Praefect chart is documented as experimental, and Gitaly Cluster on Kubernetes is
documented as beta. That limits the meaning of a green spike result. See GitLab's
[Praefect chart status](https://docs.gitlab.com/charts/charts/gitlab/praefect/) and
[Gitaly Cluster limitations](https://docs.gitlab.com/administration/gitaly/praefect/).

## Backup and restore runbook

This path is **not yet rehearsed**. A configuration or successful backup job is not restore
proof. Rehearse only on this existing disposable spike while it contains synthetic fixtures;
this runbook does not provision a second restore target or authorize restoring production data.
Before restore, preserve the backup archive, Rails secrets, a matching CNPG recovery point, and a
complete integrity baseline for the seeded project. Quiesce GitLab writes and CI, pause the
spike's Terraform reconciliation, and suspend the owning Flux reconciliation so bootstrap or
operators cannot rewrite state during restore. Resume them only after restore validation.

1. Confirm that the chart Toolbox is ready and backup object storage and its temporary bucket
   are configured. The GitLab chart's official entry point is `backup-utility`; it creates a
   `<backup_ID>_gitlab_backup.tar` in the configured `gitlab-backups` bucket:

   ```bash
   kubectl -n gitlab-spike get pods -l release=gitlab-spike,app=toolbox
   TOOLBOX_POD=$(kubectl -n gitlab-spike get pods -l release=gitlab-spike,app=toolbox -o jsonpath='{.items[0].metadata.name}')
   kubectl -n gitlab-spike exec "$TOOLBOX_POD" -c toolbox -- backup-utility
   ```

2. Record the exact GitLab chart/app version and backup ID. Keep a separate encrypted copy of
   the Rails secrets and the shared CNPG cluster backup, which contains both the GitLab Rails
   and Praefect metadata databases. Verify the CNPG backup is complete and retain its timestamp
   and object-store location. The chart's `backup-utility` is not a substitute for backing up
   the Praefect metadata database.
3. Do not use a standalone snapshot of a single Gitaly PVC as a Praefect backup. GitLab warns
   that Praefect does not support snapshot backup and recovery because database metadata can
   diverge from repository disks. Preserve a coordinated Praefect database plus repository
   recovery point; GitLab recommends its official backup/restore Rake tasks. See
   [Praefect recovery guidance](https://docs.gitlab.com/administration/gitaly/praefect/) and
   [what to back up](https://docs.gitlab.com/administration/backup_restore/backup_gitlab/).
4. On the same spike, after capturing the baseline and stopping writes, follow the established
   CNPG restore procedure for the shared cluster containing both Rails and Praefect databases. Do not improvise
   an in-place CNPG data-directory replacement. Then follow GitLab's chart restore procedure:
   stop Sidekiq, Webservice, and Prometheus database clients; run
   `backup-utility --restore -t <backup_ID>`; restart those deployments at their recorded
   replica counts; and run GitLab's custom instance setup if the source was not a Helm
   installation. The shared CNPG recovery point restores both databases together; the subsequent GitLab
   archive restore still requires an operator to verify the exact GitLab/CNPG procedure.
5. Verify the seeded project's branch/tag refs, GitLab API access, Praefect `check` and
   per-replica checksums, LFS object fetch, artifact download, upload read, and registry pull
   by digest. Record gaps and restore time. A failed or partial restore is a failed gate.

The GitLab chart backup tool expects object storage configuration; the chart documentation
also notes that backup assembly needs local temporary disk and recommends a persistent
Toolbox volume when the archive exceeds available space. The official guide is
[Backup and restore a GitLab Helm chart instance](https://docs.gitlab.com/charts/backup-restore/),
with [restore steps](https://docs.gitlab.com/charts/backup-restore/restore/) and
[backup steps](https://docs.gitlab.com/charts/backup-restore/backup/). Do not treat independent
PVC snapshots plus a database backup as equivalent to this procedure.

## Replacement blockers to measure

Every row below is **not measured by this spike yet**. The repo evidence is a source for the
current workflow dependency, not proof of how GitLab behaves. Re-check the selected GitLab
version, license, and integration before treating any row as closed.

| Blocker                                     | Existing repo evidence                                                                                                                                                                                | Required proof                                                                                                                                                                                                                                                          |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pull mirroring may require Premium/Ultimate | Existing mirror and source-control paths are documented in `cluster/cdk8s/forgejo/app.py` and `tf/gitops/forgejo-agentydragon-repos/main.tf`.                                                         | Confirm current GitLab tier/entitlement and reproduce required pull-mirror update, refs, and pipeline-trigger behavior. GitLab currently documents pull mirroring as Premium/Ultimate: [repository mirroring](https://docs.gitlab.com/user/project/repository/mirror/). |
| AGit-style read-only `refs/for/*` PR flow   | `tf/gitops/forgejo-agentydragon-repos/main.tf` explicitly gives agents read-only upstream access and sends review changes through `refs/for/<branch>`.                                                | Demonstrate a least-privilege agent creating/updating a review through the chosen GitLab-compatible path without upstream branch write access.                                                                                                                          |
| Agent API Basic credential/header injection | `docs/personal_agents/credential_proxy.md` and `docs/personal_agents/findings/egress_and_tls.md` describe Basic credentials and injected request headers.                                             | Verify agent network policy, proxy policy, GitLab credential type, header injection, and revocation end-to-end with a spike-only identity.                                                                                                                              |
| Props registry proxy replacement            | `props/README.md` says the registry-proxy revival scaffolding is retired and its Forgejo tenant wiring must be restored deliberately; `props/registry_proxy/` implements props-specific OCI behavior. | Show image push/pull, auth isolation, tag-to-agent-definition behavior, and data retention with the GitLab registry or a separately retained props proxy.                                                                                                               |
| Private Flux Git source                     | Existing GitOps sources and credential wiring live under `cluster/cdk8s/ducktape_flux.py` and generated Flux source manifests.                                                                        | Reconcile the private `spike/acceptance` source using the intended GitLab credential and prove commit/revision, path, and auth rotation behavior.                                                                                                                       |
| Registry scanning and webhooks              | `cluster/validation/image_automation.py` requires registry image automation to be connected to an appropriate Flux Receiver.                                                                          | Prove authenticated tag discovery, image-policy selection, push-triggered reconciliation, image commits, and webhook retry behavior.                                                                                                                                    |
| LFS object storage and restore              | GitLab chart configuration here enables a dedicated LFS bucket; current repo behavior is not an interoperability result.                                                                              | Push/fetch a disposable LFS object, prove it is in the intended object store, then restore and fetch it by OID.                                                                                                                                                         |
| SSH branch protections                      | Forgejo agent roles and source push conventions are documented in `tf/gitops/forgejo-agentydragon-repos/main.tf`.                                                                                     | Verify GitLab SSH clone/push, protected-branch enforcement, service-user restrictions, and AGit replacement flow over the configured SSH route.                                                                                                                         |

This spike does not close migration, cost, license, or policy decisions. Keep production repos,
workflows, Flux sources, and credentials on their current owners until the relevant blocker has
direct evidence and a separately reviewed cutover plan.
