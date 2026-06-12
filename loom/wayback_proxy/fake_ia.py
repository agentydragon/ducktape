"""Canned Internet Archive stand-in for wayback proxy tests.

Implements just enough of the Availability API (``/wayback/available``), the
CDX API (``/cdx/search/cdx`` with ``url``, ``to``, ``limit=-1``,
``output=json``), and the ``/web/<ts><modifier>/<url>`` replay endpoint, from a
literal capture table. Deliberately independent of proxy.py — this module is
the test oracle pinning what we believe IA does: Availability's single closest
snapshot JSON, header-row CDX JSON, empty CDX body when no captures match,
``Memento-Datetime`` on replayed captures (including archived 404s), 302
timestamp canonicalization, and captured live-web redirects.

Runs in the test process: in-process for test_proxy.py, bound on 0.0.0.0 for
the compose e2e (the proxy container reaches it via host.docker.internal).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from aiohttp import web

CDX_PATH = "/cdx/search/cdx"
AVAILABILITY_PATH = "/wayback/available"
CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
MEMENTO_HEADER = {"Memento-Datetime": "Wed, 15 Jan 2020 10:30:00 GMT"}

_WEB_RE = re.compile(r"^/web/(\d{4,14})([a-z]{2}_)?/(.*)$")

AS_OF = date(2020, 6, 1)

OLD_TS = "20180704120000"
GOOD_TS = "20200115103000"
CANON_LISTED_TS = "20200301000000"
DRIFT_LISTED_TS = "20200401000000"
TOO_NEW_TS = "20230301000000"

# Plain page with pre- and post-as_of captures; CDX `original` is the https
# form (exercises fetching by the original column, not the client's URL).
EXAMPLE_URL = "http://example.com/"
EXAMPLE_ORIGINAL = "https://example.com/"
EXAMPLE_BODY = b"<html><body>archived example.com as of 2020-01-15</body></html>\n"

# Only captured after as_of: must 404 through the proxy.
FUTURE_ONLY_URL = "http://newsite.example/launch"

# Replay of the CDX-listed ts 302s to the canonical capture ts (IA behavior
# for inexact timestamps); the canonical hop is still within as_of.
CANON_URL = "http://redirects.example/page"
CANON_BODY = b"canonical capture body\n"

# Replay 302s forward past as_of: the proxy must refuse the hop.
DRIFT_URL = "http://drift.example/page"

# Captured live-web redirect: replay responds 302 to an off-archive https
# URL; the proxy must bounce the client to its http form.
MOVED_URL = "http://moved.example/old"
MOVED_TARGET = "https://moved.example/new"

# The newest capture at-or-before as_of is an archived 404 — correct as-of
# content, served with Memento-Datetime.
GONE_URL = "http://gone.example/page"
GONE_BODY = b"not found, as of 2020\n"

# CDX itself fails for this URL (tests 503 -> 502 mapping).
CDX_BROKEN_URL = "http://cdx-broken.example/"

# CDX fails for this URL, but Availability succeeds. Proves normal browsing does
# not touch CDX on the happy path.
CDX_FAILS_BUT_AVAILABLE_URL = "http://available-only.example/"
CDX_FAILS_BUT_AVAILABLE_BODY = b"served without touching cdx\n"

# Replay returns a cache-style 503 + Retry-After once, then succeeds. Exercises
# proxy-side Retry-After honoring without making tests sleep.
REPLAY_RETRY_AFTER_ONCE_URL = "http://retry-after-once.example/"
REPLAY_RETRY_AFTER_ONCE_BODY = b"served after retry-after\n"


@dataclass(frozen=True)
class Replay:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html"
    redirect_to: str | None = None


# CDX index: client-facing URL -> [(timestamp, original), ...] ascending.
CDX_CAPTURES: dict[str, list[tuple[str, str]]] = {
    EXAMPLE_URL: [(OLD_TS, EXAMPLE_ORIGINAL), (GOOD_TS, EXAMPLE_ORIGINAL), (TOO_NEW_TS, EXAMPLE_ORIGINAL)],
    FUTURE_ONLY_URL: [(TOO_NEW_TS, FUTURE_ONLY_URL)],
    CANON_URL: [(CANON_LISTED_TS, CANON_URL)],
    DRIFT_URL: [(DRIFT_LISTED_TS, DRIFT_URL)],
    MOVED_URL: [(GOOD_TS, MOVED_URL)],
    GONE_URL: [(GOOD_TS, GONE_URL)],
    CDX_FAILS_BUT_AVAILABLE_URL: [(GOOD_TS, CDX_FAILS_BUT_AVAILABLE_URL)],
    REPLAY_RETRY_AFTER_ONCE_URL: [(GOOD_TS, REPLAY_RETRY_AFTER_ONCE_URL)],
}

# Replay table: (timestamp, original URL) -> response.
REPLAYS: dict[tuple[str, str], Replay] = {
    (GOOD_TS, EXAMPLE_ORIGINAL): Replay(body=EXAMPLE_BODY),
    (OLD_TS, EXAMPLE_ORIGINAL): Replay(body=b"older capture\n"),
    (CANON_LISTED_TS, CANON_URL): Replay(redirect_to=f"/web/{GOOD_TS}id_/{CANON_URL}"),
    (GOOD_TS, CANON_URL): Replay(body=CANON_BODY),
    (DRIFT_LISTED_TS, DRIFT_URL): Replay(redirect_to=f"/web/{TOO_NEW_TS}id_/{DRIFT_URL}"),
    (TOO_NEW_TS, DRIFT_URL): Replay(body=b"post-as_of content that must never be served\n"),
    (GOOD_TS, MOVED_URL): Replay(redirect_to=MOVED_TARGET),
    (GOOD_TS, GONE_URL): Replay(status=404, body=GONE_BODY),
    (GOOD_TS, CDX_FAILS_BUT_AVAILABLE_URL): Replay(body=CDX_FAILS_BUT_AVAILABLE_BODY),
    (GOOD_TS, REPLAY_RETRY_AFTER_ONCE_URL): Replay(body=REPLAY_RETRY_AFTER_ONCE_BODY),
}

REPLAY_RETRY_AFTER_COUNTS: dict[tuple[str, str], int] = {}


def _scheme_insensitive_key(url: str) -> str:
    """Drop the scheme, mirroring IA's scheme-insensitive CDX urlkey matching."""
    return url.split("://", 1)[-1]


