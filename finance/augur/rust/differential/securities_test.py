"""Rust/JAX differential coverage for security distributions and held-to-maturity nominal
bonds and TIPS.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from decimal import Decimal
from typing import Any

import polars as pl
import pytest_bazel

from finance.augur.model.series import InflationKey, SecurityDistributionKey, SecuritySymbol
from finance.augur.rust.differential.backend import assert_backends_agree
from finance.augur.sim.scenario import BondHolding, DistributionTaxSlice, InitialLot, SecurityDistribution
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import BND, VTI, checking, taxed

VTI_DISTRIBUTION = SecurityDistributionKey(symbol=SecuritySymbol("vti"))
BND_DISTRIBUTION = SecurityDistributionKey(symbol=SecuritySymbol("bnd"))
INFLATION = InflationKey()


def distribution_case() -> Case:
    """A monthly payout on a held position, over two rollouts with different payout paths."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0))),
            horizon_months=3,
            tax_profiles=[],
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=VTI,
                    purchase_month_index=-12,
                    quantity=2.0,
                    cost_basis_per_unit=Decimal(100),
                )
            ],
            security_distributions=[
                SecurityDistribution(
                    asset=VTI,
                    agent_id="alice",
                    holding_account_id="brokerage",
                    to_account_id="checking",
                    tax_character=(DistributionTaxSlice(fraction=1.0),),
                )
            ],
        ),
        rollout_count=2,
        series={
            VTI: levels([[Decimal(100)] * 4] * 2),
            VTI_DISTRIBUTION: levels(
                [[Decimal(1), Decimal(1), Decimal(1), Decimal(1)], [Decimal(2), Decimal(3), Decimal(4), Decimal(5)]]
            ),
        },
    )


def distribution_tax_case() -> Case:
    """A fund payout split across issuers, each slice routed by its own exemption policy."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=12,
            initial_lots=[
                InitialLot(
                    lot_id="alice-bnd",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=BND,
                    purchase_month_index=-24,
                    quantity=10_000.0,
                    cost_basis_per_unit=Decimal(73),
                )
            ],
            security_distributions=[
                SecurityDistribution(
                    asset=BND,
                    agent_id="alice",
                    holding_account_id="brokerage",
                    to_account_id="checking",
                    tax_character=(
                        DistributionTaxSlice(fraction=0.4, issuer_jurisdiction_id="federal_us"),
                        DistributionTaxSlice(fraction=0.6),
                    ),
                )
            ],
            tax_profiles=[taxed("alice", "federal_us", "california")],
        ),
        rollout_count=2,
        series={
            BND: levels([[Decimal(73)] * 13] * 2),
            BND_DISTRIBUTION: levels([[Decimal("0.20")] * 13, [Decimal("0.30")] * 13]),
        },
    )


def _bond(bond_id: str, *, rate: float, issuer: str | None = None, **overrides: Any) -> BondHolding:
    """One $100,000 par bond bought a month before the horizon and held to maturity."""

    return BondHolding(
        **{
            "bond_id": bond_id,
            "agent_id": "alice",
            "account_id": "checking",
            "issuer_jurisdiction_id": issuer,
            "face_value": Decimal(100_000),
            "purchase_price": Decimal(100_000),
            "annual_coupon_rate": rate,
            "coupon_period_months": 6,
            "purchase_month_index": -1,
            "maturity_month_index": 11,
            **overrides,
        }
    )


def bond_case() -> Case:
    """Nominal bonds, TIPS, and the rounding edges of a once-per-period rational coupon.

    The three rollouts inflate, inflate less, and deflate, so the TIPS accretes both ways and
    its deflation floor is exercised.
    """

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=12,
            initial_bonds=[
                _bond("treasury", rate=0.05, issuer="federal_us"),
                _bond("california-muni", rate=0.04, issuer="california"),
                _bond("corporate", rate=0.03),
                _bond("tips", rate=0.04, issuer="federal_us", inflation_indexed=True),
                _bond(
                    "rounding-up", rate=0.01, face_value=Decimal(6), purchase_price=Decimal(6), coupon_period_months=1
                ),
                _bond(
                    "rounding-down",
                    rate=0.033333333,
                    face_value=Decimal("1.80"),
                    purchase_price=Decimal("1.80"),
                    coupon_period_months=1,
                ),
                _bond(
                    "rounding-five-month",
                    rate=0.037,
                    face_value=Decimal("12506.27"),
                    purchase_price=Decimal("12506.27"),
                    coupon_period_months=5,
                    purchase_month_index=-4,
                ),
            ],
            tax_profiles=[taxed("alice", "federal_us", "california")],
        ),
        rollout_count=3,
        series={
            INFLATION: levels(
                [
                    [*([Decimal(1)] * 6), *([Decimal(2)] * 7)],
                    [*([Decimal(1)] * 6), *([Decimal("1.5")] * 7)],
                    [*([Decimal(1)] * 6), *([Decimal("0.8")] * 7)],
                ]
            )
        },
    )


def test_backends_agree_on_monthly_security_distributions() -> None:
    """A distribution pays on the units held that month, so a growing position grows it."""

    result = assert_backends_agree(distribution_case())

    by_rollout = result.distributions.sort("rollout_index", "month_index")
    assert by_rollout.filter(pl.col("rollout_index") == 0).get_column("amount_quanta").to_list() == [200, 200, 200]
    assert by_rollout.filter(pl.col("rollout_index") == 1).get_column("amount_quanta").to_list() == [400, 600, 800]


def test_backends_agree_on_distribution_tax_character_slices() -> None:
    """Each issuer slice is routed through its jurisdiction's interest-exemption policy."""

    assert_backends_agree(distribution_tax_case())


