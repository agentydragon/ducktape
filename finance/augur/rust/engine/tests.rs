//! Unit tests for the engine's internals.

use crate::fixture::{
    AccountSpec, BondSpec, DistributionSpec, DistributionTaxSliceSpec, InitialLotSpec,
    JurisdictionIdentitySpec, LocationSpec, MortgageFinancingSpec, ObligationSpec,
    PropertyTaxPolicySpec, RecurringObligationSpec, ScenarioSpec, ScheduledPropertyPurchaseSpec,
    ScheduledSaleSpec, ScheduledTransferSpec, SeriesIndexedAmountKind, SeriesIndexedAmountSpec,
    SeriesSpec,
};

use super::*;

fn minimal_fixture() -> Fixture {
    Fixture {
        schema_version: FIXTURE_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 1,
            accounts: vec![AccountSpec {
                account: AccountRef::new("alice", "checking"),
                opening_balance: Money(0),
            }],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![],
            recurring_obligations: vec![],
            initial_lots: vec![],
            initial_bonds: vec![],
            scheduled_sales: vec![],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
        },
        series: vec![],
    }
}

#[test]
fn rejects_invalid_fixture_metadata() {
    let mut fixture = minimal_fixture();
    fixture.rollout_count = 0;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::EmptyRollouts)
    ));

    let mut fixture = minimal_fixture();
    fixture.scenario.horizon_months = 0;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::EmptyHorizon)
    ));

    let mut fixture = minimal_fixture();
    fixture.currency_code = "usd".into();
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidCurrencyCode { .. })
    ));

    let mut fixture = minimal_fixture();
    fixture.currency_quantum = "0".into();
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidCurrencyQuantum { .. })
    ));
}

#[test]
fn series_indexed_amounts_follow_rollout_specific_reset_boundaries() {
    let mut fixture = minimal_fixture();
    fixture.rollout_count = 2;
    fixture.scenario.horizon_months = 13;
    fixture.scenario.accounts.push(AccountSpec {
        account: AccountRef::new("landlord", "checking"),
        opening_balance: Money(0),
    });
    fixture.scenario.accounts[0].opening_balance = Money(100_000);
    fixture.scenario.recurring_obligations = vec![RecurringObligationSpec {
        start_month: 0,
        end_month: Some(12),
        obligation_id: "rent".into(),
        obligation_type: "cash_spend".into(),
        from: AccountRef::new("alice", "checking"),
        to: AccountRef::new("landlord", "checking"),
        amount_due: AmountSpec::SeriesIndexed(SeriesIndexedAmountSpec {
            kind: SeriesIndexedAmountKind::SeriesIndexed,
            base_amount: Money(1_001),
            series_id: "rent:test".into(),
            base_month_index: 0,
            adjustment_period_months: 12,
        }),
        property_id: None,
        deduction_category: None,
    }];
    fixture.series = vec![SeriesSpec {
        series_id: "rent:test".into(),
        snapshots: 14,
        values: [
            vec![1_000_000_000; 12],
            vec![1_100_000_000, 1_100_000_000],
            vec![1_000_000_000; 12],
            vec![1_250_000_000, 1_250_000_000],
        ]
        .concat(),
    }];

    let output = simulate(&fixture).unwrap();
    assert_eq!(output.rollouts[0].obligations[11].amount_due, Money(1_001));
    assert_eq!(output.rollouts[0].obligations[12].amount_due, Money(1_101));
    assert_eq!(output.rollouts[1].obligations[11].amount_due, Money(1_001));
    assert_eq!(output.rollouts[1].obligations[12].amount_due, Money(1_251));
}

