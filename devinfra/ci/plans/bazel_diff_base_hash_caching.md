# Cache `bazel-diff` base-commit hashes across PR runs

## Why

`bazel-ci.yml`'s PR gating regenerates the merge-base's Merkle hashes on every
PR CI run — `git checkout $BASE && bazel-diff generate-hashes -w "$PWD" -b bazel`.
The base is always a stable `devel` commit; for a given SHA its hashes never
change. Measured in the shadow-mode rollout: ~25–35 s per hash-generation call
on the bb-remote runner (warm snapshot). That's ~30–60 s of redundant work
per PR CI run, plus a `git checkout` + query of the whole target graph.

## What

Precompute hashes on `devel` push, publish them keyed by SHA, and have the PR
gating fetch them before falling back to regeneration. PR CI then only pays
the HEAD-hash cost — one query, no base checkout.

## The transport problem

Where do the hashes live between the devel-push producer and the PR CI consumer?

`bazel-ci.yml` runs the `bb-remote` action with `--run_from_commit`, which
bypasses patchset sync and checks out the exact commit inside a fresh
Firecracker VM. The GHA runner's workspace does **not** propagate into the
VM. So a file the GHA runner restores via `actions/cache` can't be handed
directly to the bb-remote script — the VM has to fetch it itself.

Three plausible transports:

### A. GHA `actions/cache`, delivered via a plain HTTPS URL

- **Producer** (devel-push workflow): `actions/cache/save` with key
  `bazel-diff-hashes-<sha>`. Value: the JSON blob.
- **Consumer** (PR CI): `actions/cache/restore` on the GHA runner side,
  then serve the restored file over the local network to the bb-remote VM.

Blocker: bb-remote VMs don't share the GHA runner's network namespace, and
the actions/cache HTTP endpoints aren't reachable outside the runner.
Would need a local HTTP shim on the GHA runner + reachable-from-VM address.
Fragile.

### B. Git tag / branch, delivered via `git fetch` in the VM

- **Producer** (devel-push workflow): after generating hashes, push a git
  tag `refs/tags/bazel-diff-hashes/<sha>` pointing to a synthetic blob-only
  commit that contains just `hashes.json`. Uses `ducktape-automation` App
  token (same pattern as `sync-pins.yml` / `pin-digests`).
- **Consumer** (bb-remote script): `git fetch --depth=1 origin
refs/tags/bazel-diff-hashes/$BASE` and `git show tag:hashes.json > /tmp/bd-cache/base.json`.

Pros: uses existing git infrastructure, no new secrets, works inside
`run_from_commit` because the VM has `git`. Cons: tag namespace grows
unbounded — needs a scheduled prune job (e.g. keep last 500 tags).

### C. SeaweedFS with a write-capable service account

- **Producer**: `aws s3 cp hashes.json s3://claude-ci-cache/bazel-diff-hashes/<sha>.json`
  using a new SOPS-encrypted write key.
- **Consumer** (bb-remote script): `aws s3 cp` back down, using the same
  key or a read-only mirror.

Pros: cleanest object-store semantics, natural TTL/LRU eviction. Cons:
new credential + SOPS rotation surface; SeaweedFS is Vallejo-only (not
web-session-friendly today, per the recent auth-required notices), so
the CI-side credential path needs to work independently of user session
state.

### D. BuildBuddy CAS (bytestream)

- **Producer**: upload the file to BuildBuddy's CAS keyed on its own
  content hash, then record `<sha> → digest` in a small side index.
- **Consumer**: fetch the digest from the index and byte-stream the file.

Pros: reuses BB infra that we're already paying for. Cons: the index
still needs a transport (probably git-tag anyway), and CAS content-addressing
means we can't skip the "content of hashes" call — we still need one
round-trip to look up the digest. Net: mostly reduces to (B) with extra
steps.

## Recommendation

**Option (B), git tag as transport.** It composes with the existing
`ducktape-automation` machinery and needs no new secrets, no extra services,
no shim layers. The only cost is a namespace-prune cron — trivial.

