//! One ordered local-operation semantics for execution and provider queries.

use super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, ArrayElement, ArraySpreadResult,
    KeyedCollectionOperationResult, LiteralHeap, LiteralLayout,
};
use super::array_transfer::ArrayMethodSelection;
use super::descriptor_transfer::NativeObjectPostcondition;
use super::effect_coverage::{EffectCoverageFragment, NativeCalleeSelection};
use super::local_operations::{
    ClassConstructorBody, ClassDefinition, ClassFieldKey, ConstructorPhase,
    DeferredCallableCreation, LocalArrayElement, LocalBinding, LocalBody, LocalCall, LocalCallee,
    LocalExpression, LocalInvocation, LocalOperation, LocalRegion,
};
use super::own_data_transfer::{self, OwnDataContext, OwnDataOwnership, OwnDataSlot};
use super::property_inference::DescriptorAttributes;
use super::value_model::{
    AbstractValue, CallSite, Context, PrivateFieldId, PropertyDefinition, ValueExpr, Values,
};
use crate::analysis_inputs::{ForeignInvocation, ProofDependencies};
use crate::execution_evidence::{OrderedFieldReadRefusal, SkippedCallReason};
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

pub(super) struct LocalFrame {
    pub(super) environment: Context,
    pub(super) binding_selections: BTreeMap<Id, CallableSelection>,
    pub(super) binding_postconditions: BTreeMap<Id, Box<NativeObjectPostcondition>>,
    pub(super) return_selection: Option<CallableSelection>,
    /// Present only when the caller delegates the actual parameter prologue
    /// to this kernel. Compatibility frames are already bound.
    pub(super) parameter_inputs: Option<Vec<SelectedValue>>,
    /// Bindings initialized by an actual operation in this activation.
    /// Prebound values alone do not establish a declarative-cell receipt.
    pub(super) initialized_bindings: BTreeSet<Id>,
    pub(super) effect_coverage: EffectCoverageFragment,
    /// Entry arguments/captures and external values read while executing.
    /// Retained for a truncated remainder even after local aliases change.
    pub(super) inputs: Values,
    pub(super) borrowed_records: BTreeSet<u32>,
    pub(super) fresh_records: BTreeSet<u32>,
    pub(super) returns: Values,
    /// An actual source throw, separate from unknown abrupt/TDZ alternatives.
    pub(super) thrown: Option<Box<KnownThrow>>,
    /// Supplied by checked base construction or an actual lexical capture.
    /// Syntax alone never grants a receiver or the constructor proof below.
    pub(super) this_values: Option<Values>,
    constructor_receiver: Option<ConstructorReceiver>,
}

/// Per-activation descriptor/brand proof, separate from the pooled weak heap.
/// Only canonical base-class construction creates this ordinary extensible
/// receiver; unsupported exterior operations cannot preserve its prefix proof.
struct ConstructorReceiver {
    class: u32,
    public_fields: BTreeMap<String, DescriptorAttributes>,
    private_fields: BTreeSet<PrivateFieldId>,
}

impl LocalFrame {
    pub(super) fn new(environment: Context) -> Self {
        Self {
            inputs: environment.values().flatten().cloned().collect(),
            environment,
            binding_selections: BTreeMap::new(),
            binding_postconditions: BTreeMap::new(),
            return_selection: None,
            parameter_inputs: None,
            initialized_bindings: BTreeSet::new(),
            effect_coverage: EffectCoverageFragment::no_effects(),
            borrowed_records: BTreeSet::new(),
            fresh_records: BTreeSet::new(),
            returns: Values::new(),
            thrown: None,
            this_values: None,
            constructor_receiver: None,
        }
    }

    pub(super) fn constructor(body: &ClassConstructorBody) -> Self {
        let mut frame = Self::new(Context::new());
        frame.this_values = Some(Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            body.class,
        ))]));
        frame.constructor_receiver = Some(ConstructorReceiver {
            class: body.class,
            public_fields: BTreeMap::new(),
            private_fields: BTreeSet::new(),
        });
        frame
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
            AbstractValue::Closure(..)
                | AbstractValue::Value(Value::Alloc(Kind::Function | Kind::Class, _))
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

#[derive(Debug, Clone)]
pub(super) struct SelectedValue {
    pub(super) values: Values,
    pub(super) selection: Option<CallableSelection>,
    pub(super) postcondition: Option<Box<NativeObjectPostcondition>>,
}

#[derive(Debug, Clone)]
pub(super) struct KnownThrow {
    pub(super) value: SelectedValue,
    pub(super) frontier: Span,
}

enum RegionExit {
    Return(Box<SelectedValue>, Span),
    Throw(Box<KnownThrow>),
    Unknown,
}

impl SelectedValue {
    pub(super) fn raw(values: Values) -> Self {
        Self {
            values,
            selection: None,
            postcondition: None,
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
        array_method: Option<Box<ArrayMethodSelection>>,
    },
    Native {
        selection: Box<NativeCalleeSelection>,
        /// No application callable targets: the intrinsic identity is in its row.
        targets: Values,
    },
}

impl EvaluatedCallee {
    pub(super) fn targets(&self) -> &Values {
        match self {
            Self::Direct(targets) => &targets.values,
            Self::Member { targets, .. } => targets,
            Self::Native { targets, .. } => targets,
        }
    }

