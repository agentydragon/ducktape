"""A household's investment strategy, declared so that no decision is left to a default.

`lower` turns a declaration into the `CashBandHousehold` (<../policy/cash_band_household.py>)
that carries it out; the declaration itself neither observes nor trades.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from math import lcm
from types import MappingProxyType

from finance.augur.policy import cash_band_household
from finance.augur.policy.cash_band import validate_band_bounds
from finance.augur.sim.ids import AccountId, AgentId, AssetId, PortfolioId


@dataclass(frozen=True, kw_only=True)
class SecuritySleeve:
    """One public security's lots across the household's source accounts, traded in whole units."""

    asset_id: AssetId


@dataclass(frozen=True, kw_only=True)
class ManagedSleeve:
    """One managed portfolio, traded in money: it has a value but no units."""

    portfolio_id: PortfolioId


type Sleeve = SecuritySleeve | ManagedSleeve


def _canonical(entry: tuple[Sleeve, Fraction]) -> tuple[int, str]:
    sleeve, _ = entry
    return (0, sleeve.asset_id) if isinstance(sleeve, SecuritySleeve) else (1, sleeve.portfolio_id)


@dataclass(frozen=True, kw_only=True)
class TargetAllocation:
    """Each sleeve's weight: the constant fraction of the sleeves' combined value it targets.

    Weights are exact and sum to exactly 1: build them from integers or decimal strings
    (`Fraction(1, 3)`, `Fraction("0.6")`), never from floats. A zero weight sells the sleeve
    down: sales take it first and it receives no purchases. Holdings no sleeve names are
    never sold.
    """

    weights: Mapping[Sleeve, Fraction]

    def __post_init__(self) -> None:
        if not self.weights:
            raise ValueError(
                "an empty allocation names nothing the household may sell, "
                "so the first claim its cash cannot cover stops the path"
            )
        for sleeve, weight in self.weights.items():
            if not isinstance(sleeve, SecuritySleeve | ManagedSleeve) or not isinstance(weight, Fraction):
                raise TypeError(f"an allocation maps sleeves to exact Fraction weights; got {sleeve=}, {weight=}")
            if weight < 0:
                raise ValueError(f"target weights must be nonnegative; got {sleeve=}, {weight=}")
        if (total := sum(self.weights.values())) != 1:
            raise ValueError(f"target weights must sum to exactly 1; got {total=}")
        # Mapping equality ignores order, but the household breaks ties, and sells down several zero
        # weights, in sleeve order: one canonical order makes equal allocations lower identically. The
        # copy also keeps a later edit to the caller's dict out of a validated allocation.
        object.__setattr__(self, "weights", MappingProxyType(dict(sorted(self.weights.items(), key=_canonical))))


# Every allocation variant a strategy accepts: dispatch on this, not on `TargetAllocation`.
type Allocation = TargetAllocation


@dataclass(frozen=True, kw_only=True)
class DriftBand:
    """Rebalance on drift alone, in months the cash band holds.

    Once any sleeve strays from its target by `tolerance_ppb` parts per billion of that
    target (250_000_000 is 25%), every sleeve is traded back toward its target. A zero-weight
    sleeve holding anything has always strayed, so the first such month exits it.
    """

    tolerance_ppb: int

    def __post_init__(self) -> None:
        if not isinstance(self.tolerance_ppb, int) or isinstance(self.tolerance_ppb, bool):
            raise TypeError(f"a drift tolerance is an integer count of parts per billion; got {self.tolerance_ppb=}")
        if self.tolerance_ppb < 0:
            raise ValueError(f"a drift tolerance must be nonnegative; got {self.tolerance_ppb=}")


@dataclass(frozen=True)
class CashflowOnly:
    """Drift alone never trades; the band's sales take overweight sleeves first and its purchases underweight ones."""


type Rebalancing = DriftBand | CashflowOnly


@dataclass(frozen=True, kw_only=True)
class ReinvestSurplus:
    """Cash the month's claims leave above the ceiling is invested down to the floor, underweight sleeves first.

    Rebalancing is chosen here because correcting drift buys the underweight sleeves; a
    household that never buys can only sell toward its target.
    """

    rebalancing: Rebalancing

    def __post_init__(self) -> None:
        if not isinstance(self.rebalancing, DriftBand | CashflowOnly):
            raise TypeError(f"choose DriftBand or CashflowOnly explicitly; got {self.rebalancing=}")


@dataclass(frozen=True, kw_only=True)
class AccumulateCash:
    """Nothing is ever bought: cash above the band's ceiling stays idle, and drift alone never trades.

    Results under this strategy are sales-only and must be reported as such; `reason` says why.
    """

    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("accumulating cash needs a nonblank reason")


type Reinvestment = ReinvestSurplus | AccumulateCash


@dataclass(frozen=True, kw_only=True)
class CashBand:
    """Cash held between `floor` and `ceiling`, in nominal integer currency quanta.

    The ceiling is the refill target: a month whose claims would leave cash below the floor
    sells to bring it back up to the ceiling. Above the ceiling, a reinvesting strategy
    invests down to the floor. The bounds do not follow inflation.
    """

    floor: int
    ceiling: int

    def __post_init__(self) -> None:
        if any(not isinstance(bound, int) or isinstance(bound, bool) for bound in (self.floor, self.ceiling)):
            raise TypeError(f"cash band bounds are integer currency quanta; got {self.floor=}, {self.ceiling=}")
        validate_band_bounds(floor=self.floor, ceiling=self.ceiling)


@dataclass(frozen=True, kw_only=True)
class Strategy:
    """What a household decides about its investments; who acts, and from which accounts, is given to `lower`."""

    allocation: Allocation
    reinvestment: Reinvestment
    cash_band: CashBand

    def __post_init__(self) -> None:
        if not isinstance(self.reinvestment, ReinvestSurplus | AccumulateCash):
            raise TypeError(f"choose ReinvestSurplus or AccumulateCash explicitly; got {self.reinvestment=}")


def lower(
    strategy: Strategy,
    *,
    agent_id: AgentId,
    cash_account_id: AccountId,
    source_account_ids: tuple[AccountId, ...],
    cause_id_prefix: str,
) -> cash_band_household.CashBandHousehold:
    """The household that runs `strategy` on `cash_account_id`, selling from `source_account_ids` in order.

    Purchases land in the first source account. `check` the result against its world before tracking it.
    """
    weights = strategy.allocation.weights
    # The relative integer weights `policy/sleeves.py` counts: numerators over the common denominator.
    scale = lcm(*(weight.denominator for weight in weights.values()))
    reinvestment = strategy.reinvestment
    return cash_band_household.CashBandHousehold(
        agent_id,
        cash_account_id=cash_account_id,
        floor=strategy.cash_band.floor,
        ceiling=strategy.cash_band.ceiling,
        sleeves=tuple(
            cash_band_household.SecuritySleeve(asset_id=sleeve.asset_id, weight=int(weight * scale))
            if isinstance(sleeve, SecuritySleeve)
            else cash_band_household.ManagedSleeve(portfolio_id=sleeve.portfolio_id, weight=int(weight * scale))
            for sleeve, weight in weights.items()
        ),
        source_account_ids=source_account_ids,
        reinvest=None
        if isinstance(reinvestment, AccumulateCash)
        else cash_band_household.Reinvest(
            rebalance_tolerance_ppb=reinvestment.rebalancing.tolerance_ppb
            if isinstance(reinvestment.rebalancing, DriftBand)
            else None
        ),
        cause_id_prefix=cause_id_prefix,
    )
