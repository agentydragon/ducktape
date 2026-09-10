use std::collections::{BTreeMap, BTreeSet};

#[cfg(test)]
use rayon::prelude::*;
use thiserror::Error;

use crate::{
    execution::{
        AccountBalance, AmountSpec, BondCashflowOutcome, BondCoupon, BondSpec, BondState,
        CapitalGainState, CapitalImprovementOutcome, DistributionOutcome, ExecutionInput,
        INPUT_SCHEMA_VERSION, IncomeState, InitialLotSpec, LotDisposition, MonthOutput,
        MortgageOriginationOutcome, MortgagePaymentOutcome, MortgageState, ObligationOutcome,
        PrimaryResidenceOutcome, PrivateEquityOpportunityOutcome, PrivateEquityProtocolOutcome,
        PropertyPurchaseOutcome, PropertyRentedFractionOutcome, PropertySaleOutcome,
        PropertySaleSpec, PropertyState, RolloutFailureOutcome, RolloutOutput, RolloutSummary,
        SecurityLotState, SeriesSpec, TaxAccrual, TaxLiabilityState, TaxPaymentOutcome,
        TaxSettlementOutcome, TlhFinancialEffect, TlhPortfolioObservation, TransferOutcome,
    },
    holdings::{AgentHoldings, HoldingsError, LotView},
    ledger::{AccountRef, JournalEntry, Ledger, LedgerError, Posting},
    money::{
        ArithmeticError, Factor, Money, PerUnit, PerUnitRate, Quantity, Units, WIRE_RATE_SCALE,
        is_quantity_scale, mul_div_i128_round_half_up, mul_div_round_half_up,
    },
    product::{BaseMetrics, ProductError, ProductInputs, SnapshotState, snapshot_metrics},
    tax::{
        IncomeLedger, IncomeSource, JurisdictionLevel, TaxError, TaxFacts, TaxRules, TaxState,
        assess, net_capital_gains, validate_rules,
    },
};

#[cfg(test)]
use crate::{
    execution::{PopulationOutput, SimulationOutput},
    product::ProductMetricSeries,
};

mod accounts;
pub mod actors;
mod cashflows;
pub mod claims;
pub mod components;
mod errors;
mod obligations;
pub mod observations;
pub mod payments;
#[cfg(test)]
mod payments_test;
mod private_equity;
mod property;
mod recorder;
mod securities;
mod taxes;
#[cfg(test)]
mod tests;
pub mod trades;
pub mod transfers;
mod validation;
pub mod world;

pub use errors::SimulationError;
pub use recorder::CaptureMode;

use accounts::*;
use cashflows::*;
use claims::*;
use obligations::*;
use private_equity::*;
use property::*;
use recorder::*;
use securities::*;
use taxes::*;

use trades::*;
use transfers::*;
use validation::*;

const EXTERNAL_AGENT: &str = "__external__";
const OPENING_EQUITY: &str = "equity:opening";
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
    ending_tlh_portfolios: Vec<TlhPortfolioObservation>,
    recorder: Recorder,
    failed_month: Option<u32>,
    /// Observed snapshots only, empty when no product agent was selected.
    #[cfg(test)]
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
            tlh_financial_effects: self.recorder.tlh_financial_effects,
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
            ending_tlh_portfolios: self.ending_tlh_portfolios,
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

#[cfg(test)]
#[derive(Clone, Copy, Debug)]
pub struct ValidatedInput<'a> {
    input: &'a ExecutionInput,
}

#[cfg(test)]
impl<'a> ValidatedInput<'a> {
    pub fn new(fixture: &'a ExecutionInput) -> Result<Self, SimulationError> {
        validate_fixture(fixture)?;
        Ok(Self { input: fixture })
    }
}

#[cfg(test)]
pub fn simulate(fixture: &ExecutionInput) -> Result<SimulationOutput, SimulationError> {
    simulate_validated(ValidatedInput::new(fixture)?)
}

#[cfg(test)]
pub fn simulate_validated(
    fixture: ValidatedInput<'_>,
) -> Result<SimulationOutput, SimulationError> {
    simulate_with_capture(fixture, CaptureMode::Forensic)
}

/// Run every rollout while retaining dense monthly state and compatibility events.
///
/// Unlike [`simulate`], this omits the balanced journal because the Python
/// compatibility output has no corresponding channel. All canonical event inputs remain present.
#[cfg(test)]
pub fn simulate_dense(fixture: &ExecutionInput) -> Result<SimulationOutput, SimulationError> {
    simulate_dense_validated(ValidatedInput::new(fixture)?)
}

