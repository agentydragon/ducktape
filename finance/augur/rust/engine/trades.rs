//! Exact lot trades, independent of the strategy selecting them.
//!
//! Requests describe holdings and quantities, not FIFO, cash bands or target weights.
//! The configured month loop supplies execution prices and invokes these operations;
//! exposing requests does not expose mutable books or a separate execution loop.

use super::*;

#[derive(Clone, Debug)]
pub(super) struct PlannedDisposition {
    pub(super) lot_index: usize,
    pub(super) units: Quantity,
    pub(super) basis: Money,
    pub(super) proceeds: Money,
    pub(super) realized_gain: Money,
}

/// One exact holding to sell. A lot ID is not authority to sell another account's lot.
#[derive(Clone, Debug)]
pub struct LotSale {
    pub account_id: String,
    pub lot_id: String,
    pub units: Quantity,
}

/// Sell the specified lots of one asset into the owner's declared proceeds account.
/// Selection order is receipt order; the executor never substitutes other lots.
#[derive(Clone, Debug)]
pub struct SaleRequest {
    pub cause_id: String,
    pub agent_id: String,
    pub proceeds_account_id: String,
    pub asset_id: String,
    pub lots: Vec<LotSale>,
}

/// Buy an exact quantity into a new lot, using the owner's declared cash account.
/// Affordability clamping and choice of lot ID belong to the caller, not execution.
#[derive(Clone, Debug)]
pub struct PurchaseRequest {
    pub cause_id: String,
    pub agent_id: String,
    pub cash_account_id: String,
    pub holding_account_id: String,
    pub asset_id: String,
    pub lot_id: String,
    pub quantity_scale: i64,
    pub units: Quantity,
}

fn invalid(cause_id: &str, reason: impl Into<String>) -> SimulationError {
    SimulationError::InvalidTrade {
        cause_id: cause_id.into(),
        reason: reason.into(),
    }
}

fn declared_account(
    input: &ExecutionInput,
    agent_id: &str,
    account_id: &str,
    cause_id: &str,
) -> Result<AccountRef, SimulationError> {
    let account = AccountRef::new(agent_id, account_id);
    if !input
        .scenario
        .accounts
        .iter()
        .any(|spec| spec.account == account)
    {
        return Err(SimulationError::UnknownAccountReference {
            context: cause_id.into(),
            agent_id: agent_id.into(),
            account_id: account_id.into(),
        });
    }
    Ok(account)
}

/// FIFO is an explicit selection helper over the caller's eligible pool. It does not
/// post money or tax facts; a different selector can submit a different exact request.
pub(super) fn select_fifo(
    lots: &[LotState],
    candidates: &[usize],
    units: Quantity,
    cause_id: &str,
) -> Result<Vec<LotSale>, SimulationError> {
    if units.0 <= 0 {
        return Err(SimulationError::InvalidSaleUnits {
            cause_id: cause_id.into(),
            units: units.0,
        });
    }
    let mut ordered = candidates.to_vec();
    if ordered.iter().any(|index| *index >= lots.len()) {
        return Err(invalid(cause_id, "unknown candidate lot"));
    }
    let unique: BTreeSet<_> = ordered.iter().copied().collect();
    if unique.len() != ordered.len() {
        return Err(invalid(cause_id, "duplicate candidate lot"));
    }
    ordered.sort_by(|left, right| {
        let left = &lots[*left].spec;
        let right = &lots[*right].spec;
        (left.purchase_month, &left.lot_id).cmp(&(right.purchase_month, &right.lot_id))
    });
    let mut remaining = units.0;
    let mut selected = Vec::new();
    for index in ordered {
        let lot = &lots[index];
        let sold = remaining.min(lot.units_remaining.0);
        if sold > 0 {
            selected.push(LotSale {
                account_id: lot.spec.account_id.clone(),
                lot_id: lot.spec.lot_id.clone(),
                units: Quantity(sold),
            });
            remaining -= sold;
        }
        if remaining == 0 {
            break;
        }
    }
    if remaining > 0 {
        return Err(SimulationError::InsufficientLotUnits {
            cause_id: cause_id.into(),
            requested: units.0,
            available: units.0 - remaining,
        });
    }
    Ok(selected)
}

