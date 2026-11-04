# Forgejo to GitLab migration: initial preflight

Snapshot: 2026-11-04 05:40–06:04 UTC. This is a point-in-time preflight for the
[migration plan](../../docs/plans/forgejo_to_gitlab_migration.md), not a cutover approval. Counts and object names can
change. Production Forgejo repositories, registry objects and workloads were not modified. Private inventory details and
credentials are intentionally omitted.

## Source inventory

- An authenticated Forgejo admin `GET /api/v1/repos/search` with pagination returned **nine private repositories**, one
  flagged as a mirror, with no public repositories returned. `git ls-remote --refs` succeeded for all nine using the
  existing admin credential. The advertised refs comprised **1,194 branch refs, 3,402 tag refs and 14,103 `refs/pull/*`
  refs**. These are Git ref counts, not counts of unique commits. Summed across repositories, 13,342 distinct pull-ref
  tip IDs did not equal an advertised branch or tag object ID in the same repository. This is not a reachability test,
  and annotated tags were not peeled. Pull-request history is outside the requested import scope, but its refs may
  retain otherwise unreachable objects; preserve them in a source backup and assess reachability before deleting the
  source.
- Reading the root `.gitattributes` on each default branch found one repository with `filter=lfs` patterns, three with a
  root file without those patterns, and five with no root file. No root `.lfsconfig` was found. This does not inspect
  nested attributes, non-default refs or the actual LFS object inventory.
- These counts are an inventory snapshot, not a complete dependency map. LFS object sets, repository sizes, private ref
  contents, Git clients, external consumers and per-repo consistency still need their own inventory. The source API
  response and ref summaries used for this check were kept out of the PR.
- A live Kubernetes query found **69 Flux ImageRepository objects** referring to `git.allegedly.works`. Three Flux
  GitRepository objects used the internal Forgejo service: `ducktape-flux/finance-agent`, `flux-system/haku-state` and
  `haku-sandbox/haku-state-write`. These are not a complete list of direct Git or OCI consumers.
- The live `agentplane-app` Forgejo registry tag selected by Flux rejected an anonymous manifest request with HTTP 401.
  With the existing pull credential, its manifest was fetched by tag and digest; the bytes matched the advertised
  SHA-256, and the first blob's SHA-256 and length matched the manifest. This checks one image, not historical tags,
  other architectures or referrers.

## GitLab spike health and recovery

- GitLab `19.5.1-ee`, chart `10.5.1`, reports the private acceptance project available. Webservice, Sidekiq, Shell,
  registry and Toolbox deployments were Ready; Gitaly and Praefect were each **3/3 Ready**. The GitLab and Forgejo CNPG
  clusters reported healthy with **3/3** and **2/2** ready instances, respectively. The GitLab integration and demo Flux
  Kustomizations were Ready.
- Praefect's `check` passed its migration, connectivity, storage, database and repository availability checks. **This
  command writes and reads a small database check record** in the spike; it is not purely read-only. GitLab's
  `gitlab:praefect:replicas[1]` task reported matching checksums for all three replicas of the acceptance project. This
  is one repository and one point in time, not a failover or restore proof.
