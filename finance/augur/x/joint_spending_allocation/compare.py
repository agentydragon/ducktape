"""A paired spending-flexibility × allocation sweep on stipulated taxable paths."""

import argparse
import json
from dataclasses import asdict
from itertools import product
from pathlib import Path

import numpy as np

from finance.augur.sim.books import JournalEntry, Record
from finance.augur.sim.holdings import Disposition
from finance.augur.sim.quantiles import currency_quantiles
from finance.augur.sim.results import ConsumptionTarget, Payment, Stop, UnpaidClaim
from finance.augur.sim.world import World
from finance.augur.x.bounded_spending.python_policy import Parameters
from finance.augur.x.joint_spending_allocation.policy import JointHousehold
from finance.augur.x.joint_spending_allocation.situation import Situation, compose, sample, situation

RETIREE = "retiree"


class Month(Record):
    month: int
    intended_consumption: int | None
    fixed_real_anchor: int | None
    cut_from_fixed_real_anchor: int | None
    consumption_requested: int | None
    consumption_paid: int
    consumption_shortfall: int | None


class PathMeasurements(Record):
    """What this experiment chose to read from one world between steps."""

    rollout_id: int
    stop: Stop | None
    closed_months: int
    ending_mark_month: int
    ending_assets: int
    terminal_assets: int | None
    tax_assessed: int
    tax_paid: int
    payments: list[Payment]
    unpaid_claims: list[UnpaidClaim]
    months: list[Month]


class Measurements(Record):
    paths: list[PathMeasurements]
    completed_paths: int
    terminal_assets_percentiles: tuple[int, ...] | None


class Replay(Record):
    """A selected path's measurements plus the journal and dispositions it copied each month."""

    measurements: PathMeasurements
    journal: list[JournalEntry]
    dispositions: list[Disposition]


class Traces(Record):
    replays: list[Replay]


def assets(world: World) -> int:
    """Cash plus marked public holdings, before unsettled taxes and unpaid claims."""
    cash = sum(
        world.accounting.ledger.balance(account) for account in world.accounting.declared if account.agent_id == RETIREE
    )
    return cash + world.holding_value(RETIREE, world.mark_month)


def run_path(
    case: Situation, rollout_id: int, *, parameters: Parameters, annual_step: int, replay: bool
) -> tuple[PathMeasurements, Replay | None]:
    """One world with a fresh household; the experiment owns the loop and records between steps."""
    world = compose(case, rollout_id)
    household = JointHousehold(parameters, annual_step=annual_step)
    world.track(household)
    world.start()
    months: list[Month] = []
    payments: list[Payment] = []
    journal: list[JournalEntry] = []
    dispositions: list[Disposition] = []
    tax_assessed = tax_paid = 0
    while not world.finished:
        world.step()
        month = world.month - 1
        intent = household.intentions.get(month)
        consumption = [
            payment.receipt
            for payment in world.payments
            if isinstance(payment.receipt.target, ConsumptionTarget)
            and payment.receipt.target.component_id == "annual_consumption"
        ]
        # An unattempted request is absent; on a stopped month the completed prefix still
        # proves actual payment is zero, so paid is known while requested is not.
        requested = consumption[0].amount_requested if consumption else None
        paid = consumption[0].amount_paid if consumption else 0
        months.append(
            Month(
                month=month,
                intended_consumption=intent.consumption if intent is not None else None,
                fixed_real_anchor=intent.fixed_real_anchor if intent is not None else None,
                cut_from_fixed_real_anchor=max(0, intent.fixed_real_anchor - intent.consumption)
                if intent is not None
                else None,
                consumption_requested=requested,
                consumption_paid=paid,
                consumption_shortfall=max(0, intent.consumption - paid) if intent is not None else None,
            )
        )
        payments.extend(world.payments)
        tax_assessed += sum(row.total_tax for row in world.accounting.tax_accruals)
        tax_paid += sum(row.amount_paid for row in world.accounting.tax_payments)
        if replay:
            journal.extend(world.accounting.journal)
            dispositions.extend(world.holdings.dispositions)
    ending = assets(world)
    measurements = PathMeasurements(
        rollout_id=rollout_id,
        stop=world.stop,
        closed_months=world.month,
        ending_mark_month=world.mark_month,
        ending_assets=ending,
        terminal_assets=ending if world.stop is None else None,
        tax_assessed=tax_assessed,
        tax_paid=tax_paid,
        payments=payments,
        unpaid_claims=world.unpaid_claims(RETIREE),
        months=months,
    )
    return measurements, Replay(
        measurements=measurements, journal=journal, dispositions=dispositions
    ) if replay else None


