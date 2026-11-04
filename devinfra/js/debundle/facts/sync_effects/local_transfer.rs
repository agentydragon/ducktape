//! One ordered local-operation semantics for execution and provider queries.

use super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, LiteralHeap, LiteralLayout,
};
use super::local_operations::{
    LocalBinding, LocalBody, LocalCallee, LocalExpression, LocalOperation,
};
use super::own_data_transfer::{self, OwnDataContext, OwnDataOwnership, OwnDataSlot};
use super::value_model::{AbstractValue, CallSite, Context, ValueExpr, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::reports::schema::OrderedFieldReadRefusal;
use std::collections::BTreeSet;
use swc_common::Span;
use swc_ecma_ast::Id;

pub(super) struct LocalFrame {
    pub(super) environment: Context,
    /// Entry arguments/captures and external values read while executing.
    /// Retained for a truncated remainder even after local aliases change.
    pub(super) inputs: Values,
    pub(super) borrowed_records: BTreeSet<u32>,
    pub(super) fresh_records: BTreeSet<u32>,
    pub(super) returns: Values,
    /// Set only by a checked constructor provider query. Other local calls do
    /// not acquire a receiver just because their syntax mentions `this`.
    pub(super) this_values: Option<Values>,
}

impl LocalFrame {
    pub(super) fn new(environment: Context) -> Self {
        Self {
            inputs: environment.values().flatten().cloned().collect(),
            environment,
            borrowed_records: BTreeSet::new(),
            fresh_records: BTreeSet::new(),
            returns: Values::new(),
            this_values: None,
        }
    }
}

#[derive(Clone)]
pub(super) enum EvaluatedCallee {
    Direct(Values),
    Member { receiver: Values, targets: Values },
}

impl EvaluatedCallee {
    pub(super) fn targets(&self) -> &Values {
        match self {
            Self::Direct(targets) | Self::Member { targets, .. } => targets,
        }
    }

    pub(super) fn receiver(&self) -> Option<&Values> {
        match self {
            Self::Direct(_) => None,
            Self::Member { receiver, .. } => Some(receiver),
        }
    }
}

pub(super) trait LocalContext: OwnDataContext {
    fn instantiate_bindings(&mut self, _frame: &mut LocalFrame) {}
    fn local_binding_stored(&mut self, _binding: &Id, _frame: &LocalFrame) {}
    fn read_binding(&mut self, binding: &Id, frame: &mut LocalFrame) -> Option<AllocationValue>;
    fn closure(&mut self, frame: &mut LocalFrame, callable: u32) -> Option<AllocationValue>;
    fn assign_capture(
        &mut self,
        frame: &mut LocalFrame,
        binding: &Id,
        values: &Values,
        span: Span,
    ) -> Option<AllocationValue>;
    /// Operands have already been evaluated in source order. Return the value
    /// from that invocation's effect transfer; do not evaluate the call again.
    fn call(
        &mut self,
        frame: &mut LocalFrame,
        callee: &EvaluatedCallee,
        arguments: &[Values],
        span: Span,
    ) -> Option<AllocationValue>;
    fn literal_layout(&self, allocation: u32) -> LiteralLayout;
    fn heap(&mut self) -> LiteralHeap<'_>;
    fn record_property_read(&mut self, cell: crate::facts::EffectCell);
    fn record_property_write(&mut self, cell: crate::facts::EffectCell);
    /// The receiver cell was read at this operation. A strict query refuses
    /// an unproved field; application execution retains its existing proofs
    /// and summarizes the remainder if getter effects remain unproved.
    fn unproved_field_read(
        &mut self,
        frame: &mut LocalFrame,
        receiver: &Id,
        receivers: &Values,
        property: &str,
        span: Span,
        refusal: OrderedFieldReadRefusal,
    ) -> Option<AllocationValue>;
    /// Application execution summarizes the remainder; a strict query refuses
    /// certification. Neither consumer retries already executed operations.
    fn unproved_field_write(
        &mut self,
        frame: &mut LocalFrame,
        receivers: &Values,
        values: &Values,
        span: Span,
    );
    fn publish_throw(&mut self, values: &Values);
    fn keyed_collection_new(
        &mut self,
        _frame: &mut LocalFrame,
        _callee: &ValueExpr,
        _arguments: &[ValueExpr],
        _site: CallSite,
    ) -> Option<AllocationValue> {
        None
    }
}

struct LocalValueContext<'a, C> {
    context: &'a mut C,
    frame: &'a mut LocalFrame,
}

