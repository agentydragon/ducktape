"""Pre-commit hook installation and background environment setup.

Calls pre-commit's Python API directly rather than shelling out.
"""

import logging
import threading
from pathlib import Path
from typing import Literal

from pre_commit.commands.install_uninstall import install, install_hooks
from pre_commit.constants import VERSION as PRE_COMMIT_VERSION
from pre_commit.store import Store
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PrecommitNotInstalled(BaseModel):
    """pre-commit hook was not installed (install failed, etc.)."""

    kind: Literal["not_installed"] = "not_installed"


class PrecommitInstallingHooks(BaseModel):
    """pre-commit hook freshly installed and background `install-hooks` is running."""

    kind: Literal["installing_hooks"] = "installing_hooks"


PrecommitSetup = PrecommitNotInstalled | PrecommitInstallingHooks


def install_precommit(project_dir: Path) -> PrecommitSetup:
    """Install git pre-commit hook and eagerly pre-install hook environments.

    Calls pre-commit's Python API directly for hook installation. Fires off
    install_hooks() in a daemon thread (long-running environment setup).

    Always runs install-hooks in the background, even if the hook file already
    exists. The hook file persists across sessions but ~/.cache/pre-commit/
    environments may not, so we always ensure environments are populated.
    """
    logger.info("pre-commit %s", PRE_COMMIT_VERSION)

    config_file = str(project_dir / ".pre-commit-config.yaml")
    git_dir = str(project_dir / ".git")
    store = Store()

    rc = install(config_file=config_file, store=store, hook_types=None, overwrite=False, hooks=False, git_dir=git_dir)
    if rc != 0:
        logger.warning("pre-commit install returned %d", rc)
        return PrecommitNotInstalled()

    logger.info("Installed git pre-commit hook")

    # Fire off install-hooks in a daemon thread to eagerly pre-install hook
    # environments (especially the ansible language:python venv). Without this,
    # the first commit pays the cost of creating the venv and downloading ansible.
    # pre-commit uses flock on ~/.cache/pre-commit/.lock, so this is safe to run
    # concurrently with a hook-triggered run.
    def _bg_install_hooks() -> None:
        try:
            install_hooks(config_file, store)
            logger.info("Background pre-commit install-hooks completed")
        except Exception:
            logger.exception("Background pre-commit install-hooks failed")

    thread = threading.Thread(target=_bg_install_hooks, daemon=True, name="pre-commit-install-hooks")
    thread.start()
    logger.info("Started background pre-commit install-hooks (thread %s)", thread.name)
    return PrecommitInstallingHooks()
