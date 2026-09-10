//! Configured all-or-none-by-source settlement control and its demand/failure capture.
//! Financial execution is shared with direct actions in `payments`.

use super::*;

pub(super) fn mortgage_monthly_payment(
    principal: Money,
    annual_rate_ppb: i64,
    term_months: u32,
) -> Result<Money, SimulationError> {
    if annual_rate_ppb == 0 {
        return Ok(principal.scaled_by(
            Factor::new(1, i64::from(term_months)),
            "zero-rate mortgage payment",
        )?);
    }
    let monthly_rate = mul_div_i128_round_half_up(
        i128::from(annual_rate_ppb),
        CONTRACT_SCALE,
        12 * i128::from(WIRE_RATE_SCALE),
        "mortgage monthly rate",
    )?;
    let factor = CONTRACT_SCALE
        .checked_add(monthly_rate)
        .ok_or(ArithmeticError::Overflow {
            operation: "mortgage rate factor",
        })?;
    let mut discount = CONTRACT_SCALE;
    for _ in 0..term_months {
        discount = mul_div_i128_round_half_up(
            discount,
            CONTRACT_SCALE,
            factor,
            "mortgage discount factor",
        )?;
    }
    let denominator = CONTRACT_SCALE
        .checked_sub(discount)
        .ok_or(ArithmeticError::Overflow {
            operation: "mortgage annuity denominator",
        })?;
    let payment = mul_div_i128_round_half_up(
        i128::from(principal.0),
        monthly_rate,
        denominator,
        "mortgage payment",
    )?;
    Ok(Money(i64::try_from(payment).map_err(|_| {
        ArithmeticError::Overflow {
            operation: "mortgage payment",
        }
    })?))
}

/// The existing runner's explicit all-or-none-by-source settlement control.
/// This is distinct from the ordered, fatal-on-first-rejection actor session.
pub(super) fn settle_grouped(
    context: &mut payments::Context<'_>,
    claims: &mut claims::Claims,
    product_agent_id: Option<&str>,
) -> Result<Settlement, SimulationError> {
    let requests: Vec<_> = claims
        .entries
        .iter()
        .enumerate()
        .filter(|(_, claim)| !claim.paid)
        .map(|(index, claim)| {
            payments::Request::PayClaim(payments::PayClaim {
                request_id: index as u64 + 1,
                cause_id: claim.cause_id.clone(),
                claim: claims::ClaimId {
                    month: claims.month,
                    index,
                },
                from: claim.from.clone(),
                amount: claim.amount_due,
            })
        })
        .collect();
    let mut due_by_source = BTreeMap::<AccountRef, Money>::new();
    for request in &requests {
        let due = due_by_source
            .get(request.from())
            .copied()
            .unwrap_or_default()
            .checked_add(request.amount())?;
        due_by_source.insert(request.from().clone(), due);
    }
    let rejected_by_source: BTreeMap<_, _> = due_by_source
        .into_iter()
        .map(|(account, due)| {
            let available = context.ledger.balance(&account)?;
            let rejection = (available.0 < due.0)
                .then_some(payments::Rejection::UnfundedGroup { available, due });
            Ok((account, rejection))
        })
        .collect::<Result<_, LedgerError>>()?;

    let mut any_failure = false;
    let mut product_shortfall = Money(0);
    for request in &requests {
        let receipt = if let Some(reason) = &rejected_by_source[request.from()] {
            request.receipt(payments::Outcome::Rejected(reason.clone()))
        } else {
            context.execute(&request.from().agent_id, claims, request)?
        };
        let funded = receipt.outcome == payments::Outcome::Paid;
        let amount_paid = receipt.amount_paid();
        let amount_due = receipt.amount_requested;
        let shortfall = amount_due.checked_sub(amount_paid)?;
        any_failure |= !funded;
        let target = request.describe(claims).expect("assembled payment target");
        let obligation_type = target.obligation_type;
        let firing_id = request.cause_id().to_owned();
        let month = context.month;
        if !funded && target.is_tax_payment {
            context.recorder.record_tax_payment(TaxPaymentOutcome {
                month,
                cause_id: firing_id.clone(),
                agent_id: request.from().agent_id.clone(),
                obligation_type: obligation_type.into(),
                amount_due,
                amount_paid,
                shortfall,
            })?;
        }
        context
            .recorder
            .record_obligation(receipt.obligation(request, &target, month, &firing_id)?);
        if product_agent_id.is_some_and(|agent| agent == request.from().agent_id) {
            product_shortfall = product_shortfall.checked_add(shortfall)?;
        }
    }
    Ok(Settlement {
        failed: any_failure,
        product_shortfall,
    })
}

/// Actual payment outcome from the canonical funding groups.
pub(super) struct Settlement {
    pub(super) failed: bool,
    pub(super) product_shortfall: Money,
}
