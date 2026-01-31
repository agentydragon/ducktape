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

**OCI isolation path**: `podman run --network=host` works because `containers.conf` sets `userns=host`, which skips user namespace creation entirely. `podman build` creates intermediate containers for each `RUN` step that don't inherit this setting, so crun tries to set up a user namespace and fails when writing to `/proc/<pid>/setgroups`.

**Chroot isolation path**: buildah's chroot mode creates a minimal `/dev` on a devtmpfs that gVisor mounts read-only. Volume mounts (`--volume /dev/null:/dev/null:rw`) don't override this because the devtmpfs layer takes precedence.
