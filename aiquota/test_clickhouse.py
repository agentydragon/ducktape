import base64
import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import pytest_bazel

from aiquota.api import (
    BackgroundCollector,
    CollectorMetrics,
    HistoryCollector,
    HistorySnapshot,
    QuotaSnapshot,
    RawUpstreamResponse,
)
from aiquota.clickhouse import ClickHouseSnapshotSink
from aiquota.models import (
    AllQuotas,
    FetchSuccess,
    HistoryObservation,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    ResetCredit,
    ResetCreditsObservation,
    TokenActivityDay,
    TokenActivityObservation,
)

if __name__ == "__main__":
    pytest_bazel.main()

pytestmark = pytest.mark.asyncio


def _snapshot() -> QuotaSnapshot:
    observed_at = datetime(2026, 8, 22, 1, 0, tzinfo=UTC)
    raw_bytes = b'{"five_hour":{"utilization":45.0}}\n'
    return QuotaSnapshot(
        quotas=AllQuotas(
            fetched_at=observed_at,
            providers=[
                ProviderQuota(
                    provider="claude",
                    last_output=ProviderFetch(
                        fetched_at=observed_at,
                        result=FetchSuccess(
                            windows=[
                                QuotaWindow(
                                    used_percent=45.0,
                                    reset_seconds=3600,
                                    window_seconds=5 * 3600,
                                    reset_at=observed_at + timedelta(hours=1),
                                )
                            ]
                        ),
                    ),
                )
            ],
        ),
        raw_responses={
            "claude": RawUpstreamResponse(
                status_code=200,
                content_type="application/json",
                body={"five_hour": {"utilization": 45.0}},
                body_base64=base64.b64encode(raw_bytes).decode(),
                body_sha256="82c2c21a2c01aff1604fa70e5efc172054c0aafa9f53c9387c20dc133789ae09",
                body_size_bytes=len(raw_bytes),
            )
        },
    )


async def test_clickhouse_sink_batches_raw_and_typed_rows() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    sink = ClickHouseSnapshotSink(
        url="http://clickhouse:8123",
        username="aiquota_ingest",
        password="secret",
        transport=httpx.MockTransport(handler),
    )

    assert await sink.write(_snapshot()) == 2
    assert len(requests) == 1
    raw_query = requests[0].url.params["query"]
    assert raw_query == "INSERT INTO aiquota.raw_http_observations FORMAT JSONEachRow"
    assert requests[0].url.params["async_insert_deduplicate"] == "1"
    assert requests[0].url.params["insert_deduplication_token"]
    raw_row = json.loads(requests[0].content)
    assert base64.b64decode(raw_row["raw_body_base64"]) == b'{"five_hour":{"utilization":45.0}}\n'
    assert raw_row["raw_body_size_bytes"] == 35
    assert raw_row["normalized_body"]
    assert raw_row["quota_windows"] == [
        {
            "window_name": "",
            "used_percent": 45.0,
            "remaining_percent": 55.0,
            "reset_at": "2026-08-22T02:00:00+00:00",
            "reset_seconds": 3600,
            "window_seconds": 18000,
            "extra_spend_enabled": None,
            "extra_spend_limit_usd": None,
            "extra_spend_used_usd": None,
            "extra_spend_utilization": None,
        }
    ]


async def test_background_collector_forces_refresh_and_records_success() -> None:
    class Fetcher:
        force_refresh: bool | None = None

        async def fetch(self, force_refresh: bool = False) -> QuotaSnapshot:
            self.force_refresh = force_refresh
            return _snapshot()

    class Sink:
        snapshot: QuotaSnapshot | None = None

        async def write(self, snapshot: QuotaSnapshot) -> int:
            self.snapshot = snapshot
            return 2

    fetcher = Fetcher()
    sink = Sink()
    metrics = CollectorMetrics()
    collector = BackgroundCollector(fetcher, sink, interval=timedelta(minutes=5), metrics=metrics)

    await collector.poll_once()

    assert fetcher.force_refresh is True
    assert sink.snapshot is not None
    assert collector.has_persisted is True
    rendered = metrics.registry.collect()
    assert any(metric.name == "aiquota_collector_ready" for metric in rendered)


