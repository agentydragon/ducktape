"""One result shape for both engines.

A differential test should not know which engine produced the rows it compares. Each
backend runs the same `Case` and answers in the canonical schemas
`sim/testing/state_helpers.py` defines, so a comparison is `assert_backends_agree(case)`
rather than a select/sort/rename written out per test — which is what the suites did
before, and what let a reader's filter (`account_id == "checking"`) hide inside one side's
accessor.

Channels only one engine has stay visibly one engine's: the balanced journal, the TLH
deferral ledger and held bond principal are Rust-only, and a test wanting them asks the
Rust result for them by name.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import polars as pl
from polars.testing import assert_frame_equal

from finance.augur.rust import simulator
from finance.augur.rust.differential.fixture import fixture_for
from finance.augur.rust.event_log import decode_event_log
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.engine.jax_engine import run_jax_scan
from finance.augur.sim.events import EVENT_FRAME_SPECS, EventLog
from finance.augur.sim.scenario import Scenario
from finance.augur.sim.testing.case import Case
from finance.augur.sim.testing.state_helpers import (
    asset_lots,
    capital_gains_ytd,
    cash_balances,
    liabilities,
    property_stakes,
    property_state,
    rollout_status,
    tax_liabilities,
)

# Parts per billion, the fixture's scale for a dimensionless rate.
RATE_SCALE_PPB = 1_000_000_000


@dataclass(frozen=True)
class StateChannel:
    """One channel both engines answer in: its columns, and the key that orders it.

    The schema is declared here rather than inferred from whatever each engine happened to
    build, because neither engine declares one. The JAX readers assemble frames from numpy
    arrays, so their identifier columns arrive as `Object` and an all-null month column as
    `Null` — carrier accidents, not differences between the engines. Conforming both sides
    to one declaration turns that from a repair into a contract, and a channel that lost a
    column fails here by name instead of comparing as absent.
    """

    name: str
    schema: pl.Schema
    key: tuple[str, ...]

    def build(self, rows: list[dict[str, Any]]) -> pl.DataFrame:
        """The channel, from rows already keyed by its column names."""

        return pl.DataFrame(rows, schema=self.schema).sort(self.key)

    def conform(self, frame: pl.DataFrame) -> pl.DataFrame:
        """The channel, from an engine's own frame: its declared columns, in order."""

        return frame.select(pl.col(name).cast(dtype) for name, dtype in self.schema.items()).sort(self.key)


_ROLLOUT_MONTH = {"rollout_index": pl.Int64, "month_index": pl.Int64}

STATE_CHANNELS = (
    StateChannel(
        "cash",
        pl.Schema({**_ROLLOUT_MONTH, "agent_id": pl.String, "account_id": pl.String, "balance_quanta": pl.Int64}),
        ("rollout_index", "month_index", "agent_id", "account_id"),
    ),
    StateChannel(
        "lots",
        pl.Schema(
            {
                **_ROLLOUT_MONTH,
                "lot_id": pl.String,
                "agent_id": pl.String,
                "account_id": pl.String,
                "asset_id": pl.String,
                "purchase_month_index": pl.Int64,
                "cost_basis_per_unit_quanta": pl.Int64,
                "remaining_quantity_quanta": pl.Int64,
                "quantity_scale": pl.Int64,
            }
        ),
        ("rollout_index", "month_index", "lot_id"),
    ),
    StateChannel(
        "capital_gains",
        pl.Schema({**_ROLLOUT_MONTH, "agent_id": pl.String, "classification": pl.String, "gain_quanta": pl.Int64}),
        ("rollout_index", "month_index", "agent_id", "classification"),
    ),
    StateChannel(
        "tax_liabilities",
        pl.Schema(
            {
                **_ROLLOUT_MONTH,
                "agent_id": pl.String,
                "jurisdiction_id": pl.String,
                "tax_year_end_month": pl.Int64,
                "amount_owed_quanta": pl.Int64,
            }
        ),
        ("rollout_index", "month_index", "agent_id", "jurisdiction_id"),
    ),
    StateChannel(
        "properties",
        pl.Schema(
            {
                **_ROLLOUT_MONTH,
                "property_id": pl.String,
                "location_id": pl.String,
                "purchase_month_index": pl.Int64,
                "adjusted_basis_quanta": pl.Int64,
            }
        ),
        ("rollout_index", "month_index", "property_id"),
    ),
    StateChannel(
        "property_stakes",
        pl.Schema(
            {
                **_ROLLOUT_MONTH,
                "property_id": pl.String,
                "agent_id": pl.String,
                "contribution_used_quanta": pl.Int64,
                "equity_ledger_quanta": pl.Int64,
            }
        ),
        ("rollout_index", "month_index", "property_id", "agent_id"),
    ),
    StateChannel(
        "liabilities",
        pl.Schema(
            {
                **_ROLLOUT_MONTH,
                "liability_id": pl.String,
                "agent_id": pl.String,
                "payment_account_id": pl.String,
                "counterparty_agent_id": pl.String,
                "counterparty_account_id": pl.String,
                "property_id": pl.String,
                "principal_quanta": pl.Int64,
                "annual_interest_rate": pl.Float64,
                "term_months": pl.Int64,
                "origination_month_index": pl.Int64,
                "monthly_payment_quanta": pl.Int64,
                "interest_paid_ytd_quanta": pl.Int64,
            }
        ),
        ("rollout_index", "month_index", "liability_id"),
    ),
    StateChannel(
        "rollout_status",
        pl.Schema({"rollout_index": pl.Int64, "status": pl.String, "failed_month": pl.Int64}),
        ("rollout_index",),
    ),
)