- All eight spike SeaweedFS Bucket resources reported Ready. The most recent completed CNPG `Backup` records observed
  were GitLab at `2026-11-04T01:33:23Z` and Forgejo at `2026-11-04T01:16:59Z`. These records establish neither coverage
  of every GitLab/Forgejo data class nor a successful restore. The GitLab backup attempt below had object-storage skips;
  restore and coordinated Praefect recovery remain unrehearsed; see the
  [spike runbook](../../x/gitlab_spike/README.md#backup-and-restore-runbook).

## Backup preflight

`backup-utility` ran in the existing Toolbox against the configured spike backup bucket. Its database and repository
dump steps completed. It then reported HTTP 403 when checking six bucket names, and said it was skipping the
corresponding categories: `external_diffs`, `terraform_state`, `pages`, `ci_secure_files`, `agent_plan_content`, and
`ci_catalog_bundles`. The spike declares separate LFS, artifacts, uploads, packages and registry buckets, plus backup
and temporary buckets; the skipped names are not among those declared. The run also warned that registry metadata DB and
OpenBao DB credential files were missing. The warnings alone do not establish that either optional database is enabled
or contains data.

The utility later logged that it had produced an archive. A separate S3 listing confirmed a 7,096,320-byte archive in
the spike backup bucket; its tar listing included a database dump, repository bundles and registry, uploads, artifacts
and LFS archives. The test wrapper used zsh's read-only `status` variable and failed to capture the utility's exit code,
so the log and the separate listing are the evidence for the archive, not a known successful exit. No restore was
attempted.

During the run, a Rails process was observed in Linux uninterruptible I/O sleep (`folio_wait_bit_common`). A 403 does
not distinguish an absent bucket from an identity that lacks access.

### Follow-up: effective feature configuration

A later read of the Toolbox's effective `gitlab.yml` found all six skipped categories had their main `enabled` flag set
to `false`. Pages' object-storage subsetting was enabled even though Pages itself was disabled. The backup log reported
that the enabled packages bucket was empty. The registry metadata database was explicitly disabled, and no OpenBao
Deployment or StatefulSet was found in the spike namespace. Recheck coverage before enabling any skipped feature; the
403s alone do not establish missing active data.

The operator chose not to rehearse a backup restore. The archive and CNPG backup records therefore do not establish
recoverability; retain that limitation in the migration decision.

## Runner-manager policy gap

At the initial preflight, the spike's `gitlab-spike-runner-manager` CiliumNetworkPolicy selected
`app=gitlab-spike-runner`, while the manager Pod had `app=gitlab-spike-runner-gitlab-runner`. The namespaced policy did
not select that Pod. At this snapshot there were no Kubernetes NetworkPolicies in the namespace and the two cluster-wide
Cilium policies selected Haku namespaces, not `gitlab-spike-ci`. The node-local `cilium-dbg endpoint get` for the
manager reported `policy-enabled: none` and unrestricted egress identity 0.

This established that the intended manager Cilium policy was not enforced at the time of the preflight; it is not a test
of every possible network path or proof of an exploitation. The runner-job policy has a separate selector.

PR [#14225](https://github.com/agentydragon/ducktape/pull/14225) later corrected the selector. After merge, Flux applied
it and Cilium reported egress enforcement on the live manager. A synthetic job then succeeded, and its test branch was
removed.

## Synthetic Git, LFS, SSH and CI tests

The following writes were confined to the already-disposable `spike/acceptance` project. Each test branch had a unique
name and was removed only after verifying its exact commit with a force-with-lease guard. Test commits and pipeline
records remain in GitLab's history, and the synthetic 8,200-byte LFS object may remain in object storage pending normal
cleanup. No GitLab object-store cleanup was run.

- A synthetic Git branch and 8,200-byte LFS object were pushed with CI skipped. A fresh clone with Git LFS installed
  locally checked out the exact commit and recovered byte-identical content; the test branch was removed. An earlier
  attempt checked out an LFS pointer because its fresh clone did not have LFS filters installed; the corrected attempt
  passed. This proves the isolated GitLab Git/LFS path, not migration of a real Forgejo LFS object.
- The public SSH service on port 2223 returned three host keys, all matching the spike's configured public host keys. A
  uniquely generated, temporary read-only deploy key could list the private acceptance repository's heads; a dry-run
  push was rejected. After removal of the exact deploy key, SSH access with that key was rejected. This does not prove
  the human OIDC user's existing SSH keys, Git push permissions or the Forgejo AGit replacement flow.
- A synthetic branch triggered pipeline **6** in `spike/acceptance`. Its single Kubernetes-runner job passed a check
  that the default Kubernetes service-account token file was absent. The exact branch was removed after completion. A
  second synthetic pipeline, **7**, reached the spike GitLab service, timed out connecting to the Kubernetes API and to
  `1.1.1.1:443`, and also confirmed the token file was absent. An external host could reach `1.1.1.1:443`, although that
  control does not share the job pod's network path. Both exact branches were removed. These probes cover specific paths
  and do not establish Haku's full production runner containment, scoped registry pushes, long-job behavior, or
  Dockerfile build compatibility.
- The GitLab spike's private published demo image rejected anonymous access with HTTP 401. An authenticated fetch
  retrieved the same manifest by tag and digest; its SHA-256 matched the registry header and the first blob's digest and
  length matched the manifest. This tests one existing image and read credential, not cross-registry copying or
  publication authorization.

No fault injection, production cutover, real private-data import, restore, production credential rotation or production
data deletion was attempted. The remaining evidence and sequencing gates are in the
[migration plan](../../docs/plans/forgejo_to_gitlab_migration.md).
