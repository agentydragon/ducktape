"""The app's simulation: each composed world stepped to its horizon with the app household acting on it."""

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from finance.augur.product.metric_composition import BASE_METRIC_NAMES
from finance.augur.product.metrics import ProductMetricArrays, product_row
from finance.augur.sim.capture import FinancialCapture, FinancialOutput, event_log
from finance.augur.sim.events import EventLog
from finance.augur.sim.scenario import Currency
from finance.augur.sim.world import Capture, World


@dataclass(frozen=True)
class WorldResult:
    """One path's record, kept between steps: the app's projections read this, never the world.

    `financial` and `events` are `None` under summary capture, which keeps only the metric slab.
    """

    rollout_id: int
    failed_month: int | None
    product_metrics: list[tuple[int, int, int, int, int, int, int]]
    financial: FinancialOutput | None = None
    events: EventLog | None = None


def execute(worlds: Iterable[World], capture: Capture, primary_agent_id: str) -> tuple[WorldResult, ...]:
    """Every path to its end: the household tracked on each world acts once a month on what it is told."""
    completed = []
    for world in worlds:
        recorder = None if capture == "summary" else FinancialCapture(world, capture=capture)
        rows = [product_row(world, primary_agent_id)]
        world.start()
        while not world.finished:
            world.step()
            if recorder is not None:
                recorder.record()
            rows.append(product_row(world, primary_agent_id))
        if recorder is None:
            completed.append(WorldResult(world.rollout_id, world.failed_month, rows))
        else:
            financial = recorder.financial()
            completed.append(WorldResult(world.rollout_id, world.failed_month, rows, financial, event_log(financial)))
    return tuple(completed)


def project_events(completed: tuple[WorldResult, ...]) -> EventLog:
    logs = []
    for result in completed:
        if result.events is None:
            raise RuntimeError("event projection requires dense or forensic capture")
        logs.append(result.events)
    return EventLog.concat(logs)


def simulate_events(worlds: Iterable[World], primary_agent_id: str) -> EventLog:
    """Dense canonical frames without the forensic journal."""
    return project_events(execute(worlds, "dense", primary_agent_id))


def simulate_product_metrics(
    worlds: Iterable[World], *, horizon_months: int, currency: Currency, primary_agent_id: str
) -> ProductMetricArrays:
    return project_product_metrics(
        execute(worlds, "summary", primary_agent_id), horizon_months=horizon_months, currency=currency
    )


def project_product_metrics(
    completed: tuple[WorldResult, ...], *, horizon_months: int, currency: Currency
) -> ProductMetricArrays:
    rollout_count = len(completed)
    snapshots = horizon_months + 1
    base_series = [[0] * (snapshots * rollout_count) for _ in BASE_METRIC_NAMES]
    failures = []
    for column, result in enumerate(completed):
        rows = result.product_metrics
        failed = result.failed_month
        expected = snapshots if failed is None else failed + 2
        if len(rows) != expected:
            raise ValueError(f"rollout produced {len(rows)} product snapshots, expected {expected}")
        failures.append(-1 if failed is None else failed)
        for snapshot, row in enumerate(rows):
            for metric, value in enumerate(row):
                base_series[metric][snapshot * rollout_count + column] = value
    return ProductMetricArrays(
        rollout_ids=tuple(result.rollout_id for result in completed),
        month_index=np.arange(snapshots, dtype=np.int64),
        failed_month=np.asarray(failures, dtype=np.int64),
        currency_code=currency.code,
        currency_quantum=format(currency.quantum, "f"),
        base_series=tuple(
            np.asarray(block, dtype=np.int64).reshape((snapshots, rollout_count)) for block in base_series
        ),
    )
