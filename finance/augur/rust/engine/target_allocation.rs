//! Target-allocation execution: the cash-band raise before obligations settle, the
//! purchases that run after they do, and quiet-band drift rebalancing.

use super::*;

#[derive(Clone, Debug)]
pub(super) struct PendingAllocationBuy {
    pub(super) policy_index: usize,
    pub(super) sleeve_index: usize,
    wanted_units: i64,
    unit_price: i64,
}

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_target_allocation_sales(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    tlh_cumulative_harvest: &mut [Money],
    month: u32,
    obligations: &[ActiveObligation],
) -> Result<Vec<PendingAllocationBuy>, SimulationError> {
    let mut pending_buys = Vec::new();
    for (policy_index, policy) in fixture
        .scenario
        .target_allocation_policies
        .iter()
        .enumerate()
    {
        let cash_account = AccountRef::new(&policy.agent_id, &policy.account_id);
        let hard_demand = obligations
            .iter()
            .filter(|obligation| obligation.from == cash_account)
            .try_fold(Money(0), |sum, obligation| {
                sum.checked_add(obligation.amount_due)
            })?;
        let current_cash = ledger.balance(&cash_account)?;
        let floor = amount_value(fixture, rollout_id, month, &policy.cash_floor)?;
        let ceiling = amount_value(fixture, rollout_id, month, &policy.cash_ceiling)?;
        let projected = current_cash.checked_sub(hard_demand)?;
        let raise = if projected.0 < floor.0 {
            ceiling.checked_sub(projected)?
        } else {
            Money(0)
        };
        let invest = if projected.0 > ceiling.0 {
            projected.checked_sub(floor)?
        } else {
            Money(0)
        };

        let source_accounts: Vec<&str> = if policy.source_account_ids.is_empty() {
            vec![policy.account_id.as_str()]
        } else {
            policy
                .source_account_ids
                .iter()
                .map(String::as_str)
                .collect()
        };
        let mut values = Vec::with_capacity(policy.sleeves.len());
        let mut prices = Vec::with_capacity(policy.sleeves.len());
        let mut scales = Vec::with_capacity(policy.sleeves.len());
        let mut available_units = Vec::with_capacity(policy.sleeves.len());
        for sleeve in &policy.sleeves {
            let series_id = format!("security:{}", sleeve.asset_id);
            let price = fixture
                .series
                .iter()
                .find(|series| series.series_id == series_id)
                .and_then(|series| series.value(rollout_id, month))
                .unwrap_or(0);
            let sleeve_lots: Vec<_> = lots
                .iter()
                .filter(|lot| {
                    lot.spec.agent_id == policy.agent_id
                        && lot.spec.asset_id == sleeve.asset_id
                        && source_accounts.contains(&lot.spec.account_id.as_str())
                })
                .collect();
            let scale = sleeve_lots.first().map_or(1, |lot| lot.spec.quantity_scale);
            let units = sleeve_lots.iter().try_fold(0_i64, |sum, lot| {
                sum.checked_add(lot.units_remaining.0)
                    .ok_or(ArithmeticError::Overflow {
                        operation: "target-allocation sleeve quantity",
                    })
            })?;
            let value = if price > 0 {
                sleeve_lots.iter().try_fold(0_i64, |sum, lot| {
                    let lot_value = mul_div_round_half_up(
                        lot.units_remaining.0,
                        price,
                        lot.spec.quantity_scale,
                        "target-allocation sleeve value",
                    )?;
                    sum.checked_add(lot_value)
                        .ok_or(ArithmeticError::Overflow {
                            operation: "target-allocation sleeve value total",
                        })
                        .map_err(SimulationError::from)
                })?
            } else {
                0
            };
            prices.push(price);
            scales.push(scale);
            available_units.push(units);
            values.push(value);
        }
        let weights: Vec<_> = policy.sleeves.iter().map(|sleeve| sleeve.weight).collect();
        let sleeve_withdrawals = withdrawal_by_sleeve(&values, &weights, raise.0)?;
        let sleeve_deposits = deposit_by_sleeve(&values, &weights, invest.0)?;
        let (rebalance_sales, rebalance_buys) = if raise == Money(0) && invest == Money(0) {
            if let Some(tolerance) = policy.rebalance_tolerance_ppb {
                rebalance_by_sleeve(&values, &weights, tolerance)?
            } else {
                (vec![0; values.len()], vec![0; values.len()])
            }
        } else {
            (vec![0; values.len()], vec![0; values.len()])
        };
        for (sleeve_index, sleeve) in policy.sleeves.iter().enumerate() {
            if policy.purchase_slots_per_sleeve > 0 {
                let band_units = quantity_for_value(
                    sleeve_deposits[sleeve_index],
                    prices[sleeve_index],
                    sleeve.quantity_scale,
                    false,
                )?;
                let rebalance_units = quantity_for_value(
                    rebalance_buys[sleeve_index],
                    prices[sleeve_index],
                    sleeve.quantity_scale,
                    false,
                )?;
                let wanted_units =
                    band_units
                        .checked_add(rebalance_units)
                        .ok_or(ArithmeticError::Overflow {
                            operation: "target-allocation purchase quantity",
                        })?;
                if wanted_units > 0 {
                    pending_buys.push(PendingAllocationBuy {
                        policy_index,
                        sleeve_index,
                        wanted_units,
                        unit_price: prices[sleeve_index],
                    });
                }
            }
            let band_units = quantity_for_value(
                sleeve_withdrawals[sleeve_index],
                prices[sleeve_index],
                scales[sleeve_index],
                true,
            )?;
            let rebalance_units = quantity_for_value(
                rebalance_sales[sleeve_index],
                prices[sleeve_index],
                scales[sleeve_index],
                false,
            )?;
            let requested = band_units
                .checked_add(rebalance_units)
                .ok_or(ArithmeticError::Overflow {
                    operation: "target-allocation sale quantity",
                })?
                .min(available_units[sleeve_index]);
            if requested <= 0 {
                continue;
            }
            let cause_id = format!(
                "{}_m{month}_security:{}",
                policy.cause_id_prefix, sleeve.asset_id
            );
            let mut remaining = requested;
            for source_account in &source_accounts {
                if remaining == 0 {
                    break;
                }
                let mut candidates: Vec<_> = lots
                    .iter()
                    .enumerate()
                    .filter(|(_, lot)| {
                        lot.spec.agent_id == policy.agent_id
                            && lot.spec.account_id == *source_account
                            && lot.spec.asset_id == sleeve.asset_id
                            && lot.units_remaining.0 > 0
                    })
                    .map(|(index, _)| index)
                    .collect();
                candidates.sort_by_key(|index| {
                    (lots[*index].fifo_rank, lots[*index].spec.lot_id.clone())
                });
                let available = candidates.iter().try_fold(0_i64, |sum, index| {
                    sum.checked_add(lots[*index].units_remaining.0).ok_or(
                        ArithmeticError::Overflow {
                            operation: "target-allocation pool quantity",
                        },
                    )
                })?;
                let target = remaining.min(available);
                if target == 0 {
                    continue;
                }
                execute_target_allocation_pool_sale(
                    fixture,
                    ledger,
                    recorder,
                    lots,
                    tax_facts,
                    tlh_cumulative_harvest,
                    month,
                    &cause_id,
                    &policy.agent_id,
                    &policy.account_id,
                    prices[sleeve_index],
                    target,
                    None,
                    &candidates,
                )?;
                remaining -= target;
            }
        }
    }
    Ok(pending_buys)
}

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_target_allocation_buys(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    buy_count: &mut [Vec<u32>],
    month: u32,
    pending_buys: &[PendingAllocationBuy],
) -> Result<(), SimulationError> {
    for order in pending_buys {
        let policy = &fixture.scenario.target_allocation_policies[order.policy_index];
        let sleeve = &policy.sleeves[order.sleeve_index];
        let cash_account = AccountRef::new(&policy.agent_id, &policy.account_id);
        let cash = ledger.balance(&cash_account)?.0.max(0);
        let affordable = quantity_for_value(cash, order.unit_price, sleeve.quantity_scale, false)?;
        let units = order.wanted_units.min(affordable);
        if units <= 0 {
            continue;
        }

        let used = buy_count[order.policy_index][order.sleeve_index];
        if used >= policy.purchase_slots_per_sleeve {
            return Err(SimulationError::TargetAllocationPurchaseSlotExhaustion {
                cause_id_prefix: policy.cause_id_prefix.clone(),
                sleeve_index: order.sleeve_index,
                configured: policy.purchase_slots_per_sleeve,
                needed: used.checked_add(1).ok_or(ArithmeticError::Overflow {
                    operation: "target-allocation purchase count",
                })?,
            });
        }
        let lot_id = format!(
            "{}_buy_p{}_s{}_{}",
            policy.cause_id_prefix, order.policy_index, order.sleeve_index, used
        );
        let lot_index = lots
            .iter()
            .position(|lot| lot.spec.lot_id == lot_id)
            .expect("validated purchase slot must be preallocated");
        let spent = Money(mul_div_round_half_up(
            units,
            order.unit_price,
            sleeve.quantity_scale,
            "target-allocation purchase value",
        )?);
        let cause_id = format!(
            "{}_buy_m{month}_security:{}",
            policy.cause_id_prefix, sleeve.asset_id
        );
        recorder.apply_entry(
            ledger,
            JournalEntry {
                month,
                cause_id,
                postings: vec![
                    Posting {
                        account: cash_account,
                        amount: spent.checked_neg()?,
                    },
                    Posting {
                        account: asset_basis_account(&lots[lot_index].spec),
                        amount: spent,
                    },
                ],
            },
        )?;
        let lot = &mut lots[lot_index];
        lot.spec.purchase_month = i32::try_from(month).map_err(|_| ArithmeticError::Overflow {
            operation: "target-allocation purchase month",
        })?;
        lot.spec.units = Quantity(units);
        lot.spec.basis = spent;
        lot.units_remaining = Quantity(units);
        lot.basis_remaining = spent;
        lot.basis_per_unit = Money(order.unit_price);
        buy_count[order.policy_index][order.sleeve_index] =
            used.checked_add(1).ok_or(ArithmeticError::Overflow {
                operation: "target-allocation purchase count",
            })?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_target_allocation_pool_sale(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    tlh_cumulative_harvest: &mut [Money],
    month: u32,
    cause_id: &str,
    agent_id: &str,
    proceeds_account_id: &str,
    price: i64,
    target: i64,
    source_account_override: Option<&str>,
    candidates: &[usize],
) -> Result<(), SimulationError> {
    let mut remaining = target;
    let mut planned = Vec::new();
    let mut total_proceeds = Money(0);
    let mut total_gain = Money(0);
    for index in candidates.iter().copied() {
        if remaining == 0 {
            break;
        }
        let lot = &lots[index];
        let units = remaining.min(lot.units_remaining.0);
        let basis = Money(mul_div_round_half_up(
            lot.basis_per_unit.0,
            units,
            lot.spec.quantity_scale,
            "target-allocation FIFO basis",
        )?);
        let proceeds = Money(mul_div_round_half_up(
            units,
            price,
            lot.spec.quantity_scale,
            "target-allocation sale proceeds",
        )?);
        let realized_gain = proceeds.checked_sub(basis)?;
        total_proceeds = total_proceeds.checked_add(proceeds)?;
        total_gain = total_gain.checked_add(realized_gain)?;
        planned.push(PlannedDisposition {
            lot_index: index,
            units: Quantity(units),
            basis,
            proceeds,
            realized_gain,
        });
        remaining -= units;
    }
    debug_assert_eq!(remaining, 0);
    let tlh_give_back =
        tlh_give_back_for_pool_sale(fixture, lots, &planned, tlh_cumulative_harvest)?;
    let mut postings = Vec::with_capacity(planned.len() + 2);
    postings.push(Posting {
        account: AccountRef::new(agent_id, proceeds_account_id),
        amount: total_proceeds,
    });
    for item in &planned {
        postings.push(Posting {
            account: asset_basis_account(&lots[item.lot_index].spec),
            amount: item.basis.checked_neg()?,
        });
    }
    postings.push(Posting {
        account: realized_gain_account(agent_id),
        amount: total_gain.checked_neg()?,
    });
    recorder.apply_entry(
        ledger,
        JournalEntry {
            month,
            cause_id: cause_id.into(),
            postings,
        },
    )?;
    for (item, give_back) in planned.into_iter().zip(tlh_give_back) {
        let lot = &mut lots[item.lot_index];
        let long_term = i64::from(month) - i64::from(lot.spec.purchase_month) >= 12;
        lot.units_remaining.0 -= item.units.0;
        lot.basis_remaining = lot.basis_remaining.checked_sub(item.basis)?;
        record_capital_gain(
            tax_facts,
            agent_id,
            item.realized_gain.checked_add(give_back)?,
            long_term,
        )?;
        recorder.record_disposition(LotDisposition {
            month,
            cause_id: cause_id.into(),
            agent_id: lot.spec.agent_id.clone(),
            source_account_id: source_account_override
                .unwrap_or(&lot.spec.account_id)
                .to_owned(),
            asset_id: canonical_lot_asset_id(&lot.spec.asset_id),
            lot_id: lot.spec.lot_id.clone(),
            purchase_month: lot.spec.purchase_month,
            quantity_scale: lot.spec.quantity_scale,
            units: item.units,
            basis: item.basis,
            proceeds: item.proceeds,
            proceeds_account_id: proceeds_account_id.to_owned(),
            realized_gain: item.realized_gain,
        })?;
    }
    Ok(())
}
