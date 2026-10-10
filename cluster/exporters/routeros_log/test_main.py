import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_bazel

from cluster.exporters.routeros_log.api import Connection
from cluster.exporters.routeros_log.entries import LogEntry
from cluster.exporters.routeros_log.main import Cursor, poll_once, push_body
from cluster.exporters.routeros_log.settings import Settings

_T0 = datetime(2026, 10, 10, 8, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 10, 10, 8, 0, 1, tzinfo=UTC)


def _entry(id: str, timestamp: datetime, topics: str = "interface,info", message: str = "m") -> LogEntry:
    return LogEntry(id=id, timestamp=timestamp, topics=topics, message=message)


def test_cursor_skips_pushed_lines_including_same_second() -> None:
    cursor = Cursor()
    cursor.advance([_entry("*1", _T0), _entry("*2", _T1)])
    later_same_second = _entry("*3", _T1)
    assert cursor.unpushed([_entry("*1", _T0), _entry("*2", _T1), later_same_second]) == [later_same_second]


def test_empty_cursor_pushes_everything() -> None:
    entries = [_entry("*1", _T0)]
    assert Cursor().unpushed(entries) == entries


def test_push_body_has_a_stream_per_topics_in_time_order() -> None:
    body = push_body(
        [_entry("*2", _T1, message="later"), _entry("*1", _T0, message="earlier"), _entry("*3", _T0, "system,error")],
        {"job": "home-switch"},
    )
    assert body == {
        "streams": [
            {
                "stream": {"job": "home-switch", "topics": "interface,info"},
                "values": [
                    [str(int(_T0.timestamp()) * 10**9), "earlier"],
                    [str(int(_T1.timestamp()) * 10**9), "later"],
                ],
            },
            {
                "stream": {"job": "home-switch", "topics": "system,error"},
                "values": [[str(int(_T0.timestamp()) * 10**9), "m"]],
            },
        ]
    }


class _FakeDevice(Connection):
    def __init__(self, log: list[dict[str, str]]) -> None:
        self.log = log

    async def command(self, path: str, attributes: dict[str, str] | None = None) -> list[dict[str, str]]:
        if path == "/system/clock/print":
            return [{"date": "2026-10-10", "time": "01:00:05", "gmt-offset": "-07:00"}]
        assert path == "/log/print"
        return self.log


_SETTINGS = Settings.model_validate(
    {
        "host": "device.test",
        "username": "reader",
        "password": "test-password",
        "ca_file": "/test/ca.crt",
        "loki_push_url": "http://loki.test/loki/api/v1/push",
        "labels": {"job": "test-device"},
    }
)


async def test_failed_push_leaves_lines_for_the_next_poll() -> None:
    pushes: list[dict[str, Any]] = []
    loki_up = False

    def loki(request: httpx.Request) -> httpx.Response:
        if not loki_up:
            return httpx.Response(503)
        pushes.append(json.loads(request.content))
        return httpx.Response(204)

    device = _FakeDevice([{".id": "*1", "time": "01:00:00", "topics": "interface,info", "message": "ether1 link down"}])
    cursor = Cursor()
    async with httpx.AsyncClient(transport=httpx.MockTransport(loki)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await poll_once(device, client, _SETTINGS, cursor)
        loki_up = True
        await poll_once(device, client, _SETTINGS, cursor)
        device.log.append({".id": "*2", "time": "01:00:04", "topics": "interface,info", "message": "ether1 link up"})
        await poll_once(device, client, _SETTINGS, cursor)

    assert [[line for stream in push["streams"] for _, line in stream["values"]] for push in pushes] == [
        ["ether1 link down"],
        ["ether1 link up"],
    ]


if __name__ == "__main__":
    pytest_bazel.main()
