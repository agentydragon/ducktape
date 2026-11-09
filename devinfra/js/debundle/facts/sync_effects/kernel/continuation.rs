//! Defunctionalized local continuation. The body is an explicit step stack;
//! a call yields after its callee and operands have run, and the driver
//! resumes the same stack with the adapter's result.

use super::super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, ArrayTransfer, LiteralHeap,
    LiteralLayout, RecordTransfer, SequenceTransfer,
};
use super::super::effect_coverage::EffectCoverageFragment;
use super::super::local_operations::{
    DeferredCallableCreation, LocalArrayElement, LocalBinding, LocalBody, LocalCallee,
    LocalExpression, LocalOperation,
};
use super::super::own_data_transfer::{self, OwnDataOwnership, OwnDataSlot};
use super::super::runtime_heap::{NativeIterableReceipt, NativeIterableUse};
use super::super::value_model::{
    AbstractValue, CallSite, HeapAddress, LiteralPropertyKind, PropertyDefinition, ValueExpr,
    Values,
};
use super::cells;
use super::closure;
use super::construct;
use super::context::{Disposition, KernelContext, Unproved};
use super::frame::{
    CallableSelection, EvaluatedCallee, Invocation, LocalCallRequest, LocalExecutionAttempt,
    LocalFrame, LocalTransferResult, LocalValueResult, RemainderDisposition, SelectedValue,
};
use crate::execution_evidence::OrderedFieldReadRefusal;
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;
use std::collections::BTreeMap;
use std::sync::Arc;
use swc_common::Span;
use swc_ecma_ast::{BinaryOp, Id};

pub(in crate::facts::sync_effects) enum LocalSuspension {
    Call(LocalCallRequest),
    Complete(LocalExecutionAttempt),
}

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
    FinishRecord(usize),
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
    FinishArray(usize),
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
        module_prefix: bool,
        property: String,
        span: Span,
    },
    DefineThisField(String),
    AssignThisField {
        property: String,
        span: Span,
    },
}

impl Step {
    /// Steps that still run after the normal path ended: they settle an
    /// aggregate or operation that devel's semantics completes regardless.
    fn finishes(&self) -> bool {
        matches!(
            self,
            Self::Throw | Self::FinishRecord(_) | Self::FinishArray(_) | Self::Sequence { .. }
        )
    }
}

pub(in crate::facts::sync_effects) struct LocalContinuation {
    pub(in crate::facts::sync_effects) frame: LocalFrame,
    steps: Vec<Step>,
    /// The value of the most recently completed expression, consumed by the
    /// step that scheduled it.
    value: Option<SelectedValue>,
    refused: Option<RemainderDisposition>,
    aggregates: BTreeMap<usize, Aggregate>,
    next_aggregate: usize,
}

impl LocalContinuation {
    pub(in crate::facts::sync_effects) fn new(frame: LocalFrame) -> Self {
        Self {
            frame,
            steps: vec![Step::Prepare],
            value: None,
            refused: None,
            aggregates: BTreeMap::new(),
            next_aggregate: 0,
        }
    }

    pub(in crate::facts::sync_effects) fn resume_call(
        &mut self,
        result: Result<LocalValueResult, RemainderDisposition>,
    ) {
        match result {
            Ok(result) => self.value = Some(absorb(&mut self.frame, result)),
            Err(remainder) => self.refused = Some(remainder),
        }
    }

