"""Tests for Loki log fetching (query construction + response parsing)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

import pytest_bazel

from props.backend.loki import LOG_WINDOW_MARGIN, _logql_for_run, fetch_run_logs, parse_query_range, run_log_window

RUN = UUID("11da4746-40f8-431a-a0f2-c11f23c1c056")


def test_run_log_window_bounds_to_run_lifetime() -> None:
    created = datetime(2026, 6, 8, 5, 0, tzinfo=UTC)
    exited = datetime(2026, 6, 8, 5, 1, tzinfo=UTC)
    now = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
    # Finished run: window is created..exited (+margin), NOT out to `now` (which would make
    # a 4h window that Loki splits into hundreds of sub-queries).
    start, end = run_log_window(created_at=created, last_status_change=exited, is_in_progress=False, now=now)
    assert start == created - LOG_WINDOW_MARGIN
    assert end == exited + LOG_WINDOW_MARGIN


def test_run_log_window_in_progress_runs_to_now() -> None:
    created = datetime(2026, 6, 8, 5, 0, tzinfo=UTC)
    now = datetime(2026, 6, 8, 5, 30, tzinfo=UTC)
    _, end = run_log_window(created_at=created, last_status_change=created, is_in_progress=True, now=now)
    assert end == now + LOG_WINDOW_MARGIN


def test_run_log_window_treats_naive_as_utc() -> None:
    # SQLAlchemy may hand back tz-naive UTC datetimes; they must not crash timestamp math.
    created = datetime(2026, 6, 8, 5, 0)  # naive
    start, _ = run_log_window(
        created_at=created, last_status_change=created, is_in_progress=False, now=datetime.now(UTC)
    )
    assert start.tzinfo is UTC


def test_logql_matches_pod_by_run_id_prefix() -> None:
    # The pod name ends in the run-id's 8-char prefix; the matcher must be anchored.
    assert _logql_for_run(RUN) == '{namespace="props",pod=~".+-11da4746"}'


def test_parse_query_range_orders_chronologically() -> None:
    payload = {
        "data": {
            "result": [
                {"stream": {"pod": "critic-x-11da4746"}, "values": [["20", "second"], ["10", "first"]]},
                {"stream": {"pod": "critic-x-11da4746"}, "values": [["30", "third"]]},
            ]
        }
    }
    assert parse_query_range(payload) == "first\nsecond\nthird"


def test_parse_query_range_empty() -> None:
    assert parse_query_range({"data": {"result": []}}) == ""


async def test_fetch_run_logs_queries_and_parses() -> None:
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"data": {"result": [{"values": [["10", "hello"]]}]}}

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_: object) -> None: ...

        async def get(self, url: str, **kwargs: object) -> _Resp:
            captured["url"] = url
            captured["params"] = kwargs["params"]
            return _Resp()

    start = datetime(2026, 6, 8, 5, 0, tzinfo=UTC)
    end = datetime(2026, 6, 8, 5, 20, tzinfo=UTC)
    with patch("props.backend.loki.httpx.AsyncClient", return_value=_Client()):
        logs = await fetch_run_logs(RUN, start=start, end=end, base_url="http://loki:3100")

    assert logs == "hello"
    assert captured["url"] == "http://loki:3100/loki/api/v1/query_range"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["query"] == '{namespace="props",pod=~".+-11da4746"}'
    assert params["direction"] == "backward"
    # Window is bounded to the run's lifetime (nanosecond epoch), not a 14-day lookback.
    assert params["start"] == str(int(start.timestamp() * 1e9))
    assert params["end"] == str(int(end.timestamp() * 1e9))


if __name__ == "__main__":
    pytest_bazel.main()
