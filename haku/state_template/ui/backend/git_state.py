"""Owns a working clone of the haku-state repo: read items + clicks, and apply
operator actions (clicks / feedback) as git commits.

This UI's backend is a writer to haku-state ``main`` alongside Haku's own runs, so
every mutation reconciles against origin (fetch + hard-reset local branch) and
retries the push on a non-fast-forward. Commits carry a distinct identity so Haku
can attribute them. pygit2 talks to the cluster-internal plaintext-HTTP Forgejo
(``http://forgejo-http.forgejo:3000/haku/haku-state.git``), so no TLS/CA handling
is needed.

All methods here are synchronous (libgit2 is blocking); callers serialize them
under ``self.lock`` and run them via ``asyncio.to_thread`` (see ``app``).

Ported from ``haku/console/git_state.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Mapping
from pathlib import Path

import pygit2
import yaml
from models import Item

logger = logging.getLogger(__name__)

# Commits attributed to the UI so Haku can tell them apart from its own runs.
UI_SIGNATURE = pygit2.Signature("haku-ui", "haku-ui@allegedly.works")


class GitState:
    def __init__(self, *, repo_url: str, username: str, password: str, clone_dir: Path, branch: str = "main") -> None:
        self._url = repo_url
        self._username = username
        self._password = password
        self._dir = Path(clone_dir)
        self._branch = branch
        self._repo: pygit2.Repository | None = None
        # Serializes the read-modify-write-push critical section against the
        # background pull loop and other requests (single in-pod writer).
        self.lock = asyncio.Lock()

    def _callbacks(self) -> pygit2.RemoteCallbacks:
        return pygit2.RemoteCallbacks(credentials=pygit2.UserPass(self._username, self._password))

    @property
    def repo(self) -> pygit2.Repository:
        if self._repo is None:
            raise RuntimeError("GitState.clone_or_open() has not run")
        return self._repo

    def clone_or_open(self) -> None:
        """Clone haku-state on first start, or reuse + reconcile an existing clone."""
        if (self._dir / ".git").exists():
            self._repo = pygit2.Repository(str(self._dir))
            self.reconcile()
        else:
            self._dir.parent.mkdir(parents=True, exist_ok=True)
            self._repo = pygit2.clone_repository(
                self._url, str(self._dir), checkout_branch=self._branch, callbacks=self._callbacks()
            )
        logger.info("haku-state ready at %s (HEAD %s)", self._dir, self.repo.head.target)

    def reconcile(self) -> None:
        """Fetch origin and hard-reset the local branch to ``origin/<branch>``."""
        self.repo.remotes["origin"].fetch(callbacks=self._callbacks())
        origin = self.repo.lookup_reference(f"refs/remotes/origin/{self._branch}").target
        self.repo.reset(origin, pygit2.enums.ResetMode.HARD)

    def read_items(self) -> list[Item]:
        items_dir = self._dir / "items"
        if not items_dir.is_dir():
            return []
        return [Item.model_validate(yaml.safe_load(p.read_text())) for p in sorted(items_dir.glob("*.yaml"))]

    def commit_push(self, changes: Mapping[str, bytes | None], message: str, *, retries: int = 5) -> None:
        """Apply ``changes`` (relpath → bytes, or None to delete), commit, and push.

        Reconciles before each attempt and retries on a non-fast-forward push so a
        concurrent Haku push doesn't lose the operator's action.
        """
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            self.reconcile()
            self._apply(changes)
            if not self._commit(message):
                logger.info("no effective change for %r; skipping commit", message)
                return
            try:
                self.repo.remotes["origin"].push([f"refs/heads/{self._branch}"], callbacks=self._callbacks())
                logger.info("pushed %r", message)
                return
            except pygit2.GitError as err:  # remote moved under us → reconcile + retry
                last_err = err
                logger.warning("push rejected (%d/%d): %s", attempt, retries, err)
                time.sleep(0.4 * attempt)
        raise RuntimeError(f"push failed after {retries} attempts: {last_err}")

    def _apply(self, changes: Mapping[str, bytes | None]) -> None:
        index = self.repo.index
        for relpath, data in changes.items():
            target = self._dir / relpath
            if data is None:
                target.unlink(missing_ok=True)
                with contextlib.suppress(KeyError, pygit2.GitError):
                    index.remove(relpath)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                index.add(relpath)
        index.write()

    def _commit(self, message: str) -> bool:
        tree = self.repo.index.write_tree()
        head = self.repo.head
        head_commit = self.repo[head.target].peel(pygit2.Commit)
        if tree == head_commit.tree_id:
            return False
        self.repo.create_commit(head.name, UI_SIGNATURE, UI_SIGNATURE, message, tree, [head_commit.id])
        return True

    # --- operator-action overlay (clicks/) + free-form feedback -----------------
    # The UI never edits items/ (Haku owns those). It records a clicked action as
    # clicks/<item_id>/<action_id> (and removes it on un-click); Haku reduces these
    # on its next run. The git history of clicks/ is the click/un-click event log.

    @staticmethod
    def _click_path(item_id: str, action_id: str) -> str:
        return f"clicks/{item_id}/{action_id}"

    def read_clicks(self) -> set[tuple[str, str]]:
        """Currently-clicked (item_id, action_id) pairs, from the clicks/ overlay."""
        root = self._dir / "clicks"
        if not root.is_dir():
            return set()
        return {
            (item_dir.name, click.name)
            for item_dir in root.iterdir()
            if item_dir.is_dir()
            for click in item_dir.iterdir()
            if click.is_file()
        }

    def set_click(self, item_id: str, action_id: str) -> None:
        stamp = f"clicked_at: {dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}\n"
        self.commit_push({self._click_path(item_id, action_id): stamp.encode()}, f"ui: click {action_id} on {item_id}")

    def clear_click(self, item_id: str, action_id: str) -> None:
        self.commit_push({self._click_path(item_id, action_id): None}, f"ui: unclick {action_id} on {item_id}")

    def write_feedback(self, text: str, item_id: str | None = None) -> None:
        # Per-item feedback names the item id in the note (and filename); Haku's run
        # contract treats an intake note referencing an item id as feedback on it.
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        if item_id is None:
            path, heading, message = f"intake/{stamp}-feedback.md", "Operator feedback", "ui: feedback"
        else:
            path = f"intake/{stamp}-feedback-{item_id}.md"
            heading = f"Operator feedback on {item_id}"
            message = f"ui: feedback on {item_id}"
        body = f"# {heading} ({stamp})\n\n{text.strip()}\n"
        self.commit_push({path: body.encode()}, message)