/// Prepare every fallible lot, tax and output change before posting the journal.
/// Only touched gain rows and TLH entries are staged; no complete book is cloned.
#[allow(clippy::too_many_arguments)]
pub(super) fn execute_lot_sale(
    input: &ExecutionInput,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut [LotState],
    tax: &mut TaxState,
    tlh: SaleTlh<'_>,
    month: u32,
    price: PerUnit,
    request: &SaleRequest,
) -> Result<(), SimulationError> {
    let proceeds_account = declared_account(
        input,
        &request.agent_id,
        &request.proceeds_account_id,
        &request.cause_id,
    )?;
    if request.lots.is_empty() || price.0 < 0 {
        return Err(invalid(
            &request.cause_id,
            "sale needs lots and a nonnegative execution price",
        ));
    }
    let mut seen = BTreeSet::new();
    let mut planned = Vec::with_capacity(request.lots.len());
    let mut total_proceeds = Money(0);
    let mut total_gain = Money(0);
    for selection in &request.lots {
        if !seen.insert(&selection.lot_id) {
            return Err(invalid(
                &request.cause_id,
                format!("duplicate lot {:?}", selection.lot_id),
            ));
        }
        let (index, lot) = lots
            .iter()
            .enumerate()
            .find(|(_, lot)| lot.spec.lot_id == selection.lot_id)
            .ok_or_else(|| {
                invalid(
                    &request.cause_id,
                    format!("unknown lot {:?}", selection.lot_id),
                )
            })?;
        if lot.spec.agent_id != request.agent_id
            || lot.spec.account_id != selection.account_id
            || lot.spec.asset_id != request.asset_id
        {
            return Err(invalid(
                &request.cause_id,
                format!(
                    "lot {:?} does not belong to the requested owner/account/asset",
                    selection.lot_id
                ),
            ));
        }
        if selection.units.0 <= 0 || selection.units.0 > lot.units_remaining.0 {
            return Err(invalid(
                &request.cause_id,
                format!(
                    "invalid quantity {} for lot {:?} with {} remaining",
                    selection.units.0, selection.lot_id, lot.units_remaining.0
                ),
            ));
        }
        let basis = lot.basis_remaining.apportion(
            selection.units,
            lot.units_remaining,
            "sale basis allocation",
        )?;
        let proceeds = price.times(
            Units::new(selection.units, lot.spec.quantity_scale),
            "sale proceeds",
        )?;
        let realized_gain = proceeds.checked_sub(basis)?;
        total_proceeds = total_proceeds.checked_add(proceeds)?;
        total_gain = total_gain.checked_add(realized_gain)?;
        planned.push(PlannedDisposition {
            lot_index: index,
            units: selection.units,
            basis,
            proceeds,
            realized_gain,
        });
    }
    let give_back = tlh.prepare(input, lots, &planned)?;
    let mut gain_updates = CapitalGainUpdates::new(tax, &request.agent_id);
    let mut replacements = Vec::with_capacity(planned.len());
    let mut dispositions = Vec::with_capacity(planned.len());
    let mut postings = vec![Posting {
        account: proceeds_account,
        amount: total_proceeds,
    }];
    for (item, give_back) in planned.iter().zip(&give_back.by_lot) {
        let lot = &lots[item.lot_index];
        let gain = item.realized_gain.checked_add(*give_back)?;
        let long_term = i64::from(month) - i64::from(lot.spec.purchase_month) >= 12;
        gain_updates.accrue(gain, long_term)?;
        replacements.push((
            item.lot_index,
            Quantity(lot.units_remaining.0 - item.units.0),
            lot.basis_remaining.checked_sub(item.basis)?,
        ));
        postings.push(Posting {
            account: asset_basis_account(&lot.spec),
            amount: item.basis.checked_neg()?,
        });
        dispositions.push(LotDisposition {
            month,
            cause_id: request.cause_id.clone(),
            agent_id: request.agent_id.clone(),
            source_account_id: lot.spec.account_id.clone(),
            asset_id: canonical_lot_asset_id(&lot.spec.asset_id),
            lot_id: lot.spec.lot_id.clone(),
            purchase_month: lot.spec.purchase_month,
            quantity_scale: lot.spec.quantity_scale,
            units: item.units,
            basis: item.basis,
            proceeds: item.proceeds,
            proceeds_account_id: request.proceeds_account_id.clone(),
            realized_gain: item.realized_gain,
        });
    }
    postings.push(Posting {
        account: realized_gain_account(&request.agent_id),
        amount: total_gain.checked_neg()?,
    });
    recorder.apply_sale(
        ledger,
        JournalEntry {
            month,
            cause_id: request.cause_id.clone(),
            postings,
        },
        dispositions,
    )?;
    // All arithmetic and account validation has completed. Nothing below can reject.
    for (index, units, basis) in replacements {
        lots[index].units_remaining = units;
        lots[index].basis_remaining = basis;
    }
    gain_updates.commit();
    tlh.commit(give_back);
    Ok(())
}