CANONICAL_STATE_CHANNELS = tuple(channel.name for channel in STATE_CHANNELS)
CHANNEL = {channel.name: channel for channel in STATE_CHANNELS}


@dataclass(frozen=True)
class SimulationResult:
    """What both engines answer with, in one schema.

    Every frame is sorted, so equality is a value comparison and never depends on the order
    an engine happened to emit rows in.
    """

    backend: str
    events: EventLog
    cash: pl.DataFrame
    lots: pl.DataFrame
    capital_gains: pl.DataFrame
    tax_liabilities: pl.DataFrame
    properties: pl.DataFrame
    property_stakes: pl.DataFrame
    liabilities: pl.DataFrame
    rollout_status: pl.DataFrame

    @property
    def state_channels(self) -> dict[str, pl.DataFrame]:
        return {name: getattr(self, name) for name in CANONICAL_STATE_CHANNELS}


@dataclass(frozen=True)
class RustResult(SimulationResult):
    """The canonical channels plus the ones only Rust keeps.

    The journal has no Python counterpart by design, and the TLH ledger and bond principal
    are engine state the JAX output does not surface.
    """

    journal: pl.DataFrame
    tlh_ledger: pl.DataFrame
    bonds: pl.DataFrame
    bond_cashflows: pl.DataFrame
    distributions: pl.DataFrame
    # The accrual fields Rust records beyond the canonical `tax_breakdowns` frame: §1250
    # tax and the shared capital-loss carryforward have no JAX event counterpart.
    tax_accrual_details: pl.DataFrame
    # A property sale's tax split. The canonical frame carries the sale, not its components.
    property_sale_details: pl.DataFrame
    # Per-snapshot property state beyond the canonical `properties` channel: the §121
    # occupancy clock and the depreciation accumulators.
    property_details: pl.DataFrame


def _sorted(rows: list[dict[str, Any]], schema: dict[str, Any], by: list[str]) -> pl.DataFrame:
    """A frame only Rust produces, so its schema is declared at its one use."""

    return pl.DataFrame(rows, schema=schema).sort(by)


def _realized_gains(frame: pl.DataFrame, taxed: set[str]) -> pl.DataFrame:
    """Taxed agents' nonzero gains.

    Scoped to taxed agents because that is what both engines model: JAX also tracks gains
    for any agent holding lots or selling, taxed or not, while Rust surfaces none for an
    untaxed agent (README.md § Scope Rust does not cover).

    Zeroes are dropped because a zero gain and an absent row say the same thing, and the two
    engines disagree only on which they emit — JAX masks by a per-tax-year active flag, Rust
    reports every snapshot.
    """

    return frame.filter(
        (pl.col("agent_id").cast(pl.String).is_in(list(taxed)) if taxed else pl.lit(value=False))
        & (pl.col("gain_quanta") != 0)
    )


