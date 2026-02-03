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

## Priority 2: Layer Consolidation for Caching

### Current Dockerfile Stats

- **84 layer-creating instructions**: 1 FROM + 43 RUN + 40 COPY
- **Overlay limit**: ~47-50 layers (kernel page size limit on mount options)

### Consolidation Opportunities

| Category             | Current | After | Savings |
| -------------------- | ------- | ----- | ------- |
| profile.d COPY       | 8       | 1     | 7       |
| use-node COPY        | 3       | 1     | 2       |
| use-ruby COPY        | 3       | 1     | 2       |
| create-venv COPY     | 4       | 1     | 3       |
| CA certs COPY        | 2       | 1     | 1       |
| Root dotfiles COPY   | 5       | 1     | 4       |
| Claude settings COPY | 6       | 2     | 4       |
| chmod RUNs           | 10      | 2     | 8       |
| **Total**            | 41      | 10    | **31**  |

**Result**: 84 - 31 = **53 layers** (may still exceed ~47-50 overlay limit)

With consolidation + potential 2-stage split, **overlay caching becomes possible**.
Note: containers/storage layer deduplication may allow more layers in practice.

### Implementation

1. Group related COPY instructions using wildcards or directories
2. Consolidate all chmod operations into 1-2 RUN instructions
3. Add layer number comments for maintainability (per user request)

## Priority 3: Stricter Version Pinning

Remaining ~200 hash differences are package version drift (util-linux, binutils, gdb).
To achieve exact binary matching:

1. Identify exact versions in live: `dpkg -l | grep -E 'util-linux|binutils|gdb'`
2. Create APT preference files to pin these versions
3. If versions unavailable in main repos, use snapshot.ubuntu.com with a specific date

## Low Priority: Investigate Remaining Diffs

After achieving <50 differences, investigate individual content changes:

- Timestamps embedded in binaries
- Build IDs in ELF headers
- Compilation-time constants

May require post-processing (strip, objcopy) to match exactly.

## Session Notes

### gVisor Limitations and Fixes

1. **Kernel keyring quota**: ✅ **FIXED** — `crun-gvisor-wrapper` now injects
   `--no-new-keyring` to prevent keyring creation. Tested with 70+ RUN steps.
2. **Overlay layer limit**: ~47-50 layers per overlay stack.
   - Kernel limit: 4096 bytes (1 page) for mount options string
   - Per-layer: ~80 bytes (graphroot path + 26-char symlink + separator)
   - Empirically verified: 90 layers at 4066 bytes succeeded, 91 at 4110 bytes failed
   - containers/storage deduplicates layers across images, so effective limit varies
3. **Overlay works on tmpfs**: Tested with 90-layer build - cache reuse confirmed.

### Keyring Fix Details

- **Root cause**: crun creates a session keyring for each container via
  `keyctl(KEYCTL_JOIN_SESSION_KEYRING)` for credential isolation.
- **gVisor limit**: ~60-70 keyrings per session, not recoverable.
- **Solution**: `--no-new-keyring` flag tells crun to skip keyring creation.
- **Implementation**: `tools/claude_hooks/config/podman/crun_gvisor_wrapper.py`
  now injects this flag for all `crun create` and `crun run` commands.

### Build Verification

**Full 111-step Dockerfile build completed successfully** (2026-02-03):

- Image: `localhost/claude-code-web-recreated:latest`
- Size: 5.66 GB
- All 111 steps completed without keyring quota errors
- This confirms the `--no-new-keyring` fix works for production Dockerfile builds
