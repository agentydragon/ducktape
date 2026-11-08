//! One ordered local-operation semantics for execution and provider queries.

use super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, LiteralHeap, LiteralLayout,
};
use super::array_transfer::ArrayPushSelection;
use super::effect_coverage::EffectCoverageFragment;
use super::local_operations::{
    DeferredCallableCreation, LocalBinding, LocalBody, LocalCallee, LocalExpression, LocalOperation,
};
use super::own_data_transfer::{self, OwnDataContext, OwnDataOwnership, OwnDataSlot};
use super::value_model::{AbstractValue, CallSite, Context, PropertyDefinition, ValueExpr, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::OrderedFieldReadRefusal;
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

pub(super) struct LocalFrame {
    pub(super) environment: Context,
    pub(super) binding_selections: BTreeMap<Id, CallableSelection>,
    pub(super) return_selection: Option<CallableSelection>,
    /// Present only when the caller delegates the actual parameter prologue
    /// to this kernel. Provider/compatibility frames are already bound.
    pub(super) parameter_inputs: Option<Vec<SelectedValue>>,
    /// Bindings initialized by an actual operation in this activation.
    /// Prebound values alone do not establish a declarative-cell receipt.
    pub(super) initialized_bindings: BTreeSet<Id>,
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
            binding_selections: BTreeMap::new(),
            return_selection: None,
            parameter_inputs: None,
            initialized_bindings: BTreeSet::new(),
            effect_coverage: EffectCoverageFragment::no_effects(),
            borrowed_records: BTreeSet::new(),
            fresh_records: BTreeSet::new(),
            returns: Values::new(),
            this_values: None,
        }
    }
}

/// Nonterminal provenance of an actually selected abstract callable. This
/// proves neither fresh allocation nor complete invocation effects.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct CallableSelection {
    pub(super) target: AbstractValue,
    pub(super) dependencies: Box<ProofDependencies>,
}

impl CallableSelection {
    pub(super) fn observed(values: &Values, dependencies: Box<ProofDependencies>) -> Option<Self> {
        if values.len() != 1 {
            return None;
        }
        let target = values.first()?.clone();
        matches!(
            &target,
            AbstractValue::Closure(..) | AbstractValue::Value(Value::Alloc(Kind::Function, _))
        )
        .then_some(Self {
            target,
            dependencies,
        })
    }

    pub(super) fn matches(&self, values: &Values) -> bool {
        values.len() == 1 && values.first() == Some(&self.target)
    }
}

#[derive(Clone)]
pub(super) struct SelectedValue {
    pub(super) values: Values,
    pub(super) selection: Option<CallableSelection>,
}

impl SelectedValue {
    pub(super) fn raw(values: Values) -> Self {
        Self {
            values,
            selection: None,
        }
    }
}

#[derive(Clone)]
pub(super) enum EvaluatedCallee {
    Direct(SelectedValue),
    Member {
        receiver: Values,
        targets: Values,
        selection_dependencies: Option<Box<ProofDependencies>>,
        array_push: Option<Box<ArrayPushSelection>>,
    },
}

