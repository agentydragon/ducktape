import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy
import pytest
import pytest_bazel

from x.ai_sub_value.analyze import Poll
from x.ai_sub_value.bootstrap import (
    BlockMethod,
    calendar_day_blocks,
    compare_blocks,
    lag_correlations,
    resampling_units,
)
from x.ai_sub_value.estimate_capacity import (
    CalibrationRow,
    MoneyPoll,
    QuotaInterval,
    TimeRange,
    UsageBin,
    aggregation_sensitivity,
    align_usage,
    build_estimates,
    calibration_intervals,
    covers_range,
    fit_rows,
    load_bins,
    overlap_bins,
    select_monotonic_runs,
    source_consistency,
    split_monotonic_runs,
    stable_money_ranges,
)
from x.ai_sub_value.plan_history import CodexPlanPeriod, read_codex_plan_history

START = datetime(2026, 1, 1, tzinfo=UTC)


def test_reset_only_discards_the_crossing_poll_pair_and_keeps_both_sides() -> None:
    reset = START + timedelta(days=7)
    polls = [Poll(START + timedelta(minutes=5 * i), i if i < 5 else i - 5, reset) for i in range(13)]
    runs, rejected = split_monotonic_runs(polls)
    assert [(run.polls[0].at, run.polls[-1].at) for run in runs] == [
        (START, START + timedelta(minutes=20)),
        (START + timedelta(minutes=25), START + timedelta(hours=1)),
    ]
    intervals = calibration_intervals(runs, chunk_minutes=60)
    assert sum((row.end - row.start).total_seconds() for row in intervals) == 55 * 60
    assert sum(row.points for row in intervals) == 11
    assert rejected == {"reset_or_discontinuity": 1}


def test_poll_aligned_chunks_keep_short_tail_without_clock_hour_rounding() -> None:
    polls = [Poll(START + timedelta(minutes=7 + 5 * i), i, None) for i in range(28)]
    runs, rejected = split_monotonic_runs(polls)
    pieces = calibration_intervals(runs, chunk_minutes=60)
    assert [(item.start.minute, item.end.minute) for item in pieces] == [(7, 7), (7, 7), (7, 22)]
    assert sum(item.points for item in pieces) == 27
    assert sum((item.end - item.start).total_seconds() for item in pieces) == 135 * 60
    whole = calibration_intervals(runs, chunk_minutes=None)
    assert len(whole) == 1
    assert whole[0].points == 27
    assert rejected == {}


def test_usage_alignment_exposes_the_metering_lag_assumption() -> None:
    interval = QuotaInterval(START, START + timedelta(hours=1), 5, START + timedelta(days=7))
    bins = [UsageBin(START, "openai", "fake-model", 100, "native", "unknown")]
    assert align_usage([interval], bins, lag_minutes=0)[0].tokens_by_model == {"fake-model": 100}
    assert align_usage([interval], bins, lag_minutes=60)[0].tokens_by_model == {}


def test_short_span_crossing_clock_hour_remains_usable() -> None:
    polls = [Poll(START + timedelta(minutes=59), 5, None), Poll(START + timedelta(hours=1), 6, None)]
    runs, rejected = split_monotonic_runs(polls)
    intervals = calibration_intervals(runs, chunk_minutes=60)
    assert len(intervals) == 1
    assert intervals[0].points == 1
    assert rejected == {}


def test_aggregation_choices_preserve_observed_time_points_and_token_totals() -> None:
    polls = [Poll(START + timedelta(minutes=7 + 5 * i), i, None) for i in range(28)]
    runs, _ = split_monotonic_runs(polls)
    bins = [
        UsageBin(START + timedelta(minutes=5 * i), "openai", "fake-model", 1000, "native", "unknown") for i in range(30)
    ]
    fits = aggregation_sensitivity(runs, bins)
    assert [row["intervals"] for row in fits] == [5, 3, 2, 1]
    assert all(row["observed_points"] == 27 for row in fits)
    assert all(row["observed_hours"] == 2.25 for row in fits)
    assert all(row["recorded_tokens"] == pytest.approx(27000) for row in fits)


