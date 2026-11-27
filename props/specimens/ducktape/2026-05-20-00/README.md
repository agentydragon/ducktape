# Sparse Bazel Graph Hygiene Specimen

This specimen was generated from ducktape commit `995a1fce6410ec4a882d2e861b41423c8c839148`. It keeps a sparse, coherent snapshot: BUILD files referenced by the issue YAMLs, the internal Bazel package closure needed to analyze/build those active issue targets, package-local source/data files for those packages, and selected workflow or deployment files needed to evaluate release/build reachability.

The active issue target set was checked by materializing `code/` into a temporary workspace, restoring `BUILD.bazel.specimen`/`BUILD.specimen` filenames, and running:

```sh
bazel build --aspects= --output_groups=default <163 active issue labels>
```

That aspect-cleared build completed successfully. The repo's default build also enables lint aspects; those were intentionally cleared for this specimen buildability check so the result reflects the issue targets and their target graph rather than unrelated lint-tool external fetches.

Issue counts:

- `artifact-only-targets`: 28 false-positive occurrences
- `product-targets-only-reached-from-tests`: 29
- `reasonable-non-test-targets-only-tested`: 27 false-positive occurrences
- `test-support-targets-missing-testonly`: 36 (+4 commented-out deferred occurrences)
- `unconsumed-bazel-targets`: 98

## Post-generation edits

The snapshot was edited by hand after generation, so it no longer matches ducktape `995a1fce6410ec4a882d2e861b41423c8c839148` byte for byte.

- **Dropped the Claude web-container reverse-engineering work** to cut the snapshot's size (about 22.8 MB of 62.6 MB):
  - Deleted `devinfra/claude/web_env/` (180 files, including the two proprietary binaries under `reference/`) and the `.claude/skills/update_container_re/` skill that maintained it.
  - Removed the `environment-manager`-only `go_deps.module(...)` entries and their `use_repo` names from `MODULE.bazel`, and the `web_env` lines from `.gitattributes`, `.gitignore` and `.pre-commit-config.yaml`.
  - Reworded the docs and comments that pointed into the deleted tree (`devinfra/claude/AGENTS.md`, `docs/{secrets_env_flow,web-setup-debug}.md`, `platform_detect.py`, `container_runtime.py`, `handler.py`, `web_setup.sh`, `devinfra/firecracker/{DESIGN.md,manager/protocol.py}`, root `AGENTS.md`). The facts stay; the pointers to deleted files are gone.
- **Removed the issue occurrences that pointed into the deleted tree:** `occ-008`..`occ-014` from both `unconsumed-bazel-targets` (7 true positives) and `artifact-only-targets` (7 false positives). Remaining `occurrence_id`s are unchanged, so the numbering has a gap.
- The Cargo `process_api RE dependencies` crates in the root `Cargo.toml` were left as they were. `devinfra/secrets/web_env.sh` is the secrets script, not part of the removed tree, and stays.