impl EvaluatedCallee {
    pub(super) fn targets(&self) -> &Values {
        match self {
            Self::Direct(targets) => &targets.values,
            Self::Member { targets, .. } => targets,
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
    pub(super) selection: Option<CallableSelection>,
}

pub(super) struct LocalTransferResult {
    pub(super) allocation: AllocationJudgment,
    pub(super) effects: EffectCoverageFragment,
}

pub(super) trait LocalContext: OwnDataContext {
    fn array_created(&mut self, _allocation: u32, _sparse: bool) {}
    fn instantiate_parameter_bindings(&mut self, _frame: &mut LocalFrame) {}
    /// Effects of declarative-cell creation, initialization, or a checked
    /// store to an initialized local. Captured cells remain separate.
    fn declarative_binding_operation(
        &self,
        _binding: &Id,
        _frame: &LocalFrame,
    ) -> EffectCoverageFragment {
        EffectCoverageFragment::default()
    }
    fn instantiate_bindings(&mut self, _frame: &mut LocalFrame) {}
    fn local_binding_stored(&mut self, _binding: &Id, _frame: &LocalFrame) {}
    fn read_binding(&mut self, binding: &Id, frame: &mut LocalFrame) -> Option<LocalValueResult>;
    fn own_data_spread_source(
        &self,
        _allocation: u32,
    ) -> Option<super::allocation_transfer::OwnDataSpreadSource> {
        None
    }
    /// Whether this exact evaluated receiver came from an initialized,
    /// non-imported module binding at the current prefix. Strict provider
    /// queries keep the default false and require fresh-local ownership.
    fn module_prefix_receiver(&self, _binding: &Id, _receivers: &Values) -> bool {
        false
    }
    fn array_push_callee(
        &mut self,
        _receivers: &Values,
        _property: &str,
    ) -> Option<(ArrayPushSelection, EffectCoverageFragment)> {
        None
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
        arguments: &[SelectedValue],
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
    /// An exact native binding and own-data slot proof can cover this whole
    /// lookup, including its global receiver. Other contexts evaluate the
    /// receiver and retain the ordinary own-data or accessor path.
    fn native_field_read(
        &mut self,
        _frame: &mut LocalFrame,
        _receiver: &Id,
        _property: &str,
        _span: Span,
    ) -> Option<LocalValueResult> {
        None
    }
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
    /// Both binary operands have completed in source order, but at least one
    /// may be an object or unknown value whose coercion can execute user code.
    fn binary_fallback(&mut self, _frame: &LocalFrame, _reached: &Values, _span: Span) {}
    fn refused_spread(&mut self, _frame: &LocalFrame, _source: &Values, _span: Span) {}
    fn incomplete_own_data_spread(
        &mut self,
        _frame: &mut LocalFrame,
        _source: &Values,
        _span: Span,
    ) {
    }
    fn spread_copy_recorded(&mut self, _allocation: u32) {}
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
    fn noninvoking_operation(&mut self) {
        self.frame
            .effect_coverage
            .compose(&EffectCoverageFragment::no_effects());
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
    fn own_data_spread_source(
        &self,
        allocation: u32,
    ) -> Option<super::allocation_transfer::OwnDataSpreadSource> {
        self.context.own_data_spread_source(allocation)
    }
    fn object_properties_are_settled(&self, allocation: u32) -> bool {
        self.context.object_properties_are_settled(allocation)
    }
    fn property_is_accessor(&self, allocation: u32, property: &str) -> bool {
        self.context.property_is_accessor(allocation, property)
    }
    fn has_dynamic_properties(&self, allocation: u32) -> bool {
        self.context.has_dynamic_properties(allocation)
    }
    fn record_property_read(&mut self, cell: crate::facts::EffectCell) {
        self.context.record_property_read(cell);
    }
    fn refused_spread(&mut self, source: &Values, span: Span) {
        self.context.refused_spread(self.frame, source, span);
    }
    fn incomplete_own_data_spread(&mut self, source: &Values, span: Span) {
        self.frame
            .effect_coverage
            .compose(&EffectCoverageFragment::default());
        self.context
            .incomplete_own_data_spread(self.frame, source, span);
    }
    fn spread_copy_recorded(&mut self, allocation: u32) {
        self.context.spread_copy_recorded(allocation);
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

fn read_binding_selected(
    context: &mut impl LocalContext,
    binding: &Id,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let result = context.read_binding(binding, frame)?;
    frame.effect_coverage.compose(&result.effects);
    judgment.compose(&result.allocation.judgment);
    let selection = result
        .selection
        .filter(|selection| selection.matches(&result.allocation.values));
    Some(completing_selected(
        SelectedValue {
            values: result.allocation.values,
            selection,
        },
        frame,
    ))
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
            let selection_dependencies = context.selection_prerequisites();
            let read = slot.read(context);
            let selection =
                CallableSelection::observed(&read.values, selection_dependencies.clone());
            context.record_property_read(read.cell);
            let mut effects = EffectCoverageFragment::lexical_operation();
            if module_prefix {
                effects.depend_on(selection_dependencies.as_ref());
            }
            Some(LocalValueResult {
                allocation: AllocationValue {
                    values: read.values,
                    judgment: AllocationJudgment::CoveredAllocationOnly(Default::default()),
                },
                effects,
                selection,
            })
        }
        Err(refusal) => context
            .unproved_field_read(frame, receiver, receivers, property, span, refusal)
            .map(|allocation| LocalValueResult {
                allocation,
                effects: EffectCoverageFragment::default(),
                selection: None,
            }),
    }
}

fn expression(
    context: &mut impl LocalContext,
    node: &LocalExpression,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    match node {
        LocalExpression::Value(node) => value_expression(context, node, frame, judgment),
        LocalExpression::PrimitiveLiteral(kind) => {
            // The canonical source literal cannot invoke code, access exterior
            // state or throw. Its value abstraction is independent evidence.
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::no_effects());
            Some(completing_selected(
                SelectedValue::raw(Values::from([AbstractValue::Value(Value::Prim(*kind))])),
                frame,
            ))
        }
        LocalExpression::This => {
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
            frame.this_values.clone().map(SelectedValue::raw)
        }
        LocalExpression::Record {
            allocation,
            properties,
        } => record_expression(context, allocation, properties, body, frame, judgment),
        LocalExpression::Array {
            allocation,
            elements,
        } => array_expression(context, allocation, elements, body, frame, judgment),
        LocalExpression::Sequence(expressions) => {
            sequence_expression(context, expressions, body, frame, judgment)
        }
        LocalExpression::Template {
            substitutions,
            span,
        } => template_expression(context, substitutions, *span, body, frame, judgment),
        LocalExpression::Binary {
            op,
            left,
            right,
            span,
        } => binary_expression(context, *op, (left, right), *span, body, frame, judgment),
        LocalExpression::Closure(callable) => {
            closure_expression(context, callable, frame, judgment)
        }
        LocalExpression::Assign {
            binding,
            value: node,
            span,
        } => assign_expression(context, binding, node, span, body, frame, judgment),
        LocalExpression::UpdateBinding { binding, span } => {
            update_binding_expression(context, binding, *span, body, frame, judgment)
        }
        LocalExpression::Call {
            callee,
            arguments,
            span,
        } => call_expression(context, callee, arguments, span, body, frame, judgment),
        LocalExpression::ReadField {
            receiver,
            property,
            span,
        } => read_field_expression(context, receiver, property, span, frame, judgment),
        LocalExpression::AssignField {
            receiver,
            property,
            value: node,
            span,
        } => assign_field_expression(
            &mut LocalValueContext { context, frame },
            receiver,
            property,
            node,
            span,
            body,
            judgment,
        ),
        LocalExpression::DefineThisField {
            property,
            value: node,
            ..
        } => define_this_field_expression(context, property, node, body, frame, judgment),
    }
}

fn binary_expression(
    context: &mut impl LocalContext,
    op: swc_ecma_ast::BinaryOp,
    (left, right): (&LocalExpression, &LocalExpression),
    span: Span,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let left = expression(context, left, body, frame, judgment)?;
    if left.values.is_empty() {
        return Some(left);
    }
    let right = expression(context, right, body, frame, judgment)?;
    if right.values.is_empty() {
        return Some(right);
    }

    let transfer = allocation_transfer::binary(
        op,
        &left.values,
        &right.values,
        AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
    );
    let Some(transfer) = transfer else {
        judgment.compose(&AllocationJudgment::Opaque);
        let mut reached = left.values;
        reached.extend(right.values);
        context.binary_fallback(frame, &reached, span);
        return None;
    };
    judgment.compose(&transfer.judgment);
    let effects = if transfer
        .values
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        // Allocation opacity does not certify effect or completion coverage.
        // A primitive operation such as Symbol coercion or BigInt division
        // can throw after both operands ran, so leave those domains unknown.
        EffectCoverageFragment::default()
    } else {
        EffectCoverageFragment::no_effects()
    };
    frame.effect_coverage.compose(&effects);
    Some(completing_selected(
        SelectedValue::raw(transfer.values),
        frame,
    ))
}

fn template_expression(
    context: &mut impl LocalContext,
    substitutions: &[LocalExpression],
    span: Span,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let string = Values::from([AbstractValue::Value(Value::Prim(PrimKinds::STRING))]);
    for substitution in substitutions {
        let operand = expression(context, substitution, body, frame, judgment)?;
        if operand.values.is_empty() {
            return Some(operand);
        }
        // String concatenation has the same primitive ToString result and
        // abrupt alternatives. Object conversion needs a separate string-hint
        // proof, so it retains the existing conservative fallback.
        let Some(transfer) = allocation_transfer::binary(
            swc_ecma_ast::BinaryOp::Add,
            &string,
            &operand.values,
            AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
        ) else {
            judgment.compose(&AllocationJudgment::Opaque);
            context.binary_fallback(frame, &operand.values, span);
            return None;
        };
        judgment.compose(&transfer.judgment);
        let effects = if transfer
            .values
            .contains(&AbstractValue::Value(Value::Unavailable))
        {
            EffectCoverageFragment::default()
        } else {
            EffectCoverageFragment::no_effects()
        };
        frame.effect_coverage.compose(&effects);
        if completing(transfer.values, frame).is_empty() {
            return Some(SelectedValue::raw(Values::new()));
        }
    }
    Some(completing_selected(SelectedValue::raw(string), frame))
}

fn value_expression(
    context: &mut impl LocalContext,
    node: &ValueExpr,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let selected = if let ValueExpr::Binding(binding) = node {
        read_binding_selected(context, binding, frame, judgment)?
    } else {
        let result = value(context, node, frame)?;
        judgment.compose(&result.judgment);
        SelectedValue::raw(result.values)
    };
    Some(completing_selected(selected, frame))
}

fn record_expression(
    context: &mut impl LocalContext,
    allocation: &u32,
    properties: &[PropertyDefinition<LocalExpression>],
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let layout = context.literal_layout(*allocation);
    if !matches!(&layout, LiteralLayout::SpreadCopy) {
        frame
            .effect_coverage
            .compose(&EffectCoverageFragment::default());
    }
    if !matches!(&layout, LiteralLayout::Record | LiteralLayout::SpreadCopy) {
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
            let selected = expression(context.context, node, body, context.frame, &mut judgment)?;
            // A later child can truncate the unfinished literal.
            context.frame.inputs.extend(selected.values.iter().cloned());
            // A child can reenter this allocation site. Its resulting
            // heap entry then also represents an older record.
            new_record &= !context.context.heap().properties.contains_key(allocation);
            Some(AllocationValue {
                values: selected.values,
                judgment,
            })
        },
    )?;
    judgment.compose(&result.judgment);
    mark_fresh_record(context, frame, *allocation, new_record, &result.values);
    Some(completing_selected(
        SelectedValue::raw(result.values),
        frame,
    ))
}

fn array_expression(
    context: &mut impl LocalContext,
    allocation: &u32,
    elements: &[Option<LocalExpression>],
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let result = allocation_transfer::array(
        &mut LocalValueContext { context, frame },
        *allocation,
        elements.iter().map(Option::as_ref),
        AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
        |context, node| {
            let mut judgment =
                AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default());
            let selected = expression(context.context, node, body, context.frame, &mut judgment)?;
            context.frame.inputs.extend(selected.values.iter().cloned());
            Some(AllocationValue {
                values: selected.values,
                judgment,
            })
        },
    )?;
    judgment.compose(&result.judgment);
    Some(completing_selected(
        SelectedValue::raw(result.values),
        frame,
    ))
}

