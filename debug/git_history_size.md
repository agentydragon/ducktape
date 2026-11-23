# Git History Size Analysis

## Update: 2026-11-23

Current tree measured at `origin/devel` commit `7efcea7d9f8b162d783195aa49ab2ddf13868cc6`. Raw file sizes are path-weighted, so copied files count more than once; they are not clone transfer sizes.

| Current tree path | Raw size | Files |
| --- | ---: | ---: |
| `props/specimens` | 105.57 MiB | 15,539 |
| `devinfra/js` | 19.85 MiB | 2,125 |
| `devinfra/claude` | 15.72 MiB | 431 |
| `agentplane/system_tests` | 13.78 MiB | 469 |
| `website/static` | 11.65 MiB | 32 |

Measured using `git ls-tree -rl HEAD` in a fresh shallow checkout, summing blob sizes by path. `props/specimens` remains the largest checkout cost; its size alone does not establish potential history or transfer savings.

The source checkout is a partial clone: `GIT_NO_LAZY_FETCH=1 git rev-list --objects --missing=print origin/devel` reported 31,365 missing objects. Its 649.76 MiB of packed objects and 1.58 GiB of temporary pack garbage describe that local store, not a full clone or reclaimable remote history. No files were removed.

The June analysis below is retained as historical evidence, **not a current rewrite plan**. Its object sizes, path existence, dependencies, workflow references and savings estimates have not been revalidated. Rewriting published history affects branches, PRs, old links and pinned commits even when files are absent from the current tree; do not treat “low-risk” below as an assessment of that operation. Before considering a rewrite, measure a complete fresh mirror, inventory all remote and protected refs, validate current consumers and recovery, and compare the benefit with the disruption. Prioritize current checkout costs and preventing new growth separately.

## Historical analysis: 2026-06-14

Date: 2026-06-14

Scope: full reachable Git history for this repository, not just the working tree.
The main baseline is `origin/*` plus tags, which is what a normal full clone of
the remote sees. I also checked all local refs in this checkout; they add less
than 1 MiB of reachable object storage, so they do not materially change the
findings.

## Summary

Git's reachable-object estimate:

| Scope | Reachable object disk usage |
| --- | ---: |
| `origin/*` plus tags | 392.75 MiB |
| all refs in this checkout | 393.62 MiB |

Local object-store check:

| Check | Value |
| --- | ---: |
| `.git/objects` on disk | 479 MiB |
| `git count-objects -vH` packed size | 455.22 MiB |
| `git count-objects -vH` loose size | 21.80 MiB |

The difference between reachable-object usage and `.git/objects` size is normal:
this checkout has multiple packs, pack overhead, and some loose/prune-packable
objects.

There are no tracked Git LFS files in this repo at this point:

- `git lfs ls-files -l` returned no files.
- `.gitattributes` contains lint/format metadata, not LFS tracking rules.
- There is no local `.git/lfs` object store.

The biggest full-history costs are ordinary Git blobs:

- PNG visual goldens and screenshots.
- Gzipped captured binaries under Claude web environment reverse-engineering
  paths.
- Historical props specimen bundle files.
- A few notebooks, eval logs, dumps, and media files.

## Measurement Method

Commands used:

```bash
git count-objects -vH
git rev-list --disk-usage --objects --remotes=origin --tags
git rev-list --disk-usage --objects --all
git rev-list --objects --all
git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(objectsize:disk)'
```

Path attribution is based on historical blob paths from `git rev-list
--objects`. For component tables, the size column is Git's current on-disk object
size for each unique blob, not working-tree size.

For rewrite-candidate savings, I recomputed the estimate as "objects reachable
only through paths being removed", so duplicated blobs that also remain reachable
elsewhere are not counted as savings. This matters a lot for `props/specimens`,
which contains copied repo snapshots.

The current-tree baseline in this file is `origin/devel`, because
`refs/remotes/origin/HEAD` points to `origin/devel`. The local checkout happened
to be on `converge-spa-bundle` during analysis, so I did not use local `HEAD` as
the current-state baseline.

## Largest Top-Level Components

All refs, historical blobs by top-level path:

