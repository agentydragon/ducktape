# Building Dockerfiles in gVisor

Standard `podman build` needs these flags to work under gVisor:

```bash
podman build \
  --network=host \
  --isolation=oci \
  --runtime=/usr/local/bin/crun-gvisor-wrapper \
  --format=docker \
  --layers=false \
  -t my-image .
```

The Dockerfile must also redirect RUN output to avoid SIGPIPE:

```dockerfile
SHELL ["/bin/bash", "-c", "set -euo pipefail; exec > /tmp/build-step.log 2>&1; eval \"$0\""]
```

## Why each flag is needed

| Flag | Fixes | Root cause |
|------|-------|------------|
| `--isolation=oci` | Read-only `/dev/null` | Chroot mode creates a devtmpfs that gVisor mounts read-only. OCI mode uses the normal device setup. |
| `--runtime=crun-gvisor-wrapper` | Missing `/proc/self/setgroups` | OCI isolation uses crun which calls `deny_setgroups()`. The wrapper injects `run.oci.keep_original_groups=1` into the OCI config. |
| `--network=host` | Container networking | Required by `containers.conf` (`userns=host`). |
| `--format=docker` | `SHELL` directive ignored | Podman defaults to OCI image format which doesn't support `SHELL`. Docker format is needed for the output redirect. |
| `--layers=false` | Disk space exhaustion | VFS driver copies the full filesystem per layer. Disabling layer caching keeps only one working copy instead of N. |
| `SHELL` redirect | Broken pipe (SIGPIPE) | Buildah's pipe handling for RUN step output causes SIGPIPE when commands produce large output. Redirecting to a file inside the container avoids the pipe entirely. |

## Verifying shared library completeness

```bash
podman run --rm --network=host \
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

podman tag "$IMAGE" my-image:latest
```