# What a lot holding no units says about its own acquisition. A preallocated
# target-allocation slot not yet bought into carries a placeholder in each, and the engines
# choose different ones — Rust zero, JAX the sleeve's configured price and the month the
# slot will eventually fill. Neither is a claim about anything while no units sit behind it.
# What a lot actually cost and when it was bought is compared through `lot_dispositions`,
# which carries both.
UNHELD_LOT_PLACEHOLDERS = ("cost_basis_per_unit_quanta", "purchase_month_index")


def _held_lots(frame: pl.DataFrame) -> pl.DataFrame:
    """Blank the acquisition fields of a lot holding nothing."""

    return frame.with_columns(
        pl.when(pl.col("remaining_quantity_quanta") == 0).then(0).otherwise(pl.col(column)).alias(column)
        for column in UNHELD_LOT_PLACEHOLDERS
    )


def run_jax(case: Case) -> SimulationResult:
    """Run the case on the Python/JAX engine, off the plan the Rust fixture is encoded from."""

    run = SimulationRun(plan=case.plan, output=run_jax_scan(case.plan), external_series=case.external_series)
    taxed = {profile.agent_id for profile in case.scenario.tax_profiles}
    return SimulationResult(
        backend="jax",
        events=run.events_log,
        cash=CHANNEL["cash"].conform(cash_balances(run)),
        lots=CHANNEL["lots"].conform(_held_lots(asset_lots(run))),
        capital_gains=CHANNEL["capital_gains"].conform(_realized_gains(capital_gains_ytd(run), taxed)),
        tax_liabilities=CHANNEL["tax_liabilities"].conform(tax_liabilities(run)),
        properties=CHANNEL["properties"].conform(property_state(run)),
        property_stakes=CHANNEL["property_stakes"].conform(property_stakes(run)),
        liabilities=CHANNEL["liabilities"].conform(liabilities(run)),
        rollout_status=CHANNEL["rollout_status"].conform(rollout_status(run)),
    )


def _rust_rows(rust: dict[str, Any], channel: str) -> list[tuple[int, int, dict[str, Any]]]:
    """Every `(rollout, month, record)` in one monthly-snapshot channel."""

    return [
        (rollout["rollout_id"], snapshot["month"], record)
        for rollout in rust["rollouts"]
        for snapshot in rollout["months"]
        for record in snapshot[channel]
    ]


def run_rust(case: Case) -> RustResult:
    """Run the case on the Rust engine, in-process through the extension module."""

    # Forensic rather than dense: the harness wants the balanced journal, which is the
    # double-entry invariant made checkable and has no JAX counterpart to compare against.
    rust = cast(dict[str, Any], json.loads(simulator.simulate_forensic_json(json.dumps(fixture_for(case)))))
    return rust_result(rust, case.scenario)


