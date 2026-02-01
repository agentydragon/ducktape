# Building Dockerfiles in gVisor

`podman build` works under gVisor with no extra flags — all gVisor workarounds are pre-configured:

```bash
podman build -t my-image .
```

The Dockerfile must redirect RUN output to avoid SIGPIPE from buildah's pipe handling:

```dockerfile
SHELL ["/bin/bash", "-c", "set -euo pipefail; exec > /tmp/build-step.log 2>&1; eval \"$0\""]
```

## What's configured automatically

| Setting | Mechanism | Fixes |
|---------|-----------|-------|
| OCI isolation | `BUILDAH_ISOLATION=oci` env var | Chroot mode creates a devtmpfs that gVisor mounts read-only, breaking `/dev/null` |
| Host networking | `netns = "host"` in `containers.conf` | gVisor doesn't support private container networking |
| Docker format | `image_default_format = "docker"` in `containers.conf` | OCI image format doesn't support `SHELL` directive (needed for SIGPIPE workaround) |
| crun-gvisor-wrapper | `runtime = "crun-gvisor"` in `containers.conf` | gVisor lacks `/proc/self/setgroups`; wrapper injects `run.oci.keep_original_groups=1` annotation |
| Host user namespace | `userns = "host"` in `containers.conf` | Skips user namespace creation (gVisor restriction) |

## Disk space

The VFS storage driver copies the full filesystem for every layer. For large images, use `--layers=false` (or `BUILDAH_LAYERS=false`) to disable layer caching and keep only one working copy:

```bash
podman build --layers=false -t my-image .
```

### crun-gvisor-wrapper detail

gVisor doesn't provide `/proc/self/setgroups`, which crun's `deny_setgroups()` tries to open. The `run.oci.keep_original_groups=1` OCI annotation tells crun to skip this call.

For `podman run`, the annotation is set in `containers.conf` and works automatically. For `podman build`, buildah doesn't propagate `containers.conf` annotations to intermediate build containers. The `crun-gvisor-wrapper` script (installed by `podman_service.py`, registered as the default runtime via `[engine.runtimes]`) intercepts crun invocations, injects the annotation into the OCI `config.json`, then exec's the real crun.

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
