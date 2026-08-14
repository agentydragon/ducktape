"""OCI container image utilities: auth, digest reading, image loading."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace

from util.bazel import runfiles
from util.crane import push_to_daemon

logger = logging.getLogger(__name__)


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


@contextlib.contextmanager
def _one_loader_at_a_time(tag: str) -> Iterator[None]:
    """Hold a machine-wide lock on *tag* for the length of a load.

    **Why this is not paranoia.** A Bazel test target is its own process, and a full `//...` runs
    as many `requires_docker` targets at once as there are job slots — 143 of them declare the tag
    — against one daemon per worker. Every one of them checks whether the image is present and,
    on a cold daemon, every one of them gets the same answer: no. They then all push the same
    layers into the same storage driver simultaneously, and what should be one ~30s load becomes a
    thundering herd that wedges container startup for minutes
    (<../debug/2026_08_14_docker_test_timeouts.md>).

    A file lock rather than anything cleverer because the thing being serialised is per machine and
    the processes share nothing else. `flock` is released when the fd closes, so a loader that dies
    mid-push does not strand the others.
    """
    lock_path = Path(tempfile.gettempdir()) / f"oci-load-{hashlib.sha256(tag.encode()).hexdigest()[:16]}.lock"
    with lock_path.open("w") as lock:
        waited_from = time.monotonic()
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (waited := time.monotonic() - waited_from) > 1.0:
            logger.info("Waited %.1fs for another process to finish loading %s", waited, tag)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def image_in_daemon(tag: str) -> bool:
    """Whether the local Docker daemon already has *tag*."""
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True, check=False).returncode == 0


def load_oci_image(image: OciImage) -> str:
    """Load an OCI image into the local Docker daemon via crane. Returns the tag.

    Serialised per tag across processes, and the presence check is repeated **inside** the lock:
    the whole point is that the first waiter to acquire it will find the image already there and
    skip a load that has just been done for it.
    """
    layout_rloc = runfiles.get_required_path(image.rloc_file).read_text().strip()
    oci_layout = runfiles.get_required_path(layout_rloc)
    with tracer.start_as_current_span(f"load_oci_image({image.tag})"), _one_loader_at_a_time(image.tag):
        if image_in_daemon(image.tag):
            return image.tag
        started = time.monotonic()
        logger.info("Loading %s into the Docker daemon (pid %d)", image.tag, os.getpid())
        push_to_daemon(oci_layout, image.tag)
        logger.info("Loaded %s in %.1fs", image.tag, time.monotonic() - started)
    return image.tag
