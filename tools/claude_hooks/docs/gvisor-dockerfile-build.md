# Building Dockerfiles in gVisor

`podman build` does not work in gVisor due to two independent issues:

- **OCI isolation**: crun calls `deny_setgroups()` which opens `/proc/<pid>/setgroups` — this file doesn't exist in gVisor.
- **Chroot isolation**: `/dev/null` is read-only, breaking apt-get/gpg.

## Workaround: podman run + commit

Simulate Dockerfile steps using `podman run` + `podman commit`:

```bash
# FROM
podman pull docker.io/library/ubuntu:24.04
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

**OCI isolation path**: Two independent issues:

1. crun 1.14.1 (system version): `can_setgroups()` opens `/proc/self/setgroups` which doesn't exist in gVisor. The `run.oci.keep_original_groups=1` annotation works for `podman run` but buildah doesn't propagate it to intermediate build containers.
2. crun 1.25.1+ (fixes setgroups): Introduces a race condition in buildah's stdio relay — the pipe read-end is closed before forked child processes finish writing, causing SIGPIPE. This is a timing-dependent interaction between buildah's poll-based I/O relay and gVisor's scheduling. Shell builtins work but any fork+exec (external commands, subshells) fails.

**Chroot isolation path**: gVisor mounts devtmpfs read-only. buildah's chroot mode inherits this, making `/dev/null` unwritable. Many packages (ca-certificates, python3, apt-key) need `/dev/null` in post-install scripts.

**Why `podman run` works**: Uses conmon for stdio management (not buildah's poll loop), and respects `containers.conf` annotations for `keep_original_groups`.
