//! Selected actor results, not a second financial evaluator. Numeric observed prefixes
//! reuse canonical cash/lot marks; payments and taxes retain their domain identities.

use super::*;

#[derive(Debug, Serialize)]
pub struct CashSeries {
    pub account: AccountRef,
    pub values: Vec<Money>,
}

#[derive(Debug, Serialize)]
pub struct HoldingSeries {
    pub account: AccountRef,
    pub asset_id: String,
    /// Gross public marks, not after-tax sale proceeds. Each lot rounds before summing.
    pub values: Vec<Money>,
}

/// Historical par/indexed carrying principal, not a liquid or market value.
#[derive(Debug, Serialize)]
pub struct BondSeries {
    pub account: AccountRef,
    pub bond_id: String,
    pub values: Vec<Money>,
}

/// A resolved claim's reporting metadata, distinct from its occurrence handle.
#[derive(Debug, Serialize)]
pub struct PaymentTarget {
    pub to: AccountRef,
    pub obligation_type: String,
    pub label: String,
    pub is_tax_payment: bool,
}

#[derive(Debug, Serialize)]
pub struct Payment {
    pub month: u32,
    pub action_index: usize,
    pub cause_id: String,
    pub from: AccountRef,
    /// Absent only when the request names an unknown or stale claim. The failed
    /// request still has its original ID and target in `receipt`.
    pub target: Option<PaymentTarget>,
    pub receipt: payments::Receipt,
}