def rust_result(rust: dict[str, Any], scenario: Scenario) -> RustResult:
    """Project a raw Rust output document into the canonical schemas.

    The scenario is needed to know which accounts it declared: the Rust ledger also carries
    the internal accounts a double-entry engine needs (opening equity, asset basis, realized
    gain, tax expense and liability, the external boundary), and none of those are cash the
    JAX engine models.
    """

    declared_accounts = {(balance.agent_id, balance.account_id) for balance in scenario.initial_cash}
    cash = CHANNEL["cash"].build(
        [
            {
                "rollout_index": rollout,
                "month_index": month,
                "agent_id": record["account"]["agent_id"],
                "account_id": record["account"]["account_id"],
                "balance_quanta": record["balance"],
            }
            for rollout, month, record in _rust_rows(rust, "balances")
            if (record["account"]["agent_id"], record["account"]["account_id"]) in declared_accounts
        ]
    )
    lots = CHANNEL["lots"].build(
        [
            {
                "rollout_index": rollout,
                "month_index": month,
                "lot_id": record["lot_id"],
                "agent_id": record["agent_id"],
                "account_id": record["account_id"],
                "asset_id": record["asset_id"],
                "purchase_month_index": record["purchase_month"],
                "cost_basis_per_unit_quanta": record["cost_basis_per_unit"],
                "remaining_quantity_quanta": record["units_remaining"],
                "quantity_scale": record["quantity_scale"],
            }
            for rollout, month, record in _rust_rows(rust, "lots")
        ]
    )
    lots = _held_lots(lots)
    capital_gains = CHANNEL["capital_gains"].build(
        [
            {
                "rollout_index": rollout,
                "month_index": month,
                "agent_id": record["agent_id"],
                "classification": classification,
                "gain_quanta": record[key],
            }
            for rollout, month, record in _rust_rows(rust, "capital_gains")
            for classification, key in (("ltcg", "long_term_gain"), ("stcg", "short_term_gain"))
            if record[key] != 0
        ]
    )
    # JAX emits a tax liability row only where the amount or the active flag changed, so the
    # Rust projection has to take the same differences rather than every snapshot's state.
    liability_rows: list[dict[str, Any]] = []
    for rollout in rust["rollouts"]:
        previous: dict[tuple[str, str, int], tuple[int, bool]] = {}
        for snapshot in rollout["months"]:
            for record in snapshot["tax_liabilities"]:
                key = (record["agent_id"], record["jurisdiction_id"], record["tax_year_end_month"])
                current = (record["amount_owed"], record["active"])
                if record["active"] and previous.get(key, (0, False)) != current:
                    liability_rows.append(
                        {
                            "rollout_index": rollout["rollout_id"],
                            "month_index": snapshot["month"],
                            "agent_id": record["agent_id"],
                            "jurisdiction_id": record["jurisdiction_id"],
                            "tax_year_end_month": record["tax_year_end_month"],
                            "amount_owed_quanta": record["amount_owed"],
                        }
                    )
                previous[key] = current
    tax_liability_frame = CHANNEL["tax_liabilities"].build(liability_rows)
    properties = CHANNEL["properties"].build(
        [
            {
                "rollout_index": rollout,
                "month_index": month,
                "property_id": record["property_id"],
                "location_id": record["location_id"],
                "purchase_month_index": record["purchase_month"],
                "adjusted_basis_quanta": record["adjusted_basis"],
            }
            for rollout, month, record in _rust_rows(rust, "properties")
            if record["active"]
        ]
    )
    stakes = CHANNEL["property_stakes"].build(
        [
            {
                "rollout_index": rollout,
                "month_index": month,
                "property_id": record["property_id"],
                "agent_id": record["owner_agent_id"],
                "contribution_used_quanta": record["contribution_used"],
                "equity_ledger_quanta": record["equity_ledger"],
            }
            for rollout, month, record in _rust_rows(rust, "properties")
            if record["active"]
        ]
    )
    liability_state = CHANNEL["liabilities"].build(
        [
            {
                "rollout_index": rollout,
                "month_index": month,
                "liability_id": record["liability_id"],
                "agent_id": record["agent_id"],
                "payment_account_id": record["payment_account_id"],
                "counterparty_agent_id": record["counterparty_agent_id"],
                "counterparty_account_id": record["counterparty_account_id"],
                "property_id": record["property_id"],
                "principal_quanta": record["principal"],
                "annual_interest_rate": record["annual_interest_rate_ppb"] / RATE_SCALE_PPB,
                "term_months": record["term_months"],
                "origination_month_index": record["origination_month"],
                "monthly_payment_quanta": record["monthly_payment"],
                "interest_paid_ytd_quanta": record["interest_paid_ytd"],
            }
            for rollout, month, record in _rust_rows(rust, "mortgages")
            if record["active"]
        ]
    )
    status = CHANNEL["rollout_status"].build(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "status": "active" if rollout["failed_month"] is None else "failed_insufficient_cash",
                "failed_month": rollout["failed_month"],
            }
            for rollout in rust["rollouts"]
        ]
    )
    journal = _sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": entry["month"],
                "cause_id": entry["cause_id"],
                "imbalance_quanta": sum(posting["amount"] for posting in entry["postings"]),
            }
            for rollout in rust["rollouts"]
            for entry in rollout["journal"]
        ],
        {"rollout_index": pl.Int64, "month_index": pl.Int64, "cause_id": pl.String, "imbalance_quanta": pl.Int64},
        ["rollout_index", "month_index", "cause_id"],
    )
    tlh_ledger = _sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "month_index": snapshot["month"],
                "policy_index": index,
                "cumulative_harvest_quanta": value,
            }
            for rollout in rust["rollouts"]
            for snapshot in rollout["months"]
            for index, value in enumerate(snapshot["tlh_cumulative_harvest"])
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "policy_index": pl.Int64,
            "cumulative_harvest_quanta": pl.Int64,
        },
        ["rollout_index", "month_index", "policy_index"],
    )
    bonds = _sorted(
        [
            {
                "rollout_index": rollout,
                "month_index": month,
                "bond_id": record["bond_id"],
                "agent_id": record["agent_id"],
                "principal_quanta": record["principal"],
                "active": record["active"],
            }
            for rollout, month, record in _rust_rows(rust, "bonds")
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "bond_id": pl.String,
            "agent_id": pl.String,
            "principal_quanta": pl.Int64,
            "active": pl.Boolean,
        },
        ["rollout_index", "month_index", "bond_id"],
    )

    def detail_frame(channel: str, keys: dict[str, Any], money: tuple[str, ...], sort_by: list[str]) -> pl.DataFrame:
        """One of Rust's own record streams, typed. `keys` maps column name to record field."""

        return _sorted(
            [
                {
                    "rollout_index": rollout["rollout_id"],
                    "month_index": record["month"],
                    **{column: record[field] for column, field in keys.items()},
                    **{f"{name}_quanta": record[name] for name in money},
                }
                for rollout in rust["rollouts"]
                for record in rollout[channel]
            ],
            {
                "rollout_index": pl.Int64,
                "month_index": pl.Int64,
                **dict.fromkeys(keys, pl.String),
                **{f"{name}_quanta": pl.Int64 for name in money},
            },
            sort_by,
        )

    bond_cashflows = detail_frame(
        "bond_cashflows",
        {"bond_id": "bond_id", "issuer_jurisdiction_id": "issuer_jurisdiction_id"},
        ("coupon", "accretion", "redemption"),
        ["rollout_index", "month_index", "bond_id"],
    )
    distributions = detail_frame(
        "distributions",
        {"asset_id": "asset_id", "issuer_jurisdiction_id": "issuer_jurisdiction_id"},
        ("amount",),
        ["rollout_index", "month_index", "asset_id"],
    )
    tax_accrual_details = detail_frame(
        "tax_accruals",
        {"agent_id": "agent_id", "jurisdiction_id": "jurisdiction_id"},
        (
            "ordinary_income",
            "short_term_gain",
            "long_term_gain",
            "section_1250_recapture",
            "ordinary_taxable",
            "long_term_capital_gain_taxable",
            "ordinary_tax",
            "capital_gain_tax",
            "section_1250_tax",
            "total_tax",
            "capital_loss_carryforward",
            "salt_deduction",
            "itemized_deduction",
        ),
        ["rollout_index", "month_index", "agent_id", "jurisdiction_id"],
    )
    property_sale_details = detail_frame(
        "property_sales",
        {"property_id": "property_id"},
        (
            "gross_proceeds",
            "mortgage_payoff",
            "net_cash_to_owner",
            "realized_gain",
            "depreciation_recapture",
            "section_121_exclusion",
            "long_term_capital_gain",
        ),
        ["rollout_index", "month_index", "property_id"],
    )
    property_details = _sorted(
        [
            {
                "rollout_index": rollout,
                "month_index": month,
                "property_id": record["property_id"],
                "rented_fraction_ppb": record["rented_fraction_ppb"],
                "owner_occupied_months": record["owner_occupied_months"],
                "cumulative_depreciation_quanta": record["cumulative_depreciation"],
                "building_basis_quanta": record["building_basis"],
            }
            for rollout, month, record in _rust_rows(rust, "properties")
            if record["active"]
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "property_id": pl.String,
            "rented_fraction_ppb": pl.Int64,
            "owner_occupied_months": pl.Int64,
            "cumulative_depreciation_quanta": pl.Int64,
            "building_basis_quanta": pl.Int64,
        },
        ["rollout_index", "month_index", "property_id"],
    )
    return RustResult(
        backend="rust",
        events=decode_event_log(rust),
        cash=cash,
        lots=lots,
        capital_gains=capital_gains,
        tax_liabilities=tax_liability_frame,
        properties=properties,
        property_stakes=stakes,
        liabilities=liability_state,
        rollout_status=status,
        journal=journal,
        tlh_ledger=tlh_ledger,
        bonds=bonds,
        bond_cashflows=bond_cashflows,
        distributions=distributions,
        tax_accrual_details=tax_accrual_details,
        property_sale_details=property_sale_details,
        property_details=property_details,
    )