def test_backends_agree_on_nominal_bonds_tips_and_issuer_tax_routing() -> None:
    result = assert_backends_agree(bond_case())
    flows = result.bond_cashflows

    def flow(rollout: int, bond_id: str, month: int) -> dict[str, Any]:
        return flows.filter(
            (pl.col("rollout_index") == rollout) & (pl.col("bond_id") == bond_id) & (pl.col("month_index") == month)
        ).to_dicts()[0]

    # Treasury and muni coupons carry their issuer; a corporate bond has none.
    assert flow(0, "treasury", 5)["coupon_quanta"] == 250_000
    assert flow(0, "treasury", 5)["issuer_jurisdiction_id"] == "federal_us"
    assert flow(0, "california-muni", 5)["coupon_quanta"] == 200_000
    assert flow(0, "corporate", 5)["coupon_quanta"] == 150_000
    assert flow(0, "corporate", 5)["issuer_jurisdiction_id"] is None

    # TIPS: phantom accretion while CPI rises, par redemption at maturity.
    assert flow(0, "tips", 6)["accretion_quanta"] == 10_000_000
    assert flow(0, "tips", 6)["issuer_jurisdiction_id"] == "federal_us"
    assert flow(0, "tips", 11)["coupon_quanta"] == 400_000
    assert flow(0, "tips", 11)["redemption_quanta"] == 20_000_000
    assert flow(1, "tips", 6)["accretion_quanta"] == 5_000_000
    assert flow(1, "tips", 11)["redemption_quanta"] == 15_000_000
    # A deflating path accretes negatively but the deflation floor holds redemption at par.
    assert flow(2, "tips", 6)["accretion_quanta"] == -2_000_000
    assert flow(2, "tips", 11)["coupon_quanta"] == 160_000
    assert flow(2, "tips", 11)["redemption_quanta"] == 10_000_000

    # Rounding edges of the once-per-period rational coupon.
    rounding_up = flows.filter((pl.col("rollout_index") == 0) & (pl.col("bond_id") == "rounding-up"))
    assert rounding_up.get_column("coupon_quanta").to_list() == [1] * 12
    assert flow(0, "rounding-down", 11)["coupon_quanta"] == 0
    assert [flow(0, "rounding-five-month", month)["coupon_quanta"] for month in (1, 6, 11)] == [19_280] * 3

    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


if __name__ == "__main__":
    pytest_bazel.main()
