//! One ordered local-operation semantics for execution and provider queries.

mod continuation;
pub(super) use continuation::{LocalCallRequest, LocalContinuation, LocalSuspension};

use super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, ArrayElement, ArraySpreadResult,
    ChildTransfer, KeyedCollectionOperationResult, LiteralHeap, LiteralLayout,
};
use super::array_transfer::ArrayMethodSelection;
use super::completion::{
    AbruptAlternative, Completion, NonNormalPossibilities, TransferLedger, UnknownThrowValues,
};
use super::descriptor_transfer::NativeObjectPostcondition;
use super::effect_coverage::{
    EffectCoverageFragment, NativeCalleeSelection, OperationStep, OperationSupportOperation,
    OperationSupportReceipt, SourceOperationSite,
};
use super::local_operations::{
    ClassConstructorBody, ClassDefinition, ClassFieldKey, ClassStaticStep, ConstructorPhase,
    DeferredCallableCreation, LocalArrayElement, LocalBinding, LocalBody, LocalCall, LocalCallee,
    LocalExpression, LocalInvocation, LocalLogicalOperator, LocalOperation, LocalRegion,
    ObjectBindingPattern, ObjectBindingProperty,
};
use super::own_data_transfer::OwnDataCopySource;
use super::own_data_transfer::{self, OwnDataContext, OwnDataOwnership, OwnDataSlot};
use super::property_inference::DescriptorAttributes;
use super::value_model::{
    AbstractValue, CallSite, Context, PrivateFieldId, PropertyDefinition, ValueExpr, Values,
};
use crate::analysis_inputs::{ForeignInvocation, ProofDependencies};
use crate::execution_evidence::{
    ObjectSpreadRefusal, OrderedCallAdmissionRefusal, OrderedFieldReadRefusal, SkippedCallReason,
};
use crate::facts::abstract_domain::value::{
    ClassDefinitionId, HeapAddress, Kind, SourceHeapResolver, Value,
};
use analysis_model::PrimKinds;
use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Clone)]
pub(super) enum LocalActivation {
    NotRequested,
    Evaluating,
    Transferred(Box<TransferLedger<(), Box<KnownThrow>>>),
    Refused,
}

impl LocalActivation {
    fn retained_values(&self) -> Values {
        match self {
            Self::Transferred(transfer) => match &transfer.completion {
                Completion::Abrupt(thrown) => thrown.value.retained_values(),
                _ => Values::new(),
            },
            Self::NotRequested | Self::Evaluating | Self::Refused => Values::new(),
        }
    }

    pub(super) fn is_abrupt(&self) -> bool {
        matches!(self, Self::Transferred(transfer) if matches!(transfer.completion, Completion::Abrupt(_)))
    }

    pub(super) fn apply_effect_proof(&self, effects: &mut EffectCoverageFragment) {
        let Self::Transferred(transfer) = self else {
            *effects = EffectCoverageFragment::default();
            return;
        };
        let dependencies = match &transfer.completion {
            Completion::Normal(()) => transfer.closed_effect_dependencies(),
            Completion::Abrupt(_) => transfer.known_abrupt_dependencies(),
            _ => None,
        };
        if let Some(dependencies) = dependencies {
            effects.depend_on(&dependencies);
        } else {
            *effects = EffectCoverageFragment::default();
        }
    }
}

#[derive(Clone)]
pub(super) struct LocalFrame {
    pub(super) environment: Context,
    pub(super) binding_selections: BTreeMap<Id, CallableSelection>,
    pub(super) binding_postconditions: BTreeMap<Id, Box<NativeObjectPostcondition>>,
    /// Present only when the caller delegates the actual parameter prologue
    /// to this kernel. Compatibility frames are already bound.
    pub(super) parameter_inputs: Option<Vec<SelectedValue>>,
    pub(super) activation: LocalActivation,
    /// Bindings initialized by an actual operation in this activation.
    /// Prebound values alone do not establish a declarative-cell receipt.
    pub(super) initialized_bindings: BTreeSet<Id>,
    pub(super) ledger: TransferLedger<LocalNormal, Box<KnownThrow>>,
    /// Exact value-only field stores performed by this activation. This is
    /// separate from the syntactic writer census and cannot be inferred from
    /// source shape or a later heap snapshot.
    pub(super) own_property_store_receipts: Vec<OwnPropertyStoreReceipt>,
    /// Entry arguments/captures and external values read while executing.
    /// Retained for a truncated remainder even after local aliases change.
    pub(super) inputs: Values,
    pub(super) borrowed_records: BTreeSet<HeapAddress>,
    pub(super) fresh_records: BTreeSet<HeapAddress>,
    /// Values reached for publication/escape accounting. This channel never
    /// supplies the body's normal result or completion proof.
    pub(super) reached_values: Values,
    /// Active canonical body boundary for an explicit full-remainder refusal.
    active_body_span: Option<Span>,
    /// Explicit event receipt from a full runtime remainder fallback. This is
    /// never inferred from generic incompleteness or unknown-call flags.
    pub(super) remainder_summarized: Cell<bool>,
    /// Supplied by checked base construction or an actual lexical capture.
    /// Syntax alone never grants a receiver or the constructor proof below.
    pub(super) this_values: Option<Values>,
    constructor_receiver: Option<ConstructorReceiver>,
    /// A path-local descriptor witness for a class currently in
    /// ClassDefinitionEvaluation. The pooled heap remains weak; this witness
    /// is scoped to this one execution of the source class definition.
    class_definition_witnesses: Vec<ClassDefinitionWitness>,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct OwnPropertyStoreReceipt {
    pub(super) source_site: SourceOperationSite,
    receiver: AbstractValue,
    postcondition: own_data_transfer::OwnPropertyStorePostcondition,
}

impl OwnPropertyStoreReceipt {
    pub(super) fn matches_writer(&self, site: &SourceOperationSite, property: &str) -> bool {
        self.source_site == *site
            && self
                .postcondition
                .proves_replacement_for(&self.receiver, property)
    }
}

pub(super) fn join_own_property_store_receipts(
    alternatives: &[&[OwnPropertyStoreReceipt]],
) -> Vec<OwnPropertyStoreReceipt> {
    let Some(first) = alternatives.first() else {
        return Vec::new();
    };
    first
        .iter()
        .filter(|receipt| {
            alternatives
                .iter()
                .skip(1)
                .all(|alternative| alternative.contains(receipt))
        })
        .cloned()
        .collect()
}

/// Per-activation descriptor/brand proof, separate from the pooled weak heap.
/// Only canonical base-class construction creates this ordinary extensible
/// receiver; unsupported exterior operations cannot preserve its prefix proof.
#[derive(Clone)]
struct ConstructorReceiver {
    class: ClassDefinitionId,
    public_fields: BTreeMap<String, DescriptorAttributes>,
    private_fields: BTreeSet<PrivateFieldId>,
}

#[derive(Clone)]
struct ClassDefinitionWitness {
    object: HeapAddress,
    own_descriptors: BTreeMap<String, DescriptorAttributes>,
    own_property_census_complete: bool,
    valid: bool,
}

impl ClassDefinitionWitness {
    fn ordinary_class(
        object: HeapAddress,
        static_members: &BTreeMap<String, DescriptorAttributes>,
    ) -> Self {
        // FunctionCreate installs configurable own name and length properties.
        let mut own_descriptors = BTreeMap::from([
            (
                "name".to_owned(),
                DescriptorAttributes {
                    writable: Some(false),
                    enumerable: Some(false),
                    configurable: Some(true),
                },
            ),
            (
                "length".to_owned(),
                DescriptorAttributes {
                    writable: Some(false),
                    enumerable: Some(false),
                    configurable: Some(true),
                },
            ),
            (
                "prototype".to_owned(),
                DescriptorAttributes {
                    writable: Some(false),
                    enumerable: Some(false),
                    configurable: Some(false),
                },
            ),
        ]);
        // Class methods/accessors are all installed before static fields and
        // carry their exact descriptor attributes. They may replace name or
        // length because those built-in descriptors are configurable.
        own_descriptors.extend(
            static_members
                .iter()
                .map(|(name, attrs)| (name.clone(), *attrs)),
        );
        Self {
            object,
            own_descriptors,
            own_property_census_complete: true,
            valid: true,
        }
    }

    fn can_define(&self, object: HeapAddress, property: &str) -> bool {
        self.valid
            && self.object == object
            && self
                .own_descriptors
                .get(property)
                .map_or(self.own_property_census_complete, |attributes| {
                    attributes.configurable == Some(true)
                })
    }
}

impl LocalFrame {
    /// Static initialization temporarily owns lexical this even when its class
    /// expression is nested inside a constructor activation.
    pub(super) fn has_class_definition_receiver(&self) -> bool {
        !self.class_definition_witnesses.is_empty()
    }

    /// A descriptor proof from the current checked base-class activation only.
    /// Callers must still require normal, complete constructor execution.
    pub(super) fn constructed_own_data(&self) -> BTreeSet<String> {
        self.constructor_receiver
            .as_ref()
            .map(|receiver| receiver.public_fields.keys().cloned().collect())
            .unwrap_or_default()
    }

    pub(super) fn new(environment: Context) -> Self {
        Self {
            inputs: environment.values().flatten().cloned().collect(),
            environment,
            binding_selections: BTreeMap::new(),
            binding_postconditions: BTreeMap::new(),
            parameter_inputs: None,
            activation: LocalActivation::NotRequested,
            initialized_bindings: BTreeSet::new(),
            ledger: TransferLedger::new(
                Completion::Normal(LocalNormal::Next),
                EffectCoverageFragment::no_effects(),
                Box::default(),
                AllocationJudgment::CoveredAllocationOnly(Box::default()),
            ),
            own_property_store_receipts: Vec::new(),
            borrowed_records: BTreeSet::new(),
            fresh_records: BTreeSet::new(),
            reached_values: Values::new(),
            active_body_span: None,
            remainder_summarized: Cell::new(false),
            this_values: None,
            constructor_receiver: None,
            class_definition_witnesses: Vec::new(),
        }
    }

    pub(super) fn constructor(body: &ClassConstructorBody, resolver: SourceHeapResolver) -> Self {
        let mut frame = Self::new(Context::new());
        let class = resolver.class_definition(body.class);
        frame.this_values = Some(Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            resolver.constructor_instances(class),
        ))]));
        frame.constructor_receiver = Some(ConstructorReceiver {
            class,
            public_fields: BTreeMap::new(),
            private_fields: BTreeSet::new(),
        });
        frame
    }
}

fn local_abrupt_values(
    abrupt: &[AbruptAlternative<Box<KnownThrow>>],
    non_normal: NonNormalPossibilities,
) -> Values {
    let mut values = abrupt
        .iter()
        .flat_map(|alternative| match alternative {
            AbruptAlternative::Exact(thrown) => thrown.value.values.iter().cloned().collect(),
            AbruptAlternative::Unknown(Some(values)) if values.0.is_empty() => {
                Values::from([AbstractValue::Value(Value::Top)])
            }
            AbruptAlternative::Unknown(Some(values)) => values.0.clone(),
            AbruptAlternative::Unknown(None) => Values::from([AbstractValue::Value(Value::Top)]),
        })
        .collect::<Values>();
    if non_normal.may_throw_without_payload {
        values.insert(AbstractValue::Value(Value::Top));
    }
    values
}

/// Nonterminal provenance of an actually selected abstract callable. This
/// proves neither fresh allocation nor complete invocation effects.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct CallableSelection {
    pub(super) target: AbstractValue,
    pub(super) dependencies: Box<ProofDependencies>,
}

fn source_function_matches(
    resolver: SourceHeapResolver,
    address: HeapAddress,
    source_callable: u32,
) -> bool {
    address.kind() == Some(Kind::Function)
        && resolver.source_origin(address) == Some(source_callable)
}

fn source_callable_value_matches(
    value: &AbstractValue,
    resolver: SourceHeapResolver,
    source_callable: u32,
) -> bool {
    match value {
        AbstractValue::Closure(key, _) => source_function_matches(resolver, *key, source_callable),
        AbstractValue::Value(Value::Alloc(Kind::Function, address)) => {
            source_function_matches(resolver, *address, source_callable)
        }
        _ => false,
    }
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
                | AbstractValue::ProviderClosure(..)
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

/// Runtime provenance of an exact abrupt completion. The site is meaningful
/// only in the executing program's heap/source namespace, never the caller's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct QualifiedThrowSite {
    pub(super) scope: crate::facts::abstract_domain::value::HeapModuleScope,
    pub(super) site: SourceOperationSite,
}

#[derive(Debug, Clone)]
pub(super) struct KnownThrow {
    pub(super) value: SelectedValue,
    /// The source operation that introduced the abrupt completion. This stays
    /// unchanged as the completion crosses callers.
    pub(super) origin: QualifiedThrowSite,
    /// The source operation that stopped the current body's continuation.
    /// A child throw is rebased to the caller's exact CallInvocation site.
    pub(super) frontier: SourceOperationSite,
}

#[derive(Clone)]
pub(super) enum LocalNormal {
    Next,
    Return {
        selected: Box<SelectedValue>,
        frontier: SourceOperationSite,
    },
    /// The body boundary was refused after some prefix ran. This is a possible
    /// normal value only, never a proven fallthrough or source return.
    Unknown(SelectedValue),
}

