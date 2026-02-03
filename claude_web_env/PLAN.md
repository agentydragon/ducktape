# Current Plan

## Priority 1: Verify Build v11 (Requires Fresh Session)

The build was blocked by **kernel keyring quota exhaustion** after ~62 RUN steps.
This is a gVisor limitation documented in `docs/sandbox-investigation.md`.

**Next session**: Run a full build with the latest changes:

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
- **Overlay limit**: 55 total layers (54 RUN/COPY/ADD + 1 FROM)
- **Keyring limit**: ~60-70 RUN steps per session

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

**Result**: 84 - 31 = **53 layers** (under 55 limit!)

With consolidation, **single-stage build with overlay caching becomes possible**.
No multi-stage split needed.

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

### gVisor Limitations Discovered

1. **Kernel keyring quota**: ~60-70 keyrings per session. Each buildah RUN creates
   a keyring. Quota does NOT reset when containers are cleaned up.
2. **Overlay layer limit**: 55 total layers due to mount option string page size limit.
3. **Overlay works on tmpfs**: Tested with 55-layer build - cache reuse confirmed.
