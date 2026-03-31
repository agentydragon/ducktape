"""Crane (go-containerregistry) CLI wrapper.

Resolves the crane binary from Bazel runfiles (@crane) and provides sync/async
push helpers. Auth is passed via a temporary DOCKER_CONFIG directory, never
written to ~/.docker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from util.bazel import runfiles
from util.oci import docker_auth_config

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
            Path(self._config_dir.name, "config.json").write_text(
                json.dumps(docker_auth_config(registry, username, password))
            )
            self._env = {**os.environ, "DOCKER_CONFIG": self._config_dir.name}

    def _run(self, *args: str) -> str:
        result = subprocess.run([str(self._path), *args], check=True, capture_output=True, text=True, env=self._env)
        return result.stdout.strip()

    async def _arun(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            str(self._path), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"crane {args[0]} failed: {stderr.decode()}")
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
        return "sha256:" + stdout.split("@sha256:", 1)[1].split()[0]
    raise RuntimeError(f"crane push did not return digest for {dest}: {stdout!r}")
