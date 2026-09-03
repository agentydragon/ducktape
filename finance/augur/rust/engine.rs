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
        InitialLotSpec, LotDisposition, MonthOutput, MortgageOriginationOutcome,
        MortgagePaymentOutcome, MortgageState, ObligationOutcome, PopulationOutput,
        PrimaryResidenceOutcome, PrivateEquityOpportunityOutcome, PrivateEquityProtocolOutcome,
        PropertyPurchaseOutcome, PropertyRentedFractionOutcome, PropertySaleOutcome,
        PropertySaleSpec, PropertyState, RolloutFailureOutcome, RolloutOutput, RolloutSummary,
        SecurityLotState, SeriesSpec, SimulationOutput, TaxAccrual, TaxLiabilityState,
        TaxPaymentOutcome, TaxSettlementOutcome, TransferOutcome,
    },
    ledger::{AccountRef, JournalEntry, Ledger, LedgerError, Posting},
    money::{ArithmeticError, Money, Quantity, mul_div_i128_round_half_up, mul_div_round_half_up},
    tax::{
        JurisdictionLevel, RATE_SCALE, TaxError, TaxFacts, assess, net_capital_gains,
        validate_rules,
    },
};

const EXTERNAL_AGENT: &str = "__external__";
const OPENING_EQUITY: &str = "equity:opening";
const RATE_SCALE_PPB: i64 = 1_000_000_000;
const INDEX_LEVEL_SCALE: i64 = 1_000_000_000;
const MAX_EXACT_F64_INTEGER: i64 = 1_i64 << 53;
const CONTRACT_SCALE: i128 = 1_000_000_000_000_000_000;
const SECTION_121_LOOKBACK_MONTHS: usize = 60;
const SECTION_121_MIN_QUALIFYING_MONTHS: usize = 24;
const PE_ASSET_PREFIX: &str = "private_equity:";

#[derive(Debug, Error)]
pub enum SimulationError {
    #[error("unsupported fixture schema version {actual}; expected {expected}")]
    SchemaVersion { actual: u32, expected: u32 },
    #[error("fixture must contain at least one rollout")]
    EmptyRollouts,
    #[error("fixture horizon must contain at least one month")]
    EmptyHorizon,
    #[error("currency code {currency_code:?} must be three uppercase ASCII letters")]
    InvalidCurrencyCode { currency_code: String },
    #[error("currency quantum {currency_quantum:?} must be a positive exact decimal")]
    InvalidCurrencyQuantum { currency_quantum: String },
    #[error("fixture dimensions overflow the supported address space")]
    FixtureDimensions,
    #[error("duplicate account {agent_id}:{account_id}")]
    DuplicateAccount {
        agent_id: String,
        account_id: String,
    },
    #[error("{context} references unknown account {agent_id}:{account_id}")]
    UnknownAccountReference {
        context: String,
        agent_id: String,
        account_id: String,
    },
    #[error("duplicate series id {series_id:?}")]
    DuplicateSeries { series_id: String },
    #[error(
        "security series {series_id:?} contains non-positive value {value} at flat index {index}"
    )]
    InvalidSecurityPrice {
        series_id: String,
        index: usize,
        value: i64,
    },
    #[error("series {series_id:?} has {actual} values; expected {expected}")]
    SeriesShape {
        series_id: String,
        actual: usize,
        expected: usize,
    },
    #[error("missing series {series_id:?}")]
    MissingSeries { series_id: String },
    #[error("missing value {series_id:?} for rollout {rollout}, snapshot {snapshot}")]
    MissingSeriesValue {
        series_id: String,
        rollout: u32,
        snapshot: u32,
    },
    #[error("lot {lot_id:?} has invalid quantity scale {quantity_scale}")]
    InvalidQuantityScale { lot_id: String, quantity_scale: i64 },
    #[error("lot {lot_id:?} has non-positive units {units} or negative basis {basis}")]
    InvalidLot {
        lot_id: String,
        units: i64,
        basis: i64,
    },
    #[error("lot {lot_id:?} total basis does not encode an exact per-unit basis")]
    InexactLotBasis { lot_id: String },
    #[error(
        "FIFO pool {agent_id}:{account_id}:{asset_id} mixes quantity scales {first_scale} and {second_scale}"
    )]
    MixedQuantityScale {
        agent_id: String,
        account_id: String,
        asset_id: String,
        first_scale: i64,
        second_scale: i64,
    },
    #[error("duplicate lot id {lot_id:?}")]
    DuplicateLot { lot_id: String },
    #[error("duplicate bond id {bond_id:?}")]
    DuplicateBond { bond_id: String },
    #[error("bond {bond_id:?} has invalid par-only held-to-maturity terms")]
    InvalidBondTerms { bond_id: String },
    #[error("duplicate jurisdiction identity {jurisdiction_id:?}")]
    DuplicateJurisdictionIdentity { jurisdiction_id: String },
    #[error("bond {bond_id:?} references unknown issuer jurisdiction {jurisdiction_id:?}")]
    UnknownBondIssuer {
        bond_id: String,
        jurisdiction_id: String,
    },
    #[error("inflation-indexed bond {bond_id:?} requires an inflation series")]
    MissingBondInflationSeries { bond_id: String },
    #[error(
        "bond {bond_id:?} coupon rate {rate_ppb} ppb cannot round-trip through the Python/JAX float boundary"
    )]
    InexactBondCouponRate { bond_id: String, rate_ppb: i64 },
    #[error(
        "indexed bond {bond_id:?} period rate cannot match the Python/JAX float boundary exactly"
    )]
    InexactBondPeriodRate { bond_id: String },
    #[error("sale {cause_id:?} has non-positive units {units}")]
    InvalidSaleUnits { cause_id: String, units: i64 },
    #[error("{kind} {cause_id:?} has non-positive amount {amount}")]
    InvalidAmount {
        kind: &'static str,
        cause_id: String,
        amount: i64,
    },
    #[error("{kind} {cause_id:?} is scheduled at month {month}, outside horizon {horizon}")]
    EventOutsideHorizon {
        kind: &'static str,
        cause_id: String,
        month: u32,
        horizon: u32,
    },
    #[error("{kind} {cause_id:?} ends at {end_month} before starting at {start_month}")]
    InvalidRecurringRange {
        kind: &'static str,
        cause_id: String,
        start_month: u32,
        end_month: u32,
    },
    #[error("{kind} identifier must not be empty")]
    EmptyIdentifier { kind: &'static str },
    #[error("unsupported income category {category:?}; only ordinary is implemented")]
    UnsupportedIncomeCategory { category: String },
    #[error("{kind} {cause_id:?} uses unsupported amount index series {series_id:?}")]
    UnsupportedAmountSeries {
        kind: &'static str,
        cause_id: String,
        series_id: String,
    },
    #[error("{kind} {cause_id:?} has an invalid series-indexed amount schedule")]
    InvalidSeriesIndexedAmount {
        kind: &'static str,
        cause_id: String,
    },
    #[error(
        "{kind} {cause_id:?} is active at month {month} before its series-indexed base month {base_month}"
    )]
    SeriesAmountBeforeBase {
        kind: &'static str,
        cause_id: String,
        month: u32,
        base_month: u32,
    },
    #[error(
        "series-indexed amount {cause_id:?} has non-positive level {value} in {series_id:?} for rollout {rollout} at month {month}"
    )]
    NonPositiveSeriesAmountLevel {
        cause_id: String,
        series_id: String,
        rollout: u32,
        month: u32,
        value: i64,
    },
    #[error(
        "series-indexed amount {cause_id:?} has level {value} in {series_id:?} for rollout {rollout} at month {month}, which cannot round-trip exactly through the Python/JAX float level boundary"
    )]
    InexactSeriesAmountLevel {
        cause_id: String,
        series_id: String,
        rollout: u32,
        month: u32,
        value: i64,
    },
    #[error("duplicate tax profile jurisdiction {agent_id}:{jurisdiction_id}")]
    DuplicateTaxJurisdiction {
        agent_id: String,
        jurisdiction_id: String,
    },
    #[error("tax profile {agent_id:?} must contain at least one jurisdiction")]
    EmptyTaxProfile { agent_id: String },
    #[error("sale {cause_id:?} references no lots for {agent_id}:{account_id}:{asset_id}")]
    MissingSalePool {
        cause_id: String,
        agent_id: String,
        account_id: String,
        asset_id: String,
    },
    #[error("sale {cause_id:?} requests {requested} units but only {available} are available")]
    InsufficientLotUnits {
        cause_id: String,
        requested: i64,
        available: i64,
    },
    #[error("distribution references no lots for {agent_id}:{account_id}:{asset_id}")]
    MissingDistributionPool {
        agent_id: String,
        account_id: String,
        asset_id: String,
    },
    #[error("duplicate distribution for {agent_id}:{account_id}:{asset_id}")]
    DuplicateDistribution {
        agent_id: String,
        account_id: String,
        asset_id: String,
    },
    #[error("distribution {agent_id}:{account_id}:{asset_id} has invalid tax-character fractions")]
    InvalidDistributionTaxCharacter {
        agent_id: String,
        account_id: String,
        asset_id: String,
    },
    #[error(
        "distribution {agent_id}:{account_id}:{asset_id} references unknown issuer jurisdiction {jurisdiction_id:?}"
    )]
    UnknownDistributionIssuer {
        agent_id: String,
        account_id: String,
        asset_id: String,
        jurisdiction_id: String,
    },
    #[error("duplicate location id {location_id:?}")]
    DuplicateLocation { location_id: String },
    #[error("property purchase {cause_id:?} references unknown location {location_id:?}")]
    UnknownLocation {
        cause_id: String,
        location_id: String,
    },
    #[error("duplicate property id {property_id:?}")]
    DuplicateProperty { property_id: String },
    #[error("duplicate mortgage liability id {liability_id:?}")]
    DuplicateMortgage { liability_id: String },
    #[error("property purchase {cause_id:?} has invalid monetary terms")]
    InvalidPropertyTerms { cause_id: String },
    #[error("mortgage {liability_id:?} has invalid principal, rate, or term")]
    InvalidMortgageTerms { liability_id: String },
    #[error("property tax policy references unknown property {property_id:?}")]
    UnknownPropertyTaxProperty { property_id: String },
    #[error("property cashflow {cause_id:?} references unknown property {property_id:?}")]
    UnknownPropertyCashflow {
        cause_id: String,
        property_id: String,
    },
    #[error("property sale references unknown property {property_id:?}")]
    UnknownPropertySale { property_id: String },
    #[error("property {property_id:?} has multiple sale events")]
    DuplicatePropertySale { property_id: String },
    #[error("property sale for {property_id:?} has invalid month or closing costs")]
    InvalidPropertySale { property_id: String },
    #[error("property lifecycle event references unknown property {property_id:?}")]
    UnknownPropertyLifecycle { property_id: String },
    #[error("property lifecycle event for {property_id:?} has invalid month or value")]
    InvalidPropertyLifecycle { property_id: String },
    #[error("property lifecycle event for {property_id:?} occurs at or after its sale")]
    PropertyLifecycleAfterSale { property_id: String },
    #[error(
        "primary-residence assignment for {agent_id:?} references invalid property {property_id:?} at month {month}"
    )]
    InvalidPrimaryResidence {
        agent_id: String,
        property_id: String,
        month: u32,
    },
    #[error("multiple primary-residence assignments for {agent_id:?} at month {month}")]
    DuplicatePrimaryResidence { agent_id: String, month: u32 },
    #[error("mortgage-interest policy references unknown liability {liability_id:?}")]
    UnknownMortgageInterestPolicy { liability_id: String },
    #[error("mortgage-interest policy owner does not match liability {liability_id:?}")]
    InvalidMortgageInterestPolicy { liability_id: String },
    #[error("property tax policy for {property_id:?} has invalid rate or range")]
    InvalidPropertyTaxPolicy { property_id: String },
    #[error("federal SALT policy for {profile_id:?} is invalid")]
    InvalidSaltPolicy { profile_id: String },
    #[error("duplicate target-allocation policy for {agent_id}:{account_id}")]
    DuplicateTargetAllocationPolicy {
        agent_id: String,
        account_id: String,
    },
    #[error("target-allocation policy for {agent_id}:{account_id} has invalid configuration")]
    InvalidTargetAllocationPolicy {
        agent_id: String,
        account_id: String,
    },
    #[error(
        "target-allocation policy {cause_id_prefix:?} sleeve {sleeve_index} ran out of purchase slots: {configured} configured, {needed} needed. Raise `purchase_slots_per_sleeve` — every purchase needs its own lot, because it has its own basis and its own holding period."
    )]
    TargetAllocationPurchaseSlotExhaustion {
        cause_id_prefix: String,
        sleeve_index: usize,
        configured: u32,
        needed: u32,
    },
    #[error(
        "target-allocation policy for {agent_id}:{account_id} names duplicate asset {asset_id:?}"
    )]
    DuplicateTargetAllocationSleeve {
        agent_id: String,
        account_id: String,
        asset_id: String,
    },
    #[error("private-equity policy for {owner_agent_id:?} has invalid configuration")]
    InvalidPrivateEquityPolicy { owner_agent_id: String },
    #[error("private-equity issuer {issuer_id:?} is missing protocol series {series_id:?}")]
    MissingPrivateEquitySeries {
        issuer_id: String,
        series_id: String,
    },
    #[error(
        "private-equity issuer {issuer_id:?} has invalid {channel} value {value} at flat index {index}"
    )]
    InvalidPrivateEquityChannel {
        issuer_id: String,
        channel: &'static str,
        index: usize,
        value: i64,
    },
    #[error("private-equity issuer {issuer_id:?} is held by multiple agents")]
    MixedPrivateEquityOwners { issuer_id: String },
    #[error("harvest policy {policy_index} has invalid configuration")]
    InvalidHarvestPolicy { policy_index: usize },
    #[error(transparent)]
    Allocation(#[from] AllocationError),
    #[error(transparent)]
    Ledger(#[from] LedgerError),
    #[error(transparent)]
    Arithmetic(#[from] ArithmeticError),
    #[error(transparent)]
    Tax(#[from] TaxError),
}

#[derive(Clone, Debug)]
struct LotState {
    spec: InitialLotSpec,
    fifo_rank: i64,
    units_remaining: Quantity,
    basis_remaining: Money,
    basis_per_unit: Money,
}

#[derive(Clone, Debug)]
struct PlannedDisposition {
    lot_index: usize,
    units: Quantity,
    basis: Money,
    proceeds: Money,
    realized_gain: Money,
}

#[derive(Clone, Debug)]
struct ScheduledTlhGiveBack {
    cumulative_start: Vec<Money>,
    pre_sale_units: Vec<i64>,
    allocated: Vec<Money>,
}

#[derive(Clone, Debug)]
struct PendingAllocationBuy {
    policy_index: usize,
    sleeve_index: usize,
    wanted_units: i64,
    unit_price: i64,
}

#[derive(Clone, Debug)]
enum ObligationEffect {
    None,
    TaxPayment {
        profile_index: usize,
    },
    TaxTrueUp {
        profile_index: usize,
        tax_year_end_month: u32,
    },
    Mortgage {
        mortgage_index: usize,
        interest: Money,
        principal: Money,
    },
    PropertyTax {
        owner_agent_id: String,
        rented_fraction_ppb: i64,
    },
}

#[derive(Clone, Debug)]
struct ActiveObligation {
    cause_id: String,
    obligation_type: String,
    from: AccountRef,
    to: AccountRef,
    amount_due: Money,
    effect: ObligationEffect,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CaptureMode {
    Summary,
    Dense,
    Forensic,
}

impl CaptureMode {
    fn captures_output(self) -> bool {
        self != Self::Summary
    }

    fn captures_journal(self) -> bool {
        self == Self::Forensic
    }
}

#[derive(Debug)]
struct Recorder {
    capture_mode: CaptureMode,
    months: Vec<MonthOutput>,
    journal: Vec<JournalEntry>,
    transfers: Vec<TransferOutcome>,
    dispositions: Vec<LotDisposition>,
    private_equity_events: Vec<PrivateEquityProtocolOutcome>,
    private_equity_opportunities: Vec<PrivateEquityOpportunityOutcome>,
    obligations: Vec<ObligationOutcome>,
    rollout_failures: Vec<RolloutFailureOutcome>,
    tax_accruals: Vec<TaxAccrual>,
    tax_payments: Vec<TaxPaymentOutcome>,
    tax_settlements: Vec<TaxSettlementOutcome>,
    bond_cashflows: Vec<BondCashflowOutcome>,
    distributions: Vec<DistributionOutcome>,
    property_purchases: Vec<PropertyPurchaseOutcome>,
    primary_residence_events: Vec<PrimaryResidenceOutcome>,
    property_rented_fraction_events: Vec<PropertyRentedFractionOutcome>,
    capital_improvements: Vec<CapitalImprovementOutcome>,
    property_sales: Vec<PropertySaleOutcome>,
    mortgage_originations: Vec<MortgageOriginationOutcome>,
    mortgage_payments: Vec<MortgagePaymentOutcome>,
    journal_entry_count: u64,
    disposition_count: u64,
    private_equity_event_count: u64,
    private_equity_opportunity_count: u64,
    tax_accrual_count: u64,
    tax_payment_count: u64,
    tax_settlement_count: u64,
    bond_cashflow_count: u64,
    distribution_count: u64,
    property_purchase_count: u64,
    primary_residence_event_count: u64,
    property_rented_fraction_event_count: u64,
    capital_improvement_count: u64,
    property_sale_count: u64,
    mortgage_payment_count: u64,
}

impl Recorder {
    fn new(capture_mode: CaptureMode) -> Self {
        Self {
            capture_mode,
            months: Vec::new(),
            journal: Vec::new(),
            transfers: Vec::new(),
            dispositions: Vec::new(),
            private_equity_events: Vec::new(),
            private_equity_opportunities: Vec::new(),
            obligations: Vec::new(),
            rollout_failures: Vec::new(),
            tax_accruals: Vec::new(),
            tax_payments: Vec::new(),
            tax_settlements: Vec::new(),
            bond_cashflows: Vec::new(),
            distributions: Vec::new(),
            property_purchases: Vec::new(),
            primary_residence_events: Vec::new(),
            property_rented_fraction_events: Vec::new(),
            capital_improvements: Vec::new(),
            property_sales: Vec::new(),
            mortgage_originations: Vec::new(),
            mortgage_payments: Vec::new(),
            journal_entry_count: 0,
            disposition_count: 0,
            private_equity_event_count: 0,
            private_equity_opportunity_count: 0,
            tax_accrual_count: 0,
            tax_payment_count: 0,
            tax_settlement_count: 0,
            bond_cashflow_count: 0,
            distribution_count: 0,
            property_purchase_count: 0,
            primary_residence_event_count: 0,
            property_rented_fraction_event_count: 0,
            capital_improvement_count: 0,
            property_sale_count: 0,
            mortgage_payment_count: 0,
        }
    }

    fn apply_entry(
        &mut self,
        ledger: &mut Ledger,
        entry: JournalEntry,
    ) -> Result<(), SimulationError> {
        ledger.apply(&entry)?;
        self.journal_entry_count =
            self.journal_entry_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "journal entry count",
                })?;
        if self.capture_mode.captures_journal() {
            self.journal.push(entry);
        }
        Ok(())
    }

    fn record_disposition(&mut self, disposition: LotDisposition) -> Result<(), SimulationError> {
        self.disposition_count =
            self.disposition_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "disposition count",
                })?;
        if self.capture_mode.captures_output() {
            self.dispositions.push(disposition);
        }
        Ok(())
    }

    fn record_private_equity_event(
        &mut self,
        event: PrivateEquityProtocolOutcome,
    ) -> Result<(), SimulationError> {
        self.private_equity_event_count =
            self.private_equity_event_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "private-equity event count",
                })?;
        if self.capture_mode.captures_output() {
            self.private_equity_events.push(event);
        }
        Ok(())
    }

    fn record_private_equity_opportunity(
        &mut self,
        opportunity: PrivateEquityOpportunityOutcome,
    ) -> Result<(), SimulationError> {
        self.private_equity_opportunity_count = self
            .private_equity_opportunity_count
            .checked_add(1)
            .ok_or(ArithmeticError::Overflow {
                operation: "private-equity opportunity count",
            })?;
        if self.capture_mode.captures_output() {
            self.private_equity_opportunities.push(opportunity);
        }
        Ok(())
    }

    fn record_transfer(&mut self, transfer: TransferOutcome) {
        if self.capture_mode.captures_output() {
            self.transfers.push(transfer);
        }
    }

    fn record_obligation(&mut self, obligation: ObligationOutcome) {
        if self.capture_mode.captures_output() {
            self.obligations.push(obligation);
        }
    }

    fn record_rollout_failure(&mut self, failure: RolloutFailureOutcome) {
        if self.capture_mode.captures_output() {
            self.rollout_failures.push(failure);
        }
    }

    fn record_tax_accrual(&mut self, accrual: TaxAccrual) -> Result<(), SimulationError> {
        self.tax_accrual_count =
            self.tax_accrual_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "tax accrual count",
                })?;
        if self.capture_mode.captures_output() {
            self.tax_accruals.push(accrual);
        }
        Ok(())
    }

    fn record_tax_payment(&mut self, payment: TaxPaymentOutcome) -> Result<(), SimulationError> {
        self.tax_payment_count =
            self.tax_payment_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "tax payment count",
                })?;
        if self.capture_mode.captures_output() {
            self.tax_payments.push(payment);
        }
        Ok(())
    }

    fn record_tax_settlement(
        &mut self,
        settlement: TaxSettlementOutcome,
    ) -> Result<(), SimulationError> {
        self.tax_settlement_count =
            self.tax_settlement_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "tax settlement count",
                })?;
        if self.capture_mode.captures_output() {
            self.tax_settlements.push(settlement);
        }
        Ok(())
    }

    fn record_distribution(
        &mut self,
        distribution: DistributionOutcome,
    ) -> Result<(), SimulationError> {
        self.distribution_count =
            self.distribution_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "distribution count",
                })?;
        if self.capture_mode.captures_output() {
            self.distributions.push(distribution);
        }
        Ok(())
    }

    fn record_bond_cashflow(
        &mut self,
        cashflow: BondCashflowOutcome,
    ) -> Result<(), SimulationError> {
        self.bond_cashflow_count =
            self.bond_cashflow_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "bond cashflow count",
                })?;
        if self.capture_mode.captures_output() {
            self.bond_cashflows.push(cashflow);
        }
        Ok(())
    }

    fn record_property_purchase(
        &mut self,
        purchase: PropertyPurchaseOutcome,
        origination: Option<MortgageOriginationOutcome>,
    ) -> Result<(), SimulationError> {
        self.property_purchase_count =
            self.property_purchase_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "property purchase count",
                })?;
        if self.capture_mode.captures_output() {
            self.property_purchases.push(purchase);
            if let Some(origination) = origination {
                self.mortgage_originations.push(origination);
            }
        }
        Ok(())
    }

    fn record_property_sale(&mut self, sale: PropertySaleOutcome) -> Result<(), SimulationError> {
        self.property_sale_count =
            self.property_sale_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "property sale count",
                })?;
        if self.capture_mode.captures_output() {
            self.property_sales.push(sale);
        }
        Ok(())
    }

    fn record_primary_residence(
        &mut self,
        event: PrimaryResidenceOutcome,
    ) -> Result<(), SimulationError> {
        self.primary_residence_event_count = self
            .primary_residence_event_count
            .checked_add(1)
            .ok_or(ArithmeticError::Overflow {
                operation: "primary-residence event count",
            })?;
        if self.capture_mode.captures_output() {
            self.primary_residence_events.push(event);
        }
        Ok(())
    }

    fn record_property_rented_fraction(
        &mut self,
        event: PropertyRentedFractionOutcome,
    ) -> Result<(), SimulationError> {
        self.property_rented_fraction_event_count = self
            .property_rented_fraction_event_count
            .checked_add(1)
            .ok_or(ArithmeticError::Overflow {
                operation: "property rented-fraction event count",
            })?;
        if self.capture_mode.captures_output() {
            self.property_rented_fraction_events.push(event);
        }
        Ok(())
    }

    fn record_capital_improvement(
        &mut self,
        event: CapitalImprovementOutcome,
    ) -> Result<(), SimulationError> {
        self.capital_improvement_count =
            self.capital_improvement_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "capital improvement count",
                })?;
        if self.capture_mode.captures_output() {
            self.capital_improvements.push(event);
        }
        Ok(())
    }

    fn record_mortgage_payment(
        &mut self,
        payment: MortgagePaymentOutcome,
    ) -> Result<(), SimulationError> {
        self.mortgage_payment_count =
            self.mortgage_payment_count
                .checked_add(1)
                .ok_or(ArithmeticError::Overflow {
                    operation: "mortgage payment count",
                })?;
        if self.capture_mode.captures_output() {
            self.mortgage_payments.push(payment);
        }
        Ok(())
    }

    fn record_month(&mut self, month: MonthOutput) {
        if self.capture_mode.captures_output() {
            self.months.push(month);
        }
    }
}

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
            simulate_rollout(fixture.fixture, rollout_id, capture_mode)
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
            simulate_rollout(fixture.fixture, rollout_id, CaptureMode::Summary)
                .map(RolloutComputation::into_summary)
        })
        .collect();
    Ok(PopulationOutput {
        schema_version: FIXTURE_SCHEMA_VERSION,
        rollouts: rollouts?,
    })
}