def run_cell(case: Situation, rollout_ids: list[int], *, parameters: Parameters, annual_step: int) -> Measurements:
    paths = [
        run_path(case, id_, parameters=parameters, annual_step=annual_step, replay=False)[0] for id_ in rollout_ids
    ]
    terminal = [path.terminal_assets for path in paths if path.terminal_assets is not None]
    return Measurements(
        paths=paths,
        completed_paths=len(terminal),
        terminal_assets_percentiles=currency_quantiles(np.asarray(terminal, dtype=np.int64), (0.0, 50.0, 100.0))
        if terminal
        else None,
    )


def replay_cell(case: Situation, rollout_ids: list[int], *, parameters: Parameters, annual_step: int) -> Traces:
    replays = []
    for id_ in rollout_ids:
        _, replay = run_path(case, id_, parameters=parameters, annual_step=annual_step, replay=True)
        if replay is None:
            raise RuntimeError("replay requested without a trace")
        replays.append(replay)
    return Traces(replays=replays)


def compare(output_dir: Path) -> None:
    horizon = 60
    case = situation(sample(horizon_months=horizon), rollout_count=3, horizon_months=horizon, taxable=True)
    output_dir.mkdir(parents=True, exist_ok=False)
    cells = []
    for rate, (flex_name, cut, raise_), (allocation_name, step) in product(
        (400, 800), (("fixed_real", 0, 0), ("bounded", 2000, 500)), (("constant", 0), ("glide", 5))
    ):
        name = f"r{rate}-{flex_name}-{allocation_name}"
        parameters = Parameters(rate, cut, raise_)
        measured = run_cell(case, [0, 1, 2], parameters=parameters, annual_step=step)
        (output_dir / f"{name}.json").write_text(measured.model_dump_json())
        traces = replay_cell(case, [2, 0], parameters=parameters, annual_step=step)
        (output_dir / f"{name}-traces.json").write_text(traces.model_dump_json())
        cells.append({"name": name, "spending": asdict(parameters), "annual_allocation_step_percent": step})
        print(f"{name}: completed={measured.completed_paths}/3; paired stipulated cases, not probability")
    (output_dir / "experiment.json").write_text(
        json.dumps(
            {
                "sampler": "three stipulated joint price/CPI paths; no fitted evidence window or random draws",
                "path_ids": [0, 1, 2],
                "path_source": "situation.py:sample, declared onto each path's World by situation.py:compose",
                "products": "test-growth and test-steady are quoted public securities with no distributions, fees or default model; neither is a bond fund",
                "taxes": "synthetic flat 20% ordinary/ST and 10% LT schedule, zero deduction/loss ordinary offset; no claim to statutory US/state/foreign coverage",
                "tax_limitations": "no personalized filing inputs, NIIT/AMT/state/foreign coverage claim, liquidation tax or horizon-end settlement; final assessed/unpaid taxes remain separate",
                "initial_situation": "USD 10000 cash and 500 units of each security at USD 100, basis USD 80/unit acquired 24 months earlier; USD 1000 nominal committed bill each year",
                "horizon_months": horizon,
                "currency_code": "USD",
                "currency_quantum": "0.01",
                "amount_basis": "nominal integer currency quanta",
                "review": "one monthly decision after scheduled cashflows and due-claim assembly; annual consumption at 0,12,...",
                "actions": "reserve due claims plus intended consumption; shared cash-band/drift trade proposal, then due claims, then consumption; no retry or tax gross-up",
                "allocation": "initial 50/50; constant or annual growth target +5 percentage points capped at 70/30; cash band USD 0/10000, purchases allowed, zero drift tolerance in-band",
                "consumption": "bounded rule on current cash plus public holdings; CPI-indexed previous amount and annual cut/raise limits; fixed_real_anchor is a separate zero-flex reference from the same opening wealth",
                "missing_amounts": "unattempted consumption request is null; actual paid is zero from the completed action prefix; post-stop months are absent; policy intention is not an engine receipt",
                "terminal_measure": "cash plus marked public holdings, before unsettled taxes and unpaid claims; completed horizon only; stopped assets have their own mark month",
                "percentiles": [0, 50, 100],
                "distribution_scope": "empirical summaries of these three cases, conditional on completion; no probabilities, independent-sampling error bars or policy ranking",
                "measurements": "read from world state between steps by this experiment; the world keeps no history",
                "cells": cells,
                "trace_rollouts": [2, 0],
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    compare(parser.parse_args().output_dir)


if __name__ == "__main__":
    main()
