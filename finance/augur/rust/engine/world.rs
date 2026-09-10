//! One rollout's canonical financial books. Ordering and stopping belong to the caller.

use super::*;
use crate::execution::TlhOperation;
use actors::outcomes;
use serde::Serialize;
use std::sync::Arc;

#[derive(Clone)]
pub struct Prepared {
    input: Arc<ExecutionInput>,
}

impl Prepared {
    pub fn new(input: ExecutionInput) -> Result<Self, SimulationError> {
        validate_fixture(&input)?;
        Ok(Self {
            input: Arc::new(input),
        })
    }

    pub fn world(
        &self,
        rollout_id: u32,
        initial_marks: Vec<TlhPortfolioObservation>,
        capture: CaptureMode,
        actor: Option<&str>,
        product_actor: Option<&str>,
    ) -> Result<World, SimulationError> {
        if rollout_id >= self.input.rollout_count {
            return Err(SimulationError::InvalidRolloutSelection);
        }
        let holdings = actor
            .map(|actor| AgentHoldings::resolve(&self.input, actor))
            .transpose()?;
        let product = product_actor
            .map(|actor| ProductInputs::resolve(&self.input, actor))
            .transpose()?;
        let mut state = RolloutState::new(&self.input, rollout_id, capture, product.as_ref())?;
        components::initialize(&self.input, &mut state, initial_marks, product.as_ref())?;
        state.recorder.capture_taxes = actor.is_some();
        let mut capture = holdings
            .as_ref()
            .map(|holdings| outcomes::Capture::new(&self.input, holdings));
        if let (Some(capture), Some(holdings)) = (&mut capture, &holdings) {
            capture.snapshot(&self.input, holdings, &state)?;
        }
        Ok(World {
            input: Arc::clone(&self.input),
            state,
            claims: Claims {
                month: 0,
                entries: Vec::new(),
            },
            holdings,
            product,
            capture,
        })
    }
}

pub struct World {
    input: Arc<ExecutionInput>,
    state: RolloutState,
    claims: Claims,
    holdings: Option<AgentHoldings>,
    product: Option<ProductInputs>,
    capture: Option<outcomes::Capture>,
}

#[derive(Serialize)]
pub struct Settlement {
    pub failed: bool,
    pub product_shortfall: Money,
}

#[derive(Serialize)]
pub struct Finished {
    pub rollout_id: u32,
    pub summary: Option<outcomes::Summary>,
    pub financial: Option<RolloutOutput>,
    pub event_frames: Option<crate::event_frames::EventFrames>,
    pub configured_summary: Option<RolloutSummary>,
    /// Observed snapshots only; population layout and post-stop padding are caller-owned.
    pub product_metrics: Vec<BaseMetrics>,
}

impl World {
    pub fn prepare_month(
        &mut self,
        month: u32,
        originations: &[mortgages::Origination],
        payoffs: &[mortgages::Payoff],
    ) -> Result<mortgages::OpeningEvents, SimulationError> {
        if month != self.state.month || month >= self.input.scenario.horizon_months {
            return Err(SimulationError::InvalidFinancialMonth {
                expected: self.state.month,
                actual: month,
            });
        }
        self.claims = Claims {
            month,
            entries: Vec::new(),
        };
        self.state
            .prepare_month_events(&self.input, originations, payoffs)
    }

    pub fn assemble_claims(
        &mut self,
        installments: &[mortgages::Installment],
    ) -> Result<(), SimulationError> {
        let mut ids = BTreeSet::new();
        for installment in installments {
            let (purchase, _) = mortgages::terms(&self.input, &installment.liability_id)?;
            let principal = self.mortgage_principal(&installment.liability_id)?;
            if !ids.insert(&installment.liability_id)
                || purchase.month >= self.state.month
                || principal.0 <= 0
                || installment.principal.0 < 0
                || installment.principal > principal
                || installment.interest.0 < 0
                || installment.rental_interest.0 < 0
                || installment.rental_interest > installment.interest
            {
                return Err(mortgages::invalid("invalid mortgage installment"));
            }
        }
        self.claims = claims::assemble(
            &self.input,
            self.state.rollout_id,
            self.state.month,
            &self.state.properties,
            installments,
            &self.state.tax_liabilities,
        )?;
        Ok(())
    }

