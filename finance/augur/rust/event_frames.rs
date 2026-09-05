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
//! Each frame below is one table: the Augur column on the left, the engine field it reads on
//! the right, and a word for how to carry it across. `event_frames!` generates both the
//! struct and the conversion from that one spec, so a column cannot be declared in one place
//! and filled in from another.
//!
//! Every struct field name is an Augur column name, and `EventFrames`' field names are
//! Augur's frame names: `finance/augur/sim/events.py` declares both, and
//! `event_log.py` checks a decoded document against those declarations.

use serde::Serialize;

use crate::fixture;
use crate::money::{Money, Quantity};

/// A run beside the event frames derived from it.
///
/// Deriving them from the output rather than recording them separately means the frames
/// cannot describe a different run than the snapshots they ship with.
///
/// Both capture modes that retain monthly state ship this. The frames are what a Python
/// reader consumes, and they say the same thing whether or not the run also kept the
/// balanced journal, so a caller wanting a trace need not pay for one.
#[derive(Debug, Serialize)]
pub struct FramedOutput<'a> {
    #[serde(flatten)]
    pub output: &'a fixture::SimulationOutput,
    pub event_frames: EventFrames,
}

impl<'a> FramedOutput<'a> {
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

/// The Augur column type each carry-word produces.
///
/// `Money` serializes as the bare integer it wraps, so a `_quanta` column keeps the engine's
/// own type rather than being unwrapped to `i64` at every field.
macro_rules! frame_type {
    (text) => { String };
    (maybe) => { Option<String> };
    (money) => { Money };
    (flag) => { bool };
    (count) => { u32 };
    (month) => { i32 };
    (rate) => { f64 };
    (units) => { f64 };
}

/// How each carry-word reads its engine field. The only two that do arithmetic are the two
/// that change units.
macro_rules! frame_value {
    (text $row:ident, $($path:ident).+) => { $row.$($path).+.clone() };
    (maybe $row:ident, $($path:ident).+) => { $row.$($path).+.clone() };
    (money $row:ident, $($path:ident).+) => { $row.$($path).+ };
    (flag $row:ident, $($path:ident).+) => { $row.$($path).+ };
    (count $row:ident, $($path:ident).+) => { $row.$($path).+ };
    (month $row:ident, $($path:ident).+) => { $row.$($path).+ };
    (rate $row:ident, $($path:ident).+) => { rate($row.$($path).+) };
    (units $row:ident, $($path:ident).+) => { units($row.$($path).+, $row.quantity_scale) };
}

/// Declare the canonical frames: `<frame name>: <row type> from <engine field> { columns }`.
///
/// `rollout_index` and `month_index` are on every frame and come from the enclosing rollout
/// and the row's own month, so they are not repeated per frame.
macro_rules! event_frames {
    ($(
        $(#[$meta:meta])*
        $target:ident : $name:ident from $source:ident {
            $( $kind:ident $field:ident = $($path:ident).+ ),* $(,)?
        }
    )*) => {
        $(
            $(#[$meta])*
            #[derive(Debug, Serialize)]
            pub struct $name {
                pub rollout_index: u32,
                pub month_index: u32,
                $( pub $field: frame_type!($kind), )*
            }
        )*

        /// Every canonical frame, keyed by the name Augur's `EventLog` knows it as.
        #[derive(Debug, Default, Serialize)]
        pub struct EventFrames {
            $( pub $target: Vec<$name>, )*
        }

        impl EventFrames {
            pub fn from_output(output: &fixture::SimulationOutput) -> Self {
                let mut frames = Self::default();
                for rollout in &output.rollouts {
                    $(
                        frames.$target.extend(rollout.$source.iter().map(|row| $name {
                            rollout_index: rollout.rollout_id,
                            month_index: row.month,
                            $( $field: frame_value!($kind row, $($path).+), )*
                        }));
                    )*
                }
                frames
            }
        }
    };
}

event_frames! {
    transfers: Transfer from transfers {
        text cause_id = cause_id,
        text from_agent_id = from.agent_id,
        text from_account_id = from.account_id,
        text to_agent_id = to.agent_id,
        text to_account_id = to.account_id,
        money amount_quanta = amount,
        maybe income_category = income_category,
    }

    lot_dispositions: LotDisposition from dispositions {
        text cause_id = cause_id,
        text agent_id = agent_id,
        text source_account_id = source_account_id,
        text asset_id = asset_id,
        text lot_id = lot_id,
        month purchase_month_index = purchase_month,
        units units_sold = units,
        money cost_basis_consumed_quanta = basis,
        money proceeds_quanta = proceeds,
        text proceeds_account_id = proceeds_account_id,
    }

    private_equity_events: PrivateEquityEvent from private_equity_events {
        text issuer_id = issuer_id,
        text asset_id = asset_id,
        text event_kind = event_kind,
        text regime = regime,
        money mark_quanta = mark,
        rate sale_capacity_fraction = sale_capacity_fraction_ppb,
        rate eligible_fraction = eligible_fraction_ppb,
        rate forced_sale_fraction = forced_sale_fraction_ppb,
        flag liquidity_blocked = liquidity_blocked,
        money forced_recovery_cashout_quanta = forced_recovery_cashout,
    }

    private_equity_opportunities: PrivateEquityOpportunity from private_equity_opportunities {
        text cause_id = cause_id,
        text issuer_id = issuer_id,
        text asset_id = asset_id,
        text event_kind = event_kind,
        text regime = regime,
        text outcome = outcome,
        money mark_quanta = mark,
        rate sale_capacity_fraction = sale_capacity_fraction_ppb,
        rate eligible_fraction = eligible_fraction_ppb,
        flag liquidity_blocked = liquidity_blocked,
        money floor_quanta = floor,
        money liquid_net_worth_quanta = liquid_net_worth,
        money shortfall_quanta = shortfall,
        units units_held = units_held,
        units sellable_units = sellable_units,
        units target_units = target_units,
        money proceeds_quanta = proceeds,
    }

    /// One engine outcome, two Augur frames: what was owed, and what was actually paid.
    obligation_accruals: ObligationAccrual from obligations {
        text cause_id = cause_id,
        text obligation_id = obligation_id,
        text obligation_type = obligation_type,
        text agent_id = from.agent_id,
        text from_account_id = from.account_id,
        text to_agent_id = to.agent_id,
        text to_account_id = to.account_id,
        money amount_due_quanta = amount_due,
    }

    obligation_settlements: ObligationSettlement from obligations {
        text cause_id = cause_id,
        text obligation_id = obligation_id,
        text obligation_type = obligation_type,
        text agent_id = from.agent_id,
        text from_account_id = from.account_id,
        money amount_due_quanta = amount_due,
        money amount_paid_quanta = amount_paid,
        money shortfall_quanta = shortfall,
        text attempted_funding_sources = attempted_funding_sources,
    }

    rollout_failures: RolloutFailure from rollout_failures {
        text cause_id = cause_id,
        text agent_id = agent_id,
        money deficit_quanta = deficit,
        text obligation_id = obligation_id,
        text obligation_type = obligation_type,
        money amount_due_quanta = amount_due,
        money amount_paid_quanta = amount_paid,
        money shortfall_quanta = shortfall,
        text attempted_funding_sources = attempted_funding_sources,
    }

    property_purchases: PropertyPurchase from property_purchases {
        text cause_id = cause_id,
        text property_id = property_id,
        text location_id = location_id,
        text buyer_agent_id = buyer_agent_id,
        money purchase_price_quanta = purchase_price,
        money closing_cost_quanta = closing_cost,
        money adjusted_basis_quanta = adjusted_basis,
        money stake_contribution_quanta = stake_contribution,
        money equity_ledger_quanta = equity_ledger,
    }

    mortgage_originations: MortgageOrigination from mortgage_originations {
        text cause_id = cause_id,
        text liability_id = liability_id,
        text agent_id = agent_id,
        text payment_account_id = payment_account_id,
        text counterparty_agent_id = counterparty_agent_id,
        text counterparty_account_id = counterparty_account_id,
        text property_id = property_id,
        money principal_quanta = principal,
        rate annual_interest_rate = annual_interest_rate_ppb,
        count term_months = term_months,
        money monthly_payment_quanta = monthly_payment,
    }

    mortgage_payments: MortgagePayment from mortgage_payments {
        text cause_id = cause_id,
        text liability_id = liability_id,
        text agent_id = agent_id,
        text counterparty_agent_id = counterparty_agent_id,
        text property_id = property_id,
        text from_account_id = from_account_id,
        text to_account_id = to_account_id,
        money interest_quanta = interest,
        money principal_quanta = principal,
        money total_payment_quanta = total_payment,
    }

    set_primary_residence_events: SetPrimaryResidence from primary_residence_events {
        text agent_id = agent_id,
        maybe property_id = property_id,
        flag is_primary_residence = is_primary_residence,
    }

    set_rented_fraction_events: SetRentedFraction from property_rented_fraction_events {
        text property_id = property_id,
        rate rented_fraction = rented_fraction_ppb,
    }

    capital_improvement_events: CapitalImprovement from capital_improvements {
        text property_id = property_id,
        money amount_quanta = amount,
        text description = description,
    }

    property_sale_events: PropertySale from property_sales {
        text property_id = property_id,
        money gross_proceeds_quanta = gross_proceeds,
        money mortgage_payoff_quanta = mortgage_payoff,
        money net_cash_to_owner_quanta = net_cash_to_owner,
        money realized_gain_quanta = realized_gain,
        money depreciation_recapture_quanta = depreciation_recapture,
        money section_121_exclusion_quanta = section_121_exclusion,
        money long_term_capital_gain_quanta = long_term_capital_gain,
    }

    /// One engine outcome again: the amount owed, and the audit trail behind it.
    tax_accruals: TaxAccrual from tax_accruals {
        text cause_id = cause_id,
        text agent_id = agent_id,
        text jurisdiction_id = jurisdiction_id,
        count tax_year_end_month = tax_year_end_month,
        money amount_quanta = total_tax,
    }

    tax_breakdowns: TaxBreakdown from tax_accruals {
        text cause_id = cause_id,
        text agent_id = agent_id,
        text jurisdiction_id = jurisdiction_id,
        count tax_year_end_month = tax_year_end_month,
        money ordinary_income_quanta = ordinary_income,
        money ltcg_quanta = long_term_gain,
        money stcg_quanta = short_term_gain,
        money standard_deduction_quanta = standard_deduction,
        money mortgage_interest_deduction_quanta = mortgage_interest_deduction,
        money salt_deduction_quanta = salt_deduction,
        money itemized_deduction_quanta = itemized_deduction,
        money ordinary_taxable_quanta = ordinary_taxable,
        money capital_gain_taxable_quanta = long_term_capital_gain_taxable,
        money ordinary_tax_quanta = ordinary_tax,
        money capital_gain_tax_quanta = capital_gain_tax,
        money total_tax_quanta = total_tax,
    }

    tax_settlements: TaxSettlement from tax_settlements {
        text cause_id = cause_id,
        text agent_id = agent_id,
        count tax_year_end_month = tax_year_end_month,
        money amount_quanta = amount,
    }
}
