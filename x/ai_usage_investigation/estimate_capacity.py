"""Fit empirical token/quota relationships from reconciled usage and clean gauges."""

import argparse
import bisect
import gzip
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy
from scipy.optimize import nnls

from x.ai_usage_investigation.analyze import Poll, discontinuity, read_rows
from x.ai_usage_investigation.bootstrap import BlockMethod, compare_blocks, resample_coefficients, resampling_units


@dataclass(frozen=True)
class UsageBin:
    at: datetime
    provider: str
    model: str
    tokens: float
    source: str
    payment_lane: str


@dataclass(frozen=True)
class QuotaInterval:
    start: datetime
    end: datetime
    points: float
    reset: datetime | None


@dataclass(frozen=True)
class CalibrationRow:
    start: datetime
    end: datetime
    points: float
    tokens_by_model: dict[str, float]

    @property
    def day(self) -> str:
        return self.start.date().isoformat()


@dataclass(frozen=True)
class MoneyPoll:
    at: datetime
    value: float


def read_money(rows: list[dict[str, Any]]) -> dict[str, list[MoneyPoll]]:
    result: dict[str, list[MoneyPoll]] = defaultdict(list)
    for row in rows:
        field = "claude_spend_used" if row["source"] == "claude" else "codex_paid"
        if row[field] in ("", "null"):
            continue
        raw = json.loads(row[field])
        value = raw["amount_minor"] / 10 ** raw["exponent"] if row["source"] == "claude" else float(raw["balance"])
        result[row["source"]].append(MoneyPoll(parse_utc(row["observed_at"]), value))
    return {provider: sorted(polls, key=lambda poll: poll.at) for provider, polls in result.items()}


@dataclass(frozen=True)
class MonotonicRun:
    polls: tuple[Poll, ...]


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def join_poll_pairs(pairs: Iterable[tuple[Poll, Poll]]) -> list[MonotonicRun]:
    runs: list[list[Poll]] = []
    for before, after in pairs:
        if runs and runs[-1][-1] == before:
            runs[-1].append(after)
        else:
            runs.append([before, after])
    return [MonotonicRun(tuple(polls)) for polls in runs]


def split_monotonic_runs(polls: list[Poll]) -> tuple[list[MonotonicRun], dict[str, int]]:
    pairs = []
    rejected: dict[str, int] = defaultdict(int)
    for before, after in pairwise(sorted(polls, key=lambda poll: poll.at)):
        if after.at - before.at > timedelta(minutes=15):
            rejected["poll_gap"] += 1
        elif discontinuity(before, after):
            rejected["reset_or_discontinuity"] += 1
        elif before.used >= 100 or after.used >= 100:
            rejected["saturated"] += 1
        else:
            pairs.append((before, after))
    return join_poll_pairs(pairs), dict(rejected)


def covers_range(ranges: list[TimeRange], start: datetime, end: datetime) -> bool:
    index = bisect.bisect_right(ranges, start, key=lambda item: item.start) - 1
    return index >= 0 and ranges[index].end >= end


def stable_money_ranges(polls: list[MoneyPoll]) -> list[TimeRange]:
    runs: list[TimeRange] = []
    for before, after in pairwise(sorted(polls, key=lambda poll: poll.at)):
        if before.value == after.value and after.at - before.at <= timedelta(minutes=15):
            if runs and runs[-1].end == before.at:
                runs[-1] = TimeRange(runs[-1].start, after.at)
            else:
                runs.append(TimeRange(before.at, after.at))
    # Preserve the existing 15-minute money-counter bracket on each side.
    padding = timedelta(minutes=15)
    return [TimeRange(run.start + padding, run.end - padding) for run in runs if run.end - run.start > 2 * padding]


