//! Private, bounded financial phases used by the Python driver. These methods do not
//! invoke policy and cannot retry a rejected action or reopen an ended month.

use super::*;
use crate::execution::TlhOperation;

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum ActionPhase {
    Awaiting,
    Executing,
    Settled,
    PrivateEquityComplete,
    Ended,
}

#[derive(Clone, Debug)]
pub struct PathStatus {
    pub rollout_id: u32,
    pub month: u32,
    pub stopped: bool,
}

fn append_receipt(path: &mut Path, action: Action, outcome: Outcome) -> Receipt {
    let action_index = path.previous_receipts.len();
    if matches!(outcome, Outcome::Rejected(_)) {
        path.stop = Some(Stop::RejectedAction {
            month: path.state.month,
            action_index,
        });
        path.state.failed_month = Some(path.state.month);
    }
    let receipt = Receipt {
        month: path.state.month,
        action_index,
        action,
        outcome,
    };
    path.previous_receipts.push(receipt.clone());
    receipt
}

impl Session {
    /// Private request preflight reads one actual account, not a copied population.
    pub fn account_balance(
        &self,
        rollout_id: u32,
        agent: &str,
        account: &str,
    ) -> Result<Option<Money>, SimulationError> {
        if !self.configured
            && self
                .holdings
                .as_ref()
                .is_none_or(|scope| scope.agent_id() != agent)
        {
            return Ok(None);
        }
        let Phase::Pending { paths, claims } = &self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let index = paths
            .iter()
            .position(|path| path.state.rollout_id == rollout_id)
            .ok_or(SimulationError::InvalidRolloutSelection)?;
        if claims[index].is_none() || paths[index].state.failed_month.is_some() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let key = AccountRef::new(agent, account);
        if !self
            .input
            .scenario
            .accounts
            .iter()
            .any(|item| item.account == key)
        {
            return Ok(None);
        }
        Ok(Some(paths[index].state.ledger.balance(&key)?))
    }

