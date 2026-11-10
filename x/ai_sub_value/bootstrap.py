"""Paired block resampling and descriptive dependence diagnostics."""

import random
from collections import defaultdict
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any

import numpy
from scipy.optimize import nnls


class BlockMethod(StrEnum):
    INDEPENDENT_HOURS = "independent_hours"
    SIX_HOURS = "six_hours"
    ONE_DAY = "one_day"
    THREE_DAYS = "three_days"
    SEVEN_DAYS = "seven_days"
    CIRCULAR_THREE_DAYS = "circular_three_days"
    CIRCULAR_SEVEN_DAYS = "circular_seven_days"


def calendar_day_blocks(days: list[str], *, length: int = 3) -> list[list[str]]:
    runs: list[list[str]] = []
    for day in days:
        if not runs or datetime.fromisoformat(day) - datetime.fromisoformat(runs[-1][-1]) != timedelta(days=1):
            runs.append([day])
        else:
            runs[-1].append(day)
    return [run[start : start + length] for run in runs for start in range(max(1, len(run) - length + 1))]


def resampling_units(starts: list[datetime], method: BlockMethod) -> tuple[list[list[int]], list[list[int]]]:
    """Return observation groups and moving windows of group indices."""
    by_hour: dict[datetime, list[int]] = defaultdict(list)
    for i, at in enumerate(starts):
        by_hour[at.replace(minute=0, second=0, microsecond=0)].append(i)
    hours = sorted(by_hour)
    hour_units = [by_hour[hour] for hour in hours]
    if method == BlockMethod.INDEPENDENT_HOURS:
        return hour_units, [[i] for i in range(len(hour_units))]
    if method == BlockMethod.SIX_HOURS:
        # Gaps in retained hours end runs, rather than joining distant observations.
        runs: list[list[int]] = []
        for i, hour in enumerate(hours):
            if not runs or hour - hours[runs[-1][-1]] != timedelta(hours=1):
                runs.append([i])
            else:
                runs[-1].append(i)
        blocks = [run[start : start + 6] for run in runs for start in range(max(1, len(run) - 5))]
        return hour_units, blocks
    length = {
        BlockMethod.ONE_DAY: 1,
        BlockMethod.THREE_DAYS: 3,
        BlockMethod.SEVEN_DAYS: 7,
        BlockMethod.CIRCULAR_THREE_DAYS: 3,
        BlockMethod.CIRCULAR_SEVEN_DAYS: 7,
    }[method]
    by_day: dict[str, list[int]] = defaultdict(list)
    for i, at in enumerate(starts):
        by_day[at.date().isoformat()].append(i)
    days = sorted(by_day)
    if method in (BlockMethod.CIRCULAR_THREE_DAYS, BlockMethod.CIRCULAR_SEVEN_DAYS):
        # A complete calendar grid keeps empty days as gaps. Circular windows give
        # every calendar day equal inclusion across candidate blocks.
        first, last = datetime.fromisoformat(days[0]), datetime.fromisoformat(days[-1])
        days = [(first + timedelta(days=i)).date().isoformat() for i in range((last - first).days + 1)]
        return [by_day[day] for day in days], [
            [(start + offset) % len(days) for offset in range(length)] for start in range(len(days))
        ]
    day_indices = {day: i for i, day in enumerate(days)}
    return [by_day[day] for day in days], [
        [day_indices[day] for day in block] for block in calendar_day_blocks(days, length=length)
    ]


def resample_coefficients(
    matrix: numpy.ndarray,
    response: numpy.ndarray,
    units: list[list[int]],
    blocks: list[list[int]],
    *,
    samples: int,
    seed: int,
) -> numpy.ndarray:
    rng = random.Random(seed)
    draws = []
    for _ in range(samples):
        selected: list[int] = []
        while len(selected) < len(units):
            selected.extend(rng.choice(blocks))
        indices = [index for unit in selected[: len(units)] for index in units[unit]]
        coefficients, _ = nnls(matrix[indices], response[indices], maxiter=max(1000, 20 * matrix.shape[1]))
        draws.append(coefficients)
    return numpy.array(draws)


