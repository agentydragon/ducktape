"""Augur run-loop phase functions: each operates on (plan, buffers, current, month)
and mutates `current` in place. Called from engine._run_month_step in fixed order.
Cross-phase helpers (_sale_unit_price, _record_capital_gains) live here too since
multiple phase functions invoke them."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from augur.model.series import PrivateEquityRegimeCode
from augur.sim.buffers import CurrentStateBuffers, SimulationBuffers
from augur.sim.codec.helpers import text
from augur.sim.compiler import CompiledSimulation
from augur.sim.compiler.helpers import AMOUNT_FIXED, NO_CODE
from augur.sim.enums import (
    CapitalGainClassification,
    LifecycleKind,
    ObligationSource,
    PrivateEquityDispositionKind,
    PrivateEquityOpportunityOutcome,
)
from augur.sim.tax import net_capital_gains_with_carryforward
from augur.sim.tensor_fifo import FifoSaleResult, fifo_sell_dollars, fifo_sell_units, lot_order_for_pool
from augur.sim.tlh_harvest import monthly_harvest_fraction, split_short_long


def _apply_scheduled_transfers(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    for slot in range(plan.transfers.cause.shape[1]):
        if plan.transfers.cause[month, slot] < 0:
            continue
        amount = _amount_values(
            plan,
            kind=int(plan.transfers.amount_kind[month, slot]),
            fixed=float(plan.transfers.amount_fixed[month, slot]),
            base=float(plan.transfers.amount_base[month, slot]),
            series_index=int(plan.transfers.amount_series[month, slot]),
            base_month=int(plan.transfers.amount_base_month[month, slot]),
            adjustment_period=int(plan.transfers.amount_period[month, slot]),
            month=month,
        )
        buffers.transfers.active[month, slot, active_rollout] = True
        buffers.transfers.amount[month, slot, active_rollout] = amount[active_rollout]
        from_slot = int(plan.transfers.from_slot[month, slot])
        if from_slot >= 0:
            current.cash[from_slot, active_rollout] -= amount[active_rollout]
        to_slot = int(plan.transfers.to_slot[month, slot])
        if to_slot >= 0:
            current.cash[to_slot, active_rollout] += amount[active_rollout]
        profile = int(plan.transfers.income_profile[month, slot])
        if profile >= 0:
            current.ordinary_ytd[profile, active_rollout] += amount[active_rollout]
        deduction_profile = int(plan.transfers.deduction_profile[month, slot])
        if deduction_profile >= 0:
            current.ordinary_ytd[deduction_profile, active_rollout] -= amount[active_rollout]


def _amount_values(
    plan: CompiledSimulation,
    *,
    kind: int,
    fixed: float,
    base: float,
    series_index: int,
    base_month: int,
    adjustment_period: int,
    month: int,
) -> np.ndarray:
    if kind == AMOUNT_FIXED:
        return np.full(plan.rollout_count, fixed, dtype=np.float64)
    elapsed = month - base_month
    reset_month = base_month + (elapsed // adjustment_period) * adjustment_period
    base_level = plan.external_values[series_index, :, base_month]
    reset_level = plan.external_values[series_index, :, reset_month]
    return base * reset_level / base_level


def _compute_tax_for_link(
    plan: CompiledSimulation, current: CurrentStateBuffers, *, link: int, salt_deduction: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Run the bracket math for one tax link, given a pre-computed SALT addition.

    Returns `(mortgage_interest_deduction, itemized_deduction, ordinary_taxable,
    capital_taxable, ordinary_tax, capital_tax)`. `salt_deduction` is zero for
    non-SALT links; for the federal SALT link it carries the capped SALT total
    that should stack onto MID inside itemized.

    §1250 unrecaptured-depreciation gain is routed by `tax_link_section_1250_rate`:
    - Federal-style links (rate > 0): the recapture stays out of the ordinary bracket
      walk. After `ordinary_tax` is known, the IRS Unrecaptured §1250 Worksheet rule
      taxes the recapture at the *lesser of* its implied marginal ordinary rate (what
      it would owe if stacked on top of `ordinary_taxable`) or the flat cap rate (25%
      on `federal_us`). The result is added to `capital_tax`.
    - State-style links (rate == 0): the recapture is added to ordinary income and
      flows through the standard bracket walk (CA treats it as ordinary).
    """

    profile = int(plan.tax.link_profile[link])
    gain_profile = int(plan.tax_profile_capital_gain_index[profile])
    ordinary = current.ordinary_ytd[profile, :]
    ltcg = current.capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM, :]
    stcg = current.capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM, :]
    recapture = current.recapture_section_1250_ytd[profile, :]
    section_1250_rate = float(plan.tax.link_section_1250_rate[link])
    standard_deduction = float(plan.tax.link_standard_deduction[link])
    if bool(plan.mid.link_active[link]):
        # MID applies only to the owner-occupied share of interest. Rented-share interest
        # is deducted via the Schedule E hook at the top of `_apply_tax_accruals`.
        owner_interest_ytd = current.liability_interest_ytd - current.liability_rental_interest_ytd
        mortgage_interest_deduction = plan.mid.principal_ratio[link] @ owner_interest_ytd
    else:
        mortgage_interest_deduction = np.zeros(plan.rollout_count, dtype=np.float64)
    itemized_deduction = mortgage_interest_deduction + salt_deduction
    deduction_used = np.maximum(itemized_deduction, standard_deduction)

    # State-style §1250 lumps recapture into ordinary income; federal-style holds it out
    # so the IRS worksheet cap can apply after the bracket walk.
    federal_style_section_1250 = section_1250_rate > 0.0
    ordinary_for_brackets = ordinary if federal_style_section_1250 else ordinary + recapture

    ordinary_upper = plan.tax.link_ordinary_upper[link]
    ordinary_rate = plan.tax.link_ordinary_rate[link]
    ordinary_count = int(plan.tax.link_ordinary_count[link])
    if int(plan.tax.link_has_ltcg[link]) == 1:
        ordinary_taxable = np.maximum(ordinary_for_brackets + stcg - deduction_used, 0.0)
        capital_taxable = ltcg
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = _apply_ltcg_brackets(
            ltcg,
            ordinary_taxable,
            upper=plan.tax.link_ltcg_upper[link],
            rate=plan.tax.link_ltcg_rate[link],
            count=int(plan.tax.link_ltcg_count[link]),
        )
    else:
        ordinary_taxable = np.maximum(ordinary_for_brackets + ltcg + stcg - deduction_used, 0.0)
        capital_taxable = np.zeros(plan.rollout_count, dtype=np.float64)
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = np.zeros(plan.rollout_count, dtype=np.float64)

    if federal_style_section_1250:
        # IRS Unrecaptured §1250 Gain Worksheet: lesser of the implied marginal ordinary
        # tax on the recapture (what it would owe stacked on top of ordinary_taxable) or
        # the flat federal cap. Sub-25%-bracket taxpayers benefit from the marginal floor;
        # high-bracket taxpayers are unchanged because the 25% cap binds.
        ordinary_tax_with_recapture = _apply_brackets(
            ordinary_taxable + recapture, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count
        )
        implied_recapture_tax = np.maximum(ordinary_tax_with_recapture - ordinary_tax, 0.0)
        section_1250_tax = np.minimum(implied_recapture_tax, recapture * section_1250_rate)
    else:
        section_1250_tax = np.zeros(plan.rollout_count, dtype=np.float64)

    capital_tax = ltcg_tax + section_1250_tax
    return mortgage_interest_deduction, itemized_deduction, ordinary_taxable, capital_taxable, ordinary_tax, capital_tax


def _write_tax_link_buffers(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    current: CurrentStateBuffers,
    *,
    link: int,
    month: int,
    active_rollout: np.ndarray,
    standard_deduction: float,
    mortgage_interest_deduction: np.ndarray,
    salt_deduction: np.ndarray,
    itemized_deduction: np.ndarray,
    ordinary_taxable: np.ndarray,
    capital_taxable: np.ndarray,
    ordinary_tax: npt.NDArray[np.float64],
    capital_tax: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    profile = int(plan.tax.link_profile[link])
    gain_profile = int(plan.tax_profile_capital_gain_index[profile])
    ordinary = current.ordinary_ytd[profile, :]
    ltcg = current.capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM, :]
    stcg = current.capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM, :]
    tax: npt.NDArray[np.float64] = ordinary_tax + capital_tax
    buffers.taxes.accrual_active[month, link, active_rollout] = True
    buffers.taxes.accrual_amount[month, link, active_rollout] = tax[active_rollout]
    buffers.taxes.breakdown_ordinary[month, link, active_rollout] = ordinary[active_rollout]
    buffers.taxes.breakdown_ltcg[month, link, active_rollout] = ltcg[active_rollout]
    buffers.taxes.breakdown_stcg[month, link, active_rollout] = stcg[active_rollout]
    buffers.taxes.breakdown_standard_deduction[month, link, active_rollout] = standard_deduction
    buffers.taxes.breakdown_mortgage_interest_deduction[month, link, active_rollout] = mortgage_interest_deduction[
        active_rollout
    ]
    buffers.taxes.breakdown_salt_deduction[month, link, active_rollout] = salt_deduction[active_rollout]
    buffers.taxes.breakdown_itemized_deduction[month, link, active_rollout] = itemized_deduction[active_rollout]
    buffers.taxes.breakdown_ordinary_taxable[month, link, active_rollout] = ordinary_taxable[active_rollout]
    buffers.taxes.breakdown_capital_taxable[month, link, active_rollout] = capital_taxable[active_rollout]
    buffers.taxes.breakdown_ordinary_tax[month, link, active_rollout] = ordinary_tax[active_rollout]
    buffers.taxes.breakdown_capital_tax[month, link, active_rollout] = capital_tax[active_rollout]

    tax_slot = _tax_liability_slot_for(plan, profile_index=profile, link_index=link, year_end_month=month)
    if tax_slot >= 0:
        current.tax_liability_active[tax_slot, active_rollout] = True
        current.tax_liability_amount[tax_slot, active_rollout] = tax[active_rollout]
    return tax


