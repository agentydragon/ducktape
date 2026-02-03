# Claude Code Web Environment

Reproducible reconstruction of the Claude Code web container, verified by
full-filesystem manifest diffing.

## Quick Start

```bash
cd claude_web_env

# Set up overlay storage on tmpfs (315 GB, supports xattr unlike 9p root)
mount -t tmpfs -o size=200G,exec tmpfs /tmp/tmpfs-exec
mkdir -p /tmp/tmpfs-exec/containers/{storage,run}
cat > /tmp/storage-overlay.conf << 'EOF'
[storage]
driver = "overlay"
runroot = "/tmp/tmpfs-exec/containers/run"
graphroot = "/tmp/tmpfs-exec/containers/storage"
EOF

# Build with overlay layer caching
CONTAINERS_STORAGE_CONF=/tmp/storage-overlay.conf \
  podman build \
    --network=host --isolation=oci \
    --runtime=/usr/local/bin/crun-gvisor-wrapper \
    --format=docker \
    -t claude-code-web-recreated .

# Capture live manifest (ground truth)
uv run --script tools/capture-manifest.py > live-manifest.ndjson

# Capture built manifest (via podman mount — can't podman run under gVisor)
CONTAINERS_STORAGE_CONF=/tmp/storage-overlay.conf \
  podman create --name capture-tmp localhost/claude-code-web-recreated /bin/true
MOUNT_PATH=$(CONTAINERS_STORAGE_CONF=/tmp/storage-overlay.conf podman mount capture-tmp)
uv run --script tools/capture-manifest.py "$MOUNT_PATH" > built-manifest.ndjson
CONTAINERS_STORAGE_CONF=/tmp/storage-overlay.conf \
  podman unmount capture-tmp && \
CONTAINERS_STORAGE_CONF=/tmp/storage-overlay.conf \
  podman rm capture-tmp

# Diff
uv run --script tools/diff-manifests.py \
  live-manifest.ndjson built-manifest.ndjson \
  --exclusions exclusions.yaml -o diff-report.md
```

### Why overlay on tmpfs?

The gVisor sandbox root filesystem is 9p (30 GB), which doesn't support xattr —
required by the overlay filesystem driver. But `/dev/shm` is tmpfs (315 GB) with
full xattr support. By mounting a new exec-enabled tmpfs and pointing podman's
storage there, we get native overlay with **layer caching and deduplication**.

This means unchanged Dockerfile steps reuse cached layers instantly, and each
layer stores only its diff (not a full filesystem copy like VFS).

**Fallback** (if tmpfs is unavailable): add `--layers=false` and use the default
VFS driver on 9p. This works but requires a full rebuild every time.

See <docs/sandbox-investigation.md> for detailed sandbox characterization.

## Directory Layout

| Path | Purpose |
|------|---------|
| `Dockerfile` | Full container build from Ubuntu 24.04 |
| `exclusions.yaml` | Diff-time exclusion rules (commented) |
| `rootfs/` | **Container content** — mirrors live filesystem structure |
| `tools/` | Build/diff tooling (capture-manifest.py, diff-manifests.py) |
| `reference/` | Reference snapshots from live (not used in build) |
| `docs/` | Container spec, sandbox investigation, skill definitions |
| `proxy-ca/` | Build-time TLS certificates (from TLS-inspecting proxy) |

### `rootfs/` Structure

Container content files are organized to mirror their placement in the live
container filesystem. The Dockerfile COPYs from these paths:

```
rootfs/
├── etc/
│   ├── apt/preferences.d/     # APT version pins
│   └── profile.d/             # Shell profile scripts
├── home/claude/               # Claude user home
├── root/
│   ├── .gitconfig
│   └── .local/bin/env
└── usr/local/
    ├── bin/                   # Helper scripts, environment-manager
    └── share/ca-certificates/ # CA certs
```

### `reference/` Contents

Reference snapshots captured from the live container for documentation purposes.
**Not used during build** — these are for understanding the live environment:

- `*-settings.json` — Claude Code settings
- `*-env-vars.txt` — Environment variable snapshots
- `*.gz` — Compressed binaries (process_api, code-sign)

## Manifest Format

NDJSON — one JSON object per line: `path`, `type` (`f`/`d`/`l`/`p`/`s`),
`perms`, `owner`, `group`, `size`, `sha256` (files ≤50MB), `link_target`.
Exclusions apply at diff time only, so manifests never need recapturing.

## Exclusion Categories

Configured in `exclusions.yaml`:

| Category | Purpose |
|----------|---------|
| `skip_paths` | Ignored entirely (`/proc`, `/sys`, caches) |
| `volatile_paths` | Differences expected (tool builds: rbenv, nvm, uv, rustup) |
| `hash_may_differ` | File must exist on both sides, content may differ |
| `only_in_live` | Expected only in live (proprietary binaries, runtime state) |
| `only_in_built` | Expected only in built (npm cache artifacts) |
| `ignore_owner`/`ignore_group` | Skip ownership (gVisor UID mapping) |

## Installed Runtimes

| Component | Versions |
|-----------|----------|
| Node.js | 20.19.6, 21.7.3, 22.21.1 (active) |
| Python | 3.10, 3.11, 3.12, 3.13 |
| Ruby | 3.1.6, 3.2.6, 3.3.6 |
| Go | 1.24.7, 1.25.1 |
| Rust | stable (minimal) |
| Bun | 1.3.4 |
| Java | OpenJDK 21 |
| PHP | 8.4 |

See <docs/container-spec.md> for runtime details and gVisor constraints.
