# Claude Code Web Environment

Reproducible reconstruction of the Claude Code web container, verified by
full-filesystem manifest diffing. Goal: no diff exclusions except session-start-hook
artifacts and unavoidable runtime differences (`/proc`, `/sys`, caches) — anything
the Dockerfile can fix is fixed there.

## Quick Start

Runs on any machine with Docker and network access:

```bash
# Full build + diff (runs fetch_debs)
bazel run //devinfra/claude/web_env/tools:build_and_diff_bin
# Diff only (image already built)
bazel run //devinfra/claude/web_env/tools:build_and_diff_bin -- --diff-only
```

Commit `diff_report.md` with Dockerfile/rootfs changes. Implementation:
`tools/build_and_diff.py`.

## Layout

| Path              | Purpose                                                                               |
| ----------------- | ------------------------------------------------------------------------------------- |
| `Dockerfile`      | Full build from Ubuntu 24.04                                                          |
| `exclusions.yaml` | Diff-time exclusion rules                                                             |
| `rootfs/`         | Container content at its live paths (`etc/`, `usr/`, `root/`, `home/claude/`), COPYed |
| `tools/`          | Build/diff tooling (`capture_manifest.py`, `diff_manifests.py`, `fetch_debs.py`)      |
| `reference/`      | Live snapshots, see below                                                             |
| `re/`             | Reverse engineering of `environment-manager` and `process_api`                        |
| `docs/`           | <docs/container_spec.md> (runtime details), environment discovery notes               |

`reference/`:

- `versions-YYYY-MM-DD.yaml` — runtimes, dpkg, npm globals, binary hashes (from
  `//devinfra/claude/web_env/tools:capture_versions_bin`).
- `environment-manager.gz`, `process_api.gz` — proprietary binaries, gunzipped into
  the image by the Dockerfile.
- `*-settings.json`, `*-env-vars.txt` — documentation only.

## Manifests and exclusions

Manifests are NDJSON: `path`, `type` (`f`/`d`/`l`/`p`/`s`), `perms`, `owner`,
`group`, `size`, `sha256` (files ≤50MB), `link_target`. Exclusions apply at diff
time only, so manifests never need recapturing.

| `exclusions.yaml` category | Meaning                                                     |
| -------------------------- | ----------------------------------------------------------- |
| `skip_paths`               | Ignored entirely (`/proc`, `/sys`, caches)                  |
| `volatile_paths`           | Differences expected (tool builds: rbenv, nvm, uv, rustup)  |
| `hash_may_differ`          | Must exist on both sides; content may differ                |
| `only_in_live`             | Expected only in live (proprietary binaries, runtime state) |
| `only_in_built`            | Expected only in built (npm cache artifacts)                |
