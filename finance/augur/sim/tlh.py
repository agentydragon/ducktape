"""Stateful reduced-form TLH approximation, owned by the Python experiment.

Losses are modeled, not reconstructed constituent trades. Each private cohort's
adjusted basis falls by its modeled loss; later redemptions use that same basis.
The component does not assess taxes or move household cash. Its caller settles
the returned financial effects and includes the observation in household wealth.
All amounts and prices are integer currency quanta.
"""

from dataclasses import dataclass, replace
from math import isqrt
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE, quantity_for_value, rate_to_ppb
from finance.augur.sim.money import round_ratio


def _nonnegative(**amounts: int) -> None:
    for name, amount in amounts.items():
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise TypeError(f"{name} must be an integer quantum count")
        if amount < 0:
            raise ValueError(f"{name} must be nonnegative")


class TlhAssumptions(BaseModel):
    """Heuristic gross-loss yields; these are not forecasts of after-tax alpha."""

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
class TlhOpeningPosition:
    """Imported financial facts, not a reconstruction of past modeled harvesting."""

    units: int
    reported_tax_basis: int
    purchase_month: int


@dataclass(frozen=True)
class TlhOpening:
    """State before the next advance; opening positions may enter that next month."""

    month: int
    price: int
    quantity_scale: int
    positions: tuple[TlhOpeningPosition, ...]
    cash: int = 0


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
class ContributionResult:
    cash_paid: int


@dataclass(frozen=True)
class WithdrawalResult:
    cash_received: int
    realizations: ModeledRealizations


@dataclass(frozen=True)
class _Cohort:
    units: int
    basis: int
    purchase_month: int