| Component | Packed blob size | Raw blob size | Blob count |
| --- | ---: | ---: | ---: |
| `augur` | 127.43 MiB | 248.75 MiB | 5,237 |
| `devinfra` | 65.23 MiB | 204.39 MiB | 7,774 |
| `claude_web_env` | 51.80 MiB | 56.54 MiB | 382 |
| `adgn` | 41.85 MiB | 122.50 MiB | 10,419 |
| `props` | 36.42 MiB | 143.03 MiB | 12,326 |
| `cluster` | 11.03 MiB | 56.30 MiB | 10,200 |
| `finance` | 8.59 MiB | 14.24 MiB | 829 |
| `website` | 6.63 MiB | 49.23 MiB | 204 |
| root files | 5.80 MiB | 233.81 MiB | 2,732 |
| `x` | 3.45 MiB | 12.06 MiB | 1,196 |

Largest second-level paths:

| Path | Packed blob size | Raw blob size | Blob count |
| --- | ---: | ---: | ---: |
| `augur/frontend` | 93.52 MiB | 113.75 MiB | 640 |
| `devinfra/claude` | 56.46 MiB | 73.92 MiB | 2,620 |
| `claude_web_env/reference` | 51.57 MiB | 53.41 MiB | 14 |
| `adgn/src` | 39.58 MiB | 90.72 MiB | 7,452 |
| `augur/app` | 29.60 MiB | 36.81 MiB | 275 |
| `props/specimens` | 24.45 MiB | 86.61 MiB | 7,436 |
| `devinfra/js` | 8.25 MiB | 125.28 MiB | 4,213 |
| `finance/augur` | 7.65 MiB | 11.88 MiB | 494 |
| `cluster/docs` | 7.27 MiB | 20.14 MiB | 846 |
| `props/docs` | 6.64 MiB | 8.55 MiB | 176 |

Largest file extensions:

| Extension | Packed blob size | Raw blob size | Blob count |
| --- | ---: | ---: | ---: |
| `.png` | 150.46 MiB | 165.67 MiB | 661 |
| `.gz` | 106.54 MiB | 108.47 MiB | 17 |
| `.bundle` | 35.33 MiB | 39.24 MiB | 6 |
| `.py` | 15.90 MiB | 284.60 MiB | 27,054 |
| `.md` | 11.39 MiB | 102.36 MiB | 8,091 |
| `.rs` | 6.21 MiB | 92.68 MiB | 2,877 |
| `.eval` | 6.21 MiB | 6.27 MiB | 13 |
| `.dump` | 5.84 MiB | 5.99 MiB | 1 |
| `.ipynb` | 5.21 MiB | 69.11 MiB | 10 |
| `.lock` | 5.13 MiB | 190.83 MiB | 369 |

## Largest Individual Blobs

| Packed size | Raw size | Blob | Path |
| ---: | ---: | --- | --- |
| 31.54 MiB | 31.86 MiB | `704644f957fc2e9020d2b2e4c4a5615a6335678c` | `adgn/src/adgn/props/specimens/ducktape/specimens.bundle` |
| 19.24 MiB | 19.28 MiB | `7fb5602fec530e68ca66d8ff3c3a0f9a5b694203` | `devinfra/claude/web_env/reference/environment-manager.gz` |
| 18.48 MiB | 18.53 MiB | `4a47868c1463a3f797d27d1bc143093b24550ee5` | `devinfra/claude/web_env/reference/environment-manager.gz` |
| 12.77 MiB | 12.79 MiB | `0c9fd0593014906c563c097e6d067aecbf8d267a` | `devinfra/claude/web_env/reference/environment-manager.gz` |
| 12.47 MiB | 12.49 MiB | `6f1aa1e83ec5f12596a510ae3f52d8dbf026ee9b` | `claude_web_env/reference/environment-manager.gz` |
| 12.44 MiB | 12.46 MiB | `afddbfa83702c76c587d60e75b5dab64c5a89977` | `claude_web_env/reference/environment-manager.gz` |
| 12.29 MiB | 12.31 MiB | `a054acca063aabdef883c122b8bf3d32560824bb` | `claude_web_env/reference/environment-manager.gz` |
| 11.77 MiB | 11.79 MiB | `ebee84dbf670fd3a21f0d5a200e47edaefe942f9` | `claude_web_env/reference/environment-manager.gz` |
| 5.84 MiB | 5.99 MiB | `bc4155fe4c1e03c861bb8c7f60c42e56c8acfdc4` | `props/docs/openai_evaluation/results.dump` |
| 5.19 MiB | 23.04 MiB | `df889146a07da34c3ac75c2115f05c5ccf57ccdf` | `website/notebooks/2020-12-31-cartpole-q-learning.ipynb` |

