//! Borrowed actor-scoped facts from the current financial books.
//!
//! Views cannot advance the clock, inspect another actor, or read arbitrary path rows.
//! Account balances are available cash under the engine's immediate-settlement model.
//! Current marks are gross values, not promised sale proceeds or tax previews.

use super::*;

/// Borrow the authoritative state at one execution boundary, without capture tables.
pub(super) struct Books<'a> {
    pub ledger: &'a Ledger,
    pub lots: &'a [LotState],
    pub mortgages: &'a [MortgageState],
    pub tax: &'a TaxState,
    pub tax_liabilities: &'a [TaxLiabilityState],
    pub tlh_cumulative_harvest: &'a [Money],
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
                    price: self.scope.public_price(
                        self.input,
                        &lot.spec.asset_id,
                        self.rollout,
                        self.month,
                    )?,
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

    /// Reduced-form harvesting keeps basis reductions at account/asset-pool scope,
    /// separately from individual lot basis. These amounts are not per-lot tax basis.
    pub fn harvest_adjustments(&self) -> impl Iterator<Item = HarvestAdjustment<'_>> {
        self.input
            .scenario
            .harvest_policies
            .iter()
            .zip(self.books.tlh_cumulative_harvest)
            .filter(|(policy, _)| policy.owner_agent_id == self.agent_id())
            .map(|(policy, amount)| HarvestAdjustment {
                account_id: &policy.account_id,
                asset_id: &policy.asset_id,
                cumulative_harvest: *amount,
            })
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

    /// Remaining lot-book basis, before the pool-level `harvest_adjustments`.
    pub fn book_basis(&self) -> Money {
        self.lot.basis_remaining
    }

    pub fn value(&self) -> Result<Money, ArithmeticError> {
        self.lot.view().value(self.price)
    }
}

pub struct HarvestAdjustment<'a> {
    pub account_id: &'a str,
    pub asset_id: &'a str,
    pub cumulative_harvest: Money,
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
