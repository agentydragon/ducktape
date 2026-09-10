//! An authored batch rule: liquidate public lots if due claims exceed cash, then pay.
//! The example has one household cash account; execution owns prices, basis and taxes.

use augur_native_invocation::{run, write_output};
use augur_rust_simulator::{
    allocation::quantity_for_value,
    engine::{
        CaptureMode, SimulationError,
        actors::{self, Action, Decision, DecisionActions},
        payments::PayClaim,
        trades::{LotSale, PurchaseRequest, SaleRequest},
    },
    money::{Money, Quantity},
};
use serde::Serialize;

fn decide(batch: Vec<Decision<'_>>) -> Result<Vec<DecisionActions>, SimulationError> {
    batch
        .into_iter()
        .map(|decision| {
            let observation = decision.observation;
            let claims: Vec<_> = observation.claims().collect();
            let due = claims
                .iter()
                .try_fold(Money(0), |total, claim| total.checked_add(claim.amount_due))?;
            let mut actions = Vec::new();
            let positions = observation
                .books
                .public_positions()
                .collect::<Result<Vec<_>, _>>()?;
            if observation.books.month() == 0
                && positions.is_empty()
                && observation.books.cash()? > Money(0)
            {
                let pool = observation.books.holding_pools().next().ok_or_else(|| {
                    SimulationError::UnsupportedActorInput {
                        reason: "the example's opening investment requires a declared public pool"
                            .into(),
                    }
                })?;
                let price = observation.books.public_price(&pool.asset_id)?;
                let units = quantity_for_value(
                    observation.books.cash()?.0,
                    price.0,
                    pool.quantity_scale,
                    false,
                )?;
                if units > 0 {
                    actions.push(Action::Buy(PurchaseRequest {
                        cause_id: "opening-investment".into(),
                        agent_id: observation.books.agent_id().into(),
                        cash_account_id: "checking".into(),
                        holding_account_id: pool.account_id.clone(),
                        asset_id: pool.asset_id.clone(),
                        lot_id: "opening-investment".into(),
                        quantity_scale: pool.quantity_scale,
                        units: Quantity(units),
                    }));
                }
            }
            if observation.books.cash()? < due {
                for position in positions {
                    actions.push(Action::Sell(SaleRequest {
                        cause_id: format!("fund-{}", position.lot_id()),
                        agent_id: observation.books.agent_id().into(),
                        proceeds_account_id: "checking".into(),
                        asset_id: position.asset_id().into(),
                        lots: vec![LotSale {
                            account_id: position.account_id().into(),
                            lot_id: position.lot_id().into(),
                            units: position.units().quantity(),
                        }],
                    }));
                }
            }
            for (request_id, claim) in claims.into_iter().enumerate() {
                actions.push(Action::PayClaim(PayClaim {
                    request_id: request_id as u64,
                    cause_id: format!("pay-{}", claim.cause_id),
                    claim: claim.id,
                    from: claim.from.clone(),
                    amount: claim.amount_due,
                }));
            }
            Ok(DecisionActions {
                rollout_id: decision.rollout_id,
                month: observation.books.month(),
                actions,
            })
        })
        .collect()
}

#[derive(Serialize)]
struct Output {
    rollouts: Vec<actors::Rollout>,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    run(|input, output, parameters| {
        let (mode, parameters) = parameters
            .split_first()
            .ok_or("expected capture mode and rollout IDs")?;
        let capture = match mode.as_str() {
            "summary" => CaptureMode::Summary,
            "dense" => CaptureMode::Dense,
            "forensic" => CaptureMode::Forensic,
            _ => return Err("capture must be summary, dense or forensic".into()),
        };
        let ids = parameters
            .iter()
            .map(|value| value.parse())
            .collect::<Result<Vec<u32>, _>>()?;
        let rollouts = actors::simulate(input, "example-household", &ids, capture, decide)?;
        write_output(output, &Output { rollouts })
    })
}
