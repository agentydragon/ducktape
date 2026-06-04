"""Tests for Loki log fetching (query construction + response parsing)."""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID

import pytest_bazel

from props.backend.loki import _logql_for_run, fetch_run_logs, parse_query_range

RUN = UUID("11da4746-40f8-431a-a0f2-c11f23c1c056")


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

    with patch("props.backend.loki.httpx.AsyncClient", return_value=_Client()):
        logs = await fetch_run_logs(RUN, base_url="http://loki:3100")

    assert logs == "hello"
    assert captured["url"] == "http://loki:3100/loki/api/v1/query_range"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["query"] == '{namespace="props",pod=~".+-11da4746"}'
    assert params["direction"] == "backward"


if __name__ == "__main__":
    pytest_bazel.main()
