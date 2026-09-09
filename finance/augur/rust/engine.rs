use std::collections::{BTreeMap, BTreeSet};

use rayon::prelude::*;
use thiserror::Error;

use crate::{
    allocation::{
        AllocationError, deposit_by_sleeve, quantity_for_value, rebalance_by_sleeve,
        withdrawal_by_sleeve,
    },
    execution::{
        AccountBalance, AmountSpec, BondCashflowOutcome, BondSpec, BondState, CapitalGainState,
        CapitalImprovementOutcome, DistributionOutcome, ExecutionInput, HarvestPolicySpec,
        INPUT_SCHEMA_VERSION, IncomeState, InitialLotSpec, LotDisposition, MonthOutput,
        MortgageOriginationOutcome, MortgagePaymentOutcome, MortgageState, ObligationOutcome,
        PopulationOutput, PrimaryResidenceOutcome, PrivateEquityOpportunityOutcome,
        PrivateEquityProtocolOutcome, PropertyPurchaseOutcome, PropertyRentedFractionOutcome,
        PropertySaleOutcome, PropertySaleSpec, PropertyState, RolloutFailureOutcome, RolloutOutput,
        RolloutSummary, SecurityLotState, SeriesSpec, SimulationOutput, TaxAccrual,
        TaxLiabilityState, TaxPaymentOutcome, TaxSettlementOutcome, TransferOutcome,
    },
    holdings::{AgentHoldings, HoldingsError, LotView, cash_balance},
    ledger::{AccountRef, JournalEntry, Ledger, LedgerError, Posting},
    money::{
        ArithmeticError, Factor, Money, PerUnit, PerUnitRate, Quantity, Units, WIRE_RATE_SCALE,
        is_quantity_scale, mul_div_i128_round_half_up, mul_div_round_half_up,
    },
    product::{
        BaseMetrics, ProductError, ProductInputs, ProductMetricSeries, SnapshotState,
        snapshot_metrics,
    },
    tax::{
        IncomeLedger, IncomeSource, JurisdictionLevel, TaxError, TaxFacts, TaxRules, TaxState,
        assess, net_capital_gains, validate_rules,
    },
};

mod accounts;
pub mod allocation;
mod cashflows;
mod errors;
mod obligations;
mod private_equity;
mod property;
mod recorder;
mod securities;
pub mod spending;
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
const MONTHS_PER_YEAR: i64 = 12;
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
    /// Observed snapshots only, empty when no product agent was selected.
    product_metrics: Vec<BaseMetrics>,
    consumption_requested: Vec<Money>,
    consumption_paid: Vec<Money>,
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
pub struct ValidatedInput<'a> {
    input: &'a ExecutionInput,
}

impl<'a> ValidatedInput<'a> {
    pub fn new(fixture: &'a ExecutionInput) -> Result<Self, SimulationError> {
        validate_fixture(fixture)?;
        Ok(Self { input: fixture })
    }
}

pub fn simulate(fixture: &ExecutionInput) -> Result<SimulationOutput, SimulationError> {
    simulate_validated(ValidatedInput::new(fixture)?)
}

pub fn simulate_validated(
    fixture: ValidatedInput<'_>,
) -> Result<SimulationOutput, SimulationError> {
    simulate_with_capture(fixture, CaptureMode::Forensic)
}

/// Run every rollout while retaining dense monthly state and compatibility events.
///
/// Unlike [`simulate`], this omits the balanced journal because the Python
/// compatibility output has no corresponding channel. This is the apples-to-apples dense
/// benchmark and backend handoff path; all canonical event inputs remain present.
pub fn simulate_dense(fixture: &ExecutionInput) -> Result<SimulationOutput, SimulationError> {
    simulate_dense_validated(ValidatedInput::new(fixture)?)
}

pub fn simulate_dense_validated(
    fixture: ValidatedInput<'_>,
) -> Result<SimulationOutput, SimulationError> {
    simulate_with_capture(fixture, CaptureMode::Dense)
}

fn simulate_with_capture(
    fixture: ValidatedInput<'_>,
    capture_mode: CaptureMode,
) -> Result<SimulationOutput, SimulationError> {
    let rollouts: Result<Vec<_>, _> = (0..fixture.input.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            simulate_rollout(fixture.input, rollout_id, capture_mode, None, None, None)
                .map(RolloutComputation::into_output)
        })
        .collect();
    Ok(SimulationOutput {
        schema_version: INPUT_SCHEMA_VERSION,
        rollouts: rollouts?,
    })
}

