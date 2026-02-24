from __future__ import annotations

from datetime import UTC, datetime, timedelta

# Sample buckets used for ActivityWatch tests. Titles were redacted.
SAMPLE_BUCKETS: dict[str, dict[str, object]] = {
    "aw-watcher-afk_test": {
        "id": "aw-watcher-afk_test",
        "created": "2025-05-22T00:30:13.592843+00:00",
        "name": None,
        "type": "afkstatus",
        "client": "aw-watcher-afk",
        "hostname": "test",
        "data": {},
        "last_updated": "2025-05-22T09:53:48.805000+00:00",
    },
    "aw-watcher-window_test": {
        "id": "aw-watcher-window_test",
        "created": "2025-05-22T00:30:15.292850+00:00",
        "name": None,
        "type": "currentwindow",
        "client": "aw-watcher-window",
        "hostname": "test",
        "data": {},
        "last_updated": "2025-05-22T09:53:54.741000+00:00",
    },
    "aw-watcher-web_test": {
        "id": "aw-watcher-web_test",
        "created": "2025-05-22T00:30:15.292850+00:00",
        "name": None,
        "type": "web.tab.current",
        "client": "aw-watcher-web",
        "hostname": "test",
        "data": {},
        "last_updated": "2025-05-22T09:53:54.741000+00:00",
    },
}

SAMPLE_WINDOW_EVENTS = [
    {
        "id": 1,
        "timestamp": datetime(2025, 5, 22, 9, 50, 0, tzinfo=UTC),
        "duration": timedelta(seconds=120),
        "data": {"app": "ExampleBrowser", "title": "Example Domain"},
    },
    {
        "id": 2,
        "timestamp": datetime(2025, 5, 22, 9, 52, 0, tzinfo=UTC),
        "duration": timedelta(seconds=60),
        "data": {"app": "Editor", "title": "README.md"},
    },
]

SAMPLE_WEB_EVENTS = [
    {
        "id": 10,
        "timestamp": datetime(2025, 5, 22, 9, 50, 0, tzinfo=UTC),
        "duration": timedelta(seconds=90),
        "data": {"url": "https://example.com", "title": "Example"},
    },
    {
        "id": 11,
        "timestamp": datetime(2025, 5, 22, 9, 51, 30, tzinfo=UTC),
        "duration": timedelta(seconds=30),
        "data": {"url": "https://wikipedia.org", "title": "Wikipedia"},
    },
]

SAMPLE_AFK_EVENTS = [
    {
        "id": 20,
        "timestamp": datetime(2025, 5, 22, 9, 50, 0, tzinfo=UTC),
        "duration": timedelta(seconds=180),
        "data": {"status": "not-afk"},
    },
    {
        "id": 21,
        "timestamp": datetime(2025, 5, 22, 9, 53, 0, tzinfo=UTC),
        "duration": timedelta(seconds=30),
        "data": {"status": "afk"},
    },
]