    pub fn mortgage_principal(&self, liability_id: &str) -> Result<Money, SimulationError> {
        mortgages::principal(&self.input, &self.state.ledger, liability_id)
    }

    pub fn property_rented_fraction(&self, property_id: &str) -> Result<i64, SimulationError> {
        self.state
            .properties
            .iter()
            .find(|property| property.property_id == property_id)
            .map(|property| property.rented_fraction_ppb)
            .ok_or_else(|| mortgages::invalid("property has not originated"))
    }

    pub fn paid_mortgages(&self) -> Vec<&str> {
        self.claims
            .entries
            .iter()
            .filter_map(|claim| match &claim.effect {
                ObligationEffect::Mortgage(installment) if claim.paid => {
                    Some(installment.liability_id.as_str())
                }
                _ => None,
            })
            .collect()
    }

    pub fn observe<'a>(&'a self, scope: &'a AgentHoldings) -> actors::Observation<'a> {
        actors::Observation {
            books: observations::ActorBooks {
                scope,
                books: observations::Books {
                    ledger: &self.state.ledger,
                    lots: &self.state.lots,
                    tax: &self.state.tax,
                    tax_liabilities: &self.state.tax_liabilities,
                    tlh_portfolios: &self.state.tlh_portfolios,
                },
                input: &self.input,
                rollout: self.state.rollout_id,
                month: self.state.month,
            },
            claims: &self.claims,
        }
    }

    pub fn scope(&self, actor: &str) -> Result<AgentHoldings, SimulationError> {
        Ok(AgentHoldings::resolve(&self.input, actor)?)
    }

    pub fn apply(
        &mut self,
        actor: &str,
        action: &actors::Action,
        action_index: usize,
    ) -> Result<actors::Outcome, SimulationError> {
        let scope = self.scope(actor)?;
        let (outcome, payment) = actors::execute(
            &mut self.state,
            &self.input,
            &scope,
            &mut self.claims,
            action,
        )?;
        if let (Some((request, receipt)), Some(capture)) = (payment, &mut self.capture) {
            capture.payments.push(outcomes::Payment::new(
                self.state.month,
                action_index,
                &request,
                &self.claims,
                receipt,
            ));
        }
        Ok(outcome)
    }

    pub fn account_balance(
        &self,
        actor: &str,
        account: &str,
    ) -> Result<Option<Money>, SimulationError> {
        let key = AccountRef::new(actor, account);
        if !self
            .input
            .scenario
            .accounts
            .iter()
            .any(|item| item.account == key)
        {
            return Ok(None);
        }
        Ok(Some(self.state.ledger.balance(&key)?))
    }

    pub fn apply_component(
        &mut self,
        actor: &str,
        cause_id: &str,
        effects: &components::ComponentEffects,
        action: Option<&actors::Action>,
        operation: TlhOperation,
    ) -> Result<(), SimulationError> {
        let operation = match action {
            Some(actors::Action::Contribute(_)) => TlhOperation::Contribution,
            Some(actors::Action::Withdraw(_) | actors::Action::Liquidate(_)) => {
                TlhOperation::Redemption
            }
            Some(_) => {
                return Err(SimulationError::InvalidComponentEffect {
                    reason: "not a component request".into(),
                });
            }
            None => operation,
        };
        if let Some(action) = action {
            if action.cause_id() != cause_id {
                return Err(SimulationError::InvalidComponentEffect {
                    reason: "action and component effect cause differ".into(),
                });
            }
            components::validate_request(action, effects, &self.state.tlh_portfolios)?;
        }
        components::settle(
            &self.input,
            &mut self.state,
            actor,
            cause_id,
            effects,
            operation,
        )
    }

