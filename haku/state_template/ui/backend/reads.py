"""Feature reads — compose the generic Forgejo client into haku-state's data shapes.

Nearly every surface now composes over the generic tree+blobs proxy on the *frontend* (items,
garden, runs, responses) — see repo.ts/client.ts. What's left here is the one read that isn't a
content fetch: the "last scan" freshness stamp, derived from the commit log.
"""

from __future__ import annotations

from forgejo import Forgejo

# Haku-the-scanner commits as this author; the "last scan" time is the newest such commit
# (NOT the UI's response/feedback writes, NOT Flux image-automation commits).
_HAKU_AUTHOR_EMAIL = "haku@example.com"


async def read_scan_time(forgejo: Forgejo) -> str:
    """When haku-state last meaningfully changed (ISO 8601): the newest Haku-authored commit,
    surfaced to the UI footer as "last scan". Items themselves are read from `items/*.md` via the
    generic proxy — the frontend composes them; this is just the freshness stamp."""
    commits = await forgejo.commits()
    return next(
        (c["commit"]["author"]["date"] for c in commits if c["commit"]["author"]["email"] == _HAKU_AUTHOR_EMAIL),
        commits[0]["commit"]["author"]["date"],
    )
