//! One ordered local-operation semantics for execution and provider queries.

use super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, LiteralHeap, LiteralLayout,
};
use super::local_operations::{LocalBinding, LocalBody, LocalExpression, LocalOperation};
use super::value_model::{
    AbstractValue, Context, OwnPropertyDescriptorSyntax, OwnPropertyShape, ValueExpr, Values,
};
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
}

impl LocalFrame {
    pub(super) fn new(environment: Context) -> Self {
        Self {
            inputs: environment.values().flatten().cloned().collect(),
            environment,
            borrowed_records: BTreeSet::new(),
            fresh_records: BTreeSet::new(),
            returns: Values::new(),
        }
    }
}

pub(super) trait LocalContext {
    fn read_binding(&mut self, binding: &Id, frame: &mut LocalFrame) -> Option<AllocationValue>;
    fn literal_layout(&self, allocation: u32) -> LiteralLayout;
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape>;
    fn object_properties_are_settled(&self, allocation: u32) -> bool;
    fn heap(&mut self) -> LiteralHeap<'_>;
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
    if let ValueExpr::ObjectLiteral(object, _) = expression
        && new_record
        && context.ordinary_record_shape(*object).is_some()
        && result
            .values
            .contains(&AbstractValue::Value(Value::Alloc(Kind::Object, *object)))
    {
        frame.fresh_records.insert(*object);
    }
    Some(result)
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
) -> Result<u32, OrderedFieldReadRefusal> {
    if receivers.len() != 1 {
        return Err(OrderedFieldReadRefusal::ReceiverNotSingleton);
    }
    let Some(AbstractValue::Value(Value::Alloc(Kind::Object, object))) = receivers.first() else {
        return Err(OrderedFieldReadRefusal::ReceiverNotObject);
    };
    if frame.borrowed_records.contains(object) {
        return Err(OrderedFieldReadRefusal::BorrowedReceiver);
    }
    if !context.object_properties_are_settled(*object) {
        return Err(OrderedFieldReadRefusal::UnsettledReceiver);
    }
    let Some(shape) = context.ordinary_record_shape(*object) else {
        return Err(OrderedFieldReadRefusal::UnsupportedRecordShape);
    };
    if !frame.fresh_records.contains(object) {
        return Err(OrderedFieldReadRefusal::AllocationNotFresh);
    }
    let Some(descriptor) = shape.properties.get(property) else {
        return Err(OrderedFieldReadRefusal::MissingField);
    };
    if !matches!(descriptor, OwnPropertyDescriptorSyntax::Data) {
        return Err(OrderedFieldReadRefusal::NonDataField);
    }
    let key = Some(property.to_owned());
    if !context
        .heap()
        .properties
        .get(object)
        .is_some_and(|properties| properties.contains_key(&key))
    {
        return Err(OrderedFieldReadRefusal::MissingHeapValue);
    }
    Ok(*object)
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
        LocalExpression::Sequence(expressions) => {
            let mut values = Values::from([AbstractValue::UNDEFINED]);
            for node in expressions.iter() {
                values = expression(context, node, body, frame, judgment)?;
                if values.is_empty() {
                    break;
                }
            }
            Some(values)
        }
        LocalExpression::Assign(binding, node) => {
            let values = expression(context, node, body, frame, judgment)?;
            if values.is_empty() {
                return Some(values);
            }
            if body.bindings[binding] == LocalBinding::Const
                || (body.bindings[binding] == LocalBinding::Let
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
            Some(values)
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
            match own_data_field(context, &receivers, property, frame) {
                Ok(object) => {
                    // The shared guard requires an existing own data slot on a
                    // settled, fresh, unborrowed ordinary record. It is not
                    // broadened for reads of other receivers.
                    let values =
                        context.heap().properties[&object][&Some(property.clone())].clone();
                    Some(completing(values, frame))
                }
                Err(refusal) => {
                    let result = context.unproved_field_read(
                        frame, receiver, &receivers, property, *span, refusal,
                    )?;
                    judgment.compose(&result.judgment);
                    Some(completing(result.values, frame))
                }
            }
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
            let Ok(object) = own_data_field(context, &receivers, property, frame) else {
                context.unproved_field_write(frame, &receivers, &values, *span);
                return None;
            };
            context
                .heap()
                .write_property(object, Some(property.clone()), &values);
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