## Current `origin/devel` Tree Size

This is not full-history size, but it matters for checkout cost and future
accumulation.

Largest current top-level raw file sizes:

| Component | Raw tree size | File count |
| --- | ---: | ---: |
| `props` | 163.96 MiB | 20,226 |
| `website` | 35.37 MiB | 110 |
| `devinfra` | 27.76 MiB | 769 |
| `cluster` | 17.18 MiB | 1,642 |
| `x` | 9.31 MiB | 820 |
| `finance` | 8.25 MiB | 448 |

Selected current paths:

| Path | Raw tree size | File count |
| --- | ---: | ---: |
| `props/specimens` | 160.71 MiB | 19,779 |
| `devinfra/claude/web_env/reference` | 20.83 MiB | 10 |
| `website/notebooks` | 22.97 MiB | 2 |
| `cluster/docs/inference/runs` | 6.61 MiB | 47 |
| `finance/augur/frontend/__screenshots__` | 3.18 MiB | 8 |

The `props/specimens` finding is important: deleting that path would barely
shrink full clone transfer if its copied blobs also remain elsewhere in history,
but it would dramatically shrink the checked-out tree.

## Rewrite Candidate Savings

Estimated packed blob savings if history is rewritten and then aggressively
garbage-collected/repacked:

| Candidate | Estimated saved packed size | Estimated saved raw size | Notes |
| --- | ---: | ---: | --- |
| Drop `props/docs/openai_evaluation/results.dump` | 5.84 MiB | 5.99 MiB | Missing on `origin/devel`; good low-risk target. |
| Drop all historical `.bundle` paths | 35.33 MiB | 39.24 MiB | Missing on `origin/devel`; dominated by old props bundles. |
| Drop `results.dump` plus all `.bundle` paths | 41.17 MiB | 45.24 MiB | Best first rewrite tranche. |
| Above, plus prune stale web-env reference blobs while preserving `origin/devel` files | 126.96 MiB | 133.01 MiB | Keeps current binaries, removes old versions. |
| Above, plus delete all web-env reference blobs | 147.70 MiB | 153.84 MiB | Requires replacing current Dockerfile input path or re-adding files. |
| Above, plus prune stale Augur screenshots while preserving `origin/devel` goldens | 136.84 MiB | 152.27 MiB | Keeps current visual tests intact. |
| Above, plus delete all Augur screenshots | 139.87 MiB | 155.45 MiB | Breaks current visual-golden tests unless tests/artifacts change. |
| Above, plus prune both stale web-env refs and stale Augur screenshots while preserving current files | about 222.63 MiB | about 240.04 MiB | Best conservative Phase 1 plus Phase 2 estimate. |
| Aggressive: drop bundles, dump, all web-env refs, Augur screenshots, website notebooks, and cluster eval logs | 257.89 MiB | 316.91 MiB | High disruption unless replacements land first. |
| Drop only `props/specimens` | 931.30 KiB | 6.92 MiB | Small clone-size win due dedupe, but large checkout-size win. |
| Drop `website/notebooks` | 5.19 MiB | 46.07 MiB | Current notebook is large raw; better to strip outputs or externalize. |
| Drop `cluster/docs/inference/runs` | 6.30 MiB | 6.78 MiB | Reasonable if raw eval logs move outside Git. |

### Low-Risk First Tranche

I would start with:

1. Remove `props/docs/openai_evaluation/results.dump` from all history.
2. Remove all historical `.bundle` files:
   - `adgn/src/adgn/props/specimens/ducktape/specimens.bundle`
   - `adgn/src/adgn/props/specimens/ducktape/snapshots.bundle`
   - `adgn/src/adgn/props/bundles/specimens-2025-11-20-22.bundle`
   - `adgn/src/adgn/props/specimens/specimens.bundle`

Expected win: about 41 MiB packed, with little current-tree risk because these
files are not present on `origin/devel`.

Relevant history:

```text
7f352c6a0 docs: recover OpenAI evaluation workflow and critic run results (#498)
103876f49 Simplify evaluation docs and add OpenAI evaluation guide (#500)
8feb5fb66 wip
9101d8b92 wip
97a7ebfe6 refactor(props): rename specimen_ids to snapshot_slugs throughout codebase
380dd825b refactor(props): update bundle/registry to use snapshots.yaml
751a2a33c feat(props): add batch Jsonnet loading and 2025-11-26-code-quality specimen
3f58d2cb3 feat(props): add bundle verification tests and regenerate bundle
780f270aa feat(props): add 2025-11-22-post-fixes specimen to bundle
8723fd778 refactor(props): migrate specimens to hierarchical structure
bf3e6e3b3 Add and fixup new specimens
```

