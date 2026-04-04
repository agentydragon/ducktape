"""OciImage constants for pre-built container images.

These correspond to the oci_tarball / oci_layout_rloc targets in this package's BUILD.bazel.
Test fixtures use these with util.oci.load_oci_image() to pre-load images
into the Docker daemon for Testcontainers.
"""

from util.oci import OciImage

DEBIAN_SLIM = OciImage("_main/third_party/containers/debian_slim.rloc", "debian-slim:test")
MITMPROXY = OciImage("_main/third_party/containers/mitmproxy.rloc", "mitmproxy:11")
POSTGRES_18 = OciImage("_main/third_party/containers/postgres_18.rloc", "postgres:18")
PYTHON_3_13_SLIM = OciImage("_main/third_party/containers/python_3_13_slim.rloc", "python:3.13-slim")
REGISTRY_2 = OciImage("_main/third_party/containers/registry_2.rloc", "registry:2")
RYUK = OciImage("_main/third_party/containers/ryuk.rloc", "testcontainers/ryuk:0.8.1")
