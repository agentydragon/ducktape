use async_trait::async_trait;
use denomination::Denomination;
use exchange_rate::ExchangeRate;
use std::error::Error;

#[async_trait]
pub trait Converter {
    type Config;

    async fn take_snapshot(
        config: &Self::Config,
        denominations: &'life1 [&Denomination],
        base: &Denomination,
    ) -> Result<Vec<ExchangeRate>, Box<dyn Error>>;
}