impl LocalFrame {
    fn absorb_value_ledger(
        &mut self,
        ledger: TransferLedger<SelectedValue, Box<KnownThrow>>,
    ) -> Option<(SelectedValue, AllocationJudgment)> {
        let TransferLedger {
            completion,
            effects,
            dependencies,
            allocation,
        } = ledger;
        self.ledger.effects.compose(&effects);
        self.ledger.dependencies.extend(dependencies.as_ref());
        let (prefix_normal, mut prefix_abrupt, mut prefix_non_normal) =
            self.ledger.completion.clone().into_alternatives();
        let (child_normal, child_abrupt, child_non_normal) = completion.into_alternatives();
        let can_continue = prefix_normal.is_some();
        let child_can_continue = child_normal.is_some();
        self.reached_values
            .extend(local_abrupt_values(&child_abrupt, child_non_normal));
        if can_continue {
            prefix_abrupt.extend(child_abrupt);
            prefix_non_normal.may_throw_without_payload |=
                child_non_normal.may_throw_without_payload;
            prefix_non_normal.may_diverge |= child_non_normal.may_diverge;
            prefix_non_normal.unresolved |= child_non_normal.unresolved;
        } else if let Some(normal) = &child_normal {
            // A child that ran after a terminal prefix is retained as reached
            // data, but it cannot reopen the caller's normal continuation.
            self.reached_values.extend(normal.values.iter().cloned());
        }
        self.ledger.completion = Completion::from_alternatives(
            if can_continue && child_can_continue {
                prefix_normal
            } else {
                None
            },
            prefix_abrupt,
            prefix_non_normal,
        );
        if self.ledger.completion.requires_path_partition() {
            return None;
        }
        Some((
            if can_continue {
                child_normal.unwrap_or_else(|| SelectedValue::raw(Values::new()))
            } else {
                SelectedValue::raw(Values::new())
            },
            allocation,
        ))
    }

    pub(super) fn is_normal_next(&self) -> bool {
        matches!(
            &self.ledger.completion,
            Completion::Normal(LocalNormal::Next)
        )
    }

    fn mark_opaque(&mut self) {
        let (normal, abrupt, mut non_normal) = self.ledger.completion.clone().into_alternatives();
        if normal.is_some() {
            non_normal.may_diverge = true;
            non_normal.unresolved = true;
        }
        self.ledger.completion = Completion::from_alternatives(normal, abrupt, non_normal);
    }

    fn complete_return(&mut self, selected: SelectedValue, frontier: SourceOperationSite) {
        let normal = LocalNormal::Return {
            selected: Box::new(selected),
            frontier,
        };
        let (prior_normal, abrupt, non_normal) = self.ledger.completion.clone().into_alternatives();
        self.ledger.completion = if matches!(prior_normal, Some(LocalNormal::Unknown(_))) {
            Completion::from_alternatives(prior_normal, abrupt, non_normal)
        } else if prior_normal.is_some() {
            Completion::from_alternatives(Some(normal), abrupt, non_normal)
        } else {
            Completion::from_alternatives(None, abrupt, non_normal)
        };
    }

    fn complete_throw(&mut self, thrown: KnownThrow) {
        let (normal, mut abrupt, non_normal) = self.ledger.completion.clone().into_alternatives();
        if normal.is_some() {
            self.reached_values
                .extend(thrown.value.values.iter().cloned());
            abrupt.push(AbruptAlternative::Exact(Box::new(thrown)));
        } else {
            self.ledger.completion = Completion::from_alternatives(None, abrupt, non_normal);
            return;
        }
        self.ledger.completion = Completion::from_alternatives(None, abrupt, non_normal);
    }

    fn absorb_allocation_completion(&mut self, completion: Completion<Values, ()>) {
        let (prefix_normal, mut abrupt, mut non_normal) =
            self.ledger.completion.clone().into_alternatives();
        if prefix_normal.is_none() {
            return;
        }
        let (child_normal, child_abrupt, child_non_normal) = completion.into_alternatives();
        for alternative in &child_abrupt {
            match alternative {
                AbruptAlternative::Exact(()) | AbruptAlternative::Unknown(None) => {
                    self.reached_values.insert(AbstractValue::Value(Value::Top));
                }
                AbruptAlternative::Unknown(Some(values)) => {
                    if values.0.is_empty() {
                        self.reached_values.insert(AbstractValue::Value(Value::Top));
                    } else {
                        self.reached_values.extend(values.0.iter().cloned());
                    }
                }
            }
        }
        if child_non_normal.may_throw_without_payload {
            self.reached_values.insert(AbstractValue::Value(Value::Top));
        }
        let converted_abrupt: Vec<AbruptAlternative<Box<KnownThrow>>> = child_abrupt
            .into_iter()
            .map(|alternative| match alternative {
                AbruptAlternative::Exact(()) => {
                    AbruptAlternative::Unknown(Some(UnknownThrowValues::new(Values::from([
                        AbstractValue::Value(Value::Top),
                    ]))))
                }
                AbruptAlternative::Unknown(values) => AbruptAlternative::Unknown(values),
            })
            .collect();
        abrupt.extend(converted_abrupt);
        non_normal.may_throw_without_payload |= child_non_normal.may_throw_without_payload;
        non_normal.may_diverge |= child_non_normal.may_diverge;
        non_normal.unresolved |= child_non_normal.unresolved;
        self.ledger.completion = Completion::from_alternatives(
            child_normal.map(|_| prefix_normal.expect("checked normal prefix")),
            abrupt,
            non_normal,
        );
    }
}

