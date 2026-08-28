"""ClickHouse persistence for historical aiquota observations."""

import base64
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol

import httpx

from aiquota.models import (
    AllQuotas,
    FetchError,
    FetchSuccess,
    HistoryObservation,
    ProviderQuota,
    ResetCreditsObservation,
    TokenActivityObservation,
    history_capture_key,
)

_DATASET = "aiquota"


class RawUpstreamResponse(Protocol):
    status_code: int
    content_type: str | None
    body: object | None
    body_base64: str | None
    body_sha256: str | None
    body_size_bytes: int | None
    truncated: bool


class QuotaSnapshot(Protocol):
    @property
    def quotas(self) -> AllQuotas: ...

    @property
    def raw_responses(self) -> Mapping[str, RawUpstreamResponse]: ...


class HistorySnapshot(Protocol):
    @property
    def observations(self) -> Sequence[HistoryObservation]: ...

    @property
    def fetched_at(self) -> datetime: ...

    @property
    def raw_responses(self) -> Mapping[str, RawUpstreamResponse]: ...


class ClickHouseSnapshotSink:
    """Append raw provider responses and normalized quota windows via HTTP."""

    def __init__(
        self,
        *,
        url: str,
        username: str,
        password: str,
        database: str = "aiquota",
        raw_table: str = "raw_http_observations",
        windows_table: str = "aiquota_windows",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._database = database
        self._raw_table = raw_table
        self._windows_table = windows_table
        self._timeout = timeout
        self._transport = transport

    async def write(self, snapshot: QuotaSnapshot) -> int:
        raw_rows = [_raw_row(snapshot, provider.provider) for provider in snapshot.quotas.providers]
        window_count = sum(_window_count(provider) for provider in snapshot.quotas.providers)
        async with httpx.AsyncClient(
            auth=(self._username, self._password), timeout=self._timeout, transport=self._transport
        ) as client:
            await self._insert(client, self._raw_table, raw_rows)
        return len(raw_rows) + window_count

    async def write_history(self, snapshot: HistorySnapshot) -> int:
        """Append history observations to the same raw table the quota poll writes.

        History rows carry no quota windows; the typed projections read the
        `token_activity` / `reset_credits` columns instead.
        """

        rows = [_history_row(snapshot, observation) for observation in snapshot.observations]
        if not rows:
            return 0
        async with httpx.AsyncClient(
            auth=(self._username, self._password), timeout=self._timeout, transport=self._transport
        ) as client:
            await self._insert(client, self._raw_table, rows)
        return len(rows)

    async def _insert(self, client: httpx.AsyncClient, table: str, rows: list[dict[str, object]]) -> None:
        body = "".join(f"{json.dumps(row, separators=(',', ':'))}\n" for row in rows)
        deduplication_token = hashlib.sha256(f"{self._database}.{table}:".encode() + body.encode()).hexdigest()
        response = await client.post(
            self._url,
            params={
                "query": f"INSERT INTO {self._database}.{table} FORMAT JSONEachRow",
                # Periodic collectors produce tiny batches. Let ClickHouse coalesce
                # them rather than creating one MergeTree part per HTTP request.
                "async_insert": "1",
                "wait_for_async_insert": "1",
                "async_insert_busy_timeout_ms": "5000",
                "async_insert_deduplicate": "1",
                "insert_deduplication_token": deduplication_token,
                "date_time_input_format": "best_effort",
            },
            content=body.encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        response.raise_for_status()


def _raw_row(snapshot: QuotaSnapshot, provider_name: str) -> dict[str, object]:
    provider = next(provider for provider in snapshot.quotas.providers if provider.provider == provider_name)
    raw = snapshot.raw_responses.get(provider_name)
    raw_bytes, raw_sha256, body_size, truncated = _raw_bytes(raw)
    normalized = provider.model_dump_json()
    quota_windows = _window_values(provider)
    error = provider.last_output.result.error if isinstance(provider.last_output.result, FetchError) else ""
    event_key = f"{provider_name}:{provider.last_output.fetched_at.isoformat()}:{raw_sha256}"
    return {
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"aiquota:{event_key}")),
        "schema_version": 1,
        "dataset": _DATASET,
        "source": provider_name,
        "observed_at": provider.last_output.fetched_at.isoformat(),
        # Keep retries byte-for-byte stable so ReplicatedMergeTree insert
        # deduplication can discard a replay after a partial two-table write.
        "ingested_at": snapshot.quotas.fetched_at.astimezone(UTC).isoformat(),
        "status_code": raw.status_code if raw else 0,
        "content_type": (raw.content_type or "") if raw else "",
        "raw_body_base64": base64.b64encode(raw_bytes).decode(),
        "raw_body_sha256": raw_sha256,
        "raw_body_size_bytes": body_size,
        "raw_body_truncated": truncated,
        "quota_windows": quota_windows,
        "normalized_body": normalized,
        "error": error,
    }


