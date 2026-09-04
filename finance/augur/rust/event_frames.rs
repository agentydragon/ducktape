//! The canonical event frames, in Augur's column names and units.
//!
//! The engine records outcomes in its own vocabulary: integer `Money` and `Quantity`, rates
//! as parts per billion, an `AccountRef` pair rather than two flat columns. Augur's event
//! log names the same facts differently and carries rates and unit counts as floats. This
//! module is where the two meet, so the knowledge that `sale_capacity_fraction_ppb` is
//! parts per billion and that a `Quantity` divides by its lot's `quantity_scale` sits beside
//! the engine that defines those units, and a field renamed in `fixture.rs` fails the build
//! here rather than surfacing later as a `KeyError` in a Python decoder.
//!
//! Every struct field name below is an Augur column name, and `EventFrames`' field names are
//! Augur's frame names: `finance/augur/sim/events.py` declares both, and
//! `differential/output_adapter.py` checks a decoded document against those declarations.

use serde::Serialize;

use crate::fixture;
use crate::money::Quantity;

/// A forensic run beside the event frames derived from it.
///
/// Deriving them from the output rather than recording them separately means the frames
/// cannot describe a different run than the snapshots they ship with.
#[derive(Debug, Serialize)]
pub struct ForensicDocument<'a> {
    #[serde(flatten)]
    pub output: &'a fixture::SimulationOutput,
    pub event_frames: EventFrames,
}

impl<'a> ForensicDocument<'a> {
    pub fn new(output: &'a fixture::SimulationOutput) -> Self {
        Self {
            event_frames: EventFrames::from_output(output),
            output,
        }
    }
}

/// Parts per billion, the engine's scale for a dimensionless rate.
const RATE_SCALE_PPB: f64 = 1_000_000_000.0;

fn rate(ppb: i64) -> f64 {
    ppb as f64 / RATE_SCALE_PPB
}

/// A `Quantity` is a count of `quantity_scale` sub-units; Augur reports whole units.
fn units(quantity: Quantity, quantity_scale: i64) -> f64 {
    quantity.0 as f64 / quantity_scale as f64
}

