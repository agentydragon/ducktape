"""Random fixtures for the Rust/JAX differential fuzzer, in two tiers.

JAX bakes the plan's structure into the compiled program, so what a fixture varies decides
what it costs. `Shape` is everything that reaches the XLA cache key — which policy families
are present, how many of each, the horizon, the rollout count, and the policy thresholds and
lifecycle months JAX folds in as Python scalars. A value draw is everything the compiled
program takes as a traced input: opening balances, transfer and obligation amounts, lot
bases and quantities, sale months and units, tax brackets, and every external series.

So `build_fixture(shape, value_seed)` splits its randomness in two. `Draw.structure` is
seeded from the shape and produces identical constants for every fixture of that shape;
`Draw.value` is seeded from `value_seed` and moves only what the compiled program reads at
run time. Many value seeds over one shape therefore pay one XLA compile between them, and a
new shape pays a fresh one.

Values are biased toward rounding ties (`rounding_boundary.py`): with both engines integer
throughout, a disagreement lives at a rounding site or in an ordering, and a rounding site
only has an opinion where the exact quotient falls on the half.

Each family brings its own agents and accounts, so enabling two of them does not make one
consume the other's liquidity and turn every rollout into an early cash failure.
"""

import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from finance.augur.rust.differential.rounding_boundary import half_way_operand

# Millionths of a unit: the quantity scale every asset here uses.
QUANTITY_SCALE = 1_000_000
RATE_SCALE_PPB = 1_000_000_000
SCHEMA_VERSION = 8
FEDERAL = "federal_us"
STATE = "california"
LOCATION = "sf"


class Family(StrEnum):
    """A policy family a shape may switch on. Each brings its own agents."""

    TRANSFERS = "transfers"
    OBLIGATIONS = "obligations"
    SECURITIES = "securities"
    BONDS = "bonds"
    DISTRIBUTIONS = "distributions"
    TARGET_ALLOCATION = "target_allocation"
    HARVEST = "harvest"
    PRIVATE_EQUITY = "private_equity"
    PROPERTY = "property"
    TAX = "tax"


@dataclass(frozen=True)
class Shape:
    """The structural knobs. Two fixtures sharing a `Shape` share one compiled program."""

    name: str
    families: frozenset[Family]
    horizon_months: int
    rollout_count: int
    seed: int
    lots: int = 2
    sales: int = 1
    transfers: int = 2
    obligations: int = 1
    bonds: int = 2

    def __post_init__(self) -> None:
        if self.horizon_months < 1:
            raise ValueError(f"horizon must be positive; got {self.horizon_months=}")
        if self.rollout_count < 1:
            raise ValueError(f"rollout count must be positive; got {self.rollout_count=}")
        if Family.SECURITIES in self.families and self.sales > min(self.lots, self.horizon_months):
            raise ValueError(f"one sale per lot and per month at most; got {self.sales=} {self.lots=}")


