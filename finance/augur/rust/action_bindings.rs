//! Current actor facts and exact requests over the existing in-process Python boundary.
//! Only terminal output and canonical prior receipts use the existing JSON encoding.

use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use std::sync::Arc;

use augur_rust_simulator::engine::{
    CaptureMode, actors, claims, components, payments, trades, transfers,
};
use augur_rust_simulator::event_frames::FramedOutput;
use augur_rust_simulator::execution::{self, BondCoupon};
use augur_rust_simulator::ledger::AccountRef;
use augur_rust_simulator::money::{Money, Quantity};

use crate::{parse, to_py_err};

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
struct HoldingPool {
    account_id: String,
    asset_id: String,
    quantity_scale: i64,
    price: i64,
}

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
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
#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
struct FixedCoupon {
    amount: i64,
}

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
struct IndexedCoupon {
    annual_rate_ppb: i64,
}

#[derive(Clone)]
#[pyclass(frozen, module = "finance.augur.rust._simulator")]
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
#[pyclass(frozen, module = "finance.augur.rust._simulator")]
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

#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
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
    tlh_portfolios: Vec<TlhPortfolioObservation>,
    claims: Vec<Claim>,
    previous_receipts: Py<PyAny>,
}

#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
struct Decision {
    rollout_id: u32,
    observation: Py<Observation>,
}

/// The wrapped canonical request is immutable and contains no execution authority.
#[derive(Clone)]
#[pyclass(frozen, module = "finance.augur.rust._simulator")]
struct Action {
    request: actors::Action,
    claim_owner: Option<(Arc<()>, u32)>,
}

