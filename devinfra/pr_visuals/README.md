# devinfra/pr_visuals

Trusted publisher for PR visual reviews. The "Publish PR visuals" workflow
(`.github/workflows/pr-visuals-publish.yml`) runs `publisher.py` after completed PR
(same-repo or agent fork) and `devel` Bazel CI runs, including failed ones. A
cancelled CI run stops visual publication and closes any announced check. For
completed runs, it finds the commit's Bazel invocations, collects targets whose undeclared
outputs contain a `visual-review.json` manifest (schema `util/visual_review.py`),
publishes an immutable review bundle and shared PNGs to
`s3.allegedly.works/pr-visuals`, and upserts a singleton review comment on the PR.
Cache-hit tests still report undeclared outputs, so a bundle covers every visual
target the run covered — this needs a download-mode flag
(<devinfra/docs/bazel_caching.md> § Undeclared test outputs under BwoB).

The comment leads with CI/baseline warnings and change counts, then groups changed
targets by impact (top two expanded), linking each to its gallery page and CI run;
it is capped at 55,000 characters, with omitted targets in the gallery. The gallery
index uses the same ordering (sortable by name or changed-screenshot count); per-test
pages show Before beside After in fixed-aspect frames, and explain the missing diff
overlay when dimensions change. `//devinfra/pr_visuals:test_gallery_visual` captures
the index and a test page on desktop and mobile.

Publisher internals — invocation discovery, PR resolution, cancelled-run handling,
baseline resolution, check-runs, storage layout, releases: <docs/publisher.md>.

## Opting a visual test in

Automatic with a shared harness:

- Python browser tests (`py_visual_test`, or `py_test` with a real server):
  `VisualPage.capture()` from `util.testing.visual_capture` writes a PNG and
  registers it; see <../../util/testing/frontend_visual/README.md>.
- Other Python image producers (e.g. GNOME shell captures):
  `util.testing.visual_review.publish_review_png(png, ...)` once per rendered case.
- Custom drivers: `util.testing.visual_review.write_visual_review_manifest`.

## Determinism checks

`//devinfra/pr_visuals:determinism_bin` repeats visual targets and compares the PNGs
registered in each run's `visual-review.json` by content-addressed CAS URI (it
downloads manifests, not PNGs). Changed bytes, missing declared images, and images
dropped from a later run's manifest fail; manifest membership alone defines the
review set. Unregistered PNGs are reported as diagnostics and never fail. Nonzero
build/test exits and non-passing statuses still fail, even with no review images.

## Retention

The publisher never deletes. Shared images must outlive every retained review that
references them; object age alone is not an expiry criterion. One-time cleanup
procedure (incl. the measured 2026-10-19 cleanup): <docs/storage-cleanup.md>; no
recurring expiry exists. `//devinfra/pr_visuals:retention_bin` (`bb run`) prints a
read-only JSON plan — always stale, never a deletion command or a safe-to-delete
claim: <docs/retention.md>.
