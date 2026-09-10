//! Current actor facts and exact requests over the existing in-process Python boundary.
//! Only terminal output and canonical prior receipts use the existing JSON encoding.

use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use std::sync::Arc;

use augur_rust_simulator::engine::{CaptureMode, actors, claims, payments, trades, transfers};
use augur_rust_simulator::execution::BondCoupon;
use augur_rust_simulator::ledger::AccountRef;
use augur_rust_simulator::money::{Money, Quantity};

use crate::{parse, to_py_err};

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust.simulator")]
struct HoldingPool {
    account_id: String,
    asset_id: String,
    quantity_scale: i64,
    price: i64,
}

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust.simulator")]
struct PublicPosition {
    account_id: String,
    asset_id: String,
    lot_id: String,
    purchase_month: i32,
    units: i64,
    quantity_scale: i64,
    book_basis: i64,
    price: i64,
    value: i64,
}

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust.simulator")]
struct FixedCoupon {
    amount: i64,
}

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust.simulator")]
struct IndexedCoupon {
    annual_rate_ppb: i64,
}

#[derive(Clone)]
#[pyclass(frozen, module = "finance.augur.rust.simulator")]
struct HeldBond {
    #[pyo3(get)]
    bond_id: String,
    #[pyo3(get)]
    account_id: String,
    #[pyo3(get)]
    issuer_jurisdiction_id: Option<String>,
    #[pyo3(get)]
    face_value: i64,
    #[pyo3(get)]
    purchase_price: i64,
    coupon: BondCoupon,
    #[pyo3(get)]
    coupon_period_months: u32,
    #[pyo3(get)]
    purchase_month: i32,
    #[pyo3(get)]
    maturity_month: i32,
    #[pyo3(get)]
    principal: i64,
}

#[pymethods]
impl HeldBond {
    #[getter]
    fn coupon(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self.coupon {
            BondCoupon::Fixed { amount } => FixedCoupon { amount: amount.0 }.into_py_any(py),
            BondCoupon::Indexed { annual_rate_ppb } => {
                IndexedCoupon { annual_rate_ppb }.into_py_any(py)
            }
        }
    }
}

#[derive(Clone)]
#[pyclass(frozen, module = "finance.augur.rust.simulator")]
struct Claim {
    id: claims::ClaimId,
    rollout_id: u32,
    session: Arc<()>,
    #[pyo3(get)]
    cause_id: String,
    #[pyo3(get)]
    obligation_type: String,
    #[pyo3(get)]
    from_account: (String, String),
    #[pyo3(get)]
    to_account: (String, String),
    #[pyo3(get)]
    amount_due: i64,
    #[pyo3(get)]
    due_month: u32,
}

#[pyclass(frozen, get_all, module = "finance.augur.rust.simulator")]
struct Observation {
    agent_id: String,
    month: u32,
    cpi: Option<(i64, i64)>,
    cash: i64,
    public_holdings: i64,
    accounts: Vec<(String, i64)>,
    holding_pools: Vec<HoldingPool>,
    public_positions: Vec<PublicPosition>,
    held_bonds: Vec<HeldBond>,
    claims: Vec<Claim>,
    previous_receipts: Py<PyAny>,
}

#[pyclass(frozen, get_all, module = "finance.augur.rust.simulator")]
struct Decision {
    rollout_id: u32,
    observation: Py<Observation>,
}

/// The wrapped canonical request is immutable and contains no execution authority.
#[derive(Clone)]
#[pyclass(frozen, module = "finance.augur.rust.simulator")]
struct Action {
    request: actors::Action,
    claim_owner: Option<(Arc<()>, u32)>,
}

#[pymethods]
impl Action {
    #[staticmethod]
    fn sell(
        cause_id: String,
        agent_id: String,
        proceeds_account_id: String,
        asset_id: String,
        lots: Vec<(String, String, i64)>,
    ) -> Self {
        Self {
            claim_owner: None,
            request: actors::Action::Sell(trades::SaleRequest {
                cause_id,
                agent_id,
                proceeds_account_id,
                asset_id,
                lots: lots
                    .into_iter()
                    .map(|(account_id, lot_id, units)| trades::LotSale {
                        account_id,
                        lot_id,
                        units: Quantity(units),
                    })
                    .collect(),
            }),
        }
    }

    #[staticmethod]
    fn buy(
        cause_id: String,
        from_account: (String, String),
        holding_account_id: String,
        asset_id: String,
        lot_id: String,
        units: i64,
        quantity_scale: i64,
    ) -> Self {
        Self {
            claim_owner: None,
            request: actors::Action::Buy(trades::PurchaseRequest {
                cause_id,
                agent_id: from_account.0,
                cash_account_id: from_account.1,
                holding_account_id,
                asset_id,
                lot_id,
                units: Quantity(units),
                quantity_scale,
            }),
        }
    }

