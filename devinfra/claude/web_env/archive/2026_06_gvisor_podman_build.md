# Archived: gVisor-era podman/9p build procedure (2026-06)

Historical note. Until 2026-06, Claude Code web sessions ran under gVisor (9p
root, no bridge networking, UID mapping). The container was built with
`podman build` and inspected via `podman create` + `podman mount` because
`podman run` did not work under gVisor. podman/buildah/fuse-overlayfs are no
longer in the container; the canonical build path is now
`bazel run //devinfra/claude/web_env/tools:build_and_diff_bin` (see <../AGENTS.md>).

This file preserves the obsolete procedure for reference only. **Do not run it
on Firecracker sessions.**

## Quick Start (obsolete)

```bash
cd devinfra/claude/web_env

# Set up tmpfs storage (REQUIRED - 9p root is too slow)
mount -t tmpfs -o size=200G,exec tmpfs /tmp/tmpfs-exec
mkdir -p /tmp/tmpfs-exec/containers/{storage,run}

# Create storage config (VFS for >54 layer Dockerfiles)
cat > /tmp/storage-tmpfs-vfs.conf << 'EOF'
[storage]
driver = "vfs"
runroot = "/tmp/tmpfs-exec/containers/run"
graphroot = "/tmp/tmpfs-exec/containers/storage"
EOF

# Build (~20 min on tmpfs)
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
  podman build --layers=false \
    --network=host --isolation=oci --format=docker \
    -t claude-code-web-recreated .

# Capture live manifest (ground truth)
bazel run //devinfra/claude/web_env/tools:capture_manifest_bin -- > live_manifest.ndjson

# Capture built manifest (via podman mount — can't podman run under gVisor)
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
  podman create --name capture-tmp localhost/claude-code-web-recreated /bin/true
MOUNT_PATH=$(CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman mount capture-tmp)
bazel run //devinfra/claude/web_env/tools:capture_manifest_bin -- "$MOUNT_PATH" > built_manifest.ndjson
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman unmount capture-tmp
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman rm capture-tmp

# Diff
bazel run //devinfra/claude/web_env/tools:diff_manifests_bin -- \
  live_manifest.ndjson built_manifest.ndjson -o diff_report.md
```

## Storage Driver Choice (obsolete)

The gVisor sandbox root filesystem was **9p** (30 GB), which is slow and lacks xattr.
podman storage was always put on **tmpfs** — ~10x faster, 315 GB of space.

| Driver           | Config                              | Layer caching | Layer limit | Speed                    |
| ---------------- | ----------------------------------- | ------------- | ----------- | ------------------------ |
| Overlay on tmpfs | `driver = "overlay"`                | Yes           | ~54 layers  | Fast (cached steps skip) |
| VFS on tmpfs     | `driver = "vfs"` + `--layers=false` | No            | None        | ~20 min full rebuild     |
| VFS on 9p        | Default podman config               | No            | None        | ~60 min (slow I/O)       |

The 98-step Dockerfile exceeded the ~54 layer limit, so VFS on tmpfs was used.

## Sandbox Constraints (obsolete)

Key constraints when building under gVisor:

- **9p root**: No xattr, no overlay. Use tmpfs for container storage.
- **No `podman run`**: Use `podman create` + `podman mount` for inspection.
- **`--format=docker`**: Required. Buildah default causes SIGPIPE under gVisor.
- **`--network=host`**: Required. No bridge networking in gVisor.
- **Disk budget**: 30 GB on 9p, 315 GB on tmpfs. Always prefer tmpfs.