def _captures_for(url: str) -> list[tuple[str, str]]:
    """CDX captures for `url`, matched scheme-insensitively like real IA."""
    if (exact := CDX_CAPTURES.get(url)) is not None:
        return exact
    wanted = _scheme_insensitive_key(url)
    return next((v for k, v in CDX_CAPTURES.items() if _scheme_insensitive_key(k) == wanted), [])


def _cdx_response(request: web.BaseRequest) -> web.Response:
    url = request.query["url"]
    if url in (CDX_BROKEN_URL, CDX_FAILS_BUT_AVAILABLE_URL):
        return web.Response(status=503, text="CDX is having a bad day\n")
    to_ts = request.query.get("to", "99999999999999").ljust(14, "9")
    matching = [capture for capture in _captures_for(url) if capture[0] <= to_ts]
    if request.query.get("limit") == "-1":
        matching = matching[-1:]
    if not matching:
        # Real CDX returns an empty body (not an empty JSON array) for no matches.
        return web.Response(body=b"")
    rows = [CDX_HEADER] + [
        [f"fake({url})", ts, original, "text/html", "200", "FAKEDIGEST", "123"] for ts, original in matching
    ]
    return web.Response(body=json.dumps(rows).encode(), content_type="application/json")


def _availability_response(request: web.BaseRequest) -> web.Response:
    url = request.query["url"]
    timestamp = request.query.get("timestamp", "99999999999999").ljust(14, "9")
    captures = _captures_for(url)

    # The real API appears to center "closest" around the requested timestamp,
    # not necessarily "newest <= timestamp"; force the future-only fixture to
    # exercise the proxy's own future-timestamp guard and CDX fallback.
    matching = [capture for capture in captures if capture[0] <= timestamp]
    if not matching and captures:
        matching = [captures[0]]

    # Model Availability being less expressive than CDX: archived non-200
    # captures may not be returned as "available", so the proxy must fall back to
    # CDX to preserve historical 404/500 semantics.
    if url == GONE_URL or not matching:
        payload = {"url": url, "archived_snapshots": {}}
        return web.Response(body=json.dumps(payload).encode(), content_type="application/json")

    ts, original = matching[-1]
    payload = {
        "url": url,
        "archived_snapshots": {
            "closest": {
                "status": "200",
                "available": True,
                "url": f"http://web.archive.org/web/{ts}/{original}",
                "timestamp": ts,
            }
        },
    }
    return web.Response(
        body=json.dumps(payload).encode(), content_type="application/json", headers={"x-rl": "0", "x-na": "0"}
    )


async def handle(request: web.BaseRequest) -> web.StreamResponse:
    raw_path = request.raw_path
    if raw_path.startswith(AVAILABILITY_PATH):
        return _availability_response(request)
    if raw_path.startswith(CDX_PATH):
        return _cdx_response(request)
    if (match := _WEB_RE.match(raw_path)) is not None:
        ts, _modifier, inner = match.groups()
        if (ts, inner) == (GOOD_TS, REPLAY_RETRY_AFTER_ONCE_URL):
            key = (ts, inner)
            count = REPLAY_RETRY_AFTER_COUNTS.get(key, 0)
            REPLAY_RETRY_AFTER_COUNTS[key] = count + 1
            if count == 0:
                return web.Response(status=503, text="archive shard busy\n", headers={"Retry-After": "0"})
        replay = REPLAYS.get((ts, inner))
        if replay is None:
            return web.Response(status=404, text="snapshot not found\n")  # IA-level miss: no Memento header
        if replay.redirect_to is not None:
            return web.Response(status=302, headers={"Location": replay.redirect_to})
        return web.Response(
            status=replay.status, body=replay.body, headers={"Content-Type": replay.content_type, **MEMENTO_HEADER}
        )
    return web.Response(status=404, text="unknown fake-ia path\n")


async def start(port: int = 0, host: str = "127.0.0.1") -> web.ServerRunner:
    """Start the fake; bound port is in runner.addresses."""
    REPLAY_RETRY_AFTER_COUNTS.clear()
    runner = web.ServerRunner(web.Server(handle))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