/// Run every rollout while retaining only fixed-size per-rollout summaries.
///
/// This is the population/benchmark path. It executes the same state machine
/// as [`simulate`] without allocating monthly snapshots, journals, or event
/// traces for every rollout.
pub fn simulate_summaries(fixture: &ExecutionInput) -> Result<PopulationOutput, SimulationError> {
    simulate_summaries_validated(ValidatedInput::new(fixture)?)
}

pub fn simulate_summaries_validated(
    fixture: ValidatedInput<'_>,
) -> Result<PopulationOutput, SimulationError> {
    let rollouts: Result<Vec<_>, _> = (0..fixture.input.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            simulate_rollout(
                fixture.input,
                rollout_id,
                CaptureMode::Summary,
                None,
                None,
                None,
            )
            .map(RolloutComputation::into_summary)
        })
        .collect();
    Ok(PopulationOutput {
        schema_version: INPUT_SCHEMA_VERSION,
        rollouts: rollouts?,
    })
}

/// Run every rollout and retain only the seven base product metric series.
///
/// This is the percentile-fan workload: it allocates no monthly snapshot, journal, or
/// event trace, so a 100,000-rollout population costs `snapshots × rollouts` integers per
/// metric rather than a dense output tree.
pub fn simulate_product_metrics(
    fixture: &ExecutionInput,
    primary_agent_id: &str,
) -> Result<ProductMetricSeries, SimulationError> {
    simulate_product_metrics_validated(ValidatedInput::new(fixture)?, primary_agent_id)
}

pub fn simulate_product_metrics_validated(
    fixture: ValidatedInput<'_>,
    primary_agent_id: &str,
) -> Result<ProductMetricSeries, SimulationError> {
    let inputs = ProductInputs::resolve(fixture.input, primary_agent_id)?;
    let rollouts: Result<Vec<_>, _> = (0..fixture.input.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            simulate_rollout(
                fixture.input,
                rollout_id,
                CaptureMode::Summary,
                Some(&inputs),
                None,
                None,
            )
            .map(|computation| (computation.product_metrics, computation.failed_month))
        })
        .collect();
    Ok(ProductMetricSeries::from_rollouts(
        fixture.input.scenario.horizon_months + 1,
        &rollouts?,
    )?)
}

/// Live books and capture for one trajectory. Prepared paths are shared; mutable financial
/// state, the next event month and the stop boundary belong to this rollout alone.
struct RolloutState<'a> {
    fixture: &'a ExecutionInput,
    rollout_id: u32,
    product: Option<&'a ProductInputs>,
    month: u32,
    ledger: Ledger,
    lots: Vec<LotState>,
    properties: Vec<PropertyState>,
    mortgages: Vec<MortgageState>,
    tax: TaxState,
    tax_liabilities: Vec<TaxLiabilityState>,
    tlh_cumulative_harvest: Vec<Money>,
    target_allocation_buy_count: Vec<Vec<u32>>,
    primary_residence_by_agent: BTreeMap<String, Option<String>>,
    recorder: Recorder,
    failed_month: Option<u32>,
    product_metrics: Vec<BaseMetrics>,
    consumption_requested: Vec<Money>,
    consumption_paid: Vec<Money>,
}

fn simulate_rollout(
    fixture: &ExecutionInput,
    rollout_id: u32,
    capture_mode: CaptureMode,
    product: Option<&ProductInputs>,
    spending: Option<&mut spending::Policy<'_>>,
    allocation: Option<&mut allocation::Policy<'_>>,
) -> Result<RolloutComputation, SimulationError> {
    RolloutState::new(fixture, rollout_id, capture_mode, product)?.run(spending, allocation)
}

