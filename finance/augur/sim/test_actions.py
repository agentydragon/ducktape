"""Claim occurrence serialization must not transfer session execution authority."""

import pytest_bazel

from finance.augur.sim.actions import ClaimId, PayClaim
from finance.augur.sim.books import AccountRef
from finance.augur.sim.observations import Claim, Observation, observation_from_json


def test_observed_claim_request_roundtrip_loses_opaque_owner() -> None:
    account = AccountRef(agent_id="test-payer", account_id="checking")
    facts = Observation(
        agent_id=account.agent_id,
        month=3,
        cpi=None,
        cash=10,
        public_holdings=0,
        accounts=((account.account_id, 10),),
        holding_pools=(),
        public_positions=(),
        held_bonds=(),
        tlh_portfolios=(),
        claims=(
            Claim(
                month=3,
                index=2,
                cause_id="test-bill",
                obligation_type="rent",
                from_account=account,
                to_account=AccountRef(agent_id="test-recipient", account_id="checking"),
                amount_due=10,
            ),
        ),
    )
    owner = object()
    observed = observation_from_json(
        facts.model_dump_json(by_alias=True), owner=owner, rollout_id=7, previous_receipts=()
    )
    [claim] = observed.claims
    request = PayClaim(request_id=0, cause_id="pay", claim=claim, from_account=account, amount=10)
    assert request.claim._belongs_to(owner, 7)
    assert not request.claim._belongs_to(owner, 0)
    assert not request.claim._belongs_to(object(), 7)

    restored = PayClaim.model_validate_json(request.model_dump_json(by_alias=True))
    assert restored.claim == ClaimId(month=3, index=2)
    assert not restored.claim._belongs_to(owner, 7)
    assert restored.amount == request.amount


if __name__ == "__main__":
    pytest_bazel.main()
