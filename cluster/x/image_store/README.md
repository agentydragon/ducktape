# Image store lifecycle: design probe for #11053

Status: experimental design proposal for [#11053](https://github.com/agentydragon/ducktape/issues/11053), 2026-10-18. Backend, retention policy, and deployment remain open decisions.

## Evidence and the useful boundary

The two one-off cleanups removed 4,432 published versions and 541.27 GiB of logical blobs. That required collecting references across four Git repositories and live Kubernetes resources, refreshing them between deletion batches, and accounting for concurrent moving tags. The report is merged under [`cluster/x/forgejo_retention_report/`](../forgejo_retention_report/README.md); it is deliberately read-only. Deleting registry blobs still leaves a separate SeaweedFS compaction backlog. These observations motivate a reconciler that owns availability and references, with registry-native blob accounting and storage-native reclamation.

Existing publication already uses `devinfra/ci/image_registry.py`, `.github/workflows/push-images.yml`, and `util/crane.py`. Preserve this integration unless a backend probe demonstrates a reason to replace it. `latest` currently answers a CI deduplication question; it cannot stand in for the set of deployment pins.

The existing Zot service is a pull-through cache, configured in `cluster/k8s/oci-cache/config.json`: pull-recency/count retention, untagged deletion, S3 storage, and on-demand imports. Its internal listener intentionally has no authentication for Docker mirror compatibility. An image can be required by a dormant workload without being pulled recently. Neither this retention policy nor that authorization boundary is sufficient for a durable private-image store without changes.

## Target behavior

Identify content by registry/repository plus immutable digest. Record the tag-to-digest resolution used by a desired revision; a moving alias does not replace protection of the previously observed live digest. Track why each digest is needed, the source revision/resource UID, scan generation, and last successful observation.

Reference roots include desired Git heads, Flux's applied revisions when different, currently live workload/image IDs, dormant and scaled-to-zero templates, CronJobs, VM container disks, sandbox templates, explicit external pins, and the agreed rollback roots. Enumerate supported kinds and source repositories explicitly. Unsupported or unreadable declared roots block deletion in the affected scope. Losing a root is not evidence that its images are unused.

A candidate lifecycle is:

`publication lease → imported and verified → desired/live/rollback protection → unreferenced observation → grace period → revalidated deletion → registry blob GC → storage compaction`

A publication lease is acquired before upload starts and stays valid until the desired reference is discoverable or publication is abandoned. A deployable pin is published only after the destination has the full required manifest graph, not just a tag. Import every platform child of an index and preserve the signatures, SBOMs and other OCI artifacts chosen by policy. Validate subject/referrer reachability and deletion behavior with the actual backend; do not assume a tag is a universal protection mechanism.

No new layer store or hand-maintained layer reference counts: use the registry's own manifest/blob ownership. Maintain image-level intent and availability status outside it.

## Publication versus deletion: the key invariant

A final rescan alone cannot close the race between a new pin and deletion. Managed publishers and pin promotion must participate in a common lease/fencing protocol with the collector. Once a digest is selected for deletion, either protection wins and deletion is cancelled, or deletion wins and promotion must wait for verified republishing. A retry after an ambiguous delete re-reads backend state before proceeding.

Git merges and manual writers that bypass this protocol cannot be made atomic by watching them faster. Until those writers are integrated, retain their namespaces/digests or use an explicitly bounded guarantee with a conservative grace period and fresh availability checks. A finite grace period reduces risk; it is not a proof against arbitrary bypass writers.

Keep newly missing references through complete healthy scan generations and the configured grace period. A failed scan does not age them toward deletion. Explicitly distinguish a deliberately removed root from a failed fetch or an empty result. Define whether an expired lease can be collected only after publication status is resolved.

## Backend alternatives

| Option                         | Reuses                                                           | Additional work / decision gate                                                                                                                                                                                                                                                |
| ------------------------------ | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Forgejo + lifecycle reconciler | Current CI, credentials, API deletion, shared-blob accounting    | Reconcile protect/delete intent; verify indexes, OCI referrers, concurrent upload/GC, and API ownership. Bound admin blob-GC authority separately from package deletion.                                                                                                       |
| Dedicated durable Zot instance | Existing operational knowledge, OCI import/mirroring support, S3 | Configure authenticated writers/readers and deletion authority; establish native retention behavior that cannot override protection. Verify artifact graph and concurrent GC. Adds another managed registry but can separate the private store from disposable cache behavior. |
| Extend the current Zot service | Existing endpoint, deployment and bucket                         | First prove that protected durable content can be isolated from cache eviction and that private repositories cannot leak through the unauthenticated internal listener. If either fails, stop this option rather than weakening private-image requirements.                    |
| Other dedicated registry       | Potential native policy/protection/replication features          | Evaluate only if the first two probes expose a concrete missing capability. Migration alone does not solve Flux reference discovery or publication races.                                                                                                                      |

Suggested order: probe Forgejo first because it avoids moving working publication and credentials. Run the dedicated Zot probe if Forgejo cannot meet artifact or concurrency requirements with a small adapter. This is an investigation order, not a backend decision.

Native age/newest-N policies may govern an explicitly disposable publication area. They must not independently delete from the protected durable area. A protection-tag scheme is only a candidate implementation: prove that it keeps all relevant aliases, platform children, and artifacts and that every cleanup path honors it.

## Policy and ownership decisions still needed

- Rollback roots/window, publication grace, treatment of old ReplicaSets/Jobs, and explicit long-lived external pins. The one-day window used today was a one-off choice.
- Promotion boundary: before Git merge, through Flux image automation, or another owner. Identify every pin writer before enabling deletion.
- Reference scope across repositories/environments and who acknowledges retirement of a source.
- Explicit destination references versus runtime mirror rewriting. Preserve digest identity and specify tag resolution if a mirror is chosen.
- Pull, push, delete and administration identities. GitOps Terraform continues owning Forgejo tokens; no manually minted production credentials.
- Bootstrap/outage behavior: keep the registry and its storage/operator dependencies independently pullable; decide which bootstrap images need an external or offline fallback. A store backed by the same cluster is not an independent backup.

## Cheap probe and rollout

1. In a disposable repository, upload a multi-platform image plus a selected referrer. Protect an old pin, a dormant workload digest and a currently live older digest. Delete an unprotected alias/version that shares a blob. Confirm every protected manifest and layer remains pullable with fresh credentials and an empty content cache.
2. Exercise a new pin concurrent with candidate deletion, a paused/incomplete source scan, a publisher crash, and a retry after an ambiguous API response. Require the invariant above; do not accept “the timing happened to work.”
3. Compare Forgejo and an isolated Zot instance against these same cases. Inspect actual API/native-GC semantics, not a feature matrix alone.
4. Initially reconcile reports and missing-image availability only. Then enable guarded import/promotion. Enable deletion for one declared owner/repository after full reference coverage is established. Keep a kill switch that stops deletions without stopping imports or pulls.

Acceptance is a successful fresh pull after GC for all protected cases, correct shared-blob retention, a stale-source deletion block, and a demonstrated publication/deletion ordering. This document adds no implementation or test targets.

## Observability and recovery

Report last complete source generation and age, protected reasons, missing required digests, lease state, candidates and deletion results. Track logical registry bytes separately from SeaweedFS disk free space, deleted bytes, vacuum progress and read-only state. Alert on required-image unavailability and prolonged reclaimable-garbage accumulation under disk pressure, rather than treating a successful registry-GC job as physical reclamation.

Rollback disables deletion and retains the previous registry/reference path. Before any backend migration, copy and verify protected graphs, switch a small set of explicit references, prove fresh pulls, and retain the old backend through the selected rollback period. A migration must not prune stateful GitOps ownership as part of the same unrehearsed change.

## Sources

- [Image store lifecycle issue #11053](https://github.com/agentydragon/ducktape/issues/11053)
- Local: `cluster/x/forgejo_retention_report/README.md`, `devinfra/ci/image_registry.py`, `.github/workflows/push-images.yml`, `util/crane.py`, `cluster/k8s/oci-cache/config.json`, `cluster/k8s/oci-cache/README.md`, `cluster/cdk8s/oci_cache/zot.py`.
- [Forgejo container registry](https://forgejo.org/docs/latest/user/packages/container/)
- [Zot configuration, storage, and GC](https://zotregistry.dev/v2.1.0/admin-guide/admin-configuration/)
- [Distribution GC and write ordering](https://distribution.github.io/distribution/about/garbage-collection/)