def test_companion_reset_splits_only_affected_poll_spans() -> None:
    polls = [Poll(START + timedelta(minutes=5 * i), i, None) for i in range(13)]
    other = [Poll(poll.at, poll.used if i < 6 else poll.used - 6, None) for i, poll in enumerate(polls)]
    money = [MoneyPoll(START + timedelta(minutes=5 * i), 0) for i in range(-4, 17)]
    runs, rejected = select_monotonic_runs(
        polls, companions=[other], money=money, usage_start=START, usage_end=START + timedelta(hours=1)
    )
    assert [len(run.polls) - 1 for run in runs] == [5, 6]
    assert rejected == {"other_window_missing_unclean_or_saturated": 1}


def test_gaps_saturation_and_changed_reset_metadata_break_runs() -> None:
    polls = [Poll(START + timedelta(minutes=5 * i), i, START + timedelta(days=7)) for i in range(8)]
    polls[3] = Poll(polls[3].at, 100, polls[3].reset)
    polls[5:] = [Poll(p.at + timedelta(hours=1), p.used, START + timedelta(days=8)) for p in polls[5:]]
    runs, rejected = split_monotonic_runs(polls)
    assert [len(run.polls) - 1 for run in runs] == [2, 2]
    assert rejected == {"saturated": 1, "reset_or_discontinuity": 1, "poll_gap": 1}
    moved_reset = [
        Poll(START, 1, START + timedelta(days=7)),
        Poll(START + timedelta(minutes=5), 2, START + timedelta(days=8)),
    ]
    assert split_monotonic_runs(moved_reset)[1] == {"reset_or_discontinuity": 1}


def test_source_overlap_maxes_families_after_summing_native_streams() -> None:
    bins = [
        UsageBin(START, "openai", "gpt-a", amount, source, "unknown")
        for amount, source in ((10, "codex_direct_response"), (4, "codex_validated_counter_delta"), (12, "langfuse"))
    ]
    assert sum(row.tokens for row in overlap_bins(bins, scenario="sum_sources")) == 26
    assert sum(row.tokens for row in overlap_bins(bins, scenario="max_source_family")) == 14


def test_bootstrap_blocks_never_bridge_missing_calendar_days() -> None:
    days = ["2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05"]
    blocks = calendar_day_blocks(days)
    assert ["2026-01-02", "2026-01-04"] not in blocks
    assert ["2026-01-01", "2026-01-02"] in blocks
    assert ["2026-01-04", "2026-01-05"] in blocks


def test_explicit_api_lane_excluded_and_canonical_model_used(tmp_path: Path) -> None:
    def row(lane: str, model: str | None, canonical: str | None = None) -> dict:
        return {
            "provider": "anthropic",
            "payment_lane": lane,
            "model": model,
            "canonical_model": canonical,
            "bucket_start_utc": START.isoformat(),
            "source": "langfuse",
            "total_tokens": None,
            "uncached_input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_write_input_tokens": 30,
            "output_tokens": 40,
        }

    path = tmp_path / "usage.json"
    path.write_text(
        json.dumps(
            {
                "rows": [
                    row("anthropic_direct_api_key", "claude-a"),
                    row("anthropic_subscription_via_cli_proxy_oauth", "alias", "claude-canonical"),
                    row("unknown", None),
                    row("unknown_route_scope", "claude-unknown-route"),
                ]
            }
        )
    )
    bins, audit = load_bins([path])
    assert [(item.model, item.tokens) for item in bins] == [("claude-canonical", 100)]
    assert audit["excluded_paid_or_other_provider"] == 1
    assert audit["excluded_unknown_model"] == 1
    assert audit["excluded_langfuse_unconfirmed_subscription"] == 1