#[derive(Debug, Serialize)]
pub struct Transfer {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub from_agent_id: String,
    pub from_account_id: String,
    pub to_agent_id: String,
    pub to_account_id: String,
    pub amount_quanta: i64,
    pub income_category: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct LotDisposition {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub agent_id: String,
    pub source_account_id: String,
    pub asset_id: String,
    pub lot_id: String,
    pub purchase_month_index: i32,
    pub units_sold: f64,
    pub cost_basis_consumed_quanta: i64,
    pub proceeds_quanta: i64,
    pub proceeds_account_id: String,
}

#[derive(Debug, Serialize)]
pub struct PrivateEquityEvent {
    pub rollout_index: u32,
    pub month_index: u32,
    pub issuer_id: String,
    pub asset_id: String,
    pub event_kind: String,
    pub regime: String,
    pub mark_quanta: i64,
    pub sale_capacity_fraction: f64,
    pub eligible_fraction: f64,
    pub forced_sale_fraction: f64,
    pub liquidity_blocked: bool,
    pub forced_recovery_cashout_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct PrivateEquityOpportunity {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub issuer_id: String,
    pub asset_id: String,
    pub event_kind: String,
    pub regime: String,
    pub outcome: String,
    pub mark_quanta: i64,
    pub sale_capacity_fraction: f64,
    pub eligible_fraction: f64,
    pub liquidity_blocked: bool,
    pub floor_quanta: i64,
    pub liquid_net_worth_quanta: i64,
    pub shortfall_quanta: i64,
    pub units_held: f64,
    pub sellable_units: f64,
    pub target_units: f64,
    pub proceeds_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct ObligationAccrual {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub obligation_id: String,
    pub obligation_type: String,
    pub agent_id: String,
    pub from_account_id: String,
    pub to_agent_id: String,
    pub to_account_id: String,
    pub amount_due_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct ObligationSettlement {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub obligation_id: String,
    pub obligation_type: String,
    pub agent_id: String,
    pub from_account_id: String,
    pub amount_due_quanta: i64,
    pub amount_paid_quanta: i64,
    pub shortfall_quanta: i64,
    pub attempted_funding_sources: String,
}

#[derive(Debug, Serialize)]
pub struct RolloutFailure {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub agent_id: String,
    pub deficit_quanta: i64,
    pub obligation_id: String,
    pub obligation_type: String,
    pub amount_due_quanta: i64,
    pub amount_paid_quanta: i64,
    pub shortfall_quanta: i64,
    pub attempted_funding_sources: String,
}

#[derive(Debug, Serialize)]
pub struct PropertyPurchase {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub property_id: String,
    pub location_id: String,
    pub buyer_agent_id: String,
    pub purchase_price_quanta: i64,
    pub closing_cost_quanta: i64,
    pub adjusted_basis_quanta: i64,
    pub stake_contribution_quanta: i64,
    pub equity_ledger_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct MortgageOrigination {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub liability_id: String,
    pub agent_id: String,
    pub payment_account_id: String,
    pub counterparty_agent_id: String,
    pub counterparty_account_id: String,
    pub property_id: String,
    pub principal_quanta: i64,
    pub annual_interest_rate: f64,
    pub term_months: u32,
    pub monthly_payment_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct MortgagePayment {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub liability_id: String,
    pub agent_id: String,
    pub counterparty_agent_id: String,
    pub property_id: String,
    pub from_account_id: String,
    pub to_account_id: String,
    pub interest_quanta: i64,
    pub principal_quanta: i64,
    pub total_payment_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct SetPrimaryResidence {
    pub rollout_index: u32,
    pub month_index: u32,
    pub agent_id: String,
    pub property_id: Option<String>,
    pub is_primary_residence: bool,
}

#[derive(Debug, Serialize)]
pub struct SetRentedFraction {
    pub rollout_index: u32,
    pub month_index: u32,
    pub property_id: String,
    pub rented_fraction: f64,
}

#[derive(Debug, Serialize)]
pub struct CapitalImprovement {
    pub rollout_index: u32,
    pub month_index: u32,
    pub property_id: String,
    pub amount_quanta: i64,
    pub description: String,
}

#[derive(Debug, Serialize)]
pub struct PropertySale {
    pub rollout_index: u32,
    pub month_index: u32,
    pub property_id: String,
    pub gross_proceeds_quanta: i64,
    pub mortgage_payoff_quanta: i64,
    pub net_cash_to_owner_quanta: i64,
    pub realized_gain_quanta: i64,
    pub depreciation_recapture_quanta: i64,
    pub section_121_exclusion_quanta: i64,
    pub long_term_capital_gain_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct TaxAccrual {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub agent_id: String,
    pub jurisdiction_id: String,
    pub tax_year_end_month: u32,
    pub amount_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct TaxBreakdown {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub agent_id: String,
    pub jurisdiction_id: String,
    pub tax_year_end_month: u32,
    pub ordinary_income_quanta: i64,
    pub ltcg_quanta: i64,
    pub stcg_quanta: i64,
    pub standard_deduction_quanta: i64,
    pub mortgage_interest_deduction_quanta: i64,
    pub salt_deduction_quanta: i64,
    pub itemized_deduction_quanta: i64,
    pub ordinary_taxable_quanta: i64,
    pub capital_gain_taxable_quanta: i64,
    pub ordinary_tax_quanta: i64,
    pub capital_gain_tax_quanta: i64,
    pub total_tax_quanta: i64,
}

#[derive(Debug, Serialize)]
pub struct TaxSettlement {
    pub rollout_index: u32,
    pub month_index: u32,
    pub cause_id: String,
    pub agent_id: String,
    pub tax_year_end_month: u32,
    pub amount_quanta: i64,
}

/// Every canonical frame, keyed by the name Augur's `EventLog` knows it as.
#[derive(Debug, Default, Serialize)]
pub struct EventFrames {
    pub transfers: Vec<Transfer>,
    pub lot_dispositions: Vec<LotDisposition>,
    pub private_equity_events: Vec<PrivateEquityEvent>,
    pub private_equity_opportunities: Vec<PrivateEquityOpportunity>,
    pub obligation_accruals: Vec<ObligationAccrual>,
    pub obligation_settlements: Vec<ObligationSettlement>,
    pub rollout_failures: Vec<RolloutFailure>,
    pub property_purchases: Vec<PropertyPurchase>,
    pub mortgage_originations: Vec<MortgageOrigination>,
    pub mortgage_payments: Vec<MortgagePayment>,
    pub set_primary_residence_events: Vec<SetPrimaryResidence>,
    pub set_rented_fraction_events: Vec<SetRentedFraction>,
    pub capital_improvement_events: Vec<CapitalImprovement>,
    pub property_sale_events: Vec<PropertySale>,
    pub tax_accruals: Vec<TaxAccrual>,
    pub tax_breakdowns: Vec<TaxBreakdown>,
    pub tax_settlements: Vec<TaxSettlement>,
}

impl EventFrames {
    pub fn from_output(output: &fixture::SimulationOutput) -> Self {
        let mut frames = Self::default();
        for rollout in &output.rollouts {
            frames.extend_from_rollout(rollout);
        }
        frames
    }

    fn extend_from_rollout(&mut self, rollout: &fixture::RolloutOutput) {
        let index = rollout.rollout_id;
        self.transfers
            .extend(rollout.transfers.iter().map(|row| Transfer {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                from_agent_id: row.from.agent_id.clone(),
                from_account_id: row.from.account_id.clone(),
                to_agent_id: row.to.agent_id.clone(),
                to_account_id: row.to.account_id.clone(),
                amount_quanta: row.amount.0,
                income_category: row.income_category.clone(),
            }));
        self.lot_dispositions
            .extend(rollout.dispositions.iter().map(|row| LotDisposition {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                agent_id: row.agent_id.clone(),
                source_account_id: row.source_account_id.clone(),
                asset_id: row.asset_id.clone(),
                lot_id: row.lot_id.clone(),
                purchase_month_index: row.purchase_month,
                units_sold: units(row.units, row.quantity_scale),
                cost_basis_consumed_quanta: row.basis.0,
                proceeds_quanta: row.proceeds.0,
                proceeds_account_id: row.proceeds_account_id.clone(),
            }));
        self.private_equity_events
            .extend(
                rollout
                    .private_equity_events
                    .iter()
                    .map(|row| PrivateEquityEvent {
                        rollout_index: index,
                        month_index: row.month,
                        issuer_id: row.issuer_id.clone(),
                        asset_id: row.asset_id.clone(),
                        event_kind: row.event_kind.clone(),
                        regime: row.regime.clone(),
                        mark_quanta: row.mark.0,
                        sale_capacity_fraction: rate(row.sale_capacity_fraction_ppb),
                        eligible_fraction: rate(row.eligible_fraction_ppb),
                        forced_sale_fraction: rate(row.forced_sale_fraction_ppb),
                        liquidity_blocked: row.liquidity_blocked,
                        forced_recovery_cashout_quanta: row.forced_recovery_cashout.0,
                    }),
            );
        self.private_equity_opportunities
            .extend(rollout.private_equity_opportunities.iter().map(|row| {
                PrivateEquityOpportunity {
                    rollout_index: index,
                    month_index: row.month,
                    cause_id: row.cause_id.clone(),
                    issuer_id: row.issuer_id.clone(),
                    asset_id: row.asset_id.clone(),
                    event_kind: row.event_kind.clone(),
                    regime: row.regime.clone(),
                    outcome: row.outcome.clone(),
                    mark_quanta: row.mark.0,
                    sale_capacity_fraction: rate(row.sale_capacity_fraction_ppb),
                    eligible_fraction: rate(row.eligible_fraction_ppb),
                    liquidity_blocked: row.liquidity_blocked,
                    floor_quanta: row.floor.0,
                    liquid_net_worth_quanta: row.liquid_net_worth.0,
                    shortfall_quanta: row.shortfall.0,
                    units_held: units(row.units_held, row.quantity_scale),
                    sellable_units: units(row.sellable_units, row.quantity_scale),
                    target_units: units(row.target_units, row.quantity_scale),
                    proceeds_quanta: row.proceeds.0,
                }
            }));
        // One engine outcome, two Augur frames: what was owed, and what was actually paid.
        self.obligation_accruals
            .extend(rollout.obligations.iter().map(|row| ObligationAccrual {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                obligation_id: row.obligation_id.clone(),
                obligation_type: row.obligation_type.clone(),
                agent_id: row.from.agent_id.clone(),
                from_account_id: row.from.account_id.clone(),
                to_agent_id: row.to.agent_id.clone(),
                to_account_id: row.to.account_id.clone(),
                amount_due_quanta: row.amount_due.0,
            }));
        self.obligation_settlements
            .extend(rollout.obligations.iter().map(|row| ObligationSettlement {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                obligation_id: row.obligation_id.clone(),
                obligation_type: row.obligation_type.clone(),
                agent_id: row.from.agent_id.clone(),
                from_account_id: row.from.account_id.clone(),
                amount_due_quanta: row.amount_due.0,
                amount_paid_quanta: row.amount_paid.0,
                shortfall_quanta: row.shortfall.0,
                attempted_funding_sources: row.attempted_funding_sources.clone(),
            }));
        self.rollout_failures
            .extend(rollout.rollout_failures.iter().map(|row| RolloutFailure {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                agent_id: row.agent_id.clone(),
                deficit_quanta: row.deficit.0,
                obligation_id: row.obligation_id.clone(),
                obligation_type: row.obligation_type.clone(),
                amount_due_quanta: row.amount_due.0,
                amount_paid_quanta: row.amount_paid.0,
                shortfall_quanta: row.shortfall.0,
                attempted_funding_sources: row.attempted_funding_sources.clone(),
            }));
        self.property_purchases
            .extend(
                rollout
                    .property_purchases
                    .iter()
                    .map(|row| PropertyPurchase {
                        rollout_index: index,
                        month_index: row.month,
                        cause_id: row.cause_id.clone(),
                        property_id: row.property_id.clone(),
                        location_id: row.location_id.clone(),
                        buyer_agent_id: row.buyer_agent_id.clone(),
                        purchase_price_quanta: row.purchase_price.0,
                        closing_cost_quanta: row.closing_cost.0,
                        adjusted_basis_quanta: row.adjusted_basis.0,
                        stake_contribution_quanta: row.stake_contribution.0,
                        equity_ledger_quanta: row.equity_ledger.0,
                    }),
            );
        self.mortgage_originations
            .extend(
                rollout
                    .mortgage_originations
                    .iter()
                    .map(|row| MortgageOrigination {
                        rollout_index: index,
                        month_index: row.month,
                        cause_id: row.cause_id.clone(),
                        liability_id: row.liability_id.clone(),
                        agent_id: row.agent_id.clone(),
                        payment_account_id: row.payment_account_id.clone(),
                        counterparty_agent_id: row.counterparty_agent_id.clone(),
                        counterparty_account_id: row.counterparty_account_id.clone(),
                        property_id: row.property_id.clone(),
                        principal_quanta: row.principal.0,
                        annual_interest_rate: rate(row.annual_interest_rate_ppb),
                        term_months: row.term_months,
                        monthly_payment_quanta: row.monthly_payment.0,
                    }),
            );
        self.mortgage_payments
            .extend(rollout.mortgage_payments.iter().map(|row| MortgagePayment {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                liability_id: row.liability_id.clone(),
                agent_id: row.agent_id.clone(),
                counterparty_agent_id: row.counterparty_agent_id.clone(),
                property_id: row.property_id.clone(),
                from_account_id: row.from_account_id.clone(),
                to_account_id: row.to_account_id.clone(),
                interest_quanta: row.interest.0,
                principal_quanta: row.principal.0,
                total_payment_quanta: row.total_payment.0,
            }));
        self.set_primary_residence_events
            .extend(
                rollout
                    .primary_residence_events
                    .iter()
                    .map(|row| SetPrimaryResidence {
                        rollout_index: index,
                        month_index: row.month,
                        agent_id: row.agent_id.clone(),
                        property_id: row.property_id.clone(),
                        is_primary_residence: row.is_primary_residence,
                    }),
            );
        self.set_rented_fraction_events
            .extend(
                rollout
                    .property_rented_fraction_events
                    .iter()
                    .map(|row| SetRentedFraction {
                        rollout_index: index,
                        month_index: row.month,
                        property_id: row.property_id.clone(),
                        rented_fraction: rate(row.rented_fraction_ppb),
                    }),
            );
        self.capital_improvement_events
            .extend(
                rollout
                    .capital_improvements
                    .iter()
                    .map(|row| CapitalImprovement {
                        rollout_index: index,
                        month_index: row.month,
                        property_id: row.property_id.clone(),
                        amount_quanta: row.amount.0,
                        description: row.description.clone(),
                    }),
            );
        self.property_sale_events
            .extend(rollout.property_sales.iter().map(|row| PropertySale {
                rollout_index: index,
                month_index: row.month,
                property_id: row.property_id.clone(),
                gross_proceeds_quanta: row.gross_proceeds.0,
                mortgage_payoff_quanta: row.mortgage_payoff.0,
                net_cash_to_owner_quanta: row.net_cash_to_owner.0,
                realized_gain_quanta: row.realized_gain.0,
                depreciation_recapture_quanta: row.depreciation_recapture.0,
                section_121_exclusion_quanta: row.section_121_exclusion.0,
                long_term_capital_gain_quanta: row.long_term_capital_gain.0,
            }));
        // One engine outcome again: the amount owed, and the audit trail behind it.
        self.tax_accruals
            .extend(rollout.tax_accruals.iter().map(|row| TaxAccrual {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                agent_id: row.agent_id.clone(),
                jurisdiction_id: row.jurisdiction_id.clone(),
                tax_year_end_month: row.tax_year_end_month,
                amount_quanta: row.total_tax.0,
            }));
        self.tax_breakdowns
            .extend(rollout.tax_accruals.iter().map(|row| TaxBreakdown {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                agent_id: row.agent_id.clone(),
                jurisdiction_id: row.jurisdiction_id.clone(),
                tax_year_end_month: row.tax_year_end_month,
                ordinary_income_quanta: row.ordinary_income.0,
                ltcg_quanta: row.long_term_gain.0,
                stcg_quanta: row.short_term_gain.0,
                standard_deduction_quanta: row.standard_deduction.0,
                mortgage_interest_deduction_quanta: row.mortgage_interest_deduction.0,
                salt_deduction_quanta: row.salt_deduction.0,
                itemized_deduction_quanta: row.itemized_deduction.0,
                ordinary_taxable_quanta: row.ordinary_taxable.0,
                capital_gain_taxable_quanta: row.long_term_capital_gain_taxable.0,
                ordinary_tax_quanta: row.ordinary_tax.0,
                capital_gain_tax_quanta: row.capital_gain_tax.0,
                total_tax_quanta: row.total_tax.0,
            }));
        self.tax_settlements
            .extend(rollout.tax_settlements.iter().map(|row| TaxSettlement {
                rollout_index: index,
                month_index: row.month,
                cause_id: row.cause_id.clone(),
                agent_id: row.agent_id.clone(),
                tax_year_end_month: row.tax_year_end_month,
                amount_quanta: row.amount.0,
            }));
    }
}
