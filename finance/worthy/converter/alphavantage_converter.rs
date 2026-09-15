use alphavantage::{Client, time_series::IntradayInterval};
use async_trait::async_trait;
use converter::Converter;
use denomination::Denomination;
use exchange_rate::ExchangeRate;
use log::{error, trace};
use rust_decimal::prelude::*;
use serde::Deserialize;
use std::error::Error;

pub struct AlphaVantageConverter {}

#[derive(Debug, Deserialize)]
pub struct AlphaVantageConverterConfig {
    api_key: String,
}

#[async_trait]
impl Converter for AlphaVantageConverter {
    type Config = AlphaVantageConverterConfig;

    async fn take_snapshot(
        config: &Self::Config,
        denominations: &'life1 [&Denomination],
        _base: &Denomination,
    ) -> Result<Vec<ExchangeRate>, Box<dyn Error>> {
        let AlphaVantageConverterConfig { api_key } = config;
        let client = Client::new(api_key);

        //let rates = Vec::new();
        let currencies: Vec<&str> = denominations
            .iter()
            .filter_map(|d| match d {
                Denomination::Currency { currency } => Some(currency.as_str()),
                _ => None,
            })
            .collect();

        let mut rates = Vec::new();
        // TODO(agentydragon): Do this in parallel. But ensure we keep a slow QPS.
        for denomination in denominations.iter() {
            if let Denomination::Stock { stock } = denomination {
                let time_series = client
                    .get_time_series_intraday(stock, IntradayInterval::OneMinute)
                    .await;
                if time_series.is_err() {
                    error!("{stock} {time_series:?}");
                    continue;
                }
                let time_series = time_series.unwrap();

                let entry = time_series.entries.last().unwrap();
                trace!("{stock} {entry:?}");

                rates.push(ExchangeRate {
                    //timestamp: entry.date.timestamp,
                    from: Denomination::Stock {
                        stock: stock.clone(),
                    },
                    to: Denomination::Currency {
                        currency: "USD".to_string(),
                    },
                    // TODO: it's OHLC, maybe another?
                    rate: Decimal::from_f64(entry.close).unwrap(),
                });
            }
        }

        // TODO(agentydragon): Do this in parallel. But ensure we keep a slow QPS.
        for currency_from in currencies.iter() {
            for currency_to in currencies.iter() {
                if currency_from == currency_to {
                    continue;
                }
                let exchange_rate = client.get_exchange_rate(currency_from, currency_to).await;
                if let Ok(exchange_rate) = exchange_rate {
                    trace!("{exchange_rate:?}: ok");
                } else {
                    error!("for {currency_from}:{currency_to} -> {exchange_rate:?}: skip");
                }
            }
        }
        Ok(rates)
    }
}
