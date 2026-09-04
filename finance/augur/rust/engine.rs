use std::collections::{BTreeMap, BTreeSet};

use rayon::prelude::*;
use thiserror::Error;

use crate::{
    allocation::{
        AllocationError, deposit_by_sleeve, quantity_for_value, rebalance_by_sleeve,
        withdrawal_by_sleeve,
    },
    fixture::{
        AccountBalance, AmountSpec, BondCashflowOutcome, BondSpec, BondState, CapitalGainState,
        CapitalImprovementOutcome, DistributionOutcome, FIXTURE_SCHEMA_VERSION, Fixture,
        HarvestPolicySpec, InitialLotSpec, LotDisposition, MonthOutput, MortgageOriginationOutcome,
        MortgagePaymentOutcome, MortgageState, ObligationOutcome, PopulationOutput,
        PrimaryResidenceOutcome, PrivateEquityOpportunityOutcome, PrivateEquityProtocolOutcome,
        PropertyPurchaseOutcome, PropertyRentedFractionOutcome, PropertySaleOutcome,
        PropertySaleSpec, PropertyState, RolloutFailureOutcome, RolloutOutput, RolloutSummary,
        SecurityLotState, SeriesSpec, SimulationOutput, TaxAccrual, TaxLiabilityState,
        TaxPaymentOutcome, TaxSettlementOutcome, TransferOutcome,
    },
    ledger::{AccountRef, JournalEntry, Ledger, LedgerError, Posting},
    money::{ArithmeticError, Money, Quantity, mul_div_i128_round_half_up, mul_div_round_half_up},
    product::{
        BaseMetrics, LotView, ProductError, ProductInputs, ProductMetricSeries, SnapshotState,
        snapshot_metrics,
    },
    tax::{
        JurisdictionLevel, RATE_SCALE, TaxError, TaxFacts, assess, net_capital_gains,
        validate_rules,
    },
};

mod accounts;
mod cashflows;
mod errors;
mod obligations;
mod private_equity;
mod property;
mod recorder;
mod securities;
mod target_allocation;
mod taxes;
#[cfg(test)]
mod tests;
mod tlh;
mod validation;

pub use errors::SimulationError;

use accounts::*;
use cashflows::*;
use obligations::*;
use private_equity::*;
use property::*;
use recorder::*;
use securities::*;
use target_allocation::*;
use taxes::*;
use tlh::*;
use validation::*;

const EXTERNAL_AGENT: &str = "__external__";
const OPENING_EQUITY: &str = "equity:opening";
const RATE_SCALE_PPB: i64 = 1_000_000_000;
const MONTHS_PER_YEAR: i64 = 12;
const INDEX_LEVEL_SCALE: i64 = 1_000_000_000;
const MAX_EXACT_F64_INTEGER: i64 = 1_i64 << 53;
const CONTRACT_SCALE: i128 = 1_000_000_000_000_000_000;
const SECTION_121_LOOKBACK_MONTHS: usize = 60;
const SECTION_121_MIN_QUALIFYING_MONTHS: usize = 24;
const PE_ASSET_PREFIX: &str = "private_equity:";

#[derive(Debug)]
struct RolloutComputation {
    rollout_id: u32,
    ending_balances: Vec<AccountBalance>,
    ending_bonds: Vec<BondState>,
    ending_properties: Vec<PropertyState>,
    ending_mortgages: Vec<MortgageState>,
    ending_tax_liabilities: Vec<TaxLiabilityState>,
    ending_tlh_cumulative_harvest: Vec<Money>,
    recorder: Recorder,
    failed_month: Option<u32>,
    /// One row per snapshot (`horizon_months + 1`), empty when no product agent was selected.
    product_metrics: Vec<BaseMetrics>,
}