def select_monotonic_runs(
    polls: list[Poll],
    *,
    companions: list[list[Poll]],
    money: list[MoneyPoll],
    usage_start: datetime,
    usage_end: datetime,
) -> tuple[list[MonotonicRun], dict[str, int]]:
    runs, rejected = split_monotonic_runs(polls)
    companion_ranges = [
        [TimeRange(run.polls[0].at, run.polls[-1].at) for run in split_monotonic_runs(other)[0]] for other in companions
    ]
    money_ranges = stable_money_ranges(money)
    accepted = []
    for run in runs:
        for before, after in pairwise(run.polls):
            reason = None
            if before.at < usage_start or after.at > usage_end:
                reason = "outside_usage_window"
            elif any(not covers_range(ranges, before.at, after.at) for ranges in companion_ranges):
                reason = "other_window_missing_unclean_or_saturated"
            elif not covers_range(money_ranges, before.at, after.at):
                reason = "paid_counter_change_or_missing_money_coverage"
            if reason is not None:
                rejected[reason] = rejected.get(reason, 0) + 1
            else:
                accepted.append((before, after))
    return join_poll_pairs(accepted), rejected


def calibration_intervals(runs: list[MonotonicRun], *, chunk_minutes: int | None) -> list[QuotaInterval]:
    """Partition at real polls, retaining every valid edge including short tails."""
    result = []
    for run in runs:
        first = 0
        for last in range(1, len(run.polls)):
            if last == len(run.polls) - 1 or (
                chunk_minutes is not None
                and run.polls[last].at - run.polls[first].at >= timedelta(minutes=chunk_minutes)
            ):
                before, after = run.polls[first], run.polls[last]
                result.append(QuotaInterval(before.at, after.at, after.used - before.used, before.reset))
                first = last
    return result


