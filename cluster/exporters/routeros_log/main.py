"""Ships a RouterOS device's log to Loki, by polling `/log/print` over the API.

The device keeps its log in a memory ring, so polling backfills whatever happened while this
shipper or Loki was unreachable (a home WAN outage, a restart), as long as the ring still holds it.
Each poll pushes the lines past the last pushed one. After a restart that cursor is empty and the
whole ring is pushed again; Loki drops a line identical to one it holds (same stream, timestamp
and text).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from prometheus_client import Counter, Gauge, start_http_server

from cluster.exporters.routeros_log.api import Connection
from cluster.exporters.routeros_log.entries import LogEntry, parse_clock, parse_entry
from cluster.exporters.routeros_log.settings import Settings

logger = logging.getLogger(__name__)

_PUSHED = Counter("routeros_log_lines_pushed_total", "Log lines pushed to Loki.")
_POLLS = Counter("routeros_log_polls_total", "Polls of the device's log, by outcome.", ["outcome"])
_LAST_SUCCESS = Gauge("routeros_log_last_success_timestamp_seconds", "When a poll last pushed everything it read.")


@dataclass
class Cursor:
    """The newest pushed timestamp, and the ids pushed at exactly that second (RouterOS stamps to the
    second, so several lines share one)."""

    timestamp: datetime | None = None
    ids: set[str] = field(default_factory=set)

    def unpushed(self, entries: list[LogEntry]) -> list[LogEntry]:
        return [
            entry
            for entry in entries
            if self.timestamp is None
            or entry.timestamp > self.timestamp
            or (entry.timestamp == self.timestamp and entry.id not in self.ids)
        ]

    def advance(self, pushed: list[LogEntry]) -> None:
        if not pushed:
            return
        newest = max(entry.timestamp for entry in pushed)
        if self.timestamp is None or newest > self.timestamp:
            self.timestamp, self.ids = newest, set()
        self.ids |= {entry.id for entry in pushed if entry.timestamp == newest}


def push_body(entries: list[LogEntry], labels: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Loki's JSON push body: a stream per `topics`, lines in time order."""
    streams: defaultdict[str, list[LogEntry]] = defaultdict(list)
    for entry in sorted(entries, key=lambda entry: entry.timestamp):
        streams[entry.topics].append(entry)
    return {
        "streams": [
            {
                "stream": {**labels, "topics": topics},
                "values": [[str(int(entry.timestamp.timestamp()) * 10**9), entry.message] for entry in lines],
            }
            for topics, lines in streams.items()
        ]
    }


async def read_log(connection: Connection) -> list[LogEntry]:
    clock = parse_clock((await connection.command("/system/clock/print"))[0])
    return [parse_entry(reply, clock) for reply in await connection.command("/log/print")]


async def poll_once(connection: Connection, client: httpx.AsyncClient, settings: Settings, cursor: Cursor) -> None:
    entries = cursor.unpushed(await read_log(connection))
    if entries:
        response = await client.post(str(settings.loki_push_url), json=push_body(entries, settings.labels))
        response.raise_for_status()
        cursor.advance(entries)
        _PUSHED.inc(len(entries))
    _LAST_SUCCESS.set_to_current_time()


async def ship_forever(settings: Settings) -> None:
    ssl_context = ssl.create_default_context(cafile=settings.ca_file)
    cursor = Cursor()
    connection: Connection | None = None
    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        while True:
            started = time.monotonic()
            try:
                if connection is None:
                    connection = await Connection.open(
                        host=settings.host,
                        port=settings.port,
                        ssl_context=ssl_context,
                        username=settings.username,
                        password=settings.password.get_secret_value(),
                        timeout_seconds=settings.timeout_seconds,
                    )
                await poll_once(connection, client, settings, cursor)
                _POLLS.labels(outcome="success").inc()
            except Exception:
                # The device or Loki is unreachable; the device's ring keeps the lines until the next poll.
                logger.exception("poll failed")
                _POLLS.labels(outcome="failure").inc()
                if connection is not None:
                    connection.abort()
                    connection = None
            await asyncio.sleep(max(0, settings.poll_interval_seconds - (time.monotonic() - started)))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    start_http_server(settings.listen_port)
    asyncio.run(ship_forever(settings))


if __name__ == "__main__":
    main()