### Second Tranche: Claude Web Environment Binaries

The old `environment-manager.gz` and `process_api.gz` blobs are the largest
single cleanup opportunity after the safe tranche.

Current `origin/devel` still uses:

- `devinfra/claude/web_env/reference/environment-manager.gz` at 20,221,237 bytes.
- `devinfra/claude/web_env/reference/process_api.gz` at 1,574,387 bytes.

The current Dockerfile copies these files into the image:

```text
devinfra/claude/web_env/Dockerfile:179:COPY reference/environment-manager.gz reference/process_api.gz /tmp/
```

Pragmatic options:

1. Preserve current files, strip old blob versions.
   - Saves about 85.79 MiB beyond the safe tranche.
   - Keeps current builds working.
   - Requires a blob-ID based filter rather than a simple path deletion.
2. Externalize the current binaries and delete the path from Git.
   - Saves about 106.53 MiB for the web-env refs, or 147.70 MiB including the
     safe tranche.
   - Requires changing `devinfra/claude/web_env/Dockerfile` and the update skill
     docs so the source of truth is an artifact, release asset, or content-addressed
     fetch.

If this path stays in Git, keep only the current version or use Git LFS/artifact
storage for future updates. Gzipped binaries do not delta well enough to keep
committing every refresh as ordinary Git history.

#### Git LFS Option For Web-Env Reference Binaries

These files are a good Git LFS fit:

- `devinfra/claude/web_env/reference/environment-manager.gz`
- `devinfra/claude/web_env/reference/process_api.gz`
- Historical pre-move paths under `claude_web_env/reference/*.gz`

They are compressed binary blobs, have no useful text diff, and future updates
would otherwise keep adding poorly-delta-compressible blobs to normal Git
history.

There are two different migrations:

1. Future-only migration:

   ```bash
   git lfs track 'devinfra/claude/web_env/reference/*.gz'
   git add .gitattributes
   git add --renormalize devinfra/claude/web_env/reference/*.gz
   git commit -m 'Move Claude web-env reference binaries to Git LFS'
   ```

   This prevents future `.gz` updates from inflating normal Git objects, but it
   does not remove the already-committed historical blobs.

2. Full history migration:

   ```bash
   git lfs migrate import --everything \
     --include='devinfra/claude/web_env/reference/environment-manager.gz,devinfra/claude/web_env/reference/process_api.gz,claude_web_env/reference/environment-manager.gz,claude_web_env/reference/process_api.gz'
   ```

   Then force-push rewritten Git refs and push all LFS objects:

   ```bash
   git lfs push --all origin
   ```

   If `forgejo` remains a real mirror, push LFS objects there too.

Operational caveats:

- CI checkout currently uses `actions/checkout@v6` without `lfs: true`; for
  example `.github/workflows/bazel-ci.yml` checks out the repo before running
  Bazel. Any workflow that builds/tests these paths must fetch LFS objects, or
  `devinfra/claude/web_env/Dockerfile` will receive pointer files instead of
  gzip bytes.
- Add a direct guard such as `gzip -t devinfra/claude/web_env/reference/*.gz`
  so missing LFS checkout fails with an obvious error.
- Check any source-archive consumers. GitHub/Forgejo-generated archive tarballs
  can contain LFS pointer files depending on host behavior/configuration.
  `MODULE.bazel` already contains self-archive usage for specimens, so this
  should be verified before relying on archive fetches for LFS-tracked paths.

Recommendation: use Git LFS for these web-env reference binaries if they remain
repo-local inputs. It preserves the simple Dockerfile model while preventing
future binary churn from accumulating in normal Git packfiles. A future-only LFS
migration is easy; reclaiming existing history still requires the normal
history-rewrite coordination.

### Third Tranche: Visual Goldens

Historical Augur screenshots under `augur/frontend/__screenshots__` and current
goldens under `finance/augur/frontend/__screenshots__` dominate PNG history.

Current `origin/devel` has only 3.18 MiB of `finance/augur` screenshots, but
historical screenshot churn accounts for roughly 98 MiB under the old Augur path.