#[test]
fn series_indexed_amount_validation_rejects_invalid_paths() {
    let amount = AmountSpec::SeriesIndexed(SeriesIndexedAmountSpec {
        kind: SeriesIndexedAmountKind::SeriesIndexed,
        base_amount: Money(1),
        series_id: "inflation".into(),
        base_month_index: 1,
        adjustment_period_months: 12,
    });
    let mut fixture = minimal_fixture();
    fixture.scenario.horizon_months = 2;
    fixture
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 0,
            cause_id: "too-early".into(),
            from: AccountRef::new("alice", "checking"),
            to: AccountRef::new("alice", "checking"),
            amount,
            income_category: None,
            deduction_category: None,
        });
    fixture.series.push(SeriesSpec {
        series_id: "inflation".into(),
        snapshots: 3,
        values: vec![1_000_000_000, 1_000_000_000, 1_000_000_000],
    });
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::SeriesAmountBeforeBase {
            month: 0,
            base_month: 1,
            ..
        })
    ));

    fixture.scenario.scheduled_transfers[0].month = 1;
    fixture.series[0].values[1] = 0;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::NonPositiveSeriesAmountLevel {
            month: 1,
            value: 0,
            ..
        })
    ));

    fixture.series[0].values[1] = 1_000_000_000;
    if let AmountSpec::SeriesIndexed(amount) = &mut fixture.scenario.scheduled_transfers[0].amount {
        amount.adjustment_period_months = 0;
    }
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidSeriesIndexedAmount { .. })
    ));

    if let AmountSpec::SeriesIndexed(amount) = &mut fixture.scenario.scheduled_transfers[0].amount {
        amount.adjustment_period_months = 1;
        amount.series_id = "security:vti".into();
    }
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnsupportedAmountSeries { .. })
    ));

    if let AmountSpec::SeriesIndexed(amount) = &mut fixture.scenario.scheduled_transfers[0].amount {
        amount.series_id = "inflation".into();
    }
    fixture.series[0].values[1] = (1_i64 << 53) + 1;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InexactSeriesAmountLevel { month: 1, .. })
    ));
}

