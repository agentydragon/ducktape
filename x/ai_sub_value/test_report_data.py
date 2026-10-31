from datetime import UTC, datetime, timedelta

import pytest_bazel

from x.ai_sub_value.report_data import assemble_usage, quota_weekly


def usage(source: str, tokens: int | None) -> dict:
    return {
        "bucket_start_utc": "2026-01-05T00:00:00Z",
        "provider": "anthropic",
        "model": "fake-model",
        "source": source,
        "payment_lane": "unknown",
        "requests": 1,
        "uncached_input_tokens": 10,
        "cache_read_input_tokens": 80,
        "cache_write_input_tokens": 5,
        "output_tokens": 5,
        "reasoning_output_tokens": 3,
        "total_tokens": tokens,
    }


def test_message_total_does_not_add_reasoning_twice() -> None:
    result = assemble_usage({"native": [usage("claude_message", None)]})
    assert result["rows"][0]["total_tokens"] == 100
    assert result["rows"][0]["reasoning_output_tokens"] == 3


def test_unresolved_cross_source_overlap_has_a_captured_union_range() -> None:
    result = assemble_usage({"native": [usage("native", 100)], "langfuse": [usage("langfuse", 70)]})
    combined = result["combined_rows"][0]
    assert combined["captured_total_lower"] == 100
    assert combined["captured_total_upper"] == 170


def test_missing_total_leaves_the_captured_upper_bound_unknown() -> None:
    result = assemble_usage({"native": [usage("native", 100)], "langfuse": [usage("langfuse", None)]})
    combined = result["combined_rows"][0]
    assert combined["captured_total_lower"] == 100
    assert combined["captured_total_upper"] is None
    assert combined["missing_total_rows"] == 1


def test_incomplete_source_group_does_not_present_a_known_subtotal_as_complete() -> None:
    result = assemble_usage({"native": [usage("native", 100), usage("native", None)]})
    assert result["rows"][0]["total_tokens"] is None
    assert result["rows"][0]["known_token_subtotals"]["total_tokens"] == 100
    assert result["combined_rows"][0]["captured_total_lower"] == 100
    assert result["combined_rows"][0]["captured_total_upper"] is None


def test_weekly_quota_uses_every_poll_and_excludes_reset_and_gap_pairs() -> None:
    start = datetime(2026, 1, 5, tzinfo=UTC)
    reset = start + timedelta(days=7)
    rows = [
        {
            "event_id": str(i),
            "provider": "fake-provider",
            "window_seconds": 604800,
            "window_name": "",
            "observed_at": (start + timedelta(minutes=minute)).isoformat(),
            "used_percent": used,
            "reset_at": reset.isoformat(),
        }
        for i, (minute, used) in enumerate([(0, 20), (5, 30), (10, 0), (15, 4), (45, 9)])
    ]
    result = quota_weekly([*rows, rows[0]])[0]
    assert result["observed_increase_pp"] == 14
    assert result["restored_headroom_pp"] == 30
    assert result["observed_drops"] == 1
    assert result["broken_pairs"] == 2


if __name__ == "__main__":
    pytest_bazel.main()