Options:

1. Preserve current goldens, strip stale historical screenshot blobs.
   - Saves about 95.67 MiB beyond the safe tranche.
   - Keeps visual tests intact.
2. Move visual goldens out of Git.
   - Saves a little more now, but adds operational complexity.
   - Tests would need to fetch baselines from a pinned artifact store.

The first option is probably the best cost/risk tradeoff.

#### Current PNG Optimization State

I checked current tracked PNGs on `origin/devel` by parsing PNG chunks and
losslessly recompressing the existing filtered IDAT streams with zlib level 9.
This is not a full `oxipng`/`zopflipng` search, but it gives a useful lower
bound for cheap lossless optimization. Ancillary metadata is negligible for the
browser screenshots.

| Scope | Current size | Estimated lossless zlib-9 savings | Notes |
| --- | ---: | ---: | --- |
| All real current PNGs, unique blobs | 18.11 MiB | 1.70 MiB (9.4%) | 146 unique blobs. |
| All real current PNGs, path-weighted | 28.63 MiB | 2.98 MiB (10.4%) | Includes duplicated paths such as `props/specimens`. |
| Visual baseline-ish PNG paths | 18.85 MiB | 2.03 MiB (10.8%) | Includes screenshots/baselines and copied specimen paths. |
| `finance/augur/frontend/__screenshots__` | 3.18 MiB | about 154-162 KiB (about 5%) | Already fairly tight. |
| `x/study_casino/frontend/__screenshots__` | 4.57 MiB | about 709-720 KiB (about 15%) | More slack than Augur. |
| `airlock/frontend/baselines` | 809 KiB | about 41 KiB (about 5%) | Already fairly tight. |
| `props/frontend/src/components/baselines` | 678 KiB unique | about 32 KiB (about 5%) | Already fairly tight. |

Most screenshot PNGs are 8-bit RGB with no interlace. The Augur files are large
because they are full-page 1280px-wide screenshots with heights up to roughly
4400px, not because they carry large metadata or are obviously badly encoded.

Two tracked files have `.png` names but JPEG signatures:

- `website/static/2021-10-18-mean-reward.png`
- `website/static/2021-10-18-standing.png`

They are also copied under `props/specimens/ducktape/2026-05-20-00/code/...`.

Recommendation: adding a deterministic lossless optimizer is fine as a hygiene
guardrail, but it should not be expected to solve repo history size. For current
visual baselines, recompression is a single-digit percentage win in most suites.
The real savings are pruning stale historical screenshots, keeping current
baseline sets modest, and avoiding copied snapshot duplication.

### Props Specimens

`props/specimens` is huge in the current tree: 160.71 MiB on `origin/devel`.
However, deleting only that path saves less than 1 MiB of full-history packed
clone size because most blobs are copied from elsewhere in the repo and remain
reachable outside `props/specimens`.

This still deserves cleanup, but for a different reason:

- It makes checkouts and working-tree scans heavy.
- It can accidentally duplicate future large assets.
- The copied snapshots contain stale commit references, old cluster YAML, old
  screenshots, and old docs.

Recommendation: keep specimen manifests, issue YAML, and minimal source excerpts;
do not store full repo snapshots. For larger reproducible specimens, use
content-addressed tarballs or external repo archives with explicit hashes.

### Notebooks And Eval Logs

`website/notebooks/2020-12-31-cartpole-q-learning.ipynb` is 24 MiB in the
current tree and accounts for a 5.19 MiB packed historical blob. The raw notebook
history is 69 MiB. Strip outputs or convert heavy notebooks to source plus
generated artifacts.

`cluster/docs/inference/runs` is about 6.61 MiB in the current tree and 6.30 MiB
packed historically. Keep summaries, commands, and compact tables in Git; move
raw `.eval` logs, raw output, and large run artifacts to BuildBuddy artifacts,
object storage, or a release asset.

## Commit References In Current Repo

I scanned `origin/devel` tracked text files for 40-character hex strings that
resolve to commits in this repository, and for likely abbreviated commit
references in git-ish contexts.

Findings:

- 461 text lines contain 40-character hex strings.
- 102 unique 40-character hex strings appeared.
- 11 of those resolve to Git commit objects in this repo.
- Those 11 commit IDs have 69 current tracked occurrences.
- 128 abbreviated tokens in git-ish contexts resolve to commits in this repo.

