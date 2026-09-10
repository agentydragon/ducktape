//! Borrowed actor-scoped facts from the current financial books.
//!
//! Views cannot advance the clock, inspect another actor, or read arbitrary path rows.
//! Account balances are available cash under the engine's immediate-settlement model.
//! Current marks are gross values, not promised sale proceeds or tax previews.

use super::*;
use crate::execution::HoldingPoolSpec;

/// Borrow the authoritative state at one execution boundary, without capture tables.
pub(super) struct Books<'a> {
    pub ledger: &'a Ledger,
    pub lots: &'a [LotState],
    pub mortgages: &'a [MortgageState],
    pub tax: &'a TaxState,
    pub tax_liabilities: &'a [TaxLiabilityState],
    pub tlh_portfolios: &'a [TlhPortfolioObservation],
}

/// Information for one actor at the supplied observation/mark month. Only the engine
/// constructs a view; no path ID, future input series or mutable books reach the policy.
pub struct ActorBooks<'a> {
    pub(super) scope: &'a AgentHoldings,
    pub(super) books: Books<'a>,
    pub(super) input: &'a ExecutionInput,
    pub(super) rollout: u32,
    pub(super) month: u32,
}

impl ActorBooks<'_> {
    pub fn agent_id(&self) -> &str {
        self.scope.agent_id()
    }

    /// All exposed marks use this month, even when the book was recorded later.
    pub fn month(&self) -> u32 {
        self.month
    }

    /// Observed CPI relative to origin, or absent when the experiment models no CPI.
    pub fn cpi(&self) -> Result<Option<Factor>, SimulationError> {
        if !self
            .input
            .series
            .iter()
            .any(|series| series.series_id == "inflation")
        {
            return Ok(None);
        }
        let current = series_value(self.input, "inflation", self.rollout, self.month)?;
        let origin = series_value(self.input, "inflation", self.rollout, 0)?;
        validate_amount_index_level(
            "actor observation",
            "inflation",
            self.rollout,
            self.month,
            current,
        )?;
        validate_amount_index_level("actor observation", "inflation", self.rollout, 0, origin)?;
        Ok(Some(Factor::new(current, origin)))
    }

    /// Declared cash accounts only, not internal equity, tax or asset-basis postings.
    pub fn accounts(&self) -> impl Iterator<Item = Result<Account<'_>, LedgerError>> {
        self.scope.accounts().map(|account| {
            Ok(Account {
                account,
                available: self.books.ledger.balance(account)?,
            })
        })
    }

    pub fn cash(&self) -> Result<Money, LedgerError> {
        self.scope.cash(self.books.ledger)
    }

    /// Owned public pools, including unheld assets available for purchase.
    pub fn holding_pools(&self) -> impl Iterator<Item = &HoldingPoolSpec> {
        self.input.scenario.holding_pools.iter().filter(|pool| {
            pool.agent_id == self.agent_id() && private_equity_issuer(&pool.asset_id).is_none()
        })
    }

    /// A declared public asset's quote at this observation's month, without a position.
    pub fn public_price(&self, asset_id: &str) -> Result<PerUnit, HoldingsError> {
        self.scope
            .public_price(self.input, asset_id, self.rollout, self.month)
    }

    /// Remaining public lots, with current book basis and current marks. Liquidated
    /// lots and private-equity positions are not public positions.
    pub fn public_positions(
        &self,
    ) -> impl Iterator<Item = Result<PublicPosition<'_>, HoldingsError>> {
        self.books
            .lots
            .iter()
            .filter(|lot| {
                lot.spec.agent_id == self.agent_id()
                    && lot.units_remaining.0 != 0
                    && private_equity_issuer(&lot.spec.asset_id).is_none()
            })
            .map(|lot| {
                Ok(PublicPosition {
                    lot,
                    price: self.public_price(&lot.spec.asset_id)?,
                })
            })
    }

    pub fn public_value(&self) -> Result<Money, HoldingsError> {
        self.scope.public_value(
            self.input,
            self.books.lots.iter().map(LotState::view),
            self.rollout,
            self.month,
        )
    }

    /// Originated, still-active borrower contracts; a scheduled future purchase is
    /// not a mortgage contract. Counterparty terms do not disclose its other books.
    pub fn mortgages(&self) -> impl Iterator<Item = &MortgageState> {
        self.books.mortgages.iter().filter(|mortgage| {
            mortgage.agent_id == self.agent_id()
                && mortgage.active
                && mortgage.origination_month <= self.month
        })
    }

    /// Recorded year-to-date income by declared source, including live zero rows.
    pub fn income(&self) -> impl Iterator<Item = (&IncomeSource, Money)> {
        self.books.tax.income.rows_for(self.agent_id())
    }

    /// Booked jurisdiction facts, not an assessment of future income. Assessment-only
    /// fields are populated at assessment time; see `TaxFacts` for their semantics.
    pub fn tax_facts(&self) -> impl Iterator<Item = (&str, &TaxFacts)> {
        self.books
            .tax
            .facts
            .iter()
            .filter(|((agent, _), _)| agent == self.agent_id())
            .map(|((_, jurisdiction), facts)| (jurisdiction.as_str(), facts))
    }

    /// Already assessed, outstanding liabilities, not future estimates or payment claims.
    pub fn tax_liabilities(&self) -> impl Iterator<Item = &TaxLiabilityState> {
        self.books.tax_liabilities.iter().filter(|liability| {
            liability.agent_id == self.agent_id()
                && liability.active
                && liability.tax_year_end_month <= self.month
        })
    }

    /// Reported managed-portfolio facts; Python owns the underlying cohorts.
    pub fn tlh_portfolios(&self) -> impl Iterator<Item = &TlhPortfolioObservation> {
        self.books
            .tlh_portfolios
            .iter()
            .filter(|portfolio| portfolio.owner_agent_id == self.agent_id())
    }
}