class TlhPortfolio:
    """One rollout's opaque holdings and harvesting memory.

    Advance once before each monthly decision. Contributions enter at the current
    mark; withdrawals redeem FIFO at that mark and return an exact gross cash
    amount. Share-grid overfill remains as cash inside the portfolio. A caller
    needing transactional settlement can operate on a deepcopy, adopting it only
    when the accounting engine accepts its financial effects.

    Each fill and remaining position is marked separately to the nearest currency
    quantum. Splitting a fractional position can therefore differ by a rounding
    quantum from liquidating it in one fill; basis is apportioned without loss.

    Sale character uses Augur's monthly holding-period convention (12 months is
    long-term). Harvested character is a model assumption; constituent holding
    periods and wash-sale mechanics are not simulated.
    """

    def __init__(self, assumptions: TlhAssumptions, opening: TlhOpening) -> None:
        _nonnegative(price=opening.price, cash=opening.cash, quantity_scale=opening.quantity_scale)
        scale = opening.quantity_scale
        while scale > 1 and scale % 10 == 0:
            scale //= 10
        if scale != 1:
            raise ValueError("quantity_scale must be a positive power of ten")
        for position in opening.positions:
            _nonnegative(units=position.units, reported_tax_basis=position.reported_tax_basis)
            if position.purchase_month > opening.month + 1:
                raise ValueError("opening position cannot be purchased in the future")
            if position.units == 0 and position.reported_tax_basis != 0:
                raise ValueError("an empty opening position cannot have basis")
        self._assumptions = assumptions
        self._month = opening.month
        self._price = opening.price
        self._quantity_scale = opening.quantity_scale
        self._cash = opening.cash
        self._cohorts = [
            _Cohort(position.units, position.reported_tax_basis, position.purchase_month)
            for position in sorted(opening.positions, key=lambda position: position.purchase_month)
            if position.units
        ]

    def _value(self, units: int) -> int:
        return round_ratio(units * self._price, self._quantity_scale)

    def observe(self) -> TlhObservation:
        return self._observe_at_price(self._price)

    def _observe_at_price(self, price: int) -> TlhObservation:
        """Closing-report valuation without advancing the model or realizing next month's loss."""

        _nonnegative(price=price)
        return TlhObservation(
            value=self._cash + sum(round_ratio(cohort.units * price, self._quantity_scale) for cohort in self._cohorts),
            reported_tax_basis=self._cash + sum(cohort.basis for cohort in self._cohorts),
        )

    def advance(self, market: TlhMarketUpdate) -> ModeledRealizations:
        _nonnegative(price=market.price)
        if market.month != self._month + 1:
            raise ValueError("TLH must advance exactly one month at a time")
        scale = MONEY_FACTOR_SCALE
        drawdown = round_ratio(max(0, self._price - market.price) * scale, self._price) if self._price else 0
        updated = []
        short_term = 0
        long_term = 0
        for cohort in self._cohorts:
            value = round_ratio(cohort.units * market.price, self._quantity_scale)
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

    def contribute(self, amount: int) -> ContributionResult:
        _nonnegative(amount=amount)
        units = quantity_for_value(amount, self._price, self._quantity_scale, round_up=False) if self._price else 0
        invested = self._value(units)
        if units:
            self._cohorts.append(_Cohort(units, invested, self._month))
        self._cash += amount - invested
        return ContributionResult(cash_paid=amount)

    def withdraw(self, gross_amount: int) -> WithdrawalResult:
        _nonnegative(gross_amount=gross_amount)
        if gross_amount == 0:
            return WithdrawalResult(0, ModeledRealizations())
        value = self.observe().value
        if gross_amount > value:
            raise ValueError("gross withdrawal exceeds portfolio value")
        if gross_amount == value:
            return self.liquidate()
        cash = self._cash
        units_to_sell = 0
        for cohort in self._cohorts:
            if cash >= gross_amount:
                break
            units = min(
                cohort.units, quantity_for_value(gross_amount - cash, self._price, self._quantity_scale, round_up=True)
            )
            units_to_sell += units
            cash += self._value(units)
        if cash < gross_amount:
            raise ValueError("share-grid rounding cannot fund the requested withdrawal")
        redeemed = self._redeem(units_to_sell)
        self._cash += redeemed.cash_received - gross_amount
        return WithdrawalResult(gross_amount, redeemed.realizations)

    def liquidate(self) -> WithdrawalResult:
        redeemed = self._redeem(sum(cohort.units for cohort in self._cohorts))
        cash = self._cash + redeemed.cash_received
        self._cash = 0
        return WithdrawalResult(cash, redeemed.realizations)

    def _withdraw_units(self, units: int) -> WithdrawalResult:
        """Configured fixed-unit schedules redeem exposure without exposing cohort state."""

        _nonnegative(units=units)
        if units == 0:
            return WithdrawalResult(0, ModeledRealizations())
        total_units = sum(cohort.units for cohort in self._cohorts)
        if units > total_units:
            raise ValueError("scheduled redemption exceeds portfolio exposure")
        if units == total_units:
            return self.liquidate()
        return self._redeem(units)

    def _redeem(self, units: int) -> WithdrawalResult:
        remaining = units
        updated = []
        cash = 0
        short_term = 0
        long_term = 0
        for cohort in self._cohorts:
            sold = min(remaining, cohort.units)
            proceeds = self._value(sold)
            basis = round_ratio(cohort.basis * sold, cohort.units)
            gain = proceeds - basis
            if self._month - cohort.purchase_month >= 12:
                long_term += gain
            else:
                short_term += gain
            cash += proceeds
            remaining -= sold
            if sold < cohort.units:
                updated.append(replace(cohort, units=cohort.units - sold, basis=cohort.basis - basis))
        self._cohorts = updated
        return WithdrawalResult(cash, ModeledRealizations(short_term, long_term))

    def _distribution(self, per_unit_rate: int) -> int:
        """Cash paid externally, quoted in nano-quanta per unit on the input rate grid."""

        _nonnegative(per_unit_rate=per_unit_rate)
        return round_ratio(
            sum(cohort.units for cohort in self._cohorts) * per_unit_rate, self._quantity_scale * MONEY_FACTOR_SCALE
        )
