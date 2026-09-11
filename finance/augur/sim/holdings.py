"""Exact remaining lots, admitted purchases and explicit-lot sales over the canonical ledger."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import Buy, LotSale, Sell
from finance.augur.sim.books import AccountRef, JournalEntry, Posting, SecurityLotState
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import apportion, checked_count, checked_wide, is_quantity_scale, position_value
from finance.augur.sim.prepared import PreparedLot, PreparedScenario, _ScheduledSale


@dataclass
class Lot:
    spec: PreparedLot
    units_remaining: int
    basis_remaining: int

    def snapshot(self) -> SecurityLotState:
        spec = self.spec
        return SecurityLotState(
            lot_id=spec.lot_id,
            agent_id=spec.agent_id,
            account_id=spec.account_id,
            asset_id=spec.asset_id if private_issuer(spec.asset_id) is not None else f"security:{spec.asset_id}",
            purchase_month=spec.purchase_month,
            quantity_scale=spec.quantity_scale,
            units_remaining=self.units_remaining,
            basis_remaining=self.basis_remaining,
        )


@dataclass(frozen=True)
class Disposition:
    month: int
    cause_id: str
    agent_id: str
    source_account_id: str
    asset_id: str
    lot_id: str
    purchase_month: int
    quantity_scale: int
    units: int
    basis: int
    proceeds: int
    proceeds_account_id: str
    realized_gain: int


def basis_account(agent: str, account: str, asset: str) -> AccountRef:
    return AccountRef(agent_id=agent, account_id=f"asset-basis:{account}:{asset}")


def gain_account(agent: str) -> AccountRef:
    return AccountRef(agent_id=agent, account_id="income:realized-gain")


def private_issuer(asset: str) -> str | None:
    if asset.startswith("private_equity:"):
        return asset.removeprefix("private_equity:") or None
    return None


class Holdings:
    def __init__(self, scenario: PreparedScenario, accounting: Accounting) -> None:
        self.lots = [Lot(spec, spec.units, spec.basis) for spec in scenario.initial_lots]
        self.dispositions: list[Disposition] = []
        self.disposition_count = 0
        for pool in scenario.holding_pools:
            accounting.ledger.ensure_account(basis_account(pool.agent_id, pool.account_id, pool.asset_id))
            accounting.ledger.ensure_account(gain_account(pool.agent_id))
        for lot in self.lots:
            spec = lot.spec
            if spec.basis:
                accounting.apply(
                    JournalEntry(
                        month=0,
                        cause_id=f"opening-lot:{spec.lot_id}",
                        postings=[
                            Posting(
                                account=basis_account(spec.agent_id, spec.account_id, spec.asset_id), amount=spec.basis
                            ),
                            Posting(
                                account=AccountRef(agent_id=spec.agent_id, account_id="equity:opening"),
                                amount=checked_count(-spec.basis, "money negation"),
                            ),
                        ],
                    )
                )

    def fifo(self, candidates: Sequence[int], units: int) -> tuple[LotSale, ...]:
        if units <= 0:
            raise ValueError("sale units must be positive")
        if any(not 0 <= index < len(self.lots) for index in candidates):
            raise ValueError("unknown candidate lot")
        if len(set(candidates)) != len(candidates):
            raise ValueError("duplicate candidate lot")
        ordered = sorted(
            candidates, key=lambda index: (self.lots[index].spec.purchase_month, self.lots[index].spec.lot_id)
        )
        remaining = units
        selected = []
        for index in ordered:
            lot = self.lots[index]
            sold = min(remaining, lot.units_remaining)
            if sold > 0:
                selected.append(LotSale(account_id=lot.spec.account_id, lot_id=lot.spec.lot_id, units=sold))
                remaining -= sold
            if not remaining:
                break
        if remaining:
            raise ValueError(f"sale of {units} units exceeds available lots; only {units - remaining} are available")
        return tuple(selected)

    def scheduled_sale(self, accounting: Accounting, market: MarketPath, sale: _ScheduledSale) -> None:
        candidates = [
            index
            for index, lot in enumerate(self.lots)
            if (lot.spec.agent_id, lot.spec.account_id, lot.spec.asset_id)
            == (sale.agent_id, sale.account_id, sale.asset_id)
            and lot.units_remaining > 0
        ]
        if not candidates:
            raise ValueError("missing sale pool")
        request = Sell(
            cause_id=sale.cause_id,
            agent_id=sale.agent_id,
            proceeds_account_id=sale.proceeds_account_id,
            asset_id=sale.asset_id,
            lots=self.fifo(candidates, sale.units),
        )
        self.sell(accounting, sale.month, request, price=market.value(f"security:{sale.asset_id}", sale.month))

    def _selected(self, accounting: Accounting, request: Sell, proceeds: int) -> list[tuple[int, int]]:
        destination = AccountRef(agent_id=request.agent_id, account_id=request.proceeds_account_id)
        if destination not in accounting.declared:
            raise ValueError("unknown declared proceeds account")
        if not request.lots or proceeds < 0:
            raise ValueError("sale needs lots and nonnegative execution proceeds")
        by_id = {lot.spec.lot_id: index for index, lot in enumerate(self.lots)}
        seen = set()
        selected = []
        for selection in request.lots:
            if selection.lot_id in seen:
                raise ValueError(f"duplicate lot {selection.lot_id!r}")
            seen.add(selection.lot_id)
            if selection.lot_id not in by_id:
                raise ValueError(f"unknown lot {selection.lot_id!r}")
            index = by_id[selection.lot_id]
            lot = self.lots[index]
            if (lot.spec.agent_id, lot.spec.account_id, lot.spec.asset_id) != (
                request.agent_id,
                selection.account_id,
                request.asset_id,
            ):
                raise ValueError("lot does not belong to the requested owner/account/asset")
            if not 0 < selection.units <= lot.units_remaining:
                raise ValueError("invalid quantity for remaining lot")
            selected.append((index, selection.units))
        return selected

    def sell(self, accounting: Accounting, month: int, request: Sell, *, price: int) -> None:
        selected = self._selected(accounting, request, price)
        amounts = [position_value(price, units, self.lots[index].spec.quantity_scale) for index, units in selected]
        self._post_sale(accounting, month, request, selected, amounts)

    def cashout(self, accounting: Accounting, month: int, request: Sell, *, total: int) -> None:
        selected = self._selected(accounting, request, total)
        scale = max(self.lots[index].spec.quantity_scale for index, _ in selected)
        weights = [
            checked_wide(units * (scale // self.lots[index].spec.quantity_scale), "total sale proceeds allocation")
            for index, units in selected
        ]
        denominator = checked_wide(sum(weights), "total sale proceeds allocation")
        amounts, remainders = [], []
        residual = total
        for weight in weights:
            numerator = checked_wide(total * weight, "total sale proceeds allocation")
            amount, remainder = divmod(numerator, denominator)
            amounts.append(checked_count(amount, "total sale proceeds allocation"))
            residual = checked_count(residual - amount, "money subtraction")
            remainders.append(remainder)
        for index in sorted(range(len(selected)), key=lambda index: (-remainders[index], index)):
            if not residual:
                break
            amounts[index] = checked_count(amounts[index] + 1, "money addition")
            residual -= 1
        if residual:
            raise ArithmeticError("sale proceeds allocation did not exhaust the stated total")
        self._post_sale(accounting, month, request, selected, amounts)

    def _post_sale(
        self,
        accounting: Accounting,
        month: int,
        request: Sell,
        selected: Sequence[tuple[int, int]],
        amounts: Sequence[int],
    ) -> None:
        replacements, dispositions, basis_postings = [], [], []
        total_proceeds = total_gain = 0
        tax = deepcopy(accounting.tax)
        for (index, units), proceeds in zip(selected, amounts, strict=True):
            lot = self.lots[index]
            spec = lot.spec
            basis = apportion(lot.basis_remaining, units, lot.units_remaining)
            gain = checked_count(proceeds - basis, "money subtraction")
            total_proceeds = checked_count(total_proceeds + proceeds, "money addition")
            total_gain = checked_count(total_gain + gain, "money addition")
            tax.gain(request.agent_id, gain, long_term=month - spec.purchase_month >= 12)
            replacements.append(
                (index, lot.units_remaining - units, checked_count(lot.basis_remaining - basis, "money subtraction"))
            )
            basis_postings.append(
                Posting(
                    account=basis_account(spec.agent_id, spec.account_id, spec.asset_id),
                    amount=checked_count(-basis, "money negation"),
                )
            )
            asset = spec.asset_id if private_issuer(spec.asset_id) is not None else f"security:{spec.asset_id}"
            dispositions.append(
                Disposition(
                    month,
                    request.cause_id,
                    request.agent_id,
                    spec.account_id,
                    asset,
                    spec.lot_id,
                    spec.purchase_month,
                    spec.quantity_scale,
                    units,
                    basis,
                    proceeds,
                    request.proceeds_account_id,
                    gain,
                )
            )
        count = self.disposition_count + len(dispositions)
        if count >= 1 << 64:
            raise OverflowError("integer overflow during disposition count")
        accounting.apply(
            JournalEntry(
                month=month,
                cause_id=request.cause_id,
                postings=[
                    Posting(
                        account=AccountRef(agent_id=request.agent_id, account_id=request.proceeds_account_id),
                        amount=total_proceeds,
                    ),
                    *basis_postings,
                    Posting(
                        account=gain_account(request.agent_id), amount=checked_count(-total_gain, "money negation")
                    ),
                ],
            )
        )
        for index, units, basis in replacements:
            self.lots[index].units_remaining = units
            self.lots[index].basis_remaining = basis
        accounting.tax = tax
        self.disposition_count = count
        if accounting.capture != "summary":
            self.dispositions.extend(dispositions)

    def buy(self, scenario: PreparedScenario, accounting: Accounting, month: int, request: Buy, *, price: int) -> None:
        cash = AccountRef(agent_id=request.agent_id, account_id=request.cash_account_id)
        if cash not in accounting.declared:
            raise ValueError("unknown declared cash account")
        if (
            request.units <= 0
            or price <= 0
            or not is_quantity_scale(request.quantity_scale)
            or private_issuer(request.asset_id) is not None
        ):
            raise ValueError("purchase needs a public security, positive units/price and a supported quantity scale")
        if not request.lot_id or any(lot.spec.lot_id == request.lot_id for lot in self.lots):
            raise ValueError("purchase needs a new nonempty lot ID")
        if not any(
            (pool.agent_id, pool.account_id, pool.asset_id, pool.quantity_scale)
            == (request.agent_id, request.holding_account_id, request.asset_id, request.quantity_scale)
            for pool in scenario.holding_pools
        ):
            raise ValueError("holding pool, asset and quantity scale must be declared by the input")
        if any(
            (item.owner_agent_id, item.account_id, item.asset_id)
            == (request.agent_id, request.holding_account_id, request.asset_id)
            for item in scenario.tlh_portfolios
        ):
            raise ValueError("managed portfolio contributions are not ordinary lot purchases")
        spent = position_value(price, request.units, request.quantity_scale)
        if spent > accounting.ledger.balance(cash):
            raise ValueError("insufficient purchase cash")
        if not 0 <= month < 1 << 31:
            raise OverflowError("integer overflow during purchase month")
        spec = PreparedLot(
            lot_id=request.lot_id,
            agent_id=request.agent_id,
            account_id=request.holding_account_id,
            asset_id=request.asset_id,
            purchase_month=month,
            quantity_scale=request.quantity_scale,
            units=request.units,
            basis=spent,
        )
        accounting.apply(
            JournalEntry(
                month=month,
                cause_id=request.cause_id,
                postings=[
                    Posting(account=cash, amount=checked_count(-spent, "money negation")),
                    Posting(account=basis_account(spec.agent_id, spec.account_id, spec.asset_id), amount=spent),
                ],
            )
        )
        self.lots.append(Lot(spec, request.units, spent))
