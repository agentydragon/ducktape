# Per-Snapshot Manifest Files

**Status:** Implemented
**Updated:** 2025-12

## Current State

Each snapshot has its own `manifest.yaml` file at `<repo>/<version>/manifest.yaml`.

- **12 snapshots** in `~/code/specimens`
- **4 test fixtures** in `tests/props/fixtures/specimens`
- Loader: `FilesystemLoader.discover_snapshots()` in `db/sync/_loader.py`

## Structure

```
specimens/
  ducktape/
    2025-11-20-00/
      manifest.yaml        # source, split, bundle
      issues/
        *.yaml             # Issue definitions
      code/                # Source code (for vcs: local)
```

Slug is derived from the directory path (`<repo>/<version>`), not stored in manifest.

## Manifest Schema

```yaml
# specimens/ducktape/2025-11-20-00/manifest.yaml

source:
  vcs: local
  root: code

split: train

bundle:
  source_commit: b729b362de957d127d1e8ac17d8811665ce805fe
  include: [adgn/, wt/]
  exclude: [adgn/agent/web/]
```

## Adding New Snapshots

Create `manifest.yaml` in the snapshot directory. The loader discovers it automatically via `rglob("manifest.yaml")`.

## Tests

See `tests/props/db/sync/test_manifest_loading.py` for loader tests.
