"""Exact remaining lots, admitted purchases and explicit-lot sales over the canonical ledger."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from finance.augur.model.asset_key import AssetKey, PrivateEquityAssetKey
from finance.augur.model.series import IssuerId, SecurityKey, SecuritySymbol
from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import Buy, LotSale, Sell
from finance.augur.sim.actor import Statement
from finance.augur.sim.books import AccountRef, JournalEntry, Posting, SecurityLotState
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import apportion, checked_count, checked_wide, is_quantity_scale, position_value
from finance.augur.sim.observations import HoldingPool, PublicPosition
from finance.augur.sim.prepared import PreparedHoldingPool, PreparedLot
from finance.augur.sim.trading_costs import trading_cost


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
            asset_id=spec.asset_id,
            purchase_month=spec.purchase_month,
            quantity_scale=spec.quantity_scale,
            units_remaining=self.units_remaining,
            basis_remaining=self.basis_remaining,
        )


@dataclass(frozen=True)
class Disposition:
    """One lot's part in a sale; `proceeds` is the amount realized, net of the lot's trading cost."""

    month: int
    cause_id: str
    agent_id: AgentId
    source_account_id: AccountId
    asset_id: AssetId
    lot_id: LotId
    purchase_month: int
    quantity_scale: int
    units: int
    basis: int
    proceeds: int
    proceeds_account_id: AccountId
    realized_gain: int


@dataclass(frozen=True)
class Traded:
    """A settled buy or sale: the gross value of its lots and the trading cost it paid."""

    gross: int
    cost: int


def basis_account(agent: AgentId, account: AccountId, asset: AssetId) -> AccountRef:
    return AccountRef(agent_id=agent, account_id=AccountId(f"asset-basis:{account}:{asset}"))


def gain_account(agent: AgentId) -> AccountRef:
    return AccountRef(agent_id=agent, account_id=AccountId("income:realized-gain"))


def _cost_entry(month: int, cause_id: str, cash: AccountRef, account: AccountRef, cost: int) -> JournalEntry:
    """Trade `cause_id`'s cost out of `cash`: into a bought lot's basis, or against a sale's realized gain."""
    return JournalEntry(
        month=month,
        cause_id=f"{cause_id}:trading-cost",
        postings=[
            Posting(account=cash, amount=checked_count(-cost, "money negation")),
            Posting(account=account, amount=cost),
        ],
    )


def private_issuer(asset: AssetId) -> IssuerId | None:
    if asset.startswith("private_equity:"):
        return IssuerId(asset.removeprefix("private_equity:")) or None
    return None


def asset_key(asset: AssetId) -> AssetKey:
    """The typed identity behind a sim `AssetId`; inverse of the compiler's `_asset_id`."""

    issuer = private_issuer(asset)
    return SecurityKey(symbol=SecuritySymbol(asset)) if issuer is None else PrivateEquityAssetKey(issuer_id=issuer)


class PositionStatement(Statement):
    """The owner's declared public pools at current prices and its open lots in them."""

    pools: tuple[HoldingPool, ...]
    positions: tuple[PublicPosition, ...]