    pub(super) fn receiver(&self) -> Option<&Values> {
        match self {
            Self::Direct(_) => None,
            Self::Member { receiver, .. } => Some(receiver),
            Self::Native { .. } => None,
        }
    }
}

pub(super) struct LocalValueResult {
    pub(super) allocation: AllocationValue,
    pub(super) effects: EffectCoverageFragment,
    pub(super) selection: Option<CallableSelection>,
    pub(super) postcondition: Option<Box<NativeObjectPostcondition>>,
}

pub(super) struct LocalTransferResult {
    pub(super) allocation: AllocationJudgment,
    pub(super) effects: EffectCoverageFragment,
}

#[derive(Clone, Copy)]
pub(super) enum BindingOperation {
    Create,
    Initialize,
    Store,
}

pub(super) trait LocalContext: OwnDataContext {
    /// An actual initialized receiver. Execution consumers must record their
    /// conservative fallback when that operand is unavailable.
    fn receiver_value(&mut self, frame: &mut LocalFrame) -> Option<Values> {
        frame.this_values.clone()
    }
    fn constructor_phase(&mut self, _phase: ConstructorPhase) {}
    fn constructor_prefix_is_unexposed(&self, _class: u32) -> bool {
        false
    }
    /// A fresh receiver with no own field still needs its actual prototype
    /// chain proved absent at this assignment, after evaluating the RHS.
    fn constructor_assignment_absence(
        &mut self,
        _class: u32,
        _property: &str,
    ) -> Option<ProofDependencies> {
        None
    }
    /// Select an exact intrinsic before arguments run. A missing selection
    /// keeps the ordinary binding/member transfer and conservative fallback.
    fn native_callee(
        &mut self,
        _frame: &mut LocalFrame,
        _invocation: ForeignInvocation,
        _callee: &LocalCallee,
        _span: Span,
    ) -> Option<(NativeCalleeSelection, EffectCoverageFragment)> {
        None
    }
    /// An exact imported-member selection can carry a separate runtime
    /// identity/lookup premise. It covers both the receiver cell read and
    /// GetValue, before arguments execute; it grants no body effects.
    fn imported_member_callee(
        &mut self,
        _frame: &mut LocalFrame,
        _receiver: &Id,
        _property: &str,
        _span: Span,
    ) -> Option<(Values, LocalValueResult)> {
        None
    }
    fn array_method_callee(
        &mut self,
        _receivers: &Values,
        _property: &str,
        _span: Span,
    ) -> Option<(ArrayMethodSelection, EffectCoverageFragment)> {
        None
    }
    fn array_creation_started(&mut self, _allocation: u32) {}
    fn array_created(&mut self, _allocation: u32, _sparse: bool) {}
    fn array_allocation_is_fresh(&self, _allocation: u32) -> bool {
        false
    }
    fn array_spread(
        &mut self,
        _frame: &mut LocalFrame,
        _span: Span,
        _operand: &Values,
    ) -> Option<ArraySpreadResult> {
        None
    }
    fn primitive_array_created(&mut self, _allocation: u32, _dependencies: &ProofDependencies) {}
    fn instantiate_parameter_bindings(&mut self, _frame: &mut LocalFrame) {}
    /// Establish an immutable name environment from the actual invoked
    /// function value. A source key or provider prebinding alone is not proof.
    fn named_self_binding(
        &mut self,
        _binding: &Id,
        _frame: &LocalFrame,
    ) -> Option<EffectCoverageFragment> {
        None
    }
    /// Effects of actual declarative-cell creation, initialization or a checked
    /// mutable local store. Operand evaluation and captured stores are separate.
    fn declarative_binding_operation(
        &mut self,
        _binding: &Id,
        _frame: &LocalFrame,
        _operation: BindingOperation,
    ) -> EffectCoverageFragment {
        EffectCoverageFragment::default()
    }
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
    ) -> Option<LocalValueResult>;
    /// Operands have already been evaluated in source order. Return the value
    /// from that invocation's effect transfer; do not evaluate the call again.
    fn call(
        &mut self,
        frame: &mut LocalFrame,
        invocation: ForeignInvocation,
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
    /// A symbolic provider query can defer a checked own-data input read to
    /// its complete input-layout condition. Application execution never uses
    /// this as a substitute for a current-prefix slot proof.
    fn symbolic_field_read(
        &mut self,
        _receivers: &Values,
        _property: &str,
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
    /// Operands have already executed. Summarize possible coercion code from
    /// these retained values without evaluating either operand again.
    fn unproved_operator(&mut self, _frame: &mut LocalFrame, _operands: &Values, _span: Span) {}
    /// An optional callee was selected, but its non-nullish source identity
    /// was not proved. Arguments have not run. Application execution must
    /// summarize the remainder without treating the call as unconditional.
    fn unproved_optional_call(&mut self, _frame: &LocalFrame, _callee: &Values, _span: Span) {}
    fn publish_throw(&mut self, values: &Values);
    /// Called only after a represented linear region has stopped at a known
    /// completion, or its handler was bypassed. No syntax-order heuristic.
    fn skipped_region(
        &mut self,
        _frame: &mut LocalFrame,
        _region: Span,
        _frontier: Span,
        _reason: SkippedCallReason,
    ) -> bool {
        false
    }
    fn instantiate_catch_binding(&mut self, _binding: &Id, _frame: &LocalFrame) {}
    fn leave_catch_binding(&mut self, _binding: &Id) {}
    fn unproved_control_flow(&mut self, _frame: &LocalFrame, _span: Span) {}
    fn keyed_collection_new(
        &mut self,
        _frame: &mut LocalFrame,
        _callee: &ValueExpr,
        _arguments: &[ValueExpr],
        _site: CallSite,
    ) -> Option<KeyedCollectionOperationResult> {
        None
    }
    fn set_construction(
        &mut self,
        _frame: &mut LocalFrame,
        _source: &ValueExpr,
        _argument: &SelectedValue,
        _site: CallSite,
    ) -> Option<LocalValueResult> {
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
    fn array_creation_started(&mut self, allocation: u32) {
        self.context.array_creation_started(allocation);
    }
    fn array_created(&mut self, allocation: u32, sparse: bool) {
        self.context.array_created(allocation, sparse);
    }
    fn array_allocation_is_fresh(&self, allocation: u32) -> bool {
        self.context.array_allocation_is_fresh(allocation)
    }
    fn array_spread(&mut self, span: Span, operand: &Values) -> Option<ArraySpreadResult> {
        let spread = self.context.array_spread(self.frame, span, operand)?;
        self.frame.effect_coverage.compose(
            &EffectCoverageFragment::ordered_native_iterable_protocol(&spread.dependencies),
        );
        Some(spread)
    }
    fn primitive_array_created(&mut self, allocation: u32, dependencies: &ProofDependencies) {
        self.context
            .primitive_array_created(allocation, dependencies);
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
    fn operation_effects(&mut self, effects: EffectCoverageFragment) {
        self.frame.effect_coverage.compose(&effects);
    }
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue> {
        value(self.context, expression, self.frame)
    }
    fn noninvoking_operation(&mut self) {
        self.frame
            .effect_coverage
            .compose(&EffectCoverageFragment::no_effects());
    }
    fn literal_creation_operation(&mut self, covered: bool) {
        let effects = if covered {
            EffectCoverageFragment::no_effects()
        } else {
            EffectCoverageFragment::default()
        };
        self.frame.effect_coverage.compose(&effects);
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
    ) -> Option<KeyedCollectionOperationResult> {
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
    if values.remove(&AbstractValue::Value(Value::Unavailable)) && frame.thrown.is_none() {
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
    if selected
        .postcondition
        .as_ref()
        .is_some_and(|proof| !proof.matches(&selected.values))
    {
        selected.postcondition = None;
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
    let postcondition = result
        .postcondition
        .or_else(|| frame.binding_postconditions.get(binding).cloned())
        .filter(|proof| proof.matches(&result.allocation.values));
    Some(completing_selected(
        SelectedValue {
            values: result.allocation.values,
            selection,
            postcondition,
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
    let slot = own_data_transfer::resolve_present_read(
        context,
        receivers,
        property,
        OwnDataOwnership::CurrentPrefix,
    );
    match slot {
        Ok(slot) => {
            let selection_dependencies = context.selection_prerequisites();
            let read = slot.read(context);
            let selection =
                CallableSelection::observed(&read.values, selection_dependencies.clone());
            context.record_property_read(read.cell);
            let mut effects = EffectCoverageFragment::modeled_own_data_access();
            effects.depend_on(selection_dependencies.as_ref());
            Some(LocalValueResult {
                allocation: AllocationValue {
                    values: read.values,
                    judgment: AllocationJudgment::CoveredAllocationOnly(Default::default()),
                },
                effects,
                selection,
                postcondition: None,
            })
        }
        Err(refusal) => context
            .symbolic_field_read(receivers, property)
            .or_else(|| {
                context
                    .unproved_field_read(frame, receiver, receivers, property, span, refusal)
                    .map(|allocation| LocalValueResult {
                        allocation,
                        effects: EffectCoverageFragment::default(),
                        selection: None,
                        postcondition: None,
                    })
            }),
    }
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
        frame.binding_selections.remove(binding);
        let result = context.assign_capture(frame, binding, &values, span)?;
        frame.effect_coverage.compose(&result.effects);
        judgment.compose(&result.allocation.judgment);
        return Some(completing_selected(
            SelectedValue::raw(result.allocation.values),
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
    // This PutValue reaches an initialized mutable declarative cell.
    // The adapter independently covers this cell operation;
    // evaluating its RHS has already composed its own effects.
    frame
        .effect_coverage
        .compose(&context.declarative_binding_operation(binding, frame, BindingOperation::Store));
    frame.environment.insert(binding.clone(), values.clone());
    if let Some(selection) = selected
        .selection
        .filter(|selection| selection.matches(&values))
    {
        frame.binding_selections.insert(binding.clone(), selection);
    } else {
        frame.binding_selections.remove(binding);
    }
    frame.binding_postconditions.remove(binding);
    context.local_binding_stored(binding, frame);
    Some(SelectedValue {
        values,
        selection: frame.binding_selections.get(binding).cloned(),
        postcondition: None,
    })
}

fn expression(
    context: &mut impl LocalContext,
    node: &LocalExpression,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    match node {
        LocalExpression::Binary {
            operator,
            left,
            right,
            span,
        } => binary_expression(
            &mut LocalValueContext { context, frame },
            operator,
            left,
            right,
            span,
            body,
            judgment,
        ),
        LocalExpression::Template {
            substitutions,
            span,
        } => template_expression(context, substitutions, span, body, frame, judgment),
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
        LocalExpression::ClassDefinition(definition) => {
            let result = define_class(definition);
            judgment.compose(&result.allocation.judgment);
            frame.effect_coverage.compose(&result.effects);
            Some(SelectedValue::raw(result.allocation.values))
        }
        LocalExpression::This => {
            let values = context.receiver_value(frame)?;
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::no_effects());
            Some(SelectedValue::raw(values))
        }
        LocalExpression::Record {
            allocation,
            properties,
        } => record_expression(context, allocation, properties, body, frame, judgment),
        LocalExpression::Array {
            allocation,
            elements,
        } => array_expression(context, allocation, elements, body, frame, judgment),
        LocalExpression::SetConstruction {
            array,
            source,
            site,
        } => set_construction_expression(context, array, source, site, body, frame, judgment),
        LocalExpression::Sequence(expressions) => {
            sequence_expression(context, expressions, body, frame, judgment)
        }
        LocalExpression::Closure(callable) => {
            closure_expression(context, callable, frame, judgment)
        }
        LocalExpression::Assign {
            binding,
            value: node,
            span,
        } => assign_expression(context, binding, node, span, body, frame, judgment),
        LocalExpression::UpdateBinding { binding, span } => {
            update_binding_expression(context, binding, span, body, frame, judgment)
        }
        LocalExpression::Call(call) => call_expression(context, call, body, frame, judgment),
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
        LocalExpression::AssignPrivateThisField { property, value } => {
            assign_private_this_field_expression(context, property, value, body, frame, judgment)
        }
        LocalExpression::AssignPublicThisField { property, value } => {
            assign_public_this_field_expression(context, property, value, body, frame, judgment)
        }
    }
}

fn binary_expression(
    local: &mut LocalValueContext<'_, impl LocalContext>,
    operator: &swc_ecma_ast::BinaryOp,
    left: &LocalExpression,
    right: &LocalExpression,
    span: &Span,
    body: &LocalBody,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let left = expression(local.context, left, body, local.frame, judgment)?;
    if left.values.is_empty() {
        return Some(left);
    }
    let right = expression(local.context, right, body, local.frame, judgment)?;
    if right.values.is_empty() {
        return Some(right);
    }
    let values = match allocation_transfer::binary_result(*operator, &left.values, &right.values) {
        allocation_transfer::PrimitiveOperationResult::Covered(values) => {
            local
                .frame
                .effect_coverage
                .compose(&EffectCoverageFragment::no_effects());
            values
        }
        allocation_transfer::PrimitiveOperationResult::MayThrow(values) => {
            *judgment = AllocationJudgment::Opaque;
            local
                .frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
            values
        }
        allocation_transfer::PrimitiveOperationResult::CoercionRequired => {
            let mut operands = left.values;
            operands.extend(right.values);
            local
                .frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
            local
                .context
                .unproved_operator(local.frame, &operands, *span);
            return None;
        }
    };
    Some(completing_selected(SelectedValue::raw(values), local.frame))
}

fn template_expression(
    context: &mut impl LocalContext,
    substitutions: &[LocalExpression],
    span: &Span,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    for substitution in substitutions {
        let operand = expression(context, substitution, body, frame, judgment)?;
        if operand.values.is_empty() {
            return Some(operand);
        }
        // ToString happens before evaluating the following substitution.
        // A primitive String result never erases operand effects or throws.
        let values = match allocation_transfer::template_substitution_result(&operand.values) {
            allocation_transfer::PrimitiveOperationResult::Covered(values) => {
                frame
                    .effect_coverage
                    .compose(&EffectCoverageFragment::no_effects());
                values
            }
            allocation_transfer::PrimitiveOperationResult::MayThrow(values) => {
                *judgment = AllocationJudgment::Opaque;
                frame
                    .effect_coverage
                    .compose(&EffectCoverageFragment::default());
                values
            }
            allocation_transfer::PrimitiveOperationResult::CoercionRequired => {
                frame
                    .effect_coverage
                    .compose(&EffectCoverageFragment::default());
                context.unproved_operator(frame, &operand.values, *span);
                return None;
            }
        };
        let converted = completing_selected(SelectedValue::raw(values), frame);
        if converted.values.is_empty() {
            return Some(converted);
        }
    }
    frame
        .effect_coverage
        .compose(&EffectCoverageFragment::no_effects());
    Some(completing_selected(
        SelectedValue::raw(Values::from([AbstractValue::Value(Value::Prim(
            PrimKinds::STRING,
        ))])),
        frame,
    ))
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
    elements: &[LocalArrayElement],
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let result = allocation_transfer::array(
        &mut LocalValueContext { context, frame },
        *allocation,
        elements.iter().map(|element| match element {
            LocalArrayElement::Hole => ArrayElement::Hole,
            LocalArrayElement::Value(value) => ArrayElement::Value(value),
            LocalArrayElement::Spread { value, span } => ArrayElement::Spread(value, *span),
        }),
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

fn set_construction_expression(
    context: &mut impl LocalContext,
    array: &LocalExpression,
    source: &ValueExpr,
    site: &CallSite,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let argument = expression(context, array, body, frame, judgment)?;
    if argument.values.is_empty() {
        return Some(completing_selected(argument, frame));
    }
    frame.inputs.extend(argument.values.iter().cloned());
    let result = context.set_construction(frame, source, &argument, *site)?;
    judgment.compose(&result.allocation.judgment);
    frame.effect_coverage.compose(&result.effects);
    frame
        .inputs
        .extend(result.allocation.values.iter().cloned());
    Some(completing_selected(
        SelectedValue::raw(result.allocation.values),
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
            postcondition: None,
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
            postcondition: result.postcondition,
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
    let selected = expression(context, node, body, frame, judgment)?;
    store_binding(context, body, frame, judgment, binding, selected, *span)
}

fn update_binding_expression(
    context: &mut impl LocalContext,
    binding: &Id,
    span: &Span,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let operand = read_binding_selected(context, binding, frame, judgment)?;
    if operand.values.is_empty() {
        return Some(operand);
    }
    let values = match allocation_transfer::update_result(&operand.values) {
        allocation_transfer::PrimitiveOperationResult::Covered(values) => {
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::no_effects());
            values
        }
        allocation_transfer::PrimitiveOperationResult::MayThrow(values) => {
            *judgment = AllocationJudgment::Opaque;
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
            values
        }
        allocation_transfer::PrimitiveOperationResult::CoercionRequired => {
            frame
                .effect_coverage
                .compose(&EffectCoverageFragment::default());
            context.unproved_operator(frame, &operand.values, *span);
            return None;
        }
    };
    // Both the returned old numeric value and stored new numeric
    // value have these kinds; exact numbers are outside this domain.
    let selected = completing_selected(SelectedValue::raw(values), frame);
    store_binding(context, body, frame, judgment, binding, selected, *span)
}

fn call_expression(
    context: &mut impl LocalContext,
    call: &LocalCall,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let LocalCall {
        invocation,
        callee,
        arguments,
        span,
    } = call;
    let optional = matches!(invocation, LocalInvocation::OptionalCall);
    let invocation = invocation.foreign_invocation();
    // GetValue(callee) precedes every argument. In particular, an
    // argument assignment must not replace this captured callee.
    let callee = if let Some((selection, effects)) =
        context.native_callee(frame, invocation, callee, *span)
    {
        frame.effect_coverage.compose(&effects);
        EvaluatedCallee::Native {
            selection: Box::new(selection),
            targets: Values::new(),
        }
    } else {
        match callee {
            LocalCallee::Creation(creation) => {
                EvaluatedCallee::Direct(closure_expression(context, creation, frame, judgment)?)
            }
            LocalCallee::Binding(binding) => {
                EvaluatedCallee::Direct(read_binding_selected(context, binding, frame, judgment)?)
            }
            LocalCallee::Member {
                receiver,
                property,
                span: member_span,
            } => {
                let mut array_method = None;
                let (receivers, result, selection_dependencies) = if let Some((receivers, result)) =
                    context.imported_member_callee(frame, receiver, property, *member_span)
                {
                    (receivers, result, None)
                } else {
                    let result = value(context, &ValueExpr::Binding(receiver.clone()), frame)?;
                    judgment.compose(&result.judgment);
                    let receivers = completing(result.values, frame);
                    if receivers.is_empty() {
                        return Some(SelectedValue::raw(Values::new()));
                    }
                    let module_prefix = context.module_prefix_receiver(receiver, &receivers);
                    let result = if let Some((selection, effects)) = (invocation
                        == ForeignInvocation::Call)
                        .then(|| context.array_method_callee(&receivers, property, *span))
                        .flatten()
                    {
                        array_method = Some(Box::new(selection));
                        LocalValueResult {
                            allocation: AllocationValue::opaque(Values::new()),
                            effects,
                            selection: None,
                            postcondition: None,
                        }
                    } else {
                        read_field(context, frame, receiver, &receivers, property, *member_span)?
                    };
                    let selection_dependencies = module_prefix
                        .then(|| result.effects.lexical_selection_dependencies())
                        .flatten();
                    (receivers, result, selection_dependencies)
                };
                frame.inputs.extend(receivers.iter().cloned());
                // Selection/GetValue is an executed lexical operation and
                // is composed now, before any argument can throw or mutate
                // the selected property.
                frame.effect_coverage.compose(&result.effects);
                judgment.compose(&result.allocation.judgment);
                EvaluatedCallee::Member {
                    receiver: receivers,
                    targets: completing(result.allocation.values, frame),
                    selection_dependencies,
                    array_method,
                }
            }
        }
    };
    if callee.targets().is_empty()
        && !matches!(
            &callee,
            EvaluatedCallee::Native { .. }
                | EvaluatedCallee::Member {
                    array_method: Some(_),
                    ..
                }
        )
    {
        return Some(SelectedValue::raw(Values::new()));
    }
    frame.inputs.extend(callee.targets().iter().cloned());
    if optional
        && !matches!(&callee,
            EvaluatedCallee::Direct(selected)
                if selected.values.len() == 1
                    && matches!(selected.values.first(),
                        Some(AbstractValue::Closure(..)
                            | AbstractValue::Value(Value::Alloc(Kind::Function, _)))))
    {
        // This bounded lane proves that no short-circuit branch exists.
        // Nullish, mixed and unknown targets need skipped-call coverage;
        // they must not execute arguments or acquire an invocation receipt.
        context.unproved_optional_call(frame, callee.targets(), *span);
        return None;
    }
    let mut inputs = Vec::with_capacity(arguments.len());
    for argument in arguments.iter() {
        let selected = expression(context, argument, body, frame, judgment)?;
        if selected.values.is_empty() {
            return Some(SelectedValue::raw(Values::new()));
        }
        frame.inputs.extend(selected.values.iter().cloned());
        inputs.push(selected);
    }
    let result = context.call(frame, invocation, &callee, &inputs, *span)?;
    frame.effect_coverage.compose(&result.effects);
    judgment.compose(&result.allocation.judgment);
    Some(completing_selected(
        SelectedValue {
            values: result.allocation.values,
            selection: result.selection,
            postcondition: result.postcondition,
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
                postcondition: result.postcondition,
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
            postcondition: result.postcondition,
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
        let mut effects = EffectCoverageFragment::modeled_own_data_access();
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

/// Class creation allocates an unpublished constructor/prototype and installs
/// statically named methods. Field initializers, parameter defaults and bodies
/// remain deferred; the canonical definition plan excludes eager user steps.
/// This grants neither ordinary-object shape nor constructor-call coverage.
pub(super) fn define_class(definition: &ClassDefinition) -> LocalValueResult {
    LocalValueResult {
        allocation: AllocationValue {
            values: Values::from([AbstractValue::Value(Value::Alloc(
                Kind::Class,
                definition.class,
            ))]),
            judgment: AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
        },
        effects: EffectCoverageFragment::no_effects(),
        selection: None,
        postcondition: None,
    }
}

fn constructor_receiver<'a>(
    context: &impl LocalContext,
    frame: &'a mut LocalFrame,
    judgment: &AllocationJudgment,
) -> Option<&'a mut ConstructorReceiver> {
    if !matches!(judgment, AllocationJudgment::CoveredAllocationOnly(_))
        && !(frame
            .constructor_receiver
            .as_ref()
            .is_some_and(|receiver| context.constructor_prefix_is_unexposed(receiver.class))
            && frame.effect_coverage.closed_effect_dependencies().is_some())
    {
        return None;
    }
    let receiver = frame.constructor_receiver.as_mut()?;
    let expected = Values::from([AbstractValue::Value(Value::Alloc(
        Kind::Object,
        receiver.class,
    ))]);
    (frame.this_values.as_ref() == Some(&expected)).then_some(receiver)
}

fn define_this_field_expression(
    context: &mut impl LocalContext,
    property: &ClassFieldKey,
    node: &LocalExpression,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
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
    let receiver = constructor_receiver(context, frame, judgment)?;
    let object = receiver.class;
    match property {
        ClassFieldKey::Public(property) => {
            // DefineField uses CreateDataPropertyOrThrow, not [[Set]]. A
            // fresh base receiver is extensible and every earlier public
            // definition here remains configurable; inherited setters are
            // never consulted, including the name __proto__.
            if receiver
                .public_fields
                .get(property)
                .is_some_and(|attributes| attributes.configurable != Some(true))
            {
                return None;
            }
            receiver
                .public_fields
                .insert(property.clone(), DescriptorAttributes::literal(true));
            context
                .heap()
                .write_property(object, Some(property.clone()), &selected.values);
            context.record_property_write(crate::facts::EffectCell::ObjectShape(object));
            context.record_property_write(crate::facts::EffectCell::OwnProperty {
                origin: object,
                property: property.clone(),
            });
        }
        ClassFieldKey::Private(property) => {
            context.record_property_write(crate::facts::EffectCell::PrivateSlot {
                origin: object,
                class: object,
                name: property.clone(),
            });
            let property = PrivateFieldId {
                class: object,
                name: property.clone(),
            };
            // PrivateFieldAdd throws on a duplicate brand/slot. Canonical
            // definitions establish its absence in this fresh activation.
            // This ordinary receiver is not host-exotic, so the host's
            // private-element hook must complete normally without other steps.
            if !receiver.private_fields.insert(property.clone()) {
                return None;
            }
            context
                .heap()
                .write_private_property(object, property, &selected.values)?;
        }
    }
    frame
        .effect_coverage
        .compose(&EffectCoverageFragment::modeled_own_data_access());
    Some(selected)
}

fn assign_public_this_field_expression(
    context: &mut impl LocalContext,
    property: &str,
    node: &LocalExpression,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    // Capture the receiver reference before the RHS. Resolve [[Set]]'s own
    // descriptor after the RHS using the same uninterrupted fresh prefix.
    let object = constructor_receiver(context, frame, judgment)?.class;
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
    let receiver = constructor_receiver(context, frame, judgment)?;
    if receiver.class != object {
        return None;
    }
    let existing = receiver.public_fields.get(property);
    let absence = if existing.is_none() {
        Some(context.constructor_assignment_absence(object, property)?)
    } else {
        None
    };
    if existing.is_some_and(|attributes| attributes.writable != Some(true)) {
        return None;
    }
    if let Some(dependencies) = &absence {
        receiver
            .public_fields
            .insert(property.to_owned(), DescriptorAttributes::literal(true));
        let mut effects = EffectCoverageFragment::modeled_own_data_access();
        effects.depend_on(dependencies);
        frame.effect_coverage.compose(&effects);
        context.record_property_write(crate::facts::EffectCell::ObjectShape(object));
    }
    // Existing fields use their actual own descriptor. A new field requires
    // the separate whole-prototype absence proof above; a private slot alone
    // cannot bypass [[Set]] or inherited setters.
    context
        .heap()
        .write_property(object, Some(property.to_owned()), &selected.values);
    context.record_property_write(crate::facts::EffectCell::OwnProperty {
        origin: object,
        property: property.to_owned(),
    });
    frame
        .effect_coverage
        .compose(&EffectCoverageFragment::modeled_own_data_access());
    Some(selected)
}

fn assign_private_this_field_expression(
    context: &mut impl LocalContext,
    property: &str,
    node: &LocalExpression,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
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
    let receiver = constructor_receiver(context, frame, judgment)?;
    let object = receiver.class;
    let property = PrivateFieldId {
        class: object,
        name: property.to_owned(),
    };
    // This is PrivateSet on an existing field, never private method/accessor
    // dispatch. Definition order is a runtime prefix proof, not a name census.
    if !receiver.private_fields.contains(&property) {
        return None;
    }
    context.record_property_write(crate::facts::EffectCell::PrivateSlot {
        origin: object,
        class: property.class,
        name: property.name.clone(),
    });
    context
        .heap()
        .write_private_property(object, property, &selected.values)?;
    frame
        .effect_coverage
        .compose(&EffectCoverageFragment::modeled_own_data_access());
    Some(selected)
}

/// Base receivers initialize instance fields before FunctionDeclarationInstantiation.
/// Defaults remain outside the canonical constructor plan; each simple parameter
/// still receives actual creation/initialization operations in the body phase.
pub(super) fn execute_constructor(
    context: &mut impl LocalContext,
    body: &ClassConstructorBody,
    arguments: Vec<SelectedValue>,
    frame: &mut LocalFrame,
) -> Option<LocalTransferResult> {
    if frame.constructor_receiver.as_ref()?.class != body.class
        || frame.parameter_inputs.is_some()
        || !frame.environment.is_empty()
    {
        return None;
    }
    context.constructor_phase(ConstructorPhase::Initializers);
    let initializers = execute(context, &body.initializers, frame)?;
    if (!matches!(
        initializers.allocation,
        AllocationJudgment::CoveredAllocationOnly(_)
    ) && !(context.constructor_prefix_is_unexposed(body.class)
        && initializers.effects.closed_effect_dependencies().is_some()))
        || frame.returns != Values::from([AbstractValue::UNDEFINED])
    {
        return None;
    }
    frame.returns = Values::new();
    frame.parameter_inputs = Some(arguments);
    context.constructor_phase(ConstructorPhase::Body);
    let mut result = execute(context, &body.constructor, frame)?;
    result.allocation.compose(&initializers.allocation);
    Some(result)
}
// Prologue state is gone before the body invokes another callable. Defaults
// still execute through the same ordered expression transfers.
fn initialize_parameters(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<std::ops::ControlFlow<()>> {
    let arguments = frame
        .parameter_inputs
        .clone()
        .expect("selected invocation parameters");
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
    for parameter in body.parameters.iter() {
        frame
            .effect_coverage
            .compose(&context.declarative_binding_operation(
                &parameter.binding,
                frame,
                BindingOperation::Create,
            ));
    }
    for (index, parameter) in body.parameters.iter().enumerate() {
        let mut selected = arguments
            .get(index)
            .cloned()
            .unwrap_or_else(|| SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
        if let Some(default) = &parameter.default
            && selected.values.remove(&AbstractValue::UNDEFINED)
        {
            let alternative = !selected.values.is_empty();
            let default = expression(context, default, body, frame, judgment)?;
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
            return Some(std::ops::ControlFlow::Break(()));
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
            .compose(&context.declarative_binding_operation(
                &parameter.binding,
                frame,
                BindingOperation::Initialize,
            ));
    }
    Some(std::ops::ControlFlow::Continue(()))
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
    // A named function's immutable self binding exists before parameter
    // defaults. Preserve the selected callee's original closure identity.
    for (binding, kind) in &body.bindings {
        if let LocalBinding::NamedSelf(function) = kind
            && frame.environment.get(binding).is_some_and(|values| {
                values.len() == 1
                    && matches!(values.first(),
                        Some(AbstractValue::Closure(key, _)
                        | AbstractValue::Value(Value::Alloc(Kind::Function, key)))
                            if key == function)
            })
            && let Some(effects) = context.named_self_binding(binding, frame)
        {
            if let Some(selection) =
                effects
                    .lexical_selection_dependencies()
                    .and_then(|dependencies| {
                        CallableSelection::observed(&frame.environment[binding], dependencies)
                    })
            {
                frame.binding_selections.insert(binding.clone(), selection);
            }
            frame.initialized_bindings.insert(binding.clone());
            frame.effect_coverage.compose(&effects);
        }
    }
    if frame.parameter_inputs.is_some()
        && initialize_parameters(context, body, frame, &mut judgment)?.is_break()
    {
        return Some(finish(frame, judgment));
    }
    for (binding, kind) in &body.bindings {
        match kind {
            LocalBinding::Parameter => {}
            LocalBinding::CatchParameter => {
                frame.environment.remove(binding);
                frame.binding_selections.remove(binding);
            }
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
        if *kind == LocalBinding::CatchParameter {
            continue;
        }
        if matches!(kind, LocalBinding::Parameter | LocalBinding::NamedSelf(_))
            && frame.initialized_bindings.contains(binding)
        {
            continue;
        }
        // Let/const cells have actually been created in TDZ above. Hoisted
        // values and compatibility parameters retain their separate boundary.
        let effects = if matches!(kind, LocalBinding::Let | LocalBinding::Const) {
            context.declarative_binding_operation(binding, frame, BindingOperation::Create)
        } else {
            EffectCoverageFragment::default()
        };
        frame.effect_coverage.compose(&effects);
    }
    let completion = execute_operations(context, body, &body.operations, frame, &mut judgment)?;
    // Source throws and structured handlers need the complete callable's call
    // inventory, including a source suffix omitted by canonical lowering.
    if body.operations.iter().any(|operation| {
        matches!(
            operation,
            LocalOperation::Throw(..) | LocalOperation::TryCatch { .. }
        )
    }) && let Some(region) = body.source_span
    {
        let frontier = match &completion {
            std::ops::ControlFlow::Break(RegionExit::Return(_, span)) => {
                Some((*span, SkippedCallReason::KnownReturn))
            }
            std::ops::ControlFlow::Break(RegionExit::Throw(thrown)) => {
                Some((thrown.frontier, SkippedCallReason::KnownThrow))
            }
            _ => None,
        };
        if let Some((frontier, reason)) = frontier
            && !context.skipped_region(frame, region, frontier, reason)
        {
            context.unproved_control_flow(frame, region);
            return None;
        }
    }
    match completion {
        std::ops::ControlFlow::Continue(()) => {
            frame.return_selection = None;
            frame.returns.insert(AbstractValue::UNDEFINED);
        }
        std::ops::ControlFlow::Break(RegionExit::Return(selected, _)) => {
            frame.returns.extend(selected.values);
            frame.return_selection = selected
                .selection
                .filter(|selection| selection.matches(&frame.returns));
        }
        std::ops::ControlFlow::Break(RegionExit::Throw(thrown)) => frame.thrown = Some(thrown),
        std::ops::ControlFlow::Break(RegionExit::Unknown) => {
            frame
                .returns
                .insert(AbstractValue::Value(Value::Unavailable));
        }
    }
    Some(finish(frame, judgment))
}

fn abrupt_exit(frame: &mut LocalFrame) -> std::ops::ControlFlow<RegionExit> {
    std::ops::ControlFlow::Break(match frame.thrown.take() {
        Some(thrown) if frame.returns.is_empty() => RegionExit::Throw(thrown),
        _ => RegionExit::Unknown,
    })
}

fn execute_operations(
    context: &mut impl LocalContext,
    body: &LocalBody,
    operations: &[LocalOperation],
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<std::ops::ControlFlow<RegionExit>> {
    use std::ops::ControlFlow::{Break, Continue};
    for operation in operations {
        match operation {
            LocalOperation::Initialize(binding, node) => {
                let selected = match node {
                    Some(node) => expression(context, node, body, frame, judgment)?,
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
                    return Some(abrupt_exit(frame));
                }
                let SelectedValue {
                    values,
                    selection,
                    postcondition,
                } = selected;
                if matches!(body.bindings[binding], LocalBinding::Const)
                    && let Some(proof) = postcondition.filter(|proof| proof.matches(&values))
                {
                    frame.binding_postconditions.insert(binding.clone(), proof);
                } else {
                    frame.binding_postconditions.remove(binding);
                }
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
                    .compose(&context.declarative_binding_operation(
                        binding,
                        frame,
                        BindingOperation::Initialize,
                    ));
            }
            LocalOperation::Evaluate(node) => {
                if expression(context, node, body, frame, judgment)?
                    .values
                    .is_empty()
                {
                    return Some(abrupt_exit(frame));
                }
            }
            LocalOperation::Return(node, span) => {
                let selected = match node {
                    Some(node) => expression(context, node, body, frame, judgment)?,
                    None => SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])),
                };
                if selected.values.is_empty() {
                    return Some(abrupt_exit(frame));
                }
                return Some(Break(RegionExit::Return(Box::new(selected), *span)));
            }
            LocalOperation::Throw(node, span) => {
                let selected = expression(context, node, body, frame, judgment)?;
                if selected.values.is_empty() {
                    return Some(abrupt_exit(frame));
                }
                frame.effect_coverage.abrupt();
                // Preserve the conservative escape policy until throw-value
                // reach is also represented at every nonlocal invocation boundary.
                context.publish_throw(&selected.values);
                return Some(Break(RegionExit::Throw(Box::new(KnownThrow {
                    value: selected,
                    frontier: *span,
                }))));
            }
            LocalOperation::TryCatch {
                body: region,
                binding,
                handler,
            } => {
                let completion =
                    execute_try_catch(context, body, region, binding, handler, frame, judgment)?;
                if completion.is_break() {
                    return Some(completion);
                }
            }
        }
    }
    Some(Continue(()))
}

// Keep handler environments and completion routing out of ordinary deep
// invocation frames; their payloads are carried by boxed completion values.
#[inline(never)]
fn execute_try_catch(
    context: &mut impl LocalContext,
    body: &LocalBody,
    region: &LocalRegion,
    binding: &Option<Id>,
    handler: &LocalRegion,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<std::ops::ControlFlow<RegionExit>> {
    use std::ops::ControlFlow::{Break, Continue};
    let completion = execute_operations(context, body, &region.operations, frame, judgment)?;
    match completion {
        Continue(()) | Break(RegionExit::Return(..)) => {
            let frontier = match &completion {
                Break(RegionExit::Return(_, frontier)) => *frontier,
                _ => region.span,
            };
            if !context.skipped_region(
                frame,
                handler.span,
                frontier,
                SkippedCallReason::HandlerNotEntered,
            ) {
                context.unproved_control_flow(frame, region.span);
                return None;
            }
            if let Break(RegionExit::Return(_, frontier)) = &completion
                && !context.skipped_region(
                    frame,
                    region.span,
                    *frontier,
                    SkippedCallReason::KnownReturn,
                )
            {
                context.unproved_control_flow(frame, region.span);
                return None;
            }
            if completion.is_break() {
                return Some(completion);
            }
        }
        Break(RegionExit::Throw(thrown)) if frame.returns.is_empty() => {
            if !context.skipped_region(
                frame,
                region.span,
                thrown.frontier,
                SkippedCallReason::KnownThrow,
            ) || !frame.effect_coverage.handle_known_throw()
            {
                context.publish_throw(&thrown.value.values);
                context.unproved_control_flow(frame, region.span);
                return None;
            }
            if let Some(binding) = binding {
                frame.environment.insert(
                    binding.clone(),
                    Values::from([AbstractValue::Value(Value::Unavailable)]),
                );
                context.instantiate_catch_binding(binding, frame);
                frame
                    .effect_coverage
                    .compose(&context.declarative_binding_operation(
                        binding,
                        frame,
                        BindingOperation::Create,
                    ));
                frame.inputs.extend(thrown.value.values.iter().cloned());
                frame
                    .environment
                    .insert(binding.clone(), thrown.value.values.clone());
                if let Some(selection) = thrown
                    .value
                    .selection
                    .filter(|selection| selection.matches(&thrown.value.values))
                {
                    frame.binding_selections.insert(binding.clone(), selection);
                }
                context.local_binding_stored(binding, frame);
                frame.initialized_bindings.insert(binding.clone());
                frame
                    .effect_coverage
                    .compose(&context.declarative_binding_operation(
                        binding,
                        frame,
                        BindingOperation::Initialize,
                    ));
            }
            let result = execute_operations(context, body, &handler.operations, frame, judgment);
            if let Some(binding) = binding {
                context.leave_catch_binding(binding);
                frame.environment.remove(binding);
                frame.binding_selections.remove(binding);
                frame.binding_postconditions.remove(binding);
                frame.initialized_bindings.remove(binding);
            }
            let result = result?;
            let frontier = match &result {
                Break(RegionExit::Return(_, span)) => Some((*span, SkippedCallReason::KnownReturn)),
                Break(RegionExit::Throw(thrown)) => {
                    Some((thrown.frontier, SkippedCallReason::KnownThrow))
                }
                _ => None,
            };
            if let Some((frontier, reason)) = frontier
                && !context.skipped_region(frame, handler.span, frontier, reason)
            {
                context.unproved_control_flow(frame, handler.span);
                return None;
            }
            if result.is_break() {
                return Some(result);
            }
        }
        _ => {
            context.unproved_control_flow(frame, region.span);
            return None;
        }
    }
    Some(Continue(()))
}

fn finish(frame: &LocalFrame, mut judgment: AllocationJudgment) -> LocalTransferResult {
    let mut effects = frame.effect_coverage.clone();
    if frame.thrown.is_some() {
        judgment = AllocationJudgment::Opaque;
        effects.abrupt();
    }
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