def test_money_coverage_splits_at_changes_and_retains_existing_bracket() -> None:
    polls = [MoneyPoll(START + timedelta(minutes=5 * i), 10 if i < 12 else 11) for i in range(25)]
    ranges = stable_money_ranges(polls)
    assert ranges == [
        TimeRange(START + timedelta(minutes=15), START + timedelta(minutes=40)),
        TimeRange(START + timedelta(minutes=75), START + timedelta(minutes=105)),
    ]
    assert covers_range(ranges, START + timedelta(minutes=20), START + timedelta(minutes=35))
    assert not covers_range(ranges, START + timedelta(minutes=35), START + timedelta(minutes=80))


def test_distinct_model_workloads_recover_a_known_quota_relationship() -> None:
    rows = [
        CalibrationRow(
            start=START + timedelta(days=i),
            end=START + timedelta(days=i, hours=1),
            points=2 * (i % 5 + 1) + 0.5 * ((i * 3) % 7 + 1),
            tokens_by_model={"fake-a": 1e6 * (i % 5 + 1), "fake-b": 1e6 * ((i * 3) % 7 + 1)},
        )
        for i in range(30)
    ]
    result = fit_rows(rows, bootstrap_samples=40, seed=123)
    assert result["matrix_rank"] == 2
    estimates = {row["model"]: row for row in result["models"]}
    assert estimates["fake-a"]["million_recorded_tokens_per_100_points"] == pytest.approx(50)
    assert estimates["fake-b"]["million_recorded_tokens_per_100_points"] == pytest.approx(200)
    assert result["validation"]["held_out_mean_absolute_error_pp"] < 1e-8


def test_perfectly_correlated_models_are_marked_unidentified() -> None:
    rows = [
        CalibrationRow(
            START + timedelta(days=i), START + timedelta(days=i, hours=1), 2 * i, {"fake-a": i * 1e6, "fake-b": i * 2e6}
        )
        for i in range(1, 20)
    ]
    result = fit_rows(rows, bootstrap_samples=30, seed=123)
    assert all(row["identifiability_status"] == "model_mix_not_identified" for row in result["models"])
    assert all(row["million_recorded_tokens_per_100_points"] is None for row in result["models"])


def test_bad_out_of_time_validation_withholds_capacity_for_every_model() -> None:
    rows = [
        CalibrationRow(
            START + timedelta(days=i),
            START + timedelta(days=i, hours=1),
            2 if i < 16 else 80,
            {"old-model": 1e6, "rare-model": 1e5 if i < 3 else 0},
        )
        for i in range(20)
    ]
    result = fit_rows(rows, bootstrap_samples=20, seed=2)
    assert result["validation"]["held_out_r_squared"] is None or result["validation"]["held_out_r_squared"] < 0
    assert all(item["million_recorded_tokens_per_100_points"] is None for item in result["models"])
    assert all(item["diagnostic_points_per_million_recorded_tokens"] is not None for item in result["models"])


def test_interval_crossing_validation_boundary_is_not_counted_as_training_support() -> None:
    rows = [
        CalibrationRow(
            START + timedelta(days=i),
            START + timedelta(days=i + 1) if i == 7 else START + timedelta(days=i, hours=1),
            2,
            {"fake-model": 1e6},
        )
        for i in range(10)
    ]
    rows[7] = CalibrationRow(rows[7].start, rows[7].end + timedelta(minutes=5), 2, {"fake-model": 1e6})
    fit = fit_rows(rows, bootstrap_samples=10, seed=1)
    assert fit["validation"]["cross_boundary_intervals_excluded"] == 1
    assert fit["models"][0]["training_active_days"] == 7
    assert fit["models"][0]["held_out_active_days"] == 2


