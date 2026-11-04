//! One ordered local-operation semantics for execution and provider queries.

use super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, LiteralHeap, LiteralLayout,
};
use super::effect_coverage::EffectCoverageFragment;
use super::local_operations::{
    DeferredCallableCreation, LocalBinding, LocalBody, LocalCallee, LocalExpression, LocalOperation,
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
    effect_coverage: EffectCoverageFragment,
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
            effect_coverage: EffectCoverageFragment::no_effects(),
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
    Member {
        receiver: Values,
        targets: Values,
        selection_dependencies: Option<Box<ProofDependencies>>,
    },
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

pub(super) struct LocalValueResult {
    pub(super) allocation: AllocationValue,
    pub(super) effects: EffectCoverageFragment,
}

pub(super) struct LocalTransferResult {
    pub(super) allocation: AllocationJudgment,
    pub(super) effects: EffectCoverageFragment,
}

pub(super) trait LocalContext: OwnDataContext {
    fn array_created(&mut self, _allocation: u32, _sparse: bool) {}
    fn instantiate_bindings(&mut self, _frame: &mut LocalFrame) {}
    fn local_binding_stored(&mut self, _binding: &Id, _frame: &LocalFrame) {}
    fn read_binding(&mut self, binding: &Id, frame: &mut LocalFrame) -> Option<LocalValueResult>;
    /// Whether this exact evaluated receiver came from an initialized,
    /// non-imported module binding at the current prefix. Strict provider
    /// queries keep the default false and require fresh-local ownership.
    fn module_prefix_receiver(&self, _binding: &Id, _receivers: &Values) -> bool {
        false
    }
    /// Conditions under which the current settled prefix is valid. The
    /// application adapter snapshots these at an actual read/write transfer;
    /// strict provider queries do not need application prerequisites.
    fn selection_prerequisites(&self) -> Box<ProofDependencies> {
        Box::default()
    }
    fn closure(
        &mut self,
        frame: &mut LocalFrame,
        creation: DeferredCallableCreation,
    ) -> Option<LocalValueResult>;
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
    ) -> Option<LocalValueResult>;
    fn literal_layout(&self, allocation: u32) -> LiteralLayout;
    fn literal_property_kind(
        &self,
        allocation: u32,
        position: usize,
    ) -> super::value_model::LiteralPropertyKind;
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
    fn array_created(&mut self, allocation: u32, sparse: bool) {
        self.context.array_created(allocation, sparse);
    }
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue> {
        let result = self.context.read_binding(binding, self.frame)?;
        self.frame.effect_coverage.compose(&result.effects);
        Some(result.allocation)
    }
    fn unmodeled_effect_operation(&mut self) {
        self.frame
            .effect_coverage
            .compose(&EffectCoverageFragment::default());
    }
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue> {
        value(self.context, expression, self.frame)
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.context.literal_layout(allocation)
    }
    fn literal_property_kind(
        &self,
        allocation: u32,
        position: usize,
    ) -> super::value_model::LiteralPropertyKind {
        self.context.literal_property_kind(allocation, position)
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
) -> Option<LocalValueResult> {
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
            let selection_dependencies = module_prefix.then(|| context.selection_prerequisites());
            let read = slot.read(context);
            context.record_property_read(read.cell);
            let mut effects = EffectCoverageFragment::lexical_operation();
            if let Some(dependencies) = selection_dependencies {
                effects.depend_on(dependencies.as_ref());
            }
            Some(LocalValueResult {
                allocation: AllocationValue {
                    values: read.values,
                    judgment: AllocationJudgment::CoveredAllocationOnly(Default::default()),
                },
                effects,
            })
        }
        Err(refusal) => context
            .unproved_field_read(frame, receiver, receivers, property, span, refusal)
            .map(|allocation| LocalValueResult {
                allocation,
                effects: EffectCoverageFragment::default(),
            }),
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
        LocalExpression::PrimitiveLiteral(kind) => {
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::lexical_operation());
            Some(Values::from([AbstractValue::Value(Value::Prim(*kind))]))
        }
        LocalExpression::This => {
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
            frame.this_values.clone()
        }
        LocalExpression::Record {
            allocation,
            properties,
        } => {
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
            if !matches!(context.literal_layout(*allocation), LiteralLayout::Record) {
                return None;
            }
            let mut new_record = !context.heap().properties.contains_key(allocation);
            let result = allocation_transfer::record(
                &mut LocalValueContext { context, frame },
                *allocation,
                properties
                    .iter()
                    .map(|property| property.map(|value| value)),
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
        LocalExpression::Array {
            allocation,
            elements,
        } => {
            let result = allocation_transfer::array(
                &mut LocalValueContext { context, frame },
                *allocation,
                elements.iter().map(Option::as_ref),
                AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
                |context, node| {
                    let mut judgment =
                        AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default());
                    let values =
                        expression(context.context, node, body, context.frame, &mut judgment)?;
                    context.frame.inputs.extend(values.iter().cloned());
                    Some(AllocationValue { values, judgment })
                },
            )?;
            judgment.compose(&result.judgment);
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
            frame.effect_coverage.compose(&result.effects);
            judgment.compose(&result.allocation.judgment);
            Some(completing(result.allocation.values, frame))
        }
        LocalExpression::Assign {
            binding,
            value: node,
            span,
        } => {
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
            let values = expression(context, node, body, frame, judgment)?;
            if values.is_empty() {
                return Some(values);
            }
            let Some(kind) = body.bindings.get(binding) else {
                let result = context.assign_capture(frame, binding, &values, *span)?;
                judgment.compose(&result.judgment);
                return Some(completing(result.values, frame));
            };
            if matches!(kind, LocalBinding::Const | LocalBinding::NamedSelf(_))
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
                    let module_prefix = context.module_prefix_receiver(receiver, &receivers);
                    let result = read_field(context, frame, receiver, &receivers, property, *span)?;
                    let selection_dependencies = module_prefix
                        .then(|| result.effects.lexical_selection_dependencies())
                        .flatten();
                    // Selection/GetValue is an executed lexical operation and
                    // is composed now, before any argument can throw or mutate
                    // the selected property.
                    frame.effect_coverage.compose(&result.effects);
                    judgment.compose(&result.allocation.judgment);
                    EvaluatedCallee::Member {
                        receiver: receivers,
                        targets: completing(result.allocation.values, frame),
                        selection_dependencies,
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
            frame.effect_coverage.compose(&result.effects);
            judgment.compose(&result.allocation.judgment);
            Some(completing(result.allocation.values, frame))
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
            frame.effect_coverage.compose(&result.effects);
            judgment.compose(&result.allocation.judgment);
            Some(completing(result.allocation.values, frame))
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
            // Capture the applicable module-prefix lane alongside the LHS
            // reference. The RHS may rebind the source cell, but JavaScript
            // has already evaluated the original receiver value.
            let module_prefix = context.module_prefix_receiver(receiver, &receivers);
            let values = expression(context, node, body, frame, judgment)?;
            if values.is_empty() {
                return Some(values);
            }
            let slot = if module_prefix {
                own_data_transfer::resolve_writable_slot(context, &receivers, property)
            } else {
                own_data_field(context, &receivers, property, frame).ok()
            };
            let Some(slot) = slot else {
                context.unproved_field_write(frame, &receivers, &values, *span);
                return None;
            };
            let writable = context.property_is_writable(slot.object, property);
            let write_dependencies = writable.then(|| context.selection_prerequisites());
            context.record_property_write(slot.cell());
            context
                .heap()
                .write_property(slot.object, Some(property.clone()), &values);
            if writable {
                let mut effects = EffectCoverageFragment::lexical_operation();
                effects.depend_on(
                    write_dependencies
                        .as_ref()
                        .expect("writable proof captures its prefix dependencies")
                        .as_ref(),
                );
                frame.effect_coverage.compose(&effects);
            } else {
                frame
                    .effect_coverage
                    .compose(&EffectCoverageFragment::default());
            }
            Some(values)
        }
        LocalExpression::DefineThisField {
            property,
            value: node,
            ..
        } => {
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
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
) -> Option<LocalTransferResult> {
    let mut judgment = AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default());
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
                                AbstractValue::Closure(key, _)
                                | AbstractValue::Value(Value::Alloc(Kind::Function, key))
                                    if key == function)
                        })
                }) {
                    frame.environment.remove(binding);
                }
            }
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
    if !body.bindings.is_empty() {
        // Activation-local cells/functions, self bindings and parameter
        // prologues have no operation receipts in this module-cell-only family.
        frame
            .effect_coverage
            .compose(&EffectCoverageFragment::default());
    }
    context.instantiate_bindings(frame);
    for operation in body.operations.iter() {
        match operation {
            LocalOperation::Initialize(binding, node) => {
                frame
                    .effect_coverage
                    .compose(&EffectCoverageFragment::default());
                let values = match node {
                    Some(node) => expression(context, node, body, frame, &mut judgment)?,
                    None if matches!(
                        body.bindings[binding],
                        LocalBinding::Var
                            | LocalBinding::Parameter
                            | LocalBinding::HoistedFunction(_)
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
                frame
                    .effect_coverage
                    .compose(&EffectCoverageFragment::default());
                frame.effect_coverage.abrupt();
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

fn finish(frame: &LocalFrame, mut judgment: AllocationJudgment) -> LocalTransferResult {
    let mut effects = frame.effect_coverage.clone();
    if frame
        .returns
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        judgment = AllocationJudgment::Opaque;
        if frame.returns.len() == 1 {
            effects.abrupt();
        } else {
            effects.uncertain_completion();
        }
    }
    LocalTransferResult {
        allocation: judgment,
        effects,
    }
}