pub struct Account<'a> {
    pub account: &'a AccountRef,
    pub available: Money,
}

/// A read-only lot with a quote at its parent `ActorBooks::month()`.
pub struct PublicPosition<'a> {
    lot: &'a LotState,
    pub price: PerUnit,
}

impl PublicPosition<'_> {
    pub fn lot_id(&self) -> &str {
        &self.lot.spec.lot_id
    }

    pub fn account_id(&self) -> &str {
        &self.lot.spec.account_id
    }

    pub fn asset_id(&self) -> &str {
        &self.lot.spec.asset_id
    }

    pub fn purchase_month(&self) -> i32 {
        self.lot.spec.purchase_month
    }

    pub fn units(&self) -> Units {
        Units::new(self.lot.units_remaining, self.lot.spec.quantity_scale)
    }

    pub fn quantity_scale(&self) -> i64 {
        self.lot.spec.quantity_scale
    }

    /// Remaining exact tax basis of this ordinary holding.
    pub fn book_basis(&self) -> Money {
        self.lot.basis_remaining
    }

    pub fn value(&self) -> Result<Money, ArithmeticError> {
        self.lot.view().value(self.price)
    }
}

/// An assembled, unpaid demand. `due_month` is the current monthly settlement deadline,
/// not a statutory deadline. Projecting a claim does not choose how to fund or pay it.
pub struct Claim<'a> {
    pub id: claims::ClaimId,
    pub cause_id: &'a str,
    pub obligation_type: &'a str,
    pub from: &'a AccountRef,
    pub to: &'a AccountRef,
    pub amount_due: Money,
    pub due_month: u32,
}

/// Only already-assembled demands of the named payer; never evaluate future input terms.
pub(super) fn due_claims<'a>(
    claims: &'a claims::Claims,
    agent_id: &'a str,
) -> impl Iterator<Item = Claim<'a>> {
    claims
        .entries
        .iter()
        .enumerate()
        .filter(move |(_, obligation)| obligation.from.agent_id == agent_id && !obligation.paid)
        .map(move |(index, obligation)| Claim {
            id: claims::ClaimId {
                month: claims.month,
                index,
            },
            cause_id: &obligation.cause_id,
            obligation_type: &obligation.obligation_type,
            from: &obligation.from,
            to: &obligation.to,
            amount_due: obligation.amount_due,
            due_month: claims.month,
        })
}