fn sequence_expression(
    context: &mut impl LocalContext,
    expressions: &[LocalExpression],
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let mut selection = None;
    let result = allocation_transfer::sequence(
        &mut LocalValueContext { context, frame },
        expressions.iter(),
        AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
        |context, node| {
            let mut judgment =
                AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default());
            let selected = expression(context.context, node, body, context.frame, &mut judgment)?;
            selection = selected.selection;
            Some(AllocationValue {
                values: selected.values,
                judgment,
            })
        },
    )?;
    judgment.compose(&result.judgment);
    Some(completing_selected(
        SelectedValue {
            values: result.values,
            selection,
        },
        frame,
    ))
}

fn closure_expression(
    context: &mut impl LocalContext,
    callable: &DeferredCallableCreation,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let result = context.closure(frame, *callable)?;
    frame.effect_coverage.compose(&result.effects);
    judgment.compose(&result.allocation.judgment);
    Some(completing_selected(
        SelectedValue {
            values: result.allocation.values,
            selection: result.selection,
        },
        frame,
    ))
}

fn assign_expression(
    context: &mut impl LocalContext,
    binding: &Id,
    node: &LocalExpression,
    span: &Span,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    frame
        .effect_coverage
        .compose(&EffectCoverageFragment::default());
    let selected = expression(context, node, body, frame, judgment)?;
    store_binding(context, body, frame, judgment, binding, selected, *span)
}

