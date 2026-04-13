"""Crane (go-containerregistry) CLI wrapper.

Resolves the crane binary from Bazel runfiles (@crane) and provides sync/async
push helpers. Auth is passed via a temporary DOCKER_CONFIG directory, never
written to ~/.docker.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from util.bazel import runfiles

logger = logging.getLogger(__name__)

_CRANE_RLOCATION = "crane/crane"


def get_crane() -> Path:
    """Resolve the crane binary from Bazel runfiles."""
    return runfiles.get_required_path(_CRANE_RLOCATION)


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, available as an OCI layout directory."""

    repo_name: str
    image_rlocation: str


def _format_crane_error(args: tuple[str, ...], returncode: int | None, stderr: str, stdout: str) -> str:
    """Build a multiline message describing a failed crane subprocess.

    Used by both the sync and async wrappers so failures look the same
    regardless of which path raised. crane writes its error messages to
    stderr; stdout is included for the rare case where it carries useful
    context (e.g. a partial push trace).
    """
    parts = [f"crane {' '.join(args)} failed (exit {returncode})"]
    if stderr.strip():
        parts.append(f"stderr:\n{stderr.rstrip()}")
    if stdout.strip():
        parts.append(f"stdout:\n{stdout.rstrip()}")
    return "\n".join(parts)


class Crane:
    """Typed wrapper around the crane CLI.

    If registry credentials are provided, all commands run with DOCKER_CONFIG
    pointing to a temporary directory containing the auth config.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        registry: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._path = path or get_crane()
        self._env: dict[str, str] | None = None
        self._config_dir: tempfile.TemporaryDirectory[str] | None = None
        if registry and username and password:
            self._config_dir = tempfile.TemporaryDirectory(prefix="crane_auth_")
            auth = base64.b64encode(f"{username}:{password}".encode()).decode()
            config = {"auths": {registry: {"auth": auth}}}
            Path(self._config_dir.name, "config.json").write_text(json.dumps(config))
            self._env = {**os.environ, "DOCKER_CONFIG": self._config_dir.name}

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run([str(self._path), *args], check=True, capture_output=True, text=True, env=self._env)
        except subprocess.CalledProcessError as e:
            # Surface stderr/stdout in the exception message. By default
            # `CalledProcessError.__str__` only prints `cmd` and `returncode`,
            # which makes errors from `check=True, capture_output=True` show up
            # as the useless `Command '[...]' returned non-zero exit status N.`
            # in tracebacks. Without the captured streams we can't tell whether
            # crane hit a 401 from GHCR, a network blip during a blob upload,
            # or anything else.
            raise RuntimeError(_format_crane_error(args, e.returncode, e.stderr or "", e.stdout or "")) from e
        return result.stdout.strip()

    async def _arun(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            str(self._path), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(_format_crane_error(args, proc.returncode, stderr.decode(), stdout.decode()))
        return stdout.decode().strip()

    def digest(self, image_ref: str) -> str:
        return self._run("digest", image_ref)

    def ls(self, repo: str) -> list[str]:
        return self._run("ls", repo).splitlines()

    def push(self, image_dir: Path, ref: str) -> None:
        self._run("push", str(image_dir), ref)

    async def apush(self, image_dir: Path, ref: str, *, insecure: bool = False) -> str:
        """Async push. Returns the crane output (image reference with digest)."""
        args = ["push", str(image_dir), ref]
        if insecure:
            args.append("--insecure")
        return await self._arun(*args)

    def tag(self, ref: str, tag: str) -> None:
        self._run("tag", ref, tag)

    async def push_bazel_image(self, image: BazelImage, registry_url: str, tag: str, *, insecure: bool = False) -> str:
        """Push a Bazel-built OCI image to a registry. Returns the digest."""
        image_path = runfiles.get_required_path(image.image_rlocation)
        dest = f"{registry_url}/{image.repo_name}:{tag}"
        logger.info("Pushing %s -> %s via crane", image_path, dest)
        stdout = await self.apush(image_path, dest, insecure=insecure)
        digest = _parse_crane_digest(stdout, dest)
        logger.info("Pushed %s: %s", dest, digest)
        return digest


def _parse_crane_digest(stdout: str, dest: str) -> str:
    if "@sha256:" in stdout:
        return "sha256:" + stdout.split("@sha256:", 1)[1].split(maxsplit=1)[0]
    raise RuntimeError(f"crane push did not return digest for {dest}: {stdout!r}")


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

    buf = io.BytesIO()
    # dereference=True: Bazel runfiles are symlinks into the execroot. Without
    # dereferencing, tar records them as symlink entries with absolute target
    # paths. Docker extracts the tarball and tries to follow those symlinks,
    # which fail when the daemon runs outside Bazel's sandbox.
    with tarfile.open(fileobj=buf, mode="w", dereference=True) as tar:
        # Add manifest.json
        manifest_data = json.dumps(docker_manifest).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_data)
        tar.addfile(info, io.BytesIO(manifest_data))
        # Add config blob
        tar.add(oci_layout / config_blob_rel, arcname=config_blob_rel)
        # Add layer blobs
        for layer_rel in layer_rels:
            tar.add(oci_layout / layer_rel, arcname=layer_rel)

    docker = shutil.which("docker") or shutil.which("podman")
    if not docker:
        raise RuntimeError("Neither docker nor podman CLI found")
    buf.seek(0)
    result = subprocess.run([docker, "load"], input=buf.read(), check=False, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker load failed: {result.stderr.decode()}")
