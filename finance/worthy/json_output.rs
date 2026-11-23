//! Worthy snapshots use the historical JSON field names for assets and exchange rates.

use asset::Asset;
use chrono::prelude::*;
use denomination::Denomination;
use exchange_rate::ExchangeRate;
use rust_decimal::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct Snapshot {
    pub timestamp: DateTime<FixedOffset>,
    pub source_snapshot: Vec<SourceSnapshot>,
    pub converter_snapshots: Vec<ConverterSnapshot>,
    #[serde(with = "SnapshotAsset")]
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
    #[serde(with = "asset_list")]
    pub snapshot: Vec<Asset>,
}

#[derive(Serialize, Deserialize)]
#[serde(remote = "Denomination", tag = "Type")]
enum SnapshotDenomination {
    #[serde(rename = "currency")]
    Currency {
        #[serde(rename = "Symbol")]
        currency: String,
    },
    #[serde(rename = "crypto")]
    Cryptocurrency {
        #[serde(rename = "Symbol")]
        symbol: String,
    },
    #[serde(rename = "stock")]
    Stock {
        #[serde(rename = "Symbol")]
        stock: String,
    },
}

#[derive(Serialize, Deserialize)]
#[serde(remote = "Asset")]
struct SnapshotAsset {
    #[serde(flatten, with = "SnapshotDenomination")]
    denomination: Denomination,
    #[serde(rename = "Amount")]
    amount: Decimal,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
pub enum ConverterType {
    #[serde(rename = "alphavantage")]
    AlphaVantage,
    // Retained so saved snapshots remain readable after the converter was removed.
    #[serde(rename = "fixer")]
    Fixer,
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Eq)]
#[serde(rename_all = "PascalCase")]
pub struct ConverterSnapshot {
    pub id: String,
    #[serde(rename = "Type")]
    pub converter_type: ConverterType,
    #[serde(with = "rate_list")]
    pub snapshot: Vec<ExchangeRate>,
}

#[derive(Serialize, Deserialize)]
#[serde(remote = "ExchangeRate")]
struct SnapshotRate {
    #[serde(rename = "Source", with = "SnapshotDenomination")]
    from: Denomination,
    #[serde(rename = "Target", with = "SnapshotDenomination")]
    to: Denomination,
    #[serde(rename = "TargetPerSource")]
    rate: Decimal,
}

mod asset_list {
    use super::*;
    use serde::{Deserializer, Serializer};

    #[derive(Serialize, Deserialize)]
    #[serde(transparent)]
    struct Item(#[serde(with = "SnapshotAsset")] Asset);

    pub fn serialize<S: Serializer>(assets: &[Asset], serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_seq(assets.iter().cloned().map(Item))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Vec<Asset>, D::Error> {
        Vec::<Item>::deserialize(deserializer)
            .map(|items| items.into_iter().map(|item| item.0).collect())
    }
}

mod rate_list {
    use super::*;
    use serde::{Deserializer, Serializer};

    #[derive(Serialize, Deserialize)]
    #[serde(transparent)]
    struct Item(#[serde(with = "SnapshotRate")] ExchangeRate);

    pub fn serialize<S: Serializer>(
        rates: &[ExchangeRate],
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        serializer.collect_seq(rates.iter().cloned().map(Item))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Vec<ExchangeRate>, D::Error> {
        Vec::<Item>::deserialize(deserializer)
            .map(|items| items.into_iter().map(|item| item.0).collect())
    }
}