impl RolloutComputation {
    fn into_output(self) -> RolloutOutput {
        RolloutOutput {
            rollout_id: self.rollout_id,
            months: self.recorder.months,
            journal: self.recorder.journal,
            transfers: self.recorder.transfers,
            dispositions: self.recorder.dispositions,
            private_equity_events: self.recorder.private_equity_events,
            private_equity_opportunities: self.recorder.private_equity_opportunities,
            obligations: self.recorder.obligations,
            rollout_failures: self.recorder.rollout_failures,
            tax_accruals: self.recorder.tax_accruals,
            tax_payments: self.recorder.tax_payments,
            tax_settlements: self.recorder.tax_settlements,
            bond_cashflows: self.recorder.bond_cashflows,
            distributions: self.recorder.distributions,
            property_purchases: self.recorder.property_purchases,
            primary_residence_events: self.recorder.primary_residence_events,
            property_rented_fraction_events: self.recorder.property_rented_fraction_events,
            capital_improvements: self.recorder.capital_improvements,
            property_sales: self.recorder.property_sales,
            mortgage_originations: self.recorder.mortgage_originations,
            mortgage_payments: self.recorder.mortgage_payments,
            failed_month: self.failed_month,
        }
    }

    fn into_summary(self) -> RolloutSummary {
        RolloutSummary {
            rollout_id: self.rollout_id,
            ending_balances: self.ending_balances,
            ending_bonds: self.ending_bonds,
            ending_properties: self.ending_properties,
            ending_mortgages: self.ending_mortgages,
            ending_tax_liabilities: self.ending_tax_liabilities,
            ending_tlh_cumulative_harvest: self.ending_tlh_cumulative_harvest,
            journal_entry_count: self.recorder.journal_entry_count,
            disposition_count: self.recorder.disposition_count,
            private_equity_event_count: self.recorder.private_equity_event_count,
            private_equity_opportunity_count: self.recorder.private_equity_opportunity_count,
            tax_accrual_count: self.recorder.tax_accrual_count,
            tax_payment_count: self.recorder.tax_payment_count,
            tax_settlement_count: self.recorder.tax_settlement_count,
            bond_cashflow_count: self.recorder.bond_cashflow_count,
            distribution_count: self.recorder.distribution_count,
            property_purchase_count: self.recorder.property_purchase_count,
            primary_residence_event_count: self.recorder.primary_residence_event_count,
            property_rented_fraction_event_count: self
                .recorder
                .property_rented_fraction_event_count,
            capital_improvement_count: self.recorder.capital_improvement_count,
            property_sale_count: self.recorder.property_sale_count,
            mortgage_payment_count: self.recorder.mortgage_payment_count,
            failed_month: self.failed_month,
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct ValidatedFixture<'a> {
    fixture: &'a Fixture,
}

impl<'a> ValidatedFixture<'a> {
    pub fn new(fixture: &'a Fixture) -> Result<Self, SimulationError> {
        validate_fixture(fixture)?;
        Ok(Self { fixture })
    }
}

pub fn simulate(fixture: &Fixture) -> Result<SimulationOutput, SimulationError> {
    simulate_validated(ValidatedFixture::new(fixture)?)
}

pub fn simulate_validated(
    fixture: ValidatedFixture<'_>,
) -> Result<SimulationOutput, SimulationError> {
    simulate_with_capture(fixture, CaptureMode::Forensic)
}

/// Run every rollout while retaining dense monthly state and compatibility events.
///
/// Unlike [`simulate`], this omits the Rust-only balanced journal because the Python/JAX
/// compatibility output has no corresponding channel. This is the apples-to-apples dense
/// benchmark and backend handoff path; all canonical event inputs remain present.
pub fn simulate_dense(fixture: &Fixture) -> Result<SimulationOutput, SimulationError> {
    simulate_dense_validated(ValidatedFixture::new(fixture)?)
}

pub fn simulate_dense_validated(
    fixture: ValidatedFixture<'_>,
) -> Result<SimulationOutput, SimulationError> {
    simulate_with_capture(fixture, CaptureMode::Dense)
}

fn simulate_with_capture(
    fixture: ValidatedFixture<'_>,
    capture_mode: CaptureMode,
) -> Result<SimulationOutput, SimulationError> {
    let rollouts: Result<Vec<_>, _> = (0..fixture.fixture.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            simulate_rollout(fixture.fixture, rollout_id, capture_mode, None)
                .map(RolloutComputation::into_output)
        })
        .collect();
    Ok(SimulationOutput {
        schema_version: FIXTURE_SCHEMA_VERSION,
        rollouts: rollouts?,
    })
}

/// Run every rollout while retaining only fixed-size per-rollout summaries.
///
/// This is the population/benchmark path. It executes the same state machine
/// as [`simulate`] without allocating monthly snapshots, journals, or event
/// traces for every rollout.
pub fn simulate_summaries(fixture: &Fixture) -> Result<PopulationOutput, SimulationError> {
    simulate_summaries_validated(ValidatedFixture::new(fixture)?)
}

pub fn simulate_summaries_validated(
    fixture: ValidatedFixture<'_>,
) -> Result<PopulationOutput, SimulationError> {
    let rollouts: Result<Vec<_>, _> = (0..fixture.fixture.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            simulate_rollout(fixture.fixture, rollout_id, CaptureMode::Summary, None)
                .map(RolloutComputation::into_summary)
        })
        .collect();
    Ok(PopulationOutput {
        schema_version: FIXTURE_SCHEMA_VERSION,
        rollouts: rollouts?,
    })
}

