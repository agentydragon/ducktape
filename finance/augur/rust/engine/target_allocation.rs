//! Target-allocation execution: the cash-band raise before obligations settle, the
//! purchases that run after they do, and quiet-band drift rebalancing.

use super::*;
use crate::execution::TargetAllocationPolicySpec;

#[derive(Clone, Debug)]
pub(super) struct PendingAllocationBuy {
    pub(super) policy_index: usize,
    pub(super) sleeve_index: usize,
    wanted_units: i64,
    unit_price: i64,
}

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_target_allocation_sales(
    fixture: &ExecutionInput,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax: &mut TaxState,
    tlh_cumulative_harvest: &mut [Money],
    month: u32,
    obligations: &claims::Claims,
    consumption: Option<&payments::Consume>,
    decision: Option<&allocation::Policy<'_>>,
) -> Result<Vec<PendingAllocationBuy>, SimulationError> {
    let mut pending_buys = Vec::new();
    for (policy_index, policy) in fixture
        .scenario
        .target_allocation_policies
        .iter()
        .enumerate()
    {
        let cash_account = AccountRef::new(&policy.agent_id, &policy.account_id);
        let consumption_due = consumption
            .filter(|request| request.from == cash_account)
            .map_or(Money(0), |request| request.amount);
        let hard_demand = observations::due_claims(obligations, &policy.agent_id)
            .filter(|claim| claim.from == &cash_account)
            .try_fold(consumption_due, |sum, claim| {
                sum.checked_add(claim.amount_due)
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

        let source_accounts = source_accounts(policy);
        let holdings = sleeve_holdings(fixture, rollout_id, month, lots, policy)?;
        let values: Vec<_> = holdings.iter().map(|holding| holding.value).collect();
        let prices: Vec<_> = holdings.iter().map(|holding| holding.price).collect();
        let scales: Vec<_> = holdings
            .iter()
            .map(|holding| holding.quantity_scale)
            .collect();
        let available_units: Vec<_> = holdings.iter().map(|holding| holding.units).collect();
        let configured_weights: Vec<_> =
            policy.sleeves.iter().map(|sleeve| sleeve.weight).collect();
        let weights = decision
            .filter(|decision| decision.policy_index == policy_index)
            .map_or(configured_weights.as_slice(), |decision| {
                decision.weights.as_slice()
            });
        let sleeve_withdrawals = withdrawal_by_sleeve(&values, weights, raise.0)?;
        let sleeve_deposits = deposit_by_sleeve(&values, weights, invest.0)?;
        let (rebalance_sales, rebalance_buys) = if raise == Money(0) && invest == Money(0) {
            if let Some(tolerance) = policy.rebalance_tolerance_ppb {
                rebalance_by_sleeve(&values, weights, tolerance)?
            } else {
                (vec![0; values.len()], vec![0; values.len()])
            }
        } else {
            (vec![0; values.len()], vec![0; values.len()])
        };
        for (sleeve_index, sleeve) in policy.sleeves.iter().enumerate() {
            if policy.allow_purchases {
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
                let candidates: Vec<_> = lots
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
                let request = SaleRequest {
                    cause_id: cause_id.clone(),
                    agent_id: policy.agent_id.clone(),
                    proceeds_account_id: policy.account_id.clone(),
                    asset_id: sleeve.asset_id.clone(),
                    lots: select_fifo(lots, &candidates, Quantity(target), &cause_id)?,
                };
                execute_lot_sale(
                    fixture,
                    ledger,
                    recorder,
                    lots,
                    tax,
                    SaleTlh::Pool(tlh_cumulative_harvest),
                    month,
                    PerUnit(prices[sleeve_index]),
                    &request,
                )?;
                remaining -= target;
            }
        }
    }
    Ok(pending_buys)
}

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_target_allocation_buys(
    fixture: &ExecutionInput,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut Vec<LotState>,
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
        let next = used.checked_add(1).ok_or(ArithmeticError::Overflow {
            operation: "target-allocation purchase count",
        })?;
        let request = PurchaseRequest {
            cause_id: format!(
                "{}_buy_m{month}_security:{}",
                policy.cause_id_prefix, sleeve.asset_id
            ),
            agent_id: policy.agent_id.clone(),
            cash_account_id: policy.account_id.clone(),
            holding_account_id: policy
                .source_account_ids
                .first()
                .unwrap_or(&policy.account_id)
                .clone(),
            asset_id: sleeve.asset_id.clone(),
            lot_id: format!(
                "{}_buy_p{}_s{}_{}",
                policy.cause_id_prefix, order.policy_index, order.sleeve_index, used
            ),
            quantity_scale: sleeve.quantity_scale,
            units: Quantity(units),
        };
        execute_purchase(
            fixture,
            ledger,
            recorder,
            lots,
            month,
            PerUnit(order.unit_price),
            &request,
        )?;
        buy_count[order.policy_index][order.sleeve_index] = next;
    }
    Ok(())
}

fn source_accounts(policy: &TargetAllocationPolicySpec) -> Vec<&str> {
    if policy.source_account_ids.is_empty() {
        vec![policy.account_id.as_str()]
    } else {
        policy
            .source_account_ids
            .iter()
            .map(String::as_str)
            .collect()
    }
}

pub(super) struct SleeveHolding {
    pub(super) value: i64,
    price: i64,
    quantity_scale: i64,
    units: i64,
}

/// Value the declared source pools with the same per-lot rounding used by execution.
pub(super) fn sleeve_holdings(
    input: &ExecutionInput,
    rollout: u32,
    month: u32,
    lots: &[LotState],
    policy: &TargetAllocationPolicySpec,
) -> Result<Vec<SleeveHolding>, SimulationError> {
    let sources = source_accounts(policy);
    policy
        .sleeves
        .iter()
        .map(|sleeve| {
            let price = series_value(
                input,
                &format!("security:{}", sleeve.asset_id),
                rollout,
                month,
            )?;
            let sleeve_lots: Vec<_> = lots
                .iter()
                .filter(|lot| {
                    lot.spec.agent_id == policy.agent_id
                        && lot.spec.asset_id == sleeve.asset_id
                        && sources.contains(&lot.spec.account_id.as_str())
                })
                .collect();
            let quantity_scale = sleeve_lots.first().map_or(1, |lot| lot.spec.quantity_scale);
            let units = sleeve_lots.iter().try_fold(0_i64, |sum, lot| {
                sum.checked_add(lot.units_remaining.0)
                    .ok_or(ArithmeticError::Overflow {
                        operation: "target-allocation sleeve quantity",
                    })
            })?;
            let value = sleeve_lots.iter().try_fold(Money(0), |sum, lot| {
                sum.checked_add(lot.view().value(PerUnit(price))?)
            })?;
            Ok(SleeveHolding {
                value: value.0,
                price,
                quantity_scale,
                units,
            })
        })
        .collect()
}
