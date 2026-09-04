"""One result shape for both engines.

A differential test should not know which engine produced the rows it compares. Each
backend runs the same fixture and answers in the canonical schemas
`sim/testing/state_helpers.py` defines, so a comparison is `assert_backends_agree(fixture)`
rather than a select/sort/rename written out per test — which is what the suites did
before, and what let a reader's filter (`account_id == "checking"`) hide inside one side's
accessor.

Channels only one engine has stay visibly one engine's: the balanced journal, the TLH
deferral ledger and held bond principal are Rust-only, and a test wanting them asks the
Rust result for them by name.
"""

import json
from dataclasses import dataclass
from typing import Any, cast

import polars as pl
from polars.testing import assert_frame_equal

from finance.augur.rust import simulator
from finance.augur.rust.differential.fixture_adapter import run_legacy_fixture
from finance.augur.rust.differential.output_adapter import decode_rust_event_log
from finance.augur.sim.events import EVENT_FRAME_SPECS, EventLog
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

# The state both engines answer in. Listed rather than derived from the dataclass, so
# adding a field to `SimulationResult` is not silently also adding a compared channel.
CANONICAL_STATE_CHANNELS = (
    "cash",
    "lots",
    "capital_gains",
    "tax_liabilities",
    "properties",
    "property_stakes",
    "liabilities",
    "rollout_status",
)


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


def _sorted(rows: list[dict[str, Any]], schema: dict[str, Any], by: list[str]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=schema).sort(by)


def _canonical(frame: pl.DataFrame, by: list[str]) -> pl.DataFrame:
    """Sort, and give string columns a string dtype.

    The JAX readers build their identifier columns from numpy object arrays, so polars types
    them `Object`. That is a carrier detail, not a difference between the engines.
    """

    casts = [pl.col(name).cast(pl.String) for name, dtype in frame.schema.items() if dtype == pl.Object]
    # An all-null column has dtype Null, which says nothing about what it holds. Every
    # nullable column in these schemas is a month index, so give it the integer type the
    # Rust side declares rather than comparing Null against Int64.
    casts += [pl.col(name).cast(pl.Int64) for name, dtype in frame.schema.items() if dtype == pl.Null]
    return (frame.with_columns(casts) if casts else frame).sort(by)


def _taxed_agents(fixture: dict[str, Any]) -> set[str]:
    return {profile["agent_id"] for profile in fixture["scenario"].get("tax_profiles", [])}


def run_jax(fixture: dict[str, Any]) -> SimulationResult:
    """Run the fixture on the Python/JAX engine."""

    run = run_legacy_fixture(fixture)
    lots = asset_lots(run)
    taxed = _taxed_agents(fixture)
    return SimulationResult(
        backend="jax",
        events=run.events_log,
        cash=_canonical(cash_balances(run), ["rollout_index", "month_index", "agent_id", "account_id"]),
        lots=_canonical(lots.drop("remaining_quantity"), ["rollout_index", "month_index", "lot_id"]),
        # Scoped to taxed agents because that is what both engines model. JAX also tracks
        # gains for any agent holding lots or selling, taxed or not; Rust surfaces none for
        # an untaxed agent. See docs/product_metrics.md § Capital gains without a tax
        # profile, and `test_rust_omits_capital_gains_for_an_untaxed_agent` below.
        capital_gains=_canonical(
            capital_gains_ytd(run).filter(
                pl.col("agent_id").cast(pl.String).is_in(list(taxed)) if taxed else pl.lit(value=False)
            ),
            ["rollout_index", "month_index", "agent_id", "classification"],
        ),
        tax_liabilities=_canonical(
            tax_liabilities(run), ["rollout_index", "month_index", "agent_id", "jurisdiction_id"]
        ),
        properties=_canonical(property_state(run), ["rollout_index", "month_index", "property_id"]),
        property_stakes=_canonical(property_stakes(run), ["rollout_index", "month_index", "property_id", "agent_id"]),
        liabilities=_canonical(liabilities(run), ["rollout_index", "month_index", "liability_id"]),
        rollout_status=_canonical(rollout_status(run), ["rollout_index"]),
    )


def _rust_rows(rust: dict[str, Any], channel: str) -> list[tuple[int, int, dict[str, Any]]]:
    """Every `(rollout, month, record)` in one monthly-snapshot channel."""

    return [
        (rollout["rollout_id"], snapshot["month"], record)
        for rollout in rust["rollouts"]
        for snapshot in rollout["months"]
        for record in snapshot[channel]
    ]


