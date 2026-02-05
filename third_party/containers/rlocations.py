"""Runfiles rlocation constants for pre-built container image tarballs.

These correspond to the oci_load targets in this package's BUILD.bazel.
Test fixtures use these with test_util.image_loader.load_image() to pre-load
images into the Docker daemon for Testcontainers.
"""

POSTGRES_16_TARBALL = "_main/third_party/containers/postgres_16_load/tarball.tar"
REGISTRY_2_TARBALL = "_main/third_party/containers/registry_2_load/tarball.tar"
RYUK_TARBALL = "_main/third_party/containers/ryuk_load/tarball.tar"