impl SelectedValue {
    pub(super) fn retained_values(&self) -> Values {
        let mut values = self.values.clone();
        if let Some(postcondition) = &self.postcondition {
            values.extend(postcondition.retained_values());
        }
        values
    }

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
        /// A recognized array-iteration selection that failed before argument
        /// evaluation. The call transfer may use it only to refine an already
        /// generic native refusal; ordinary GetValue fallback remains intact.
        array_iteration_refusal: Option<OrderedCallAdmissionRefusal>,
    },
    Native {
        selection: Box<NativeCalleeSelection>,
        /// No application callable targets: the intrinsic identity is in its row.
        targets: Values,
    },
    /// `%Function.prototype%.call/apply` was selected on this target before
    /// arguments ran. After argument evaluation the variant carries the
    /// normalized target receiver and positional arguments on the request.
    FunctionCallMethod {
        method: FunctionCallMethod,
        target: SelectedValue,
        receiver: Option<Values>,
        dependencies: Box<ProofDependencies>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum FunctionCallMethod {
    Call,
    Apply,
}

#[derive(Clone)]
pub(super) struct FunctionCallMethodSelection {
    pub(super) method: FunctionCallMethod,
    /// Address whose settled own surface proves that the selected method is
    /// inherited from the intact Function prototype.
    pub(super) receiver_address: HeapAddress,
    pub(super) dependencies: ProofDependencies,
}

pub(super) struct FunctionApplyArguments {
    pub(super) arguments: Vec<SelectedValue>,
    pub(super) object: Option<HeapAddress>,
    pub(super) dependencies: ProofDependencies,
}

impl EvaluatedCallee {
    pub(super) fn targets(&self) -> &Values {
        match self {
            Self::Direct(targets) => &targets.values,
            Self::Member { targets, .. } => targets,
            Self::Native { targets, .. } => targets,
            Self::FunctionCallMethod { target, .. } => &target.values,
        }
    }

    pub(super) fn receiver(&self) -> Option<&Values> {
        match self {
            Self::Direct(_) => None,
            Self::Member { receiver, .. } => Some(receiver),
            Self::Native { .. } => None,
            Self::FunctionCallMethod { receiver, .. } => receiver.as_ref(),
        }
    }
}

pub(super) struct LocalValueResult {
    pub(super) ledger: TransferLedger<SelectedValue, Box<KnownThrow>>,
}

pub(super) struct LocalTransferResult {
    pub(super) ledger: TransferLedger<LocalNormal, Box<KnownThrow>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum RemainderDisposition {
    NeedsSummary,
    AlreadySummarized,
}

pub(super) enum LocalExecutionAttempt {
    Completed(LocalTransferResult),
    Refused {
        result: LocalTransferResult,
        remainder: RemainderDisposition,
    },
}

impl LocalValueResult {
    fn normal(
        selected: SelectedValue,
        effects: EffectCoverageFragment,
        allocation: AllocationJudgment,
    ) -> Self {
        Self {
            ledger: TransferLedger::new(
                Completion::Normal(selected),
                effects,
                Box::default(),
                allocation,
            ),
        }
    }

    fn absorb(
        self,
        frame: &mut LocalFrame,
        judgment: &mut AllocationJudgment,
        resolver: SourceHeapResolver,
    ) -> Option<SelectedValue> {
        let (selected, allocation) = frame.absorb_value_ledger(self.ledger)?;
        judgment.compose(&allocation);
        if frame.ledger.completion.requires_path_partition() {
            return None;
        }
        Some(completing_selected(selected, frame, resolver))
    }
}

#[derive(Clone, Copy)]
pub(super) enum BindingOperation {
    Create,
    Initialize,
    Store,
}

pub(super) trait LocalContext: OwnDataContext {
    #[cfg(test)]
    fn diagnostic(&self, _message: String) {}

    fn constructor_instance_address(&self, class: ClassDefinitionId) -> HeapAddress {
        self.heap_resolver().constructor_instances(class)
    }
    /// An actual initialized receiver. Execution consumers must record their
    /// conservative fallback when that operand is unavailable.
    fn receiver_value(&mut self, frame: &mut LocalFrame) -> Option<Values> {
        frame.this_values.clone()
    }
    fn constructor_phase(&mut self, _phase: ConstructorPhase) {}
    fn constructor_prefix_is_unexposed(&self, _class: ClassDefinitionId) -> bool {
        false
    }
    /// A fresh receiver with no own field still needs its actual prototype
    /// chain proved absent at this assignment, after evaluating the RHS.
    fn constructor_assignment_absence(
        &mut self,
        _class: ClassDefinitionId,
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
    /// Select an array method before arguments run. A diagnosed refusal keeps
    /// the ordinary member read and call transfer as the fallback.
    fn array_method_callee(
        &mut self,
        _receivers: &Values,
        _property: &str,
        _span: Span,
    ) -> Result<Option<(ArrayMethodSelection, EffectCoverageFragment)>, OrderedCallAdmissionRefusal>
    {
        Ok(None)
    }
    /// Select the original Function.prototype.call/apply method before its
    /// source arguments run. The receiver is the function that the native
    /// method will forward to; absence keeps the ordinary member read path.
    fn function_call_method_callee(
        &mut self,
        _frame: &mut LocalFrame,
        _receivers: &Values,
        _property: &str,
        _span: Span,
        _source_site: &SourceOperationSite,
    ) -> Option<(FunctionCallMethodSelection, EffectCoverageFragment)> {
        None
    }
    /// Convert the already-evaluated second operand of Function.prototype.apply
    /// into positional values only when its own data shape is exact.
    fn function_apply_arguments(
        &mut self,
        _frame: &mut LocalFrame,
        _argument_list: &Values,
        _span: Span,
    ) -> Option<FunctionApplyArguments> {
        None
    }
    /// Source-qualified heap identity for the fresh rest-parameter array.
    /// Provider contexts override this using their retained provider resolver;
    /// the default application adapter must still opt in explicitly.
    fn rest_parameter_allocation(
        &self,
        _site: &SourceOperationSite,
        _allocation: u32,
    ) -> Option<HeapAddress> {
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
    ) -> ChildTransfer<ArraySpreadResult> {
        ChildTransfer::BeforeExecution
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
    /// Enter one represented lexical region after its binding values have
    /// been installed in TDZ. Contexts that do not model region cell identity
    /// may conservatively refuse non-empty scopes; refusal must leave the
    /// active-cell namespace unchanged.
    fn enter_region_bindings(
        &mut self,
        bindings: &BTreeMap<Id, LocalBinding>,
        _frame: &mut LocalFrame,
    ) -> bool {
        bindings.is_empty()
    }
    /// Restore the active-cell namespace for the most recently entered region.
    /// A refused region can retain local values and widen captured cells for a
    /// whole-remainder summary before its lexical environment is removed.
    fn leave_region_bindings(&mut self, _frame: &mut LocalFrame, _refused: bool) {}
    fn local_binding_stored(&mut self, _binding: &Id, _frame: &LocalFrame) {}
    fn read_binding(
        &mut self,
        binding: &Id,
        frame: &mut LocalFrame,
        source_site: Option<&SourceOperationSite>,
    ) -> Option<LocalValueResult>;
    /// The one selected direct-import call may prove its module callee read
    /// after source closure validation. Other reads use ordinary coverage.
    fn read_call_callee(
        &mut self,
        binding: &Id,
        _call_span: Span,
        frame: &mut LocalFrame,
        source_site: Option<&SourceOperationSite>,
    ) -> Option<LocalValueResult> {
        self.read_binding(binding, frame, source_site)
    }
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
        selected: &SelectedValue,
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
        source_site: &SourceOperationSite,
        span: Span,
    ) -> Option<LocalValueResult>;
    fn literal_layout(&self, allocation: u32) -> LiteralLayout;
    fn exact_object_spread_copy(&self, _allocation: u32) -> bool {
        false
    }
    fn object_spread_copy_support(
        &mut self,
        _frame: &mut LocalFrame,
        _source: &OwnDataCopySource,
        _dependencies: &ProofDependencies,
    ) -> bool {
        false
    }
    fn refused_object_spread(
        &mut self,
        _frame: &mut LocalFrame,
        _site: CallSite,
        _source: &Values,
        _refusal: ObjectSpreadRefusal,
    ) -> ChildTransfer<AllocationValue> {
        ChildTransfer::RemainderAfterPrefix { summarized: false }
    }
    fn incomplete_object_spread(
        &mut self,
        _frame: &mut LocalFrame,
        site: CallSite,
        _source: &Values,
        _refusal: ObjectSpreadRefusal,
    ) -> ChildTransfer<AllocationValue> {
        ChildTransfer::Value(AllocationValue::opaque(Values::from([
            AbstractValue::Value(Value::Alloc(
                Kind::Object,
                self.heap_resolver()
                    .allocation(Kind::Object, site.allocation_id()),
            )),
            AbstractValue::Value(Value::Unavailable),
        ])))
    }
    fn object_spread_dependencies(&self) -> ProofDependencies {
        ProofDependencies::default()
    }
    fn literal_property_kind(
        &self,
        allocation: u32,
        position: usize,
    ) -> super::value_model::LiteralPropertyKind;
    fn heap(&mut self) -> LiteralHeap<'_>;
    fn record_property_read(&mut self, cell: crate::facts::EffectCell);
    fn record_property_write(&mut self, cell: crate::facts::EffectCell);
    /// Apply the heap half of a preflighted class DefineField operation.
    /// Returning false means this context has no runtime class-object store;
    /// the kernel then refuses the class definition without claiming that the
    /// field completed.
    fn create_class_data_property(
        &mut self,
        _target: HeapAddress,
        _property: &str,
        _values: &Values,
        _attributes: DescriptorAttributes,
    ) -> bool {
        false
    }
    /// Only application contexts project pooled source regions into their
    /// relocation footprint. Isolated providers override this so their local
    /// allocation ordinals cannot escape into importer analysis.
    fn operation_support_receipt(
        &self,
        _frame: &LocalFrame,
        _operation: OperationSupportOperation,
        origin: HeapAddress,
        dependencies: &ProofDependencies,
    ) -> Option<OperationSupportReceipt>;
    fn record_operation_support(&mut self, _receipt: OperationSupportReceipt) {}
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
        receiver: Option<&Id>,
        receivers: &Values,
        property: &str,
        span: Span,
        refusal: OrderedFieldReadRefusal,
    ) -> Option<AllocationValue>;
    /// Retain possible normal values for targets after an already summarized
    /// unknown binding operation. This does not execute or initialize them.
    fn unproved_binding_targets(
        &mut self,
        _frame: &mut LocalFrame,
        _targets: &[ObjectBindingProperty],
    ) {
    }
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
    /// Called only for a source-authenticated skipped region: a linear region
    /// stopped at known completion, a handler was bypassed, or an If arm was
    /// excluded on this feasible continuation leaf. No syntax-order heuristic.
    fn skipped_region(
        &mut self,
        _frame: &mut LocalFrame,
        _region: &SourceOperationSite,
        _frontier: &SourceOperationSite,
        _reason: SkippedCallReason,
    ) -> bool {
        false
    }
    fn instantiate_catch_binding(&mut self, _binding: &Id, _frame: &LocalFrame) {}
    fn leave_catch_binding(&mut self, _binding: &Id) {}
    fn unproved_control_flow(&mut self, _frame: &LocalFrame, _span: Span) {}
    /// The shared kernel reached an unsupported child after an earlier sibling
    /// ran. Application execution summarizes the complete body here; strict
    /// source queries return false and preserve the refused prefix only.
    fn abandon_remainder(&mut self, frame: &mut LocalFrame) -> bool {
        if let Some(span) = frame.active_body_span {
            self.unproved_control_flow(frame, span);
        }
        frame.mark_opaque();
        frame.remainder_summarized.get()
    }
    fn keyed_collection_new(
        &mut self,
        _frame: &mut LocalFrame,
        _callee: &ValueExpr,
        _arguments: &[ValueExpr],
        _source_site: &SourceOperationSite,
        _site: CallSite,
    ) -> Option<KeyedCollectionOperationResult> {
        None
    }
    fn set_construction(
        &mut self,
        _frame: &mut LocalFrame,
        _source: &ValueExpr,
        _argument: &SelectedValue,
        _source_site: &SourceOperationSite,
        _site: CallSite,
    ) -> Option<LocalValueResult> {
        None
    }
}

struct LocalValueContext<'a, C> {
    context: &'a mut C,
    frame: &'a mut LocalFrame,
    source_site: Option<&'a SourceOperationSite>,
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
    fn array_spread(&mut self, span: Span, operand: &Values) -> ChildTransfer<ArraySpreadResult> {
        match self.context.array_spread(self.frame, span, operand) {
            ChildTransfer::Value(spread) => {
                self.frame.ledger.effects.compose(
                    &EffectCoverageFragment::ordered_native_iterable_protocol(&spread.dependencies),
                );
                ChildTransfer::Value(spread)
            }
            ChildTransfer::BeforeExecution if self.frame.remainder_summarized.get() => {
                ChildTransfer::RemainderAfterPrefix { summarized: true }
            }
            other => other,
        }
    }
    fn primitive_array_created(&mut self, allocation: u32, dependencies: &ProofDependencies) {
        self.context
            .primitive_array_created(allocation, dependencies);
    }
    fn binding(&mut self, binding: &Id) -> ChildTransfer<AllocationValue> {
        let Some(result) = self
            .context
            .read_binding(binding, self.frame, self.source_site)
        else {
            return if self.frame.remainder_summarized.get() {
                ChildTransfer::RemainderAfterPrefix { summarized: true }
            } else {
                ChildTransfer::BeforeExecution
            };
        };
        let dependencies = result.ledger.dependencies.as_ref().clone();
        let Some((selected, judgment)) = self.frame.absorb_value_ledger(result.ledger) else {
            return ChildTransfer::BeforeExecution;
        };
        ChildTransfer::Value(allocation_value_for_selected(
            self.frame,
            selected,
            judgment,
            dependencies,
            self.context.heap_resolver(),
        ))
    }
    fn unmodeled_effect_operation(&mut self) {
        self.frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
    }
    fn operation_effects(&mut self, effects: EffectCoverageFragment) {
        self.frame.ledger.effects.compose(&effects);
    }
    fn child(
        &mut self,
        expression: &ValueExpr,
        _prefix_reached: bool,
    ) -> ChildTransfer<AllocationValue> {
        if self.frame.ledger.completion.requires_path_partition() {
            return if self.frame.remainder_summarized.get() {
                ChildTransfer::RemainderAfterPrefix { summarized: true }
            } else {
                ChildTransfer::BeforeExecution
            };
        }
        match value_transfer(self.context, expression, self.frame, self.source_site) {
            ChildTransfer::BeforeExecution if self.frame.remainder_summarized.get() => {
                ChildTransfer::RemainderAfterPrefix { summarized: true }
            }
            transfer => transfer,
        }
    }
    fn abandon_remainder(&mut self) -> bool {
        self.context.abandon_remainder(self.frame)
    }
    fn noninvoking_operation(&mut self) {
        self.frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::no_effects());
    }
    fn literal_creation_operation(&mut self, covered: bool) {
        let effects = if covered {
            EffectCoverageFragment::no_effects()
        } else {
            EffectCoverageFragment::default()
        };
        self.frame.ledger.effects.compose(&effects);
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.context.literal_layout(allocation)
    }
    fn exact_object_spread_copy(&self, allocation: u32) -> bool {
        self.context.exact_object_spread_copy(allocation)
    }
    fn object_spread_copy_support(
        &mut self,
        source: &OwnDataCopySource,
        dependencies: &ProofDependencies,
    ) -> bool {
        self.context
            .object_spread_copy_support(self.frame, source, dependencies)
    }
    fn refused_object_spread(
        &mut self,
        site: CallSite,
        source: &Values,
        refusal: ObjectSpreadRefusal,
    ) -> ChildTransfer<AllocationValue> {
        match self
            .context
            .refused_object_spread(self.frame, site, source, refusal)
        {
            ChildTransfer::BeforeExecution if self.frame.remainder_summarized.get() => {
                ChildTransfer::RemainderAfterPrefix { summarized: true }
            }
            transfer => transfer,
        }
    }
    fn incomplete_object_spread(
        &mut self,
        site: CallSite,
        source: &Values,
        refusal: ObjectSpreadRefusal,
    ) -> ChildTransfer<AllocationValue> {
        self.context
            .incomplete_object_spread(self.frame, site, source, refusal)
    }
    fn object_spread_dependencies(&self) -> ProofDependencies {
        self.context.object_spread_dependencies()
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
    ) -> ChildTransfer<KeyedCollectionOperationResult> {
        let Some(source_site) = self.source_site else {
            return ChildTransfer::BeforeExecution;
        };
        match self
            .context
            .keyed_collection_new(self.frame, callee, arguments, source_site, site)
        {
            None if self.frame.remainder_summarized.get() => {
                ChildTransfer::RemainderAfterPrefix { summarized: true }
            }
            None => ChildTransfer::BeforeExecution,
            Some(result) => ChildTransfer::Value(result),
        }
    }
}

impl<C: LocalContext> OwnDataContext for LocalValueContext<'_, C> {
    fn heap_resolver(&self) -> SourceHeapResolver {
        self.context.heap_resolver()
    }
    fn object_surface(&self) -> &super::object_surface::ObjectSurfaceStore {
        self.context.object_surface()
    }
    fn ordinary_record_shape(
        &self,
        allocation: HeapAddress,
    ) -> Option<&super::value_model::OwnPropertyShape> {
        self.context.ordinary_record_shape(allocation)
    }
    fn own_properties(&self, allocation: HeapAddress) -> Option<&BTreeMap<Option<String>, Values>> {
        self.context.own_properties(allocation)
    }
    fn object_properties_are_settled(&self, allocation: HeapAddress) -> bool {
        self.context.object_properties_are_settled(allocation)
    }
    fn copy_data_properties_are_exact(&self, allocation: HeapAddress) -> bool {
        self.context.copy_data_properties_are_exact(allocation)
    }
    fn instance_properties_are_settled(&self, class: ClassDefinitionId) -> bool {
        self.context.instance_properties_are_settled(class)
    }
    fn property_values(&self, allocation: HeapAddress, property: &str) -> Option<&Values> {
        self.context.property_values(allocation, property)
    }
    fn property_is_accessor(&self, allocation: HeapAddress, property: &str) -> bool {
        self.context.property_is_accessor(allocation, property)
    }
    fn property_is_writable(&self, allocation: HeapAddress, property: &str) -> bool {
        self.context.property_is_writable(allocation, property)
    }
    fn first_generation_dense_array_length(&self, allocation: HeapAddress) -> Option<u32> {
        self.context.first_generation_dense_array_length(allocation)
    }
}

fn value_with_site(
    context: &mut impl LocalContext,
    expression: &ValueExpr,
    frame: &mut LocalFrame,
    source_site: Option<&SourceOperationSite>,
) -> Option<AllocationValue> {
    match value_transfer(context, expression, frame, source_site) {
        ChildTransfer::Value(value) => Some(value),
        ChildTransfer::BeforeExecution | ChildTransfer::RemainderAfterPrefix { .. } => None,
    }
}

fn value_transfer(
    context: &mut impl LocalContext,
    expression: &ValueExpr,
    frame: &mut LocalFrame,
    source_site: Option<&SourceOperationSite>,
) -> ChildTransfer<AllocationValue> {
    // A site already in the heap may denote an older allocation. The current
    // domain cannot distinguish that generation from this invocation's record.
    let new_record = match expression {
        ValueExpr::ObjectLiteral(object, _) => {
            let address = context.heap_resolver().allocation(Kind::Object, *object);
            !context.heap().surface.has_surface_state(address)
                && !frame.borrowed_records.contains(&address)
        }
        ValueExpr::ObjectSpreadLiteral(site, _) => {
            let address = context
                .heap_resolver()
                .allocation(Kind::Object, site.allocation_id());
            !context.heap().surface.has_surface_state(address)
                && !frame.borrowed_records.contains(&address)
        }
        _ => false,
    };
    let result = match allocation_transfer::transfer(
        &mut LocalValueContext {
            context,
            frame,
            source_site,
        },
        expression,
    ) {
        ChildTransfer::Value(value) => value,
        ChildTransfer::BeforeExecution if frame.remainder_summarized.get() => {
            return ChildTransfer::RemainderAfterPrefix { summarized: true };
        }
        ChildTransfer::BeforeExecution => return ChildTransfer::BeforeExecution,
        remainder @ ChildTransfer::RemainderAfterPrefix { .. } => return remainder,
    };
    let mut result = result;
    reflect_allocation_value(&mut result, frame);
    let values = result
        .ledger
        .completion
        .may_normal()
        .cloned()
        .unwrap_or_default();
    if let ValueExpr::ObjectLiteral(object, _) = expression {
        mark_fresh_record(context, frame, *object, new_record, &values);
    }
    if let ValueExpr::ObjectSpreadLiteral(site, _) = expression {
        mark_fresh_record(context, frame, site.allocation_id(), new_record, &values);
    }
    ChildTransfer::Value(result)
}

fn mark_fresh_record(
    context: &impl LocalContext,
    frame: &mut LocalFrame,
    allocation: u32,
    new_record: bool,
    values: &Values,
) {
    let address = context.heap_resolver().allocation(Kind::Object, allocation);
    if new_record
        && frame.ledger.is_exact_normal()
        && !frame.borrowed_records.contains(&address)
        && context.ordinary_record_shape(address).is_some()
        && values.contains(&AbstractValue::Value(Value::Alloc(Kind::Object, address)))
    {
        frame.fresh_records.insert(address);
    }
}

