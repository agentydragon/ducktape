//! Defunctionalized local continuation. The body is an explicit step stack;
//! a call yields after its callee and operands have run, and the driver
//! resumes the same stack with the adapter's result.

use super::bindings::{
    bind_parameter, prepare_body_bindings, store_binding, update_binding_expression,
};
use super::branch;
use super::cells;
use super::closure;
use super::construct;
use super::context::{Disposition, KernelContext, Unproved};
use super::field_access::{read_field, read_field_expression, store_field};
use super::frame::{
    EvaluatedCallee, Invocation, LocalCallRequest, LocalExecutionAttempt, LocalFrame,
    LocalTransferResult, LocalValueResult, RemainderDisposition, SelectedValue,
};
use super::value_context::LocalValueContext;
use analysis_model::PrimKinds;
use analysis_vocabulary::value::{Kind, Value};
use std::collections::BTreeMap;
use std::sync::Arc;
use swc_common::Span;
use swc_ecma_ast::{BinaryOp, Id};
use value_model::local_operations::{
    DeferredCallableCreation, LocalArrayElement, LocalBinding, LocalBody, LocalCallee,
    LocalExpression, LocalOperation, LogicalOperator,
};
use value_model::{AbstractValue, CallableSelection, PropertyDefinition, ValueExpr, Values};
use world::allocation_transfer::{
    self, AllocationJudgment, AllocationValue, ArrayTransfer, LiteralLayout, RecordTransfer,
    SequenceTransfer,
};
use world::effect_coverage::EffectCoverageFragment;

pub(crate) enum LocalSuspension {
    Call(LocalCallRequest),
    Complete(LocalExecutionAttempt),
    /// The body continues on every one of these paths from the same state.
    /// Each child holds the whole remaining body; the polled continuation is
    /// spent.
    Fork(Vec<LocalContinuation>),
}

/// Leaves one activation may fork into. Past it the kernel refuses the body
/// instead of branching: path count is otherwise exponential in the number
/// of undecided tests.
const BRANCH_BUDGET: usize = 8;

#[derive(Clone)]
enum Aggregate {
    Record {
        transfer: RecordTransfer,
        allocation: u32,
        /// A site already in the heap may denote an older allocation; a child
        /// can also reenter this site. Only a first, uninterrupted record is
        /// fresh.
        new_record: bool,
    },
    Array(Box<ArrayTransfer>),
}

#[derive(Clone)]
enum Step {
    Prepare,
    Parameter(usize),
    /// The default expression of parameter `binding` has run; `supplied` is
    /// the non-undefined part of the argument, present when `alternative`.
    BindDefault {
        binding: Id,
        supplied: SelectedValue,
        alternative: bool,
    },
    PrepareBody,
    Operation(usize),
    Expression(LocalExpression),
    Initialize(Id),
    Discard,
    Return,
    Throw,
    RecordNext {
        aggregate: usize,
        properties: Arc<[PropertyDefinition<LocalExpression>]>,
        index: usize,
    },
    RecordValue {
        aggregate: usize,
        index: usize,
    },
    ArrayNext {
        aggregate: usize,
        elements: Arc<[LocalArrayElement]>,
        index: usize,
    },
    ArrayValue(usize),
    /// The spread operand has run; expand its iterator receipt into the
    /// literal.
    ArraySpread {
        aggregate: usize,
        span: Span,
    },
    FinishAggregate(usize),
    Sequence {
        expressions: Arc<[LocalExpression]>,
        index: usize,
        transfer: SequenceTransfer,
        selection: Option<CallableSelection>,
    },
    Template {
        substitutions: Arc<[LocalExpression]>,
        index: usize,
        span: Span,
    },
    TemplateConvert(Span),
    BinaryLeft {
        op: BinaryOp,
        right: LocalExpression,
        span: Span,
    },
    BinaryRight {
        op: BinaryOp,
        left: SelectedValue,
        span: Span,
    },
    Assign(Id, Span),
    Arguments {
        callee: EvaluatedCallee,
        arguments: Vec<SelectedValue>,
        expressions: Arc<[LocalExpression]>,
        index: usize,
        span: Span,
        invocation: Invocation,
    },
    StoreField {
        receivers: Values,
        prefix_tracked: bool,
        property: String,
        span: Span,
    },
    DefineThisField(String),
    AssignThisField {
        property: String,
        span: Span,
    },
    /// The test has run; continue with the arm its truthiness selects.
    ConditionalSelect {
        consequent: LocalExpression,
        alternate: LocalExpression,
    },
    /// The left operand has run; keep it where it short-circuits and run
    /// `right` where it does not.
    LogicalSelect {
        op: LogicalOperator,
        right: LocalExpression,
    },
    /// The value a fork leaf starts from.
    Selected(SelectedValue),
}

impl Step {
    /// Steps that still run after the normal path ended: they settle an
    /// aggregate or operation that devel's semantics completes regardless.
    fn finishes(&self) -> bool {
        matches!(
            self,
            Self::Throw | Self::FinishAggregate(_) | Self::Sequence { .. }
        )
    }
}

#[derive(Clone)]
pub struct LocalContinuation {
    pub(crate) frame: LocalFrame,
    steps: Vec<Step>,
    /// The value of the most recently completed expression, consumed by the
    /// step that scheduled it.
    value: Option<SelectedValue>,
    refused: Option<RemainderDisposition>,
    aggregates: BTreeMap<usize, Aggregate>,
    next_aggregate: usize,
    /// Leaves this path may still fork into, itself included.
    branch_budget: usize,
}

impl LocalContinuation {
    pub fn new(frame: LocalFrame) -> Self {
        Self {
            frame,
            steps: vec![Step::Prepare],
            value: None,
            refused: None,
            aggregates: BTreeMap::new(),
            next_aggregate: 0,
            branch_budget: BRANCH_BUDGET,
        }
    }