#[test]
fn nominal_and_indexed_bonds_follow_coupon_redemption_and_accretion_contracts() {
    let mut fixture = minimal_fixture();
    fixture.rollout_count = 2;
    fixture.scenario.horizon_months = 13;
    fixture.scenario.jurisdictions = vec![JurisdictionIdentitySpec {
        jurisdiction_id: "federal_us".into(),
        level: JurisdictionLevel::Federal,
    }];
    fixture.scenario.initial_bonds = vec![
        BondSpec {
            bond_id: "treasury".into(),
            agent_id: "alice".into(),
            account_id: "checking".into(),
            issuer_jurisdiction_id: Some("federal_us".into()),
            face_value: Money(100_000_000),
            purchase_price: Money(100_000_000),
            annual_coupon_rate_ppb: 50_000_000,
            coupon_period_months: 6,
            inflation_indexed: false,
            purchase_month_index: -1,
            maturity_month_index: 11,
        },
        BondSpec {
            bond_id: "tips".into(),
            agent_id: "alice".into(),
            account_id: "checking".into(),
            issuer_jurisdiction_id: Some("federal_us".into()),
            face_value: Money(100_000_000),
            purchase_price: Money(100_000_000),
            annual_coupon_rate_ppb: 40_000_000,
            coupon_period_months: 6,
            inflation_indexed: true,
            purchase_month_index: -1,
            maturity_month_index: 11,
        },
        BondSpec {
            bond_id: "expired".into(),
            agent_id: "alice".into(),
            account_id: "checking".into(),
            issuer_jurisdiction_id: Some("federal_us".into()),
            face_value: Money(100_000_000),
            purchase_price: Money(100_000_000),
            annual_coupon_rate_ppb: 50_000_000,
            coupon_period_months: 6,
            inflation_indexed: false,
            purchase_month_index: -13,
            maturity_month_index: -1,
        },
    ];
    fixture.series = vec![SeriesSpec {
        series_id: "inflation".into(),
        snapshots: 14,
        values: [
            vec![1_000_000_000; 6],
            vec![2_000_000_000; 8],
            vec![1_000_000_000; 6],
            vec![1_500_000_000; 8],
        ]
        .concat(),
    }];

    let output = simulate(&fixture).unwrap();
    let first = &output.rollouts[0];
    let first_by_bond_month: BTreeMap<_, _> = first
        .bond_cashflows
        .iter()
        .map(|flow| ((flow.bond_id.as_str(), flow.month), flow))
        .collect();
    assert_eq!(
        first_by_bond_month[&("treasury", 5)].coupon,
        Money(2_500_000)
    );
    assert_eq!(
        first_by_bond_month[&("treasury", 11)].redemption,
        Money(100_000_000)
    );
    assert_eq!(first_by_bond_month[&("tips", 5)].coupon, Money(2_000_000));
    assert_eq!(
        first_by_bond_month[&("tips", 6)].accretion,
        Money(100_000_000)
    );
    assert_eq!(first_by_bond_month[&("tips", 11)].coupon, Money(4_000_000));
    assert_eq!(
        first_by_bond_month[&("tips", 11)].redemption,
        Money(200_000_000)
    );
    assert_eq!(first.months[5].bonds[1].principal, Money(100_000_000));
    assert_eq!(first.months[6].bonds[1].principal, Money(200_000_000));
    assert!(!first.months[12].bonds[1].active);
    assert_eq!(first.months[12].bonds[1].principal, Money(0));
    assert!(first.bond_cashflows.iter().all(|flow| flow.month <= 11));
    assert!(
        first
            .bond_cashflows
            .iter()
            .all(|flow| flow.bond_id != "expired")
    );

    let second = &output.rollouts[1];
    let second_tips = second
        .bond_cashflows
        .iter()
        .filter(|flow| flow.bond_id == "tips")
        .collect::<Vec<_>>();
    assert_eq!(second_tips[1].accretion, Money(50_000_000));
    assert_eq!(second_tips.last().unwrap().redemption, Money(150_000_000));
    assert!(
        output
            .rollouts
            .iter()
            .all(|rollout| rollout.journal.iter().all(|entry| {
                entry
                    .postings
                    .iter()
                    .map(|posting| i128::from(posting.amount.0))
                    .sum::<i128>()
                    == 0
            }))
    );
}

#[test]
fn bond_validation_rejects_non_par_and_missing_index_paths() {
    let mut fixture = minimal_fixture();
    fixture.scenario.initial_bonds = vec![BondSpec {
        bond_id: "bad".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        issuer_jurisdiction_id: None,
        face_value: Money(100),
        purchase_price: Money(99),
        annual_coupon_rate_ppb: 50_000_000,
        coupon_period_months: 6,
        inflation_indexed: false,
        purchase_month_index: -6,
        maturity_month_index: 6,
    }];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidBondTerms { .. })
    ));

    fixture.scenario.initial_bonds[0].purchase_price = Money(100);
    fixture.scenario.initial_bonds[0].inflation_indexed = true;
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::MissingBondInflationSeries { .. })
    ));

    fixture.scenario.initial_bonds[0].inflation_indexed = false;
    fixture.scenario.initial_bonds[0].issuer_jurisdiction_id = Some("federal_us".into());
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnknownBondIssuer { .. })
    ));
}

#[test]
fn nominal_bond_coupon_rounds_the_full_rational_once() {
    let mut bond = BondSpec {
        bond_id: "rounding".into(),
        agent_id: "alice".into(),
        account_id: "checking".into(),
        issuer_jurisdiction_id: None,
        face_value: Money(600),
        purchase_price: Money(600),
        annual_coupon_rate_ppb: 10_000_000,
        coupon_period_months: 1,
        inflation_indexed: false,
        purchase_month_index: 0,
        maturity_month_index: 12,
    };
    assert_eq!(bond_coupon(bond.face_value, &bond).unwrap(), Money(1));

    bond.face_value = Money(180);
    bond.purchase_price = Money(180);
    bond.annual_coupon_rate_ppb = 33_333_333;
    assert_eq!(bond_coupon(bond.face_value, &bond).unwrap(), Money(0));

    bond.face_value = Money(1_250_627);
    bond.purchase_price = Money(1_250_627);
    bond.annual_coupon_rate_ppb = 37_000_000;
    bond.coupon_period_months = 5;
    bond.maturity_month_index = 60;
    assert_eq!(bond_coupon(bond.face_value, &bond).unwrap(), Money(19_280));
}