# What a suite parameterizes over when the property under test should hold for either
# engine, rather than being a comparison between them.
type Backend = Callable[[Case], SimulationResult]

BACKENDS: tuple[Backend, ...] = (run_jax, run_rust)


def _failure_months(status: pl.DataFrame) -> pl.DataFrame:
    """`(rollout_index, month_index)` for each rollout that ran out of cash.

    Read from `rollout_status`, which is itself compared between the engines and agrees — so
    the rows this excludes below are identified by something the two engines concur on, not
    by one engine's account of where it stopped.
    """

    return (
        status.filter(pl.col("failed_month").is_not_null())
        .select("rollout_index", pl.col("failed_month").alias("month_index"))
        .cast({"month_index": pl.Int64})
    )


def _outside_the_failure_month(frame: pl.DataFrame, failure_months: pl.DataFrame) -> pl.DataFrame:
    """Drop what a rollout recorded during the month it could not pay.

    The engines do not agree about that one month and the question is open: Rust stops inside
    its month loop at the phase that could not pay, so whether a phase was recorded depends on
    where it sits in that order, while JAX cannot leave a vectorized scan partway through a
    month and reports the whole month or none of it. No month-level rule reproduces an ordering
    *within* one, so this is a modelling decision nobody has made rather than a defect either
    engine can be said to have.

    Both answers stay pinned in `known_divergence_test.py`, which is what keeps this narrow: a
    change to either engine's behaviour in the failure month still fails there and has to state
    its intent. What is given up is the fuzzer's chance of finding an *unrelated* bug that
    happens to land in the failure month of a failed rollout — one month of the rollouts that
    fail at all. Every other month of every rollout still compares in full.
    """

    if not {"rollout_index", "month_index"} <= set(frame.columns) or failure_months.is_empty():
        return frame
    return frame.join(failure_months, on=["rollout_index", "month_index"], how="anti")


def assert_results_agree(expected: SimulationResult, actual: SimulationResult) -> None:
    """Every channel both engines answer in, plus every canonical event frame."""

    for name, frame in expected.state_channels.items():
        try:
            assert_frame_equal(actual.state_channels[name], frame, check_row_order=False, check_column_order=False)
        except AssertionError as error:
            raise AssertionError(f"state channel {name!r} differs between backends") from error
    failure_months = _failure_months(expected.rollout_status)
    for spec in EVENT_FRAME_SPECS:
        try:
            assert_frame_equal(
                _outside_the_failure_month(actual.events.frame(spec), failure_months),
                _outside_the_failure_month(expected.events.frame(spec), failure_months),
                check_row_order=False,
            )
        except AssertionError as error:
            raise AssertionError(f"event frame {spec.name!r} differs between backends") from error


def assert_backends_agree(case: Case) -> RustResult:
    """Run the case on both engines and return the Rust result.

    Returning the Rust one is not a preference: it carries the same values in every shared
    channel by the time this returns, plus the journal and ledgers JAX has no counterpart
    for, so a caller needing those does not run the case twice.
    """

    jax_result, rust = run_jax(case), run_rust(case)
    assert_results_agree(jax_result, rust)
    return rust
