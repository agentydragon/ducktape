# BuildBuddy RBE worker image with podman support.
#
# Provides system tools needed by Bazel remote actions: python3 (for py_binary
# shebang bootstrap), build-essential (C linker for Rust toolchain bootstrap),
# git, ca-certificates, file/zip (for Bazel test-setup.sh), and podman for
# container-based e2e tests.
FROM docker.io/library/ubuntu:24.04

# Enable universe repo (podman lives there) and install dependencies.
RUN sed -i 's/^Components: main$/Components: main universe/' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        file \
        fuse-overlayfs \
        git \
        podman \
        python3 \
        uidmap \
        zip \
    && rm -rf /var/lib/apt/lists/*

# Pre-configure Podman for rootful VFS operation (no overlay/fuse needed).
# BuildBuddy workers run as root but may lack /dev/fuse or overlay support.
# Config files are shared with tools/claude_hooks (gVisor podman setup).
COPY tools/claude_hooks/config/podman/storage.conf /etc/containers/storage.conf
COPY tools/claude_hooks/config/podman/containers.conf /etc/containers/containers.conf
COPY tools/claude_hooks/config/podman/policy.json /etc/containers/policy.json
