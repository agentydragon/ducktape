"""Product's optional public-security funding policy, authored as ordinary Python actions.

Weights group each symbol across ordered source accounts. Product zero weights are
exclusions, not zero-target exits. Scheduled claims and tax calculation remain engine
facts; this policy proposes funding sales followed by full payments, without retry.
"""

from decimal import Decimal

from finance.augur.model.series import SecurityKey
from finance.augur.policy.cash_band import Raise, cash_band
from finance.augur.policy.sleeves import withdraw_by_symbol
from finance.augur.product.wire import FundingPolicy
from finance.augur.rust.simulator import Action, Decision, DecisionActions
from finance.augur.sim.fixed_point import currency_amount_to_quanta
from finance.augur.sim.scenario import InitialLot


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
        actor_id: str,
        cash_account_id: str,
        initial_lots: tuple[InitialLot, ...],
        currency_quantum: Decimal,
    ) -> None:
        self.actor_id = actor_id
        self.cash_account_id = cash_account_id
        public_lots = [lot for lot in initial_lots if lot.agent_id == actor_id and isinstance(lot.asset, SecurityKey)]
        held = {str(lot.asset.symbol) for lot in public_lots if isinstance(lot.asset, SecurityKey)}
        self.targets = {
            str(sleeve.symbol): sleeve.weight
            for sleeve in config.sleeve_weights
            if sleeve.weight > 0 and str(sleeve.symbol) in held
        }
        self.source_accounts = tuple(
            dict.fromkeys(
                lot.account_id
                for lot in public_lots
                if isinstance(lot.asset, SecurityKey) and str(lot.asset.symbol) in self.targets
            )
        )
        self.floor = int(currency_amount_to_quanta(config.cash_floor, quantum=currency_quantum))
        self.ceiling = int(currency_amount_to_quanta(config.cash_ceiling, quantum=currency_quantum))
        self.indexed = config.cash_band_index_to_inflation

    def __call__(self, batch: list[Decision]) -> list[DecisionActions]:
        responses = []
        for decision in batch:
            observation = decision.observation
            if observation.agent_id != self.actor_id:
                raise ValueError("product funding policy received another actor's observation")
            actions = []
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
                    if claim.from_account == (self.actor_id, self.cash_account_id)
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
                Action.pay_claim(
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