    pub(in crate::facts::sync_effects) fn poll(
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
                Ok(Some(request)) => return LocalSuspension::Call(request),
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
    ) -> Result<Option<LocalCallRequest>, RemainderDisposition> {
        match step {
            Step::Prepare => {
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
            Step::Parameter(index) => {
                let Some(parameter) = body.parameters.get(index) else {
                    return Ok(None);
                };
                let Some(arguments) = &self.frame.parameter_inputs else {
                    return Err(RemainderDisposition::NeedsSummary);
                };
                let mut selected = arguments.get(index).cloned().unwrap_or_else(|| {
                    SelectedValue::raw(Values::from([AbstractValue::UNDEFINED]))
                });
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
            }
            Step::BindDefault {
                binding,
                mut supplied,
                alternative,
            } => {
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
            Step::PrepareBody => prepare_body_bindings(context, body, &mut self.frame),
            Step::Operation(index) => {
                let Some(operation) = body.operations.get(index) else {
                    self.frame.complete_return(SelectedValue::raw(Values::from([
                        AbstractValue::UNDEFINED,
                    ])));
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
                            LocalBinding::Var
                                | LocalBinding::Parameter
                                | LocalBinding::HoistedFunction(_)
                        ) {
                            self.value =
                                Some(SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
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
                                self.value = Some(SelectedValue::raw(Values::from([
                                    AbstractValue::UNDEFINED,
                                ])));
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
            }
            Step::Expression(node) => return self.expression(context, body, node),
            Step::Initialize(binding) => {
                let selected = self.take_value();
                if selected.values.is_empty() {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
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
            } => {
                let Some(property) = properties.get(index) else {
                    return Ok(None);
                };
                let Some(Aggregate::Record { transfer, .. }) = self.aggregates.get(&aggregate)
                else {
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
            }
            Step::RecordValue { aggregate, index } => {
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
            }
            Step::FinishRecord(aggregate) => {
                let Some(Aggregate::Record {
                    transfer,
                    allocation,
                    new_record,
                }) = self.aggregates.remove(&aggregate)
                else {
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
                let result = transfer.finish(&mut LocalValueContext {
                    context,
                    frame: &mut self.frame,
                    refusal: None,
                });
                self.frame.ledger.allocation.compose(&result.judgment);
                mark_fresh_record(
                    context,
                    &mut self.frame,
                    allocation,
                    new_record,
                    &result.values,
                );
                self.value = Some(completing_selected(
                    SelectedValue::raw(result.values),
                    &mut self.frame,
                ));
            }
            Step::ArrayNext {
                aggregate,
                elements,
                index,
            } => {
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
            }
            Step::ArraySpread { aggregate, span } => {
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
            Step::FinishArray(aggregate) => {
                let Some(Aggregate::Array(transfer)) = self.aggregates.remove(&aggregate) else {
                    return Err(RemainderDisposition::NeedsSummary);
                };
                if !self.frame.continues() {
                    self.frame
                        .ledger
                        .allocation
                        .compose(&AllocationJudgment::Opaque);
                    self.value = None;
                    return Ok(None);
                }
                let result = transfer.finish(&mut LocalValueContext {
                    context,
                    frame: &mut self.frame,
                    refusal: None,
                });
                self.frame.ledger.allocation.compose(&result.judgment);
                self.value = Some(completing_selected(
                    SelectedValue::raw(result.values),
                    &mut self.frame,
                ));
            }
            Step::Sequence {
                expressions,
                index,
                mut transfer,
                mut selection,
            } => {
                if index > 0 {
                    if !self.frame.continues() {
                        self.frame
                            .ledger
                            .allocation
                            .compose(&AllocationJudgment::Opaque);
                        self.value = None;
                        return Ok(None);
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
                        return Ok(None);
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
            Step::Template {
                substitutions,
                index,
                span,
            } => match substitutions.get(index) {
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
            },
            Step::TemplateConvert(span) => {
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
                mut arguments,
                expressions,
                index,
                span,
                invocation,
            } => {
                let selected = self.take_value();
                if selected.values.is_empty() {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
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
                        return Ok(Some(LocalCallRequest {
                            callee,
                            arguments,
                            span,
                            invocation,
                        }));
                    }
                }
            }
            Step::StoreField {
                receivers,
                module_prefix,
                property,
                span,
            } => {
                let selected = self.take_value();
                if selected.values.is_empty() {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
                store_field(
                    context,
                    &mut self.frame,
                    &receivers,
                    module_prefix,
                    &property,
                    &selected.values,
                    span,
                )?;
                self.value = Some(selected);
            }
            Step::DefineThisField(property) => {
                let selected = self.take_value();
                if selected.values.is_empty() {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
                construct::define_field(context, &mut self.frame, &property, &selected.values)?;
                self.value = Some(selected);
            }
            Step::AssignThisField { property, span } => {
                let selected = self.take_value();
                if selected.values.is_empty() {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
                construct::assign_field(
                    context,
                    &mut self.frame,
                    &property,
                    &selected.values,
                    span,
                )?;
                self.value = Some(selected);
            }
        }
        Ok(None)
    }

    fn expression(
        &mut self,
        context: &mut impl KernelContext,
        body: &LocalBody,
        node: LocalExpression,
    ) -> Result<Option<LocalCallRequest>, RemainderDisposition> {
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
                self.steps.push(Step::FinishRecord(aggregate));
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
                self.steps.push(Step::FinishArray(aggregate));
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
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
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
                            array_push: Some(_),
                            ..
                        }
                    )
                {
                    self.frame.stop_normal_path();
                    return Ok(None);
                }
                self.frame.inputs.extend(callee.targets().iter().cloned());
                return Ok(self.invoke(callee, arguments, span, Invocation::Call));
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
                return Ok(self.invoke(
                    EvaluatedCallee::Direct(callee),
                    arguments,
                    span,
                    Invocation::Construct,
                ));
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
                // Capture the applicable module-prefix lane alongside the LHS
                // reference. The RHS may rebind the source cell, but JavaScript
                // has already evaluated the original receiver value.
                let module_prefix = context.module_prefix_receiver(&receiver, &receivers);
                self.steps.push(Step::StoreField {
                    receivers,
                    module_prefix,
                    property,
                    span,
                });
                self.steps.push(Step::Expression(value.as_ref().clone()));
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

/// The kernel's view of `allocation_transfer` for a frame: binding reads go to
/// the adapter and effects into the ledger. An adapter refusal met while
/// transferring is kept so the caller reports the right remainder.
// CLEANUP(added 2026-11-09): dissolve with `AllocationContext` in L2c.
struct LocalValueContext<'a, C> {
    context: &'a mut C,
    frame: &'a mut LocalFrame,
    refusal: Option<RemainderDisposition>,
}

impl<C: KernelContext> AllocationContext for LocalValueContext<'_, C> {
    fn source_covered(&self) -> bool {
        true
    }
    fn array_created(&mut self, allocation: u32, tainted: bool) {
        if tainted {
            // A hole has no own property, so later index reads see the
            // prototype; an unproved spread has unknown slots. Either way the
            // object is no longer a settled literal.
            self.context
                .world_mut()
                .surface
                .tainted_objects
                .insert(allocation);
        }
    }
    /// The one `IterateSpread` rule: the adapter's receipt covers the
    /// protocol and its source reads go into this frame's ledger; without a
    /// receipt the body's remainder is the adapter's to summarize.
    fn array_spread(&mut self, span: Span, operand: &Values) -> Option<NativeIterableReceipt> {
        match self
            .context
            .native_iterable(NativeIterableUse::ArraySpread(span), operand)
        {
            Some(receipt) => {
                self.frame
                    .ledger
                    .heap_accesses
                    .reads
                    .extend(receipt.external_heap_contents().reads);
                self.frame.ledger.effects.compose(&receipt.coverage);
                Some(receipt)
            }
            None => {
                self.frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
                self.refusal = Some(
                    match self
                        .context
                        .unproved(self.frame, Unproved::IterateSpread { operand, span })
                    {
                        Disposition::Summarized => RemainderDisposition::AlreadySummarized,
                        Disposition::Resolved(_) | Disposition::Refused => {
                            RemainderDisposition::NeedsSummary
                        }
                    },
                );
                None
            }
        }
    }
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue> {
        match self.context.read_binding(binding, self.frame) {
            Ok(result) => {
                self.frame.ledger.effects.compose(&result.effects);
                Some(result.allocation)
            }
            Err(remainder) => {
                self.refusal = Some(remainder);
                None
            }
        }
    }
    fn unmodeled_effect_operation(&mut self) {
        self.frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
    }
    fn noninvoking_operation(&mut self) {
        self.frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::no_effects());
    }
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue> {
        match value_of(self.context, self.frame, expression) {
            Ok(value) => Some(value),
            Err(remainder) => {
                self.refusal = Some(remainder);
                None
            }
        }
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.context.index().allocation_layout(allocation)
    }
    fn literal_property_kind(&self, allocation: u32, position: usize) -> LiteralPropertyKind {
        self.context
            .index()
            .literal_property_kind(allocation, position)
    }
    fn heap(&mut self) -> LiteralHeap<'_> {
        self.context.world_mut().literal_heap()
    }
    fn keyed_collection_new(
        &mut self,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<AllocationValue> {
        self.context
            .keyed_collection_new(self.frame, callee, arguments, site)
    }
}

/// Transfer one closed value expression in this frame. Scalar consumers
/// (the strict provider's direct allocation return) enter the kernel here.
pub(in crate::facts::sync_effects) fn value_of(
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
fn completing(mut values: Values, frame: &mut LocalFrame) -> Values {
    if values.remove(&AbstractValue::Value(Value::Unavailable)) {
        frame.record_unknown_abrupt();
    }
    values
}

fn completing_selected(mut selected: SelectedValue, frame: &mut LocalFrame) -> SelectedValue {
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
fn absorb(frame: &mut LocalFrame, result: LocalValueResult) -> SelectedValue {
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
fn absorb_primitive_transfer(frame: &mut LocalFrame, transfer: &AllocationValue) {
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

fn unproved_operator(
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

fn own_data_field(
    context: &impl KernelContext,
    receivers: &Values,
    property: &str,
    frame: &LocalFrame,
) -> Result<OwnDataSlot, OrderedFieldReadRefusal> {
    own_data_transfer::resolve_slot(
        context,
        receivers,
        property,
        OwnDataOwnership::FreshLocal {
            borrowed: &frame.borrowed_records,
            fresh: &frame.fresh_records,
        },
    )
}

fn read_field(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    receiver: &Id,
    receivers: &Values,
    property: &str,
    span: Span,
) -> Result<LocalValueResult, RemainderDisposition> {
    let module_prefix = context.module_prefix_receiver(receiver, receivers);
    let slot = if module_prefix {
        own_data_transfer::resolve_slot(
            context,
            receivers,
            property,
            OwnDataOwnership::CurrentPrefix,
        )
    } else {
        own_data_field(context, receivers, property, frame)
    };
    match slot {
        Ok(slot) => {
            let selection_dependencies = context.current_prerequisites();
            let read = slot.read(context);
            let selection =
                CallableSelection::observed(&read.values, selection_dependencies.clone());
            frame.ledger.heap_accesses.reads.insert(read.cell);
            let mut effects = EffectCoverageFragment::lexical_operation();
            if module_prefix {
                effects.depend_on(selection_dependencies.as_ref());
            }
            Ok(LocalValueResult {
                allocation: AllocationValue {
                    values: read.values,
                    judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
                },
                effects,
                selection,
            })
        }
        Err(refusal) => match context.unproved(
            frame,
            Unproved::FieldRead {
                receiver,
                receivers,
                property,
                span,
                refusal,
            },
        ) {
            Disposition::Resolved(allocation) => Ok(LocalValueResult {
                allocation,
                effects: EffectCoverageFragment::default(),
                selection: None,
            }),
            Disposition::Summarized => Err(RemainderDisposition::AlreadySummarized),
            Disposition::Refused => Err(RemainderDisposition::NeedsSummary),
        },
    }
}

fn read_field_expression(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    receiver: &Id,
    property: &str,
    span: Span,
) -> Result<SelectedValue, RemainderDisposition> {
    if let Some(result) = context.native_field_read(frame, receiver, property, span) {
        return Ok(absorb(frame, result));
    }
    let receivers = value_of(context, frame, &ValueExpr::Binding(receiver.clone()))?;
    frame.ledger.allocation.compose(&receivers.judgment);
    let receivers = completing(receivers.values, frame);
    if receivers.is_empty() {
        return Ok(SelectedValue::raw(Values::new()));
    }
    let result = read_field(context, frame, receiver, &receivers, property, span)?;
    Ok(absorb(frame, result))
}

fn store_field(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    receivers: &Values,
    module_prefix: bool,
    property: &str,
    values: &Values,
    span: Span,
) -> Result<(), RemainderDisposition> {
    let slot = if module_prefix {
        own_data_transfer::resolve_writable_slot(context, receivers, property)
    } else {
        own_data_field(context, receivers, property, frame).ok()
    };
    let Some(slot) = slot else {
        return Err(
            match context.unproved(
                frame,
                Unproved::FieldWrite {
                    receivers,
                    values,
                    span,
                },
            ) {
                Disposition::Summarized => RemainderDisposition::AlreadySummarized,
                Disposition::Resolved(_) | Disposition::Refused => {
                    RemainderDisposition::NeedsSummary
                }
            },
        );
    };
    let write_dependencies = context
        .property_is_writable(slot.object, property)
        .then(|| context.current_prerequisites());
    frame.ledger.heap_accesses.writes.insert(slot.cell());
    context.world_mut().literal_heap().write_property(
        slot.object,
        Some(property.to_owned()),
        values,
    );
    let effects = match write_dependencies {
        Some(dependencies) => {
            let mut effects = EffectCoverageFragment::lexical_operation();
            effects.depend_on(dependencies.as_ref());
            effects
        }
        None => EffectCoverageFragment::default(),
    };
    frame.ledger.effects.compose(&effects);
    Ok(())
}

fn closure_expression(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    callable: DeferredCallableCreation,
) -> Result<SelectedValue, RemainderDisposition> {
    let result = closure::create(context, frame, callable)?;
    Ok(absorb(frame, result))
}

fn store_binding(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    binding: &Id,
    selected: SelectedValue,
    span: Span,
) -> Result<SelectedValue, RemainderDisposition> {
    let values = selected.values;
    if values.is_empty() {
        frame.stop_normal_path();
        return Ok(SelectedValue::raw(values));
    }
    let Some(kind) = body.bindings.get(binding) else {
        frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        frame.binding_selections.remove(binding);
        let result = cells::store_captured(context, frame, binding, &values, span)?;
        frame.ledger.allocation.compose(&result.judgment);
        return Ok(completing_selected(
            SelectedValue::raw(result.values),
            frame,
        ));
    };
    let uninitialized_let = match kind {
        LocalBinding::Let => {
            let Some(current) = frame.environment.get(binding) else {
                return Err(RemainderDisposition::NeedsSummary);
            };
            current.contains(&AbstractValue::Value(Value::Unavailable))
        }
        _ => false,
    };
    if matches!(kind, LocalBinding::Const | LocalBinding::NamedSelf(_))
        || (*kind == LocalBinding::Parameter
            && frame.parameter_inputs.is_some()
            && !frame.initialized_bindings.contains(binding))
        || uninitialized_let
    {
        // Assignment to a const, a named self binding or a cell still in TDZ
        // throws before storing anything.
        frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        frame.record_unknown_abrupt();
        frame.stop_normal_path();
        return Ok(SelectedValue::raw(Values::new()));
    }
    if matches!(kind, LocalBinding::Parameter | LocalBinding::Let)
        && frame.initialized_bindings.contains(binding)
    {
        // The adapter's declarative-cell proof already rejects live captured
        // cells. Assignment's earlier unknown fragment remains unknown.
        let effects = cells::declarative_binding_operation(context, body, binding);
        frame.ledger.effects.compose(&effects);
    } else {
        frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
    }
    frame.environment.insert(binding.clone(), values.clone());
    if let Some(selection) = selected
        .selection
        .filter(|selection| selection.matches(&values))
    {
        frame.binding_selections.insert(binding.clone(), selection);
    } else {
        frame.binding_selections.remove(binding);
    }
    cells::declared_binding_stored(context, body, binding, frame);
    Ok(SelectedValue {
        values,
        selection: frame.binding_selections.get(binding).cloned(),
    })
}

fn update_binding_expression(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    binding: &Id,
    span: Span,
) -> Result<SelectedValue, RemainderDisposition> {
    let result = context.read_binding(binding, frame)?;
    let operand = absorb(frame, result);
    if operand.values.is_empty() {
        return Ok(operand);
    }
    let Some(result) = allocation_transfer::update_result(&operand.values) else {
        frame.ledger.allocation.compose(&AllocationJudgment::Opaque);
        frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        return Err(unproved_operator(context, frame, &operand.values, span));
    };
    absorb_primitive_transfer(frame, &result);
    let selected = completing_selected(SelectedValue::raw(result.values), frame);
    store_binding(context, body, frame, binding, selected, span)
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
                    array_push: None,
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
                    array_push: None,
                });
            }
            frame.inputs.extend(receivers.iter().cloned());
            let module_prefix = context.module_prefix_receiver(receiver, &receivers);
            let mut array_push = None;
            let result = if let Some((selection, effects)) =
                context.array_method_callee(&receivers, property)
            {
                array_push = Some(Box::new(selection));
                LocalValueResult {
                    allocation: AllocationValue::opaque(Values::new()),
                    effects,
                    selection: None,
                }
            } else {
                read_field(context, frame, receiver, &receivers, property, *member_span)?
            };
            let selection_dependencies = module_prefix
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
                array_push,
            })
        }
    }
}

fn bind_parameter(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    binding: &Id,
    selected: SelectedValue,
) {
    if selected.values.is_empty() {
        frame.stop_normal_path();
        return;
    }
    let selection = selected
        .selection
        .filter(|selection| selection.matches(&selected.values));
    frame.inputs.extend(selected.values.iter().cloned());
    frame.environment.insert(binding.clone(), selected.values);
    match selection {
        Some(selection) => {
            frame.binding_selections.insert(binding.clone(), selection);
        }
        None => {
            frame.binding_selections.remove(binding);
        }
    }
    cells::declared_binding_stored(context, body, binding, frame);
    frame.initialized_bindings.insert(binding.clone());
    let effects = cells::declarative_binding_operation(context, body, binding);
    frame.ledger.effects.compose(&effects);
}

fn prepare_body_bindings(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) {
    for (binding, kind) in &body.bindings {
        match kind {
            LocalBinding::Parameter => {}
            LocalBinding::NamedSelf(function) | LocalBinding::HoistedFunction(function) => {
                // A pre-prologue parameter/var value is not the hoisted
                // function. Preserve only an already-established exact value;
                // an unsupported activation value remains absent until use.
                if frame.environment.get(binding).is_some_and(|values| {
                    values.is_empty()
                        || values.iter().any(|value| {
                            !matches!(value,
                                AbstractValue::Closure(HeapAddress { callable: key, .. }, _)
                                | AbstractValue::Value(Value::Alloc(Kind::Function, key))
                                    if key == function)
                        })
                }) {
                    frame.environment.remove(binding);
                    frame.binding_selections.remove(binding);
                }
            }
            LocalBinding::Var => {
                let initialized = frame
                    .environment
                    .entry(binding.clone())
                    .or_insert_with(|| Values::from([AbstractValue::UNDEFINED]));
                if frame
                    .binding_selections
                    .get(binding)
                    .is_some_and(|selection| !selection.matches(initialized))
                {
                    frame.binding_selections.remove(binding);
                }
            }
            LocalBinding::Let | LocalBinding::Const => {
                frame.environment.insert(
                    binding.clone(),
                    Values::from([AbstractValue::Value(Value::Unavailable)]),
                );
                frame.binding_selections.remove(binding);
            }
        }
    }
    cells::instantiate_body_cells(context, body, frame);
    for (binding, kind) in &body.bindings {
        if *kind == LocalBinding::Parameter && frame.initialized_bindings.contains(binding) {
            continue;
        }
        // Let/const cells have actually been created in TDZ above. Hoisted
        // values and compatibility parameters retain their separate boundary.
        let effects = if matches!(kind, LocalBinding::Let | LocalBinding::Const) {
            cells::declarative_binding_operation(context, body, binding)
        } else {
            EffectCoverageFragment::default()
        };
        frame.ledger.effects.compose(&effects);
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
