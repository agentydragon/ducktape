import pytest
import pytest_bazel
from pydantic import ValidationError

from aiquota.providers.claude import _to_aiquota_extra_usage
from devinfra.claude.claude_api.usage import UsageResponse, normalized_extra_usage

if __name__ == "__main__":
    pytest_bazel.main()


def test_extra_usage_disabled_with_null_numbers_parses() -> None:
    # The OAuth usage API returns explicit `null` for the numeric fields
    # when extra-usage isn't entitled on the plan. Parsing must succeed
    # (the disabled variant carries no numbers) — see the regression
    # report where this raised three `float_type` ValidationErrors.
    payload = {
        "five_hour": None,
        "seven_day": None,
        "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None},
    }
    usage = UsageResponse.model_validate(payload)
    assert usage.extra_usage is not None
    assert usage.extra_usage.is_enabled is False


def test_extra_usage_enabled_requires_numbers() -> None:
    payload = {
        "extra_usage": {"is_enabled": True, "monthly_limit": 4600.0, "used_credits": 2324.85, "utilization": 50.54}
    }
    usage = UsageResponse.model_validate(payload)
    assert usage.extra_usage is not None
    assert usage.extra_usage.monthly_limit == 4600.0


def test_extra_usage_enabled_allows_null_utilization() -> None:
    payload = {
        "extra_usage": {"is_enabled": True, "monthly_limit": 250000, "used_credits": 125000, "utilization": None}
    }
    usage = UsageResponse.model_validate(payload)
    extra = _to_aiquota_extra_usage(normalized_extra_usage(usage))
    assert extra is not None
    assert extra.monthly_limit_usd == 2500.0
    assert extra.used_usd == 1250.0
    assert extra.utilization == 50.0


def test_extra_usage_enabled_rejects_null_money() -> None:
    payload = {"extra_usage": {"is_enabled": True, "monthly_limit": None, "used_credits": None}}
    with pytest.raises(ValidationError):
        UsageResponse.model_validate(payload)


def test_spend_shape_drives_extra_usage() -> None:
    payload = {
        "extra_usage": {"is_enabled": True, "monthly_limit": 250000, "used_credits": 0.0, "utilization": None},
        "spend": {
            "enabled": True,
            "limit": {"amount_minor": 250000, "currency": "USD", "exponent": 2},
            "used": {"amount_minor": 12345, "currency": "USD", "exponent": 2},
            "percent": 4.94,
            "severity": "normal",
        },
    }
    usage = UsageResponse.model_validate(payload)
    extra = _to_aiquota_extra_usage(normalized_extra_usage(usage))
    assert extra is not None
    assert extra.monthly_limit_usd == 2500.0
    assert extra.used_usd == 123.45
    assert extra.utilization == 4.94
