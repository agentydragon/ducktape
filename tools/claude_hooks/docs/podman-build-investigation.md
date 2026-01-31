# podman build in gVisor: Investigation Notes

Investigation into making `podman build` work natively in gVisor, rather than
relying on the `podman run` + `podman commit` workaround.

## Summary

`podman build` fails in gVisor through **both** isolation modes:

| Isolation     | crun 1.14.1 (system)                              | crun 1.25.1+                                     |
| ------------- | ------------------------------------------------- | ------------------------------------------------ |
| OCI (default) | `/proc/self/setgroups: No such file or directory` | SIGPIPE/broken pipe for any forked child process |
| Chroot        | `/dev/null: Read-only file system`                | `/dev/null: Read-only file system`               |

Neither path produces a working `podman build`. The `podman run` + `podman commit`
workaround documented in <gvisor-dockerfile-build.md> remains the only viable
approach.

## Issue 1: OCI isolation — setgroups (crun 1.14.1)

### Symptom

```
error running container: from /usr/bin/crun creating container for [...]:
error opening file `/proc/self/setgroups`: No such file or directory
```

### Root cause

crun's `can_setgroups()` function (linux.c, around line 2905 in v1.14.1) calls
`read_all_file("/proc/self/setgroups", ...)` without handling ENOENT. gVisor
does not implement `/proc/self/setgroups`, so the call fails fatally.

The `run.oci.keep_original_groups=1` annotation configured in `containers.conf`
returns early from `can_setgroups()` and works for `podman run`, but buildah
does **not** propagate container annotations to intermediate build containers.

### Fix in newer crun

Fixed in crun 1.25.1+ (commit `3f5258a0`): `can_setgroups()` was refactored to
use `libcrun_open_proc_file()` which handles ENOENT gracefully, returning 1
(allow setgroups) instead of an error.

## Issue 2: OCI isolation — SIGPIPE (crun 1.25.1+)

### Symptom

Upgrading crun to 1.25.1 or 1.26 fixes the setgroups error, but introduces a
new failure: any `RUN` step that spawns a child process writing to stdout gets
SIGPIPE (exit code 141) or "container exited on broken pipe" (exit code 1).

```
STEP 2/2: RUN seq 1 5
Error: building at STEP "RUN seq 1 5": while running runtime: exit status 141
```

### Behavior matrix

| Command                                      | Result             |
| -------------------------------------------- | ------------------ |
| `echo hello` (builtin)                       | OK                 |
| `echo a; echo b; echo c` (multiple builtins) | OK                 |
| `(echo hello)` (subshell = fork)             | SIGPIPE            |
| `/bin/echo hello` (external)                 | SIGPIPE            |
| `/bin/true` then `echo end`                  | SIGPIPE (on echo)  |
| `sleep 10` (external, no stdout)             | OK                 |
| `seq 1 1`                                    | SIGPIPE            |
| Any command under `strace -ff`               | OK (timing change) |

### Analysis

The SIGPIPE means the pipe read-end is being closed before the child process can
write. Key observations:

1. **Shell builtins work** — bash writes directly to fd 1 (the pipe). No fork.
2. **External commands fail** — bash fork()+exec(). The child inherits fd 1 but
   gets SIGPIPE when writing.
3. **Subshells fail** — `(echo hello)` forks without exec. The child's fd 1 is
   the same pipe, but writing fails.
4. **`/bin/true` (no output) causes subsequent builtin echo to fail** — the
   fork+exec for /bin/true somehow invalidates the parent's fd 1.
5. **strace makes it work** — classic race condition indicator.
6. **sleep works** — no stdout output, so no SIGPIPE.

### Mechanism (buildah source: `run_common.go`)

buildah's `runUsingRuntime` creates stdio pipes (`runMakeStdioPipe` — uses
`unix.Pipe()` **without O_CLOEXEC**), then:

1. Passes pipe write-ends as `crun create`'s stdout/stderr
2. Starts a poll-based I/O relay goroutine (`runCopyStdio` → `runCopyStdioPassData`)
3. Runs `crun start` to begin container execution
4. Polls `crun state` every 100ms to detect container exit
5. When container stops: closes `finishCopy[1]` pipe → relay goroutine sees
   POLLHUP on `finishCopy[0]` → returns → deferred cleanup closes pipe read-ends

The race: for fast commands, the container can exit before the relay goroutine
reads all data. The `finishCopy` signal causes the relay to exit, closing the
pipe read-ends. If a child process in the container hasn't finished writing yet,
it gets SIGPIPE.

This race is likely exacerbated by gVisor's different scheduling/pipe behavior
compared to native Linux. The strace overhead is enough to change timing and
avoid the race.

### Not a gVisor pipe/fork bug

Tested directly: pipe + fork + poll works correctly in gVisor (both on host and
inside containers via `podman run`). The issue is specific to buildah's stdio
relay interacting with crun's container lifecycle under gVisor's scheduling.

## Issue 3: Chroot isolation — read-only /dev/null

### Symptom

```
/bin/sh: 1: cannot create /dev/null: Read-only file system
```

### Root cause

In chroot isolation, buildah creates a minimal `/dev` using a tmpfs mount. On
gVisor, the devtmpfs filesystem is mounted read-only. This cannot be overridden
with volume mounts (`--volume /dev/null:/dev/null:rw`), `mount -t tmpfs`, or
file replacement (`rm /dev/null; touch /dev/null`).

### Impact

Many packages fail during install because their post-install scripts use
`/dev/null`:

- `ca-certificates` (critical for HTTPS)
- `python3` (rtupdate hooks)
- `apt-key` (GPG verification)

Simple commands work if they avoid `/dev/null`, but real-world Dockerfile builds
invariably need it.

## Solution: chroot isolation + CAP_SYS_ADMIN

The chroot `/dev/null` issue is solvable. The host `/dev` IS a writable tmpfs —
buildah just bind-mounts it read-only for security. Adding `CAP_SYS_ADMIN` lets
the container `mount -o remount,rw /dev`.

```bash
podman build --network=host --isolation=chroot --cap-add=SYS_ADMIN \
  -f Dockerfile .
```

Each `RUN` step that touches `/dev/null` must prepend `mount -o remount,rw /dev &&`
(mount state doesn't persist across steps):

```dockerfile
RUN mount -o remount,rw /dev \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/*
```

This was verified by building the full RBE worker image (832 MB, 30+ packages
including build-essential, podman, Chromium shared libs) successfully.

### Why OCI isolation remains broken

- **OCI isolation** has a race condition in buildah's stdio relay that cannot
  be fixed via configuration — it requires an upstream buildah fix or gVisor
  scheduling changes
- **Chroot isolation** works with the `CAP_SYS_ADMIN` + remount workaround

### Why `podman run` works without workarounds

- Uses conmon for stdio management (not buildah's poll loop), avoiding the
  SIGPIPE race
- Respects `containers.conf` annotations for `keep_original_groups`
- No /dev/null issues because the container gets a proper /dev from crun's OCI
  setup
