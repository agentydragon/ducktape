"""Git-scrape public evidence upstreams into the augur-evidence repo.

Clones the evidence repo, GETs each public source (FRED / Yahoo / Zillow) with
its per-source User-Agent, writes each into the working tree under its
`output_filename`, then commits-if-changed + pushes. Git provides history,
change-detection (`git add -A` stages nothing when bytes are unchanged, so
Zillow's monthly republish doesn't accrue an empty daily commit), and an atomic
"latest" (HEAD) — no object store, no pointer protocol.

Auth: GIT_USERNAME/GIT_PASSWORD injected into the http(s) remote URL (mirrors
the budget exporter). HOME=/tmp so git finds a writable config dir.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from finance.augur.ingest.evidence_sources import EVIDENCE_SOURCES, EvidenceSource
from finance.augur.ingest.http_fetch import FETCH_ERRORS, HttpGet, http_get as _real_http_get

logger = logging.getLogger(__name__)

_AUTHOR_NAME = "augur evidence scraper"
_AUTHOR_EMAIL = "augur@allegedly.works"


def _authenticated_url(url: str, *, username: str, password: str) -> str:
    """Inject git credentials into an http(s) remote URL."""
    parts = urlsplit(url)
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout


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


def commit_and_push(repo: Path, branch: str, *, now: datetime) -> bool:
    """Stage everything; commit + push only if the working tree changed. Returns True if pushed."""
    _git(repo, "add", "-A")
    if not _git(repo, "status", "--porcelain").strip():
        logger.info("evidence unchanged; nothing to commit")
        return False
    _git(
        repo,
        "-c",
        f"user.name={_AUTHOR_NAME}",
        "-c",
        f"user.email={_AUTHOR_EMAIL}",
        "commit",
        "-m",
        f"evidence: refresh {now.date().isoformat()}",
    )
    _git(repo, "push", "origin", branch)
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
) -> int:
    """Clone, refresh every source, commit-if-changed + push.

    Returns a process exit code: nonzero if any source failed to fetch, so the
    CronJob surfaces a partial outage. Sources that did fetch are still committed.
    """
    # Empty creds (tests against a local filesystem remote) clone the URL as-is.
    url = _authenticated_url(git_url, username=username, password=password) if username and password else git_url
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _git(Path(tmp), "clone", "--depth", "1", "--branch", branch, url, str(repo))
        failures = write_sources(repo, sources, http_get=http_get)
        commit_and_push(repo, branch, now=now)
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