fn completing(values: Values, frame: &mut LocalFrame) -> Values {
    if values.contains(&AbstractValue::Value(Value::Unavailable)) {
        // Unavailable remains a possible normal payload. Keep it in the value
        // projection while recording its loss in the completion ledger.
        frame
            .reached_values
            .insert(AbstractValue::Value(Value::Unavailable));
        frame.mark_opaque();
    }
    values
}

fn completing_selected(
    mut selected: SelectedValue,
    frame: &mut LocalFrame,
    resolver: SourceHeapResolver,
) -> SelectedValue {
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
        .is_some_and(|proof| !proof.matches(resolver, &selected.values))
    {
        selected.postcondition = None;
    }
    selected
}

fn allocation_value_for_selected(
    frame: &mut LocalFrame,
    selected: SelectedValue,
    judgment: AllocationJudgment,
    dependencies: ProofDependencies,
    resolver: SourceHeapResolver,
) -> AllocationValue {
    let selected = completing_selected(selected, frame, resolver);
    if frame.ledger.completion.may_normal().is_some() && selected.values.is_empty() {
        frame.mark_opaque();
    }
    let (normal, abrupt, non_normal) = frame.ledger.completion.clone().into_alternatives();
    let abrupt = abrupt
        .into_iter()
        .map(|alternative| match alternative {
            AbruptAlternative::Exact(thrown) => AbruptAlternative::Unknown(Some(
                UnknownThrowValues::new(thrown.value.values.clone()),
            )),
            other => other,
        })
        .collect();
    let normal = normal.map(|_| {
        if selected.values.is_empty() {
            Values::from([AbstractValue::Value(Value::Unavailable)])
        } else {
            selected.values
        }
    });
    let completion = Completion::from_alternatives(normal, abrupt, non_normal);
    let completion: Completion<Values, ()> = completion.map_parts(
        |values| values,
        |thrown| {
            AbruptAlternative::Unknown(Some(UnknownThrowValues::new(thrown.value.values.clone())))
        },
    );
    AllocationValue::with_completion(completion, judgment, dependencies)
}

fn reflect_allocation_value(result: &mut AllocationValue, frame: &mut LocalFrame) {
    frame.ledger.effects.compose(&result.ledger.effects);
    frame
        .ledger
        .dependencies
        .extend(result.ledger.dependencies.as_ref());
    let completion = result.ledger.completion.clone();
    frame.absorb_allocation_completion(completion.clone());
    if completion.has_no_normal_path() {
        return;
    }
    if let Some(values) = completion.may_normal().cloned() {
        completing(values, frame);
    }
}

fn absorb_allocation_value(
    mut result: AllocationValue,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
    resolver: SourceHeapResolver,
) -> SelectedValue {
    reflect_allocation_value(&mut result, frame);
    judgment.compose(&result.ledger.allocation);
    if frame.ledger.completion.has_no_normal_path()
        || frame.ledger.completion.requires_path_partition()
    {
        return SelectedValue::raw(Values::new());
    }
    result
        .ledger
        .completion
        .may_normal()
        .cloned()
        .map(|values| {
            if matches!(&result.ledger.completion, Completion::OpaqueMayNormal(_)) {
                frame.mark_opaque();
            }
            completing_selected(SelectedValue::raw(values), frame, resolver)
        })
        .unwrap_or_else(|| SelectedValue::raw(Values::new()))
}

fn read_binding_selected(
    context: &mut impl LocalContext,
    binding: &Id,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
    source_site: Option<&SourceOperationSite>,
) -> Option<SelectedValue> {
    let result = context.read_binding(binding, frame, source_site)?;
    let (mut selected, allocation) = frame.absorb_value_ledger(result.ledger)?;
    judgment.compose(&allocation);
    if selected.selection.is_none() {
        selected.selection = frame
            .binding_selections
            .get(binding)
            .cloned()
            .filter(|selection| selection.matches(&selected.values));
    }
    if selected.postcondition.is_none() {
        selected.postcondition = frame
            .binding_postconditions
            .get(binding)
            .cloned()
            .filter(|proof| proof.matches(context.heap_resolver(), &selected.values));
    }
    Some(completing_selected(
        selected,
        frame,
        context.heap_resolver(),
    ))
}

fn read_call_callee_selected(
    context: &mut impl LocalContext,
    binding: &Id,
    call_span: Span,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
    source_site: Option<&SourceOperationSite>,
) -> Option<SelectedValue> {
    let result = context.read_call_callee(binding, call_span, frame, source_site)?;
    let (mut selected, allocation) = frame.absorb_value_ledger(result.ledger)?;
    judgment.compose(&allocation);
    if selected.selection.is_none() {
        selected.selection = frame
            .binding_selections
            .get(binding)
            .cloned()
            .filter(|selection| selection.matches(&selected.values));
    }
    if selected.postcondition.is_none() {
        selected.postcondition = frame
            .binding_postconditions
            .get(binding)
            .cloned()
            .filter(|proof| proof.matches(context.heap_resolver(), &selected.values));
    }
    Some(completing_selected(
        selected,
        frame,
        context.heap_resolver(),
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
    receiver: Option<&Id>,
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
            let mut selection_dependencies = context.selection_prerequisites();
            let read = slot.read(context);
            selection_dependencies.extend(&read.dependencies);
            let selection =
                CallableSelection::observed(&read.values, selection_dependencies.clone());
            let effects = if let Some(origin) = slot_origin_from_cell(&read.cell)
                && let Some(receipt) = context.operation_support_receipt(
                    frame,
                    OperationSupportOperation::OwnDataRead,
                    origin,
                    &selection_dependencies,
                ) {
                context.record_property_read(read.cell.clone());
                context.record_property_read(crate::facts::EffectCell::ObjectShape(origin));
                let mut effects = EffectCoverageFragment::modeled_own_data_access();
                effects.depend_on(selection_dependencies.as_ref());
                effects.operation_support(receipt.clone());
                context.record_operation_support(receipt);
                effects
            } else {
                // The own-property operation was selected, but its mutable
                // descriptor support has no transportable address.
                context.record_property_read(read.cell.clone());
                EffectCoverageFragment::default()
            };
            Some(LocalValueResult::normal(
                SelectedValue {
                    values: read.values,
                    selection,
                    postcondition: None,
                },
                effects,
                AllocationJudgment::CoveredAllocationOnly(Box::default()),
            ))
        }
        Err(refusal) => context
            .symbolic_field_read(receivers, property)
            .or_else(|| {
                context
                    .unproved_field_read(frame, receiver, receivers, property, span, refusal)
                    .map(|allocation| {
                        let super::completion::TransferLedger {
                            completion,
                            mut effects,
                            dependencies,
                            allocation: judgment,
                        } = allocation.ledger;
                        effects.compose(&EffectCoverageFragment::default());
                        let completion = completion.map_parts(SelectedValue::raw, |()| {
                            AbruptAlternative::Unknown(Some(UnknownThrowValues::new(Values::from(
                                [AbstractValue::Value(Value::Top)],
                            ))))
                        });
                        LocalValueResult {
                            ledger: TransferLedger::new(
                                completion,
                                effects,
                                dependencies,
                                judgment,
                            ),
                        }
                    })
            }),
    }
}

fn slot_origin_from_cell(cell: &crate::facts::EffectCell) -> Option<HeapAddress> {
    match cell {
        crate::facts::EffectCell::OwnProperty { origin, .. } => Some(*origin),
        _ => None,
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
    if selected.values.is_empty() {
        if frame.ledger.completion.has_no_normal_path() {
            return Some(SelectedValue::raw(selected.values));
        }
        frame.mark_opaque();
        return None;
    }
    let Some(kind) = body.bindings.get(binding) else {
        frame.binding_selections.remove(binding);
        let result = context.assign_capture(frame, binding, &selected, span)?;
        return result.absorb(frame, judgment, context.heap_resolver());
    };
    let values = selected.values;
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
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        frame
            .reached_values
            .insert(AbstractValue::Value(Value::Unavailable));
        frame.mark_opaque();
        return Some(SelectedValue::raw(Values::new()));
    }
    // This PutValue reaches an initialized mutable declarative cell.
    // The adapter independently covers this cell operation;
    // evaluating its RHS has already composed its own effects.
    frame
        .ledger
        .effects
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
    if frame.ledger.completion.requires_path_partition() {
        refuse_unpartitioned_completion(context, frame);
        return None;
    }
    let result = match node.without_source() {
        LocalExpression::Conditional { .. } | LocalExpression::Logical { .. } => None,
        LocalExpression::Binary {
            operator,
            left,
            right,
            span,
        } => binary_expression(
            &mut LocalValueContext {
                context,
                frame,
                source_site: None,
            },
            BinaryOperatorTransfer {
                operator,
                left,
                right,
                span,
                frontier: node
                    .source_site_for_step(OperationStep::BinaryOperator)
                    .cloned(),
            },
            body,
            judgment,
        ),
        LocalExpression::Template {
            substitutions,
            span,
        } => template_expression(
            context,
            substitutions,
            span,
            node.source_site_for_step(OperationStep::TemplateCoercion)
                .cloned(),
            body,
            frame,
            judgment,
        ),
        LocalExpression::Value(value) => {
            let source_site = match value {
                ValueExpr::New(..) => node.source_site_for_step(OperationStep::CallInvocation),
                _ => node
                    .source_site_for_step(OperationStep::Expression)
                    .or_else(|| node.source_site_for_step(OperationStep::CallInvocation)),
            };
            value_expression(context, value, source_site, frame, judgment)
        }
        LocalExpression::PrimitiveLiteral(kind) => {
            // The canonical source literal cannot invoke code, access exterior
            // state or throw. Its value abstraction is independent evidence.
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::no_effects());
            Some(completing_selected(
                SelectedValue::raw(Values::from([AbstractValue::Value(Value::Prim(*kind))])),
                frame,
                context.heap_resolver(),
            ))
        }
        LocalExpression::ClassDefinition(definition) => {
            define_class(definition, context, body, frame, judgment)
        }
        LocalExpression::This => {
            let values = context.receiver_value(frame)?;
            frame
                .ledger
                .effects
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
            site: allocation_site,
        } => set_construction_expression(
            &mut LocalValueContext {
                context,
                frame,
                source_site: None,
            },
            array,
            source,
            allocation_site,
            node.source_site_for_step(OperationStep::CallInvocation)?,
            body,
            judgment,
        ),
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
        LocalExpression::UpdateBinding { binding, span } => update_binding_expression(
            context,
            UpdateBindingOperation {
                binding,
                span,
                read_site: node
                    .source_site_for_step(OperationStep::BindingGetValue)
                    .cloned(),
                source_site: node
                    .source_site_for_step(OperationStep::UpdateCoercion)
                    .cloned(),
                body,
            },
            frame,
            judgment,
        ),
        LocalExpression::Call(call) => call_expression(
            context,
            call,
            node.source_site_for_step(OperationStep::CallInvocation)?,
            node.source_site_for_step(OperationStep::CallCalleeGetValue),
            body,
            frame,
            judgment,
        ),
        LocalExpression::ReadField {
            receiver,
            property,
            span,
            optional_receiver,
        } => {
            if *optional_receiver {
                return None;
            }
            read_field_expression(
                context,
                expression_binding(receiver)?,
                property,
                span,
                node.source_site_for_step(OperationStep::FieldGetValue),
                frame,
                judgment,
            )
        }
        LocalExpression::AssignField {
            receiver,
            property,
            value: value_node,
            span,
        } => assign_field_expression(
            &mut LocalValueContext {
                context,
                frame,
                source_site: node.source_site_for_step(OperationStep::FieldPutValue),
            },
            AssignFieldOperation {
                receiver,
                property,
                value: value_node,
                span,
                receiver_source_site: node.source_site_for_step(OperationStep::FieldGetValue),
                body,
            },
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
        LocalExpression::Sourced { .. } => {
            unreachable!("source wrapper is removed before expression transfer")
        }
    };
    if result.is_none() {
        frame.mark_opaque();
    }
    result
}

struct BinaryOperatorTransfer<'a> {
    operator: &'a swc_ecma_ast::BinaryOp,
    left: &'a LocalExpression,
    right: &'a LocalExpression,
    span: &'a Span,
    frontier: Option<SourceOperationSite>,
}

fn binary_expression(
    local: &mut LocalValueContext<'_, impl LocalContext>,
    transfer: BinaryOperatorTransfer<'_>,
    body: &LocalBody,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let BinaryOperatorTransfer {
        operator,
        left,
        right,
        span,
        frontier,
    } = transfer;
    let left = expression(local.context, left, body, local.frame, judgment)?;
    if left.values.is_empty() {
        if local.frame.ledger.completion.has_no_normal_path() {
            return Some(left);
        }
        local.frame.mark_opaque();
        return None;
    }
    let right = expression(local.context, right, body, local.frame, judgment)?;
    if right.values.is_empty() {
        if local.frame.ledger.completion.has_no_normal_path() {
            return Some(right);
        }
        local.frame.mark_opaque();
        return None;
    }
    apply_binary(local, operator, left, right, span, frontier, judgment)
}

