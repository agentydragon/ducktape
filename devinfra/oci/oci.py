"""OCI container image utilities: auth, digest reading, image loading.

Also the Bazel-facing half of crane: `devinfra/oci/crane.py` is deliberately standard
library only so the CI publish planners can import it without Bazel, so
resolving crane and Bazel-built images out of runfiles lives here instead.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace
from python.runfiles import Runfiles

from devinfra.oci.crane import Crane, parse_pushed_digest

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

_CRANE_RLOCATION = "crane/crane"


def bazel_crane(*, registry: str | None = None, username: str | None = None, password: str | None = None) -> Crane:
    """A `Crane` on the binary Bazel staged in runfiles (`data = ["@crane"]`)."""
    return Crane(
        (Runfiles.CreateOrRaise().root() / _CRANE_RLOCATION), registry=registry, username=username, password=password
    )


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, available as an OCI layout directory."""

    repo_name: str
    image_rlocation: str


async def push_bazel_image(
    crane: Crane, image: BazelImage, registry_url: str, tag: str, *, insecure: bool = False
) -> str:
    """Push a Bazel-built OCI image to a registry. Returns the digest."""
    image_path = Runfiles.CreateOrRaise().root() / image.image_rlocation
    dest = f"{registry_url}/{image.repo_name}:{tag}"
    logger.info("Pushing %s -> %s via crane", image_path, dest)
    stdout = await crane.apush(image_path, dest, insecure=insecure)
    digest = parse_pushed_digest(stdout, dest)
    logger.info("Pushed %s: %s", dest, digest)
    return digest


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
    (<../devinfra/buildbuddy/debug/2026_08_14_docker_test_timeouts.md>).

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
    question a tag cannot answer, since `postgres:18` and `python:3.14-slim` move upstream and are
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


def load_oci_image(layout_rlocation: str, *, image_ref: str | None = None) -> str:
    """Load an OCI layout from runfiles and return its local Docker image reference.

    By default the reference is derived from the manifest digest, so distinct image
    versions cannot overwrite each other's tag. An explicit reference is for consumers
    that require a particular name. The digest and daemon image ID are checked before
    and after acquiring a per-reference lock, avoiding redundant concurrent loads and
    detecting pruning or an out-of-band retag.
    """
    oci_layout = Runfiles.CreateOrRaise().root() / layout_rlocation
    wanted = read_oci_layout_digest(oci_layout)
    if image_ref is None:
        # Docker tags have a restricted alphabet and length. Preserve ordinary OCI
        # SHA-256 digests verbatim; hash other digest strings to obtain a valid tag.
        if re.fullmatch(r"sha256:[0-9a-f]{64}", wanted):
            digest_tag = wanted.replace(":", "-", 1)
        else:
            digest_tag = "digest-" + hashlib.sha256(wanted.encode()).hexdigest()
        image_ref = f"ducktape/oci:{digest_tag}"
    if _already_loaded(image_ref, wanted):
        return image_ref
    with tracer.start_as_current_span(f"load_oci_image({image_ref})"), _one_loader_at_a_time(image_ref):
        if _already_loaded(image_ref, wanted):
            return image_ref
        started = time.monotonic()
        logger.info("Loading %s (%s) into the Docker daemon (pid %d)", image_ref, wanted[:19], os.getpid())
        push_to_daemon(oci_layout, image_ref)
        # Record the image ID Docker actually stored, which may differ from the OCI config digest.
        _loaded_marker(image_ref).write_text(f"{wanted} {daemon_image_id(image_ref)}")
        logger.info("Loaded %s in %.1fs", image_ref, time.monotonic() - started)
    return image_ref


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Load a Bazel OCI layout using the shared image cache guard.")
    parser.add_argument("rloc_file", help="Bazel runfiles path to the image's .rloc file")
    parser.add_argument("tag", help="Docker image tag to load")
    args = parser.parse_args()
    layout_rlocation = (Runfiles.CreateOrRaise().root() / args.rloc_file).read_text().strip()
    load_oci_image(layout_rlocation, image_ref=args.tag)


if __name__ == "__main__":
    main()
