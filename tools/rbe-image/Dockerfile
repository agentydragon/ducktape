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
RUN mkdir -p /etc/containers \
    && printf '[storage]\ndriver = "vfs"\n' > /etc/containers/storage.conf \
    && printf '[containers]\nuserns = "host"\nannotations = ["run.oci.keep_original_groups=1"]\n\n[engine]\nnetwork_backend = "cni"\n' > /etc/containers/containers.conf \
    && printf '{"default":[{"type":"insecureAcceptAnything"}]}\n' > /etc/containers/policy.json