fn store_binding(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
    binding: &Id,
    selected: SelectedValue,
    span: Span,
) -> Option<SelectedValue> {
    let values = selected.values;
    if values.is_empty() {
        return Some(SelectedValue::raw(values));
    }
    let Some(kind) = body.bindings.get(binding) else {
        frame
            .effect_coverage
            .compose(&EffectCoverageFragment::default());
        frame.binding_selections.remove(binding);
        let result = context.assign_capture(frame, binding, &values, span)?;
        judgment.compose(&result.judgment);
        return Some(completing_selected(
            SelectedValue::raw(result.values),
            frame,
        ));
    };
    if matches!(kind, LocalBinding::Const | LocalBinding::NamedSelf(_))
        || (*kind == LocalBinding::Parameter
            && frame.parameter_inputs.is_some()
            && !frame.initialized_bindings.contains(binding))
        || (*kind == LocalBinding::Let
            && frame
                .environment
                .get(binding)
                .expect("instantiated local binding")
                .contains(&AbstractValue::Value(Value::Unavailable)))
    {
        frame
            .effect_coverage
            .compose(&EffectCoverageFragment::default());
        frame
            .returns
            .insert(AbstractValue::Value(Value::Unavailable));
        return Some(SelectedValue::raw(Values::new()));
    }
    if matches!(kind, LocalBinding::Parameter | LocalBinding::Let)
        && frame.initialized_bindings.contains(binding)
    {
        // The adapter's declarative-cell proof already rejects live captured
        // cells. Assignment's earlier unknown fragment remains unknown.
        frame
            .effect_coverage
            .compose(&context.declarative_binding_operation(binding, frame));
    } else {
        frame
            .effect_coverage
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
    context.local_binding_stored(binding, frame);
    Some(SelectedValue {
        values,
        selection: frame.binding_selections.get(binding).cloned(),
    })
}

fn update_binding_expression(
    context: &mut impl LocalContext,
    binding: &Id,
    span: Span,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let operand = read_binding_selected(context, binding, frame, judgment)?;
    if operand.values.is_empty() {
        return Some(operand);
    }
    let Some(result) = allocation_transfer::update_result(&operand.values) else {
        judgment.compose(&AllocationJudgment::Opaque);
        frame
            .effect_coverage
            .compose(&EffectCoverageFragment::default());
        context.binary_fallback(frame, &operand.values, span);
        return None;
    };
    judgment.compose(&result.judgment);
    let effects = if result
        .values
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        EffectCoverageFragment::default()
    } else {
        EffectCoverageFragment::no_effects()
    };
    frame.effect_coverage.compose(&effects);
    let selected = completing_selected(SelectedValue::raw(result.values), frame);
    store_binding(context, body, frame, judgment, binding, selected, span)
}

