"""One result shape any engine answers in.

A behavioural suite should not know which engine produced the rows it reads. Each engine
projects a run into the channels declared here, so a suite asserts against
`SimulationResult` and inherits into a runner per engine, the way the acceptance suites do.

Each engine's projection lives beside that engine — `jax_result.py` here, `rust/result.py`
there — so a suite reading one engine's channels does not pull the other engine in behind it.
The two normalizations below are shared because both engines apply them: a zero gain and an
absent row say the same thing, and a lot holding no units says nothing about its own
acquisition. Neither is one engine bent to match the other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import polars as pl

from finance.augur.sim.events import EventLog
from finance.augur.sim.testing.case import Case


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


# What a suite parameterizes over when the property under test should hold for any engine
# rather than being a comparison between two.
type Backend = Callable[[Case], SimulationResult]


def realized_gains(frame: pl.DataFrame, taxed: set[str]) -> pl.DataFrame:
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


def held_lots(frame: pl.DataFrame) -> pl.DataFrame:
    """Blank the acquisition fields of a lot holding nothing."""

    return frame.with_columns(
        pl.when(pl.col("remaining_quantity_quanta") == 0).then(0).otherwise(pl.col(column)).alias(column)
        for column in UNHELD_LOT_PLACEHOLDERS
    )