fn validate_fixture(fixture: &Fixture) -> Result<(), SimulationError> {
    if fixture.schema_version != FIXTURE_SCHEMA_VERSION {
        return Err(SimulationError::SchemaVersion {
            actual: fixture.schema_version,
            expected: FIXTURE_SCHEMA_VERSION,
        });
    }
    if fixture.rollout_count == 0 {
        return Err(SimulationError::EmptyRollouts);
    }
    if fixture.scenario.horizon_months == 0 {
        return Err(SimulationError::EmptyHorizon);
    }
    if fixture.currency_code.len() != 3
        || !fixture
            .currency_code
            .bytes()
            .all(|byte| byte.is_ascii_uppercase())
    {
        return Err(SimulationError::InvalidCurrencyCode {
            currency_code: fixture.currency_code.clone(),
        });
    }
    if !is_positive_decimal(&fixture.currency_quantum) {
        return Err(SimulationError::InvalidCurrencyQuantum {
            currency_quantum: fixture.currency_quantum.clone(),
        });
    }
    let snapshots = fixture
        .scenario
        .horizon_months
        .checked_add(1)
        .ok_or(SimulationError::FixtureDimensions)?;
    let expected = usize::try_from(u64::from(fixture.rollout_count) * u64::from(snapshots))
        .map_err(|_| SimulationError::FixtureDimensions)?;
    let mut series_ids = BTreeSet::new();
    for series in &fixture.series {
        validate_identifier("series", &series.series_id)?;
        if !series_ids.insert(series.series_id.clone()) {
            return Err(SimulationError::DuplicateSeries {
                series_id: series.series_id.clone(),
            });
        }
        if series.snapshots != snapshots || series.values.len() != expected {
            return Err(SimulationError::SeriesShape {
                series_id: series.series_id.clone(),
                actual: series.values.len(),
                expected,
            });
        }
        if (series.series_id.starts_with("security:")
            || series.series_id.starts_with("security_distribution:")
            || series.series_id.starts_with("home_value:"))
            && let Some((index, value)) = series
                .values
                .iter()
                .copied()
                .enumerate()
                .find(|(_, value)| *value <= 0)
        {
            return Err(SimulationError::InvalidSecurityPrice {
                series_id: series.series_id.clone(),
                index,
                value,
            });
        }
    }

    let mut accounts = BTreeSet::new();
    let mut agents = BTreeSet::new();
    for account in &fixture.scenario.accounts {
        agents.insert(account.account.agent_id.clone());
        if !accounts.insert(account.account.clone()) {
            return Err(SimulationError::DuplicateAccount {
                agent_id: account.account.agent_id.clone(),
                account_id: account.account.account_id.clone(),
            });
        }
    }
    let mut jurisdiction_levels = BTreeMap::new();
    for jurisdiction in &fixture.scenario.jurisdictions {
        validate_identifier("jurisdiction", &jurisdiction.jurisdiction_id)?;
        if jurisdiction_levels
            .insert(jurisdiction.jurisdiction_id.clone(), jurisdiction.level)
            .is_some()
        {
            return Err(SimulationError::DuplicateJurisdictionIdentity {
                jurisdiction_id: jurisdiction.jurisdiction_id.clone(),
            });
        }
    }
    for transfer in &fixture.scenario.scheduled_transfers {
        validate_identifier("scheduled transfer", &transfer.cause_id)?;
        validate_event_month(
            "scheduled transfer",
            &transfer.cause_id,
            transfer.month,
            fixture.scenario.horizon_months,
        )?;
        validate_amount_spec(
            fixture,
            "scheduled transfer",
            &transfer.cause_id,
            &transfer.amount,
            std::iter::once(transfer.month),
        )?;
        validate_income_category(transfer.income_category.as_deref())?;
        validate_income_category(transfer.deduction_category.as_deref())?;
        validate_account(&accounts, &transfer.from, &transfer.cause_id)?;
        validate_account(&accounts, &transfer.to, &transfer.cause_id)?;
    }
    for transfer in &fixture.scenario.recurring_transfers {
        validate_identifier("recurring transfer", &transfer.cause_id)?;
        validate_event_month(
            "recurring transfer",
            &transfer.cause_id,
            transfer.start_month,
            fixture.scenario.horizon_months,
        )?;
        if let Some(end_month) = transfer.end_month
            && end_month < transfer.start_month
        {
            return Err(SimulationError::InvalidRecurringRange {
                kind: "recurring transfer",
                cause_id: transfer.cause_id.clone(),
                start_month: transfer.start_month,
                end_month,
            });
        }
        validate_amount_spec(
            fixture,
            "recurring transfer",
            &transfer.cause_id,
            &transfer.amount,
            transfer.start_month
                ..=transfer
                    .end_month
                    .unwrap_or(fixture.scenario.horizon_months - 1)
                    .min(fixture.scenario.horizon_months - 1),
        )?;
        validate_income_category(transfer.income_category.as_deref())?;
        validate_income_category(transfer.deduction_category.as_deref())?;
        validate_account(&accounts, &transfer.from, &transfer.cause_id)?;
        validate_account(&accounts, &transfer.to, &transfer.cause_id)?;
    }
    for obligation in &fixture.scenario.obligations {
        validate_identifier("obligation", &obligation.obligation_id)?;
        validate_identifier("obligation type", &obligation.obligation_type)?;
        validate_event_month(
            "obligation",
            &obligation.obligation_id,
            obligation.month,
            fixture.scenario.horizon_months,
        )?;
        validate_amount_spec(
            fixture,
            "obligation",
            &obligation.obligation_id,
            &obligation.amount_due,
            std::iter::once(obligation.month),
        )?;
        validate_account(&accounts, &obligation.from, &obligation.obligation_id)?;
        validate_account(&accounts, &obligation.to, &obligation.obligation_id)?;
    }
    for obligation in &fixture.scenario.recurring_obligations {
        validate_identifier("recurring obligation", &obligation.obligation_id)?;
        validate_identifier("obligation type", &obligation.obligation_type)?;
        validate_event_month(
            "recurring obligation",
            &obligation.obligation_id,
            obligation.start_month,
            fixture.scenario.horizon_months,
        )?;
        if let Some(end_month) = obligation.end_month
            && end_month < obligation.start_month
        {
            return Err(SimulationError::InvalidRecurringRange {
                kind: "recurring obligation",
                cause_id: obligation.obligation_id.clone(),
                start_month: obligation.start_month,
                end_month,
            });
        }
        validate_amount_spec(
            fixture,
            "recurring obligation",
            &obligation.obligation_id,
            &obligation.amount_due,
            obligation.start_month
                ..=obligation
                    .end_month
                    .unwrap_or(fixture.scenario.horizon_months - 1)
                    .min(fixture.scenario.horizon_months - 1),
        )?;
        validate_account(&accounts, &obligation.from, &obligation.obligation_id)?;
        validate_account(&accounts, &obligation.to, &obligation.obligation_id)?;
    }

    let mut lots = BTreeSet::new();
    let mut pool_scales = BTreeMap::new();
    for lot in &fixture.scenario.initial_lots {
        validate_identifier("lot", &lot.lot_id)?;
        validate_identifier("asset", &lot.asset_id)?;
        if lot.quantity_scale <= 0 {
            return Err(SimulationError::InvalidQuantityScale {
                lot_id: lot.lot_id.clone(),
                quantity_scale: lot.quantity_scale,
            });
        }
        if lot.units.0 <= 0 || lot.basis.0 < 0 {
            return Err(SimulationError::InvalidLot {
                lot_id: lot.lot_id.clone(),
                units: lot.units.0,
                basis: lot.basis.0,
            });
        }
        if i128::from(lot.basis.0) * i128::from(lot.quantity_scale) % i128::from(lot.units.0) != 0 {
            return Err(SimulationError::InexactLotBasis {
                lot_id: lot.lot_id.clone(),
            });
        }
        if !lots.insert(lot.lot_id.clone()) {
            return Err(SimulationError::DuplicateLot {
                lot_id: lot.lot_id.clone(),
            });
        }
        let pool = (
            lot.agent_id.clone(),
            lot.account_id.clone(),
            lot.asset_id.clone(),
        );
        if let Some(first_scale) = pool_scales.insert(pool, lot.quantity_scale)
            && first_scale != lot.quantity_scale
        {
            return Err(SimulationError::MixedQuantityScale {
                agent_id: lot.agent_id.clone(),
                account_id: lot.account_id.clone(),
                asset_id: lot.asset_id.clone(),
                first_scale,
                second_scale: lot.quantity_scale,
            });
        }
        if !agents.contains(&lot.agent_id) {
            return Err(SimulationError::UnknownAccountReference {
                context: format!("lot {:?}", lot.lot_id),
                agent_id: lot.agent_id.clone(),
                account_id: lot.account_id.clone(),
            });
        }
    }
    let mut private_equity_issuers = BTreeMap::<String, String>::new();
    for lot in &fixture.scenario.initial_lots {
        let Some(issuer_id) = private_equity_issuer(&lot.asset_id) else {
            continue;
        };
        if let Some(first_owner) =
            private_equity_issuers.insert(issuer_id.to_owned(), lot.agent_id.clone())
            && first_owner != lot.agent_id
        {
            return Err(SimulationError::MixedPrivateEquityOwners {
                issuer_id: issuer_id.to_owned(),
            });
        }
    }
    let mut pe_policy_owners = BTreeSet::new();
    for policy in &fixture.scenario.private_equity_tender_policies {
        if !agents.contains(&policy.owner_agent_id)
            || !pe_policy_owners.insert(policy.owner_agent_id.clone())
        {
            return Err(SimulationError::InvalidPrivateEquityPolicy {
                owner_agent_id: policy.owner_agent_id.clone(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&policy.owner_agent_id, &policy.proceeds_account_id),
            "private-equity proceeds",
        )?;
        validate_amount_spec(
            fixture,
            "private-equity policy",
            &policy.owner_agent_id,
            &policy.liquid_net_worth_floor,
            0..fixture.scenario.horizon_months,
        )?;
    }
    for issuer_id in private_equity_issuers.keys() {
        validate_private_equity_channels(fixture, issuer_id)?;
    }
    for (policy_index, policy) in fixture.scenario.harvest_policies.iter().enumerate() {
        let valid = agents.contains(&policy.owner_agent_id)
            && policy.peak_annual_yield_ppb > 0
            && policy.floor_annual_yield_ppb >= 0
            && policy.floor_annual_yield_ppb <= policy.peak_annual_yield_ppb
            && policy.maturity_decay_exponent_ppb > 0
            && policy.drawdown_sensitivity_ppb >= 0
            && (0..=RATE_SCALE_PPB).contains(&policy.short_term_fraction_ppb)
            && private_equity_issuer(&policy.asset_id).is_none()
            && fixture
                .series
                .iter()
                .any(|series| series.series_id == format!("security:{}", policy.asset_id));
        if !valid {
            return Err(SimulationError::InvalidHarvestPolicy { policy_index });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&policy.owner_agent_id, &policy.account_id),
            "harvest policy",
        )?;
    }
    for policy in &fixture.scenario.target_allocation_policies {
        if policy.purchase_slots_per_sleeve == 0 {
            continue;
        }
        let account_id = policy
            .source_account_ids
            .first()
            .unwrap_or(&policy.account_id);
        for sleeve in &policy.sleeves {
            let pool = (
                policy.agent_id.clone(),
                account_id.clone(),
                sleeve.asset_id.clone(),
            );
            if let Some(first_scale) = pool_scales.insert(pool, sleeve.quantity_scale)
                && first_scale != sleeve.quantity_scale
            {
                return Err(SimulationError::MixedQuantityScale {
                    agent_id: policy.agent_id.clone(),
                    account_id: account_id.clone(),
                    asset_id: sleeve.asset_id.clone(),
                    first_scale,
                    second_scale: sleeve.quantity_scale,
                });
            }
        }
    }
    let mut bonds = BTreeSet::new();
    for bond in &fixture.scenario.initial_bonds {
        validate_identifier("bond", &bond.bond_id)?;
        if !bonds.insert(bond.bond_id.clone()) {
            return Err(SimulationError::DuplicateBond {
                bond_id: bond.bond_id.clone(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&bond.agent_id, &bond.account_id),
            &format!("bond {:?}", bond.bond_id),
        )?;
        if let Some(issuer) = &bond.issuer_jurisdiction_id
            && !jurisdiction_levels.contains_key(issuer)
        {
            return Err(SimulationError::UnknownBondIssuer {
                bond_id: bond.bond_id.clone(),
                jurisdiction_id: issuer.clone(),
            });
        }
        let Some(term_months) = bond
            .maturity_month_index
            .checked_sub(bond.purchase_month_index)
        else {
            return Err(SimulationError::InvalidBondTerms {
                bond_id: bond.bond_id.clone(),
            });
        };
        if bond.face_value.0 <= 0
            || bond.purchase_price != bond.face_value
            || bond.annual_coupon_rate_ppb < 0
            || bond.coupon_period_months == 0
            || term_months <= 0
            || i64::from(term_months) % i64::from(bond.coupon_period_months) != 0
        {
            return Err(SimulationError::InvalidBondTerms {
                bond_id: bond.bond_id.clone(),
            });
        }
        let reconstructed_rate = ((bond.annual_coupon_rate_ppb as f64 / RATE_SCALE as f64)
            * RATE_SCALE as f64)
            .round() as i64;
        if bond.annual_coupon_rate_ppb > MAX_EXACT_F64_INTEGER
            || reconstructed_rate != bond.annual_coupon_rate_ppb
        {
            return Err(SimulationError::InexactBondCouponRate {
                bond_id: bond.bond_id.clone(),
                rate_ppb: bond.annual_coupon_rate_ppb,
            });
        }
        if bond.inflation_indexed {
            let exact_period_rate = bond_period_rate_ppb(bond)?;
            let legacy_period_rate = (((bond.annual_coupon_rate_ppb as f64 / RATE_SCALE as f64)
                * f64::from(bond.coupon_period_months)
                / 12.0
                * RATE_SCALE as f64)
                + 0.5)
                .floor() as i64;
            if exact_period_rate != legacy_period_rate {
                return Err(SimulationError::InexactBondPeriodRate {
                    bond_id: bond.bond_id.clone(),
                });
            }
            if !series_ids.contains("inflation") {
                return Err(SimulationError::MissingBondInflationSeries {
                    bond_id: bond.bond_id.clone(),
                });
            }
            let base_month = bond.purchase_month_index.max(0) as u32;
            if base_month > fixture.scenario.horizon_months {
                return Err(SimulationError::InvalidBondTerms {
                    bond_id: bond.bond_id.clone(),
                });
            }
            for rollout in 0..fixture.rollout_count {
                for snapshot in 0..=fixture.scenario.horizon_months {
                    let level = fixture
                        .series
                        .iter()
                        .find(|series| series.series_id == "inflation")
                        .and_then(|series| series.value(rollout, snapshot))
                        .ok_or_else(|| SimulationError::MissingSeriesValue {
                            series_id: "inflation".into(),
                            rollout,
                            snapshot,
                        })?;
                    validate_amount_index_level(
                        &bond.bond_id,
                        "inflation",
                        rollout,
                        snapshot,
                        level,
                    )?;
                }
            }
        }
    }
    for sale in &fixture.scenario.scheduled_sales {
        validate_identifier("sale", &sale.cause_id)?;
        validate_identifier("asset", &sale.asset_id)?;
        validate_event_month(
            "sale",
            &sale.cause_id,
            sale.month,
            fixture.scenario.horizon_months,
        )?;
        if sale.units.0 <= 0 {
            return Err(SimulationError::InvalidSaleUnits {
                cause_id: sale.cause_id.clone(),
                units: sale.units.0,
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&sale.agent_id, &sale.proceeds_account_id),
            &sale.cause_id,
        )?;
        if !pool_scales.contains_key(&(
            sale.agent_id.clone(),
            sale.account_id.clone(),
            sale.asset_id.clone(),
        )) {
            return Err(SimulationError::MissingSalePool {
                cause_id: sale.cause_id.clone(),
                agent_id: sale.agent_id.clone(),
                account_id: sale.account_id.clone(),
                asset_id: sale.asset_id.clone(),
            });
        }
        let series_id = format!("security:{}", sale.asset_id);
        if !series_ids.contains(&series_id) {
            return Err(SimulationError::MissingSeries { series_id });
        }
    }
    let mut distributions = BTreeSet::new();
    for distribution in &fixture.scenario.distributions {
        validate_identifier("distribution asset", &distribution.asset_id)?;
        let pool = (
            distribution.agent_id.clone(),
            distribution.holding_account_id.clone(),
            distribution.asset_id.clone(),
        );
        if !distributions.insert(pool.clone()) {
            return Err(SimulationError::DuplicateDistribution {
                agent_id: pool.0,
                account_id: pool.1,
                asset_id: pool.2,
            });
        }
        if !pool_scales.contains_key(&pool) {
            return Err(SimulationError::MissingDistributionPool {
                agent_id: pool.0,
                account_id: pool.1,
                asset_id: pool.2,
            });
        }
        let fraction_total = distribution
            .tax_character
            .iter()
            .map(|slice| i128::from(slice.fraction_ppb))
            .sum::<i128>();
        if distribution.tax_character.is_empty()
            || distribution.tax_character.iter().any(|slice| {
                let reconstructed = ((slice.fraction_ppb as f64 / RATE_SCALE as f64)
                    * RATE_SCALE as f64)
                    .round() as i64;
                slice.fraction_ppb <= 0 || reconstructed != slice.fraction_ppb
            })
            || fraction_total != i128::from(RATE_SCALE)
        {
            return Err(SimulationError::InvalidDistributionTaxCharacter {
                agent_id: distribution.agent_id.clone(),
                account_id: distribution.holding_account_id.clone(),
                asset_id: distribution.asset_id.clone(),
            });
        }
        for slice in &distribution.tax_character {
            if let Some(issuer) = &slice.issuer_jurisdiction_id
                && !jurisdiction_levels.contains_key(issuer)
            {
                return Err(SimulationError::UnknownDistributionIssuer {
                    agent_id: distribution.agent_id.clone(),
                    account_id: distribution.holding_account_id.clone(),
                    asset_id: distribution.asset_id.clone(),
                    jurisdiction_id: issuer.clone(),
                });
            }
        }
        validate_account(
            &accounts,
            &AccountRef::new(&distribution.agent_id, &distribution.to_account_id),
            "distribution destination",
        )?;
        let series_id = format!("security_distribution:{}", distribution.asset_id);
        if !series_ids.contains(&series_id) {
            return Err(SimulationError::MissingSeries { series_id });
        }
    }
    let mut target_allocation_accounts = BTreeSet::new();
    for (policy_index, policy) in fixture
        .scenario
        .target_allocation_policies
        .iter()
        .enumerate()
    {
        validate_identifier("target-allocation cause", &policy.cause_id_prefix)?;
        validate_account(
            &accounts,
            &AccountRef::new(&policy.agent_id, &policy.account_id),
            &policy.cause_id_prefix,
        )?;
        if !target_allocation_accounts.insert((policy.agent_id.clone(), policy.account_id.clone()))
        {
            return Err(SimulationError::DuplicateTargetAllocationPolicy {
                agent_id: policy.agent_id.clone(),
                account_id: policy.account_id.clone(),
            });
        }
        if let Some(tolerance) = policy.rebalance_tolerance_ppb {
            let reconstructed =
                ((tolerance as f64 / RATE_SCALE as f64) * RATE_SCALE as f64).round() as i64;
            if !(0..=MAX_EXACT_F64_INTEGER).contains(&tolerance)
                || reconstructed != tolerance
                || policy.purchase_slots_per_sleeve == 0
            {
                return Err(SimulationError::InvalidTargetAllocationPolicy {
                    agent_id: policy.agent_id.clone(),
                    account_id: policy.account_id.clone(),
                });
            }
        }
        if policy.sleeves.is_empty()
            || policy.cash_floor.base_amount().0 < 0
            || policy.cash_floor.base_amount().0 > policy.cash_ceiling.base_amount().0
        {
            return Err(SimulationError::InvalidTargetAllocationPolicy {
                agent_id: policy.agent_id.clone(),
                account_id: policy.account_id.clone(),
            });
        }
        validate_amount_spec(
            fixture,
            "target-allocation floor",
            &policy.cause_id_prefix,
            &policy.cash_floor,
            0..fixture.scenario.horizon_months,
        )?;
        validate_amount_spec(
            fixture,
            "target-allocation ceiling",
            &policy.cause_id_prefix,
            &policy.cash_ceiling,
            0..fixture.scenario.horizon_months,
        )?;
        let sources = if policy.source_account_ids.is_empty() {
            vec![policy.account_id.as_str()]
        } else {
            policy
                .source_account_ids
                .iter()
                .map(String::as_str)
                .collect()
        };
        if sources.iter().copied().collect::<BTreeSet<_>>().len() != sources.len() {
            return Err(SimulationError::InvalidTargetAllocationPolicy {
                agent_id: policy.agent_id.clone(),
                account_id: policy.account_id.clone(),
            });
        }
        let mut assets = BTreeSet::new();
        for (sleeve_index, sleeve) in policy.sleeves.iter().enumerate() {
            validate_identifier("target-allocation asset", &sleeve.asset_id)?;
            if sleeve.weight <= 0 || sleeve.quantity_scale <= 0 {
                return Err(SimulationError::InvalidTargetAllocationPolicy {
                    agent_id: policy.agent_id.clone(),
                    account_id: policy.account_id.clone(),
                });
            }
            if !assets.insert(sleeve.asset_id.clone()) {
                return Err(SimulationError::DuplicateTargetAllocationSleeve {
                    agent_id: policy.agent_id.clone(),
                    account_id: policy.account_id.clone(),
                    asset_id: sleeve.asset_id.clone(),
                });
            }
            let scales: BTreeSet<_> = sources
                .iter()
                .filter_map(|source| {
                    pool_scales
                        .get(&(
                            policy.agent_id.clone(),
                            (*source).to_owned(),
                            sleeve.asset_id.clone(),
                        ))
                        .copied()
                })
                .collect();
            if scales.len() > 1
                || scales
                    .iter()
                    .next()
                    .is_some_and(|scale| *scale != sleeve.quantity_scale)
            {
                return Err(SimulationError::InvalidTargetAllocationPolicy {
                    agent_id: policy.agent_id.clone(),
                    account_id: policy.account_id.clone(),
                });
            }
            for slot_index in 0..policy.purchase_slots_per_sleeve {
                let lot_id = format!(
                    "{}_buy_p{policy_index}_s{sleeve_index}_{slot_index}",
                    policy.cause_id_prefix
                );
                if !lots.insert(lot_id.clone()) {
                    return Err(SimulationError::DuplicateLot { lot_id });
                }
            }
        }
    }
    let mut tax_jurisdictions = BTreeSet::new();
    for profile in &fixture.scenario.tax_profiles {
        if profile.jurisdictions.is_empty() {
            return Err(SimulationError::EmptyTaxProfile {
                agent_id: profile.agent_id.clone(),
            });
        }
        if profile.prior_year_tax.0 < 0 || profile.section_121_exclusion.0 < 0 {
            return Err(SimulationError::InvalidAmount {
                kind: "tax profile prior-year tax",
                cause_id: profile.agent_id.clone(),
                amount: profile.prior_year_tax.0,
            });
        }
        validate_identifier("tax profile agent", &profile.agent_id)?;
        if !agents.contains(&profile.agent_id) {
            return Err(SimulationError::UnknownAccountReference {
                context: "tax profile".into(),
                agent_id: profile.agent_id.clone(),
                account_id: "checking".into(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&profile.agent_id, &profile.payment_account_id),
            "tax profile payment account",
        )?;
        validate_account(
            &accounts,
            &AccountRef::new(
                &profile.tax_authority_agent_id,
                &profile.tax_authority_account_id,
            ),
            "tax profile authority account",
        )?;
        for rules in &profile.jurisdictions {
            validate_identifier("tax jurisdiction", &rules.jurisdiction_id)?;
            validate_rules(rules)?;
            if !tax_jurisdictions.insert((profile.agent_id.clone(), rules.jurisdiction_id.clone()))
            {
                return Err(SimulationError::DuplicateTaxJurisdiction {
                    agent_id: profile.agent_id.clone(),
                    jurisdiction_id: rules.jurisdiction_id.clone(),
                });
            }
        }
    }
    let mut salt_profiles = BTreeSet::new();
    for policy in &fixture.scenario.federal_salt_deduction_policies {
        let Some(profile) = fixture
            .scenario
            .tax_profiles
            .iter()
            .find(|profile| profile.agent_id == policy.profile_id)
        else {
            return Err(SimulationError::InvalidSaltPolicy {
                profile_id: policy.profile_id.clone(),
            });
        };
        if !salt_profiles.insert(policy.profile_id.as_str())
            || !profile
                .jurisdictions
                .iter()
                .any(|rules| rules.jurisdiction_id == policy.federal_jurisdiction_id)
            || policy.cap_schedule.iter().any(|entry| entry.cap.0 < 0)
        {
            return Err(SimulationError::InvalidSaltPolicy {
                profile_id: policy.profile_id.clone(),
            });
        }
    }
    let mut locations = BTreeSet::new();
    for location in &fixture.scenario.locations {
        validate_identifier("location", &location.location_id)?;
        if !locations.insert(location.location_id.clone()) {
            return Err(SimulationError::DuplicateLocation {
                location_id: location.location_id.clone(),
            });
        }
        if location.annual_property_tax_rate_ppb < 0 || location.annual_special_assessment.0 < 0 {
            return Err(SimulationError::InvalidPropertyTaxPolicy {
                property_id: location.location_id.clone(),
            });
        }
    }
    let mut properties = BTreeMap::new();
    let mut mortgages = BTreeSet::new();
    let mut mortgage_owners = BTreeMap::new();
    for purchase in &fixture.scenario.scheduled_property_purchases {
        validate_identifier("property purchase", &purchase.cause_id)?;
        validate_identifier("property", &purchase.property_id)?;
        validate_event_month(
            "property purchase",
            &purchase.cause_id,
            purchase.month,
            fixture.scenario.horizon_months,
        )?;
        if !locations.contains(&purchase.location_id) {
            return Err(SimulationError::UnknownLocation {
                cause_id: purchase.cause_id.clone(),
                location_id: purchase.location_id.clone(),
            });
        }
        if properties
            .insert(purchase.property_id.clone(), purchase)
            .is_some()
        {
            return Err(SimulationError::DuplicateProperty {
                property_id: purchase.property_id.clone(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id),
            &purchase.cause_id,
        )?;
        validate_account(
            &accounts,
            &AccountRef::new(&purchase.seller_agent_id, &purchase.seller_account_id),
            &purchase.cause_id,
        )?;
        let principal = purchase
            .mortgage
            .as_ref()
            .map_or(Money(0), |mortgage| mortgage.principal);
        if purchase.purchase_price.0 <= 0
            || purchase.down_payment.0 < 0
            || purchase.buyer_closing_cost.0 < 0
            || !(0..=RATE_SCALE_PPB).contains(&purchase.rented_fraction_ppb)
            || !(0..=RATE_SCALE_PPB).contains(&purchase.land_value_fraction_ppb)
            || purchase.down_payment.checked_add(principal)? != purchase.purchase_price
        {
            return Err(SimulationError::InvalidPropertyTerms {
                cause_id: purchase.cause_id.clone(),
            });
        }
        if let Some(mortgage) = &purchase.mortgage {
            validate_identifier("mortgage", &mortgage.liability_id)?;
            if !mortgages.insert(mortgage.liability_id.clone()) {
                return Err(SimulationError::DuplicateMortgage {
                    liability_id: mortgage.liability_id.clone(),
                });
            }
            mortgage_owners.insert(
                mortgage.liability_id.clone(),
                purchase.buyer_agent_id.clone(),
            );
            if mortgage.principal.0 <= 0
                || mortgage.annual_interest_rate_ppb < 0
                || mortgage.annual_interest_rate_ppb > RATE_SCALE_PPB
                || mortgage.term_months == 0
            {
                return Err(SimulationError::InvalidMortgageTerms {
                    liability_id: mortgage.liability_id.clone(),
                });
            }
            validate_account(
                &accounts,
                &AccountRef::new(&mortgage.lender_agent_id, &mortgage.lender_account_id),
                &mortgage.liability_id,
            )?;
            mortgage_monthly_payment(
                mortgage.principal,
                mortgage.annual_interest_rate_ppb,
                mortgage.term_months,
            )?;
        }
    }
    let mut mortgage_interest_policies = BTreeSet::new();
    for policy in &fixture.scenario.mortgage_interest_deduction_policies {
        let Some(owner_agent_id) = mortgage_owners.get(&policy.liability_id) else {
            return Err(SimulationError::UnknownMortgageInterestPolicy {
                liability_id: policy.liability_id.clone(),
            });
        };
        if owner_agent_id != &policy.owner_agent_id {
            return Err(SimulationError::InvalidMortgageInterestPolicy {
                liability_id: policy.liability_id.clone(),
            });
        }
        if !matches!(policy.debt_class.as_str(), "acquisition" | "home_equity")
            || policy
                .per_jurisdiction_principal_cap
                .values()
                .any(|cap| cap.0 < 0)
        {
            return Err(SimulationError::InvalidMortgageInterestPolicy {
                liability_id: policy.liability_id.clone(),
            });
        }
        for jurisdiction_id in policy.per_jurisdiction_principal_cap.keys() {
            validate_identifier("mortgage-interest jurisdiction", jurisdiction_id)?;
        }
        if !mortgage_interest_policies.insert(policy.liability_id.clone()) {
            return Err(SimulationError::InvalidMortgageInterestPolicy {
                liability_id: policy.liability_id.clone(),
            });
        }
    }
    let mut property_sales = BTreeSet::new();
    for sale in &fixture.scenario.property_sales {
        validate_identifier("property sale", &sale.property_id)?;
        validate_event_month(
            "property sale",
            &sale.property_id,
            sale.month,
            fixture.scenario.horizon_months,
        )?;
        let Some(purchase) = properties.get(&sale.property_id) else {
            return Err(SimulationError::UnknownPropertySale {
                property_id: sale.property_id.clone(),
            });
        };
        if !property_sales.insert(sale.property_id.clone()) {
            return Err(SimulationError::DuplicatePropertySale {
                property_id: sale.property_id.clone(),
            });
        }
        if sale.month <= purchase.month || sale.closing_cost_bps > 10_000 {
            return Err(SimulationError::InvalidPropertySale {
                property_id: sale.property_id.clone(),
            });
        }
        let series_id = format!("home_value:{}", purchase.location_id);
        if !series_ids.contains(&series_id) {
            return Err(SimulationError::MissingSeries { series_id });
        }
    }
    let sale_month_by_property: BTreeMap<_, _> = fixture
        .scenario
        .property_sales
        .iter()
        .map(|sale| (sale.property_id.as_str(), sale.month))
        .collect();
    let mut primary_initial_agents = BTreeSet::new();
    for assignment in &fixture.scenario.initial_primary_residences {
        if !primary_initial_agents.insert(assignment.agent_id.as_str()) {
            return Err(SimulationError::DuplicatePrimaryResidence {
                agent_id: assignment.agent_id.clone(),
                month: 0,
            });
        }
        validate_primary_residence_assignment(
            &agents,
            &properties,
            &sale_month_by_property,
            &assignment.agent_id,
            &assignment.property_id,
            0,
        )?;
    }
    let mut primary_event_keys = BTreeSet::new();
    for event in &fixture.scenario.primary_residence_events {
        validate_event_month(
            "primary residence",
            &event.agent_id,
            event.month,
            fixture.scenario.horizon_months,
        )?;
        if !primary_event_keys.insert((event.agent_id.as_str(), event.month)) {
            return Err(SimulationError::DuplicatePrimaryResidence {
                agent_id: event.agent_id.clone(),
                month: event.month,
            });
        }
        if let Some(property_id) = event.property_id.as_deref() {
            validate_primary_residence_assignment(
                &agents,
                &properties,
                &sale_month_by_property,
                &event.agent_id,
                property_id,
                event.month,
            )?;
        } else if !agents.contains(&event.agent_id) {
            return Err(SimulationError::InvalidPrimaryResidence {
                agent_id: event.agent_id.clone(),
                property_id: String::new(),
                month: event.month,
            });
        }
    }
    for event in &fixture.scenario.property_rented_fraction_events {
        validate_property_lifecycle_event(
            &properties,
            &fixture.scenario.property_sales,
            &event.property_id,
            event.month,
            fixture.scenario.horizon_months,
        )?;
        if !(0..=RATE_SCALE_PPB).contains(&event.rented_fraction_ppb) {
            return Err(SimulationError::InvalidPropertyLifecycle {
                property_id: event.property_id.clone(),
            });
        }
    }
    for event in &fixture.scenario.capital_improvement_events {
        validate_property_lifecycle_event(
            &properties,
            &fixture.scenario.property_sales,
            &event.property_id,
            event.month,
            fixture.scenario.horizon_months,
        )?;
        if event.amount.0 <= 0 {
            return Err(SimulationError::InvalidPropertyLifecycle {
                property_id: event.property_id.clone(),
            });
        }
    }
    let mut property_tax_months = BTreeSet::new();
    for policy in &fixture.scenario.property_tax_policies {
        let Some(purchase) = properties.get(&policy.property_id) else {
            return Err(SimulationError::UnknownPropertyTaxProperty {
                property_id: policy.property_id.clone(),
            });
        };
        if purchase.buyer_agent_id != policy.owner_agent_id
            || policy.annual_tax_rate_ppb.is_some_and(|rate| rate < 0)
            || policy.start_month >= fixture.scenario.horizon_months
            || policy.end_month.is_some_and(|end| {
                end < policy.start_month || end >= fixture.scenario.horizon_months
            })
        {
            return Err(SimulationError::InvalidPropertyTaxPolicy {
                property_id: policy.property_id.clone(),
            });
        }
        validate_account(
            &accounts,
            &AccountRef::new(&policy.owner_agent_id, &policy.from_account_id),
            "property tax payer",
        )?;
        validate_account(
            &accounts,
            &AccountRef::new(
                &policy.tax_authority_agent_id,
                &policy.tax_authority_account_id,
            ),
            "property tax authority",
        )?;
        let end = policy
            .end_month
            .unwrap_or(fixture.scenario.horizon_months - 1);
        for month in policy.start_month..=end {
            if !property_tax_months.insert((policy.property_id.clone(), month)) {
                return Err(SimulationError::InvalidPropertyTaxPolicy {
                    property_id: policy.property_id.clone(),
                });
            }
        }
    }
    for cashflow in &fixture.scenario.scheduled_property_cashflows {
        validate_identifier("scheduled property cashflow", &cashflow.cause_id)?;
        validate_event_month(
            "scheduled property cashflow",
            &cashflow.cause_id,
            cashflow.month,
            fixture.scenario.horizon_months,
        )?;
        validate_amount_spec(
            fixture,
            "scheduled property cashflow",
            &cashflow.cause_id,
            &cashflow.amount,
            std::iter::once(cashflow.month),
        )?;
        validate_income_category(cashflow.income_category.as_deref())?;
        validate_income_category(cashflow.deduction_category.as_deref())?;
        validate_account(&accounts, &cashflow.from, &cashflow.cause_id)?;
        validate_account(&accounts, &cashflow.to, &cashflow.cause_id)?;
        if !properties.contains_key(&cashflow.property_id) {
            return Err(SimulationError::UnknownPropertyCashflow {
                cause_id: cashflow.cause_id.clone(),
                property_id: cashflow.property_id.clone(),
            });
        }
    }
    for cashflow in &fixture.scenario.recurring_property_cashflows {
        validate_identifier("recurring property cashflow", &cashflow.cause_id)?;
        validate_event_month(
            "recurring property cashflow",
            &cashflow.cause_id,
            cashflow.start_month,
            fixture.scenario.horizon_months,
        )?;
        if let Some(end_month) = cashflow.end_month
            && end_month < cashflow.start_month
        {
            return Err(SimulationError::InvalidRecurringRange {
                kind: "recurring property cashflow",
                cause_id: cashflow.cause_id.clone(),
                start_month: cashflow.start_month,
                end_month,
            });
        }
        validate_amount_spec(
            fixture,
            "recurring property cashflow",
            &cashflow.cause_id,
            &cashflow.amount,
            cashflow.start_month
                ..=cashflow
                    .end_month
                    .unwrap_or(fixture.scenario.horizon_months - 1)
                    .min(fixture.scenario.horizon_months - 1),
        )?;
        validate_income_category(cashflow.income_category.as_deref())?;
        validate_income_category(cashflow.deduction_category.as_deref())?;
        validate_account(&accounts, &cashflow.from, &cashflow.cause_id)?;
        validate_account(&accounts, &cashflow.to, &cashflow.cause_id)?;
        if !properties.contains_key(&cashflow.property_id) {
            return Err(SimulationError::UnknownPropertyCashflow {
                cause_id: cashflow.cause_id.clone(),
                property_id: cashflow.property_id.clone(),
            });
        }
    }
    Ok(())
}

fn validate_event_month(
    kind: &'static str,
    cause_id: &str,
    month: u32,
    horizon: u32,
) -> Result<(), SimulationError> {
    if month >= horizon {
        return Err(SimulationError::EventOutsideHorizon {
            kind,
            cause_id: cause_id.into(),
            month,
            horizon,
        });
    }
    Ok(())
}

fn validate_property_lifecycle_event(
    properties: &BTreeMap<String, &crate::fixture::ScheduledPropertyPurchaseSpec>,
    sales: &[PropertySaleSpec],
    property_id: &str,
    month: u32,
    horizon: u32,
) -> Result<(), SimulationError> {
    validate_identifier("property lifecycle", property_id)?;
    validate_event_month("property lifecycle", property_id, month, horizon)?;
    let Some(purchase) = properties.get(property_id) else {
        return Err(SimulationError::UnknownPropertyLifecycle {
            property_id: property_id.into(),
        });
    };
    if month <= purchase.month {
        return Err(SimulationError::InvalidPropertyLifecycle {
            property_id: property_id.into(),
        });
    }
    if sales
        .iter()
        .any(|sale| sale.property_id == property_id && month >= sale.month)
    {
        return Err(SimulationError::PropertyLifecycleAfterSale {
            property_id: property_id.into(),
        });
    }
    Ok(())
}

fn validate_primary_residence_assignment(
    agents: &BTreeSet<String>,
    properties: &BTreeMap<String, &crate::fixture::ScheduledPropertyPurchaseSpec>,
    sale_month_by_property: &BTreeMap<&str, u32>,
    agent_id: &str,
    property_id: &str,
    month: u32,
) -> Result<(), SimulationError> {
    let valid = agents.contains(agent_id)
        && properties.get(property_id).is_some_and(|purchase| {
            purchase.buyer_agent_id == agent_id
                && month >= purchase.month
                && sale_month_by_property
                    .get(property_id)
                    .is_none_or(|sale_month| month <= *sale_month)
        });
    if !valid {
        return Err(SimulationError::InvalidPrimaryResidence {
            agent_id: agent_id.into(),
            property_id: property_id.into(),
            month,
        });
    }
    Ok(())
}

fn validate_identifier(kind: &'static str, value: &str) -> Result<(), SimulationError> {
    if value.trim().is_empty() {
        return Err(SimulationError::EmptyIdentifier { kind });
    }
    Ok(())
}

fn private_equity_issuer(asset_id: &str) -> Option<&str> {
    asset_id
        .strip_prefix(PE_ASSET_PREFIX)
        .filter(|issuer_id| !issuer_id.is_empty())
}

fn canonical_lot_asset_id(asset_id: &str) -> String {
    if private_equity_issuer(asset_id).is_some() {
        asset_id.to_owned()
    } else {
        format!("security:{asset_id}")
    }
}

fn private_equity_series_id(channel: &str, issuer_id: &str) -> String {
    format!("private_equity_{channel}:{issuer_id}")
}

fn validate_private_equity_channels(
    fixture: &Fixture,
    issuer_id: &str,
) -> Result<(), SimulationError> {
    let channel = |name: &str| -> Result<&SeriesSpec, SimulationError> {
        let series_id = private_equity_series_id(name, issuer_id);
        fixture
            .series
            .iter()
            .find(|series| series.series_id == series_id)
            .ok_or_else(|| SimulationError::MissingPrivateEquitySeries {
                issuer_id: issuer_id.to_owned(),
                series_id,
            })
    };
    let mark = channel("mark")?;
    let regime = channel("regime")?;
    let event_kind = channel("event_kind")?;
    let opportunity = channel("sale_opportunity")?;
    let capacity = channel("sale_capacity")?;
    let eligible = channel("eligible")?;
    let forced_sale = channel("forced_sale")?;
    let blocked = channel("liquidity_blocked")?;
    let recovery = channel("forced_recovery")?;
    let valuation = channel("company_valuation")?;
    validate_private_equity_series_range(issuer_id, "mark", mark, 0, i64::MAX)?;
    validate_private_equity_series_range(issuer_id, "regime", regime, 1, 4)?;
    validate_private_equity_series_range(issuer_id, "event_kind", event_kind, 0, 7)?;
    validate_private_equity_series_range(issuer_id, "sale_opportunity", opportunity, 0, 1)?;
    validate_private_equity_series_range(issuer_id, "sale_capacity", capacity, 0, RATE_SCALE_PPB)?;
    validate_private_equity_series_range(issuer_id, "eligible", eligible, 0, RATE_SCALE_PPB)?;
    validate_private_equity_series_range(issuer_id, "forced_sale", forced_sale, 0, RATE_SCALE_PPB)?;
    validate_private_equity_series_range(issuer_id, "liquidity_blocked", blocked, 0, 1)?;
    validate_private_equity_series_range(issuer_id, "forced_recovery", recovery, 0, i64::MAX)?;
    validate_private_equity_series_range(issuer_id, "company_valuation", valuation, 0, i64::MAX)?;
    for (index, (event, active)) in event_kind
        .values
        .iter()
        .zip(&opportunity.values)
        .enumerate()
    {
        if (*event == 1) != (*active == 1) {
            return Err(SimulationError::InvalidPrivateEquityChannel {
                issuer_id: issuer_id.to_owned(),
                channel: "event/opportunity consistency",
                index,
                value: *event,
            });
        }
    }
    Ok(())
}

fn validate_private_equity_series_range(
    issuer_id: &str,
    channel: &'static str,
    series: &SeriesSpec,
    minimum: i64,
    maximum: i64,
) -> Result<(), SimulationError> {
    if let Some((index, value)) = series
        .values
        .iter()
        .copied()
        .enumerate()
        .find(|(_, value)| !(minimum..=maximum).contains(value))
    {
        return Err(SimulationError::InvalidPrivateEquityChannel {
            issuer_id: issuer_id.to_owned(),
            channel,
            index,
            value,
        });
    }
    Ok(())
}

fn is_positive_decimal(value: &str) -> bool {
    let mut saw_digit = false;
    let mut saw_nonzero = false;
    let mut saw_dot = false;
    let bytes = value.as_bytes();
    if bytes.is_empty() || bytes.first() == Some(&b'.') || bytes.last() == Some(&b'.') {
        return false;
    }
    for byte in bytes {
        match byte {
            b'0'..=b'9' => {
                saw_digit = true;
                saw_nonzero |= *byte != b'0';
            }
            b'.' if !saw_dot => saw_dot = true,
            _ => return false,
        }
    }
    saw_digit && saw_nonzero
}

fn validate_amount_spec(
    fixture: &Fixture,
    kind: &'static str,
    cause_id: &str,
    amount: &AmountSpec,
    months: impl IntoIterator<Item = u32>,
) -> Result<(), SimulationError> {
    let AmountSpec::SeriesIndexed(amount) = amount else {
        return Ok(());
    };
    if amount.adjustment_period_months == 0 {
        return Err(SimulationError::InvalidSeriesIndexedAmount {
            kind,
            cause_id: cause_id.into(),
        });
    }
    let valid_rent_series = amount
        .series_id
        .strip_prefix("rent:")
        .is_some_and(|location_id| !location_id.is_empty());
    if amount.series_id != "inflation" && !valid_rent_series {
        return Err(SimulationError::UnsupportedAmountSeries {
            kind,
            cause_id: cause_id.into(),
            series_id: amount.series_id.clone(),
        });
    }
    let months = months.into_iter().collect::<Vec<_>>();
    if let Some(month) = months
        .iter()
        .copied()
        .find(|month| *month < amount.base_month_index)
    {
        return Err(SimulationError::SeriesAmountBeforeBase {
            kind,
            cause_id: cause_id.into(),
            month,
            base_month: amount.base_month_index,
        });
    }
    let series = fixture
        .series
        .iter()
        .find(|series| series.series_id == amount.series_id)
        .ok_or_else(|| SimulationError::MissingSeries {
            series_id: amount.series_id.clone(),
        })?;
    let mut required_months = BTreeSet::from([amount.base_month_index]);
    for month in months {
        let elapsed = month - amount.base_month_index;
        let reset_month = amount.base_month_index
            + (elapsed / amount.adjustment_period_months) * amount.adjustment_period_months;
        required_months.insert(reset_month);
    }
    for rollout in 0..fixture.rollout_count {
        for reset_month in &required_months {
            let value = series.value(rollout, *reset_month).ok_or_else(|| {
                SimulationError::MissingSeriesValue {
                    series_id: amount.series_id.clone(),
                    rollout,
                    snapshot: *reset_month,
                }
            })?;
            validate_amount_index_level(cause_id, &amount.series_id, rollout, *reset_month, value)?;
        }
    }
    Ok(())
}

fn validate_amount_index_level(
    cause_id: &str,
    series_id: &str,
    rollout: u32,
    month: u32,
    value: i64,
) -> Result<(), SimulationError> {
    if value <= 0 {
        return Err(SimulationError::NonPositiveSeriesAmountLevel {
            cause_id: cause_id.into(),
            series_id: series_id.into(),
            rollout,
            month,
            value,
        });
    }
    let reconstructed =
        ((value as f64 / INDEX_LEVEL_SCALE as f64) * INDEX_LEVEL_SCALE as f64).round() as i64;
    if value > MAX_EXACT_F64_INTEGER || reconstructed != value {
        return Err(SimulationError::InexactSeriesAmountLevel {
            cause_id: cause_id.into(),
            series_id: series_id.into(),
            rollout,
            month,
            value,
        });
    }
    Ok(())
}

fn validate_income_category(category: Option<&str>) -> Result<(), SimulationError> {
    if let Some(category) = category
        && category != "ordinary"
    {
        return Err(SimulationError::UnsupportedIncomeCategory {
            category: category.into(),
        });
    }
    Ok(())
}

fn validate_account(
    accounts: &BTreeSet<AccountRef>,
    account: &AccountRef,
    context: &str,
) -> Result<(), SimulationError> {
    if !accounts.contains(account) {
        return Err(SimulationError::UnknownAccountReference {
            context: context.into(),
            agent_id: account.agent_id.clone(),
            account_id: account.account_id.clone(),
        });
    }
    Ok(())
}

fn simulate_rollout(
    fixture: &Fixture,
    rollout_id: u32,
    capture_mode: CaptureMode,
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
            active_obligations.push(ActiveObligation {
                cause_id: format!("{}_m{month}", obligation.obligation_id),
                obligation_type: obligation.obligation_type.clone(),
                from: obligation.from.clone(),
                to: obligation.to.clone(),
                amount_due: amount_value(fixture, rollout_id, month, &obligation.amount_due)?,
                effect: ObligationEffect::None,
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
            active_obligations.push(ActiveObligation {
                cause_id: format!("{}_m{month}", obligation.obligation_id),
                obligation_type: obligation.obligation_type.clone(),
                from: obligation.from.clone(),
                to: obligation.to.clone(),
                amount_due: amount_value(fixture, rollout_id, month, &obligation.amount_due)?,
                effect: ObligationEffect::None,
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
        let settlement_failed = settle_obligations(
            fixture,
            &mut ledger,
            &mut recorder,
            &mut tax_facts,
            &properties,
            &mut mortgages,
            &mut tax_liabilities,
            month,
            &active_obligations,
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
    })
}

fn execute_primary_residence_events(
    fixture: &Fixture,
    recorder: &mut Recorder,
    primary_residence_by_agent: &mut BTreeMap<String, Option<String>>,
    month: u32,
) -> Result<(), SimulationError> {
    let mut events: Vec<_> = fixture
        .scenario
        .primary_residence_events
        .iter()
        .filter(|event| event.month == month)
        .collect();
    events.sort_by_key(|event| &event.agent_id);
    for event in events {
        primary_residence_by_agent.insert(event.agent_id.clone(), event.property_id.clone());
        recorder.record_primary_residence(PrimaryResidenceOutcome {
            month,
            agent_id: event.agent_id.clone(),
            property_id: event.property_id.clone(),
            is_primary_residence: event.property_id.is_some(),
        })?;
    }
    Ok(())
}

fn execute_distributions(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &[LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    month: u32,
) -> Result<(), SimulationError> {
    for distribution in &fixture.scenario.distributions {
        let pool_lots: Vec<_> = lots
            .iter()
            .filter(|lot| {
                lot.spec.agent_id == distribution.agent_id
                    && lot.spec.account_id == distribution.holding_account_id
                    && lot.spec.asset_id == distribution.asset_id
            })
            .collect();
        let scale = pool_lots[0].spec.quantity_scale;
        let units = pool_lots.iter().try_fold(0_i64, |total, lot| {
            total
                .checked_add(lot.units_remaining.0)
                .ok_or(ArithmeticError::Overflow {
                    operation: "distribution pool quantity",
                })
        })?;
        let per_unit = series_value(
            fixture,
            &format!("security_distribution:{}", distribution.asset_id),
            rollout_id,
            month,
        )?;
        let total_amount = Money(mul_div_round_half_up(
            per_unit,
            units,
            scale,
            "security distribution",
        )?);
        for (slice_index, slice) in distribution.tax_character.iter().enumerate() {
            let amount = Money(mul_div_round_half_up(
                total_amount.0,
                slice.fraction_ppb,
                RATE_SCALE,
                "security distribution tax slice",
            )?);
            let cause_id = format!(
                "distribution:{}:{}:s{slice_index}:m{month}",
                distribution.agent_id, distribution.asset_id
            );
            transfer_money(
                ledger,
                recorder,
                month,
                &cause_id,
                &AccountRef::new(EXTERNAL_AGENT, "boundary"),
                &AccountRef::new(&distribution.agent_id, &distribution.to_account_id),
                amount,
            )?;
            record_interest_income(
                fixture,
                tax_facts,
                &distribution.agent_id,
                slice.issuer_jurisdiction_id.as_deref(),
                jurisdiction_level(fixture, slice.issuer_jurisdiction_id.as_deref()),
                amount,
            )?;
            recorder.record_distribution(DistributionOutcome {
                month,
                agent_id: distribution.agent_id.clone(),
                holding_account_id: distribution.holding_account_id.clone(),
                asset_id: distribution.asset_id.clone(),
                slice_index: u32::try_from(slice_index).map_err(|_| ArithmeticError::Overflow {
                    operation: "distribution slice index",
                })?,
                fraction_ppb: slice.fraction_ppb,
                issuer_jurisdiction_id: slice.issuer_jurisdiction_id.clone(),
                units: Quantity(units),
                amount,
            })?;
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn execute_property_lifecycle_events(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &mut [PropertyState],
    mortgages: &mut [MortgageState],
    primary_residence_by_agent: &mut BTreeMap<String, Option<String>>,
    month: u32,
) -> Result<(), SimulationError> {
    let property_ids: BTreeSet<_> = fixture
        .scenario
        .property_rented_fraction_events
        .iter()
        .filter(|event| event.month == month)
        .map(|event| event.property_id.as_str())
        .chain(
            fixture
                .scenario
                .capital_improvement_events
                .iter()
                .filter(|event| event.month == month)
                .map(|event| event.property_id.as_str()),
        )
        .chain(
            fixture
                .scenario
                .property_sales
                .iter()
                .filter(|event| event.month == month)
                .map(|event| event.property_id.as_str()),
        )
        .collect();
    for property_id in property_ids {
        for event in fixture
            .scenario
            .property_rented_fraction_events
            .iter()
            .filter(|event| event.month == month && event.property_id == property_id)
        {
            let Some(property) = properties
                .iter_mut()
                .find(|property| property.property_id == event.property_id && property.active)
            else {
                continue;
            };
            property.rented_fraction_ppb = event.rented_fraction_ppb;
            recorder.record_property_rented_fraction(PropertyRentedFractionOutcome {
                month,
                property_id: event.property_id.clone(),
                rented_fraction_ppb: event.rented_fraction_ppb,
            })?;
        }
        for event in fixture
            .scenario
            .capital_improvement_events
            .iter()
            .filter(|event| event.month == month && event.property_id == property_id)
        {
            let Some(property) = properties
                .iter_mut()
                .find(|property| property.property_id == event.property_id && property.active)
            else {
                continue;
            };
            let purchase = fixture
                .scenario
                .scheduled_property_purchases
                .iter()
                .find(|purchase| purchase.property_id == event.property_id)
                .expect("validated improvement has a purchase");
            recorder.apply_entry(
                ledger,
                JournalEntry {
                    month,
                    cause_id: format!("capital-improvement:{}:{month}", event.property_id),
                    postings: vec![
                        Posting {
                            account: AccountRef::new(
                                &purchase.buyer_agent_id,
                                &purchase.buyer_account_id,
                            ),
                            amount: event.amount.checked_neg()?,
                        },
                        Posting {
                            account: property_asset_account(
                                &purchase.buyer_agent_id,
                                &purchase.property_id,
                            ),
                            amount: event.amount,
                        },
                    ],
                },
            )?;
            property.building_basis = property.building_basis.checked_add(event.amount)?;
            recorder.record_capital_improvement(CapitalImprovementOutcome {
                month,
                property_id: event.property_id.clone(),
                amount: event.amount,
                // The existing lifecycle codec currently emits an empty
                // description for compiled improvement rows.
                description: String::new(),
            })?;
        }
        execute_property_sales(
            fixture,
            rollout_id,
            ledger,
            recorder,
            tax_facts,
            properties,
            mortgages,
            primary_residence_by_agent,
            month,
            property_id,
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn execute_property_sales(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &mut [PropertyState],
    mortgages: &mut [MortgageState],
    primary_residence_by_agent: &mut BTreeMap<String, Option<String>>,
    month: u32,
    property_id: &str,
) -> Result<(), SimulationError> {
    let mut sales: Vec<&PropertySaleSpec> = fixture
        .scenario
        .property_sales
        .iter()
        .filter(|sale| sale.month == month && sale.property_id == property_id)
        .collect();
    sales.sort_by_key(|sale| &sale.property_id);
    for sale in sales {
        let Some(property_index) = properties
            .iter()
            .position(|property| property.property_id == sale.property_id && property.active)
        else {
            continue;
        };
        let property = &properties[property_index];
        let purchase = fixture
            .scenario
            .scheduled_property_purchases
            .iter()
            .find(|purchase| purchase.property_id == sale.property_id)
            .expect("validated property sale has a purchase");
        let series_id = format!("home_value:{}", purchase.location_id);
        let base_value = series_value(fixture, &series_id, rollout_id, 0)?;
        let sale_value = series_value(fixture, &series_id, rollout_id, month)?;
        let market_value = Money(mul_div_round_half_up(
            purchase.purchase_price.0,
            sale_value,
            base_value,
            "property market value",
        )?);
        let gross_proceeds = Money(mul_div_round_half_up(
            market_value.0,
            i64::from(10_000 - sale.closing_cost_bps),
            10_000,
            "property sale proceeds",
        )?);
        let mortgage_indices: Vec<_> = mortgages
            .iter()
            .enumerate()
            .filter(|(_, mortgage)| mortgage.property_id == sale.property_id && mortgage.active)
            .map(|(index, _)| index)
            .collect();
        let mortgage_payoff = mortgage_indices.iter().try_fold(Money(0), |total, index| {
            total.checked_add(mortgages[*index].principal)
        })?;
        let net_cash = gross_proceeds.checked_sub(mortgage_payoff)?;
        // Match the legacy contract exactly: capitalized buyer closing costs
        // enter the depreciable building basis, but the sale-gain formula uses
        // purchase price + later capex - cumulative depreciation.
        let capital_improvements = property
            .building_basis
            .checked_sub(property.building_basis_initial)?;
        let tax_adjusted_basis = purchase
            .purchase_price
            .checked_add(capital_improvements)?
            .checked_sub(property.cumulative_depreciation)?;
        let realized_gain = gross_proceeds.checked_sub(tax_adjusted_basis)?;
        let depreciation_recapture = Money(
            realized_gain
                .0
                .max(0)
                .min(property.cumulative_depreciation.0),
        );
        let post_recapture_gain =
            Money(realized_gain.checked_sub(depreciation_recapture)?.0.max(0));
        let qualifies_for_section_121 = property
            .owner_occupied_window
            .iter()
            .filter(|occupied| **occupied)
            .count()
            >= SECTION_121_MIN_QUALIFYING_MONTHS;
        let exclusion_cap = fixture
            .scenario
            .tax_profiles
            .iter()
            .find(|profile| profile.agent_id == purchase.buyer_agent_id)
            .map_or(Money(0), |profile| profile.section_121_exclusion);
        let section_121_exclusion = if qualifies_for_section_121 {
            Money(post_recapture_gain.0.min(exclusion_cap.0))
        } else {
            Money(0)
        };
        let long_term_capital_gain = post_recapture_gain.checked_sub(section_121_exclusion)?;
        let property_asset_balance = property.adjusted_basis.checked_add(capital_improvements)?;
        let basis_writeoff = property
            .adjusted_basis
            .checked_sub(purchase.purchase_price)?
            .checked_add(property.cumulative_depreciation)?;
        let mut postings = vec![
            Posting {
                account: AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id),
                amount: net_cash,
            },
            Posting {
                account: property_asset_account(&purchase.buyer_agent_id, &purchase.property_id),
                amount: property_asset_balance.checked_neg()?,
            },
            Posting {
                account: property_basis_writeoff_account(
                    &purchase.buyer_agent_id,
                    &purchase.property_id,
                ),
                amount: basis_writeoff,
            },
            Posting {
                account: realized_gain_account(&purchase.buyer_agent_id),
                amount: realized_gain.checked_neg()?,
            },
        ];
        for index in &mortgage_indices {
            let mortgage = &mortgages[*index];
            postings.extend([
                Posting {
                    account: mortgage_liability_account(&mortgage.agent_id, &mortgage.liability_id),
                    amount: mortgage.principal,
                },
                Posting {
                    account: mortgage_receivable_account(
                        &mortgage.counterparty_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal.checked_neg()?,
                },
                Posting {
                    account: mortgage_funding_account(
                        &mortgage.counterparty_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal,
                },
            ]);
        }
        recorder.apply_entry(
            ledger,
            JournalEntry {
                month,
                cause_id: format!("property-sale:{}", sale.property_id),
                postings,
            },
        )?;
        properties[property_index].active = false;
        properties[property_index].rented_fraction_ppb = 0;
        properties[property_index].building_basis = Money(0);
        if primary_residence_by_agent
            .get(&purchase.buyer_agent_id)
            .is_some_and(|assignment| assignment.as_deref() == Some(&sale.property_id))
        {
            primary_residence_by_agent.insert(purchase.buyer_agent_id.clone(), None);
        }
        for index in mortgage_indices {
            mortgages[index].principal = Money(0);
            mortgages[index].active = false;
        }
        record_capital_gain(
            tax_facts,
            &purchase.buyer_agent_id,
            long_term_capital_gain,
            true,
        )?;
        record_section_1250_recapture(tax_facts, &purchase.buyer_agent_id, depreciation_recapture)?;
        recorder.record_property_sale(PropertySaleOutcome {
            month,
            property_id: sale.property_id.clone(),
            gross_proceeds,
            mortgage_payoff,
            net_cash_to_owner: net_cash,
            realized_gain,
            depreciation_recapture,
            section_121_exclusion,
            long_term_capital_gain,
        })?;
    }
    Ok(())
}

fn accrue_primary_residence_occupancy(
    primary_residence_by_agent: &BTreeMap<String, Option<String>>,
    properties: &mut [PropertyState],
    month: u32,
) -> Result<(), SimulationError> {
    let window_index = usize::try_from(month).map_err(|_| ArithmeticError::Overflow {
        operation: "primary-residence window index",
    })? % SECTION_121_LOOKBACK_MONTHS;
    for property in properties {
        let occupied = property.active
            && property.rented_fraction_ppb < RATE_SCALE_PPB
            && primary_residence_by_agent
                .get(&property.owner_agent_id)
                .and_then(|assignment| assignment.as_deref())
                == Some(property.property_id.as_str());
        property.owner_occupied_window[window_index] = occupied;
        if occupied {
            property.owner_occupied_months =
                property
                    .owner_occupied_months
                    .checked_add(1)
                    .ok_or(ArithmeticError::Overflow {
                        operation: "primary-residence occupied-month count",
                    })?;
        }
    }
    Ok(())
}

fn execute_property_purchases(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    properties: &mut Vec<PropertyState>,
    mortgages: &mut Vec<MortgageState>,
    month: u32,
) -> Result<(), SimulationError> {
    for purchase in fixture
        .scenario
        .scheduled_property_purchases
        .iter()
        .filter(|purchase| purchase.month == month)
    {
        let principal = purchase
            .mortgage
            .as_ref()
            .map_or(Money(0), |mortgage| mortgage.principal);
        let adjusted_basis = purchase
            .purchase_price
            .checked_add(purchase.buyer_closing_cost)?;
        let building_basis_initial = Money(mul_div_round_half_up(
            purchase.purchase_price.0,
            RATE_SCALE_PPB - purchase.land_value_fraction_ppb,
            RATE_SCALE_PPB,
            "property building basis",
        )?)
        .checked_add(purchase.buyer_closing_cost)?;
        let stake = purchase
            .down_payment
            .checked_add(purchase.buyer_closing_cost)?;
        let equity = purchase.purchase_price.checked_sub(principal)?;
        let buyer_cash = AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id);
        let seller_cash = AccountRef::new(&purchase.seller_agent_id, &purchase.seller_account_id);
        let mut postings = vec![
            Posting {
                account: buyer_cash,
                amount: stake.checked_neg()?,
            },
            Posting {
                account: seller_cash,
                amount: stake,
            },
            Posting {
                account: property_asset_account(&purchase.buyer_agent_id, &purchase.property_id),
                amount: adjusted_basis,
            },
            Posting {
                account: property_sale_clearing_account(
                    &purchase.seller_agent_id,
                    &purchase.property_id,
                ),
                amount: stake.checked_neg()?,
            },
        ];
        let mut origination = None;
        if let Some(mortgage) = &purchase.mortgage {
            let monthly_payment = mortgage_monthly_payment(
                mortgage.principal,
                mortgage.annual_interest_rate_ppb,
                mortgage.term_months,
            )?;
            postings.extend([
                Posting {
                    account: mortgage_liability_account(
                        &purchase.buyer_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal.checked_neg()?,
                },
                Posting {
                    account: mortgage_receivable_account(
                        &mortgage.lender_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal,
                },
                Posting {
                    account: mortgage_funding_account(
                        &mortgage.lender_agent_id,
                        &mortgage.liability_id,
                    ),
                    amount: mortgage.principal.checked_neg()?,
                },
            ]);
            mortgages.push(MortgageState {
                liability_id: mortgage.liability_id.clone(),
                property_id: purchase.property_id.clone(),
                agent_id: purchase.buyer_agent_id.clone(),
                payment_account_id: purchase.buyer_account_id.clone(),
                counterparty_agent_id: mortgage.lender_agent_id.clone(),
                counterparty_account_id: mortgage.lender_account_id.clone(),
                origination_month: month,
                annual_interest_rate_ppb: mortgage.annual_interest_rate_ppb,
                term_months: mortgage.term_months,
                monthly_payment,
                principal: mortgage.principal,
                interest_paid_ytd: Money(0),
                rental_interest_paid_ytd: Money(0),
                principal_paid_ytd: Money(0),
                active: true,
            });
            origination = Some(MortgageOriginationOutcome {
                month,
                cause_id: format!("{}_mortgage_origination", purchase.cause_id),
                liability_id: mortgage.liability_id.clone(),
                agent_id: purchase.buyer_agent_id.clone(),
                payment_account_id: purchase.buyer_account_id.clone(),
                counterparty_agent_id: mortgage.lender_agent_id.clone(),
                counterparty_account_id: mortgage.lender_account_id.clone(),
                property_id: purchase.property_id.clone(),
                principal: mortgage.principal,
                annual_interest_rate_ppb: mortgage.annual_interest_rate_ppb,
                term_months: mortgage.term_months,
                monthly_payment,
            });
        } else {
            postings.push(Posting {
                account: property_sale_clearing_account(
                    &purchase.seller_agent_id,
                    &purchase.property_id,
                ),
                amount: principal,
            });
        }
        recorder.apply_entry(
            ledger,
            JournalEntry {
                month,
                cause_id: purchase.cause_id.clone(),
                postings,
            },
        )?;
        if stake.0 > 0 {
            recorder.record_transfer(TransferOutcome {
                month,
                cause_id: format!("{}_buyer_cash", purchase.cause_id),
                from: AccountRef::new(&purchase.buyer_agent_id, &purchase.buyer_account_id),
                to: AccountRef::new(&purchase.seller_agent_id, &purchase.seller_account_id),
                amount: stake,
                income_category: None,
            });
        }
        properties.push(PropertyState {
            property_id: purchase.property_id.clone(),
            location_id: purchase.location_id.clone(),
            owner_agent_id: purchase.buyer_agent_id.clone(),
            purchase_month: month,
            adjusted_basis,
            rented_fraction_ppb: purchase.rented_fraction_ppb,
            building_basis_initial,
            building_basis: building_basis_initial,
            cumulative_depreciation: Money(0),
            depreciation_ytd: Money(0),
            owner_occupied_months: 0,
            owner_occupied_window: vec![false; SECTION_121_LOOKBACK_MONTHS],
            contribution_used: stake,
            equity_ledger: equity,
            active: true,
        });
        recorder.record_property_purchase(
            PropertyPurchaseOutcome {
                month,
                cause_id: purchase.cause_id.clone(),
                property_id: purchase.property_id.clone(),
                location_id: purchase.location_id.clone(),
                buyer_agent_id: purchase.buyer_agent_id.clone(),
                purchase_price: purchase.purchase_price,
                closing_cost: purchase.buyer_closing_cost,
                adjusted_basis,
                stake_contribution: stake,
                equity_ledger: equity,
            },
            origination,
        )?;
    }
    Ok(())
}

fn execute_cashflows(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &[PropertyState],
    month: u32,
) -> Result<(), SimulationError> {
    for cashflow in fixture
        .scenario
        .scheduled_transfers
        .iter()
        .filter(|cashflow| cashflow.month == month)
    {
        apply_cashflow(
            ledger,
            recorder,
            tax_facts,
            month,
            &cashflow.cause_id,
            &cashflow.from,
            &cashflow.to,
            amount_value(fixture, rollout_id, month, &cashflow.amount)?,
            cashflow.income_category.as_deref(),
            cashflow.deduction_category.as_deref(),
        )?;
    }
    for cashflow in fixture
        .scenario
        .recurring_transfers
        .iter()
        .filter(|cashflow| {
            cashflow.start_month <= month && cashflow.end_month.is_none_or(|end| month <= end)
        })
    {
        apply_cashflow(
            ledger,
            recorder,
            tax_facts,
            month,
            &cashflow.cause_id,
            &cashflow.from,
            &cashflow.to,
            amount_value(fixture, rollout_id, month, &cashflow.amount)?,
            cashflow.income_category.as_deref(),
            cashflow.deduction_category.as_deref(),
        )?;
    }
    for cashflow in fixture
        .scenario
        .scheduled_property_cashflows
        .iter()
        .filter(|cashflow| cashflow.month == month)
    {
        if properties
            .iter()
            .any(|property| property.property_id == cashflow.property_id && property.active)
        {
            apply_cashflow(
                ledger,
                recorder,
                tax_facts,
                month,
                &cashflow.cause_id,
                &cashflow.from,
                &cashflow.to,
                amount_value(fixture, rollout_id, month, &cashflow.amount)?,
                cashflow.income_category.as_deref(),
                cashflow.deduction_category.as_deref(),
            )?;
        }
    }
    for cashflow in fixture
        .scenario
        .recurring_property_cashflows
        .iter()
        .filter(|cashflow| {
            cashflow.start_month <= month && cashflow.end_month.is_none_or(|end| month <= end)
        })
    {
        if properties
            .iter()
            .any(|property| property.property_id == cashflow.property_id && property.active)
        {
            apply_cashflow(
                ledger,
                recorder,
                tax_facts,
                month,
                &cashflow.cause_id,
                &cashflow.from,
                &cashflow.to,
                amount_value(fixture, rollout_id, month, &cashflow.amount)?,
                cashflow.income_category.as_deref(),
                cashflow.deduction_category.as_deref(),
            )?;
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn apply_cashflow(
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    month: u32,
    cause_id: &str,
    from: &AccountRef,
    to: &AccountRef,
    amount: Money,
    income_category: Option<&str>,
    deduction_category: Option<&str>,
) -> Result<(), SimulationError> {
    transfer_money(ledger, recorder, month, cause_id, from, to, amount)?;
    let recorded_income_category = (income_category == Some("ordinary")
        && tax_facts
            .keys()
            .any(|(agent_id, _)| agent_id == &to.agent_id))
    .then(|| "ordinary".to_owned());
    recorder.record_transfer(TransferOutcome {
        month,
        cause_id: cause_id.into(),
        from: from.clone(),
        to: to.clone(),
        amount,
        income_category: recorded_income_category,
    });
    record_transfer_income(tax_facts, &to.agent_id, income_category, amount)?;
    record_transfer_deduction(tax_facts, &from.agent_id, deduction_category, amount)
}

fn property_obligations(
    fixture: &Fixture,
    properties: &[PropertyState],
    mortgages: &[MortgageState],
    month: u32,
) -> Result<Vec<ActiveObligation>, SimulationError> {
    let mut obligations = Vec::new();
    for (index, mortgage) in mortgages.iter().enumerate() {
        if !mortgage.active || mortgage.origination_month >= month || mortgage.principal.0 <= 0 {
            continue;
        }
        let interest = Money(mul_div_round_half_up(
            mortgage.principal.0,
            mortgage.annual_interest_rate_ppb,
            12 * RATE_SCALE_PPB,
            "mortgage monthly interest",
        )?);
        let due = Money(
            mortgage
                .monthly_payment
                .0
                .min(mortgage.principal.checked_add(interest)?.0),
        );
        let principal = Money((due.0 - interest.0).max(0).min(mortgage.principal.0));
        obligations.push(ActiveObligation {
            cause_id: format!("{}_payment_m{month}", mortgage.liability_id),
            obligation_type: "mortgage_payment".into(),
            from: AccountRef::new(&mortgage.agent_id, &mortgage.payment_account_id),
            to: AccountRef::new(
                &mortgage.counterparty_agent_id,
                &mortgage.counterparty_account_id,
            ),
            amount_due: due,
            effect: ObligationEffect::Mortgage {
                mortgage_index: index,
                interest,
                principal,
            },
        });
    }
    for policy in &fixture.scenario.property_tax_policies {
        let Some(property) = properties
            .iter()
            .find(|property| property.property_id == policy.property_id && property.active)
        else {
            continue;
        };
        if property.purchase_month >= month
            || policy.start_month > month
            || policy.end_month.is_some_and(|end| month > end)
        {
            continue;
        }
        let purchase = fixture
            .scenario
            .scheduled_property_purchases
            .iter()
            .find(|purchase| purchase.property_id == policy.property_id)
            .expect("validated property has a purchase");
        let location = fixture
            .scenario
            .locations
            .iter()
            .find(|location| location.location_id == property.location_id)
            .expect("validated property has a location");
        let rate = policy
            .annual_tax_rate_ppb
            .unwrap_or(location.annual_property_tax_rate_ppb);
        let annual_tax_numerator = i128::from(purchase.purchase_price.0)
            .checked_mul(i128::from(rate))
            .and_then(|value| {
                i128::from(location.annual_special_assessment.0)
                    .checked_mul(i128::from(RATE_SCALE_PPB))
                    .and_then(|special| value.checked_add(special))
            })
            .ok_or(ArithmeticError::Overflow {
                operation: "property tax",
            })?;
        let amount_due = Money(
            i64::try_from(mul_div_i128_round_half_up(
                annual_tax_numerator,
                1,
                12 * i128::from(RATE_SCALE_PPB),
                "property tax",
            )?)
            .map_err(|_| ArithmeticError::Overflow {
                operation: "property tax",
            })?,
        );
        obligations.push(ActiveObligation {
            cause_id: format!("{}_property_tax_m{month}", policy.property_id),
            obligation_type: "property_tax".into(),
            from: AccountRef::new(&policy.owner_agent_id, &policy.from_account_id),
            to: AccountRef::new(
                &policy.tax_authority_agent_id,
                &policy.tax_authority_account_id,
            ),
            amount_due,
            effect: ObligationEffect::PropertyTax {
                owner_agent_id: policy.owner_agent_id.clone(),
                rented_fraction_ppb: property.rented_fraction_ppb,
            },
        });
    }
    Ok(obligations)
}

fn tax_obligations(
    fixture: &Fixture,
    tax_liabilities: &[TaxLiabilityState],
    month: u32,
) -> Result<Vec<ActiveObligation>, SimulationError> {
    let Some(quarter) = estimated_tax_quarter(month) else {
        return Ok(Vec::new());
    };
    let mut obligations = Vec::new();
    if quarter <= 3 {
        for (profile_index, profile) in fixture.scenario.tax_profiles.iter().enumerate() {
            if profile.prior_year_tax.0 <= 0 {
                continue;
            }
            let amount_due = Money(mul_div_round_half_up(
                profile.prior_year_tax.0,
                1,
                4,
                "quarterly estimated tax",
            )?);
            if amount_due == Money(0) {
                continue;
            }
            obligations.push(ActiveObligation {
                cause_id: format!(
                    "{}_estimated_tax_q{quarter}_y{}",
                    profile.agent_id,
                    month / 12
                ),
                obligation_type: "estimated_tax".into(),
                from: AccountRef::new(&profile.agent_id, &profile.payment_account_id),
                to: AccountRef::new(
                    &profile.tax_authority_agent_id,
                    &profile.tax_authority_account_id,
                ),
                amount_due,
                effect: ObligationEffect::TaxPayment { profile_index },
            });
        }
        return Ok(obligations);
    }

    let tax_year = month / 12 - 1;
    let tax_year_end_month = tax_year * 12 + 11;
    for (profile_index, profile) in fixture.scenario.tax_profiles.iter().enumerate() {
        let actual = tax_liabilities
            .iter()
            .filter(|liability| {
                liability.active
                    && liability.agent_id == profile.agent_id
                    && liability.tax_year_end_month == tax_year_end_month
            })
            .try_fold(Money(0), |total, liability| {
                total.checked_add(liability.amount_owed)
            })?;
        let safe_harbor = Money(profile.prior_year_tax.0.min(actual.0));
        let first_three_quarters = Money(mul_div_round_half_up(
            profile.prior_year_tax.0,
            3,
            4,
            "first three estimated-tax quarters",
        )?);
        let q4_due = Money((safe_harbor.0 - first_three_quarters.0).max(0));
        if q4_due != Money(0) {
            obligations.push(ActiveObligation {
                cause_id: format!("{}_estimated_tax_q4_y{tax_year}", profile.agent_id),
                obligation_type: "estimated_tax".into(),
                from: AccountRef::new(&profile.agent_id, &profile.payment_account_id),
                to: AccountRef::new(
                    &profile.tax_authority_agent_id,
                    &profile.tax_authority_account_id,
                ),
                amount_due: q4_due,
                effect: ObligationEffect::TaxPayment { profile_index },
            });
        }
        let true_up_due = Money((actual.0 - safe_harbor.0).max(0));
        if true_up_due != Money(0) {
            obligations.push(ActiveObligation {
                cause_id: format!("{}_tax_true_up_y{tax_year}", profile.agent_id),
                obligation_type: "tax_true_up".into(),
                from: AccountRef::new(&profile.agent_id, &profile.payment_account_id),
                to: AccountRef::new(
                    &profile.tax_authority_agent_id,
                    &profile.tax_authority_account_id,
                ),
                amount_due: true_up_due,
                effect: ObligationEffect::TaxTrueUp {
                    profile_index,
                    tax_year_end_month,
                },
            });
        }
    }
    Ok(obligations)
}

fn estimated_tax_quarter(month: u32) -> Option<u32> {
    match month % 12 {
        3 => Some(1),
        5 => Some(2),
        8 => Some(3),
        0 if month > 0 => Some(4),
        _ => None,
    }
}

fn mortgage_monthly_payment(
    principal: Money,
    annual_rate_ppb: i64,
    term_months: u32,
) -> Result<Money, SimulationError> {
    if annual_rate_ppb == 0 {
        return Ok(Money(mul_div_round_half_up(
            principal.0,
            1,
            i64::from(term_months),
            "zero-rate mortgage payment",
        )?));
    }
    let monthly_rate = mul_div_i128_round_half_up(
        i128::from(annual_rate_ppb),
        CONTRACT_SCALE,
        12 * i128::from(RATE_SCALE_PPB),
        "mortgage monthly rate",
    )?;
    let factor = CONTRACT_SCALE
        .checked_add(monthly_rate)
        .ok_or(ArithmeticError::Overflow {
            operation: "mortgage rate factor",
        })?;
    let mut discount = CONTRACT_SCALE;
    for _ in 0..term_months {
        discount = mul_div_i128_round_half_up(
            discount,
            CONTRACT_SCALE,
            factor,
            "mortgage discount factor",
        )?;
    }
    let denominator = CONTRACT_SCALE
        .checked_sub(discount)
        .ok_or(ArithmeticError::Overflow {
            operation: "mortgage annuity denominator",
        })?;
    let payment = mul_div_i128_round_half_up(
        i128::from(principal.0),
        monthly_rate,
        denominator,
        "mortgage payment",
    )?;
    Ok(Money(i64::try_from(payment).map_err(|_| {
        ArithmeticError::Overflow {
            operation: "mortgage payment",
        }
    })?))
}

fn record_transfer_income(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    recipient_agent_id: &str,
    income_category: Option<&str>,
    amount: Money,
) -> Result<(), SimulationError> {
    if income_category != Some("ordinary") {
        return Ok(());
    }
    for ((agent_id, _), facts) in tax_facts {
        if agent_id == recipient_agent_id {
            facts.ordinary_income = facts.ordinary_income.checked_add(amount)?;
        }
    }
    Ok(())
}

fn record_transfer_deduction(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    payer_agent_id: &str,
    deduction_category: Option<&str>,
    amount: Money,
) -> Result<(), SimulationError> {
    if deduction_category != Some("ordinary") {
        return Ok(());
    }
    for ((agent_id, _), facts) in tax_facts {
        if agent_id == payer_agent_id {
            facts.ordinary_income = facts.ordinary_income.checked_sub(amount)?;
        }
    }
    Ok(())
}

fn record_capital_gain(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    agent_id: &str,
    gain: Money,
    long_term: bool,
) -> Result<(), SimulationError> {
    for ((taxpayer, _), facts) in tax_facts {
        if taxpayer != agent_id {
            continue;
        }
        if long_term {
            facts.long_term_gain = facts.long_term_gain.checked_add(gain)?;
        } else {
            facts.short_term_gain = facts.short_term_gain.checked_add(gain)?;
        }
    }
    Ok(())
}

fn record_section_1250_recapture(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    agent_id: &str,
    amount: Money,
) -> Result<(), SimulationError> {
    for ((taxpayer, _), facts) in tax_facts {
        if taxpayer == agent_id {
            facts.section_1250_recapture = facts.section_1250_recapture.checked_add(amount)?;
        }
    }
    Ok(())
}

fn record_rental_interest_deduction(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    agent_id: &str,
    amount: Money,
) -> Result<(), SimulationError> {
    for ((taxpayer, _), facts) in tax_facts {
        if taxpayer == agent_id {
            facts.ordinary_income = facts.ordinary_income.checked_sub(amount)?;
            facts.rental_interest_deduction =
                facts.rental_interest_deduction.checked_add(amount)?;
        }
    }
    Ok(())
}

fn record_property_tax_paid(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    agent_id: &str,
    amount: Money,
    rented_fraction_ppb: i64,
) -> Result<(), SimulationError> {
    let rental_deduction = Money(mul_div_round_half_up(
        amount.0,
        rented_fraction_ppb,
        RATE_SCALE_PPB,
        "rental property tax deduction",
    )?);
    let owner_property_tax = Money(mul_div_round_half_up(
        amount.0,
        RATE_SCALE_PPB - rented_fraction_ppb,
        RATE_SCALE_PPB,
        "owner property tax",
    )?);
    for ((taxpayer, _), facts) in tax_facts {
        if taxpayer == agent_id {
            facts.ordinary_income = facts.ordinary_income.checked_sub(rental_deduction)?;
            facts.property_tax_paid = facts.property_tax_paid.checked_add(owner_property_tax)?;
        }
    }
    Ok(())
}

fn accrue_property_depreciation(
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &mut [PropertyState],
) -> Result<(), SimulationError> {
    for property in properties
        .iter_mut()
        .filter(|property| property.active && property.rented_fraction_ppb > 0)
    {
        let monthly_factor_ppb = mul_div_round_half_up(
            property.rented_fraction_ppb,
            1,
            330,
            "property monthly depreciation factor",
        )?;
        let depreciation = Money(mul_div_round_half_up(
            property.building_basis.0,
            monthly_factor_ppb,
            RATE_SCALE_PPB,
            "property monthly depreciation",
        )?);
        property.cumulative_depreciation =
            property.cumulative_depreciation.checked_add(depreciation)?;
        property.depreciation_ytd = property.depreciation_ytd.checked_add(depreciation)?;
        for ((taxpayer, _), facts) in tax_facts.iter_mut() {
            if taxpayer == &property.owner_agent_id {
                facts.ordinary_income = facts.ordinary_income.checked_sub(depreciation)?;
                facts.depreciation_deduction =
                    facts.depreciation_deduction.checked_add(depreciation)?;
            }
        }
    }
    Ok(())
}

fn reset_property_tax_year_state(
    properties: &mut [PropertyState],
    mortgages: &mut [MortgageState],
) {
    for property in properties {
        property.depreciation_ytd = Money(0);
    }
    for mortgage in mortgages {
        mortgage.interest_paid_ytd = Money(0);
        mortgage.rental_interest_paid_ytd = Money(0);
        mortgage.principal_paid_ytd = Money(0);
    }
}

fn mortgage_interest_deduction_for(
    fixture: &Fixture,
    mortgages: &[MortgageState],
    agent_id: &str,
    jurisdiction_id: &str,
) -> Result<Money, SimulationError> {
    let mut scaled_total = 0_i128;
    for policy in fixture
        .scenario
        .mortgage_interest_deduction_policies
        .iter()
        .filter(|policy| policy.owner_agent_id == agent_id)
    {
        let Some(mortgage) = mortgages
            .iter()
            .find(|mortgage| mortgage.liability_id == policy.liability_id)
        else {
            continue;
        };
        let owner_interest = mortgage
            .interest_paid_ytd
            .checked_sub(mortgage.rental_interest_paid_ytd)?;
        let origination_principal = fixture
            .scenario
            .scheduled_property_purchases
            .iter()
            .filter_map(|purchase| purchase.mortgage.as_ref())
            .find(|spec| spec.liability_id == policy.liability_id)
            .expect("validated MID policy has a mortgage")
            .principal;
        let factor_ppb = if policy.debt_class == "home_equity" {
            0
        } else {
            let cap = if policy.per_jurisdiction_principal_cap.is_empty() {
                origination_principal
            } else {
                policy
                    .per_jurisdiction_principal_cap
                    .get(jurisdiction_id)
                    .copied()
                    .unwrap_or(Money(0))
            };
            mul_div_round_half_up(
                cap.0.min(origination_principal.0),
                RATE_SCALE_PPB,
                origination_principal.0,
                "mortgage-interest principal factor",
            )?
        };
        let scaled = i128::from(owner_interest.0)
            .checked_mul(i128::from(factor_ppb))
            .ok_or(ArithmeticError::Overflow {
                operation: "mortgage-interest scaled deduction",
            })?;
        scaled_total = scaled_total
            .checked_add(scaled)
            .ok_or(ArithmeticError::Overflow {
                operation: "mortgage-interest aggregate deduction",
            })?;
    }
    let denominator = i128::from(RATE_SCALE_PPB);
    let rounded = scaled_total / denominator
        + i128::from(scaled_total % denominator >= (denominator + 1) / 2);
    Ok(Money(i64::try_from(rounded).map_err(|_| {
        ArithmeticError::Overflow {
            operation: "mortgage-interest aggregate deduction",
        }
    })?))
}

fn accrue_year_end_taxes(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    tax_liabilities: &mut Vec<TaxLiabilityState>,
    mortgages: &[MortgageState],
    month: u32,
) -> Result<(), SimulationError> {
    for profile in &fixture.scenario.tax_profiles {
        let representative_key = (
            profile.agent_id.clone(),
            profile.jurisdictions[0].jurisdiction_id.clone(),
        );
        let representative = *tax_facts
            .get(&representative_key)
            .expect("validated tax profile has representative facts");
        let (net_short, net_long, ordinary_offset, shared_carryforward) = net_capital_gains(
            representative.short_term_gain,
            representative.long_term_gain,
            representative.capital_loss_carryforward,
            profile.jurisdictions[0].max_capital_loss_ordinary_offset,
        )?;
        for rules in &profile.jurisdictions {
            let facts = tax_facts
                .get_mut(&(profile.agent_id.clone(), rules.jurisdiction_id.clone()))
                .expect("validated tax profile has jurisdiction facts");
            facts.short_term_gain = net_short;
            facts.long_term_gain = net_long;
            facts.capital_loss_carryforward = Money(0);
            facts.ordinary_income = facts.ordinary_income.checked_sub(ordinary_offset)?;
        }
        let mut annual = BTreeMap::new();
        for rules in &profile.jurisdictions {
            let key = (profile.agent_id.clone(), rules.jurisdiction_id.clone());
            let facts = tax_facts
                .get_mut(&key)
                .expect("validated tax profile has initialized facts");
            facts.mortgage_interest_deduction = mortgage_interest_deduction_for(
                fixture,
                mortgages,
                &profile.agent_id,
                &rules.jurisdiction_id,
            )?;
            facts.salt_deduction = Money(0);
            facts.itemized_deduction = facts.mortgage_interest_deduction;
            let facts = *facts;
            let assessment = assess(facts, rules)?;
            annual.insert(rules.jurisdiction_id.clone(), (facts, assessment));
        }
        if let Some(policy) = fixture
            .scenario
            .federal_salt_deduction_policies
            .iter()
            .find(|policy| policy.profile_id == profile.agent_id)
        {
            let state_tax = annual
                .iter()
                .filter(|(jurisdiction_id, _)| {
                    jurisdiction_id.as_str() != policy.federal_jurisdiction_id
                })
                .try_fold(Money(0), |total, (_, (_, assessment))| {
                    total.checked_add(assessment.total_tax)
                })?;
            let property_tax = annual
                .get(&policy.federal_jurisdiction_id)
                .map_or(Money(0), |(facts, _)| facts.property_tax_paid);
            let salt_total = property_tax.checked_add(state_tax)?;
            let salt_cap = salt_cap_for(policy, month / 12);
            let salt_deduction = Money(salt_total.0.min(salt_cap.0));
            let federal_rules = profile
                .jurisdictions
                .iter()
                .find(|rules| rules.jurisdiction_id == policy.federal_jurisdiction_id)
                .expect("validated SALT policy has a federal tax link");
            let (facts, assessment) = annual
                .get_mut(&policy.federal_jurisdiction_id)
                .expect("validated SALT policy has annual facts");
            facts.salt_deduction = salt_deduction;
            facts.itemized_deduction = facts
                .mortgage_interest_deduction
                .checked_add(salt_deduction)?;
            *assessment = assess(*facts, federal_rules)?;
        }
        for rules in &profile.jurisdictions {
            let key = (profile.agent_id.clone(), rules.jurisdiction_id.clone());
            let (facts, assessment) = annual
                .remove(&rules.jurisdiction_id)
                .expect("annual tax assessment exists for every jurisdiction");
            let cause_id = format!(
                "{}_{}_year_end_accrual_m{month}",
                profile.agent_id, rules.jurisdiction_id
            );
            if assessment.total_tax != Money(0) {
                recorder.apply_entry(
                    ledger,
                    JournalEntry {
                        month,
                        cause_id: cause_id.clone(),
                        postings: vec![
                            Posting {
                                account: tax_expense_account(
                                    &profile.agent_id,
                                    &rules.jurisdiction_id,
                                ),
                                amount: assessment.total_tax,
                            },
                            Posting {
                                account: tax_liability_account(
                                    &profile.agent_id,
                                    &rules.jurisdiction_id,
                                ),
                                amount: assessment.total_tax.checked_neg()?,
                            },
                        ],
                    },
                )?;
            }
            tax_liabilities.push(TaxLiabilityState {
                agent_id: profile.agent_id.clone(),
                jurisdiction_id: rules.jurisdiction_id.clone(),
                tax_year_end_month: month,
                amount_owed: assessment.total_tax,
                active: true,
            });
            recorder.record_tax_accrual(TaxAccrual {
                month,
                cause_id,
                agent_id: profile.agent_id.clone(),
                jurisdiction_id: rules.jurisdiction_id.clone(),
                tax_year_end_month: month,
                ordinary_income: facts
                    .ordinary_income
                    .checked_sub(assessment.ordinary_loss_offset)?,
                short_term_gain: assessment.short_term_gain,
                long_term_gain: assessment.long_term_gain,
                section_1250_recapture: facts.section_1250_recapture,
                rental_interest_deduction: facts.rental_interest_deduction,
                depreciation_deduction: facts.depreciation_deduction,
                standard_deduction: rules.standard_deduction,
                mortgage_interest_deduction: facts.mortgage_interest_deduction,
                salt_deduction: facts.salt_deduction,
                itemized_deduction: facts.itemized_deduction,
                ordinary_taxable: assessment.ordinary_taxable,
                long_term_capital_gain_taxable: assessment.long_term_capital_gain_taxable,
                ordinary_tax: assessment.ordinary_tax,
                capital_gain_tax: assessment.capital_gain_tax,
                section_1250_tax: assessment.section_1250_tax,
                total_tax: assessment.total_tax,
                capital_loss_carryforward: shared_carryforward,
            })?;
            tax_facts.insert(
                key,
                TaxFacts {
                    capital_loss_carryforward: shared_carryforward,
                    ..TaxFacts::default()
                },
            );
        }
    }
    Ok(())
}

fn salt_cap_for(policy: &crate::fixture::FederalSaltDeductionSpec, year_index: u32) -> Money {
    if policy.cap_schedule.is_empty() {
        return Money(i64::MAX);
    }
    policy
        .cap_schedule
        .iter()
        .filter(|entry| entry.effective_year_index <= year_index)
        .max_by_key(|entry| entry.effective_year_index)
        .map_or(Money(0), |entry| entry.cap)
}

#[allow(clippy::too_many_arguments)]
fn settle_obligations(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    properties: &[PropertyState],
    mortgages: &mut [MortgageState],
    tax_liabilities: &mut [TaxLiabilityState],
    month: u32,
    obligations: &[ActiveObligation],
) -> Result<bool, SimulationError> {
    let mut due_by_source = BTreeMap::<AccountRef, Money>::new();
    for obligation in obligations {
        let due = due_by_source
            .get(&obligation.from)
            .copied()
            .unwrap_or_default()
            .checked_add(obligation.amount_due)?;
        due_by_source.insert(obligation.from.clone(), due);
    }
    let funded_by_source: BTreeMap<_, _> = due_by_source
        .into_iter()
        .map(|(account, due)| {
            let funded = ledger
                .balance(&account)
                .map(|available| available.0 >= due.0)?;
            Ok((account, funded))
        })
        .collect::<Result<_, LedgerError>>()?;

    let mut any_failure = false;
    for obligation in obligations {
        let funded = funded_by_source[&obligation.from];
        let firing_id = obligation.cause_id.clone();
        let attempted_funding_sources =
            target_allocation_attempted_sources(fixture, &obligation.from);
        let is_tax_payment = matches!(
            obligation.effect,
            ObligationEffect::TaxPayment { .. } | ObligationEffect::TaxTrueUp { .. }
        );
        let (amount_paid, shortfall) = if funded {
            match obligation.effect {
                ObligationEffect::None => transfer_money(
                    ledger,
                    recorder,
                    month,
                    &firing_id,
                    &obligation.from,
                    &obligation.to,
                    obligation.amount_due,
                )?,
                ObligationEffect::PropertyTax {
                    ref owner_agent_id,
                    rented_fraction_ppb,
                } => {
                    transfer_money(
                        ledger,
                        recorder,
                        month,
                        &firing_id,
                        &obligation.from,
                        &obligation.to,
                        obligation.amount_due,
                    )?;
                    record_property_tax_paid(
                        tax_facts,
                        owner_agent_id,
                        obligation.amount_due,
                        rented_fraction_ppb,
                    )?;
                }
                ObligationEffect::TaxPayment { profile_index } => {
                    book_tax_payment(
                        fixture,
                        ledger,
                        recorder,
                        month,
                        &firing_id,
                        profile_index,
                        obligation.amount_due,
                    )?;
                }
                ObligationEffect::TaxTrueUp {
                    profile_index,
                    tax_year_end_month,
                } => {
                    book_tax_payment(
                        fixture,
                        ledger,
                        recorder,
                        month,
                        &firing_id,
                        profile_index,
                        obligation.amount_due,
                    )?;
                    let profile = &fixture.scenario.tax_profiles[profile_index];
                    let settled = settle_tax_liabilities(
                        ledger,
                        recorder,
                        tax_liabilities,
                        month,
                        profile,
                        tax_year_end_month,
                    )?;
                    recorder.record_tax_settlement(TaxSettlementOutcome {
                        month,
                        cause_id: format!(
                            "{}_tax_settlement_y{}",
                            profile.agent_id,
                            (tax_year_end_month - 11) / 12
                        ),
                        agent_id: profile.agent_id.clone(),
                        tax_year_end_month,
                        amount: settled,
                    })?;
                }
                ObligationEffect::Mortgage {
                    mortgage_index,
                    interest,
                    principal,
                } => {
                    let mortgage = &mut mortgages[mortgage_index];
                    recorder.apply_entry(
                        ledger,
                        JournalEntry {
                            month,
                            cause_id: firing_id.clone(),
                            postings: vec![
                                Posting {
                                    account: obligation.from.clone(),
                                    amount: obligation.amount_due.checked_neg()?,
                                },
                                Posting {
                                    account: obligation.to.clone(),
                                    amount: obligation.amount_due,
                                },
                                Posting {
                                    account: mortgage_liability_account(
                                        &mortgage.agent_id,
                                        &mortgage.liability_id,
                                    ),
                                    amount: principal,
                                },
                                Posting {
                                    account: mortgage_interest_expense_account(
                                        &mortgage.agent_id,
                                        &mortgage.liability_id,
                                    ),
                                    amount: interest,
                                },
                                Posting {
                                    account: mortgage_receivable_account(
                                        &mortgage.counterparty_agent_id,
                                        &mortgage.liability_id,
                                    ),
                                    amount: principal.checked_neg()?,
                                },
                                Posting {
                                    account: mortgage_interest_income_account(
                                        &mortgage.counterparty_agent_id,
                                        &mortgage.liability_id,
                                    ),
                                    amount: interest.checked_neg()?,
                                },
                            ],
                        },
                    )?;
                    mortgage.principal = mortgage.principal.checked_sub(principal)?;
                    mortgage.interest_paid_ytd =
                        mortgage.interest_paid_ytd.checked_add(interest)?;
                    let rented_fraction_ppb = properties
                        .iter()
                        .find(|property| property.property_id == mortgage.property_id)
                        .map_or(0, |property| property.rented_fraction_ppb);
                    let rental_interest = Money(mul_div_round_half_up(
                        interest.0,
                        rented_fraction_ppb,
                        RATE_SCALE_PPB,
                        "rental mortgage interest",
                    )?);
                    mortgage.rental_interest_paid_ytd = mortgage
                        .rental_interest_paid_ytd
                        .checked_add(rental_interest)?;
                    record_rental_interest_deduction(
                        tax_facts,
                        &mortgage.agent_id,
                        rental_interest,
                    )?;
                    mortgage.principal_paid_ytd =
                        mortgage.principal_paid_ytd.checked_add(principal)?;
                    if mortgage.principal == Money(0) {
                        mortgage.active = false;
                    }
                    recorder.record_mortgage_payment(MortgagePaymentOutcome {
                        month,
                        cause_id: firing_id.clone(),
                        liability_id: mortgage.liability_id.clone(),
                        agent_id: mortgage.agent_id.clone(),
                        counterparty_agent_id: mortgage.counterparty_agent_id.clone(),
                        property_id: mortgage.property_id.clone(),
                        from_account_id: mortgage.payment_account_id.clone(),
                        to_account_id: mortgage.counterparty_account_id.clone(),
                        interest,
                        principal,
                        total_payment: obligation.amount_due,
                    })?;
                }
            }
            (obligation.amount_due, Money(0))
        } else {
            any_failure = true;
            (Money(0), obligation.amount_due)
        };
        if is_tax_payment {
            recorder.record_tax_payment(TaxPaymentOutcome {
                month,
                cause_id: firing_id.clone(),
                agent_id: obligation.from.agent_id.clone(),
                obligation_type: obligation.obligation_type.clone(),
                amount_due: obligation.amount_due,
                amount_paid,
                shortfall,
            })?;
        }
        if amount_paid.0 > 0 {
            recorder.record_transfer(TransferOutcome {
                month,
                cause_id: firing_id.clone(),
                from: obligation.from.clone(),
                to: obligation.to.clone(),
                amount: amount_paid,
                income_category: None,
            });
        }
        if !funded {
            recorder.record_rollout_failure(RolloutFailureOutcome {
                month,
                cause_id: format!("{firing_id}_failure"),
                agent_id: obligation.from.agent_id.clone(),
                deficit: shortfall,
                obligation_id: firing_id.clone(),
                obligation_type: obligation.obligation_type.clone(),
                amount_due: obligation.amount_due,
                amount_paid,
                shortfall,
                attempted_funding_sources: attempted_funding_sources.clone(),
            });
        }
        recorder.record_obligation(ObligationOutcome {
            month,
            cause_id: firing_id.clone(),
            obligation_id: firing_id,
            obligation_type: obligation.obligation_type.clone(),
            from: obligation.from.clone(),
            to: obligation.to.clone(),
            amount_due: obligation.amount_due,
            amount_paid,
            shortfall,
            attempted_funding_sources,
            failure_active: !funded,
        });
    }
    Ok(any_failure)
}

fn target_allocation_attempted_sources(fixture: &Fixture, account: &AccountRef) -> String {
    fixture
        .scenario
        .target_allocation_policies
        .iter()
        .find(|policy| {
            policy.agent_id == account.agent_id && policy.account_id == account.account_id
        })
        .map(|policy| {
            policy
                .sleeves
                .iter()
                .map(|sleeve| format!("security:{}", sleeve.asset_id))
                .collect::<Vec<_>>()
                .join(",")
        })
        .unwrap_or_default()
}

fn execute_tlh_harvest(
    fixture: &Fixture,
    rollout_id: u32,
    lots: &[LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    cumulative_harvest: &mut [Money],
    month: u32,
) -> Result<(), SimulationError> {
    for (policy_index, policy) in fixture.scenario.harvest_policies.iter().enumerate() {
        if !tax_facts
            .keys()
            .any(|(agent_id, _)| agent_id == &policy.owner_agent_id)
        {
            continue;
        }
        let policy_lots: Vec<&LotState> = lots
            .iter()
            .filter(|lot| {
                lot.spec.agent_id == policy.owner_agent_id
                    && lot.spec.account_id == policy.account_id
                    && lot.spec.asset_id == policy.asset_id
                    && lot.units_remaining.0 > 0
            })
            .collect();
        if policy_lots.is_empty() {
            continue;
        }
        let series_id = format!("security:{}", policy.asset_id);
        let price = series_value(fixture, &series_id, rollout_id, month)?;
        let prior_price = series_value(fixture, &series_id, rollout_id, month.saturating_sub(1))?;
        let market_value = policy_lots.iter().try_fold(Money(0), |total, lot| {
            total.checked_add(Money(mul_div_round_half_up(
                lot.units_remaining.0,
                price,
                lot.spec.quantity_scale,
                "TLH market value",
            )?))
        })?;
        let original_basis = policy_lots.iter().try_fold(Money(0), |total, lot| {
            total.checked_add(lot.basis_remaining)
        })?;
        let adjusted_basis = Money(
            original_basis
                .0
                .saturating_sub(cumulative_harvest[policy_index].0)
                .max(0),
        );
        let embedded_gain = if market_value.0 > 0 {
            ((market_value.0 - adjusted_basis.0) as f64 / market_value.0 as f64).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let period_return = if prior_price > 0 {
            (price - prior_price) as f64 / prior_price as f64
        } else {
            0.0
        };
        let peak = policy.peak_annual_yield_ppb as f64 / RATE_SCALE_PPB as f64;
        let floor = policy.floor_annual_yield_ppb as f64 / RATE_SCALE_PPB as f64;
        let gamma = policy.maturity_decay_exponent_ppb as f64 / RATE_SCALE_PPB as f64;
        let sensitivity = policy.drawdown_sensitivity_ppb as f64 / RATE_SCALE_PPB as f64;
        let maturity = (1.0 - embedded_gain).powf(gamma);
        let base_monthly = (floor + (peak - floor) * maturity) / 12.0;
        let fraction = base_monthly * (1.0 + sensitivity * (-period_return).max(0.0));
        let fraction_ppb = f64_factor_to_ppb(fraction, "TLH harvest fraction")?;
        let ceiling = Money(
            original_basis
                .0
                .saturating_sub(cumulative_harvest[policy_index].0)
                .max(0),
        );
        let gross = Money(
            mul_div_round_half_up(
                market_value.0,
                fraction_ppb,
                RATE_SCALE_PPB,
                "TLH gross harvest",
            )?
            .min(ceiling.0),
        );
        let short_term = Money(mul_div_round_half_up(
            gross.0,
            policy.short_term_fraction_ppb,
            RATE_SCALE_PPB,
            "TLH short-term fraction",
        )?);
        let long_term = gross.checked_sub(short_term)?;
        if short_term != Money(0) {
            record_capital_gain(
                tax_facts,
                &policy.owner_agent_id,
                short_term.checked_neg()?,
                false,
            )?;
        }
        if long_term != Money(0) {
            record_capital_gain(
                tax_facts,
                &policy.owner_agent_id,
                long_term.checked_neg()?,
                true,
            )?;
        }
        cumulative_harvest[policy_index] = cumulative_harvest[policy_index].checked_add(gross)?;
    }
    Ok(())
}

fn f64_factor_to_ppb(value: f64, operation: &'static str) -> Result<i64, SimulationError> {
    if !value.is_finite() || value < 0.0 {
        return Err(SimulationError::Arithmetic(ArithmeticError::Overflow {
            operation,
        }));
    }
    let scaled = value * RATE_SCALE_PPB as f64;
    if scaled > i64::MAX as f64 {
        return Err(SimulationError::Arithmetic(ArithmeticError::Overflow {
            operation,
        }));
    }
    Ok((scaled + 0.5).floor() as i64)
}

fn tlh_give_back_for_pool_sale(
    fixture: &Fixture,
    lots: &[LotState],
    planned: &[PlannedDisposition],
    cumulative_harvest: &mut [Money],
) -> Result<Vec<Money>, SimulationError> {
    let mut give_back = vec![Money(0); planned.len()];
    for (policy_index, policy) in fixture.scenario.harvest_policies.iter().enumerate() {
        let matching: Vec<(usize, &PlannedDisposition)> = planned
            .iter()
            .enumerate()
            .filter(|(_, item)| {
                let lot = &lots[item.lot_index];
                lot.spec.agent_id == policy.owner_agent_id
                    && lot.spec.account_id == policy.account_id
                    && lot.spec.asset_id == policy.asset_id
            })
            .collect();
        if matching.is_empty() || cumulative_harvest[policy_index] == Money(0) {
            continue;
        }
        let pre_sale_units = lots
            .iter()
            .filter(|lot| {
                lot.spec.agent_id == policy.owner_agent_id
                    && lot.spec.account_id == policy.account_id
                    && lot.spec.asset_id == policy.asset_id
            })
            .try_fold(0_i64, |total, lot| {
                total
                    .checked_add(lot.units_remaining.0)
                    .ok_or(ArithmeticError::Overflow {
                        operation: "TLH pre-sale units",
                    })
            })?;
        let sold_units = matching.iter().try_fold(0_i64, |total, (_, item)| {
            total
                .checked_add(item.units.0)
                .ok_or(ArithmeticError::Overflow {
                    operation: "TLH sold units",
                })
        })?;
        if pre_sale_units <= 0 || sold_units <= 0 {
            continue;
        }
        let total_give_back = Money(mul_div_round_half_up(
            cumulative_harvest[policy_index].0,
            sold_units,
            pre_sale_units,
            "TLH sale give-back",
        )?);
        let mut allocated = Money(0);
        for (planned_index, item) in matching {
            let amount = Money(mul_div_round_half_up(
                total_give_back.0,
                item.units.0,
                sold_units,
                "TLH per-lot give-back",
            )?);
            give_back[planned_index] = give_back[planned_index].checked_add(amount)?;
            allocated = allocated.checked_add(amount)?;
        }
        cumulative_harvest[policy_index] =
            cumulative_harvest[policy_index].checked_sub(allocated)?;
    }
    Ok(give_back)
}

fn scheduled_tlh_give_back_state(
    fixture: &Fixture,
    lots: &[LotState],
    cumulative_harvest: &[Money],
) -> Result<ScheduledTlhGiveBack, SimulationError> {
    let pre_sale_units = fixture
        .scenario
        .harvest_policies
        .iter()
        .map(|policy| {
            lots.iter()
                .filter(|lot| {
                    lot.spec.agent_id == policy.owner_agent_id
                        && lot.spec.account_id == policy.account_id
                        && lot.spec.asset_id == policy.asset_id
                })
                .try_fold(0_i64, |total, lot| {
                    total
                        .checked_add(lot.units_remaining.0)
                        .ok_or(ArithmeticError::Overflow {
                            operation: "TLH scheduled-sale units",
                        })
                })
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ScheduledTlhGiveBack {
        cumulative_start: cumulative_harvest.to_vec(),
        pre_sale_units,
        allocated: vec![Money(0); cumulative_harvest.len()],
    })
}

fn tlh_give_back_for_scheduled_sale(
    fixture: &Fixture,
    lots: &[LotState],
    planned: &[PlannedDisposition],
    state: &mut ScheduledTlhGiveBack,
) -> Result<Vec<Money>, SimulationError> {
    let mut give_back = vec![Money(0); planned.len()];
    for (policy_index, policy) in fixture.scenario.harvest_policies.iter().enumerate() {
        if state.cumulative_start[policy_index] == Money(0)
            || state.pre_sale_units[policy_index] <= 0
        {
            continue;
        }
        for (planned_index, item) in planned.iter().enumerate() {
            let lot = &lots[item.lot_index];
            if lot.spec.agent_id != policy.owner_agent_id
                || lot.spec.account_id != policy.account_id
                || lot.spec.asset_id != policy.asset_id
            {
                continue;
            }
            let amount = Money(mul_div_round_half_up(
                state.cumulative_start[policy_index].0,
                item.units.0,
                state.pre_sale_units[policy_index],
                "TLH scheduled-sale give-back",
            )?);
            give_back[planned_index] = give_back[planned_index].checked_add(amount)?;
            state.allocated[policy_index] = state.allocated[policy_index].checked_add(amount)?;
        }
    }
    Ok(give_back)
}

fn apply_scheduled_tlh_give_back(
    state: &ScheduledTlhGiveBack,
    cumulative_harvest: &mut [Money],
) -> Result<(), SimulationError> {
    for (cumulative, allocated) in cumulative_harvest.iter_mut().zip(&state.allocated) {
        *cumulative = cumulative.checked_sub(*allocated)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn execute_private_equity(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    tlh_cumulative_harvest: &mut [Money],
    month: u32,
) -> Result<(), SimulationError> {
    let issuers: BTreeSet<String> = lots
        .iter()
        .filter_map(|lot| private_equity_issuer(&lot.spec.asset_id).map(str::to_owned))
        .collect();
    for issuer_id in issuers {
        let asset_id = format!("{PE_ASSET_PREFIX}{issuer_id}");
        let mut candidates: Vec<usize> = lots
            .iter()
            .enumerate()
            .filter(|(_, lot)| lot.spec.asset_id == asset_id)
            .map(|(index, _)| index)
            .collect();
        candidates.sort_by_key(|index| (lots[*index].fifo_rank, lots[*index].spec.lot_id.clone()));
        let first = candidates[0];
        let owner_agent_id = lots[first].spec.agent_id.clone();
        let quantity_scale = lots[first].spec.quantity_scale;
        let mark = pe_channel_value(fixture, &issuer_id, "mark", rollout_id, month)?;
        let regime_code = pe_channel_value(fixture, &issuer_id, "regime", rollout_id, month)?;
        let event_kind_code =
            pe_channel_value(fixture, &issuer_id, "event_kind", rollout_id, month)?;
        let tender_active =
            pe_channel_value(fixture, &issuer_id, "sale_opportunity", rollout_id, month)? == 1;
        let capacity = pe_channel_value(fixture, &issuer_id, "sale_capacity", rollout_id, month)?;
        let eligible = pe_channel_value(fixture, &issuer_id, "eligible", rollout_id, month)?;
        let forced_sale = pe_channel_value(fixture, &issuer_id, "forced_sale", rollout_id, month)?;
        let liquidity_blocked =
            pe_channel_value(fixture, &issuer_id, "liquidity_blocked", rollout_id, month)? == 1;
        let forced_recovery =
            pe_channel_value(fixture, &issuer_id, "forced_recovery", rollout_id, month)?;
        let event_kind = private_equity_event_kind(event_kind_code);
        let regime = private_equity_regime(regime_code);
        if event_kind_code != 0 {
            recorder.record_private_equity_event(PrivateEquityProtocolOutcome {
                month,
                issuer_id: issuer_id.clone(),
                asset_id: asset_id.clone(),
                event_kind: event_kind.into(),
                regime: regime.into(),
                mark: Money(mark),
                sale_capacity_fraction_ppb: capacity,
                eligible_fraction_ppb: eligible,
                forced_sale_fraction_ppb: forced_sale,
                liquidity_blocked,
                forced_recovery_cashout: Money(forced_recovery),
            })?;
        }
        let units_held = pe_units_held(lots, &candidates)?;
        let Some(policy) = fixture
            .scenario
            .private_equity_tender_policies
            .iter()
            .find(|policy| policy.owner_agent_id == owner_agent_id)
        else {
            if tender_active {
                recorder.record_private_equity_opportunity(PrivateEquityOpportunityOutcome {
                    month,
                    cause_id: format!("pe_opportunity_m{month}_{issuer_id}"),
                    issuer_id: issuer_id.clone(),
                    asset_id,
                    event_kind: event_kind.into(),
                    regime: regime.into(),
                    outcome: "no_policy".into(),
                    mark: Money(mark),
                    sale_capacity_fraction_ppb: capacity,
                    eligible_fraction_ppb: eligible,
                    liquidity_blocked,
                    floor: Money(0),
                    liquid_net_worth: Money(0),
                    shortfall: Money(0),
                    quantity_scale,
                    units_held: Quantity(units_held),
                    sellable_units: Quantity(pe_sellable_units(units_held, capacity, eligible)?),
                    target_units: Quantity(0),
                    proceeds: Money(0),
                })?;
            }
            continue;
        };

        if forced_recovery > 0 && units_held > 0 {
            let recovery_price = mul_div_round_half_up(
                forced_recovery,
                quantity_scale,
                units_held,
                "private-equity recovery price",
            )?;
            execute_target_allocation_pool_sale(
                fixture,
                ledger,
                recorder,
                lots,
                tax_facts,
                tlh_cumulative_harvest,
                month,
                &format!("pe_forced_recovery_m{month}_{issuer_id}"),
                &owner_agent_id,
                &policy.proceeds_account_id,
                recovery_price,
                units_held,
                Some(&owner_agent_id),
                &candidates,
            )?;
        }
        let units_after_recovery = pe_units_held(lots, &candidates)?;
        if forced_sale > 0 && mark > 0 && units_after_recovery > 0 {
            let forced_target = mul_div_round_half_up(
                units_after_recovery,
                forced_sale,
                RATE_SCALE_PPB,
                "private-equity forced-sale quantity",
            )?
            .min(units_after_recovery);
            execute_target_allocation_pool_sale(
                fixture,
                ledger,
                recorder,
                lots,
                tax_facts,
                tlh_cumulative_harvest,
                month,
                &format!("pe_forced_sale_m{month}_{issuer_id}"),
                &owner_agent_id,
                &policy.proceeds_account_id,
                mark,
                forced_target,
                Some(&owner_agent_id),
                &candidates,
            )?;
        }

        let floor = amount_value(fixture, rollout_id, month, &policy.liquid_net_worth_floor)?;
        let liquid_net_worth = private_equity_liquid_net_worth(
            fixture,
            rollout_id,
            ledger,
            lots,
            &owner_agent_id,
            month,
        )?;
        let shortfall = Money((floor.0 - liquid_net_worth.0).max(0));
        let units_after_forced = pe_units_held(lots, &candidates)?;
        let sellable = pe_sellable_units(units_after_forced, capacity, eligible)?;
        let shortfall_units = if mark > 0 {
            ceil_quantity_for_money(shortfall.0, mark, quantity_scale)?
        } else {
            0
        };
        let public_active = regime_code == 2;
        let opportunity_active = (tender_active || public_active) && !liquidity_blocked && mark > 0;
        let target = if opportunity_active {
            shortfall_units.min(sellable)
        } else {
            0
        };
        let mut outcome = "sold";
        if shortfall.0 <= 0 {
            outcome = "floor_satisfied";
        }
        if capacity == 0 || eligible == 0 {
            outcome = "capacity_zero";
        }
        if mark <= 0 {
            outcome = "nonpositive_mark";
        }
        if liquidity_blocked {
            outcome = "liquidity_blocked";
        }
        if units_after_forced <= 0 {
            outcome = "no_units";
        }
        if tender_active {
            recorder.record_private_equity_opportunity(PrivateEquityOpportunityOutcome {
                month,
                cause_id: format!("pe_opportunity_m{month}_{issuer_id}"),
                issuer_id: issuer_id.clone(),
                asset_id: asset_id.clone(),
                event_kind: event_kind.into(),
                regime: regime.into(),
                outcome: outcome.into(),
                mark: Money(mark),
                sale_capacity_fraction_ppb: capacity,
                eligible_fraction_ppb: eligible,
                liquidity_blocked,
                floor,
                liquid_net_worth,
                shortfall,
                quantity_scale,
                units_held: Quantity(units_after_forced),
                sellable_units: Quantity(sellable),
                target_units: Quantity(target),
                proceeds: Money(mul_div_round_half_up(
                    target,
                    mark,
                    quantity_scale,
                    "private-equity opportunity proceeds",
                )?),
            })?;
        }
        if target > 0 {
            let cause_id = if public_active {
                format!("pe_public_market_m{month}_{issuer_id}")
            } else {
                format!("pe_tender_m{month}_{issuer_id}")
            };
            execute_target_allocation_pool_sale(
                fixture,
                ledger,
                recorder,
                lots,
                tax_facts,
                tlh_cumulative_harvest,
                month,
                &cause_id,
                &owner_agent_id,
                &policy.proceeds_account_id,
                mark,
                target,
                Some(&owner_agent_id),
                &candidates,
            )?;
        }
    }
    Ok(())
}

fn pe_channel_value(
    fixture: &Fixture,
    issuer_id: &str,
    channel: &str,
    rollout_id: u32,
    month: u32,
) -> Result<i64, SimulationError> {
    series_value(
        fixture,
        &private_equity_series_id(channel, issuer_id),
        rollout_id,
        month,
    )
}

fn pe_units_held(lots: &[LotState], candidates: &[usize]) -> Result<i64, SimulationError> {
    candidates.iter().try_fold(0_i64, |sum, index| {
        sum.checked_add(lots[*index].units_remaining.0)
            .ok_or(ArithmeticError::Overflow {
                operation: "private-equity units held",
            })
            .map_err(SimulationError::from)
    })
}

fn pe_sellable_units(units: i64, capacity: i64, eligible: i64) -> Result<i64, SimulationError> {
    let factor = i128::from(capacity)
        .checked_mul(i128::from(eligible))
        .ok_or(ArithmeticError::Overflow {
            operation: "private-equity sellable factor",
        })?;
    let denominator = i128::from(RATE_SCALE_PPB) * i128::from(RATE_SCALE_PPB);
    let result = mul_div_i128_round_half_up(
        i128::from(units),
        factor,
        denominator,
        "private-equity sellable quantity",
    )?;
    i64::try_from(result).map_err(|_| {
        SimulationError::Arithmetic(ArithmeticError::Overflow {
            operation: "private-equity sellable quantity",
        })
    })
}

fn ceil_quantity_for_money(value: i64, price: i64, scale: i64) -> Result<i64, SimulationError> {
    let numerator =
        i128::from(value)
            .checked_mul(i128::from(scale))
            .ok_or(ArithmeticError::Overflow {
                operation: "private-equity shortfall quantity",
            })?;
    let denominator = i128::from(price);
    let rounded = numerator
        .checked_add(denominator - 1)
        .ok_or(ArithmeticError::Overflow {
            operation: "private-equity shortfall quantity",
        })?
        / denominator;
    i64::try_from(rounded).map_err(|_| {
        SimulationError::Arithmetic(ArithmeticError::Overflow {
            operation: "private-equity shortfall quantity",
        })
    })
}

fn private_equity_liquid_net_worth(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &Ledger,
    lots: &[LotState],
    owner_agent_id: &str,
    month: u32,
) -> Result<Money, SimulationError> {
    let mut total = fixture
        .scenario
        .accounts
        .iter()
        .filter(|account| account.account.agent_id == owner_agent_id)
        .try_fold(Money(0), |sum, account| {
            sum.checked_add(ledger.balance(&account.account)?)
                .map_err(SimulationError::from)
        })?;
    for lot in lots.iter().filter(|lot| {
        lot.spec.agent_id == owner_agent_id
            && private_equity_issuer(&lot.spec.asset_id).is_none()
            && lot.units_remaining.0 > 0
    }) {
        let series_id = format!("security:{}", lot.spec.asset_id);
        let Some(price) = fixture
            .series
            .iter()
            .find(|series| series.series_id == series_id)
            .and_then(|series| series.value(rollout_id, month))
        else {
            continue;
        };
        total = total.checked_add(Money(mul_div_round_half_up(
            lot.units_remaining.0,
            price,
            lot.spec.quantity_scale,
            "private-equity liquid lot value",
        )?))?;
    }
    Ok(total)
}

fn private_equity_regime(code: i64) -> &'static str {
    match code {
        1 => "private_operating",
        2 => "public_market",
        3 => "acquired",
        4 => "collapsed",
        _ => unreachable!("validated private-equity regime code"),
    }
}

fn private_equity_event_kind(code: i64) -> &'static str {
    match code {
        0 => "none",
        1 => "tender",
        2 => "admin_mark_update",
        3 => "public_market_open",
        4 => "acquisition_cashout",
        5 => "legal_impairment",
        6 => "forced_recovery",
        7 => "collapse",
        _ => unreachable!("validated private-equity event-kind code"),
    }
}

fn book_tax_payment(
    fixture: &Fixture,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    month: u32,
    cause_id: &str,
    profile_index: usize,
    amount: Money,
) -> Result<(), SimulationError> {
    if amount == Money(0) {
        return Ok(());
    }
    let profile = &fixture.scenario.tax_profiles[profile_index];
    recorder.apply_entry(
        ledger,
        JournalEntry {
            month,
            cause_id: cause_id.into(),
            postings: vec![
                Posting {
                    account: AccountRef::new(&profile.agent_id, &profile.payment_account_id),
                    amount: amount.checked_neg()?,
                },
                Posting {
                    account: AccountRef::new(
                        &profile.tax_authority_agent_id,
                        &profile.tax_authority_account_id,
                    ),
                    amount,
                },
                Posting {
                    account: tax_prepayment_account(&profile.agent_id),
                    amount,
                },
                Posting {
                    account: tax_authority_revenue_account(&profile.tax_authority_agent_id),
                    amount: amount.checked_neg()?,
                },
            ],
        },
    )
}

fn settle_tax_liabilities(
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_liabilities: &mut [TaxLiabilityState],
    month: u32,
    profile: &crate::fixture::TaxProfileSpec,
    tax_year_end_month: u32,
) -> Result<Money, SimulationError> {
    let matching: Vec<usize> = tax_liabilities
        .iter()
        .enumerate()
        .filter(|(_, liability)| {
            liability.active
                && liability.agent_id == profile.agent_id
                && liability.tax_year_end_month == tax_year_end_month
        })
        .map(|(index, _)| index)
        .collect();
    let total = matching.iter().try_fold(Money(0), |sum, index| {
        sum.checked_add(tax_liabilities[*index].amount_owed)
    })?;
    if total != Money(0) {
        let mut postings = matching
            .iter()
            .filter_map(|index| {
                let liability = &tax_liabilities[*index];
                (liability.amount_owed != Money(0)).then(|| Posting {
                    account: tax_liability_account(&liability.agent_id, &liability.jurisdiction_id),
                    amount: liability.amount_owed,
                })
            })
            .collect::<Vec<_>>();
        postings.push(Posting {
            account: tax_prepayment_account(&profile.agent_id),
            amount: total.checked_neg()?,
        });
        recorder.apply_entry(
            ledger,
            JournalEntry {
                month,
                cause_id: format!(
                    "{}_tax_settlement_y{}",
                    profile.agent_id,
                    (tax_year_end_month - 11) / 12
                ),
                postings,
            },
        )?;
    }
    for index in matching {
        tax_liabilities[index].amount_owed = Money(0);
    }
    Ok(total)
}

fn transfer_money(
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    month: u32,
    cause_id: &str,
    from: &AccountRef,
    to: &AccountRef,
    amount: Money,
) -> Result<(), SimulationError> {
    recorder.apply_entry(
        ledger,
        JournalEntry {
            month,
            cause_id: cause_id.into(),
            postings: vec![
                Posting {
                    account: from.clone(),
                    amount: amount.checked_neg()?,
                },
                Posting {
                    account: to.clone(),
                    amount,
                },
            ],
        },
    )
}

#[allow(clippy::too_many_arguments)]
fn execute_sale(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    scheduled_tlh: &mut ScheduledTlhGiveBack,
    sale: &crate::fixture::ScheduledSaleSpec,
) -> Result<(), SimulationError> {
    let mut candidates: Vec<usize> = lots
        .iter()
        .enumerate()
        .filter(|(_, lot)| {
            lot.spec.agent_id == sale.agent_id
                && lot.spec.account_id == sale.account_id
                && lot.spec.asset_id == sale.asset_id
                && lot.units_remaining.0 > 0
        })
        .map(|(index, _)| index)
        .collect();
    if candidates.is_empty() {
        return Err(SimulationError::MissingSalePool {
            cause_id: sale.cause_id.clone(),
            agent_id: sale.agent_id.clone(),
            account_id: sale.account_id.clone(),
            asset_id: sale.asset_id.clone(),
        });
    }
    candidates.sort_by_key(|index| (lots[*index].fifo_rank, lots[*index].spec.lot_id.clone()));
    let available = candidates.iter().try_fold(0_i64, |total, index| {
        total
            .checked_add(lots[*index].units_remaining.0)
            .ok_or(ArithmeticError::Overflow {
                operation: "sale pool quantity",
            })
    })?;
    if sale.units.0 > available {
        return Err(SimulationError::InsufficientLotUnits {
            cause_id: sale.cause_id.clone(),
            requested: sale.units.0,
            available,
        });
    }
    let series_id = format!("security:{}", sale.asset_id);
    let price = Money(series_value(fixture, &series_id, rollout_id, sale.month)?);
    let mut remaining = sale.units.0;
    let mut planned = Vec::new();
    let mut total_proceeds = Money(0);
    let mut total_gain = Money(0);
    for index in candidates {
        if remaining == 0 {
            break;
        }
        let lot = &lots[index];
        let units = remaining.min(lot.units_remaining.0);
        let basis = Money(mul_div_round_half_up(
            lot.basis_per_unit.0,
            units,
            lot.spec.quantity_scale,
            "FIFO basis allocation",
        )?);
        let proceeds = Money(mul_div_round_half_up(
            price.0,
            units,
            lot.spec.quantity_scale,
            "sale proceeds",
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
    let tlh_give_back = tlh_give_back_for_scheduled_sale(fixture, lots, &planned, scheduled_tlh)?;

    let mut postings = Vec::with_capacity(planned.len() + 2);
    postings.push(Posting {
        account: AccountRef::new(&sale.agent_id, &sale.proceeds_account_id),
        amount: total_proceeds,
    });
    for item in &planned {
        postings.push(Posting {
            account: asset_basis_account(&lots[item.lot_index].spec),
            amount: item.basis.checked_neg()?,
        });
    }
    postings.push(Posting {
        account: realized_gain_account(&sale.agent_id),
        amount: total_gain.checked_neg()?,
    });
    recorder.apply_entry(
        ledger,
        JournalEntry {
            month: sale.month,
            cause_id: sale.cause_id.clone(),
            postings,
        },
    )?;

    for (item, give_back) in planned.into_iter().zip(tlh_give_back) {
        let lot = &mut lots[item.lot_index];
        let long_term = i64::from(sale.month) - i64::from(lot.spec.purchase_month) >= 12;
        lot.units_remaining.0 -= item.units.0;
        lot.basis_remaining = lot.basis_remaining.checked_sub(item.basis)?;
        record_capital_gain(
            tax_facts,
            &sale.agent_id,
            item.realized_gain.checked_add(give_back)?,
            long_term,
        )?;
        recorder.record_disposition(LotDisposition {
            month: sale.month,
            cause_id: sale.cause_id.clone(),
            agent_id: lot.spec.agent_id.clone(),
            source_account_id: lot.spec.account_id.clone(),
            asset_id: canonical_lot_asset_id(&lot.spec.asset_id),
            lot_id: lot.spec.lot_id.clone(),
            purchase_month: lot.spec.purchase_month,
            quantity_scale: lot.spec.quantity_scale,
            units: item.units,
            basis: item.basis,
            proceeds: item.proceeds,
            proceeds_account_id: sale.proceeds_account_id.clone(),
            realized_gain: item.realized_gain,
        })?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn execute_target_allocation_sales(
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
fn execute_target_allocation_buys(
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
fn execute_target_allocation_pool_sale(
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

fn series_value(
    fixture: &Fixture,
    series_id: &str,
    rollout: u32,
    snapshot: u32,
) -> Result<i64, SimulationError> {
    let series: &SeriesSpec = fixture
        .series
        .iter()
        .find(|series| series.series_id == series_id)
        .ok_or_else(|| SimulationError::MissingSeries {
            series_id: series_id.into(),
        })?;
    series
        .value(rollout, snapshot)
        .ok_or_else(|| SimulationError::MissingSeriesValue {
            series_id: series_id.into(),
            rollout,
            snapshot,
        })
}

fn amount_value(
    fixture: &Fixture,
    rollout: u32,
    month: u32,
    amount: &AmountSpec,
) -> Result<Money, SimulationError> {
    match amount {
        AmountSpec::Fixed(amount) => Ok(*amount),
        AmountSpec::FixedSchedule(amount) => Ok(amount.amount),
        AmountSpec::SeriesIndexed(amount) => {
            let elapsed = month - amount.base_month_index;
            let reset_month = amount.base_month_index
                + (elapsed / amount.adjustment_period_months) * amount.adjustment_period_months;
            let base_level =
                series_value(fixture, &amount.series_id, rollout, amount.base_month_index)?;
            let reset_level = series_value(fixture, &amount.series_id, rollout, reset_month)?;
            Ok(Money(mul_div_round_half_up(
                amount.base_amount.0,
                reset_level,
                base_level,
                "series-indexed amount",
            )?))
        }
    }
}

fn bond_is_active(bond: &BondSpec, snapshot_month: u32) -> bool {
    i64::from(bond.purchase_month_index) <= i64::from(snapshot_month)
        && i64::from(snapshot_month) < i64::from(bond.maturity_month_index)
}

fn bond_pays(bond: &BondSpec, month: u32) -> bool {
    let elapsed = i64::from(month) - i64::from(bond.purchase_month_index);
    elapsed > 0
        && i64::from(month) <= i64::from(bond.maturity_month_index)
        && elapsed % i64::from(bond.coupon_period_months) == 0
}

fn bond_principal(
    fixture: &Fixture,
    rollout_id: u32,
    bond: &BondSpec,
    snapshot_month: u32,
) -> Result<Money, SimulationError> {
    if !bond.inflation_indexed {
        return Ok(bond.face_value);
    }
    let base_month = bond.purchase_month_index.max(0) as u32;
    let base_level = series_value(fixture, "inflation", rollout_id, base_month)?;
    let level = series_value(fixture, "inflation", rollout_id, snapshot_month)?;
    Ok(Money(mul_div_round_half_up(
        bond.face_value.0,
        level,
        base_level,
        "bond indexed principal",
    )?))
}

fn bond_period_rate_ppb(bond: &BondSpec) -> Result<i64, SimulationError> {
    mul_div_round_half_up(
        bond.annual_coupon_rate_ppb,
        i64::from(bond.coupon_period_months),
        12,
        "bond period rate",
    )
    .map_err(Into::into)
}

fn bond_coupon(principal: Money, bond: &BondSpec) -> Result<Money, SimulationError> {
    let coupon = if bond.inflation_indexed {
        i128::from(mul_div_round_half_up(
            principal.0,
            bond_period_rate_ppb(bond)?,
            RATE_SCALE,
            "indexed bond coupon",
        )?)
    } else {
        let rate_times_period = i128::from(bond.annual_coupon_rate_ppb)
            .checked_mul(i128::from(bond.coupon_period_months))
            .ok_or(ArithmeticError::Overflow {
                operation: "nominal bond coupon rate",
            })?;
        mul_div_i128_round_half_up(
            i128::from(principal.0),
            rate_times_period,
            i128::from(RATE_SCALE) * 12,
            "nominal bond coupon",
        )?
    };
    Ok(Money(i64::try_from(coupon).map_err(|_| {
        ArithmeticError::Overflow {
            operation: "bond coupon",
        }
    })?))
}

fn bond_states(
    fixture: &Fixture,
    rollout_id: u32,
    snapshot_month: u32,
    failed: bool,
) -> Result<Vec<BondState>, SimulationError> {
    fixture
        .scenario
        .initial_bonds
        .iter()
        .map(|bond| {
            let active = !failed && bond_is_active(bond, snapshot_month);
            Ok(BondState {
                bond_id: bond.bond_id.clone(),
                agent_id: bond.agent_id.clone(),
                account_id: bond.account_id.clone(),
                principal: if active {
                    bond_principal(fixture, rollout_id, bond, snapshot_month)?
                } else {
                    Money(0)
                },
                active,
            })
        })
        .collect()
}

fn record_interest_income(
    fixture: &Fixture,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    recipient_agent_id: &str,
    issuer_jurisdiction_id: Option<&str>,
    issuer_level: Option<JurisdictionLevel>,
    amount: Money,
) -> Result<(), SimulationError> {
    for profile in fixture
        .scenario
        .tax_profiles
        .iter()
        .filter(|profile| profile.agent_id == recipient_agent_id)
    {
        for rules in &profile.jurisdictions {
            if rules.taxes_interest_from(issuer_jurisdiction_id, issuer_level) {
                let facts = tax_facts
                    .get_mut(&(profile.agent_id.clone(), rules.jurisdiction_id.clone()))
                    .expect("validated tax facts must exist for every profile jurisdiction");
                facts.interest_income = facts.interest_income.checked_add(amount)?;
            }
        }
    }
    Ok(())
}

fn jurisdiction_level(
    fixture: &Fixture,
    issuer_jurisdiction_id: Option<&str>,
) -> Option<JurisdictionLevel> {
    let issuer = issuer_jurisdiction_id?;
    fixture
        .scenario
        .jurisdictions
        .iter()
        .find(|jurisdiction| jurisdiction.jurisdiction_id == issuer)
        .map(|jurisdiction| jurisdiction.level)
}

fn execute_bonds(
    fixture: &Fixture,
    rollout_id: u32,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    tax_facts: &mut BTreeMap<(String, String), TaxFacts>,
    month: u32,
) -> Result<(), SimulationError> {
    for bond in &fixture.scenario.initial_bonds {
        let principal = bond_principal(fixture, rollout_id, bond, month)?;
        let coupon = if bond_pays(bond, month) {
            bond_coupon(principal, bond)?
        } else {
            Money(0)
        };
        let redemption = if i64::from(month) == i64::from(bond.maturity_month_index) {
            if bond.inflation_indexed {
                Money(principal.0.max(bond.face_value.0))
            } else {
                bond.face_value
            }
        } else {
            Money(0)
        };
        let accretion = if bond.inflation_indexed && month > 0 && bond_is_active(bond, month) {
            principal.checked_sub(bond_principal(fixture, rollout_id, bond, month - 1)?)?
        } else {
            Money(0)
        };
        let paid = coupon.checked_add(redemption)?;
        if paid != Money(0) {
            let cause_id = format!("bond:{}:m{month}", bond.bond_id);
            transfer_money(
                ledger,
                recorder,
                month,
                &cause_id,
                &AccountRef::new(EXTERNAL_AGENT, "boundary"),
                &AccountRef::new(&bond.agent_id, &bond.account_id),
                paid,
            )?;
        }
        let income = coupon.checked_add(accretion)?;
        if income != Money(0) {
            record_interest_income(
                fixture,
                tax_facts,
                &bond.agent_id,
                bond.issuer_jurisdiction_id.as_deref(),
                jurisdiction_level(fixture, bond.issuer_jurisdiction_id.as_deref()),
                income,
            )?;
        }
        if coupon != Money(0) || accretion != Money(0) || redemption != Money(0) {
            recorder.record_bond_cashflow(BondCashflowOutcome {
                month,
                cause_id: format!("bond:{}:m{month}", bond.bond_id),
                bond_id: bond.bond_id.clone(),
                agent_id: bond.agent_id.clone(),
                account_id: bond.account_id.clone(),
                issuer_jurisdiction_id: bond.issuer_jurisdiction_id.clone(),
                coupon,
                accretion,
                redemption,
                principal,
            })?;
        }
    }
    Ok(())
}

fn asset_basis_account(lot: &InitialLotSpec) -> AccountRef {
    AccountRef::new(
        &lot.agent_id,
        format!("asset-basis:{}:{}", lot.account_id, lot.asset_id),
    )
}

fn realized_gain_account(agent_id: &str) -> AccountRef {
    AccountRef::new(agent_id, "income:realized-gain")
}

fn property_basis_writeoff_account(agent_id: &str, property_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("expense:property-basis:{property_id}"))
}

fn tax_expense_account(agent_id: &str, jurisdiction_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("expense:tax:{jurisdiction_id}"))
}

fn tax_liability_account(agent_id: &str, jurisdiction_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("liability:tax:{jurisdiction_id}"))
}

fn tax_prepayment_account(agent_id: &str) -> AccountRef {
    AccountRef::new(agent_id, "asset:tax-prepayments")
}

fn tax_authority_revenue_account(agent_id: &str) -> AccountRef {
    AccountRef::new(agent_id, "income:tax-payments")
}

fn property_asset_account(agent_id: &str, property_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("asset:property:{property_id}"))
}

fn property_sale_clearing_account(agent_id: &str, property_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("equity:property-sale:{property_id}"))
}

fn mortgage_liability_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("liability:mortgage:{liability_id}"))
}

fn mortgage_interest_expense_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(
        agent_id,
        format!("expense:mortgage-interest:{liability_id}"),
    )
}

fn mortgage_receivable_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(
        agent_id,
        format!("asset:mortgage-receivable:{liability_id}"),
    )
}

fn mortgage_interest_income_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("income:mortgage-interest:{liability_id}"))
}

fn mortgage_funding_account(agent_id: &str, liability_id: &str) -> AccountRef {
    AccountRef::new(agent_id, format!("equity:mortgage-funding:{liability_id}"))
}

#[allow(clippy::too_many_arguments)]
fn month_output(
    fixture: &Fixture,
    rollout_id: u32,
    month: u32,
    ledger: &Ledger,
    lots: &[LotState],
    properties: &[PropertyState],
    mortgages: &[MortgageState],
    tax_liabilities: &[TaxLiabilityState],
    tax_facts: &BTreeMap<(String, String), TaxFacts>,
    tlh_cumulative_harvest: &[Money],
    failed: bool,
) -> Result<MonthOutput, SimulationError> {
    Ok(MonthOutput {
        month,
        balances: account_balances(ledger, failed),
        lots: security_lot_states(lots, failed),
        bonds: bond_states(fixture, rollout_id, month, failed)?,
        properties: property_states(properties, failed),
        mortgages: mortgage_states(mortgages, failed),
        tax_liabilities: tax_liability_states(tax_liabilities, failed),
        capital_gains: capital_gain_states(fixture, tax_facts, failed),
        tlh_cumulative_harvest: if failed {
            vec![Money(0); tlh_cumulative_harvest.len()]
        } else {
            tlh_cumulative_harvest.to_vec()
        },
        failed,
    })
}

fn capital_gain_states(
    fixture: &Fixture,
    tax_facts: &BTreeMap<(String, String), TaxFacts>,
    failed: bool,
) -> Vec<CapitalGainState> {
    fixture
        .scenario
        .tax_profiles
        .iter()
        .map(|profile| {
            let facts = tax_facts
                .get(&(
                    profile.agent_id.clone(),
                    profile.jurisdictions[0].jurisdiction_id.clone(),
                ))
                .expect("validated tax profile has representative facts");
            CapitalGainState {
                agent_id: profile.agent_id.clone(),
                short_term_gain: if failed {
                    Money(0)
                } else {
                    facts.short_term_gain
                },
                long_term_gain: if failed {
                    Money(0)
                } else {
                    facts.long_term_gain
                },
            }
        })
        .collect()
}

fn security_lot_states(lots: &[LotState], failed: bool) -> Vec<SecurityLotState> {
    lots.iter()
        .map(|lot| SecurityLotState {
            lot_id: lot.spec.lot_id.clone(),
            agent_id: lot.spec.agent_id.clone(),
            account_id: lot.spec.account_id.clone(),
            asset_id: canonical_lot_asset_id(&lot.spec.asset_id),
            purchase_month: lot.spec.purchase_month,
            quantity_scale: lot.spec.quantity_scale,
            units_remaining: if failed {
                Quantity(0)
            } else {
                lot.units_remaining
            },
            basis_remaining: if failed {
                Money(0)
            } else {
                lot.basis_remaining
            },
            cost_basis_per_unit: lot.basis_per_unit,
        })
        .collect()
}

fn tax_liability_states(
    tax_liabilities: &[TaxLiabilityState],
    failed: bool,
) -> Vec<TaxLiabilityState> {
    tax_liabilities
        .iter()
        .cloned()
        .map(|mut liability| {
            if failed {
                liability.amount_owed = Money(0);
            }
            liability
        })
        .collect()
}

fn property_states(properties: &[PropertyState], failed: bool) -> Vec<PropertyState> {
    properties
        .iter()
        .cloned()
        .map(|mut property| {
            if failed {
                property.adjusted_basis = Money(0);
                property.contribution_used = Money(0);
                property.equity_ledger = Money(0);
            }
            property
        })
        .collect()
}

fn mortgage_states(mortgages: &[MortgageState], failed: bool) -> Vec<MortgageState> {
    mortgages
        .iter()
        .cloned()
        .map(|mut mortgage| {
            if failed {
                mortgage.monthly_payment = Money(0);
                mortgage.principal = Money(0);
                mortgage.interest_paid_ytd = Money(0);
                mortgage.principal_paid_ytd = Money(0);
            }
            mortgage
        })
        .collect()
}

fn account_balances(ledger: &Ledger, failed: bool) -> Vec<AccountBalance> {
    ledger
        .balances()
        .iter()
        .map(|(account, balance)| AccountBalance {
            account: account.clone(),
            balance: if failed { Money(0) } else { *balance },
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use crate::fixture::{
        AccountSpec, BondSpec, DistributionSpec, DistributionTaxSliceSpec, InitialLotSpec,
        JurisdictionIdentitySpec, LocationSpec, MortgageFinancingSpec, ObligationSpec,
        PropertyTaxPolicySpec, RecurringObligationSpec, ScenarioSpec,
        ScheduledPropertyPurchaseSpec, ScheduledSaleSpec, ScheduledTransferSpec,
        SeriesIndexedAmountKind, SeriesIndexedAmountSpec, SeriesSpec,
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
        if let AmountSpec::SeriesIndexed(amount) =
            &mut fixture.scenario.scheduled_transfers[0].amount
        {
            amount.adjustment_period_months = 0;
        }
        assert!(matches!(
            simulate(&fixture),
            Err(SimulationError::InvalidSeriesIndexedAmount { .. })
        ));

        if let AmountSpec::SeriesIndexed(amount) =
            &mut fixture.scenario.scheduled_transfers[0].amount
        {
            amount.adjustment_period_months = 1;
            amount.series_id = "security:vti".into();
        }
        assert!(matches!(
            simulate(&fixture),
            Err(SimulationError::UnsupportedAmountSeries { .. })
        ));

        if let AmountSpec::SeriesIndexed(amount) =
            &mut fixture.scenario.scheduled_transfers[0].amount
        {
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
        assert_eq!(final_month.mortgages[0].principal_paid_ytd, Money(39_820));
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
                    },
                    RecurringObligationSpec {
                        start_month: 1,
                        end_month: Some(2),
                        obligation_id: "utility".into(),
                        obligation_type: "cash_spend".into(),
                        from: alice_cash,
                        to: utility_cash,
                        amount_due: Money(1).into(),
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
}
