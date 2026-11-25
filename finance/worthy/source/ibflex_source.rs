use asset::Asset;
use async_trait::async_trait;
use denomination::Denomination;
use ibflex::{
    AssetCategory, FlexQuerySuccess, FlexStatement, LevelOfDetail::Summary, OpenPosition,
    Side::Long, run_flex_query,
};
use rust_decimal::Decimal;
use serde::Deserialize;
use source::Source;
use std::collections::HashMap;
use std::error::Error;
use std::{
    fmt,
    fmt::{Display, Formatter},
};

pub struct IBFlexSource {}

#[derive(Debug, Deserialize)]
pub struct IBFlexSourceConfig {
    query_id: String,
    token: String,
}

#[derive(Debug)]
struct UnhandledResponse {
    message: String,
}

impl Error for UnhandledResponse {}
impl Display for UnhandledResponse {
    fn fmt(&self, f: &mut Formatter) -> fmt::Result {
        write!(f, "Unhandled response: {}", self.message)
    }
}

fn get_only_flex_statement(r: &FlexQuerySuccess) -> Result<&FlexStatement, UnhandledResponse> {
    let flex_statements = &r.flex_statements;
    if flex_statements.count != 1 {
        return Err(UnhandledResponse {
            message: format!(
                "expected 1 returned FlexStatements, got {} {:?}",
                flex_statements.count, r
            ),
        });
    }
    let flex_statements = &flex_statements.flex_statements;
    if flex_statements.len() != 1 {
        return Err(UnhandledResponse {
            message: format!(
                "expected 1 returned FlexStatement per FlexStatements, got {} {:?}",
                flex_statements.len(),
                r
            ),
        });
    }
    Ok(&flex_statements[0])
}

fn check_position(position: &OpenPosition) -> Result<(), UnhandledResponse> {
    if position.multiplier != Decimal::new(1, 0) {
        return Err(UnhandledResponse {
            message: "multiplier != 1 not supported".to_string(),
        });
    }
    if position.asset_category != AssetCategory::Stock {
        return Err(UnhandledResponse {
            message: "only stocks supported".to_string(),
        });
    }
    if !position.put_call.is_empty()
        || !position.issuer.is_empty()
        || !position.expiry.is_empty()
        || position.level_of_detail != Summary
    {
        return Err(UnhandledResponse {
            message: "unexpected fields populated".to_string(),
        });
    }
    if position.side != Long {
        return Err(UnhandledResponse {
            message: "only long positions supported".to_string(),
        });
    }
    Ok(())
}

#[async_trait]
impl Source for IBFlexSource {
    type Config = IBFlexSourceConfig;

    async fn take_snapshot(config: &Self::Config) -> Result<Vec<Asset>, Box<dyn Error>> {
        let IBFlexSourceConfig { query_id, token } = config;
        let r = run_flex_query(token, query_id).await?;
        let s = get_only_flex_statement(&r)?;

        let mut seen_exchange_rates: HashMap<String, Decimal> = HashMap::new();

        let empty = Vec::new();
        let positions: &Vec<OpenPosition> =
            s.open_positions.open_position.as_ref().unwrap_or(&empty);
        positions
            .iter()
            .map(|position| -> Result<Asset, Box<dyn Error>> {
                check_position(position)?;
                match seen_exchange_rates.get(&position.currency) {
                    Some(seen_exchange_rate)
                        if (*seen_exchange_rate != position.fx_rate_to_base) =>
                    {
                        return Err(UnhandledResponse {
                            message: "inconsistent rate for currency".to_string(),
                        }
                        .into());
                    }
                    _ => {
                        seen_exchange_rates
                            .entry(position.currency.clone())
                            .or_insert(position.fx_rate_to_base);
                    }
                }
                Ok(Asset {
                    denomination: Denomination::Stock {
                        stock: position.symbol.clone(),
                    },
                    amount: position.position,
                })
            })
            .collect()
        // TODO(prvak): import cash (CashReport); add currency conversions and known market prices
    }
}
