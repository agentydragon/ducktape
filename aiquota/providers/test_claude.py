from pathlib import Path

import pytest_bazel

from aiquota.providers.claude import _spend_to_extra_spend, _to_success
from devinfra.claude.claude_api.usage import UsageResponse

if __name__ == "__main__":
    pytest_bazel.main()


def test_spend_shape_drives_extra_spend() -> None:
    usage = UsageResponse.model_validate(
        {
            "spend": {
                "enabled": True,
                "limit": {"amount_minor": 250000, "currency": "USD", "exponent": 2},
                "used": {"amount_minor": 12345, "currency": "USD", "exponent": 2},
                "percent": 4.94,
                "severity": "normal",
            }
        }
    )
    extra = _spend_to_extra_spend(usage.spend)
    assert extra is not None
    assert extra.monthly_limit_usd == 2500.0
    assert extra.used_usd == 123.45
    assert extra.utilization == 4.94


def test_disabled_spend_does_not_render_extra_spend() -> None:
    usage = UsageResponse.model_validate({"spend": {"enabled": False, "disabled_reason": "payment_method_required"}})
    assert _spend_to_extra_spend(usage.spend) is None


def test_raw_usage_fixture_preserves_provider_windows() -> None:
    usage = UsageResponse.model_validate_json((Path(__file__).parent / "fixtures" / "claude_usage.json").read_text())

    result = _to_success(usage)

    assert [(window.window_seconds, window.used_percent) for window in result.windows] == [
        (5 * 3600, 73),
        (7 * 86400, 75),
    ]
