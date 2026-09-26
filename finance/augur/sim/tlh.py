"""A reduced-form approximation of a direct-indexing account's tax-loss harvesting.

Losses are modeled assumptions, not reconstructed constituent sales: no constituent
market, wash-sale rule (within or across accounts) or provider's actual harvested
holding periods is simulated, and no forecast is calibrated. Each cohort's adjusted
basis falls by its modeled loss, and later redemptions use that same basis.

The component neither assesses tax nor moves household cash; its caller settles the
returned effects. For a contribution, redemption or modeled harvest those satisfy

    cash received by the household + change in reported tax basis
        = realized short-term gain + realized long-term gain

and a distribution adds its declared income character on the income side. Money and
prices are integer currency quanta; exposure is exact (see `_Cohort`).
"""

from dataclasses import dataclass, replace
from fractions import Fraction
from math import isqrt
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, rate_to_ppb
from finance.augur.sim.money import round_ratio


def _nonnegative(**amounts: int) -> None:
    for name, amount in amounts.items():
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise TypeError(f"{name} must be an integer quantum count")
        if amount < 0:
            raise ValueError(f"{name} must be nonnegative")


class TlhAssumptions(BaseModel):
    """The gross-loss curve and the modeled short-term fraction of harvested losses.

    Heuristic gross-loss yields, not forecasts of after-tax alpha or tax savings;
    the household's tax accounting decides what a loss is worth.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    peak_annual_yield: float = Field(ge=0)
    floor_annual_yield: float = Field(ge=0)
    maturity_decay_exponent: float = Field(gt=0, multiple_of=0.5)
    drawdown_sensitivity: float = Field(ge=0)
    short_term_fraction: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _validate_curve(self) -> Self:
        if self.floor_annual_yield > self.peak_annual_yield:
            raise ValueError("floor_annual_yield must not exceed peak_annual_yield")
        for rate in (self.peak_annual_yield, self.floor_annual_yield, self.drawdown_sensitivity):
            if rate * MONEY_FACTOR_SCALE >= 1 << 63:
                raise ValueError("TLH rates must fit signed 64-bit parts per billion")
        return self

    def monthly_loss_fraction(self, *, embedded_gain_ppb: int, drawdown_ppb: int) -> int:
        """Monthly gross-loss fraction in PPB; the position's basis caps the loss."""

        scale = MONEY_FACTOR_SCALE
        if not 0 <= embedded_gain_ppb <= scale or drawdown_ppb < 0:
            raise ValueError("embedded gain must be in [0, 1]; drawdown must be nonnegative")
        base = scale - embedded_gain_ppb
        half_exponent = int(self.maturity_decay_exponent * 2)
        maturity = scale
        for _ in range(half_exponent // 2):
            maturity = round_ratio(maturity * base, scale)
        if half_exponent % 2:
            maturity = round_ratio(maturity * isqrt(base * scale), scale)
        floor = rate_to_ppb(self.floor_annual_yield)
        annual = floor + round_ratio((rate_to_ppb(self.peak_annual_yield) - floor) * maturity, scale)
        monthly = round_ratio(annual, 12)
        drawdown_multiplier = scale + round_ratio(rate_to_ppb(self.drawdown_sensitivity) * drawdown_ppb, scale)
        return round_ratio(monthly * drawdown_multiplier, scale)


@dataclass(frozen=True)
class TlhOpeningCohort:
    """One tax lot as a direct-indexing statement reports it: value at the opening mark, adjusted basis and
    purchase month. Imported facts, not a reconstruction of past modeled harvesting. A lot reported at zero
    value holds no exposure and keeps its basis until liquidation."""

    value: int
    reported_tax_basis: int
    purchase_month: int


@dataclass(frozen=True)
class TlhOpening:
    """State before the next advance; opening cohorts may enter that next month.

    An empty portfolio is valid: it can take a first contribution without an invented opening position.
    """

    month: int
    price: int
    cohorts: tuple[TlhOpeningCohort, ...]


@dataclass(frozen=True)
class TlhObservation:
    value: int
    reported_tax_basis: int


@dataclass(frozen=True)
class TlhMarketUpdate:
    month: int
    price: int


@dataclass(frozen=True)
class ModeledRealizations:
    """Signed taxable gains; negative values are losses. No tax payment is implied."""

    short_term_gain: int = 0
    long_term_gain: int = 0


@dataclass(frozen=True)
class WithdrawalResult:
    cash_received: int
    realizations: ModeledRealizations


@dataclass(frozen=True)
class _Cohort:
    # Exposure is counted in units of the index level, so it is worth exactly `exposure * price` at
    # any mark and rides the index ratio without rounding. Money is rounded only where it leaves.
    exposure: Fraction
    basis: int
    purchase_month: int


def _money(amount: Fraction) -> int:
    return round_ratio(amount.numerator, amount.denominator)


class TlhPortfolio:
    """One rollout's opaque holdings and harvesting memory, denominated in money.

    Advance once a month, before the owner observes it and before any contribution
    or redemption that month, scheduled ones included. Month zero uses the opening
    mark as its previous mark, so its drawdown is zero, but it still takes a baseline
    harvest on opening cohorts; their pre-simulation history is not replayed. A
    contribution made after the advance first harvests the following month.

    A contribution of X becomes exposure worth exactly X at the current mark; a
    withdrawal of X sells exactly X of exposure, FIFO, each cohort giving up basis in
    proportion to the value it sells. Nothing is rounded to a share grid and nothing
    is kept back as cash, so there is no grid for a policy to size against. A caller
    needing transactional settlement operates on a deepcopy and adopts it only when
    the accounting engine accepts its financial effects.

    Sale character uses Augur's monthly holding-period convention (12 months is
    long-term); the harvested character is the assumptions' short-term fraction.
    """

    def __init__(self, assumptions: TlhAssumptions, opening: TlhOpening) -> None:
        _nonnegative(price=opening.price)
        for cohort in opening.cohorts:
            _nonnegative(value=cohort.value, reported_tax_basis=cohort.reported_tax_basis)
            if cohort.purchase_month > opening.month + 1:
                raise ValueError("opening cohort cannot be purchased in the future")
            if cohort.value and not opening.price:
                raise ValueError("a zero opening mark values every cohort at zero")
        self._assumptions = assumptions
        self._month = opening.month
        self._price = opening.price
        # A cohort reported at zero value holds no exposure: it keeps its basis until liquidation.
        self._cohorts = [
            _Cohort(
                Fraction(cohort.value, opening.price) if cohort.value else Fraction(0),
                cohort.reported_tax_basis,
                cohort.purchase_month,
            )
            for cohort in sorted(opening.cohorts, key=lambda cohort: cohort.purchase_month)
            if cohort.value or cohort.reported_tax_basis
        ]

    def _exposure(self) -> Fraction:
        return sum((cohort.exposure for cohort in self._cohorts), Fraction(0))

    def observe(self) -> TlhObservation:
        """Value and reported tax basis only; cohorts and harvesting memory stay private."""
        return self._observe_at_price(self._price)

    def _observe_at_price(self, price: int) -> TlhObservation:
        """Closing-report valuation without advancing the model or realizing next month's loss."""

        _nonnegative(price=price)
        return TlhObservation(
            value=_money(self._exposure() * price), reported_tax_basis=sum(cohort.basis for cohort in self._cohorts)
        )

    def advance(self, market: TlhMarketUpdate) -> ModeledRealizations:
        """Mark to the month's price and harvest each cohort from its own embedded gain and the index drawdown.

        A loss lowers only its cohort's basis, never below zero, so a new contribution inherits no other
        cohort's past reductions. Returns the signed gross realized losses.
        """
        _nonnegative(price=market.price)
        if market.month != self._month + 1:
            raise ValueError("TLH must advance exactly one month at a time")
        scale = MONEY_FACTOR_SCALE
        drawdown = round_ratio(max(0, self._price - market.price) * scale, self._price) if self._price else 0
        updated = []
        short_term = 0
        long_term = 0
        for cohort in self._cohorts:
            value = _money(cohort.exposure * market.price)
            embedded_gain = round_ratio(max(0, value - cohort.basis) * scale, value) if value else 0
            fraction = self._assumptions.monthly_loss_fraction(embedded_gain_ppb=embedded_gain, drawdown_ppb=drawdown)
            loss = min(cohort.basis, round_ratio(value * fraction, scale))
            short_loss = round_ratio(loss * rate_to_ppb(self._assumptions.short_term_fraction), scale)
            short_term -= short_loss
            long_term -= loss - short_loss
            updated.append(replace(cohort, basis=cohort.basis - loss))
        self._cohorts = updated
        self._month = market.month
        self._price = market.price
        return ModeledRealizations(short_term, long_term)

    def contribute(self, amount: int) -> None:
        _nonnegative(amount=amount)
        if not amount:
            return
        if not self._price:
            raise ValueError("a worthless index takes no contribution")
        self._cohorts.append(_Cohort(Fraction(amount, self._price), amount, self._month))

    def withdraw(self, gross_amount: int) -> WithdrawalResult:
        """Sell exactly `gross_amount` of exposure; what the household keeps after tax is not promised."""
        _nonnegative(gross_amount=gross_amount)
        if gross_amount == 0:
            return WithdrawalResult(0, ModeledRealizations())
        value = self.observe().value
        if gross_amount > value:
            raise ValueError("gross withdrawal exceeds portfolio value")
        if gross_amount == value:
            return self.liquidate()
        # Below the rounded mark means below the exact worth, so the FIFO walk sells all of it.
        remaining = Fraction(gross_amount, self._price)
        shares = []
        for cohort in self._cohorts:
            sold = min(remaining, cohort.exposure)
            shares.append(sold / cohort.exposure if cohort.exposure else Fraction(0))
            remaining -= sold
        return WithdrawalResult(gross_amount, self._sell(shares))

    def liquidate(self) -> WithdrawalResult:
        """Sell everything, including zero-value cohorts, whose remaining basis realizes as a loss."""
        cash = self.observe().value
        return WithdrawalResult(cash, self._sell([Fraction(1)] * len(self._cohorts)))

    def _sell(self, shares: list[Fraction]) -> ModeledRealizations:
        """Sell each cohort's share of its exposure at the current mark.

        Basis is rounded per sale, so splitting a sale can move a quantum of gain between its parts;
        liquidation takes whatever basis is left.
        """
        sold = Fraction(0)
        paid = 0
        kept = []
        short_term = 0
        long_term = 0
        for cohort, share in zip(self._cohorts, shares, strict=True):
            # Each fill is the rounded running total less what earlier fills paid, so the fills
            # sum to the order's cash exactly.
            sold += cohort.exposure * share
            proceeds = _money(sold * self._price) - paid
            paid += proceeds
            basis = _money(cohort.basis * share)
            if share != 1:
                kept.append(replace(cohort, exposure=cohort.exposure * (1 - share), basis=cohort.basis - basis))
            if self._month - cohort.purchase_month >= 12:
                long_term += proceeds - basis
            else:
                short_term += proceeds - basis
        self._cohorts = kept
        return ModeledRealizations(short_term, long_term)

    def distribution(self, rate: int) -> int:
        """Cash paid externally on a rate quoted in nano-quanta per unit of the index level: `rate / price × value`."""

        _nonnegative(rate=rate)
        return _money(self._exposure() * rate / MONEY_FACTOR_SCALE)