    /// A scalar consumer has one result and no join: the body is refused at
    /// its first undecided test and nothing after it runs.
    pub(crate) fn refuse_branching(&mut self) {
        self.frame.ledger.allocation = AllocationJudgment::Opaque;
        self.refused = Some(RemainderDisposition::NeedsSummary);
    }

    /// Two paths from this state: the remaining steps after `left` and after
    /// `right`. The budget is split so the leaves of one activation stay
    /// bounded however the forks nest.
    fn fork(&self, left: Step, right: Step) -> Result<LocalSuspension, RemainderDisposition> {
        if self.branch_budget < 2 {
            return Err(RemainderDisposition::NeedsSummary);
        }
        let mut left_path = self.clone();
        let mut right_path = self.clone();
        left_path.branch_budget = self.branch_budget / 2;
        right_path.branch_budget = self.branch_budget - left_path.branch_budget;
        left_path.steps.push(left);
        right_path.steps.push(right);
        Ok(LocalSuspension::Fork(vec![left_path, right_path]))
    }

    pub(crate) fn resume_call(&mut self, result: Result<LocalValueResult, RemainderDisposition>) {
        match result {
            Ok(result) => self.value = Some(absorb(&mut self.frame, result)),
            Err(remainder) => self.refused = Some(remainder),
        }
    }

    pub(crate) fn poll(
        &mut self,
        context: &mut impl KernelContext,
        body: &LocalBody,
    ) -> LocalSuspension {
        if let Some(remainder) = self.refused {
            return LocalSuspension::Complete(LocalExecutionAttempt::Refused(remainder));
        }
        while let Some(step) = self.steps.pop() {
            // A return, throw or unavailable operand ends the normal path:
            // every later operation of this body is skipped.
            if !self.frame.continues() && !step.finishes() {
                self.value = None;
                continue;
            }
            match self.step(context, body, step) {
                Ok(None) => {}
                Ok(Some(suspension)) => return suspension,
                Err(remainder) => {
                    self.refused = Some(remainder);
                    return LocalSuspension::Complete(LocalExecutionAttempt::Refused(remainder));
                }
            }
        }
        LocalSuspension::Complete(LocalExecutionAttempt::Completed(finish(&self.frame)))
    }

    fn take_value(&mut self) -> SelectedValue {
        self.value
            .take()
            .unwrap_or_else(|| SelectedValue::raw(Values::new()))
    }

    /// The step's selected value. An empty selection ends the normal path:
    /// there is no value left for this step to consume, so the caller stops
    /// rather than continuing with an empty one.
    fn take_selected_or_stop(&mut self) -> Option<SelectedValue> {
        let selected = self.take_value();
        if selected.values.is_empty() {
            self.frame.stop_normal_path();
            return None;
        }
        Some(selected)
    }

    fn begin_aggregate(&mut self, aggregate: Aggregate) -> Result<usize, RemainderDisposition> {
        let id = self.next_aggregate;
        self.next_aggregate = id
            .checked_add(1)
            .ok_or(RemainderDisposition::NeedsSummary)?;
        self.aggregates.insert(id, aggregate);
        Ok(id)
    }

