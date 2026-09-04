//! The typed private-equity tender protocol: issuer marks and regimes, tender capacity
//! and eligibility, liquidity blocks, and the liquid-net-worth floor that governs sales.

use super::*;

pub(super) fn private_equity_issuer(asset_id: &str) -> Option<&str> {
    asset_id
        .strip_prefix(PE_ASSET_PREFIX)
        .filter(|issuer_id| !issuer_id.is_empty())
}

pub(super) fn private_equity_series_id(channel: &str, issuer_id: &str) -> String {
    format!("private_equity_{channel}:{issuer_id}")
}

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_private_equity(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    tlh_cumulative_harvest: &mut [Money],
    month: u32,
) -> Result<(), SimulationError> {
    let issuers: BTreeSet<String> = lots
        .iter()
        .filter_map(|lot| private_equity_issuer(&lot.spec.asset_id).map(str::to_owned))
        .collect();
    for issuer_id in issuers {
        let asset_id = format!("{PE_ASSET_PREFIX}{issuer_id}");
        let mut candidates: Vec<usize> = lots
            .iter()
            .enumerate()
            .filter(|(_, lot)| lot.spec.asset_id == asset_id)
            .map(|(index, _)| index)
            .collect();
        candidates.sort_by_key(|index| (lots[*index].fifo_rank, lots[*index].spec.lot_id.clone()));
        let first = candidates[0];
        let owner_agent_id = lots[first].spec.agent_id.clone();
        let quantity_scale = lots[first].spec.quantity_scale;
        let mark = pe_channel_value(fixture, &issuer_id, "mark", rollout_id, month)?;
        let regime_code = pe_channel_value(fixture, &issuer_id, "regime", rollout_id, month)?;
        let event_kind_code =
            pe_channel_value(fixture, &issuer_id, "event_kind", rollout_id, month)?;
        let tender_active =
            pe_channel_value(fixture, &issuer_id, "sale_opportunity", rollout_id, month)? == 1;
        let capacity = pe_channel_value(fixture, &issuer_id, "sale_capacity", rollout_id, month)?;
        let eligible = pe_channel_value(fixture, &issuer_id, "eligible", rollout_id, month)?;
        let forced_sale = pe_channel_value(fixture, &issuer_id, "forced_sale", rollout_id, month)?;
        let liquidity_blocked =
            pe_channel_value(fixture, &issuer_id, "liquidity_blocked", rollout_id, month)? == 1;
        let forced_recovery =
            pe_channel_value(fixture, &issuer_id, "forced_recovery", rollout_id, month)?;
        let event_kind = private_equity_event_kind(event_kind_code);
        let regime = private_equity_regime(regime_code);
        if event_kind_code != 0 {
            recorder.record_private_equity_event(PrivateEquityProtocolOutcome {
                month,
                issuer_id: issuer_id.clone(),
                asset_id: asset_id.clone(),
                event_kind: event_kind.into(),
                regime: regime.into(),
                mark: Money(mark),
                sale_capacity_fraction_ppb: capacity,
                eligible_fraction_ppb: eligible,
                forced_sale_fraction_ppb: forced_sale,
                liquidity_blocked,
                forced_recovery_cashout: Money(forced_recovery),
            })?;
        }
        let units_held = pe_units_held(lots, &candidates)?;
        let Some(policy) = fixture
            .scenario
            .private_equity_tender_policies
            .iter()
            .find(|policy| policy.owner_agent_id == owner_agent_id)
        else {
            if tender_active {
                recorder.record_private_equity_opportunity(PrivateEquityOpportunityOutcome {
                    month,
                    cause_id: format!("pe_opportunity_m{month}_{issuer_id}"),
                    issuer_id: issuer_id.clone(),
                    asset_id,
                    event_kind: event_kind.into(),
                    regime: regime.into(),
                    outcome: "no_policy".into(),
                    mark: Money(mark),
                    sale_capacity_fraction_ppb: capacity,
                    eligible_fraction_ppb: eligible,
                    liquidity_blocked,
                    floor: Money(0),
                    liquid_net_worth: Money(0),
                    shortfall: Money(0),
                    quantity_scale,
                    units_held: Quantity(units_held),
                    sellable_units: Quantity(pe_sellable_units(units_held, capacity, eligible)?),
                    target_units: Quantity(0),
                    proceeds: Money(0),
                })?;
            }
            continue;
        };

        if forced_recovery > 0 && units_held > 0 {
            let recovery_price = mul_div_round_half_up(
                forced_recovery,
                quantity_scale,
                units_held,
                "private-equity recovery price",
            )?;
            execute_target_allocation_pool_sale(
                fixture,
                ledger,
                recorder,
                lots,
                tax_facts,
                tlh_cumulative_harvest,
                month,
                &format!("pe_forced_recovery_m{month}_{issuer_id}"),
                &owner_agent_id,
                &policy.proceeds_account_id,
                recovery_price,
                units_held,
                Some(&owner_agent_id),
                &candidates,
            )?;
        }
        let units_after_recovery = pe_units_held(lots, &candidates)?;
        if forced_sale > 0 && mark > 0 && units_after_recovery > 0 {
            let forced_target = mul_div_round_half_up(
                units_after_recovery,
                forced_sale,
                RATE_SCALE_PPB,
                "private-equity forced-sale quantity",
            )?
            .min(units_after_recovery);
            execute_target_allocation_pool_sale(
                fixture,
                ledger,
                recorder,
                lots,
                tax_facts,
                tlh_cumulative_harvest,
                month,
                &format!("pe_forced_sale_m{month}_{issuer_id}"),
                &owner_agent_id,
                &policy.proceeds_account_id,
                mark,
                forced_target,
                Some(&owner_agent_id),
                &candidates,
            )?;
        }

        let floor = amount_value(fixture, rollout_id, month, &policy.liquid_net_worth_floor)?;
        let liquid_net_worth = private_equity_liquid_net_worth(
            fixture,
            rollout_id,
            ledger,
            lots,
            &owner_agent_id,
            month,
        )?;
        let shortfall = Money((floor.0 - liquid_net_worth.0).max(0));
        let units_after_forced = pe_units_held(lots, &candidates)?;
        let sellable = pe_sellable_units(units_after_forced, capacity, eligible)?;
        let shortfall_units = if mark > 0 {
            ceil_quantity_for_money(shortfall.0, mark, quantity_scale)?
        } else {
            0
        };
        let public_active = regime_code == 2;
        let opportunity_active = (tender_active || public_active) && !liquidity_blocked && mark > 0;
        let target = if opportunity_active {
            shortfall_units.min(sellable)
        } else {
            0
        };
        let mut outcome = "sold";
        if shortfall.0 <= 0 {
            outcome = "floor_satisfied";
        }
        if capacity == 0 || eligible == 0 {
            outcome = "capacity_zero";
        }
        if mark <= 0 {
            outcome = "nonpositive_mark";
        }
        if liquidity_blocked {
            outcome = "liquidity_blocked";
        }
        if units_after_forced <= 0 {
            outcome = "no_units";
        }
        if tender_active {
            recorder.record_private_equity_opportunity(PrivateEquityOpportunityOutcome {
                month,
                cause_id: format!("pe_opportunity_m{month}_{issuer_id}"),
                issuer_id: issuer_id.clone(),
                asset_id: asset_id.clone(),
                event_kind: event_kind.into(),
                regime: regime.into(),
                outcome: outcome.into(),
                mark: Money(mark),
                sale_capacity_fraction_ppb: capacity,
                eligible_fraction_ppb: eligible,
                liquidity_blocked,
                floor,
                liquid_net_worth,
                shortfall,
                quantity_scale,
                units_held: Quantity(units_after_forced),
                sellable_units: Quantity(sellable),
                target_units: Quantity(target),
                proceeds: Money(mul_div_round_half_up(
                    target,
                    mark,
                    quantity_scale,
                    "private-equity opportunity proceeds",
                )?),
            })?;
        }
        if target > 0 {
            let cause_id = if public_active {
                format!("pe_public_market_m{month}_{issuer_id}")
            } else {
                format!("pe_tender_m{month}_{issuer_id}")
            };
            execute_target_allocation_pool_sale(
                fixture,
                ledger,
                recorder,
                lots,
                tax_facts,
                tlh_cumulative_harvest,
                month,
                &cause_id,
                &owner_agent_id,
                &policy.proceeds_account_id,
                mark,
                target,
                Some(&owner_agent_id),
                &candidates,
            )?;
        }
    }
    Ok(())
}