fn apply_binary(
    local: &mut LocalValueContext<'_, impl LocalContext>,
    operator: &swc_ecma_ast::BinaryOp,
    left: SelectedValue,
    right: SelectedValue,
    span: &Span,
    frontier: Option<SourceOperationSite>,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let operation = allocation_transfer::binary_result(*operator, &left.values, &right.values);
    let values = match operation {
        allocation_transfer::PrimitiveOperationResult::Covered(values) => {
            local
                .frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::no_effects());
            values
        }
        allocation_transfer::PrimitiveOperationResult::MayThrow(values) => {
            *judgment = AllocationJudgment::Opaque;
            local
                .frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            local.frame.mark_opaque();
            values
        }
        allocation_transfer::PrimitiveOperationResult::Abrupt
            if local.frame.is_normal_next()
                && frontier
                    .as_ref()
                    .is_some_and(|site| site.step == OperationStep::BinaryOperator)
                && !local
                    .frame
                    .reached_values
                    .contains(&AbstractValue::Value(Value::Unavailable))
                && !left
                    .values
                    .contains(&AbstractValue::Value(Value::Unavailable))
                && !right
                    .values
                    .contains(&AbstractValue::Value(Value::Unavailable)) =>
        {
            *judgment = AllocationJudgment::Opaque;
            let thrown = SelectedValue::raw(Values::from([AbstractValue::Value(Value::Top)]));
            local.context.publish_throw(&thrown.values);
            let frontier = frontier.expect("guarded binary operation site");
            local.frame.complete_throw(KnownThrow {
                value: thrown,
                origin: QualifiedThrowSite {
                    scope: local.context.heap_resolver().scope(),
                    site: frontier.clone(),
                },
                frontier,
            });
            return Some(SelectedValue::raw(Values::new()));
        }
        allocation_transfer::PrimitiveOperationResult::Abrupt => {
            // A definite operator throw on the surviving normal path cannot
            // erase an earlier unknown or mixed completion.
            *judgment = AllocationJudgment::Opaque;
            local
                .frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            return Some(completing_selected(
                SelectedValue::raw(Values::from([AbstractValue::Value(Value::Unavailable)])),
                local.frame,
                local.context.heap_resolver(),
            ));
        }
        allocation_transfer::PrimitiveOperationResult::CoercionRequired => {
            let mut operands = left.values;
            operands.extend(right.values);
            local
                .frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            local
                .context
                .unproved_operator(local.frame, &operands, *span);
            local.frame.mark_opaque();
            return None;
        }
    };
    Some(completing_selected(
        SelectedValue::raw(values),
        local.frame,
        local.context.heap_resolver(),
    ))
}

fn convert_template_operand(
    context: &mut impl LocalContext,
    operand: SelectedValue,
    span: &Span,
    source_site: Option<SourceOperationSite>,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    // ToString happens before evaluating the following substitution.
    // A primitive String result never erases operand effects or throws.
    let values = match allocation_transfer::template_substitution_result(&operand.values) {
        allocation_transfer::PrimitiveOperationResult::Covered(values) => {
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::no_effects());
            values
        }
        allocation_transfer::PrimitiveOperationResult::MayThrow(values) => {
            *judgment = AllocationJudgment::Opaque;
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            frame.mark_opaque();
            values
        }
        allocation_transfer::PrimitiveOperationResult::Abrupt
            if frame.is_normal_next()
                && source_site
                    .as_ref()
                    .is_some_and(|site| site.step == OperationStep::TemplateCoercion)
                && !frame
                    .reached_values
                    .contains(&AbstractValue::Value(Value::Unavailable))
                && !operand
                    .values
                    .contains(&AbstractValue::Value(Value::Unavailable)) =>
        {
            *judgment = AllocationJudgment::Opaque;
            let thrown = SelectedValue::raw(Values::from([AbstractValue::Value(Value::Top)]));
            context.publish_throw(&thrown.values);
            let frontier = source_site.clone().expect("guarded template coercion site");
            frame.complete_throw(KnownThrow {
                value: thrown,
                origin: QualifiedThrowSite {
                    scope: context.heap_resolver().scope(),
                    site: frontier.clone(),
                },
                frontier,
            });
            return Some(SelectedValue::raw(Values::new()));
        }
        allocation_transfer::PrimitiveOperationResult::Abrupt => {
            *judgment = AllocationJudgment::Opaque;
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            frame.mark_opaque();
            return Some(completing_selected(
                SelectedValue::raw(Values::from([AbstractValue::Value(Value::Unavailable)])),
                frame,
                context.heap_resolver(),
            ));
        }
        allocation_transfer::PrimitiveOperationResult::CoercionRequired => {
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            context.unproved_operator(frame, &operand.values, *span);
            frame.mark_opaque();
            return None;
        }
    };
    let converted = completing_selected(SelectedValue::raw(values), frame, context.heap_resolver());
    Some(converted)
}

fn template_expression(
    context: &mut impl LocalContext,
    substitutions: &[LocalExpression],
    span: &Span,
    source_site: Option<SourceOperationSite>,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    for substitution in substitutions {
        let operand = expression(context, substitution, body, frame, judgment)?;
        if operand.values.is_empty() {
            if frame.ledger.completion.has_no_normal_path() {
                return Some(operand);
            }
            frame.mark_opaque();
            return None;
        }
        let converted =
            convert_template_operand(context, operand, span, source_site.clone(), frame, judgment)?;
        if !frame.is_normal_next() {
            return Some(converted);
        }
    }
    frame
        .ledger
        .effects
        .compose(&EffectCoverageFragment::no_effects());
    Some(completing_selected(
        SelectedValue::raw(Values::from([AbstractValue::Value(Value::Prim(
            PrimKinds::STRING,
        ))])),
        frame,
        context.heap_resolver(),
    ))
}

fn value_expression(
    context: &mut impl LocalContext,
    node: &ValueExpr,
    source_site: Option<&SourceOperationSite>,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let selected = if let ValueExpr::Binding(binding) = node {
        read_binding_selected(context, binding, frame, judgment, source_site)?
    } else {
        let result = value_with_site(context, node, frame, source_site)?;
        absorb_allocation_value(result, frame, judgment, context.heap_resolver())
    };
    Some(completing_selected(
        selected,
        frame,
        context.heap_resolver(),
    ))
}

fn receive_allocation_transfer(
    frame: &mut LocalFrame,
    transfer: ChildTransfer<AllocationValue>,
) -> Option<AllocationValue> {
    match transfer {
        ChildTransfer::Value(value) => Some(value),
        ChildTransfer::BeforeExecution if frame.remainder_summarized.get() => None,
        ChildTransfer::BeforeExecution => None,
        ChildTransfer::RemainderAfterPrefix { summarized } => {
            if summarized {
                frame.remainder_summarized.set(true);
            }
            None
        }
    }
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
    let address = context
        .heap_resolver()
        .allocation(Kind::Object, *allocation);
    let mut new_record = !context.heap().surface.has_surface_state(address);
    let transfer = allocation_transfer::record(
        &mut LocalValueContext {
            context,
            frame,
            source_site: None,
        },
        *allocation,
        properties
            .iter()
            .map(|property| property.map(|value| value)),
        AllocationJudgment::CoveredAllocationOnly(Box::default()),
        |context, node, _prefix_reached| {
            let mut judgment = AllocationJudgment::CoveredAllocationOnly(Box::default());
            let Some(selected) =
                expression(context.context, node, body, context.frame, &mut judgment)
            else {
                return if context.frame.remainder_summarized.get() {
                    ChildTransfer::RemainderAfterPrefix { summarized: true }
                } else {
                    ChildTransfer::BeforeExecution
                };
            };
            // A later child can truncate the unfinished literal.
            context.frame.inputs.extend(selected.values.iter().cloned());
            // A child can reenter this allocation site. Its resulting
            // heap entry then also represents an older record.
            new_record &= !context.context.heap().surface.has_surface_state(address);
            ChildTransfer::Value(allocation_value_for_selected(
                context.frame,
                selected,
                judgment,
                ProofDependencies::default(),
                context.heap_resolver(),
            ))
        },
    );
    let result = receive_allocation_transfer(frame, transfer)?;
    let selected = absorb_allocation_value(result, frame, judgment, context.heap_resolver());
    mark_fresh_record(context, frame, *allocation, new_record, &selected.values);
    Some(selected)
}

fn array_expression(
    context: &mut impl LocalContext,
    allocation: &u32,
    elements: &[LocalArrayElement],
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let transfer = allocation_transfer::array(
        &mut LocalValueContext {
            context,
            frame,
            source_site: None,
        },
        *allocation,
        elements.iter().map(|element| match element {
            LocalArrayElement::Hole => ArrayElement::Hole,
            LocalArrayElement::Value(value) => ArrayElement::Value(value),
            LocalArrayElement::Spread { value, span } => ArrayElement::Spread(value, *span),
        }),
        AllocationJudgment::CoveredAllocationOnly(Box::default()),
        |context, node, _prefix_reached| {
            let mut judgment = AllocationJudgment::CoveredAllocationOnly(Box::default());
            let Some(selected) =
                expression(context.context, node, body, context.frame, &mut judgment)
            else {
                return if context.frame.remainder_summarized.get() {
                    ChildTransfer::RemainderAfterPrefix { summarized: true }
                } else {
                    ChildTransfer::BeforeExecution
                };
            };
            context.frame.inputs.extend(selected.values.iter().cloned());
            ChildTransfer::Value(allocation_value_for_selected(
                context.frame,
                selected,
                judgment,
                ProofDependencies::default(),
                context.heap_resolver(),
            ))
        },
    );
    let result = receive_allocation_transfer(frame, transfer)?;
    Some(absorb_allocation_value(
        result,
        frame,
        judgment,
        context.heap_resolver(),
    ))
}

fn rest_parameter_array(
    context: &mut impl LocalContext,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
    site: &SourceOperationSite,
    allocation: u32,
    arguments: &[Values],
) -> Option<SelectedValue> {
    if site.step != OperationStep::Allocate {
        return None;
    }
    let address = context.rest_parameter_allocation(site, allocation)?;
    let elements: Vec<_> = arguments.to_vec();
    frame
        .inputs
        .extend(elements.iter().flat_map(|values| values.iter().cloned()));
    let transfer = allocation_transfer::array_at(
        &mut LocalValueContext {
            context,
            frame,
            source_site: Some(site),
        },
        allocation,
        address,
        elements.iter().map(ArrayElement::Value),
        AllocationJudgment::CoveredAllocationOnly(Box::default()),
        |_context, values, _prefix_reached| {
            ChildTransfer::Value(AllocationValue::normal(
                values.clone(),
                AllocationJudgment::CoveredAllocationOnly(Box::default()),
            ))
        },
    );
    let result = receive_allocation_transfer(frame, transfer)?;
    Some(absorb_allocation_value(
        result,
        frame,
        judgment,
        context.heap_resolver(),
    ))
}

fn set_construction_expression(
    local: &mut LocalValueContext<'_, impl LocalContext>,
    array: &LocalExpression,
    source: &ValueExpr,
    site: &CallSite,
    source_site: &SourceOperationSite,
    body: &LocalBody,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let argument = expression(local.context, array, body, local.frame, judgment)?;
    if argument.values.is_empty() {
        if local.frame.ledger.completion.has_no_normal_path() {
            return Some(completing_selected(
                argument,
                local.frame,
                local.context.heap_resolver(),
            ));
        }
        local.frame.mark_opaque();
        return None;
    }
    local.frame.inputs.extend(argument.values.iter().cloned());
    let result =
        local
            .context
            .set_construction(local.frame, source, &argument, source_site, *site)?;
    let selected = result.absorb(local.frame, judgment, local.context.heap_resolver())?;
    local.frame.inputs.extend(selected.values.iter().cloned());
    Some(selected)
}

fn sequence_expression(
    context: &mut impl LocalContext,
    expressions: &[LocalExpression],
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let mut selection = None;
    let transfer = allocation_transfer::sequence(
        &mut LocalValueContext {
            context,
            frame,
            source_site: None,
        },
        expressions.iter(),
        AllocationJudgment::CoveredAllocationOnly(Box::default()),
        |context, node, _prefix_reached| {
            let mut judgment = AllocationJudgment::CoveredAllocationOnly(Box::default());
            let Some(selected) =
                expression(context.context, node, body, context.frame, &mut judgment)
            else {
                return if context.frame.remainder_summarized.get() {
                    ChildTransfer::RemainderAfterPrefix { summarized: true }
                } else {
                    ChildTransfer::BeforeExecution
                };
            };
            selection = selected.selection.clone();
            ChildTransfer::Value(allocation_value_for_selected(
                context.frame,
                selected,
                judgment,
                ProofDependencies::default(),
                context.heap_resolver(),
            ))
        },
    );
    let result = receive_allocation_transfer(frame, transfer)?;
    let selected = absorb_allocation_value(result, frame, judgment, context.heap_resolver());
    let selection = selection.filter(|selection| selection.matches(&selected.values));
    Some(SelectedValue {
        values: selected.values,
        selection,
        postcondition: None,
    })
}

fn closure_expression(
    context: &mut impl LocalContext,
    callable: &DeferredCallableCreation,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let result = context.closure(frame, *callable)?;
    result.absorb(frame, judgment, context.heap_resolver())
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

struct UpdateBindingOperation<'a> {
    binding: &'a Id,
    span: &'a Span,
    read_site: Option<SourceOperationSite>,
    source_site: Option<SourceOperationSite>,
    body: &'a LocalBody,
}

