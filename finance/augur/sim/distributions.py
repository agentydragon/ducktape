"""Ordinary holding distributions, rounded once per pool and then per tax slice."""

from copy import deepcopy

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.books import AccountRef, DistributionOutcome, JournalEntry, Posting
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.holdings import Holdings
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import checked_count, distribution_value, mul_div
from finance.augur.sim.prepared import PreparedScenario
from finance.augur.sim.scenario import InterestIncome


class Distributions:
    def __init__(self) -> None:
        self.outcomes: list[DistributionOutcome] = []
        self.count = 0

    def advance(
        self, scenario: PreparedScenario, accounting: Accounting, holdings: Holdings, market: MarketPath, month: int
    ) -> None:
        for spec in scenario.distributions:
            if any(
                (row.owner_agent_id, row.account_id, row.asset_id)
                == (spec.agent_id, spec.holding_account_id, spec.asset_id)
                for row in scenario.tlh_portfolios
            ):
                continue
            lots = [
                lot
                for lot in holdings.lots
                if (lot.spec.agent_id, lot.spec.account_id, lot.spec.asset_id)
                == (spec.agent_id, spec.holding_account_id, spec.asset_id)
            ]
            units = checked_count(sum(lot.units_remaining for lot in lots), "distribution pool quantity")
            scale = lots[0].spec.quantity_scale if lots else 1
            total = distribution_value(market.value(f"security_distribution:{spec.asset_id}", month), units, scale)
            tax = deepcopy(accounting.tax)
            entries = []
            outcomes = []
            for index, slice_ in enumerate(spec.tax_character):
                amount = mul_div(total, slice_.fraction_ppb, MONEY_FACTOR_SCALE, "security distribution tax slice")
                cause = f"distribution:{spec.agent_id}:{spec.asset_id}:s{index}:m{month}"
                entries.append(
                    JournalEntry(
                        month=month,
                        cause_id=cause,
                        postings=[
                            Posting(
                                account=AccountRef(agent_id="__external__", account_id="boundary"),
                                amount=checked_count(-amount, "money negation"),
                            ),
                            Posting(
                                account=AccountRef(agent_id=spec.agent_id, account_id=spec.to_account_id), amount=amount
                            ),
                        ],
                    )
                )
                tax.income.accrue(
                    spec.agent_id, InterestIncome(issuer_jurisdiction_id=slice_.issuer_jurisdiction_id), amount
                )
                outcomes.append(
                    DistributionOutcome(
                        month=month,
                        agent_id=spec.agent_id,
                        holding_account_id=spec.holding_account_id,
                        asset_id=spec.asset_id,
                        slice_index=index,
                        fraction_ppb=slice_.fraction_ppb,
                        issuer_jurisdiction_id=slice_.issuer_jurisdiction_id,
                        units=units,
                        amount=amount,
                    )
                )
            accounting.apply_entries(entries)
            accounting.tax = tax
            self.count += len(outcomes)
            if accounting.capture != "summary":
                self.outcomes.extend(outcomes)
