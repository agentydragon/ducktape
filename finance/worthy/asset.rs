use denomination::Denomination;
use rust_decimal::prelude::Decimal;
use serde::Deserialize;

#[derive(Debug, Deserialize, Clone, PartialEq, Eq)]
pub struct Asset {
    pub amount: Decimal,
    #[serde(flatten)]
    pub denomination: Denomination,
}