def run_rust(fixture: dict[str, Any]) -> RustResult:
    """Run the fixture on the Rust engine, in-process through the extension module."""

    rust = cast(dict[str, Any], json.loads(simulator.simulate_dense_json(json.dumps(fixture))))
    return rust_result(rust, fixture)


def rust_result(rust: dict[str, Any], fixture: dict[str, Any]) -> RustResult:
    """Project a raw Rust output document into the canonical schemas.

    The fixture is needed to know which accounts the scenario declared: the Rust ledger also
    carries the internal accounts a double-entry engine needs (opening equity, asset basis,
    realized gain, tax expense and liability, the external boundary), and none of those are
    cash the JAX engine models.
    """

    declared_accounts = {
        (spec["account"]["agent_id"], spec["account"]["account_id"]) for spec in fixture["scenario"]["accounts"]
    }
    cash = _sorted(
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
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "agent_id": pl.String,
            "account_id": pl.String,
            "balance_quanta": pl.Int64,
        },
        ["rollout_index", "month_index", "agent_id", "account_id"],
    )
    lots = _sorted(
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
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "lot_id": pl.String,
            "agent_id": pl.String,
            "account_id": pl.String,
            "asset_id": pl.String,
            "purchase_month_index": pl.Int64,
            "cost_basis_per_unit_quanta": pl.Int64,
            "remaining_quantity_quanta": pl.Int64,
            "quantity_scale": pl.Int64,
        },
        ["rollout_index", "month_index", "lot_id"],
    )
    capital_gains = _sorted(
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
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "agent_id": pl.String,
            "classification": pl.String,
            "gain_quanta": pl.Int64,
        },
        ["rollout_index", "month_index", "agent_id", "classification"],
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
    tax_liability_frame = _sorted(
        liability_rows,
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "agent_id": pl.String,
            "jurisdiction_id": pl.String,
            "tax_year_end_month": pl.Int64,
            "amount_owed_quanta": pl.Int64,
        },
        ["rollout_index", "month_index", "agent_id", "jurisdiction_id"],
    )
    properties = _sorted(
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
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "property_id": pl.String,
            "location_id": pl.String,
            "purchase_month_index": pl.Int64,
            "adjusted_basis_quanta": pl.Int64,
        },
        ["rollout_index", "month_index", "property_id"],
    )
    stakes = _sorted(
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
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
            "property_id": pl.String,
            "agent_id": pl.String,
            "contribution_used_quanta": pl.Int64,
            "equity_ledger_quanta": pl.Int64,
        },
        ["rollout_index", "month_index", "property_id", "agent_id"],
    )
    liability_state = _sorted(
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
                "principal_paid_ytd_quanta": record["principal_paid_ytd"],
            }
            for rollout, month, record in _rust_rows(rust, "mortgages")
            if record["active"]
        ],
        {
            "rollout_index": pl.Int64,
            "month_index": pl.Int64,
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
            "principal_paid_ytd_quanta": pl.Int64,
        },
        ["rollout_index", "month_index", "liability_id"],
    )
    status = _sorted(
        [
            {
                "rollout_index": rollout["rollout_id"],
                "status": "active" if rollout["failed_month"] is None else "failed_insufficient_cash",
                "failed_month": rollout["failed_month"],
            }
            for rollout in rust["rollouts"]
        ],
        {"rollout_index": pl.Int64, "status": pl.String, "failed_month": pl.Int64},
        ["rollout_index"],
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
    return RustResult(
        backend="rust",
        events=decode_rust_event_log(rust),
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
    )


BACKENDS = (run_jax, run_rust)


def assert_results_agree(expected: SimulationResult, actual: SimulationResult) -> None:
    """Every channel both engines answer in, plus every canonical event frame."""

    for name, frame in expected.state_channels.items():
        try:
            assert_frame_equal(actual.state_channels[name], frame, check_row_order=False, check_column_order=False)
        except AssertionError as error:
            raise AssertionError(f"state channel {name!r} differs between backends") from error
    for spec in EVENT_FRAME_SPECS:
        try:
            assert_frame_equal(actual.events.frame(spec), expected.events.frame(spec), check_row_order=False)
        except AssertionError as error:
            raise AssertionError(f"event frame {spec.name!r} differs between backends") from error


def assert_backends_agree(fixture: dict[str, Any]) -> RustResult:
    """Run the fixture on both engines and return the Rust result.

    Returning the Rust one is not a preference: it carries the same values in every shared
    channel by the time this returns, plus the journal and ledgers JAX has no counterpart
    for, so a caller needing those does not run the fixture twice.
    """

    jax_result, rust = run_jax(fixture), run_rust(fixture)
    assert_results_agree(jax_result, rust)
    return rust
