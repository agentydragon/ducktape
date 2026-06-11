//use chrono::prelude::*;
//use chrono_tz::Tz;
use denomination::Denomination;
use rust_decimal::prelude::Decimal;
//use std::time::Instant;

#[derive(Debug, PartialEq, Clone)]
pub struct ExchangeRate {
    //timestamp: DateTime<Tz>,
    pub from: Denomination,
    pub to: Denomination,
    pub rate: Decimal,
}