def _apply_tlh_harvest(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    """Reduced-form tax-loss-harvesting (Piece 2b). LIMITED, DELIBERATELY-APPROXIMATE MODEL.

    This is NOT a real direct-indexing harvester. It does not simulate the sleeve's constituent
    stocks, does not track which names are below basis, and does not run FIFO on sub-lots. For
    each `HarvestPolicy` it instead books a CALIBRATED capital loss each month — a function of the
    index path Augur already samples (`augur/sim/tlh_harvest.py`), shaped by the position's
    embedded-gain fraction and amplified in drawdowns. Every parameter is `[HEURISTIC]`, anchored
    only to the account's first-year (TY2025) 1099-B; the decay rate is an external prior, not
    fitted (no prior-year forms exist). See `augur/plans/tax_loss_harvesting.md`.

    The harvested loss is honest tax DEFERRAL, not free money: the loss is offset by a basis
    give-back at sale (`_record_capital_gains` reduces the sold lots' basis by the accumulated
    `tlh_cumulative_harvest`). The net lifetime benefit is bounded — it can only come from
    rate-arbitrage (ST loss now vs LT gain later), the $3k/yr ordinary offset, and deferral timing.

    Per policy, per active rollout this month:
      MV               = sum over the policy's lots of remaining_units * index_price[month]
      original_basis   = sum over the policy's lots of remaining_units * cost_basis_per_unit
      adjusted_basis   = max(0, original_basis - tlh_cumulative_harvest)   # give-back already taken
      e                = clip((MV - adjusted_basis) / MV, 0, 1)            # embedded-gain fraction
      period_return    = index_price[month] / index_price[month-1] - 1     # 0 at month 0
      gross_harvest    = MV * monthly_harvest_fraction(period_return, e, params)
      gross_harvest    = min(gross_harvest, original_basis - tlh_cumulative_harvest)  # loss ceiling
    Then split ST/LT by the policy's short_term_fraction and inject the loss as a NEGATIVE into
    capital_gain_ytd[gain_profile, ST|LT, :] (Piece-1 netting handles it unchanged), and add the
    gross to tlh_cumulative_harvest. The ceiling keeps adjusted_basis >= 0 and e in [0, 1]; the
    e -> floor decay already prevents over-harvesting in long bull runs (a vol-tied ceiling is a
    documented future refinement — see the plan — and is intentionally NOT built here).

    What a more honest / less fake implementation would look like (do not build now): the plan's
    option #3 — 5-10 representative sleeves, each = index factor + scaled idiosyncratic noise, with
    REAL FIFO harvesting on sub-lots so losses emerge from actual below-basis names; and option #4
    — a full factor model with hundreds of names (explicitly never to be built: unobservable,
    uncalibratable parameters).
    """

    policy_count = plan.harvest_policies.gain_profile_index.shape[0]
    if policy_count == 0:
        return
    active_rollout = ~current.failed
    if not active_rollout.any():
        return

    harvest = plan.harvest_policies
    for policy_idx in range(policy_count):
        gain_profile = int(harvest.gain_profile_index[policy_idx])
        if gain_profile < 0:
            continue  # owner has no capital-gain profile; nothing to net a harvested loss against
        lot_indices = np.flatnonzero(harvest.lot_mask[policy_idx])
        if lot_indices.size == 0:
            continue
        series_index = int(harvest.series_index[policy_idx])
        price = plan.external_values[series_index, :, month]  # (R,)
        if not np.isfinite(price).all() or (price < 0.0).any():
            raise ValueError(f"harvest policy {policy_idx} index series produced a negative or non-finite price")

        remaining = current.lot_remaining[lot_indices, :]  # (lot, R)
        market_value = (remaining * price[None, :]).sum(axis=0)  # (R,)
        original_basis = (remaining * plan.lot_cost_basis_per_unit[lot_indices, None]).sum(axis=0)  # (R,)
        cumulative = current.tlh_cumulative_harvest[policy_idx, :]  # (R,)
        adjusted_basis = np.maximum(0.0, original_basis - cumulative)
        embedded_gain_fraction = np.divide(
            market_value - adjusted_basis, market_value, out=np.zeros_like(market_value), where=market_value > 0.0
        )

        # Period return drives the drawdown kicker. Month 0 has no prior price, so treat it as flat
        # (return 0): the position still harvests at its base monthly rate.
        if month == 0:
            period_return = np.zeros(plan.rollout_count, dtype=np.float64)
        else:
            prior_price = plan.external_values[series_index, :, month - 1]
            period_return = np.divide(
                price - prior_price, prior_price, out=np.zeros_like(price), where=prior_price > 0.0
            )

        fraction = monthly_harvest_fraction(period_return, embedded_gain_fraction, harvest.params[policy_idx])
        gross_harvest = market_value * fraction
        # Loss ceiling: never harvest more than the remaining below-basis room, so cumulative
        # harvest stays <= original_basis (adjusted_basis >= 0). Only harvest on active rollouts.
        ceiling = np.maximum(0.0, original_basis - cumulative)
        gross_harvest = np.where(active_rollout, np.minimum(np.maximum(gross_harvest, 0.0), ceiling), 0.0)
        if not (gross_harvest > 0.0).any():
            continue

        split = split_short_long(gross_harvest, harvest.short_term_fraction[policy_idx])
        # Inject the harvested loss as a NEGATIVE realized gain — never a synthetic gain or a tax
        # credit. Piece-1's `net_capital_gains_with_carryforward` nets it like any other loss.
        if (split.short_term_usd > 0.0).any():
            current.capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM, :] -= split.short_term_usd
            current.capital_gain_active[
                gain_profile, CapitalGainClassification.SHORT_TERM, split.short_term_usd > 0.0
            ] = True
        if (split.long_term_usd > 0.0).any():
            current.capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM, :] -= split.long_term_usd
            current.capital_gain_active[
                gain_profile, CapitalGainClassification.LONG_TERM, split.long_term_usd > 0.0
            ] = True
        # Accumulate the give-back scalar: this is exactly the deferred gain repaid at sale.
        current.tlh_cumulative_harvest[policy_idx, :] += gross_harvest


