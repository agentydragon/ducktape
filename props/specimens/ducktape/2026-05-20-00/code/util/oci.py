"""OCI container image utilities: auth, digest reading, image loading."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace

from util.bazel import runfiles
from util.crane import push_to_daemon


@dataclass(frozen=True)
class OciImage:
    """An OCI image bundled in Bazel runfiles.

    rloc_file: runfiles path to the .rloc file produced by oci_tarball / oci_layout_rloc.
    tag: Docker image tag to load as (e.g. "postgres:18").
    """

    rloc_file: str
    tag: str


# ---------------------------------------------------------------------------
# Docker auth
# ---------------------------------------------------------------------------


def docker_auth_config(registry: str, username: str, password: str) -> dict[str, object]:
    """Build a Docker auth config dict for a single registry."""
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"auths": {registry: {"auth": auth}}}


def write_docker_auth(registry: str, username: str, password: str, *, overwrite: bool = False) -> None:
    """Write ~/.docker/config.json with registry credentials."""
    docker_dir = Path.home() / ".docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    config_path = docker_dir / "config.json"
    if config_path.exists() and not overwrite:
        raise FileExistsError(f"{config_path} already exists (pass overwrite=True to replace)")
    config_path.write_text(json.dumps(docker_auth_config(registry, username, password)))


# ---------------------------------------------------------------------------
# OCI layout
# ---------------------------------------------------------------------------


def read_oci_layout_digest(image_dir: Path) -> str:
    """Read the image manifest digest from an OCI layout's index.json."""
    index = json.loads((image_dir / "index.json").read_text())
    digest: str = index["manifests"][0]["digest"]
    return digest


# ---------------------------------------------------------------------------
# Image loading via crane
# ---------------------------------------------------------------------------

tracer = trace.get_tracer(__name__)


def load_oci_image(image: OciImage) -> str:
    """Load an OCI image into the local Docker daemon via crane. Returns the tag."""
    layout_rloc = runfiles.get_required_path(image.rloc_file).read_text().strip()
    oci_layout = runfiles.get_required_path(layout_rloc)
    with tracer.start_as_current_span(f"load_oci_image({image.tag})"):
        push_to_daemon(oci_layout, image.tag)
    return image.tag