fn pe_channel_value(
    fixture: &Fixture,
    issuer_id: &str,
    channel: &str,
    rollout_id: u32,
    month: u32,
) -> Result<i64, SimulationError> {
    series_value(
        fixture,
        &private_equity_series_id(channel, issuer_id),
        rollout_id,
        month,
    )
}

fn pe_units_held(lots: &[LotState], candidates: &[usize]) -> Result<i64, SimulationError> {
    candidates.iter().try_fold(0_i64, |sum, index| {
        sum.checked_add(lots[*index].units_remaining.0)
            .ok_or(ArithmeticError::Overflow {
                operation: "private-equity units held",
            })
            .map_err(SimulationError::from)
    })
}

fn pe_sellable_units(units: i64, capacity: i64, eligible: i64) -> Result<i64, SimulationError> {
    let factor = i128::from(capacity)
        .checked_mul(i128::from(eligible))
        .ok_or(ArithmeticError::Overflow {
            operation: "private-equity sellable factor",
        })?;
    let denominator = i128::from(RATE_SCALE_PPB) * i128::from(RATE_SCALE_PPB);
    let result = mul_div_i128_round_half_up(
        i128::from(units),
        factor,
        denominator,
        "private-equity sellable quantity",
    )?;
    i64::try_from(result).map_err(|_| {
        SimulationError::Arithmetic(ArithmeticError::Overflow {
            operation: "private-equity sellable quantity",
        })
    })
}

