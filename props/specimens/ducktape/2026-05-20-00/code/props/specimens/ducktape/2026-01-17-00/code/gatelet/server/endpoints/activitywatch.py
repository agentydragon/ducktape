from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from aw_client import ActivityWatchClient

from gatelet.server.config import ActivityWatchSettings

logger = logging.getLogger(__name__)


async def fetch_recent_activity(aw_settings: ActivityWatchSettings, minutes: int = 15) -> dict[str, Any] | None:
    """Fetch ActivityWatch activity summary for the given window."""
    if not aw_settings.enabled:
        return None

    parsed = urlparse(aw_settings.server_url)

    def _duration_seconds(ev: Any) -> float:
        dur = ev.get("duration", 0)
        if isinstance(dur, timedelta):
            return dur.total_seconds()
        return float(dur)

    def _query() -> dict[str, Any] | None:
        try:
            awc = ActivityWatchClient("gatelet", host=parsed.hostname, port=parsed.port, protocol=parsed.scheme)
            awc.connect()

            buckets = awc.get_buckets()
            window_bk = [b for b in buckets if b.startswith("aw-watcher-window")]
            web_bk = [b for b in buckets if b.startswith("aw-watcher-web")]
            afk_bk = [b for b in buckets if b.startswith("aw-watcher-afk")]

            now = datetime.now(UTC)
            start = now - timedelta(minutes=minutes)

            app_secs: dict[str, float] = defaultdict(float)
            url_secs: dict[str, float] = defaultdict(float)
            afk_secs = 0.0
            active_secs = 0.0

            for bid in window_bk:
                for ev in awc.get_events(bid, start=start, end=now):
                    app = ev.get("data", {}).get("app", "unknown-app")
                    app_secs[app] += _duration_seconds(ev)

            for bid in web_bk:
                for ev in awc.get_events(bid, start=start, end=now):
                    url = ev.get("data", {}).get("url")
                    if url:
                        url_secs[url] += _duration_seconds(ev)

            for bid in afk_bk:
                for ev in awc.get_events(bid, start=start, end=now):
                    status = ev.get("data", {}).get("status", "unknown")
                    if status == "afk":
                        afk_secs += _duration_seconds(ev)
                    else:
                        active_secs += _duration_seconds(ev)

            return {
                "active": timedelta(seconds=active_secs),
                "afk": timedelta(seconds=afk_secs),
                "app": [
                    (app, timedelta(seconds=secs))
                    for app, secs in sorted(app_secs.items(), key=lambda kv: kv[1], reverse=True)
                ],
                "url": [
                    (url, timedelta(seconds=secs))
                    for url, secs in sorted(url_secs.items(), key=lambda kv: kv[1], reverse=True)
                ],
            }
        except Exception as exc:  # pragma: no cover - network errors
            logger.error("Failed to fetch ActivityWatch data: %s", exc)
            return None

    return await asyncio.to_thread(_query)