def _apply_pe_tenders(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    """Fire LNW-floor-driven private-equity tender sales for any issuer whose sampled tender
    event activates this month.

    Per issuer with a policy assignment:
      1. Look up the per-rollout boolean of "tender fires this month".
      2. If any rollout fires: read the issuer's per-rollout mark (level series).
      3. Compute the owner's liquid net worth = cash + non-PE lot value.
      4. shortfall = max(0, floor - LNW), capped by available PE value.
      5. Apply required issuer-level liquidity controls from exogenous series: blocked
         liquidity prevents sale, while eligible/capacity fractions limit sellable units.
      6. Drain FIFO from the issuer's lots at the mark, credit proceeds to the policy's
         designated cash slot, accrue the cap gain to the owner's capital_gain_ytd.

    Multiple issuers tendering the same month are processed in array order; each updates
    cash and lot_remaining before the next issuer's LNW computation runs, so the floor
    genuinely caps aggregate sale across same-month tenders.
    """

    issuer_count = plan.pe_issuers.codes.shape[0]
    if issuer_count == 0:
        return
    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    channels = plan.pe_channels
    for issuer_idx in range(issuer_count):
        if int(plan.pe_issuers.codes[issuer_idx]) < 0:
            continue
        policy_idx = int(plan.pe_issuers.policy_index[issuer_idx])
        mark = channels.marks[issuer_idx, :, month]
        if not np.isfinite(mark).all() or (mark < 0.0).any():
            raise ValueError(
                f"private-equity mark series for issuer {text(plan, plan.pe_issuers.codes[issuer_idx])!r} "
                "produced a negative or non-finite value"
            )
        positive_mark = mark > 0.0
        tender_active = channels.sale_opportunity_active[issuer_idx, :, month] & active_rollout
        regime_code = channels.regime_codes[issuer_idx, :, month]
        public_market_active = regime_code == int(PrivateEquityRegimeCode.PUBLIC_MARKET)
        liquidity_open = ~channels.liquidity_blocked[issuer_idx, :, month]
        forced_sale_fraction = channels.forced_sale_fractions[issuer_idx, :, month]
        forced_recovery_cashout_usd = channels.forced_recovery_cashout_usd[issuer_idx, :, month]
        if (forced_recovery_cashout_usd < 0.0).any():
            raise ValueError("private-equity forced-recovery cashout series produced a negative value")

        lot_indices = np.flatnonzero(plan.pe_issuers.lot_mask[issuer_idx])
        if lot_indices.size == 0:
            continue
        ordered_lots = lot_indices[np.argsort(plan.lot_purchase_month[lot_indices], kind="stable")]
        units_held = current.lot_remaining[ordered_lots, :].sum(axis=0)
        sale_capacity_fraction = channels.sale_capacity_fractions[issuer_idx, :, month]
        eligible_fraction = channels.eligible_fractions[issuer_idx, :, month]

        if policy_idx < 0:
            _record_pe_opportunity(
                buffers,
                month=month,
                issuer_idx=issuer_idx,
                active=tender_active,
                outcome=np.full(plan.rollout_count, int(PrivateEquityOpportunityOutcome.NO_POLICY), dtype=np.int64),
                floor=np.zeros(plan.rollout_count, dtype=np.float64),
                liquid_net_worth=np.zeros(plan.rollout_count, dtype=np.float64),
                shortfall=np.zeros(plan.rollout_count, dtype=np.float64),
                units_held=units_held,
                sellable_units=units_held * sale_capacity_fraction * eligible_fraction,
                target_units=np.zeros(plan.rollout_count, dtype=np.float64),
                proceeds=np.zeros(plan.rollout_count, dtype=np.float64),
            )
            continue

        recovery_active = (forced_recovery_cashout_usd > 0.0) & active_rollout & (units_held > 0.0)
        if recovery_active.any():
            recovery_unit_price = np.divide(
                forced_recovery_cashout_usd,
                units_held,
                out=np.ones_like(forced_recovery_cashout_usd),
                where=units_held > 0.0,
            )
            recovery_result = fifo_sell_units(
                lot_remaining=current.lot_remaining.T,
                ordered_lots=ordered_lots,
                target_units=np.where(recovery_active, units_held, 0.0),
                unit_price=recovery_unit_price,
                cost_basis_per_unit=plan.lot_cost_basis_per_unit,
            )
            _apply_pe_sale_result(
                plan,
                buffers,
                current,
                month=month,
                issuer_idx=issuer_idx,
                policy_idx=policy_idx,
                disposition_kind=PrivateEquityDispositionKind.FORCED_RECOVERY,
                result=recovery_result,
                oversell_label="PE forced recovery",
            )

        units_held = current.lot_remaining[ordered_lots, :].sum(axis=0)
        forced_sale_active = (forced_sale_fraction > 0.0) & active_rollout & positive_mark & (units_held > 0.0)
        if forced_sale_active.any():
            forced_sale_result = fifo_sell_units(
                lot_remaining=current.lot_remaining.T,
                ordered_lots=ordered_lots,
                target_units=np.where(forced_sale_active, units_held * forced_sale_fraction, 0.0),
                unit_price=mark,
                cost_basis_per_unit=plan.lot_cost_basis_per_unit,
            )
            _apply_pe_sale_result(
                plan,
                buffers,
                current,
                month=month,
                issuer_idx=issuer_idx,
                policy_idx=policy_idx,
                disposition_kind=PrivateEquityDispositionKind.FORCED_SALE,
                result=forced_sale_result,
                oversell_label="PE forced sale",
            )

        floor = _amount_values(
            plan,
            kind=int(plan.pe_policies.floor_kind[policy_idx]),
            fixed=float(plan.pe_policies.floor_fixed[policy_idx]),
            base=float(plan.pe_policies.floor_base[policy_idx]),
            series_index=int(plan.pe_policies.floor_series[policy_idx]),
            base_month=int(plan.pe_policies.floor_base_month[policy_idx]),
            adjustment_period=int(plan.pe_policies.floor_period[policy_idx]),
            month=month,
        )
        lnw = _compute_liquid_net_worth(plan, current, policy_idx=policy_idx, month=month)
        shortfall = np.maximum(0.0, floor - lnw)
        units_held = current.lot_remaining[ordered_lots, :].sum(axis=0)
        sellable_units = units_held * sale_capacity_fraction * eligible_fraction
        shortfall_units = np.divide(shortfall, mark, out=np.zeros_like(shortfall), where=mark > 0.0)
        target_units = np.minimum(shortfall_units, sellable_units)
        opportunity_active = (tender_active | public_market_active) & active_rollout & liquidity_open & positive_mark
        target_units = np.where(opportunity_active, target_units, 0.0)
        opportunity_outcome = np.full(plan.rollout_count, int(PrivateEquityOpportunityOutcome.SOLD), dtype=np.int64)
        opportunity_outcome = np.where(
            shortfall <= 0.0, int(PrivateEquityOpportunityOutcome.FLOOR_SATISFIED), opportunity_outcome
        )
        opportunity_outcome = np.where(
            (sale_capacity_fraction * eligible_fraction) <= 0.0,
            int(PrivateEquityOpportunityOutcome.CAPACITY_ZERO),
            opportunity_outcome,
        )
        opportunity_outcome = np.where(
            ~positive_mark, int(PrivateEquityOpportunityOutcome.NONPOSITIVE_MARK), opportunity_outcome
        )
        opportunity_outcome = np.where(
            channels.liquidity_blocked[issuer_idx, :, month],
            int(PrivateEquityOpportunityOutcome.LIQUIDITY_BLOCKED),
            opportunity_outcome,
        )
        opportunity_outcome = np.where(
            units_held <= 0.0, int(PrivateEquityOpportunityOutcome.NO_UNITS), opportunity_outcome
        )
        _record_pe_opportunity(
            buffers,
            month=month,
            issuer_idx=issuer_idx,
            active=tender_active,
            outcome=opportunity_outcome,
            floor=floor,
            liquid_net_worth=lnw,
            shortfall=shortfall,
            units_held=units_held,
            sellable_units=sellable_units,
            target_units=target_units,
            proceeds=target_units * mark,
        )
        if not (target_units > 0.0).any():
            continue

        # `fifo_sell_units` works in (R, L); current.lot_remaining is (L, R) per B0,
        # so transpose at the call seam. Tender and public-market liquidity can differ by
        # rollout in one vectorized batch, so record them into separate disposition-kind slots.
        _apply_pe_target_units_sale(
            plan,
            buffers,
            current,
            month=month,
            issuer_idx=issuer_idx,
            policy_idx=policy_idx,
            ordered_lots=ordered_lots,
            mark=mark,
            target_units=np.where(tender_active & ~public_market_active, target_units, 0.0),
            disposition_kind=PrivateEquityDispositionKind.TENDER,
            oversell_label="PE tender",
        )
        _apply_pe_target_units_sale(
            plan,
            buffers,
            current,
            month=month,
            issuer_idx=issuer_idx,
            policy_idx=policy_idx,
            ordered_lots=ordered_lots,
            mark=mark,
            target_units=np.where(public_market_active, target_units, 0.0),
            disposition_kind=PrivateEquityDispositionKind.PUBLIC_MARKET,
            oversell_label="PE public market sale",
        )


def _record_pe_opportunity(
    buffers: SimulationBuffers,
    *,
    month: int,
    issuer_idx: int,
    active: npt.NDArray[np.bool_],
    outcome: npt.NDArray[np.int64],
    floor: npt.NDArray[np.float64],
    liquid_net_worth: npt.NDArray[np.float64],
    shortfall: npt.NDArray[np.float64],
    units_held: npt.NDArray[np.float64],
    sellable_units: npt.NDArray[np.float64],
    target_units: npt.NDArray[np.float64],
    proceeds: npt.NDArray[np.float64],
) -> None:
    if not active.any():
        return
    destination = buffers.private_equity_opportunities
    destination.active[month, issuer_idx, active] = True
    destination.outcome[month, issuer_idx, active] = outcome[active]
    destination.floor[month, issuer_idx, active] = floor[active]
    destination.liquid_net_worth[month, issuer_idx, active] = liquid_net_worth[active]
    destination.shortfall[month, issuer_idx, active] = shortfall[active]
    destination.units_held[month, issuer_idx, active] = units_held[active]
    destination.sellable_units[month, issuer_idx, active] = sellable_units[active]
    destination.target_units[month, issuer_idx, active] = target_units[active]
    destination.proceeds[month, issuer_idx, active] = proceeds[active]


def _apply_pe_target_units_sale(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    current: CurrentStateBuffers,
    *,
    month: int,
    issuer_idx: int,
    policy_idx: int,
    ordered_lots: npt.NDArray[np.int64],
    mark: npt.NDArray[np.float64],
    target_units: npt.NDArray[np.float64],
    disposition_kind: PrivateEquityDispositionKind,
    oversell_label: str,
) -> None:
    if not (target_units > 0.0).any():
        return
    result = fifo_sell_units(
        lot_remaining=current.lot_remaining.T,
        ordered_lots=ordered_lots,
        target_units=target_units,
        unit_price=mark,
        cost_basis_per_unit=plan.lot_cost_basis_per_unit,
    )
    _apply_pe_sale_result(
        plan,
        buffers,
        current,
        month=month,
        issuer_idx=issuer_idx,
        policy_idx=policy_idx,
        disposition_kind=disposition_kind,
        result=result,
        oversell_label=oversell_label,
    )


def _apply_pe_sale_result(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    current: CurrentStateBuffers,
    *,
    month: int,
    issuer_idx: int,
    policy_idx: int,
    disposition_kind: PrivateEquityDispositionKind,
    result: FifoSaleResult,
    oversell_label: str,
) -> None:
    if result.oversell.any():
        raise ValueError(
            f"{oversell_label} attempted to sell more than available lots for issuer "
            f"{text(plan, plan.pe_issuers.codes[issuer_idx])}"
        )
    current.lot_remaining -= result.sold_units.T
    proceeds_slot = int(plan.pe_policies.proceeds_cash_slot[policy_idx])
    if proceeds_slot >= 0:
        current.cash[proceeds_slot, :] += result.total_proceeds
    owner_code = int(plan.pe_policies.owner_agent[policy_idx])
    _record_capital_gains(
        plan,
        current,
        month=month,
        agent_code=owner_code,
        sold_units=result.sold_units,
        gains=result.proceeds - result.cost_basis_consumed,
    )
    sale_active = result.sold_units > 0.0  # (R, L)
    kind_idx = int(disposition_kind)
    buffers.lot_dispositions.pe.active[month, issuer_idx, kind_idx] |= sale_active.T  # (R, L) -> (lot, R)
    buffers.lot_dispositions.pe.units[month, issuer_idx, kind_idx] += result.sold_units.T
    buffers.lot_dispositions.pe.basis[month, issuer_idx, kind_idx] += result.cost_basis_consumed.T
    buffers.lot_dispositions.pe.proceeds[month, issuer_idx, kind_idx] += result.proceeds.T


def _compute_liquid_net_worth(
    plan: CompiledSimulation, current: CurrentStateBuffers, *, policy_idx: int, month: int
) -> npt.NDArray[np.float64]:
    """Per-rollout LNW = cash in policy-owner accounts + non-PE-lot value at current prices."""

    owner_cash_mask = plan.pe_policies.owner_cash_mask[policy_idx]
    cash_total = (current.cash * owner_cash_mask[:, None]).sum(axis=0)
    lot_mask = plan.pe_policies.owner_non_pe_lot_mask[policy_idx]
    if not lot_mask.any():
        return np.asarray(cash_total, dtype=np.float64)
    lot_indices = np.flatnonzero(lot_mask)
    series_indices = plan.lot_asset_series_index[lot_indices]
    valid = series_indices >= 0
    safe_series_indices = np.where(valid, series_indices, 0)
    prices = plan.external_values[safe_series_indices, :, month]  # (lot, rollout)
    prices = np.where(valid[:, None], prices, 0.0)
    prices = np.nan_to_num(prices, nan=0.0)
    quantities = current.lot_remaining[lot_indices, :]  # (lot, rollout)
    lot_value = (quantities * prices).sum(axis=0)
    return np.asarray(cash_total + lot_value, dtype=np.float64)


def _apply_lifecycle_events(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    """Apply this month's PropertyLifecycleEvent rows to per-rollout runtime state.

    Three kinds share the same machinery:
    - `LifecycleKind.FRACTION`: mutate `current.property_rented_fraction[prop, :]` to the
      event's new value.
    - `LifecycleKind.CAPITAL_IMPROVEMENT`: debit owner's cash by `amount_usd` and increase
      `current.property_building_basis[prop, :]` by the same amount.
    - `LifecycleKind.SALE`: dispatch to `_apply_property_sale` which also fills the per-event
      `sale_*` arrays on `buffers.lifecycle`.

    For each event that fires for an active rollout/property pair,
    `buffers.lifecycle.fired[event_index, r]` is set. The decoder turns this into a
    polars frame so the frontend can render markers.

    Phase 3 lifecycle events are deterministic per rollout. Future policy-driven decisions
    would emit per-rollout records; this apply machinery handles them by indexing the rollout
    subset.
    """

    starts = plan.lifecycle_events.month_starts
    if month + 1 >= starts.shape[0]:
        return
    begin = int(starts[month])
    end = int(starts[month + 1])
    if begin == end:
        return
    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    for i in range(begin, end):
        prop = int(plan.lifecycle_events.property_slot[i])
        kind = int(plan.lifecycle_events.kind[i])
        active_property_rollout = active_rollout & current.property_active[prop, :]
        if not active_property_rollout.any():
            continue
        if kind == LifecycleKind.FRACTION:
            new_fraction = float(plan.lifecycle_events.rented_fraction[i])
            current.property_rented_fraction[prop, active_property_rollout] = new_fraction
            buffers.lifecycle.fired[i, active_property_rollout] = True
        elif kind == LifecycleKind.CAPITAL_IMPROVEMENT:
            amount = float(plan.lifecycle_events.amount[i])
            owner_cash_slot = int(plan.properties.buyer_slot[prop])
            if owner_cash_slot >= 0:
                current.cash[owner_cash_slot, active_property_rollout] -= amount
            current.property_building_basis[prop, active_property_rollout] += amount
            buffers.lifecycle.fired[i, active_property_rollout] = True
        elif kind == LifecycleKind.SALE:
            _apply_property_sale(
                plan,
                buffers,
                current,
                month=month,
                event_index=i,
                prop=prop,
                closing_cost_pct=float(plan.lifecycle_events.amount[i]),
                active_rollout=active_property_rollout,
            )
            buffers.lifecycle.fired[i, active_property_rollout] = True


def _apply_primary_residence_events(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    """Apply this month's agent-level primary-residence assignment events."""

    starts = plan.primary_residence_events.month_starts
    if month + 1 >= starts.shape[0]:
        return
    begin = int(starts[month])
    end = int(starts[month + 1])
    if begin == end:
        return
    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    for event_index in range(begin, end):
        agent_slot = int(plan.primary_residence_events.agent_slot[event_index])
        property_slot = int(plan.primary_residence_events.property_slot[event_index])
        current.agent_primary_residence_property[agent_slot] = property_slot
        buffers.primary_residence.fired[event_index, active_rollout] = True


SECTION_121_LOOKBACK_MONTHS = 60
SECTION_121_MIN_QUALIFYING_MONTHS = 24
# Per-profile cap lives on the plan: `plan.tax.profile_section_121_exclusion[owner_profile]`.
# Compiler populates it from `_SECTION_121_EXCLUSION_USD_BY_FILING_STATUS`, which only knows the
# single-filer variant today — any other filing status raises NotImplementedError at compile
# time so no rollout silently runs with the wrong cap.


def _apply_property_sale(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    current: CurrentStateBuffers,
    *,
    month: int,
    event_index: int,
    prop: int,
    closing_cost_pct: float,
    active_rollout: np.ndarray,
) -> None:
    """Execute a PropertySaleEvent for one property and log the per-rollout amounts.

    - Market value = purchase_price × home_value_series[t] / home_value_series[base_month].
    - Gross proceeds = market value × (1 - closing_cost_pct / 100).
    - Net proceeds to owner cash = gross - outstanding mortgage balance.
    - Realized gain = gross_proceeds - (purchase_price + capex - cumulative_depreciation).
    - §1250 recapture = min(realized_gain, cumulative_dep) → routed to a dedicated YTD bucket
      so the federal link can tax it at 25% (its dedicated rate), while CA-style links treat
      it as ordinary income inside their standard bracket walk.
    - §121: if the property was owner-occupied at least
      `SECTION_121_MIN_QUALIFYING_MONTHS` of the last `SECTION_121_LOOKBACK_MONTHS`,
      exclude up to `plan.tax.profile_section_121_exclusion[owner_profile]` of the
      post-recapture gain from LTCG. The cap is keyed on filing status at compile time.
    - Remainder = post-exclusion LTCG → added to owner's long_term_capital_gain_ytd.
    - Mortgage paid off; property frozen (property_active → False, rented_fraction → 0,
      building_basis → 0, cumulative_depreciation preserved for record).
    - Per-rollout proceeds/payoff/gain/recapture/121-exclusion/ltcg are written to
      `buffers.lifecycle.sale_*[event_index, r]`.
    """

    rollout_count = plan.rollout_count
    sale_gross_proceeds = np.zeros(rollout_count, dtype=np.float64)
    sale_mortgage_payoff = np.zeros(rollout_count, dtype=np.float64)
    sale_net_cash = np.zeros(rollout_count, dtype=np.float64)
    sale_realized_gain = np.zeros(rollout_count, dtype=np.float64)
    sale_recapture = np.zeros(rollout_count, dtype=np.float64)
    sale_section_121 = np.zeros(rollout_count, dtype=np.float64)
    sale_long_term_gain = np.zeros(rollout_count, dtype=np.float64)

    series_idx = int(plan.property_home_value_series_index[prop])
    if series_idx < 0:
        property_id = plan.strings[int(plan.properties.id[prop])]
        msg = f"property sale for property_id {property_id!r} reached engine without a home-value series"
        raise RuntimeError(msg)
    base_value = plan.external_values[series_idx, :, 0]  # per-rollout, base month
    sale_value_series = plan.external_values[series_idx, :, month]  # per-rollout, sale month
    purchase_price = float(plan.properties.purchase_price[prop])
    market_value = purchase_price * sale_value_series / base_value  # (R,)
    gross_proceeds = market_value * (1.0 - closing_cost_pct / 100.0)

    # Adjusted basis = (purchase_price + capex done) - cumulative depreciation. The runtime
    # building_basis includes capex bumps but excludes land; reconstitute full basis below.
    initial_building_basis = float(plan.property_building_basis[prop])
    capex = current.property_building_basis[prop, :] - initial_building_basis
    cum_dep = current.property_cumulative_depreciation[prop, :]
    adjusted_basis = purchase_price + capex - cum_dep
    realized_gain = gross_proceeds - adjusted_basis
    recapture = np.minimum(np.maximum(realized_gain, 0.0), cum_dep)
    post_recapture_gain = np.maximum(realized_gain - recapture, 0.0)

    # §121 ownership/use test: count owner-occupied months in the last
    # SECTION_121_LOOKBACK_MONTHS. `property_owner_occupied_months` is cumulative-since-purchase
    # and is only incremented this month after `_apply_lifecycle_events` returns; subtracting
    # the lookback snapshot gives the count of qualifying months strictly inside the window.
    current_cum = current.property_owner_occupied_months[prop, :].astype(np.int64)
    lookback_snapshot_index = max(0, month - SECTION_121_LOOKBACK_MONTHS)
    snapshot_cum = buffers.state.property_owner_occupied_months_state[lookback_snapshot_index, prop, :].astype(np.int64)
    months_in_window = current_cum - snapshot_cum
    qualifies = months_in_window >= SECTION_121_MIN_QUALIFYING_MONTHS
    owner_profile = int(plan.property_owner_profile_index[prop])
    # `property_owner_profile_index` is filled at compile time; a property with no tax owner
    # (sentinel -1) means there's nobody to exclude for, so §121 collapses to 0.
    exclusion_cap = float(plan.tax.profile_section_121_exclusion[owner_profile]) if owner_profile >= 0 else 0.0
    section_121_exclusion = np.where(qualifies, np.minimum(post_recapture_gain, exclusion_cap), 0.0)
    ltcg = post_recapture_gain - section_121_exclusion

    owner_cash_slot = int(plan.properties.buyer_slot[prop])
    # Pay off any outstanding mortgage on this property; net cash to owner = gross - payoff.
    mortgage_payoff = np.zeros(rollout_count, dtype=np.float64)
    for lia in range(int(plan.liabilities.property_slot.shape[0])):
        if int(plan.liabilities.property_slot[lia]) == prop:
            mortgage_payoff += current.liability_principal[lia, :]
            current.liability_principal[lia, :] = 0.0
            current.liability_active[lia, :] = False

    net_cash = gross_proceeds - mortgage_payoff
    if owner_cash_slot >= 0:
        current.cash[owner_cash_slot, active_rollout] += net_cash[active_rollout]

    # Tax routing: recapture goes to its own YTD bucket (federal cap dispatch happens in
    # `_compute_tax_for_link`); the post-recapture, post-§121 remainder is LTCG.
    # `owner_profile` was already resolved above for the §121 cap lookup.
    if owner_profile >= 0:
        current.recapture_section_1250_ytd[owner_profile, active_rollout] += recapture[active_rollout]
        gain_profile = int(plan.tax_profile_capital_gain_index[owner_profile])
        if gain_profile >= 0:
            current.capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM, active_rollout] += ltcg[
                active_rollout
            ]
            current.capital_gain_active[gain_profile, CapitalGainClassification.LONG_TERM, active_rollout] = True

    # Freeze property state. cumulative_depreciation preserved as a historical record.
    current.property_active[prop, active_rollout] = False
    current.property_rented_fraction[prop, active_rollout] = 0.0
    current.property_building_basis[prop, active_rollout] = 0.0
    owner_agent_slot = int(plan.property_owner_agent_index[prop])
    if owner_agent_slot >= 0 and int(current.agent_primary_residence_property[owner_agent_slot]) == prop:
        current.agent_primary_residence_property[owner_agent_slot] = NO_CODE

    # Log per-rollout amounts (zero on failed rollouts).
    sale_gross_proceeds[active_rollout] = gross_proceeds[active_rollout]
    sale_mortgage_payoff[active_rollout] = mortgage_payoff[active_rollout]
    sale_net_cash[active_rollout] = net_cash[active_rollout]
    sale_realized_gain[active_rollout] = realized_gain[active_rollout]
    sale_recapture[active_rollout] = recapture[active_rollout]
    sale_section_121[active_rollout] = section_121_exclusion[active_rollout]
    sale_long_term_gain[active_rollout] = ltcg[active_rollout]
    buffers.lifecycle.sale_gross_proceeds[event_index] = sale_gross_proceeds
    buffers.lifecycle.sale_mortgage_payoff[event_index] = sale_mortgage_payoff
    buffers.lifecycle.sale_net_cash[event_index] = sale_net_cash
    buffers.lifecycle.sale_realized_gain[event_index] = sale_realized_gain
    buffers.lifecycle.sale_recapture[event_index] = sale_recapture
    buffers.lifecycle.sale_section_121_exclusion[event_index] = sale_section_121
    buffers.lifecycle.sale_long_term_gain[event_index] = sale_long_term_gain


def _apply_owner_occupied_month(plan: CompiledSimulation, current: CurrentStateBuffers) -> None:
    """Increment per-property owner-occupied-month counters for §121 tracking.

    A property is "owner-occupied this month" if it is active, assigned as the owning
    agent's primary residence, and not fully rented. The cumulative count is the §121 base; the
    snapshot history lets the sale handler compute the 24-of-last-60-months window.
    """

    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    property_count = current.property_rented_fraction.shape[0]
    for prop in range(property_count):
        owner_agent_slot = int(plan.property_owner_agent_index[prop])
        if owner_agent_slot < 0 or int(current.agent_primary_residence_property[owner_agent_slot]) != prop:
            continue
        owner_occupied = (
            active_rollout & current.property_active[prop, :] & (current.property_rented_fraction[prop, :] < 1.0)
        )
        current.property_owner_occupied_months[prop, owner_occupied] += 1


def _apply_depreciation_accrual(plan: CompiledSimulation, current: CurrentStateBuffers) -> None:
    """Accrue §168 straight-line depreciation for each rented property.

    Monthly depreciation = `building_basis × current.property_rented_fraction / (27.5 × 12)`.
    Reads the runtime `current.property_rented_fraction[p, r]` so mid-horizon lifecycle
    events (StartRenting/StopRenting/ChangeRentalPlan) take effect immediately. Updates both
    the cumulative buffer (used for §1250 recapture at sale) and the YTD buffer (read at
    year-end by `_apply_tax_accruals` to net Schedule E depreciation against ordinary income).
    """

    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    property_count = current.property_rented_fraction.shape[0]
    for prop in range(property_count):
        active_for_property = active_rollout & current.property_active[prop, :]
        if not active_for_property.any():
            continue
        # Both rented_fraction and building_basis are runtime per-rollout state — they may
        # have been mutated by PropertyLifecycleEvent rows this month.
        rented = current.property_rented_fraction[prop, :]
        basis = current.property_building_basis[prop, :]
        monthly_dep = basis * rented / (27.5 * 12.0)
        current.property_cumulative_depreciation[prop, active_for_property] += monthly_dep[active_for_property]
        current.property_depreciation_ytd[prop, active_for_property] += monthly_dep[active_for_property]


def _apply_capital_loss_netting(
    plan: CompiledSimulation, current: CurrentStateBuffers, active_rollout: np.ndarray
) -> None:
    """Year-end §1211/§1212 netting, run once per capital-gain agent before the per-link bracket
    walks. Replaces this year's raw ST/LT YTD gains with the post-netting figures the walks then
    tax, reduces ordinary_ytd by the (≤$3k) capital-loss offset, and persists the residual loss in
    `capital_loss_carryforward` for future years. Only active rollouts are mutated."""
    processed: set[int] = set()
    for profile in range(current.ordinary_ytd.shape[0]):
        gain_profile = int(plan.tax_profile_capital_gain_index[profile])
        if gain_profile < 0 or gain_profile in processed:
            continue
        processed.add(gain_profile)
        net_short_term, net_long_term, ordinary_offset, carryforward_out = net_capital_gains_with_carryforward(
            current.capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM, :],
            current.capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM, :],
            current.capital_loss_carryforward[gain_profile, :],
        )
        current.capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM, active_rollout] = net_short_term[
            active_rollout
        ]
        current.capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM, active_rollout] = net_long_term[
            active_rollout
        ]
        current.ordinary_ytd[profile, active_rollout] -= ordinary_offset[active_rollout]
        current.capital_loss_carryforward[gain_profile, active_rollout] = carryforward_out[active_rollout]