fn ceil_quantity_for_money(value: i64, price: i64, scale: i64) -> Result<i64, SimulationError> {
    let numerator =
        i128::from(value)
            .checked_mul(i128::from(scale))
            .ok_or(ArithmeticError::Overflow {
                operation: "private-equity shortfall quantity",
            })?;
    let denominator = i128::from(price);
    let rounded = numerator
        .checked_add(denominator - 1)
        .ok_or(ArithmeticError::Overflow {
            operation: "private-equity shortfall quantity",
        })?
        / denominator;
    i64::try_from(rounded).map_err(|_| {
        SimulationError::Arithmetic(ArithmeticError::Overflow {
            operation: "private-equity shortfall quantity",
        })
    })
}

fn private_equity_liquid_net_worth(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &Ledger,
    lots: &[LotState],
    owner_agent_id: &str,
    month: u32,
) -> Result<Money, SimulationError> {
    let mut total = fixture
        .scenario
        .accounts
        .iter()
        .filter(|account| account.account.agent_id == owner_agent_id)
        .try_fold(Money(0), |sum, account| {
            sum.checked_add(ledger.balance(&account.account)?)
                .map_err(SimulationError::from)
        })?;
    for lot in lots.iter().filter(|lot| {
        lot.spec.agent_id == owner_agent_id
            && private_equity_issuer(&lot.spec.asset_id).is_none()
            && lot.units_remaining.0 > 0
    }) {
        let series_id = format!("security:{}", lot.spec.asset_id);
        let Some(price) = fixture
            .series
            .iter()
            .find(|series| series.series_id == series_id)
            .and_then(|series| series.value(rollout_id, month))
        else {
            continue;
        };
        total = total.checked_add(Money(mul_div_round_half_up(
            lot.units_remaining.0,
            price,
            lot.spec.quantity_scale,
            "private-equity liquid lot value",
        )?))?;
    }
    Ok(total)
}

fn private_equity_regime(code: i64) -> &'static str {
    match code {
        1 => "private_operating",
        2 => "public_market",
        3 => "acquired",
        4 => "collapsed",
        _ => unreachable!("validated private-equity regime code"),
    }
}

fn private_equity_event_kind(code: i64) -> &'static str {
    match code {
        0 => "none",
        1 => "tender",
        2 => "admin_mark_update",
        3 => "public_market_open",
        4 => "acquisition_cashout",
        5 => "legal_impairment",
        6 => "forced_recovery",
        7 => "collapse",
        _ => unreachable!("validated private-equity event-kind code"),
    }
}
