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

### Current Plan

See <PLAN.md> for current work items and priorities.
