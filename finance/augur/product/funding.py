"""Product's optional public-security funding policy, authored as ordinary Python actions.

Weights group each symbol across ordered source accounts. Product zero weights are
exclusions, not zero-target exits. Scheduled claims and tax calculation remain engine
facts; this policy proposes funding sales followed by full payments, without retry.
"""

from finance.augur.api.portfolio import PortfolioConfig
from finance.augur.model.series import SecurityKey
from finance.augur.policy.cash_band import Raise, cash_band
from finance.augur.policy.sleeves import withdraw_by_symbol
from finance.augur.product.holdings import opening_lots
from finance.augur.product.wire import FundingPolicy, ManagedSleeveWeight, SecuritySleeveWeight
from finance.augur.sim.actions import Action, DecisionActions, PayClaim
from finance.augur.sim.ids import AccountId, AgentId, AssetId
from finance.augur.sim.money import Currency
from finance.augur.sim.observations import Decision


class Policy:
    """One monthly batch call: cash-band funding, then claims in observed order.

    Source account order comes from the actor's opening lots, as in the product
    configuration. A saved symbol not initially held is ignored. Empty/excluded
    targets disable all sales, but not cash-only payments. Surplus is never invested.
    """

    def __init__(
        self,
        config: FundingPolicy,
        *,
        actor_id: AgentId,
        cash_account_id: AccountId,
        portfolio: PortfolioConfig,
        currency: Currency,
    ) -> None:
        self.actor_id = actor_id
        self.cash_account_id = cash_account_id
        public_lots = [
            (position.account_id, AssetId(position.asset.symbol))
            for owner, position, _ in opening_lots(portfolio)
            if owner == actor_id and isinstance(position.asset, SecurityKey)
        ]
        held = {asset_id for _, asset_id in public_lots}
        if any(isinstance(sleeve, ManagedSleeveWeight) for sleeve in config.sleeve_weights):
            raise ValueError("this policy sells ordinary lots only; it has no managed-portfolio sleeves")
        self.targets = {
            AssetId(sleeve.symbol): sleeve.weight
            for sleeve in config.sleeve_weights
            if isinstance(sleeve, SecuritySleeveWeight) and sleeve.weight > 0 and AssetId(sleeve.symbol) in held
        }
        self.source_accounts = tuple(
            dict.fromkeys(account_id for account_id, asset_id in public_lots if asset_id in self.targets)
        )
        self.floor = currency.quanta(config.cash_floor)
        self.ceiling = currency.quanta(config.cash_ceiling)
        self.indexed = config.cash_band_index_to_inflation

    def __call__(self, batch: list[Decision]) -> list[DecisionActions]:
        responses = []
        for decision in batch:
            observation = decision.observation
            if observation.agent_id != self.actor_id:
                raise ValueError("product funding policy received another actor's observation")
            actions: list[Action] = []
            if self.targets:
                floor, ceiling = self.floor, self.ceiling
                if self.indexed and ceiling > 0:
                    if observation.cpi is None:
                        raise ValueError("indexed product cash band requires a supplied CPI path")
                    current, origin = observation.cpi
                    # Round original quanta × current/origin once, not last month's rounded bound.
                    floor = (2 * floor * current + origin) // (2 * origin)
                    ceiling = (2 * ceiling * current + origin) // (2 * origin)
                cash = dict(observation.accounts)[self.cash_account_id]
                due = sum(
                    claim.amount_due
                    for claim in observation.claims
                    if (claim.from_account.agent_id, claim.from_account.account_id)
                    == (self.actor_id, self.cash_account_id)
                )
                proposal = cash_band(projected_cash=cash - due, floor=floor, ceiling=ceiling)
                if isinstance(proposal, Raise):
                    actions = withdraw_by_symbol(
                        observation,
                        targets=self.targets,
                        source_account_ids=self.source_accounts,
                        cash_account_id=self.cash_account_id,
                        amount=proposal.amount,
                        cause_id=f"product-funding-{observation.month}",
                    )
            actions.extend(
                PayClaim(
                    request_id=index + 1,
                    cause_id=claim.cause_id,
                    claim=claim,
                    from_account=claim.from_account,
                    amount=claim.amount_due,
                )
                for index, claim in enumerate(observation.claims)
            )
            responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
        return responses