def _apply_tax_accruals(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    active_rollout = ~current.failed
    if month % 12 != 11 or not active_rollout.any():
        return

    # Schedule E rental interest deduction: for each liability, the YTD rented-share interest
    # (accumulated per-month against the runtime `current.property_rented_fraction`) deducts
    # from the owner's ordinary_ytd. The owner share is fed into MID below. This must run
    # before the bracket walk reads ordinary_ytd.
    liability_count = current.liability_rental_interest_ytd.shape[0]
    for lia in range(liability_count):
        profile = int(plan.liability_owner_profile_index[lia])
        if profile < 0:
            continue
        schedule_e_interest = current.liability_rental_interest_ytd[lia, :]
        if not bool((schedule_e_interest != 0.0).any()):
            continue
        current.ordinary_ytd[profile, active_rollout] -= schedule_e_interest[active_rollout]

    # Schedule E §168 depreciation deduction: the YTD depreciation accrued this calendar year
    # for each rented property deducts from the owner's ordinary_ytd. Then reset YTD.
    property_count = plan.property_owner_profile_index.shape[0]
    for prop in range(property_count):
        profile = int(plan.property_owner_profile_index[prop])
        if profile < 0:
            continue
        ytd = current.property_depreciation_ytd[prop, :]
        if not bool((ytd != 0.0).any()):
            continue
        current.ordinary_ytd[profile, active_rollout] -= ytd[active_rollout]
    current.property_depreciation_ytd[:, active_rollout] = 0.0

    # Capital-loss netting + carryforward (§1211/§1212). Must run before the bracket walks so the
    # netted ST/LT gains and the $3k ordinary-income offset are reflected in every jurisdiction
    # link's computation (each link reads capital_gain_ytd / ordinary_ytd directly).
    _apply_capital_loss_netting(plan, current, active_rollout)

    link_count = plan.tax.link_profile.shape[0]
    # First pass: every link that isn't a SALT-active federal link. Stash its annual tax so
    # the SALT pass can sum state-link contributions per federal link.
    annual_tax_by_link = np.zeros((plan.rollout_count, max(1, link_count)), dtype=np.float64)
    zero_salt = np.zeros(plan.rollout_count, dtype=np.float64)
    for link in range(link_count):
        if bool(plan.salt.link_active[link]):
            continue
        standard_deduction = float(plan.tax.link_standard_deduction[link])
        (
            mortgage_interest_deduction,
            itemized_deduction,
            ordinary_taxable,
            capital_taxable,
            ordinary_tax,
            capital_tax,
        ) = _compute_tax_for_link(plan, current, link=link, salt_deduction=zero_salt)
        tax = _write_tax_link_buffers(
            plan,
            buffers,
            current,
            link=link,
            month=month,
            active_rollout=active_rollout,
            standard_deduction=standard_deduction,
            mortgage_interest_deduction=mortgage_interest_deduction,
            salt_deduction=zero_salt,
            itemized_deduction=itemized_deduction,
            ordinary_taxable=ordinary_taxable,
            capital_taxable=capital_taxable,
            ordinary_tax=ordinary_tax,
            capital_tax=capital_tax,
        )
        annual_tax_by_link[:, link] = tax

    # Second pass: SALT-active federal links. SALT = property tax YTD for this profile + sum of
    # contributing-state-link annual tax, all capped per the year's schedule entry.
    year_index = month // 12
    cap_year_index = min(year_index, plan.salt.cap_by_year.shape[1] - 1)
    for link in range(link_count):
        if not bool(plan.salt.link_active[link]):
            continue
        profile = int(plan.tax.link_profile[link])
        state_tax_total = annual_tax_by_link @ plan.salt.contributing_mask[link].astype(np.float64)
        salt_total = current.property_tax_ytd[profile, :] + state_tax_total
        cap = float(plan.salt.cap_by_year[link, cap_year_index])
        salt_deduction = np.minimum(salt_total, cap)
        standard_deduction = float(plan.tax.link_standard_deduction[link])
        (
            mortgage_interest_deduction,
            itemized_deduction,
            ordinary_taxable,
            capital_taxable,
            ordinary_tax,
            capital_tax,
        ) = _compute_tax_for_link(plan, current, link=link, salt_deduction=salt_deduction)
        tax = _write_tax_link_buffers(
            plan,
            buffers,
            current,
            link=link,
            month=month,
            active_rollout=active_rollout,
            standard_deduction=standard_deduction,
            mortgage_interest_deduction=mortgage_interest_deduction,
            salt_deduction=salt_deduction,
            itemized_deduction=itemized_deduction,
            ordinary_taxable=ordinary_taxable,
            capital_taxable=capital_taxable,
            ordinary_tax=ordinary_tax,
            capital_tax=capital_tax,
        )
        annual_tax_by_link[:, link] = tax

    for profile in range(current.ordinary_ytd.shape[0]):
        current.ordinary_ytd[profile, active_rollout] = 0.0
        gain_profile = int(plan.tax_profile_capital_gain_index[profile])
        ltcg_active = active_rollout & current.capital_gain_active[gain_profile, CapitalGainClassification.LONG_TERM, :]
        stcg_active = (
            active_rollout & current.capital_gain_active[gain_profile, CapitalGainClassification.SHORT_TERM, :]
        )
        current.capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM, ltcg_active] = 0.0
        current.capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM, stcg_active] = 0.0
    # Zero YTD interest at year-end so next year's MID accumulation starts fresh. Mirrors the
    # ordinary/capital-gain YTD resets above.
    current.liability_interest_ytd[:, active_rollout] = 0.0
    current.liability_rental_interest_ytd[:, active_rollout] = 0.0
    # Same treatment for property-tax YTD; the federal SALT pass above has consumed it.
    current.property_tax_ytd[:, active_rollout] = 0.0
    # §1250 recapture YTD: consumed by both federal (flat 25%) and state (ordinary brackets)
    # links above. Reset so next year's recapture from a separate sale starts fresh.
    current.recapture_section_1250_ytd[:, active_rollout] = 0.0

    # Record this year's accrued liabilities as a balance-change event (sparse replacement for
    # the old per-month dense tax-liability state history). snapshot_month = month + 1 to match
    # the snapshot timeline the codec reports as month_index.
    created_slots = np.flatnonzero(plan.tax_liabilities.year_end_month == month)
    buffers.tax_liability_changes.record(
        snapshot_month=month + 1,
        slots=created_slots,
        amount=current.tax_liability_amount,
        active=current.tax_liability_active,
    )