class Holdings:
    def __init__(self) -> None:
        self.pools: tuple[PreparedHoldingPool, ...] = ()
        self.lots: list[Lot] = []
        # Holdings a managed portfolio owns; ordinary purchases into them are refused.
        self.managed: set[tuple[AgentId, AccountId, AssetId]] = set()
        # This month's dispositions, cleared by `begin_month`; lots are the state.
        self.dispositions: list[Disposition] = []

    def declare_pool(self, accounting: Accounting, pool: PreparedHoldingPool) -> None:
        self.pools = (*self.pools, pool)
        accounting.ledger.ensure_account(basis_account(pool.agent_id, pool.account_id, pool.asset_id))
        accounting.ledger.ensure_account(gain_account(pool.agent_id))

    def hold(self, accounting: Accounting, spec: PreparedLot) -> None:
        """Open a lot held at month zero, its basis against the owner's opening equity."""
        self.lots.append(Lot(spec, spec.units, spec.basis))
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
                            account=AccountRef(agent_id=spec.agent_id, account_id=AccountId("equity:opening")),
                            amount=checked_count(-spec.basis, "money negation"),
                        ),
                    ],
                )
            )

    def reserve(self, owner_agent_id: AgentId, account_id: AccountId, asset_id: AssetId) -> None:
        self.managed.add((owner_agent_id, account_id, asset_id))

    def public_price(self, actor: AgentId, asset: AssetId, market: MarketPath, month: int) -> int:
        if private_issuer(asset) is not None or not any(
            pool.agent_id == actor and pool.asset_id == asset for pool in self.pools
        ):
            raise ValueError("asset has no declared public holding pool")
        return market.value(f"security:{asset}", month)

    def statement(self, actor: AgentId, market: MarketPath, month: int) -> PositionStatement:
        positions = []
        for lot in self.lots:
            spec = lot.spec
            if spec.agent_id != actor or lot.units_remaining == 0 or private_issuer(spec.asset_id) is not None:
                continue
            price = self.public_price(actor, spec.asset_id, market, month)
            positions.append(
                PublicPosition(
                    account_id=spec.account_id,
                    asset_id=spec.asset_id,
                    lot_id=spec.lot_id,
                    purchase_month=spec.purchase_month,
                    units=lot.units_remaining,
                    quantity_scale=spec.quantity_scale,
                    book_basis=lot.basis_remaining,
                    price=price,
                    value=position_value(price, lot.units_remaining, spec.quantity_scale),
                )
            )
        return PositionStatement(
            month=month,
            pools=tuple(
                HoldingPool(
                    account_id=pool.account_id,
                    asset_id=pool.asset_id,
                    quantity_scale=pool.quantity_scale,
                    price=self.public_price(actor, pool.asset_id, market, month),
                )
                for pool in self.pools
                if pool.agent_id == actor and private_issuer(pool.asset_id) is None
            ),
            positions=tuple(positions),
        )

    def begin_month(self) -> None:
        self.dispositions.clear()

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

    def sell(self, accounting: Accounting, month: int, request: Sell, *, price: int, cost_rate_ppb: int) -> Traded:
        """Sell the selected lots at `price`; each pays `cost_rate_ppb` of its gross value (<trading_costs.py>)."""
        selected = self._selected(accounting, request, price)
        amounts = [position_value(price, units, self.lots[index].spec.quantity_scale) for index, units in selected]
        costs = [trading_cost(amount, cost_rate_ppb) for amount in amounts]
        return self._post_sale(accounting, month, request, selected, amounts, costs)

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
        self._post_sale(accounting, month, request, selected, amounts, [0] * len(amounts))

    def _post_sale(
        self,
        accounting: Accounting,
        month: int,
        request: Sell,
        selected: Sequence[tuple[int, int]],
        amounts: Sequence[int],
        costs: Sequence[int],
    ) -> Traded:
        """Each lot realizes its gross amount less its trading cost; the costs are paid in an entry of their own."""
        replacements, dispositions, basis_postings = [], [], []
        total_gross = total_basis = total_cost = 0
        tax = deepcopy(accounting.tax)
        for (index, units), gross, cost in zip(selected, amounts, costs, strict=True):
            lot = self.lots[index]
            spec = lot.spec
            basis = apportion(lot.basis_remaining, units, lot.units_remaining)
            proceeds = checked_count(gross - cost, "money subtraction")
            gain = checked_count(proceeds - basis, "money subtraction")
            total_gross = checked_count(total_gross + gross, "money addition")
            total_basis = checked_count(total_basis + basis, "money addition")
            total_cost = checked_count(total_cost + cost, "money addition")
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
            dispositions.append(
                Disposition(
                    month,
                    request.cause_id,
                    request.agent_id,
                    spec.account_id,
                    spec.asset_id,
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
        cash = AccountRef(agent_id=request.agent_id, account_id=request.proceeds_account_id)
        gains = gain_account(request.agent_id)
        entries = [
            JournalEntry(
                month=month,
                cause_id=request.cause_id,
                postings=[
                    Posting(account=cash, amount=total_gross),
                    *basis_postings,
                    Posting(account=gains, amount=checked_count(total_basis - total_gross, "money subtraction")),
                ],
            )
        ]
        if total_cost:
            entries.append(_cost_entry(month, request.cause_id, cash, gains, total_cost))
        accounting.apply_entries(entries)
        for index, units, basis in replacements:
            self.lots[index].units_remaining = units
            self.lots[index].basis_remaining = basis
        accounting.tax = tax
        self.dispositions.extend(dispositions)
        return Traded(total_gross, total_cost)

    def buy(self, accounting: Accounting, month: int, request: Buy, *, price: int, cost_rate_ppb: int) -> Traded:
        """Buy a new lot at `price`, its basis the gross value plus `cost_rate_ppb` of it (<trading_costs.py>)."""
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
            for pool in self.pools
        ):
            raise ValueError("holding pool, asset and quantity scale must be declared by the input")
        if (request.agent_id, request.holding_account_id, request.asset_id) in self.managed:
            raise ValueError("managed portfolio contributions are not ordinary lot purchases")
        spent = position_value(price, request.units, request.quantity_scale)
        cost = trading_cost(spent, cost_rate_ppb)
        basis = checked_count(spent + cost, "money addition")
        if basis > accounting.ledger.balance(cash):
            raise ValueError("insufficient purchase cash" + (f" for {spent} and trading cost {cost}" if cost else ""))
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
            basis=basis,
        )
        held = basis_account(spec.agent_id, spec.account_id, spec.asset_id)
        entries = [
            JournalEntry(
                month=month,
                cause_id=request.cause_id,
                postings=[
                    Posting(account=cash, amount=checked_count(-spent, "money negation")),
                    Posting(account=held, amount=spent),
                ],
            )
        ]
        if cost:
            entries.append(_cost_entry(month, request.cause_id, cash, held, cost))
        accounting.apply_entries(entries)
        self.lots.append(Lot(spec, request.units, basis))
        return Traded(spent, cost)
