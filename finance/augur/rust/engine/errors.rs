//! Every way a simulation can refuse to run or fail mid-rollout.
//!
//! One enum rather than per-phase error types: a caller handles a rejected fixture and a
//! failed rollout at the same boundary, and the variants carry the identifiers needed to
//! locate the offending row.

use super::*;

#[derive(Debug, Error)]
pub enum SimulationError {
    #[error(transparent)]
    Product(#[from] ProductError),
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
    #[error("{kind} {cause_id:?} references unknown property {property_id:?}")]
    UnknownPropertyReference {
        kind: &'static str,
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
