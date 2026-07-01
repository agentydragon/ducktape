import pytest_bazel

from devinfra.claude.claude_api.usage import UsageResponse

if __name__ == "__main__":
    pytest_bazel.main()


def test_usage_response_spend() -> None:
    payload = {
        "spend": {
            "enabled": True,
            "limit": {"amount_minor": 250000, "currency": "USD", "exponent": 2},
            "percent": 4.94,
            "severity": "normal",
            "used": {"amount_minor": 12345, "currency": "USD", "exponent": 2},
        }
    }
    usage = UsageResponse.model_validate(payload)
    assert usage.spend is not None
    assert usage.spend.has_usage_totals
    assert usage.spend.limit is not None
    assert usage.spend.limit.major_units == 2500.0
    assert usage.spend.used is not None
    assert usage.spend.used.major_units == 123.45
    assert usage.spend.utilization_percent == 4.94


def test_usage_response_spend_derives_percent_when_missing() -> None:
    usage = UsageResponse.model_validate(
        {
            "spend": {
                "enabled": True,
                "limit": {"amount_minor": 250000, "currency": "USD", "exponent": 2},
                "used": {"amount_minor": 125000, "currency": "USD", "exponent": 2},
            }
        }
    )
    assert usage.spend is not None
    assert usage.spend.utilization_percent == 50.0


def test_usage_response_disabled_spend_has_no_usage_totals() -> None:
    usage = UsageResponse.model_validate({"spend": {"enabled": False, "disabled_reason": "payment_method_required"}})
    assert usage.spend is not None
    assert not usage.spend.has_usage_totals