Follow-up scan details:

| Category | Commit-like occurrences |
| --- | ---: |
| `props/specimens` copied snapshots | 165 |
| debug notes / captured outputs | 64 |
| docs / plans / markdown | 53 |
| live cluster manifests | 39 |
| code / tests / build files | 19 |
| config data | 2 |

Important references to handle during a history rewrite:

| Reference | Current file(s) | Rewrite impact |
| --- | --- | --- |
| `4ad33013af27e159863bed92ffcfdb55b388e46c` | `MODULE.bazel` and copied specimens | This is a live `http_archive` of this repo. It must be updated after rewriting history, or replaced with another specimen source. |
| `3b9e37c50911c40c11a51903de961c2db0f50f59` | `airlock/frontend/tests/harness/harness.ts` | Fixture metadata and GitHub commit URL become stale. |
| `b86700d` | `finance/augur/api/server_test.py` | Test fixture uses deployment metadata with abbreviated source commits. |
| `680d78946bf72e5e3601cdb69299546b495cab1b` | `devinfra/claude/docs/web-setup-debug.md` | Pinned raw GitHub setup URL becomes invalid unless old GitHub history remains reachable. |
| various abbreviated SHAs | `cluster/k8s/**`, `cluster/debug/**` | Image tags embed commit suffixes; old container tags may remain valid even if Git SHAs change, but they no longer resolve as source links. |
| various debug/runbook SHAs | `debug/**`, `cluster/docs/lessons_learned/**`, `plans/**` | These become historical prose references. Update only if they need to stay navigable. |
| `995a1fce6410ec4a882d2e861b41423c8c839148` | `props/specimens/ducktape/2026-05-20-00/**` | Specimen provenance becomes stale; this is another reason to reduce copied repo snapshots. |

Required fixups after a rewrite:

- `MODULE.bazel`: update the self-archive `http_archive` pinned to
  `4ad33013af27e159863bed92ffcfdb55b388e46c`, including the `strip_prefix`,
  URL, and `sha256`, or replace that specimen source with a non-self-archive
  mechanism.
- `devinfra/claude/docs/web-setup-debug.md`: update or remove the pinned
  `raw.githubusercontent.com/.../680d78946bf72e5e3601cdb69299546b495cab1b/...`
  setup URL. This is an executable instruction, not just passive history.
- `airlock/frontend/tests/harness/harness.ts`: update deployment fixture
  metadata and commit URL expectations if those tests still expect a commit URL
  to resolve.
- `finance/augur/api/server_test.py`: update deployment fixture metadata for
  `source_commit`, `source_commit_url`, and image tag suffix expectations.

References not to blindly rewrite:

- `cluster/k8s/**` image tags such as `devel-20260614062503-868de2d`.
  These are live GHCR image identities, often managed by Flux image automation.
  Changing them means changing deployed image refs, not merely updating docs.
- Debug captures under `cluster/debug/**` often record historical live output.
  Rewrite only if the file is used as executable config or active instructions.

Docs policy recommendation:

- For `debug/**`, `cluster/docs/lessons_learned/**`, `cluster/archive/**`,
  `plans/**`, and similar historical prose, do not try to individually rewrite
  every commit SHA. Add a short archive note after the history rewrite saying
  pre-rewrite SHAs are historical references and may not resolve in the rewritten
  repository.
- For Markdown files that contain executable commands or URLs, update those
  commands. `devinfra/claude/docs/web-setup-debug.md` is the current known
  example.

Noisy source:

- `props/specimens/**` contains many duplicated old repo snapshots, so it also
  duplicates old commit references. This should be handled by the broader
  specimen cleanup/restructure, not by hand-editing every copied reference.

The scan does not cover external systems:

- GitHub PR descriptions and comments.
- BuildBuddy invocations.
- GHCR image labels.
- Cluster live resources not currently in Git.
- Browser bookmarks or pasted runbook URLs.

If we rewrite history, I would do a follow-up pass that rewrites or annotates
current tracked commit references. The critical path is `MODULE.bazel`, because
it affects builds. Most debug-note references can be left as historical if we
explicitly document that pre-rewrite SHAs are archival.

## Recommended Cleanup Plan

### Phase 1: Low-Risk History Rewrite

Use `git-filter-repo` in a fresh clone or disposable worktree. It was not
installed in the current shell (`git filter-repo` was unavailable), so add it to
the devshell or run it via Nix before doing the rewrite.

