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


def _lock_path(tag: str) -> Path:
    return Path(tempfile.gettempdir()) / f"oci-load-{hashlib.sha256(tag.encode()).hexdigest()[:16]}.lock"


@contextlib.contextmanager
def _one_loader_at_a_time(tag: str) -> Iterator[None]:
    """Hold a machine-wide lock on *tag* for the length of a load.

    **Why this is not paranoia.** A Bazel test target is its own process, and a full `//...` runs
    as many `requires_docker` targets at once as there are job slots — 143 of them declare the tag
    — against one daemon per worker. Without this they all find the image missing at the same
    moment and push the same layers into the same storage driver simultaneously, and what should be
    one ~30s load becomes a thundering herd that wedges container startup for minutes
    (<../debug/2026_08_14_docker_test_timeouts.md>).

    A file lock rather than anything cleverer because the thing being serialised is per machine and
    the processes share nothing else. `flock` is released when the fd closes, so a loader that dies
    mid-push does not strand the others.
    """
    with _lock_path(tag).open("w") as lock:
        waited_from = time.monotonic()
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (waited := time.monotonic() - waited_from) > 1.0:
            logger.info("Waited %.1fs for another process to finish loading %s", waited, tag)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def daemon_image_id(tag: str) -> str | None:
    """The image id the local Docker daemon holds under *tag*, or None if it holds nothing."""
    inspect = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True, check=False
    )
    return inspect.stdout.strip() if inspect.returncode == 0 else None


def _loaded_marker(tag: str) -> Path:
    return Path(tempfile.gettempdir()) / f"oci-load-{hashlib.sha256(tag.encode()).hexdigest()[:16]}.loaded"


def _already_loaded(tag: str, wanted: str) -> bool:
    """Whether this machine already loaded exactly *wanted* under *tag*, and it is still there.

    **Two facts, and both have to hold.** The marker says which layout was loaded — that is the
    question a tag cannot answer, since `postgres:18` and `python:3.13-slim` move upstream and are
    pinned by digest in Bazel, and a tag this repo builds is reused on every build. The daemon says
    whether what was loaded is still present and still the same image, which is what catches a
    prune or an out-of-band retag and stops the marker from being believed on its own.

    **Why not just compare the daemon's id to the layout's config digest.** Because they are not
    the same number: Docker's classic image store rewrites an OCI config on load, so `.Id` is the
    digest of its rewritten config. Comparing them would never match, and "never matches" here does
    not fail visibly — it silently reloads every image on every target, which is slower than doing
    nothing at all. So the daemon's id is recorded rather than predicted, and only ever compared
    against itself.
    """
    marker = _loaded_marker(tag)
    if not marker.exists():
        return False
    layout, _, loaded_id = marker.read_text().strip().partition(" ")
    return layout == wanted and loaded_id == daemon_image_id(tag)


def load_oci_image(image: OciImage) -> str:
    """Ensure the local Docker daemon holds *image*, loading it via crane if it does not.

    **Callers do not check first.** Every call site used to decide for itself whether to skip, in
    the same three lines, and each was a place to get the question in `_already_loaded` wrong.

    Checked twice on purpose: once before the lock, so a warm daemon costs a file read and one
    `docker image inspect`, and once **inside** it, so the first waiter behind a loader finds the
    image already there and skips the load just done for it.
    """
    layout_rloc = runfiles.get_required_path(image.rloc_file).read_text().strip()
    oci_layout = runfiles.get_required_path(layout_rloc)
    wanted = read_oci_layout_digest(oci_layout)
    if _already_loaded(image.tag, wanted):
        return image.tag
    with tracer.start_as_current_span(f"load_oci_image({image.tag})"), _one_loader_at_a_time(image.tag):
        if _already_loaded(image.tag, wanted):
            return image.tag
        started = time.monotonic()
        logger.info("Loading %s (%s) into the Docker daemon (pid %d)", image.tag, wanted[:19], os.getpid())
        push_to_daemon(oci_layout, image.tag)
        # Written after the push and recording what the daemon actually ended up with, so a crash
        # mid-load leaves the older truth rather than a claim about bytes it never received.
        _loaded_marker(image.tag).write_text(f"{wanted} {daemon_image_id(image.tag)}")
        logger.info("Loaded %s in %.1fs", image.tag, time.monotonic() - started)
    return image.tag