def lag_correlations(starts: list[datetime], values: numpy.ndarray) -> list[dict[str, Any]]:
    # Match actual UTC hour labels; consecutive retained rows can be days apart.
    by_hour: dict[datetime, float] = defaultdict(float)
    for at, value in zip(starts, values, strict=True):
        by_hour[at.replace(minute=0, second=0, microsecond=0)] += float(value)
    result = []
    for lag in (1, 2, 3, 6, 12, 24, 48, 72, 168):
        pairs = [
            (value, by_hour[at + timedelta(hours=lag)])
            for at, value in by_hour.items()
            if at + timedelta(hours=lag) in by_hour
        ]
        correlation = None
        if len(pairs) >= 3:
            left = numpy.array([a for a, _ in pairs])
            right = numpy.array([b for _, b in pairs])
            if numpy.std(left) > 0 and numpy.std(right) > 0:
                correlation = float(numpy.corrcoef(left, right)[0, 1])
        result.append({"lag_hours": lag, "pairs": len(pairs), "correlation": correlation})
    return result


def compare_blocks(
    starts: list[datetime],
    models: list[str],
    matrix: numpy.ndarray,
    response: numpy.ndarray,
    coefficients: numpy.ndarray,
    *,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    residual = response - matrix @ coefficients
    methods = []
    for method in BlockMethod:
        units, blocks = resampling_units(starts, method)
        seeds = (seed, seed + 1)
        draws = [resample_coefficients(matrix, response, units, blocks, samples=samples, seed=s) for s in seeds]
        combined = numpy.concatenate(draws)
        estimates = []
        for i, model in enumerate(models):
            lower, upper = (float(value) for value in numpy.quantile(combined[:, i], [0.05, 0.95]))
            estimates.append(
                {
                    "model": model,
                    "coefficient_bootstrap_90": [lower, upper],
                    "capacity_bootstrap_90_million_tokens": [
                        100 / upper if upper > 1e-12 else None,
                        100 / lower if lower > 1e-12 else None,
                    ],
                    "zero_coefficient_fraction": float(numpy.mean(combined[:, i] <= 1e-12)),
                    "coefficient_intervals_by_seed": [
                        {"seed": s, "interval": numpy.quantile(draw[:, i], [0.05, 0.95]).tolist()}
                        for s, draw in zip(seeds, draws, strict=True)
                    ],
                }
            )
        block_rows = [sum(len(units[index]) for index in block) for block in blocks]
        methods.append(
            {
                "method": method,
                "candidate_blocks": len(blocks),
                "observations_per_candidate_block": {
                    "min": min(block_rows),
                    "median": float(numpy.median(block_rows)),
                    "max": max(block_rows),
                },
                "models": estimates,
            }
        )
    return {
        "scope": "source-sum, zero-lag; same poll-aligned intervals, model columns and point fit for every resampler; hourly methods group all intervals by UTC start hour",
        "replications_per_seed": samples,
        "seeds": [seed, seed + 1],
        "gaps_between_retained_hours": sum(
            b - a != timedelta(hours=1)
            for a, b in pairwise(sorted({at.replace(minute=0, second=0, microsecond=0) for at in starts}))
        ),
        "residual_lag_correlations": lag_correlations(starts, residual),
        "model_score_lag_correlations": {
            model: lag_correlations(starts, matrix[:, i] * residual) for i, model in enumerate(models)
        },
        "methods": methods,
        "caveats": [
            "These are conditional sensitivity intervals, not an empirically selected optimal block length or a coverage guarantee.",
            "Day methods retain all eligible hours per sampled UTC day; six-hour blocks stop at missing retained hours. Runs shorter than the requested block form a shorter block.",
            "Overlapping moving windows underweight run endpoints; long blocks provide fewer effectively distinct resamples, especially for recently introduced models.",
            "Circular day methods sample the complete calendar grid, including empty days, and wrap from the record's end to its beginning. They balance endpoint inclusion but introduce artificial adjacency at that seam; short series still limit long blocks.",
            "Residual and model-score correlations sum interval contributions by UTC start hour, then use only observed pairs at the stated elapsed-hour lag; they are descriptive and not significance tests.",
            "A null upper capacity means the coefficient interval reaches zero; this does not establish unlimited usage. Model identification still follows the primary-fit gates.",
        ],
    }
