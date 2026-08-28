"""OCI container image utilities: auth, digest reading, image loading.

Also the Bazel-facing half of crane: `util/crane.py` is deliberately standard
library only so the CI publish planners can import it without Bazel, so
resolving crane and Bazel-built images out of runfiles lives here instead.
"""

from __future__ import annotations

import base64
import functools
import io
import json
import logging
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace

from util.bazel import runfiles
from util.crane import Crane, parse_pushed_digest

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

_CRANE_RLOCATION = "crane/crane"


def bazel_crane(*, registry: str | None = None, username: str | None = None, password: str | None = None) -> Crane:
    """A `Crane` on the binary Bazel staged in runfiles (`data = ["@crane"]`)."""
    return Crane(runfiles.get_required_path(_CRANE_RLOCATION), registry=registry, username=username, password=password)


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, available as an OCI layout directory."""

    repo_name: str
    image_rlocation: str


async def push_bazel_image(
    crane: Crane, image: BazelImage, registry_url: str, tag: str, *, insecure: bool = False
) -> str:
    """Push a Bazel-built OCI image to a registry. Returns the digest."""
    image_path = runfiles.get_required_path(image.image_rlocation)
    dest = f"{registry_url}/{image.repo_name}:{tag}"
    logger.info("Pushing %s -> %s via crane", image_path, dest)
    stdout = await crane.apush(image_path, dest, insecure=insecure)
    digest = parse_pushed_digest(stdout, dest)
    logger.info("Pushed %s: %s", dest, digest)
    return digest


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


@functools.cache
def load_oci_image(image: OciImage) -> str:
    """Ensure the local Docker daemon holds *image*, loading it if it does not.

    **Cached per process, because that is the only scope that can hit.** Every RBE action runs in
    its own freshly booted microVM with its own empty daemon, so a load is never shared with
    another target and no two processes ever race for one — measurements and the options for
    changing that: <../devinfra/docs/container_image_loading.md>. What remains worth skipping is a
    second fixture in the same test asking for a tag this process already loaded.

    The cache is keyed on the whole `OciImage`, so a different layout under the same tag still
    loads; nothing here tries to answer whether a *previous process* loaded it, which is the
    question the daemon cannot be asked cheaply and that no longer has a caller.
    """
    layout_rloc = runfiles.get_required_path(image.rloc_file).read_text().strip()
    oci_layout = runfiles.get_required_path(layout_rloc)
    with tracer.start_as_current_span(f"load_oci_image({image.tag})"):
        started = time.monotonic()
        # Timed and logged because this runs in session fixture setup, before pytest emits a line:
        # when it wedges, the whole test log is otherwise silence
        # (<../debug/2026_08_14_docker_test_timeouts.md>).
        logger.info("Loading %s into the Docker daemon", image.tag)
        push_to_daemon(oci_layout, image.tag)
        logger.info("Loaded %s in %.1fs", image.tag, time.monotonic() - started)
    return image.tag


def push_to_daemon(oci_layout: Path, tag: str) -> None:
    """Load an OCI layout directory into the local Docker daemon.

    Converts the OCI layout to a Docker-format tarball and pipes it to
    ``docker load``. This is equivalent to what rules_oci's oci_load does.

    TODO: The Docker daemon has no API for loading OCI layouts directly —
    every path (crane, skopeo, regctl, rules_oci's oci_load) ends up
    building a Docker-format tarball and piping it to ``docker load``.
    If Docker ever adds native OCI layout loading, replace this.
    """
    index = json.loads((oci_layout / "index.json").read_text())
    manifest_digest: str = index["manifests"][0]["digest"]
    manifest_blob = oci_layout / "blobs" / manifest_digest.replace(":", "/")
    manifest = json.loads(manifest_blob.read_text())

    config_digest: str = manifest["config"]["digest"]
    config_blob_rel = "blobs/" + config_digest.replace(":", "/")
    layer_rels = ["blobs/" + layer["digest"].replace(":", "/") for layer in manifest["layers"]]

    docker_manifest = [{"Config": config_blob_rel, "RepoTags": [tag], "Layers": layer_rels}]

    docker = shutil.which("docker") or shutil.which("podman")
    if not docker:
        raise RuntimeError("Neither docker nor podman CLI found")

    # Streamed into `docker load` rather than assembled first: the tarball is the whole image, so
    # buffering it cost one copy to build and another to hand over, and the daemon sat idle until
    # the last byte was written. Writing to the pipe lets the unpack overlap the build.
    #
    # dereference=True: Bazel runfiles are symlinks into the execroot. Without dereferencing, tar
    # records them as symlink entries with absolute target paths. Docker extracts the tarball and
    # tries to follow those symlinks, which fail when the daemon runs outside Bazel's sandbox.
    with tracer.start_as_current_span("docker_load") as span:
        loader = subprocess.Popen(
            [docker, "load"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert loader.stdin is not None
        written = 0
        try:
            # `w|` is tar's streaming mode: no seeking back to patch headers, which a pipe cannot do.
            with tarfile.open(fileobj=loader.stdin, mode="w|", dereference=True) as tar:
                manifest_data = json.dumps(docker_manifest).encode()
                info = tarfile.TarInfo(name="manifest.json")
                info.size = len(manifest_data)
                tar.addfile(info, io.BytesIO(manifest_data))
                tar.add(oci_layout / config_blob_rel, arcname=config_blob_rel)
                for layer_rel in layer_rels:
                    tar.add(oci_layout / layer_rel, arcname=layer_rel)
                written = tar.offset
        except BrokenPipeError as broken:
            # `docker load` died mid-stream; its stderr says why and is worth more than the EPIPE.
            _, stderr = loader.communicate()
            raise RuntimeError(f"docker load failed: {stderr.decode()}") from broken
        finally:
            if not loader.stdin.closed:
                loader.stdin.close()
        _, stderr = loader.communicate()
        span.set_attribute("tarball_bytes", written)
    if loader.returncode != 0:
        raise RuntimeError(f"docker load failed: {stderr.decode()}")