_PROFILE_BYTES = b'{"stats":{"daily_usage_buckets":[{"start_date":"2026-08-21","tokens":12}]}}'


def _history_snapshot() -> HistorySnapshot:
    observed_at = datetime(2026, 8, 22, 1, 0, tzinfo=UTC)
    return HistorySnapshot(
        fetched_at=observed_at,
        observations=[
            HistoryObservation(
                provider="codex",
                observed_at=observed_at,
                payload=TokenActivityObservation(days=[TokenActivityDay(start_date=date(2026, 8, 21), tokens=12)]),
            ),
            HistoryObservation(
                provider="codex",
                observed_at=observed_at,
                payload=ResetCreditsObservation(
                    credits=[
                        ResetCredit(
                            credit_id="credit_a",
                            reset_type="five_hour",
                            status="consumed",
                            granted_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
                        )
                    ]
                ),
            ),
        ],
        raw_responses={
            "codex_token_activity": RawUpstreamResponse(
                status_code=200,
                content_type="application/json",
                body=json.loads(_PROFILE_BYTES),
                body_base64=base64.b64encode(_PROFILE_BYTES).decode(),
                body_sha256=hashlib.sha256(_PROFILE_BYTES).hexdigest(),
                body_size_bytes=len(_PROFILE_BYTES),
            )
        },
    )


def _mock_sink(requests: list[httpx.Request]) -> ClickHouseSnapshotSink:
    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    return ClickHouseSnapshotSink(
        url="http://clickhouse:8123",
        username="aiquota_ingest",
        password="secret",
        transport=httpx.MockTransport(handler),
    )


async def test_history_rows_carry_typed_payloads_and_their_own_raw_body() -> None:
    requests: list[httpx.Request] = []

    assert await _mock_sink(requests).write_history(_history_snapshot()) == 2

    assert len(requests) == 1
    activity, credits = (json.loads(line) for line in requests[0].content.splitlines())
    assert activity["source"] == "codex"
    assert activity["quota_windows"] == []
    assert activity["token_activity"] == [{"start_date": "2026-08-21", "tokens": 12}]
    assert activity["reset_credits"] == []
    # Each endpoint's body is looked up under its own capture key, so the
    # profile response is not overwritten by the reset-credit response.
    assert base64.b64decode(activity["raw_body_base64"]) == _PROFILE_BYTES
    assert credits["token_activity"] == []
    assert credits["reset_credits"] == [
        {
            "credit_id": "credit_a",
            "reset_type": "five_hour",
            "status": "consumed",
            "granted_at": "2026-08-20T09:00:00+00:00",
            "expires_at": None,
        }
    ]
    assert activity["event_id"] != credits["event_id"]


async def test_history_write_without_observations_makes_no_request() -> None:
    requests: list[httpx.Request] = []
    empty = HistorySnapshot(observations=[], fetched_at=datetime(2026, 8, 22, 1, 0, tzinfo=UTC), raw_responses={})

    assert await _mock_sink(requests).write_history(empty) == 0
    assert requests == []


async def test_history_collector_records_failures_without_raising() -> None:
    class Fetcher:
        async def fetch_history(self) -> HistorySnapshot:
            raise httpx.ConnectError("clickhouse down")

    class Sink:
        async def write_history(self, snapshot: HistorySnapshot) -> int:
            raise AssertionError("must not write after a failed fetch")

    metrics = CollectorMetrics()
    await HistoryCollector(Fetcher(), Sink(), interval=timedelta(hours=1), metrics=metrics).poll_once()

    samples = {
        (sample.name, sample.labels.get("result")): sample.value
        for metric in metrics.registry.collect()
        for sample in metric.samples
    }
    assert samples[("aiquota_history_poll_total", "error")] == 1
    assert ("aiquota_history_poll_total", "success") not in samples
