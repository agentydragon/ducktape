# devinfra/pr_visuals

Trusted publisher for PR visual reviews. The "Publish PR visuals" workflow
(`.github/workflows/pr-visuals-publish.yml`) runs `publisher.py` after completed PR
(same-repo or agent fork) and `devel` Bazel CI runs, including failed ones. A
cancelled CI run stops visual publication and closes any announced check. For
completed runs, it derives the triggering run's Bazel invocation ID and collects targets whose undeclared
outputs contain structured records under `test-outputs/records/` (schema `devinfra/testing/output_records.py`),
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

Publisher internals — invocation selection, PR resolution, cancelled-run handling,
baseline resolution, check-runs, storage layout, releases: <docs/publisher.md>.
The framework-independent record format and identity contract are described in
<docs/test_output_protocol.md>.

## Opting a visual test in

Automatic with a shared harness:

- Python browser tests (`py_visual_test`, or `py_test` with a real server):
  `VisualPage.capture()` from `util.testing.frontend_visual.capture` writes a PNG and
  registers it; see <../../util/testing/frontend_visual/README.md>.
- Other Python image producers (e.g. GNOME shell captures):
  the `test_outputs` pytest fixture binds each output to its full pytest node ID.
- Other frameworks and languages can write the same versioned records and payloads
  directly; Python callers can use `devinfra.testing.output_writer.publish_output`
  with UTF-8 text or bytes, or `open_output` for file-style writes.

## Determinism checks

`//devinfra/pr_visuals:determinism_bin` repeats visual targets and compares collected
screenshot identities and CAS digests without downloading image blobs. Missing or
incomplete runs and differing screenshot bytes are reported; diagnostics are not
compared as screenshots. Failed attempts do not supply a successful run's outputs.

## Retention

The publisher never deletes. Shared images must outlive every retained review that
references them; object age alone is not an expiry criterion. One-time cleanup
procedure (incl. the measured 2026-10-19 cleanup): <docs/storage-cleanup.md>; no
recurring expiry exists. `//devinfra/pr_visuals:retention_bin` (`bb run`) prints a
read-only JSON plan — always stale, never a deletion command or a safe-to-delete
claim: <docs/retention.md>.