/// Run every rollout and retain only the seven base product metric series.
///
/// This is the percentile-fan workload: it allocates no monthly snapshot, journal, or
/// event trace, so a 100,000-rollout population costs `snapshots × rollouts` integers per
/// metric rather than a dense output tree.
pub fn simulate_product_metrics(
    fixture: &Fixture,
    primary_agent_id: &str,
) -> Result<ProductMetricSeries, SimulationError> {
    simulate_product_metrics_validated(ValidatedFixture::new(fixture)?, primary_agent_id)
}

pub fn simulate_product_metrics_validated(
    fixture: ValidatedFixture<'_>,
    primary_agent_id: &str,
) -> Result<ProductMetricSeries, SimulationError> {
    let inputs = ProductInputs::resolve(fixture.fixture, primary_agent_id)?;
    let rollouts: Result<Vec<_>, _> = (0..fixture.fixture.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            simulate_rollout(
                fixture.fixture,
                rollout_id,
                CaptureMode::Summary,
                Some(&inputs),
            )
            .map(|computation| (computation.product_metrics, computation.failed_month))
        })
        .collect();
    Ok(ProductMetricSeries::from_rollouts(
        fixture.fixture.scenario.horizon_months + 1,
        &rollouts?,
    )?)
}

fn simulate_rollout(
    fixture: &Fixture,
    rollout_id: u32,
    capture_mode: CaptureMode,
    product: Option<&ProductInputs>,
) -> Result<RolloutComputation, SimulationError> {
    let mut accounts: Vec<AccountRef> = fixture
        .scenario
        .accounts
        .iter()
        .map(|spec| spec.account.clone())
        .collect();
    for spec in &fixture.scenario.accounts {
        accounts.push(AccountRef::new(&spec.account.agent_id, OPENING_EQUITY));
    }
    accounts.push(AccountRef::new(EXTERNAL_AGENT, "boundary"));
    for lot in &fixture.scenario.initial_lots {
        accounts.push(asset_basis_account(lot));
        accounts.push(realized_gain_account(&lot.agent_id));
        accounts.push(AccountRef::new(&lot.agent_id, OPENING_EQUITY));
    }
    for policy in &fixture.scenario.target_allocation_policies {
        let account_id = policy
            .source_account_ids
            .first()
            .unwrap_or(&policy.account_id);
        accounts.push(realized_gain_account(&policy.agent_id));
        for sleeve in &policy.sleeves {
            accounts.push(asset_basis_account(&InitialLotSpec {
                lot_id: String::new(),
                agent_id: policy.agent_id.clone(),
                account_id: account_id.clone(),
                asset_id: sleeve.asset_id.clone(),
                purchase_month: 0,
                quantity_scale: sleeve.quantity_scale,
                units: Quantity(0),
                basis: Money(0),
            }));
        }
    }
    for profile in &fixture.scenario.tax_profiles {
        accounts.push(tax_prepayment_account(&profile.agent_id));
        accounts.push(tax_authority_revenue_account(
            &profile.tax_authority_agent_id,
        ));
        for rules in &profile.jurisdictions {
            accounts.push(tax_expense_account(
                &profile.agent_id,
                &rules.jurisdiction_id,
            ));
            accounts.push(tax_liability_account(
                &profile.agent_id,
                &rules.jurisdiction_id,
            ));
        }
    }
    for purchase in &fixture.scenario.scheduled_property_purchases {
        accounts.push(property_asset_account(
            &purchase.buyer_agent_id,
            &purchase.property_id,
        ));
        accounts.push(realized_gain_account(&purchase.buyer_agent_id));
        accounts.push(property_basis_writeoff_account(
            &purchase.buyer_agent_id,
            &purchase.property_id,
        ));
        accounts.push(property_sale_clearing_account(
            &purchase.seller_agent_id,
            &purchase.property_id,
        ));
        if let Some(mortgage) = &purchase.mortgage {
            accounts.push(mortgage_liability_account(
                &purchase.buyer_agent_id,
                &mortgage.liability_id,
            ));
            accounts.push(mortgage_interest_expense_account(
                &purchase.buyer_agent_id,
                &mortgage.liability_id,
            ));
            accounts.push(mortgage_receivable_account(
                &mortgage.lender_agent_id,
                &mortgage.liability_id,
            ));
            accounts.push(mortgage_interest_income_account(
                &mortgage.lender_agent_id,
                &mortgage.liability_id,
            ));
            accounts.push(mortgage_funding_account(
                &mortgage.lender_agent_id,
                &mortgage.liability_id,
            ));
        }
    }
    accounts.sort();
    accounts.dedup();
    let mut ledger = Ledger::with_accounts(accounts);
    let mut recorder = Recorder::new(capture_mode);
    let mut tax_facts: BTreeMap<(String, String), TaxFacts> = fixture
        .scenario
        .tax_profiles
        .iter()
        .flat_map(|profile| {
            profile.jurisdictions.iter().map(|rules| {
                (
                    (profile.agent_id.clone(), rules.jurisdiction_id.clone()),
                    TaxFacts::default(),
                )
            })
        })
        .collect();
    let mut tlh_cumulative_harvest = vec![Money(0); fixture.scenario.harvest_policies.len()];

    for spec in &fixture.scenario.accounts {
        if spec.opening_balance != Money(0) {
            recorder.apply_entry(
                &mut ledger,
                JournalEntry {
                    month: 0,
                    cause_id: format!(
                        "opening:{}:{}",
                        spec.account.agent_id, spec.account.account_id
                    ),
                    postings: vec![
                        Posting {
                            account: spec.account.clone(),
                            amount: spec.opening_balance,
                        },
                        Posting {
                            account: AccountRef::new(&spec.account.agent_id, OPENING_EQUITY),
                            amount: spec.opening_balance.checked_neg()?,
                        },
                    ],
                },
            )?;
        }
    }

    let purchase_slot_count = fixture
        .scenario
        .target_allocation_policies
        .iter()
        .try_fold(0_usize, |total, policy| {
            let slots = usize::try_from(policy.purchase_slots_per_sleeve).map_err(|_| {
                ArithmeticError::Overflow {
                    operation: "target-allocation purchase slot count",
                }
            })?;
            let policy_slots =
                policy
                    .sleeves
                    .len()
                    .checked_mul(slots)
                    .ok_or(ArithmeticError::Overflow {
                        operation: "target-allocation purchase slot count",
                    })?;
            total
                .checked_add(policy_slots)
                .ok_or(ArithmeticError::Overflow {
                    operation: "target-allocation purchase slot count",
                })
        })?;
    let mut lots = Vec::with_capacity(fixture.scenario.initial_lots.len() + purchase_slot_count);
    for spec in &fixture.scenario.initial_lots {
        if spec.basis != Money(0) {
            recorder.apply_entry(
                &mut ledger,
                JournalEntry {
                    month: 0,
                    cause_id: format!("opening-lot:{}", spec.lot_id),
                    postings: vec![
                        Posting {
                            account: asset_basis_account(spec),
                            amount: spec.basis,
                        },
                        Posting {
                            account: AccountRef::new(&spec.agent_id, OPENING_EQUITY),
                            amount: spec.basis.checked_neg()?,
                        },
                    ],
                },
            )?;
        }
        lots.push(LotState {
            spec: spec.clone(),
            fifo_rank: i64::from(spec.purchase_month),
            units_remaining: spec.units,
            basis_remaining: spec.basis,
            basis_per_unit: Money(
                i64::try_from(
                    i128::from(spec.basis.0) * i128::from(spec.quantity_scale)
                        / i128::from(spec.units.0),
                )
                .map_err(|_| ArithmeticError::Overflow {
                    operation: "initial lot per-unit basis",
                })?,
            ),
        });
    }
    let mut purchase_slot_rank = i64::from(fixture.scenario.horizon_months)
        .checked_add(
            i64::try_from(fixture.scenario.initial_lots.len()).map_err(|_| {
                ArithmeticError::Overflow {
                    operation: "target-allocation purchase slot rank",
                }
            })?,
        )
        .ok_or(ArithmeticError::Overflow {
            operation: "target-allocation purchase slot rank",
        })?;
    for (policy_index, policy) in fixture
        .scenario
        .target_allocation_policies
        .iter()
        .enumerate()
    {
        let account_id = policy
            .source_account_ids
            .first()
            .unwrap_or(&policy.account_id);
        for (sleeve_index, sleeve) in policy.sleeves.iter().enumerate() {
            for slot_index in 0..policy.purchase_slots_per_sleeve {
                lots.push(LotState {
                    spec: InitialLotSpec {
                        lot_id: format!(
                            "{}_buy_p{policy_index}_s{sleeve_index}_{slot_index}",
                            policy.cause_id_prefix
                        ),
                        agent_id: policy.agent_id.clone(),
                        account_id: account_id.clone(),
                        asset_id: sleeve.asset_id.clone(),
                        purchase_month: 0,
                        quantity_scale: sleeve.quantity_scale,
                        units: Quantity(0),
                        basis: Money(0),
                    },
                    fifo_rank: purchase_slot_rank,
                    units_remaining: Quantity(0),
                    basis_remaining: Money(0),
                    basis_per_unit: Money(0),
                });
                purchase_slot_rank =
                    purchase_slot_rank
                        .checked_add(1)
                        .ok_or(ArithmeticError::Overflow {
                            operation: "target-allocation purchase slot rank",
                        })?;
            }
        }
    }
    let mut target_allocation_buy_count: Vec<Vec<u32>> = fixture
        .scenario
        .target_allocation_policies
        .iter()
        .map(|policy| vec![0; policy.sleeves.len()])
        .collect();
    let mut properties = Vec::<PropertyState>::new();
    let mut mortgages = Vec::<MortgageState>::new();
    let mut tax_liabilities = Vec::<TaxLiabilityState>::new();
    let mut primary_residence_by_agent: BTreeMap<String, Option<String>> = fixture
        .scenario
        .initial_primary_residences
        .iter()
        .map(|assignment| {
            (
                assignment.agent_id.clone(),
                Some(assignment.property_id.clone()),
            )
        })
        .collect();

    let mut failed_month = None;
    let mut product_metrics = Vec::new();
    if recorder.capture_mode.captures_output() {
        recorder.record_month(month_output(
            fixture,
            rollout_id,
            0,
            &ledger,
            &lots,
            &properties,
            &mortgages,
            &tax_liabilities,
            &tax_facts,
            &tlh_cumulative_harvest,
            false,
        )?);
    }
    if let Some(inputs) = product {
        product_metrics.push(product_snapshot(
            fixture,
            inputs,
            rollout_id,
            0,
            &ledger,
            &lots,
            &properties,
            &mortgages,
            Money(0),
            false,
        )?);
    }
    for month in 0..fixture.scenario.horizon_months {
        if failed_month.is_some() {
            if recorder.capture_mode.captures_output() {
                recorder.record_month(month_output(
                    fixture,
                    rollout_id,
                    month + 1,
                    &ledger,
                    &lots,
                    &properties,
                    &mortgages,
                    &tax_liabilities,
                    &tax_facts,
                    &tlh_cumulative_harvest,
                    true,
                )?);
            }
            if let Some(inputs) = product {
                product_metrics.push(product_snapshot(
                    fixture,
                    inputs,
                    rollout_id,
                    month + 1,
                    &ledger,
                    &lots,
                    &properties,
                    &mortgages,
                    Money(0),
                    true,
                )?);
            }
            continue;
        }
        execute_primary_residence_events(
            fixture,
            &mut recorder,
            &mut primary_residence_by_agent,
            month,
        )?;
        execute_property_lifecycle_events(
            fixture,
            rollout_id,
            &mut ledger,
            &mut recorder,
            &mut tax_facts,
            &mut properties,
            &mut mortgages,
            &mut primary_residence_by_agent,
            month,
        )?;
        execute_bonds(
            fixture,
            rollout_id,
            &mut ledger,
            &mut recorder,
            &mut tax_facts,
            month,
        )?;
        execute_distributions(
            fixture,
            rollout_id,
            &mut ledger,
            &mut recorder,
            &lots,
            &mut tax_facts,
            month,
        )?;
        execute_property_purchases(
            fixture,
            &mut ledger,
            &mut recorder,
            &mut properties,
            &mut mortgages,
            month,
        )?;
        execute_cashflows(
            fixture,
            rollout_id,
            &mut ledger,
            &mut recorder,
            &mut tax_facts,
            &properties,
            month,
        )?;
        let mut scheduled_tlh =
            scheduled_tlh_give_back_state(fixture, &lots, &tlh_cumulative_harvest)?;
        for sale in fixture
            .scenario
            .scheduled_sales
            .iter()
            .filter(|sale| sale.month == month)
        {
            execute_sale(
                fixture,
                rollout_id,
                &mut ledger,
                &mut recorder,
                &mut lots,
                &mut tax_facts,
                &mut scheduled_tlh,
                sale,
            )?;
        }
        apply_scheduled_tlh_give_back(&scheduled_tlh, &mut tlh_cumulative_harvest)?;
        let mut active_obligations = Vec::new();
        for obligation in fixture
            .scenario
            .obligations
            .iter()
            .filter(|obligation| obligation.month == month)
        {
            let Some(effect) = configured_obligation_effect(
                &properties,
                obligation.property_id.as_deref(),
                obligation.deduction_category.as_deref(),
            ) else {
                continue;
            };
            active_obligations.push(ActiveObligation {
                cause_id: format!("{}_m{month}", obligation.obligation_id),
                obligation_type: obligation.obligation_type.clone(),
                from: obligation.from.clone(),
                to: obligation.to.clone(),
                amount_due: amount_value(fixture, rollout_id, month, &obligation.amount_due)?,
                effect,
            });
        }
        for obligation in fixture
            .scenario
            .recurring_obligations
            .iter()
            .filter(|obligation| {
                obligation.start_month <= month
                    && obligation.end_month.is_none_or(|end| month <= end)
            })
        {
            let Some(effect) = configured_obligation_effect(
                &properties,
                obligation.property_id.as_deref(),
                obligation.deduction_category.as_deref(),
            ) else {
                continue;
            };
            active_obligations.push(ActiveObligation {
                cause_id: format!("{}_m{month}", obligation.obligation_id),
                obligation_type: obligation.obligation_type.clone(),
                from: obligation.from.clone(),
                to: obligation.to.clone(),
                amount_due: amount_value(fixture, rollout_id, month, &obligation.amount_due)?,
                effect,
            });
        }
        active_obligations.extend(property_obligations(
            fixture,
            &properties,
            &mortgages,
            month,
        )?);
        active_obligations.extend(tax_obligations(fixture, &tax_liabilities, month)?);
        let target_allocation_buys = execute_target_allocation_sales(
            fixture,
            rollout_id,
            &mut ledger,
            &mut recorder,
            &mut lots,
            &mut tax_facts,
            &mut tlh_cumulative_harvest,
            month,
            &active_obligations,
        )?;
        let (settlement_failed, product_shortfall) = settle_obligations(
            fixture,
            &mut ledger,
            &mut recorder,
            &mut tax_facts,
            &properties,
            &mut mortgages,
            &mut tax_liabilities,
            month,
            &active_obligations,
            product.map(|inputs| inputs.primary_agent_id()),
        )?;
        if settlement_failed {
            failed_month = Some(month);
        } else {
            execute_target_allocation_buys(
                fixture,
                &mut ledger,
                &mut recorder,
                &mut lots,
                &mut target_allocation_buy_count,
                month,
                &target_allocation_buys,
            )?;
            execute_tlh_harvest(
                fixture,
                rollout_id,
                &lots,
                &mut tax_facts,
                &mut tlh_cumulative_harvest,
                month,
            )?;
            execute_private_equity(
                fixture,
                rollout_id,
                &mut ledger,
                &mut recorder,
                &mut lots,
                &mut tax_facts,
                &mut tlh_cumulative_harvest,
                month,
            )?;
        }
        if failed_month.is_none() {
            accrue_primary_residence_occupancy(
                &primary_residence_by_agent,
                &mut properties,
                month,
            )?;
            accrue_property_depreciation(&mut tax_facts, &mut properties)?;
        }
        if failed_month.is_none() && (month + 1) % 12 == 0 {
            accrue_year_end_taxes(
                fixture,
                &mut ledger,
                &mut recorder,
                &mut tax_facts,
                &mut tax_liabilities,
                &mortgages,
                month,
            )?;
            reset_property_tax_year_state(&mut properties, &mut mortgages);
        }
        if recorder.capture_mode.captures_output() {
            recorder.record_month(month_output(
                fixture,
                rollout_id,
                month + 1,
                &ledger,
                &lots,
                &properties,
                &mortgages,
                &tax_liabilities,
                &tax_facts,
                &tlh_cumulative_harvest,
                failed_month.is_some(),
            )?);
        }
        if let Some(inputs) = product {
            product_metrics.push(product_snapshot(
                fixture,
                inputs,
                rollout_id,
                month + 1,
                &ledger,
                &lots,
                &properties,
                &mortgages,
                product_shortfall,
                failed_month.is_some(),
            )?);
        }
    }
    debug_assert_eq!(ledger.trial_balance(), 0);
    Ok(RolloutComputation {
        rollout_id,
        ending_balances: account_balances(&ledger, failed_month.is_some()),
        ending_bonds: bond_states(
            fixture,
            rollout_id,
            fixture.scenario.horizon_months,
            failed_month.is_some(),
        )?,
        ending_properties: property_states(&properties, failed_month.is_some()),
        ending_mortgages: mortgage_states(&mortgages, failed_month.is_some()),
        ending_tax_liabilities: tax_liability_states(&tax_liabilities, failed_month.is_some()),
        ending_tlh_cumulative_harvest: if failed_month.is_some() {
            vec![Money(0); tlh_cumulative_harvest.len()]
        } else {
            tlh_cumulative_harvest
        },
        recorder,
        failed_month,
        product_metrics,
    })
}

