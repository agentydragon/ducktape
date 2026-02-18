# Specimen Build and Sync

How specimens are compiled by Bazel and synced to the database.

## Bazel Targets

The `specimen_targets()` macro in <../defs.bzl> generates three targets per specimen:

1. **`{name}_code_tar`** — Deterministic uncompressed tar of source code. Files with `.specimen` suffix are restored to original names (e.g. `BUILD.bazel`). For external repos (`http_archive`), the strip prefix is auto-detected from `File.owner.workspace_root`.
2. **`{name}_data_blob`** — Single YAML blob merging all `issues/**/*.yaml` with `snapshot_slug` and `split` metadata. Validated through `SpecimenData` Pydantic model at build time.
3. **`test_{name}`** — Per-specimen `py_test` using <../test_specimen.py>, receiving artifact paths via env vars (`SPECIMEN_CODE_TAR`, `SPECIMEN_DATA_YAML`). Tests use testcontainers PostgreSQL and run on RBE.

Both rules are backed by `compile.py` (`code-tar` and `data-blob` subcommands).

## `.specimen` Suffix Convention

Bazel requires `BUILD.bazel` files in directories it manages. Since specimen `code/` directories contain source code from other projects (which may have their own `BUILD.bazel` files), these must be renamed to avoid Bazel treating them as package boundaries. The convention is to append `.specimen` (e.g. `BUILD.bazel.specimen`). The `create_code_tar` rule strips this suffix when creating the archive.

## Sync Path

`SpecimenBundle` (dataclass in `props/db/sync/sync.py`) holds `slug`, `code_tar`, and `data_yaml` paths. `sync_specimen()` reads the bundle artifacts and syncs snapshot, snapshot files, issues, and critic scopes to the DB. Both `test_specimen.py` and the `props db sync-specimen` CLI use this same function.

Model metadata is synced by `sync_model_metadata_with_session()` (called during `props db recreate` and at backend startup).

## External Specimens

Remote-VCS specimens (crush, older ducktape snapshots) use `http_archive` in `MODULE.bazel`, pinned to a specific commit SHA. The archive is exposed as a filegroup and passed as `code_srcs` to `specimen_targets()`.
