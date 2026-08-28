import pytest
import pytest_bazel
from pydantic import ValidationError

from aiquota.models import FetchError, FetchSuccess, QuotaWindow

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

    with pytest.raises(ValidationError, match="quota window identities must be unique"):
        FetchSuccess(windows=windows)


def test_fetch_error_from_exception_uses_type_when_message_is_blank() -> None:
    err = FetchError.from_exception(TimeoutError(), "quota fetch")
    assert err.error == "quota fetch: TimeoutError"
