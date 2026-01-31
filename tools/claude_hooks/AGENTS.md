@README.md

## Agent Instructions

- **Session start log**: `~/.cache/claude-hooks/session-start.log`
- **Supervisor logs**: `~/.config/claude-hooks/supervisor/supervisord.log` (supervisor daemon), `~/.config/claude-hooks/supervisor/auth-proxy.{log,err.log}` (auth proxy service)
- **gVisor environment**: Claude Code web runs on gVisor, not real Linux. Some syscalls behave differently.
- **9p filesystem limitation**: Root `/` is 9p. Supervisor uses TCP socket (`127.0.0.1:19001`) instead of Unix socket to avoid 9p hard link issues (EOPNOTSUPP).

## Building Dockerfiles in gVisor

`podman build` does not work in gVisor due to two independent issues:

- **OCI isolation**: crun calls `deny_setgroups()` which opens `/proc/<pid>/setgroups` — this file doesn't exist in gVisor.
- **Chroot isolation**: `/dev/null` is read-only, breaking apt-get/gpg.

**Workaround**: simulate Dockerfile steps using `podman run` + `podman commit`:

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

To verify shared library completeness of an image (e.g., for Chromium):

```bash
podman run --rm --network=host \
  -v /path/to/binary:/tmp/binary:ro \
  my-image:latest ldd /tmp/binary | grep "not found"
```

## Debugging Commands

```bash
# Check session start log
tail -100 ~/.cache/claude-hooks/session-start.log

# Verify auth proxy connectivity
curl -s --max-time 5 -x http://127.0.0.1:18081 https://bcr.bazel.build/ | head -1

# Check Bazel configuration
cat ~/.cache/claude-hooks/auth-proxy/bazelrc

# Check supervisor status
python -m supervisor.supervisorctl -c ~/.config/claude-hooks/supervisor/supervisord.conf status
```