def _history_row(snapshot: HistorySnapshot, observation: HistoryObservation) -> dict[str, object]:
    payload = observation.payload
    raw = snapshot.raw_responses.get(history_capture_key(observation.provider, payload.kind))
    raw_bytes, raw_sha256, body_size, truncated = _raw_bytes(raw)
    match payload:
        case TokenActivityObservation():
            token_activity = [{"start_date": day.start_date.isoformat(), "tokens": day.tokens} for day in payload.days]
            reset_credits: list[dict[str, object]] = []
        case ResetCreditsObservation():
            token_activity = []
            reset_credits = [
                {
                    "credit_id": credit.credit_id,
                    "reset_type": credit.reset_type,
                    "status": credit.status,
                    "granted_at": credit.granted_at.isoformat(),
                    "expires_at": credit.expires_at.isoformat() if credit.expires_at else None,
                }
                for credit in payload.credits
            ]
    event_key = f"{observation.provider}:{payload.kind}:{observation.observed_at.isoformat()}:{raw_sha256}"
    return {
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"aiquota:{event_key}")),
        "schema_version": 1,
        "dataset": _DATASET,
        "source": observation.provider,
        "observed_at": observation.observed_at.isoformat(),
        "ingested_at": snapshot.fetched_at.astimezone(UTC).isoformat(),
        "status_code": raw.status_code if raw else 0,
        "content_type": (raw.content_type or "") if raw else "",
        "raw_body_base64": base64.b64encode(raw_bytes).decode(),
        "raw_body_sha256": raw_sha256,
        "raw_body_size_bytes": body_size,
        "raw_body_truncated": truncated,
        "quota_windows": [],
        "token_activity": token_activity,
        "reset_credits": reset_credits,
        "normalized_body": observation.model_dump_json(),
        "error": "",
    }


def _window_values(provider: ProviderQuota) -> list[dict[str, object]]:
    result = provider.last_output.result
    if not isinstance(result, FetchSuccess):
        return []
    extra = result.extra_spend
    return [
        {
            "window_name": window.name or "",
            "used_percent": window.used_percent,
            "remaining_percent": max(0.0, 100.0 - window.used_percent),
            "reset_at": window.reset_at.isoformat() if window.reset_at else None,
            "reset_seconds": window.reset_seconds,
            "window_seconds": window.window_seconds,
            "extra_spend_enabled": extra.is_enabled if extra else None,
            "extra_spend_limit_usd": extra.monthly_limit_usd if extra else None,
            "extra_spend_used_usd": extra.used_usd if extra else None,
            "extra_spend_utilization": extra.utilization if extra else None,
        }
        for window in result.windows
    ]


def _window_count(provider: ProviderQuota) -> int:
    result = provider.last_output.result
    return len(result.windows) if isinstance(result, FetchSuccess) else 0


def _raw_bytes(raw: RawUpstreamResponse | None) -> tuple[bytes, str, int, bool]:
    if raw is None:
        content = b""
        return content, hashlib.sha256(content).hexdigest(), 0, False
    if raw.body_base64 is not None:
        content = base64.b64decode(raw.body_base64)
        return (
            content,
            raw.body_sha256 or hashlib.sha256(content).hexdigest(),
            raw.body_size_bytes if raw.body_size_bytes is not None else len(content),
            raw.truncated,
        )
    content = raw.body.encode() if isinstance(raw.body, str) else json.dumps(raw.body, separators=(",", ":")).encode()
    return content, hashlib.sha256(content).hexdigest(), len(content), raw.truncated