fn update_binding_expression(
    context: &mut impl LocalContext,
    operation: UpdateBindingOperation<'_>,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let UpdateBindingOperation {
        binding,
        span,
        read_site,
        source_site,
        body,
    } = operation;
    let operand = read_binding_selected(context, binding, frame, judgment, read_site.as_ref())?;
    if operand.values.is_empty() {
        if frame.ledger.completion.has_no_normal_path() {
            return Some(operand);
        }
        frame.mark_opaque();
        return None;
    }
    let values = match allocation_transfer::update_result(&operand.values) {
        allocation_transfer::PrimitiveOperationResult::Covered(values) => {
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::no_effects());
            values
        }
        allocation_transfer::PrimitiveOperationResult::MayThrow(values) => {
            *judgment = AllocationJudgment::Opaque;
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            frame.mark_opaque();
            values
        }
        allocation_transfer::PrimitiveOperationResult::Abrupt
            if frame.is_normal_next()
                && source_site
                    .as_ref()
                    .is_some_and(|site| site.step == OperationStep::UpdateCoercion)
                && !frame
                    .reached_values
                    .contains(&AbstractValue::Value(Value::Unavailable))
                && !operand
                    .values
                    .contains(&AbstractValue::Value(Value::Unavailable)) =>
        {
            *judgment = AllocationJudgment::Opaque;
            let thrown = SelectedValue::raw(Values::from([AbstractValue::Value(Value::Top)]));
            context.publish_throw(&thrown.values);
            let frontier = source_site.clone().expect("guarded update coercion site");
            frame.complete_throw(KnownThrow {
                value: thrown,
                origin: QualifiedThrowSite {
                    scope: context.heap_resolver().scope(),
                    site: frontier.clone(),
                },
                frontier,
            });
            return Some(SelectedValue::raw(Values::new()));
        }
        allocation_transfer::PrimitiveOperationResult::Abrupt => {
            *judgment = AllocationJudgment::Opaque;
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            frame.mark_opaque();
            Values::from([AbstractValue::Value(Value::Unavailable)])
        }
        allocation_transfer::PrimitiveOperationResult::CoercionRequired => {
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
            context.unproved_operator(frame, &operand.values, *span);
            frame.mark_opaque();
            return None;
        }
    };
    // Both the returned old numeric value and stored new numeric
    // value have these kinds; exact numbers are outside this domain.
    let selected = completing_selected(SelectedValue::raw(values), frame, context.heap_resolver());
    store_binding(context, body, frame, judgment, binding, selected, *span)
}

fn expression_binding(expression: &LocalExpression) -> Option<&Id> {
    match expression.without_source() {
        LocalExpression::Value(ValueExpr::Binding(binding)) => Some(binding),
        _ => None,
    }
}

fn select_call_callee(
    context: &mut impl LocalContext,
    call: &LocalCall,
    callee_getvalue_site: Option<&SourceOperationSite>,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<EvaluatedCallee> {
    let LocalCall {
        invocation,
        callee,
        span,
        ..
    } = call;
    let invocation = invocation.foreign_invocation();
    if let LocalCallee::Member {
        receiver,
        optional_receiver,
        ..
    } = callee
        && (*optional_receiver || expression_binding(receiver).is_none())
    {
        // Resumable receiver evaluation belongs to the typed machine. This
        // scalar boundary refuses before evaluating any part of the reference.
        return None;
    }
    // GetValue(callee) precedes every argument. In particular, an
    // argument assignment must not replace this captured callee.
    let callee = if let Some((selection, effects)) =
        context.native_callee(frame, invocation, callee, *span)
    {
        frame.ledger.effects.compose(&effects);
        EvaluatedCallee::Native {
            selection: Box::new(selection),
            targets: Values::new(),
        }
    } else {
        match callee {
            LocalCallee::Creation(creation) => {
                EvaluatedCallee::Direct(closure_expression(context, creation, frame, judgment)?)
            }
            LocalCallee::Binding(binding) => EvaluatedCallee::Direct(read_call_callee_selected(
                context,
                binding,
                *span,
                frame,
                judgment,
                callee_getvalue_site,
            )?),
            LocalCallee::Member {
                receiver,
                property,
                span: member_span,
                optional_receiver,
            } => {
                if *optional_receiver {
                    return None;
                }
                let receiver = expression_binding(receiver)?;
                if let Some((receivers, result)) =
                    context.imported_member_callee(frame, receiver, property, *member_span)
                {
                    let result = result.absorb(frame, judgment, context.heap_resolver())?;
                    frame.inputs.extend(receivers.iter().cloned());
                    EvaluatedCallee::Member {
                        receiver: receivers,
                        targets: result.values,
                        selection_dependencies: None,
                        array_method: None,
                        array_iteration_refusal: None,
                    }
                } else {
                    let result = value_with_site(
                        context,
                        &ValueExpr::Binding(receiver.clone()),
                        frame,
                        callee_getvalue_site,
                    )?;
                    let selected =
                        absorb_allocation_value(result, frame, judgment, context.heap_resolver());
                    let receivers = selected.values;
                    select_member_callee(
                        context,
                        frame,
                        judgment,
                        MemberReference {
                            receiver_binding: Some(receiver),
                            receiver_selection: selected.selection,
                            receivers,
                            property,
                            member_span: *member_span,
                            call_span: *span,
                            callee_getvalue_site,
                            invocation,
                        },
                    )?
                }
            }
        }
    };
    if frame.ledger.completion.has_no_normal_path() {
        return None;
    }
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
        frame.mark_opaque();
        return None;
    }
    frame.inputs.extend(callee.targets().iter().cloned());
    Some(callee)
}

struct MemberReference<'a> {
    receiver_binding: Option<&'a Id>,
    receiver_selection: Option<CallableSelection>,
    receivers: Values,
    property: &'a str,
    member_span: Span,
    call_span: Span,
    callee_getvalue_site: Option<&'a SourceOperationSite>,
    invocation: ForeignInvocation,
}

/// GetValue on an already evaluated member reference. The receiver is retained
/// by the continuation before arguments or nested calls can replace its binding.
fn select_member_callee(
    context: &mut impl LocalContext,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
    reference: MemberReference<'_>,
) -> Option<EvaluatedCallee> {
    let MemberReference {
        receiver_binding,
        receiver_selection,
        receivers,
        property,
        member_span,
        call_span,
        callee_getvalue_site,
        invocation,
    } = reference;
    if receivers.is_empty() {
        if !frame.ledger.completion.has_no_normal_path() {
            frame.mark_opaque();
        }
        return None;
    }
    let mut array_iteration_refusal = None;
    let array_selection = if invocation == ForeignInvocation::Call {
        context.array_method_callee(&receivers, property, call_span)
    } else {
        Ok(None)
    };
    let result = match array_selection {
        Ok(Some((selection, effects))) => {
            // The selection proves normal, inert GetValue of this native
            // method. Its identity lives in the typed selection, not an empty
            // scalar value that would fabricate an opaque completion.
            frame.ledger.effects.compose(&effects);
            judgment.compose(&AllocationJudgment::Opaque);
            frame.inputs.extend(receivers.iter().cloned());
            return Some(EvaluatedCallee::Member {
                receiver: receivers,
                targets: Values::new(),
                selection_dependencies: None,
                array_method: Some(Box::new(selection)),
                array_iteration_refusal: None,
            });
        }
        Ok(None) => {
            if invocation == ForeignInvocation::Call
                && let Some((selection, effects)) = context.function_call_method_callee(
                    frame,
                    &receivers,
                    property,
                    call_span,
                    callee_getvalue_site?,
                )
            {
                frame.ledger.effects.compose(&effects);
                frame.inputs.extend(receivers.iter().cloned());
                let target_selection =
                    receiver_selection.filter(|selection| selection.matches(&receivers));
                let mut dependencies = selection.dependencies;
                if let Some(target) = &target_selection {
                    dependencies.extend(&target.dependencies);
                }
                return Some(EvaluatedCallee::FunctionCallMethod {
                    method: selection.method,
                    target: SelectedValue {
                        values: receivers,
                        selection: target_selection,
                        postcondition: None,
                    },
                    receiver: None,
                    dependencies: Box::new(dependencies),
                });
            }
            read_field(
                context,
                frame,
                receiver_binding,
                &receivers,
                property,
                member_span,
            )?
        }
        Err(refusal) => {
            array_iteration_refusal = Some(refusal);
            read_field(
                context,
                frame,
                receiver_binding,
                &receivers,
                property,
                member_span,
            )?
        }
    };
    let selection_dependencies = result.ledger.lexical_selection_dependencies();
    let result = result.absorb(frame, judgment, context.heap_resolver())?;
    frame.inputs.extend(receivers.iter().cloned());
    Some(EvaluatedCallee::Member {
        receiver: receivers,
        targets: result.values,
        selection_dependencies,
        array_method: None,
        array_iteration_refusal,
    })
}

fn call_expression(
    context: &mut impl LocalContext,
    call: &LocalCall,
    source_site: &SourceOperationSite,
    callee_getvalue_site: Option<&SourceOperationSite>,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let Some(callee) = select_call_callee(context, call, callee_getvalue_site, frame, judgment)
    else {
        return frame
            .ledger
            .completion
            .has_no_normal_path()
            .then(|| SelectedValue::raw(Values::new()));
    };
    if matches!(call.invocation, LocalInvocation::OptionalCall)
        && !matches!(
            &callee,
            EvaluatedCallee::Native { .. }
                | EvaluatedCallee::Member {
                    array_method: Some(_),
                    ..
                }
        )
    {
        let (may_skip, targets) = continuation::split_nullish(callee.targets()).ok()?;
        if may_skip {
            if targets.is_empty() {
                frame
                    .ledger
                    .effects
                    .compose(&EffectCoverageFragment::default());
                return Some(SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])));
            }
            // This compatibility adapter cannot own two worlds. The shared
            // machine handles this split before either argument path executes.
            context.unproved_optional_call(frame, callee.targets(), call.span);
            return None;
        }
    }
    let LocalCall {
        invocation,
        arguments,
        span,
        ..
    } = call;
    let invocation = invocation.foreign_invocation();
    let mut inputs = Vec::with_capacity(arguments.len());
    for argument in arguments.iter() {
        let selected = expression(context, argument, body, frame, judgment)?;
        if selected.values.is_empty() {
            if frame.ledger.completion.has_no_normal_path() {
                return Some(SelectedValue::raw(Values::new()));
            }
            frame.mark_opaque();
            return None;
        }
        frame.inputs.extend(selected.values.iter().cloned());
        inputs.push(selected);
    }
    let result = context.call(frame, invocation, &callee, &inputs, source_site, *span)?;
    result.absorb(frame, judgment, context.heap_resolver())
}

fn read_field_expression(
    context: &mut impl LocalContext,
    receiver: &Id,
    property: &str,
    span: &Span,
    source_site: Option<&SourceOperationSite>,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    if let Some(result) = context.native_field_read(frame, receiver, property, *span) {
        return result.absorb(frame, judgment, context.heap_resolver());
    }
    let receivers = value_with_site(
        context,
        &ValueExpr::Binding(receiver.clone()),
        frame,
        source_site,
    )?;
    let receivers =
        absorb_allocation_value(receivers, frame, judgment, context.heap_resolver()).values;
    if receivers.is_empty() {
        if frame.ledger.completion.has_no_normal_path() {
            return Some(SelectedValue::raw(Values::new()));
        }
        frame.mark_opaque();
        return None;
    }
    let result = read_field(context, frame, Some(receiver), &receivers, property, *span)?;
    result.absorb(frame, judgment, context.heap_resolver())
}

struct AssignFieldOperation<'a> {
    receiver: &'a Id,
    property: &'a str,
    value: &'a LocalExpression,
    span: &'a Span,
    receiver_source_site: Option<&'a SourceOperationSite>,
    body: &'a LocalBody,
}

fn select_field_reference(
    local: &mut LocalValueContext<'_, impl LocalContext>,
    receiver: &Id,
    receiver_source_site: Option<&SourceOperationSite>,
    judgment: &mut AllocationJudgment,
) -> Option<(Values, bool)> {
    // Capture the LHS reference before the RHS can replace its binding.
    let receivers = value_with_site(
        local.context,
        &ValueExpr::Binding(receiver.clone()),
        local.frame,
        receiver_source_site,
    )?;
    let receivers = absorb_allocation_value(
        receivers,
        local.frame,
        judgment,
        local.context.heap_resolver(),
    )
    .values;
    if receivers.is_empty() {
        if local.frame.ledger.completion.has_no_normal_path() {
            return None;
        }
        local.frame.mark_opaque();
        return None;
    }
    // Capture the applicable module-prefix lane alongside the LHS
    // reference. The RHS may rebind the source cell, but JavaScript
    // has already evaluated the original receiver value.
    let module_prefix = local.context.module_prefix_receiver(receiver, &receivers);
    Some((receivers, module_prefix))
}

fn assign_field_expression(
    local: &mut LocalValueContext<'_, impl LocalContext>,
    operation: AssignFieldOperation<'_>,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    let AssignFieldOperation {
        receiver,
        property,
        value: node,
        span,
        receiver_source_site,
        body,
    } = operation;
    let Some((receivers, module_prefix)) =
        select_field_reference(local, receiver, receiver_source_site, judgment)
    else {
        return local
            .frame
            .ledger
            .completion
            .has_no_normal_path()
            .then(|| SelectedValue::raw(Values::new()));
    };
    let selected = expression(local.context, node, body, local.frame, judgment)?;
    if selected.values.is_empty() {
        if local.frame.ledger.completion.has_no_normal_path() {
            return Some(selected);
        }
        local.frame.mark_opaque();
        return None;
    }
    store_field_selected(local, receivers, module_prefix, property, selected, span)
}

