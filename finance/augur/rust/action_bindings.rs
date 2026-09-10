//! Private transport of financial facts. Domain objects and orchestration live in Python.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

use augur_rust_simulator::engine::{CaptureMode, actors, components, world};
use augur_rust_simulator::execution::{TlhOperation, TlhPortfolioObservation};
use augur_rust_simulator::money::Money;

use crate::{parse, to_py_err};

fn decode<T: DeserializeOwned>(document: &str) -> PyResult<T> {
    serde_json::from_str(document).map_err(to_py_err)
}

fn encode(value: &impl serde::Serialize) -> PyResult<String> {
    serde_json::to_string(value).map_err(to_py_err)
}

#[pyclass(name = "_PreparedWorlds", module = "finance.augur.rust._simulator")]
struct PreparedWorlds {
    prepared: world::Prepared,
}

#[pymethods]
impl PreparedWorlds {
    #[new]
    fn new(run: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self {
            prepared: world::Prepared::new(parse(run)?).map_err(to_py_err)?,
        })
    }

    #[pyo3(signature = (rollout_id, initial_marks_json, *, capture, actor=None, product_actor=None))]
    fn world(
        &self,
        rollout_id: u32,
        initial_marks_json: &str,
        capture: &str,
        actor: Option<&str>,
        product_actor: Option<&str>,
    ) -> PyResult<World> {
        let capture = match capture {
            "summary" => CaptureMode::Summary,
            "dense" => CaptureMode::Dense,
            "forensic" => CaptureMode::Forensic,
            _ => return Err(PyValueError::new_err("unknown capture mode")),
        };
        Ok(World {
            world: Some(
                self.prepared
                    .world(
                        rollout_id,
                        decode(initial_marks_json)?,
                        capture,
                        actor,
                        product_actor,
                    )
                    .map_err(to_py_err)?,
            ),
        })
    }
}

#[pyclass(name = "_World", module = "finance.augur.rust._simulator")]
struct World {
    world: Option<world::World>,
}

impl World {
    fn get(&self) -> PyResult<&world::World> {
        self.world
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("world was consumed"))
    }

    fn get_mut(&mut self) -> PyResult<&mut world::World> {
        self.world
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("world was consumed"))
    }
}

#[pymethods]
impl World {
    fn prepare_month(
        &mut self,
        month: u32,
        mortgage_originations_json: &str,
        mortgage_payoffs_json: &str,
    ) -> PyResult<String> {
        encode(
            &self
                .get_mut()?
                .prepare_month(
                    month,
                    &decode::<Vec<_>>(mortgage_originations_json)?,
                    &decode::<Vec<_>>(mortgage_payoffs_json)?,
                )
                .map_err(to_py_err)?,
        )
    }

    fn assemble_claims(&mut self, mortgage_payments_json: &str) -> PyResult<()> {
        self.get_mut()?
            .assemble_claims(&decode::<Vec<_>>(mortgage_payments_json)?)
            .map_err(to_py_err)
    }

    fn mortgage_principal(&self, liability_id: &str) -> PyResult<i64> {
        Ok(self
            .get()?
            .mortgage_principal(liability_id)
            .map_err(to_py_err)?
            .0)
    }

    fn property_rented_fraction(&self, property_id: &str) -> PyResult<i64> {
        self.get()?
            .property_rented_fraction(property_id)
            .map_err(to_py_err)
    }

    fn paid_mortgages_json(&self) -> PyResult<String> {
        encode(&self.get()?.paid_mortgages())
    }