fn call_expression(
    context: &mut impl LocalContext,
    callee: &LocalCallee,
    arguments: &[LocalExpression],
    span: &Span,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    // GetValue(callee) precedes every argument. In particular, an
    // argument assignment must not replace this captured callee.
    let callee = match callee {
        LocalCallee::Creation(callable) => {
            EvaluatedCallee::Direct(closure_expression(context, callable, frame, judgment)?)
        }
        LocalCallee::Binding(binding) => {
            EvaluatedCallee::Direct(read_binding_selected(context, binding, frame, judgment)?)
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
                return Some(SelectedValue::raw(Values::new()));
            }
            frame.inputs.extend(receivers.iter().cloned());
            let module_prefix = context.module_prefix_receiver(receiver, &receivers);
            let mut array_push = None;
            let result = if let Some((selection, effects)) =
                context.array_push_callee(&receivers, property)
            {
                array_push = Some(Box::new(selection));
                LocalValueResult {
                    allocation: AllocationValue::opaque(Values::new()),
                    effects,
                    selection: None,
                }
            } else {
                read_field(context, frame, receiver, &receivers, property, *span)?
            };
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
                array_push,
            }
        }
    };
    if callee.targets().is_empty()
        && !matches!(
            &callee,
            EvaluatedCallee::Member {
                array_push: Some(_),
                ..
            }
        )
    {
        return Some(SelectedValue::raw(Values::new()));
    }
    frame.inputs.extend(callee.targets().iter().cloned());
    let mut inputs = Vec::with_capacity(arguments.len());
    for argument in arguments.iter() {
        let selected = expression(context, argument, body, frame, judgment)?;
        if selected.values.is_empty() {
            return Some(SelectedValue::raw(Values::new()));
        }
        frame.inputs.extend(selected.values.iter().cloned());
        inputs.push(selected);
    }
    let result = context.call(frame, &callee, &inputs, *span)?;
    frame.effect_coverage.compose(&result.effects);
    judgment.compose(&result.allocation.judgment);
    Some(completing_selected(
        SelectedValue {
            values: result.allocation.values,
            selection: result.selection,
        },
        frame,
    ))
}

