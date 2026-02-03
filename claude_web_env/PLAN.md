# Current Plan

## Priority 1: Verify Build v11

Run a full build with the latest changes:

```bash
# Set up tmpfs storage (required for reasonable build times)
mount -t tmpfs -o size=200G,exec tmpfs /tmp/tmpfs-exec
mkdir -p /tmp/tmpfs-exec/containers/{storage,run}
cat > /tmp/storage-tmpfs-vfs.conf << 'EOF'
[storage]
driver = "vfs"
runroot = "/tmp/tmpfs-exec/containers/run"
graphroot = "/tmp/tmpfs-exec/containers/storage"
EOF

cd claude_web_env
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
  podman build --layers=false --network=host --isolation=oci \
  --format=docker -t claude-code-web-recreated .
```

Recent changes to verify:

- **Stripping**: Removes `/usr/share/doc`, `/usr/share/man`, `/usr/include`, `/usr/sbin`
- **APT sources**: Uses exact deb822 `.sources` files matching live container
- **process_api**: Baked in from `reference/process_api.gz`
- **Root dotfiles**: `.bashrc`, `.profile`, `.wget-hsts` baked in
- **python3-doc**: Now installed in Dockerfile
- **Session hook artifacts**: New exclusion category for files created by `tools/claude_hooks`

## Priority 2: Stricter Version Pinning

Remaining ~200 hash differences are package version drift (util-linux, binutils, gdb).
To achieve exact binary matching:

1. Identify exact versions in live: `dpkg -l | grep -E 'util-linux|binutils|gdb'`
2. Create APT preference files to pin these versions
3. If versions unavailable in main repos, use snapshot.ubuntu.com with a specific date

## Priority 3: Multi-Stage Build for Caching

The 98-step Dockerfile exceeds the overlay layer limit (~54 layers). To enable caching:

1. Split into multi-stage build with <50 steps per stage
2. Final stage combines outputs
3. Enables overlay storage driver for fast incremental rebuilds

## Low Priority: Investigate Remaining Diffs

After achieving <50 differences, investigate individual content changes:

- Timestamps embedded in binaries
- Build IDs in ELF headers
- Compilation-time constants

May require post-processing (strip, objcopy) to match exactly.
