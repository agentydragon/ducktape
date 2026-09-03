use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::money::{ArithmeticError, Money};

#[derive(Clone, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct AccountRef {
    pub agent_id: String,
    pub account_id: String,
}

impl AccountRef {
    pub fn new(agent_id: impl Into<String>, account_id: impl Into<String>) -> Self {
        Self {
            agent_id: agent_id.into(),
            account_id: account_id.into(),
        }
    }
}

/// Signed debit posting. Positive values are debits and negative values are
/// credits. Therefore a balanced entry sums to zero without consulting account
/// type or normal balance.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Posting {
    pub account: AccountRef,
    pub amount: Money,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct JournalEntry {
    pub month: u32,
    pub cause_id: String,
    pub postings: Vec<Posting>,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct Ledger {
    balances: BTreeMap<AccountRef, Money>,
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum LedgerError {
    #[error("journal entry {cause_id:?} at month {month} is unbalanced by {imbalance} quanta")]
    Unbalanced {
        cause_id: String,
        month: u32,
        imbalance: i128,
    },
    #[error("unknown account {agent_id}:{account_id}")]
    UnknownAccount {
        agent_id: String,
        account_id: String,
    },
    #[error(transparent)]
    Arithmetic(#[from] ArithmeticError),
}

impl Ledger {
    pub fn with_accounts(accounts: impl IntoIterator<Item = AccountRef>) -> Self {
        Self {
            balances: accounts
                .into_iter()
                .map(|account| (account, Money(0)))
                .collect(),
        }
    }

    pub fn ensure_account(&mut self, account: AccountRef) {
        self.balances.entry(account).or_insert(Money(0));
    }

    pub fn balance(&self, account: &AccountRef) -> Result<Money, LedgerError> {
        self.balances
            .get(account)
            .copied()
            .ok_or_else(|| LedgerError::UnknownAccount {
                agent_id: account.agent_id.clone(),
                account_id: account.account_id.clone(),
            })
    }

    pub fn balances(&self) -> &BTreeMap<AccountRef, Money> {
        &self.balances
    }

    pub fn apply(&mut self, entry: &JournalEntry) -> Result<(), LedgerError> {
        let imbalance: i128 = entry
            .postings
            .iter()
            .map(|posting| i128::from(posting.amount.0))
            .sum();
        if imbalance != 0 {
            return Err(LedgerError::Unbalanced {
                cause_id: entry.cause_id.clone(),
                month: entry.month,
                imbalance,
            });
        }

        // Aggregate compound postings by account, then validate and calculate
        // every resulting balance before mutating, so a failed entry is atomic.
        let mut deltas = BTreeMap::<AccountRef, Money>::new();
        for posting in &entry.postings {
            let current = deltas.get(&posting.account).copied().unwrap_or_default();
            deltas.insert(
                posting.account.clone(),
                current.checked_add(posting.amount)?,
            );
        }
        let mut updated = Vec::with_capacity(deltas.len());
        for (account, delta) in deltas {
            let current = self.balance(&account)?;
            updated.push((account, current.checked_add(delta)?));
        }
        for (account, balance) in updated {
            self.balances.insert(account, balance);
        }
        Ok(())
    }

    pub fn trial_balance(&self) -> i128 {
        self.balances
            .values()
            .map(|amount| i128::from(amount.0))
            .sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compound_entry_balances_and_applies_atomically() {
        let cash = AccountRef::new("alice", "cash");
        let asset = AccountRef::new("alice", "asset_basis:vti");
        let gain = AccountRef::new("alice", "income:realized_gain");
        let mut ledger = Ledger::with_accounts([cash.clone(), asset.clone(), gain.clone()]);
        ledger
            .apply(&JournalEntry {
                month: 4,
                cause_id: "sale".into(),
                postings: vec![
                    Posting {
                        account: cash.clone(),
                        amount: Money(150),
                    },
                    Posting {
                        account: asset.clone(),
                        amount: Money(-100),
                    },
                    Posting {
                        account: gain.clone(),
                        amount: Money(-50),
                    },
                ],
            })
            .unwrap();
        assert_eq!(ledger.balance(&cash), Ok(Money(150)));
        assert_eq!(ledger.balance(&asset), Ok(Money(-100)));
        assert_eq!(ledger.balance(&gain), Ok(Money(-50)));
        assert_eq!(ledger.trial_balance(), 0);
    }

    #[test]
    fn rejects_unbalanced_entry_without_mutation() {
        let cash = AccountRef::new("alice", "cash");
        let mut ledger = Ledger::with_accounts([cash.clone()]);
        let result = ledger.apply(&JournalEntry {
            month: 0,
            cause_id: "bad".into(),
            postings: vec![Posting {
                account: cash.clone(),
                amount: Money(1),
            }],
        });
        assert!(matches!(result, Err(LedgerError::Unbalanced { .. })));
        assert_eq!(ledger.balance(&cash), Ok(Money(0)));
    }

    #[test]
    fn repeated_account_postings_are_accumulated_before_mutation() {
        let cash = AccountRef::new("alice", "cash");
        let equity = AccountRef::new("alice", "equity");
        let mut ledger = Ledger::with_accounts([cash.clone(), equity.clone()]);
        ledger
            .apply(&JournalEntry {
                month: 0,
                cause_id: "compound".into(),
                postings: vec![
                    Posting {
                        account: cash.clone(),
                        amount: Money(100),
                    },
                    Posting {
                        account: cash.clone(),
                        amount: Money(50),
                    },
                    Posting {
                        account: equity.clone(),
                        amount: Money(-150),
                    },
                ],
            })
            .unwrap();
        assert_eq!(ledger.balance(&cash), Ok(Money(150)));
        assert_eq!(ledger.balance(&equity), Ok(Money(-150)));
        assert_eq!(ledger.trial_balance(), 0);
    }
}