fn read_field_expression(
    context: &mut impl LocalContext,
    receiver: &Id,
    property: &str,
    span: &Span,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    if let Some(result) = context.native_field_read(frame, receiver, property, *span) {
        frame.effect_coverage.compose(&result.effects);
        judgment.compose(&result.allocation.judgment);
        return Some(completing_selected(
            SelectedValue {
                values: result.allocation.values,
                selection: result.selection,
            },
            frame,
        ));
    }
    let receivers = value(context, &ValueExpr::Binding(receiver.clone()), frame)?;
    judgment.compose(&receivers.judgment);
    let receivers = completing(receivers.values, frame);
    if receivers.is_empty() {
        return Some(SelectedValue::raw(Values::new()));
    }
    let result = read_field(context, frame, receiver, &receivers, property, *span)?;
    frame.effect_coverage.compose(&result.effects);
    judgment.compose(&result.allocation.judgment);
    Some(completing_selected(
        SelectedValue {
            values: result.allocation.values,
            selection: result.selection,
        },
        frame,
    ))
}

fn assign_field_expression(
    local: &mut LocalValueContext<'_, impl LocalContext>,
    receiver: &Id,
    property: &str,
    node: &LocalExpression,
    span: &Span,
    body: &LocalBody,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    // Capture the LHS reference before the RHS can replace its binding.
    let receivers = value(
        local.context,
        &ValueExpr::Binding(receiver.clone()),
        local.frame,
    )?;
    judgment.compose(&receivers.judgment);
    let receivers = completing(receivers.values, local.frame);
    if receivers.is_empty() {
        return Some(SelectedValue::raw(Values::new()));
    }
    // Capture the applicable module-prefix lane alongside the LHS
    // reference. The RHS may rebind the source cell, but JavaScript
    // has already evaluated the original receiver value.
    let module_prefix = local.context.module_prefix_receiver(receiver, &receivers);
    let selected = expression(local.context, node, body, local.frame, judgment)?;
    if selected.values.is_empty() {
        return Some(selected);
    }
    let slot = if module_prefix {
        own_data_transfer::resolve_writable_slot(local.context, &receivers, property)
    } else {
        own_data_field(local.context, &receivers, property, local.frame).ok()
    };
    let Some(slot) = slot else {
        local
            .context
            .unproved_field_write(local.frame, &receivers, &selected.values, *span);
        return None;
    };
    let writable = local.context.property_is_writable(slot.object, property);
    let write_dependencies = writable.then(|| local.context.selection_prerequisites());
    local.context.record_property_write(slot.cell());
    local
        .context
        .heap()
        .write_property(slot.object, Some(property.to_owned()), &selected.values);
    if writable {
        let mut effects = EffectCoverageFragment::lexical_operation();
        effects.depend_on(
            write_dependencies
                .as_ref()
                .expect("writable proof captures its prefix dependencies")
                .as_ref(),
        );
        local.frame.effect_coverage.compose(&effects);
    } else {
        local
            .frame
            .effect_coverage
            .compose(&EffectCoverageFragment::default());
    }
    Some(selected)
}