impl Payment {
    pub(in crate::engine) fn new(
        month: u32,
        action_index: usize,
        request: &payments::Request,
        claims: &Claims,
        receipt: payments::Receipt,
    ) -> Self {
        Self {
            month,
            action_index,
            cause_id: request.cause_id().into(),
            from: request.from().clone(),
            target: request.describe(claims).map(|target| PaymentTarget {
                to: target.to.clone(),
                obligation_type: target.obligation_type.into(),
                label: target.label.into(),
                is_tax_payment: target.is_tax_payment,
            }),
            receipt,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct UnpaidClaim {
    pub id: claims::ClaimId,
    pub cause_id: String,
    pub obligation_type: String,
    pub from: AccountRef,
    pub to: AccountRef,
    pub amount_due: Money,
}

/// Always present, independent of detailed capture. Series contain opening snapshot 0
/// and observed closings only: their length is `ending_book.month + 1`. A failed
/// closing uses `ending_mark_month`, not the next unobserved month's prices.
///
/// Payment occurrences are retained for component/account reductions; journals, trade
/// histories and historical lot books are not. Taxes are canonical selected domain
/// records (including assessments and refunds), not inferred from generic outflows.
#[derive(Debug, Serialize)]
pub struct Summary {
    pub actor_id: String,
    pub cash: Vec<CashSeries>,
    pub public_holdings: Vec<HoldingSeries>,
    pub bond_principal: Vec<BondSeries>,
    pub payments: Vec<Payment>,
    pub unpaid_claims: Vec<UnpaidClaim>,
    pub tax_accruals: Vec<TaxAccrual>,
    pub tax_payments: Vec<TaxPaymentOutcome>,
    pub tax_settlements: Vec<TaxSettlementOutcome>,
    /// One exact terminal/stop book, including remaining lots/basis and counterparties.
    /// This is a result, not the actor's policy observation.
    pub ending_book: MonthOutput,
    pub ending_mark_month: u32,
}

pub(in crate::engine) struct Capture {
    cash: Vec<CashSeries>,
    holdings: BTreeMap<(AccountRef, String), Vec<Money>>,
    bond_principal: Vec<(usize, BondSeries)>,
    pub payments: Vec<Payment>,
    pub unpaid_claims: Vec<UnpaidClaim>,
}

impl Capture {
    pub fn new(input: &ExecutionInput, holdings: &AgentHoldings) -> Self {
        Self {
            cash: holdings
                .accounts()
                .map(|account| CashSeries {
                    account: account.clone(),
                    values: Vec::new(),
                })
                .collect(),
            holdings: BTreeMap::new(),
            bond_principal: input
                .scenario
                .initial_bonds
                .iter()
                .enumerate()
                .filter(|(_, bond)| bond.agent_id == holdings.agent_id())
                .map(|(index, bond)| {
                    (
                        index,
                        BondSeries {
                            account: AccountRef::new(&bond.agent_id, &bond.account_id),
                            bond_id: bond.bond_id.clone(),
                            values: Vec::new(),
                        },
                    )
                })
                .collect(),
            payments: Vec::new(),
            unpaid_claims: Vec::new(),
        }
    }

    pub fn snapshot(
        &mut self,
        input: &ExecutionInput,
        holdings: &AgentHoldings,
        state: &RolloutState,
    ) -> Result<(), SimulationError> {
        for cash in &mut self.cash {
            cash.values.push(state.ledger.balance(&cash.account)?);
        }
        for (index, series) in &mut self.bond_principal {
            series.values.push(
                bond_held_principal(
                    input,
                    state.rollout_id,
                    &input.scenario.initial_bonds[*index],
                    state.month,
                    state.failed_month.unwrap_or(state.month),
                )?
                .unwrap_or(Money(0)),
            );
        }
        for lot in state
            .lots
            .iter()
            .filter(|lot| lot.spec.agent_id == holdings.agent_id())
        {
            let account = AccountRef {
                agent_id: lot.spec.agent_id.clone(),
                account_id: lot.spec.account_id.clone(),
            };
            self.holdings
                .entry((account, lot.spec.asset_id.clone()))
                .or_insert_with(|| vec![Money(0); state.month as usize]);
        }
        for portfolio in state
            .tlh_portfolios
            .iter()
            .filter(|item| item.owner_agent_id == holdings.agent_id())
        {
            self.holdings
                .entry((
                    AccountRef::new(&portfolio.owner_agent_id, &portfolio.account_id),
                    portfolio.asset_id.clone(),
                ))
                .or_insert_with(|| vec![Money(0); state.month as usize]);
        }
        for ((account, asset), values) in &mut self.holdings {
            let managed = state
                .tlh_portfolios
                .iter()
                .filter(|item| {
                    item.owner_agent_id == account.agent_id
                        && item.account_id == account.account_id
                        && item.asset_id == *asset
                })
                .try_fold(Money(0), |sum, item| sum.checked_add(item.value))?;
            values.push(
                holdings
                    .public_value(
                        input,
                        state
                            .lots
                            .iter()
                            .filter(|lot| {
                                lot.spec.account_id == account.account_id
                                    && lot.spec.asset_id == *asset
                            })
                            .map(LotState::view),
                        state.rollout_id,
                        state.failed_month.unwrap_or(state.month),
                    )?
                    .checked_add(managed)?,
            );
        }
        Ok(())
    }

    pub fn finish(
        self,
        input: &ExecutionInput,
        actor: &str,
        state: &mut RolloutState,
    ) -> Result<Summary, SimulationError> {
        let recorder = &mut state.recorder;
        // Dense/forensic traces also retain these records; compact capture moves them.
        let tax_accruals = if recorder.capture_mode.captures_output() {
            recorder.tax_accruals.clone()
        } else {
            std::mem::take(&mut recorder.tax_accruals)
        };
        let tax_payments = if recorder.capture_mode.captures_output() {
            recorder.tax_payments.clone()
        } else {
            std::mem::take(&mut recorder.tax_payments)
        };
        let tax_settlements = if recorder.capture_mode.captures_output() {
            recorder.tax_settlements.clone()
        } else {
            std::mem::take(&mut recorder.tax_settlements)
        };
        Ok(Summary {
            actor_id: actor.into(),
            cash: self.cash,
            bond_principal: self
                .bond_principal
                .into_iter()
                .map(|(_, series)| series)
                .collect(),
            public_holdings: self
                .holdings
                .into_iter()
                .map(|((account, asset_id), values)| HoldingSeries {
                    account,
                    asset_id,
                    values,
                })
                .collect(),
            payments: self.payments,
            unpaid_claims: self.unpaid_claims,
            tax_accruals,
            tax_payments,
            tax_settlements,
            ending_book: month_output(
                input,
                state.rollout_id,
                state.month,
                &state.ledger,
                &state.lots,
                &state.properties,
                &state.mortgages,
                &state.tax_liabilities,
                &state.tax,
                &state.tlh_portfolios,
                state.failed_month.is_some(),
            )?,
            ending_mark_month: state.failed_month.unwrap_or(state.month),
        })
    }
}