#[test]
fn rejects_invalid_references_before_rollout_execution() {
    let mut fixture = minimal_fixture();
    fixture
        .scenario
        .scheduled_transfers
        .push(ScheduledTransferSpec {
            month: 0,
            cause_id: "unknown-source".into(),
            from: AccountRef::new("missing", "checking"),
            to: AccountRef::new("alice", "checking"),
            amount: Money(1).into(),
            income_category: None,
            deduction_category: None,
        });
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnknownAccountReference { .. })
    ));
}

#[test]
fn distribution_tax_character_requires_a_complete_known_issuer_split() {
    let mut fixture = minimal_fixture();
    fixture.scenario.initial_lots = vec![InitialLotSpec {
        lot_id: "bnd".into(),
        agent_id: "alice".into(),
        account_id: "brokerage".into(),
        asset_id: "bnd".into(),
        purchase_month: -1,
        quantity_scale: 1_000_000,
        units: Quantity(1_000_000),
        basis: Money(1),
    }];
    fixture.scenario.distributions = vec![DistributionSpec {
        agent_id: "alice".into(),
        holding_account_id: "brokerage".into(),
        asset_id: "bnd".into(),
        to_account_id: "checking".into(),
        tax_character: vec![DistributionTaxSliceSpec {
            fraction_ppb: 400_000_000,
            issuer_jurisdiction_id: None,
        }],
    }];
    fixture.series = vec![SeriesSpec {
        series_id: "security_distribution:bnd".into(),
        snapshots: 2,
        values: vec![1, 1],
    }];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidDistributionTaxCharacter { .. })
    ));

    fixture.scenario.distributions[0].tax_character = vec![DistributionTaxSliceSpec {
        fraction_ppb: RATE_SCALE,
        issuer_jurisdiction_id: Some("federal_us".into()),
    }];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnknownDistributionIssuer { .. })
    ));
}

#[test]
fn rejects_invalid_property_contracts_before_rollout_execution() {
    let mut fixture = minimal_fixture();
    fixture.scenario.accounts.push(AccountSpec {
        account: AccountRef::new("seller", "checking"),
        opening_balance: Money(0),
    });
    fixture.scenario.scheduled_property_purchases = vec![ScheduledPropertyPurchaseSpec {
        month: 0,
        cause_id: "buy-home".into(),
        property_id: "home".into(),
        location_id: "missing".into(),
        buyer_agent_id: "alice".into(),
        buyer_account_id: "checking".into(),
        seller_agent_id: "seller".into(),
        seller_account_id: "checking".into(),
        purchase_price: Money(10),
        down_payment: Money(10),
        buyer_closing_cost: Money(0),
        rented_fraction_ppb: 0,
        land_value_fraction_ppb: 200_000_000,
        mortgage: None,
    }];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::UnknownLocation { .. })
    ));

    fixture.scenario.locations.push(LocationSpec {
        location_id: "missing".into(),
        display_name: "Known now".into(),
        jurisdiction_ids: vec![],
        annual_property_tax_rate_ppb: 0,
        annual_special_assessment: Money(0),
    });
    fixture.scenario.scheduled_property_purchases[0].down_payment = Money(9);
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidPropertyTerms { .. })
    ));
}

