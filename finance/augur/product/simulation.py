"""The app's simulation: each composed world stepped to its horizon with the app household acting on it."""

from collections.abc import Iterable

import numpy as np

from finance.augur.sim.capture import FinancialCapture, WorldResult, event_log
from finance.augur.sim.events import EventLog
from finance.augur.sim.metric_composition import BASE_METRIC_NAMES
from finance.augur.sim.product_metrics import ProductMetricArrays, product_row
from finance.augur.sim.scenario import Currency
from finance.augur.sim.world import Capture, World


def execute(worlds: Iterable[World], capture: Capture, primary_agent_id: str) -> tuple[WorldResult, ...]:
    """Every path to its end: the household tracked on each world acts once a month on what it is told."""
    completed = []
    for world in worlds:
        recorder = FinancialCapture(world, capture=capture)
        rows = [product_row(world, primary_agent_id)]
        world.start()
        while not world.finished:
            world.step()
            recorder.record()
            rows.append(product_row(world, primary_agent_id))
        financial = recorder.financial()
        completed.append(
            WorldResult(
                world.rollout_id,
                financial,
                event_log(financial) if financial is not None else None,
                recorder.configured_summary() if capture == "summary" else None,
                rows,
            )
        )
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
        if result.financial is not None:
            failed = result.financial.failed_month
        elif result.configured_summary is not None:
            failed = result.configured_summary.failed_month
        else:
            raise RuntimeError("product aggregation requires configured financial capture")
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