    #[staticmethod]
    fn transfer(
        cause_id: String,
        from_account: (String, String),
        to_account: (String, String),
        amount: i64,
    ) -> Self {
        Self {
            claim_owner: None,
            request: actors::Action::Transfer(transfers::TransferRequest {
                cause_id,
                from: account(from_account),
                to: account(to_account),
                amount: Money(amount),
            }),
        }
    }

    #[staticmethod]
    fn pay_claim(
        request_id: u64,
        cause_id: String,
        claim: &Claim,
        from_account: (String, String),
        amount: i64,
    ) -> Self {
        Self {
            claim_owner: Some((Arc::clone(&claim.session), claim.rollout_id)),
            request: actors::Action::PayClaim(payments::PayClaim {
                request_id,
                cause_id,
                claim: claim.id,
                from: account(from_account),
                amount: Money(amount),
            }),
        }
    }

    #[staticmethod]
    fn consume(
        request_id: u64,
        cause_id: String,
        component_id: String,
        from_account: (String, String),
        to_account: (String, String),
        amount: i64,
    ) -> Self {
        Self {
            claim_owner: None,
            request: actors::Action::Consume(payments::Consume {
                request_id,
                cause_id,
                component_id,
                from: account(from_account),
                to: account(to_account),
                amount: Money(amount),
            }),
        }
    }
}

fn account((agent, account): (String, String)) -> AccountRef {
    AccountRef::new(&agent, &account)
}

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust.simulator")]
struct DecisionActions {
    rollout_id: u32,
    month: u32,
    actions: Vec<Action>,
}

#[pymethods]
impl DecisionActions {
    #[new]
    fn new(rollout_id: u32, month: u32, actions: Vec<Action>) -> Self {
        Self {
            rollout_id,
            month,
            actions,
        }
    }
}

#[pyclass(module = "finance.augur.rust.simulator")]
struct ActionSession {
    session: Option<actors::Session>,
    identity: Arc<()>,
}

#[pymethods]
impl ActionSession {
    #[new]
    #[pyo3(signature = (run, actor, rollout_ids, *, capture = "forensic"))]
    fn new(
        py: Python<'_>,
        run: &Bound<'_, PyAny>,
        actor: &str,
        rollout_ids: Vec<u32>,
        capture: &str,
    ) -> PyResult<Self> {
        let input = parse(run)?;
        let capture = match capture {
            "summary" => CaptureMode::Summary,
            "dense" => CaptureMode::Dense,
            "forensic" => CaptureMode::Forensic,
            _ => return Err(to_py_err("capture must be summary, dense or forensic")),
        };
        let session = py
            .detach(|| actors::Session::new(input, actor, &rollout_ids, capture))
            .map_err(to_py_err)?;
        Ok(Self {
            session: Some(session),
            identity: Arc::new(()),
        })
    }

    fn start(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let mut session = self.session.take().ok_or_else(closed)?;
        py.detach(|| session.start()).map_err(to_py_err)?;
        self.handoff(py, session)
    }

    /// Extraction/routing errors close the session too: there is no resubmission path.
    fn advance(&mut self, py: Python<'_>, responses: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let mut session = self.session.take().ok_or_else(closed)?;
        let responses = responses
            .extract::<Vec<DecisionActions>>()?
            .into_iter()
            .map(|response| {
                if response.actions.iter().any(|action| {
                    action.claim_owner.as_ref().is_some_and(|(owner, id)| {
                        !Arc::ptr_eq(owner, &self.identity) || *id != response.rollout_id
                    })
                }) {
                    return Err(to_py_err(
                        "claim handle belongs to a different rollout or session",
                    ));
                }
                Ok(actors::DecisionActions {
                    rollout_id: response.rollout_id,
                    month: response.month,
                    actions: response
                        .actions
                        .into_iter()
                        .map(|action| action.request)
                        .collect(),
                })
            })
            .collect::<PyResult<_>>()?;
        py.detach(|| session.advance(responses))
            .map_err(to_py_err)?;
        self.handoff(py, session)
    }

    fn close(&mut self) {
        self.session = None;
    }
}

fn closed() -> PyErr {
    to_py_err("action session is finished, aborted or closed")
}