#[test]
fn rejects_mixed_quantity_scales_and_invalid_security_prices() {
    let mut fixture = minimal_fixture();
    fixture.scenario.initial_lots = vec![
        InitialLotSpec {
            lot_id: "a".into(),
            agent_id: "alice".into(),
            account_id: "brokerage".into(),
            asset_id: "vti".into(),
            purchase_month: -2,
            quantity_scale: 1_000_000,
            units: Quantity(1_000_000),
            basis: Money(1),
        },
        InitialLotSpec {
            lot_id: "b".into(),
            agent_id: "alice".into(),
            account_id: "brokerage".into(),
            asset_id: "vti".into(),
            purchase_month: -1,
            quantity_scale: 1_000,
            units: Quantity(1_000),
            basis: Money(1),
        },
    ];
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::MixedQuantityScale { .. })
    ));

    let mut fixture = minimal_fixture();
    fixture.series.push(SeriesSpec {
        series_id: "security:vti".into(),
        snapshots: 2,
        values: vec![100, -1],
    });
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InvalidSecurityPrice {
            index: 1,
            value: -1,
            ..
        })
    ));
}

#[test]
fn transfer_and_fifo_sale_remain_balanced() {
    let alice_cash = AccountRef::new("alice", "checking");
    let bob_cash = AccountRef::new("bob", "checking");
    let fixture = Fixture {
        schema_version: FIXTURE_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 2,
        scenario: ScenarioSpec {
            horizon_months: 2,
            accounts: vec![
                AccountSpec {
                    account: alice_cash.clone(),
                    opening_balance: Money(1_000),
                },
                AccountSpec {
                    account: bob_cash.clone(),
                    opening_balance: Money(2_000),
                },
            ],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![ScheduledTransferSpec {
                month: 0,
                cause_id: "gift".into(),
                from: bob_cash,
                to: alice_cash,
                amount: Money(500).into(),
                income_category: None,
                deduction_category: None,
            }],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![],
            recurring_obligations: vec![],
            initial_lots: vec![InitialLotSpec {
                lot_id: "lot-1".into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "vti".into(),
                purchase_month: -12,
                quantity_scale: 1_000_000,
                units: Quantity(2_000_000),
                basis: Money(20_000),
            }],
            initial_bonds: vec![],
            scheduled_sales: vec![ScheduledSaleSpec {
                month: 1,
                cause_id: "sell-vti".into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "vti".into(),
                units: Quantity(1_000_000),
                proceeds_account_id: "checking".into(),
            }],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
        },
        series: vec![SeriesSpec {
            series_id: "security:vti".into(),
            snapshots: 3,
            values: vec![10_000, 15_000, 15_000, 10_000, 20_000, 20_000],
        }],
    };
    let output = simulate(&fixture).unwrap();
    let dense = simulate_dense(&fixture).unwrap();
    let summaries = simulate_summaries(&fixture).unwrap();
    assert_eq!(output.rollouts.len(), 2);
    assert_eq!(dense.rollouts.len(), 2);
    assert_eq!(summaries.rollouts.len(), 2);
    assert_eq!(output.rollouts[0].dispositions[0].proceeds, Money(15_000));
    assert_eq!(output.rollouts[1].dispositions[0].proceeds, Money(20_000));
    for (forensic, dense) in output.rollouts.iter().zip(&dense.rollouts) {
        let mut expected = forensic.clone();
        expected.journal.clear();
        assert_eq!(&expected, dense);
        assert!(dense.journal.is_empty());
    }
    for (rollout, summary) in output.rollouts.iter().zip(&summaries.rollouts) {
        assert_eq!(summary.rollout_id, rollout.rollout_id);
        assert_eq!(
            summary.ending_balances,
            rollout.months.last().unwrap().balances
        );
        assert_eq!(
            summary.ending_properties,
            rollout.months.last().unwrap().properties
        );
        assert_eq!(summary.ending_bonds, rollout.months.last().unwrap().bonds);
        assert_eq!(
            summary.ending_mortgages,
            rollout.months.last().unwrap().mortgages
        );
        assert_eq!(summary.journal_entry_count, rollout.journal.len() as u64);
        assert_eq!(summary.disposition_count, rollout.dispositions.len() as u64);
        assert_eq!(summary.tax_accrual_count, rollout.tax_accruals.len() as u64);
        assert_eq!(
            summary.bond_cashflow_count,
            rollout.bond_cashflows.len() as u64
        );
        assert_eq!(
            summary.distribution_count,
            rollout.distributions.len() as u64
        );
        assert_eq!(
            summary.property_purchase_count,
            rollout.property_purchases.len() as u64
        );
        assert_eq!(
            summary.mortgage_payment_count,
            rollout.mortgage_payments.len() as u64
        );
        assert_eq!(summary.failed_month, rollout.failed_month);
    }
    for rollout in output.rollouts {
        for entry in rollout.journal {
            assert_eq!(
                entry
                    .postings
                    .iter()
                    .map(|posting| i128::from(posting.amount.0))
                    .sum::<i128>(),
                0
            );
        }
    }
}

