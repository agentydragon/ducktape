"""OCI container image utilities: auth, digest reading, image loading."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

from opentelemetry import trace

from util.bazel import runfiles

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
# Image loading via Docker/Podman
# ---------------------------------------------------------------------------


tracer = trace.get_tracer(__name__)


def load_image(tarball_rlocation: str) -> None:
    """Load a Docker image tarball into the container runtime."""
    tarball_name = tarball_rlocation.rsplit("/", 1)[-1]
    with tracer.start_as_current_span(f"load_image({tarball_name})"):
        with tracer.start_as_current_span("resolve_runfiles_path"):
            tarball_path = runfiles.get_required_path(tarball_rlocation)
        _docker_load(tarball_path, tarball_rlocation)


def _docker_load(tarball_path: Path, tarball_rlocation: str) -> None:
    cmd = shutil.which("docker") or shutil.which("podman")
    if not cmd:
        raise RuntimeError("Neither docker nor podman CLI found")
    result = subprocess.run([cmd, "load", "-i", str(tarball_path)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load image from {tarball_rlocation}: {result.stderr}")


def load_bazel_image(load_script_path: str, image_tag: str) -> str:
    """Load an OCI image from a Bazel oci_load target via the generated load.sh script."""
    load_script = runfiles.get_required_path(f"_main/{load_script_path}")

    result = subprocess.run(
        [load_script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "DOCKER_CLI_EXPERIMENTAL": "enabled"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load image {image_tag}: {result.stderr}")

    return image_tag
