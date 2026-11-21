use denomination::Denomination;
use rust_decimal::prelude::Decimal;

#[derive(Debug, PartialEq, Clone)]
pub struct ExchangeRate {
    pub from: Denomination,
    pub to: Denomination,
    pub rate: Decimal,
}