    pub fn set_component_marks(
        &mut self,
        marks: Vec<TlhPortfolioObservation>,
    ) -> Result<(), SimulationError> {
        components::mark(&self.input, &mut self.state, marks)
    }

    pub fn component_distribution(
        &mut self,
        distribution_index: usize,
        total: Money,
    ) -> Result<(), SimulationError> {
        if total.0 < 0 {
            return Err(SimulationError::InvalidComponentEffect {
                reason: "negative distribution".into(),
            });
        }
        let spec = self
            .input
            .scenario
            .distributions
            .get(distribution_index)
            .ok_or_else(|| SimulationError::InvalidComponentEffect {
                reason: "unknown distribution".into(),
            })?;
        let observation = self
            .state
            .tlh_portfolios
            .iter()
            .find(|item| {
                item.owner_agent_id == spec.agent_id
                    && item.account_id == spec.holding_account_id
                    && item.asset_id == spec.asset_id
            })
            .ok_or_else(|| SimulationError::InvalidComponentEffect {
                reason: "distribution has no managed holding".into(),
            })?
            .clone();
        let mut outcomes = Vec::new();
        let mut interest = Vec::new();
        let mut cash = Money(0);
        for (index, slice) in spec.tax_character.iter().enumerate() {
            let amount = distribution_slice(total, slice.fraction_ppb)?;
            cash = cash.checked_add(amount)?;
            interest.push(components::InterestIncome {
                issuer_jurisdiction_id: slice.issuer_jurisdiction_id.clone(),
                amount,
            });
            outcomes.push(DistributionOutcome {
                month: self.state.month,
                agent_id: spec.agent_id.clone(),
                holding_account_id: spec.holding_account_id.clone(),
                asset_id: spec.asset_id.clone(),
                slice_index: u32::try_from(index).map_err(|_| ArithmeticError::Overflow {
                    operation: "distribution slice index",
                })?,
                fraction_ppb: slice.fraction_ppb,
                issuer_jurisdiction_id: slice.issuer_jurisdiction_id.clone(),
                units: None,
                amount,
            });
        }
        let count = self
            .state
            .recorder
            .distribution_count
            .checked_add(outcomes.len() as u64)
            .ok_or(ArithmeticError::Overflow {
                operation: "distribution count",
            })?;
        components::settle(
            &self.input,
            &mut self.state,
            &spec.agent_id,
            &format!(
                "distribution:{}:{}:m{}",
                spec.agent_id, spec.asset_id, self.claims.month
            ),
            &components::ComponentEffects {
                observation,
                cash_account_id: Some(spec.to_account_id.clone()),
                cash_amount: cash,
                short_term_gain: Money(0),
                long_term_gain: Money(0),
                interest,
            },
            TlhOperation::Distribution,
        )?;
        self.state.recorder.distribution_count = count;
        if self.state.recorder.capture_mode.captures_output() {
            self.state.recorder.distributions.extend(outcomes);
        }
        Ok(())
    }

    pub fn scheduled_sale(&mut self, sale_index: usize) -> Result<(), SimulationError> {
        let sale = self
            .input
            .scenario
            .scheduled_sales
            .get(sale_index)
            .ok_or(SimulationError::InvalidScheduledSale { index: sale_index })?;
        if sale.month != self.state.month {
            return Err(SimulationError::InvalidFinancialMonth {
                expected: self.state.month,
                actual: sale.month,
            });
        }
        execute_sale(
            &self.input,
            self.state.rollout_id,
            &mut self.state.ledger,
            &mut self.state.recorder,
            &mut self.state.lots,
            &mut self.state.tax,
            sale,
        )
    }

