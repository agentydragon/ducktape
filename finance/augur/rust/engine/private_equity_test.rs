//! Exogenous recovery amounts are totals, independent of the issuer's per-unit mark.

use super::*;
use crate::execution::PrivateEquityTenderPolicySpec;

fn recovery_input(total: i64, positions: &[(i64, i64)]) -> ExecutionInput {
    let mut input = minimal_fixture();
    input.currency_quantum = "1".into();
    input.scenario.initial_lots = positions
        .iter()
        .enumerate()
        .map(|(index, (units, scale))| InitialLotSpec {
            lot_id: format!("recovery-{index}"),
            agent_id: "alice".into(),
            account_id: format!("holding-{index}"),
            asset_id: "private_equity:acme".into(),
            purchase_month: -24 + index as i32,
            quantity_scale: *scale,
            units: Quantity(*units),
            basis: Money(3 + index as i64),
        })
        .collect();
    input
        .scenario
        .private_equity_tender_policies
        .push(PrivateEquityTenderPolicySpec {
            owner_agent_id: "alice".into(),
            proceeds_account_id: "checking".into(),
            liquid_net_worth_floor: Money(0).into(),
        });
    for (channel, value) in [
        ("mark", 9),
        ("regime", 4),
        ("event_kind", 6),
        ("sale_opportunity", 0),
        ("sale_capacity", WIRE_RATE_SCALE),
        ("eligible", WIRE_RATE_SCALE),
        ("forced_sale", 0),
        ("liquidity_blocked", 1),
        ("forced_recovery", total),
        ("company_valuation", 0),
    ] {
        input.series.push(SeriesSpec {
            series_id: private_equity_series_id(channel, "acme"),
            snapshots: 2,
            values: vec![value; 2],
        });
    }
    input
}

fn assert_recovery(total: i64, positions: &[(i64, i64)], proceeds: &[i64]) {
    let mut input = recovery_input(total, positions);
    // The existing FIFO selection, not storage order, decides ties between lots.
    input.scenario.initial_lots.reverse();
    let output = simulate(&input).unwrap();
    let rollout = &output.rollouts[0];
    assert_eq!(rollout.failed_month, None);
    assert_eq!(
        rollout
            .dispositions
            .iter()
            .map(|sale| sale.proceeds.0)
            .collect::<Vec<_>>(),
        proceeds
    );
    assert_eq!(
        rollout
            .dispositions
            .iter()
            .map(|sale| sale.proceeds.0)
            .sum::<i64>(),
        total
    );
    let ending = rollout.months.last().unwrap();
    assert!(
        ending
            .lots
            .iter()
            .all(|lot| { lot.units_remaining == Quantity(0) && lot.basis_remaining == Money(0) })
    );
    assert_eq!(
        ending
            .balances
            .iter()
            .find(|row| row.account == AccountRef::new("alice", "checking"))
            .unwrap()
            .balance,
        Money(total)
    );
    for (index, sale) in rollout.dispositions.iter().enumerate() {
        assert_eq!(sale.units, Quantity(positions[index].0));
        assert_eq!(sale.quantity_scale, positions[index].1);
        assert_eq!(sale.basis, Money(3 + index as i64));
        assert_eq!(
            sale.realized_gain,
            Money(proceeds[index] - (3 + index as i64))
        );
        assert_eq!(sale.source_account_id, format!("holding-{index}"));
    }
    assert_eq!(
        ending
            .balances
            .iter()
            .map(|row| i128::from(row.balance.0))
            .sum::<i128>(),
        0
    );
}

#[test]
fn recovery_total_one_for_three_units_is_not_rounded_to_zero() {
    assert_recovery(1, &[(3, 1)], &[1]);
}

#[test]
fn recovery_total_two_for_three_units_is_not_rounded_to_three() {
    assert_recovery(2, &[(3, 1)], &[2]);
}

#[test]
fn recovery_total_is_apportioned_across_lots_and_accounts() {
    assert_recovery(5, &[(1, 1), (2, 1), (3, 1)], &[1, 2, 2]);
}

#[test]
fn recovery_uses_economic_units_across_different_account_scales() {
    assert_recovery(2, &[(1, 1), (10, 10), (100, 100)], &[1, 1, 0]);
}

#[test]
fn recovery_cashout_applies_to_the_remaining_position_after_an_earlier_sale() {
    let mut input = recovery_input(2, &[(3, 1), (1, 1)]);
    input.scenario.horizon_months = 2;
    for series in &mut input.series {
        series.snapshots = 3;
        series.values.push(series.values[1]);
        if series.series_id == private_equity_series_id("forced_recovery", "acme") {
            series.values[0] = 0;
        }
        if series.series_id == private_equity_series_id("forced_sale", "acme") {
            series.values[0] = WIRE_RATE_SCALE / 2;
        }
    }
    let output = simulate(&input).unwrap();
    let rollout = &output.rollouts[0];
    assert_eq!(rollout.dispositions[0].proceeds, Money(18));
    assert_eq!(rollout.dispositions[0].basis, Money(2));
    assert_eq!(
        rollout.dispositions[1..]
            .iter()
            .map(|sale| (sale.units, sale.basis, sale.proceeds))
            .collect::<Vec<_>>(),
        [
            (Quantity(1), Money(1), Money(1)),
            (Quantity(1), Money(4), Money(1))
        ]
    );
    let ending = rollout.months.last().unwrap();
    assert!(
        ending
            .lots
            .iter()
            .all(|lot| lot.units_remaining == Quantity(0) && lot.basis_remaining == Money(0))
    );
    assert_eq!(
        ending
            .balances
            .iter()
            .find(|row| row.account == AccountRef::new("alice", "checking"))
            .unwrap()
            .balance,
        Money(20)
    );
}
