@README.md

## Agent Instructions

- **Container content goes in `rootfs/`** — organized to mirror live filesystem paths.
- **Reference snapshots go in `reference/`** — documentation, not build inputs.
- **Exclusions** in `exclusions.yaml` have inline comments. Use `volatile_paths` for non-deterministic tool installations. Use `only_in_live` only for things that truly cannot be reproduced.
- **Prefer baking files in** over excluding them. Snapshot and COPY into `rootfs/` rather than adding to `only_in_live`.
- **Do not commit secrets or tokens**. Reference files have tokens redacted.
- **Pydantic models** in `tools/manifest.py` are shared between capture and diff tools.

### Tool Availability

- **podman 4.9.3 + buildah 1.33.7**: Available and working
- **docker / buildx / BuildKit**: NOT available in the sandbox
- **fuse-overlayfs**: Installed but broken (gVisor lacks `FUSE_CAP_READDIRPLUS`)

## Next Action Items (for continuation)

### Priority 1: Verify Build v11

Run a full build with the stripping and version pins added in the previous session:

```bash
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
  podman build --layers=false --network=host --isolation=oci \
  --format=docker -t claude-code-web-recreated .
```

Expected: Stripping should remove ~27K files previously "only in built".
The python3-apt and PHP 8.4 pins should eliminate version mismatches.

### Priority 2: Stricter Version Pinning

Remaining ~200 hash differences are package version drift (util-linux, binutils, gdb).
To achieve exact binary matching:

1. Identify exact versions in live: `dpkg -l | grep -E 'util-linux|binutils|gdb'`
2. Create APT preference files to pin these versions
3. If versions unavailable in main repos, use snapshot.ubuntu.com with a specific date

### Priority 3: Multi-Stage Build for Caching

The 98-step Dockerfile exceeds the overlay layer limit (~54 layers). To enable caching:

1. Split into multi-stage build with <50 steps per stage
2. Final stage combines outputs
3. Enables overlay storage driver for fast incremental rebuilds

### Low Priority: Investigate Remaining Diffs

After achieving <50 differences, investigate individual content changes:
- Timestamps embedded in binaries
- Build IDs in ELF headers
- Compilation-time constants

May require post-processing (strip, objcopy) to match exactly.
