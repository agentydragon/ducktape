@README.md

## Agent Instructions

- **Manifest format is NDJSON**. Use `uv run --script tools/capture-manifest.py` to capture.
- **Built manifest uses podman mount**, not `podman run` (gVisor NoCgroups). See README.
- **Container content goes in `rootfs/`** — organized to mirror live filesystem paths.
- **Exclusions** in `exclusions.yaml` have inline comments. Use `volatile_paths` for non-deterministic tool installations. Use `only_in_live` only for things that truly cannot be reproduced.
- **Prefer baking files in** over excluding them. Snapshot and COPY into `rootfs/` rather than adding to `only_in_live`.
- **Reference snapshots go in `reference/`** — these are documentation, not build inputs.
- **Do not commit secrets or tokens**. Reference files have tokens redacted.
- **Pydantic models** in `tools/manifest.py` are shared between capture and diff tools.

## Storage Setup (Critical for Performance)

The gVisor sandbox root filesystem is **9p** (30 GB), which is slow and lacks xattr.
**Always use tmpfs** for podman storage — it's ~10x faster and has 315 GB of space.

```bash
# Set up tmpfs storage (do this FIRST, before any podman operations)
mount -t tmpfs -o size=200G,exec tmpfs /tmp/tmpfs-exec
mkdir -p /tmp/tmpfs-exec/containers/{storage,run}
```

### Storage driver choice

**Overlay on tmpfs** provides layer caching (unchanged Dockerfile steps skip instantly).
However, it hits a kernel mount option page size limit (~4096 bytes for `lowerdir`)
at around 50-55 layers. Our 98-step Dockerfile exceeds this at step ~76.

**VFS on tmpfs** (`--layers=false`) has no layer limit. No caching, but tmpfs I/O
is fast enough for a full rebuild in ~20 minutes.

| Driver | Config | Layer caching | Layer limit | Speed |
|--------|--------|---------------|-------------|-------|
| Overlay on tmpfs | `driver = "overlay"` + `--layers=true` | Yes | ~54 layers | Fast (cached steps skip) |
| VFS on tmpfs | `driver = "vfs"` + `--layers=false` | No | None | ~20 min full rebuild |
| VFS on 9p | Default podman config | No | None | ~60 min (slow I/O) |

**Recommendation**: Use VFS on tmpfs for the full 98-step Dockerfile. If the Dockerfile
is restructured into multi-stage builds with <50 steps per stage, overlay becomes viable.

### Build command

```bash
# VFS on tmpfs (current recommended approach)
cat > /tmp/storage-tmpfs-vfs.conf << 'EOF'
[storage]
driver = "vfs"
runroot = "/tmp/tmpfs-exec/containers/run"
graphroot = "/tmp/tmpfs-exec/containers/storage"
EOF

cd claude_web_env
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
  podman build --layers=false \
    --network=host --isolation=oci \
    --runtime=/usr/local/bin/crun-gvisor-wrapper \
    --format=docker \
    -t claude-code-web-recreated .
```

### Overlay with layer caching (for smaller Dockerfiles or multi-stage)

```bash
cat > /tmp/storage-overlay.conf << 'EOF'
[storage]
driver = "overlay"
runroot = "/tmp/tmpfs-exec/containers/run"
graphroot = "/tmp/tmpfs-exec/containers/storage"
EOF

CONTAINERS_STORAGE_CONF=/tmp/storage-overlay.conf \
  podman build \
    --network=host --isolation=oci \
    --runtime=/usr/local/bin/crun-gvisor-wrapper \
    --format=docker \
    -t claude-code-web-recreated .
```

## Tool Availability

- **podman 4.9.3 + buildah 1.33.7**: Available and working
- **docker / buildx / BuildKit**: NOT available in the sandbox. `buildx` is a Docker CLI plugin requiring BuildKit daemon — not compatible with podman.
- **fuse-overlayfs**: Installed but broken — gVisor lacks `FUSE_CAP_READDIRPLUS` support

## Manifest Capture and Diff Workflow

```bash
# Capture live manifest (ground truth)
uv run --script tools/capture-manifest.py > live-manifest.ndjson

# Create container and mount for inspection (can't use podman run in gVisor)
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
  podman create --name capture-tmp localhost/claude-code-web-recreated /bin/true
MOUNT_PATH=$(CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman mount capture-tmp)
uv run --script tools/capture-manifest.py "$MOUNT_PATH" > built-manifest.ndjson
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman unmount capture-tmp
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman rm capture-tmp

# Diff
uv run --script tools/diff-manifests.py \
  live-manifest.ndjson built-manifest.ndjson \
  --exclusions exclusions.yaml -o diff-report.md
```

## Sandbox Constraints

See <docs/sandbox-investigation.md> for full details. Key constraints:

- **9p root**: No xattr, no overlay. Use tmpfs for container storage.
- **No `podman run`**: crun fails on `/proc/self/setgroups` in nested containers.
  Use `crun-gvisor-wrapper` (injects `run.oci.keep_original_groups=1`).
  For inspection, use `podman create` + `podman mount`.
- **`--format=docker`**: Required. Buildah default `RUN` output causes SIGPIPE under gVisor.
- **`--network=host`**: Required. No bridge networking in gVisor.
- **Disk budget**: 30 GB on 9p, 315 GB on tmpfs (`/dev/shm`). Always prefer tmpfs.

## Next Action Items (for continuation)

### Priority 1: Verify Build v11

Run a full build with the new stripping and version pins, then diff:

```bash
# Build (~20 min)
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
  podman build --layers=false -t claude-code-web-recreated . 2>&1 | tee build.log

# Capture and diff (see workflow above)
```

Expected: Stripping should remove ~27K files that were previously "only in built".
The python3-apt and PHP 8.4 pins should eliminate those version mismatches.

### Priority 2: Stricter Version Pinning

The remaining ~200 hash differences are package version drift (util-linux, binutils,
gdb, etc.). To achieve exact binary matching:

1. Identify the exact versions in live: `dpkg -l | grep -E 'util-linux|binutils|gdb'`
2. Create APT preference files to pin these versions
3. If versions aren't available in main repos, use snapshot.ubuntu.com with a
   specific date for all APT sources

### Priority 3: Multi-Stage Build for Caching

The 98-step Dockerfile exceeds the overlay layer limit (~54 layers). To enable
layer caching:

1. Split Dockerfile into multi-stage build with <50 steps per stage
2. Each stage builds part of the environment
3. Final stage combines outputs
4. This allows using overlay storage driver for fast incremental rebuilds

### Priority 4: BuildKit Cache Mounts

Podman 4.9.3 supports `RUN --mount=type=cache` natively. However, under gVisor:

- **Single cache mount per RUN**: Works
- **Multiple cache mounts per RUN**: Fails (exit status 100)
- **`sharing=locked` option**: Fails under gVisor

If using cache mounts, use separate RUN instructions for each cached directory.

### Low Priority: Investigate Remaining Diffs

After achieving <50 differences, investigate individual file content changes.
Some may be:

- Timestamps embedded in binaries
- Build IDs in ELF headers
- Compilation-time constants

These may require post-processing (strip, objcopy) to match exactly.
