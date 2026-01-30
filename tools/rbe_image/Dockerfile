# BuildBuddy RBE worker image with Podman for container-based tests.
#
# Python is provided by Bazel's hermetic toolchain (bootstrap_impl=script),
# so this image only needs system tools: git, ca-certs, and podman.
FROM docker.io/library/ubuntu:24.04

# Enable universe repo (podman lives there) and install dependencies.
RUN sed -i 's/^Components: main$/Components: main universe/' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        podman \
        fuse-overlayfs \
        uidmap \
    && rm -rf /var/lib/apt/lists/*

# Pre-configure Podman for rootful VFS operation (no overlay/fuse needed).
# BuildBuddy workers run as root but may lack /dev/fuse or overlay support.
# Config files are shared with tools/claude_hooks (gVisor podman setup).
COPY tools/claude_hooks/config/podman/storage.conf /etc/containers/storage.conf
COPY tools/claude_hooks/config/podman/containers.conf /etc/containers/containers.conf
COPY tools/claude_hooks/config/podman/policy.json /etc/containers/policy.json
