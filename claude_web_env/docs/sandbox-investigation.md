# Claude Code Web Sandbox Investigation

Precise characterization of the gVisor sandbox environment used by Claude Code
web sessions, with focus on container build capabilities and storage drivers.

## Runtime Identity

| Property | Value |
|----------|-------|
| Kernel | gVisor (reports `4.4.0`, boot: `vmlinuz-4.4.0-gvisor`) |
| Platform | x86_64 GNU/Linux |
| Cgroup hierarchy | v1 (7 controllers: cpu, cpuacct, cpuset, devices, job, memory, pids) |
| Container ID format | `container_{session_id}--claude_code_remote--{instance_id}` |
| Root UID | 0 (runs as root inside sandbox) |
| CPU | 16 cores |
| Memory | 21 GiB |

gVisor emulates Linux kernel 4.4.0 but supports many newer syscalls. The kernel
version string is hardcoded and does not reflect the actual gVisor release
version. The `dmesg` output includes humorous fake boot messages ("Adversarially
training Redcode AI...", "Preparing for the zombie uprising...").

## Filesystem Layout

| Mount point | Type | Size | Notes |
|-------------|------|------|-------|
| `/` | 9p (v9fs) | 30 GB | Root filesystem, shared with `/tmp` |
| `/dev/shm` | tmpfs | 315 GB | Shared memory, `noexec,nosuid` by default |
| `/dev` | tmpfs (devtmpfs) | — | Minimal device nodes |
| `/proc` | proc | — | gVisor-emulated procfs |
| `/sys` | sysfs | — | Limited sysfs |

The 9p filesystem is the primary I/O bottleneck. It supports symlinks, hardlinks,
and standard POSIX operations, but **does not support extended attributes (xattr)**.

## Capabilities

Granted (bitmask `0xa82c35fb`):

| Capability | Status |
|------------|--------|
| `CAP_CHOWN` | granted |
| `CAP_DAC_OVERRIDE` | granted |
| `CAP_FOWNER` | granted |
| `CAP_FSETID` | granted |
| `CAP_KILL` | granted |
| `CAP_SETGID` | granted |
| `CAP_SETUID` | granted |
| `CAP_SETPCAP` | granted |
| `CAP_NET_BIND_SERVICE` | granted |
| `CAP_NET_ADMIN` | granted |
| `CAP_NET_RAW` | granted |
| `CAP_SYS_CHROOT` | granted |
| `CAP_SYS_PTRACE` | granted |
| `CAP_SYS_ADMIN` | granted |
| `CAP_MKNOD` | granted (but `mknod` fails: EPERM) |
| `CAP_AUDIT_WRITE` | granted |
| `CAP_SETFCAP` | granted |

Notable absences: `CAP_DAC_READ_SEARCH`, `CAP_SYS_MODULE`, `CAP_SYS_RAWIO`,
`CAP_SYS_RESOURCE`, `CAP_SYS_TIME`.

## Namespace Support

All Linux namespace types are functional via `unshare()`:

| Namespace | Status |
|-----------|--------|
| `CLONE_NEWUSER` | works |
| `CLONE_NEWNS` | works |
| `CLONE_NEWPID` | works |
| `CLONE_NEWNET` | works |
| `CLONE_NEWUTS` | works |
| `CLONE_NEWIPC` | works |
| `CLONE_NEWCGROUP` | works |

This is why podman/buildah can run container builds (they use `CLONE_NEWNS`
and `CLONE_NEWUSER`).

## Syscall Support

| Syscall | Status | Notes |
|---------|--------|-------|
| `mount(overlay)` on 9p | EINVAL | 9p lacks xattr support required by overlay |
| `mount(overlay)` on tmpfs | works | tmpfs supports xattr |
| `mount(tmpfs)` | works | Can create new tmpfs mounts with arbitrary options |
| `mount -o remount` | works | Can change mount flags (e.g., remove `noexec`) |
| `xattr` on 9p | ENOTSUP | Extended attributes not supported on 9p |
| `xattr` on tmpfs | works | Full xattr support |
| `mknod` | EPERM | Even with `CAP_MKNOD` |
| `chroot` | works | |
| FUSE (`/dev/fuse`) | partial | FUSE mounts succeed but `FUSE_CAP_READDIRPLUS` (0x40) is not supported; fuse-overlayfs silently fails to enumerate lower dir |
| `inotify` | works | |
| `epoll` | works | |
| `eventfd` | works | |
| `memfd_create` | works | |
| `pipe2` | works | |
| `sendfile` | works | |
| `timerfd_create` | works | |
| `signalfd` | EINVAL | |

## Supported Filesystem Types

From `/proc/filesystems`:

- `9p` — primary root filesystem
- `overlay` — **works on tmpfs**, fails on 9p (no xattr)
- `tmpfs` — fully functional
- `mqueue`, `cgroup`, `devtmpfs`, `proc`, `sysfs`, `devpts`, `erofs`, `fuse`

## Storage Driver Analysis for Podman

### VFS on 9p (default)

- Works on 9p — no special kernel requirements
- Each layer is a full filesystem copy (no deduplication)
- 98-step Dockerfile with ~6 GB final image requires ~6 GB per cached layer
- With `--layers=true`: exhausts 30 GB 9p disk by step 20
- With `--layers=false`: fits in 30 GB but no caching (full rebuild every time)
- 9p I/O is slow (~60 min for full build)

### VFS on tmpfs (current recommendation for large Dockerfiles)

- Same semantics as VFS on 9p, but stored on 315 GB tmpfs
- tmpfs I/O is ~10x faster than 9p (~20 min for full build)
- Still no layer caching with `--layers=false`, but fast enough for full rebuilds
- Use when overlay hits the layer count limit (>54 layers)

### Overlay on tmpfs (works, with layer count limit)

- **Native overlay works on tmpfs** because tmpfs supports xattr
- `/dev/shm` is 315 GB tmpfs (far larger than 30 GB 9p root)
- `/dev/shm` has `noexec` by default, but `mount -o remount,exec` works
- Alternatively, `mount -t tmpfs -o size=200G,exec tmpfs /path` creates a new tmpfs
- Layer deduplication: only diffs stored per layer, 8 MB for alpine vs full copy
- Layer caching works: unchanged steps reuse cached layers instantly

**Layer count limit**: The kernel imposes a page size limit (~4096 bytes) on the
`lowerdir` mount option string. Each overlay layer adds ~70 characters to this string.
At ~54 layers, the string exceeds the limit and `mount(overlay)` returns EINVAL.
Our 98-step Dockerfile hits this at step ~76.

**Workaround**: Restructure the Dockerfile into multi-stage builds where each stage
has <50 steps. Each stage starts a fresh overlay stack, resetting the layer count.
With 41 RUN instructions across 98 total steps, 3 stages of ~33 steps each would
stay within the limit while enabling full layer caching.

Configuration:

```conf
# /tmp/storage-overlay.conf
[storage]
driver = "overlay"
runroot = "/tmp/tmpfs-exec/containers/run"
graphroot = "/tmp/tmpfs-exec/containers/storage"
```

```bash
mount -t tmpfs -o size=200G,exec tmpfs /tmp/tmpfs-exec
mkdir -p /tmp/tmpfs-exec/containers/{storage,run}
CONTAINERS_STORAGE_CONF=/tmp/storage-overlay.conf podman build ...
```

### fuse-overlayfs (broken)

- Installed at `/usr/bin/fuse-overlayfs` (version 1.13-dev, FUSE 3.14.0)
- `/dev/fuse` exists and is accessible
- Mount succeeds but gVisor's FUSE doesn't support `FUSE_CAP_READDIRPLUS`
- Result: upper layer writes work, but lower layer content is invisible
- **Not usable for podman** — would produce empty/broken layers

### Overlay on 9p (broken)

- `mount -t overlay` returns EINVAL on 9p filesystem
- Root cause: 9p doesn't implement extended attributes (`setxattr` → ENOTSUP)
- Overlay requires xattr for `opaque` directory markers and layer metadata
- **Cannot work** regardless of capabilities or mount options

## Device Nodes

Minimal set provided by gVisor:

```
/dev/full, /dev/fuse, /dev/null, /dev/ptmx → pts/ptmx,
/dev/pts/, /dev/random, /dev/shm/, /dev/tty, /dev/urandom, /dev/zero
/dev/fd → /proc/self/fd
/dev/net/ (tun?)
```

No block devices, no loop devices, no `/dev/mapper`.

## BuildKit Cache Mounts

Podman 4.1.1+ supports BuildKit-style `RUN --mount=type=cache` syntax natively
(no separate BuildKit daemon needed). However, under gVisor:

| Feature | Status | Notes |
|---------|--------|-------|
| Single cache mount | works | `RUN --mount=type=cache,target=/path` |
| Multiple cache mounts | fails | Exit status 100 with gVisor |
| `sharing=locked` option | fails | gVisor doesn't support this mode |

**Workaround**: Use separate RUN instructions for each cache mount, or combine
directories under a single mount target.

## Implications for Container Builds

1. **Always store on tmpfs**, not 9p. Mount a new exec-enabled tmpfs and point
   `CONTAINERS_STORAGE_CONF` there. This gives 315 GB space and ~10x faster I/O.
2. **For Dockerfiles with >54 layers**: Use VFS on tmpfs with `--layers=false`.
   Layer caching via overlay hits the mount option page size limit at ~54 layers.
3. **For Dockerfiles with <54 layers**: Use overlay on tmpfs for layer caching.
   Multi-stage builds can keep each stage under the limit.
4. **Cache mounts work with limitations**: Single `--mount=type=cache` works,
   but multiple mounts in one RUN fail under gVisor.
5. **Cannot use `podman run`** — crun fails opening `/proc/self/setgroups` inside
   nested containers. Use the `crun-gvisor-wrapper` which injects
   `run.oci.keep_original_groups=1` annotation. For inspection, use
   `podman create` + `podman mount` instead.
6. **`--format=docker`** is needed because buildah's default `RUN` output causes
   SIGPIPE under gVisor when the build pipe closes
7. **`--network=host`** is required (no bridge networking in gVisor)
8. **No docker/buildx/BuildKit**: Only podman + buildah available. buildx is a
   Docker CLI plugin requiring BuildKit daemon — not compatible with podman.
