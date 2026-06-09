"""Git-scrape public evidence upstreams into the augur-evidence repo.

Clones the evidence repo, GETs each public source (FRED / Yahoo / Zillow) with
its per-source User-Agent, writes each into the working tree under its
`output_filename`, then commits-if-changed + pushes. Git provides history,
change-detection (an unchanged upstream produces the same tree, so Zillow's
monthly republish doesn't accrue an empty daily commit), and an atomic "latest"
(HEAD) — no object store, no pointer protocol.

Clone/commit/push go through pygit2 (libgit2); GIT_USERNAME/GIT_PASSWORD drive a
UserPass credential callback, so no credentials are embedded in the remote URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pygit2

from finance.augur.ingest.http_fetch import FETCH_ERRORS, HttpGet, http_get as _real_http_get
from finance.evidence.sources import EVIDENCE_SOURCES, EvidenceSource

logger = logging.getLogger(__name__)

_AUTHOR = pygit2.Signature("augur evidence scraper", "augur@allegedly.works")


def write_sources(workdir: Path, sources: Iterable[EvidenceSource], *, http_get: HttpGet) -> int:
    """GET each source and write it into `workdir/<output_filename>`.

    Per-source upstream failures are logged + counted, not raised (tenacity
    already rode out transient blips), so one dead upstream doesn't block
    refreshing the rest. Returns the failure count.
    """
    failures = 0
    for source in sources:
        try:
            body = http_get(source.upstream_url, source.user_agent)
        except FETCH_ERRORS:
            logger.warning("failed to fetch %s", source.provenance_label, exc_info=True)
            failures += 1
            continue
        (workdir / source.output_filename).write_bytes(body)
        logger.info("fetched %s -> %s (%d bytes)", source.provenance_label, source.output_filename, len(body))
    return failures


def commit_and_push(
    repo: pygit2.Repository, branch: str, *, now: datetime, callbacks: pygit2.RemoteCallbacks | None = None
) -> bool:
    """Stage everything; commit + push only if the tree changed. Returns True if pushed."""
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    head = repo.head
    head_commit = repo[head.target].peel(pygit2.Commit)
    if tree == head_commit.tree_id:
        logger.info("evidence unchanged; nothing to commit")
        return False
    repo.create_commit(
        head.name, _AUTHOR, _AUTHOR, f"evidence: refresh {now.date().isoformat()}", tree, [head_commit.id]
    )
    repo.remotes["origin"].push([f"refs/heads/{branch}"], callbacks=callbacks)
    logger.info("pushed refreshed evidence to %s", branch)
    return True


def run_scrape(
    git_url: str,
    branch: str,
    sources: Iterable[EvidenceSource],
    *,
    username: str,
    password: str,
    http_get: HttpGet,
    now: datetime,
    depth: int = 1,
) -> int:
    """Clone, refresh every source, commit-if-changed + push.

    Returns a process exit code: nonzero if any source failed to fetch, so the
    CronJob surfaces a partial outage. Sources that did fetch are still committed.

    `depth` defaults to a shallow (depth=1) clone — the scraper only needs the tip to write
    the refresh and commit on top of it, and the full history stays server-side in Forgejo.
    Tests against a local filesystem remote must pass `depth=0` (libgit2's local transport
    does not support shallow fetch).
    """
    # Empty creds (tests against a local filesystem remote) clone without a credential callback.
    callbacks = (
        pygit2.RemoteCallbacks(credentials=pygit2.UserPass(username, password)) if username and password else None
    )
    with tempfile.TemporaryDirectory() as tmp:
        repo_path = Path(tmp) / "repo"
        repo = pygit2.clone_repository(
            git_url, str(repo_path), checkout_branch=branch, callbacks=callbacks, depth=depth
        )
        failures = write_sources(repo_path, sources, http_get=http_get)
        commit_and_push(repo, branch, now=now, callbacks=callbacks)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="augur-evidence", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--git-url", required=True, help="Evidence repo http(s) URL (creds via GIT_USERNAME/GIT_PASSWORD)."
    )
    parser.add_argument("--branch", default="main", help="Branch to clone + push (default: main).")
    args = parser.parse_args(argv)

    return run_scrape(
        args.git_url,
        args.branch,
        EVIDENCE_SOURCES,
        username=os.environ["GIT_USERNAME"],
        password=os.environ["GIT_PASSWORD"],
        http_get=_real_http_get,
        now=datetime.now(UTC),
    )


if __name__ == "__main__":
    sys.exit(main())