    pub fn component_distribution(
        &mut self,
        rollout_id: u32,
        distribution_index: usize,
        total: Money,
    ) -> Result<(), SimulationError> {
        if self.action_phase != ActionPhase::Awaiting || total.0 < 0 {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let index = paths
            .iter()
            .position(|path| path.state.rollout_id == rollout_id)
            .ok_or(SimulationError::InvalidRolloutSelection)?;
        let state = &mut paths[index].state;
        if claims[index].is_none() || state.failed_month.is_some() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let spec = self
            .input
            .scenario
            .distributions
            .get(distribution_index)
            .ok_or(SimulationError::InvalidActorSessionState)?;
        let observation = state
            .tlh_portfolios
            .iter()
            .find(|item| {
                item.owner_agent_id == spec.agent_id
                    && item.account_id == spec.holding_account_id
                    && item.asset_id == spec.asset_id
            })
            .ok_or(SimulationError::InvalidActorSessionState)?
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
                month: state.month,
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
        let count = state
            .recorder
            .distribution_count
            .checked_add(outcomes.len() as u64)
            .ok_or(ArithmeticError::Overflow {
                operation: "distribution count",
            })?;
        components::settle(
            &self.input,
            state,
            &spec.agent_id,
            &format!(
                "distribution:{}:{}:m{}",
                spec.agent_id, spec.asset_id, state.month
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
        state.recorder.distribution_count = count;
        if state.recorder.capture_mode.captures_output() {
            state.recorder.distributions.extend(outcomes);
        }
        Ok(())
    }

    pub fn begin_actions(&mut self, keys: Vec<(u32, u32)>) -> Result<(), SimulationError> {
        if self.action_phase != ActionPhase::Awaiting {
            self.phase = Phase::Closed;
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let expected: BTreeSet<_> = paths
            .iter()
            .zip(claims.iter())
            .filter(|(_, claims)| claims.is_some())
            .map(|(path, _)| (path.state.rollout_id, path.state.month))
            .collect();
        let actual: BTreeSet<_> = keys.iter().copied().collect();
        if actual != expected || keys.len() != expected.len() {
            self.phase = Phase::Closed;
            return Err(SimulationError::InvalidActorResponses);
        }
        for (path, claims) in paths.iter_mut().zip(claims.iter()) {
            if claims.is_some() {
                path.previous_receipts.clear();
            }
        }
        self.action_phase = ActionPhase::Executing;
        Ok(())
    }

    pub fn apply(&mut self, rollout_id: u32, action: Action) -> Result<Receipt, SimulationError> {
        if !matches!(
            self.action_phase,
            ActionPhase::Executing | ActionPhase::Settled
        ) {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let index = paths
            .iter()
            .position(|path| path.state.rollout_id == rollout_id)
            .ok_or(SimulationError::InvalidRolloutSelection)?;
        let path = &mut paths[index];
        let claims = claims[index]
            .as_mut()
            .ok_or(SimulationError::InvalidActorSessionState)?;
        if path.state.failed_month.is_some() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let actor = if self.configured {
            action_actor(&action)
        } else {
            self.holdings
                .as_ref()
                .ok_or(SimulationError::InvalidActorSessionState)?
                .agent_id()
        };
        let scope = self
            .scopes
            .get(actor)
            .ok_or(SimulationError::InvalidActorSessionState)?;
        let (outcome, payment) = execute(&mut path.state, &self.input, scope, claims, &action)?;
        if let (Some((request, receipt)), Some(capture)) = (payment, &mut path.capture) {
            capture.payments.push(outcomes::Payment::new(
                path.state.month,
                path.previous_receipts.len(),
                &request,
                claims,
                receipt,
            ));
        }
        Ok(append_receipt(path, action, outcome))
    }

    pub fn reject(
        &mut self,
        rollout_id: u32,
        action: Action,
        detail: String,
    ) -> Result<Receipt, SimulationError> {
        if !matches!(
            self.action_phase,
            ActionPhase::Executing | ActionPhase::Settled
        ) {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let index = paths
            .iter()
            .position(|path| path.state.rollout_id == rollout_id)
            .ok_or(SimulationError::InvalidRolloutSelection)?;
        let path = &mut paths[index];
        if claims[index].is_none() || path.state.failed_month.is_some() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        Ok(append_receipt(
            path,
            action,
            Outcome::Rejected(Rejection::InvalidRequest(detail)),
        ))
    }

    pub fn apply_component(
        &mut self,
        rollout_id: u32,
        cause_id: &str,
        effects: &components::ComponentEffects,
        action: Option<Action>,
        operation: TlhOperation,
    ) -> Result<Option<Receipt>, SimulationError> {
        if matches!(
            self.action_phase,
            ActionPhase::Ended | ActionPhase::PrivateEquityComplete
        ) || (action.is_some()
            && !matches!(
                self.action_phase,
                ActionPhase::Executing | ActionPhase::Settled
            ))
        {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let index = paths
            .iter()
            .position(|path| path.state.rollout_id == rollout_id)
            .ok_or(SimulationError::InvalidRolloutSelection)?;
        let path = &mut paths[index];
        if claims[index].is_none() || path.state.failed_month.is_some() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let operation = match &action {
            Some(Action::Contribute(_)) => TlhOperation::Contribution,
            Some(Action::Withdraw(_) | Action::Liquidate(_)) => TlhOperation::Redemption,
            Some(_) => return Err(SimulationError::InvalidActorSessionState),
            None if operation == TlhOperation::ModeledRealization
                && self.action_phase == ActionPhase::Awaiting =>
            {
                operation
            }
            None if operation == TlhOperation::Redemption
                && self.configured
                && self.action_phase == ActionPhase::Executing =>
            {
                operation
            }
            None => return Err(SimulationError::InvalidActorSessionState),
        };
        if action
            .as_ref()
            .is_some_and(|request| request.cause_id() != cause_id)
        {
            return Err(SimulationError::InvalidComponentEffect {
                reason: "action cause differs from component effect cause".into(),
            });
        }
        let actor = if self.configured || action.is_none() {
            effects.observation.owner_agent_id.as_str()
        } else {
            self.holdings
                .as_ref()
                .ok_or(SimulationError::InvalidActorSessionState)?
                .agent_id()
        };
        let result = action
            .as_ref()
            .map_or(Ok(()), |action| {
                components::validate_request(action, effects, &path.state.tlh_portfolios)
            })
            .and_then(|()| {
                components::settle(
                    &self.input,
                    &mut path.state,
                    actor,
                    cause_id,
                    effects,
                    operation,
                )
            });
        if let Some(action) = action {
            // Expected investor failures use reject before the component runs. A bad
            // effect is a model/accounting error, not a simulated investment failure.
            result?;
            Ok(Some(append_receipt(path, action, Outcome::Executed)))
        } else {
            result?;
            Ok(None)
        }
    }

    pub fn statuses(&self) -> Result<Vec<PathStatus>, SimulationError> {
        if matches!(self.phase, Phase::Finished(_)) {
            return Ok(Vec::new());
        }
        let Phase::Pending { paths, claims } = &self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        Ok(paths
            .iter()
            .zip(claims)
            .filter(|(_, claims)| claims.is_some())
            .map(|(path, _)| PathStatus {
                rollout_id: path.state.rollout_id,
                month: path.state.month,
                stopped: path.state.failed_month.is_some(),
            })
            .collect())
    }

    pub fn end_actions(&mut self) -> Result<Vec<PathStatus>, SimulationError> {
        if self.action_phase
            != (if self.configured {
                ActionPhase::PrivateEquityComplete
            } else {
                ActionPhase::Executing
            })
        {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        for (path, claims) in paths.iter_mut().zip(claims) {
            let Some(claims) = claims else {
                continue;
            };
            if self.configured {
                if let Some(receipts) = &mut path.trace_receipts {
                    receipts.extend(path.previous_receipts.iter().cloned());
                }
            } else {
                finish_actions(path, self.holdings.as_ref().expect("actor scope"), claims);
            }
        }
        self.action_phase = ActionPhase::Ended;
        self.statuses()
    }

    pub fn set_component_marks(
        &mut self,
        rows: Vec<(u32, Vec<TlhPortfolioObservation>)>,
    ) -> Result<(), SimulationError> {
        if self.action_phase != ActionPhase::Ended {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let expected: BTreeSet<_> = paths
            .iter()
            .zip(claims.iter())
            .filter(|(_, claims)| claims.is_some())
            .map(|(path, _)| path.state.rollout_id)
            .collect();
        let actual: BTreeSet<_> = rows.iter().map(|(id, _)| *id).collect();
        if expected != actual || actual.len() != rows.len() {
            return Err(SimulationError::InvalidRolloutSelection);
        }
        for (id, observations) in rows {
            let path = paths
                .iter_mut()
                .find(|path| path.state.rollout_id == id)
                .expect("validated identity");
            components::mark(&self.input, &mut path.state, observations)?;
        }
        Ok(())
    }

    pub fn close_month(&mut self) -> Result<(), SimulationError> {
        if self.action_phase != ActionPhase::Ended {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { mut paths, claims } =
            std::mem::replace(&mut self.phase, Phase::Closed)
        else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        for (path, claims) in paths.iter_mut().zip(claims) {
            if claims.is_some() {
                path.state.close_month(
                    &self.input,
                    self.product.as_ref(),
                    path.product_shortfall,
                )?;
                if let Some(capture) = &mut path.capture {
                    capture.snapshot(
                        &self.input,
                        self.holdings.as_ref().expect("actor scope"),
                        &path.state,
                    )?;
                }
            }
        }
        self.prepare(paths)
    }

    pub fn allocation_plan(
        &self,
        rollout_id: u32,
        policy_index: usize,
    ) -> Result<AllocationPlan, SimulationError> {
        if !self.configured || self.action_phase != ActionPhase::Executing {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let index = paths
            .iter()
            .position(|path| path.state.rollout_id == rollout_id)
            .ok_or(SimulationError::InvalidRolloutSelection)?;
        if paths[index].state.failed_month.is_some() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        allocation_plan(
            &self.input,
            &paths[index].state,
            claims[index]
                .as_ref()
                .ok_or(SimulationError::InvalidActorSessionState)?,
            policy_index,
        )
    }

    pub fn allocation_buy(
        &mut self,
        rollout_id: u32,
        order: &PendingAllocationBuy,
    ) -> Result<Option<Action>, SimulationError> {
        if !self.configured || self.action_phase != ActionPhase::Settled {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let index = paths
            .iter()
            .position(|path| path.state.rollout_id == rollout_id)
            .ok_or(SimulationError::InvalidRolloutSelection)?;
        if claims[index].is_none() || paths[index].state.failed_month.is_some() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        allocation_buy(&self.input, &mut paths[index].state, order)
    }

    pub fn finish_summaries(&mut self) -> Result<PopulationOutput, SimulationError> {
        if !self.configured {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Finished(paths) = std::mem::replace(&mut self.phase, Phase::Closed) else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        Ok(PopulationOutput {
            schema_version: INPUT_SCHEMA_VERSION,
            rollouts: paths
                .into_iter()
                .map(|path| {
                    path.state
                        .finish(&self.input)
                        .map(RolloutComputation::into_summary)
                })
                .collect::<Result<_, _>>()?,
        })
    }

    pub fn scheduled_sale(
        &mut self,
        rollout_id: u32,
        sale_index: usize,
    ) -> Result<(), SimulationError> {
        if !self.configured || self.action_phase != ActionPhase::Executing {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let index = paths
            .iter()
            .position(|path| path.state.rollout_id == rollout_id)
            .ok_or(SimulationError::InvalidRolloutSelection)?;
        let state = &mut paths[index].state;
        if claims[index].is_none() || state.failed_month.is_some() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let sale = self
            .input
            .scenario
            .scheduled_sales
            .get(sale_index)
            .ok_or(SimulationError::InvalidActorSessionState)?;
        if sale.month != state.month {
            return Err(SimulationError::InvalidActorSessionState);
        }
        execute_sale(
            &self.input,
            rollout_id,
            &mut state.ledger,
            &mut state.recorder,
            &mut state.lots,
            &mut state.tax,
            sale,
        )
    }

    pub fn settle_claims(&mut self) -> Result<Vec<PathStatus>, SimulationError> {
        if !self.configured || self.action_phase != ActionPhase::Executing {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        for (path, claims) in paths.iter_mut().zip(claims) {
            let Some(claims) = claims else {
                continue;
            };
            let state = &mut path.state;
            if state.failed_month.is_some() {
                continue;
            }
            let settled = settle_grouped(
                &mut payments::Context {
                    fixture: &self.input,
                    ledger: &mut state.ledger,
                    recorder: &mut state.recorder,
                    tax: &mut state.tax,
                    properties: &state.properties,
                    mortgages: &mut state.mortgages,
                    tax_liabilities: &mut state.tax_liabilities,
                    month: state.month,
                },
                claims,
                self.product.as_ref().map(ProductInputs::primary_agent_id),
            )?;
            path.product_shortfall = settled.product_shortfall;
            if settled.failed {
                state.failed_month = Some(state.month);
            }
        }
        self.action_phase = ActionPhase::Settled;
        self.statuses()
    }

    pub fn run_private_equity(&mut self) -> Result<(), SimulationError> {
        if !self.configured || self.action_phase != ActionPhase::Settled {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Pending { paths, claims } = &mut self.phase else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        for (path, claims) in paths.iter_mut().zip(claims) {
            if claims.is_none() || path.state.failed_month.is_some() {
                continue;
            }
            let state = &mut path.state;
            execute_private_equity(
                &self.input,
                state.rollout_id,
                &mut state.ledger,
                &mut state.recorder,
                &mut state.lots,
                &mut state.tax,
                &state.tlh_portfolios,
                state.month,
            )?;
        }
        self.action_phase = ActionPhase::PrivateEquityComplete;
        Ok(())
    }

    pub fn finish_configured(&mut self) -> Result<SimulationOutput, SimulationError> {
        if !self.configured {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Finished(paths) = std::mem::replace(&mut self.phase, Phase::Closed) else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        Ok(SimulationOutput {
            schema_version: INPUT_SCHEMA_VERSION,
            rollouts: paths
                .into_iter()
                .map(|path| {
                    path.state
                        .finish(&self.input)
                        .map(RolloutComputation::into_output)
                })
                .collect::<Result<_, _>>()?,
        })
    }

    pub fn finish_product(&mut self) -> Result<ProductMetricSeries, SimulationError> {
        if !self.configured || self.product.is_none() {
            return Err(SimulationError::InvalidActorSessionState);
        }
        let Phase::Finished(paths) = std::mem::replace(&mut self.phase, Phase::Closed) else {
            return Err(SimulationError::InvalidActorSessionState);
        };
        let rollouts: Vec<_> = paths
            .into_iter()
            .map(|path| (path.state.product_metrics, path.state.failed_month))
            .collect();
        Ok(ProductMetricSeries::from_rollouts(
            self.input.scenario.horizon_months + 1,
            &rollouts,
        )?)
    }
}

fn action_actor(action: &Action) -> &str {
    match action {
        Action::Sell(request) => &request.agent_id,
        Action::Buy(request) => &request.agent_id,
        Action::Transfer(request) => &request.from.agent_id,
        Action::PayClaim(request) => &request.from.agent_id,
        Action::Consume(request) => &request.from.agent_id,
        Action::Contribute(request) | Action::Withdraw(request) => &request.agent_id,
        Action::Liquidate(request) => &request.agent_id,
    }
}
