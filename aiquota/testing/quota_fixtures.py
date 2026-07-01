"""Shared aiquota render test fixtures."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from aiquota.models import (
    AllQuotas,
    ExtraSpend,
    FetchError,
    FetchSuccess,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    SuccessfulProviderFetch,
)

FIXTURE_NAMES = ("empty", "tints", "hot", "extra_enabled_not_burning", "stale_fallback")
PROVIDER_ORDER = ("claude", "codex", "zai")
DEFAULT_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def load_fixture_data(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at top level")
    return data


def load_quota_fixture(path: Path, *, now: datetime = DEFAULT_NOW) -> AllQuotas:
    return fixture_data_to_quotas(load_fixture_data(path), now=now)


def fixture_data_to_quotas(data: dict[str, Any], *, now: datetime = DEFAULT_NOW) -> AllQuotas:
    providers = [_provider_quota(provider, data[provider], now=now) for provider in PROVIDER_ORDER if provider in data]
    return AllQuotas(providers=providers, fetched_at=now)


def _provider_quota(provider: str, node: object, *, now: datetime) -> ProviderQuota:
    if not isinstance(node, dict):
        raise ValueError(f"{provider} fixture must be a mapping")
    return ProviderQuota(
        provider=provider,
        last_output=ProviderFetch(fetched_at=_last_check_at(node, now=now), result=_last_output_result(node)),
        last_success=_last_success(node.get("lastSuccess"), now=now),
    )


def _last_output_result(node: dict[str, Any]) -> FetchSuccess | FetchError:
    error = node.get("error")
    if error is not None:
        return FetchError(error=str(error))
    return FetchSuccess(
        short_window=_window(node.get("short")),
        long_window=_window(node.get("long")),
        extra_spend=_extra_spend(node.get("extraSpend")),
    )


def _last_success(node: object, *, now: datetime) -> SuccessfulProviderFetch | None:
    if node is None:
        return None
    if not isinstance(node, dict):
        raise ValueError("lastSuccess must be a mapping")
    age = node.get("ageSeconds")
    fetched_at = now - timedelta(seconds=float(age)) if age is not None else now
    return SuccessfulProviderFetch(
        fetched_at=fetched_at,
        result=FetchSuccess(
            short_window=_window(node.get("short")),
            long_window=_window(node.get("long")),
            extra_spend=_extra_spend(node.get("extraSpend")),
        ),
    )


def _last_check_at(node: dict[str, Any], *, now: datetime) -> datetime:
    age = node.get("lastCheckAgeSeconds")
    if age is not None:
        return now - timedelta(seconds=float(age))
    return now


def _window(node: object) -> QuotaWindow | None:
    if node is None:
        return None
    if not isinstance(node, dict):
        raise ValueError("window fixture must be a mapping")
    return QuotaWindow(
        used_percent=float(node["usedPercent"]),
        reset_seconds=float(node["resetSeconds"]),
        window_seconds=float(node["windowSeconds"]),
    )


def _extra_spend(node: object) -> ExtraSpend | None:
    if node is None:
        return None
    if not isinstance(node, dict):
        raise ValueError("extraSpend fixture must be a mapping")
    return ExtraSpend(
        is_enabled=bool(node["is_enabled"]),
        monthly_limit_usd=float(node["monthly_limit_usd"]),
        used_usd=float(node["used_usd"]),
        utilization=float(node["utilization"]),
    )
