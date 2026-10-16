@README.md

## Visual target selection

Any test that writes a `visual-review.json` manifest under Bazel undeclared outputs needs
the `visual` tag so the determinism sweep includes it. PNGs not registered in the manifest
are diagnostics and do not need the tag.