impl<'a> RolloutState<'a> {
    fn new(
        fixture: &'a ExecutionInput,
        rollout_id: u32,
        capture_mode: CaptureMode,
        product: Option<&'a ProductInputs>,
    ) -> Result<Self, SimulationError> {
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
        let tax = TaxState {
            income: IncomeLedger::for_taxpayers(
                fixture
                    .scenario
                    .tax_profiles
                    .iter()
                    .map(|profile| profile.agent_id.as_str()),
                &fixture.scenario.income_sources,
            ),
            facts: fixture
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
                .collect(),
        };
        let tlh_cumulative_harvest = vec![Money(0); fixture.scenario.harvest_policies.len()];

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

        let mut lots = Vec::with_capacity(fixture.scenario.initial_lots.len());
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
                units_remaining: spec.units,
                basis_remaining: spec.basis,
            });
        }
        let target_allocation_buy_count: Vec<Vec<u32>> = fixture
            .scenario
            .target_allocation_policies
            .iter()
            .map(|policy| vec![0; policy.sleeves.len()])
            .collect();
        let properties = Vec::<PropertyState>::new();
        let mortgages = Vec::<MortgageState>::new();
        let tax_liabilities = Vec::<TaxLiabilityState>::new();
        let primary_residence_by_agent: BTreeMap<String, Option<String>> = fixture
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
                &tax,
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
        Ok(Self {
            fixture,
            rollout_id,
            product,
            month: 0,
            ledger,
            lots,
            properties,
            mortgages,
            tax,
            tax_liabilities,
            tlh_cumulative_harvest,
            target_allocation_buy_count,
            primary_residence_by_agent,
            recorder,
            failed_month: None,
            product_metrics,
            consumption_requested: Vec::new(),
            consumption_paid: Vec::new(),
        })
    }

    fn run(
        mut self,
        mut spending: Option<&mut spending::Policy<'_>>,
        mut allocation: Option<&mut allocation::Policy<'_>>,
    ) -> Result<RolloutComputation, SimulationError> {
        let fixture = self.fixture;
        let rollout_id = self.rollout_id;
        let product = self.product;
        while self.month < fixture.scenario.horizon_months && self.failed_month.is_none() {
            let month = self.month;
            if let Some(policy) = allocation.as_deref_mut() {
                policy.review(fixture, rollout_id, month, &self.ledger, &self.lots)?;
            }
            // Decide from opening-of-month holdings and current prices, before this month's
            // cashflows. The resulting demand is funded with the other monthly obligations.
            let spending_obligation = if let Some(policy) = spending.as_deref_mut() {
                policy.obligation(fixture, rollout_id, month, &self.ledger, &self.lots)?
            } else {
                None
            };
            execute_primary_residence_events(
                fixture,
                &mut self.recorder,
                &mut self.primary_residence_by_agent,
                month,
            )?;
            execute_property_lifecycle_events(
                fixture,
                rollout_id,
                &mut self.ledger,
                &mut self.recorder,
                &mut self.tax,
                &mut self.properties,
                &mut self.mortgages,
                &mut self.primary_residence_by_agent,
                month,
            )?;
            execute_bonds(
                fixture,
                rollout_id,
                &mut self.ledger,
                &mut self.recorder,
                &mut self.tax,
                month,
            )?;
            execute_distributions(
                fixture,
                rollout_id,
                &mut self.ledger,
                &mut self.recorder,
                &self.lots,
                &mut self.tax,
                month,
            )?;
            execute_property_purchases(
                fixture,
                &mut self.ledger,
                &mut self.recorder,
                &mut self.properties,
                &mut self.mortgages,
                month,
            )?;
            execute_cashflows(
                fixture,
                rollout_id,
                &mut self.ledger,
                &mut self.recorder,
                &mut self.tax,
                &self.properties,
                month,
            )?;
            let mut scheduled_tlh =
                scheduled_tlh_give_back_state(fixture, &self.lots, &self.tlh_cumulative_harvest)?;
            for sale in fixture
                .scenario
                .scheduled_sales
                .iter()
                .filter(|sale| sale.month == month)
            {
                execute_sale(
                    fixture,
                    rollout_id,
                    &mut self.ledger,
                    &mut self.recorder,
                    &mut self.lots,
                    &mut self.tax,
                    &mut scheduled_tlh,
                    sale,
                )?;
            }
            apply_scheduled_tlh_give_back(&scheduled_tlh, &mut self.tlh_cumulative_harvest)?;
            let mut active_obligations = Vec::new();
            // Identify the actual callback demand by its position, never by a user-chosen ID
            // or category that another configured obligation could share.
            let spending_obligation_index = spending_obligation
                .as_ref()
                .map(|_| active_obligations.len());
            let requested = spending_obligation
                .as_ref()
                .map_or(Money(0), |claim| claim.amount_due);
            active_obligations.extend(spending_obligation);
            for obligation in fixture
                .scenario
                .obligations
                .iter()
                .filter(|obligation| obligation.month == month)
            {
                let Some(effect) = configured_obligation_effect(
                    &self.properties,
                    obligation.property_id.as_deref(),
                    obligation.deduction_category.as_deref(),
                    obligation.deductible_fraction_ppb,
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
                    &self.properties,
                    obligation.property_id.as_deref(),
                    obligation.deduction_category.as_deref(),
                    obligation.deductible_fraction_ppb,
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
                &self.properties,
                &self.mortgages,
                month,
            )?);
            active_obligations.extend(tax_obligations(fixture, &self.tax_liabilities, month)?);
            let target_allocation_buys = execute_target_allocation_sales(
                fixture,
                rollout_id,
                &mut self.ledger,
                &mut self.recorder,
                &mut self.lots,
                &mut self.tax,
                &mut self.tlh_cumulative_harvest,
                month,
                &active_obligations,
                allocation.as_deref(),
            )?;
            let settlement = settle_obligations(
                fixture,
                &mut self.ledger,
                &mut self.recorder,
                &mut self.tax,
                &self.properties,
                &mut self.mortgages,
                &mut self.tax_liabilities,
                month,
                &active_obligations,
                product.map(|inputs| inputs.primary_agent_id()),
                spending_obligation_index,
            )?;
            if self.recorder.capture_mode == CaptureMode::Summary && spending.is_some() {
                self.consumption_requested.push(requested);
                self.consumption_paid
                    .push(settlement.spending_paid.unwrap_or(Money(0)));
            }
            if settlement.failed {
                self.failed_month = Some(month);
            } else {
                execute_target_allocation_buys(
                    fixture,
                    &mut self.ledger,
                    &mut self.recorder,
                    &mut self.lots,
                    &mut self.target_allocation_buy_count,
                    month,
                    &target_allocation_buys,
                )?;
                execute_tlh_harvest(
                    fixture,
                    rollout_id,
                    &self.lots,
                    &mut self.tax,
                    &mut self.tlh_cumulative_harvest,
                    month,
                )?;
                execute_private_equity(
                    fixture,
                    rollout_id,
                    &mut self.ledger,
                    &mut self.recorder,
                    &mut self.lots,
                    &mut self.tax,
                    &mut self.tlh_cumulative_harvest,
                    month,
                )?;
            }
            if self.failed_month.is_none() {
                accrue_primary_residence_occupancy(
                    &self.primary_residence_by_agent,
                    &mut self.properties,
                    month,
                )?;
                accrue_property_depreciation(&mut self.tax, &mut self.properties)?;
            }
            if self.failed_month.is_none() && (month + 1) % 12 == 0 {
                accrue_year_end_taxes(
                    fixture,
                    &mut self.ledger,
                    &mut self.recorder,
                    &mut self.tax,
                    &mut self.tax_liabilities,
                    &self.mortgages,
                    month,
                )?;
                reset_property_tax_year_state(&mut self.properties, &mut self.mortgages);
            }
            if self.recorder.capture_mode.captures_output() {
                self.recorder.record_month(month_output(
                    fixture,
                    rollout_id,
                    month + 1,
                    &self.ledger,
                    &self.lots,
                    &self.properties,
                    &self.mortgages,
                    &self.tax_liabilities,
                    &self.tax,
                    &self.tlh_cumulative_harvest,
                    self.failed_month.is_some(),
                )?);
            }
            if let Some(inputs) = product {
                self.product_metrics.push(product_snapshot(
                    fixture,
                    inputs,
                    rollout_id,
                    month + 1,
                    &self.ledger,
                    &self.lots,
                    &self.properties,
                    &self.mortgages,
                    settlement.product_shortfall,
                    self.failed_month.is_some(),
                )?);
            }
            self.month += 1;
        }
        debug_assert_eq!(self.ledger.trial_balance(), 0);
        Ok(RolloutComputation {
            rollout_id,
            ending_balances: account_balances(&self.ledger),
            ending_bonds: bond_states(
                fixture,
                rollout_id,
                self.failed_month.unwrap_or(self.month),
            )?,
            ending_properties: self.properties,
            ending_mortgages: self.mortgages,
            ending_tax_liabilities: self.tax_liabilities,
            ending_tlh_cumulative_harvest: self.tlh_cumulative_harvest,
            recorder: self.recorder,
            failed_month: self.failed_month,
            product_metrics: self.product_metrics,
            consumption_requested: self.consumption_requested,
            consumption_paid: self.consumption_paid,
        })
    }
}

/// Reduce the live rollout state to one snapshot's base product metrics.
///
/// A stopped post-event book uses the failure month's observed marks, not future prices.
#[allow(clippy::too_many_arguments)]
fn product_snapshot(
    fixture: &ExecutionInput,
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
    let lot_views: Vec<_> = lots.iter().map(LotState::view).collect();
    let valuation_month = if failed { snapshot - 1 } else { snapshot };
    let bonds = bond_states(fixture, rollout_id, valuation_month)?;
    let state = SnapshotState {
        ledger,
        lots: &lot_views,
        properties,
        mortgages,
        bonds: &bonds,
        shortfall,
    };
    Ok(snapshot_metrics(
        fixture,
        inputs,
        &state,
        rollout_id,
        valuation_month,
    )?)
}
