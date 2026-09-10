//! Currency-quantum conservation, not parity with independent per-lot rounding.

use super::*;

fn input_with_lots(count: usize) -> (ExecutionInput, Vec<LotState>) {
    let mut input = crate::engine::tests::minimal_fixture();
    input.scenario.initial_lots = (0..count)
        .map(|index| InitialLotSpec {
            lot_id: format!("lot-{index}"),
            agent_id: "alice".into(),
            account_id: "checking".into(),
            asset_id: "fund".into(),
            purchase_month: if index == 0 { -12 } else { 0 },
            quantity_scale: 1,
            units: Quantity(1),
            basis: Money(100),
        })
        .collect();
    input.scenario.harvest_policies.push(HarvestPolicySpec {
        owner_agent_id: "alice".into(),
        account_id: "checking".into(),
        asset_id: "fund".into(),
        peak_annual_yield_ppb: WIRE_RATE_SCALE / 100,
        floor_annual_yield_ppb: 0,
        maturity_decay_exponent_ppb: WIRE_RATE_SCALE,
        drawdown_sensitivity_ppb: 0,
        short_term_fraction_ppb: WIRE_RATE_SCALE,
    });
    input.series.push(SeriesSpec {
        series_id: "security:fund".into(),
        snapshots: 2,
        values: vec![1, 1],
    });
    ValidatedInput::new(&input).unwrap();
    let lots = input
        .scenario
        .initial_lots
        .iter()
        .map(|spec| LotState {
            spec: spec.clone(),
            units_remaining: spec.units,
            basis_remaining: spec.basis,
        })
        .collect();
    (input, lots)
}

fn disposition(lot_index: usize) -> PlannedDisposition {
    PlannedDisposition {
        lot_index,
        units: Quantity(1),
        basis: Money(100),
        proceeds: Money(1),
        realized_gain: Money(-99),
    }
}

#[test]
fn full_pool_liquidation_cannot_strand_or_overdraw_one_deferred_quantum() {
    for count in 2..=7 {
        let (input, lots) = input_with_lots(count);
        let planned: Vec<_> = (0..count).map(disposition).collect();
        let mut cumulative = vec![Money(1)];
        let sale = SaleTlh::Pool(&mut cumulative);
        let prepared = sale.prepare(&input, &lots, &planned).unwrap();
        assert!(prepared.by_lot.iter().all(|amount| amount.0 >= 0));
        assert_eq!(
            prepared.by_lot.iter().map(|amount| amount.0).sum::<i64>(),
            1,
            "{count} lots"
        );
        sale.commit(prepared);
        assert_eq!(cumulative, [Money(0)], "{count} lots");
    }
}

#[test]
fn partial_pool_sale_returns_the_rounded_proportion_and_nonnegative_remainder() {
    for count in 2..=7 {
        for deferred in [1, 2, 5, 9] {
            let (input, lots) = input_with_lots(count);
            for sold in 1..count {
                let planned: Vec<_> = (0..sold).map(disposition).collect();
                let mut cumulative = vec![Money(deferred)];
                let sale = SaleTlh::Pool(&mut cumulative);
                let prepared = sale.prepare(&input, &lots, &planned).unwrap();
                let total: i64 = prepared.by_lot.iter().map(|amount| amount.0).sum();
                // Exact rational half-up expectation for these small integer cases.
                let expected = (deferred * sold as i64 + count as i64 / 2) / count as i64;
                assert_eq!(total, expected);
                assert!(prepared.by_lot.iter().all(|amount| amount.0 >= 0));
                sale.commit(prepared);
                assert_eq!(cumulative[0].0 + total, deferred);
                assert!(cumulative[0].0 >= 0);
            }
        }
    }
}

#[test]
fn scheduled_full_liquidation_conserves_deferral_when_split_into_transactions() {
    let (input, lots) = input_with_lots(3);
    let mut cumulative = vec![Money(1)];
    let mut state = scheduled_tlh_give_back_state(&input, &lots, &cumulative).unwrap();
    let mut give_back: Vec<Money> = Vec::new();
    for index in 0..3 {
        let sale = SaleTlh::Scheduled(&mut state);
        let prepared = sale.prepare(&input, &lots, &[disposition(index)]).unwrap();
        give_back.extend(&prepared.by_lot);
        sale.commit(prepared);
    }
    apply_scheduled_tlh_give_back(&state, &mut cumulative).unwrap();
    assert_eq!(give_back.iter().map(|amount| amount.0).sum::<i64>(), 1);
    assert_eq!(cumulative, [Money(0)]);
}

#[test]
fn scheduled_partial_and_full_sales_are_invariant_to_request_fragmentation() {
    let (input, lots) = input_with_lots(6);
    for deferred in [1, 2, 5, 9] {
        for sold in [2, 4, 6] {
            let mut reference = None;
            for chunk_size in 1..=sold {
                let planned: Vec<_> = (0..sold).map(disposition).collect();
                let mut cumulative = vec![Money(deferred)];
                let mut state = scheduled_tlh_give_back_state(&input, &lots, &cumulative).unwrap();
                let mut by_lot: Vec<Money> = Vec::new();
                for chunk in planned.chunks(chunk_size) {
                    let sale = SaleTlh::Scheduled(&mut state);
                    let prepared = sale.prepare(&input, &lots, chunk).unwrap();
                    by_lot.extend(&prepared.by_lot);
                    sale.commit(prepared);
                }
                apply_scheduled_tlh_give_back(&state, &mut cumulative).unwrap();
                let total: i64 = by_lot.iter().map(|amount| amount.0).sum();
                assert_eq!(total, (deferred * sold as i64 + 3) / 6);
                assert_eq!(total + cumulative[0].0, deferred);
                assert!(cumulative[0].0 >= 0);
                if sold == 6 {
                    assert_eq!(cumulative, [Money(0)]);
                }
                if let Some(expected) = &reference {
                    assert_eq!(&by_lot, expected);
                } else {
                    reference = Some(by_lot);
                }
            }
        }
    }
}