impl ActionSession {
    fn handoff(&mut self, py: Python<'_>, mut session: actors::Session) -> PyResult<Py<PyAny>> {
        if session.is_finished() {
            let rollouts = py.detach(|| session.finish()).map_err(to_py_err)?;
            let document = format!(
                "{{\"rollouts\":{}}}",
                serde_json::to_string(&rollouts).map_err(to_py_err)?
            );
            return Ok(py
                .import("finance.augur.sim.results")?
                .getattr("Finished")?
                .call_method1("model_validate_json", (document,))?
                .unbind());
        }
        let batch = session
            .decisions()
            .map_err(to_py_err)?
            .into_iter()
            .map(|decision| {
                let observation = decision.observation;
                let books = &observation.books;
                let cpi = books.cpi().map_err(to_py_err)?;
                Ok(Decision {
                    rollout_id: decision.rollout_id,
                    observation: Py::new(
                        py,
                        Observation {
                            agent_id: books.agent_id().into(),
                            month: books.month(),
                            cpi: cpi.map(|level| (level.numerator(), level.denominator())),
                            cash: books.cash().map_err(to_py_err)?.0,
                            public_holdings: books.public_value().map_err(to_py_err)?.0,
                            accounts: books
                                .accounts()
                                .map(|account| {
                                    account
                                        .map(|account| {
                                            (
                                                account.account.account_id.clone(),
                                                account.available.0,
                                            )
                                        })
                                        .map_err(to_py_err)
                                })
                                .collect::<PyResult<_>>()?,
                            holding_pools: books
                                .holding_pools()
                                .map(|pool| {
                                    Ok(HoldingPool {
                                        account_id: pool.account_id.clone(),
                                        asset_id: pool.asset_id.clone(),
                                        quantity_scale: pool.quantity_scale,
                                        price: books
                                            .public_price(&pool.asset_id)
                                            .map_err(to_py_err)?
                                            .0,
                                    })
                                })
                                .collect::<PyResult<_>>()?,
                            public_positions: books
                                .public_positions()
                                .map(|position| {
                                    let position = position.map_err(to_py_err)?;
                                    Ok(PublicPosition {
                                        account_id: position.account_id().into(),
                                        asset_id: position.asset_id().into(),
                                        lot_id: position.lot_id().into(),
                                        purchase_month: position.purchase_month(),
                                        units: position.units().quantity().0,
                                        quantity_scale: position.quantity_scale(),
                                        book_basis: position.book_basis().0,
                                        price: position.price.0,
                                        value: position.value().map_err(to_py_err)?.0,
                                    })
                                })
                                .collect::<PyResult<_>>()?,
                            held_bonds: observation
                                .held_bonds()
                                .map(|position| {
                                    let position = position.map_err(to_py_err)?;
                                    let terms = position.terms;
                                    Ok(HeldBond {
                                        bond_id: terms.bond_id.clone(),
                                        account_id: terms.account_id.clone(),
                                        issuer_jurisdiction_id: terms
                                            .issuer_jurisdiction_id
                                            .clone(),
                                        face_value: terms.face_value.0,
                                        purchase_price: terms.purchase_price.0,
                                        coupon: terms.coupon,
                                        coupon_period_months: terms.coupon_period_months,
                                        purchase_month: terms.purchase_month_index,
                                        maturity_month: terms.maturity_month_index,
                                        principal: position.principal.0,
                                    })
                                })
                                .collect::<PyResult<_>>()?,
                            claims: observation
                                .claims()
                                .map(|claim| Claim {
                                    id: claim.id,
                                    rollout_id: decision.rollout_id,
                                    session: Arc::clone(&self.identity),
                                    cause_id: claim.cause_id.into(),
                                    obligation_type: claim.obligation_type.into(),
                                    from_account: (
                                        claim.from.agent_id.clone(),
                                        claim.from.account_id.clone(),
                                    ),
                                    to_account: (
                                        claim.to.agent_id.clone(),
                                        claim.to.account_id.clone(),
                                    ),
                                    amount_due: claim.amount_due.0,
                                    due_month: claim.due_month,
                                })
                                .collect(),
                            previous_receipts: py
                                .import("finance.augur.sim.results")?
                                .getattr("receipts_from_json")?
                                .call1((serde_json::to_string(observation.previous_receipts)
                                    .map_err(to_py_err)?,))?
                                .unbind(),
                        },
                    )?,
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        let result = batch.into_py_any(py)?;
        self.session = Some(session);
        Ok(result)
    }
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<ActionSession>()?;
    module.add_class::<Action>()?;
    module.add_class::<Claim>()?;
    module.add_class::<HoldingPool>()?;
    module.add_class::<PublicPosition>()?;
    module.add_class::<HeldBond>()?;
    module.add_class::<FixedCoupon>()?;
    module.add_class::<IndexedCoupon>()?;
    module.add_class::<Observation>()?;
    module.add_class::<Decision>()?;
    module.add_class::<DecisionActions>()?;
    Ok(())
}
