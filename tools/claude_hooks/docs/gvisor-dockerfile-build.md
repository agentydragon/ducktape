# Building Dockerfiles in gVisor

`podman build` requires specific flags to work in gVisor:

```bash
podman build --network=host --isolation=chroot --cap-add=SYS_ADMIN \
  -f Dockerfile .
```

**Every `RUN` step that may touch `/dev/null`** (apt-get, most package installs)
must begin with `mount -o remount,rw /dev &&`. Since each `RUN` step gets a
fresh container, the remount does not persist across steps.

## Why these flags

| Flag                  | Reason                                                                                                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--network=host`      | gVisor doesn't support CNI/netavark networking                                                                                                                |
| `--isolation=chroot`  | OCI isolation fails: crun 1.14.1 lacks `/proc/self/setgroups`; crun 1.25.1+ has a SIGPIPE race in buildah's stdio relay (see <podman-build-investigation.md>) |
| `--cap-add=SYS_ADMIN` | Chroot isolation bind-mounts host `/dev` read-only; `CAP_SYS_ADMIN` lets the `mount -o remount,rw /dev` workaround succeed                                    |

## Example: building a Dockerfile

```dockerfile
FROM docker.io/library/ubuntu:24.04

# Remount /dev rw so apt post-install scripts can use /dev/null.
# Must be in the SAME RUN step as apt-get (mount state doesn't persist).
RUN mount -o remount,rw /dev \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/*

# COPY works normally
COPY myfile.conf /etc/myfile.conf

# Steps that don't touch /dev/null don't need the remount
RUN echo "hello" > /tmp/greeting
```

```bash
podman build --no-cache --network=host --isolation=chroot --cap-add=SYS_ADMIN \
  -t my-image:latest -f Dockerfile .
```

## Fallback: podman run + commit

If `podman build` doesn't work for a specific case, simulate Dockerfile steps
manually:

```bash
# FROM
IMAGE=docker.io/library/ubuntu:24.04

# RUN
podman run --rm=false --name build-step --network=host "$IMAGE" \
  /bin/sh -c 'apt-get update && apt-get install -y curl'
IMAGE=$(podman commit build-step | tail -1)
podman rm -f build-step

# COPY (mount source, cp inside container, commit)
podman run --rm=false --name build-step --network=host \
  -v /path/to/local/file.conf:/mnt/file.conf:ro \
  "$IMAGE" /bin/sh -c 'cp /mnt/file.conf /etc/file.conf'
IMAGE=$(podman commit build-step | tail -1)
podman rm -f build-step

# Tag final image
podman tag "$IMAGE" my-image:latest
```

## Verifying shared library completeness

To check that an image has all shared libraries needed by a binary (e.g., Chromium headless shell):

```bash
podman run --rm --network=host \
  -v /path/to/binary:/tmp/binary:ro \
  my-image:latest ldd /tmp/binary | grep "not found"
```

No output means all libraries resolve.

## Root cause details

See <podman-build-investigation.md> for detailed investigation notes.

**Why `/dev/null` is read-only in chroot**: The host `/dev` is a writable tmpfs, but buildah's chroot isolation explicitly bind-mounts it read-only into the container (hardcoded in `chroot/run_linux.go:421`: `devFlags := commonFlags | unix.MS_NOEXEC | unix.MS_NOSUID | unix.MS_RDONLY`). With `CAP_SYS_ADMIN`, the container can `mount -o remount,rw /dev` to undo this.

**OCI isolation path**: Two independent issues:

1. crun 1.14.1 (system version): `can_setgroups()` opens `/proc/self/setgroups` which doesn't exist in gVisor. The `run.oci.keep_original_groups=1` annotation works for `podman run` but buildah doesn't propagate it to intermediate build containers.
2. crun 1.25.1+ (fixes setgroups): Introduces a race condition in buildah's stdio relay — the pipe read-end is closed before forked child processes finish writing, causing SIGPIPE. This is a timing-dependent interaction between buildah's poll-based I/O relay and gVisor's scheduling. Shell builtins work but any fork+exec (external commands, subshells) fails.

**Chroot isolation path**: buildah bind-mounts host `/dev` read-only. Solvable with `CAP_SYS_ADMIN` + `mount -o remount,rw /dev` in each RUN step.

**Why `podman run` works without workarounds**: Uses conmon for stdio management (not buildah's poll loop), and respects `containers.conf` annotations for `keep_original_groups`.
