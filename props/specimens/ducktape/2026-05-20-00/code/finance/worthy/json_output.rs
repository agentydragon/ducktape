//! Module parsing JSON output of worthy2.

use chrono::prelude::*;
use rust_decimal::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct Snapshot {
    pub timestamp: DateTime<FixedOffset>,
    pub source_snapshot: Vec<SourceSnapshot>,
    pub converter_snapshots: Vec<ConverterSnapshot>,
    pub total: Asset,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
pub enum SourceType {
    #[serde(rename = "hardcoded")]
    Hardcoded,
    #[serde(rename = "ibflex")]
    IBFlex,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct SourceSnapshot {
    pub id: String,
    pub name: String,
    #[serde(rename = "Type")]
    pub source_type: SourceType,
    pub snapshot: Vec<Asset>,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(tag = "Type")]
pub enum Denomination {
    //  "Type": "currency",
    //  "Symbol": "CZK",
    #[serde(rename = "currency")]
    Currency {
        #[serde(rename = "Symbol")]
        symbol: String,
    },
    #[serde(rename = "crypto")]
    Cryptocurrency {
        #[serde(rename = "Symbol")]
        symbol: String,
    },
    #[serde(rename = "stock")]
    Stock {
        #[serde(rename = "Symbol")]
        symbol: String,
    },
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
pub struct Asset {
    #[serde(flatten)]
    pub denomination: Denomination,
    #[serde(rename = "Amount")]
    pub amount: Decimal,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
pub enum ConverterType {
    #[serde(rename = "currencylayer")]
    CurrencyLayer,
    #[serde(rename = "alphavantage")]
    AlphaVantage,
    #[serde(rename = "fixer")]
    Fixer,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct ConverterSnapshot {
    pub id: String,
    #[serde(rename = "Type")]
    pub converter_type: ConverterType,
    pub snapshot: Vec<Conversion>,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct Conversion {
    pub source: Denomination,
    pub target: Denomination,
    pub target_per_source: Decimal,
}