def test_fable_scoped_gauge_includes_inactive_flagged_polls() -> None:
    scoped = [
        {
            "event_id": str(i),
            "observed_at": (START + timedelta(minutes=5 * i)).isoformat(),
            "provider": "claude",
            "limit_kind": "weekly_scoped",
            "model_scope": "Fable",
            "used_percent": 10 + i // 6,
            "reset_at": (START + timedelta(days=7)).isoformat(),
            "is_active": 1,
        }
        for i in range(13)
    ]
    money = {"claude": [MoneyPoll(START + timedelta(minutes=5 * i), 0) for i in range(-4, 17)]}
    bins = [
        UsageBin(START + timedelta(minutes=5 * i), "anthropic", "claude-fable-5-1", 100, "claude_message", "unknown")
        for i in range(13)
    ]
    result = build_estimates(
        [], bins, money, bootstrap_samples=4, sensitivity_samples=4, chunk_minutes=60, scoped_quota_rows=scoped
    )
    assert result["scoped_quota_audit"]["active_polls"] == 13
    assert [(item["name"], item["gauge_scope"]) for item in result["results"]] == [
        ("Fable", "reported Fable weekly_scoped gauge percent")
    ]
    assert result["results"][0]["scenarios"][0]["intervals"] == 1
    scoped[6]["is_active"] = 0
    result = build_estimates(
        [], bins, money, bootstrap_samples=4, sensitivity_samples=4, chunk_minutes=60, scoped_quota_rows=scoped
    )
    assert result["scoped_quota_audit"]["inactive_polls"] == 1
    assert result["scoped_quota_audit"]["included_polls"] == 13
    assert result["results"][0]["scenarios"][0]["intervals"] == 1


def test_codex_plan_change_splits_calibration_even_when_provider_plan_type_does_not() -> None:
    transition = START + timedelta(hours=1)
    periods = [CodexPlanPeriod(START, transition, "Pro 200"), CodexPlanPeriod(transition, None, "Pro 500")]
    reset = START + timedelta(days=7)
    quota = [
        {
            "event_id": str(i),
            "provider": "codex",
            "window_name": "",
            "window_seconds": 604800,
            "observed_at": (START + timedelta(minutes=5 * i)).isoformat(),
            "used_percent": i if i < 12 else 2 * (i - 12),
            "reset_at": reset.isoformat(),
            "reported_plan_type": "pro",
        }
        for i in range(25)
    ]
    bins = [
        UsageBin(START + timedelta(minutes=5 * i), "openai", "gpt-example", 1000, "native", "unknown")
        for i in range(25)
    ]
    with pytest.raises(ValueError, match="timestamped --codex-plan-history"):
        build_estimates(quota, bins, {}, bootstrap_samples=2, sensitivity_samples=2, chunk_minutes=60)

    result = build_estimates(
        quota, bins, {}, bootstrap_samples=2, sensitivity_samples=2, chunk_minutes=60, codex_plan_periods=periods
    )
    codex = [item for item in result["results"] if item["provider"] == "codex"]
    assert [(item["subscription_plan"], item["provider_reported_plan_types"]) for item in codex] == [
        ("Pro 200", ["pro"]),
        ("Pro 500", ["pro"]),
    ]
    assert [item["matched_from"] for item in codex] == [START.isoformat(), transition.isoformat()]

    changed_category = [{**row, "reported_plan_type": "pro" if i < 12 else "plus"} for i, row in enumerate(quota)]
    with pytest.raises(ValueError, match="plan_type changed within annotated period"):
        build_estimates(
            changed_category,
            bins,
            {},
            bootstrap_samples=2,
            sensitivity_samples=2,
            chunk_minutes=60,
            codex_plan_periods=[CodexPlanPeriod(START, None, "Pro 200")],
        )


