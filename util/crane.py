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

    def digest(self, image_ref: str) -> str:
        return self._run("digest", image_ref)

    def ls(self, repo: str) -> list[str]:
        return self._run("ls", repo).splitlines()

    def push(self, image_dir: Path, ref: str) -> None:
        self._run("push", str(image_dir), ref)

    def tag(self, ref: str, tag: str) -> None:
        self._run("tag", ref, tag)


@dataclass(frozen=True)
class BazelImage:
    """OCI image built by Bazel, available as an OCI layout directory."""

    repo_name: str
    image_rlocation: str


def _write_temp_docker_config(config_dir: str, registry_url: str, username: str, password: str) -> None:
    Path(config_dir, "config.json").write_text(json.dumps(docker_auth_config(registry_url, username, password)))


async def crane_push(
    image: BazelImage, registry_url: str, tag: str, *, username: str | None = None, password: str | None = None
) -> str:
    """Push an OCI layout directory to a registry via crane (async).

    Uses asyncio subprocess to avoid blocking the event loop.
    Returns the digest (sha256:...) of the pushed image.
    """
    crane = get_crane()
    image_path = runfiles.get_required_path(image.image_rlocation)
    dest = f"{registry_url}/{image.repo_name}:{tag}"

    env: dict[str, str] | None = None
    with tempfile.TemporaryDirectory(prefix="crane_auth_") as config_dir_str:
        if username and password:
            _write_temp_docker_config(config_dir_str, registry_url, username, password)
            env = {**os.environ, "DOCKER_CONFIG": config_dir_str}

        logger.info("Pushing %s -> %s via crane", image_path, dest)
        proc = await asyncio.create_subprocess_exec(
            crane,
            "push",
            str(image_path),
            dest,
            "--insecure",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"crane push failed for {dest}: {stderr.decode()}")
        digest = _parse_crane_digest(stdout.decode().strip(), dest)
        logger.info("Pushed %s: %s", dest, digest)
        return digest


def _parse_crane_digest(stdout: str, dest: str) -> str:
    if "@sha256:" in stdout:
        return "sha256:" + stdout.split("@sha256:", 1)[1].split()[0]
    raise RuntimeError(f"crane push did not return digest for {dest}: {stdout!r}")