Target:

- Remove `props/docs/openai_evaluation/results.dump`.
- Remove all historical `.bundle` blobs.

Expected result:

- Save about 41 MiB packed.
- No current `origin/devel` file loss.
- No known build/test impact from current tree.

Sketch:

```bash
git clone --mirror git@github.com:agentydragon/ducktape.git ducktape-rewrite.git
cd ducktape-rewrite.git

git filter-repo \
  --invert-paths \
  --path props/docs/openai_evaluation/results.dump \
  --path adgn/src/adgn/props/specimens/ducktape/specimens.bundle \
  --path adgn/src/adgn/props/specimens/ducktape/snapshots.bundle \
  --path adgn/src/adgn/props/bundles/specimens-2025-11-20-22.bundle \
  --path adgn/src/adgn/props/specimens/specimens.bundle

git gc --prune=now --aggressive
git count-objects -vH
```

Before force-pushing, regenerate and test a normal clone from the rewritten
remote locally.

### Phase 2: Stale Blob Pruning

If Phase 1 is acceptable, prune stale versions while preserving current files:

- Old `devinfra/claude/web_env/reference/*.gz` and old
  `claude_web_env/reference/*.gz`.
- Old `augur/frontend/__screenshots__/*.png`.

Expected additional win:

- Web-env stale binaries: about 85.79 MiB beyond Phase 1.
- Augur stale screenshots: about 95.67 MiB beyond Phase 1.

This should use a blob-ID based filter or a custom `git-filter-repo` callback,
not a simple path deletion, because current `origin/devel` still needs
`devinfra/claude/web_env/reference/*.gz` and current visual tests still need
`finance/augur/frontend/__screenshots__/*.png`.

### Phase 3: Current Tree Policy Changes

These are not just history cleanup; they change how the repo behaves going
forward.

1. Replace full copied `props/specimens/**/code` trees with minimal specimen
   sources or external content-addressed archives.
2. Strip notebook outputs before commit, especially under `website/notebooks`.
3. Keep inference benchmark summaries in Git, but store raw eval logs outside
   Git.
4. Add guardrails for large blobs.

Recommended guardrails:

- Pre-commit hook rejecting new blobs above a threshold, with an allowlist for
  deliberate exceptions.
- CI check comparing new blobs against `origin/devel` and failing on new
  large binary blobs unless allowlisted.
- `.gitattributes` policy for binary outputs:
  - Either Git LFS for assets that must live with the repo.
  - Or explicit "do not commit generated artifacts here" docs and tests.
- Separate rules for high-churn visual goldens:
  - Keep current small baselines in Git if tests need them.
  - Do not keep old generated screenshots forever as normal Git history if the
    UI churns heavily.

## Practical Rewrite Warnings

History rewrite is feasible here, but it is operationally disruptive:

- Every branch and tag included in the rewrite gets new commit IDs.
- Open PRs and local clones need to rebase or reclone.
- GitHub will keep some old objects alive for a while through PR refs, forks,
  caches, and protected refs.
- Any external links to old commit URLs may break or become archival-only.
- Build systems that fetch this repo by commit must be updated.

For this repo, the concrete pre-flight checklist should be:

1. Inventory protected branches and open PRs.
2. Freeze merges during the rewrite.
3. Rewrite a mirror clone.
4. Run a normal clone from the rewritten mirror and compare `origin/devel`.
5. Update `MODULE.bazel` self-archive SHA/hash if the referenced commit is
   rewritten or decide to preserve that archive target.
6. Update or annotate current tracked commit references that must remain
   navigable.
7. Force-push rewritten refs.
8. Ask collaborators/agents to reclone or hard reset intentionally.
9. Re-run CI from the rewritten `origin/devel`.

## Recommendation

Do Phase 1 first. It is simple, removes dead artifacts you already called out,
and has the lowest chance of breaking current behavior.

Then decide between two Phase 2 profiles:

- Conservative: preserve current web-env binaries and current visual goldens,
  prune only stale historical blob versions. This should cut roughly 220 MiB
  total while keeping current behavior.
- Aggressive: externalize web-env binaries and visual/eval artifacts. This can
  cut roughly 250 MiB, but it needs real replacement plumbing and tests.

Independently of history rewriting, clean up `props/specimens` because it is the
dominant current checkout cost even though it is not the dominant clone-transfer
cost.
