//! Reduced-form tax-loss harvesting: the calibrated yield curve, the cumulative deferral
//! ledger, and the proportional basis give-back at sale time.

use super::*;

#[derive(Clone, Debug)]
pub(super) struct ScheduledTlhGiveBack {
    cumulative_start: Vec<Money>,
    pre_sale_units: Vec<i64>,
    allocated: Vec<Money>,
}

pub(super) fn execute_tlh_harvest(
    fixture: &Fixture,
    rollout_id: u32,
    lots: &[LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    cumulative_harvest: &mut [Money],
    month: u32,
) -> Result<(), SimulationError> {
    for (policy_index, policy) in fixture.scenario.harvest_policies.iter().enumerate() {
        if !tax_facts
            .keys()
            .any(|(agent_id, _)| agent_id == &policy.owner_agent_id)
        {
            continue;
        }
        let policy_lots: Vec<&LotState> = lots
            .iter()
            .filter(|lot| {
                lot.spec.agent_id == policy.owner_agent_id
                    && lot.spec.account_id == policy.account_id
                    && lot.spec.asset_id == policy.asset_id
                    && lot.units_remaining.0 > 0
            })
            .collect();
        if policy_lots.is_empty() {
            continue;
        }
        let series_id = format!("security:{}", policy.asset_id);
        let price = series_value(fixture, &series_id, rollout_id, month)?;
        let prior_price = series_value(fixture, &series_id, rollout_id, month.saturating_sub(1))?;
        let market_value = policy_lots.iter().try_fold(Money(0), |total, lot| {
            total.checked_add(Money(mul_div_round_half_up(
                lot.units_remaining.0,
                price,
                lot.spec.quantity_scale,
                "TLH market value",
            )?))
        })?;
        let original_basis = policy_lots.iter().try_fold(Money(0), |total, lot| {
            total.checked_add(lot.basis_remaining)
        })?;
        let adjusted_basis = Money(
            original_basis
                .0
                .saturating_sub(cumulative_harvest[policy_index].0)
                .max(0),
        );
        let embedded_gain = if market_value.0 > 0 {
            ((market_value.0 - adjusted_basis.0) as f64 / market_value.0 as f64).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let period_return = if prior_price > 0 {
            (price - prior_price) as f64 / prior_price as f64
        } else {
            0.0
        };
        let peak = policy.peak_annual_yield_ppb as f64 / RATE_SCALE_PPB as f64;
        let floor = policy.floor_annual_yield_ppb as f64 / RATE_SCALE_PPB as f64;
        let gamma = policy.maturity_decay_exponent_ppb as f64 / RATE_SCALE_PPB as f64;
        let sensitivity = policy.drawdown_sensitivity_ppb as f64 / RATE_SCALE_PPB as f64;
        let maturity = (1.0 - embedded_gain).powf(gamma);
        let base_monthly = (floor + (peak - floor) * maturity) / 12.0;
        let fraction = base_monthly * (1.0 + sensitivity * (-period_return).max(0.0));
        let fraction_ppb = f64_factor_to_ppb(fraction, "TLH harvest fraction")?;
        let ceiling = Money(
            original_basis
                .0
                .saturating_sub(cumulative_harvest[policy_index].0)
                .max(0),
        );
        let gross = Money(
            mul_div_round_half_up(
                market_value.0,
                fraction_ppb,
                RATE_SCALE_PPB,
                "TLH gross harvest",
            )?
            .min(ceiling.0),
        );
        let short_term = Money(mul_div_round_half_up(
            gross.0,
            policy.short_term_fraction_ppb,
            RATE_SCALE_PPB,
            "TLH short-term fraction",
        )?);
        let long_term = gross.checked_sub(short_term)?;
        if short_term != Money(0) {
            record_capital_gain(
                tax_facts,
                &policy.owner_agent_id,
                short_term.checked_neg()?,
                false,
            )?;
        }
        if long_term != Money(0) {
            record_capital_gain(
                tax_facts,
                &policy.owner_agent_id,
                long_term.checked_neg()?,
                true,
            )?;
        }
        cumulative_harvest[policy_index] = cumulative_harvest[policy_index].checked_add(gross)?;
    }
    Ok(())
}

fn f64_factor_to_ppb(value: f64, operation: &'static str) -> Result<i64, SimulationError> {
    if !value.is_finite() || value < 0.0 {
        return Err(SimulationError::Arithmetic(ArithmeticError::Overflow {
            operation,
        }));
    }
    let scaled = value * RATE_SCALE_PPB as f64;
    if scaled > i64::MAX as f64 {
        return Err(SimulationError::Arithmetic(ArithmeticError::Overflow {
            operation,
        }));
    }
    Ok((scaled + 0.5).floor() as i64)
}

pub(super) fn tlh_give_back_for_pool_sale(
    fixture: &Fixture,
    lots: &[LotState],
    planned: &[PlannedDisposition],
    cumulative_harvest: &mut [Money],
) -> Result<Vec<Money>, SimulationError> {
    let mut give_back = vec![Money(0); planned.len()];
    for (policy_index, policy) in fixture.scenario.harvest_policies.iter().enumerate() {
        let matching: Vec<(usize, &PlannedDisposition)> = planned
            .iter()
            .enumerate()
            .filter(|(_, item)| {
                let lot = &lots[item.lot_index];
                lot.spec.agent_id == policy.owner_agent_id
                    && lot.spec.account_id == policy.account_id
                    && lot.spec.asset_id == policy.asset_id
            })
            .collect();
        if matching.is_empty() || cumulative_harvest[policy_index] == Money(0) {
            continue;
        }
        let pre_sale_units = lots
            .iter()
            .filter(|lot| {
                lot.spec.agent_id == policy.owner_agent_id
                    && lot.spec.account_id == policy.account_id
                    && lot.spec.asset_id == policy.asset_id
            })
            .try_fold(0_i64, |total, lot| {
                total
                    .checked_add(lot.units_remaining.0)
                    .ok_or(ArithmeticError::Overflow {
                        operation: "TLH pre-sale units",
                    })
            })?;
        let sold_units = matching.iter().try_fold(0_i64, |total, (_, item)| {
            total
                .checked_add(item.units.0)
                .ok_or(ArithmeticError::Overflow {
                    operation: "TLH sold units",
                })
        })?;
        if pre_sale_units <= 0 || sold_units <= 0 {
            continue;
        }
        let total_give_back = Money(mul_div_round_half_up(
            cumulative_harvest[policy_index].0,
            sold_units,
            pre_sale_units,
            "TLH sale give-back",
        )?);
        let mut allocated = Money(0);
        for (planned_index, item) in matching {
            let amount = Money(mul_div_round_half_up(
                total_give_back.0,
                item.units.0,
                sold_units,
                "TLH per-lot give-back",
            )?);
            give_back[planned_index] = give_back[planned_index].checked_add(amount)?;
            allocated = allocated.checked_add(amount)?;
        }
        cumulative_harvest[policy_index] =
            cumulative_harvest[policy_index].checked_sub(allocated)?;
    }
    Ok(give_back)
}

pub(super) fn scheduled_tlh_give_back_state(
    fixture: &Fixture,
    lots: &[LotState],
    cumulative_harvest: &[Money],
) -> Result<ScheduledTlhGiveBack, SimulationError> {
    let pre_sale_units = fixture
        .scenario
        .harvest_policies
        .iter()
        .map(|policy| {
            lots.iter()
                .filter(|lot| {
                    lot.spec.agent_id == policy.owner_agent_id
                        && lot.spec.account_id == policy.account_id
                        && lot.spec.asset_id == policy.asset_id
                })
                .try_fold(0_i64, |total, lot| {
                    total
                        .checked_add(lot.units_remaining.0)
                        .ok_or(ArithmeticError::Overflow {
                            operation: "TLH scheduled-sale units",
                        })
                })
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ScheduledTlhGiveBack {
        cumulative_start: cumulative_harvest.to_vec(),
        pre_sale_units,
        allocated: vec![Money(0); cumulative_harvest.len()],
    })
}

pub(super) fn tlh_give_back_for_scheduled_sale(
    fixture: &Fixture,
    lots: &[LotState],
    planned: &[PlannedDisposition],
    state: &mut ScheduledTlhGiveBack,
) -> Result<Vec<Money>, SimulationError> {
    let mut give_back = vec![Money(0); planned.len()];
    for (policy_index, policy) in fixture.scenario.harvest_policies.iter().enumerate() {
        if state.cumulative_start[policy_index] == Money(0)
            || state.pre_sale_units[policy_index] <= 0
        {
            continue;
        }
        for (planned_index, item) in planned.iter().enumerate() {
            let lot = &lots[item.lot_index];
            if lot.spec.agent_id != policy.owner_agent_id
                || lot.spec.account_id != policy.account_id
                || lot.spec.asset_id != policy.asset_id
            {
                continue;
            }
            let amount = Money(mul_div_round_half_up(
                state.cumulative_start[policy_index].0,
                item.units.0,
                state.pre_sale_units[policy_index],
                "TLH scheduled-sale give-back",
            )?);
            give_back[planned_index] = give_back[planned_index].checked_add(amount)?;
            state.allocated[policy_index] = state.allocated[policy_index].checked_add(amount)?;
        }
    }
    Ok(give_back)
}

pub(super) fn apply_scheduled_tlh_give_back(
    state: &ScheduledTlhGiveBack,
    cumulative_harvest: &mut [Money],
) -> Result<(), SimulationError> {
    for (cumulative, allocated) in cumulative_harvest.iter_mut().zip(&state.allocated) {
        *cumulative = cumulative.checked_sub(*allocated)?;
    }
    Ok(())
}
