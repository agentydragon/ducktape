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
from typing import Any

from more_itertools import one
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


def _read_blob(image_dir: Path, digest: str) -> dict[str, Any]:
    algorithm, _, hexdigest = digest.partition(":")
    blob: dict[str, Any] = json.loads((image_dir / "blobs" / algorithm / hexdigest).read_text())
    return blob


def read_oci_config_digest(image_dir: Path) -> str:
    """The digest of the image *config* blob, which is what Docker reports as an image's `Id`.

    Not the manifest digest `read_oci_layout_digest` returns: that identifies the manifest, and
    the daemon does not generally know it for an image crane pushed — a loaded image has no
    `RepoDigests` to compare against. The config digest it does know, and it identifies the same
    bytes, so it is the one thing both ends can name.
    """
    manifest = _read_blob(image_dir, read_oci_layout_digest(image_dir))
    if (nested := manifest.get("manifests")) is not None:
        # A multi-platform index. Bazel pins these per platform, so one entry is the norm and more
        # than one means the caller is loading something this cannot identify unambiguously.
        manifest = _read_blob(image_dir, one(nested)["digest"])
    config_digest: str = manifest["config"]["digest"]
    return config_digest


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
    """The config digest the local Docker daemon holds under *tag*, or None if it holds nothing."""
    inspect = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True, check=False
    )
    return inspect.stdout.strip() if inspect.returncode == 0 else None


def load_oci_image(image: OciImage) -> str:
    """Ensure the local Docker daemon holds *image*, loading it via crane if it does not.

    **Identity is the digest, not the tag.** A tag is not an identity here: `postgres:18` and
    `python:3.13-slim` move upstream and are pinned by digest in Bazel, so a pin bump leaves the
    tag unchanged — and a tag this repo builds (`wayback-proxy:latest`) is reused on every build.
    Skipping on the tag alone would pin a warm worker to whatever it loaded first, with nothing
    short of a manual `docker rmi` able to dislodge it.

    **Callers do not check first.** Every call site used to decide for itself whether to skip, in
    the same three lines, and each was a place to get that wrong.

    The comparison is against the daemon rather than against any note this kept, so there is no
    state to go stale or to lie. If the two ever disagree about how to spell the same image, the
    result is a load that did not need doing — never a stale image served as a fresh one.

    Checked twice on purpose: once before the lock, so a warm daemon costs one `docker image
    inspect`, and once **inside** it, so the first waiter behind a loader finds the image already
    there and skips the load just done for it.
    """
    layout_rloc = runfiles.get_required_path(image.rloc_file).read_text().strip()
    oci_layout = runfiles.get_required_path(layout_rloc)
    wanted = read_oci_config_digest(oci_layout)
    if daemon_image_id(image.tag) == wanted:
        return image.tag
    with tracer.start_as_current_span(f"load_oci_image({image.tag})"), _one_loader_at_a_time(image.tag):
        if daemon_image_id(image.tag) == wanted:
            return image.tag
        started = time.monotonic()
        logger.info("Loading %s (%s) into the Docker daemon (pid %d)", image.tag, wanted[:19], os.getpid())
        push_to_daemon(oci_layout, image.tag)
        logger.info("Loaded %s in %.1fs", image.tag, time.monotonic() - started)
    return image.tag
