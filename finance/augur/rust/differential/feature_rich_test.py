"""Rust/JAX differential coverage for the generated feature-rich scenario, compared frame by
frame across every canonical event channel.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

import pytest_bazel

from finance.augur.rust.differential.backend import assert_backends_agree
from finance.augur.sim.testing.case import Case

# Every policy family the scenario is built to exercise, named by a record channel that is
# empty unless that family actually ran. Comparing two engines that both did nothing would
# pass, so this is what makes the frame-by-frame agreement above mean something.
EXERCISED_EVENT_FRAMES = (
    "lot_dispositions",
    "private_equity_events",
    "private_equity_opportunities",
    "obligation_accruals",
    "obligation_settlements",
    "tax_accruals",
    "tax_breakdowns",
    "tax_settlements",
    "property_purchases",
    "set_primary_residence_events",
    "set_rented_fraction_events",
    "capital_improvement_events",
    "property_sale_events",
    "mortgage_originations",
    "mortgage_payments",
)


def test_backends_agree_on_the_feature_rich_scenario(feature_rich: Case) -> None:
    """The whole scenario, every state channel and every canonical event frame."""

    result = assert_backends_agree(feature_rich)

    assert result.rollout_status.get_column("status").unique().to_list() == ["active"]
    for name in EXERCISED_EVENT_FRAMES:
        assert not getattr(result.events, name).is_empty(), f"{name} never fired"
    for name in ("bond_cashflows", "distributions"):
        assert not getattr(result, name).is_empty(), f"{name} never fired"

    causes = set(result.events.lot_dispositions.get_column("cause_id"))
    assert {"tlh-half-sale", "tlh-final-sale"} <= causes
    assert any(cause.startswith("benchmark-allocation_") for cause in causes)
    assert any(cause.startswith(("pe_forced_sale_", "pe_forced_recovery_")) for cause in causes)
    assert any(cause.startswith(("pe_tender_", "pe_public_market_")) for cause in causes)

    # A policy-bought lot records the month its own rollout paid, not a compile-time one.
    bought = result.lots.filter(
        result.lots.get_column("lot_id").str.starts_with("benchmark-allocation_buy_")
        & (result.lots.get_column("remaining_quantity_quanta") > 0)
    )
    assert not bought.is_empty()
    assert bought.get_column("purchase_month_index").min() >= 0


if __name__ == "__main__":
    pytest_bazel.main()
