"""Partial-clone git store for the cpap-data repo (subprocess git CLI).

The nightly sync only needs HEAD's tree and the manifest blob — not the
multi-GB EDF archive — so `clone()` uses `--depth=1 --filter=blob:none
--no-checkout` (KB-scale transfer; historical blobs stay server-side and are
lazily fetched from the promisor remote on demand). `read-tree HEAD` then
populates the index so a later commit of only newly downloaded files preserves
the rest of the tree.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def _auth_env(username: str | None, password: str | None) -> dict[str, str]:
    """Env with Basic-auth `http.extraHeader` git config, so credentials travel
    via git's environment-config mechanism rather than argv or the remote URL
    (precedent: finance/evidence/checkout.py). No-credential mode (both None)
    serves tests against `file://` remotes."""
    if (username is None) != (password is None):
        raise ValueError("username and password must be both set or both unset")
    if username is None or password is None:
        return dict(os.environ)
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return os.environ | {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
    }


@dataclass
class GitStore:
    workdir: Path
    branch: str
    env: dict[str, str]

    @classmethod
    def clone(
        cls, url: str, dest: Path, *, branch: str = "main", username: str | None = None, password: str | None = None
    ) -> GitStore:
        env = _auth_env(username, password)
        subprocess.run(
            ["git", "clone", "--depth=1", "--filter=blob:none", "--no-checkout", "--branch", branch, url, str(dest)],
            check=True,
            env=env,
        )
        store = cls(workdir=dest, branch=branch, env=env)
        store._run("read-tree", "HEAD")
        return store

    def _run(self, *args: str) -> None:
        """Run git in the workdir, inheriting stdout/stderr (visible in job logs)."""
        subprocess.run(["git", "-C", str(self.workdir), *args], check=True, env=self.env)

    def _capture(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", "-C", str(self.workdir), *args], check=check, env=self.env, capture_output=True)

    def head_blob(self, path: str) -> bytes | None:
        """Content of HEAD:<path>, or None if absent. May lazily fetch the blob from the promisor remote."""
        if not self._capture("ls-tree", "HEAD", "--", path).stdout.strip():
            return None
        return self._capture("cat-file", "blob", f"HEAD:{path}").stdout

    def stage(self, paths: Iterable[str]) -> None:
        self._run("add", "--", *paths)

    def has_staged_changes(self) -> bool:
        result = self._capture("diff", "--cached", "--quiet", check=False)
        if result.returncode not in (0, 1):
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        return result.returncode == 1

    def commit(self, message: str, *, author_name: str, author_email: str) -> None:
        self._run("-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", message)

    def push(self) -> None:
        self._run("push", "origin", f"HEAD:{self.branch}")
