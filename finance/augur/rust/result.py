"""Rust's projection into the result shape every engine answers in.

`sim/testing/simulation_result.py` declares that shape and JAX's projection into it; this is
the Rust side. It lives here rather than in the differential harness so a suite reading Rust's
channels does not depend on the package whose job is comparing Rust against JAX — and so it
does not drag the JAX engine in behind it.

Forensic rather than dense: the harness wants the balanced journal, which is Rust's
double-entry invariant made checkable and has no JAX counterpart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import polars as pl

from finance.augur.rust import simulator
from finance.augur.rust.case_fixture import fixture_for
from finance.augur.rust.event_log import decode_event_log
from finance.augur.sim.scenario import Scenario
from finance.augur.sim.testing.case import Case
from finance.augur.sim.testing.simulation_result import CHANNEL, SimulationResult, held_lots

# Parts per billion, the fixture's scale for a dimensionless rate.
RATE_SCALE_PPB = 1_000_000_000


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
    lots = held_lots(lots)
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
