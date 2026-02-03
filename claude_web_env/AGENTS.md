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
./tools/build-and-diff.sh
```

This script:

1. Sets up tmpfs storage (if needed)
2. Builds the Dockerfile with VFS storage (~20 min)
3. Captures manifests from live and built images
4. Generates `diff-report.md`

If you only need to regenerate the diff (image already built):

```bash
./tools/build-and-diff.sh --diff-only
```

**Commit `diff-report.md`** along with your Dockerfile/rootfs changes. The diff report documents the current delta between built and live containers.

> **Keep this procedure up to date**: If the build process changes (new storage options, different flags, etc.), update both this file and `tools/build-and-diff.sh`.

### Tool Availability

- **podman 4.9.3 + buildah 1.33.7**: Available and working
- **docker / buildx / BuildKit**: NOT available in the sandbox
- **fuse-overlayfs**: Installed but broken (gVisor lacks `FUSE_CAP_READDIRPLUS`)

### Current Plan

See <PLAN.md> for current work items and priorities.
