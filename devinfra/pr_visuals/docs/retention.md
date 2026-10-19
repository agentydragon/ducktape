# PR visual retention planning

`//devinfra/pr_visuals:retention_bin` builds a read-only report from S3-compatible
storage and GitHub. The planner has no write, tagging, lifecycle, or deletion path.
It preserves every baseline pointer, and groups stored objects under both
`commits/<sha>/` (legacy) and `reviews/<sha>/` (current) bundle layouts.

Run the planner with the normal AWS environment variables and a GitHub token that can
read pull requests and Actions:

```bash
bb run //devinfra/pr_visuals:retention_bin -- \
  --endpoint https://s3.example.invalid \
  --bucket pr-visuals \
  --repository agentydragon/ducktape \
  --observed-at 2026-10-19T04:00:00Z \
  --cutoff 2026-10-16T04:00:00Z > retention-plan.json
```

`--endpoint`, `--bucket`, and `--repository` are required. `GITHUB_TOKEN` is read from
the environment. AWS credentials and region use the standard AWS environment and SDK
chain. If omitted, `--observed-at` defaults to the current UTC time and `--cutoff` is
three days earlier. Supply both timestamps to hold the age window fixed across runs.
Reads remain live, so this does not reproduce a past bucket or GitHub snapshot. The
CLI uses S3 List/Get and GitHub pull request, compare, and workflow-run reads. It
requests 10,000-key listing pages, as supported by SeaweedFS 4.46, and reads metadata
with at most eight concurrent requests. Progress goes to stderr; stdout is JSON.

The JSON report includes exact object keys for candidate bundles and shared PNGs,
candidate and retained object counts and bytes, the preserved pointers, unknown keys,
unpublished GitHub or metadata dependency SHAs, and any blocker. Objects at or newer
than the cutoff are retained. Open PR heads, GitHub's actual compare merge bases,
recently closed heads from the previous eight hours, and active Actions heads protect
matching bundles when those bundles have been published. Recorded `base_sha` and
`merge_base_sha` references are followed transitively when their bundles exist; a
missing provenance-only bundle is reported, since not every commit has visual output.
Every retained bundle's aggregate and available target metadata are read to retain
shared assets named by `candidate_sha256`, `baseline_sha256`, or `diff_sha256`.

Unknown root keys stay retained. An unrecognized key under `commits/` or `reviews/`, a
broken baseline pointer, missing metadata for a retained bundle, malformed retained
metadata, or an absent shared image named by retained metadata blocks all candidate
output. The report's `safe_to_delete_now` and `shared_assets.safe_to_delete_now` fields
are always false. A plan describes one listing and can be stale as soon as it is
printed.

## Publisher race and decision gate

The simplest starting policy is to expire eligible review bundles while retaining
the deduplicated image pool indefinitely. This avoids the image-reuse race entirely.
Measure unique-image growth before adding shared-image collection; repeated copies
no longer drive that pool's size. The planner's image candidates can inform that
decision without turning them into deletion requests.

Three-day age grace does not make shared-image collection safe. The publisher checks
whether `assets/<sha256>.png` exists and reuses an old object. A collector can observe
that old object as unreferenced after the check but before the new review metadata is
published. Deleting it in that interval breaks the in-flight review even though the
image's age exceeded the grace period.

Two deletion backends are worth evaluating once coordination is designed:

- A mark-and-sweep worker can recheck the protected-reference graph and delete exact
  object keys. It needs a bucket-wide coordination protocol that rejects stale fencing
  tokens on publication and collection operations. A publisher-side check before a
  write is insufficient if the lease can expire between the check and the write. A
  lease timeout by itself is insufficient because a paused writer can outlive its
  lease.
- A tagger can mark eligible objects and let SeaweedFS S3 Lifecycle expire objects
  selected by age and tag. SeaweedFS documents [tag filters and expiration
  rules](https://github.com/seaweedfs/seaweedfs/wiki/S3-Lifecycle), and the deployed
  4.46 S3 handlers support the API. The tagger still needs the same reference graph,
  and publishers need coordination before reusing an eligible image. A tag without
  fencing or an in-flight publication root recreates the same race. Before relying on
  this option, verify that lifecycle enforcement is configured and running for this
  bucket; API support alone does not prove that a worker applies expiry. Object Lock
  retention is a minimum deletion hold, not an expiration mechanism.

Durable publication intentions could keep all images a publisher may reference rooted
from before its first object lookup through manifest publication. The publisher would
remove the intention only after the manifest is committed; abandoned intentions can
retain data until separately reviewed. A repo-wide GitHub Actions concurrency group
alone is not a coordination protocol: GitHub permits one pending run per group, and a
newer pending run can replace an older one.

The decision gate is to select one protocol, verify its controller and worker ownership,
include every deployed publisher version, and pass an interleaving test where a publisher
reuses an old image while collection is in progress. Until then the shared-image list is
informational. This planner does not write tags or configure lifecycle, and it does not
authorize deletion.