pub(super) fn execute_purchase(
    input: &ExecutionInput,
    ledger: &mut Ledger,
    recorder: &mut Recorder,
    lots: &mut Vec<LotState>,
    month: u32,
    price: PerUnit,
    request: &PurchaseRequest,
) -> Result<(), SimulationError> {
    let cash_account = declared_account(
        input,
        &request.agent_id,
        &request.cash_account_id,
        &request.cause_id,
    )?;
    if request.units.0 <= 0
        || price.0 <= 0
        || !is_quantity_scale(request.quantity_scale)
        || private_equity_issuer(&request.asset_id).is_some()
    {
        return Err(invalid(
            &request.cause_id,
            "purchase needs a public security, positive units/price and a supported quantity scale",
        ));
    }
    if request.lot_id.is_empty() || lots.iter().any(|lot| lot.spec.lot_id == request.lot_id) {
        return Err(invalid(
            &request.cause_id,
            "purchase needs a new nonempty lot ID",
        ));
    }
    // Holding pools are declared by lots/allocation bindings, not by the cash-account list.
    let declared = input
        .scenario
        .initial_lots
        .iter()
        .filter(|lot| {
            lot.agent_id == request.agent_id && lot.account_id == request.holding_account_id
        })
        .map(|lot| (&lot.asset_id, lot.quantity_scale))
        .chain(
            input
                .scenario
                .target_allocation_policies
                .iter()
                .filter(|policy| {
                    policy.agent_id == request.agent_id
                        && policy
                            .source_account_ids
                            .first()
                            .unwrap_or(&policy.account_id)
                            == &request.holding_account_id
                })
                .flat_map(|policy| {
                    policy
                        .sleeves
                        .iter()
                        .map(|sleeve| (&sleeve.asset_id, sleeve.quantity_scale))
                }),
        )
        .any(|(asset, scale)| asset == &request.asset_id && scale == request.quantity_scale);
    if !declared {
        return Err(invalid(
            &request.cause_id,
            "holding pool, asset and quantity scale must be declared by the input",
        ));
    }
    let spent = price.times(
        Units::new(request.units, request.quantity_scale),
        "purchase value",
    )?;
    if spent > ledger.balance(&cash_account)? {
        return Err(invalid(&request.cause_id, "insufficient purchase cash"));
    }
    let spec = InitialLotSpec {
        lot_id: request.lot_id.clone(),
        agent_id: request.agent_id.clone(),
        account_id: request.holding_account_id.clone(),
        asset_id: request.asset_id.clone(),
        purchase_month: i32::try_from(month).map_err(|_| ArithmeticError::Overflow {
            operation: "purchase month",
        })?,
        quantity_scale: request.quantity_scale,
        units: request.units,
        basis: spent,
    };
    recorder.apply_entry(
        ledger,
        JournalEntry {
            month,
            cause_id: request.cause_id.clone(),
            postings: vec![
                Posting {
                    account: cash_account,
                    amount: spent.checked_neg()?,
                },
                Posting {
                    account: asset_basis_account(&spec),
                    amount: spent,
                },
            ],
        },
    )?;
    lots.push(LotState {
        spec,
        units_remaining: request.units,
        basis_remaining: spent,
    });
    Ok(())
}