fn define_this_field_expression(
    context: &mut impl LocalContext,
    property: &str,
    node: &LocalExpression,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    frame
        .effect_coverage
        .compose(&EffectCoverageFragment::default());
    let selected = expression(context, node, body, frame, judgment)?;
    if selected.values.is_empty() {
        return Some(selected);
    }
    if selected
        .values
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        return None;
    }
    let this_values = frame.this_values.as_ref()?;
    let Some(AbstractValue::Value(Value::Alloc(Kind::Object, object))) = this_values.first() else {
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
        .write_property(*object, Some(property.to_owned()), &selected.values);
    Some(selected)
}
/// Syntax coverage comes from LocalBody's canonical lowering. The judgment
/// composes every reached operation, including values discarded by a sequence.
pub(super) fn execute(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) -> Option<LocalTransferResult> {
    let mut judgment = AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default());
    frame.return_selection = None;
    if let Some(arguments) = frame.parameter_inputs.clone() {
        // Later parameters are in TDZ while an earlier default executes.
        // Body declarations are instantiated only after this environment.
        for parameter in body.parameters.iter() {
            frame.environment.insert(
                parameter.binding.clone(),
                Values::from([AbstractValue::Value(Value::Unavailable)]),
            );
            frame.binding_selections.remove(&parameter.binding);
        }
        context.instantiate_parameter_bindings(frame);
        for (index, parameter) in body.parameters.iter().enumerate() {
            let mut selected = arguments
                .get(index)
                .cloned()
                .unwrap_or_else(|| SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
            if let Some(default) = &parameter.default
                && selected.values.remove(&AbstractValue::UNDEFINED)
            {
                let alternative = !selected.values.is_empty();
                let default = expression(context, default, body, frame, &mut judgment)?;
                if alternative {
                    // Abstract alternatives do not prove which prologue path
                    // completed. Keep every reached effect and value.
                    frame
                        .effect_coverage
                        .compose(&EffectCoverageFragment::default());
                    selected.values.extend(default.values);
                    selected.selection = None;
                } else {
                    selected = default;
                }
            }
            if selected.values.is_empty() {
                return Some(finish(frame, judgment));
            }
            let selection = selected
                .selection
                .filter(|selection| selection.matches(&selected.values));
            frame.inputs.extend(selected.values.iter().cloned());
            frame
                .environment
                .insert(parameter.binding.clone(), selected.values);
            if let Some(selection) = selection {
                frame
                    .binding_selections
                    .insert(parameter.binding.clone(), selection);
            } else {
                frame.binding_selections.remove(&parameter.binding);
            }
            context.local_binding_stored(&parameter.binding, frame);
            frame.initialized_bindings.insert(parameter.binding.clone());
            frame
                .effect_coverage
                .compose(&context.declarative_binding_operation(&parameter.binding, frame));
        }
    }
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
    context.instantiate_bindings(frame);
    for (binding, kind) in &body.bindings {
        if *kind == LocalBinding::Parameter && frame.initialized_bindings.contains(binding) {
            continue;
        }
        // Let/const cells have actually been created in TDZ above. Hoisted
        // values and compatibility parameters retain their separate boundary.
        let effects = if matches!(kind, LocalBinding::Let | LocalBinding::Const) {
            context.declarative_binding_operation(binding, frame)
        } else {
            EffectCoverageFragment::default()
        };
        frame.effect_coverage.compose(&effects);
    }
    for operation in body.operations.iter() {
        match operation {
            LocalOperation::Initialize(binding, node) => {
                let selected = match node {
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
                    None => SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])),
                };
                if selected.values.is_empty() {
                    return Some(finish(frame, judgment));
                }
                let SelectedValue { values, selection } = selected;
                let selection = selection.filter(|selection| selection.matches(&values));
                frame.environment.insert(binding.clone(), values);
                if let Some(selection) = selection {
                    frame.binding_selections.insert(binding.clone(), selection);
                } else {
                    frame.binding_selections.remove(binding);
                }
                context.local_binding_stored(binding, frame);
                frame.initialized_bindings.insert(binding.clone());
                frame
                    .effect_coverage
                    .compose(&context.declarative_binding_operation(binding, frame));
            }
            LocalOperation::Evaluate(node) => {
                if expression(context, node, body, frame, &mut judgment)?
                    .values
                    .is_empty()
                {
                    return Some(finish(frame, judgment));
                }
            }
            LocalOperation::Return(node) => {
                let selected = match node {
                    Some(node) => expression(context, node, body, frame, &mut judgment)?,
                    None => SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])),
                };
                let selection = selected
                    .selection
                    .filter(|selection| selection.matches(&selected.values));
                frame.returns.extend(selected.values);
                frame.return_selection = selection.filter(|selection| {
                    !frame
                        .returns
                        .contains(&AbstractValue::Value(Value::Unavailable))
                        && selection.matches(&frame.returns)
                });
                return Some(finish(frame, judgment));
            }
            LocalOperation::Throw(node) => {
                frame
                    .effect_coverage
                    .compose(&EffectCoverageFragment::default());
                frame.effect_coverage.abrupt();
                let selected = expression(context, node, body, frame, &mut judgment)?;
                frame.return_selection = None;
                context.publish_throw(&selected.values);
                frame
                    .returns
                    .insert(AbstractValue::Value(Value::Unavailable));
                return Some(finish(frame, judgment));
            }
        }
    }
    frame.return_selection = None;
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