    fn step(
        &mut self,
        context: &mut impl KernelContext,
        body: &LocalBody,
        step: Step,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        match step {
            Step::Prepare => self.prepare_activation(context, body),
            Step::Parameter(index) => return self.bind_next_parameter(context, body, index),
            Step::BindDefault {
                binding,
                supplied,
                alternative,
            } => self.bind_default_parameter(context, body, binding, supplied, alternative),
            Step::PrepareBody => prepare_body_bindings(context, body, &mut self.frame),
            Step::Operation(index) => return self.schedule_operation(body, index),
            Step::Expression(node) => return self.expression(context, body, node),
            Step::Initialize(binding) => self.initialize_binding(context, body, binding),
            Step::Discard => {
                if self.take_value().values.is_empty() {
                    self.frame.stop_normal_path();
                }
            }
            Step::Return => {
                let selected = self.take_value();
                self.frame.complete_return(selected);
            }
            Step::Throw => {
                let selected = self.take_value();
                self.frame.complete_throw(selected.values);
            }
            Step::RecordNext {
                aggregate,
                properties,
                index,
            } => return self.schedule_record_property(aggregate, properties, index),
            Step::RecordValue { aggregate, index } => {
                return self.record_property_value(context, aggregate, index);
            }
            Step::ArrayNext {
                aggregate,
                elements,
                index,
            } => return self.schedule_array_element(aggregate, elements, index),
            Step::ArraySpread { aggregate, span } => {
                return self.spread_array_value(context, aggregate, span);
            }
            Step::ArrayValue(aggregate) => {
                let selected = self.take_value();
                self.frame.inputs.extend(selected.values.iter().cloned());
                let Some(Aggregate::Array(transfer)) = self.aggregates.get_mut(&aggregate) else {
                    return Err(RemainderDisposition::NeedsSummary);
                };
                transfer.child(AllocationValue {
                    values: selected.values,
                    judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
                });
            }
            Step::FinishAggregate(aggregate) => return self.finish_aggregate(context, aggregate),
            Step::Sequence {
                expressions,
                index,
                transfer,
                selection,
            } => self.continue_sequence(expressions, index, transfer, selection),
            Step::Template {
                substitutions,
                index,
                span,
            } => self.continue_template(substitutions, index, span),
            Step::TemplateConvert(span) => {
                return self.convert_template_substitution(context, span);
            }
            Step::BinaryLeft { op, right, span } => {
                let left = self.take_value();
                if left.values.is_empty() {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
                self.steps.push(Step::BinaryRight { op, left, span });
                self.steps.push(Step::Expression(right));
            }
            Step::BinaryRight { op, left, span } => {
                return self.apply_binary_operator(context, op, left, span);
            }
            Step::Assign(binding, span) => {
                let selected = self.take_value();
                self.value = Some(store_binding(
                    context,
                    body,
                    &mut self.frame,
                    &binding,
                    selected,
                    span,
                )?);
            }
            Step::Arguments {
                callee,
                arguments,
                expressions,
                index,
                span,
                invocation,
            } => {
                return Ok(self.schedule_call_argument(
                    callee,
                    arguments,
                    expressions,
                    index,
                    span,
                    invocation,
                ));
            }
            Step::StoreField {
                receivers,
                prefix_tracked,
                property,
                span,
            } => {
                return self.store_field_value(context, receivers, prefix_tracked, property, span);
            }
            Step::DefineThisField(property) => {
                let Some(selected) = self.take_selected_or_stop() else {
                    return Ok(None);
                };
                construct::define_field(context, &mut self.frame, &property, &selected.values)?;
                self.value = Some(selected);
            }
            Step::AssignThisField { property, span } => {
                let Some(selected) = self.take_selected_or_stop() else {
                    return Ok(None);
                };
                construct::assign_field(
                    context,
                    &mut self.frame,
                    &property,
                    &selected.values,
                    span,
                )?;
                self.value = Some(selected);
            }
            Step::ConditionalSelect {
                consequent,
                alternate,
            } => return self.select_conditional_arm(consequent, alternate),
            Step::LogicalSelect { op, right } => return self.select_logical_operand(op, right),
            Step::Selected(selected) => self.value = Some(selected),
        }
        Ok(None)
    }

    /// Schedules the body after its parameter prologue. With parameter
    /// inputs, every parameter starts in TDZ and binds in order.
    fn prepare_activation(&mut self, context: &mut impl KernelContext, body: &LocalBody) {
        self.steps.push(Step::Operation(0));
        self.steps.push(Step::PrepareBody);
        if self.frame.parameter_inputs.is_some() {
            // Later parameters are in TDZ while an earlier default
            // executes. Body declarations are instantiated only after
            // this environment.
            for parameter in body.parameters.iter() {
                self.frame.environment.insert(
                    parameter.binding.clone(),
                    Values::from([AbstractValue::Value(Value::Unavailable)]),
                );
                self.frame.binding_selections.remove(&parameter.binding);
            }
            cells::instantiate_parameter_cells(context, body, &self.frame);
            self.steps.push(Step::Parameter(0));
        }
    }

    /// Binds parameter `index` to its argument, first running its default
    /// where the argument may be undefined.
    fn bind_next_parameter(
        &mut self,
        context: &mut impl KernelContext,
        body: &LocalBody,
        index: usize,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let Some(parameter) = body.parameters.get(index) else {
            return Ok(None);
        };
        let Some(arguments) = &self.frame.parameter_inputs else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        let mut selected = arguments
            .get(index)
            .cloned()
            .unwrap_or_else(|| SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
        self.steps.push(Step::Parameter(index + 1));
        if let Some(default) = &parameter.default
            && selected.values.remove(&AbstractValue::UNDEFINED)
        {
            self.steps.push(Step::BindDefault {
                binding: parameter.binding.clone(),
                alternative: !selected.values.is_empty(),
                supplied: selected,
            });
            self.steps.push(Step::Expression(default.clone()));
            return Ok(None);
        }
        bind_parameter(context, body, &mut self.frame, &parameter.binding, selected);
        Ok(None)
    }

    /// Binds a defaulted parameter to its default's value, joined with the
    /// supplied non-undefined alternative.
    fn bind_default_parameter(
        &mut self,
        context: &mut impl KernelContext,
        body: &LocalBody,
        binding: Id,
        mut supplied: SelectedValue,
        alternative: bool,
    ) {
        let default = self.take_value();
        let selected = if alternative {
            // Abstract alternatives do not prove which prologue path
            // completed. Keep every reached effect and value.
            self.frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            supplied.values.extend(default.values);
            supplied.selection = None;
            supplied
        } else {
            default
        };
        bind_parameter(context, body, &mut self.frame, &binding, selected);
    }

    /// Schedules body operation `index`, or completes with `undefined` past
    /// the last one.
    fn schedule_operation(
        &mut self,
        body: &LocalBody,
        index: usize,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let Some(operation) = body.operations.get(index) else {
            self.frame
                .complete_return(SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
            return Ok(None);
        };
        self.steps.push(Step::Operation(index + 1));
        match operation {
            LocalOperation::Initialize(binding, Some(value)) => {
                self.steps.push(Step::Initialize(binding.clone()));
                self.steps.push(Step::Expression(value.clone()));
            }
            LocalOperation::Initialize(binding, None) => {
                let Some(kind) = body.bindings.get(binding) else {
                    return Err(RemainderDisposition::NeedsSummary);
                };
                if !matches!(
                    kind,
                    LocalBinding::Var | LocalBinding::Parameter | LocalBinding::HoistedFunction(_)
                ) {
                    self.value = Some(SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
                    self.steps.push(Step::Initialize(binding.clone()));
                }
            }
            LocalOperation::Evaluate(value) => {
                self.steps.push(Step::Discard);
                self.steps.push(Step::Expression(value.clone()));
            }
            LocalOperation::Return(value) => {
                self.steps.push(Step::Return);
                match value {
                    Some(value) => self.steps.push(Step::Expression(value.clone())),
                    None => {
                        self.value =
                            Some(SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
                    }
                }
            }
            LocalOperation::Throw(value) => {
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
                self.frame.ledger.effects.abrupt();
                self.steps.push(Step::Throw);
                self.steps.push(Step::Expression(value.clone()));
            }
        }
        Ok(None)
    }

    /// Stores the selected value into a declared binding and records the
    /// declarative operation's effects.
    fn initialize_binding(
        &mut self,
        context: &mut impl KernelContext,
        body: &LocalBody,
        binding: Id,
    ) {
        let Some(selected) = self.take_selected_or_stop() else {
            return;
        };
        let SelectedValue { values, selection } = selected;
        let selection = selection.filter(|selection| selection.matches(&values));
        self.frame.environment.insert(binding.clone(), values);
        match selection {
            Some(selection) => {
                self.frame
                    .binding_selections
                    .insert(binding.clone(), selection);
            }
            None => {
                self.frame.binding_selections.remove(&binding);
            }
        }
        cells::declared_binding_stored(context, body, &binding, &self.frame);
        self.frame.initialized_bindings.insert(binding.clone());
        let effects = cells::declarative_binding_operation(context, body, &binding);
        self.frame.ledger.effects.compose(&effects);
    }

    /// Schedules record property `index` while the literal can complete.
    fn schedule_record_property(
        &mut self,
        aggregate: usize,
        properties: Arc<[PropertyDefinition<LocalExpression>]>,
        index: usize,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let Some(property) = properties.get(index) else {
            return Ok(None);
        };
        let Some(Aggregate::Record { transfer, .. }) = self.aggregates.get(&aggregate) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        if !transfer.can_complete() {
            return Ok(None);
        }
        let value = property.value().clone();
        self.steps.push(Step::RecordNext {
            aggregate,
            properties,
            index: index + 1,
        });
        self.steps.push(Step::RecordValue { aggregate, index });
        self.steps.push(Step::Expression(value));
        Ok(None)
    }

    /// Adds record property `index`'s value to the literal's transfer.
    fn record_property_value(
        &mut self,
        context: &mut impl KernelContext,
        aggregate: usize,
        index: usize,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let selected = self.take_value();
        // A later child can truncate the unfinished literal.
        self.frame.inputs.extend(selected.values.iter().cloned());
        let Some(Aggregate::Record {
            transfer,
            allocation,
            new_record,
        }) = self.aggregates.get_mut(&aggregate)
        else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        // A child can reenter this allocation site. Its resulting
        // heap entry then also represents an older record.
        *new_record &= context
            .world()
            .surface
            .own_properties(*allocation)
            .is_none();
        // The child's judgment was composed into the ledger as it ran.
        transfer.child(
            index,
            AllocationValue {
                values: selected.values,
                judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
            },
        );
        Ok(None)
    }

    /// Schedules array element `index` (a hole, value or spread) while the
    /// literal can complete.
    fn schedule_array_element(
        &mut self,
        aggregate: usize,
        elements: Arc<[LocalArrayElement]>,
        index: usize,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let Some(Aggregate::Array(transfer)) = self.aggregates.get_mut(&aggregate) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        if !transfer.can_complete() {
            return Ok(None);
        }
        match elements.get(index) {
            None => {}
            Some(LocalArrayElement::Hole) => {
                transfer.hole();
                self.steps.push(Step::ArrayNext {
                    aggregate,
                    elements,
                    index: index + 1,
                });
            }
            Some(LocalArrayElement::Value(element)) => {
                let element = element.clone();
                self.steps.push(Step::ArrayNext {
                    aggregate,
                    elements,
                    index: index + 1,
                });
                self.steps.push(Step::ArrayValue(aggregate));
                self.steps.push(Step::Expression(element));
            }
            Some(LocalArrayElement::Spread { value, span }) => {
                let (value, span) = (value.clone(), *span);
                self.steps.push(Step::ArrayNext {
                    aggregate,
                    elements,
                    index: index + 1,
                });
                self.steps.push(Step::ArraySpread { aggregate, span });
                self.steps.push(Step::Expression(value));
            }
        }
        Ok(None)
    }

    /// Expands the spread operand's iterator receipt into the array literal.
    fn spread_array_value(
        &mut self,
        context: &mut impl KernelContext,
        aggregate: usize,
        span: Span,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let selected = self.take_value();
        self.frame.inputs.extend(selected.values.iter().cloned());
        let Some(Aggregate::Array(transfer)) = self.aggregates.get_mut(&aggregate) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        let mut bridge = LocalValueContext {
            context,
            frame: &mut self.frame,
            refusal: None,
        };
        transfer.spread(
            &mut bridge,
            span,
            AllocationValue {
                values: selected.values,
                judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
            },
        );
        if let Some(remainder) = bridge.refusal {
            self.frame
                .ledger
                .allocation
                .compose(&AllocationJudgment::Opaque);
            return Err(remainder);
        }
        Ok(None)
    }

    /// Completes a record or array literal into its allocation, unless the
    /// normal path ended inside it.
    fn finish_aggregate(
        &mut self,
        context: &mut impl KernelContext,
        aggregate: usize,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let Some(aggregate) = self.aggregates.remove(&aggregate) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        if !self.frame.continues() {
            // An unfinished literal is not an allocation; no heap entry
            // is written for it.
            self.frame
                .ledger
                .allocation
                .compose(&AllocationJudgment::Opaque);
            self.value = None;
            return Ok(None);
        }
        let mut bridge = LocalValueContext {
            context,
            frame: &mut self.frame,
            refusal: None,
        };
        let (result, record) = match aggregate {
            Aggregate::Record {
                transfer,
                allocation,
                new_record,
            } => (transfer.finish(&mut bridge), Some((allocation, new_record))),
            Aggregate::Array(transfer) => (transfer.finish(&mut bridge), None),
        };
        self.frame.ledger.allocation.compose(&result.judgment);
        if let Some((allocation, new_record)) = record {
            mark_fresh_record(
                context,
                &mut self.frame,
                allocation,
                new_record,
                &result.values,
            );
        }
        self.value = Some(completing_selected(
            SelectedValue::raw(result.values),
            &mut self.frame,
        ));
        Ok(None)
    }

    /// Adds the previous sequence expression's value, then schedules the next
    /// one or completes with the last value.
    fn continue_sequence(
        &mut self,
        expressions: Arc<[LocalExpression]>,
        index: usize,
        mut transfer: SequenceTransfer,
        mut selection: Option<CallableSelection>,
    ) {
        if index > 0 {
            if !self.frame.continues() {
                self.frame
                    .ledger
                    .allocation
                    .compose(&AllocationJudgment::Opaque);
                self.value = None;
                return;
            }
            let selected = self.take_value();
            selection = selected.selection;
            if !transfer.child(AllocationValue {
                values: selected.values,
                judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
            }) {
                self.frame.stop_normal_path();
                self.frame
                    .ledger
                    .allocation
                    .compose(&AllocationJudgment::Opaque);
                return;
            }
        }
        match expressions.get(index) {
            Some(next) => {
                let next = next.clone();
                self.steps.push(Step::Sequence {
                    expressions,
                    index: index + 1,
                    transfer,
                    selection,
                });
                self.steps.push(Step::Expression(next));
            }
            None => {
                let result = transfer.finish();
                self.frame.ledger.allocation.compose(&result.judgment);
                self.value = Some(completing_selected(
                    SelectedValue {
                        values: result.values,
                        selection,
                    },
                    &mut self.frame,
                ));
            }
        }
    }

    /// Schedules template substitution `index` and its string conversion, or
    /// completes with a string past the last one.
    fn continue_template(
        &mut self,
        substitutions: Arc<[LocalExpression]>,
        index: usize,
        span: Span,
    ) {
        match substitutions.get(index) {
            Some(next) => {
                let next = next.clone();
                self.steps.push(Step::Template {
                    substitutions,
                    index: index + 1,
                    span,
                });
                self.steps.push(Step::TemplateConvert(span));
                self.steps.push(Step::Expression(next));
            }
            None => {
                self.value = Some(completing_selected(
                    SelectedValue::raw(Values::from([AbstractValue::Value(Value::Prim(
                        PrimKinds::STRING,
                    ))])),
                    &mut self.frame,
                ));
            }
        }
    }

    /// Converts a template substitution's value to a string.
    fn convert_template_substitution(
        &mut self,
        context: &mut impl KernelContext,
        span: Span,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let operand = self.take_value();
        if operand.values.is_empty() {
            self.frame.stop_normal_path();
            return Ok(None);
        }
        // String concatenation has the same primitive ToString result and
        // abrupt alternatives. Object conversion needs a separate string-hint
        // proof, so it retains the existing conservative fallback.
        let string = Values::from([AbstractValue::Value(Value::Prim(PrimKinds::STRING))]);
        let transfer = allocation_transfer::binary(
            BinaryOp::Add,
            &string,
            &operand.values,
            AllocationJudgment::CoveredAllocationOnly(Box::default()),
        );
        let Some(transfer) = transfer else {
            self.frame
                .ledger
                .allocation
                .compose(&AllocationJudgment::Opaque);
            return Err(unproved_operator(
                context,
                &mut self.frame,
                &operand.values,
                span,
            ));
        };
        absorb_primitive_transfer(&mut self.frame, &transfer);
        completing(transfer.values, &mut self.frame);
        self.value = None;
        Ok(None)
    }

    /// Applies a binary operator to its evaluated left and right operands.
    fn apply_binary_operator(
        &mut self,
        context: &mut impl KernelContext,
        op: BinaryOp,
        left: SelectedValue,
        span: Span,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let right = self.take_value();
        if right.values.is_empty() {
            self.frame.stop_normal_path();
            return Ok(None);
        }
        let transfer = allocation_transfer::binary(
            op,
            &left.values,
            &right.values,
            AllocationJudgment::CoveredAllocationOnly(Box::default()),
        );
        let Some(transfer) = transfer else {
            self.frame
                .ledger
                .allocation
                .compose(&AllocationJudgment::Opaque);
            let mut reached = left.values;
            reached.extend(right.values);
            return Err(unproved_operator(context, &mut self.frame, &reached, span));
        };
        absorb_primitive_transfer(&mut self.frame, &transfer);
        self.value = Some(completing_selected(
            SelectedValue::raw(transfer.values),
            &mut self.frame,
        ));
        Ok(None)
    }

    /// Adds an evaluated call argument, then schedules the next one or yields
    /// the call once every argument has run.
    fn schedule_call_argument(
        &mut self,
        callee: EvaluatedCallee,
        mut arguments: Vec<SelectedValue>,
        expressions: Arc<[LocalExpression]>,
        index: usize,
        span: Span,
        invocation: Invocation,
    ) -> Option<LocalSuspension> {
        let selected = self.take_selected_or_stop()?;
        self.frame.inputs.extend(selected.values.iter().cloned());
        arguments.push(selected);
        let next = index + 1;
        match expressions.get(next) {
            Some(argument) => {
                let argument = argument.clone();
                self.steps.push(Step::Arguments {
                    callee,
                    arguments,
                    expressions,
                    index: next,
                    span,
                    invocation,
                });
                self.steps.push(Step::Expression(argument));
            }
            None => {
                return Some(LocalSuspension::Call(LocalCallRequest {
                    callee,
                    arguments,
                    span,
                    invocation,
                }));
            }
        }
        None
    }

    /// Stores the selected value into `property` of the evaluated receivers.
    fn store_field_value(
        &mut self,
        context: &mut impl KernelContext,
        receivers: Values,
        prefix_tracked: bool,
        property: String,
        span: Span,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let Some(selected) = self.take_selected_or_stop() else {
            return Ok(None);
        };
        store_field(
            context,
            &mut self.frame,
            &receivers,
            prefix_tracked,
            &property,
            &selected.values,
            span,
        )?;
        self.value = Some(selected);
        Ok(None)
    }

    /// Continues with the conditional arm the test's truthiness selects, or
    /// forks when either may run.
    fn select_conditional_arm(
        &mut self,
        consequent: LocalExpression,
        alternate: LocalExpression,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let test = self.take_value();
        if test.values.is_empty() {
            self.frame.stop_normal_path();
            return Ok(None);
        }
        // A truncated arm can still reach the tested value.
        self.frame.inputs.extend(test.values.iter().cloned());
        branch::test_selected(&mut self.frame);
        let (truthy, falsy) = branch::split_truthiness(&test.values);
        match (truthy.is_empty(), falsy.is_empty()) {
            (false, true) => self.steps.push(Step::Expression(consequent)),
            (true, false) => self.steps.push(Step::Expression(alternate)),
            _ => {
                return self
                    .fork(Step::Expression(consequent), Step::Expression(alternate))
                    .map(Some);
            }
        }
        Ok(None)
    }

    /// Keeps the left operand where it short-circuits and runs `right` where
    /// it does not, forking when both may happen.
    fn select_logical_operand(
        &mut self,
        op: LogicalOperator,
        right: LocalExpression,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        let left = self.take_value();
        if left.values.is_empty() {
            self.frame.stop_normal_path();
            return Ok(None);
        }
        self.frame.inputs.extend(left.values.iter().cloned());
        branch::test_selected(&mut self.frame);
        let (retained, runs_right) = match op {
            LogicalOperator::And => {
                let (truthy, falsy) = branch::split_truthiness(&left.values);
                (falsy, !truthy.is_empty())
            }
            LogicalOperator::Or => {
                let (truthy, falsy) = branch::split_truthiness(&left.values);
                (truthy, !falsy.is_empty())
            }
            LogicalOperator::NullishCoalescing => branch::split_nullish(&left.values),
        };
        if retained.is_empty() {
            self.steps.push(Step::Expression(right));
            return Ok(None);
        }
        let selection = left
            .selection
            .filter(|selection| selection.matches(&retained));
        let retained = SelectedValue {
            values: retained,
            selection,
        };
        if runs_right {
            return self
                .fork(Step::Selected(retained), Step::Expression(right))
                .map(Some);
        }
        self.value = Some(retained);
        Ok(None)
    }

    fn expression(
        &mut self,
        context: &mut impl KernelContext,
        body: &LocalBody,
        node: LocalExpression,
    ) -> Result<Option<LocalSuspension>, RemainderDisposition> {
        match node {
            LocalExpression::Value(node) => {
                self.value = Some(value_expression(context, &node, &mut self.frame)?);
            }
            LocalExpression::PrimitiveLiteral(kind) => {
                // The canonical source literal cannot invoke code, access exterior
                // state or throw. Its value abstraction is independent evidence.
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::no_effects());
                self.value = Some(completing_selected(
                    SelectedValue::raw(Values::from([AbstractValue::Value(Value::Prim(kind))])),
                    &mut self.frame,
                ));
            }
            LocalExpression::This => {
                // A base constructor's `this` is bound at entry: resolving it
                // runs no code and cannot be in its TDZ.
                let Some(this_values) = self.frame.this_values.clone() else {
                    return Err(RemainderDisposition::NeedsSummary);
                };
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::no_effects());
                self.value = Some(SelectedValue::raw(this_values));
            }
            LocalExpression::Record {
                allocation,
                properties,
            } => {
                if !matches!(
                    context.index().allocation_layout(allocation),
                    LiteralLayout::Record
                ) {
                    return Err(RemainderDisposition::NeedsSummary);
                }
                let new_record = context.world().surface.own_properties(allocation).is_none();
                let transfer = RecordTransfer::begin(
                    &mut LocalValueContext {
                        context,
                        frame: &mut self.frame,
                        refusal: None,
                    },
                    allocation,
                    &properties,
                    AllocationJudgment::CoveredAllocationOnly(Box::default()),
                )
                .ok_or(RemainderDisposition::NeedsSummary)?;
                let aggregate = self.begin_aggregate(Aggregate::Record {
                    transfer,
                    allocation,
                    new_record,
                })?;
                self.steps.push(Step::FinishAggregate(aggregate));
                self.steps.push(Step::RecordNext {
                    aggregate,
                    properties,
                    index: 0,
                });
            }
            LocalExpression::Array {
                allocation,
                elements,
            } => {
                let has_spread = elements
                    .iter()
                    .any(|element| matches!(element, LocalArrayElement::Spread { .. }));
                let transfer = ArrayTransfer::begin(
                    &mut LocalValueContext {
                        context,
                        frame: &mut self.frame,
                        refusal: None,
                    },
                    allocation,
                    has_spread,
                    AllocationJudgment::CoveredAllocationOnly(Box::default()),
                );
                let aggregate = self.begin_aggregate(Aggregate::Array(Box::new(transfer)))?;
                self.steps.push(Step::FinishAggregate(aggregate));
                self.steps.push(Step::ArrayNext {
                    aggregate,
                    elements,
                    index: 0,
                });
            }
            LocalExpression::Sequence(expressions) => {
                self.steps.push(Step::Sequence {
                    expressions,
                    index: 0,
                    transfer: SequenceTransfer::new(AllocationJudgment::CoveredAllocationOnly(
                        Box::default(),
                    )),
                    selection: None,
                });
            }
            LocalExpression::Template {
                substitutions,
                span,
            } => {
                self.steps.push(Step::Template {
                    substitutions,
                    index: 0,
                    span,
                });
            }
            LocalExpression::Binary {
                op,
                left,
                right,
                span,
            } => {
                self.steps.push(Step::BinaryLeft {
                    op,
                    right: right.as_ref().clone(),
                    span,
                });
                self.steps.push(Step::Expression(left.as_ref().clone()));
            }
            LocalExpression::Closure(callable) => {
                self.value = Some(closure_expression(context, &mut self.frame, callable)?);
            }
            LocalExpression::Assign {
                binding,
                value,
                span,
            } => {
                // Operand effects compose while the RHS runs. The checked
                // PutValue in `store_binding` supplies the store's fragment;
                // preemptively opening it would discard that evidence.
                self.steps.push(Step::Assign(binding, span));
                self.steps.push(Step::Expression(value.as_ref().clone()));
            }
            LocalExpression::UpdateBinding { binding, span } => {
                self.value = Some(update_binding_expression(
                    context,
                    body,
                    &mut self.frame,
                    &binding,
                    span,
                )?);
            }
            LocalExpression::Call {
                callee,
                arguments,
                span,
            } => {
                // GetValue(callee) precedes every argument. In particular, an
                // argument assignment must not replace this captured callee.
                let callee = evaluated_callee(context, &mut self.frame, &callee, span)?;
                if callee.targets().is_empty()
                    && !matches!(
                        &callee,
                        EvaluatedCallee::Member {
                            array_method: Some(_),
                            ..
                        }
                    )
                {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
                self.frame.inputs.extend(callee.targets().iter().cloned());
                return Ok(self
                    .invoke(callee, arguments, span, Invocation::Call)
                    .map(LocalSuspension::Call));
            }
            LocalExpression::Construct {
                callee,
                arguments,
                span,
            } => {
                // EvaluateNew: GetValue(constructor) precedes every argument.
                let result = context.read_binding(&callee, &mut self.frame)?;
                let callee = absorb(&mut self.frame, result);
                if callee.values.is_empty() {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
                self.frame.inputs.extend(callee.values.iter().cloned());
                return Ok(self
                    .invoke(
                        EvaluatedCallee::Direct(callee),
                        arguments,
                        span,
                        Invocation::Construct,
                    )
                    .map(LocalSuspension::Call));
            }
            LocalExpression::ReadField {
                receiver,
                property,
                span,
            } => {
                self.value = Some(read_field_expression(
                    context,
                    &mut self.frame,
                    &receiver,
                    &property,
                    span,
                )?);
            }
            LocalExpression::AssignField {
                receiver,
                property,
                value,
                span,
            } => {
                // Capture the LHS reference before the RHS can replace its binding.
                let receivers = value_of(
                    context,
                    &mut self.frame,
                    &ValueExpr::Binding(receiver.clone()),
                )?;
                self.frame.ledger.allocation.compose(&receivers.judgment);
                let receivers = completing(receivers.values, &mut self.frame);
                if receivers.is_empty() {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
                // Capture the applicable prefix-tracked lane alongside the LHS
                // reference. The RHS may rebind the source cell, but JavaScript
                // has already evaluated the original receiver value. Only a
                // module binding's value stores through the prefix: an element
                // record a frame local holds is tracked for reads alone.
                let prefix_tracked = !self.frame.environment.contains_key(&receiver)
                    && context.prefix_tracked_receiver(&receiver, &receivers);
                self.steps.push(Step::StoreField {
                    receivers,
                    prefix_tracked,
                    property,
                    span,
                });
                self.steps.push(Step::Expression(value.as_ref().clone()));
            }
            LocalExpression::Conditional {
                test,
                consequent,
                alternate,
            } => {
                self.steps.push(Step::ConditionalSelect {
                    consequent: consequent.as_ref().clone(),
                    alternate: alternate.as_ref().clone(),
                });
                self.steps.push(Step::Expression(test.as_ref().clone()));
            }
            LocalExpression::Logical { op, left, right } => {
                self.steps.push(Step::LogicalSelect {
                    op,
                    right: right.as_ref().clone(),
                });
                self.steps.push(Step::Expression(left.as_ref().clone()));
            }
            LocalExpression::DefineThisField { property, value } => {
                self.steps.push(Step::DefineThisField(property));
                self.steps.push(Step::Expression(value.as_ref().clone()));
            }
            LocalExpression::AssignThisField {
                property,
                value,
                span,
            } => {
                // The reference is `this`, already bound; only the value runs.
                self.steps.push(Step::AssignThisField { property, span });
                self.steps.push(Step::Expression(value.as_ref().clone()));
            }
        }
        Ok(None)
    }

    /// Evaluate the arguments of an already evaluated callee in order, then
    /// suspend at the invocation.
    fn invoke(
        &mut self,
        callee: EvaluatedCallee,
        arguments: Arc<[LocalExpression]>,
        span: Span,
        invocation: Invocation,
    ) -> Option<LocalCallRequest> {
        match arguments.first() {
            None => Some(LocalCallRequest {
                callee,
                arguments: Vec::new(),
                span,
                invocation,
            }),
            Some(first) => {
                let first = first.clone();
                self.steps.push(Step::Arguments {
                    callee,
                    arguments: Vec::with_capacity(arguments.len()),
                    expressions: arguments,
                    index: 0,
                    span,
                    invocation,
                });
                self.steps.push(Step::Expression(first));
                None
            }
        }
    }
}

/// Transfer one closed value expression in this frame. Scalar consumers
/// (the strict provider's direct allocation return) enter the kernel here.
pub fn value_of(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    expression: &ValueExpr,
) -> Result<AllocationValue, RemainderDisposition> {
    // A site already in the heap may denote an older allocation. The current
    // domain cannot distinguish that generation from this invocation's record.
    let new_record = match expression {
        ValueExpr::ObjectLiteral(object, _) => {
            context.world().surface.own_properties(*object).is_none()
                && !frame.borrowed_records.contains(object)
        }
        _ => false,
    };
    let mut bridge = LocalValueContext {
        context,
        frame,
        refusal: None,
    };
    let result = allocation_transfer::transfer(&mut bridge, expression);
    let refusal = bridge.refusal;
    let Some(result) = result else {
        return Err(refusal.unwrap_or(RemainderDisposition::NeedsSummary));
    };
    if let ValueExpr::ObjectLiteral(object, _) = expression {
        mark_fresh_record(context, frame, *object, new_record, &result.values);
    }
    Ok(result)
}

fn mark_fresh_record(
    context: &impl KernelContext,
    frame: &mut LocalFrame,
    allocation: u32,
    new_record: bool,
    values: &Values,
) {
    if new_record
        && !frame.borrowed_records.contains(&allocation)
        && context.ordinary_record_shape(allocation).is_some()
        && values.contains(&AbstractValue::Value(Value::Alloc(
            Kind::Object,
            allocation,
        )))
    {
        frame.fresh_records.insert(allocation);
    }
}

/// `Unavailable` in an operand is a possible abrupt completion of that
/// operand; the normal path continues with the other values. An operand
/// left with no value stops the operation that consumes it: an empty target
/// set is legitimate for a selected native method.
pub(super) fn completing(mut values: Values, frame: &mut LocalFrame) -> Values {
    if values.remove(&AbstractValue::Value(Value::Unavailable)) {
        frame.record_unknown_abrupt();
    }
    values
}

pub(super) fn completing_selected(
    mut selected: SelectedValue,
    frame: &mut LocalFrame,
) -> SelectedValue {
    selected.values = completing(selected.values, frame);
    if selected
        .selection
        .as_ref()
        .is_some_and(|selection| !selection.matches(&selected.values))
    {
        selected.selection = None;
    }
    selected
}

/// Compose an adapter value into the frame and project the kernel value.
pub(super) fn absorb(frame: &mut LocalFrame, result: LocalValueResult) -> SelectedValue {
    frame.ledger.effects.compose(&result.effects);
    frame.ledger.allocation.compose(&result.allocation.judgment);
    let selection = result
        .selection
        .filter(|selection| selection.matches(&result.allocation.values));
    completing_selected(
        SelectedValue {
            values: result.allocation.values,
            selection,
        },
        frame,
    )
}

/// A primitive operation that may throw after both operands ran leaves the
/// effect and completion domains unknown; allocation opacity alone does not.
pub(super) fn absorb_primitive_transfer(frame: &mut LocalFrame, transfer: &AllocationValue) {
    frame.ledger.allocation.compose(&transfer.judgment);
    let effects = if transfer
        .values
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        EffectCoverageFragment::default()
    } else {
        EffectCoverageFragment::no_effects()
    };
    frame.ledger.effects.compose(&effects);
}

pub(super) fn unproved_operator(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    operands: &Values,
    span: Span,
) -> RemainderDisposition {
    match context.unproved(frame, Unproved::Operator { operands, span }) {
        Disposition::Summarized => RemainderDisposition::AlreadySummarized,
        // A value is not an answer for an operator; stay conservative.
        Disposition::Resolved(_) | Disposition::Refused => RemainderDisposition::NeedsSummary,
    }
}

fn value_expression(
    context: &mut impl KernelContext,
    node: &ValueExpr,
    frame: &mut LocalFrame,
) -> Result<SelectedValue, RemainderDisposition> {
    if let ValueExpr::Binding(binding) = node {
        let result = context.read_binding(binding, frame)?;
        return Ok(absorb(frame, result));
    }
    let result = value_of(context, frame, node)?;
    frame.ledger.allocation.compose(&result.judgment);
    Ok(completing_selected(
        SelectedValue::raw(result.values),
        frame,
    ))
}

fn closure_expression(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    callable: DeferredCallableCreation,
) -> Result<SelectedValue, RemainderDisposition> {
    let result = closure::create(context, frame, callable)?;
    Ok(absorb(frame, result))
}

fn evaluated_callee(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    callee: &LocalCallee,
    span: Span,
) -> Result<EvaluatedCallee, RemainderDisposition> {
    match callee {
        LocalCallee::Creation(callable) => Ok(EvaluatedCallee::Direct(closure_expression(
            context, frame, *callable,
        )?)),
        LocalCallee::Binding(binding) => {
            let result = context.read_call_callee(binding, span, frame)?;
            Ok(EvaluatedCallee::Direct(absorb(frame, result)))
        }
        LocalCallee::Member {
            receiver,
            property,
            span: member_span,
        } => {
            if let Some(result) = context.native_callee(receiver, property, span) {
                frame.ledger.effects.compose(&result.effects);
                return Ok(EvaluatedCallee::Member {
                    receiver: Values::new(),
                    targets: completing(result.allocation.values, frame),
                    selection_dependencies: None,
                    array_method: None,
                });
            }
            let result = value_of(context, frame, &ValueExpr::Binding(receiver.clone()))?;
            frame.ledger.allocation.compose(&result.judgment);
            let receivers = completing(result.values, frame);
            if receivers.is_empty() {
                return Ok(EvaluatedCallee::Member {
                    receiver: receivers,
                    targets: Values::new(),
                    selection_dependencies: None,
                    array_method: None,
                });
            }
            frame.inputs.extend(receivers.iter().cloned());
            let prefix_tracked = context.prefix_tracked_receiver(receiver, &receivers);
            let mut array_method = None;
            let result = if let Some((selection, effects)) =
                context.array_method_callee(&receivers, property)
            {
                array_method = Some(Box::new(selection));
                LocalValueResult {
                    allocation: AllocationValue::opaque(Values::new()),
                    effects,
                    selection: None,
                }
            } else {
                read_field(context, frame, receiver, &receivers, property, *member_span)?
            };
            let selection_dependencies = prefix_tracked
                .then(|| result.effects.lexical_selection_dependencies())
                .flatten();
            // Selection/GetValue is an executed lexical operation and
            // is composed now, before any argument can throw or mutate
            // the selected property.
            frame.ledger.effects.compose(&result.effects);
            frame.ledger.allocation.compose(&result.allocation.judgment);
            Ok(EvaluatedCallee::Member {
                receiver: receivers,
                targets: completing(result.allocation.values, frame),
                selection_dependencies,
                array_method,
            })
        }
    }
}

fn finish(frame: &LocalFrame) -> LocalTransferResult {
    let mut ledger = frame.ledger.clone();
    if ledger.completion.may_be_abrupt() {
        if ledger.completion.has_no_normal_path() {
            ledger.effects.abrupt();
        } else {
            ledger.effects.uncertain_completion();
        }
    }
    LocalTransferResult { ledger }
}