    pub fn settle_claims(&mut self) -> Result<Settlement, SimulationError> {
        let settled = settle_grouped(
            &mut payments::Context {
                fixture: &self.input,
                ledger: &mut self.state.ledger,
                recorder: &mut self.state.recorder,
                tax: &mut self.state.tax,
                tax_liabilities: &mut self.state.tax_liabilities,
                month: self.state.month,
            },
            &mut self.claims,
            self.product.as_ref().map(ProductInputs::primary_agent_id),
        )?;
        Ok(Settlement {
            failed: settled.failed,
            product_shortfall: settled.product_shortfall,
        })
    }

    pub fn run_private_equity(&mut self) -> Result<(), SimulationError> {
        execute_private_equity(
            &self.input,
            self.state.rollout_id,
            &mut self.state.ledger,
            &mut self.state.recorder,
            &mut self.state.lots,
            &mut self.state.tax,
            &self.state.tlh_portfolios,
            self.state.month,
        )
    }

    pub fn unpaid_claims(&self, actor: &str) -> Vec<outcomes::UnpaidClaim> {
        observations::due_claims(&self.claims, actor)
            .filter(|claim| claim.amount_due.0 > 0)
            .map(|claim| outcomes::UnpaidClaim {
                id: claim.id,
                cause_id: claim.cause_id.into(),
                obligation_type: claim.obligation_type.into(),
                from: claim.from.clone(),
                to: claim.to.clone(),
                amount_due: claim.amount_due,
            })
            .collect()
    }

    pub fn close_month(
        &mut self,
        failed: bool,
        shortfall: Money,
        interest: &[mortgages::Interest],
        snapshots: &[MortgageSnapshot],
    ) -> Result<(), SimulationError> {
        mortgages::validate_snapshots(&self.input, &self.state, snapshots)?;
        let mut interest_ids = BTreeSet::new();
        for row in interest {
            mortgages::terms(&self.input, &row.liability_id)?;
            if !interest_ids.insert(&row.liability_id)
                || row.owner_interest_paid_ytd.0 < 0
                || row.origination_principal.0 <= 0
            {
                return Err(mortgages::invalid("invalid mortgage tax-year fact"));
            }
        }
        if interest_ids != snapshots.iter().map(|item| &item.liability_id).collect() {
            return Err(mortgages::invalid(
                "mortgage tax facts must cover originated loans",
            ));
        }
        if let Some(holdings) = &self.holdings {
            let unpaid = self.unpaid_claims(holdings.agent_id());
            self.capture.as_mut().expect("actor capture").unpaid_claims = unpaid;
            actors::record_claims(&mut self.state.recorder, &self.claims);
        }
        if failed {
            self.state.failed_month = Some(self.state.month);
        }
        self.state.close_month(
            &self.input,
            self.product.as_ref(),
            shortfall,
            interest,
            snapshots,
        )?;
        if let (Some(capture), Some(holdings)) = (&mut self.capture, &self.holdings) {
            capture.snapshot(&self.input, holdings, &self.state)?;
        }
        Ok(())
    }

    pub fn finish(mut self, snapshots: &[MortgageSnapshot]) -> Result<Finished, SimulationError> {
        mortgages::validate_snapshots(&self.input, &self.state, snapshots)?;
        let summary = self
            .capture
            .take()
            .map(|capture| {
                capture.finish(
                    &self.input,
                    self.holdings.as_ref().expect("actor scope").agent_id(),
                    &mut self.state,
                    snapshots,
                )
            })
            .transpose()?;
        let product_metrics = std::mem::take(&mut self.state.product_metrics);
        let captures_output = self.state.recorder.capture_mode.captures_output();
        let computation = self.state.finish(&self.input, snapshots)?;
        let rollout_id = computation.rollout_id;
        let (financial, configured_summary) = if captures_output {
            (Some(computation.into_output()), None)
        } else if summary.is_none() {
            (None, Some(computation.into_summary()))
        } else {
            (None, None)
        };
        let event_frames = financial
            .as_ref()
            .map(|financial| crate::event_frames::EventFrames::from_rollouts([financial]));
        Ok(Finished {
            rollout_id,
            summary,
            financial,
            event_frames,
            configured_summary,
            product_metrics,
        })
    }
}