impl<C: LocalContext> AllocationContext for LocalValueContext<'_, C> {
    fn source_covered(&self) -> bool {
        true
    }
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue> {
        self.context.read_binding(binding, self.frame)
    }
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue> {
        value(self.context, expression, self.frame)
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.context.literal_layout(allocation)
    }
    fn heap(&mut self) -> LiteralHeap<'_> {
        self.context.heap()
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

fn value(
    context: &mut impl LocalContext,
    expression: &ValueExpr,
    frame: &mut LocalFrame,
) -> Option<AllocationValue> {
    // A site already in the heap may denote an older allocation. The current
    // domain cannot distinguish that generation from this invocation's record.
    let new_record = match expression {
        ValueExpr::ObjectLiteral(object, _) => {
            !context.heap().properties.contains_key(object)
                && !frame.borrowed_records.contains(object)
        }
        _ => false,
    };
    let result =
        allocation_transfer::transfer(&mut LocalValueContext { context, frame }, expression)?;
    if let ValueExpr::ObjectLiteral(object, _) = expression {
        mark_fresh_record(context, frame, *object, new_record, &result.values);
    }
    Some(result)
}

fn mark_fresh_record(
    context: &impl LocalContext,
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

fn completing(mut values: Values, frame: &mut LocalFrame) -> Values {
    if values.remove(&AbstractValue::Value(Value::Unavailable)) {
        frame
            .returns
            .insert(AbstractValue::Value(Value::Unavailable));
    }
    values
}

fn own_data_field(
    context: &mut impl LocalContext,
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
    context: &mut impl LocalContext,
    frame: &mut LocalFrame,
    receiver: &Id,
    receivers: &Values,
    property: &str,
    span: Span,
) -> Option<AllocationValue> {
    match own_data_field(context, receivers, property, frame) {
        Ok(slot) => {
            let read = slot.read(context);
            context.record_property_read(read.cell);
            Some(AllocationValue {
                values: read.values,
                judgment: AllocationJudgment::CoveredAllocationOnly(Default::default()),
            })
        }
        Err(refusal) => {
            context.unproved_field_read(frame, receiver, receivers, property, span, refusal)
        }
    }
}

fn expression(
    context: &mut impl LocalContext,
    node: &LocalExpression,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<Values> {
    match node {
        LocalExpression::Value(node) => {
            let result = value(context, node, frame)?;
            judgment.compose(&result.judgment);
            Some(completing(result.values, frame))
        }
        LocalExpression::This => frame.this_values.clone(),
        LocalExpression::Record {
            allocation,
            properties,
        } => {
            if !matches!(context.literal_layout(*allocation), LiteralLayout::Record) {
                return None;
            }
            let mut new_record = !context.heap().properties.contains_key(allocation);
            let result = allocation_transfer::record(
                &mut LocalValueContext { context, frame },
                *allocation,
                properties
                    .iter()
                    .map(|(name, value)| (Some(name.clone()), value)),
                AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
                |context, node| {
                    let mut judgment =
                        AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default());
                    let values =
                        expression(context.context, node, body, context.frame, &mut judgment)?;
                    // A later child can truncate the unfinished literal.
                    context.frame.inputs.extend(values.iter().cloned());
                    // A child can reenter this allocation site. Its resulting
                    // heap entry then also represents an older record.
                    new_record &= !context.context.heap().properties.contains_key(allocation);
                    Some(AllocationValue { values, judgment })
                },
            )?;
            judgment.compose(&result.judgment);
            mark_fresh_record(context, frame, *allocation, new_record, &result.values);
            Some(completing(result.values, frame))
        }
        LocalExpression::Sequence(expressions) => {
            let result = allocation_transfer::sequence(
                &mut LocalValueContext { context, frame },
                expressions.iter(),
                AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
                |context, node| {
                    let mut judgment =
                        AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default());
                    let values =
                        expression(context.context, node, body, context.frame, &mut judgment)?;
                    Some(AllocationValue { values, judgment })
                },
            )?;
            judgment.compose(&result.judgment);
            Some(completing(result.values, frame))
        }
        LocalExpression::Closure(callable) => {
            let result = context.closure(frame, *callable)?;
            judgment.compose(&result.judgment);
            Some(completing(result.values, frame))
        }
        LocalExpression::Assign {
            binding,
            value: node,
            span,
        } => {
            let values = expression(context, node, body, frame, judgment)?;
            if values.is_empty() {
                return Some(values);
            }
            let Some(kind) = body.bindings.get(binding) else {
                let result = context.assign_capture(frame, binding, &values, *span)?;
                judgment.compose(&result.judgment);
                return Some(completing(result.values, frame));
            };
            if *kind == LocalBinding::Const
                || (*kind == LocalBinding::Let
                    && frame
                        .environment
                        .get(binding)
                        .expect("instantiated local binding")
                        .contains(&AbstractValue::Value(Value::Unavailable)))
            {
                frame
                    .returns
                    .insert(AbstractValue::Value(Value::Unavailable));
                return Some(Values::new());
            }
            frame.environment.insert(binding.clone(), values.clone());
            context.local_binding_stored(binding, frame);
            Some(values)
        }
        LocalExpression::Call {
            callee,
            arguments,
            span,
        } => {
            // GetValue(callee) precedes every argument. In particular, an
            // argument assignment must not replace this captured callee.
            let callee = match callee {
                LocalCallee::Binding(binding) => {
                    let result = value(context, &ValueExpr::Binding(binding.clone()), frame)?;
                    judgment.compose(&result.judgment);
                    EvaluatedCallee::Direct(completing(result.values, frame))
                }
                LocalCallee::Member {
                    receiver,
                    property,
                    span,
                } => {
                    let result = value(context, &ValueExpr::Binding(receiver.clone()), frame)?;
                    judgment.compose(&result.judgment);
                    let receivers = completing(result.values, frame);
                    if receivers.is_empty() {
                        return Some(Values::new());
                    }
                    frame.inputs.extend(receivers.iter().cloned());
                    let result = read_field(context, frame, receiver, &receivers, property, *span)?;
                    judgment.compose(&result.judgment);
                    EvaluatedCallee::Member {
                        receiver: receivers,
                        targets: completing(result.values, frame),
                    }
                }
            };
            if callee.targets().is_empty() {
                return Some(Values::new());
            }
            frame.inputs.extend(callee.targets().iter().cloned());
            let mut inputs = Vec::with_capacity(arguments.len());
            for argument in arguments.iter() {
                let values = expression(context, argument, body, frame, judgment)?;
                if values.is_empty() {
                    return Some(Values::new());
                }
                frame.inputs.extend(values.iter().cloned());
                inputs.push(values);
            }
            let result = context.call(frame, &callee, &inputs, *span)?;
            judgment.compose(&result.judgment);
            Some(completing(result.values, frame))
        }
        LocalExpression::ReadField {
            receiver,
            property,
            span,
        } => {
            let receivers = value(context, &ValueExpr::Binding(receiver.clone()), frame)?;
            judgment.compose(&receivers.judgment);
            let receivers = completing(receivers.values, frame);
            if receivers.is_empty() {
                return Some(Values::new());
            }
            let result = read_field(context, frame, receiver, &receivers, property, *span)?;
            judgment.compose(&result.judgment);
            Some(completing(result.values, frame))
        }
        LocalExpression::AssignField {
            receiver,
            property,
            value: node,
            span,
        } => {
            // Capture the LHS reference before the RHS can replace its binding.
            let receivers = value(context, &ValueExpr::Binding(receiver.clone()), frame)?;
            judgment.compose(&receivers.judgment);
            let receivers = completing(receivers.values, frame);
            if receivers.is_empty() {
                return Some(Values::new());
            }
            let values = expression(context, node, body, frame, judgment)?;
            if values.is_empty() {
                return Some(values);
            }
            let Ok(slot) = own_data_field(context, &receivers, property, frame) else {
                context.unproved_field_write(frame, &receivers, &values, *span);
                return None;
            };
            context.record_property_write(slot.cell());
            context
                .heap()
                .write_property(slot.object, Some(property.clone()), &values);
            Some(values)
        }
        LocalExpression::DefineThisField {
            property,
            value: node,
            ..
        } => {
            let values = expression(context, node, body, frame, judgment)?;
            if values.is_empty() {
                return Some(values);
            }
            if values.contains(&AbstractValue::Value(Value::Unavailable)) {
                return None;
            }
            let this_values = frame.this_values.as_ref()?;
            let Some(AbstractValue::Value(Value::Alloc(Kind::Object, object))) =
                this_values.first()
            else {
                return None;
            };
            if this_values.len() != 1 {
                return None;
            }
            // Constructor summaries use this heap entry only to walk values
            // retained by the fresh instance. `ordinary_record_shape` remains
            // the prerequisite for reads, so class slots never become
            // ordinary data fields by being recorded here.
            context
                .heap()
                .write_property(*object, Some(property.clone()), &values);
            Some(values)
        }
    }
}

