//! Scoped reads of canonical cash and public positions at supplied market marks.
//! Resolved scopes own account/price identities, never balances or a second position book.

use std::collections::BTreeMap;

use crate::execution::ExecutionInput;
use crate::ledger::{AccountRef, Ledger, LedgerError};
use crate::money::{ArithmeticError, Money, PerUnit, Quantity, Units};

/// Borrowed position facts; each lot rounds to currency quanta before totals are summed.
#[derive(Clone, Copy)]
pub struct LotView<'a> {
    pub agent_id: &'a str,
    pub asset_id: &'a str,
    pub units_remaining: i64,
    pub quantity_scale: i64,
}

impl LotView<'_> {
    pub fn value(&self, price: PerUnit) -> Result<Money, ArithmeticError> {
        price.times(
            Units::new(Quantity(self.units_remaining), self.quantity_scale),
            "holding value",
        )
    }
}

/// Sum exactly the named accounts; the caller defines the account/actor scope.
pub fn cash_balance<'a>(
    ledger: &Ledger,
    accounts: impl IntoIterator<Item = &'a AccountRef>,
) -> Result<Money, LedgerError> {
    accounts.into_iter().try_fold(Money(0), |sum, account| {
        Ok(sum.checked_add(ledger.balance(account)?)?)
    })
}

/// Actor-wide declared cash accounts and public assets, including configured buy sleeves.
/// Public holdings exclude private equity, individual bonds and property; they are gross
/// marks, not the proceeds of a hypothetical after-tax liquidation.
#[derive(Clone, Debug)]
pub struct AgentHoldings {
    agent_id: String,
    cash_accounts: Vec<AccountRef>,
    public_series_by_asset: BTreeMap<String, usize>,
}

impl AgentHoldings {
    pub fn resolve(input: &ExecutionInput, agent_id: &str) -> Result<Self, HoldingsError> {
        let cash_accounts: Vec<_> = input
            .scenario
            .accounts
            .iter()
            .filter(|spec| spec.account.agent_id == agent_id)
            .map(|spec| spec.account.clone())
            .collect();
        if cash_accounts.is_empty() {
            return Err(HoldingsError::UnknownAgent {
                agent_id: agent_id.into(),
            });
        }
        let series_rows: BTreeMap<_, _> = input
            .series
            .iter()
            .enumerate()
            .map(|(row, series)| (series.series_id.as_str(), row))
            .collect();
        let assets = input
            .scenario
            .initial_lots
            .iter()
            .filter(|lot| lot.agent_id == agent_id)
            .map(|lot| lot.asset_id.as_str())
            .chain(
                input
                    .scenario
                    .target_allocation_policies
                    .iter()
                    .filter(|policy| policy.agent_id == agent_id)
                    .flat_map(|policy| {
                        policy.sleeves.iter().map(|sleeve| sleeve.asset_id.as_str())
                    }),
            );
        let mut public_series_by_asset = BTreeMap::new();
        for asset_id in assets.filter(|asset| {
            asset
                .strip_prefix("private_equity:")
                .is_none_or(|issuer| issuer.is_empty())
        }) {
            let series_id = format!("security:{asset_id}");
            let row = series_rows
                .get(series_id.as_str())
                .ok_or(HoldingsError::MissingSeries { series_id })?;
            public_series_by_asset.insert(asset_id.to_owned(), *row);
        }
        Ok(Self {
            agent_id: agent_id.into(),
            cash_accounts,
            public_series_by_asset,
        })
    }

    pub fn agent_id(&self) -> &str {
        &self.agent_id
    }

    pub fn cash(&self, ledger: &Ledger) -> Result<Money, LedgerError> {
        cash_balance(ledger, &self.cash_accounts)
    }

    pub fn accounts(&self) -> impl Iterator<Item = &AccountRef> {
        self.cash_accounts.iter()
    }

    pub(crate) fn public_price(
        &self,
        input: &ExecutionInput,
        asset_id: &str,
        rollout: u32,
        month: u32,
    ) -> Result<PerUnit, HoldingsError> {
        let row = self.public_series_by_asset.get(asset_id).ok_or_else(|| {
            HoldingsError::MissingSeries {
                series_id: format!("security:{asset_id}"),
            }
        })?;
        let series = &input.series[*row];
        series
            .value(rollout, month)
            .map(PerUnit)
            .ok_or_else(|| HoldingsError::MissingSeriesValue {
                series_id: series.series_id.clone(),
                rollout,
                month,
            })
    }

    /// Read current remaining lots at exactly the caller's observed market month.
    pub fn public_value<'a>(
        &self,
        input: &ExecutionInput,
        lots: impl IntoIterator<Item = LotView<'a>>,
        rollout: u32,
        month: u32,
    ) -> Result<Money, HoldingsError> {
        lots.into_iter()
            .filter(|lot| {
                lot.agent_id == self.agent_id
                    && lot.units_remaining != 0
                    && lot
                        .asset_id
                        .strip_prefix("private_equity:")
                        .is_none_or(|issuer| issuer.is_empty())
            })
            .try_fold(Money(0), |sum, lot| {
                let price = self.public_price(input, lot.asset_id, rollout, month)?;
                Ok(sum.checked_add(lot.value(price)?)?)
            })
    }
}

#[derive(Debug, thiserror::Error, Eq, PartialEq)]
pub enum HoldingsError {
    #[error("scenario has no account for agent {agent_id:?}")]
    UnknownAgent { agent_id: String },
    #[error("public holdings need series {series_id:?}, which the input does not supply")]
    MissingSeries { series_id: String },
    #[error("series {series_id:?} has no value at rollout {rollout} month {month}")]
    MissingSeriesValue {
        series_id: String,
        rollout: u32,
        month: u32,
    },
    #[error(transparent)]
    Arithmetic(#[from] ArithmeticError),
}
