"""Already-held nominal bonds and TIPS: supplied marks, contractual coupons and redemption."""

from collections.abc import Sequence
from copy import deepcopy

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.books import AccountRef, BondCashflowOutcome, BondState
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import checked_count, mul_div
from finance.augur.sim.prepared import PreparedBond, PreparedFixedAmount, PreparedIndexedCoupon
from finance.augur.sim.scenario import InterestIncome


class HeldBonds:
    def __init__(self, bonds: Sequence[PreparedBond], market: MarketPath) -> None:
        self.terms = tuple(bonds)
        self.market = market
        self.cashflows: list[BondCashflowOutcome] = []
        self.cashflow_count = 0

    def principal(self, bond: PreparedBond, month: int) -> int:
        if not isinstance(bond.coupon, PreparedIndexedCoupon):
            return bond.face_value
        return mul_div(
            bond.face_value,
            self.market.value("inflation", month),
            self.market.value("inflation", max(0, bond.purchase_month_index)),
            "bond indexed principal",
        )

    def held_principal(self, bond: PreparedBond, snapshot_month: int, valuation_month: int) -> int | None:
        if bond.purchase_month_index > max(0, snapshot_month - 1) or bond.maturity_month_index < snapshot_month:
            return None
        return self.principal(bond, valuation_month)

    def snapshots(self, snapshot_month: int, valuation_month: int) -> list[BondState]:
        rows = []
        for bond in self.terms:
            principal = self.held_principal(bond, snapshot_month, valuation_month)
            rows.append(
                BondState(
                    bond_id=bond.bond_id,
                    agent_id=bond.agent_id,
                    account_id=bond.account_id,
                    principal=0 if principal is None else principal,
                    active=principal is not None,
                )
            )
        return rows

    def advance(self, accounting: Accounting, month: int) -> None:
        for bond in self.terms:
            principal = self.principal(bond, month)
            elapsed = month - bond.purchase_month_index
            coupon = 0
            if elapsed > 0 and month <= bond.maturity_month_index and elapsed % bond.coupon_period_months == 0:
                if isinstance(bond.coupon, PreparedFixedAmount):
                    coupon = bond.coupon.amount
                else:
                    period_rate = mul_div(
                        bond.coupon.annual_rate_ppb, bond.coupon_period_months, 12, "bond period rate"
                    )
                    coupon = mul_div(principal, period_rate, MONEY_FACTOR_SCALE, "indexed bond coupon")
            indexed = isinstance(bond.coupon, PreparedIndexedCoupon)
            redemption = max(principal, bond.face_value) if indexed else bond.face_value
            if month != bond.maturity_month_index:
                redemption = 0
            accretion = 0
            if indexed and month > 0 and bond.purchase_month_index <= month < bond.maturity_month_index:
                accretion = checked_count(principal - self.principal(bond, month - 1), "money subtraction")
            paid = checked_count(coupon + redemption, "money addition")
            income = checked_count(coupon + accretion, "money addition")
            tax = deepcopy(accounting.tax)
            if income:
                tax.income.accrue(
                    bond.agent_id, InterestIncome(issuer_jurisdiction_id=bond.issuer_jurisdiction_id), income
                )
            changed = coupon != 0 or accretion != 0 or redemption != 0
            count = self.cashflow_count + int(changed)
            if count >= 1 << 64:
                raise OverflowError("integer overflow during bond cashflow count")
            cause = f"bond:{bond.bond_id}:m{month}"
            if paid:
                accounting.move(
                    month,
                    cause,
                    AccountRef(agent_id="__external__", account_id="boundary"),
                    AccountRef(agent_id=bond.agent_id, account_id=bond.account_id),
                    paid,
                )
            accounting.tax = tax
            self.cashflow_count = count
            if changed and accounting.capture != "summary":
                self.cashflows.append(
                    BondCashflowOutcome(
                        month=month,
                        cause_id=cause,
                        bond_id=bond.bond_id,
                        agent_id=bond.agent_id,
                        account_id=bond.account_id,
                        issuer_jurisdiction_id=bond.issuer_jurisdiction_id,
                        coupon=coupon,
                        accretion=accretion,
                        redemption=redemption,
                        principal=principal,
                    )
                )
