# Sparse Bazel Graph Hygiene Specimen

This specimen was generated from ducktape commit `995a1fce6410ec4a882d2e861b41423c8c839148`. It keeps a sparse, coherent snapshot: BUILD files referenced by the issue YAMLs, the internal Bazel package closure needed to analyze/build those active issue targets, package-local source/data files for those packages, and selected workflow or deployment files needed to evaluate release/build reachability.

The active issue target set was checked by materializing `code/` into a temporary workspace, restoring `BUILD.bazel.specimen`/`BUILD.specimen` filenames, and running:

```sh
bazel build --aspects= --output_groups=default <170 active issue labels>
```

That aspect-cleared build completed successfully. The repo's default build also enables lint aspects; those were intentionally cleared for this specimen buildability check so the result reflects the issue targets and their target graph rather than unrelated lint-tool external fetches.

Issue counts:

- `artifact-only-targets`: 35 false-positive occurrences
- `product-targets-only-reached-from-tests`: 29
- `reasonable-non-test-targets-only-tested`: 27 false-positive occurrences
- `test-support-targets-missing-testonly`: 36 (+4 commented-out deferred occurrences)
- `unconsumed-bazel-targets`: 105
