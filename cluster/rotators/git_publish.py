"""Publish rotation outputs from a sparse Git checkout rooted at the current directory."""

import logging
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def github_remote(repository: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repository}.git"


def sparse_checkout(remote: str, paths: Iterable[Path | str]) -> None:
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], check=True)
    subprocess.run(["git", "config", "core.sparseCheckout", "true"], check=True)
    Path(".git/info/sparse-checkout").write_text("\n".join(map(str, paths)) + "\n")
    subprocess.run(["git", "fetch", "-q", "--depth=1", "--no-tags", "origin", "devel"], check=True)
    subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], check=True)


def push_outputs(
    paths: Iterable[Path], *, author_name: str, author_email: str, subject: str, names: Iterable[str]
) -> bool:
    """Return whether a commit was pushed. A failed push raises."""
    subprocess.run(["git", "config", "user.name", author_name], check=True)
    subprocess.run(["git", "config", "user.email", author_email], check=True)
    for path in paths:
        subprocess.run(["git", "add", "--", str(path)], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        logger.info("tokens minted but SOPS outputs are unchanged on disk; nothing to commit")
        return False
    summary = ", ".join(names)
    message = f"chore: rotate {subject} ({datetime.now(UTC):%Y-%m-%d}): {summary}"
    subprocess.run(["git", "commit", "-q", "-m", message], check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:devel"], check=True)
    logger.info("pushed: %s", summary)
    return True