fn store_field_selected(
    local: &mut LocalValueContext<'_, impl LocalContext>,
    receivers: Values,
    module_prefix: bool,
    property: &str,
    selected: SelectedValue,
    span: &Span,
) -> Option<SelectedValue> {
    let (slot, store_postcondition) = if module_prefix {
        match own_data_transfer::resolve_writable_slot(local.context, &receivers, property) {
            Some(writable_slot) => (
                writable_slot.slot().clone(),
                own_data_transfer::OwnPropertyStorePostcondition::ReplaceExistingWritableData(
                    writable_slot,
                ),
            ),
            None => {
                local.context.unproved_field_write(
                    local.frame,
                    &receivers,
                    &selected.values,
                    *span,
                );
                return None;
            }
        }
    } else {
        let Some(slot) = own_data_field(local.context, &receivers, property, local.frame).ok()
        else {
            local
                .context
                .unproved_field_write(local.frame, &receivers, &selected.values, *span);
            return None;
        };
        let postcondition = own_data_transfer::store_postcondition(local.context, slot.clone());
        (slot, postcondition)
    };
    let writable = matches!(
        &store_postcondition,
        own_data_transfer::OwnPropertyStorePostcondition::ReplaceExistingWritableData(_)
    );
    let write_dependencies = writable.then(|| local.context.selection_prerequisites());
    let support_receipt = writable.then(|| {
        local.context.operation_support_receipt(
            local.frame,
            OperationSupportOperation::WritableOwnDataStore,
            slot.object,
            write_dependencies
                .as_ref()
                .expect("writable proof captures its prefix dependencies"),
        )
    });
    for cell in store_postcondition.write_cells() {
        local.context.record_property_write(cell);
    }
    let writable_ordinary_record = writable
        && receivers.len() == 1
        && matches!(
            receivers.iter().next(),
            Some(AbstractValue::Value(Value::Alloc(Kind::Object, object)))
                if *object == slot.object
        );
    if writable_ordinary_record {
        local.context.heap().write_existing_data_value(
            slot.object,
            property.to_owned(),
            &selected.values,
        );
    } else {
        local.context.heap().write_property(
            slot.object,
            Some(property.to_owned()),
            &selected.values,
        );
    }
    if writable_ordinary_record
        && support_receipt.as_ref().is_some_and(Option::is_some)
        && let (Some(receiver), Some(source_site)) = (
            receivers.iter().next(),
            local
                .source_site
                .filter(|site| site.step == OperationStep::FieldPutValue),
        )
        && store_postcondition.proves_replacement_for(receiver, property)
    {
        local
            .frame
            .own_property_store_receipts
            .push(OwnPropertyStoreReceipt {
                source_site: source_site.clone(),
                receiver: receiver.clone(),
                postcondition: store_postcondition.clone(),
            });
    }
    if let (true, Some(Some(receipt))) = (writable, support_receipt) {
        let mut effects = EffectCoverageFragment::modeled_own_data_access();
        effects.depend_on(
            write_dependencies
                .as_ref()
                .expect("writable proof captures its prefix dependencies")
                .as_ref(),
        );
        local
            .context
            .record_property_read(crate::facts::EffectCell::ObjectShape(slot.object));
        effects.operation_support(receipt.clone());
        local.context.record_operation_support(receipt);
        local.frame.ledger.effects.compose(&effects);
    } else {
        local
            .frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        // A writable slot can still lack a transportable support receipt; that
        // loses coverage but does not invalidate the operation's normality.
        // MayCreateOrRedefine, however, leaves PutValue's abrupt behavior
        // unproved (for example, an accessor or a non-writable property).
        if !writable {
            local.frame.mark_opaque();
        }
    }
    Some(selected)
}

/// Scalar consumers drive the same class steps as runtime families. A fork is
/// refused after preserving the evaluated prefix, before either dependent arm.
fn define_class(
    definition: &ClassDefinition,
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    LocalContinuation::evaluate_expression(
        context,
        body,
        frame,
        judgment,
        LocalExpression::ClassDefinition(definition.clone()),
    )
}