@dataclass(frozen=True)
class Draw:
    """The two random sources, kept apart so the compile tier is visible at every call site."""

    structure: random.Random
    value: random.Random

    def money(self, low: int, high: int) -> int:
        """A cash amount, odd more often than not.

        Odd matters because the per-unit money sites divide by the quantity scale after
        multiplying by a half-unit quantity: `odd * (2k+1) * scale/2 % scale` is exactly
        `scale/2`, which is the tie.
        """

        amount = self.value.randint(low, high)
        return amount | 1 if self.value.random() < 0.6 else amount

    def half_units(self, whole_unit_budget: int) -> int:
        """A quantity that is an odd multiple of half a unit, so a per-unit divide can tie.

        Never zero and never over budget, so how many sales a shape declares cannot depend on
        the value draw — a sale dropped for want of units would move the sale count, and the
        sale count is in the compile key.
        """

        return self.value.randrange(1, 2 * max(whole_unit_budget, 1), 2) * (QUANTITY_SCALE // 2)

    def tie(self, multiplier: int, denominator: int, *, near: int, minimum: int = 1) -> int:
        """`near`, or the nearest operand that puts `multiplier / denominator` exactly on the tie.

        Half the draws stay off the boundary on purpose: a fuzzer that only ever probes ties
        stops covering the ordinary path, where an ordering difference would show instead.
        """

        if self.value.random() < 0.5:
            return max(near, minimum)
        solved = half_way_operand(multiplier, denominator, near=near, minimum=minimum)
        return max(near, minimum) if solved is None else solved


@dataclass
class _Build:
    shape: Shape
    draw: Draw
    scenario: dict[str, Any] = field(default_factory=dict)
    # series id -> one row of values per rollout.
    series: dict[str, list[list[int]]] = field(default_factory=dict)
    agents: set[str] = field(default_factory=set)

    @property
    def snapshots(self) -> int:
        return self.shape.horizon_months + 1

    def account(self, agent_id: str, account_id: str, balance: int) -> None:
        self.agents.add(agent_id)
        self.entries("accounts").append(
            {"account": {"agent_id": agent_id, "account_id": account_id}, "opening_balance": balance}
        )

    def entries(self, key: str) -> list[Any]:
        entries: list[Any] = self.scenario.setdefault(key, [])
        return entries

    def month(self) -> int:
        return self.draw.value.randrange(self.shape.horizon_months)

    def add_series(self, series_id: str, rows: list[list[int]]) -> None:
        self.series[series_id] = rows

    def path(self, *, low: int, high: int, drift: int = 0) -> list[list[int]]:
        """One positive integer path per rollout, drawn from the value source."""

        return [
            [max(1, self.draw.value.randint(low, high) + drift * month) for month in range(self.snapshots)]
            for _ in range(self.shape.rollout_count)
        ]


def _account_ref(agent_id: str, account_id: str = "checking") -> dict[str, str]:
    return {"agent_id": agent_id, "account_id": account_id}


def _series_indexed(base_amount: int, series_id: str, *, base_month: int = 0, period: int = 1) -> dict[str, Any]:
    return {
        "kind": "series_indexed",
        "base_amount": base_amount,
        "series_id": series_id,
        "base_month_index": base_month,
        "adjustment_period_months": period,
    }


def _indexed_amount(build: _Build, series_id: str, *, near: int) -> int | dict[str, Any]:
    """A fixed amount, or one indexed to `series_id` with its base solved onto the tie.

    The indexed conversion is `base_amount * level[reset] / level[base]`, so the base amount
    is the free operand: solving it against one rollout's level pair puts that month's
    conversion exactly on the half.
    """

    if build.draw.value.random() < 0.5:
        return build.draw.money(near // 2, near)
    levels = build.series[series_id][0]
    reset = build.draw.value.randrange(1, len(levels))
    return _series_indexed(build.draw.tie(levels[reset], levels[0], near=near), series_id)


def _income_tag(build: _Build) -> dict[str, str]:
    match build.draw.value.randrange(3):
        case 0:
            return {"income_category": "ordinary"}
        case 1:
            return {"deduction_category": "ordinary"}
        case _:
            return {}


def _index_series(build: _Build) -> None:
    """The `inflation` and `rent` levels every indexed amount reads.

    Month 0 is the base every conversion divides by, and it is drawn even so that a half of
    it exists at all: `base_amount * level / base` can only tie on an even base.
    """

    for series_id in ("inflation", f"rent:{LOCATION}"):
        rows = build.path(low=800_000_000, high=1_200_000_000, drift=4_000_000)
        for row in rows:
            row[0] = 1_000_000_000
        build.add_series(series_id, rows)


def _transfers(build: _Build) -> None:
    build.account("earner", "checking", build.draw.money(0, 5_000_000))
    build.entries("recurring_transfers").append(
        {
            "start_month": 0,
            "end_month": build.shape.horizon_months - 1,
            "cause_id": "salary",
            "from": _account_ref("payroll"),
            "to": _account_ref("earner"),
            "amount": _indexed_amount(build, "inflation", near=400_001),
            "income_category": "ordinary",
        }
    )
    for index in range(build.shape.transfers):
        outbound = build.draw.value.random() < 0.5
        build.entries("scheduled_transfers").append(
            {
                "month": build.month(),
                "cause_id": f"transfer-{index}",
                "from": _account_ref("earner" if outbound else "payroll"),
                "to": _account_ref("vendor" if outbound else "earner"),
                "amount": _indexed_amount(build, f"rent:{LOCATION}", near=250_001),
                **_income_tag(build),
            }
        )


def _obligations(build: _Build) -> None:
    # Deliberately thin: an obligation the agent cannot fund freezes the rollout, and both
    # engines have to agree on the month it froze, which is a channel of its own.
    build.account("payer", "checking", build.draw.money(0, 3_000_000))
    for index in range(build.shape.obligations):
        start = build.month()
        build.entries("recurring_obligations").append(
            {
                "start_month": start,
                "end_month": build.draw.value.randrange(start, build.shape.horizon_months),
                "obligation_id": f"recurring-{index}",
                "obligation_type": "rent" if index % 2 else "cash_spend",
                "from": _account_ref("payer"),
                "to": _account_ref("vendor"),
                "amount_due": _indexed_amount(build, "inflation", near=200_001),
            }
        )
    build.entries("obligations").append(
        {
            "month": build.month(),
            "obligation_id": "one-off",
            "obligation_type": "cash_spend",
            "from": _account_ref("payer"),
            "to": _account_ref("vendor"),
            "amount_due": build.draw.money(1, 2_000_000),
        }
    )


def _lot(build: _Build, *, lot_id: str, agent_id: str, account_id: str, asset_id: str, units: int) -> None:
    """One lot whose per-unit basis is odd, and whose total basis is therefore exact.

    Whole units keep `basis_per_unit * units / scale` integral, which the validator demands;
    an odd per-unit basis is what lets a later part-lot sale of a half-unit multiple land its
    basis allocation exactly on the tie.
    """

    basis_per_unit = build.draw.money(1, 40_000) | 1
    build.entries("initial_lots").append(
        {
            "lot_id": lot_id,
            "agent_id": agent_id,
            "account_id": account_id,
            "asset_id": asset_id,
            # Structural: the compiler folds each pool's FIFO order into the program.
            "purchase_month": -build.draw.structure.randrange(1, 48),
            "quantity_scale": QUANTITY_SCALE,
            "units": units,
            "basis": basis_per_unit * units // QUANTITY_SCALE,
        }
    )


def _securities(build: _Build) -> None:
    build.account("trader", "checking", build.draw.money(100_000, 5_000_000))
    build.account("trader", "brokerage", 0)
    whole_units = [build.draw.value.randrange(2, 40) * QUANTITY_SCALE for _ in range(build.shape.lots)]
    for index, units in enumerate(whole_units):
        _lot(
            build, lot_id=f"trader-vti-{index}", agent_id="trader", account_id="brokerage", asset_id="vti", units=units
        )
    prices = build.path(low=1, high=60_000)
    remaining = sum(whole_units)
    sale_months = sorted(build.draw.value.sample(range(build.shape.horizon_months), build.shape.sales))
    for index, month in enumerate(sale_months):
        # Each sale spends at most an even share of what is left, so every later sale is still
        # fundable. A sale dropped for want of units would make the sale count — and so the
        # compiled program — a function of the value seed.
        units = build.draw.half_units(remaining // (QUANTITY_SCALE * (build.shape.sales - index)))
        remaining -= units
        # The proceeds site is `price * units / scale`; units is fixed by now, so price is the
        # free operand. The legacy adapter takes one fixed sale price per scheduled sale, so
        # every rollout has to carry the same level in that month.
        tie_price = build.draw.tie(units, QUANTITY_SCALE, near=prices[0][month])
        for row in prices:
            row[month] = tie_price
        build.entries("scheduled_sales").append(
            {
                "month": month,
                "cause_id": f"sale-{index}",
                "agent_id": "trader",
                "account_id": "brokerage",
                "asset_id": "vti",
                "units": units,
                "proceeds_account_id": "checking",
            }
        )
    build.add_series("security:vti", prices)


def _bonds(build: _Build) -> None:
    build.account("bondholder", "checking", build.draw.money(0, 2_000_000))
    for index in range(build.shape.bonds):
        # A multiple of twelve million ppb, so the period rate `rate * period / 12` is exact
        # for every period below and survives the float boundary the legacy adapter still has.
        period = build.draw.structure.choice([1, 3, 6, 12])
        rate_ppb = build.draw.structure.randrange(1, 60) * 12 * 1_000_000
        indexed = build.draw.structure.random() < 0.3
        # A whole number of coupon periods: the legacy surface refuses a stub period outright,
        # since pricing one needs a day-count convention it does not have.
        periods = build.draw.structure.randrange(1, max(2, (build.shape.horizon_months + 1) // period + 1))
        maturity = periods * period - 1
        face = build.draw.tie(rate_ppb * period // 12, RATE_SCALE_PPB, near=build.draw.money(500_000, 40_000_000))
        build.entries("initial_bonds").append(
            {
                "bond_id": f"bond-{index}",
                "agent_id": "bondholder",
                "account_id": "checking",
                **({"issuer_jurisdiction_id": build.draw.structure.choice([FEDERAL, STATE])} if index % 2 else {}),
                "face_value": face,
                "purchase_price": face,
                "annual_coupon_rate_ppb": rate_ppb,
                "coupon_period_months": period,
                "inflation_indexed": indexed,
                "purchase_month_index": -1,
                "maturity_month_index": maturity,
            }
        )


def _distributions(build: _Build) -> None:
    build.account("fundholder", "checking", build.draw.money(0, 1_000_000))
    build.account("fundholder", "brokerage", 0)
    _lot(
        build,
        lot_id="fundholder-bnd",
        agent_id="fundholder",
        account_id="brokerage",
        asset_id="bnd",
        units=build.draw.value.randrange(2, 30) * QUANTITY_SCALE,
    )
    build.add_series("security:bnd", build.path(low=1, high=20_000))
    build.add_series("security_distribution:bnd", build.path(low=1, high=400))
    federal_share = build.draw.structure.randrange(0, RATE_SCALE_PPB + 1)
    build.entries("distributions").append(
        {
            "agent_id": "fundholder",
            "holding_account_id": "brokerage",
            "asset_id": "bnd",
            "to_account_id": "checking",
            "tax_character": [
                {"fraction_ppb": federal_share, "issuer_jurisdiction_id": FEDERAL},
                {"fraction_ppb": RATE_SCALE_PPB - federal_share},
            ],
        }
    )


def _target_allocation(build: _Build) -> None:
    build.account("allocator", "checking", build.draw.money(1_000_000, 30_000_000))
    build.account("allocator", "brokerage-a", 0)
    build.account("allocator", "brokerage-b", 0)
    for asset, account in (("vti", "brokerage-a"), ("bnd", "brokerage-b")):
        _lot(
            build,
            lot_id=f"allocator-{asset}",
            agent_id="allocator",
            account_id=account,
            asset_id=asset,
            units=build.draw.value.randrange(1, 50) * QUANTITY_SCALE,
        )
    for asset in ("vti", "bnd"):
        build.series.setdefault(f"security:{asset}", build.path(low=1, high=30_000))
    floor = build.draw.structure.randrange(0, 4_000_000)
    build.entries("target_allocation_policies").append(
        {
            "agent_id": "allocator",
            "account_id": "checking",
            "source_account_ids": ["brokerage-a", "brokerage-b"],
            "sleeves": [
                {"asset_id": "vti", "weight": build.draw.value.randrange(1, 5), "quantity_scale": QUANTITY_SCALE},
                {"asset_id": "bnd", "weight": build.draw.value.randrange(1, 5), "quantity_scale": QUANTITY_SCALE},
            ],
            "cash_floor": floor,
            "cash_ceiling": floor + build.draw.structure.randrange(1, 4_000_000),
            "cause_id_prefix": "fuzz-allocation",
            "purchase_slots_per_sleeve": build.draw.structure.randrange(1, 4),
            "rebalance_tolerance_ppb": build.draw.structure.randrange(1, 10) * 100_000_000,
        }
    )


def _harvest(build: _Build) -> None:
    build.account("harvester", "checking", build.draw.money(0, 2_000_000))
    build.account("harvester", "brokerage", 0)
    _lot(
        build,
        lot_id="harvester-sp500",
        agent_id="harvester",
        account_id="brokerage",
        asset_id="sp500",
        units=build.draw.value.randrange(1, 40) * QUANTITY_SCALE,
    )
    build.add_series("security:sp500", build.path(low=1, high=1_000))
    peak = build.draw.structure.randrange(1, 300) * 1_000_000
    build.entries("harvest_policies").append(
        {
            "owner_agent_id": "harvester",
            "account_id": "brokerage",
            "asset_id": "sp500",
            "peak_annual_yield_ppb": peak,
            "floor_annual_yield_ppb": build.draw.structure.randrange(0, peak + 1),
            "maturity_decay_exponent_ppb": build.draw.structure.randrange(1, 6) * (RATE_SCALE_PPB // 2),
            "drawdown_sensitivity_ppb": build.draw.structure.randrange(0, 12) * RATE_SCALE_PPB,
            "short_term_fraction_ppb": build.draw.structure.randrange(0, RATE_SCALE_PPB + 1),
        }
    )


def _private_equity(build: _Build) -> None:
    build.account("pe_owner", "checking", build.draw.money(0, 2_000_000))
    build.account("pe_owner", "private", 0)
    for index in range(2):
        _lot(
            build,
            lot_id=f"pe-acme-{index}",
            agent_id="pe_owner",
            account_id="private",
            asset_id="private_equity:acme",
            units=build.draw.value.randrange(1, 60) * QUANTITY_SCALE,
        )
    build.add_series("private_equity_mark:acme", build.path(low=1, high=30_000))
    for channel, low, high in (
        ("regime", 1, 4),
        ("event_kind", 0, 7),
        ("sale_opportunity", 0, 1),
        ("sale_capacity", 0, RATE_SCALE_PPB),
        ("eligible", 0, RATE_SCALE_PPB),
        ("forced_sale", 0, RATE_SCALE_PPB),
        ("liquidity_blocked", 0, 1),
        ("forced_recovery", 0, 4_000_000),
        ("company_valuation", 0, 0),
    ):
        build.add_series(
            f"private_equity_{channel}:acme",
            [
                [build.draw.value.randint(low, high) for _ in range(build.snapshots)]
                for _ in range(build.shape.rollout_count)
            ],
        )
    build.entries("private_equity_tender_policies").append(
        {
            "owner_agent_id": "pe_owner",
            "proceeds_account_id": "checking",
            "liquid_net_worth_floor": build.draw.structure.randrange(0, 8_000_000),
        }
    )


def _property(build: _Build) -> None:
    build.account("homeowner", "checking", build.draw.money(20_000_000, 90_000_000))
    build.account("seller", "checking", 0)
    build.account("bank", "checking", 0)
    build.account("county", "checking", 0)
    build.account("tenant", "checking", 50_000_000)
    build.entries("locations").append(
        {
            "location_id": LOCATION,
            "display_name": "Fuzz City",
            "jurisdiction_ids": [FEDERAL, STATE],
            "annual_property_tax_rate_ppb": build.draw.structure.randrange(1, 20) * 1_000_000,
            "annual_special_assessment": build.draw.structure.randrange(0, 50_000),
        }
    )
    build.add_series(f"home_value:{LOCATION}", build.path(low=10_000_000, high=90_000_000))
    # Every scalar below is folded into the compiled program, so all of it is structural.
    # The price is a whole dollar and the land share a whole percent, together keeping
    # `price * (1 - land_share)` — the building basis JAX depreciates — on a whole quantum,
    # which is the only form the legacy surface accepts for a configured amount.
    price = build.draw.structure.randrange(100_000, 800_000) * 100
    principal = build.draw.structure.randrange(0, price)
    purchase_month = build.draw.structure.randrange(0, max(1, build.shape.horizon_months // 2))
    build.entries("scheduled_property_purchases").append(
        {
            "month": purchase_month,
            "cause_id": "buy-home",
            "property_id": "home",
            "location_id": LOCATION,
            "buyer_agent_id": "homeowner",
            "buyer_account_id": "checking",
            "seller_agent_id": "seller",
            "seller_account_id": "checking",
            "purchase_price": price,
            "down_payment": price - principal,
            "buyer_closing_cost": build.draw.structure.randrange(0, 2_000_000),
            "rented_fraction_ppb": build.draw.structure.randrange(0, RATE_SCALE_PPB + 1),
            "land_value_fraction_ppb": build.draw.structure.randrange(0, 100) * (RATE_SCALE_PPB // 100),
            **(
                {
                    "mortgage": {
                        "liability_id": "home-mortgage",
                        "lender_agent_id": "bank",
                        "lender_account_id": "checking",
                        "principal": principal,
                        "annual_interest_rate_ppb": build.draw.structure.randrange(0, 12) * 12_000_000,
                        "term_months": build.draw.structure.randrange(12, 361),
                    }
                }
                if principal > 0
                else {}
            ),
        }
    )
    build.entries("property_tax_policies").append(
        {
            "property_id": "home",
            "owner_agent_id": "homeowner",
            "from_account_id": "checking",
            "tax_authority_agent_id": "county",
            "tax_authority_account_id": "checking",
            "annual_tax_rate_ppb": build.draw.structure.randrange(1, 24) * 1_000_000,
            "start_month": purchase_month,
        }
    )
    build.entries("recurring_property_cashflows").append(
        {
            "start_month": purchase_month,
            "end_month": build.shape.horizon_months - 1,
            "property_id": "home",
            "cause_id": "rent",
            "from": _account_ref("tenant"),
            "to": _account_ref("homeowner"),
            "amount": _indexed_amount(build, f"rent:{LOCATION}", near=500_001),
            "income_category": "ordinary",
        }
    )
    if purchase_month + 1 < build.shape.horizon_months:
        rest = range(purchase_month + 1, build.shape.horizon_months)
        build.entries("property_rented_fraction_events").append(
            {
                "month": build.draw.structure.choice(rest),
                "property_id": "home",
                "rented_fraction_ppb": build.draw.structure.randrange(0, RATE_SCALE_PPB + 1),
            }
        )
        build.entries("capital_improvement_events").append(
            {
                "month": build.draw.structure.choice(rest),
                "property_id": "home",
                "amount": build.draw.structure.randrange(1, 3_000_000),
                "description": "improvement",
            }
        )
        if build.draw.structure.random() < 0.6:
            build.entries("property_sales").append(
                {
                    "month": build.draw.structure.choice(rest),
                    "property_id": "home",
                    "closing_cost_bps": build.draw.structure.randrange(0, 1_000),
                }
            )
    if principal > 0:
        build.entries("mortgage_interest_deduction_policies").append(
            {"liability_id": "home-mortgage", "owner_agent_id": "homeowner", "debt_class": "acquisition"}
        )
    if build.draw.structure.random() < 0.5:
        build.entries("initial_primary_residences").append({"agent_id": "homeowner", "property_id": "home"})


def _brackets(build: _Build, count: int) -> list[dict[str, Any]]:
    # Sampled rather than drawn independently: both engines refuse bracket edges that are not
    # strictly increasing, and a duplicate would read as one engine refusing a fixture the
    # other ran — a finding, and a wrong one.
    uppers = sorted(build.draw.value.sample(range(100_000, 20_000_000), count - 1))
    rates = sorted(build.draw.value.randrange(0, 400) * 1_000_000 for _ in range(count))
    return [{"upper": upper, "rate_ppb": rate} for upper, rate in zip(uppers, rates[:-1], strict=True)] + [
        {"upper": None, "rate_ppb": rates[-1]}
    ]


def _tax(build: _Build) -> None:
    build.account("irs", "checking", 0)
    taxed = sorted(build.agents & {"earner", "trader", "bondholder", "fundholder", "harvester", "homeowner", "payer"})
    for agent_id in taxed:
        jurisdictions = [
            {
                "jurisdiction_id": FEDERAL,
                "ordinary_brackets": _brackets(build, build.draw.structure.randrange(1, 4)),
                "long_term_capital_gain_brackets": _brackets(build, 2),
                "standard_deduction": build.draw.money(0, 2_000_000),
                "max_capital_loss_ordinary_offset": build.draw.structure.randrange(0, 500_000),
                "exempt_interest_from_levels": ["state"],
                "exempts_own_issue": False,
                "section_1250_rate_ppb": build.draw.structure.randrange(0, 30) * 10_000_000,
            }
        ]
        if build.draw.structure.random() < 0.5:
            jurisdictions.append(
                {
                    "jurisdiction_id": STATE,
                    "ordinary_brackets": _brackets(build, build.draw.structure.randrange(1, 4)),
                    "long_term_capital_gain_brackets": [],
                    "standard_deduction": build.draw.money(0, 1_000_000),
                    "max_capital_loss_ordinary_offset": build.draw.structure.randrange(0, 500_000),
                    "exempt_interest_from_levels": ["federal"],
                    "exempts_own_issue": True,
                    "section_1250_rate_ppb": 0,
                }
            )
        # Whether a profile owes estimated tax is structural: it decides whether the compiler
        # emits the quarterly obligation slots at all. How much is traced, and its tie is any
        # prior-year tax congruent to 2 mod 4, the quarter being `prior_year_tax / 4`.
        build.entries("tax_profiles").append(
            {
                "agent_id": agent_id,
                "tax_authority_agent_id": "irs",
                "prior_year_tax": (
                    build.draw.tie(1, 4, near=build.draw.money(1, 4_000_000))
                    if build.draw.structure.random() < 0.7
                    else 0
                ),
                "section_121_exclusion": build.draw.structure.randrange(0, 30_000_000),
                "jurisdictions": jurisdictions,
            }
        )
    if taxed and build.draw.structure.random() < 0.5:
        build.entries("federal_salt_deduction_policies").append(
            {
                "profile_id": taxed[0],
                "federal_jurisdiction_id": FEDERAL,
                "cap_schedule": [
                    {"effective_year_index": year, "cap": build.draw.structure.randrange(0, 5_000_000)}
                    for year in range(build.shape.horizon_months // 12 + 1)
                ],
            }
        )


# Order matters: the index series exist before any amount indexes them, and the tax family
# reads the agent set the earlier families built.
_FAMILY_BUILDERS = (
    (Family.TRANSFERS, _transfers),
    (Family.OBLIGATIONS, _obligations),
    (Family.SECURITIES, _securities),
    (Family.BONDS, _bonds),
    (Family.DISTRIBUTIONS, _distributions),
    (Family.TARGET_ALLOCATION, _target_allocation),
    (Family.HARVEST, _harvest),
    (Family.PRIVATE_EQUITY, _private_equity),
    (Family.PROPERTY, _property),
    (Family.TAX, _tax),
)


def build_fixture(shape: Shape, value_seed: int) -> dict[str, Any]:
    """One fixture: `shape` fixes the compiled program, `value_seed` moves its traced inputs."""

    build = _Build(shape=shape, draw=Draw(structure=random.Random(shape.seed), value=random.Random(value_seed)))
    build.scenario["horizon_months"] = shape.horizon_months
    build.scenario["jurisdictions"] = [
        {"jurisdiction_id": FEDERAL, "level": "federal"},
        {"jurisdiction_id": STATE, "level": "state"},
    ]
    # Rust defaults an absent scenario list to empty; the legacy adapter indexes these seven
    # directly, so an empty list stands in for the omission it treats as equivalent.
    for key in ("scheduled_transfers", "recurring_transfers", "obligations", "initial_lots", "scheduled_sales"):
        build.entries(key)
    build.account("payroll", "checking", 0)
    build.account("vendor", "checking", 0)
    _index_series(build)
    for family, builder in _FAMILY_BUILDERS:
        if family in shape.families:
            builder(build)
    return {
        "schema_version": SCHEMA_VERSION,
        "currency_code": "USD",
        "currency_quantum": "0.01",
        "rollout_count": shape.rollout_count,
        "scenario": build.scenario,
        "series": [
            {"series_id": series_id, "snapshots": build.snapshots, "values": [value for row in rows for value in row]}
            for series_id, rows in sorted(build.series.items())
        ],
    }


# The families a structural draw may combine. Property and private equity carry the most
# folded-in structure, so they are drawn less often than the cheap families.
_STRUCTURAL_WEIGHTS: tuple[tuple[Family, float], ...] = (
    (Family.TRANSFERS, 0.8),
    (Family.OBLIGATIONS, 0.7),
    (Family.SECURITIES, 0.8),
    (Family.BONDS, 0.5),
    (Family.DISTRIBUTIONS, 0.4),
    (Family.TARGET_ALLOCATION, 0.4),
    (Family.HARVEST, 0.3),
    (Family.PRIVATE_EQUITY, 0.3),
    (Family.PROPERTY, 0.35),
    (Family.TAX, 0.6),
)


# The shapes the value tier holds fixed: one per cluster of policy families, so the set
# covers the surface without the combinatorial compile bill of covering it pair by pair.
VALUE_TIER_SHAPES = (
    Shape(
        name="securities",
        families=frozenset({Family.SECURITIES, Family.TAX}),
        horizon_months=14,
        rollout_count=2,
        seed=1,
        lots=3,
        sales=3,
    ),
    Shape(
        name="cashflow",
        families=frozenset({Family.TRANSFERS, Family.OBLIGATIONS, Family.TAX}),
        horizon_months=14,
        rollout_count=2,
        seed=2,
        transfers=3,
        obligations=2,
    ),
    Shape(
        name="markets",
        families=frozenset({Family.BONDS, Family.DISTRIBUTIONS, Family.TARGET_ALLOCATION, Family.HARVEST}),
        horizon_months=13,
        rollout_count=2,
        seed=3,
        bonds=3,
    ),
    Shape(
        name="property",
        families=frozenset({Family.PROPERTY, Family.TRANSFERS, Family.TAX}),
        horizon_months=25,
        rollout_count=2,
        seed=4,
    ),
)


def random_shape(seed: int) -> Shape:
    """A structural draw. Each one costs a fresh XLA compile, so callers run few of them."""

    rng = random.Random(seed)
    families = {family for family, weight in _STRUCTURAL_WEIGHTS if rng.random() < weight}
    # A shape with nothing in it still runs, but it says nothing; give it the cheapest family.
    families.add(Family.TRANSFERS)
    lots = rng.randrange(1, 4)
    return Shape(
        name=f"random-{seed}",
        families=frozenset(families),
        horizon_months=rng.choice([3, 6, 13, 25]),
        rollout_count=rng.randrange(1, 4),
        seed=seed,
        lots=lots,
        sales=rng.randrange(0, lots + 1),
        transfers=rng.randrange(0, 3),
        obligations=rng.randrange(0, 3),
        bonds=rng.randrange(1, 4),
    )
