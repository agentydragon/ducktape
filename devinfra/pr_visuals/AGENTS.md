@README.md

## Visual target selection

Any test that writes a `visual-review.json` manifest under Bazel undeclared outputs needs
the `visual` tag so the determinism sweep includes it. PNGs not registered in the manifest
are diagnostics and do not need the tag.

If a repeated `bbr test` attempt leaves no BuildBuddy invocation, retain completed runs in the
report (compare them only when at least two are available), report the attempted command and
exit code, and fail the sweep. Do not retry the attempt or count its absent outputs as missing
screenshots.
