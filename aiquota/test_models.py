import pytest
import pytest_bazel
from pydantic import ValidationError

from aiquota.models import FetchSuccess, QuotaWindow

if __name__ == "__main__":
    pytest_bazel.main()


def test_fetch_success_sorts_windows_by_provider_duration() -> None:
    weekly = QuotaWindow(used_percent=6, reset_seconds=1, window_seconds=604800)
    session = QuotaWindow(used_percent=2, reset_seconds=1, window_seconds=18000)

    assert FetchSuccess(windows=[weekly, session]).windows == [session, weekly]


def test_fetch_success_rejects_duplicate_durations() -> None:
    windows = [
        QuotaWindow(used_percent=6, reset_seconds=1, window_seconds=604800),
        QuotaWindow(used_percent=2, reset_seconds=2, window_seconds=604800),
    ]

    with pytest.raises(ValidationError):
        FetchSuccess(windows=windows)


def test_reset_credit_expiry_lookup_failure_round_trips_as_explicit_state() -> None:
    failed = FetchSuccess(available_reset_credits=2, reset_credit_expiry_lookup_failed=True)

    assert FetchSuccess.model_validate_json(failed.model_dump_json()) == failed
    assert FetchSuccess(available_reset_credits=2).reset_credit_expiry_lookup_failed is False
