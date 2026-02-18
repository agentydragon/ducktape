"""Pre-commit hook installation and background environment setup."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PrecommitNotInstalled(BaseModel):
    """pre-commit hook was not installed (binary missing, install failed, etc.)."""

    kind: Literal["not_installed"] = "not_installed"


class PrecommitInstallingHooks(BaseModel):
    """pre-commit hook freshly installed and background `install-hooks` is running."""

    kind: Literal["installing_hooks"] = "installing_hooks"
    pid: int
    log_path: Path


PrecommitSetup = PrecommitNotInstalled | PrecommitInstallingHooks


def install_precommit(project_dir: Path, session_dir: Path) -> PrecommitSetup:
    """Install git pre-commit hook and eagerly pre-install hook environments.

    If `pre-commit` is not on PATH, installs it via pip first.
    pre-commit is not pre-installed in Claude Code on the web's container.
    pre-commit itself handles missing .git, missing config, and idempotent installs.

    Always runs `install-hooks` in the background, even if the hook file already
    exists. The hook file persists across sessions but ~/.cache/pre-commit/
    environments may not, so we always ensure environments are populated.
    """
    precommit = shutil.which("pre-commit")
    if not precommit:
        logger.info("pre-commit not found on PATH, installing via pip...")
        try:
            subprocess.run(["pip", "install", "pre-commit"], check=True, capture_output=True, text=True, timeout=120)
            logger.info("Installed pre-commit via pip")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning("Failed to install pre-commit via pip: %s", e)
            return PrecommitNotInstalled()
        precommit = shutil.which("pre-commit")
        if not precommit:
            logger.warning("pre-commit still not found on PATH after pip install")
            return PrecommitNotInstalled()

    try:
        version = subprocess.run([precommit, "--version"], capture_output=True, text=True, check=True, timeout=5)
        logger.info("pre-commit %s", version.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    hook_installed = False
    try:
        install_result = subprocess.run(
            [precommit, "install"], check=False, cwd=project_dir, capture_output=True, text=True, timeout=30
        )
        if install_result.returncode == 0:
            logger.info("Installed git pre-commit hook")
            hook_installed = True
        else:
            logger.warning("pre-commit install failed: %s", install_result.stderr)
    except subprocess.TimeoutExpired:
        logger.warning("pre-commit install timed out")

    if not hook_installed:
        return PrecommitNotInstalled()

    # Launch install-hooks in the background to eagerly pre-install hook
    # environments (especially the ansible language:python venv). Without this,
    # the first commit pays the cost of creating the venv and downloading ansible.
    # pre-commit uses flock on ~/.cache/pre-commit/.lock, so this is safe to run
    # concurrently with a hook-triggered run.
    log_path = session_dir / "pre-commit-install-hooks.log"
    log_fh = log_path.open("w")
    proc = subprocess.Popen(
        [precommit, "install-hooks"], cwd=project_dir, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True
    )
    logger.info("Started background pre-commit install-hooks (pid %d), log: %s", proc.pid, log_path)
    return PrecommitInstallingHooks(pid=proc.pid, log_path=log_path)
