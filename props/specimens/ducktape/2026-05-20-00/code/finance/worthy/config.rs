use alphavantage_converter::AlphaVantageConverterConfig;
use asset::Asset;
use currencylayer_converter::CurrencyLayerConverterConfig;
use fixer_converter::FixerConverterConfig;
use ibflex_source::IBFlexSourceConfig;
use rust_decimal::prelude::Decimal;
use serde::Deserialize;
use std::collections::HashMap;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
#[serde(tag = "type")]
pub enum SourceType {
    Hardcoded { assets: Vec<Asset> },
    IBFlex(IBFlexSourceConfig),
}

#[derive(Deserialize, Debug)]
pub struct SourceConfig {
    pub name: String,
    #[serde(flatten)]
    pub source_type: SourceType,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
#[serde(tag = "type")]
pub enum ConverterConfig {
    CurrencyLayer(CurrencyLayerConverterConfig),
    AlphaVantage(AlphaVantageConverterConfig),
    Fixer(FixerConverterConfig),
}

#[derive(Deserialize, Debug)]
pub struct ModellingConfig {
    pub monthly_saving: Asset,
    /// Yearly yields. 0.03 = 3%
    pub yearly_yields: Vec<Decimal>,
    /// Monthly spending targets to simulate
    pub monthly_targets: Vec<Asset>,
}

#[derive(Deserialize, Debug)]
pub struct Adjustment {
    /// Name of the adjustment. Will be displayed in cFIREsim.
    pub name: String,

    /// Name of sources that makes up this adjustment.
    pub source: Vec<String>,

    /// Year when the adjustment will be released.
    // TODO: implement adjustments other than released on a given year
    pub year: u16,
}

#[derive(Deserialize, Debug)]
pub struct SocialSecurity {
    /// Year when social security payments start.
    pub start_year: u16,

    /// Monthly amount paid on social security.
    pub monthly_amount: u16,
}

#[derive(Deserialize, Debug)]
pub struct CFireSimConfig {
    /// Names of sources that make up the main portfolio to withdraw from.
    pub portfolio: Vec<String>,

    pub adjustment: Vec<Adjustment>,

    pub social_security: SocialSecurity,

    pub retirement_year: u16,
    pub retirement_end_year: u16,
    pub initial_yearly_spending: u32,
}

#[derive(Deserialize, Debug)]
pub struct Config {
    /// Keyed by source ID.
    #[serde(rename = "sources")]
    pub source_config: HashMap<String, SourceConfig>,

    /// Keyed by converter ID.
    #[serde(rename = "converters")]
    pub converter_config: HashMap<String, ConverterConfig>,

    pub common_currency: String,
    pub dated_json_output: String,
    pub csv_output: String,
    pub modelling: ModellingConfig,

    /// cFIREsim configuration.
    pub cfiresim: Option<CFireSimConfig>,
}