/// Syntax coverage comes from LocalBody's canonical lowering. The judgment
/// composes every reached operation, including values discarded by a sequence.
pub(super) fn execute(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) -> Option<AllocationJudgment> {
    let mut judgment = AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default());
    for (binding, kind) in &body.bindings {
        match kind {
            LocalBinding::Parameter => {}
            LocalBinding::Var => {
                frame
                    .environment
                    .entry(binding.clone())
                    .or_insert_with(|| Values::from([AbstractValue::UNDEFINED]));
            }
            LocalBinding::Let | LocalBinding::Const => {
                frame.environment.insert(
                    binding.clone(),
                    Values::from([AbstractValue::Value(Value::Unavailable)]),
                );
            }
        }
    }
    context.instantiate_bindings(frame);
    for operation in body.operations.iter() {
        match operation {
            LocalOperation::Initialize(binding, node) => {
                let values = match node {
                    Some(node) => expression(context, node, body, frame, &mut judgment)?,
                    None if matches!(
                        body.bindings[binding],
                        LocalBinding::Var | LocalBinding::Parameter
                    ) =>
                    {
                        continue;
                    }
                    None => Values::from([AbstractValue::UNDEFINED]),
                };
                if values.is_empty() {
                    return Some(finish(frame, judgment));
                }
                frame.environment.insert(binding.clone(), values);
                context.local_binding_stored(binding, frame);
            }
            LocalOperation::Evaluate(node) => {
                if expression(context, node, body, frame, &mut judgment)?.is_empty() {
                    return Some(finish(frame, judgment));
                }
            }
            LocalOperation::Return(node) => {
                let values = match node {
                    Some(node) => expression(context, node, body, frame, &mut judgment)?,
                    None => Values::from([AbstractValue::UNDEFINED]),
                };
                frame.returns.extend(values);
                return Some(finish(frame, judgment));
            }
            LocalOperation::Throw(node) => {
                let values = expression(context, node, body, frame, &mut judgment)?;
                context.publish_throw(&values);
                frame
                    .returns
                    .insert(AbstractValue::Value(Value::Unavailable));
                return Some(finish(frame, judgment));
            }
        }
    }
    frame.returns.insert(AbstractValue::UNDEFINED);
    Some(finish(frame, judgment))
}

fn finish(frame: &LocalFrame, mut judgment: AllocationJudgment) -> AllocationJudgment {
    if frame
        .returns
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        judgment = AllocationJudgment::Opaque;
    }
    judgment
}