#[test]
fn financed_property_purchase_and_first_monthly_carry_match_contract() {
    let mut fixture = minimal_fixture();
    fixture.scenario.horizon_months = 2;
    fixture.scenario.accounts = vec![
        AccountSpec {
            account: AccountRef::new("alice", "checking"),
            opening_balance: Money(12_000_000),
        },
        AccountSpec {
            account: AccountRef::new("seller", "checking"),
            opening_balance: Money(0),
        },
        AccountSpec {
            account: AccountRef::new("bank", "checking"),
            opening_balance: Money(0),
        },
        AccountSpec {
            account: AccountRef::new("county", "checking"),
            opening_balance: Money(0),
        },
    ];
    fixture.scenario.locations = vec![LocationSpec {
        location_id: "sf".into(),
        display_name: "San Francisco".into(),
        jurisdiction_ids: vec![],
        annual_property_tax_rate_ppb: 11_800_000,
        annual_special_assessment: Money(0),
    }];
    fixture.scenario.scheduled_property_purchases = vec![ScheduledPropertyPurchaseSpec {
        month: 0,
        cause_id: "alice-buys-home".into(),
        property_id: "home".into(),
        location_id: "sf".into(),
        buyer_agent_id: "alice".into(),
        buyer_account_id: "checking".into(),
        seller_agent_id: "seller".into(),
        seller_account_id: "checking".into(),
        purchase_price: Money(50_000_000),
        down_payment: Money(10_000_000),
        buyer_closing_cost: Money(1_000_000),
        rented_fraction_ppb: 0,
        land_value_fraction_ppb: 200_000_000,
        mortgage: Some(MortgageFinancingSpec {
            liability_id: "home-mortgage".into(),
            lender_agent_id: "bank".into(),
            lender_account_id: "checking".into(),
            principal: Money(40_000_000),
            annual_interest_rate_ppb: 60_000_000,
            term_months: 360,
        }),
    }];
    fixture.scenario.property_tax_policies = vec![PropertyTaxPolicySpec {
        property_id: "home".into(),
        owner_agent_id: "alice".into(),
        from_account_id: "checking".into(),
        tax_authority_agent_id: "county".into(),
        tax_authority_account_id: "checking".into(),
        annual_tax_rate_ppb: Some(12_000_000),
        start_month: 0,
        end_month: None,
    }];

    let rollout = simulate(&fixture).unwrap().rollouts.remove(0);
    let month_zero = &rollout.months[1];
    assert_eq!(month_zero.properties[0].adjusted_basis, Money(51_000_000));
    assert_eq!(
        month_zero.properties[0].contribution_used,
        Money(11_000_000)
    );
    assert_eq!(month_zero.properties[0].equity_ledger, Money(10_000_000));
    assert_eq!(month_zero.mortgages[0].monthly_payment, Money(239_820));
    assert_eq!(month_zero.mortgages[0].principal, Money(40_000_000));

    let final_month = &rollout.months[2];
    assert_eq!(final_month.mortgages[0].interest_paid_ytd, Money(200_000));
    assert_eq!(final_month.mortgages[0].principal, Money(39_960_180));
    let cash: BTreeMap<_, _> = final_month
        .balances
        .iter()
        .filter(|balance| balance.account.account_id == "checking")
        .map(|balance| (balance.account.agent_id.as_str(), balance.balance))
        .collect();
    assert_eq!(cash["alice"], Money(710_180));
    assert_eq!(cash["seller"], Money(11_000_000));
    assert_eq!(cash["bank"], Money(239_820));
    assert_eq!(cash["county"], Money(50_000));
    assert_eq!(rollout.property_purchases.len(), 1);
    assert_eq!(rollout.mortgage_originations.len(), 1);
    assert_eq!(rollout.mortgage_payments.len(), 1);
    assert!(rollout.journal.iter().all(|entry| {
        entry
            .postings
            .iter()
            .map(|posting| i128::from(posting.amount.0))
            .sum::<i128>()
            == 0
    }));
}