/// Reduce the live rollout state to one snapshot's base product metrics.
///
/// Dollar state is zeroed for a frozen rollout by the same `failed` flag the dense
/// snapshot serializers use, so the two output channels never disagree about a failure.
#[allow(clippy::too_many_arguments)]
fn product_snapshot(
    fixture: &Fixture,
    inputs: &ProductInputs,
    rollout_id: u32,
    snapshot: u32,
    ledger: &Ledger,
    lots: &[LotState],
    properties: &[PropertyState],
    mortgages: &[MortgageState],
    shortfall: Money,
    failed: bool,
) -> Result<BaseMetrics, SimulationError> {
    let lot_views: Vec<LotView<'_>> = lots
        .iter()
        .map(|lot| LotView {
            agent_id: &lot.spec.agent_id,
            asset_id: &lot.spec.asset_id,
            units_remaining: if failed { 0 } else { lot.units_remaining.0 },
            quantity_scale: lot.spec.quantity_scale,
        })
        .collect();
    let bonds = bond_states(fixture, rollout_id, snapshot, failed)?;
    let mortgage_states = mortgage_states(mortgages, failed);
    let empty_ledger = Ledger::default();
    let state = SnapshotState {
        ledger: if failed { &empty_ledger } else { ledger },
        lots: &lot_views,
        properties,
        mortgages: &mortgage_states,
        bonds: &bonds,
        shortfall,
        failed,
    };
    Ok(snapshot_metrics(
        fixture, inputs, &state, rollout_id, snapshot,
    )?)
}