def _apply_brackets(
    amount: npt.NDArray[np.float64], *, upper: np.ndarray, rate: np.ndarray, count: int
) -> npt.NDArray[np.float64]:
    if count <= 0:
        return np.zeros(amount.shape, dtype=np.float64)
    upper = upper[:count]
    rate = rate[:count]
    previous_upper = np.concatenate((np.array([0.0], dtype=np.float64), upper[:-1]))
    slice_top = np.minimum(amount[:, None], upper[None, :])
    in_bracket = np.maximum(slice_top - previous_upper[None, :], 0.0)
    return np.asarray((in_bracket * rate[None, :]).sum(axis=1), dtype=np.float64)


def _apply_ltcg_brackets(
    ltcg_amount: npt.NDArray[np.float64],
    ordinary_taxable: npt.NDArray[np.float64],
    *,
    upper: np.ndarray,
    rate: np.ndarray,
    count: int,
) -> npt.NDArray[np.float64]:
    if count <= 0:
        return np.zeros(ltcg_amount.shape, dtype=np.float64)
    upper = upper[:count]
    rate = rate[:count]
    previous_upper = np.concatenate((np.array([0.0], dtype=np.float64), upper[:-1]))
    total_taxable = ordinary_taxable + ltcg_amount
    slice_top = np.minimum(total_taxable[:, None], upper[None, :])
    slice_bottom = np.maximum(ordinary_taxable[:, None], previous_upper[None, :])
    in_bracket = np.maximum(slice_top - slice_bottom, 0.0)
    return np.asarray((in_bracket * rate[None, :]).sum(axis=1), dtype=np.float64)


