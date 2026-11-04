# Forgejo migration source inventory

Snapshot: 2026-11-04 UTC. This extends the [initial preflight](2026-11-03-migration-preflight.md). Reads covered the
Forgejo API, advertised Git refs, complete default-branch Git trees, the Forgejo PostgreSQL read replica, live Kubernetes
resources and the ducktape and gaffer-private configuration checkouts. Values can change while an inventory is collected.
Private ref names, object IDs, package versions and credential values are not published here.

## Git repositories and non-Git records

- The admin API returned nine private repositories. `git ls-remote --refs` succeeded for all nine: 1,194 branch refs,
  3,402 tag refs and 14,103 pull-request refs. Full ref names and IDs were captured privately for comparison; this is
  not proof that pull-request-only objects are reachable from branches or tags. Preserve those refs with the source.
- All nine default-branch trees were read at their advertised commit IDs, including nested attribute files. One
  repository has LFS patterns. Forgejo's database records ten LFS objects for that repository, 295,926,233 bytes in
  total; the other eight have no LFS metadata rows. All ten responded to an LFS download range request for one byte.
  This is not a full-object checksum, a migration, or an exhaustive analysis of non-default-branch LFS pointers.
- The API identifies one GitHub pull mirror, `haku/ducktape`, with an eight-hour interval. The separate
  `agentydragon/ducktape` copy is not flagged as a mirror. Determine the desired disposition and update schedule for
  both; do not assume both are active mirrors or that GitLab Free provides an equivalent native pull mirror.
- The database records 185 pull requests across four repositories and no issue records in these repositories. The
  mirror's 3,402 release-table records are tag-only records, not Forgejo releases. There are no attachment rows. Haku's
  repository has 344 CI artifact metadata rows, with 76,021,967 compressed bytes recorded. These counts are metadata,
  not proof all referenced bytes are still retrievable; historical issue/PR import is outside scope, but preserve data
  until its retention disposition is decided.
- Three Forgejo workflow files occur in the default branch of the Haku state repository. API run histories exist for
  three other repositories as well, including waiting/cancelled runs; observed history does not prove an active runner
  or that their `.github` workflows are needed in GitLab. Check non-default branches and actual producers before
  selecting workflows to port. Haku has one configured branch-protection rule and four Actions secret names. Across the
  inventoried repositories there are two repository webhooks: one Haku push/package hook and one finance push hook.
  Secret values were not read from Forgejo.

## Registry and live consumers

- A read-replica snapshot had **129 container package names**: 125 under the CI image account and four under Haku.
  It contained **4,015 published package-version records** (3,621 and 394 respectively); four records were digest-form
  versions. Each published version had a lead manifest metadata row. An additional 14 internal `_upload` records did
  not have a manifest and are not published versions. Counts are point-in-time, not a guarantee that every tag, blob,
  architecture or referrer can be pulled. A private snapshot maps versions to lead-manifest digests; it must be
  refreshed before final copy and compared against actual destination registry responses. For one live selected CI
  image, the source manifest bytes and registry digest header matched the database snapshot's SHA-256.
- The 69 live Forgejo-backed Flux ImageRepositories refer to package names present in that snapshot: 66 in the CI
  account and three in Haku. These are scan objects, not 69 distinct workloads or an exhaustive image inventory.
- At this snapshot, 60 live workload templates reference Forgejo images: 46 Deployments, one StatefulSet and 13
  CronJobs across 30 namespaces, with 55 distinct image repository paths. This excludes Jobs, naked Pods, scaled-to-zero
  or parked configurations, node/VM consumers, historical rollback manifests and outside-cluster clients. The private
  inventory records their current image and pull-secret references; no secret values are included.
- The Forgejo registry pull credential is distributed by ExternalSecrets in many namespaces. The main CI image
  credential and Haku's registry credential have distinct owners and scopes. Preserve namespace-local pull access,
  including cross-repository consumers in `gaffer-private`, before redirecting image scans, publishers or workloads.
- Three live Flux GitRepository objects read Forgejo: the finance source in `ducktape-flux`, and the Haku state read
  and write sources in `flux-system` and `haku-sandbox`. The root Flux GitOps source remains GitHub. Haku and finance
  repository webhooks, the registry package webhook, ImagePolicies, image automation and their credential stores must
  move with their corresponding consumers, not merely with DNS.

## Ownership and cutover boundaries

- The `forgejo` Flux Kustomization owns the two-replica Forgejo application, its shared Git PVC and related
  configuration. The separate `forgejo-images` and `flux-image-automation-forgejo` units own registry credential and
  scan plumbing. Forgejo's S3 bucket contains LFS, packages, attachments and artifacts; the separate database-backup
  bucket and the Git PVC are distinct data sources. Both Bucket resources reported Ready and `Retain` at this snapshot.
- Crossplane OpenTofu Workspaces own the active `haku-state`, `budget-ledger`, `cpap-data`, `finance-agent`,
  `forgejo-agentydragon`, `forgejo-agentydragon-repos`, `forgejo-claude` and `forgejo-images` roots. They manage the
  relevant users, repositories, memberships, SSH keys, secrets and webhooks. `tf/gitops/augur-evidence` exists in the
  parked configuration; the private checkout has a separate `tf/thrive-scrape` owner. Preserve state and do not remove
  old owners before their resources are safely handed off.
- `forgejo-token-rotation` mints agent credentials; Haku's runner uses the Forgejo registration and queue APIs, and
  Haku UI/Anki and the agent egress layer use Forgejo-specific Git or API paths. Credentials, source URLs, review
  permissions, Haku's allowed direct writes and registry publication need separate destination mappings and tests.
  The private checkout also contains live Forgejo image references and a separately owned service repository; changes
  there cannot be assumed to follow from a ducktape-only PR.

## Remaining boundaries

This completes the enumerations above, not an assertion that every consumer everywhere is known. Before each cutover,
refresh refs, LFS IDs and registry tags/digests; check actual LFS content and copied registry manifests/blobs, required
architectures and signatures; inventory non-default-branch LFS pointers where relevant; identify dormant and
off-cluster clients and writers; and assign a disposition to artifacts and other non-Git data. Trace and fence each
writer, verify destination permissions, and compare final inventories under a bounded freeze. Repository identity,
credential and image details are held in a private local inventory rather than committed to this public repository.
