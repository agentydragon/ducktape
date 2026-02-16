# Specimen Test Infrastructure - Design

## Current Architecture

### Bazel Targets (per specimen)

The `specimen_targets()` macro in <defs.bzl> generates three targets per specimen:

1. **`{name}_code_tar`** — Deterministic uncompressed tar of source code, with `.specimen` suffix stripped (restoring original filenames like `BUILD.bazel`). Built by a custom Starlark rule (`create_code_tar`) invoking `compile.py code-tar`. For external repos (http_archive), the strip prefix is auto-detected from `File.owner.workspace_root`.
2. **`{name}_data_blob`** — Single YAML blob merging all `issues/**/*.yaml` files with `snapshot_slug` and `split` metadata. Built by a custom Starlark rule (`create_data_blob`) invoking `compile.py data-blob`. Validated through `SpecimenData` Pydantic model at build time.
3. **`test_{name}`** — Per-specimen `py_test` using <test_specimen.py>, receiving artifact paths via env vars (`SPECIMEN_CODE_TAR`, `SPECIMEN_DATA_YAML`).

### Sync Path

`SpecimenBundle` (dataclass in `props/db/sync/sync.py`) holds `slug`, `code_tar`, and `data_yaml` paths. `sync_specimen()` reads the bundle artifacts and syncs snapshot, snapshot files, and issues to the DB. Both `test_specimen.py` and the `props-sync-specimen` CLI use this same function.

`SpecimenData` (Pydantic model) defines the data blob schema: `{snapshot_slug, split, issues: dict[str, YAMLIssue]}`.

### External Specimens

Remote-VCS specimens (crush, older ducktape snapshots) use `http_archive` in `MODULE.bazel`, pinned to a specific commit SHA. The archive is exposed as a filegroup and passed as `code_srcs` to `specimen_targets()`.

### Pre-commit Hooks

- `block-specimen-code-changes` — Blocks modifications to `code/` in committed specimens (those with an `issues/` directory in HEAD).
- `validate-specimen-issue-ids` — Validates issue file naming conventions.

## Achieved Design Goals

| Goal                                | Implementation                                                                    |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| **Per-specimen tests**              | Each specimen has its own test target (parallel, isolated, RBE-compatible)        |
| **Generic test**                    | Single `test_specimen.py` instantiated per specimen via macro                     |
| **No `local = True`**               | Tests use testcontainers PostgreSQL, run on RBE                                   |
| **No shell-out in tar creation**    | Custom Starlark rules + `compile.py` tool (replaces earlier genrule shell script) |
| **No committed manifests**          | Slug and split are macro params; `SpecimenData` blob is generated at build time   |
| **Compiled issues blob**            | `create_data_blob` rule merges YAML files into single blob (using Python, not yq) |
| **External repo support**           | `http_archive` in `MODULE.bazel` for crush and older ducktape snapshots           |
| **Unified sync for tests and prod** | Both use `SpecimenBundle` + `sync_specimen()`                                     |

## Remaining Gaps

### 1. Pre-commit hook doesn't distinguish `.specimen` renames from content changes

The `block-specimen-code-changes` hook blocks all staged changes under `props/specimens/*/code/` for committed specimens. This means adding new `.specimen`-renamed BUILD files to an existing specimen requires either temporarily removing the `issues/` directory or bypassing the hook. Low priority — new specimens rarely need retroactive BUILD file renaming.

## Resolved

- **`sync_specimen` now syncs `critic_scopes_expected_to_recall`**: `_sync_critic_scopes_for_specimen` is called after flushing issues. All 4 previously-failing tests (`test_sync_occurrence_update`, `test_tp_occurrence_credits`, `test_view_extracts_grade_fields`, `test_grading_edges_constraints`) pass.
- **Legacy sync path removed**: Deleted `sync_snapshots_to_db`, `sync_issues_to_db`, `sync_file_sets_to_db`, `resolve_git_content`, `create_snapshot_archive`, and related source-resolution code (`SnapshotDoc`, `BundleFilter`, `LocalSource`/`GitSource`/`GitHubSource` models, `pygit2` dependency). `sync_all` renamed to `sync_metadata` (only syncs model metadata; specimens are synced via `sync_specimen`). `load_yaml_issues` kept in `yaml_loader.py` (tested by `test_collect_errors.py`).

## Possible Future Work

- **Refine pre-commit hook**: Allow `.specimen` file additions/renames while still blocking content changes to committed specimen code.