def overlap_bins(bins: list[UsageBin], *, scenario: str) -> list[UsageBin]:
    if scenario == "sum_sources":
        return bins
    if scenario != "max_source_family":
        raise ValueError(f"unknown overlap scenario: {scenario}")
    grouped: dict[tuple[datetime, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in bins:
        family = source_family(row.source)
        grouped[(row.at, row.provider, row.model)][family] += row.tokens
    return [
        UsageBin(at, provider, model, max(families.values()), "source_family_max", "assumed_subscription")
        for (at, provider, model), families in grouped.items()
    ]


def source_family(source: str) -> str:
    return "cloud" if source == "claude_code_web" else "langfuse" if source == "langfuse" else "native"


def align_usage(intervals: list[QuotaInterval], bins: list[UsageBin], *, lag_minutes: int) -> list[CalibrationRow]:
    """Allocate five-minute aggregates by overlap; lag is an explicit scenario."""
    ordered = sorted(bins, key=lambda row: row.at)
    times = [row.at for row in ordered]
    result = []
    lag = timedelta(minutes=lag_minutes)
    width = timedelta(minutes=5)
    for interval in intervals:
        start, end = interval.start - lag, interval.end - lag
        first = bisect.bisect_right(times, start - width)
        last = bisect.bisect_left(times, end)
        values: dict[str, float] = defaultdict(float)
        for row in ordered[first:last]:
            overlap = min(end, row.at + width) - max(start, row.at)
            if overlap > timedelta(0):
                values[row.model] += row.tokens * overlap.total_seconds() / width.total_seconds()
        result.append(
            CalibrationRow(start=interval.start, end=interval.end, points=interval.points, tokens_by_model=dict(values))
        )
    return result


def calibration_matrix(rows: list[CalibrationRow]) -> tuple[list[str], numpy.ndarray, numpy.ndarray]:
    models = sorted({model for row in rows for model, tokens in row.tokens_by_model.items() if tokens > 0})
    return (
        models,
        numpy.array([[row.tokens_by_model.get(model, 0) / 1e6 for model in models] for row in rows]),
        numpy.array([row.points for row in rows], dtype=float),
    )


def aggregation_sensitivity(runs: list[MonotonicRun], bins: list[UsageBin]) -> list[dict[str, Any]]:
    result = []
    for minutes in (30, 60, 120, None):
        intervals = calibration_intervals(runs, chunk_minutes=minutes)
        rows = align_usage(intervals, bins, lag_minutes=0)
        models, matrix, response = calibration_matrix(rows)
        if not models:
            continue
        coefficients, _ = nnls(matrix, response, maxiter=max(1000, 20 * len(models)))
        scale = numpy.linalg.norm(matrix, axis=0)
        standardized = matrix / numpy.where(scale > 0, scale, 1)
        result.append(
            {
                "chunk_minutes": minutes,
                "qualification": "diagnostic_point_fit_only; no bootstrap or held-out coverage claim",
                "intervals": len(rows),
                "observed_hours": sum((row.end - row.start).total_seconds() for row in rows) / 3600,
                "observed_points": float(numpy.sum(response)),
                "recorded_tokens": sum(sum(row.tokens_by_model.values()) for row in rows),
                "matrix_rank": int(numpy.linalg.matrix_rank(standardized)),
                "model_columns": len(models),
                "models": [
                    {
                        "model": model,
                        "points_per_million_recorded_tokens": float(coefficient),
                        "million_recorded_tokens_per_100_points": 100 / float(coefficient)
                        if coefficient > 1e-12
                        else None,
                    }
                    for model, coefficient in zip(models, coefficients, strict=True)
                ],
            }
        )
    return result


def fit_rows(
    rows: list[CalibrationRow], *, bootstrap_samples: int, seed: int, sensitivity_samples: int = 0
) -> dict[str, Any]:
    models, matrix, response = calibration_matrix(rows)
    days = sorted({row.day for row in rows})
    if not models or not rows:
        return {
            "status": "no_matching_usage",
            "models": [],
            "intervals": len(rows),
            "observed_hours": sum((row.end - row.start).total_seconds() for row in rows) / 3600,
            "days": len(days),
        }
    coefficients, _ = nnls(matrix, response, maxiter=max(1000, 20 * len(models)))
    prediction = matrix @ coefficients
    residual = response - prediction
    scale = numpy.linalg.norm(matrix, axis=0)
    standardized = matrix / numpy.where(scale > 0, scale, 1)
    rank = int(numpy.linalg.matrix_rank(standardized))
    condition = float(numpy.linalg.cond(standardized))
    starts = [row.start for row in rows]
    units, blocks = resampling_units(starts, BlockMethod.THREE_DAYS)
    sampled_coefficients = resample_coefficients(matrix, response, units, blocks, samples=bootstrap_samples, seed=seed)
    split = max(1, int(len(days) * 0.8))
    train_days, test_days = set(days[:split]), set(days[split:])
    cutoff = datetime.fromisoformat(days[split]).replace(tzinfo=UTC) if split < len(days) else None
    train = [i for i, row in enumerate(rows) if row.day in train_days and (cutoff is None or row.end <= cutoff)]
    test = [i for i, row in enumerate(rows) if row.day in test_days]
    validation: dict[str, Any] = {
        "training_days": len(train_days),
        "held_out_days": len(test_days),
        "cross_boundary_intervals_excluded": len(rows) - len(train) - len(test),
    }
    if train and test:
        training_fit, _ = nnls(matrix[train], response[train], maxiter=max(1000, 20 * len(models)))
        held_out_error = response[test] - matrix[test] @ training_fit
        validation["held_out_mean_absolute_error_pp"] = float(numpy.mean(numpy.abs(held_out_error)))
        denominator = float(numpy.sum((response[test] - numpy.mean(response[test])) ** 2))
        validation["held_out_r_squared"] = (
            1 - float(held_out_error @ held_out_error) / denominator if denominator else None
        )
    predictive_r_squared = validation.get("held_out_r_squared")
    predictive_ok = predictive_r_squared is not None and predictive_r_squared >= 0
    estimates = []
    for index, model in enumerate(models):
        active = [row.day for row in rows if row.tokens_by_model.get(model, 0) > 0]
        training_active = {rows[i].day for i in train if rows[i].tokens_by_model.get(model, 0) > 0}
        held_out_active = {rows[i].day for i in test if rows[i].tokens_by_model.get(model, 0) > 0}
        others = numpy.delete(standardized, index, axis=1)
        column = standardized[:, index]
        explained = (
            others @ numpy.linalg.lstsq(others, column, rcond=None)[0] if others.shape[1] else numpy.zeros_like(column)
        )
        separation = float(numpy.linalg.norm(column - explained))
        quantiles = numpy.quantile(sampled_coefficients[:, index], [0.05, 0.95])
        coefficient = float(coefficients[index])
        lower, upper = (float(quantiles[0]), float(quantiles[1]))
        identifiability_status = "identified"
        if separation < 0.05:
            identifiability_status = "model_mix_not_identified"
        elif len(set(active)) < 8:
            identifiability_status = "few_independent_days"
        elif lower <= 1e-12:
            identifiability_status = "zero_cost_within_interval"
        elif len(training_active) < 2 or len(held_out_active) < 2:
            identifiability_status = "insufficient_train_or_test_support"
        elif not all(numpy.isfinite(value) for value in (coefficient, lower, upper)):
            identifiability_status = "nonfinite_interval"
        status = (
            "conditional_estimate"
            if predictive_ok and identifiability_status == "identified"
            else ("poor_or_missing_predictive_validation" if not predictive_ok else identifiability_status)
        )
        publishable = status == "conditional_estimate"
        estimates.append(
            {
                "model": model,
                "status": status,
                "identifiability_status": identifiability_status,
                "active_days": len(set(active)),
                "active_intervals": len(active),
                "training_active_days": len(training_active),
                "held_out_active_days": len(held_out_active),
                "separation_from_other_models": separation,
                "diagnostic_points_per_million_recorded_tokens": coefficient,
                "diagnostic_coefficient_bootstrap_90": [lower, upper],
                "points_per_million_recorded_tokens": coefficient if publishable else None,
                "coefficient_bootstrap_90": [lower, upper] if publishable else None,
                "million_recorded_tokens_per_100_points": 100 / coefficient if publishable else None,
                "capacity_bootstrap_90_million_tokens": [100 / upper, 100 / lower] if publishable else None,
            }
        )
    return {
        "status": "fit",
        "intervals": len(rows),
        "observed_hours": sum((row.end - row.start).total_seconds() for row in rows) / 3600,
        "days": len(days),
        "matrix_rank": rank,
        "model_columns": len(models),
        "condition_number": condition if numpy.isfinite(condition) else None,
        "observed_points": float(numpy.sum(response)),
        "recorded_tokens": sum(sum(row.tokens_by_model.values()) for row in rows),
        "mean_absolute_error_pp": float(numpy.mean(numpy.abs(residual))),
        "intervals_with_quota_rise_without_recorded_tokens": sum(
            row.points > 0 and not row.tokens_by_model for row in rows
        ),
        "validation": validation,
        "models": estimates,
        **(
            {
                "block_sensitivity": compare_blocks(
                    starts, models, matrix, response, coefficients, samples=sensitivity_samples, seed=seed
                )
            }
            if sensitivity_samples
            else {}
        ),
    }


def load_bins(paths: list[Path]) -> tuple[list[UsageBin], dict[str, Any]]:
    result = []
    audit: dict[str, Any] = {
        "source_files": [str(path) for path in paths],
        "rows_without_token_total": 0,
        "derived_anthropic_total_rows": 0,
        "excluded_paid_or_other_provider": 0,
        "excluded_unknown_model": 0,
        "excluded_langfuse_unconfirmed_subscription": 0,
        "assumed_subscription_unknown_lane_rows": 0,
        "known_subscription_lane_rows": 0,
        "excluded_partial_langfuse_totals": 0,
        "excluded_conflicting_langfuse_totals": 0,
    }
    for path in paths:
        document = json.loads(gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_text())
        rows = document if isinstance(document, list) else document["rows"]
        for row in rows:
            lane = row.get("payment_lane")
            known_subscription_lanes = {
                "openai_subscription_via_cli_proxy_oauth",
                "anthropic_subscription_via_cli_proxy_oauth",
            }
            assumed_unknown_lanes = {None, "", "unknown", "unknown_route_scope"}
            if row["provider"] not in ("openai", "anthropic") or lane not in (
                known_subscription_lanes | assumed_unknown_lanes
            ):
                audit["excluded_paid_or_other_provider"] += 1
                continue
            audit[
                "known_subscription_lane_rows"
                if lane in known_subscription_lanes
                else "assumed_subscription_unknown_lane_rows"
            ] += 1
            model = row.get("canonical_model") or row.get("model")
            if not model:
                audit["excluded_unknown_model"] += 1
                continue
            if row["source"] == "langfuse":
                if lane not in known_subscription_lanes:
                    audit["excluded_langfuse_unconfirmed_subscription"] += 1
                    continue
                if row.get("total_known_requests", 0) < row.get("requests", 0):
                    audit["excluded_partial_langfuse_totals"] += 1
                    continue
                if row.get("total_reconciliation_delta") not in (None, 0):
                    audit["excluded_conflicting_langfuse_totals"] += 1
                    continue
            tokens = row["total_tokens"]
            if tokens is None and row["provider"] == "anthropic":
                fields = [
                    "uncached_input_tokens",
                    "cache_read_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                ]
                if all(row[field] is not None for field in fields):
                    tokens = sum(row[field] for field in fields)
                    audit["derived_anthropic_total_rows"] += 1
            if tokens is None:
                audit["rows_without_token_total"] += 1
                continue
            result.append(
                UsageBin(
                    at=parse_utc(row["bucket_start_utc"]),
                    provider=row["provider"],
                    model=model,
                    tokens=float(tokens),
                    source=row["source"],
                    payment_lane=row["payment_lane"],
                )
            )
    return result, audit


def source_consistency(
    intervals: list[QuotaInterval], bins: list[UsageBin], *, bootstrap_samples: int
) -> dict[str, Any]:
    """Compare a shared model coefficient with native/Web interaction columns."""
    eligible = [row for row in bins if source_family(row.source) in ("native", "cloud")]
    if not eligible or not intervals:
        return {"status": "no_native_web_comparison"}
    split_bins = [
        UsageBin(
            row.at, row.provider, f"{row.model}::{source_family(row.source)}", row.tokens, row.source, row.payment_lane
        )
        for row in eligible
    ]
    split_rows = align_usage(intervals, split_bins, lag_minutes=0)
    source_days: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in split_rows:
        for label, tokens in row.tokens_by_model.items():
            if tokens > 0:
                model, family = label.rsplit("::", 1)
                source_days[model][family].add(row.day)
    comparable = sorted(
        model
        for model, families in source_days.items()
        if len(families.get("native", set())) >= 8 and len(families.get("cloud", set())) >= 8
    )
    if not comparable:
        return {
            "status": "insufficient_dual_source_support",
            "active_days_by_model": {
                model: {family: len(days) for family, days in families.items()}
                for model, families in source_days.items()
            },
        }
    samples = min(bootstrap_samples, 100)
    shared = fit_rows(align_usage(intervals, eligible, lag_minutes=0), bootstrap_samples=samples, seed=20261031)
    interaction = fit_rows(split_rows, bootstrap_samples=samples, seed=20261031)
    by_label = {row["model"]: row for row in interaction["models"]}
    ratios = []
    for model in comparable:
        native = by_label[f"{model}::native"]
        web = by_label[f"{model}::cloud"]
        supported = all(
            row["training_active_days"] >= 2
            and row["held_out_active_days"] >= 2
            and row["separation_from_other_models"] >= 0.05
            for row in (native, web)
        )
        native_coefficient = native["diagnostic_points_per_million_recorded_tokens"]
        web_coefficient = web["diagnostic_points_per_million_recorded_tokens"]
        ratios.append(
            {
                "model": model,
                "native_active_days": native["active_days"],
                "web_active_days": web["active_days"],
                "native_points_per_million_tokens_diagnostic": native_coefficient,
                "web_points_per_million_tokens_diagnostic": web_coefficient,
                "native_to_web_coefficient_ratio_diagnostic": native_coefficient / web_coefficient
                if supported and web_coefficient > 0
                else None,
                "interaction_identified": supported,
            }
        )
    return {
        "status": "diagnostic_only",
        "compared_models": ratios,
        "shared_held_out": shared["validation"],
        "source_interaction_held_out": interaction["validation"],
        "caveat": "Source interactions are confounded by time, account scope, cache/output mix and cross-source duplicates; they do not prove equal provider pricing.",
    }


def build_estimates(
    quota_rows: list[dict[str, Any]],
    bins: list[UsageBin],
    money: dict[str, list[MoneyPoll]],
    *,
    bootstrap_samples: int,
    sensitivity_samples: int,
    chunk_minutes: int,
    scoped_quota_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    groups: dict[tuple[str, int, str], list[Poll]] = defaultdict(list)
    seen = set()
    for row in quota_rows:
        identity = (row["event_id"], row["provider"], row["window_seconds"], row["window_name"])
        if identity in seen:
            continue
        seen.add(identity)
        if row["window_seconds"] not in (18000, 604800):
            continue
        groups[(row["provider"], row["window_seconds"], row["window_name"])].append(
            Poll(
                parse_utc(row["observed_at"]),
                row["used_percent"],
                parse_utc(row["reset_at"]) if row["reset_at"] else None,
            )
        )
    scoped_audit: dict[str, int] = defaultdict(int)
    for row in scoped_quota_rows or []:
        scoped_audit["observed_polls"] += 1
        if (
            row.get("provider") != "claude"
            or row.get("limit_kind") != "weekly_scoped"
            or row.get("model_scope") != "Fable"
        ):
            scoped_audit["unsupported_scope"] += 1
            continue
        if row.get("is_active") not in (True, 1, "1", "true"):
            scoped_audit["inactive_polls"] += 1
        else:
            scoped_audit["active_polls"] += 1
        identity = (row["event_id"], "claude", 604800, "Fable")
        if identity in seen:
            scoped_audit["duplicate_polls"] += 1
            continue
        seen.add(identity)
        groups[("claude", 604800, "Fable")].append(
            Poll(
                parse_utc(row["observed_at"]),
                row["used_percent"],
                parse_utc(row["reset_at"]) if row["reset_at"] else None,
            )
        )
        scoped_audit["included_polls"] += 1
    results = []
    for (provider, seconds, name), polls in sorted(groups.items()):
        if name and name != "Fable":
            continue
        account_provider = {"claude": "anthropic", "codex": "openai"}[provider]
        relevant = [row for row in bins if row.provider == account_provider]
        if name == "Fable":
            relevant = [row for row in relevant if "fable" in row.model.lower()]
        if not relevant:
            results.append({"provider": provider, "window_seconds": seconds, "name": name, "status": "no_usage_source"})
            continue
        first, last = min(row.at for row in relevant), max(row.at for row in relevant) + timedelta(minutes=5)
        other_windows = [
            other_polls
            for (other_provider, other_seconds, other_name), other_polls in groups.items()
            if other_provider == provider and (other_seconds, other_name) != (seconds, name) and not other_name
        ]
        runs, rejected = select_monotonic_runs(
            polls, companions=other_windows, money=money.get(provider, []), usage_start=first, usage_end=last
        )
        matched = calibration_intervals(runs, chunk_minutes=chunk_minutes)
        scenarios = []
        for overlap_scenario in ("sum_sources", "max_source_family"):
            scenario_bins = overlap_bins(relevant, scenario=overlap_scenario)
            for lag in (0, 5, 15):
                aligned = align_usage(matched, scenario_bins, lag_minutes=lag)
                scenarios.append(
                    {
                        "source_overlap_scenario": overlap_scenario,
                        "metering_lag_minutes": lag,
                        **fit_rows(
                            aligned,
                            bootstrap_samples=bootstrap_samples,
                            seed=20261031,
                            sensitivity_samples=sensitivity_samples
                            if overlap_scenario == "sum_sources" and lag == 0
                            else 0,
                        ),
                    }
                )
        periods = []
        if matched:
            cutoff = max(interval.end for interval in matched)
            week = cutoff.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=cutoff.weekday())
            for age in reversed(range(5)):
                end = min(week + timedelta(days=7 - 7 * age), cutoff)
                start = end - timedelta(days=28)
                selected = [interval for interval in matched if interval.start >= start and interval.end <= end]
                if not selected:
                    continue
                periods.append(
                    {
                        "ending_week": (week - timedelta(days=7 * age)).date().isoformat(),
                        "calibration_start": start.isoformat(),
                        "calibration_end": end.isoformat(),
                        "source_overlap_scenario": "sum_sources",
                        "metering_lag_minutes": 0,
                        **fit_rows(
                            align_usage(selected, relevant, lag_minutes=0),
                            bootstrap_samples=bootstrap_samples,
                            seed=20261031,
                        ),
                    }
                )
        results.append(
            {
                "provider": provider,
                "window_seconds": seconds,
                "name": name,
                "gauge_scope": "reported Fable weekly_scoped gauge percent"
                if name == "Fable"
                else "general provider gauge percent",
                "matched_from": first.isoformat(),
                "matched_through": last.isoformat(),
                "rejected_poll_pairs": rejected,
                "monotonic_runs": [
                    {
                        "start": run.polls[0].at.isoformat(),
                        "end": run.polls[-1].at.isoformat(),
                        "poll_pairs": len(run.polls) - 1,
                        "points": run.polls[-1].used - run.polls[0].used,
                    }
                    for run in runs
                ],
                "aggregation_sensitivity": aggregation_sensitivity(runs, relevant),
                "scenarios": scenarios,
                "native_web_source_consistency": source_consistency(
                    matched, relevant, bootstrap_samples=bootstrap_samples
                )
                if provider == "claude"
                else {"status": "not_applicable"},
                "rolling_four_week_estimates": periods,
            }
        )
    return {
        "method": "nonnegative linear quota points versus million recorded tokens per exact model; poll-aligned monotonic ranges with short tails retained; empirical token mix",
        "chunk_minutes": chunk_minutes,
        "confidence": "90% percentile bootstrap from contiguous three-calendar-day UTC moving blocks (shorter blocks at run boundaries); conditional on source accounting, account scope, model, and metering scenario",
        "systematic_uncertainty": [
            "Shared per-model quota cost between native sessions and Claude Code Web is an explicit working assumption; source-interaction diagnostics can test rough agreement but not prove equal provider pricing",
            "Incomplete capture and unknown payment/account scope are outside bootstrap intervals",
            "Integer-gauge quantization and within-five-minute request timing are separate measurement uncertainties",
            "Coefficients average the observed cache/output/effort/speed mix and do not define universal model entitlements",
            "Resets, gaps over fifteen minutes, companion general-window coverage/saturation and changing/missing money counters are screened; unobserved between-poll resets remain possible",
            "Source-family maximum is an overlap sensitivity, not a population lower bound; source sum can double count cross-system requests",
            "The reported Fable scoped gauge is its own percent scale; is_active semantics and its relationship to the general weekly gauge are unknown. Scoped policy may change, and general-weekly coefficients may mix effects of simultaneous scoped limits",
            "Unknown native/cloud billing scope is included under the user's overwhelmingly subscription traffic assumption; this is not a measured per-request payment attribution",
        ],
        "bootstrap_samples": bootstrap_samples,
        "scoped_quota_audit": dict(scoped_audit),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quota", type=Path, required=True)
    parser.add_argument("--accounting", type=Path, required=True)
    parser.add_argument("--usage", type=Path, nargs="+", required=True)
    parser.add_argument("--scoped-quota", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=500)
    parser.add_argument("--sensitivity-samples", type=int, default=2000, help="Replications per seed and block method")
    parser.add_argument(
        "--chunk-minutes",
        type=int,
        default=60,
        help="Elapsed-time target within monotonic runs; all short tails retained",
    )
    args = parser.parse_args()
    bins, audit = load_bins(args.usage)
    result = build_estimates(
        read_rows(args.quota),
        bins,
        read_money(read_rows(args.accounting)),
        bootstrap_samples=args.bootstrap_samples,
        sensitivity_samples=args.sensitivity_samples,
        chunk_minutes=args.chunk_minutes,
        scoped_quota_rows=read_rows(args.scoped_quota) if args.scoped_quota else None,
    )
    result["input_audit"] = audit
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"series": len(result["results"]), "usage_bins": len(bins), "output": str(args.output)}))


if __name__ == "__main__":
    main()