#[test]
fn oversell_is_rejected_before_any_disposition() {
    let alice_cash = AccountRef::new("alice", "checking");
    let fixture = Fixture {
        schema_version: FIXTURE_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 1,
            accounts: vec![AccountSpec {
                account: alice_cash,
                opening_balance: Money(0),
            }],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![],
            recurring_obligations: vec![],
            initial_lots: vec![InitialLotSpec {
                lot_id: "lot-1".into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "vti".into(),
                purchase_month: -1,
                quantity_scale: 1_000_000,
                units: Quantity(1_000_000),
                basis: Money(10_000),
            }],
            initial_bonds: vec![],
            scheduled_sales: vec![ScheduledSaleSpec {
                month: 0,
                cause_id: "oversell".into(),
                agent_id: "alice".into(),
                account_id: "brokerage".into(),
                asset_id: "vti".into(),
                units: Quantity(1_000_001),
                proceeds_account_id: "checking".into(),
            }],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
        },
        series: vec![SeriesSpec {
            series_id: "security:vti".into(),
            snapshots: 2,
            values: vec![10_000, 10_000],
        }],
    };
    assert!(matches!(
        simulate(&fixture),
        Err(SimulationError::InsufficientLotUnits {
            requested: 1_000_001,
            available: 1_000_000,
            ..
        })
    ));
}

#[test]
fn failure_stops_future_actions_and_zeroes_value_state() {
    let alice_cash = AccountRef::new("alice", "checking");
    let bob_cash = AccountRef::new("bob", "checking");
    let fixture = Fixture {
        schema_version: FIXTURE_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 2,
            accounts: vec![
                AccountSpec {
                    account: alice_cash.clone(),
                    opening_balance: Money(100),
                },
                AccountSpec {
                    account: bob_cash.clone(),
                    opening_balance: Money(0),
                },
            ],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![ScheduledTransferSpec {
                month: 1,
                cause_id: "must-not-run".into(),
                from: alice_cash.clone(),
                to: bob_cash.clone(),
                amount: Money(1).into(),
                income_category: None,
                deduction_category: None,
            }],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![ObligationSpec {
                month: 0,
                obligation_id: "too-large".into(),
                obligation_type: "cash_spend".into(),
                from: alice_cash,
                to: bob_cash,
                amount_due: Money(101).into(),
                property_id: None,
                deduction_category: None,
            }],
            recurring_obligations: vec![],
            initial_lots: vec![],
            initial_bonds: vec![],
            scheduled_sales: vec![],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
        },
        series: vec![],
    };
    let rollout = simulate(&fixture).unwrap().rollouts.remove(0);
    assert_eq!(rollout.failed_month, Some(0));
    assert!(!rollout.months[0].failed);
    assert!(rollout.months[1].failed);
    assert!(rollout.months[2].failed);
    assert!(
        rollout.months[1..]
            .iter()
            .flat_map(|month| &month.balances)
            .all(|balance| balance.balance == Money(0))
    );
    assert!(
        rollout
            .journal
            .iter()
            .all(|entry| entry.cause_id != "must-not-run")
    );
}

