"""Divergences the fuzzer found that the engines have not been made to agree on yet.

A burn-down, not an archive: an entry leaves this file when the two engines agree, taking its
case with it. Each pins what each engine answers today, so a change to either side fails here
and whoever made it decides which answer was meant — rather than the disagreement quietly
moving to a new number.

**This file is the only thing guarding the failure month.** The fuzz targets used to fail on
everything recorded here, which was the right default while the list was expected to empty
out; it left `structural_fuzz_test` permanently red on a question nobody was going to answer
soon. `assert_results_agree` now compares event frames outside a failed rollout's failure
month, so the cases below are where both engines' answers in that month are written down. If
either engine changes what it records there, these tests fail and the change has to say which
answer it meant — but a *new* divergence confined to the failure month will no longer be
found for you.

What a frozen rollout reports for *later* months is settled: it reports nothing, and both
engines now agree. What remains is the failure month itself, which is not a month-level
question at all. Rust stops inside its month loop at the phase that could not pay, so whether
a phase was recorded depends on where it sits in that order — the year-end assessment runs
before the tax payment and is kept, the private-equity protocol pass runs after the
obligation payment and is not. JAX cannot leave a vectorized scan partway through a month, so
it reports the whole failure month or none of it, and no month-level rule reproduces an
ordering within one.

Two entries below are that ordering seen from opposite sides, and together they are the
proof. A rollout that fails paying its tax true-up still records the year-end assessment,
because the assessment runs first — so `month <= failure` is what Rust does there. A rollout
that fails an obligation in its year-end month records no accrual at all, because the accrual
runs later — so `month < failure` is what Rust does there. No single month-level cut is both.
"""

from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.rust.result import run_rust
from finance.augur.sim.scenario import FixedAmount, InitialAccountBalance, InitialLot, PrivateEquityTenderPolicy
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import cash_spend, checking, taxed
from finance.augur.sim.testing.jax_result import run_jax

ACME = IssuerId("acme")


YEAR_END_MONTH = 11


def frozen_accrual_case(*, affordable: bool) -> Case:
    """A taxed agent whose obligation in the year-end month is more than they have."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(100)), ("vendor", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=YEAR_END_MONTH + 1,
            scheduled_obligations=[
                cash_spend(
                    "year-end-bill",
                    month=YEAR_END_MONTH,
                    agent_id="alice",
                    to_agent_id="vendor",
                    amount_due=Decimal(10) if affordable else Decimal(1_000),
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
    )


@pytest.mark.parametrize("affordable", [True, False], ids=["funded", "frozen"])
def test_a_rollout_that_froze_in_its_year_end_month_accrues_only_in_jax(affordable: bool) -> None:
    """The other side of the ordering, and the reason no month-level rule can settle it.

    The year-end accrual runs after the obligation pass, so a rollout that could not pay in the
    year-end month never reaches it in Rust — where the tax true-up case has Rust recording the
    assessment in the month it failed, because that assessment runs *before* the payment it
    could not make. One says keep the failure month, the other says drop it.

    The funded case is the anchor: with the bill payable both engines accrue, so the frozen one
    shows the freeze and not a scenario that never accrued.
    """

    case = frozen_accrual_case(affordable=affordable)
    accrued = [(YEAR_END_MONTH, "alice")] if affordable else []
    jax_rows = run_jax(case).events.tax_accruals.select(["month_index", "agent_id"]).rows()
    assert jax_rows == [(YEAR_END_MONTH, "alice")]
    assert run_rust(case).events.tax_accruals.select(["month_index", "agent_id"]).rows() == accrued


def frozen_private_equity_case(*, freeze: bool) -> Case:
    """A private-equity holding whose issuer marks itself up every month after month 0.

    With `freeze`, an obligation the owner cannot fund stops the rollout at month 1, which is
    before the second and third marks the protocol still publishes.
    """

    kinds = [PrivateEquityEventKindCode.NONE] + [PrivateEquityEventKindCode.ADMIN_MARK_UPDATE] * 3
    snapshots = len(kinds)
    horizon_months = snapshots - 1
    return Case(
        scenario=scenario(
            [
                InitialAccountBalance(agent_id="pe_owner", account_id="checking", balance=Decimal(100)),
                InitialAccountBalance(agent_id="pe_owner", account_id="private", balance=Decimal(0)),
                InitialAccountBalance(agent_id="vendor", account_id="checking", balance=Decimal(0)),
            ],
            horizon_months=horizon_months,
            tax_profiles=[],
            scheduled_obligations=(
                [
                    cash_spend(
                        "unfundable", month=1, agent_id="pe_owner", to_agent_id="vendor", amount_due=Decimal(1_000)
                    )
                ]
                if freeze
                else []
            ),
            initial_lots=[
                InitialLot(
                    lot_id="pe-acme",
                    agent_id="pe_owner",
                    account_id="private",
                    asset=PrivateEquityAssetKey(issuer_id=ACME),
                    purchase_month_index=-12,
                    quantity=10.0,
                    cost_basis_per_unit=Decimal(10),
                )
            ],
            private_equity_tender_policies=[
                PrivateEquityTenderPolicy(
                    owner_agent_id="pe_owner",
                    proceeds_account_id="checking",
                    liquid_net_worth_floor=FixedAmount(amount=Decimal(0)),
                )
            ],
        ),
        rollout_count=1,
        private_equity=PrivateEquityBundle.from_issuer_arrays(
            ACME,
            mark_usd_per_unit=np.full((1, snapshots), 100.0),
            regime_code=np.full((1, snapshots), int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64),
            event_kind_code=np.asarray([[int(kind) for kind in kinds]], dtype=np.int64),
            sale_opportunity_active=np.zeros((1, snapshots), dtype=bool),
            sale_capacity_fraction=np.ones((1, snapshots)),
            eligible_fraction=np.ones((1, snapshots)),
            forced_sale_fraction=np.zeros((1, snapshots)),
            liquidity_blocked=np.zeros((1, snapshots), dtype=bool),
            forced_recovery_cashout_usd=np.zeros((1, snapshots)),
            company_valuation_usd=np.zeros((1, snapshots)),
            rollout_count=1,
            horizon_months=horizon_months,
        ),
    )


@pytest.mark.parametrize("freeze", [False, True], ids=["funded", "frozen"])
def test_jax_reports_private_equity_events_a_frozen_rollout_never_saw(freeze: bool) -> None:
    """The two engines disagree about the failure month, and only about the failure month.

    The rollout freezes at month 1, which is also when the issuer publishes its first mark.
    Rust's protocol pass sits after the obligation payment in its month loop, so the month it
    could not pay ends before the pass runs and the mark is never recorded. JAX steps whole
    months, so it reports that month's mark and then stops — the marks at months 2 and 3, which
    it used to report as well, are gone.

    The funded case is the anchor: with the obligation payable both engines report the same
    two events, so what the frozen case shows is the freeze and not the holding.
    """

    case = frozen_private_equity_case(freeze=freeze)
    marks = [(1, "admin_mark_update"), (2, "admin_mark_update")]
    jax_events = run_jax(case).events.private_equity_events
    assert jax_events.select(["month_index", "event_kind"]).rows() == (marks[:1] if freeze else marks)
    assert run_rust(case).events.private_equity_events.select(["month_index", "event_kind"]).rows() == (
        [] if freeze else marks
    )


if __name__ == "__main__":
    pytest_bazel.main()