#[cfg(test)]
pub fn simulate_dense_validated(
    fixture: ValidatedInput<'_>,
) -> Result<SimulationOutput, SimulationError> {
    simulate_with_capture(fixture, CaptureMode::Dense)
}

#[cfg(test)]
fn simulate_with_capture(
    fixture: ValidatedInput<'_>,
    capture_mode: CaptureMode,
) -> Result<SimulationOutput, SimulationError> {
    let rollouts: Result<Vec<_>, _> = (0..fixture.input.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            simulate_rollout(fixture.input, rollout_id, capture_mode, None)
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
/// This executes the same state machine
/// as [`simulate`] without allocating monthly snapshots, journals, or event
/// traces for every rollout.
#[cfg(test)]
pub fn simulate_summaries(fixture: &ExecutionInput) -> Result<PopulationOutput, SimulationError> {
    simulate_summaries_validated(ValidatedInput::new(fixture)?)
}

#[cfg(test)]
pub fn simulate_summaries_validated(
    fixture: ValidatedInput<'_>,
) -> Result<PopulationOutput, SimulationError> {
    let rollouts: Result<Vec<_>, _> = (0..fixture.input.rollout_count)
        .into_par_iter()
        .map(|rollout_id| {
            simulate_rollout(fixture.input, rollout_id, CaptureMode::Summary, None)
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
#[cfg(test)]
pub fn simulate_product_metrics(
    fixture: &ExecutionInput,
    primary_agent_id: &str,
) -> Result<ProductMetricSeries, SimulationError> {
    simulate_product_metrics_validated(ValidatedInput::new(fixture)?, primary_agent_id)
}

#[cfg(test)]
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
/// The driver supplies its immutable input context, so a session can own paths and books
/// side by side without either borrowing the other.
struct RolloutState {
    rollout_id: u32,
    month: u32,
    ledger: Ledger,
    lots: Vec<LotState>,
    properties: Vec<PropertyState>,
    mortgages: Vec<MortgageState>,
    tax: TaxState,
    tax_liabilities: Vec<TaxLiabilityState>,
    tlh_portfolios: Vec<TlhPortfolioObservation>,
    primary_residence_by_agent: BTreeMap<String, Option<String>>,
    recorder: Recorder,
    failed_month: Option<u32>,
    product_metrics: Vec<BaseMetrics>,
}

#[cfg(test)]
fn simulate_rollout(
    fixture: &ExecutionInput,
    rollout_id: u32,
    capture_mode: CaptureMode,
    product: Option<&ProductInputs>,
) -> Result<RolloutComputation, SimulationError> {
    RolloutState::new(fixture, rollout_id, capture_mode, product)?.run(fixture, product)
}

impl RolloutState {
    fn new(
        fixture: &ExecutionInput,
        rollout_id: u32,
        capture_mode: CaptureMode,
        product: Option<&ProductInputs>,
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
        for pool in &fixture.scenario.holding_pools {
            accounts.push(asset_basis_account(
                &pool.agent_id,
                &pool.account_id,
                &pool.asset_id,
            ));
            accounts.push(realized_gain_account(&pool.agent_id));
        }
        for portfolio in &fixture.scenario.tlh_portfolios {
            accounts.push(AccountRef::new(&portfolio.owner_agent_id, OPENING_EQUITY));
            accounts.push(AccountRef::new(
                &portfolio.owner_agent_id,
                format!("asset:managed-portfolio:{}", portfolio.portfolio_id),
            ));
            accounts.push(realized_gain_account(&portfolio.owner_agent_id));
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
        let tlh_portfolios = Vec::new();

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
                                account: asset_basis_account(
                                    &spec.agent_id,
                                    &spec.account_id,
                                    &spec.asset_id,
                                ),
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
                &tlh_portfolios,
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
                &tlh_portfolios,
                &properties,
                &mortgages,
                Money(0),
                false,
            )?);
        }
        Ok(Self {
            rollout_id,
            month: 0,
            ledger,
            lots,
            properties,
            mortgages,
            tax,
            tax_liabilities,
            tlh_portfolios,
            primary_residence_by_agent,
            recorder,
            failed_month: None,
            product_metrics,
        })
    }

    fn is_finished(&self, fixture: &ExecutionInput) -> bool {
        self.failed_month.is_some() || self.month == fixture.scenario.horizon_months
    }

    #[cfg(test)]
    fn run(
        mut self,
        fixture: &ExecutionInput,
        product: Option<&ProductInputs>,
    ) -> Result<RolloutComputation, SimulationError> {
        while !self.is_finished(fixture) {
            self = self.advance_month(fixture, product)?;
        }
        self.finish(fixture)
    }

    /// Execute one configured month. Terminal states are unchanged.
    /// Consuming the state makes any execution
    /// error fatal: a caller cannot resume a month whose books may be partly updated.
    #[cfg(test)]
    fn advance_month(
        mut self,
        fixture: &ExecutionInput,
        product: Option<&ProductInputs>,
    ) -> Result<Self, SimulationError> {
        if self.is_finished(fixture) {
            return Ok(self);
        }
        let rollout_id = self.rollout_id;
        let month = self.month;
        let mut claims = self.prepare_month(fixture)?;
        let settlement = settle_grouped(
            &mut payments::Context {
                fixture,
                ledger: &mut self.ledger,
                recorder: &mut self.recorder,
                tax: &mut self.tax,
                properties: &self.properties,
                mortgages: &mut self.mortgages,
                tax_liabilities: &mut self.tax_liabilities,
                month,
            },
            &mut claims,
            product.map(|inputs| inputs.primary_agent_id()),
        )?;
        if settlement.failed {
            self.failed_month = Some(month);
        } else {
            execute_private_equity(
                fixture,
                rollout_id,
                &mut self.ledger,
                &mut self.recorder,
                &mut self.lots,
                &mut self.tax,
                &self.tlh_portfolios,
                month,
            )?;
        }
        self.close_month(fixture, product, settlement.product_shortfall)?;
        Ok(self)
    }

    /// Apply scheduled events and assemble due claims without choosing their funding.
    /// Callers place their single policy review on the appropriate side of this phase.
    #[cfg(test)]
    fn prepare_month(
        &mut self,
        fixture: &ExecutionInput,
    ) -> Result<claims::Claims, SimulationError> {
        self.prepare_month_events(fixture)?;
        let rollout_id = self.rollout_id;
        let month = self.month;
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
                sale,
            )?;
        }
        claims::assemble(
            fixture,
            rollout_id,
            month,
            &self.properties,
            &self.mortgages,
            &self.tax_liabilities,
        )
    }

    fn prepare_month_events(&mut self, fixture: &ExecutionInput) -> Result<(), SimulationError> {
        let rollout_id = self.rollout_id;
        let month = self.month;
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
        Ok(())
    }

    /// Accrue and capture the completed month. Failed books use the observed stop marks
    /// and receive no further occupancy, depreciation or year-end assessment.
    fn close_month(
        &mut self,
        fixture: &ExecutionInput,
        product: Option<&ProductInputs>,
        product_shortfall: Money,
    ) -> Result<(), SimulationError> {
        let rollout_id = self.rollout_id;
        let month = self.month;
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
                &self.tlh_portfolios,
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
                &self.tlh_portfolios,
                &self.properties,
                &self.mortgages,
                product_shortfall,
                self.failed_month.is_some(),
            )?);
        }
        self.month += 1;
        Ok(())
    }

    fn finish(self, fixture: &ExecutionInput) -> Result<RolloutComputation, SimulationError> {
        assert!(
            self.is_finished(fixture),
            "only terminal rollouts can be finalized"
        );
        let rollout_id = self.rollout_id;
        debug_assert_eq!(self.ledger.trial_balance(), 0);
        Ok(RolloutComputation {
            rollout_id,
            ending_balances: account_balances(&self.ledger),
            ending_bonds: bond_states(
                fixture,
                rollout_id,
                self.month,
                self.failed_month.unwrap_or(self.month),
            )?,
            ending_properties: self.properties,
            ending_mortgages: self.mortgages,
            ending_tax_liabilities: self.tax_liabilities,
            ending_tlh_portfolios: self.tlh_portfolios,
            recorder: self.recorder,
            failed_month: self.failed_month,
            #[cfg(test)]
            product_metrics: self.product_metrics,
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
    tlh_portfolios: &[TlhPortfolioObservation],
    properties: &[PropertyState],
    mortgages: &[MortgageState],
    shortfall: Money,
    failed: bool,
) -> Result<BaseMetrics, SimulationError> {
    let lot_views: Vec<_> = lots.iter().map(LotState::view).collect();
    let valuation_month = if failed { snapshot - 1 } else { snapshot };
    let bonds = bond_states(fixture, rollout_id, snapshot, valuation_month)?;
    let state = SnapshotState {
        ledger,
        lots: &lot_views,
        tlh_portfolios,
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