#[pymethods]
impl Action {
    #[getter]
    fn request(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(py
            .import("finance.augur.sim.results")?
            .getattr("action_from_json")?
            .call1((serde_json::to_string(&self.request).map_err(to_py_err)?,))?
            .unbind())
    }
    #[staticmethod]
    fn contribute(
        cause_id: String,
        agent_id: String,
        portfolio_id: String,
        cash_account_id: String,
        amount: i64,
    ) -> Self {
        Self {
            claim_owner: None,
            request: actors::Action::Contribute(components::CashRequest {
                cause_id,
                agent_id,
                portfolio_id,
                cash_account_id,
                amount: Money(amount),
            }),
        }
    }
    #[staticmethod]
    fn withdraw(
        cause_id: String,
        agent_id: String,
        portfolio_id: String,
        cash_account_id: String,
        amount: i64,
    ) -> Self {
        Self {
            claim_owner: None,
            request: actors::Action::Withdraw(components::CashRequest {
                cause_id,
                agent_id,
                portfolio_id,
                cash_account_id,
                amount: Money(amount),
            }),
        }
    }
    #[staticmethod]
    fn liquidate(
        cause_id: String,
        agent_id: String,
        portfolio_id: String,
        cash_account_id: String,
    ) -> Self {
        Self {
            claim_owner: None,
            request: actors::Action::Liquidate(components::LiquidateRequest {
                cause_id,
                agent_id,
                portfolio_id,
                cash_account_id,
            }),
        }
    }
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
#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
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

#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
struct TlhPortfolioObservation {
    portfolio_id: String,
    owner_agent_id: String,
    account_id: String,
    asset_id: String,
    value: i64,
    reported_tax_basis: i64,
}
#[pymethods]
impl TlhPortfolioObservation {
    #[new]
    fn new(
        portfolio_id: String,
        owner_agent_id: String,
        account_id: String,
        asset_id: String,
        value: i64,
        reported_tax_basis: i64,
    ) -> Self {
        Self {
            portfolio_id,
            owner_agent_id,
            account_id,
            asset_id,
            value,
            reported_tax_basis,
        }
    }
}
impl From<execution::TlhPortfolioObservation> for TlhPortfolioObservation {
    fn from(value: execution::TlhPortfolioObservation) -> Self {
        Self {
            portfolio_id: value.portfolio_id,
            owner_agent_id: value.owner_agent_id,
            account_id: value.account_id,
            asset_id: value.asset_id,
            value: value.value.0,
            reported_tax_basis: value.reported_tax_basis.0,
        }
    }
}
impl From<TlhPortfolioObservation> for execution::TlhPortfolioObservation {
    fn from(value: TlhPortfolioObservation) -> Self {
        Self {
            portfolio_id: value.portfolio_id,
            owner_agent_id: value.owner_agent_id,
            account_id: value.account_id,
            asset_id: value.asset_id,
            value: Money(value.value),
            reported_tax_basis: Money(value.reported_tax_basis),
        }
    }
}
#[derive(Clone)]
#[pyclass(frozen, module = "finance.augur.rust._simulator")]
struct InterestIncome {
    inner: components::InterestIncome,
}
#[pymethods]
impl InterestIncome {
    #[new]
    fn new(issuer_jurisdiction_id: Option<String>, amount: i64) -> Self {
        Self {
            inner: components::InterestIncome {
                issuer_jurisdiction_id,
                amount: Money(amount),
            },
        }
    }
}
#[derive(Clone)]
#[pyclass(frozen, module = "finance.augur.rust._simulator")]
struct ComponentEffects {
    inner: components::ComponentEffects,
}
#[pymethods]
impl ComponentEffects {
    #[new]
    fn new(
        observation: TlhPortfolioObservation,
        cash_account_id: Option<String>,
        cash_amount: i64,
        short_term_gain: i64,
        long_term_gain: i64,
        interest: Vec<InterestIncome>,
    ) -> Self {
        Self {
            inner: components::ComponentEffects {
                observation: observation.into(),
                cash_account_id,
                cash_amount: Money(cash_amount),
                short_term_gain: Money(short_term_gain),
                long_term_gain: Money(long_term_gain),
                interest: interest.into_iter().map(|item| item.inner).collect(),
            },
        }
    }
}
#[derive(Clone)]
#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
struct PathStatus {
    rollout_id: u32,
    month: u32,
    stopped: bool,
}
fn statuses(values: Vec<actors::PathStatus>) -> Vec<PathStatus> {
    values
        .into_iter()
        .map(|value| PathStatus {
            rollout_id: value.rollout_id,
            month: value.month,
            stopped: value.stopped,
        })
        .collect()
}
#[derive(Clone)]
#[pyclass(frozen, module = "finance.augur.rust._simulator")]
struct PendingBuy {
    inner: actors::PendingAllocationBuy,
}
#[pymethods]
impl PendingBuy {
    #[getter]
    fn policy_index(&self) -> usize {
        self.inner.policy_index
    }
    #[getter]
    fn sleeve_index(&self) -> usize {
        self.inner.sleeve_index
    }
}
#[pyclass(frozen, get_all, module = "finance.augur.rust._simulator")]
struct AllocationPlan {
    sales: Vec<Action>,
    buys: Vec<PendingBuy>,
}

#[pyclass(name = "_NativeSession", module = "finance.augur.rust._simulator")]
struct NativeSession {
    session: Option<actors::Session>,
    identity: Arc<()>,
    actor: String,
    capture: CaptureMode,
}
#[pymethods]
impl NativeSession {
    #[new]
    #[pyo3(signature = (run, actor, rollout_ids, component_observations, *, capture = "forensic", configured = false, product_actor = None))]
    fn new(
        run: &Bound<'_, PyAny>,
        actor: Option<&str>,
        rollout_ids: Vec<u32>,
        component_observations: Vec<(u32, Vec<TlhPortfolioObservation>)>,
        capture: &str,
        configured: bool,
        product_actor: Option<&str>,
    ) -> PyResult<Self> {
        let input = parse(run)?;
        if !configured && actor.is_none_or(str::is_empty) {
            return Err(to_py_err("actor mode requires an actor"));
        }
        let actor = actor.unwrap_or("");
        let capture = match capture {
            "summary" => CaptureMode::Summary,
            "dense" => CaptureMode::Dense,
            "forensic" => CaptureMode::Forensic,
            _ => return Err(to_py_err("capture must be summary, dense or forensic")),
        };
        let observations = component_observations
            .into_iter()
            .map(|(id, values)| (id, values.into_iter().map(Into::into).collect()))
            .collect();
        let session = run
            .py()
            .detach(|| {
                actors::Session::with_options(
                    input,
                    actor,
                    &rollout_ids,
                    capture,
                    configured,
                    product_actor,
                    observations,
                )
            })
            .map_err(to_py_err)?;
        Ok(Self {
            session: Some(session),
            identity: Arc::new(()),
            actor: actor.into(),
            capture,
        })
    }
    fn start(&mut self) -> PyResult<()> {
        self.with_session(actors::Session::start)
    }
    #[pyo3(signature = (actor_id = None))]
    fn observations(&self, py: Python<'_>, actor_id: Option<&str>) -> PyResult<Py<PyAny>> {
        let session = self.session.as_ref().ok_or_else(closed)?;
        let batch = session
            .decisions_for(actor_id.unwrap_or(&self.actor))
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
                            tlh_portfolios: books
                                .tlh_portfolios()
                                .cloned()
                                .map(TlhPortfolioObservation::from)
                                .collect(),
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

        batch.into_py_any(py)
    }
    fn begin_actions(&mut self, responses: &Bound<'_, PyAny>) -> PyResult<()> {
        let mut session = self.session.take().ok_or_else(closed)?;
        let responses = responses.extract::<Vec<DecisionActions>>()?;
        for response in &responses {
            for action in &response.actions {
                self.validate_claim(response.rollout_id, action)?;
            }
        }
        session
            .begin_actions(
                responses
                    .iter()
                    .map(|response| (response.rollout_id, response.month))
                    .collect(),
            )
            .map_err(to_py_err)?;
        self.session = Some(session);
        Ok(())
    }
    fn apply(&mut self, py: Python<'_>, rollout_id: u32, action: Action) -> PyResult<Py<PyAny>> {
        self.validate_claim(rollout_id, &action)?;
        let receipt = self.with_session(|session| session.apply(rollout_id, action.request))?;
        receipt_object(py, &receipt)
    }
    fn reject(
        &mut self,
        py: Python<'_>,
        rollout_id: u32,
        action: Action,
        detail: String,
    ) -> PyResult<Py<PyAny>> {
        let receipt =
            self.with_session(|session| session.reject(rollout_id, action.request, detail))?;
        receipt_object(py, &receipt)
    }
    #[pyo3(signature = (rollout_id, cause_id, effects, action = None))]
    fn apply_component(
        &mut self,
        py: Python<'_>,
        rollout_id: u32,
        cause_id: &str,
        effects: ComponentEffects,
        action: Option<Action>,
    ) -> PyResult<Option<Py<PyAny>>> {
        self.with_session(|session| {
            session.apply_component(
                rollout_id,
                cause_id,
                &effects.inner,
                action.map(|item| item.request),
            )
        })?
        .map(|receipt| receipt_object(py, &receipt))
        .transpose()
    }
    fn component_distribution(
        &mut self,
        rollout_id: u32,
        distribution_index: usize,
        total: i64,
    ) -> PyResult<()> {
        self.with_session(|session| {
            session.component_distribution(rollout_id, distribution_index, Money(total))
        })
    }
    fn set_component_marks(
        &mut self,
        rows: Vec<(u32, Vec<TlhPortfolioObservation>)>,
    ) -> PyResult<()> {
        self.with_session(|session| {
            session.set_component_marks(
                rows.into_iter()
                    .map(|(id, values)| (id, values.into_iter().map(Into::into).collect()))
                    .collect(),
            )
        })
    }
    fn scheduled_sale(&mut self, rollout_id: u32, sale_index: usize) -> PyResult<()> {
        self.with_session(|session| session.scheduled_sale(rollout_id, sale_index))
    }
    fn allocation_plan(
        &mut self,
        rollout_id: u32,
        policy_index: usize,
    ) -> PyResult<AllocationPlan> {
        let plan =
            self.with_session(|session| session.allocation_plan(rollout_id, policy_index))?;
        Ok(AllocationPlan {
            sales: plan
                .sales
                .into_iter()
                .map(|request| Action {
                    request,
                    claim_owner: None,
                })
                .collect(),
            buys: plan
                .buys
                .into_iter()
                .map(|inner| PendingBuy { inner })
                .collect(),
        })
    }
    fn allocation_buy(&mut self, rollout_id: u32, buy: PendingBuy) -> PyResult<Option<Action>> {
        Ok(self
            .with_session(|session| session.allocation_buy(rollout_id, &buy.inner))?
            .map(|request| Action {
                request,
                claim_owner: None,
            }))
    }
    fn settle_claims(&mut self) -> PyResult<Vec<PathStatus>> {
        self.with_session(actors::Session::settle_claims)
            .map(statuses)
    }
    fn run_private_equity(&mut self) -> PyResult<()> {
        self.with_session(actors::Session::run_private_equity)
    }
    fn end_actions(&mut self) -> PyResult<Vec<PathStatus>> {
        self.with_session(actors::Session::end_actions)
            .map(statuses)
    }
    fn close_month(&mut self) -> PyResult<()> {
        self.with_session(actors::Session::close_month)
    }
    fn current_paths(&self) -> PyResult<Vec<PathStatus>> {
        self.session
            .as_ref()
            .ok_or_else(closed)?
            .statuses()
            .map(statuses)
            .map_err(to_py_err)
    }
    fn is_finished(&self) -> PyResult<bool> {
        Ok(self.session.as_ref().ok_or_else(closed)?.is_finished())
    }
    fn finish(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let rollouts = self.with_session(actors::Session::finish)?;
        let document = format!(
            "{{\"rollouts\":{}}}",
            serde_json::to_string(&rollouts).map_err(to_py_err)?
        );
        self.close();
        Ok(py
            .import("finance.augur.sim.results")?
            .getattr("Finished")?
            .call_method1("model_validate_json", (document,))?
            .unbind())
    }
    fn finish_configured_json(&mut self) -> PyResult<String> {
        let result = if self.capture == CaptureMode::Summary {
            let output = self.with_session(actors::Session::finish_summaries)?;
            serde_json::to_string(&output).map_err(to_py_err)
        } else {
            let output = self.with_session(actors::Session::finish_configured)?;
            serde_json::to_string(&FramedOutput::new(&output)).map_err(to_py_err)
        };
        self.close();
        result
    }
    fn finish_product_metrics(&mut self) -> PyResult<crate::ProductMetrics> {
        let series = self.with_session(actors::Session::finish_product)?;
        self.close();
        Ok(crate::ProductMetrics::from(series))
    }
    fn close(&mut self) {
        self.session = None;
    }
}
impl NativeSession {
    fn with_session<T>(
        &mut self,
        operation: impl FnOnce(
            &mut actors::Session,
        ) -> Result<T, augur_rust_simulator::engine::SimulationError>,
    ) -> PyResult<T> {
        let mut session = self.session.take().ok_or_else(closed)?;
        let result = operation(&mut session).map_err(to_py_err)?;
        self.session = Some(session);
        Ok(result)
    }
    fn validate_claim(&self, rollout_id: u32, action: &Action) -> PyResult<()> {
        if action
            .claim_owner
            .as_ref()
            .is_some_and(|(owner, id)| !Arc::ptr_eq(owner, &self.identity) || *id != rollout_id)
        {
            return Err(to_py_err(
                "claim handle belongs to a different rollout or session",
            ));
        }
        Ok(())
    }
}
fn receipt_object(py: Python<'_>, receipt: &actors::Receipt) -> PyResult<Py<PyAny>> {
    Ok(py
        .import("finance.augur.sim.results")?
        .getattr("Receipt")?
        .call_method1(
            "model_validate_json",
            (serde_json::to_string(receipt).map_err(to_py_err)?,),
        )?
        .unbind())
}
fn closed() -> PyErr {
    to_py_err("action session is finished, aborted or closed")
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeSession>()?;
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
    module.add_class::<TlhPortfolioObservation>()?;
    module.add_class::<ComponentEffects>()?;
    module.add_class::<InterestIncome>()?;
    module.add_class::<PathStatus>()?;
    module.add_class::<AllocationPlan>()?;
    module.add_class::<PendingBuy>()?;
    Ok(())
}
