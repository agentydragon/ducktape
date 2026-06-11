import pytest
import pytest_bazel
from pydantic import ValidationError

from aiquota.providers.claude import _ExtraUsageDisabled, _ExtraUsageEnabled, _UsageResponse

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
    usage = _UsageResponse.model_validate(payload)
    assert isinstance(usage.extra_usage, _ExtraUsageDisabled)


def test_extra_usage_enabled_requires_numbers() -> None:
    payload = {
        "extra_usage": {"is_enabled": True, "monthly_limit": 4600.0, "used_credits": 2324.85, "utilization": 50.54}
    }
    usage = _UsageResponse.model_validate(payload)
    assert isinstance(usage.extra_usage, _ExtraUsageEnabled)
    assert usage.extra_usage.monthly_limit == 4600.0


def test_extra_usage_enabled_rejects_null_numbers() -> None:
    # If the API ever sends `is_enabled=True` with null numbers we want a
    # loud failure, not a silent fallback to 0.
    payload = {"extra_usage": {"is_enabled": True, "monthly_limit": None, "used_credits": None, "utilization": None}}
    with pytest.raises(ValidationError):
        _UsageResponse.model_validate(payload)
