@README.md

## Agent Instructions

- **Container content goes in `rootfs/`** — organized to mirror live filesystem paths.
- **Reference snapshots go in `reference/`** — proprietary binaries (environment-manager, process_api) are stored here as gzipped files and baked into the Dockerfile.
- **Exclusions** in `exclusions.yaml` have inline comments. Use `volatile_paths` for non-deterministic tool installations. Use `only_in_live` only for things that truly cannot be reproduced.
- **Prefer baking files in** over excluding them. Snapshot and COPY into `rootfs/` rather than adding to `only_in_live`.

### Exclusion Minimization Goal

The goal is **zero exclusions** that aren't:

1. **Session start hook artifacts** — files created by `tools/claude_hooks` at runtime
2. **Unavoidable runtime differences** — `/proc`, `/sys`, `/dev`, caches, runtime state

If a difference can be fixed by updating the Dockerfile (pinning a version, adding a file), **fix it in the Dockerfile** rather than adding an exclusion. Exclusions should be a last resort for truly unavoidable runtime differences.

- **Do not commit secrets or tokens**. Reference files have tokens redacted.
- **Pydantic models** in `tools/manifest.py` are shared between capture and diff tools.

### Build Workflow

After making changes to the Dockerfile or `rootfs/` content, **always run a build and update the diff report**:

```bash
cd claude_web_env
./tools/build_and_diff.sh
```

This script:

1. Sets up tmpfs storage (if needed)
2. Builds the Dockerfile with VFS storage (~20 min)
3. Captures manifests from live and built images
4. Generates `diff_report.md`

If you only need to regenerate the diff (image already built):

```bash
./tools/build_and_diff.sh --diff-only
```

**Commit `diff_report.md`** along with your Dockerfile/rootfs changes. The diff report documents the current delta between built and live containers.

> **Keep this procedure up to date**: If the build process changes (new storage options, different flags, etc.), update both this file and `tools/build_and_diff.sh`.

### Tool Availability

- **podman 4.9.3 + buildah 1.33.7**: Available and working
- **docker / buildx / BuildKit**: NOT available in the sandbox
- **fuse-overlayfs**: Installed but broken (gVisor lacks `FUSE_CAP_READDIRPLUS`)

### Container Update Procedure

When the live container has been updated (new runtime versions, new binaries, new
environment variables), follow this procedure to bring the reconstruction in sync:

#### 1. Capture current versions

```bash
cd claude_web_env
uv run --script tools/capture_versions.py > reference/versions-$(date +%Y-%m-%d).yaml
```

This creates a structured YAML snapshot of all runtime versions, npm packages,
binary hashes, environment variables, and environment-manager metadata.

#### 2. Compare against previous capture

```bash
uv run --script tools/capture_versions.py --diff reference/versions-YYYY-MM-DD.yaml
```

This shows a unified diff of what changed. Use this to identify which Dockerfile
version pins, rootfs files, and documentation need updating.

#### 3. Update the Dockerfile

For each changed version identified in the diff:

- **Node.js versions**: Update download URLs in the Layer 9 `RUN` step
- **npm globals**: Update version pins in the Layer 10 `RUN` step
- **Bun version**: Update the `bun-v` pin in Layer 17
- **Go versions**: Update download URLs in Layer 13
- **golangci-lint**: Update version in Layer 13
- **APT packages**: Check if snapshot date needs advancing or version pins need updating

#### 4. Update documentation

Files that may need updates when the container changes:

| File                            | What to update                                                   |
| ------------------------------- | ---------------------------------------------------------------- |
| `docs/environment_discovery.md` | environment-manager version, help output, flags, env vars, tools |
| `docs/container_spec.md`        | Captured date, any changed runtime properties                    |
| `README.md`                     | Build instructions if changed                                    |
| `PLAN.md`                       | Diff summary after rebuild                                       |

For `environment_discovery.md` specifically, check:

- **environment-manager `--version`** output
- **environment-manager `--help`** for new/changed subcommands
- **`task-run --help`** and **`orchestrator --help`** for new/changed flags
- **`print-sandbox-settings`** for changed sandbox configuration
- **Environment variables**: run `env | grep -E "^(CLAUDE|CODESIGN|MCP_)"` and compare
- **Stop hook**: diff `/home/claude/.claude/stop-hook-git-check.sh` against docs

#### 5. Rebuild and diff

```bash
./tools/build_and_diff.sh
```

Review `diff_report.md` and update `PLAN.md` with the new diff summary.

#### 6. Commit everything

Commit the versions snapshot, Dockerfile changes, doc updates, and diff report together.

### Current Plan

See <PLAN.md> for current work items and priorities.