def _tax_liability_slot_for(
    plan: CompiledSimulation, *, profile_index: int, link_index: int, year_end_month: int
) -> int:
    slots = np.flatnonzero(
        (plan.tax_liabilities.profile_index == profile_index)
        & (plan.tax_liabilities.link_index == link_index)
        & (plan.tax_liabilities.year_end_month == year_end_month)
    )
    if slots.size == 0:
        return NO_CODE
    return int(slots[0])


def _apply_property_purchases(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    for prop in range(plan.properties.month.shape[0]):
        if plan.properties.month[prop] != month:
            continue
        buffers.properties.purchase_active[month, prop, active_rollout] = True
        current.property_active[prop, active_rollout] = True
        current.property_basis[prop, active_rollout] = plan.properties.adjusted_basis[prop]
        current.property_ownership[prop, active_rollout] = plan.properties.ownership[prop]
        current.property_contribution[prop, active_rollout] = plan.properties.stake_contribution[prop]
        current.property_equity[prop, active_rollout] = plan.properties.equity_ledger[prop]

        buyer_cash = float(plan.properties.stake_contribution[prop])
        if buyer_cash > 0.0:
            buffers.properties.transfer_active[month, prop, active_rollout] = True
            buyer_slot = int(plan.properties.buyer_slot[prop])
            if buyer_slot >= 0:
                current.cash[buyer_slot, active_rollout] -= buyer_cash
            seller_slot = int(plan.properties.seller_slot[prop])
            if seller_slot >= 0:
                current.cash[seller_slot, active_rollout] += buyer_cash

        liability_slot = int(plan.properties.mortgage_slot[prop])
        if liability_slot >= 0:
            buffers.properties.mortgage_origination_active[month, liability_slot, active_rollout] = True
            current.liability_active[liability_slot, active_rollout] = True
            current.liability_principal[liability_slot, active_rollout] = plan.liabilities.principal[liability_slot]
            current.liability_monthly_payment[liability_slot, active_rollout] = plan.liabilities.monthly_payment[
                liability_slot
            ]
            current.liability_interest_ytd[liability_slot, active_rollout] = 0.0
            current.liability_principal_ytd[liability_slot, active_rollout] = 0.0


def _apply_scheduled_asset_sales(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    for sale in range(plan.sales.month.shape[0]):
        if plan.sales.month[sale] != month:
            continue
        ordered_lots = lot_order_for_pool(
            lot_agent_codes=plan.lot_agent_codes,
            lot_account_codes=plan.lot_account_codes,
            lot_asset_codes=plan.lot_asset_codes,
            lot_purchase_month=plan.lot_purchase_month,
            lot_id_codes=plan.lot_id_codes,
            agent_code=int(plan.sales.agent[sale]),
            account_code=int(plan.sales.source_account[sale]),
            asset_code=int(plan.sales.asset[sale]),
        )
        target_units = np.where(active_rollout, float(plan.sales.quantity[sale]), 0.0)
        price = _sale_unit_price(plan, month=month, sale=sale)
        result = fifo_sell_units(
            lot_remaining=current.lot_remaining.T,
            ordered_lots=ordered_lots,
            target_units=target_units,
            unit_price=price,
            cost_basis_per_unit=plan.lot_cost_basis_per_unit,
        )
        if result.oversell.any():
            raise ValueError(
                f"scheduled asset sale exceeds available lots: {text(plan, plan.sales.cause[month, sale])}"
            )

        current.lot_remaining -= result.sold_units.T
        proceeds_slot = int(plan.sales.proceeds_slot[sale])
        if proceeds_slot >= 0:
            current.cash[proceeds_slot, :] += result.total_proceeds
        _record_capital_gains(
            plan,
            current,
            month=month,
            agent_code=int(plan.sales.agent[sale]),
            sold_units=result.sold_units,
            gains=result.proceeds - result.cost_basis_consumed,
        )
        # Horizon-collapsed disposition: each scheduled sale fires once, so write to its slot directly
        # (the firing month is static — `plan.sales.month` — and recovered at decode time).
        sale_active = result.sold_units > 0.0
        buffers.lot_dispositions.scheduled.active[sale] = sale_active.T
        buffers.lot_dispositions.scheduled.units[sale] += result.sold_units.T
        buffers.lot_dispositions.scheduled.basis[sale] += result.cost_basis_consumed.T
        buffers.lot_dispositions.scheduled.proceeds[sale] += result.proceeds.T


def _apply_liquidity_policy_sales(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    active_rollout = ~current.failed
    if not active_rollout.any():
        return

    obligation_active = buffers.obligations.active[month]
    obligation_due = buffers.obligations.due[month]
    for policy in range(plan.liquidity_policies.agent.shape[0]):
        policy_agent = int(plan.liquidity_policies.agent[policy])
        policy_cash_slot = int(plan.liquidity_policies.cash_slot[policy])

        matching_obligations = np.flatnonzero(
            (plan.obligations.agent[month] == policy_agent) & (plan.obligations.from_slot[month] == policy_cash_slot)
        )
        if matching_obligations.size:
            matching_active = obligation_active[matching_obligations]
            hard_demand = np.where(matching_active, obligation_due[matching_obligations], 0.0).sum(axis=0)
            for row, slot in enumerate(matching_obligations):
                buffers.obligations.attempt_policy[month, slot, matching_active[row]] = policy
        else:
            hard_demand = np.zeros(plan.rollout_count, dtype=np.float64)

        cash_balance = (
            current.cash[policy_cash_slot, :]
            if policy_cash_slot >= 0
            else np.zeros(plan.rollout_count, dtype=np.float64)
        )
        required_sale = np.maximum(hard_demand - cash_balance, 0.0)
        post_required_cash = cash_balance + required_sale - hard_demand
        # Indexed amounts: per-rollout this-month values from compile-time amount arrays. Lets
        # the buffer track CPI when the wire emits a SeriesIndexedAmount; a `FixedAmount` (or
        # raw float) gives a constant vector with no work.
        buffer_trigger_values = _amount_values(
            plan,
            kind=int(plan.liquidity_policies.trigger_kind[policy]),
            fixed=float(plan.liquidity_policies.trigger_fixed[policy]),
            base=float(plan.liquidity_policies.trigger_base[policy]),
            series_index=int(plan.liquidity_policies.trigger_series[policy]),
            base_month=int(plan.liquidity_policies.trigger_base_month[policy]),
            adjustment_period=int(plan.liquidity_policies.trigger_period[policy]),
            month=month,
        )
        buffer_sale_values = _amount_values(
            plan,
            kind=int(plan.liquidity_policies.sale_kind[policy]),
            fixed=float(plan.liquidity_policies.sale_fixed[policy]),
            base=float(plan.liquidity_policies.sale_base[policy]),
            series_index=int(plan.liquidity_policies.sale_series[policy]),
            base_month=int(plan.liquidity_policies.sale_base_month[policy]),
            adjustment_period=int(plan.liquidity_policies.sale_period[policy]),
            month=month,
        )
        buffer_sale = np.where(
            (buffer_sale_values > 0.0) & (post_required_cash < buffer_trigger_values), buffer_sale_values, 0.0
        )
        remaining_target = np.where(active_rollout, required_sale + buffer_sale, 0.0)
        if not np.any((hard_demand > 0.0) | (remaining_target > 0.0)):
            continue

        for asset_idx in range(plan.liquidity_policies.assets.shape[1]):
            asset_code = int(plan.liquidity_policies.assets[policy, asset_idx])
            if asset_code < 0 or not np.any(remaining_target > 0.0):
                continue
            series_index = int(plan.liquidity_policies.asset_series[policy, asset_idx])
            if series_index < 0:
                continue
            raw_price = plan.external_values[series_index, :, month]
            valid_price = np.isfinite(raw_price) & (raw_price > 0.0)
            unit_price = np.where(valid_price, raw_price, 0.0)

            for source_account in plan.liquidity_policies.source_accounts[policy]:
                source_account_code = int(source_account)
                if source_account_code < 0 or not np.any(remaining_target > 0.0):
                    continue
                ordered_lots = lot_order_for_pool(
                    lot_agent_codes=plan.lot_agent_codes,
                    lot_account_codes=plan.lot_account_codes,
                    lot_asset_codes=plan.lot_asset_codes,
                    lot_purchase_month=plan.lot_purchase_month,
                    lot_id_codes=plan.lot_id_codes,
                    agent_code=policy_agent,
                    account_code=source_account_code,
                    asset_code=asset_code,
                )
                if ordered_lots.size == 0:
                    continue

                available_value = current.lot_remaining[ordered_lots, :].sum(axis=0) * unit_price
                target_dollars = np.minimum(np.maximum(remaining_target, 0.0), available_value)
                target_dollars = np.where(valid_price & active_rollout, target_dollars, 0.0)
                if not np.any(target_dollars > 0.0):
                    continue

                result = fifo_sell_dollars(
                    lot_remaining=current.lot_remaining.T,
                    ordered_lots=ordered_lots,
                    target_dollars=target_dollars,
                    unit_price=unit_price,
                    cost_basis_per_unit=plan.lot_cost_basis_per_unit,
                )
                if result.oversell.any():
                    raise ValueError(
                        "liquidity policy attempted to sell more than available lots: "
                        f"{plan.liquidity_policies.cause_id_prefixes[policy]}"
                    )

                current.lot_remaining -= result.sold_units.T
                if policy_cash_slot >= 0:
                    current.cash[policy_cash_slot, :] += result.total_proceeds
                _record_capital_gains(
                    plan,
                    current,
                    month=month,
                    agent_code=policy_agent,
                    sold_units=result.sold_units,
                    gains=result.proceeds - result.cost_basis_consumed,
                )
                sale_active = result.sold_units > 0.0
                buffers.lot_dispositions.liquidity.active[month, policy, asset_idx] |= sale_active.T
                buffers.lot_dispositions.liquidity.units[month, policy, asset_idx] += result.sold_units.T
                buffers.lot_dispositions.liquidity.basis[month, policy, asset_idx] += result.cost_basis_consumed.T
                buffers.lot_dispositions.liquidity.proceeds[month, policy, asset_idx] += result.proceeds.T
                remaining_target = np.maximum(remaining_target - result.total_proceeds, 0.0)


def _apply_obligation_accruals(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    active_rollout = ~current.failed
    if not active_rollout.any():
        return
    for slot in range(plan.obligations.cause.shape[1]):
        if plan.obligations.cause[month, slot] < 0 or plan.obligations.source_kind[month, slot] < 0:
            continue
        source_kind = int(plan.obligations.source_kind[month, slot])
        source_index = int(plan.obligations.source_index[month, slot])
        amount = np.zeros(plan.rollout_count, dtype=np.float64)
        active = active_rollout.copy()

        if source_kind == ObligationSource.CONFIGURED_OBLIGATION:
            amount = _amount_values(
                plan,
                kind=int(plan.obligations.amount_kind[month, slot]),
                fixed=float(plan.obligations.amount_fixed[month, slot]),
                base=float(plan.obligations.amount_base[month, slot]),
                series_index=int(plan.obligations.amount_series[month, slot]),
                base_month=int(plan.obligations.amount_base_month[month, slot]),
                adjustment_period=int(plan.obligations.amount_period[month, slot]),
                month=month,
            )
        elif source_kind == ObligationSource.MORTGAGE_PAYMENT:
            liab = source_index
            prop = int(plan.liabilities.property_slot[liab])
            active &= (
                current.liability_active[liab, :]
                & (plan.properties.month[prop] < month)
                & (current.liability_principal[liab, :] > 0.0)
            )
            interest = current.liability_principal[liab, :] * float(plan.liabilities.annual_rate[liab]) / 12.0
            amount = np.minimum(
                current.liability_monthly_payment[liab, :], current.liability_principal[liab, :] + interest
            )
        elif source_kind == ObligationSource.PROPERTY_TAX:
            prop = source_index
            active &= current.property_active[prop, :] & (plan.properties.month[prop] < month)
            rate = float(plan.obligations.amount_fixed[month, slot])
            if np.isnan(rate):
                rate = float(plan.properties.location_tax_rate[prop])
            ad_valorem_monthly = plan.properties.initial_assessed_value[prop] * rate / 12.0
            non_ad_valorem_monthly = plan.properties.special_assessment_annual_usd[prop] / 12.0
            amount = np.full(plan.rollout_count, ad_valorem_monthly + non_ad_valorem_monthly)
        elif source_kind == ObligationSource.ESTIMATED_TAX:
            amount = np.full(plan.rollout_count, float(plan.tax.profile_prior_year_tax[source_index]) / 4.0)
        elif source_kind in (ObligationSource.ESTIMATED_TAX_Q4, ObligationSource.TAX_TRUE_UP):
            profile = source_index
            tax_year_end = (month // 12 - 1) * 12 + 11
            actual = _actual_tax_for_profile_year(plan, current, profile_index=profile, year_end_month=tax_year_end)
            safe_harbor = np.minimum(float(plan.tax.profile_prior_year_tax[profile]), actual)
            paid_before_q4 = float(plan.tax.profile_prior_year_tax[profile]) * 0.75
            if source_kind == ObligationSource.ESTIMATED_TAX_Q4:
                amount = np.maximum(safe_harbor - paid_before_q4, 0.0)
            else:
                amount = np.maximum(actual - safe_harbor, 0.0)
        else:
            continue

        active &= amount > 0.0
        if not active.any():
            continue
        buffers.obligations.active[month, slot, active] = True
        buffers.obligations.due[month, slot, active] = amount[active]


def _apply_obligation_settlement(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    active = buffers.obligations.active[month]
    if not active.any():
        return

    due = buffers.obligations.due[month]
    funded = _obligation_group_funded(plan, current, month=month, active=active, due=due)
    tax_profile_count = plan.tax.profile_agent.shape[0]
    tax_payment_failed = np.zeros((tax_profile_count, plan.rollout_count), dtype=np.bool_)
    tax_settlement_candidate = np.zeros((tax_profile_count, plan.rollout_count), dtype=np.float64)
    tax_settlement_candidate_year_end = np.full((tax_profile_count, plan.rollout_count), NO_CODE, dtype=np.int64)

    for slot in range(active.shape[0]):
        active_slot = active[slot]
        if not active_slot.any():
            continue
        source_kind = int(plan.obligations.source_kind[month, slot])
        source_index = int(plan.obligations.source_index[month, slot])

        if source_kind == ObligationSource.TAX_TRUE_UP:
            profile = source_index
            tax_year_end = (month // 12 - 1) * 12 + 11
            actual = _actual_tax_for_profile_year(plan, current, profile_index=profile, year_end_month=tax_year_end)
            tax_settlement_candidate[profile, active_slot] = actual[active_slot]
            tax_settlement_candidate_year_end[profile, active_slot] = tax_year_end

        amount = due[slot]
        paid = active_slot & funded[slot]
        if paid.any():
            buffers.obligations.paid[month, slot, paid] = amount[paid]
            from_slot = int(plan.obligations.from_slot[month, slot])
            if from_slot >= 0:
                current.cash[from_slot, paid] -= amount[paid]
            to_slot = int(plan.obligations.to_slot[month, slot])
            if to_slot >= 0:
                current.cash[to_slot, paid] += amount[paid]
            if source_kind == ObligationSource.MORTGAGE_PAYMENT:
                _apply_mortgage_payment(
                    plan, buffers, current, month=month, liability_slot=source_index, paid=paid, amount=amount
                )
            # Accumulate property-tax payments into the owner's per-profile YTD bucket so the
            # year-end federal SALT pass can read them. Only the owner-use share contributes
            # to SALT; the rented share routes to Schedule E via deduction_profile. The
            # compiler ties every property-tax obligation to a property_slot (kind==2 branch),
            # so the engine always reads runtime `current.property_rented_fraction` —
            # mid-horizon lifecycle events take effect without any compile-time fallback.
            property_tax_profile = int(plan.obligations.property_tax_profile[month, slot])
            property_slot = int(plan.obligations.property_slot[month, slot])
            if property_tax_profile >= 0:
                assert property_slot >= 0, "property-tax obligation must be tied to a property slot"
                rented_per_rollout = current.property_rented_fraction[property_slot, :]
                owner_per_rollout = 1.0 - rented_per_rollout
                current.property_tax_ytd[property_tax_profile, paid] += amount[paid] * owner_per_rollout[paid]
            # Schedule E deduction: decrement payer's ordinary_ytd. For property-tax
            # obligations the deductible_fraction comes from runtime state; for other
            # deductible obligations it comes from the compile-time value.
            deduction_profile = int(plan.obligations.deduction_profile[month, slot])
            if deduction_profile >= 0:
                if property_slot >= 0:
                    rented_per_rollout = current.property_rented_fraction[property_slot, :]
                    current.ordinary_ytd[deduction_profile, paid] -= amount[paid] * rented_per_rollout[paid]
                else:
                    deductible_fraction = float(plan.obligations.deductible_fraction[month, slot])
                    current.ordinary_ytd[deduction_profile, paid] -= amount[paid] * deductible_fraction

        failed = active_slot & ~funded[slot]
        if failed.any():
            buffers.obligations.shortfall[month, slot, failed] = amount[failed]
            buffers.obligations.failure_active[month, slot, failed] = True
            first_failure = failed & (current.failed_month < 0)
            current.failed[failed] = True
            current.failed_month[first_failure] = month
            if source_kind in (
                ObligationSource.ESTIMATED_TAX,
                ObligationSource.ESTIMATED_TAX_Q4,
                ObligationSource.TAX_TRUE_UP,
            ):
                tax_payment_failed[source_index, failed] = True

    _apply_tax_settlements(
        plan,
        buffers,
        current,
        month=month,
        tax_settlement_candidate=tax_settlement_candidate,
        tax_settlement_candidate_year_end=tax_settlement_candidate_year_end,
        tax_payment_failed=tax_payment_failed,
    )


def _obligation_group_funded(
    plan: CompiledSimulation, current: CurrentStateBuffers, *, month: int, active: np.ndarray, due: np.ndarray
) -> np.ndarray:
    funded = np.zeros(active.shape, dtype=np.bool_)
    for slot in range(active.shape[0]):
        active_slot = active[slot]
        if not active_slot.any():
            continue
        agent = int(plan.obligations.agent[month, slot])
        from_slot = int(plan.obligations.from_slot[month, slot])
        group = (plan.obligations.agent[month] == agent) & (plan.obligations.from_slot[month] == from_slot)
        group_due = np.where(active[group], due[group], 0.0).sum(axis=0)
        available = current.cash[from_slot, :] if from_slot >= 0 else np.zeros(plan.rollout_count, dtype=np.float64)
        funded[slot] = active_slot & (available >= group_due - 1e-9)
    return funded


def _apply_mortgage_payment(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    current: CurrentStateBuffers,
    *,
    month: int,
    liability_slot: int,
    paid: np.ndarray,
    amount: np.ndarray,
) -> None:
    principal_before = current.liability_principal[liability_slot, :]
    interest = np.minimum(principal_before * float(plan.liabilities.annual_rate[liability_slot]) / 12.0, amount)
    principal = np.minimum(np.maximum(amount - interest, 0.0), principal_before)

    buffers.properties.mortgage_payment_active[month, liability_slot, paid] = True
    buffers.properties.mortgage_payment_interest[month, liability_slot, paid] = interest[paid]
    buffers.properties.mortgage_payment_principal[month, liability_slot, paid] = principal[paid]
    buffers.properties.mortgage_payment_total[month, liability_slot, paid] = amount[paid]
    current.liability_principal[liability_slot, paid] = np.maximum(0.0, principal_before[paid] - principal[paid])
    current.liability_interest_ytd[liability_slot, paid] += interest[paid]
    current.liability_principal_ytd[liability_slot, paid] += principal[paid]
    # Per-month rented share of interest, indexed by runtime property_rented_fraction so that
    # mid-horizon lifecycle transitions take effect immediately for MID + Schedule E.
    prop_slot = int(plan.liabilities.property_slot[liability_slot])
    if prop_slot >= 0:
        rented = current.property_rented_fraction[prop_slot, :]
        current.liability_rental_interest_ytd[liability_slot, paid] += interest[paid] * rented[paid]


def _apply_tax_settlements(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    current: CurrentStateBuffers,
    *,
    month: int,
    tax_settlement_candidate: np.ndarray,
    tax_settlement_candidate_year_end: np.ndarray,
    tax_payment_failed: np.ndarray,
) -> None:
    for profile in range(tax_settlement_candidate.shape[0]):
        active = (tax_settlement_candidate[profile] > 0.0) & ~tax_payment_failed[profile]
        if not active.any():
            continue
        buffers.taxes.settlement_active[month, profile, active] = True
        buffers.taxes.settlement_amount[month, profile, active] = tax_settlement_candidate[profile, active]
        buffers.taxes.settlement_year_end_month[month, profile, active] = tax_settlement_candidate_year_end[
            profile, active
        ]
        for year_end_month in np.unique(tax_settlement_candidate_year_end[profile, active]):
            if year_end_month < 0:
                continue
            year_active = active & (tax_settlement_candidate_year_end[profile] == year_end_month)
            _settle_tax_liabilities_for_profile_year(
                plan,
                current,
                profile_index=profile,
                year_end_month=int(year_end_month),
                settlement_amount=tax_settlement_candidate[profile],
                active=year_active,
            )
            # Record the post-settlement balance of this (profile, year) as a change event.
            settled_slots = np.flatnonzero(
                (plan.tax_liabilities.profile_index == profile)
                & (plan.tax_liabilities.year_end_month == int(year_end_month))
            )
            buffers.tax_liability_changes.record(
                snapshot_month=month + 1,
                slots=settled_slots,
                amount=current.tax_liability_amount,
                active=current.tax_liability_active,
            )


def _settle_tax_liabilities_for_profile_year(
    plan: CompiledSimulation,
    current: CurrentStateBuffers,
    *,
    profile_index: int,
    year_end_month: int,
    settlement_amount: np.ndarray,
    active: np.ndarray,
) -> None:
    if not active.any():
        return
    slots = np.flatnonzero(
        (plan.tax_liabilities.profile_index == profile_index) & (plan.tax_liabilities.year_end_month == year_end_month)
    )
    if slots.size == 0:
        return
    slot_amounts = current.tax_liability_amount[slots, :]
    eligible_amounts = np.where(current.tax_liability_active[slots, :], slot_amounts, 0.0)
    outstanding = eligible_amounts.sum(axis=0)
    settlement = np.where(active, settlement_amount, 0.0)
    weights = np.divide(
        eligible_amounts, outstanding[None, :], out=np.zeros_like(eligible_amounts), where=outstanding[None, :] > 0.0
    )
    settled = np.minimum(eligible_amounts, weights * settlement[None, :])
    current.tax_liability_amount[slots, :] = np.maximum(0.0, slot_amounts - settled)


def _actual_tax_for_profile_year(
    plan: CompiledSimulation, current: CurrentStateBuffers, *, profile_index: int, year_end_month: int
) -> npt.NDArray[np.float64]:
    slots = np.flatnonzero(
        (plan.tax_liabilities.profile_index == profile_index) & (plan.tax_liabilities.year_end_month == year_end_month)
    )
    if slots.size == 0:
        return np.zeros(plan.rollout_count, dtype=np.float64)
    return np.asarray(
        np.where(current.tax_liability_active[slots, :], current.tax_liability_amount[slots, :], 0.0).sum(axis=0),
        dtype=np.float64,
    )


def _sale_unit_price(plan: CompiledSimulation, *, month: int, sale: int) -> np.ndarray:
    fixed_price = float(plan.sales.price_fixed[sale])
    if not np.isnan(fixed_price):
        return np.full(plan.rollout_count, fixed_price, dtype=np.float64)
    series_index = int(plan.sales.price_series[sale])
    return plan.external_values[series_index, :, month]


def _record_capital_gains(
    plan: CompiledSimulation,
    current: CurrentStateBuffers,
    *,
    month: int,
    agent_code: int,
    sold_units: np.ndarray,
    gains: np.ndarray,
) -> None:
    if sold_units.size == 0:
        return
    # TLH basis give-back (Piece 2b): before recording gains, fold the deferred harvested loss back
    # into the realized gain of any sold harvest-policy lots, so the deferral is honestly repaid.
    # Mutates a per-lot copy of `gains` (caller's array is untouched) and drains the cumulative
    # harvest scalar. Applies across EVERY sale path because they all route through here.
    gains = _apply_tlh_give_back(plan, current, sold_units=sold_units, gains=gains)
    for profile in range(plan.capital_gain_agent_codes.shape[0]):
        if int(plan.capital_gain_agent_codes[profile]) != agent_code:
            continue
        for lot in range(plan.lot_id_codes.shape[0]):
            cls = (
                CapitalGainClassification.LONG_TERM
                if month - int(plan.lot_purchase_month[lot]) >= 12
                else CapitalGainClassification.SHORT_TERM
            )
            active = sold_units[:, lot] > 0.0
            current.capital_gain_active[profile, cls, active] = True
            current.capital_gain_ytd[profile, cls, :] += gains[:, lot]


def _apply_tlh_give_back(
    plan: CompiledSimulation, current: CurrentStateBuffers, *, sold_units: np.ndarray, gains: np.ndarray
) -> np.ndarray:
    """Repay deferred harvested loss as extra realized gain on sold harvest-policy lots.

    CORRECTNESS-CRITICAL (the part the design says to get right and test hardest). `sold_units`
    and `gains` are `(R, L)`. For each `HarvestPolicy`, the fraction of the policy's pre-sale units
    being sold in THIS sale determines the share of `tlh_cumulative_harvest` that is realized now:
    that share is added to the gain of the sold policy-lots (split across them by sold units, so
    each lot keeps its own ST/LT character) and subtracted from the cumulative scalar. A full
    liquidation (across one or many sales) therefore gives back exactly the whole accumulated
    harvest — never more (the scalar is drained, so it cannot double-pay) and never less. Lots left
    unsold at the terminal carry their unrealized deferred gain forward, which is correct: nothing
    is realized, so nothing is given back. Returns the give-back-adjusted `(R, L)` gains; the
    caller's array is not mutated. The harvest phase guarantees `cumulative <= original_basis`, so
    the give-back can never exceed the gain that the reduced basis implies.
    """

    harvest = plan.harvest_policies
    policy_count = harvest.gain_profile_index.shape[0]
    adjusted_gains = gains
    copied = False
    for policy_idx in range(policy_count):
        if int(harvest.gain_profile_index[policy_idx]) < 0:
            continue
        lot_indices = np.flatnonzero(harvest.lot_mask[policy_idx])
        if lot_indices.size == 0:
            continue
        sold_policy = sold_units[:, lot_indices]  # (R, policy_lots)
        units_sold = sold_policy.sum(axis=1)  # (R,)
        if not (units_sold > 0.0).any():
            continue
        # Pre-sale held units of the policy = units still remaining + units just sold. `sold_units`
        # was already subtracted from `current.lot_remaining` by the caller before this runs.
        remaining_policy = current.lot_remaining[lot_indices, :].T  # (R, policy_lots)
        pre_sale_units = remaining_policy.sum(axis=1) + units_sold  # (R,)
        cumulative = current.tlh_cumulative_harvest[policy_idx, :]  # (R,)
        fraction_sold = np.divide(units_sold, pre_sale_units, out=np.zeros_like(units_sold), where=pre_sale_units > 0.0)
        give_back = fraction_sold * cumulative  # (R,)
        if not (give_back > 0.0).any():
            continue
        if not copied:
            adjusted_gains = gains.copy()
            copied = True
        # Distribute the give-back across the sold policy-lots in proportion to each lot's sold
        # units, so the realized extra gain follows the lots actually disposed (preserving ST/LT).
        per_lot_weight = np.divide(
            sold_policy, units_sold[:, None], out=np.zeros_like(sold_policy), where=units_sold[:, None] > 0.0
        )  # (R, policy_lots), rows sum to 1 where any policy lot sold
        adjusted_gains[:, lot_indices] += per_lot_weight * give_back[:, None]
        current.tlh_cumulative_harvest[policy_idx, :] = cumulative - give_back
    return adjusted_gains
