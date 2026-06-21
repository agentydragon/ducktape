"""Startup bootstrap: write git creds, then clone/refresh ducktape (context) and
haku-state (memory) so the agent has both checked out before its first wake.

Clones via pygit2 (hermetic — no git binary needed, like the console). A `~/.netrc` is
written from the same creds so the agent's own shell `git` commits/pushes (via
run_command) authenticate too. `*_repo_url = None` skips that clone (the dir is assumed
already present, e.g. in tests or a pre-seeded volume).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pygit2

from haku.agent.config import Settings

logger = logging.getLogger(__name__)


def write_netrc(*, host: str, username: str, password: str, path: Path) -> None:
    path.write_text(f"machine {host}\n  login {username}\n  password {password}\n")
    path.chmod(0o600)


def clone_or_refresh(repo_url: str, dest: Path, *, callbacks: pygit2.RemoteCallbacks, depth: int = 0) -> None:
    """Clone `repo_url` into `dest`, or fetch + hard-reset an existing clone to origin."""
    if (dest / ".git").exists():
        repo = pygit2.Repository(str(dest))
        branch = repo.head.shorthand
        repo.remotes["origin"].fetch(callbacks=callbacks)
        repo.reset(repo.lookup_reference(f"refs/remotes/origin/{branch}").target, pygit2.enums.ResetMode.HARD)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        pygit2.clone_repository(repo_url, str(dest), callbacks=callbacks, depth=depth)
    logger.info("ready: %s", dest)


def bootstrap(settings: Settings) -> None:
    """Clone/refresh ducktape + haku-state and write git creds. Synchronous (libgit2 is
    blocking); call via `asyncio.to_thread` from async startup."""
    creds: pygit2.UserPass | None = None
    if settings.git_username and settings.git_password:
        creds = pygit2.UserPass(settings.git_username, settings.git_password)
        if settings.git_host:
            write_netrc(
                host=settings.git_host,
                username=settings.git_username,
                password=settings.git_password,
                path=Path.home() / ".netrc",
            )
    callbacks = pygit2.RemoteCallbacks(credentials=creds)
    if settings.ducktape_repo_url:
        clone_or_refresh(
            settings.ducktape_repo_url, settings.ducktape_dir, callbacks=callbacks, depth=settings.ducktape_clone_depth
        )
    if settings.state_repo_url:
        clone_or_refresh(settings.state_repo_url, settings.state_dir, callbacks=callbacks)
