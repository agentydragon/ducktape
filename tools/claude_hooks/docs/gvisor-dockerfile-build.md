# Building Dockerfiles in gVisor

`podman build` works under gVisor with no extra flags — all gVisor workarounds are pre-configured:

```bash
podman build -t my-image .
```

Typical builds (package installs, file copies, compilation) work without any special Dockerfile changes. A SIGPIPE issue exists for RUN steps that produce very large stdout (>~3MB) — see [SIGPIPE on large RUN output](#sigpipe-on-large-run-output) for details and workarounds.

## What's configured automatically

| Setting             | Mechanism                                              | Fixes                                                                                            |
| ------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| OCI isolation       | `BUILDAH_ISOLATION=oci` env var                        | Chroot mode creates a devtmpfs that gVisor mounts read-only, breaking `/dev/null`                |
| Host networking     | `netns = "host"` in `containers.conf`                  | gVisor doesn't support private container networking                                              |
| Docker format       | `image_default_format = "docker"` in `containers.conf` | OCI image format doesn't support `SHELL` directive                                               |
| crun-gvisor-wrapper | `runtime = "crun-gvisor"` in `containers.conf`         | gVisor lacks `/proc/self/setgroups`; wrapper injects `run.oci.keep_original_groups=1` annotation |
| Host user namespace | `userns = "host"` in `containers.conf`                 | Skips user namespace creation (gVisor restriction)                                               |

## Disk space

The VFS storage driver copies the full filesystem for every layer. For large images, use `--layers=false` (or `BUILDAH_LAYERS=false`) to disable layer caching and keep only one working copy:

```bash
podman build --layers=false -t my-image .
```

### crun-gvisor-wrapper detail

gVisor doesn't provide `/proc/self/setgroups`, which crun's `deny_setgroups()` tries to open. The `run.oci.keep_original_groups=1` OCI annotation tells crun to skip this call.

For `podman run`, the annotation is set in `containers.conf` and works automatically. For `podman build`, buildah doesn't propagate `containers.conf` annotations to intermediate build containers. The `crun-gvisor-wrapper` script (installed by `podman_service.py`, registered as the default runtime via `[engine.runtimes]`) intercepts crun invocations, injects the annotation into the OCI `config.json`, then exec's the real crun.

## SIGPIPE on large RUN output

RUN steps that write more than ~3MB to stdout (roughly 440k short lines) fail with `container exited on broken pipe` (SIGPIPE / signal 13). This is a race condition in buildah's stdio relay, exacerbated by gVisor's higher I/O overhead.

**Typical builds are not affected.** Commands like `apk add`, `pip install`, `ls -laR /`, file copies, and compilation produce well under this threshold. The issue only manifests with synthetic high-output commands like `seq 1 500000`.

### Root cause

Tested with podman 4.9.3, buildah 1.33.7, crun 1.14.1, on gVisor (kernel 4.4.0).

The bug is in buildah's `runCopyStdioPassData` ([`run_common.go`](https://github.com/containers/buildah/blob/v1.33.7/run_common.go)). The function relays container stdout through a Unix pipe chain:

```
container process → pipe(64KB) → buildah stdio goroutine → fd 1 → podman
```

The race:

1. **Container writes to stdout pipe.** When the pipe buffer (64KB) is full, the container blocks on `write()`. buildah's stdio goroutine reads from the pipe in 8KB chunks ([line 881](https://github.com/containers/buildah/blob/v1.33.7/run_common.go#L881), [907](https://github.com/containers/buildah/blob/v1.33.7/run_common.go#L907)) and relays to its own fd 1.

2. **Main goroutine polls container state** every 100ms via `crun state` ([lines 661–698](https://github.com/containers/buildah/blob/v1.33.7/run_common.go#L661-L698)). When the container exits, it closes `finishCopy[1]` ([line 703](https://github.com/containers/buildah/blob/v1.33.7/run_common.go#L703)) to signal the stdio goroutine.

3. **Stdio goroutine returns immediately on `finishCopy`.** In `runCopyStdioPassData`, the `unix.Poll` loop checks `finishCopy[0]` at [line 984](https://github.com/containers/buildah/blob/v1.33.7/run_common.go#L984). When it has revents, the function returns — without draining remaining pipe data or relay buffers.

4. **Deferred close kills the pipe read ends** ([lines 780–781](https://github.com/containers/buildah/blob/v1.33.7/run_common.go#L780-L781)). If the container process is still blocked on `write()` to the (now-closed) pipe, it receives SIGPIPE.

5. **buildah reports the signal** as `container exited on broken pipe` ([line 1118](https://github.com/containers/buildah/blob/v1.33.7/run_common.go#L1118)), and the build fails with exit status 1.

This is NOT a gVisor-specific bug — the same code path exists on native Linux. gVisor's higher per-syscall overhead slows the relay, widening the race window. On native Linux the relay is fast enough that the race rarely triggers.

Evidence:

- `podman run` handles 1M+ lines fine (uses conmon for stdio, not buildah's relay)
- `RUN seq 1 500000; sleep 5` succeeds — the sleep gives the relay time to drain
- The bug exists in buildah main (latest commit at time of investigation) — not yet fixed upstream

### Workarounds

If a RUN step does produce very large stdout, redirect output inside the command:

```dockerfile
# Redirect to /dev/null (discard output)
RUN seq 1 1000000 > /dev/null 2>&1

# Or redirect to a log file (preserve output in the image)
RUN some-verbose-command > /tmp/build.log 2>&1
```

## Verifying shared library completeness

```bash
podman run --rm \
  -v /path/to/binary:/tmp/binary:ro \
  my-image:latest ldd /tmp/binary | grep "not found"
```

No output means all libraries resolve.

## Fallback: podman run + commit

If `podman build` still fails for a specific case, individual steps can be executed manually:

```bash
podman pull docker.io/library/ubuntu:24.04
IMAGE=docker.io/library/ubuntu:24.04

# RUN
podman run --rm=false --name build-step "$IMAGE" \
  /bin/sh -c 'apt-get update && apt-get install -y curl'
IMAGE=$(podman commit build-step | tail -1)
podman rm -f build-step

# COPY (mount source, cp inside container, commit)
podman run --rm=false --name build-step \
  -v /path/to/local/file.conf:/mnt/file.conf:ro \
  "$IMAGE" /bin/sh -c 'cp /mnt/file.conf /etc/file.conf'
IMAGE=$(podman commit build-step | tail -1)
podman rm -f build-step

podman tag "$IMAGE" my-image:latest
```
