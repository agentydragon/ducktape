//! Purchase-anchored gross property valuation, before sale costs, debt or taxes.

use crate::execution::{ExecutionInput, ScheduledPropertyPurchaseSpec};
use crate::money::{Factor, Money};

#[derive(Clone, Debug)]
pub(crate) struct Valuation {
    purchase_price: Money,
    purchase_month: u32,
    home_value_series: usize,
}

impl Valuation {
    pub(crate) fn new(purchase: &ScheduledPropertyPurchaseSpec, home_value_series: usize) -> Self {
        Self {
            purchase_price: purchase.purchase_price,
            purchase_month: purchase.month,
            home_value_series,
        }
    }

    /// The nominal purchase price is anchored at acquisition, not the path's origin.
    /// Callers supply the last observed mark month for a stopped book.
    pub(crate) fn at(
        &self,
        input: &ExecutionInput,
        rollout: u32,
        valuation_month: u32,
    ) -> Result<Money, ValuationError> {
        let series = &input.series[self.home_value_series];
        let value = |month| {
            series
                .value(rollout, month)
                .ok_or_else(|| ValuationError::MissingMark {
                    series_id: series.series_id.clone(),
                    rollout,
                    month,
                })
        };
        Ok(self.purchase_price.scaled_by(
            Factor::new(value(valuation_month)?, value(self.purchase_month)?),
            "property market value",
        )?)
    }
}

#[derive(Debug, thiserror::Error, Eq, PartialEq)]
pub enum ValuationError {
    #[error("series {series_id:?} has no value at rollout {rollout} month {month}")]
    MissingMark {
        series_id: String,
        rollout: u32,
        month: u32,
    },
    #[error(transparent)]
    Arithmetic(#[from] crate::money::ArithmeticError),
}