def test_codex_plan_history_reader_rejects_overlapping_periods(tmp_path: Path) -> None:
    path = tmp_path / "codex-plan-history.json"
    path.write_text(
        json.dumps(
            {
                "periods": [
                    {"start": START.isoformat(), "end": (START + timedelta(days=2)).isoformat(), "plan": "Pro 200"},
                    {"start": (START + timedelta(days=1)).isoformat(), "end": None, "plan": "Pro 500"},
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="must not overlap"):
        read_codex_plan_history(path)


def test_native_web_equal_token_cost_has_consistent_interaction_fit() -> None:
    intervals = []
    bins = []
    for i in range(30):
        day = START + timedelta(days=i)
        native = 1 + i % 4
        web = 1 + (i * 3) % 5
        intervals.append(QuotaInterval(day, day + timedelta(hours=1), native + web, None))
        bins.append(UsageBin(day, "anthropic", "claude-example", native * 1e6, "claude_message", "unknown"))
        bins.append(UsageBin(day, "anthropic", "claude-example", web * 1e6, "claude_code_web", "unknown"))
    diagnostic = source_consistency(intervals, bins, bootstrap_samples=10)
    assert diagnostic["status"] == "diagnostic_only"
    comparison = diagnostic["compared_models"][0]
    assert comparison["interaction_identified"]
    assert comparison["native_to_web_coefficient_ratio_diagnostic"] == pytest.approx(1)
    assert diagnostic["shared_held_out"]["held_out_mean_absolute_error_pp"] < 1e-8


def test_six_hour_resampling_does_not_stitch_over_excluded_hours() -> None:
    starts = [START + timedelta(hours=i) for i in (0, 1, 2, 10, 11, 12, 13, 14, 15, 16)]
    units, blocks = resampling_units(starts, BlockMethod.SIX_HOURS)
    assert [len(block) for block in blocks] == [3, 6, 6]
    assert all(
        starts[units[b][0]] - starts[units[a][0]] == timedelta(hours=1) for block in blocks for a, b in pairwise(block)
    )


def test_lag_correlations_match_elapsed_hours_instead_of_retained_row_offsets() -> None:
    starts = [START + timedelta(hours=i) for i in (0, 1, 24, 25, 48, 49, 72, 73)]
    values = numpy.array([0, 0, 1, 1, 2, 2, 3, 3], dtype=float)
    lags = {row["lag_hours"]: row for row in lag_correlations(starts, values)}
    assert lags[1]["pairs"] == 4
    assert lags[2]["pairs"] == 0
    assert lags[24]["pairs"] == 6
    assert lags[24]["correlation"] == pytest.approx(1)


def test_circular_blocks_balance_calendar_endpoints_and_preserve_empty_days() -> None:
    starts = [START + timedelta(days=i) for i in (0, 1, 3, 4)]
    units, blocks = resampling_units(starts, BlockMethod.CIRCULAR_THREE_DAYS)
    assert units[2] == []
    assert [sum(day in block for block in blocks) for day in range(5)] == [3] * 5
    assert {index for unit in units for index in unit} == set(range(len(starts)))


def test_multiple_intervals_in_one_hour_are_resampled_and_correlated_together() -> None:
    starts = [START + timedelta(minutes=i) for i in (0, 30, 60, 90, 120, 150, 180, 210)]
    units, _ = resampling_units(starts, BlockMethod.INDEPENDENT_HOURS)
    assert units == [[0, 1], [2, 3], [4, 5], [6, 7]]
    values = numpy.array([0, 0, 1, 0, 2, 0, 3, 0], dtype=float)
    lag = lag_correlations(starts, values)[0]
    assert lag["pairs"] == 3
    assert lag["correlation"] == pytest.approx(1)


def test_day_blocks_capture_shared_day_errors_that_hour_resampling_loses() -> None:
    starts = [START + timedelta(hours=i) for i in range(40 * 24)]
    matrix = numpy.ones((len(starts), 1))
    shocks = numpy.random.default_rng(47).normal(size=40)
    response = 5 + numpy.repeat(shocks, 24)
    result = compare_blocks(
        starts, ["fake-model"], matrix, response, numpy.array([numpy.mean(response)]), samples=120, seed=14
    )
    intervals = {row["method"]: row["models"][0]["coefficient_bootstrap_90"] for row in result["methods"]}
    hour_low, hour_high = intervals[BlockMethod.INDEPENDENT_HOURS]
    day_low, day_high = intervals[BlockMethod.ONE_DAY]
    assert day_high - day_low > 3 * (hour_high - hour_low)


if __name__ == "__main__":
    pytest_bazel.main()
