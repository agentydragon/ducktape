# Claude Code Web Environment

Reproducible reconstruction of the Claude Code web container, verified by
full-filesystem manifest diffing.

**Goal**: Zero diff exclusions that aren't session-start-hook artifacts or
unavoidable runtime differences (`/proc`, `/sys`, caches). Any difference
fixable by updating the Dockerfile should be fixed there, not excluded.

## Quick Start

```bash
cd claude_web_env

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
bazel run //claude_web_env/tools:capture_manifest -- > live-manifest.ndjson

# Capture built manifest (via podman mount — can't podman run under gVisor)
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf \
  podman create --name capture-tmp localhost/claude-code-web-recreated /bin/true
MOUNT_PATH=$(CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman mount capture-tmp)
bazel run //claude_web_env/tools:capture_manifest -- "$MOUNT_PATH" > built-manifest.ndjson
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman unmount capture-tmp
CONTAINERS_STORAGE_CONF=/tmp/storage-tmpfs-vfs.conf podman rm capture-tmp

# Diff
bazel run //claude_web_env/tools:diff_manifests -- \
  live-manifest.ndjson built-manifest.ndjson \
  --exclusions exclusions.yaml -o diff_report.md
```

## Storage Driver Choice

The gVisor sandbox root filesystem is **9p** (30 GB), which is slow and lacks xattr.
Always use **tmpfs** for podman storage — it's ~10x faster and has 315 GB of space.

| Driver           | Config                              | Layer caching | Layer limit | Speed                    |
| ---------------- | ----------------------------------- | ------------- | ----------- | ------------------------ |
| Overlay on tmpfs | `driver = "overlay"`                | Yes           | ~54 layers  | Fast (cached steps skip) |
| VFS on tmpfs     | `driver = "vfs"` + `--layers=false` | No            | None        | ~20 min full rebuild     |
| VFS on 9p        | Default podman config               | No            | None        | ~60 min (slow I/O)       |

**Our 98-step Dockerfile exceeds the ~54 layer limit**, so use VFS on tmpfs.
Multi-stage builds with <50 steps per stage could enable overlay caching.

## Sandbox Constraints

Key constraints when building under gVisor (see <docs/sandbox-investigation.md>):

- **9p root**: No xattr, no overlay. Use tmpfs for container storage.
- **No `podman run`**: Use `podman create` + `podman mount` for inspection.
- **`--format=docker`**: Required. Buildah default causes SIGPIPE under gVisor.
- **`--network=host`**: Required. No bridge networking in gVisor.
- **Disk budget**: 30 GB on 9p, 315 GB on tmpfs. Always prefer tmpfs.

## Directory Layout

| Path              | Purpose                                                     |
| ----------------- | ----------------------------------------------------------- |
| `Dockerfile`      | Full container build from Ubuntu 24.04                      |
| `exclusions.yaml` | Diff-time exclusion rules (commented)                       |
| `rootfs/`         | **Container content** — mirrors live filesystem structure   |
| `tools/`          | Build/diff tooling (capture_manifest.py, diff_manifests.py) |
| `reference/`      | Reference snapshots from live (not used in build)           |
| `docs/`           | Container spec, sandbox investigation, skill definitions    |
| `proxy-ca/`       | Build-time TLS certificates (from TLS-inspecting proxy)     |

### `rootfs/` Structure

Container content files are organized to mirror their placement in the live
container filesystem. The Dockerfile COPYs from these paths:

```
rootfs/
├── etc/
│   ├── apt/
│   │   ├── preferences.d/     # APT version pins (php84-pin)
│   │   └── sources.list.d/    # PPA sources (deb822 format)
│   └── profile.d/             # Shell profile scripts
├── home/claude/
│   ├── .claude/               # Claude Code settings, hooks, skills
│   ├── scripts/               # Helper scripts directory
│   └── README.md
├── process_api/               # Proprietary process API server
│   └── process_api            # ELF binary from live container
├── root/
│   ├── .bashrc                # Shell config
│   ├── .profile               # Login profile
│   ├── .claude/               # Claude Code settings, skills
│   ├── .gitconfig
│   └── .local/bin/env
└── usr/local/
    ├── bin/                   # Helper scripts, environment-manager
    └── share/ca-certificates/ # CA certs
```

### `reference/` Contents

Snapshots captured from the live container:

- `versions-YYYY-MM-DD.yaml` — Structured version snapshot (runtimes, dpkg, npm globals, binary hashes)
- `environment-manager.gz` — Claude Code's environment manager (baked into build)
- `process_api.gz` — Anthropic's process API server (baked into build)
- `*-settings.json` — Claude Code settings (documentation only)
- `*-env-vars.txt` — Environment variable snapshots (documentation only)

The proprietary binaries are compressed with gzip and COPYed/decompressed in the Dockerfile.

## Manifest Format

NDJSON — one JSON object per line: `path`, `type` (`f`/`d`/`l`/`p`/`s`),
`perms`, `owner`, `group`, `size`, `sha256` (files ≤50MB), `link_target`.
Exclusions apply at diff time only, so manifests never need recapturing.

## Exclusion Categories

Configured in `exclusions.yaml`:

| Category                      | Purpose                                                     |
| ----------------------------- | ----------------------------------------------------------- |
| `skip_paths`                  | Ignored entirely (`/proc`, `/sys`, caches)                  |
| `volatile_paths`              | Differences expected (tool builds: rbenv, nvm, uv, rustup)  |
| `hash_may_differ`             | File must exist on both sides, content may differ           |
| `only_in_live`                | Expected only in live (proprietary binaries, runtime state) |
| `only_in_built`               | Expected only in built (npm cache artifacts)                |
| `ignore_owner`/`ignore_group` | Skip ownership (gVisor UID mapping)                         |

## Installed Runtimes

See `reference/versions-YYYY-MM-DD.yaml` for exact versions of all runtimes,
dpkg packages, npm globals, and binary hashes. Generated by
`//claude_web_env/tools:capture_versions`.

See <docs/container-spec.md> for runtime details and gVisor constraints.