/// This witness describes this activation's class creation. It grants no
/// pooled own-presence certificate and never predefines future static fields.
fn begin_class_definition(
    definition: &ClassDefinition,
    context: &impl LocalContext,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<(SelectedValue, Option<Values>)> {
    if !frame.ledger.is_exact_normal() {
        frame.mark_opaque();
        return None;
    }
    let resolver = context.heap_resolver();
    let object = resolver.class_object(resolver.class_definition(definition.class));
    let selected = SelectedValue::raw(Values::from([AbstractValue::Value(Value::Alloc(
        Kind::Class,
        object,
    ))]));
    judgment.compose(&AllocationJudgment::CoveredAllocationOnly(Box::default()));
    let previous_this = frame.this_values.replace(selected.values.clone());
    frame
        .class_definition_witnesses
        .push(ClassDefinitionWitness::ordinary_class(
            object,
            &definition.static_member_descriptors,
        ));
    Some((selected, previous_this))
}

fn validate_class_static_value(
    frame: &mut LocalFrame,
    may_escape: bool,
    value: &SelectedValue,
) -> Option<()> {
    if may_escape || !frame.ledger.is_exact_normal() || value.values.is_empty() {
        if let Some(witness) = frame.class_definition_witnesses.last_mut() {
            witness.valid = false;
        }
        frame.mark_opaque();
        return None;
    }
    frame
        .class_definition_witnesses
        .last()
        .filter(|witness| witness.valid)?;
    Some(())
}

fn define_class_static_field(
    context: &mut impl LocalContext,
    frame: &mut LocalFrame,
    property: &str,
    value: &SelectedValue,
) -> Option<()> {
    let witness = frame.class_definition_witnesses.last_mut()?;
    let object = witness.object;
    if !witness.can_define(object, property) {
        frame.mark_opaque();
        return None;
    }
    let attributes = DescriptorAttributes::literal(true);
    if !context.create_class_data_property(object, property, &value.values, attributes) {
        frame.mark_opaque();
        return None;
    }
    witness
        .own_descriptors
        .insert(property.to_owned(), attributes);
    context.record_property_write(crate::facts::EffectCell::ObjectShape(object));
    context.record_property_write(crate::facts::EffectCell::OwnProperty {
        origin: object,
        property: property.to_owned(),
    });
    frame
        .ledger
        .effects
        .compose(&EffectCoverageFragment::modeled_own_data_access());
    Some(())
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
            && frame.ledger.closed_effect_dependencies().is_some())
    {
        return None;
    }
    let receiver = frame.constructor_receiver.as_mut()?;
    let expected = Values::from([AbstractValue::Value(Value::Alloc(
        Kind::Object,
        context.constructor_instance_address(receiver.class),
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
    define_this_field_selected(context, property, selected, frame, judgment)
}

fn define_this_field_selected(
    context: &mut impl LocalContext,
    property: &ClassFieldKey,
    selected: SelectedValue,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    if selected.values.is_empty() {
        if frame.ledger.completion.has_no_normal_path() {
            return Some(selected);
        }
        frame.mark_opaque();
        return None;
    }
    if selected
        .values
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        return None;
    }
    let receiver = constructor_receiver(context, frame, judgment)?;
    let class = receiver.class;
    let object = context.constructor_instance_address(class);
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
            context.heap().create_data_property(
                object,
                property.clone(),
                &selected.values,
                DescriptorAttributes::literal(true),
            );
            let origin = object;
            context.record_property_write(crate::facts::EffectCell::ObjectShape(origin));
            context.record_property_write(crate::facts::EffectCell::OwnProperty {
                origin,
                property: property.clone(),
            });
        }
        ClassFieldKey::Private(property) => {
            let class = receiver.class;
            let origin = context.constructor_instance_address(class);
            context.record_property_write(crate::facts::EffectCell::PrivateSlot {
                origin,
                class,
                name: property.clone(),
            });
            let property = PrivateFieldId {
                class,
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
                .write_private_property(origin, property, &selected.values)?;
        }
    }
    frame
        .ledger
        .effects
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
    let class = constructor_receiver(context, frame, judgment)?.class;
    let selected = expression(context, node, body, frame, judgment)?;
    assign_public_this_field_selected(context, property, class, selected, frame, judgment)
}

fn assign_public_this_field_selected(
    context: &mut impl LocalContext,
    property: &str,
    class: ClassDefinitionId,
    selected: SelectedValue,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    if selected.values.is_empty() {
        if frame.ledger.completion.has_no_normal_path() {
            return Some(selected);
        }
        frame.mark_opaque();
        return None;
    }
    if selected
        .values
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        return None;
    }
    let receiver = constructor_receiver(context, frame, judgment)?;
    if receiver.class != class {
        return None;
    }
    let object = context.constructor_instance_address(class);
    let existing = receiver.public_fields.get(property);
    let absence = if existing.is_none() {
        Some(context.constructor_assignment_absence(class, property)?)
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
        frame.ledger.effects.compose(&effects);
        context.record_property_write(crate::facts::EffectCell::ObjectShape(object));
    }
    // Existing fields use their actual own descriptor. A new field requires
    // the separate whole-prototype absence proof above; a private slot alone
    // cannot bypass [[Set]] or inherited setters.
    if absence.is_some() {
        context.heap().set_new_own_data_property(
            object,
            property.to_owned(),
            &selected.values,
            DescriptorAttributes::literal(true),
        );
    } else {
        context
            .heap()
            .write_existing_data_value(object, property.to_owned(), &selected.values);
    }
    context.record_property_write(crate::facts::EffectCell::OwnProperty {
        origin: object,
        property: property.to_owned(),
    });
    frame
        .ledger
        .effects
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
    assign_private_this_field_selected(context, property, selected, frame, judgment)
}

fn assign_private_this_field_selected(
    context: &mut impl LocalContext,
    property: &str,
    selected: SelectedValue,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<SelectedValue> {
    if selected.values.is_empty() {
        if frame.ledger.completion.has_no_normal_path() {
            return Some(selected);
        }
        frame.mark_opaque();
        return None;
    }
    if selected
        .values
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        return None;
    }
    let receiver = constructor_receiver(context, frame, judgment)?;
    let class = receiver.class;
    let object = context.constructor_instance_address(class);
    let property = PrivateFieldId {
        class,
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
        .ledger
        .effects
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
) -> Option<LocalExecutionAttempt> {
    if frame.constructor_receiver.as_ref()?.class
        != context.heap_resolver().class_definition(body.class)
        || frame.parameter_inputs.is_some()
        || !frame.environment.is_empty()
    {
        return None;
    }
    context.constructor_phase(ConstructorPhase::Initializers);
    let initializers = execute(context, &body.initializers, frame);
    let initializers = match initializers {
        LocalExecutionAttempt::Completed(result) => result,
        refused @ LocalExecutionAttempt::Refused { .. } => return Some(refused),
    };
    if initializers.ledger.completion.has_no_normal_path() {
        return Some(LocalExecutionAttempt::Completed(initializers));
    }
    if (!matches!(
        initializers.ledger.allocation,
        AllocationJudgment::CoveredAllocationOnly(_)
    ) && !(context
        .constructor_prefix_is_unexposed(context.heap_resolver().class_definition(body.class))
        && initializers.ledger.closed_effect_dependencies().is_some()))
        || !frame.is_normal_next()
    {
        let result = refuse_result(initializers, frame);
        return Some(LocalExecutionAttempt::Refused {
            result,
            remainder: if frame.remainder_summarized.get() {
                RemainderDisposition::AlreadySummarized
            } else {
                RemainderDisposition::NeedsSummary
            },
        });
    }
    frame.parameter_inputs = Some(arguments);
    context.constructor_phase(ConstructorPhase::Body);
    match execute(context, &body.constructor, frame) {
        LocalExecutionAttempt::Completed(mut result) => {
            result
                .ledger
                .allocation
                .compose(&initializers.ledger.allocation);
            Some(LocalExecutionAttempt::Completed(result))
        }
        LocalExecutionAttempt::Refused {
            mut result,
            remainder,
        } => {
            result
                .ledger
                .allocation
                .compose(&initializers.ledger.allocation);
            Some(LocalExecutionAttempt::Refused { result, remainder })
        }
    }
}
// Parameter cells exist in TDZ before any default expression runs. The
// continuation kernel initializes them in source order, suspending calls between
// these two shared operations without instantiating body declarations early.
fn instantiate_parameters(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) {
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
            .ledger
            .effects
            .compose(&context.declarative_binding_operation(
                &parameter.binding,
                frame,
                BindingOperation::Create,
            ));
    }
}

fn bind_parameter(
    context: &mut impl LocalContext,
    frame: &mut LocalFrame,
    binding: &Id,
    selected: SelectedValue,
) {
    let selection = selected
        .selection
        .filter(|selection| selection.matches(&selected.values));
    frame.inputs.extend(selected.values.iter().cloned());
    frame.environment.insert(binding.clone(), selected.values);
    if let Some(selection) = selection {
        frame.binding_selections.insert(binding.clone(), selection);
    } else {
        frame.binding_selections.remove(binding);
    }
    context.local_binding_stored(binding, frame);
    frame.initialized_bindings.insert(binding.clone());
    frame
        .ledger
        .effects
        .compose(&context.declarative_binding_operation(
            binding,
            frame,
            BindingOperation::Initialize,
        ));
}

/// Syntax coverage comes from LocalBody's canonical lowering. The judgment
/// composes every reached operation, including values discarded by a sequence.
pub(super) fn execute(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) -> LocalExecutionAttempt {
    let owned = std::mem::replace(frame, LocalFrame::new(Context::new()));
    let mut continuation = LocalContinuation::new(owned);
    let attempt = loop {
        match continuation.poll(context, body) {
            LocalSuspension::Call(call) => {
                let result = context.call(
                    &mut continuation.frame,
                    call.invocation,
                    &call.callee,
                    &call.arguments,
                    &call.source_site,
                    call.span,
                );
                continuation.resume_call(context, result);
            }
            LocalSuspension::Complete(attempt) => break attempt,
            LocalSuspension::Fork(_) => continuation.refuse_branching(),
        }
    };
    *frame = continuation.frame;
    attempt
}

fn prepare_body_prefix(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) -> Option<()> {
    frame.active_body_span = body.source_span;
    frame.remainder_summarized.set(false);
    if body.has_unscoped_lexical_bindings {
        frame.mark_opaque();
        return None;
    }
    // A named function's immutable self binding exists before parameter
    // defaults. Preserve the selected callee's original closure identity.
    for (binding, kind) in &body.activation_bindings {
        if let LocalBinding::NamedSelf(function) = kind
            && frame.environment.get(binding).is_some_and(|values| {
                values.len() == 1
                    && values.first().is_some_and(|value| {
                        source_callable_value_matches(value, context.heap_resolver(), *function)
                    })
            })
            && let Some(effects) = context.named_self_binding(binding, frame)
        {
            frame.ledger.effects.compose(&effects);
            if let Some(selection) =
                frame
                    .ledger
                    .lexical_selection_dependencies()
                    .and_then(|dependencies| {
                        CallableSelection::observed(&frame.environment[binding], dependencies)
                    })
            {
                frame.binding_selections.insert(binding.clone(), selection);
            }
            frame.initialized_bindings.insert(binding.clone());
        }
    }
    Some(())
}

fn prepare_body_bindings(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) -> Option<()> {
    for (binding, kind) in &body.bindings {
        if *kind == LocalBinding::CatchParameter {
            frame.environment.remove(binding);
            frame.binding_selections.remove(binding);
            frame.binding_postconditions.remove(binding);
            frame.initialized_bindings.remove(binding);
        }
    }
    for (binding, kind) in &body.activation_bindings {
        match kind {
            LocalBinding::Parameter => {}
            LocalBinding::CatchParameter => unreachable!("not activation scoped"),
            LocalBinding::NamedSelf(function) | LocalBinding::HoistedFunction(function) => {
                // A pre-prologue parameter/var value is not the hoisted
                // function. Preserve only an already-established exact value;
                // an unsupported activation value remains absent until use.
                if frame.environment.get(binding).is_some_and(|values| {
                    values.is_empty()
                        || values.iter().any(|value| {
                            !source_callable_value_matches(
                                value,
                                context.heap_resolver(),
                                *function,
                            )
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
    for (binding, kind) in &body.activation_bindings {
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
        frame.ledger.effects.compose(&effects);
    }
    Some(())
}

fn finish_body_control(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) -> Option<()> {
    // Source throws and structured handlers need the complete callable's call
    // inventory, including a source suffix omitted by canonical lowering.
    if (frame.activation.is_abrupt()
        || body.operations.iter().any(|operation| {
            matches!(
                operation.without_source(),
                LocalOperation::Throw(..)
                    | LocalOperation::TryCatch { .. }
                    | LocalOperation::If { .. }
            )
        }))
        && let Some(region) = body
            .source_region_site
            .as_ref()
            .filter(|site| site.step == OperationStep::BodyRegion)
    {
        let frontier = match &frame.ledger.completion {
            Completion::Normal(LocalNormal::Return { frontier, .. }) => {
                Some((frontier.clone(), SkippedCallReason::KnownReturn))
            }
            Completion::Abrupt(thrown) => {
                Some((thrown.frontier.clone(), SkippedCallReason::KnownThrow))
            }
            _ => None,
        };
        if let Some((frontier, reason)) = frontier
            && !context.skipped_region(frame, region, &frontier, reason)
        {
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
        }
    }
    Some(())
}

fn initialize_binding(
    context: &mut impl LocalContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    binding: &Id,
    selected: SelectedValue,
) {
    let SelectedValue {
        values,
        selection,
        postcondition,
    } = selected;
    if matches!(body.bindings[binding], LocalBinding::Const)
        && let Some(proof) =
            postcondition.filter(|proof| proof.matches(context.heap_resolver(), &values))
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
        .ledger
        .effects
        .compose(&context.declarative_binding_operation(
            binding,
            frame,
            BindingOperation::Initialize,
        ));
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum RegionCompletionAction {
    Continue,
    /// The normal projection falls through, but unresolved behavior remains
    /// in the ledger. This permits ordered suffix analysis without proving
    /// exact completion or coverage.
    ContinueMayNormal,
    Stop,
    Refuse,
}

fn completion_region_action(frame: &LocalFrame) -> RegionCompletionAction {
    match &frame.ledger.completion {
        Completion::Normal(LocalNormal::Next) => RegionCompletionAction::Continue,
        Completion::Normal(LocalNormal::Return { .. })
        | Completion::Abrupt(_)
        | Completion::UnknownThrow(_) => RegionCompletionAction::Stop,
        Completion::OpaqueMayNormal(LocalNormal::Next) => RegionCompletionAction::ContinueMayNormal,
        Completion::Normal(LocalNormal::Unknown(_))
        | Completion::OpaqueMayNormal(LocalNormal::Unknown(_)) => RegionCompletionAction::Refuse,
        Completion::OpaqueMayNormal(LocalNormal::Return { .. }) => RegionCompletionAction::Stop,
        Completion::Alternatives { normal: None, .. }
        | Completion::Alternatives {
            normal: Some(LocalNormal::Return { .. }),
            ..
        } => RegionCompletionAction::Stop,
        Completion::Alternatives {
            normal: Some(LocalNormal::Next),
            abrupt,
            non_normal,
        } if abrupt.is_empty() && *non_normal == NonNormalPossibilities::default() => {
            RegionCompletionAction::Continue
        }
        Completion::Alternatives {
            normal: Some(LocalNormal::Next),
            abrupt,
            non_normal,
        } if abrupt.is_empty() && *non_normal != NonNormalPossibilities::default() => {
            RegionCompletionAction::ContinueMayNormal
        }
        Completion::Alternatives { .. } => RegionCompletionAction::Refuse,
    }
}

fn refuse_unpartitioned_completion(context: &mut impl LocalContext, frame: &LocalFrame) {
    if let Some(span) = frame.active_body_span {
        context.unproved_control_flow(frame, span);
    }
}

#[derive(Clone)]
struct RegionBindingSnapshot {
    binding: Id,
    environment: Option<Values>,
    selection: Option<CallableSelection>,
    postcondition: Option<Box<NativeObjectPostcondition>>,
    initialized: bool,
}

#[derive(Clone)]
struct LocalRegionSnapshot(Vec<RegionBindingSnapshot>);

impl LocalRegionSnapshot {
    fn enter(
        context: &mut impl LocalContext,
        bindings: &BTreeMap<Id, LocalBinding>,
        frame: &mut LocalFrame,
    ) -> Option<Self> {
        if bindings
            .values()
            .any(|kind| !matches!(kind, LocalBinding::Let | LocalBinding::Const))
        {
            return None;
        }
        let snapshot = Self(
            bindings
                .keys()
                .map(|binding| RegionBindingSnapshot {
                    binding: binding.clone(),
                    environment: frame.environment.get(binding).cloned(),
                    selection: frame.binding_selections.get(binding).cloned(),
                    postcondition: frame.binding_postconditions.get(binding).cloned(),
                    initialized: frame.initialized_bindings.contains(binding),
                })
                .collect(),
        );
        for binding in bindings.keys() {
            frame.environment.insert(
                binding.clone(),
                Values::from([AbstractValue::Value(Value::Unavailable)]),
            );
            frame.binding_selections.remove(binding);
            frame.binding_postconditions.remove(binding);
            frame.initialized_bindings.remove(binding);
        }
        if !context.enter_region_bindings(bindings, frame) {
            snapshot.restore(frame);
            return None;
        }
        for binding in bindings.keys() {
            frame
                .ledger
                .effects
                .compose(&context.declarative_binding_operation(
                    binding,
                    frame,
                    BindingOperation::Create,
                ));
        }
        Some(snapshot)
    }

    fn restore(self, frame: &mut LocalFrame) {
        for entry in self.0 {
            match entry.environment {
                Some(values) => {
                    frame.environment.insert(entry.binding.clone(), values);
                }
                None => {
                    frame.environment.remove(&entry.binding);
                }
            }
            match entry.selection {
                Some(selection) => {
                    frame
                        .binding_selections
                        .insert(entry.binding.clone(), selection);
                }
                None => {
                    frame.binding_selections.remove(&entry.binding);
                }
            }
            match entry.postcondition {
                Some(postcondition) => {
                    frame
                        .binding_postconditions
                        .insert(entry.binding.clone(), postcondition);
                }
                None => {
                    frame.binding_postconditions.remove(&entry.binding);
                }
            }
            if entry.initialized {
                frame.initialized_bindings.insert(entry.binding);
            } else {
                frame.initialized_bindings.remove(&entry.binding);
            }
        }
    }
}

fn initialize_object_selected(
    context: &mut impl LocalContext,
    body: &LocalBody,
    pattern: &ObjectBindingPattern,
    receivers: SelectedValue,
    frame: &mut LocalFrame,
    judgment: &mut AllocationJudgment,
) -> Option<()> {
    if receivers.values.is_empty() {
        if frame.ledger.completion.has_no_normal_path() {
            return Some(());
        }
        context.unproved_binding_targets(frame, &pattern.properties);
        frame.mark_opaque();
        return None;
    }
    // Keep the evaluated graph reachable if a later property is
    // refused. Neither its RHS nor its earlier reads run again.
    frame.inputs.extend(receivers.values.iter().cloned());
    for (index, property) in pattern.properties.iter().enumerate() {
        let Some(result) = read_field(
            context,
            frame,
            None,
            &receivers.values,
            &property.property,
            property.span,
        ) else {
            context.unproved_binding_targets(frame, &pattern.properties[index..]);
            frame.mark_opaque();
            return None;
        };
        let selected = result.absorb(frame, judgment, context.heap_resolver())?;
        if selected.values.is_empty() {
            if frame.ledger.completion.has_no_normal_path() {
                return Some(());
            }
            context.unproved_binding_targets(frame, &pattern.properties[index..]);
            frame.mark_opaque();
            return None;
        }
        initialize_binding(context, body, frame, &property.binding, selected);
    }
    Some(())
}

fn refuse_frame(frame: &mut LocalFrame) {
    match &frame.ledger.completion {
        Completion::Normal(LocalNormal::Return { selected, .. })
        | Completion::OpaqueMayNormal(LocalNormal::Return { selected, .. }) => {
            frame.reached_values.extend(selected.values.iter().cloned());
        }
        Completion::Normal(LocalNormal::Unknown(selected))
        | Completion::OpaqueMayNormal(LocalNormal::Unknown(selected)) => {
            frame.reached_values.extend(selected.values.iter().cloned());
        }
        Completion::Abrupt(thrown) => {
            frame
                .reached_values
                .extend(thrown.value.values.iter().cloned());
        }
        Completion::UnknownThrow(values) => {
            frame.reached_values.extend(values.0.iter().cloned());
        }
        Completion::Normal(LocalNormal::Next) | Completion::OpaqueMayNormal(LocalNormal::Next) => {}
        Completion::Alternatives {
            normal,
            abrupt,
            non_normal,
        } => {
            match normal {
                Some(LocalNormal::Return { selected, .. }) => {
                    frame.reached_values.extend(selected.values.iter().cloned());
                }
                Some(LocalNormal::Unknown(selected)) => {
                    frame.reached_values.extend(selected.values.iter().cloned());
                }
                Some(LocalNormal::Next) | None => {}
            }
            frame
                .reached_values
                .extend(local_abrupt_values(abrupt, *non_normal));
        }
    }
    frame.ledger.completion =
        Completion::OpaqueMayNormal(LocalNormal::Unknown(SelectedValue::raw(Values::from([
            AbstractValue::Value(Value::Top),
        ]))));
    frame.mark_opaque();
}

fn refuse_result(mut result: LocalTransferResult, frame: &mut LocalFrame) -> LocalTransferResult {
    refuse_frame(frame);
    result.ledger.completion = frame.ledger.completion.clone();
    result.ledger.allocation = AllocationJudgment::Opaque;
    result
}

fn finish_refused(frame: &mut LocalFrame) -> LocalTransferResult {
    refuse_frame(frame);
    finish(frame, AllocationJudgment::Opaque)
}

fn finish(frame: &LocalFrame, mut judgment: AllocationJudgment) -> LocalTransferResult {
    if frame
        .reached_values
        .contains(&AbstractValue::Value(Value::Unavailable))
    {
        judgment = AllocationJudgment::Opaque;
    }
    LocalTransferResult {
        ledger: TransferLedger::new(
            frame.ledger.completion.clone(),
            frame.ledger.effects.clone(),
            Box::new((*frame.ledger.dependencies).clone()),
            judgment,
        ),
    }
}