#[test]
fn same_source_recurring_obligations_settle_all_or_none() {
    let alice_cash = AccountRef::new("alice", "checking");
    let landlord_cash = AccountRef::new("landlord", "checking");
    let utility_cash = AccountRef::new("utility", "checking");
    let fixture = Fixture {
        schema_version: FIXTURE_SCHEMA_VERSION,
        currency_code: "USD".into(),
        currency_quantum: "0.01".into(),
        rollout_count: 1,
        scenario: ScenarioSpec {
            horizon_months: 3,
            accounts: vec![
                AccountSpec {
                    account: alice_cash.clone(),
                    opening_balance: Money(100_000),
                },
                AccountSpec {
                    account: landlord_cash.clone(),
                    opening_balance: Money(0),
                },
                AccountSpec {
                    account: utility_cash.clone(),
                    opening_balance: Money(0),
                },
            ],
            jurisdictions: vec![],
            locations: vec![],
            scheduled_transfers: vec![],
            recurring_transfers: vec![],
            scheduled_property_cashflows: vec![],
            recurring_property_cashflows: vec![],
            obligations: vec![],
            recurring_obligations: vec![
                RecurringObligationSpec {
                    start_month: 0,
                    end_month: Some(2),
                    obligation_id: "rent".into(),
                    obligation_type: "cash_spend".into(),
                    from: alice_cash.clone(),
                    to: landlord_cash,
                    amount_due: Money(60_000).into(),
                    property_id: None,
                    deduction_category: None,
                },
                RecurringObligationSpec {
                    start_month: 1,
                    end_month: Some(2),
                    obligation_id: "utility".into(),
                    obligation_type: "cash_spend".into(),
                    from: alice_cash,
                    to: utility_cash,
                    amount_due: Money(1).into(),
                    property_id: None,
                    deduction_category: None,
                },
            ],
            initial_lots: vec![],
            initial_bonds: vec![],
            scheduled_sales: vec![],
            tax_profiles: vec![],
            distributions: vec![],
            target_allocation_policies: vec![],
            private_equity_tender_policies: vec![],
            harvest_policies: vec![],
            scheduled_property_purchases: vec![],
            initial_primary_residences: vec![],
            primary_residence_events: vec![],
            property_rented_fraction_events: vec![],
            capital_improvement_events: vec![],
            property_sales: vec![],
            mortgage_interest_deduction_policies: vec![],
            property_tax_policies: vec![],
            federal_salt_deduction_policies: vec![],
        },
        series: vec![],
    };
    let rollout = simulate(&fixture).unwrap().rollouts.remove(0);
    assert_eq!(rollout.failed_month, Some(1));
    assert_eq!(
        rollout
            .journal
            .iter()
            .filter(|entry| entry.cause_id.starts_with("rent_m"))
            .map(|entry| entry.cause_id.as_str())
            .collect::<Vec<_>>(),
        vec!["rent_m0"]
    );
    assert!(
        rollout
            .journal
            .iter()
            .all(|entry| entry.cause_id != "utility_m1")
    );
    assert_eq!(rollout.obligations.len(), 3);
    assert_eq!(rollout.obligations[0].amount_paid, Money(60_000));
    assert_eq!(rollout.obligations[0].shortfall, Money(0));
    assert_eq!(rollout.obligations[1].obligation_id, "rent_m1");
    assert_eq!(rollout.obligations[1].amount_paid, Money(0));
    assert_eq!(rollout.obligations[1].shortfall, Money(60_000));
    assert!(rollout.obligations[1].failure_active);
    assert_eq!(rollout.obligations[2].obligation_id, "utility_m1");
    assert_eq!(rollout.obligations[2].amount_paid, Money(0));
    assert_eq!(rollout.obligations[2].shortfall, Money(1));
    assert!(rollout.obligations[2].failure_active);
}
