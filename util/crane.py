"""Crane (go-containerregistry) CLI wrapper.

Sync and async wrappers over the subset of the CLI this repository uses. Auth is
passed via a temporary DOCKER_CONFIG directory, never written to ~/.docker.

Standard library only, and it takes the binary rather than finding one: the
callers do not agree on where crane comes from. Under Bazel it is a runfile
(`util/oci.py` resolves that one); on a GitHub Actions runner the publish
planners import this module as bare `python3 -m`, with crane on PATH from the
workflow's setup-crane step and no Bazel, no pypi and no runfiles in sight.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# The registry's own error codes, from the OCI distribution spec, which crane
# passes through in its message — it has no structured output mode (checked on
# 0.18.0: the only global flags are --platform, --insecure and -v, and -v dumps
# the error JSON into a debug log rather than to stdout).
#
# Absence must stay distinct from a transport or auth failure: callers read it as
# "nothing published yet, go ahead", so a blip read as absence churns a fresh push
# past whatever watches the registry.
#
# GOTCHA: on GHCR these cover an absent *tag* but not an absent *repository*.
# Fetching a pull token for a repository that does not exist is refused before any
# manifest request, so crane reports `DENIED: requested access to the resource is
# denied` — indistinguishable from a genuine permission failure, which is why it is
# not listed here. A first-ever push of a newly added image may therefore raise
# rather than read as absent. Unverified with push-scoped credentials, which may
# get a token and then a real NAME_UNKNOWN.
_ABSENT_MARKERS = ("NAME_UNKNOWN", "MANIFEST_UNKNOWN")


class CraneError(Exception):
    """A crane invocation exited non-zero.

    Its own type rather than `RuntimeError` so a caller can say "crane failed"
    without also catching every unrelated failure raised beneath it — one of them
    decides whether to publish an image, and swallowing the wrong exception there
    republishes on a bug somewhere else entirely.

    Keeps crane's streams rather than only a rendered message, so
    `repository_absent` classifies on what crane wrote to stderr instead of
    searching prose that also contains the reference being looked up.
    """

    def __init__(self, command: tuple[str, ...], returncode: int | None, stderr: str, stdout: str) -> None:
        super().__init__(_format_crane_error(command, returncode, stderr, stdout))
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout

    @property
    def repository_absent(self) -> bool:
        """Whether crane failed because nothing has ever been pushed there."""
        return any(marker in self.stderr for marker in _ABSENT_MARKERS)


def find_crane() -> Path:
    """The crane on PATH — how a shell or a CI runner gets one."""
    if found := shutil.which("crane"):
        return Path(found)
    raise RuntimeError("crane is not on PATH")


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
        self._path = path or find_crane()
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
            raise CraneError(args, e.returncode, e.stderr or "", e.stdout or "") from e
        return result.stdout.strip()

    async def _arun(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            str(self._path), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise CraneError(args, proc.returncode, stderr.decode(), stdout.decode())
        return stdout.decode().strip()

    def digest(self, image_ref: str) -> str:
        return self._run("digest", image_ref)

    def digest_or_none(self, image_ref: str) -> str | None:
        """Remote digest of `image_ref`, or None when the tag/repo doesn't exist yet.

        For content-dedup before a push: an unpublished tag or repo means "nothing
        there, push it". Any other crane failure (auth, transport, 5xx) re-raises —
        see CraneError for why a real error must not be read as "absent".
        """
        try:
            return self._run("digest", image_ref)
        except CraneError as e:
            if e.repository_absent:
                return None
            raise

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


def parse_pushed_digest(stdout: str, dest: str) -> str:
    """The digest crane reports after a push, from its `repo@sha256:...` output."""
    if "@sha256:" in stdout:
        return "sha256:" + stdout.split("@sha256:", 1)[1].split(maxsplit=1)[0]
    raise RuntimeError(f"crane push did not return digest for {dest}: {stdout!r}")
