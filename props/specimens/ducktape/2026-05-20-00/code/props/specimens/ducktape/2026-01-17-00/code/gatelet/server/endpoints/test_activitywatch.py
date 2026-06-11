from gatelet.server.config import ActivityWatchSettings
from gatelet.server.endpoints import activitywatch
from gatelet.server.tests import activitywatch_sample as sample

EPS = 0.01


async def test_fetch_recent_activity_disabled():
    aw_settings = ActivityWatchSettings(enabled=False)
    result = await activitywatch.fetch_recent_activity(aw_settings)
    assert result is None


async def test_fetch_recent_activity(monkeypatch):
    class StubClient:
        def connect(self):
            pass

        def get_buckets(self):
            return sample.SAMPLE_BUCKETS

        def get_events(self, bucket_id, start=None, end=None):
            if bucket_id.startswith("aw-watcher-window"):
                return sample.SAMPLE_WINDOW_EVENTS
            if bucket_id.startswith("aw-watcher-web"):
                return sample.SAMPLE_WEB_EVENTS
            if bucket_id.startswith("aw-watcher-afk"):
                return sample.SAMPLE_AFK_EVENTS
            return []

    monkeypatch.setattr(activitywatch, "ActivityWatchClient", lambda *a, **k: StubClient())

    aw_settings = ActivityWatchSettings(enabled=True, server_url="http://localhost:5600")
    result = await activitywatch.fetch_recent_activity(aw_settings, minutes=10)
    assert result is not None
    assert abs(result["active"].total_seconds() / 60 - 3.0) < EPS
    assert abs(result["afk"].total_seconds() / 60 - 0.5) < EPS
    assert result["app"][0][0] == "ExampleBrowser"
    assert result["url"][0][0] == "https://example.com"