    fn observe_json(&self, actor: &str) -> PyResult<String> {
        let world = self.get()?;
        let scope = world.scope(actor).map_err(to_py_err)?;
        let observation = world.observe(&scope);
        let books = &observation.books;
        let accounts = books
            .accounts()
            .map(|account| {
                let account = account.map_err(to_py_err)?;
                Ok((account.account.account_id.clone(), account.available.0))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let pools = books
            .holding_pools()
            .map(|pool| {
                Ok(json!({
                    "account_id": pool.account_id,
                    "asset_id": pool.asset_id,
                    "quantity_scale": pool.quantity_scale,
                    "price": books.public_price(&pool.asset_id).map_err(to_py_err)?.0,
                }))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let positions = books
            .public_positions()
            .map(|position| {
                let position = position.map_err(to_py_err)?;
                Ok(json!({
                    "account_id": position.account_id(),
                    "asset_id": position.asset_id(),
                    "lot_id": position.lot_id(),
                    "purchase_month": position.purchase_month(),
                    "units": position.units().quantity().0,
                    "quantity_scale": position.quantity_scale(),
                    "book_basis": position.book_basis().0,
                    "price": position.price.0,
                    "value": position.value().map_err(to_py_err)?.0,
                }))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let bonds = observation
            .held_bonds()
            .map(|position| {
                let position = position.map_err(to_py_err)?;
                let terms = position.terms;
                Ok(json!({
                    "bond_id": terms.bond_id,
                    "account_id": terms.account_id,
                    "issuer_jurisdiction_id": terms.issuer_jurisdiction_id,
                    "face_value": terms.face_value,
                    "purchase_price": terms.purchase_price,
                    "coupon": terms.coupon,
                    "coupon_period_months": terms.coupon_period_months,
                    "purchase_month": terms.purchase_month_index,
                    "maturity_month": terms.maturity_month_index,
                    "principal": position.principal,
                }))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let claims = observation
            .claims()
            .map(|claim| {
                let mut result = serde_json::to_value(claim.id).map_err(to_py_err)?;
                result["cause_id"] = json!(claim.cause_id);
                result["obligation_type"] = json!(claim.obligation_type);
                result["from"] = json!(claim.from);
                result["to"] = json!(claim.to);
                result["amount_due"] = json!(claim.amount_due);
                Ok(result)
            })
            .collect::<PyResult<Vec<Value>>>()?;
        encode(&json!({
            "agent_id": books.agent_id(),
            "month": books.month(),
            "cpi": books.cpi().map_err(to_py_err)?.map(|level| (level.numerator(),level.denominator())),
            "cash": books.cash().map_err(to_py_err)?.0,
            "public_holdings": books.public_value().map_err(to_py_err)?.0,
            "accounts": accounts,
            "holding_pools": pools,
            "public_positions": positions,
            "held_bonds": bonds,
            "tlh_portfolios": books.tlh_portfolios().collect::<Vec<_>>(),
            "claims": claims,
        }))
    }

    fn apply_json(
        &mut self,
        actor: &str,
        action_json: &str,
        action_index: usize,
    ) -> PyResult<String> {
        encode(
            &self
                .get_mut()?
                .apply(actor, &decode::<actors::Action>(action_json)?, action_index)
                .map_err(to_py_err)?,
        )
    }

    fn account_balance(&self, actor: &str, account: &str) -> PyResult<Option<i64>> {
        Ok(self
            .get()?
            .account_balance(actor, account)
            .map_err(to_py_err)?
            .map(|amount| amount.0))
    }

    fn component_distribution(&mut self, distribution_index: usize, total: i64) -> PyResult<()> {
        self.get_mut()?
            .component_distribution(distribution_index, Money(total))
            .map_err(to_py_err)
    }

    #[pyo3(signature = (actor, cause_id, effects_json, action_json=None, *, operation="modeled_realization"))]
    fn apply_component_json(
        &mut self,
        actor: &str,
        cause_id: &str,
        effects_json: &str,
        action_json: Option<&str>,
        operation: &str,
    ) -> PyResult<()> {
        let effects: components::ComponentEffects = decode(effects_json)?;
        let action: Option<actors::Action> = action_json.map(decode).transpose()?;
        let operation = match operation {
            "modeled_realization" => TlhOperation::ModeledRealization,
            "redemption" => TlhOperation::Redemption,
            _ => {
                return Err(PyValueError::new_err(
                    "unknown actionless component operation",
                ));
            }
        };
        self.get_mut()?
            .apply_component(actor, cause_id, &effects, action.as_ref(), operation)
            .map_err(to_py_err)
    }

    fn set_component_marks_json(&mut self, marks_json: &str) -> PyResult<()> {
        let marks: Vec<TlhPortfolioObservation> = decode(marks_json)?;
        self.get_mut()?
            .set_component_marks(marks)
            .map_err(to_py_err)
    }

    fn scheduled_sale(&mut self, sale_index: usize) -> PyResult<()> {
        self.get_mut()?
            .scheduled_sale(sale_index)
            .map_err(to_py_err)
    }

    fn settle_claims_json(&mut self) -> PyResult<String> {
        encode(&self.get_mut()?.settle_claims().map_err(to_py_err)?)
    }

    fn run_private_equity(&mut self) -> PyResult<()> {
        self.get_mut()?.run_private_equity().map_err(to_py_err)
    }

    fn unpaid_claims_json(&self, actor: &str) -> PyResult<String> {
        encode(&self.get()?.unpaid_claims(actor))
    }

    #[pyo3(signature = (*, failed, shortfall, mortgage_interest_json, mortgage_snapshots_json))]
    fn close_month(
        &mut self,
        failed: bool,
        shortfall: i64,
        mortgage_interest_json: &str,
        mortgage_snapshots_json: &str,
    ) -> PyResult<()> {
        self.get_mut()?
            .close_month(
                failed,
                Money(shortfall),
                &decode::<Vec<_>>(mortgage_interest_json)?,
                &decode::<Vec<_>>(mortgage_snapshots_json)?,
            )
            .map_err(to_py_err)
    }

    fn finish_json(&mut self, mortgage_snapshots_json: &str) -> PyResult<String> {
        let world = self
            .world
            .take()
            .ok_or_else(|| PyValueError::new_err("world was consumed"))?;
        encode(
            &world
                .finish(&decode::<Vec<_>>(mortgage_snapshots_json)?)
                .map_err(to_py_err)?,
        )
    }
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PreparedWorlds>()?;
    module.add_class::<World>()?;
    Ok(())
}
