"""The app's simulation: one composed world per path with the app household tracked on it, stepped to the horizon."""

import numpy as np

from finance.augur.policy.configured_allocation import validate_prepared
from finance.augur.policy.configured_household import ConfiguredHousehold
from finance.augur.sim.capture import FinancialCapture, WorldResult, event_log
from finance.augur.sim.events import EventLog
from finance.augur.sim.ids import AgentId
from finance.augur.sim.metric_composition import BASE_METRIC_NAMES
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.product_metrics import ProductMetricArrays, product_row
from finance.augur.sim.validation import validate
from finance.augur.sim.world import Capture, World


def execute(run: CompiledRun, capture: Capture, primary_agent_id: str) -> tuple[WorldResult, ...]:
    """Every path to its end: the household acts once a month on what the world tells it."""
    validate_prepared(run)
    validate(run)
    household_id = AgentId(primary_agent_id)
    completed = []
    for rollout_id in range(run.rollout_count):
        world = World.from_run(run, rollout_id)
        world.track(
            ConfiguredHousehold(
                household_id, run.scenario._target_allocation_policies, scheduled_sales=run.scenario._scheduled_sales
            )
        )
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
                rollout_id,
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


def simulate_events(run: CompiledRun, primary_agent_id: str) -> EventLog:
    """Dense canonical frames without the forensic journal."""
    return project_events(execute(run, "dense", primary_agent_id))


def simulate_product_metrics(run: CompiledRun, primary_agent_id: str) -> ProductMetricArrays:
    return project_product_metrics(run, execute(run, "summary", primary_agent_id))


def project_product_metrics(run: CompiledRun, completed: tuple[WorldResult, ...]) -> ProductMetricArrays:
    rollout_count = len(completed)
    snapshots = run.scenario.horizon_months + 1
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
        currency_code=run.currency_code,
        currency_quantum=run.currency_quantum,
        base_series=tuple(
            np.asarray(block, dtype=np.int64).reshape((snapshots, rollout_count)) for block in base_series
        ),
    )
