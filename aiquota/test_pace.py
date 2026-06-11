import pytest_bazel

from aiquota.models import QuotaWindow
from aiquota.pace import binding_tint, compute_pace, tint_for

if __name__ == "__main__":
    pytest_bazel.main()


def test_compute_pace_on_track() -> None:
    w = QuotaWindow(used_percent=50.0, reset_seconds=9000.0, window_seconds=18000.0)
    pace = compute_pace(w)
    assert pace is not None
    assert pace.deviation == 0.0
    assert pace.stable


def test_compute_pace_ahead() -> None:
    w = QuotaWindow(used_percent=75.0, reset_seconds=9000.0, window_seconds=18000.0)
    pace = compute_pace(w)
    assert pace is not None
    assert pace.deviation == 25.0
    assert pace.projected_at_reset is not None
    assert pace.projected_at_reset > 100


def test_compute_pace_zero_window() -> None:
    w = QuotaWindow(used_percent=50.0, reset_seconds=0.0, window_seconds=0.0)
    assert compute_pace(w) is None


def test_compute_pace_early_unstable() -> None:
    w = QuotaWindow(used_percent=5.0, reset_seconds=17900.0, window_seconds=18000.0)
    pace = compute_pace(w)
    assert pace is not None
    assert not pace.stable


def test_tint_for_hot_short() -> None:
    pace = compute_pace(QuotaWindow(used_percent=90.0, reset_seconds=9000.0, window_seconds=18000.0))
    assert tint_for(pace, 90.0, is_short=True) == "hot"


def test_tint_for_warn() -> None:
    pace = compute_pace(QuotaWindow(used_percent=60.0, reset_seconds=9000.0, window_seconds=18000.0))
    assert tint_for(pace, 60.0, is_short=False) == "warn"


def test_tint_for_cool() -> None:
    pace = compute_pace(QuotaWindow(used_percent=20.0, reset_seconds=9000.0, window_seconds=18000.0))
    assert tint_for(pace, 20.0, is_short=False) == "cool"


def test_tint_for_unknown() -> None:
    assert tint_for(None, None, is_short=False) == "unknown"


def test_binding_tint_short_hot_wins() -> None:
    assert binding_tint("hot", "ok") == "hot"
    assert binding_tint("hot", "cool") == "hot"


def test_binding_tint_takes_worse() -> None:
    assert binding_tint("warn", "ok") == "warn"
    assert binding_tint("ok", "warn") == "warn"