## Sketch — Option B

**New workflow** `.github/workflows/bazel-diff-hashes.yml`:

```yaml
name: bazel-diff hashes
on:
  push:
    branches: [devel]
concurrency:
  group: bazel-diff-hashes-${{ github.ref }}
  cancel-in-progress: false # each SHA gets its own tag; don't cancel
permissions:
  contents: write
jobs:
  precompute:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 1
      - uses: ./.github/actions/setup-nix-devtools
        with:
          package: citools # provides bazel-diff, bazelisk
      - uses: ./.github/actions/mint-automation-token
        id: app
        with:
          sops_age_key: ${{ secrets.SOPS_AGE_KEY }}
      - uses: ./.github/actions/bb-remote
        with:
          run_from_commit: ${{ github.sha }}
          script: |
            set -euo pipefail
            mkdir -p /tmp/bd-out
            bazel-diff generate-hashes -w "$PWD" -b bazel /tmp/bd-out/hashes.json
            # Ship the file back to the GHA runner via git — commit + tag inside
            # the VM's workspace, then push the tag to origin using the automation
            # App token forwarded through env.
            git config user.name  "ducktape-automation[bot]"
            git config user.email "ducktape-automation[bot]@users.noreply.github.com"
            cp /tmp/bd-out/hashes.json /tmp/bazel-diff-hashes.json
            BLOB=$(git hash-object -w /tmp/bazel-diff-hashes.json)
            TREE=$(printf '100644 blob %s\thashes.json\n' "$BLOB" | git mktree)
            COMMIT=$(git commit-tree -m "bazel-diff hashes for ${GITHUB_SHA}" "$TREE")
            git tag "bazel-diff-hashes/${GITHUB_SHA}" "$COMMIT"
            git push "https://x-access-token:${APP_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
              "refs/tags/bazel-diff-hashes/${GITHUB_SHA}"
        env:
          APP_TOKEN: ${{ steps.app.outputs.token }}
```

**Modified `bazel-ci.yml`** (inside the existing PR gating branch, before the
current `bazel-diff generate-hashes` for `$BASE`):

```bash
if git fetch --depth=1 origin "refs/tags/bazel-diff-hashes/$BASE" 2>/dev/null; then
  git show "refs/tags/bazel-diff-hashes/$BASE:hashes.json" > /tmp/bd-cache/base.json
  echo "base hashes: cache hit for $BASE"
else
  echo "base hashes: cache miss for $BASE — regenerating"
  git -c advice.detachedHead=false checkout --quiet "$BASE"
  bazel-diff generate-hashes -w "$PWD" -b bazel /tmp/bd-cache/base.json
  git -c advice.detachedHead=false checkout --quiet "$HEAD_SHA"
fi
```

**Cleanup cron** — a scheduled workflow that keeps the newest ~500
`bazel-diff-hashes/*` tags and deletes the rest. Trivial; can be filed as a
separate follow-up once the main workflow lands.

## Non-goals

- Not trying to cache the HEAD hashes — HEAD moves on every push, no
  reuse to be had.
- Not trying to cache the `get-impacted-targets` diff output itself — it's
  seconds; the win comes from skipping `generate-hashes` for the base.

## Open questions

1. Is the extra ~1 s from `git fetch` for the tag amortized by skipping
   the ~30 s hash-gen? Trivially yes on a cache hit; on a miss we pay
   the fetch + regen (same as today).
2. Should we key the tag on `<sha> + MODULE.bazel.lock hash` to invalidate
   on lockfile changes? The Merkle hashes bazel-diff produces are already
   inputs-hash-based — an unrelated lockfile change would produce a different
   `hashes.json`, so the tag content is safe. Only concern is if we run
   the precompute against `devel` before its RBE image bump lands — the
   next PR CI would still cache-hit and use stale hashes. Mitigated by the
   fact that the precompute workflow triggers on `push: devel`, running
   after the image bump commits.
