import pytest
import pytest_bazel
from pydantic import ValidationError

from devinfra.claude.claude_api.usage import UsageResponse, normalized_extra_usage

if __name__ == "__main__":
    pytest_bazel.main()


def test_extra_usage_disabled_with_null_numbers_parses() -> None:
    payload = {
        "five_hour": None,
        "seven_day": None,
        "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None},
    }
    usage = UsageResponse.model_validate(payload)
    assert usage.extra_usage is not None
    assert usage.extra_usage.is_enabled is False
    assert normalized_extra_usage(usage) is None


def test_extra_usage_enabled_allows_null_utilization() -> None:
    payload = {
        "extra_usage": {"is_enabled": True, "monthly_limit": 250000, "used_credits": 125000, "utilization": None}
    }
    usage = UsageResponse.model_validate(payload)
    extra = normalized_extra_usage(usage)
    assert extra is not None
    assert extra.monthly_limit == 250000
    assert extra.used_credits == 125000
    assert extra.utilization == 50.0


def test_extra_usage_enabled_rejects_null_money() -> None:
    payload = {"extra_usage": {"is_enabled": True, "monthly_limit": None, "used_credits": None}}
    with pytest.raises(ValidationError):
        UsageResponse.model_validate(payload)


def test_spend_shape_drives_extra_usage() -> None:
    payload = {
        "extra_usage": {
            "currency": "USD",
            "daily": None,
            "decimal_places": 2,
            "disabled_reason": None,
            "is_enabled": True,
            "monthly_limit": 250000,
            "used_credits": 0.0,
            "utilization": None,
            "weekly": None,
        },
        "spend": {
            "enabled": True,
            "limit": {"amount_minor": 250000, "currency": "USD", "exponent": 2},
            "percent": 4.94,
            "severity": "normal",
            "used": {"amount_minor": 12345, "currency": "USD", "exponent": 2},
        },
    }
    usage = UsageResponse.model_validate(payload)
    extra = normalized_extra_usage(usage)
    assert extra is not None
    assert extra.monthly_limit == 250000
    assert extra.used_credits == 12345
    assert extra.utilization == 4.94
    assert extra.currency == "USD"
