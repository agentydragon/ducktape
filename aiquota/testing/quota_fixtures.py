"""Shared aiquota render test fixtures, validated as production quota models."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from aiquota.models import AllQuotas
from aiquota.render.view_model import to_view

FIXTURE_NAMES = (
    "empty",
    "tints",
    "binding_summary",
    "hot",
    "exhausted",
    "extra_enabled_not_burning",
    "stale_fallback",
    "paid_credits",
    "claude_prepaid_balance",
    "claude_prepaid_balance_unavailable",
    "reset_credit_expiry_unavailable",
    "wire_fields",
)
DEFAULT_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def load_quota_fixture(path: Path) -> AllQuotas:
    return AllQuotas.model_validate(yaml.safe_load(path.read_text()))


def load_quota_view_fixture(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Return the same derived wire view used by `/v1/quotas` and the desktop daemon."""
    quotas = load_quota_fixture(path)
    return to_view(quotas, now if now is not None else quotas.fetched_at).model_dump(mode="json")
