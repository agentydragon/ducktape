//! Call-site execution effects distinguish function values from function bodies: returning a closure never executes it.

use super::adapters::application::{LocalExecutionContext, LocalExecutionScope};
use super::alternatives::execution_state::{ExecutionPath, JoinedPath};
use super::alternatives::{Family, Leaf, join_frames};
use super::array_transfer::ArrayContext;
use super::effect_coverage::{
    BindingEffectCoverage, EffectCoverageFragment, LocalBodyIdentity, LocalCallReceipt,
    RootInitializerIdentity,
};
use super::execution_index::{
    Call, CallKind, Callable, CallablePrefixEffects, CallbackResultWrite, ExecutionIndex,
    LocalConstructor, MemberValueWrite, NativePointEffects, Parameter, RetainedCallbackCall,
    native_call_identity_key, native_own_data_read_slots, pattern_source,
};
use super::kernel::closure::{CapturePolicy, lexical_captures};
use super::kernel::construct;
use super::kernel::context::ControlContext;
use super::kernel::continuation::LocalContinuation;
use super::kernel::driver;
use super::kernel::frame::{
    CallableSelection, LocalExecutionAttempt, LocalFrame, RemainderDisposition, SelectedValue,
};
use super::local_operations::{LocalBinding, LocalBody};
use super::native::operation::{
    self as native_operation, CheckedNativePlan, NativeOperationCommit, NativeOperationContext,
};
use super::noninvoking_initialization::{
    NativeConstructorCandidate, NativeConstructorForm, NativeConstructorKind,
    NativeConstructorTransfer, OriginalInitializer,
};
use super::own_data_transfer::{self, OwnDataContext, OwnDataOwnership, OwnDataRead};
use super::runtime_callbacks::{
    BoundedInvocationFallbackDiagnostics, BoundedInvocationFallbackRequest, CallbackOperation,
    CallbackResult,
};
use super::runtime_heap::{
    HeapOperation, HeapResult, NativeIterableInput, NativeIterableReceipt, NativeIterableUse,
    NativeIterableYields,
};
use super::runtime_inference::{
    CallTransfer, ProgramPoint, RuntimeOperation, RuntimeQueries, RuntimeValue,
};
use super::value_model::{
    AbstractValue, ArrayIteration, CallSite, CaptureInterner, Captured, CapturedContext, Context,
    DenseArray, DenseArrayIterationTransfer, HeapAddress, InstanceOrigin, LexicalCaptures,
    LexicalCellId, MemberKey, OwnPropertyShape, RetainedResultKind, ValueExpr, Values,
    canonical_array_index, ordered_values,
};
use super::world::cells::{CellInitialization, CellTrace};
use super::world::descriptor_heap::{
    self, DescriptorApplication, DescriptorContext, DescriptorHeap, OwnDescriptorMapState,
};
use super::world::object_surface::{AppliedAccessor, ObjectSurfaceStore};
use super::world::prefix_values::{PrefixValues, TopBindings};
use super::world::{World, WorldScope};
use crate::analysis_inputs::{
    AnalysisInputs, ImportedPrimitiveRead, NativeInputKind, NativeIntrinsic, ProofDependencies,
    ResolvedInitializerCallEffectInput,
};
use crate::analysis_provenance::InferenceRule;
use crate::execution_evidence::{
    AssumedEffectReason, ConstructionRejection, ExecutionOperation, ExecutionPrecisionLoss,
    ExecutionPrecisionLossReason, GlobalReachCause, HavocReach, OrderedCallAdmissionRefusal,
    PossibleGetterLookupSite, PossibleGetterPromotion,
};
use crate::facts::ExportReentryPolicy;
use crate::facts::StableArraySpreadShapes;
use crate::facts::abstract_domain::builtins::{
    ARRAY_PROTOTYPE_SLOTS, ArgSel, IntrinsicOp, Invocation, IterationResult, Return, Row, Throws,
};
use crate::facts::abstract_domain::effect::{CoerceOp, CoerceOperand};
use crate::facts::abstract_domain::heap::Mutation;
use crate::facts::abstract_domain::integrity::{Integrity, IntrinsicSlot};
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::callable_inference::CallableInferenceInputs;
use crate::facts::evaluation_effects::{EvaluationEffects, GlobalAccessKind};
use crate::facts::item_views::TopLevelItemView;
use crate::facts::local_effect_targets::MetadataDecorateCall;
use crate::facts::semantic_inference::{
    NativePremise, SemanticFacts, SemanticKey, names_intrinsic, native_premise,
};
use crate::facts::source_unit_read_closure::SourceUnitReadClosure;
use crate::facts::statement_facts::{
    CallbackFactoryBarrierFact, CallbackFactoryBarrierRejection, CallbackFactoryUseFact,
    HeapContentCells, ReturnedRecordUseFact, SettledElements, StatementFacts, StatementKind,
    SynchronousCalls,
};
use crate::facts::sync_effects::property_inference::{
    OwnPropertyProjection, OwnPropertyProof, OwnPropertyRequest, infer_own_property_projection,
};
use crate::facts::value_flow::{ChunkValueFlow, Values as FlowValues};
use crate::facts::{
    DataflowOpacityCause, EffectCell, LocalClassConstructionProof, LocalClassConstructionShapes,
    NativeIterableOperandOrigin, NativeOwnDataReadDiagnostic, SetIterableShapes,
    StatementPurityContext, TopLevelNativeIterableOperandSite, set_iterables::SetIterableProof,
};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use crate::purity::{ChunkCodeGraph, Effects};
use crate::reports::schema::{
    BoundedInvocationFallbackCallSite, BoundedInvocationFallbackInputPath,
    BoundedInvocationFallbackPathEdge, BoundedInvocationFallbackSeed,
    BoundedInvocationFallbackUnknownPathReason, CallbackFactoryUseKind,
    ClassMembersPublicationWriter, FirstClassMembersPublication, LocalRecordFactoryRejection,
    NativeIntegrityCheck, NativeIntegrityGuard, NativePrefixObservation,
    NativePrototypePropertyCheck, RecordFactoryRejectionReason,
};
use analysis_model::PrimKinds;
use imbl::OrdMap;
use rustc_hash::{FxHashMap, FxHashSet};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use swc_common::{DUMMY_SP, Span, SyntaxContext};
use swc_ecma_ast::*;

pub(super) enum NativeIdentitySource {
    Inferred {
        premise: Box<NativePremise>,
    },
    AuthorInput {
        evidence: Box<crate::analysis_inputs::NativeOperationInputEvidence>,
        dependencies: Box<ProofDependencies>,
    },
}

impl NativeIdentitySource {
    pub(super) fn dependencies(&self) -> &ProofDependencies {
        match self {
            Self::Inferred { premise } => &premise.dependencies,
            Self::AuthorInput { dependencies, .. } => dependencies,
        }
    }
}

enum NativeCallIdentity {
    Proved(&'static Row, NativeIdentitySource),
    /// The callee names an intrinsic row whose slots are not intact.
    FailedGuard(&'static [IntrinsicSlot]),
}

/// What iterating a value can run; see `Executor::iteration_proof`.
enum IterationProof {
    Native,
    PrototypeLookup,
    Unknown,
}

/// An ordinary call's local target, for `Executor::execute_local_call_target`.
struct LocalCallTarget<'a> {
    key: u32,
    captured: Option<CapturedContext>,
    args: &'a [Values],
    argument_selections: &'a [Option<CallableSelection>],
    coverage_target: Option<&'a (
        u32,
        LocalBodyIdentity,
        LocalBodyIdentity,
        Box<ProofDependencies>,
    )>,
    single_target: bool,
    needs_value: bool,
    call_span: Span,
}

/// The receiver of a method callee, also behind an optional chain's
/// short-circuit alternative.
fn method_callee_receiver(callee: &ValueExpr) -> Option<&ValueExpr> {
    match callee {
        ValueExpr::Member(receiver, _, _, _) => Some(receiver),
        ValueExpr::Alternatives(options) => options.iter().find_map(method_callee_receiver),
        _ => None,
    }
}

pub(super) fn from_flow(values: FlowValues) -> Values {
    values.into_iter().map(AbstractValue::Value).collect()
}

/// Source-summary lanes flatten assignments, so a read may precede all of
/// them. Only widens: a binding no assignment or environment entry gives a
/// value keeps the lane's fallback for an absent binding, since routes other
/// than `assignments` (a closure's write, an update) may set it. Kept out of
/// line: `Executor::invoke` recurses once per nested call and its frame size
/// bounds the supported call depth.
#[inline(never)]
fn seed_undefined_before_assignment(
    env: &mut Context,
    bindings: &BTreeSet<Id>,
    assignments: &[(Id, ValueExpr)],
) {
    for id in bindings {
        if env.contains_key(id) || assignments.iter().any(|(assigned, _)| assigned == id) {
            env.entry(id.clone())
                .or_default()
                .insert(AbstractValue::UNDEFINED);
        }
    }
}

/// Whether an incoming argument may be `undefined`, so its parameter's
/// default may run: no value, unknown, or a primitive kind set including
/// `undefined`. Only objects, functions and other primitives exclude it.
#[inline(never)]
fn may_be_undefined(values: &Values) -> bool {
    values.is_empty()
        || values.iter().any(|value| match value {
            AbstractValue::Value(Value::Prim(kinds)) => kinds.intersects(PrimKinds::UNDEFINED),
            AbstractValue::Value(Value::Alloc(..))
            | AbstractValue::Closure(..)
            | AbstractValue::ClassMembers(_)
            | AbstractValue::Instance(..) => false,
            _ => true,
        })
}

pub(super) fn unknown() -> Values {
    Values::from([AbstractValue::Value(Value::Top)])
}

#[derive(Debug, Clone, Default)]
pub(super) struct Outcome {
    pub(super) effects: SynchronousCalls,
    /// The callable body was entered, rather than only conservatively
    /// summarized by a recursion or invocation-budget fallback.
    pub(super) body_invoked: bool,
    /// Nonterminal operation receipts, never a whole-owner certificate.
    pub(super) effect_coverage: EffectCoverageFragment,
    /// Set before executing an ordered root initializer; callable outcomes
    /// never carry a root identity and cannot seal their parent's effects.
    pub(super) root_binding_effect_identity: Option<RootInitializerIdentity>,
    pub(super) returns: Values,
    pub(super) return_selection: Option<CallableSelection>,
    pub(super) complete: bool,
    pub(super) executed_owners: BTreeSet<SourceUnitOrdinal>,
    pub(super) unknown_call: bool,
}

impl Outcome {
    fn lose_precision(
        &mut self,
        reason: ExecutionPrecisionLossReason,
        reach: HavocReach,
        owner: SourceUnitOrdinal,
        span: Span,
    ) {
        self.complete = false;
        let loss = ExecutionPrecisionLoss {
            reason,
            reach,
            statement_ordinal: owner,
            source_location: None,
            span,
        };
        self.record_loss(loss);
    }

    /// One record per reason and site, retaining distinct ordered-read and
    /// call-admission refusals. The same site can be reached with different
    /// inputs; its reach is `global` if any reach was.
    fn record_loss(&mut self, loss: ExecutionPrecisionLoss) {
        let losses = &mut self.effects.precision_losses;
        match losses.iter_mut().find(|existing| {
            let same_reason = match (&existing.reason, &loss.reason) {
                (
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: existing_operation,
                        ordered_field_read_refusal: existing_refusal,
                        ordered_call_admission_refusal: existing_call_refusal,
                    },
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: loss_operation,
                        ordered_field_read_refusal: loss_refusal,
                        ordered_call_admission_refusal: loss_call_refusal,
                    },
                ) => {
                    existing_operation == loss_operation
                        && (existing_refusal == loss_refusal
                            || existing_refusal.is_none()
                            || loss_refusal.is_none())
                        && (existing_call_refusal == loss_call_refusal
                            || existing_call_refusal.is_none()
                            || loss_call_refusal.is_none())
                }
                _ => existing.reason == loss.reason,
            };
            same_reason
                && existing.statement_ordinal == loss.statement_ordinal
                && existing.span == loss.span
        }) {
            Some(existing) => {
                if let (
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        ordered_field_read_refusal: existing_field_refusal,
                        ordered_call_admission_refusal: existing_call_refusal,
                        ..
                    },
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        ordered_field_read_refusal: loss_field_refusal,
                        ordered_call_admission_refusal: loss_call_refusal,
                        ..
                    },
                ) = (&mut existing.reason, &loss.reason)
                {
                    if existing_field_refusal.is_none() {
                        *existing_field_refusal = *loss_field_refusal;
                    }
                    if existing_call_refusal.is_none() {
                        *existing_call_refusal = *loss_call_refusal;
                    }
                }
                if existing.reach == HavocReach::Bounded {
                    existing.reach = loss.reach;
                }
            }
            None => losses.push(loss),
        }
    }

    pub(super) fn unsupported(
        &mut self,
        operation: ExecutionOperation,
        owner: SourceUnitOrdinal,
        span: Span,
    ) {
        self.lose_precision(
            ExecutionPrecisionLossReason::UnsupportedOperation {
                operation,
                ordered_field_read_refusal: None,
                ordered_call_admission_refusal: None,
            },
            HavocReach::Global {
                cause: GlobalReachCause::UnmodelledOperation,
            },
            owner,
            span,
        );
    }

    pub(super) fn lose_call_target(&mut self, owner: SourceUnitOrdinal, span: Span) {
        self.lose_precision(
            ExecutionPrecisionLossReason::UnknownCallTarget,
            HavocReach::Global {
                cause: GlobalReachCause::UnknownCallee,
            },
            owner,
            span,
        );
    }

    pub(super) fn inherit_completeness(
        &mut self,
        complete: bool,
        losses: Vec<ExecutionPrecisionLoss>,
    ) {
        self.complete &= complete;
        self.inherit_precision_losses(losses);
    }

    fn inherit_precision_losses(&mut self, losses: Vec<ExecutionPrecisionLoss>) {
        for loss in losses {
            self.record_loss(loss);
        }
    }

    /// Union all nested heap may-cells for conservative ordering, but admit
    /// their explicit-premise subset only from a non-assumed invocation.
    /// Binding effects retain their separate fallback provenance.
    pub(super) fn absorb_call_effects(
        &mut self,
        nested: &mut SynchronousCalls,
        assumption: Option<&AssumedEffectReason>,
    ) {
        if assumption.is_none() {
            self.effects
                .non_assumed_heap_accesses
                .extend(&nested.non_assumed_heap_accesses);
        }
        self.effects
            .heap_contents
            .extend(&std::mem::take(&mut nested.heap_contents));
        let assumed = std::mem::take(&mut nested.fallback_only);
        absorb_effects(
            &mut self.effects.reads,
            &mut self.effects.fallback_only.reads,
            std::mem::take(&mut nested.reads),
            &assumed.reads,
            assumption,
            |(id, _)| id,
            |effects: &BTreeSet<_>| HeldBindings::ByOwner(effects),
        );
        absorb_effects(
            &mut self.effects.rebinds,
            &mut self.effects.fallback_only.rebinds,
            std::mem::take(&mut nested.rebinds),
            &assumed.rebinds,
            assumption,
            |(id, _)| id,
            |effects: &BTreeSet<_>| HeldBindings::ByOwner(effects),
        );
        absorb_effects(
            &mut self.effects.uninitialized_reads,
            &mut self.effects.fallback_only.uninitialized_reads,
            std::mem::take(&mut nested.uninitialized_reads),
            &assumed.uninitialized_reads,
            assumption,
            |id| id,
            |bindings: &BTreeSet<_>| HeldBindings::Plain(bindings),
        );
    }
}

/// The bindings an effect set holds, before new effects are inserted.
#[derive(Clone, Copy)]
enum HeldBindings<'a> {
    /// Reads or rebinds, keyed by binding then owner.
    ByOwner(&'a BTreeSet<(Id, SourceUnitOrdinal)>),
    Plain(&'a BTreeSet<Id>),
}

impl<'a> HeldBindings<'a> {
    fn len(self) -> usize {
        match self {
            Self::ByOwner(effects) => effects.len(),
            Self::Plain(bindings) => bindings.len(),
        }
    }

    fn contains(self, binding: &Id) -> bool {
        match self {
            Self::ByOwner(effects) => effects
                .range((binding.clone(), SourceUnitOrdinal(0))..)
                .next()
                .is_some_and(|(id, _)| id == binding),
            Self::Plain(bindings) => bindings.contains(binding),
        }
    }

    /// In order; a binding repeats once per owner.
    fn iter(self) -> Box<dyn Iterator<Item = &'a Id> + 'a> {
        match self {
            Self::ByOwner(effects) => Box::new(effects.iter().map(|(id, _)| id)),
            Self::Plain(bindings) => Box::new(bindings.iter()),
        }
    }
}

/// Lookups per binding beat a merge walk when the held set is this many times
/// larger.
const PROVENANCE_WALK_RATIO: usize = 16;

/// Tags `bindings` (sorted, distinct), about to be inserted into an effect
/// set, as assumed by `reason` unless `held` already has them: an effect both
/// observed and assumed is observed, and an assumed one keeps its first path.
fn note_assumed(
    assumed: &mut BTreeMap<Id, AssumedEffectReason>,
    bindings: &[&Id],
    held: HeldBindings<'_>,
    reason: &AssumedEffectReason,
) {
    let mut added = Vec::new();
    if bindings.len().saturating_mul(PROVENANCE_WALK_RATIO) < held.len() {
        added.extend(bindings.iter().copied().filter(|id| !held.contains(id)));
    } else {
        let mut held = held.iter().peekable();
        for &id in bindings {
            while held.next_if(|candidate| *candidate < id).is_some() {}
            if held.peek() != Some(&id) {
                added.push(id);
            }
        }
    }
    // Tags only name held bindings, so `added` is disjoint from `assumed`.
    let tags = added.into_iter().map(|id| (id.clone(), reason.clone()));
    if tags.len().saturating_mul(PROVENANCE_WALK_RATIO) < assumed.len() {
        assumed.extend(tags);
    } else {
        assumed.append(&mut tags.collect());
    }
}

/// Clears the tags of `bindings` (sorted, distinct): they are now observed.
fn note_observed(assumed: &mut BTreeMap<Id, AssumedEffectReason>, bindings: &[&Id]) {
    if assumed.is_empty() || bindings.is_empty() {
        return;
    }
    if bindings.len().saturating_mul(PROVENANCE_WALK_RATIO) < assumed.len() {
        for id in bindings {
            assumed.remove(*id);
        }
    } else {
        let mut observed = bindings.iter().peekable();
        assumed.retain(|id, _| {
            while observed.next_if(|candidate| **candidate < id).is_some() {}
            observed.peek().is_none_or(|candidate| **candidate != id)
        });
    }
}

/// Inserts the effects of sorted `bindings`. Large fallback summaries merge
/// in bulk, avoiding a tree lookup for every repeated binding; small additions
/// insert into the larger set.
fn insert_effects<T: Ord>(effects: &mut BTreeSet<T>, bindings: &[&Id], effect: impl Fn(&Id) -> T) {
    if bindings.len() >= 64 && bindings.len().saturating_mul(2) >= effects.len() {
        let mut promoted = bindings.iter().map(|id| effect(id)).collect();
        effects.append(&mut promoted);
    } else {
        effects.extend(bindings.iter().map(|id| effect(id)));
    }
}

fn absorb_effects<T: Ord>(
    target: &mut BTreeSet<T>,
    target_assumed: &mut BTreeMap<Id, AssumedEffectReason>,
    source: BTreeSet<T>,
    source_assumed: &BTreeMap<Id, AssumedEffectReason>,
    assumption: Option<&AssumedEffectReason>,
    binding: impl Fn(&T) -> &Id,
    held: impl Fn(&BTreeSet<T>) -> HeldBindings<'_>,
) {
    if assumption.is_some() || !source_assumed.is_empty() || !target_assumed.is_empty() {
        let mut ids: Vec<&Id> = source.iter().map(&binding).collect();
        ids.dedup();
        match assumption {
            Some(reason) => note_assumed(target_assumed, &ids, held(target), reason),
            None => {
                let (assumed, observed): (Vec<&Id>, Vec<&Id>) = ids
                    .into_iter()
                    .partition(|id| source_assumed.contains_key(*id));
                note_observed(target_assumed, &observed);
                for id in assumed {
                    note_assumed(target_assumed, &[id], held(target), &source_assumed[id]);
                }
            }
        }
    }
    target.extend(source);
}

/// A suboperation proof, never whole-owner initialization coverage.
struct NonInvokingClassHeritageRead<'a> {
    binding: &'a Id,
    dependencies: ProofDependencies,
}

/// A checked non-publication decision, independent of owner completion and
/// timing coverage. Both root suppression and use reporting consume it.
pub(super) struct PrivateCallbackFactoryResult {
    pub(super) proof_use: CallbackFactoryUseFact,
    dependencies: ProofDependencies,
}

/// Callback roots retained by a fallback stay escaped for the rest of the
/// chunk. Cache their synchronous read closure once; initialization timing is
/// checked against the current prefix when the summary is applied.
#[derive(Clone, Default)]
pub(super) struct CallbackFallbackSummary {
    pub(super) expanded: BTreeSet<SourceUnitOrdinal>,
    pub(super) deferred: BTreeMap<Id, SourceUnitOrdinal>,
    pub(super) incomplete_expanded: bool,
    pub(super) reads: BTreeSet<Id>,
    pub(super) rebinds: BTreeSet<Id>,
    /// `reads` in insertion order; the fallback applies a suffix of it.
    pub(super) read_additions: Vec<Id>,
    pub(super) rebind_additions: Vec<Id>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct InvocationSignature {
    callable: u32,
    arguments: Vec<Values>,
    argument_selections: Vec<Option<CallableSelection>>,
    captured_context: Option<CapturedContext>,
    fresh_checked_constructor: bool,
    reachability_revision: u64,
    prefix_revision: u64,
    top_owner: Option<SourceUnitOrdinal>,
}

/// See [`Executor::callback_pool_observation`].
struct CallbackPoolObservation {
    prefix_revision: u64,
    reachability_revision: u64,
    prefix_object_properties: OrdMap<u32, BTreeMap<Option<String>, Values>>,
    dense_arrays: OrdMap<u32, DenseArray>,
    primitive_arrays: OrdMap<u32, ProofDependencies>,
    accumulated_values: OrdMap<u32, Values>,
    applied_accessors: OrdMap<u32, BTreeMap<String, AppliedAccessor>>,
    class_superclasses: OrdMap<u32, Values>,
    set_sizes: [usize; 12],
    array_intrinsic_mutated: bool,
    uncertain_native_execution: bool,
}

impl PartialEq for CallbackPoolObservation {
    fn eq(&self, other: &Self) -> bool {
        // Untouched persistent maps share their root; compare contents only
        // when a pass rebuilt one.
        fn same<K: Ord, V: PartialEq>(left: &OrdMap<K, V>, right: &OrdMap<K, V>) -> bool {
            left.ptr_eq(right) || left == right
        }
        self.prefix_revision == other.prefix_revision
            && self.reachability_revision == other.reachability_revision
            && self.set_sizes == other.set_sizes
            && self.array_intrinsic_mutated == other.array_intrinsic_mutated
            && self.uncertain_native_execution == other.uncertain_native_execution
            && same(
                &self.prefix_object_properties,
                &other.prefix_object_properties,
            )
            && same(&self.dense_arrays, &other.dense_arrays)
            && same(&self.primitive_arrays, &other.primitive_arrays)
            && same(&self.accumulated_values, &other.accumulated_values)
            && same(&self.applied_accessors, &other.applied_accessors)
            && same(&self.class_superclasses, &other.class_superclasses)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct ActiveInvocation {
    callable: u32,
    top_owner: Option<SourceUnitOrdinal>,
}

struct BoundedFallbackPublicationDiagnostic {
    input_paths: BTreeMap<u32, BoundedInvocationFallbackInputPath>,
    callsite: Option<BoundedInvocationFallbackCallSite>,
}

#[derive(Clone, Copy)]
enum StoredReferenceEdge<'a> {
    ObjectProperty(Option<&'a str>),
    ArrayProperty(Option<&'a str>),
    AppliedGetter(&'a str),
    AppliedSetter(&'a str),
    ClosureHomeObject,
    ClosureSnapshot(&'a str),
    ClosureCell(&'a str),
    InstanceRetainedValue,
    DeferredCallbackRetainedValue,
    AuditedResultRetainedValue,
    DescriptorMapRetainedValue,
    EscapedCallbackPoolExpansion,
}

trait CallbackTraversalItem: Sized {
    fn value(&self) -> &AbstractValue;

    fn retained_child(&self, value: &AbstractValue, edge: StoredReferenceEdge<'_>) -> Self;

    fn class_members_path(&self) -> Option<BoundedInvocationFallbackInputPath>;
}

impl CallbackTraversalItem for AbstractValue {
    fn value(&self) -> &AbstractValue {
        self
    }

    fn retained_child(&self, value: &AbstractValue, _edge: StoredReferenceEdge<'_>) -> Self {
        value.clone()
    }

    fn class_members_path(&self) -> Option<BoundedInvocationFallbackInputPath> {
        None
    }
}

struct TracedCallbackValue {
    value: AbstractValue,
    path: BoundedInvocationFallbackInputPath,
}

impl CallbackTraversalItem for TracedCallbackValue {
    fn value(&self) -> &AbstractValue {
        &self.value
    }

    fn retained_child(&self, value: &AbstractValue, edge: StoredReferenceEdge<'_>) -> Self {
        let mut path = self.path.clone();
        if let BoundedInvocationFallbackInputPath::Retained { edges, .. } = &mut path {
            edges.push(path_edge_report(edge));
        }
        Self {
            value: value.clone(),
            path,
        }
    }

    fn class_members_path(&self) -> Option<BoundedInvocationFallbackInputPath> {
        let mut path = self.path.clone();
        if let BoundedInvocationFallbackInputPath::Retained { edges, .. } = &mut path {
            edges.push(BoundedInvocationFallbackPathEdge::ClassMembersCapability);
        }
        Some(path)
    }
}

fn path_edge_report(edge: StoredReferenceEdge<'_>) -> BoundedInvocationFallbackPathEdge {
    match edge {
        StoredReferenceEdge::ObjectProperty(Some(property)) => {
            BoundedInvocationFallbackPathEdge::ObjectProperty {
                property: property.to_owned(),
            }
        }
        StoredReferenceEdge::ObjectProperty(None) => {
            BoundedInvocationFallbackPathEdge::UnknownObjectProperty
        }
        StoredReferenceEdge::ArrayProperty(Some(property)) => {
            if let Some(index) = canonical_array_index(property) {
                BoundedInvocationFallbackPathEdge::ArrayElement { index }
            } else {
                BoundedInvocationFallbackPathEdge::ObjectProperty {
                    property: property.to_owned(),
                }
            }
        }
        StoredReferenceEdge::ArrayProperty(None) => {
            BoundedInvocationFallbackPathEdge::UnknownArrayProperty
        }
        StoredReferenceEdge::AppliedGetter(property) => {
            BoundedInvocationFallbackPathEdge::AppliedGetter {
                property: Some(property.to_owned()),
            }
        }
        StoredReferenceEdge::AppliedSetter(property) => {
            BoundedInvocationFallbackPathEdge::AppliedSetter {
                property: Some(property.to_owned()),
            }
        }
        StoredReferenceEdge::ClosureHomeObject => {
            BoundedInvocationFallbackPathEdge::ClosureHomeObject
        }
        StoredReferenceEdge::ClosureSnapshot(binding) => {
            BoundedInvocationFallbackPathEdge::ClosureSnapshot {
                binding: binding.to_owned(),
            }
        }
        StoredReferenceEdge::ClosureCell(binding) => {
            BoundedInvocationFallbackPathEdge::ClosureCell {
                binding: binding.to_owned(),
            }
        }
        StoredReferenceEdge::InstanceRetainedValue => {
            BoundedInvocationFallbackPathEdge::InstanceRetainedValue
        }
        StoredReferenceEdge::DeferredCallbackRetainedValue => {
            BoundedInvocationFallbackPathEdge::DeferredCallbackRetainedValue
        }
        StoredReferenceEdge::AuditedResultRetainedValue => {
            BoundedInvocationFallbackPathEdge::AuditedResultRetainedValue
        }
        StoredReferenceEdge::DescriptorMapRetainedValue => {
            BoundedInvocationFallbackPathEdge::DescriptorMapRetainedValue
        }
        StoredReferenceEdge::EscapedCallbackPoolExpansion => {
            BoundedInvocationFallbackPathEdge::EscapedCallbackPoolExpansion
        }
    }
}

// Callable cycles can keep changing their argument/capture snapshots and thus
// evade the state-specific memoization key. Bound recursive analysis separately
// from that cache so a finite source program cannot consume the Rust stack.
const MAX_SYNCHRONOUS_INVOCATION_DEPTH: usize = 64;
// Count actual expansions, including those whose changing prefix state defeats
// memoization. A cache-size limit alone does not bound a branching call tree.
pub(super) const MAX_SYNCHRONOUS_INVOCATIONS: usize = 256;
// Giving every callback the entire pool as an argument creates a quadratic
// value cross product, even without recursion. Summarize large pools before
// manufacturing capture snapshots that retain that cross product.
const MAX_SYNCHRONOUS_CALLBACK_POOL: usize = 64;

/// Reuse the canonical nested identifier census, including deferred reads.
/// Borrow declaration keys for this temporary join; retain only the cells
/// that a nested callable can actually capture from an ordered activation.
fn captured_local_bindings(index: &ExecutionIndex) -> FxHashSet<Id> {
    let owners: FxHashMap<_, _> = index
        .callables
        .iter()
        .filter_map(|(key, callable)| Some((*key, callable.body_facts.ordered_plan()?)))
        .filter(|(_, body)| !body.requires_keyed_collection_new())
        .flat_map(|(key, body)| body.bindings.keys().map(move |binding| (binding, key)))
        .collect();
    let mut captured = FxHashSet::default();
    // A nested callable's write must reach its owner's frame: the binding
    // gets a cell, which the writer's ordered lane requires and stores to.
    for (key, callable) in &index.callables {
        for binding in callable.writes() {
            if let Some(owner) = owners.get(binding)
                && owner != key
            {
                captured.insert(binding.clone());
            }
        }
    }
    for (key, reads) in &index.prototype_callable_reads {
        let Some(span) = index.callable_spans.get(key) else {
            continue;
        };
        for binding in reads {
            let Some(owner) = owners.get(binding) else {
                continue;
            };
            if owner == key {
                continue;
            }
            if index
                .callable_spans
                .get(owner)
                .is_some_and(|outer| outer.lo <= span.lo && span.hi <= outer.hi)
            {
                captured.insert(binding.clone());
            }
        }
    }
    captured
}

#[derive(Debug, Clone)]
pub(super) struct NativeCallResolution {
    row: &'static Row,
    /// Every heap mutation of the admitted row, checked against the prefix
    /// the row was admitted in and not yet committed.
    plan: CheckedNativePlan,
    iterable: Option<NativeIterableReceipt>,
    operation_coverage: Option<EffectCoverageFragment>,
    /// Source cells an own-property projection row reads: the object's shape
    /// and, for descriptors, every projected value slot. A closed native
    /// receipt covers the heap domain only together with these reads.
    projected_reads: Vec<EffectCell>,
    /// Prerequisites of the admission: callee identity, operand proofs and
    /// the prefix premises they read. The admitted row runs no user code, so
    /// these also cover the operation's effect domains.
    dependencies: ProofDependencies,
}

pub(super) struct NewEvaluation {
    pub(super) values: Values,
    operation_coverage: Option<EffectCoverageFragment>,
}

#[derive(Clone, Default)]
pub(super) struct RecordFactoryPrototypePremises {
    constructor_fields: BTreeSet<(u32, String)>,
    missing_input_fields: BTreeSet<(u32, String)>,
}

impl RecordFactoryPrototypePremises {
    pub(super) fn is_empty(&self) -> bool {
        self.constructor_fields.is_empty() && self.missing_input_fields.is_empty()
    }

    pub(super) fn properties(&self) -> Vec<String> {
        self.constructor_fields
            .iter()
            .chain(&self.missing_input_fields)
            .map(|(_, property)| property.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

pub(super) struct ExecutionInputs<'source> {
    pub(super) index: &'source ExecutionIndex,
    graph: &'source ChunkCodeGraph,
    pub(super) facts: &'source [StatementFacts],
    /// Each statement's source-only purity transfer, before its coercions
    /// resolve against the values settled at its program point.
    statement_effects: Vec<Effects>,
    pub(super) flow: &'source ChunkValueFlow,
    set_iterable_proof: &'source SetIterableProof,
    pub(super) shadowed: &'source BTreeSet<&'static str>,
    imported_values: &'source BTreeMap<String, Value>,
    imported_primitive_reads: &'source BTreeMap<String, ImportedPrimitiveRead>,
    initializer_call_effect_inputs: &'source [ResolvedInitializerCallEffectInput],
    pub(super) declared_pure_initializers: &'source BTreeSet<String>,
    /// First-order pre-await body cells of each callable.
    pub(super) callable_heap_contents: &'source BTreeMap<u32, HeapContentCells>,
    /// Hashed: effect promotion tests every lazy read of every promoted
    /// callable against it. `binding_owner` holds the same bindings in order.
    pub(super) top_bindings: FxHashSet<Id>,
    binding_owner: BTreeMap<Id, SourceUnitOrdinal>,
    source_unit_read_closure: SourceUnitReadClosure,
    synchronous_owner_adjacency: Vec<Vec<SourceUnitOrdinal>>,
    /// The only declaration in this owner is one direct lexical binding,
    /// with no other declaration site. Only its incoming read edge may wait.
    deferred_read_binding: Vec<Option<Id>>,
    synchronous_owner_effects: Vec<Option<&'source CallablePrefixEffects>>,
    pub(super) exported_owners: BTreeSet<SourceUnitOrdinal>,
    /// The exports code that reenters the module during its evaluation can
    /// read: their bindings, and whether the default export is among them.
    reentry_bindings: BTreeSet<Id>,
    reentry_default: bool,
    pub(super) call_site_owners: BTreeMap<CallSite, (SourceUnitOrdinal, Span)>,
    mutable_captures: BTreeSet<Id>,
    /// Callables whose `arguments` some read reaches: their own, or one in
    /// a callable nested in them (an arrow's `arguments` is its enclosing
    /// function's). Over-approximates for a nested function's own.
    reads_arguments: BTreeSet<u32>,
    /// Per class, the enclosing scope's bindings its members read. A class
    /// value carries no captured context, so a member (a constructor's
    /// `super(...)` operands) reads them as unknown later.
    pub(super) class_captured_reads: BTreeMap<u32, BTreeSet<Id>>,
    /// Locals a source-summary lane reads as unknown in addition to its own
    /// assignments: another callable writes them, or an assignment reads
    /// them before a later one assigns them.
    stale_locals: BTreeSet<Id>,
    /// Locals whose stored values a read may see only as unknown: the
    /// writes above, and a binding some callable writes that a closure
    /// captures (its snapshot is unknown). Every store publishes its value.
    untracked_locals: BTreeSet<Id>,
    pub(super) captured_local_bindings: FxHashSet<Id>,
    pub(super) var_bindings: BTreeSet<Id>,
    changed_member_roots: BTreeSet<Id>,
    export_reentry_enabled: bool,
    pub(super) needed_bindings: BTreeSet<Id>,
    /// Top-level bindings no statement but their declaring one writes. The
    /// prefix values of other bindings may miss a write the index does not
    /// model (a `for-of` head, an update), so coercions never resolve
    /// against them.
    single_writer_bindings: BTreeSet<Id>,
    /// Source units whose own code may call a callee the source does not name.
    unknown_callee_owners: BTreeSet<SourceUnitOrdinal>,
    /// Per source unit: its synchronous read closure contains one of
    /// `unknown_callee_owners`. Built on the first reach classification.
    reaches_unknown_callee: std::sync::OnceLock<Vec<bool>>,
}

pub(super) struct OwnPropertyQuery {
    source_object: u32,
    source_values: Values,
    prefix_properties: BTreeMap<Option<String>, Values>,
    tainted: bool,
    request: OwnPropertyRequest,
}

impl ExecutionInputs<'_> {
    pub(super) fn capture_policy(&self) -> CapturePolicy<'_> {
        CapturePolicy::Application {
            module_bindings: &self.top_bindings,
            mutable: &self.mutable_captures,
        }
    }

    /// Admit a source-checked imported primitive to the ordered kernel. This
    /// grants entry only; each reached read still checks its actual value.
    pub(super) fn imported_primitive_read(&self, binding: &Id) -> Option<&ImportedPrimitiveRead> {
        if !self.index.imported_bindings.contains(binding) {
            return None;
        }
        let read = self.imported_primitive_reads.get(binding.0.as_ref())?;
        (!read.kinds.is_empty()
            && read.dependency_closure.contains(&read.provider_module)
            && !read.source_cells.is_empty()
            && self.imported_values.get(binding.0.as_ref()) == Some(&Value::Prim(read.kinds)))
        .then_some(read)
    }

    pub(super) fn initializer_call_effect_input(
        &self,
        owner: SourceUnitOrdinal,
        root_binding: &Id,
        callee: &Id,
        call_span: Span,
    ) -> Option<&ResolvedInitializerCallEffectInput> {
        self.initializer_call_effect_inputs.iter().find(|input| {
            input.binding == *root_binding
                && input.callee == *callee
                && input.call_span == call_span
                && self.binding_owner.get(root_binding) == Some(&owner)
        })
    }

    fn synchronous_effects(
        &self,
        owner: SourceUnitOrdinal,
        incomplete_reachability: bool,
    ) -> (&BTreeSet<Id>, &BTreeSet<Id>) {
        if !incomplete_reachability && let Some(effects) = self.synchronous_owner_effects[owner.0] {
            return (&effects.reads, &effects.rebinds);
        }
        let fact = &self.facts[owner.0];
        (&fact.reads.lazy, &fact.rebinds.lazy)
    }

    /// Expand actual roots immediately. A read of a future sole lexical
    /// declaration retains the read at its source, but cannot execute the
    /// declaration's deferred callable bodies until that binding initializes.
    fn extend_available_source_units(
        &self,
        roots: impl IntoIterator<Item = SourceUnitOrdinal>,
        initialized: &BTreeSet<Id>,
        incomplete_reachability: bool,
        expanded: &mut BTreeSet<SourceUnitOrdinal>,
        deferred: &mut BTreeMap<Id, SourceUnitOrdinal>,
    ) -> Vec<SourceUnitOrdinal> {
        let ready: Vec<_> = deferred
            .iter()
            .filter(|(binding, _)| incomplete_reachability || initialized.contains(*binding))
            .map(|(binding, owner)| (binding.clone(), *owner))
            .collect();
        let mut pending = VecDeque::new();
        let mut reached = Vec::new();
        for (binding, owner) in ready {
            deferred.remove(&binding);
            if expanded.insert(owner) {
                reached.push(owner);
                pending.push_back(owner);
            }
        }
        for root in roots {
            if root.0 < self.synchronous_owner_adjacency.len() && expanded.insert(root) {
                if let Some(binding) = &self.deferred_read_binding[root.0] {
                    deferred.remove(binding);
                }
                reached.push(root);
                pending.push_back(root);
            }
        }
        while let Some(source) = pending.pop_front() {
            for &target in &self.synchronous_owner_adjacency[source.0] {
                if expanded.contains(&target) {
                    continue;
                }
                if !incomplete_reachability
                    && let Some(binding) = &self.deferred_read_binding[target.0]
                    && !initialized.contains(binding)
                {
                    deferred.insert(binding.clone(), target);
                    continue;
                }
                expanded.insert(target);
                reached.push(target);
                pending.push_back(target);
            }
        }
        reached
    }

    /// The purity transfer at `owner`'s program point, `state` being the
    /// settled `Prefix(owner)`.
    pub(super) fn statement_effects(
        &self,
        owner: SourceUnitOrdinal,
        state: &ExecutionState,
    ) -> Effects {
        let fact = &self.facts[owner.0];
        self.statement_effects[owner.0]
            .clone()
            // A binding the statement rebinds may hold another value at the
            // coercion; any such rebind is itself an effect.
            .resolve_coercions(|operand| match operand {
                CoerceOperand::Known(kinds) => Some(*kinds),
                CoerceOperand::Binding(binding) => (!fact.rebinds.eager.contains(binding))
                    .then(|| self.settled_prims(state, binding))
                    .flatten(),
                CoerceOperand::Member(binding, property) => self
                    .plain_data_member(state, binding, property)?
                    .iter()
                    .try_fold(PrimKinds::NONE, |kinds, value| match value {
                        AbstractValue::Value(Value::Prim(value)) => Some(kinds.union(*value)),
                        _ => None,
                    }),
            })
    }

    /// The values of `binding.property` in `state` when they are all
    /// primitive, every allocation `binding` holds was filled by a
    /// parameter-mutation initializer (a TS enum), and `binding` is
    /// `PlainData`: no write or escape reaches those allocations after their
    /// initializer, so the settled own properties are the ones any later read
    /// sees. A primitive value of
    /// the binding (a `var` before its initializer) makes the read throw
    /// before any coercion and is skipped.
    pub(super) fn plain_data_member(
        &self,
        state: &ExecutionState,
        binding: &Id,
        property: &str,
    ) -> Option<Values> {
        if !self.graph.is_plain_data(binding.0.as_ref())
            || !self.top_bindings.contains(binding)
            || !self.needed_bindings.contains(binding)
            || !self.single_writer_bindings.contains(binding)
            || !state.world.module.initialized_bindings.contains(binding)
        {
            return None;
        }
        let mut values = Values::new();
        let mut objects = 0;
        for value in state.world.module.prefix_values.get(binding)? {
            match value {
                AbstractValue::Value(Value::Prim(_)) => {}
                // PlainData above proves the whole lifetime after the checked
                // initializer. Initializer writes can conservatively taint an
                // enum allocation without invalidating that stronger proof.
                AbstractValue::Value(Value::Alloc(Kind::Object, object))
                    if state
                        .world
                        .publication
                        .parameter_initialized_objects
                        .contains(object) =>
                {
                    let properties = state.world.surface.prefix_object_properties.get(object)?;
                    values.extend(properties.get(&Some(property.to_string()))?.iter().cloned());
                    // A computed-key write may have stored under this key.
                    if let Some(dynamic) = properties.get(&None) {
                        values.extend(dynamic.iter().cloned());
                    }
                    objects += 1;
                }
                _ => return None,
            }
        }
        (objects > 0 && values.iter().all(AbstractValue::is_primitive)).then_some(values)
    }

    /// The primitive types `binding` holds in `state`; `None` when it may
    /// hold an object, may be in its temporal dead zone, or is not tracked.
    /// Only bindings in `needed_bindings` have every assignment applied to
    /// the prefix values.
    fn settled_prims(&self, state: &ExecutionState, binding: &Id) -> Option<PrimKinds> {
        let values = if self.index.imported_bindings.contains(binding) {
            Values::from([AbstractValue::Value(
                *self.imported_values.get(binding.0.as_ref())?,
            )])
        } else if self.top_bindings.contains(binding)
            && self.needed_bindings.contains(binding)
            && self.single_writer_bindings.contains(binding)
        {
            let mut values = state
                .world
                .module
                .prefix_values
                .get(binding)
                .cloned()
                .unwrap_or_default();
            if !state.world.module.initialized_bindings.contains(binding) {
                values.insert(if self.var_bindings.contains(binding) {
                    AbstractValue::UNDEFINED
                } else {
                    AbstractValue::Value(Value::Unavailable)
                });
            }
            values
        } else {
            return None;
        };
        values
            .iter()
            .try_fold(PrimKinds::NONE, |kinds, value| match value {
                AbstractValue::Value(Value::Prim(value)) => Some(kinds.union(*value)),
                _ => None,
            })
    }

    pub(super) fn project_own_property(&self, query: OwnPropertyQuery) -> Option<OwnPropertyProof> {
        infer_own_property_projection(
            query.source_object,
            &query.source_values,
            self.index.own_descriptor_shapes.get(&query.source_object)?,
            &query.prefix_properties,
            query.tainted,
            self.index.semantic_facts.as_deref()?,
            query.request,
        )
    }
}

/// Execution-control state of the current run beside the semantic `World`.
/// Observations of admission predicates are not here either; see
/// [`Observations`].
pub(super) struct ExecutionState {
    /// Program-prefix values, heap facts, reachability and identity-bearing
    /// cells, copy-on-write so a fork clones the handle, not the contents.
    pub(super) world: Arc<World>,
    native_point_effects: NativePointEffects,
    activated_native_effects: BTreeSet<(SourceUnitOrdinal, Option<u32>)>,
    pub(super) author_host_contract_uses: Vec<crate::reports::schema::ReentryFreeOperationUse>,
    class_member_effects: BTreeMap<SourceUnitOrdinal, (BTreeSet<Id>, BTreeSet<Id>)>,
    active_class_member_promotions: BTreeSet<u32>,
    pub(super) prior_reachability_roots: Option<BTreeSet<SourceUnitOrdinal>>,
    pub(super) current_top_owner: Option<SourceUnitOrdinal>,
    pub(super) proof_uses: Vec<CallbackFactoryUseFact>,
    pub(super) returned_record_proof_uses: Vec<ReturnedRecordUseFact>,
    pub(super) checking_record_factory_contract: bool,
    pub(super) prior_effect_owners: BTreeSet<SourceUnitOrdinal>,
    /// Callables run so far by the current top-level owner; joins
    /// `prior_effect_owners` when it finishes.
    pub(super) statement_executed_owners: BTreeSet<SourceUnitOrdinal>,
    /// All invoked callable keys, including outcomes a proof probe discards.
    /// The root union preserves the conservative heap order independently of
    /// the per-invocation non-assumed premise subset.
    pub(super) statement_invoked_callables: BTreeSet<u32>,
    pub(super) array_iteration_depth: usize,
    /// Element records of the kernel iteration rows whose callbacks are
    /// running: the row checked each one settled and source-shaped before
    /// its first callback, so a callback reads them at the current prefix.
    pub(super) iteration_element_records: BTreeSet<u32>,
    array_iteration_protected_objects: BTreeSet<u32>,
    pub(super) array_iteration_unsupported: bool,
    invocation_depth: usize,
    pub(super) invocation_count: usize,
    callback_fallback: CallbackFallbackSummary,
    pub(super) fallback_outcome: Outcome,
    /// The conservative path in force while it promotes or executes code the
    /// statement only may run; effects it contributes are assumed. Cleared
    /// inside each invocation, whose outcome (and memo) records provenance
    /// relative to itself.
    pub(super) assumption: Option<AssumedEffectReason>,
    pub(super) fallback_callback_sizes: Option<(usize, usize)>,
    pub(super) fallback_local_owners: BTreeSet<SourceUnitOrdinal>,
    pub(super) fallback_local_deferred: BTreeMap<Id, SourceUnitOrdinal>,
    capture_contexts: CaptureInterner<LexicalCaptures>,
    callback_sets: CaptureInterner<Values>,
    active: BTreeSet<ActiveInvocation>,
    /// Context lookup only; completed outcomes live in the shared fact store.
    invocation_memos: OrdMap<InvocationSignature, ProgramPoint>,
    export_callback_memos: ExportCallbackMemos,
}

/// Which export surface an export-callback walk starts from.
#[derive(Clone, Copy)]
enum ExportSurface {
    /// The bindings the reentry policy exposes to opaque calls.
    Reentry,
    /// Every exported binding plus the default export surface.
    Exported,
}

/// The last export-callback walk per surface, reused while every world input
/// it read is unchanged. The walk's other inputs are `ExecutionInputs`, fixed
/// for the run, and interned captures, which are immutable.
#[derive(Default)]
pub(super) struct ExportCallbackMemos(std::cell::RefCell<[Option<ExportCallbackMemo>; 2]>);

struct ExportCallbackMemo {
    reads: ExportSurfaceReads,
    callbacks: Values,
}

/// Clones of the persistent world collections an export-callback walk reads.
/// Holding a clone makes any later write copy the collection first, so
/// `ptr_eq` against it proves the contents are unchanged.
///
/// Sync: every world field read by `visit_export_surface_callback_roots`,
/// `walk_callback_values` and `visit_stored_references_with_edges`.
struct ExportSurfaceReads {
    prefix_values: PrefixValues,
    exported_runtime_values: Values,
    global_escaped_callbacks: Values,
    prefix_object_properties: OrdMap<u32, BTreeMap<Option<String>, Values>>,
    applied_accessors: OrdMap<u32, BTreeMap<String, AppliedAccessor>>,
    descriptors: OrdMap<CallSite, OwnDescriptorMapState>,
    /// The walk reads lexical cells only through the captures of the closures
    /// it expands, and every expanded closure is in its result.
    cells: Vec<(LexicalCellId, Values)>,
}

impl ExportSurfaceReads {
    fn new(world: &World, callbacks: &Values) -> Self {
        let cells = callbacks
            .iter()
            .filter_map(|value| match value {
                AbstractValue::Closure(_, captured) => Some(captured),
                _ => None,
            })
            .flat_map(|captured| captured.values.cells.values())
            .map(|cell| (*cell, world.cells[cell.0].values.clone()))
            .collect();
        Self {
            prefix_values: world.module.prefix_values.clone(),
            exported_runtime_values: world.publication.exported_runtime_values.clone(),
            global_escaped_callbacks: world.publication.global_escaped_callbacks.clone(),
            prefix_object_properties: world.surface.prefix_object_properties.clone(),
            applied_accessors: world.surface.applied_accessors.clone(),
            descriptors: world.descriptors.clone(),
            cells,
        }
    }

    fn unchanged(&self, world: &World) -> bool {
        self.prefix_values.ptr_eq(&world.module.prefix_values)
            && self
                .exported_runtime_values
                .ptr_eq(&world.publication.exported_runtime_values)
            && self
                .global_escaped_callbacks
                .ptr_eq(&world.publication.global_escaped_callbacks)
            && self
                .prefix_object_properties
                .ptr_eq(&world.surface.prefix_object_properties)
            && self
                .applied_accessors
                .ptr_eq(&world.surface.applied_accessors)
            && self.descriptors.ptr_eq(&world.descriptors)
            && self.cells.iter().all(|(id, values)| {
                world
                    .cells
                    .get(*id)
                    .is_some_and(|cell| cell.values.ptr_eq(values))
            })
    }
}

impl ExportCallbackMemos {
    fn slot(surface: ExportSurface) -> usize {
        match surface {
            ExportSurface::Reentry => 0,
            ExportSurface::Exported => 1,
        }
    }

    fn get(&self, surface: ExportSurface, world: &World) -> Option<Values> {
        self.0.borrow()[Self::slot(surface)]
            .as_ref()
            .filter(|memo| memo.reads.unchanged(world))
            .map(|memo| memo.callbacks.clone())
    }

    fn store(&self, surface: ExportSurface, world: &World, callbacks: &Values) {
        self.0.borrow_mut()[Self::slot(surface)] = Some(ExportCallbackMemo {
            reads: ExportSurfaceReads::new(world, callbacks),
            callbacks: callbacks.clone(),
        });
    }
}

/// Run-level admission facts of one owner's execution: what the admission
/// predicates consumed or checked while proving operations. These are not
/// per-transfer effects (those ride the kernel's ledger): a predicate anywhere
/// in the executor, including the legacy evaluator and native transfers, may
/// add one, and `dependencies` and `record_factory_prototype_premises` are
/// read back mid-run. They are therefore threaded explicitly rather than
/// returned per operation: handed to a child executor with its request,
/// returned in its `RuntimeResult`, and drained into the owner's
/// `SynchronousCalls` by `execute_owner`. The fields are private so a fact
/// enters only through a named admission predicate.
#[derive(Clone, Default)]
pub(super) struct Observations {
    native_integrity_checks: BTreeSet<NativeIntegrityCheck>,
    native_prototype_property_checks: BTreeMap<String, NativePrototypePropertyCheck>,
    native_prefix_observations: Vec<NativePrefixObservation>,
    native_integrity_uses: BTreeSet<crate::reports::schema::NativeIntegrityUse>,
    native_operation_input_uses:
        BTreeMap<(u32, u32), crate::reports::schema::NativeOperationInputUse>,
    imported_primitive_array_spread_input_uses:
        Vec<crate::analysis_inputs::ImportedPrimitiveArraySpreadInputUse>,
    /// Premises consumed by this owner's proofs so far: the conservative
    /// prerequisite set of its native transfers and record-factory certificate.
    dependencies: ProofDependencies,
    inference_uses: BTreeSet<crate::analysis_provenance::InferenceUse>,
    /// Exact source-checked receivers whose prototype premises the author supplies.
    record_factory_prototype_premises: RecordFactoryPrototypePremises,
    /// The typed reason an operation of this owner's execution withheld its
    /// effect receipt. Report-only.
    owner_coverage_refusal: Option<crate::facts::OwnerInitializationCoverageRefusalReason>,
}

impl Observations {
    pub(super) fn refuse_owner_coverage(
        &mut self,
        reason: crate::facts::OwnerInitializationCoverageRefusalReason,
    ) {
        self.owner_coverage_refusal.get_or_insert(reason);
    }

    fn record_failed_native_guard(&mut self, check: NativeIntegrityCheck) {
        self.native_integrity_checks.insert(check);
    }

    /// One check per property for the owner; the first computation stands.
    fn prototype_property_check(
        &mut self,
        property: &str,
        compute: impl FnOnce() -> NativePrototypePropertyCheck,
    ) -> &NativePrototypePropertyCheck {
        self.native_prototype_property_checks
            .entry(property.to_owned())
            .or_insert_with(compute)
    }

    fn observe_native_prefix(&mut self, observation: NativePrefixObservation) {
        self.native_prefix_observations.push(observation);
    }

    fn record_native_integrity_use(&mut self, use_: crate::reports::schema::NativeIntegrityUse) {
        self.native_integrity_uses.insert(use_);
    }

    pub(super) fn record_native_operation_input_use(
        &mut self,
        span: Span,
        use_: crate::reports::schema::NativeOperationInputUse,
    ) {
        self.native_operation_input_uses
            .insert((span.lo.0, span.hi.0), use_);
    }

    /// One report entry per distinct use; a spread site can run repeatedly.
    fn record_imported_primitive_array_spread_input_use(
        &mut self,
        use_: crate::analysis_inputs::ImportedPrimitiveArraySpreadInputUse,
    ) {
        if !self
            .imported_primitive_array_spread_input_uses
            .contains(&use_)
        {
            self.imported_primitive_array_spread_input_uses.push(use_);
        }
    }

    fn consume_dependencies(&mut self, dependencies: &ProofDependencies) {
        self.dependencies.extend(dependencies);
    }

    pub(super) fn dependencies(&self) -> &ProofDependencies {
        &self.dependencies
    }

    pub(super) fn record_inference_use(&mut self, use_: crate::analysis_provenance::InferenceUse) {
        self.inference_uses.insert(use_);
    }

    fn record_inference_uses(
        &mut self,
        uses: impl IntoIterator<Item = crate::analysis_provenance::InferenceUse>,
    ) {
        self.inference_uses.extend(uses);
    }

    pub(super) fn record_factory_prototype_premises(&self) -> &RecordFactoryPrototypePremises {
        &self.record_factory_prototype_premises
    }

    fn record_factory_prototype_premises_mut(&mut self) -> &mut RecordFactoryPrototypePremises {
        &mut self.record_factory_prototype_premises
    }

    pub(super) fn clear_record_factory_prototype_premises(&mut self) {
        self.record_factory_prototype_premises = RecordFactoryPrototypePremises::default();
    }

    /// Move the report-only facts into the owner's effects. `dependencies` and
    /// the record-factory premises are read after this by owner finalization.
    pub(super) fn drain_reports_into(&mut self, effects: &mut SynchronousCalls) {
        effects.native_integrity_checks = std::mem::take(&mut self.native_integrity_checks)
            .into_iter()
            .collect();
        effects.native_prototype_property_checks =
            std::mem::take(&mut self.native_prototype_property_checks)
                .into_values()
                .collect();
        effects.native_prefix_observations = std::mem::take(&mut self.native_prefix_observations);
        effects.native_integrity_uses = std::mem::take(&mut self.native_integrity_uses)
            .into_iter()
            .collect();
        effects.native_operation_input_uses = std::mem::take(&mut self.native_operation_input_uses)
            .into_values()
            .collect();
        effects.imported_primitive_array_spread_input_uses =
            std::mem::take(&mut self.imported_primitive_array_spread_input_uses);
        effects.inference_uses = std::mem::take(&mut self.inference_uses)
            .into_iter()
            .collect();
        effects.initialization_coverage_refusal = self.owner_coverage_refusal.take();
    }
}

impl ExecutionState {
    /// A copy of the path state. The destructuring classifies every field,
    /// so a new one fails to compile until it is placed.
    pub(super) fn path(
        &self,
        allocation_judgment: &super::allocation_transfer::AllocationJudgment,
        outcome: &Outcome,
    ) -> ExecutionPath {
        let ExecutionState {
            world,
            callback_fallback,
            fallback_callback_sizes,
            fallback_outcome,
            fallback_local_owners,
            fallback_local_deferred,
            // Identity allocators: ids stay unique across leaves. Invocation
            // memos key on the world revisions, which sibling leaves never
            // share (`alternatives::Identities`).
            capture_contexts: _,
            callback_sets: _,
            invocation_memos: _,
            // Validated against the world contents each walk read.
            export_callback_memos: _,
            // Control state: the same at the end of every leaf's body as at
            // the fork, because each leaf runs the same remaining body.
            active_class_member_promotions: _,
            prior_reachability_roots: _,
            current_top_owner: _,
            checking_record_factory_contract: _,
            array_iteration_depth: _,
            iteration_element_records: _,
            invocation_depth: _,
            active: _,
            assumption: _,
            // Run-wide may-accumulators. A native write, an activated native
            // body or a protected object recorded by one leaf only makes a
            // sibling's native-intact and iteration proofs weaker; executed
            // owners and invoked callables only add fallback roots; the
            // remaining ones are reports. `class_member_effects` caches a
            // function of the source alone.
            native_point_effects: _,
            activated_native_effects: _,
            author_host_contract_uses: _,
            class_member_effects: _,
            proof_uses: _,
            returned_record_proof_uses: _,
            prior_effect_owners: _,
            statement_executed_owners: _,
            statement_invoked_callables: _,
            array_iteration_protected_objects: _,
            array_iteration_unsupported: _,
            invocation_count: _,
        } = self;
        ExecutionPath {
            world: world.clone(),
            callback_fallback: callback_fallback.clone(),
            fallback_callback_sizes: *fallback_callback_sizes,
            fallback_outcome: fallback_outcome.clone(),
            fallback_local_owners: fallback_local_owners.clone(),
            fallback_local_deferred: fallback_local_deferred.clone(),
            allocation_judgment: allocation_judgment.clone(),
            outcome: outcome.clone(),
        }
    }

    /// Replace the live path state; the caller installs the judgment and
    /// outcome it gets back.
    pub(super) fn enter(
        &mut self,
        path: ExecutionPath,
    ) -> (super::allocation_transfer::AllocationJudgment, Outcome) {
        let ExecutionPath {
            world,
            callback_fallback,
            fallback_callback_sizes,
            fallback_outcome,
            fallback_local_owners,
            fallback_local_deferred,
            allocation_judgment,
            outcome,
        } = path;
        self.world = world;
        self.callback_fallback = callback_fallback;
        self.fallback_callback_sizes = fallback_callback_sizes;
        self.fallback_outcome = fallback_outcome;
        self.fallback_local_owners = fallback_local_owners;
        self.fallback_local_deferred = fallback_local_deferred;
        (allocation_judgment, outcome)
    }
}

/// Everything an abandoned root attempt may have changed besides identity
/// allocators, so the owner can run on another lane as if it never ran.
// CLEANUP(added 2026-11-09): delete with the flat-lane rerun in
//   `roots::body::execute` at L4e, its only caller.
pub(super) struct RootAttemptCheckpoint {
    path: ExecutionPath,
    prior_reachability_roots: Option<BTreeSet<SourceUnitOrdinal>>,
    native_point_effects: NativePointEffects,
    activated_native_effects: BTreeSet<(SourceUnitOrdinal, Option<u32>)>,
    author_host_contract_uses: Vec<crate::reports::schema::ReentryFreeOperationUse>,
    proof_uses: Vec<CallbackFactoryUseFact>,
    returned_record_proof_uses: Vec<ReturnedRecordUseFact>,
    statement_executed_owners: BTreeSet<SourceUnitOrdinal>,
    statement_invoked_callables: BTreeSet<u32>,
    array_iteration_protected_objects: BTreeSet<u32>,
    array_iteration_unsupported: bool,
    invocation_count: usize,
    observations: Observations,
    evaluation_effects: EvaluationEffects,
}

impl Executor<'_, '_> {
    /// The destructuring classifies every field, so a new one fails to
    /// compile until it is placed.
    pub(super) fn checkpoint_root_attempt(&self) -> RootAttemptCheckpoint {
        let ExecutionState {
            world: _,
            callback_fallback: _,
            fallback_callback_sizes: _,
            fallback_outcome: _,
            fallback_local_owners: _,
            fallback_local_deferred: _,
            prior_reachability_roots,
            native_point_effects,
            activated_native_effects,
            author_host_contract_uses,
            proof_uses,
            returned_record_proof_uses,
            statement_executed_owners,
            statement_invoked_callables,
            array_iteration_protected_objects,
            array_iteration_unsupported,
            invocation_count,
            // Identity allocators stay live; the rewind moves the restored
            // world past every cell id and revision the attempt minted.
            capture_contexts: _,
            callback_sets: _,
            invocation_memos: _,
            // Validated against whichever world is current, restored or not.
            export_callback_memos: _,
            // A source function of the program alone.
            class_member_effects: _,
            // Control state: an owner's body leaves it as it found it.
            active_class_member_promotions: _,
            current_top_owner: _,
            checking_record_factory_contract: _,
            prior_effect_owners: _,
            array_iteration_depth: _,
            iteration_element_records: _,
            invocation_depth: _,
            active: _,
            assumption: _,
        } = &*self.state;
        RootAttemptCheckpoint {
            path: self
                .state
                .path(&self.allocation_judgment, &Outcome::default()),
            prior_reachability_roots: prior_reachability_roots.clone(),
            native_point_effects: native_point_effects.clone(),
            activated_native_effects: activated_native_effects.clone(),
            author_host_contract_uses: author_host_contract_uses.clone(),
            proof_uses: proof_uses.clone(),
            returned_record_proof_uses: returned_record_proof_uses.clone(),
            statement_executed_owners: statement_executed_owners.clone(),
            statement_invoked_callables: statement_invoked_callables.clone(),
            array_iteration_protected_objects: array_iteration_protected_objects.clone(),
            array_iteration_unsupported: *array_iteration_unsupported,
            invocation_count: *invocation_count,
            observations: self.observations.clone(),
            evaluation_effects: self.evaluation_effects.clone(),
        }
    }

    pub(super) fn rewind_root_attempt(&mut self, checkpoint: RootAttemptCheckpoint) {
        let RootAttemptCheckpoint {
            path,
            prior_reachability_roots,
            native_point_effects,
            activated_native_effects,
            author_host_contract_uses,
            proof_uses,
            returned_record_proof_uses,
            statement_executed_owners,
            statement_invoked_callables,
            array_iteration_protected_objects,
            array_iteration_unsupported,
            invocation_count,
            observations,
            evaluation_effects,
        } = checkpoint;
        let attempted = self.state.world.clone();
        let (allocation_judgment, _) = self.state.enter(path);
        super::alternatives::separate_from_abandoned(&attempted, self.state.world_mut());
        self.allocation_judgment = allocation_judgment;
        let state = &mut *self.state;
        state.prior_reachability_roots = prior_reachability_roots;
        state.native_point_effects = native_point_effects;
        state.activated_native_effects = activated_native_effects;
        state.author_host_contract_uses = author_host_contract_uses;
        state.proof_uses = proof_uses;
        state.returned_record_proof_uses = returned_record_proof_uses;
        state.statement_executed_owners = statement_executed_owners;
        state.statement_invoked_callables = statement_invoked_callables;
        state.array_iteration_protected_objects = array_iteration_protected_objects;
        state.array_iteration_unsupported = array_iteration_unsupported;
        state.invocation_count = invocation_count;
        self.observations = observations;
        self.evaluation_effects = evaluation_effects;
    }
}

impl ExecutionState {
    pub(super) fn clear_invocation_memos(&mut self, queries: &RuntimeQueries<'_, '_>) {
        for (_, point) in std::mem::take(&mut self.invocation_memos) {
            queries.retire_invocation_memo(point);
        }
        self.capture_contexts.sweep_dead();
        self.callback_sets.sweep_dead();
    }

    /// An object literal of the source owner now executing.
    pub(super) fn statement_literal(&self, index: &ExecutionIndex, object: u32) -> bool {
        self.current_top_owner.is_some()
            && index.object_literal_owners.get(&object) == self.current_top_owner.as_ref()
    }

    /// Detach the world from any shared fork before writing to it.
    pub(super) fn world_mut(&mut self) -> &mut World {
        Arc::make_mut(&mut self.world)
    }

    /// Only RuntimeRules calls this, after the owner executor and every
    /// single-consumer transfer payload have unwound. The world lists its own
    /// roots; the control state adds the retained fallback returns.
    pub(super) fn reclaim_lexical_cells(&mut self, last_owner: bool) {
        if !self.world.cells.collection_due(last_owner) {
            return;
        }
        assert!(self.current_top_owner.is_none());
        assert_eq!(self.invocation_depth, 0);
        assert!(self.active.is_empty());
        assert!(self.invocation_memos.is_empty());
        let mut trace = CellTrace::new(&self.world.cells);
        self.world.trace_roots(&mut trace);
        trace.values(&self.fallback_outcome.returns);
        let (marked, work) = trace.finish();
        self.world_mut().cells.sweep(&marked, work);
        self.capture_contexts.sweep_dead();
        self.callback_sets.sweep_dead();
    }
}

pub(super) struct Executor<'source, 'rule> {
    pub(super) inputs: &'rule ExecutionInputs<'source>,
    pub(super) state: ExecutionStateSlot,
    pub(super) observations: Observations,
    pub(super) queries: RuntimeQueries<'source, 'rule>,
    pub(super) allocation_judgment: super::allocation_transfer::AllocationJudgment,
    pub(super) source_covered: bool,
    pub(super) evaluation_effects: EvaluationEffects,
    native_initializer_candidate: Option<NativeConstructorCandidate>,
    native_initializer_transfer: Option<NativeConstructorTransfer>,
}

/// A suspended transfer has handed its state to its child. Keep the remaining
/// Rust frame small without allocating an empty state for every child request.
pub(super) struct ExecutionStateSlot(Option<Box<ExecutionState>>);

impl std::ops::Deref for ExecutionStateSlot {
    type Target = ExecutionState;

    fn deref(&self) -> &ExecutionState {
        self.0
            .as_deref()
            .expect("the active transfer owns its state")
    }
}

impl std::ops::DerefMut for ExecutionStateSlot {
    fn deref_mut(&mut self) -> &mut ExecutionState {
        self.0
            .as_deref_mut()
            .expect("the active transfer owns its state")
    }
}

/// The source proof selected for one exact factory call. Kept separate from
/// report structs so downstream purity uses a typed proof result rather than
/// reconstructing trust from serialized synchronous-execution output.
pub(super) struct CheckedLocalConstructorFactory {
    pub(super) call_span: Span,
    pub(super) checked_owners: BTreeSet<SourceUnitOrdinal>,
}

#[derive(Clone, Copy)]
pub(super) struct ValidatedMetadataDecoration {
    pub(super) target_owner: SourceUnitOrdinal,
}

/// The lowered body of a checked constructor that the kernel's construct rule
/// can run: no call, construction or spread of its own, no keyed-collection
/// operation (the application has no keyed-collection lane), and no
/// parameter property read (those keep the flat lane's checked plain-data
/// input proof).
fn kernel_constructor_body<'index>(
    index: &'index ExecutionIndex,
    key: u32,
    constructor: &LocalConstructor,
) -> Option<(&'index LocalBody, SourceUnitOrdinal)> {
    let (body, _) = index.class_provider_bodies.get(&key)?;
    (constructor.property_reads.is_empty()
        && !body.invokes()
        && !body.requires_keyed_collection_new())
    .then_some(body)
    .zip(index.class_owners.get(&key).copied())
}

/// Which body an ordered activation runs, with what only that kind carries:
/// a root's exact source identity, a constructor's fresh instance.
pub(super) enum LocalActivation {
    Callable(u32),
    RootInitializer(RootInitializerIdentity),
    /// An admitted top-level expression statement: no binding receives its
    /// value, so it has no lexical binding-effect identity.
    RootStatement,
    Constructor {
        class: u32,
        instance: Values,
    },
}

impl LocalActivation {
    fn scope(&self) -> LocalExecutionScope {
        match self {
            Self::Callable(key) => LocalExecutionScope::Callable(*key),
            Self::RootInitializer(_) | Self::RootStatement => LocalExecutionScope::Root,
            Self::Constructor { class, .. } => LocalExecutionScope::Constructor(*class),
        }
    }
}

/// One `new` the kernel's construct rule evaluated: its value and, when the
/// class body ran through the kernel to completion, that body and its effects.
pub(super) struct OrderedConstruction {
    pub(super) values: Values,
    pub(super) body: Option<(LocalBodyIdentity, EffectCoverageFragment)>,
}

impl OwnDataContext for Executor<'_, '_> {
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape> {
        self.inputs.index.ordinary_record_shape(allocation)
    }

    fn object_properties_are_settled(&self, allocation: u32) -> bool {
        self.state.world.object_properties_are_settled(allocation)
    }

    fn property_values(&self, allocation: u32, property: &str) -> Option<&Values> {
        self.state.world.surface.data_values(allocation, property)
    }

    fn property_is_accessor(&self, allocation: u32, property: &str) -> bool {
        self.state
            .world
            .surface
            .has_accessor_property(allocation, property)
    }

    fn property_is_writable(&self, allocation: u32, property: &str) -> bool {
        let has_writable_property = match self.state.world.surface.writable_prefix.get(&allocation)
        {
            Some(super::allocation_transfer::WritablePrefixState::Proven(properties)) => {
                properties.contains(property)
            }
            _ => false,
        };
        has_writable_property
            && self
                .state
                .world
                .surface
                .object_accessors_are_settled(allocation)
            && !self
                .state
                .world
                .surface
                .has_accessor_property(allocation, property)
    }

    fn first_generation_dense_array_length(&self, allocation: u32) -> Option<u32> {
        if !self.inputs.index.array_elements.contains_key(&allocation)
            || !self.state.world.arrays.dense.contains_key(&allocation)
        {
            return None;
        }
        match self.state.world.surface.writable_prefix.get(&allocation) {
            Some(super::allocation_transfer::WritablePrefixState::DenseArray { length }) => {
                Some(*length)
            }
            _ => None,
        }
    }
}

impl DescriptorContext for Executor<'_, '_> {
    fn descriptor_map(&self, site: CallSite) -> Option<&OwnDescriptorMapState> {
        self.state.world.descriptors.get(&site)
    }

    fn object_surface(&self) -> &ObjectSurfaceStore {
        &self.state.world.surface
    }

    fn plain_data_object(&self, object: u32) -> bool {
        self.inputs.index.plain_data_objects.contains(&object)
    }

    fn descriptor_conversion_is_ordinary(&mut self) -> bool {
        self.ordinary_object_prototype_intact()
    }

    fn statement_literal(&self, object: u32) -> bool {
        self.state.statement_literal(self.inputs.index, object)
    }
}

impl NativeOperationContext for Executor<'_, '_> {
    fn native_operation_revision(&self) -> u64 {
        self.state.world.revision.prefix
    }

    fn descriptor_heap(&mut self) -> DescriptorHeap<'_> {
        self.state.world_mut().descriptor_heap()
    }
}

impl ArrayContext for Executor<'_, '_> {
    fn literal_array_length(&self, object: u32, absent: &[&str]) -> Option<u32> {
        let properties = self
            .state
            .world
            .surface
            .prefix_object_properties
            .get(&object)?;
        if !self
            .state
            .world
            .surface
            .object_accessors_are_settled(object)
            || self
                .state
                .world
                .surface
                .applied_accessors
                .contains_key(&object)
            || !self.state.world.arrays.dense.contains_key(&object)
            || absent
                .iter()
                .any(|property| properties.contains_key(&Some((*property).into())))
        {
            return None;
        }
        match self.state.world.surface.writable_prefix.get(&object)? {
            super::allocation_transfer::WritablePrefixState::DenseArray { length } => Some(*length),
            _ => None,
        }
    }

    fn array_slots_intact(&mut self, slots: &[IntrinsicSlot]) -> bool {
        Executor::array_slots_intact(self, slots)
    }

    fn dense_array_slots(&self, object: u32) -> Option<Vec<Values>> {
        self.settled_dense_array_slots(object)
    }

    fn array_dependencies(&self) -> ProofDependencies {
        self.settled_prefix_dependencies()
    }

    fn append_primitives(&mut self, object: u32, length: u32, arguments: &[Values]) {
        self.state
            .world_mut()
            .arrays
            .unknown_contents
            .insert(object);
        self.state.world_mut().surface.writable_prefix.insert(
            object,
            super::allocation_transfer::WritablePrefixState::DenseArray { length },
        );
        let values = arguments.iter().flatten().cloned().collect();
        self.literal_heap().write_property(object, None, &values);
        self.state.world_mut().revision.bump_prefix();
    }
}

/// The evaluated callee targets and arguments one call transfer consumes.
pub(super) struct EvaluatedCallOperands {
    pub(super) targets: Values,
    pub(super) args: Vec<Values>,
    /// The evaluated receiver of a member callee.
    pub(super) prepared_receiver: Option<Values>,
}

/// The kernel's selections for operands it evaluated itself.
pub(super) struct SuppliedCallSelections {
    pub(super) selected_pure_initializer_call: bool,
    pub(super) selected_member_dependencies: Option<Box<ProofDependencies>>,
    pub(super) selected_value: Option<CallableSelection>,
    pub(super) argument_selections: Vec<Option<CallableSelection>>,
}

impl<'source, 'rule> Executor<'source, 'rule> {
    pub(super) fn new(
        inputs: &'rule ExecutionInputs<'source>,
        state: Box<ExecutionState>,
        observations: Observations,
        queries: RuntimeQueries<'source, 'rule>,
    ) -> Self {
        Self {
            inputs,
            state: ExecutionStateSlot(Some(state)),
            observations,
            queries,
            // Creating an executor establishes no whole-source coverage.
            allocation_judgment: super::allocation_transfer::AllocationJudgment::Opaque,
            source_covered: false,
            evaluation_effects: EvaluationEffects::default(),
            native_initializer_candidate: None,
            native_initializer_transfer: None,
        }
    }

    pub(super) fn into_transfer(
        mut self,
    ) -> (
        Box<ExecutionState>,
        Observations,
        super::allocation_transfer::AllocationJudgment,
        EvaluationEffects,
    ) {
        if let super::allocation_transfer::AllocationJudgment::CoveredAllocationOnly(dependencies) =
            &self.allocation_judgment
        {
            self.queries.assume(dependencies);
        }
        self.settle_query_prerequisites();
        self.evaluation_effects
            .depend_on(&self.state.world.prerequisites);
        (
            self.state.0.expect("the completed transfer owns its state"),
            self.observations,
            self.allocation_judgment,
            self.evaluation_effects,
        )
    }

    pub(super) fn mark_allocation_opaque(&mut self) {
        self.allocation_judgment = super::allocation_transfer::AllocationJudgment::Opaque;
    }

    /// Called only for an expression whose canonical source coverage was
    /// retained by its owner, or a child of that exact expression.
    pub(super) fn cover_source_expression(&mut self) {
        self.source_covered = true;
        self.allocation_judgment =
            super::allocation_transfer::AllocationJudgment::CoveredAllocationOnly(Box::default());
    }

    /// Conditions of the settled prefix, including value-producing child
    /// transfers, plus queries used by the current target selection. This is
    /// a conservative prerequisite set, never operation coverage evidence.
    pub(super) fn settled_prefix_dependencies(&self) -> ProofDependencies {
        let mut dependencies = self.state.world.prerequisites.clone();
        dependencies.extend(&self.queries.dependencies());
        dependencies
    }

    pub(super) fn boxed_settled_prefix_dependencies(&self) -> Box<ProofDependencies> {
        let mut dependencies = Box::<ProofDependencies>::default();
        dependencies.extend(&self.state.world.prerequisites);
        self.queries.extend_dependencies(&mut dependencies);
        dependencies
    }

    /// Fold the queries' prerequisites into the world's snapshot before the
    /// state crosses a transfer boundary. Writes only on change, so an
    /// unchanged shared world is not copied.
    fn settle_query_prerequisites(&mut self) {
        let mut prerequisites = self.state.world.prerequisites.clone();
        prerequisites.extend(&self.queries.dependencies());
        if prerequisites != self.state.world.prerequisites {
            self.state.world_mut().prerequisites = prerequisites;
        }
    }

    pub(super) fn request(&mut self, operation: RuntimeOperation) -> RuntimeValue {
        self.request_with_allocation_judgment(operation).0
    }

    fn request_with_allocation_judgment(
        &mut self,
        operation: RuntimeOperation,
    ) -> (RuntimeValue, super::allocation_transfer::AllocationJudgment) {
        self.request_transfer(operation, self.state.assumption.clone())
    }

    fn request_transfer(
        &mut self,
        operation: RuntimeOperation,
        assumption: Option<AssumedEffectReason>,
    ) -> (RuntimeValue, super::allocation_transfer::AllocationJudgment) {
        self.settle_query_prerequisites();
        let state = self
            .state
            .0
            .take()
            .expect("the requesting transfer owns its state");
        let observations = std::mem::take(&mut self.observations);
        let mut result = self.queries.request(state, observations, operation);
        self.state.0 = Some(result.state);
        self.observations = result.observations;
        self.allocation_judgment
            .compose(&result.allocation_judgment);
        // Transport effects before exposing any payload, including Values.
        result.evaluation_effects.assume(assumption.as_ref());
        self.evaluation_effects.extend(&result.evaluation_effects);
        (result.value, result.allocation_judgment)
    }

    fn request_callback_operation(&mut self, operation: CallbackOperation) -> CallbackResult {
        let result = self.request(RuntimeOperation::Callbacks(operation));
        let RuntimeValue::Callbacks(result) = result else {
            unreachable!("callback operations produce callback results");
        };
        result
    }

    /// Runs `run` as the conservative path `reason`: binding effects it
    /// contributes are assumed. An enclosing path keeps its reason.
    fn assuming<T>(&mut self, reason: AssumedEffectReason, run: impl FnOnce(&mut Self) -> T) -> T {
        let outermost = self.state.assumption.is_none();
        if outermost {
            self.state.assumption = Some(reason);
        }
        let result = run(self);
        if outermost {
            self.state.assumption = None;
        }
        result
    }

    fn remember_invocation(&mut self, signature: InvocationSignature, outcome: &Outcome) {
        if self
            .state
            .world
            .native
            .tainted_functions
            .contains(&signature.callable)
        {
            return;
        }
        // This executor owns only this invocation's child ledger. A memo
        // must never capture the caller's accumulated effects.
        let mut memo = outcome.clone();
        self.record_callable_heap_contents(signature.callable, &mut memo);
        memo.effects
            .evaluation_effects
            .extend(&self.evaluation_effects);
        memo.effects
            .evaluation_effects
            .depend_on(&self.state.world.prerequisites);
        memo.effects
            .evaluation_effects
            .depend_on(&self.queries.dependencies());
        let point = self.queries.publish_invocation_memo(&memo);
        if let Some(previous) = self.state.invocation_memos.insert(signature, point.clone())
            && previous != point
        {
            self.queries.retire_invocation_memo(previous);
        }
    }

    /// Static first-order body cells belong to this invocation, including
    /// when it is memoized. The caller later decides whether its path was
    /// assumed; a statement-wide callable set loses that distinction.
    fn record_callable_heap_contents(&self, key: u32, outcome: &mut Outcome) {
        if let Some(cells) = self.inputs.callable_heap_contents.get(&key) {
            outcome
                .effects
                .record_heap_contents(cells, outcome.body_invoked);
        }
    }

    /// The abstract callback-factory value also represents namespace members
    /// and aliases. Only a checked direct imported binding identifies the
    /// summary without changing value or cache semantics.
    pub(super) fn record_callback_factory_use(
        &mut self,
        imported_local: &Id,
        kind: CallbackFactoryUseKind,
        lexical_statement_ordinal: SourceUnitOrdinal,
        span: Span,
    ) {
        let Some(summary) = self
            .inputs
            .index
            .callback_factory_summaries
            .get(imported_local)
        else {
            return;
        };
        let proof_use = CallbackFactoryUseFact {
            imported_local: imported_local.0.to_string(),
            evidence: summary
                .callback_factory_evidence()
                .iter()
                .cloned()
                .collect(),
            kind,
            lexical_statement_ordinal,
            source_location: None,
            span,
        };
        self.record_callback_factory_use_fact(proof_use);
    }

    pub(super) fn record_callback_factory_use_fact(&mut self, proof_use: CallbackFactoryUseFact) {
        if !self.state.proof_uses.contains(&proof_use) {
            self.state.proof_uses.push(proof_use);
        }
    }

    pub(super) fn closed_enum_member_read(&mut self, value: &ValueExpr) -> bool {
        let ValueExpr::Member(receiver, Some(property), _, _) = value else {
            return false;
        };
        self.closed_enum_member_parts(receiver, property).is_some()
    }

    /// The primitive values of `receiver.property` when `receiver` is a
    /// settled `PlainData` binding holding the key as own data. The read
    /// keeps its ordinary eager dependency, but cannot retrieve a getter or a
    /// callable value.
    pub(super) fn closed_enum_member_parts(
        &mut self,
        receiver: &ValueExpr,
        property: &str,
    ) -> Option<Values> {
        let ValueExpr::Binding(binding) = receiver else {
            return None;
        };
        let values = self
            .inputs
            .plain_data_member(&self.state, binding, property)?;
        if let Some(uses) = self
            .inputs
            .graph
            .plain_data_inference_uses(binding.0.as_ref())
        {
            self.observations
                .record_inference_uses(uses.iter().cloned());
        }
        Some(values)
    }

    pub(super) fn record_returned_record_use(
        &mut self,
        callee: &ValueExpr,
        kind: crate::reports::schema::ReturnedRecordUseKind,
        lexical_statement_ordinal: SourceUnitOrdinal,
        span: Span,
    ) {
        let ValueExpr::Binding(imported_local) = callee else {
            return;
        };
        let Some(evidence) = self
            .inputs
            .index
            .returned_record_summaries
            .get(imported_local)
        else {
            return;
        };
        self.observations
            .record_inference_use(evidence.inference.clone());
        let proof_use = ReturnedRecordUseFact {
            imported_local: imported_local.0.to_string(),
            evidence: evidence.clone(),
            kind,
            lexical_statement_ordinal,
            source_location: None,
            span,
        };
        if !self.state.returned_record_proof_uses.contains(&proof_use) {
            self.state.returned_record_proof_uses.push(proof_use);
        }
    }

    /// Every slot census-intact. A failure records the mutated families.
    fn native_guard_intact(
        &mut self,
        guard: NativeIntegrityGuard,
        slots: &[IntrinsicSlot],
    ) -> bool {
        self.observe_native_prefix(guard, None);
        let integrity = &self.inputs.index.integrity;
        if let Some(premise) =
            native_premise(integrity, slots, None, InferenceRule::NativeIntegrity)
        {
            self.record_inference_use(guard, premise.inference_use, &premise.dependencies);
            return true;
        }
        self.record_failed_native_guard(guard, slots);
        false
    }

    /// Every slot census-intact, for a proof that records no inference use.
    /// Success records the census's proof dependencies (the program premises
    /// that kept the slots intact); a failure records the mutated families
    /// like [`Self::native_guard_intact`].
    fn census_slots_intact(
        &mut self,
        guard: NativeIntegrityGuard,
        slots: &[IntrinsicSlot],
    ) -> bool {
        self.observe_native_prefix(guard, None);
        match self.inputs.index.integrity.require(slots, None) {
            Integrity::Intact(dependencies) => {
                self.record_proof_dependencies(guard, dependencies);
                true
            }
            _ => {
                self.record_failed_native_guard(guard, slots);
                false
            }
        }
    }

    fn record_failed_native_guard(&mut self, guard: NativeIntegrityGuard, slots: &[IntrinsicSlot]) {
        self.observations
            .record_failed_native_guard(NativeIntegrityCheck {
                guard,
                slots: slots.iter().map(ToString::to_string).collect(),
                affected_roots: self.inputs.index.integrity.mutated_roots(slots),
            });
    }

    pub(super) fn constructor_field_prototype_absent(
        &mut self,
        class: u32,
        property: &str,
    ) -> bool {
        if Self::standard_object_prototype_property(property) {
            return false;
        }
        let premise = (class, property.to_owned());
        if self.state.checking_record_factory_contract {
            self.observations
                .record_factory_prototype_premises_mut()
                .constructor_fields
                .insert(premise);
            return true;
        }
        self.observations
            .record_factory_prototype_premises()
            .constructor_fields
            .contains(&premise)
            || self.object_prototype_property_absent(property)
    }

    fn input_field_prototype_absent(&mut self, object: u32, property: &str) -> bool {
        if Self::standard_object_prototype_property(property) {
            return false;
        }
        let premise = (object, property.to_owned());
        if self.state.checking_record_factory_contract {
            self.observations
                .record_factory_prototype_premises_mut()
                .missing_input_fields
                .insert(premise);
            return true;
        }
        self.observations
            .record_factory_prototype_premises()
            .missing_input_fields
            .contains(&premise)
            || self.object_prototype_property_absent(property)
    }

    /// Absence is stronger than getter-freedom: an inherited data value may
    /// contain callbacks missing from an argument-only instance graph.
    fn object_prototype_property_absent(&mut self, property: &str) -> bool {
        self.observe_native_prefix(
            NativeIntegrityGuard::OrdinaryObjectPrototype,
            Some(property),
        );
        if Self::standard_object_prototype_property(property) {
            return false;
        }
        let integrity = &self.inputs.index.integrity;
        let intact = self
            .observations
            .prototype_property_check(property, || {
                let (intact, blocking_mutation_count) =
                    integrity.object_prototype_property(property);
                NativePrototypePropertyCheck {
                    property: property.to_owned(),
                    intact,
                    blocking_mutation_count,
                }
            })
            .intact;
        if intact {
            // Finite mutation keys may depend on Symbol/Object/Array identity,
            // which can itself consume a captured-value census refinement.
            self.record_native_integrity_use(NativeIntegrityGuard::OrdinaryObjectPrototype);
        }
        intact
    }

    fn record_native_integrity_use(&mut self, guard: NativeIntegrityGuard) {
        self.record_proof_dependencies(guard, &self.inputs.index.native_integrity_dependencies());
    }

    pub(super) fn record_proof_dependencies(
        &mut self,
        guard: NativeIntegrityGuard,
        dependencies: &crate::analysis_inputs::ProofDependencies,
    ) {
        self.consume_proof_dependencies(dependencies);
        if !dependencies.value_inputs.is_empty()
            || !dependencies.no_accessor_install_callables.is_empty()
            || !dependencies.program_premises.is_empty()
        {
            self.observations.record_native_integrity_use(
                crate::reports::schema::NativeIntegrityUse {
                    guard,
                    value_input_refs: dependencies.value_inputs.clone(),
                    no_accessor_install_refs: dependencies.no_accessor_install_callables.clone(),
                    program_premises: dependencies.program_premises.clone(),
                },
            );
        }
    }

    fn consume_proof_dependencies(
        &mut self,
        dependencies: &crate::analysis_inputs::ProofDependencies,
    ) {
        self.queries.assume(dependencies);
        self.observations.consume_dependencies(dependencies);
    }

    pub(super) fn record_inference_use(
        &mut self,
        guard: NativeIntegrityGuard,
        proof: crate::analysis_provenance::InferenceUse,
        dependencies: &ProofDependencies,
    ) {
        self.record_proof_dependencies(guard, dependencies);
        for reference in &dependencies.native_operation_inputs {
            for input in self
                .inputs
                .index
                .resolved_native_operation_inputs
                .iter()
                .filter(|input| &input.evidence.reference == reference)
            {
                let span = input.call_span;
                self.observations.record_native_operation_input_use(
                    span,
                    crate::reports::schema::NativeOperationInputUse {
                        evidence: input.evidence.clone(),
                        source_location: input.evidence.initializer_location.clone(),
                        span,
                    },
                );
            }
        }
        self.observations.record_inference_use(proof);
    }

    /// Descriptor application targets need the ordinary prototype premise;
    /// private constructors keep their separate property-specific checks.
    pub(super) fn ordinary_object_prototype_intact(&mut self) -> bool {
        self.inputs.index.dynamic_scope_owners.is_empty()
            && self.native_guard_intact(
                NativeIntegrityGuard::OrdinaryObjectPrototype,
                &[IntrinsicSlot::Prototype(NativeIntrinsic::Object)],
            )
    }

    fn own_property_projection(
        &self,
        values: &Values,
        request: OwnPropertyRequest,
    ) -> Option<OwnPropertyProof> {
        let Some(AbstractValue::Value(Value::Alloc(Kind::Object, object))) = values.first() else {
            return None;
        };
        if values.len() != 1 {
            return None;
        }
        let properties = self
            .state
            .world
            .surface
            .prefix_object_properties
            .get(object)?;
        self.queries.project_own_property(OwnPropertyQuery {
            source_object: *object,
            source_values: values.clone(),
            prefix_properties: properties.clone(),
            tainted: !self.state.world.object_properties_are_settled(*object),
            request,
        })
    }

    fn ordinary_source_object(&self, values: &Values) -> Option<OwnPropertyProof> {
        self.own_property_projection(values, OwnPropertyRequest::Ordinary)
    }

    pub(super) fn invalidate_own_descriptor_map(&mut self, site: CallSite) {
        self.descriptor_heap().invalidate(site);
    }

    pub(super) fn descriptor_map_callbacks(&self, site: CallSite) -> Values {
        self.state
            .world
            .descriptors
            .get(&site)
            .map_or_else(Values::new, |map| map.retained_values.clone())
    }

    pub(super) fn applied_getters(&self, receivers: &Values, property: Option<&str>) -> Values {
        let mut getters = Values::new();
        for value in receivers {
            let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = value else {
                continue;
            };
            let Some(accessors) = self.state.world.surface.applied_accessors.get(object) else {
                continue;
            };
            if let Some(property) = property {
                if let Some(accessor) = accessors.get(property) {
                    getters.extend(accessor.getter.iter().cloned());
                }
            } else {
                getters.extend(
                    accessors
                        .values()
                        .flat_map(|accessor| accessor.getter.iter().cloned()),
                );
            }
        }
        getters
    }

    /// Return a builtin row only after proving the source-level intrinsic
    /// identity: the callee names the original global and the row's slots are
    /// intact, or asserted at this exact call. Operand eligibility is checked
    /// separately.
    pub(super) fn exact_native_call_row(
        &mut self,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
    ) -> Option<(&'static Row, NativeIdentitySource)> {
        match self.native_call_identity(kind, callee, site)? {
            NativeCallIdentity::FailedGuard(slots) => {
                self.record_failed_native_guard(NativeIntegrityGuard::NativeCallIdentity, slots);
                None
            }
            NativeCallIdentity::Proved(row, source) => {
                match &source {
                    NativeIdentitySource::AuthorInput { dependencies, .. } => self
                        .record_proof_dependencies(
                            NativeIntegrityGuard::NativeCallIdentity,
                            dependencies,
                        ),
                    NativeIdentitySource::Inferred { premise } => self.record_inference_use(
                        NativeIntegrityGuard::NativeCallIdentity,
                        premise.inference_use.clone(),
                        &premise.dependencies,
                    ),
                }
                Some((row, source))
            }
        }
    }

    /// The identity decision of `exact_native_call_row` without recording
    /// its premise use; `None` when the callee names no intrinsic row.
    fn native_call_identity(
        &self,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
    ) -> Option<NativeCallIdentity> {
        let (row, callee) = super::execution_index::native_call_identity_key(kind, callee)?;
        if !names_intrinsic(
            &callee,
            row.family()?,
            self.inputs.flow.is_declared(&callee.binding),
            self.inputs.shadowed,
            &self.inputs.index.global_object_names,
        ) {
            return None;
        }
        let Some(premise) = native_premise(
            &self.inputs.index.integrity,
            row.requires,
            Some(site.allocation_id()),
            InferenceRule::NativeCallIdentity,
        ) else {
            return Some(NativeCallIdentity::FailedGuard(row.requires));
        };
        if !premise.census {
            let evidence = self
                .inputs
                .index
                .native_operation_input_evidence
                .get(&site)?
                .clone();
            return Some(NativeCallIdentity::Proved(
                row,
                NativeIdentitySource::AuthorInput {
                    evidence: Box::new(evidence),
                    dependencies: Box::new(premise.dependencies),
                },
            ));
        }
        Some(NativeCallIdentity::Proved(
            row,
            NativeIdentitySource::Inferred {
                premise: Box::new(premise),
            },
        ))
    }

    pub(super) fn native_member_lookup_is_data(
        &mut self,
        callee: &ValueExpr,
        site: Option<u32>,
    ) -> bool {
        if !self.inputs.index.dynamic_scope_owners.is_empty() {
            return false;
        }
        let Some(slots) = native_own_data_read_slots(callee, |binding| {
            self.inputs.flow.is_declared(binding)
                || self.inputs.shadowed.contains(binding.0.as_ref())
        }) else {
            return false;
        };
        let Some(premise) = native_premise(
            &self.inputs.index.integrity,
            &slots,
            site,
            InferenceRule::NativeOwnDataRead,
        ) else {
            return false;
        };
        // Identity discharges this lookup only. Heap read analysis records
        // the exact read use; invocation uses require separate admission.
        self.record_proof_dependencies(
            NativeIntegrityGuard::NativeCallIdentity,
            &premise.dependencies,
        );
        self.observations
            .record_inference_use(premise.inference_use);
        true
    }

    /// Reuse the exact identity/lookup proof of the canonical outer call.
    /// Neither this lookup nor native identity certifies the invocation's
    /// inputs, timing, purity or throws.
    pub(super) fn callee_lookup_is_certified(&mut self, lookup: &Call) -> bool {
        if self.native_member_lookup_is_data(&lookup.callee, None) {
            return true;
        }
        if let ValueExpr::Member(receiver, Some(property), MemberKey::Static, _) = &lookup.callee
            && let ValueExpr::Binding(namespace) = receiver.as_ref()
            && self
                .inputs
                .index
                .audited_factory_members
                .get(namespace)
                .into_iter()
                .chain(self.inputs.index.no_reentry_members.get(namespace))
                .chain(self.inputs.index.no_reentry_record_members.get(namespace))
                .any(|members| members.contains(property))
        {
            // These maps contain only the completed namespace identity,
            // publication, mutation and namespace_independent_members proofs.
            return true;
        }
        let Some(call) = self
            .inputs
            .index
            .accessor_callee_calls
            .get(&lookup.span.into())
        else {
            return false;
        };
        let site = CallSite::from(call.span);
        if self.native_member_lookup_is_data(&lookup.callee, Some(site.allocation_id())) {
            return true;
        }
        let Some((row, _)) = self.exact_native_call_row(call.kind, &call.callee, site) else {
            return false;
        };
        let premise = native_premise(
            &self.inputs.index.integrity,
            row.requires,
            Some(site.allocation_id()),
            InferenceRule::NativeCallIdentity,
        )
        .expect("exact native call retained its identity premise");
        // Identity discharges this lookup only. An operation-use record is
        // reserved for the separately admitted invocation.
        self.record_proof_dependencies(
            NativeIntegrityGuard::NativeCallIdentity,
            &premise.dependencies,
        );
        self.observations
            .record_inference_use(premise.inference_use);
        true
    }

    /// An admitted row certifies the operation after its inputs have
    /// evaluated. It does not infer purity or exception freedom.
    pub(super) fn resolve_native_call(
        &mut self,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<NativeCallResolution> {
        let RuntimeValue::Heap(HeapResult::Native(resolution)) =
            self.request(RuntimeOperation::Heap(HeapOperation::ResolveNative {
                kind,
                callee: callee.clone(),
                site,
                expressions: expressions.to_vec(),
                arguments: args.to_vec(),
            }))
        else {
            unreachable!("native model resolution produces an optional model");
        };
        *resolution
    }

    /// Admit a row for the call's operands. The row's writes are proved
    /// first; each proof names the operands it shows to be exact ordinary
    /// objects, on which the listed internal methods run no user code. Every
    /// remaining coercion must land on a primitive operand.
    pub(super) fn resolve_native_call_transfer(
        &mut self,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<NativeCallResolution> {
        let (row, source) = self.exact_native_call_row(kind, callee, site)?;
        if !row.arity.contains(&args.len()) || !row.invokes.is_empty() {
            return None;
        }
        let iterable = self.admit_set_iteration(row, kind, callee, site, expressions, args);
        let native_call = match &source {
            NativeIdentitySource::Inferred { premise } => Some(premise.as_ref().clone()),
            NativeIdentitySource::AuthorInput { .. } => None,
        };
        let mut ordinary_operands = BTreeSet::new();
        let mut own_property_projection = None;
        let mut projected_reads = Vec::new();
        let mut application = None;
        let mut definition = None;
        for mutation in row.mutates {
            match *mutation {
                // SetIntegrityLevel runs only ordinary internal methods: it is
                // silent on a primitive and on every exact object allocation.
                // An allocation another statement already holds keeps its
                // conservative barrier, since the attribute change would have
                // to be ordered against that statement's writes.
                Mutation::Integrity(ArgSel::Arg(index)) => {
                    let values = args.get(index)?;
                    let literal = match expressions.get(index) {
                        Some(ValueExpr::ObjectLiteral(object, _)) => Some(*object),
                        _ => None,
                    };
                    if values.is_empty()
                        || !values.iter().all(|value| {
                            value.is_primitive()
                                || matches!(value, AbstractValue::Value(Value::Alloc(kind, object))
                                    if kind.tracks_prefix_own_properties()
                                        && (literal == Some(*object)
                                            || self.statement_literal(*object)))
                        })
                    {
                        return None;
                    }
                    ordinary_operands.insert(index);
                }
                Mutation::DefineProperty {
                    target: ArgSel::Arg(target),
                    key: ArgSel::Arg(key),
                    descriptor: ArgSel::Arg(descriptor),
                } => {
                    let (Some(target_values), Some(key_values), Some(descriptor_values)) =
                        (args.get(target), args.get(key), args.get(descriptor))
                    else {
                        return None;
                    };
                    definition = Some(descriptor_heap::dynamic_key_definition(
                        self,
                        target_values,
                        key_values,
                        descriptor_values,
                    )?);
                    // The key stays with the primitive coercion check below.
                    ordinary_operands.extend([target, descriptor]);
                }
                Mutation::DefineProperties {
                    target: ArgSel::Arg(target),
                    descriptors: ArgSel::Arg(descriptors),
                } => {
                    let (Some(target_values), Some(map_values)) =
                        (args.get(target), args.get(descriptors))
                    else {
                        return None;
                    };
                    application = Some(descriptor_heap::application(
                        self,
                        target_values,
                        map_values,
                    )?);
                    ordinary_operands.extend([target, descriptors]);
                }
                Mutation::FreshOwnDescriptorsOf(ArgSel::Arg(index)) => {
                    let proof = self.own_property_projection(
                        args.get(index)?,
                        OwnPropertyRequest::Descriptors {
                            native_call: native_call.clone()?,
                        },
                    )?;
                    let OwnPropertyProjection::Descriptors(projection) = &proof.projection else {
                        return None;
                    };
                    let source = projection.source_object;
                    projected_reads.push(EffectCell::ObjectShape(source));
                    projected_reads.extend(projection.ordered_names.iter().map(|name| {
                        EffectCell::OwnProperty {
                            origin: source,
                            property: name.clone(),
                        }
                    }));
                    own_property_projection = Some(proof);
                    ordinary_operands.insert(index);
                }
                Mutation::FreshOwnKeysOf(ArgSel::Arg(index)) => {
                    let source_values = args.get(index)?;
                    // The projection admits only a singleton ordinary record.
                    let Some(AbstractValue::Value(Value::Alloc(Kind::Object, source))) =
                        source_values.first()
                    else {
                        return None;
                    };
                    projected_reads.push(EffectCell::ObjectShape(*source));
                    own_property_projection = Some(self.own_property_projection(
                        source_values,
                        OwnPropertyRequest::Names {
                            native_call: native_call.clone()?,
                        },
                    )?);
                    ordinary_operands.insert(index);
                }
                // Retention needs one known callable; the slot stores it
                // without invoking it.
                Mutation::FreshRetains(ArgSel::Arg(index)) => {
                    let values = args.get(index)?;
                    if values.len() != 1
                        || !values.iter().next().is_some_and(|value| {
                            matches!(
                                value,
                                AbstractValue::Value(Value::Alloc(Kind::Function, _))
                                    | AbstractValue::Closure(_, _)
                            )
                        })
                    {
                        return None;
                    }
                }
                // A write through a `Rest` selector is not modeled.
                _ => return None,
            }
        }
        for &(selector, operation) in row.coerces {
            let ArgSel::Arg(index) = selector else {
                return None;
            };
            if iterable.is_some() && index == 0 && operation == CoerceOp::Iterate {
                continue;
            }
            if ordinary_operands.contains(&index) {
                continue;
            }
            // An absent argument is `undefined`.
            let Some(values) = args.get(index) else {
                continue;
            };
            if !operation.silent_on_primitive()
                || values.is_empty()
                || !values.iter().all(AbstractValue::is_primitive)
            {
                return None;
            }
        }
        if let NativeIdentitySource::AuthorInput { evidence, .. } = source {
            let source_span = site.source_span();
            self.observations.record_native_operation_input_use(
                source_span,
                crate::reports::schema::NativeOperationInputUse {
                    evidence: *evidence,
                    source_location: None,
                    span: source_span,
                },
            );
        }
        if let Some(proof) = &own_property_projection {
            self.observations
                .record_inference_use(proof.inference_use());
        }
        let operation_coverage = if kind == CallKind::New
            && matches!(row.op, IntrinsicOp::Map | IntrinsicOp::WeakMap)
            && expressions.is_empty()
        {
            let mut dependencies = self.observations.dependencies().clone();
            dependencies.extend(&self.queries.dependencies());
            super::allocation_transfer::keyed_collection_new(row, site, dependencies)
                .map(|result| result.effects)
        } else {
            None
        };
        let plan = native_operation::plan(
            row,
            site,
            args,
            own_property_projection.as_ref(),
            application,
            definition,
            self.state.world.revision.prefix,
        )?;
        let mut dependencies = self.observations.dependencies().clone();
        dependencies.extend(&self.queries.dependencies());
        Some(NativeCallResolution {
            row,
            plan,
            iterable,
            operation_coverage,
            projected_reads,
            dependencies,
        })
    }

    /// Preserve the exact native row contract; the shared iterable operation
    /// owns protocol/content admission for both spread and construction.
    fn admit_set_iteration(
        &mut self,
        row: &'static Row,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
        expressions: &[ValueExpr],
        arguments: &[Values],
    ) -> Option<NativeIterableReceipt> {
        if kind != CallKind::New
            || row.op != IntrinsicOp::Set
            || row.invocation != [Invocation::Construct]
            || row.arity != (0..=1)
            || row.coerces != [(ArgSel::Arg(0), CoerceOp::Iterate)]
            || row.allocates != Some(Kind::Set)
            || !row.mutates.is_empty()
            || row.returns != Return::Fresh
            || row.throws != Throws::Never
            || !matches!(callee, ValueExpr::Binding(binding) if binding.0.as_ref() == "Set")
        {
            return None;
        }
        let [expression] = expressions else {
            return None;
        };
        let [values] = arguments else {
            return None;
        };
        let input = if let Some((projection, _)) = self
            .inputs
            .set_iterable_proof
            .native_iterable_operand(site.source_span())
        {
            let NativeIterableOperandOrigin::ImmediateArrayLiteral { allocation_site } =
                projection.operand_origin();
            if Some(projection.owner()) != self.state.current_top_owner
                || !matches!(expression, ValueExpr::ObjectLiteral(array, _) if *array == allocation_site)
            {
                return None;
            }
            NativeIterableInput::ImmediateArrayLiteral(allocation_site)
        } else {
            match expression {
                ValueExpr::ObjectLiteral(array, _)
                    if self.inputs.index.array_elements.contains_key(array) =>
                {
                    NativeIterableInput::ImmediateArrayLiteral(*array)
                }
                _ => NativeIterableInput::Borrowed,
            }
        };
        self.native_iterable(NativeIterableUse::SetConstruction { site, input }, values)
    }

    pub(super) fn native_iterable(
        &mut self,
        operation: NativeIterableUse,
        operand: &Values,
    ) -> Option<NativeIterableReceipt> {
        let RuntimeValue::Heap(HeapResult::Iteration(receipt)) =
            self.request(RuntimeOperation::Heap(HeapOperation::IterateNative {
                operation,
                operand: operand.clone(),
            }))
        else {
            unreachable!("native iterable requests return their operation receipt");
        };
        *receipt
    }

    pub(super) fn transfer_native_iterable(
        &mut self,
        operation: NativeIterableUse,
        operand: &Values,
    ) -> Option<NativeIterableReceipt> {
        if let NativeIterableUse::ArraySpread(span) = operation
            && let Some((input, source_index, source)) =
                self.inputs.set_iterable_proof.imported_array_spread(span)
        {
            // The selected source and its author lifetime premise are bound
            // to this exact spread token. This transfers only finite
            // primitive yields; it deliberately creates no provider heap
            // identity or provider-content read cell.
            if !self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS) {
                return None;
            }
            let mut dependencies = self.settled_prefix_dependencies();
            dependencies
                .imported_primitive_array_spread_inputs
                .insert(input.evidence.reference.clone());
            self.record_proof_dependencies(NativeIntegrityGuard::ArrayIntrinsics, &dependencies);
            self.observations
                .record_inference_use(crate::analysis_provenance::InferenceUse {
                    conclusion: InferenceRule::ImportedPrimitiveArraySpread,
                    derivation_rules: BTreeSet::from([
                        InferenceRule::ImportedPrimitiveArraySpread,
                        InferenceRule::NativeIntegrity,
                    ]),
                    value_input_refs: BTreeSet::new(),
                    descriptor_copy_author_refs: BTreeSet::new(),
                    argument_input_refs: BTreeSet::new(),
                    native_operation_input_refs: BTreeSet::new(),
                    imported_primitive_array_spread_input_refs: BTreeSet::from([input
                        .evidence
                        .reference
                        .clone()]),
                });
            self.observations
                .record_imported_primitive_array_spread_input_use(
                    crate::analysis_inputs::ImportedPrimitiveArraySpreadInputUse {
                        evidence: input.evidence.clone(),
                        operand_index: source_index,
                    },
                );
            let yields = source
                .primitive_slots
                .iter()
                .map(|kind| Values::from([AbstractValue::Value(Value::Prim(*kind))]))
                .collect();
            return Some(NativeIterableReceipt {
                source: None,
                yields: NativeIterableYields::ExactSlots(yields),
                heap_contents: crate::facts::HeapContentCells::default(),
                input: NativeIterableInput::Borrowed,
                coverage: EffectCoverageFragment::native_iterable_protocol(&dependencies),
            });
        }
        let [AbstractValue::Value(Value::Alloc(kind, object))] =
            operand.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if !self
            .state
            .world
            .surface
            .object_accessors_are_settled(*object)
            || self.state.world.arrays.unknown_contents.contains(object)
            || self
                .state
                .world
                .surface
                .applied_accessors
                .contains_key(object)
            || self.state.world.native.tainted_sets.contains(object)
        {
            return None;
        }
        let proof = self.inputs.set_iterable_proof;
        let mut dependencies = self.settled_prefix_dependencies();
        let yields = match operation {
            NativeIterableUse::ArraySpread(span) if *kind == Kind::Set => {
                if proof.stable_spread_source(span) != Some(*object) {
                    return None;
                }
                let premise = proof.stable_spread_dependencies(span)?;
                dependencies.extend(premise);
                self.record_proof_dependencies(NativeIntegrityGuard::SetIterable, premise);
                NativeIterableYields::PrimitiveOnly
            }
            NativeIterableUse::ArraySpread(span) if *kind == Kind::Object => {
                if let Some(premise) = proof.stable_array_spread_dependencies(span) {
                    if proof.stable_array_spread_source(span) != Some(*object) {
                        return None;
                    }
                    dependencies.extend(premise);
                    let inference = proof.stable_array_spread_inference_use(span)?;
                    self.record_inference_use(
                        NativeIntegrityGuard::ArrayIntrinsics,
                        inference,
                        premise,
                    );
                } else if !self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS) {
                    return None;
                }
                if let Some(slots) = self.settled_dense_array_slots(*object) {
                    NativeIterableYields::ExactSlots(slots)
                } else {
                    let premise = self.state.world.arrays.primitive.get(object)?;
                    dependencies.extend(premise);
                    NativeIterableYields::PrimitiveOnly
                }
            }
            NativeIterableUse::SetConstruction { site, input } if *kind == Kind::Object => {
                if matches!(input, NativeIterableInput::ImmediateArrayLiteral(origin) if origin != *object)
                {
                    return None;
                }
                let span = site.source_span();
                let premise =
                    if let Some((projection, premise)) = proof.native_iterable_operand(span) {
                        if Some(projection.owner()) != self.state.current_top_owner
                            || !matches!(input,
                            NativeIterableInput::ImmediateArrayLiteral(origin)
                                if matches!(
                                    projection.operand_origin(),
                                    NativeIterableOperandOrigin::ImmediateArrayLiteral {
                                        allocation_site
                                    } if allocation_site == origin && allocation_site == *object
                                ))
                        {
                            return None;
                        }
                        premise
                    } else {
                        proof.dependencies_for_constructor(span)?
                    };
                dependencies.extend(premise);
                let yields = if let Some(slots) = self.settled_dense_array_slots(*object) {
                    if !slots.iter().all(|slot| !slot.is_empty() && slot.iter().all(|value| {
                        matches!(value, AbstractValue::Value(Value::Prim(kinds)) if [PrimKinds::STRING, PrimKinds::NUMBER, PrimKinds::BOOLEAN, PrimKinds::NULL, PrimKinds::BIGINT].contains(kinds))
                    })) { return None; }
                    NativeIterableYields::ExactSlots(slots)
                } else {
                    let array_dependencies = self.state.world.arrays.primitive.get(object)?;
                    dependencies.extend(array_dependencies);
                    NativeIterableYields::PrimitiveOnly
                };
                if let Some(evidence) = proof.author_input_for_constructor(span).cloned() {
                    self.record_proof_dependencies(NativeIntegrityGuard::SetIterable, premise);
                    self.observations.record_native_operation_input_use(
                        span,
                        crate::reports::schema::NativeOperationInputUse {
                            evidence,
                            source_location: None,
                            span,
                        },
                    );
                } else {
                    self.record_inference_use(
                        NativeIntegrityGuard::SetIterable,
                        proof.inference_use_for_constructor(span)?,
                        premise,
                    );
                }
                yields
            }
            _ => return None,
        };
        dependencies.extend(&self.settled_prefix_dependencies());
        let mut heap_contents = crate::facts::HeapContentCells::default();
        heap_contents.reads.insert(match kind {
            Kind::Set => crate::facts::EffectCell::SetContents(*object),
            Kind::Object => crate::facts::EffectCell::ArrayContents(*object),
            _ => unreachable!("native iterable admission only models Set or Array sources"),
        });
        let input = if let NativeIterableUse::SetConstruction { site, input } = operation {
            heap_contents
                .allocations
                .insert(crate::facts::EffectCell::SetContents(site.allocation_id()));
            input
        } else {
            NativeIterableInput::Borrowed
        };
        Some(NativeIterableReceipt {
            source: Some(*object),
            yields,
            heap_contents,
            input,
            coverage: EffectCoverageFragment::native_iterable_protocol(&dependencies),
        })
    }

    /// Author-asserted timing for two exact operations in the pinned Bun host.
    /// This does not supply a return value or a purity/throw summary.
    fn host_call_does_not_reenter(&mut self, call: &Call) -> bool {
        if call.has_spread
            || !call.args.is_empty()
            || !self.inputs.index.dynamic_scope_owners.is_empty()
        {
            return false;
        }
        let mut dependencies = crate::analysis_inputs::ProofDependencies::default();
        // Both arms below hold only when the index saw the matching
        // `reentry_free_operations` entry; an unaudited host selects neither.
        let operation = match (call.kind, &call.callee) {
            (CallKind::New, ValueExpr::Binding(binding))
                if self
                    .inputs
                    .index
                    .host_async_local_storage_imports
                    .contains(binding) =>
            {
                spec::ReentryFreeOperation::AsyncLocalStorageConstruct
            }
            (CallKind::Call, ValueExpr::Member(receiver, Some(member), MemberKey::Static, _))
                if call.no_reentry_candidate && member == "resolve" =>
            {
                let ValueExpr::Binding(binding) = receiver.as_ref() else {
                    return false;
                };
                if !self.inputs.index.host_promise_resolve
                    || binding.0.as_ref() != "Promise"
                    || self.inputs.flow.is_declared(binding)
                    || self.inputs.shadowed.contains("Promise")
                {
                    return false;
                }
                let site = CallSite::from(call.span);
                let native_input = self
                    .inputs
                    .index
                    .native_operation_input_evidence
                    .get(&site)
                    .filter(|evidence| {
                        evidence.operation
                            == NativeInputKind::Operation(spec::NativeOperationKind::PromiseResolve)
                    });
                let native_integrity_intact = self.native_guard_intact(
                    NativeIntegrityGuard::HostPromiseResolve,
                    &[
                        IntrinsicSlot::Global(NativeIntrinsic::Promise),
                        IntrinsicSlot::Static(NativeIntrinsic::Promise, "resolve"),
                    ],
                );
                if !native_integrity_intact && native_input.is_none() {
                    return false;
                }
                if !native_integrity_intact && let Some(evidence) = native_input {
                    dependencies
                        .native_operation_inputs
                        .insert(evidence.reference.clone());
                    self.observations.record_native_operation_input_use(
                        call.span,
                        crate::reports::schema::NativeOperationInputUse {
                            evidence: evidence.clone(),
                            source_location: None,
                            span: call.span,
                        },
                    );
                }
                spec::ReentryFreeOperation::PromiseResolve
            }
            _ => return false,
        };
        dependencies
            .host_behavior
            .insert(crate::analysis_inputs::HostBehaviorKey::ReentryFreeOperation(operation));
        self.consume_proof_dependencies(&dependencies);
        let proof_use = crate::reports::schema::ReentryFreeOperationUse {
            operation,
            source_location: None,
            span: call.span,
        };
        if !self.state.author_host_contract_uses.contains(&proof_use) {
            self.state.author_host_contract_uses.push(proof_use);
        }
        true
    }

    /// Commit the admitted row's heap mutations to this prefix, then project
    /// its normal return value. The mutations happen whether or not the
    /// caller uses the value.
    pub(super) fn execute_native_resolution(
        &mut self,
        resolution: NativeCallResolution,
        args: &[Values],
        may_throw: bool,
        site: CallSite,
    ) -> Values {
        let NativeCallResolution {
            row,
            plan,
            iterable,
            operation_coverage,
            projected_reads: _,
            dependencies: _,
        } = resolution;
        let fresh = match native_operation::execute(self, plan) {
            Ok(fresh) => fresh,
            // The prefix moved between admission and execution: nothing was
            // committed and no row fact describes this call's result.
            Err(native_operation::StalePlan) => {
                let mut values = unknown();
                values.insert(AbstractValue::Value(Value::Unavailable));
                return values;
            }
        };
        let mut values = match row.returns {
            Return::Unknown => unknown(),
            Return::Prim(kinds) => Values::from([AbstractValue::Value(Value::Prim(kinds))]),
            Return::Arg(ArgSel::Arg(index)) => args.get(index).cloned().unwrap_or_else(unknown),
            Return::Arg(ArgSel::Rest(_)) => unknown(),
            Return::Fresh if row.allocates == Some(Kind::Set) && iterable.is_some() => {
                // The common operation modeled construction from primitive
                // iteration. Preserve only its allocation kind/origin; this
                // is no Set contents, size, properties or singleton proof.
                Values::from([AbstractValue::Value(Value::Alloc(
                    Kind::Set,
                    site.allocation_id(),
                ))])
            }
            Return::Fresh if operation_coverage.is_some() => unknown(),
            Return::Fresh => fresh.unwrap_or_else(|| self.fresh_allocation_values(row, args, site)),
        };
        if may_throw {
            values.insert(AbstractValue::Value(Value::Unavailable));
        }
        values
    }

    fn record_native_initializer_transfer(
        &mut self,
        callee: &ValueExpr,
        expressions: &[ValueExpr],
        arguments: &[Values],
        may_throw: bool,
        site: CallSite,
        resolution: &NativeCallResolution,
    ) {
        let Some(candidate) = self.native_initializer_candidate else {
            return;
        };
        let kind = candidate.kind();
        let (expected_op, expected_allocation, expected_return) = match kind {
            NativeConstructorKind::Set => (IntrinsicOp::Set, Some(Kind::Set), Return::Fresh),
            NativeConstructorKind::TextEncoder => (IntrinsicOp::TextEncoder, None, Return::Unknown),
        };
        // `resolution` comes from this New dispatch's resolve_native_call,
        // which already proves the exact unshadowed global identity through
        // its census or exact-site input. The builtin binding itself stays
        // opaque in value flow, so its abstract value is not an identity test.
        let row = resolution.row;
        let dispatch_form = match (kind, expressions) {
            (NativeConstructorKind::Set | NativeConstructorKind::TextEncoder, []) => {
                NativeConstructorForm::Empty
            }
            (NativeConstructorKind::Set, [ValueExpr::ObjectLiteral(array, _)]) => {
                NativeConstructorForm::SetPrimitiveArray { array: *array }
            }
            _ => return,
        };
        let names_expected_global = matches!(callee, ValueExpr::Binding(binding)
            if binding.0.as_ref() == kind.global_name());
        // Empty collection forms cannot run coercions of absent arguments.
        // TextEncoder's host row has exact zero arity and no coercions.
        let root_call_shape_matches = match (kind, dispatch_form) {
            (NativeConstructorKind::Set, NativeConstructorForm::Empty) => {
                row.arity.contains(&0)
                    && row
                        .coerces
                        .iter()
                        .all(|(selector, _)| matches!(selector, ArgSel::Arg(_)))
            }
            (NativeConstructorKind::TextEncoder, NativeConstructorForm::Empty) => {
                row.arity == (0..=0) && row.coerces.is_empty()
            }
            (NativeConstructorKind::Set, NativeConstructorForm::SetPrimitiveArray { array }) => {
                row.arity.contains(&1)
                    && row.coerces == [(ArgSel::Arg(0), CoerceOp::Iterate)]
                    && matches!(expressions, [ValueExpr::ObjectLiteral(lowered, _)] if *lowered == array)
                    && matches!(arguments, [values]
                        if matches!(values.iter().collect::<Vec<_>>().as_slice(),
                            [AbstractValue::Value(Value::Alloc(Kind::Object, evaluated))]
                                if *evaluated == array))
            }
            _ => false,
        };
        if candidate.matches_dispatch(site, kind, dispatch_form)
            && names_expected_global
            && !may_throw
            && row.op == expected_op
            && row.invocation == [Invocation::Construct]
            && root_call_shape_matches
            && row.invokes.is_empty()
            && row.allocates == expected_allocation
            && row.mutates.is_empty()
            && row.returns == expected_return
            && row.throws == Throws::Never
        {
            let mut dependencies = self.observations.dependencies().clone();
            dependencies.extend(&self.queries.dependencies());
            self.native_initializer_transfer =
                Some(NativeConstructorTransfer::applied(candidate, dependencies));
        }
    }

    /// The fresh allocation's value: its contents are the row's writes to
    /// it; an allocation with no tracked contents is unknown.
    fn fresh_allocation_values(
        &mut self,
        row: &'static Row,
        args: &[Values],
        site: CallSite,
    ) -> Values {
        if args.is_empty()
            && super::allocation_transfer::keyed_collection_new(
                row,
                site,
                ProofDependencies::default(),
            )
            .is_some()
        {
            // The operation kernel proves the row's fresh empty-construction
            // semantics. This interpreter has no keyed-collection heap lane,
            // so keep its established conservative value projection.
            return unknown();
        }
        let mut values = Values::new();
        for mutation in row.mutates {
            match *mutation {
                // The checked plan supplies these fresh allocations; a row
                // reaching here without one has no tracked contents.
                Mutation::FreshOwnDescriptorsOf(_) | Mutation::FreshOwnKeysOf(_) => {
                    return unknown();
                }
                Mutation::FreshRetains(ArgSel::Arg(index)) => {
                    let Some(argument) = args.get(index) else {
                        return unknown();
                    };
                    // Data descriptors retain object identities, whose fields can
                    // acquire callbacks later. Also snapshot current callbacks so
                    // replacing a source property cannot erase copied closures.
                    let mut retained = argument.clone();
                    retained.extend(self.callback_values(argument));
                    // This retained graph establishes no recursive ordinary-data
                    // shape or stronger retained-factory input contract.
                    values.extend([
                        self.deferred_callbacks(retained),
                        AbstractValue::Value(Value::Top),
                    ]);
                }
                Mutation::Integrity(_)
                | Mutation::DefineProperties { .. }
                | Mutation::DefineProperty { .. } => {}
                Mutation::FreshRetains(ArgSel::Rest(_)) => return unknown(),
            }
        }
        if values.is_empty() {
            return unknown();
        }
        values
    }

    fn standard_object_prototype_property(property: &str) -> bool {
        matches!(
            property,
            "constructor"
                | "__proto__"
                | "__defineGetter__"
                | "__defineSetter__"
                | "__lookupGetter__"
                | "__lookupSetter__"
                | "hasOwnProperty"
                | "isPrototypeOf"
                | "propertyIsEnumerable"
                | "toLocaleString"
                | "toString"
                | "valueOf"
        )
    }

    /// A certified private constructor may read a missing field on its fresh
    /// plain-data input. Keep this precision within that constructor call:
    /// other paths can join a fresh object with a live imported or published
    /// value, and their ordinary member-read fallback must remain conservative.
    /// A11 also excludes an inherited user getter unless a visible mutation
    /// has invalidated the ordinary Object.prototype assumption.
    pub(super) fn absent_plain_data_property(&mut self, object: u32, property: &str) -> bool {
        if (self
            .state
            .world
            .classes
            .private_constructor_result_owners
            .is_empty()
            && !self
                .state
                .world
                .classes
                .checked_constructor_inputs
                .contains(&object))
            || Self::standard_object_prototype_property(property)
            || !self.inputs.index.plain_data_objects.contains(&object)
            || !self.state.world.object_properties_are_settled(object)
            || !self.input_field_prototype_absent(object, property)
        {
            return false;
        }
        self.state
            .world
            .surface
            .prefix_object_properties
            .get(&object)
            .is_some_and(|properties| {
                !properties.contains_key(&None)
                    && !properties.contains_key(&Some(property.to_owned()))
            })
    }

    pub(super) fn accepts_deferred_global_write(
        &mut self,
        owner: SourceUnitOrdinal,
        env: &Context,
    ) -> bool {
        if !self
            .inputs
            .index
            .deferred_global_certificates
            .contains(&owner)
        {
            return false;
        }
        let Some(candidate) = &self.inputs.index.deferred_global_candidates[owner.0] else {
            return false;
        };
        let values = self.evaluate(&candidate.value, env, env);
        matches!(
            values.iter().collect::<Vec<_>>().as_slice(),
            [AbstractValue::Closure(_, _)]
                | [AbstractValue::Value(
                    Value::Alloc(Kind::Function, _) | Value::Alloc(Kind::Class, _)
                )]
        )
    }
    /// The payload heap retains accessor callbacks as capabilities, not as
    /// the result of reading their property. Only a data-tagged lookup may
    /// project that payload into an expression value.
    pub(super) fn payload_lookup_is_data(&self, object: u32, property: Option<&str>) -> bool {
        if self
            .state
            .world
            .surface
            .applied_accessors
            .get(&object)
            .is_some_and(|accessors| {
                property.is_none_or(|property| accessors.contains_key(property))
            })
        {
            return false;
        }
        if let Some(shape) = self.inputs.index.ordinary_record_shape(object) {
            return match property {
                Some(property) => !matches!(
                    shape.properties.get(property),
                    Some(super::value_model::OwnPropertyDescriptorSyntax::Accessor { .. })
                ),
                None => shape.properties.values().all(|descriptor| {
                    matches!(
                        descriptor,
                        super::value_model::OwnPropertyDescriptorSyntax::Data
                    )
                }),
            };
        }
        // Arrays and runtime-created data copies have their own shape proof.
        // An ambiguous source literal has neither, so its pooled payload does
        // not establish the descriptor selected by a later property lookup.
        self.inputs.index.array_elements.contains_key(&object)
            || self
                .state
                .world
                .surface
                .ordinary_spread_copies
                .contains(&object)
            || self
                .state
                .world
                .surface
                .descriptor_copy_results
                .contains(&object)
            || self
                .state
                .world
                .publication
                .parameter_initialized_objects
                .contains(&object)
    }

    /// Capabilities retained by the selected member, not its Get result.
    /// Called with receivers already evaluated at the current read prefix.
    pub(super) fn selected_member_capabilities(
        &self,
        receivers: &Values,
        property: Option<&str>,
    ) -> Values {
        let mut selected = Values::new();
        for receiver in receivers {
            match receiver {
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    if let Some(properties) = self
                        .state
                        .world
                        .surface
                        .prefix_object_properties
                        .get(object)
                    {
                        if let Some(property) = property {
                            selected.extend(
                                properties
                                    .get(&Some(property.to_owned()))
                                    .into_iter()
                                    .flatten()
                                    .cloned(),
                            );
                            selected.extend(properties.get(&None).into_iter().flatten().cloned());
                        } else {
                            selected.extend(properties.values().flatten().cloned());
                        }
                    }
                }
                // These carriers already projected their retained capabilities
                // through Member. Preserve that policy without reevaluation.
                AbstractValue::Instance(_, _, _)
                | AbstractValue::DeferredCallbacks(_)
                | AbstractValue::ForeignDeferredRecord
                | AbstractValue::ArrayIntrinsic => {
                    selected.insert(receiver.clone());
                }
                AbstractValue::AuditedRetainedResult(_, captured) => {
                    selected.insert(AbstractValue::DeferredCallbacks(captured.clone()));
                }
                AbstractValue::Value(Value::Alloc(Kind::Class, key))
                | AbstractValue::ClassMembers(key) => {
                    selected.insert(AbstractValue::ClassMembers(*key));
                }
                AbstractValue::OwnDescriptorMap(site) => {
                    selected.extend(self.descriptor_map_callbacks(*site));
                }
                AbstractValue::Value(Value::Unavailable) => {}
                _ => {
                    selected.insert(AbstractValue::Value(Value::Top));
                }
            }
        }
        selected
    }

    pub(super) fn known_local_instance_member(&self, key: u32, property: Option<&str>) -> bool {
        !self.state.world.classes.tainted.contains(&key)
            && property.is_some_and(|property| {
                self.inputs
                    .index
                    .class_values
                    .get(&key)
                    .is_some_and(|class| {
                        class.ordinary_instance_methods.contains(property)
                            || class
                                .instance_fields
                                .get(property)
                                .is_some_and(|value| !matches!(value, ValueExpr::Unknown))
                    })
                    || self
                        .inputs
                        .index
                        .local_constructors
                        .get(&key)
                        .is_some_and(|constructor| constructor.own_fields.contains(property))
            })
    }

    pub(super) fn ordinary_data_properties(
        &self,
        values: &Values,
    ) -> Option<BTreeMap<Option<String>, Values>> {
        let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if !self.state.world.object_properties_are_settled(*object)
            || self
                .state
                .world
                .surface
                .applied_accessors
                .contains_key(object)
            || (!self.inputs.index.plain_data_objects.contains(object)
                && !self
                    .state
                    .world
                    .surface
                    .ordinary_spread_copies
                    .contains(object))
        {
            return None;
        }
        let properties = self
            .state
            .world
            .surface
            .prefix_object_properties
            .get(object)?;
        if properties.contains_key(&None) {
            return None;
        }
        Some(properties.clone())
    }

    fn array_intrinsics_intact(&mut self) -> bool {
        self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS)
    }

    fn array_slots_intact(&mut self, slots: &[IntrinsicSlot]) -> bool {
        let array = ("Array".into(), SyntaxContext::empty());
        !self.state.world.arrays.intrinsic_mutated
            && !self.inputs.flow.is_declared(&array)
            && !self.inputs.shadowed.contains("Array")
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && self.native_guard_intact(NativeIntegrityGuard::ArrayIntrinsics, slots)
    }

    fn settled_dense_array_slots(&self, object: u32) -> Option<Vec<Values>> {
        self.state
            .world
            .object_properties_are_settled(object)
            .then(|| {
                self.state
                    .world
                    .arrays
                    .dense
                    .get(&object)
                    .map(|array| array.slots.clone())
            })
            .flatten()
    }

    /// Transfer the ordinary dense-array iterator over the evaluated operand's
    /// prefix heap. The operation adapter supplies its already-defined native
    /// protocol premise (Set's typed certificate or a spread's exact Array
    /// slots) at the same point the previous dense-source check consulted it.
    fn dense_array_iteration_transfer(
        &mut self,
        values: &Values,
        protocol_intact: impl FnOnce(&mut Self) -> bool,
    ) -> DenseArrayIterationTransfer {
        let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return DenseArrayIterationTransfer::Refused;
        };
        // Exact singleton matching rejects an Unavailable throw alternative
        // before consulting protocol slots, as the previous dense-source
        // guard did.
        if !protocol_intact(self) {
            return DenseArrayIterationTransfer::Refused;
        }
        match self.settled_dense_array_slots(*object) {
            Some(slots) => DenseArrayIterationTransfer::Yielded {
                source: *object,
                slots,
            },
            None => DenseArrayIterationTransfer::Refused,
        }
    }

    pub(super) fn dense_array_push_receiver(&mut self, receiver: &Values) -> Option<u32> {
        let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
            receiver.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        (self.state.world.arrays.dense.contains_key(object)
            && self
                .state
                .world
                .surface
                .object_accessors_are_settled(*object)
            && self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS))
        .then_some(*object)
    }

    fn execute_dense_array_push(
        &mut self,
        callee: &ValueExpr,
        args: &[Values],
        env: &Context,
        captures: &Context,
        prepared_receiver: Option<&Values>,
    ) -> bool {
        let ValueExpr::Member(receiver, Some(method), MemberKey::Static, _) = callee else {
            return false;
        };
        if method != "push"
            || !matches!(receiver.as_ref(), ValueExpr::Binding(_))
            || args
                .iter()
                .any(|values| values.is_empty() || values.iter().any(|value| !value.is_primitive()))
        {
            return false;
        }
        let receiver = prepared_receiver
            .cloned()
            .unwrap_or_else(|| self.evaluate(receiver, env, captures));
        let Some(object) = self.dense_array_push_receiver(&receiver) else {
            return false;
        };
        // An intact ordinary array has its own writable length, and the
        // guarded prototypes cannot intercept the appended primitive slots.
        // Preserve its contained callbacks, but invalidate the precise array
        // contents after this real mutation. No descriptor replacement or
        // getter installation occurred. Refuse exact array snapshots without
        // claiming a fresh value or modeling how often this site executed.
        if self
            .state
            .world_mut()
            .arrays
            .unknown_contents
            .insert(object)
        {
            self.state.world_mut().revision.bump_prefix();
        }
        self.invalidate_writable_object(object);
        true
    }

    fn retain_dense_array(&mut self, object: u32, slots: Vec<Values>) {
        self.literal_heap().dense_array(object, slots);
    }

    pub(super) fn literal_heap(&mut self) -> super::allocation_transfer::LiteralHeap<'_> {
        self.state.world_mut().literal_heap()
    }

    // CLEANUP(added 2026-11-10): delete with the legacy gate (L4e). The
    //   kernel row `LocalExecutionContext::iterate_array`
    //   (`native/array_iteration.rs`) replaces it for kernel-run bodies; this
    //   path stays while the legacy gate reads its `Outcome` for flattened
    //   owners, the flat `invoke_with_cells` lane and refused remainders.
    pub(super) fn array_iteration_plan(
        &mut self,
        span: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<(
        ArrayIteration,
        u32,
        DenseArray,
        u32,
        Option<CapturedContext>,
    )> {
        let proof = self.inputs.index.array_iterations.get(&span)?.clone();
        proof.result?;
        let values = self.evaluate(&proof.source, env, captures);
        let DenseArrayIterationTransfer::Yielded { source, slots } = self
            .dense_array_iteration_transfer(&values, |executor| {
                executor.array_slots_intact(proof.row.requires)
            })
        else {
            return None;
        };
        let dense = DenseArray { slots };
        if dense.slots.len() > MAX_SYNCHRONOUS_INVOCATIONS {
            return None;
        }
        for values in &dense.slots {
            if values.is_empty()
                || values.iter().any(|value| match value {
                    AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                        (!self.inputs.index.plain_data_objects.contains(object)
                            && !self
                                .state
                                .world
                                .surface
                                .ordinary_spread_copies
                                .contains(object))
                            || !self.state.world.object_properties_are_settled(*object)
                    }
                    AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                        self.state.world.native.tainted_functions.contains(key)
                    }
                    AbstractValue::Instance(key, _, InstanceOrigin::Constructed) => {
                        self.class_may_run_on_borrowed_receiver(*key)
                    }
                    value if value.is_primitive() => false,
                    _ => true,
                })
            {
                return None;
            }
        }
        let callback = self.evaluate(&proof.callback, env, captures);
        let (key, captured) = match callback.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Value(Value::Alloc(Kind::Function, key))] => (*key, None),
            [AbstractValue::Closure(HeapAddress { callable: key, .. }, captured)] => {
                (*key, Some(captured.clone()))
            }
            _ => return None,
        };
        let callable = self.inputs.index.callables.get(&key)?;
        if callable.generator
            || self.inputs.index.async_callables.contains(&key)
            || callable.params.iter().any(|param| param.binding.is_none())
        {
            return None;
        }
        Some((proof, source, dense, key, captured))
    }

    /// The value an admitted iteration call evaluates to. A fresh array
    /// result is its allocation, whose slots the transfer retains; the
    /// accumulator is what the transfer last stored.
    pub(super) fn array_iteration_result(
        &mut self,
        proof: &ArrayIteration,
        dense: &DenseArray,
    ) -> Values {
        let result = proof.result.unwrap();
        match proof.row.result {
            IterationResult::Undefined => Values::from([AbstractValue::UNDEFINED]),
            IterationResult::Mapped | IterationResult::Filtered | IterationResult::FlatMapped => {
                Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, result))])
            }
            IterationResult::Boolean => {
                Values::from([AbstractValue::Value(Value::Prim(PrimKinds::BOOLEAN))])
            }
            IterationResult::Index => {
                Values::from([AbstractValue::Value(Value::Prim(PrimKinds::NUMBER))])
            }
            IterationResult::Element => {
                let mut values: Values = dense.slots.iter().flatten().cloned().collect();
                values.insert(AbstractValue::UNDEFINED);
                values
            }
            // Not yet transferred: what the accumulator can hold is unknown,
            // but every callback it can reach stays retained.
            IterationResult::Accumulated => {
                match self.state.world.arrays.accumulated_values.get(&result) {
                    Some(values) => values.clone(),
                    None => {
                        let mut reachable: Values = dense.slots.iter().flatten().cloned().collect();
                        reachable.insert(AbstractValue::Value(Value::Alloc(Kind::Object, result)));
                        let callbacks = self.callback_values(&reachable);
                        Values::from([
                            self.deferred_callbacks(callbacks),
                            AbstractValue::Value(Value::Top),
                        ])
                    }
                }
            }
        }
    }

    fn merge_array_effects(&mut self, outcome: &mut Outcome, mut nested: Outcome) {
        self.apply_rebind_effects(&nested.effects.rebinds);
        outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
        outcome.executed_owners.extend(nested.executed_owners);
        outcome.unknown_call |= nested.unknown_call;
        outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
    }

    fn execute_array_iteration(
        &mut self,
        span: CallSite,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) -> bool {
        let result = self.request(RuntimeOperation::Model(
            super::runtime_models::ModelOperation::ExecuteArrayIteration {
                span,
                environment: env.clone(),
                captures: captures.clone(),
                outcome: Box::new(std::mem::take(outcome)),
            },
        ));
        let RuntimeValue::Model(super::runtime_models::ModelResult::ArrayIteration {
            handled,
            outcome: model_outcome,
        }) = result
        else {
            unreachable!("array iteration models produce iteration results");
        };
        *outcome = *model_outcome;
        handled
    }

    /// Whether a callback result can be stored in a fresh result array, or
    /// spread into one, without running user code when the result is later
    /// read, iterated or spread.
    pub(super) fn retainable_iteration_value(&self, value: &AbstractValue) -> bool {
        match value {
            AbstractValue::Value(Value::Prim(_))
            | AbstractValue::Value(Value::Alloc(Kind::Function, _))
            | AbstractValue::Closure(_, _) => true,
            AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                self.state.world.object_properties_are_settled(*object)
                    && (self.inputs.index.plain_data_objects.contains(object)
                        || self
                            .state
                            .world
                            .surface
                            .ordinary_spread_copies
                            .contains(object)
                        || self.state.world.arrays.dense.contains_key(object))
            }
            _ => false,
        }
    }

    /// The slots a `flatMap` callback result contributes: one untainted
    /// dense array spreads its slots; any other retainable value is one
    /// slot. `None` when an array may sit beside other values, so slot
    /// positions are unknown.
    fn flattened_iteration_slots(&self, returned: &Values) -> Option<Vec<Values>> {
        let arrays: Vec<_> = returned
            .iter()
            .filter_map(|value| match value {
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    self.state.world.arrays.dense.get(object)
                }
                _ => None,
            })
            .collect();
        match arrays.as_slice() {
            [] => Some(vec![returned.clone()]),
            [array] if returned.len() == 1 => Some(array.slots.clone()),
            _ => None,
        }
    }

    // CLEANUP(added 2026-11-10): delete with the legacy gate (L4e). The
    //   kernel row `LocalExecutionContext::iterate_array`
    //   (`native/array_iteration.rs`) replaces it for kernel-run bodies; this
    //   path stays while the legacy gate reads its `Outcome` for flattened
    //   owners, the flat `invoke_with_cells` lane and refused remainders.
    pub(super) fn execute_array_iteration_transfer(
        &mut self,
        span: CallSite,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) -> bool {
        let Some((proof, source, dense, key, captured)) =
            self.array_iteration_plan(span, env, captures)
        else {
            return false;
        };
        let row = proof.row;
        let result = proof.result.unwrap();
        let source_values =
            Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, source))]);
        let original_callbacks = self.callback_values(&source_values);
        let protected_objects = self.reachable_objects(&source_values);
        self.state
            .array_iteration_protected_objects
            .extend(protected_objects);
        let mut accumulator = proof
            .initial
            .as_ref()
            .map(|initial| self.evaluate(initial, env, captures));
        let mut slots = Some(Vec::new());
        for values in &dense.slots {
            if row.result == IterationResult::Accumulated && accumulator.is_none() {
                accumulator = Some(values.clone());
                continue;
            }
            let mut arguments = vec![
                values.clone(),
                Values::from([AbstractValue::PRIMITIVE]),
                source_values.clone(),
            ];
            if let Some(accumulator) = &accumulator {
                arguments.insert(0, accumulator.clone());
            }
            let previous_unsupported = self.state.array_iteration_unsupported;
            self.state.array_iteration_unsupported = false;
            self.state.array_iteration_depth += 1;
            let nested = self.invoke(key, arguments, captured.clone(), false);
            self.state.array_iteration_depth -= 1;
            let unsupported = self.state.array_iteration_unsupported;
            self.state.array_iteration_unsupported |= previous_unsupported;
            let returned = nested.returns.clone();
            let callback_executed_completely = nested.body_invoked && nested.complete;
            self.merge_array_effects(outcome, nested);
            let current_source = self.evaluate(&proof.source, env, captures);
            let current_source = self.dense_array_iteration_transfer(&current_source, |executor| {
                executor.array_slots_intact(row.requires)
            });
            let valid = !unsupported && matches!(current_source,
                DenseArrayIterationTransfer::Yielded { source: current_object, slots }
                    if current_object == source && slots == dense.slots)
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Value(Value::Alloc(Kind::Object, object)) if !self.state.world.object_properties_are_settled(*object)))
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Value(Value::Alloc(Kind::Function, key)) | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) if self.state.world.native.tainted_functions.contains(key)))
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Instance(key, _, InstanceOrigin::Constructed) if self.class_may_run_on_borrowed_receiver(*key)));
            let valid = valid
                && (row.result != IterationResult::Mapped
                    || returned.iter().all(|value| {
                        self.retainable_iteration_value(value)
                            || matches!(value, AbstractValue::Instance(_, _, _))
                    }))
                && (row.result != IterationResult::Mapped
                    || returned
                        .iter()
                        .all(|value| self.retainable_iteration_value(value))
                    || callback_executed_completely)
                && (row.result != IterationResult::FlatMapped
                    || returned
                        .iter()
                        .all(|value| self.retainable_iteration_value(value)));
            if !valid {
                let callback_owner = self.inputs.index.callables[&key].owner;
                let effect_owner = self.state.current_top_owner.unwrap_or(callback_owner);
                // Effects the per-slot invocations reached stay observed.
                self.assuming(
                    AssumedEffectReason::WholeBodyPromotion {
                        operation: ExecutionOperation::ArrayIteration,
                    },
                    |this| this.promote_lazy_owner(callback_owner, effect_owner, outcome),
                );
                self.state
                    .world_mut()
                    .surface
                    .tainted_objects
                    .insert(result);
                self.state
                    .world_mut()
                    .surface
                    .prefix_object_properties
                    .entry(result)
                    .or_default()
                    .entry(None)
                    .or_default()
                    .extend(original_callbacks.iter().cloned());
                if row.result == IterationResult::Accumulated {
                    let mut retained = original_callbacks.clone();
                    retained.extend(self.callback_values(&returned));
                    retained.extend(accumulator.iter().flatten().cloned());
                    let fallback = Values::from([
                        self.deferred_callbacks(retained),
                        AbstractValue::Value(Value::Top),
                    ]);
                    self.join_accumulated_value(result, fallback);
                }
                self.invoke_possible_callbacks_at(
                    &[original_callbacks],
                    false,
                    Some(span.source_span()),
                    outcome,
                );
                return false;
            }
            match row.result {
                IterationResult::Mapped => {
                    let contents = self
                        .state
                        .world_mut()
                        .surface
                        .prefix_object_properties
                        .entry(result)
                        .or_default()
                        .entry(None)
                        .or_default();
                    let before = contents.len();
                    contents.extend(returned.iter().cloned());
                    if contents.len() != before {
                        self.state.world_mut().revision.bump_prefix();
                    }
                    if returned
                        .iter()
                        .all(|value| self.retainable_iteration_value(value))
                    {
                        if let Some(slots) = &mut slots {
                            slots.push(returned);
                        }
                    } else {
                        slots = None;
                        // A projected instance capability can retain the
                        // receiver's callbacks. Keep those source roots
                        // reachable from the result's unknown contents.
                        let contents = self
                            .state
                            .world_mut()
                            .surface
                            .prefix_object_properties
                            .entry(result)
                            .or_default()
                            .entry(None)
                            .or_default();
                        let before = contents.len();
                        contents.extend(original_callbacks.iter().cloned());
                        if contents.len() != before {
                            self.state.world_mut().revision.bump_prefix();
                        }
                        // The same allocation site may have a prior exact result.
                        // Revoke it before the next callback can read that site.
                        self.literal_heap().invalidate_dense_array(result);
                        let primitive_removed = self
                            .state
                            .world_mut()
                            .arrays
                            .primitive
                            .remove(&result)
                            .is_some();
                        let newly_tainted = self
                            .state
                            .world_mut()
                            .surface
                            .tainted_objects
                            .insert(result);
                        if primitive_removed || newly_tainted {
                            self.state.world_mut().revision.bump_prefix();
                        }
                    }
                }
                IterationResult::Filtered => {
                    if let Some(slots) = &mut slots {
                        slots.push(values.clone());
                    }
                }
                IterationResult::FlatMapped => {
                    match (slots.take(), self.flattened_iteration_slots(&returned)) {
                        (Some(mut retained), Some(flattened)) => {
                            retained.extend(flattened);
                            slots = Some(retained);
                        }
                        // Positions are unknown: keep the values as contents.
                        (previous, _) => {
                            let mut contents: Values =
                                previous.into_iter().flatten().flatten().collect();
                            for value in &returned {
                                match value {
                                    AbstractValue::Value(Value::Alloc(Kind::Object, object))
                                        if self.state.world.arrays.dense.contains_key(object) =>
                                    {
                                        contents.extend(
                                            self.state.world.arrays.dense[object]
                                                .slots
                                                .iter()
                                                .flatten()
                                                .cloned(),
                                        );
                                    }
                                    value => {
                                        contents.insert(value.clone());
                                    }
                                }
                            }
                            self.state
                                .world_mut()
                                .surface
                                .prefix_object_properties
                                .entry(result)
                                .or_default()
                                .entry(None)
                                .or_default()
                                .extend(contents);
                        }
                    }
                }
                IterationResult::Accumulated => accumulator = Some(returned),
                IterationResult::Undefined
                | IterationResult::Boolean
                | IterationResult::Element
                | IterationResult::Index => {}
            }
        }
        match row.result {
            IterationResult::Mapped | IterationResult::Filtered | IterationResult::FlatMapped => {
                match slots {
                    Some(slots) => self.retain_dense_array(result, slots),
                    None => {
                        self.literal_heap().invalidate_dense_array(result);
                        let primitive_removed = self
                            .state
                            .world_mut()
                            .arrays
                            .primitive
                            .remove(&result)
                            .is_some();
                        let newly_tainted = self
                            .state
                            .world_mut()
                            .surface
                            .tainted_objects
                            .insert(result);
                        if primitive_removed || newly_tainted {
                            self.state.world_mut().revision.bump_prefix();
                        }
                    }
                }
            }
            IterationResult::Accumulated => {
                // An empty receiver without an initial value throws.
                let values = accumulator
                    .unwrap_or_else(|| Values::from([AbstractValue::Value(Value::Unavailable)]));
                self.join_accumulated_value(result, values);
            }
            IterationResult::Undefined
            | IterationResult::Boolean
            | IterationResult::Element
            | IterationResult::Index => {}
        }
        true
    }

    /// Repeated evaluations of one `reduce` call join their accumulators.
    fn join_accumulated_value(&mut self, result: u32, values: Values) {
        let current = self
            .state
            .world_mut()
            .arrays
            .accumulated_values
            .entry(result)
            .or_default();
        let before = current.len();
        current.extend(values);
        if current.len() != before {
            self.state.world_mut().revision.bump_prefix();
        }
    }

    fn is_proven_map_entry_array(
        &self,
        callee: &ValueExpr,
        args: &[ValueExpr],
        env: &Context,
        captures: &Context,
        owner: SourceUnitOrdinal,
    ) -> bool {
        let ValueExpr::Binding(id) = callee else {
            return false;
        };
        id.0.as_ref() == "Map"
            && !self.inputs.flow.is_declared(id)
            && !self.inputs.shadowed.contains("Map")
            && !env.contains_key(id)
            && !captures.contains_key(id)
            && !self.inputs.index.reassigned_map
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && !self.inputs.index.dynamic_scope_owners.contains(&owner)
            && matches!(args, [ValueExpr::ObjectLiteral(span, _)]
                if self.inputs.index.map_entry_arrays.contains(span))
    }

    pub(super) fn proven_from_entries_callbacks(
        &mut self,
        call_site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<Values> {
        let proof = self
            .inputs
            .index
            .from_entries_map_calls
            .get(&call_site)
            .or_else(|| self.inputs.index.mapped_array_map_calls.get(&call_site))?
            .clone();
        let array_intrinsic = ("Array".into(), SyntaxContext::empty());
        if !self.inputs.index.dynamic_scope_owners.is_empty()
            || self.inputs.flow.is_declared(&proof.object_intrinsic)
            || self.inputs.shadowed.contains("Object")
            || self.inputs.flow.is_declared(&array_intrinsic)
            || self.inputs.shadowed.contains("Array")
            || !self.census_slots_intact(
                NativeIntegrityGuard::MappedFromEntries,
                &[
                    IntrinsicSlot::Global(NativeIntrinsic::Object),
                    IntrinsicSlot::Static(NativeIntrinsic::Object, "fromEntries"),
                    IntrinsicSlot::Prototype(NativeIntrinsic::Array),
                    IntrinsicSlot::ArrayIteratorPrototype,
                ],
            )
        {
            return None;
        }
        let ValueExpr::Binding(source_id) = &proof.source else {
            return None;
        };
        // This path deliberately accepts only a chunk-top plain-array binding.
        // Local parameters, aliases from opaque calls, and synthetic map results
        // stay on the ordinary unknown-call path.
        if !self.inputs.top_bindings.contains(source_id)
            || !self
                .inputs
                .index
                .plain_array_bindings
                .contains(source_id.0.as_ref())
        {
            return None;
        }
        let source_values = self.evaluate(&proof.source, env, captures);
        let [AbstractValue::Value(Value::Alloc(Kind::Object, array))] =
            source_values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if !self.state.world.object_properties_are_settled(*array) {
            return None;
        }
        let count = *self.inputs.index.dense_array_literals.get(array)?;
        let mut expected_entries = Values::new();
        for index in 0..count {
            let slot = self
                .inputs
                .flow
                .values_of_property(*array, &index.to_string())?;
            if slot.iter().next().is_none() || slot.iter().any(|origin| origin == Value::Top) {
                return None;
            }
            for origin in slot {
                let Value::Alloc(Kind::Object, entry) = origin else {
                    return None;
                };
                if !self.inputs.index.plain_data_objects.contains(&entry)
                    || !self
                        .inputs
                        .index
                        .primitive_data_properties
                        .contains(&(entry, proof.key.clone()))
                    || !self.state.world.object_properties_are_settled(entry)
                {
                    return None;
                }
                let key_values = self
                    .state
                    .world
                    .surface
                    .prefix_object_properties
                    .get(&entry)
                    .and_then(|properties| properties.get(&Some(proof.key.clone())))?;
                if key_values.is_empty() || key_values.iter().any(|value| !value.is_primitive()) {
                    return None;
                }
                expected_entries.insert(AbstractValue::Value(Value::Alloc(Kind::Object, entry)));
            }
        }
        let actual_entries = self
            .state
            .world
            .surface
            .prefix_object_properties
            .get(array)
            .and_then(|properties| properties.get(&None))?;
        if actual_entries != &expected_entries {
            return None;
        }
        let mut retained_values = self.callback_values(&source_values);
        // Retain the source identity so returned registry aliases taint entries.
        retained_values.extend(source_values);
        Some(retained_values)
    }

    fn callbacks_in_from_entries_source(
        &mut self,
        call_site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<Values> {
        let proof = self
            .inputs
            .index
            .from_entries_map_calls
            .get(&call_site)
            .or_else(|| self.inputs.index.mapped_array_map_calls.get(&call_site))?
            .clone();
        let source_values = self.evaluate(&proof.source, env, captures);
        Some(self.callback_values(&source_values))
    }

    pub(super) fn validated_metadata_decoration(
        &mut self,
        application: &MetadataDecorateCall,
        env: &Context,
        captures: &Context,
    ) -> Option<ValidatedMetadataDecoration> {
        if !self.inputs.index.dynamic_scope_owners.is_empty()
            || !self
                .state
                .world
                .module
                .initialized_bindings
                .contains(&application.helper)
            || !self
                .state
                .world
                .module
                .initialized_bindings
                .contains(&application.target)
        {
            return None;
        }
        let helper = self.evaluate(
            &ValueExpr::Binding(application.helper.clone()),
            env,
            captures,
        );
        let target = self.evaluate(
            &ValueExpr::Binding(application.target.clone()),
            env,
            captures,
        );
        if helper.len() != 1 || target.len() != 1 {
            return None;
        }
        let helper_key = match helper.first() {
            Some(AbstractValue::Value(Value::Alloc(Kind::Function, key))) => *key,
            Some(AbstractValue::Closure(HeapAddress { callable: key, .. }, captured))
                if captured.values.is_empty() =>
            {
                *key
            }
            _ => return None,
        };
        let Some(AbstractValue::Value(Value::Alloc(Kind::Class, target_key))) = target.first()
        else {
            return None;
        };
        if application.stable_origins(self.inputs.flow) != Some((helper_key, *target_key)) {
            return None;
        }
        if application.member_decorators.iter().any(|root| {
            !self.inputs.index.imported_bindings.contains(root)
                || self.inputs.changed_member_roots.contains(root)
        }) {
            return None;
        }
        Some(ValidatedMetadataDecoration {
            target_owner: *self.inputs.binding_owner.get(&application.target)?,
        })
    }

    pub(super) fn summarize_metadata_decoration(
        &mut self,
        application: &MetadataDecorateCall,
        validated: ValidatedMetadataDecoration,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let result = self.request(RuntimeOperation::Model(
            super::runtime_models::ModelOperation::SummarizeMetadataDecoration {
                application: application.clone(),
                validated,
                owner,
                outcome: Box::new(std::mem::take(outcome)),
            },
        ));
        let RuntimeValue::Model(super::runtime_models::ModelResult::MetadataDecoration {
            outcome: model_outcome,
        }) = result
        else {
            unreachable!("metadata decoration models produce updated outcomes");
        };
        *outcome = *model_outcome;
    }

    pub(super) fn summarize_metadata_decoration_transfer(
        &mut self,
        application: &MetadataDecorateCall,
        _validated: ValidatedMetadataDecoration,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        // The author certifies the helper's dispatch and every decorator's
        // metadata-only behavior, not purity. Preserve all static dependencies
        // of their bodies without manufacturing calls into the target methods.
        for binding in std::iter::once(&application.helper)
            .chain(&application.decorators)
            .chain(&application.member_decorators)
        {
            if let Some(&binding_owner) = self.inputs.binding_owner.get(binding) {
                let (reads, rebinds) = self
                    .inputs
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(binding_owner);
                self.promote_lazy_effects(reads, rebinds, owner, outcome);
                outcome.executed_owners.insert(binding_owner);
            }
        }
        self.apply_rebind_effects(&outcome.effects.rebinds);
    }

    fn all_owners(&self) -> impl Iterator<Item = SourceUnitOrdinal> + '_ {
        self.inputs.facts.iter().map(|fact| fact.ordinal)
    }

    pub(super) fn promote_lazy_owner(
        &self,
        owner: SourceUnitOrdinal,
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let (reads, rebinds) = self
            .inputs
            .synchronous_effects(owner, self.state.world.reachability.incomplete);
        self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
    }

    /// Attributes `reads` and `rebinds` to `effect_owner`, assumed when a
    /// conservative path is in force.
    fn promote_lazy_effects(
        &self,
        reads: &BTreeSet<Id>,
        rebinds: &BTreeSet<Id>,
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        self.promote_top_effects(
            self.top_restricted(reads),
            self.top_restricted(rebinds),
            effect_owner,
            outcome,
        );
    }

    /// The top-level bindings among sorted `ids`, in order.
    fn top_restricted<'a>(&self, ids: &'a BTreeSet<Id>) -> Vec<&'a Id> {
        ids.iter()
            .filter(|id| self.inputs.top_bindings.contains(*id))
            .collect()
    }

    /// `promote_lazy_effects` for sorted, distinct top-level `promoted_reads`
    /// and `promoted_rebinds`.
    pub(super) fn promote_top_effects(
        &self,
        promoted_reads: Vec<&Id>,
        promoted_rebinds: Vec<&Id>,
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        if self.promotes_nothing(&promoted_reads, &promoted_rebinds, effect_owner, outcome) {
            return;
        }
        let uninitialized: Vec<&Id> = promoted_reads
            .iter()
            .copied()
            .filter(|id| !self.state.world.module.initialized_bindings.contains(*id))
            .collect();
        let effects = &mut outcome.effects;
        let assumed = &mut effects.fallback_only;
        match self.state.assumption.as_ref() {
            Some(reason) => {
                let held = HeldBindings::ByOwner(&effects.reads);
                note_assumed(&mut assumed.reads, &promoted_reads, held, reason);
                let held = HeldBindings::Plain(&effects.uninitialized_reads);
                note_assumed(
                    &mut assumed.uninitialized_reads,
                    &uninitialized,
                    held,
                    reason,
                );
                let held = HeldBindings::ByOwner(&effects.rebinds);
                note_assumed(&mut assumed.rebinds, &promoted_rebinds, held, reason);
            }
            None => {
                note_observed(&mut assumed.reads, &promoted_reads);
                note_observed(&mut assumed.uninitialized_reads, &uninitialized);
                note_observed(&mut assumed.rebinds, &promoted_rebinds);
            }
        }
        insert_effects(&mut effects.reads, &promoted_reads, |id| {
            (id.clone(), effect_owner)
        });
        insert_effects(&mut effects.uninitialized_reads, &uninitialized, Id::clone);
        insert_effects(&mut effects.rebinds, &promoted_rebinds, |id| {
            (id.clone(), effect_owner)
        });
    }

    /// Whether `promote_top_effects` would leave `outcome` unchanged: every
    /// read and rebind is already held for `effect_owner`, every read that is
    /// not yet initialized is already an uninitialized read, and, when the
    /// promotion is observed, none of them carries an assumed-path tag to
    /// clear. Escaped and deferred callback promotion repeats the same owners
    /// at every opaque call, so most promotions are such repeats.
    fn promotes_nothing(
        &self,
        promoted_reads: &[&Id],
        promoted_rebinds: &[&Id],
        effect_owner: SourceUnitOrdinal,
        outcome: &Outcome,
    ) -> bool {
        let effects = &outcome.effects;
        let assumed = &effects.fallback_only;
        let observed = self.state.assumption.is_none();
        promoted_reads.iter().all(|&id| {
            effects.reads.contains(&(id.clone(), effect_owner))
                && (self.state.world.module.initialized_bindings.contains(id)
                    || effects.uninitialized_reads.contains(id))
                && !(observed
                    && (assumed.reads.contains_key(id)
                        || assumed.uninitialized_reads.contains_key(id)))
        }) && promoted_rebinds.iter().all(|&id| {
            effects.rebinds.contains(&(id.clone(), effect_owner))
                && !(observed && assumed.rebinds.contains_key(id))
        })
    }

    fn roots_for_owner(&self, owner: SourceUnitOrdinal) -> BTreeSet<SourceUnitOrdinal> {
        let mut roots = BTreeSet::from([owner]);
        if let Some(fact) = self.inputs.facts.get(owner.0) {
            roots.extend(
                self.inputs
                    .source_unit_read_closure
                    .source_units_for_bindings(
                        fact.reads.eager.iter().chain(fact.rebinds.eager.iter()),
                    ),
            );
        }
        roots
    }

    pub(super) fn fact_roots(
        &mut self,
        fact: &StatementFacts,
        metadata_target: Option<SourceUnitOrdinal>,
        private_constructor_result_owners: &BTreeSet<SourceUnitOrdinal>,
        closed_callback_factory_result: Option<&PrivateCallbackFactoryResult>,
    ) -> BTreeSet<SourceUnitOrdinal> {
        let mut roots = BTreeSet::new();
        // Storing a known callable in an author-certified inert global data
        // slot does not make that callable executable during this prefix.
        // Its ordinary eager read and all graph ordering edges remain intact.
        if self
            .state
            .world
            .publication
            .accepted_deferred_global_writes
            .contains(&fact.ordinal)
        {
            return roots;
        }
        // Export lists link bindings; evaluating one does not expose a value
        // whose declaration has not run yet.
        if fact.export_linkage.is_some() {
            return roots;
        }
        if let Some(proof) = closed_callback_factory_result {
            self.consume_proof_dependencies(&proof.dependencies);
        }
        // The full metadata timing proof covers this whole statement. Its
        // local mutation still contributes graph edges, but the call does not
        // publish an executable capability through the target or its result.
        // Rooting the statement would traverse its eager target read in the
        // synchronous owner closure and undo the target filter below.
        if metadata_target.is_none()
            && (self.inputs.exported_owners.contains(&fact.ordinal)
                || !(closed_callback_factory_result.is_some()
                    || (self.queries.prepared_statement_is_pure(fact.ordinal)
                        && (fact.declares_direct_function
                            || matches!(fact.kind, StatementKind::ClassDecl)
                            || self.closed_local_record_factory_result(fact)))
                    || !private_constructor_result_owners.is_empty()))
        {
            roots.insert(fact.ordinal);
        }
        // Reading an initialized ordinary class as heritage does not invoke
        // its constructor or methods. This proof covers only that operation;
        // eager class work and publication retain their ordinary roots.
        let (inert_heritage, heritage_dependencies) =
            match self.noninvoking_class_heritage_read(fact) {
                Some(proof) => (Some(proof.binding.clone()), Some(proof.dependencies)),
                None => (None, None),
            };
        if let Some(dependencies) = &heritage_dependencies {
            self.consume_proof_dependencies(dependencies);
        }
        roots.extend(
            self.inputs
                .source_unit_read_closure
                .source_units_for_bindings(
                    fact.reads
                        .eager
                        .iter()
                        .filter(|id| Some(*id) != inert_heritage.as_ref())
                        .chain(fact.rebinds.eager.iter()),
                )
                .into_iter()
                .filter(|owner| {
                    Some(*owner) != metadata_target
                        && !private_constructor_result_owners.contains(owner)
                }),
        );
        roots
    }

    /// A validated factory retains the inline callback only in its fresh
    /// returned graph. Creating that graph does not publish the callback;
    /// later calls, writes, and exports still flow through the value model.
    pub(super) fn private_callback_factory_result(
        &self,
        fact: &StatementFacts,
    ) -> Option<PrivateCallbackFactoryResult> {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || self.inputs.exported_owners.contains(&fact.ordinal)
            || self.inputs.index.opaque_top.contains(&fact.ordinal)
        {
            return None;
        }
        let [(binding, ValueExpr::Call(callee, _, site))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        let [call] = self.inputs.index.top_calls[fact.ordinal.0].as_slice() else {
            return None;
        };
        let source = self
            .inputs
            .index
            .initialization_source_identities
            .get(&fact.ordinal)?;
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return None;
        };
        let summary = self.inputs.index.callback_factory_summaries.get(factory)?;
        if source.owner() != fact.ordinal
            || source.binding() != binding
            || !fact.declared.contains(binding)
            || !self.inputs.index.top_definite_initializers[fact.ordinal.0].contains(binding)
            || !summary.supports_callback_factory()
            || call.kind != CallKind::Call
            || call.has_spread
            || CallSite::from(call.span) != *site
            || source.initializer_span().lo > call.span.lo
            || source.initializer_span().hi < call.span.hi
            || self.inputs.index.direct_inline_callback_calls.get(site) != Some(factory)
            // The retention summary permits provider-private effects and is
            // not an evaluation-purity proof. Keep this classifier obligation
            // until an independent exact evaluation proof can replace it.
            || !self.queries.prepared_statement_is_pure(fact.ordinal)
        {
            return None;
        }
        Some(PrivateCallbackFactoryResult {
            proof_use: CallbackFactoryUseFact {
                imported_local: factory.0.to_string(),
                evidence: summary
                    .callback_factory_evidence()
                    .iter()
                    .cloned()
                    .collect(),
                kind: CallbackFactoryUseKind::ClosedResultRoot,
                lexical_statement_ordinal: fact.ordinal,
                source_location: None,
                span: call.span,
            },
            dependencies: self.settled_prefix_dependencies(),
        })
    }

    /// A return-only local function can put its arguments into a fresh
    /// ordinary record without publishing or calling them. The initializer
    /// still runs normally; only its result owner's speculative root is
    /// omitted until a later read, call, write, or export exposes that value.
    fn closed_local_record_factory_result(&self, fact: &StatementFacts) -> bool {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || !self.queries.prepared_statement_is_pure(fact.ordinal)
            || fact.declared.len() != 1
            || self.state.world.reachability.incomplete
            || !self.inputs.index.dynamic_scope_owners.is_empty()
            || self.inputs.index.opaque_top.contains(&fact.ordinal)
            || !self.inputs.index.top_property_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_member_value_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_callback_result_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_exported_values[fact.ordinal.0].is_empty()
        {
            return false;
        }
        let [(binding, ValueExpr::Call(callee, args, site))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return false;
        };
        let [call] = self.inputs.index.top_calls[fact.ordinal.0].as_slice() else {
            return false;
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return false;
        };
        let Some(&key) = self.inputs.index.hoisted_bindings.get(factory) else {
            return false;
        };
        if !fact.declared.contains(binding)
            || !self.inputs.index.top_definite_initializers[fact.ordinal.0].contains(binding)
            || self
                .inputs
                .flow
                .values_of_binding(factory)
                .iter()
                .ne([Value::Alloc(Kind::Function, key)])
            || call.kind != CallKind::Call
            || call.has_spread
            || CallSite::from(call.span) != *site
            || !args.iter().all(|arg| {
                matches!(
                    arg,
                    ValueExpr::Alloc(Kind::Function, _) | ValueExpr::NonCallable(_)
                )
            })
        {
            return false;
        }
        let Some(callable) = self.inputs.index.callables.get(&key) else {
            return false;
        };
        if self.fresh_local_record_receiver(key, callable).is_some() {
            return true;
        }
        if callable.opaque
            || callable.generator
            || self.inputs.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.calls.is_empty()
            || !callable.rebinds.is_empty()
            || !callable.member_writes.is_empty()
            || !callable.member_value_writes.is_empty()
            || !callable.callback_result_writes.is_empty()
            || callable.dynamic_property_effect
            || !callable.escaped_bindings.is_empty()
            || !callable.namespace_escape_values.is_empty()
            || callable
                .params
                .iter()
                .any(|param| param.binding.is_none() || param.default.is_some())
            || callable.local_assignments.iter().any(|(id, value)| {
                !callable.local_functions.contains_key(id)
                    || !matches!(value, ValueExpr::Alloc(Kind::Function, _))
            })
            || callable.body_facts.value_flow().returns().len() != 1
            || !matches!(
                callable.body_facts.value_flow().returns()[0],
                ValueExpr::ObjectLiteral(..)
            )
        {
            return false;
        }
        let allowed: BTreeSet<_> = callable
            .params
            .iter()
            .filter_map(|param| param.binding.clone())
            .chain(callable.local_functions.keys().cloned())
            .collect();
        callable.reads.is_subset(&allowed)
            && self
                .closed_local_record_value(&callable.body_facts.value_flow().returns()[0], &allowed)
    }

    /// Check that the shared ordered record certificate is fully represented
    /// by the execution index before treating its writes as private own-data
    /// writes. Incomplete index coverage retains ordinary promotion.
    fn fresh_local_record_receiver(&self, key: u32, callable: &Callable) -> Option<u32> {
        let proof = self.inputs.graph.fresh_record_proof(key)?;
        let allowed: BTreeSet<_> = callable
            .params
            .iter()
            .filter_map(|param| param.binding.clone())
            .chain(std::iter::once(proof.local.clone()))
            .collect();
        if callable.opaque
            || callable.generator
            || self.inputs.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.calls.is_empty()
            || !callable.rebinds.is_empty()
            || callable.dynamic_property_effect
            || !callable.reads.is_subset(&allowed)
            || callable.local_assignments.len() != 1
            || !matches!(&callable.local_assignments[0], (id, ValueExpr::ObjectLiteral(object, _))
                if id == &proof.local && object == &proof.object)
            || callable.body_facts.value_flow().returns().len() != 1
            || !matches!(&callable.body_facts.value_flow().returns()[0], ValueExpr::Binding(id) if id == &proof.local)
            || callable.escaped_bindings != BTreeSet::from([proof.local.clone()])
            || !self.inputs.index.plain_data_objects.contains(&proof.object)
            || callable.callback_result_writes.len() != proof.write_spans.len()
            || callable.member_value_writes.len() != proof.write_spans.len()
            || callable.member_writes.len() != proof.write_spans.len()
            || callable.callback_result_writes.iter().any(|write| {
                !write.simple_assignment
                    || !write.data_read_operands
                    || !matches!(&write.receiver, ValueExpr::Binding(id) if id == &proof.local)
                    || write.property.is_none()
            })
            || callable
                .member_value_writes
                .iter()
                .any(|write| write.receiver != proof.local || write.property.is_none())
            || callable
                .member_writes
                .iter()
                .any(|(id, _)| id != &proof.local)
        {
            return None;
        }
        Some(proof.object)
    }

    fn closed_local_record_value(&self, value: &ValueExpr, allowed: &BTreeSet<Id>) -> bool {
        match value {
            ValueExpr::ObjectLiteral(key, properties) => {
                self.inputs.index.plain_data_objects.contains(key)
                    && properties.iter().all(|(name, value)| {
                        name.is_some() && self.closed_local_record_value(value, allowed)
                    })
            }
            ValueExpr::Binding(id) => allowed.contains(id),
            ValueExpr::Alloc(Kind::Function, _)
            | ValueExpr::NonCallable(_)
            | ValueExpr::Undefined => true,
            _ => false,
        }
    }

    /// An author assertion is attached to the canonical top-level factory,
    /// and only supplies premises at an exact, syntactically fresh call site.
    /// `Err` names the refused argument shape of a direct annotated call.
    pub(super) fn annotated_record_factory(
        &mut self,
        fact: &StatementFacts,
    ) -> Option<Result<(Id, Span), LocalRecordFactoryRejection>> {
        let [(_, ValueExpr::Call(callee, _, site))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return None;
        };
        if !self.inputs.top_bindings.contains(factory)
            || !self
                .inputs
                .index
                .retained_record_factories
                .contains(factory.0.as_ref())
        {
            return None;
        }
        let argument = self.inputs.index.record_factory_calls.get(site)?;
        let call = self.inputs.index.top_calls[fact.ordinal.0]
            .iter()
            .find(|call| CallSite::from(call.span) == *site)?;
        let rejection = |reason, property| LocalRecordFactoryRejection {
            factory_binding: factory.0.to_string(),
            reason,
            property,
            construction: None,
            first_callback_publication_owner: None,
            first_class_members_publication: None,
            lexical_statement_ordinal: fact.ordinal,
            source_location: None,
            span: call.span,
        };
        let record = match argument {
            Ok(record) => record,
            Err((reason, property)) => return Some(Err(rejection(*reason, property.clone()))),
        };
        // `ToString` of a proven primitive runs no user code (none of the
        // admitted primitives is a Symbol, whose `ToString` throws).
        if let Some((property, _)) = record
            .template_substitutions
            .iter()
            .find(|(_, value)| !self.closed_primitive_input(value))
        {
            return Some(Err(rejection(
                RecordFactoryRejectionReason::TemplateSubstitution,
                Some(property.clone()),
            )));
        }
        Some(Ok((factory.clone(), call.span)))
    }

    /// The first record property the checked constructor proof refused, for
    /// an annotated call whose argument passed the syntax check.
    pub(super) fn record_factory_input_rejection(
        &mut self,
        fact: &StatementFacts,
    ) -> Option<(RecordFactoryRejectionReason, Option<String>)> {
        let [(_, ValueExpr::Call(_, args, _))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        let [ValueExpr::ObjectLiteral(_, properties)] = args.as_ref() else {
            return None;
        };
        properties.iter().find_map(|(property, value)| {
            self.unclosed_constructor_input(value)
                .map(|reason| (reason, property.clone()))
        })
    }

    /// The failed check of the factory's returned top-level allocation, with
    /// its class identity when the source resolves one.
    pub(super) fn returned_class_construction_rejection(
        &mut self,
        factory: &Id,
    ) -> Option<(Option<u32>, ConstructionRejection)> {
        let key = self.inputs.index.hoisted_bindings.get(factory)?;
        let [ValueExpr::New(class, args, _)] = self
            .inputs
            .index
            .callables
            .get(key)?
            .body_facts
            .value_flow()
            .returns()
        else {
            return None;
        };
        let ValueExpr::Binding(class) = class.as_ref() else {
            return None;
        };
        let Some(class) = self.stable_local_class_key(class) else {
            return Some((None, ConstructionRejection::UncheckedConstructor));
        };
        Some((
            Some(class),
            self.eligible_local_constructor(class, args.len()).err()?,
        ))
    }

    fn unclosed_constructor_input(
        &mut self,
        value: &ValueExpr,
    ) -> Option<RecordFactoryRejectionReason> {
        if self.closed_constructor_input(value) {
            return None;
        }
        Some(match value {
            ValueExpr::Binding(_) => RecordFactoryRejectionReason::UnprovenBinding,
            ValueExpr::Member(..) => RecordFactoryRejectionReason::UnprovenMemberRead,
            ValueExpr::ObjectLiteral(_, properties) => properties
                .iter()
                .find_map(|(_, value)| self.unclosed_constructor_input(value))
                .unwrap_or(RecordFactoryRejectionReason::UnsupportedValue),
            _ => RecordFactoryRejectionReason::UnsupportedValue,
        })
    }

    /// A checked constructor can retain callbacks in a fresh instance without
    /// exposing them during the initializer. The ordinary executor still runs
    /// the wrapper and constructor. Exported results require a closure that
    /// rules out synchronous importer reentry during provider initialization.
    pub(super) fn closed_local_constructor_factory_result(
        &mut self,
        fact: &StatementFacts,
    ) -> Result<CheckedLocalConstructorFactory, RecordFactoryRejectionReason> {
        if self.state.world.reachability.incomplete {
            return Err(RecordFactoryRejectionReason::IncompleteExecution);
        }
        if self.inputs.exported_owners.contains(&fact.ordinal) {
            return Err(RecordFactoryRejectionReason::ExportedResult);
        }
        if !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || self.inputs.index.opaque_top.contains(&fact.ordinal)
            || !self.inputs.index.top_property_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_member_value_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_callback_result_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_exported_values[fact.ordinal.0].is_empty()
        {
            return Err(RecordFactoryRejectionReason::StatementShape);
        }
        let [(binding, ValueExpr::Call(callee, args, site))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return Err(RecordFactoryRejectionReason::StatementShape);
        };
        // Proven enum own-field reads, and template `ToString` of proven
        // primitives, are retained in the normal top-level executor and eager
        // facts, but cannot invoke a getter or callback.
        let calls: Vec<_> = self.inputs.index.top_calls[fact.ordinal.0]
            .iter()
            .filter(|call| match call.kind {
                CallKind::LocalMemberRead => !self.closed_enum_member_read(&call.callee),
                CallKind::ImplicitCoercion => !matches!(call.args.as_slice(),
                    [argument] if self.closed_primitive_input(argument)),
                _ => true,
            })
            .collect();
        if !args.iter().all(|arg| self.closed_constructor_input(arg)) {
            return Err(RecordFactoryRejectionReason::UnsupportedValue);
        }
        let [call] = calls.as_slice() else {
            return Err(RecordFactoryRejectionReason::ArgumentEffects);
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return Err(RecordFactoryRejectionReason::FactoryCalleeShape);
        };
        let &key = self
            .inputs
            .index
            .hoisted_bindings
            .get(factory)
            .ok_or(RecordFactoryRejectionReason::FactoryNotHoisted)?;
        if !fact.declared.contains(binding)
            || !self.inputs.index.top_definite_initializers[fact.ordinal.0].contains(binding)
        {
            return Err(RecordFactoryRejectionReason::InitializerBinding);
        }
        if self.inputs.index.exported_bindings.contains(factory) {
            return Err(RecordFactoryRejectionReason::FactoryExported);
        }
        if self
            .state
            .world
            .publication
            .global_escaped_callbacks
            .contains(&AbstractValue::Value(Value::Alloc(Kind::Function, key)))
        {
            return Err(RecordFactoryRejectionReason::FactoryEscaped);
        }
        if self
            .inputs
            .flow
            .values_of_binding(factory)
            .iter()
            .ne([Value::Alloc(Kind::Function, key)])
        {
            return Err(RecordFactoryRejectionReason::FactoryFlowIdentity);
        }
        if !self
            .state
            .world
            .module
            .prefix_values
            .get(factory)
            .is_some_and(|values| {
                values.len() == 1
                    && values.contains(&AbstractValue::Value(Value::Alloc(Kind::Function, key)))
            })
        {
            return Err(RecordFactoryRejectionReason::FactoryPrefixIdentity);
        }
        if call.kind != CallKind::Call || call.has_spread || CallSite::from(call.span) != *site {
            return Err(RecordFactoryRejectionReason::ArgumentEffects);
        }
        let callable = self
            .inputs
            .index
            .callables
            .get(&key)
            .ok_or(RecordFactoryRejectionReason::FactoryBody)?;
        if callable.opaque
            || callable.generator
            || self.inputs.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.rebinds.is_empty()
            || !callable.local_assignments.is_empty()
            || !callable.member_writes.is_empty()
            || !callable.member_value_writes.is_empty()
            || !callable.callback_result_writes.is_empty()
            || callable.dynamic_property_effect
            || !callable.escaped_bindings.is_empty()
            || !callable.namespace_escape_values.is_empty()
            || callable.body_facts.value_flow().returns().len() != 1
            || callable.params.len() != args.len()
            || callable
                .params
                .iter()
                .any(|param| param.binding.is_none() || param.default.is_some())
        {
            return Err(RecordFactoryRejectionReason::FactoryBody);
        }
        let params: BTreeMap<_, _> = callable
            .params
            .iter()
            .zip(args.iter())
            .map(|(param, arg)| (param.binding.clone().unwrap(), arg))
            .collect();
        let mut class_reads = BTreeSet::new();
        let mut allocations = BTreeMap::new();
        let checked_return = self.checked_constructor_return(
            &callable.body_facts.value_flow().returns()[0],
            &params,
            false,
            &mut class_reads,
            &mut allocations,
        );
        if !checked_return {
            return Err(RecordFactoryRejectionReason::ConstructorProof);
        }
        let allowed_reads: BTreeSet<_> = params.keys().cloned().chain(class_reads).collect();
        if !callable.reads.is_subset(&allowed_reads) {
            return Err(RecordFactoryRejectionReason::FactoryBody);
        }
        let mut indexed_allocations = BTreeMap::new();
        for nested in &callable.calls {
            let ValueExpr::Binding(class) = &nested.callee else {
                return Err(RecordFactoryRejectionReason::FactoryBody);
            };
            let class_key = self
                .stable_local_class_key(class)
                .ok_or(RecordFactoryRejectionReason::ConstructorProof)?;
            if nested.kind != CallKind::New || nested.has_spread {
                return Err(RecordFactoryRejectionReason::FactoryBody);
            }
            *indexed_allocations
                .entry((class_key, nested.args.len()))
                .or_insert(0) += 1;
        }
        if allocations != indexed_allocations {
            return Err(RecordFactoryRejectionReason::FactoryBody);
        }
        let mut owners = BTreeSet::from([callable.owner]);
        for (class_key, _) in allocations.keys() {
            owners.insert(
                *self
                    .inputs
                    .index
                    .class_owners
                    .get(class_key)
                    .ok_or(RecordFactoryRejectionReason::ConstructorProof)?,
            );
        }
        Ok(CheckedLocalConstructorFactory {
            call_span: call.span,
            checked_owners: owners,
        })
    }

    fn closed_constructor_input(&mut self, value: &ValueExpr) -> bool {
        match value {
            ValueExpr::ObjectLiteral(key, properties) => {
                if let Some(elements) = self.inputs.index.array_elements.get(key) {
                    return self.inputs.index.dense_array_literals.contains_key(key)
                        && elements.iter().all(|element| {
                            element.as_ref().is_some_and(|element| {
                                element.spread.is_none()
                                    && self.closed_constructor_input(&element.value)
                            })
                        });
                }
                self.inputs.index.plain_data_objects.contains(key)
                    && properties
                        .iter()
                        .all(|(name, value)| name.is_some() && self.closed_constructor_input(value))
            }
            ValueExpr::Member(..) => self.closed_enum_member_read(value),
            ValueExpr::Binding(binding) => {
                self.closed_primitive_binding(binding)
                    || self.closed_function_binding(binding)
                    || self.closed_primitive_data_binding(binding)
            }
            ValueExpr::Primitive(operands) => operands
                .iter()
                .all(|operand| matches!(operand, ValueExpr::NonCallable(_) | ValueExpr::Undefined)),
            ValueExpr::Void(operand) | ValueExpr::LogicalNot(operand) => {
                matches!(
                    operand.as_ref(),
                    ValueExpr::NonCallable(_) | ValueExpr::Undefined
                )
            }
            ValueExpr::StrictEquality(left, right) => [left.as_ref(), right.as_ref()]
                .into_iter()
                .all(|operand| matches!(operand, ValueExpr::NonCallable(_) | ValueExpr::Undefined)),
            ValueExpr::Alloc(Kind::Function, _)
            | ValueExpr::NonCallable(_)
            | ValueExpr::Undefined => true,
            _ => false,
        }
    }

    /// A proven primitive: an initialized literal `const` or a closed enum's
    /// own primitive member.
    fn closed_primitive_input(&mut self, value: &ValueExpr) -> bool {
        match value {
            ValueExpr::Binding(binding) => self.closed_primitive_binding(binding),
            ValueExpr::Member(..) => self.closed_enum_member_read(value),
            _ => false,
        }
    }

    fn closed_primitive_binding(&self, binding: &Id) -> bool {
        self.inputs
            .index
            .direct_primitive_const_bindings
            .contains(binding)
            && self
                .state
                .world
                .module
                .initialized_bindings
                .contains(binding)
            && self
                .state
                .world
                .module
                .prefix_values
                .get(binding)
                .is_some_and(|values| {
                    !values.is_empty() && values.iter().all(AbstractValue::is_primitive)
                })
    }

    /// An initialized top-level binding whose every whole-chunk value is one
    /// source function. Reading it runs no code, and the record retains the
    /// function exactly as it retains an inline callback: storing is not a call.
    fn closed_function_binding(&self, binding: &Id) -> bool {
        if !self.inputs.top_bindings.contains(binding)
            || !self.inputs.index.dynamic_scope_owners.is_empty()
            || !self
                .state
                .world
                .module
                .initialized_bindings
                .contains(binding)
        {
            return false;
        }
        let [Value::Alloc(Kind::Function, key)] = self
            .inputs
            .flow
            .values_of_binding(binding)
            .iter()
            .collect::<Vec<_>>()[..]
        else {
            return false;
        };
        self.state
            .world
            .module
            .prefix_values
            .get(binding)
            .is_some_and(|values| {
                values.len() == 1
                    && values.iter().all(|value| {
                        matches!(value, AbstractValue::Value(Value::Alloc(Kind::Function, function))
                        | AbstractValue::Closure(HeapAddress { callable: function, .. }, _) if *function == key)
                    })
            })
    }

    /// An initialized top-level binding to one untainted literal array or
    /// plain-data object whose every current slot holds a primitive. The
    /// shared identity runs no code when stored, holds no callback, and later
    /// writes through any alias are tracked on the same allocation.
    fn closed_primitive_data_binding(&self, binding: &Id) -> bool {
        if !self.inputs.top_bindings.contains(binding)
            || !self.inputs.index.dynamic_scope_owners.is_empty()
            || !self
                .state
                .world
                .module
                .initialized_bindings
                .contains(binding)
        {
            return false;
        }
        let [Value::Alloc(Kind::Object, object)] = self
            .inputs
            .flow
            .values_of_binding(binding)
            .iter()
            .collect::<Vec<_>>()[..]
        else {
            return false;
        };
        let literal = if self.inputs.index.array_elements.contains_key(&object) {
            self.inputs.index.dense_array_literals.contains_key(&object)
        } else {
            self.inputs.index.plain_data_objects.contains(&object)
        };
        literal
            && self.state.world.object_properties_are_settled(object)
            && self
                .state
                .world
                .module
                .prefix_values
                .get(binding)
                .is_some_and(|values| {
                    values.len() == 1
                        && values
                            .contains(&AbstractValue::Value(Value::Alloc(Kind::Object, object)))
                })
            && self
                .state
                .world
                .surface
                .prefix_object_properties
                .get(&object)
                .is_some_and(|properties| {
                    properties
                        .values()
                        .flatten()
                        .all(AbstractValue::is_primitive)
                })
            && self
                .state
                .world
                .arrays
                .dense
                .get(&object)
                .is_none_or(|dense| {
                    dense
                        .slots
                        .iter()
                        .flatten()
                        .all(AbstractValue::is_primitive)
                })
    }

    fn stable_local_class_key(&self, binding: &Id) -> Option<u32> {
        let values = self.state.world.module.prefix_values.get(binding)?;
        let [AbstractValue::Value(Value::Alloc(Kind::Class, key))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        (self
            .state
            .world
            .module
            .initialized_bindings
            .contains(binding)
            && self.inputs.index.class_bindings.get(key) == Some(binding))
        .then_some(*key)
    }

    fn checked_constructor_return(
        &mut self,
        value: &ValueExpr,
        params: &BTreeMap<Id, &ValueExpr>,
        nested: bool,
        class_reads: &mut BTreeSet<Id>,
        allocations: &mut BTreeMap<(u32, usize), usize>,
    ) -> bool {
        let ValueExpr::New(callee, args, _) = value else {
            return false;
        };
        let ValueExpr::Binding(class) = callee.as_ref() else {
            return false;
        };
        let Some(key) = self.stable_local_class_key(class) else {
            return false;
        };
        let Ok(constructor) = self.eligible_local_constructor(key, args.len()) else {
            return false;
        };
        if nested && !constructor.inert_allocation {
            return false;
        }
        let property_reads = constructor.property_reads.clone();
        class_reads.insert(class.clone());
        *allocations.entry((key, args.len())).or_insert(0) += 1;
        for arg in args.iter() {
            match arg {
                ValueExpr::New(..) => {
                    if !self.checked_constructor_return(arg, params, true, class_reads, allocations)
                    {
                        return false;
                    }
                }
                ValueExpr::Binding(binding) => {
                    if !params
                        .get(binding)
                        .is_some_and(|value| self.closed_constructor_input(value))
                    {
                        return false;
                    }
                }
                other if self.closed_constructor_input(other) => {}
                _ => return false,
            }
        }
        for (index, property) in &property_reads {
            let Some(arg) = args.get(*index) else {
                return false;
            };
            let source = match arg {
                ValueExpr::Binding(binding) => {
                    let Some(value) = params.get(binding) else {
                        return false;
                    };
                    *value
                }
                other => other,
            };
            let ValueExpr::ObjectLiteral(object, properties) = source else {
                return false;
            };
            if !self.inputs.index.plain_data_objects.contains(object)
                || (!properties
                    .iter()
                    .any(|(name, _)| name.as_deref() == Some(property))
                    && !self.input_field_prototype_absent(*object, property))
            {
                return false;
            }
        }
        true
    }

    fn noninvoking_class_heritage_read<'a>(
        &'a self,
        fact: &StatementFacts,
    ) -> Option<NonInvokingClassHeritageRead<'a>> {
        if !matches!(fact.kind, StatementKind::ClassDecl)
            || self.inputs.exported_owners.contains(&fact.ordinal)
            || self.state.world.reachability.incomplete
        {
            return None;
        }
        let (class_key, _) = self
            .inputs
            .index
            .class_bindings
            .iter()
            .find(|(key, binding)| {
                self.inputs.index.class_owners.get(key) == Some(&fact.ordinal)
                    && fact.declared.contains(binding)
            })?;
        let base = self
            .inputs
            .index
            .class_values
            .get(class_key)?
            .lexical_heritage
            .as_ref()?
            .binding();
        let values = self.state.world.module.prefix_values.get(base)?;
        let Some(AbstractValue::Value(Value::Alloc(Kind::Class, base_key))) = values.first() else {
            return None;
        };
        let base_owner = *self.inputs.index.class_owners.get(base_key)?;
        if !self.state.world.module.initialized_bindings.contains(base)
            || !fact.reads.eager.contains(base)
            || self.inputs.index.class_bindings.get(base_key) != Some(base)
            || base_owner.0 >= fact.ordinal.0
            || self.state.world.classes.tainted.contains(base_key)
            || self
                .state
                .world
                .classes
                .escaped_capabilities
                .contains(base_key)
            || values.len() != 1
        {
            return None;
        }
        // ClassDefinitionEvaluation checks IsConstructor and reads the own
        // non-writable, non-configurable `prototype` data property of this source
        // class. Neither operation executes its bodies. Effects from creating
        // either class are separate operations, not prerequisites of this read.
        Some(NonInvokingClassHeritageRead {
            binding: base,
            dependencies: self.state.world.prerequisites.clone(),
        })
    }

    pub(super) fn extend_reachable_owners(
        &mut self,
        roots: impl IntoIterator<Item = SourceUnitOrdinal>,
    ) -> bool {
        let before = self.state.world.reachability.owners.len();
        // A reachable callback can read only its synchronous prefix while the
        // chunk is initializing. In particular, a binding used after a
        // definite await cannot make the body of its future value executable
        // during an opaque call at this prefix.
        let world = self.state.world_mut();
        let reached = self.inputs.extend_available_source_units(
            roots,
            &world.module.initialized_bindings,
            world.reachability.incomplete,
            &mut world.reachability.expanded,
            &mut world.reachability.deferred,
        );
        self.state
            .world_mut()
            .reachability
            .owners
            .extend(reached.iter().copied());
        let changed = self.state.world.reachability.owners.len() != before;
        if changed {
            self.state.world_mut().revision.bump_reachability();
        }
        if reached
            .iter()
            .any(|owner| self.inputs.index.dynamic_scope_owners.contains(owner))
        {
            self.mark_reachability_incomplete();
        }
        changed
    }

    fn mark_reachability_incomplete(&mut self) {
        self.widen_native_effects();
        if !self.state.world.reachability.incomplete {
            self.state.world_mut().reachability.incomplete = true;
            self.state.world_mut().revision.bump_reachability();
        }
        // Incomplete reachability always falls back to the whole chunk.
        self.state.prior_reachability_roots = None;
    }

    fn note_unknown_execution(&mut self, owner: SourceUnitOrdinal) {
        self.widen_native_effects();
        // Exports expose live capabilities only to code that can reenter the
        // module. Builtin iteration without such reentry remains precise.
        if self.inputs.export_reentry_enabled {
            let world = &self.state.world;
            let exported = ordered_values(
                self.inputs
                    .reentry_default
                    .then_some(&world.publication.exported_runtime_values)
                    .into_iter()
                    .flatten()
                    .chain(self.inputs.reentry_bindings.iter().flat_map(|binding| {
                        world
                            .module
                            .prefix_values
                            .get(binding)
                            .into_iter()
                            .flatten()
                    }))
                    .cloned(),
            );
            self.expose_class_capabilities(&exported);
            self.taint_objects(&exported);
        }
        let mut changed = !self.state.world.reachability.unknown_call_seen;
        self.state.world_mut().reachability.unknown_call_seen = true;
        let roots = self
            .state
            .prior_reachability_roots
            .take()
            .into_iter()
            .flatten()
            .chain(self.roots_for_owner(owner));
        changed |= self.extend_reachable_owners(roots);
        if changed {
            self.state.world_mut().revision.bump_reachability();
        }
    }

    pub(super) fn note_unknown_call(&mut self, call: &Call) {
        self.note_unknown_execution(call.owner);
        if matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "eval") {
            self.mark_reachability_incomplete();
        }
    }

    fn record_executed_owner(&mut self, owner: SourceUnitOrdinal) {
        if self
            .state
            .world
            .classes
            .private_constructor_result_owners
            .contains(&owner)
        {
            return;
        }
        self.state.statement_executed_owners.insert(owner);
        if self.state.world.reachability.unknown_call_seen {
            self.extend_reachable_owners([owner]);
        } else if let Some(roots) = &mut self.state.prior_reachability_roots {
            roots.insert(owner);
        }
    }

    pub(super) fn promote_property_writes(
        &mut self,
        writes: &[(Option<Id>, Option<String>)],
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        if writes.is_empty() {
            return;
        }
        let unknown_call = outcome.unknown_call
            || self.state.world.reachability.unknown_call_seen
            || self.state.fallback_outcome.unknown_call;
        if self.inputs.index.setter_owners.is_empty() && !unknown_call {
            return;
        }
        let mut possible_owners = outcome.executed_owners.clone();
        possible_owners.insert(owner);
        possible_owners.extend(self.state.prior_effect_owners.iter().copied().filter(
            |candidate| {
                !self
                    .state
                    .world
                    .publication
                    .accepted_deferred_global_writes
                    .contains(candidate)
            },
        ));
        for (receiver, _) in writes {
            if let Some(receiver) = receiver
                && let Some(binding_owner) = self.inputs.binding_owner.get(receiver)
            {
                possible_owners.insert(*binding_owner);
            }
        }
        let unknown_receiver = writes.iter().any(|(receiver, _)| receiver.is_none());
        if unknown_call {
            possible_owners.extend(
                self.state
                    .world
                    .reachability
                    .owners
                    .iter()
                    .copied()
                    .filter(|candidate| self.may_be_available_accessor_owner(*candidate)),
            );
            if self.state.world.reachability.incomplete {
                possible_owners.extend(self.all_owners());
            }
        } else if unknown_receiver {
            possible_owners.extend(
                self.all_owners()
                    .filter(|candidate| self.may_be_available_accessor_owner(*candidate)),
            );
        }
        // Setters declared for a written property on a possible receiver stay
        // observed. After an unknown call any available accessor owner may
        // have been installed on a receiver: those others are assumed.
        let mut named_owners = BTreeSet::new();
        for (_, property) in writes {
            if let Some(property) = property {
                named_owners.extend(
                    self.inputs
                        .index
                        .setter_owners
                        .get(&Some(property.clone()))
                        .into_iter()
                        .flatten()
                        .copied(),
                );
                named_owners.extend(
                    self.inputs
                        .index
                        .setter_owners
                        .get(&None)
                        .into_iter()
                        .flatten()
                        .copied(),
                );
            } else {
                named_owners.extend(
                    self.inputs
                        .index
                        .setter_owners
                        .values()
                        .flat_map(|owners| owners.iter().copied()),
                );
            }
        }
        let named_owners: BTreeSet<_> = named_owners
            .intersection(&possible_owners)
            .copied()
            .collect();
        let assumed_owners: BTreeSet<_> = if unknown_call {
            possible_owners.difference(&named_owners).copied().collect()
        } else {
            BTreeSet::new()
        };
        if !named_owners.is_empty() || !assumed_owners.is_empty() {
            let effect_owner = if unknown_call {
                self.state.current_top_owner.unwrap_or(owner)
            } else {
                owner
            };
            for accessor_owner in named_owners {
                self.promote_lazy_owner(accessor_owner, effect_owner, outcome);
            }
            self.assuming(AssumedEffectReason::PossibleSetter, |this| {
                for accessor_owner in assumed_owners {
                    this.promote_lazy_owner(accessor_owner, effect_owner, outcome);
                }
            });
            outcome.unsupported(ExecutionOperation::PropertyWrite, owner, DUMMY_SP);
        }
    }

    fn may_be_available_accessor_owner(&self, candidate: SourceUnitOrdinal) -> bool {
        if self.state.world.reachability.incomplete {
            // Direct eval and `with` can manufacture references to lexical
            // values outside the statically known read closure.
            return true;
        }
        let Some(current) = self.state.current_top_owner else {
            return true;
        };
        // Reachability follows lexical reads, including declarations whose
        // initializer has not executed. Such a future let/const/class value
        // cannot yet have been installed as a setter. Function declarations
        // are hoisted, and a later var declaration can name a value assigned
        // earlier, so retain both. Directly executed owners are added by the
        // caller independently of this broad reachability filter.
        candidate.0 <= current.0
            || self
                .inputs
                .index
                .hoisted_function_owners
                .contains(&candidate)
            || !self.inputs.facts[candidate.0].var_declared.is_empty()
    }

    /// Follow only initialized, uniquely written canonical binding copies.
    /// A lost local result or any other initializer is not an external root.
    fn external_binding_root(&self, binding: &Id) -> bool {
        let mut current = binding;
        let mut seen = BTreeSet::new();
        loop {
            if !seen.insert(current) || self.inputs.index.reassigned_bindings.contains(current) {
                return false;
            }
            if self.inputs.index.imported_bindings.contains(current)
                || (!self.inputs.flow.is_declared(current)
                    && !self.inputs.shadowed.contains(current.0.as_ref()))
            {
                return true;
            }
            if !self.inputs.single_writer_bindings.contains(current)
                || !self
                    .state
                    .world
                    .module
                    .initialized_bindings
                    .contains(current)
            {
                return false;
            }
            let Some(writes) = self.inputs.index.binding_declaration_writes.get(current) else {
                return false;
            };
            let [write] = writes.as_slice() else {
                return false;
            };
            let Some(ValueExpr::Binding(source)) = &write.initializer else {
                return false;
            };
            if !write.direct_top_level || !write.simple_identifier {
                return false;
            }
            current = source;
        }
    }

    /// An unknown descriptor or prototype can make a read invoke any accessor
    /// previously installed by reachable code. Its returned value is Top, but
    /// its synchronous binding effects still occur at this read's prefix.
    pub(super) fn promote_possible_getter(
        &mut self,
        call: &Call,
        receivers: &Values,
        outcome: &mut Outcome,
    ) {
        let no_prior_skipped_execution = !self.state.world.native.uncertain_execution;
        if self.state.array_iteration_depth > 0 {
            self.state.array_iteration_unsupported = true;
        }
        self.note_unknown_call(call);
        self.taint_objects(receivers);
        // A direct unshadowed global is already an external graph. Source
        // callbacks installed into it are retained by publication/opaque-call
        // tracking. A local or derived Top may instead hide a private graph
        // lost by skipped execution and must retain the broader fallback.
        let external_root = matches!(&call.callee,
            ValueExpr::Member(receiver, _, _, _) if matches!(receiver.as_ref(),
                ValueExpr::Binding(binding)
                    if self.external_binding_root(binding)))
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && !self.state.world.reachability.incomplete
            && no_prior_skipped_execution
            && !receivers
                .iter()
                .any(|value| matches!(value, AbstractValue::Value(Value::Alloc(Kind::Object, _))));
        let mut owners: BTreeSet<_> = if external_root {
            BTreeSet::new()
        } else {
            self.state
                .world
                .reachability
                .owners
                .iter()
                .copied()
                .filter(|owner| self.may_be_available_accessor_owner(*owner))
                .collect()
        };
        if !external_root {
            owners.extend(outcome.executed_owners.iter().copied());
            owners.insert(call.owner);
        }
        if self.state.world.reachability.incomplete {
            owners.extend(self.all_owners());
        }
        let effect_owner = self.state.current_top_owner.unwrap_or(call.owner);
        if !external_root {
            owners.insert(effect_owner);
        }
        let lookup = PossibleGetterLookupSite {
            lexical_owner: call.owner,
            span_start: call.span.lo.0,
            span_end: call.span.hi.0,
        };
        for owner in owners {
            let reason = AssumedEffectReason::PossibleGetter {
                lookup: lookup.clone(),
                promotion: PossibleGetterPromotion::LazyOwner {
                    source_owner: owner,
                },
            };
            self.assuming(reason, |this| {
                this.promote_lazy_owner(owner, effect_owner, outcome)
            });
        }
        let reason = AssumedEffectReason::PossibleGetter {
            lookup,
            promotion: PossibleGetterPromotion::RawCallbacks,
        };
        self.assuming(reason, |this| {
            this.promote_raw_callbacks(receivers, outcome)
        });
        // Promoted getters may mutate another captured object or rebind a
        // captured cell before the next operation in this same statement.
        let reached: Values = outcome
            .effects
            .reads
            .iter()
            .filter_map(|(binding, _)| self.state.world.module.prefix_values.get(binding))
            .flatten()
            .cloned()
            .collect();
        self.unsettle(&reached);
        self.apply_rebind_effects(&outcome.effects.rebinds);
        self.invoke_possible_callbacks_at(
            std::slice::from_ref(receivers),
            false,
            Some(call.span),
            outcome,
        );
        outcome.unsupported(ExecutionOperation::MemberRead, call.owner, call.span);
        outcome.unknown_call = true;
    }

    fn safe_object_assign_target(&self, call: &Call, id: &Id) -> bool {
        if call.kind == CallKind::New
            || !call.plain_object_sources
            || !matches!(call.args.first(), Some(ValueExpr::Binding(target)) if target == id)
        {
            return false;
        }
        let ValueExpr::Member(object, Some(property), _, _) = &call.callee else {
            return false;
        };
        matches!(object.as_ref(), ValueExpr::Binding(object_id)
            if object_id.0.as_ref() == "Object"
                && !self.inputs.flow.is_declared(object_id)
                && !self.inputs.shadowed.contains("Object")
                && property == "assign")
    }

    fn stable_fresh_function_target(&self, key: u32, callable: &Callable, id: &Id) -> bool {
        callable.local_functions.contains_key(id)
            && !callable.rebinds.contains(id)
            && !callable.literal_captures.contains(id)
            && !callable.escaped_bindings.contains(id)
            && !callable.dynamic_property_effect
            && callable
                .member_writes
                .iter()
                .all(|(receiver, property)| receiver != id || property != "__proto__")
            && !callable.local_assignments.iter().any(|(target, value)| {
                if target == id {
                    return false;
                }
                let mut mentioned = BTreeSet::new();
                value.bindings(&mut mentioned);
                mentioned.contains(id)
            })
            && !self
                .inputs
                .index
                .callables
                .iter()
                .any(|(other, nested)| *other != key && nested.reads.contains(id))
            && !callable.calls.iter().any(|call| {
                let mut mentioned = BTreeSet::new();
                call.callee.bindings(&mut mentioned);
                for arg in &call.args {
                    arg.bindings(&mut mentioned);
                }
                mentioned.contains(id) && !self.safe_object_assign_target(call, id)
            })
    }

    /// Enumerate one value's retained references without executing any
    /// callable capability. Object properties can be skipped after the
    /// callback walk has already expanded that object's property map; applied
    /// accessor capabilities remain visible on every visit.
    fn visit_stored_references(
        &self,
        value: &AbstractValue,
        include_object_properties: bool,
        mut visit: impl FnMut(&AbstractValue),
    ) {
        self.visit_stored_references_with_edges(
            value,
            include_object_properties,
            |reference, _| {
                visit(reference);
            },
        );
    }

    /// The single inventory of retained-reference carriers. Edge labels are
    /// borrowed and inexpensive for ordinary callback, taint and exposure
    /// walks; diagnostic paths allocate labels only when explicitly collected.
    fn visit_stored_references_with_edges(
        &self,
        value: &AbstractValue,
        include_object_properties: bool,
        mut visit: impl FnMut(&AbstractValue, StoredReferenceEdge<'_>),
    ) {
        match value {
            AbstractValue::Value(Value::Alloc(kind, object)) => {
                if kind.tracks_prefix_references() {
                    // RegExp identity is retained so references stored on a
                    // literal remain reachable. This is containment only; it
                    // does not make regex properties or prototype behavior
                    // an ordinary-record fact.
                    if include_object_properties
                        && let Some(properties) = self
                            .state
                            .world
                            .surface
                            .prefix_object_properties
                            .get(object)
                    {
                        for (property, values) in properties {
                            let edge = if matches!(kind, Kind::Array) {
                                StoredReferenceEdge::ArrayProperty(property.as_deref())
                            } else {
                                StoredReferenceEdge::ObjectProperty(property.as_deref())
                            };
                            for value in values {
                                visit(value, edge);
                            }
                        }
                    }
                    if let Some(accessors) = self.state.world.surface.applied_accessors.get(object)
                    {
                        for (property, accessor) in accessors {
                            for getter in &accessor.getter {
                                visit(getter, StoredReferenceEdge::AppliedGetter(property));
                            }
                            for setter in &accessor.setter {
                                visit(setter, StoredReferenceEdge::AppliedSetter(property));
                            }
                        }
                    }
                } else if !matches!(kind, Kind::Function | Kind::Class) {
                    unreachable!("unsupported retained-reference allocation kind");
                }
            }
            AbstractValue::Closure(_, captured) => {
                if let Some(home_object) = captured.values.home_object {
                    visit(
                        &AbstractValue::Value(Value::Alloc(Kind::Object, home_object)),
                        StoredReferenceEdge::ClosureHomeObject,
                    );
                }
                for (binding, values) in &captured.values.snapshots {
                    for value in values {
                        visit(
                            value,
                            StoredReferenceEdge::ClosureSnapshot(binding.0.as_ref()),
                        );
                    }
                }
                for (binding, cell) in &captured.values.cells {
                    for value in &self.state.world.cells[cell.0].values {
                        visit(value, StoredReferenceEdge::ClosureCell(binding.0.as_ref()));
                    }
                }
            }
            AbstractValue::Instance(_, captured, _) => {
                for value in captured.values.iter() {
                    visit(value, StoredReferenceEdge::InstanceRetainedValue);
                }
            }
            AbstractValue::DeferredCallbacks(captured) => {
                for value in captured.values.iter() {
                    visit(value, StoredReferenceEdge::DeferredCallbackRetainedValue);
                }
            }
            AbstractValue::AuditedRetainedResult(_, captured) => {
                for value in captured.values.iter() {
                    visit(value, StoredReferenceEdge::AuditedResultRetainedValue);
                }
            }
            AbstractValue::OwnDescriptorMap(site) => {
                if let Some(map) = self.state.world.descriptors.get(site) {
                    for value in &map.retained_values {
                        visit(value, StoredReferenceEdge::DescriptorMapRetainedValue);
                    }
                }
            }
            // Keep the carrier inventory exhaustive: when AbstractValue gains
            // another retained-reference shape, this visitor must classify it.
            AbstractValue::Value(Value::Prim(_) | Value::Unavailable | Value::Top)
            | AbstractValue::ClassMembers(_)
            | AbstractValue::CallbackFactory
            | AbstractValue::InlineCallbackFactory
            | AbstractValue::ForeignDeferredRecord
            | AbstractValue::ObjectAssign
            | AbstractValue::ArrayIntrinsic
            | AbstractValue::GlobalObject
            | AbstractValue::SymbolicInput(_) => {}
        }
    }

    pub(super) fn callback_values<'v>(
        &self,
        values: impl IntoIterator<Item = &'v AbstractValue>,
    ) -> Values {
        let pending: Vec<_> = values.into_iter().cloned().collect();
        self.walk_callback_values(pending, None).0
    }

    fn callback_values_with_paths(
        &self,
        values: &Values,
        seeds: &BTreeMap<AbstractValue, BoundedInvocationFallbackSeed>,
        targets: &BTreeSet<u32>,
    ) -> (Values, BTreeMap<u32, BoundedInvocationFallbackInputPath>) {
        let pending = values
            .iter()
            .cloned()
            .map(|value| TracedCallbackValue {
                path: seeds.get(&value).cloned().map_or(
                    BoundedInvocationFallbackInputPath::Unknown {
                        reason: BoundedInvocationFallbackUnknownPathReason::SeedUnavailable,
                    },
                    |seed| BoundedInvocationFallbackInputPath::Retained {
                        seed,
                        edges: Vec::new(),
                    },
                ),
                value,
            })
            .collect();
        self.walk_callback_values(pending, Some(targets))
    }

    fn walk_callback_values<T: CallbackTraversalItem>(
        &self,
        mut pending: Vec<T>,
        trace_targets: Option<&BTreeSet<u32>>,
    ) -> (Values, BTreeMap<u32, BoundedInvocationFallbackInputPath>) {
        // Also the walk's visited set: probed once per reached value. A
        // transient hash set keeps those probes off the persistent `Values`
        // tree, which is built once, in order, from the result.
        let mut callbacks = FxHashSet::default();
        let mut paths = BTreeMap::new();
        let mut visited_objects = BTreeSet::new();
        let mut visited_descriptor_maps = BTreeSet::new();
        let mut expanded_escaped = false;
        while let Some(work) = pending.pop() {
            let value = work.value();
            let mut expand_stored_references = true;
            let mut expand_object_properties = true;
            match value {
                AbstractValue::Value(Value::Alloc(Kind::Function, _)) => {
                    callbacks.insert(value.clone());
                }
                AbstractValue::Closure(_, _) => {
                    // Capture snapshots form a DAG: multiple bindings can
                    // contain the same closure. Expand each closure once,
                    // rather than revisit it along every path through captures.
                    expand_stored_references = callbacks.insert(value.clone());
                }
                AbstractValue::Value(Value::Alloc(Kind::Class, key))
                | AbstractValue::ClassMembers(key) => {
                    if callbacks.insert(AbstractValue::ClassMembers(*key))
                        && trace_targets.is_some_and(|targets| targets.contains(key))
                        && let Some(path) = work.class_members_path()
                    {
                        paths.entry(*key).or_insert(path);
                    }
                }
                AbstractValue::Instance(key, captured, _) => {
                    if callbacks.insert(AbstractValue::ClassMembers(*key))
                        && trace_targets.is_some_and(|targets| targets.contains(key))
                        && let Some(path) = work.class_members_path()
                    {
                        paths.entry(*key).or_insert(path);
                    }
                    // Capability reach never needs the constructed-kind receipt.
                    // Coalesce a constructed instance and a projected member
                    // with the same class and retained arguments in the pool.
                    expand_stored_references = callbacks.insert(AbstractValue::Instance(
                        *key,
                        captured.clone(),
                        InstanceOrigin::Projected,
                    ));
                }
                AbstractValue::ForeignDeferredRecord => {
                    callbacks.insert(value.clone());
                }
                AbstractValue::DeferredCallbacks(_)
                | AbstractValue::AuditedRetainedResult(_, _) => {
                    expand_stored_references = callbacks.insert(value.clone());
                }
                AbstractValue::OwnDescriptorMap(site) => {
                    expand_stored_references = visited_descriptor_maps.insert(*site);
                }
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    expand_object_properties = visited_objects.insert(*object);
                }
                AbstractValue::GlobalObject | AbstractValue::Value(Value::Top)
                    if !expanded_escaped =>
                {
                    // Unknown external data cannot expose an unescaped local
                    // callback. Preserve tracked escapes; exports are added
                    // separately at opaque call sites, and known object
                    // identities are expanded through their property maps.
                    expanded_escaped = true;
                    for escaped in self
                        .state
                        .world
                        .publication
                        .global_escaped_callbacks
                        .iter()
                        .filter(|value| !matches!(value, AbstractValue::Value(Value::Top)))
                    {
                        pending.push(work.retained_child(
                            escaped,
                            StoredReferenceEdge::EscapedCallbackPoolExpansion,
                        ));
                    }
                }
                _ => {}
            }
            if expand_stored_references {
                self.visit_stored_references_with_edges(
                    value,
                    expand_object_properties,
                    |reference, edge| pending.push(work.retained_child(reference, edge)),
                );
            }
        }
        (callbacks.into_iter().collect(), paths)
    }

    /// A direct call or construction of an imported callable carrying a
    /// derived foreign call summary (SPEC.md § Derived foreign call summaries).
    pub(super) fn foreign_call_summary(
        &self,
        kind: CallKind,
        callee: &ValueExpr,
    ) -> Option<crate::analysis_inputs::ForeignCallSummary> {
        use crate::analysis_inputs::ForeignInvocation;
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let summary = self.inputs.index.foreign_call_imports.get(binding)?;
        matches!(
            (kind, summary.invocation),
            (CallKind::Call, ForeignInvocation::Call)
                | (CallKind::New, ForeignInvocation::Construct)
        )
        .then(|| summary.clone())
    }

    /// Retained arguments stay reachable through the fresh result: code that
    /// later holds it may invoke their callbacks or write through them.
    fn retain_foreign_arguments(
        &mut self,
        summary: &crate::analysis_inputs::ForeignCallSummary,
        args: &[Values],
    ) -> Values {
        let retained: Values = if summary.retained_provider_captures.is_empty() {
            summary
                .retained_arguments
                .iter()
                .filter_map(|position| args.get(*position))
                .flatten()
                .cloned()
                .collect()
        } else {
            // Unknown captured values may alias an actual argument. The
            // summary proves no separation between those inputs and captures.
            args.iter().flatten().cloned().collect()
        };
        self.expose_class_capabilities(&retained);
        self.taint_objects(&retained);
        retained
    }

    pub(super) fn foreign_call_result(
        &mut self,
        summary: &crate::analysis_inputs::ForeignCallSummary,
        args: &[Values],
    ) -> Values {
        use crate::analysis_inputs::{ForeignInvocation, ForeignReturn};
        let mut result = Values::new();
        for returned in &summary.returns {
            match returned {
                ForeignReturn::Argument { index } => result.extend(
                    args.get(*index)
                        .cloned()
                        .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED])),
                ),
                ForeignReturn::FreshAllocation => {
                    let mut retained = self.retain_foreign_arguments(summary, args);
                    if !summary.retained_provider_captures.is_empty() {
                        // Captures need not be primitive or unescaped. A later
                        // use retains arbitrary foreign/importer-cell effects.
                        retained.insert(AbstractValue::ForeignDeferredRecord);
                    }
                    // Keep live input identities, including containers that
                    // acquire callbacks after this call. The carrier remains
                    // opaque; its interned captures prove no result identity.
                    result.insert(self.deferred_callbacks(retained));
                }
                ForeignReturn::Primitive => {
                    result.insert(AbstractValue::PRIMITIVE);
                }
            }
        }
        // A class binding stays in its TDZ until the provider evaluates, which
        // an ESM cycle can delay past this call. A function declaration is
        // initialized at instantiation.
        if summary.invocation == ForeignInvocation::Construct {
            result.insert(AbstractValue::Value(Value::Unavailable));
        }
        result
    }

    pub(super) fn record_foreign_call_use(
        &mut self,
        summary: &crate::analysis_inputs::ForeignCallSummary,
    ) {
        use crate::analysis_provenance::{InferenceRule, InferenceUse};
        let mut derivation_rules = BTreeSet::from([InferenceRule::ForeignCallSummary]);
        if !summary.native_constructions.is_empty() {
            derivation_rules.insert(InferenceRule::NativeIntegrity);
        }
        self.record_inference_use(
            NativeIntegrityGuard::ForeignCallSummary,
            InferenceUse {
                conclusion: InferenceRule::ForeignCallSummary,
                derivation_rules,
                value_input_refs: summary.dependencies.value_inputs.clone(),
                descriptor_copy_author_refs: BTreeSet::new(),
                argument_input_refs: summary.dependencies.argument_inputs.clone(),
                native_operation_input_refs: summary.dependencies.native_operation_inputs.clone(),
                imported_primitive_array_spread_input_refs: summary
                    .dependencies
                    .imported_primitive_array_spread_inputs
                    .clone(),
            },
            &summary.dependencies,
        );
    }

    pub(super) fn retained_factory_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        self.retained_callbacks(
            &self.inputs.index.retained_callback_calls.get(&site)?.call,
            expressions,
            args,
            RetainedResultKind::Factory,
        )
    }

    fn retained_constructor_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        self.retained_callbacks(
            self.inputs.index.retained_constructor_calls.get(&site)?,
            expressions,
            args,
            RetainedResultKind::Constructor,
        )
    }

    fn no_reentry_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        let contract = self.inputs.index.no_reentry_callback_calls.get(&site)?;
        // This contract admits ordinary local callables only. An opaque result
        // or a returned callback graph does not establish that input shape.
        let signatures: Vec<_> = contract
            .signatures
            .iter()
            .filter(|signature| {
                signature.callback_arguments.iter().all(|position| {
                    let Some(values) = args.get(*position) else {
                        return false;
                    };
                    let is_ordinary_callable = |value: &AbstractValue| {
                        matches!(
                            value,
                            AbstractValue::Value(Value::Alloc(Kind::Function, _))
                                | AbstractValue::Closure(_, _)
                        )
                    };
                    if contract.require_single_callback_identity {
                        values.len() == 1 && values.iter().all(is_ordinary_callable)
                    } else {
                        values.iter().all(is_ordinary_callable)
                    }
                })
            })
            .cloned()
            .collect();
        let contract = RetainedCallbackCall {
            namespace_member: None,
            signatures,
            argument_count: expressions.len(),
            callback_argument_exprs: BTreeMap::new(),
            require_single_callback_identity: contract.require_single_callback_identity,
        };
        let callbacks =
            self.retained_callbacks(&contract, expressions, args, RetainedResultKind::Factory)?;
        // Independently exported aliases and already exposed callbacks keep
        // ordinary conservative timing; the provider audit cannot hide them.
        if !callbacks.is_disjoint(&self.export_callback_values()) {
            return None;
        }
        Some(callbacks)
    }

    /// Check a source-selected member callee's runtime callback identities
    /// before suppressing callback promotion from its synthetic accessor-read
    /// event. Only admitted identifiers/function literals and inert primitive
    /// expressions reach this path, so the abstract reads do not execute or
    /// skip JavaScript argument evaluation; the real call transfer evaluates
    /// all receiver and argument expressions in source order afterward.
    pub(super) fn no_reentry_member_lookup_admitted(
        &mut self,
        site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> bool {
        let Some(contract) = self.inputs.index.no_reentry_callback_calls.get(&site) else {
            return false;
        };
        let mut expressions = vec![ValueExpr::Unknown; contract.argument_count];
        let mut args = vec![Values::new(); contract.argument_count];
        for (position, expression) in &contract.callback_argument_exprs {
            let Some(expression_slot) = expressions.get_mut(*position) else {
                return false;
            };
            let Some(argument_slot) = args.get_mut(*position) else {
                return false;
            };
            *expression_slot = expression.clone();
            *argument_slot = self.evaluate(expression, env, captures);
        }
        self.no_reentry_callbacks(site, &expressions, &args)
            .is_some()
    }

    fn retained_callbacks(
        &self,
        contract: &RetainedCallbackCall,
        expressions: &[ValueExpr],
        args: &[Values],
        admission: RetainedResultKind,
    ) -> Option<Values> {
        if let Some((namespace, member)) = &contract.namespace_member
            && !self
                .inputs
                .index
                .audited_factory_members
                .get(namespace)
                .is_some_and(|members| members.contains(member))
        {
            return None;
        }
        for signature in &contract.signatures {
            let mut retained = Values::new();
            let admitted = signature.callback_arguments.iter().all(|position| {
                let expression = &expressions[*position];
                // The whole-chunk flow distinguishes a single stable function
                // identity from a rebound cell, including later writes.
                if let ValueExpr::Binding(binding) = expression
                    && (self.inputs.facts.iter().any(|fact| {
                        fact.rebinds.eager.contains(binding) || fact.rebinds.lazy.contains(binding)
                    }) || self.inputs.flow.values_of_binding(binding).iter().count() > 1)
                {
                    return false;
                }
                let values = &args[*position];
                if values.is_empty()
                    || !values.iter().all(|value| match value {
                        AbstractValue::Value(Value::Alloc(Kind::Function, key))
                        | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                            self.inputs.index.callables.contains_key(key)
                                && !self.state.world.native.tainted_functions.contains(key)
                        }
                        AbstractValue::AuditedRetainedResult(kind, captured) => {
                            (*kind == RetainedResultKind::Factory
                                || admission == RetainedResultKind::Constructor)
                                && self.audited_result_is_clean(captured)
                        }
                        _ => false,
                    })
                {
                    return false;
                }
                retained.extend(self.callback_values(values));
                true
            });
            if !admitted {
                continue;
            }
            let functions: BTreeSet<_> = retained
                .iter()
                .filter_map(|value| match value {
                    AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => Some(*key),
                    _ => None,
                })
                .collect();
            let mutating_receiver = |expression: &ValueExpr| {
                let mut root = expression;
                while let ValueExpr::Member(receiver, _, _, _) = root {
                    root = receiver;
                }
                matches!(root, ValueExpr::Binding(binding) if self.inputs.flow.values_of_binding(binding).iter()
                    .any(|origin| matches!(origin, Value::Alloc(Kind::Function, key) if functions.contains(&key))))
            };
            // Existing callable own/prototype surfaces are outside the fresh
            // input proof. Reject recognized writes through all local aliases,
            // even if they occur after this call or in a lazy callable.
            let mutated = self
                .inputs
                .index
                .intrinsic_writes
                .iter()
                .map(|write| &write.receiver)
                .any(mutating_receiver)
                || self
                    .inputs
                    .index
                    .top_callback_result_writes
                    .iter()
                    .flatten()
                    .chain(
                        self.inputs
                            .index
                            .callables
                            .values()
                            .flat_map(|callable| &callable.callback_result_writes),
                    )
                    .any(|write| mutating_receiver(&write.receiver));
            if mutated
                || retained.iter().any(|value| {
                    self.state
                        .world
                        .publication
                        .global_escaped_callbacks
                        .contains(value)
                })
            {
                continue;
            }
            return Some(retained);
        }
        None
    }

    fn fresh_primitive_write_receivers(
        &mut self,
        callable: &Callable,
        env: &Context,
    ) -> BTreeSet<u32> {
        let writes = &callable.callback_result_writes;
        if self.state.array_iteration_depth == 0
            || self.state.array_iteration_unsupported
            || !self.array_intrinsics_intact()
            || callable.opaque
            || writes.is_empty()
            || writes.len() != callable.member_value_writes.len()
            || writes.len()
                != callable.member_writes.len() + callable.dynamic_property_receivers.len()
            || callable
                .calls
                .iter()
                .any(|call| call.kind != CallKind::LocalMemberRead)
        {
            return BTreeSet::new();
        }
        let mut objects = BTreeSet::new();
        for write in writes {
            if !write.simple_assignment
                || !write.data_read_operands
                || !matches!(write.receiver, ValueExpr::Binding(_))
            {
                return BTreeSet::new();
            }
            for expression in [&write.property_key, &write.value] {
                let values = self.evaluate(expression, env, env);
                if values.is_empty() || !values.iter().all(AbstractValue::is_primitive) {
                    return BTreeSet::new();
                }
            }
            let receivers = self.evaluate(&write.receiver, env, env);
            let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
                receivers.iter().collect::<Vec<_>>().as_slice()
            else {
                return BTreeSet::new();
            };
            if !self.state.world.object_properties_are_settled(*object)
                || self
                    .state
                    .array_iteration_protected_objects
                    .contains(object)
                || (!self.inputs.index.plain_data_objects.contains(object)
                    && !self
                        .state
                        .world
                        .surface
                        .ordinary_spread_copies
                        .contains(object))
                || self.state.world.arrays.dense.contains_key(object)
            {
                return BTreeSet::new();
            }
            objects.insert(*object);
        }
        objects
    }

    /// Whether `callable` is a parameter-mutation initializer of a fresh
    /// object: it calls nothing, returns its one parameter, and every
    /// property write stores a primitive under a primitive key into that
    /// parameter, which holds only untainted plain-data object literals. Such
    /// a write creates or overwrites a data property of an object no accessor
    /// or callback can reach (A11 covers `%Object.prototype%`), so it
    /// publishes nothing. A primitive receiver throws before writing.
    fn fresh_data_writes(&mut self, callable: &Callable, env: &Context) -> Option<BTreeSet<u32>> {
        let writes = &callable.callback_result_writes;
        let [
            Parameter {
                binding: Some(parameter),
                default: None,
                ..
            },
        ] = callable.params.as_slice()
        else {
            return None;
        };
        if callable.opaque
            || !callable.calls.is_empty()
            || !matches!(callable.body_facts.value_flow().returns(), [ValueExpr::Binding(returned)] if returned == parameter)
            || writes.is_empty()
            || writes.len() != callable.member_value_writes.len()
            || writes.len()
                != callable.member_writes.len() + callable.dynamic_property_receivers.len()
            || writes.iter().any(
                |write| !matches!(&write.receiver, ValueExpr::Binding(receiver) if receiver == parameter),
            )
        {
            return None;
        }
        let primitive = |executor: &mut Self, expression: &ValueExpr| {
            let values = executor.evaluate(expression, env, env);
            !values.is_empty() && values.iter().all(AbstractValue::is_primitive)
        };
        let mut objects = BTreeSet::new();
        let mut fresh_receiver = |executor: &mut Self, expression: &ValueExpr| {
            let receivers = executor.evaluate(expression, env, env);
            let mut any_object = false;
            receivers.iter().all(|receiver| match receiver {
                AbstractValue::Value(Value::Prim(_)) => true,
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    any_object = true;
                    objects.insert(*object);
                    executor.inputs.index.plain_data_objects.contains(object)
                        && executor.state.world.object_properties_are_settled(*object)
                        && !executor.state.world.arrays.dense.contains_key(object)
                }
                _ => false,
            }) && any_object
        };
        for write in writes {
            if !write.simple_assignment
                || !primitive(self, &write.property_key)
                || !primitive(self, &write.value)
                || !fresh_receiver(self, &write.receiver)
            {
                return None;
            }
        }
        for receiver in &callable.dynamic_property_receivers {
            if !fresh_receiver(self, receiver) {
                return None;
            }
        }
        Some(objects)
    }

    pub(super) fn apply_member_value_writes(
        &mut self,
        writes: &[MemberValueWrite],
        env: &Context,
        captures: &Context,
        certified_data_receivers: &BTreeSet<u32>,
        deferred_global_write: bool,
    ) {
        if writes.is_empty() {
            return;
        }
        let RuntimeValue::Heap(HeapResult::Updated) =
            self.request(RuntimeOperation::Heap(HeapOperation::WriteMembers {
                writes: writes.to_vec(),
                environment: env.clone(),
                captures: captures.clone(),
                certified_data_receivers: certified_data_receivers.clone(),
                deferred_global_write,
            }))
        else {
            unreachable!("member writes produce a successor heap");
        };
    }

    pub(super) fn apply_member_value_writes_transfer(
        &mut self,
        writes: &[MemberValueWrite],
        env: &Context,
        captures: &Context,
        certified_data_receivers: &BTreeSet<u32>,
        deferred_global_write: bool,
    ) {
        for write in writes {
            let MemberValueWrite {
                receiver,
                property,
                value: expression,
                ..
            } = write;
            let receivers = self.evaluate(&ValueExpr::Binding(receiver.clone()), env, captures);
            let values = self.evaluate(expression, env, captures);
            self.publish_accessor_receivers(&receivers, write.site.source_span());
            for receiver in receivers {
                match receiver {
                    AbstractValue::GlobalObject | AbstractValue::ArrayIntrinsic => {
                        if receiver == AbstractValue::GlobalObject
                            && let Some(property) = property
                        {
                            self.evaluation_effects.record(
                                write.owner,
                                write.site.source_span(),
                                GlobalAccessKind::Write,
                                property,
                            );
                        }
                        if deferred_global_write && receiver == AbstractValue::GlobalObject {
                            continue;
                        }
                        self.state.world_mut().arrays.intrinsic_mutated |=
                            receiver == AbstractValue::ArrayIntrinsic;
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        self.insert_escaped_callback_values(
                            &callbacks,
                            ClassMembersPublicationWriter::GlobalOrIntrinsicMemberWrite,
                            Some(write.site.source_span()),
                            None,
                        );
                    }
                    AbstractValue::Value(Value::Alloc(kind, object))
                        if kind.tracks_prefix_own_properties() =>
                    {
                        let may_be_published =
                            self.state.world.surface.tainted_objects.contains(&object)
                                || self.state.world.surface.unsettled_objects.contains(&object);
                        if !certified_data_receivers.contains(&object) {
                            self.state
                                .world_mut()
                                .surface
                                .tainted_objects
                                .insert(object);
                        }
                        // RegExp slots share this heap only for callback
                        // containment. Ordinary-record shape and member-read
                        // proofs remain restricted to `Kind::Object`.
                        self.literal_heap()
                            .write_property(object, property.clone(), &values);
                        if may_be_published {
                            self.publish_values(
                                &values,
                                ClassMembersPublicationWriter::PublishedObjectMemberWrite,
                                Some(write.site.source_span()),
                            );
                        }
                    }
                    // A class, or its prototype: every instance inherits
                    // the latter, including a refused construction's
                    // unknown instance, and anything holding the class
                    // reaches the former.
                    AbstractValue::Value(Value::Alloc(Kind::Class, _))
                    | AbstractValue::ClassMembers(_) => {
                        self.publish_values(
                            &values,
                            ClassMembersPublicationWriter::ClassSurfaceMemberWrite,
                            Some(write.site.source_span()),
                        );
                    }
                    AbstractValue::Instance(key, captured, _) => {
                        self.state.world_mut().classes.tainted.insert(key);
                        self.taint_objects(captured.values.iter());
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        self.insert_escaped_callback_values(
                            &callbacks,
                            ClassMembersPublicationWriter::InstanceMemberWrite,
                            Some(write.site.source_span()),
                            None,
                        );
                    }
                    AbstractValue::OwnDescriptorMap(site) => {
                        self.invalidate_own_descriptor_map(site);
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        self.insert_escaped_callback_values(
                            &callbacks,
                            ClassMembersPublicationWriter::OwnDescriptorMapMemberWrite,
                            Some(write.site.source_span()),
                            None,
                        );
                    }
                    AbstractValue::Value(Value::Top)
                    | AbstractValue::DeferredCallbacks(_)
                    | AbstractValue::AuditedRetainedResult(_, _)
                    | AbstractValue::ForeignDeferredRecord => {
                        self.taint_objects(&values);
                        // A write through an untracked object may publish the
                        // callback through that object before it is passed to
                        // opaque code. Retain it as an escaped callback root.
                        let callbacks = self.callback_values(&values);
                        self.insert_escaped_callback_values(
                            &callbacks,
                            ClassMembersPublicationWriter::UntrackedReceiverMemberWrite,
                            Some(write.site.source_span()),
                            None,
                        );
                    }
                    _ => {}
                }
            }
        }
    }

    pub(super) fn promote_callback_result_writes(
        &mut self,
        writes: &[CallbackResultWrite],
        env: &Context,
        captures: &Context,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
        certified_data_receivers: &BTreeSet<u32>,
    ) {
        for write in writes {
            let receivers = self.evaluate(&write.receiver, env, captures);
            let uncertified: Values = receivers.iter().filter(|value| !matches!(value,
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) if certified_data_receivers.contains(object))).cloned().collect();
            self.taint_objects(&uncertified);
            let mut callbacks = self.callback_values(&receivers);
            let mut root = &write.receiver;
            while let ValueExpr::Member(receiver, _, _, _) = root {
                root = receiver;
            }
            let root_values = self.evaluate(root, env, captures);
            // A bare binding receiver is `apply_member_value_writes`' lane.
            if !matches!(write.receiver, ValueExpr::Binding(_)) {
                self.store_through_member_chain(
                    write,
                    root,
                    &receivers,
                    &root_values,
                    env,
                    captures,
                );
            }
            let callable_surface = root_values.iter().any(|value| {
                matches!(
                    value,
                    AbstractValue::Value(Value::Alloc(Kind::Function, _))
                        | AbstractValue::Closure(_, _)
                )
            });
            if !callable_surface
                && !callbacks.iter().any(|value| {
                    matches!(
                        value,
                        AbstractValue::DeferredCallbacks(_)
                            | AbstractValue::AuditedRetainedResult(_, _)
                    )
                })
            {
                continue;
            }
            callbacks.extend(self.callback_values(&root_values));
            if write
                .property
                .as_deref()
                .is_some_and(|property| property != "__proto__")
                && matches!(
                    write.value,
                    ValueExpr::NonCallable(_) | ValueExpr::Undefined
                )
            {
                continue;
            }
            // Callable own/prototype fields are not modeled as an ordinary
            // object graph. Installing an opaque value there, or in a retained
            // factory result, can add callbacks/accessors. Conservatively
            // include their effects now, before a later opaque consumer or
            // member projection can execute an otherwise untracked callback.
            outcome.unsupported(ExecutionOperation::PropertyWrite, owner, DUMMY_SP);
            outcome.unknown_call = true;
            self.note_unknown_execution(owner);
            let values = self.evaluate(&write.value, env, captures);
            self.promote_deferred_callbacks(&values, outcome);
            self.invoke_possible_callbacks_at(
                &[callbacks, values],
                false,
                Some(write.site.source_span()),
                outcome,
            );
        }
    }

    /// A value stored through a member chain (`a.b.c = f`) lands where a
    /// later read of the same chain finds it: in a tracked receiver's
    /// properties, or, under a global root or intrinsic, in the escaped
    /// pool every opaque call may invoke.
    fn store_through_member_chain(
        &mut self,
        write: &CallbackResultWrite,
        root: &ValueExpr,
        receivers: &Values,
        root_values: &Values,
        env: &Context,
        captures: &Context,
    ) {
        let values = self.evaluate(&write.value, env, captures);
        let global_root = matches!(root, ValueExpr::Binding(id)
            if !self.inputs.flow.is_declared(id)
                && !env.contains_key(id)
                && !captures.contains_key(id))
            || root_values.iter().chain(receivers.iter()).any(|value| {
                matches!(
                    value,
                    AbstractValue::GlobalObject | AbstractValue::ArrayIntrinsic
                )
            });
        let mut changed = false;
        if global_root
            || receivers
                .iter()
                .chain(root_values)
                .any(|receiver| match receiver {
                    AbstractValue::Value(Value::Alloc(kind, object))
                        if kind.tracks_prefix_own_properties() =>
                    {
                        self.state.world.surface.tainted_objects.contains(object)
                            || self.state.world.surface.unsettled_objects.contains(object)
                    }
                    AbstractValue::Value(Value::Top)
                    | AbstractValue::ForeignDeferredRecord
                    | AbstractValue::DeferredCallbacks(_)
                    | AbstractValue::AuditedRetainedResult(_, _)
                    | AbstractValue::Instance(_, _, _) => true,
                    // A class's prototype is inherited by every instance,
                    // including the unknown instance of a refused
                    // construction.
                    AbstractValue::ClassMembers(_)
                    | AbstractValue::Value(Value::Alloc(Kind::Class, _)) => true,
                    _ => false,
                })
        {
            self.publish_values(
                &values,
                ClassMembersPublicationWriter::MemberChainStore,
                Some(write.site.source_span()),
            );
        }
        for receiver in receivers {
            if let AbstractValue::Value(Value::Alloc(kind, object)) = receiver
                && kind.tracks_prefix_own_properties()
            {
                let stored = self
                    .state
                    .world_mut()
                    .surface
                    .prefix_object_properties
                    .entry(*object)
                    .or_default()
                    .entry(write.property.clone())
                    .or_default();
                let before = stored.len();
                stored.extend(values.iter().cloned());
                changed |= stored.len() != before;
            }
        }
        if changed {
            self.state.world_mut().revision.bump_prefix();
        }
    }

    pub(super) fn intern_captures(&mut self, captures: LexicalCaptures) -> CapturedContext {
        self.state.capture_contexts.intern(captures)
    }

    fn materialize_captures(&self, captures: Option<&LexicalCaptures>) -> Context {
        let Some(captures) = captures else {
            return Context::new();
        };
        let mut values = captures.snapshots.clone();
        for (binding, cell) in &captures.cells {
            values.insert(
                binding.clone(),
                self.state.world.cells[cell.0].values.clone(),
            );
        }
        values
    }

    fn havoc_cells(&mut self, cells: impl IntoIterator<Item = LexicalCellId>) {
        let mut changed = false;
        for id in cells {
            let cell = &mut self.state.world_mut().cells[id.0];
            if matches!(cell.kind, LocalBinding::Const | LocalBinding::NamedSelf(_))
                && cell.initialization == CellInitialization::Initialized
            {
                continue;
            }
            if cell.initialization == CellInitialization::Uninitialized {
                // Skipped enclosing code can have initialized a lexical cell.
                // Keeping only TDZ would wrongly prune its later readers.
                cell.initialization = CellInitialization::MaybeInitialized;
                changed = true;
            }
            changed |= cell.values.insert(AbstractValue::Value(Value::Top));
        }
        if changed {
            self.state.world_mut().revision.bump_prefix();
        }
    }

    fn intern_callback_set(&mut self, values: Values) -> Captured<Values> {
        self.state.callback_sets.intern(values)
    }

    pub(super) fn deferred_callbacks(&mut self, values: Values) -> AbstractValue {
        AbstractValue::DeferredCallbacks(self.intern_callback_set(values))
    }

    /// A later store through a possibly published receiver exposes the new
    /// graph too. Taint its objects even if no callback exists yet: a later
    /// insertion through an alias must publish that callback.
    fn publish_values(
        &mut self,
        values: &Values,
        writer: ClassMembersPublicationWriter,
        span: Option<Span>,
    ) {
        self.taint_objects(values);
        let callbacks = self.callback_values(values);
        if let Some(owner) = self.state.current_top_owner {
            for callback in &callbacks {
                let AbstractValue::Value(Value::Alloc(Kind::Function, key)) = callback else {
                    continue;
                };
                if !self
                    .state
                    .world
                    .publication
                    .global_escaped_callbacks
                    .contains(callback)
                {
                    self.state
                        .world_mut()
                        .publication
                        .first_callback_publication_owners
                        .entry(*key)
                        .or_insert(owner);
                }
            }
        }
        self.insert_escaped_callback_values(&callbacks, writer, span, None);
    }

    /// The only mutation point for the prefix-wide escaped callback set.
    /// Per-writer admission and taint decisions stay at their callers.
    fn insert_escaped_callback_values(
        &mut self,
        callbacks: &Values,
        writer: ClassMembersPublicationWriter,
        span: Option<Span>,
        fallback_diagnostic: Option<&BoundedFallbackPublicationDiagnostic>,
    ) {
        let source_span = span.filter(|span| *span != DUMMY_SP);
        for callback in callbacks {
            let AbstractValue::ClassMembers(key) = callback else {
                continue;
            };
            if self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(callback)
            {
                continue;
            }
            let top_level_owner = self.state.current_top_owner;
            let span_start = source_span.map(|span| span.lo.0);
            let span_end = source_span.map(|span| span.hi.0);
            let bounded_fallback_input_path = (writer
                == ClassMembersPublicationWriter::BoundedInvocationFallback)
            .then(|| {
                fallback_diagnostic
                    .and_then(|diagnostic| diagnostic.input_paths.get(key))
                    .cloned()
                    .unwrap_or(BoundedInvocationFallbackInputPath::Unknown {
                        reason:
                            BoundedInvocationFallbackUnknownPathReason::RetainedReferencePathUnavailable,
                    })
            });
            let bounded_fallback_callsite = (writer
                == ClassMembersPublicationWriter::BoundedInvocationFallback)
                .then(|| fallback_diagnostic.and_then(|diagnostic| diagnostic.callsite.clone()))
                .flatten();
            self.state
                .world_mut()
                .classes
                .first_members_publications
                .entry(*key)
                .or_insert(FirstClassMembersPublication {
                    writer,
                    top_level_owner,
                    span_start,
                    span_end,
                    source_location: None,
                    bounded_fallback_input_path,
                    bounded_fallback_callsite,
                });
        }
        let escaped = &self.state.world.publication.global_escaped_callbacks;
        let escaping_classes: Vec<u32> = callbacks
            .iter()
            .filter_map(|callback| match callback {
                AbstractValue::ClassMembers(key) if !escaped.contains(callback) => Some(*key),
                _ => None,
            })
            .collect();
        if callbacks.iter().any(|callback| !escaped.contains(callback)) {
            let world = self.state.world_mut();
            world
                .publication
                .global_escaped_callbacks
                .extend_absent(callbacks);
            world.revision.bump_prefix();
        }
        // An escaped class, or an escaped instance of it, reaches what its
        // construction stored on the instance or the class. Each class
        // escapes once, so this terminates.
        for key in escaping_classes {
            let reached = self.class_reach(key);
            if !reached.is_empty() {
                self.publish_values(&reached, ClassMembersPublicationWriter::ClassReach, span);
            }
        }
    }

    pub(super) fn audited_retained_result(
        &mut self,
        kind: RetainedResultKind,
        values: Values,
    ) -> AbstractValue {
        AbstractValue::AuditedRetainedResult(kind, self.intern_callback_set(values))
    }

    pub(super) fn audited_result_is_clean(&self, captured: &Captured<Values>) -> bool {
        if self
            .state
            .world
            .native
            .revoked_audited_results
            .contains_key(&captured.cache_identity())
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::AuditedRetainedResult(
                    RetainedResultKind::Factory,
                    captured.clone(),
                ))
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::AuditedRetainedResult(
                    RetainedResultKind::Constructor,
                    captured.clone(),
                ))
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::DeferredCallbacks(captured.clone()))
        {
            return false;
        }
        self.callback_values(captured.values.iter())
            .iter()
            .all(|value| {
                !self
                    .state
                    .world
                    .publication
                    .global_escaped_callbacks
                    .contains(value)
                    && match value {
                        AbstractValue::Value(Value::Alloc(Kind::Function, key))
                        | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                            !self.state.world.native.tainted_functions.contains(key)
                        }
                        AbstractValue::AuditedRetainedResult(_, nested) => !self
                            .state
                            .world
                            .native
                            .revoked_audited_results
                            .contains_key(&nested.cache_identity()),
                        _ => true,
                    }
            })
    }

    fn revoke_audited_result(&mut self, captured: &Captured<Values>) {
        // Equal capture sets can denote distinct runtime allocations. Sharing
        // revocation only loses precision; it never proves freshness or privacy.
        if self
            .state
            .world_mut()
            .native
            .revoked_audited_results
            .insert(captured.cache_identity(), captured.clone())
            .is_none()
        {
            self.state.world_mut().revision.bump_prefix();
        }
    }

    /// Any mutation or escape that can alter own descriptors invalidates the
    /// independent literal writability witness. The transition is monotonic
    /// and versions the execution prefix even when ordinary values do not
    /// change, so invocation memos cannot retain a stale descriptor premise.
    fn invalidate_writable_object(&mut self, object: u32) {
        self.literal_heap().invalidate_writable(object);
    }

    /// Code the interpreter did not run may have reached `values`: every
    /// object they reach, through stored references or through what a
    /// reachable body reads, may since hold anything.
    pub(super) fn unsettle<'v>(&mut self, values: impl IntoIterator<Item = &'v AbstractValue>) {
        let mut pending: Vec<_> = values.into_iter().cloned().collect();
        let mut visited = FxHashSet::default();
        // This pass does not change prefix values. Distinct closure contexts
        // still expand their own captures below, but share each binding's
        // identical prefix expansion. A later pass starts with an empty set.
        let mut expanded_prefix_reads = FxHashSet::default();
        let mut changed = false;
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            if let AbstractValue::Closure(_, captured) = &value {
                let cells: Vec<_> = captured.values.cells.values().copied().collect();
                self.havoc_cells(cells);
            }
            match &value {
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured) => {
                    self.revoke_audited_result(captured);
                }
                AbstractValue::Value(Value::Alloc(kind, object))
                    if kind.tracks_prefix_own_properties() =>
                {
                    changed |= self
                        .state
                        .world_mut()
                        .surface
                        .unsettled_objects
                        .insert(*object);
                    self.invalidate_writable_object(*object);
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                    if let Some(callable) = self.inputs.index.callables.get(key) {
                        pending.extend(
                            callable
                                .reads
                                .iter()
                                .filter(|binding| expanded_prefix_reads.insert(*binding))
                                .filter_map(|binding| {
                                    self.state.world.module.prefix_values.get(binding)
                                })
                                .flatten()
                                .cloned(),
                        );
                    }
                }
                _ => {}
            }
            self.visit_stored_references(&value, true, |reference| {
                pending.push(reference.clone());
            });
        }
        if changed {
            // Invocation memos must not reuse property proofs from before
            // a bounded callback made these objects unknown.
            self.state.world_mut().revision.bump_prefix();
        }
    }

    pub(super) fn taint_objects<'v>(
        &mut self,
        values: impl IntoIterator<Item = &'v AbstractValue>,
    ) {
        let mut pending: Vec<_> = values.into_iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            let mut expand_stored_references = true;
            match &value {
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured) => {
                    self.revoke_audited_result(captured);
                }
                AbstractValue::ArrayIntrinsic => {
                    self.state.world_mut().arrays.intrinsic_mutated = true;
                }
                AbstractValue::Value(Value::Alloc(kind, object))
                    if kind.tracks_prefix_own_properties() =>
                {
                    self.invalidate_writable_object(*object);
                    if !self
                        .state
                        .world_mut()
                        .surface
                        .tainted_objects
                        .insert(*object)
                    {
                        expand_stored_references = false;
                    }
                }
                AbstractValue::Value(Value::Alloc(Kind::Set, site))
                    if !self.state.world_mut().native.tainted_sets.insert(*site) =>
                {
                    expand_stored_references = false;
                }
                AbstractValue::OwnDescriptorMap(site) => {
                    self.invalidate_own_descriptor_map(*site);
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key)) => {
                    self.state.world_mut().native.tainted_functions.insert(*key);
                }
                AbstractValue::Closure(HeapAddress { callable: key, .. }, captured) => {
                    let cells: Vec<_> = captured.values.cells.values().copied().collect();
                    self.havoc_cells(cells);
                    self.state.world_mut().native.tainted_functions.insert(*key);
                }
                _ => {}
            }
            if expand_stored_references {
                self.visit_stored_references(&value, true, |reference| {
                    pending.push(reference.clone());
                });
            }
        }
    }

    fn eligible_local_constructor(
        &mut self,
        key: u32,
        arg_count: usize,
    ) -> Result<&LocalConstructor, ConstructionRejection> {
        let (Some(constructor), Some(binding)) = (
            self.inputs.index.local_constructors.get(&key),
            self.inputs.index.class_bindings.get(&key),
        ) else {
            return Err(ConstructionRejection::UncheckedConstructor);
        };
        // A constructorless, fieldless class only allocates an instance for a
        // direct zero-argument `new`. Even if its class object escaped, a
        // modified prototype or deferred method cannot run during allocation.
        // Any unknown/reassigned callee alternative still takes the ordinary
        // opaque path in execute_call. Other constructors need the class and
        // prototype to remain unexposed to avoid setters and method reentry.
        let inert_allocation = constructor.inert_allocation && arg_count == 0;
        let checked_default_fields = constructor.checked_default_fields && arg_count == 0;
        if constructor.checked_default_fields && arg_count != 0 {
            return Err(ConstructionRejection::ArgumentsToDefaultFields);
        }
        if !inert_allocation && !checked_default_fields {
            if self.inputs.index.other_class_uses.contains(binding) {
                return Err(ConstructionRejection::ClassUsedOutsideConstruction);
            }
            if self.inputs.export_reentry_enabled && self.inputs.reentry_bindings.contains(binding)
            {
                return Err(ConstructionRejection::ExportedClass);
            }
            if self.state.world.classes.tainted.contains(&key) {
                return Err(ConstructionRejection::ClassMutated);
            }
            if let Some(property) = constructor
                .own_fields
                .iter()
                .find(|property| !self.constructor_field_prototype_absent(key, property))
            {
                return Err(ConstructionRejection::FieldMayExistOnObjectPrototype {
                    property: property.clone(),
                });
            }
            if self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::ClassMembers(key))
            {
                return Err(ConstructionRejection::ClassMembersEscaped);
            }
        }
        if constructor
            .binding_reads
            .iter()
            .any(|id| !self.inputs.flow.is_declared(id))
        {
            return Err(ConstructionRejection::UndeclaredConstructorRead);
        }
        Ok(constructor)
    }

    fn construct_local_class(
        &mut self,
        key: u32,
        args: &[Values],
    ) -> Result<(Outcome, AbstractValue), ConstructionRejection> {
        let result = self.request(RuntimeOperation::Model(
            super::runtime_models::ModelOperation::ConstructLocalClass {
                key,
                arguments: args.to_vec(),
            },
        ));
        let RuntimeValue::Model(super::runtime_models::ModelResult::LocalClassConstruction(result)) =
            result
        else {
            unreachable!("local class models produce construction results");
        };
        *result
    }

    pub(super) fn construct_local_class_transfer(
        &mut self,
        key: u32,
        args: &[Values],
    ) -> Result<(Outcome, AbstractValue), ConstructionRejection> {
        let constructor = self.begin_local_construction(key, args)?;
        let outcome = self.invoke_checked_constructor(&constructor, args);
        Ok((outcome, self.constructed_instance(key, args)))
    }

    /// A local construction the checked constructor refused still runs its
    /// instance fields and body at `new`: record the typed loss and expose the
    /// class. The caller invokes the arguments' possible callbacks.
    fn reject_local_construction(
        &mut self,
        key: u32,
        args: &[Values],
        call: &Call,
        rejection: ConstructionRejection,
        outcome: &mut Outcome,
    ) {
        let reach = self.construction_reach(key, args);
        outcome.lose_precision(
            ExecutionPrecisionLossReason::UnsupportedConstruction { rejection },
            reach,
            call.owner,
            call.span,
        );
        outcome.unknown_call = true;
        self.note_unknown_call(call);
        self.state
            .world_mut()
            .classes
            .escaped_capabilities
            .insert(key);
        // Unsupported constructors still execute their instance fields and
        // body at `new`, even without a callback argument to seed the opaque
        // fallback.
        self.promote_class_members(key, outcome);
        self.publish_refused_instance_reach(key, call.span);
    }

    /// The unknown instance of a refused construction holds whatever its
    /// fields and constructors stored on `this`. Kept out of line for
    /// `execute_call`'s frame size.
    #[inline(never)]
    fn publish_refused_instance_reach(&mut self, key: u32, span: Span) {
        let reached = self.class_reach(key);
        self.publish_values(
            &reached,
            ClassMembersPublicationWriter::RefusedConstructionInstance,
            Some(span),
        );
    }

    /// `class` and the local classes it extends, named by binding or inline
    /// in an `extends` expression. That expression ran when the class was
    /// created; only the classes it names are followed.
    fn class_chain(&self, class: u32) -> BTreeSet<u32> {
        let index = self.inputs.index;
        let mut pending = vec![class];
        let mut chain = BTreeSet::new();
        while let Some(class) = pending.pop() {
            if !chain.insert(class) {
                continue;
            }
            let Some(heritage) = index.class_heritage_spans.get(&class) else {
                continue;
            };
            let (start, end) = (heritage.lo.0, heritage.hi.0);
            pending.extend(index.class_spans.range(start..end).map(|(base, _)| *base));
            for binding in index.references_within(start, end) {
                if self.inputs.flow.is_declared(binding) {
                    pending.extend(
                        self.inputs
                            .flow
                            .values_of_binding(binding)
                            .into_iter()
                            .filter_map(|value| match value {
                                Value::Alloc(Kind::Class, base) => Some(base),
                                _ => None,
                            }),
                    );
                }
            }
        }
        chain
    }

    /// What constructing `class`, or creating it, can store on an instance
    /// or on the class: every function its constructors, field initializers
    /// and static blocks create, and every value of the bindings they read,
    /// along its class chain. Methods are not included: they run as the
    /// class's own members, and their stores are their own.
    fn class_reach(&self, class: u32) -> Values {
        let index = self.inputs.index;
        let mut reached = Values::new();
        for class in self.class_chain(class) {
            for region in index.class_store_regions.get(&class).into_iter().flatten() {
                let (lo, hi) = (region.lo.0, region.hi.0);
                reached.extend(
                    index
                        .callable_spans
                        .range(lo..hi)
                        .filter(|(callable, _)| !index.class_member_callables.contains(*callable))
                        .map(|(callable, _)| {
                            AbstractValue::Value(Value::Alloc(Kind::Function, *callable))
                        }),
                );
                for binding in index.references_within(lo, hi) {
                    if self.inputs.flow.is_declared(binding) {
                        reached.extend(from_flow(self.inputs.flow.values_of_binding(binding)));
                    }
                }
            }
        }
        reached
    }

    /// The value of a refused construction: unknown, but it may retain every
    /// callback its arguments reach.
    fn refused_construction_values(&mut self, args: &[Values]) -> Values {
        let mut results = Values::from([AbstractValue::Value(Value::Top)]);
        let callbacks: Values = args
            .iter()
            .flat_map(|values| self.callback_values(values))
            .collect();
        if !callbacks.is_empty() {
            results.insert(self.deferred_callbacks(callbacks));
        }
        results
    }

    /// One `new` of a local class whose callee and arguments the ordered
    /// kernel evaluated. A checked class body without calls runs through the
    /// kernel's construct rule against the checked instance; a body the
    /// kernel cannot run keeps the flat lane's invocation and no coverage.
    pub(super) fn construct_local_class_ordered(
        &mut self,
        key: u32,
        arguments: Vec<SelectedValue>,
        call: &Call,
        outcome: &mut Outcome,
    ) -> OrderedConstruction {
        let args: Vec<Values> = arguments
            .iter()
            .map(|argument| argument.values.clone())
            .collect();
        let constructor = match self.begin_local_construction(key, &args) {
            Ok(constructor) => constructor,
            Err(rejection) => {
                self.reject_local_construction(key, &args, call, rejection, outcome);
                self.invoke_possible_callbacks_at(&args, false, Some(call.span), outcome);
                return OrderedConstruction {
                    values: self.refused_construction_values(&args),
                    body: None,
                };
            }
        };
        let inputs = self.inputs;
        let Some((body, class_owner)) = kernel_constructor_body(inputs.index, key, &constructor)
        else {
            let mut nested = self.invoke_checked_constructor(&constructor, &args);
            self.absorb_construction(&mut nested, outcome);
            return OrderedConstruction {
                values: Values::from([self.constructed_instance(key, &args)]),
                body: None,
            };
        };
        let instance = Values::from([self.constructed_instance(key, &args)]);
        if let Some(constructor_key) = constructor.key {
            self.record_executed_owner(class_owner);
            self.state
                .statement_invoked_callables
                .insert(constructor_key);
            self.activate_native_effects(class_owner, Some(constructor_key));
        }
        let mut nested = self.execute_local_body(
            body,
            Context::new(),
            Some(arguments),
            class_owner,
            LocalActivation::Constructor {
                class: key,
                instance: instance.clone(),
            },
        );
        let effects =
            (nested.complete && !nested.unknown_call).then(|| nested.effect_coverage.clone());
        let values = construct::construct_result(&nested.returns, &instance);
        self.absorb_construction(&mut nested, outcome);
        match values {
            Some(values) => OrderedConstruction {
                values,
                body: effects.map(|effects| {
                    (
                        LocalBodyIdentity::Constructor {
                            class: key,
                            owner: class_owner,
                        },
                        effects,
                    )
                }),
            },
            // The checked constructor admits no return statement; an object
            // result would replace the instance outside this rule.
            None => OrderedConstruction {
                values: Values::from([AbstractValue::Value(Value::Top)]),
                body: None,
            },
        }
    }

    fn absorb_construction(&mut self, nested: &mut Outcome, outcome: &mut Outcome) {
        self.apply_rebind_effects(&nested.effects.rebinds);
        outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
        outcome
            .executed_owners
            .extend(std::mem::take(&mut nested.executed_owners));
        outcome.unknown_call |= nested.unknown_call;
        outcome.inherit_completeness(
            nested.complete,
            std::mem::take(&mut nested.effects.precision_losses),
        );
    }

    /// The application identity of a checked instance: it retains every
    /// argument capability.
    fn constructed_instance(&mut self, key: u32, args: &[Values]) -> AbstractValue {
        let values: Values = args.iter().flatten().cloned().collect();
        AbstractValue::Instance(
            key,
            self.intern_callback_set(values),
            InstanceOrigin::Constructed,
        )
    }

    /// One local function or closure target of an ordinary call: invoke
    /// it, keep its receipt when it is the call's covered target, and absorb
    /// its effects. Kept out of line for `execute_call`'s frame size, which
    /// bounds the supported call depth.
    #[inline(never)]
    fn execute_local_call_target(
        &mut self,
        target: LocalCallTarget<'_>,
        result_values: &mut Values,
        result_selection: &mut Option<CallableSelection>,
        outcome: &mut Outcome,
    ) {
        let LocalCallTarget {
            key,
            captured,
            args,
            argument_selections,
            coverage_target,
            single_target,
            needs_value,
            call_span,
        } = target;
        let mut nested = self.invoke_selected(
            key,
            args.to_vec(),
            argument_selections.to_vec(),
            captured,
            false,
        );
        if single_target && coverage_target.is_some() {
            *result_selection = nested.return_selection.clone();
        }
        if let Some((expected, caller, target, prerequisites)) = coverage_target
            && *expected == key
        {
            let mut effect_coverage = nested.effect_coverage.clone();
            effect_coverage.depend_on(prerequisites);
            effect_coverage.selected_local_call(LocalCallReceipt {
                caller: caller.clone(),
                call_span,
                target: target.clone(),
                dependencies: prerequisites.clone(),
            });
            outcome.effect_coverage = effect_coverage;
        }
        if needs_value {
            result_values.extend(nested.returns.iter().cloned());
        }
        self.apply_rebind_effects(&nested.effects.rebinds);
        outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
        outcome.executed_owners.extend(nested.executed_owners);
        outcome.unknown_call |= nested.unknown_call;
        outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
    }

    /// Runs each local plain function among `targets` for `new`, and
    /// absorbs its effects into `outcome`.
    #[inline(never)]
    fn construct_plain_functions(
        &mut self,
        targets: &Values,
        args: &[Values],
        outcome: &mut Outcome,
    ) {
        for target in targets {
            let (key, captured) = match target {
                AbstractValue::Value(Value::Alloc(Kind::Function, key)) => (*key, None),
                AbstractValue::Closure(HeapAddress { callable: key, .. }, captured) => {
                    (*key, Some(captured.clone()))
                }
                _ => continue,
            };
            if !self.inputs.index.callables.contains_key(&key) {
                continue;
            }
            let mut nested = self.invoke(key, args.to_vec(), captured, false);
            self.apply_rebind_effects(&nested.effects.rebinds);
            outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
            outcome.executed_owners.extend(nested.executed_owners);
            outcome.unknown_call |= nested.unknown_call;
            outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
        }
    }

    /// The flat lane's invocation of an admitted constructor body.
    fn invoke_checked_constructor(
        &mut self,
        constructor: &LocalConstructor,
        args: &[Values],
    ) -> Outcome {
        let previous_inputs =
            std::mem::take(&mut self.state.world_mut().classes.checked_constructor_inputs);
        self.state.world_mut().classes.checked_constructor_inputs = args
            .iter()
            .flatten()
            .filter_map(|value| match value {
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => Some(*object),
                _ => None,
            })
            .collect();
        let outcome = constructor.key.map_or_else(
            || Outcome {
                complete: true,
                ..Outcome::default()
            },
            |constructor_key| self.invoke(constructor_key, args.to_vec(), None, true),
        );
        self.state.world_mut().classes.checked_constructor_inputs = previous_inputs;
        outcome
    }

    /// Admit one local construction: the checked constructor's eligibility
    /// and argument sources, with the export and inference bookkeeping every
    /// admitted construction records.
    fn begin_local_construction(
        &mut self,
        key: u32,
        args: &[Values],
    ) -> Result<LocalConstructor, ConstructionRejection> {
        let constructor = self.eligible_local_constructor(key, args.len())?.clone();
        if self.inputs.export_reentry_enabled {
            let exported: Values = self
                .inputs
                .reentry_bindings
                .iter()
                .flat_map(|id| {
                    self.state
                        .world
                        .module
                        .prefix_values
                        .get(id)
                        .into_iter()
                        .flatten()
                        .cloned()
                })
                .collect();
            self.taint_objects(&exported);
        }
        for (index, _) in &constructor.property_reads {
            let unchecked = ConstructionRejection::UncheckedPropertySource { parameter: *index };
            let values = args.get(*index).ok_or_else(|| unchecked.clone())?;
            if values.is_empty()
                || !values.iter().all(|value| {
                    matches!(value,
                    AbstractValue::Value(Value::Alloc(Kind::Object, object))
                        if self.inputs.index.plain_data_objects.contains(object)
                            && self.state.world.object_properties_are_settled(*object))
                })
            {
                return Err(unchecked);
            }
        }
        if let Some(proof) = self
            .inputs
            .index
            .local_class_construction_proof
            .inference_use_for_class(key)
        {
            let dependencies = self
                .inputs
                .index
                .local_class_construction_proof
                .dependencies_for_class(key)
                .expect("a class certificate retains its typed dependencies");
            self.record_inference_use(
                NativeIntegrityGuard::LocalClassConstruction,
                proof,
                dependencies,
            );
            if let Some((span, _)) = self.inputs.index.private_empty_set_sites.get(&key)
                && let Some(evidence) = self
                    .inputs
                    .index
                    .native_operation_input_evidence
                    .get(&CallSite::from(*span))
                && evidence.operation
                    == NativeInputKind::CallIdentity(
                        spec::NativeCallIdentityKind::SetEmptyPrivateField,
                    )
                && dependencies
                    .native_operation_inputs
                    .contains(&evidence.reference)
            {
                self.observations.record_native_operation_input_use(
                    *span,
                    crate::reports::schema::NativeOperationInputUse {
                        evidence: evidence.clone(),
                        source_location: None,
                        span: *span,
                    },
                );
            }
        }
        Ok(constructor)
    }

    /// The foreign wrapper can invoke its captured callbacks and any values
    /// they return synchronously. It cannot resume them past an await.
    pub(super) fn reachable_objects(&self, values: &Values) -> BTreeSet<u32> {
        let mut objects = BTreeSet::new();
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            if let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = &value {
                objects.insert(*object);
            }
            self.visit_stored_references(&value, true, |reference| {
                pending.push(reference.clone());
            });
        }
        objects
    }

    pub(super) fn expose_class_capabilities<'v>(
        &mut self,
        values: impl IntoIterator<Item = &'v AbstractValue>,
    ) {
        let mut pending: Vec<_> = values.into_iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            if let AbstractValue::Value(Value::Alloc(Kind::Class, key))
            | AbstractValue::ClassMembers(key)
            | AbstractValue::Instance(key, _, _) = &value
            {
                self.state
                    .world_mut()
                    .classes
                    .escaped_capabilities
                    .insert(*key);
            }
            self.visit_stored_references(&value, true, |reference| {
                pending.push(reference.clone());
            });
        }
    }

    /// The reach of a rejected local construction (SPEC.md § Synchronous
    /// execution diagnostics): the class and the callables its arguments hold.
    fn construction_reach(&self, key: u32, args: &[Values]) -> HavocReach {
        let class = AbstractValue::Value(Value::Alloc(Kind::Class, key));
        self.known_code_reach(None, std::iter::once(&class).chain(args.iter().flatten()))
    }

    /// The reach of an invocation truncated at a recursion, depth or
    /// expansion bound: the callee and the callables its inputs hold.
    fn invocation_reach(
        &self,
        key: u32,
        owner: SourceUnitOrdinal,
        args: &[Values],
        captured: &Context,
    ) -> HavocReach {
        if self.state.world.native.tainted_functions.contains(&key) {
            return self.dynamic_scope_reach().unwrap_or(HavocReach::Global {
                cause: GlobalReachCause::BorrowedReceiver,
            });
        }
        self.known_code_reach(
            Some(owner),
            args.iter().flatten().chain(captured.values().flatten()),
        )
    }

    fn dynamic_scope_reach(&self) -> Option<HavocReach> {
        (self.state.world.reachability.incomplete
            || !self.inputs.index.dynamic_scope_owners.is_empty())
        .then_some(HavocReach::Global {
            cause: GlobalReachCause::DynamicScope,
        })
    }

    /// Opaque code may hold the class or its prototype and install or borrow
    /// methods its constructor and fields run.
    fn class_may_run_on_borrowed_receiver(&self, key: u32) -> bool {
        self.state.world.classes.tainted.contains(&key)
            || self.state.world.classes.escaped_capabilities.contains(&key)
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::ClassMembers(key))
            || self
                .inputs
                .index
                .class_bindings
                .get(&key)
                .is_none_or(|binding| {
                    self.inputs.index.other_class_uses.contains(binding)
                        || (self.inputs.export_reentry_enabled
                            && self.inputs.reentry_bindings.contains(binding))
                })
    }

    /// Applies the collapse rules in order: dynamic scope, then the inputs
    /// (through stored properties, captures and accessors), then whether the
    /// synchronous read closure of `owner` and the inputs' callables and
    /// classes reaches a callee the source does not name.
    fn known_code_reach<'a>(
        &self,
        owner: Option<SourceUnitOrdinal>,
        inputs: impl IntoIterator<Item = &'a AbstractValue>,
    ) -> HavocReach {
        if let Some(reach) = self.dynamic_scope_reach() {
            return reach;
        }
        let global = |cause| HavocReach::Global { cause };
        let mut roots: Vec<_> = owner.into_iter().collect();
        let mut pending: Vec<AbstractValue> = inputs.into_iter().cloned().collect();
        let mut visited = FxHashSet::default();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            if self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&value)
            {
                return global(GlobalReachCause::TaintedInput);
            }
            match &value {
                AbstractValue::SymbolicInput(_) => return global(GlobalReachCause::TaintedInput),
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured)
                    if self
                        .state
                        .world
                        .native
                        .revoked_audited_results
                        .contains_key(&captured.cache_identity()) =>
                {
                    return global(GlobalReachCause::TaintedInput);
                }
                AbstractValue::Value(Value::Prim(_) | Value::Unavailable)
                | AbstractValue::ObjectAssign
                | AbstractValue::DeferredCallbacks(_)
                | AbstractValue::AuditedRetainedResult(_, _)
                | AbstractValue::OwnDescriptorMap(_) => {}
                AbstractValue::Value(Value::Top)
                | AbstractValue::GlobalObject
                | AbstractValue::ArrayIntrinsic => {
                    return global(GlobalReachCause::TaintedInput);
                }
                AbstractValue::CallbackFactory | AbstractValue::InlineCallbackFactory => {
                    return global(GlobalReachCause::UnknownCallee);
                }
                AbstractValue::ForeignDeferredRecord => {
                    return global(GlobalReachCause::ForeignRecord);
                }
                AbstractValue::Value(Value::Alloc(kind, object))
                    if kind.tracks_prefix_own_properties()
                        && !self.state.world.object_properties_are_settled(*object) =>
                {
                    return global(GlobalReachCause::TaintedInput);
                }
                AbstractValue::Value(Value::Alloc(Kind::Set, site))
                    if self.state.world.native.tainted_sets.contains(site) =>
                {
                    return global(GlobalReachCause::TaintedInput);
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                    if self.state.world.native.tainted_functions.contains(key) {
                        return global(GlobalReachCause::TaintedInput);
                    }
                    let Some(callable) = self.inputs.index.callables.get(key) else {
                        return global(GlobalReachCause::UnknownCallee);
                    };
                    roots.push(callable.owner);
                }
                AbstractValue::Value(Value::Alloc(Kind::Class, key))
                | AbstractValue::ClassMembers(key)
                | AbstractValue::Instance(key, _, _) => {
                    if self.class_may_run_on_borrowed_receiver(*key) {
                        return global(GlobalReachCause::BorrowedReceiver);
                    }
                    let Some(&class_owner) = self
                        .inputs
                        .flow
                        .callable_source_unit
                        .get(&Value::Alloc(Kind::Class, *key))
                    else {
                        return global(GlobalReachCause::UnknownCallee);
                    };
                    roots.push(class_owner);
                }
                AbstractValue::Value(Value::Alloc(kind, _)) => {
                    if !kind.tracks_prefix_references() {
                        unreachable!("unsupported retained-reference allocation kind");
                    }
                }
            }
            self.visit_stored_references(&value, true, |reference| {
                pending.push(reference.clone());
            });
        }
        let reaches = self.inputs.reaches_unknown_callee.get_or_init(|| {
            owners_reaching(
                &self.inputs.synchronous_owner_adjacency,
                &self.inputs.unknown_callee_owners,
            )
        });
        if roots.iter().any(|root| reaches[root.0]) {
            return global(GlobalReachCause::UnknownCallee);
        }
        // Opaque code holding a class reached here may have replaced the
        // prototype methods its instances call.
        let closure: BTreeSet<_> = SourceUnitReadClosure::extend_synchronous_source_units(
            &self.inputs.synchronous_owner_adjacency,
            roots,
            &mut BTreeSet::new(),
        )
        .into_iter()
        .collect();
        let state = &*self.state;
        if state
            .world
            .classes
            .tainted
            .iter()
            .chain(&state.world.classes.escaped_capabilities)
            .chain(
                state
                    .world
                    .publication
                    .global_escaped_callbacks
                    .iter()
                    .filter_map(|value| match value {
                        AbstractValue::ClassMembers(key) => Some(key),
                        _ => None,
                    }),
            )
            .filter_map(|key| {
                self.inputs
                    .flow
                    .callable_source_unit
                    .get(&Value::Alloc(Kind::Class, *key))
            })
            .any(|owner| closure.contains(owner))
        {
            return global(GlobalReachCause::BorrowedReceiver);
        }
        HavocReach::Bounded
    }

    pub(super) fn default_class_has_instance(&self, values: &Values) -> bool {
        let [AbstractValue::Value(Value::Alloc(Kind::Class, key))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return false;
        };
        self.inputs
            .index
            .class_values
            .get(key)
            .is_some_and(|class| class.default_has_instance)
            && !self.state.world.classes.tainted.contains(key)
            && !self.state.world.classes.escaped_capabilities.contains(key)
            && !self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::ClassMembers(*key))
            && self
                .inputs
                .index
                .class_bindings
                .get(key)
                .is_some_and(|binding| {
                    !self.inputs.index.other_class_uses.contains(binding)
                        && !self.inputs.index.exported_bindings.contains(binding)
                })
    }

    pub(super) fn ordinary_prototype_chain(&mut self, values: &Values) -> bool {
        !values.is_empty()
            && values.iter().all(|value| match value {
                AbstractValue::Value(Value::Prim(_)) => true,
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    self.state.world.object_properties_are_settled(*object)
                        && self.array_intrinsics_intact()
                        && (self.inputs.index.plain_data_objects.contains(object)
                            || self
                                .state
                                .world
                                .surface
                                .ordinary_spread_copies
                                .contains(object)
                            || self.state.world.arrays.dense.contains_key(object))
                }
                _ => false,
            })
    }

    fn contains_dense_array(&self, values: &Values) -> bool {
        self.reachable_objects(values)
            .iter()
            .any(|object| self.state.world.arrays.dense.contains_key(object))
    }

    /// [`Self::promote_raw_callbacks`] for values handed to opaque code, which
    /// may or may not invoke them: the promoted closure is assumed.
    pub(super) fn promote_escaped_callbacks(&mut self, values: &Values, outcome: &mut Outcome) {
        self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
            this.promote_raw_callbacks(values, outcome)
        });
    }

    pub(super) fn promote_raw_callbacks(&mut self, values: &Values, outcome: &mut Outcome) {
        let prior_outcome = std::mem::take(outcome);
        let result = self.request_callback_operation(CallbackOperation::PromoteRaw {
            values: values.clone(),
            outcome: Box::new(prior_outcome),
        });
        let CallbackResult::Outcome(result) = result else {
            unreachable!("raw callback promotion returns its updated outcome");
        };
        *outcome = *result;
    }

    pub(super) fn promote_raw_callbacks_transfer(
        &mut self,
        values: &Values,
        outcome: &mut Outcome,
    ) {
        let effect_owner = self.state.current_top_owner.unwrap();
        let mut owners = BTreeSet::new();
        for callback in self.callback_values(values) {
            let owner = match callback {
                AbstractValue::ForeignDeferredRecord => {
                    self.promote_foreign_record(outcome);
                    None
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => self
                    .inputs
                    .index
                    .callables
                    .get(&key)
                    .map(|callable| callable.owner),
                AbstractValue::ClassMembers(key) => self
                    .inputs
                    .flow
                    .callable_source_unit
                    .get(&Value::Alloc(Kind::Class, key))
                    .or_else(|| self.inputs.index.class_owners.get(&key))
                    .copied(),
                _ => None,
            };
            owners.extend(owner);
        }
        if self.state.world.reachability.incomplete {
            for owner in owners {
                let (reads, rebinds) = self
                    .inputs
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(owner);
                let reads = reads.clone();
                let rebinds = rebinds.clone();
                let referenced: Values = reads
                    .iter()
                    .flat_map(|id| {
                        self.state
                            .world
                            .module
                            .prefix_values
                            .get(id)
                            .into_iter()
                            .flatten()
                            .cloned()
                    })
                    .collect();
                self.taint_objects(&referenced);
                self.promote_lazy_effects(&reads, &rebinds, effect_owner, outcome);
            }
            return;
        }
        let reached = SourceUnitReadClosure::extend_synchronous_source_units(
            &self.inputs.synchronous_owner_adjacency,
            owners,
            &mut BTreeSet::new(),
        );
        for owner in reached {
            // This coverage-checked union includes every retained callable's
            // synchronous prefix, including returned callbacks and getters.
            // Bodies past a definite await cannot run during this protocol.
            let (reads, rebinds) = self.inputs.synchronous_effects(owner, false);
            let reads = reads.clone();
            let rebinds = rebinds.clone();
            let referenced: Values = reads
                .iter()
                .flat_map(|id| {
                    self.state
                        .world
                        .module
                        .prefix_values
                        .get(id)
                        .into_iter()
                        .flatten()
                        .cloned()
                })
                .collect();
            self.taint_objects(&referenced);
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, outcome);
        }
    }

    pub(super) fn promote_deferred_callbacks(&mut self, captured: &Values, outcome: &mut Outcome) {
        let prior_outcome = std::mem::take(outcome);
        let result = self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
            this.request_callback_operation(CallbackOperation::PromoteDeferred {
                captured: captured.clone(),
                outcome: Box::new(prior_outcome),
            })
        });
        let CallbackResult::Outcome(result) = result else {
            unreachable!("deferred callback promotion returns its updated outcome");
        };
        *outcome = *result;
    }

    pub(super) fn promote_deferred_callbacks_transfer(
        &mut self,
        captured: &Values,
        outcome: &mut Outcome,
    ) {
        let effect_owner = self.state.current_top_owner.unwrap();
        let mut roots = BTreeSet::new();
        for callback in self.callback_values(captured) {
            let key = match callback {
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => key,
                AbstractValue::ClassMembers(key) => {
                    self.promote_class_members(key, outcome);
                    continue;
                }
                _ => continue,
            };
            if let Some(callable) = self.inputs.index.callables.get(&key) {
                roots.insert(callable.owner);
            }
        }
        if self.state.world.reachability.incomplete {
            for owner in roots {
                let (reads, rebinds) = self
                    .inputs
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(owner);
                self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
            }
        } else {
            let reached = SourceUnitReadClosure::extend_synchronous_source_units(
                &self.inputs.synchronous_owner_adjacency,
                roots,
                &mut BTreeSet::new(),
            );
            for owner in reached {
                let (reads, rebinds) = self.inputs.synchronous_effects(owner, false);
                self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
            }
        }
    }

    fn promote_returned_callbacks(&mut self, returned: &Values) {
        let result = self.request_callback_operation(CallbackOperation::PromoteReturned {
            returned: returned.clone(),
        });
        let CallbackResult::Completed = result else {
            unreachable!("returned callback promotion has no value result");
        };
    }

    pub(super) fn promote_returned_callbacks_transfer(&mut self, returned: &Values) {
        let callbacks: Values = self
            .callback_values(returned)
            .into_iter()
            .filter(|callback| {
                match callback {
                    AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => self
                        .inputs
                        .index
                        .callables
                        .get(key)
                        .is_some_and(|callable| {
                            let world = self.state.world_mut();
                            let incomplete = world.reachability.incomplete;
                            world
                                .publication
                                .returned_callback_owners
                                .insert((incomplete, callable.owner))
                        }),
                    // Keep class capability/superclass checks on their existing
                    // path. Other aggregate values have already been expanded by
                    // callback_values and are not callable roots themselves.
                    AbstractValue::ClassMembers(_) => true,
                    _ => false,
                }
            })
            .collect();
        if callbacks.is_empty() {
            return;
        }
        let mut outcome = std::mem::take(&mut self.state.fallback_outcome);
        let rebinds_before = outcome.effects.rebinds.len();
        outcome.unsupported(
            ExecutionOperation::ReturnedCallback,
            self.state
                .current_top_owner
                .expect("returned callback has a top owner"),
            DUMMY_SP,
        );
        outcome.unknown_call = true;
        self.widen_native_effects();
        self.promote_deferred_callbacks(&callbacks, &mut outcome);
        // Class/superclass capability checks can invoke code and populate the
        // shared accumulator reentrantly. Preserve those contributions before
        // restoring the outer summary, including roots marked cached there.
        let mut reentrant = std::mem::take(&mut self.state.fallback_outcome);
        outcome.effects.reads.append(&mut reentrant.effects.reads);
        outcome
            .effects
            .rebinds
            .append(&mut reentrant.effects.rebinds);
        outcome
            .effects
            .uninitialized_reads
            .append(&mut reentrant.effects.uninitialized_reads);
        outcome
            .executed_owners
            .append(&mut reentrant.executed_owners);
        outcome.unknown_call |= reentrant.unknown_call;
        outcome.inherit_precision_losses(reentrant.effects.precision_losses);
        if outcome.effects.rebinds.len() != rebinds_before {
            self.apply_rebind_effects(&outcome.effects.rebinds);
        }
        // These effects belong to the active top-level prefix, regardless of
        // the lexical callee. Merge them once there, rather than copying the
        // same large read set through each cached nested invocation.
        self.state.fallback_outcome = outcome;
    }

    /// Opaque code holding a class or its prototype can retrieve and run any
    /// method, getter, field callback, or constructor synchronously. Cached
    /// owner traversal includes those bodies and their synchronous callees,
    /// while stopping reads after a definite await. Owners without complete
    /// callable coverage retain their whole lazy read set.
    fn promote_class_members(&mut self, key: u32, outcome: &mut Outcome) {
        if !self.state.active_class_member_promotions.insert(key) {
            return;
        }
        let owner = self
            .inputs
            .flow
            .callable_source_unit
            .get(&Value::Alloc(Kind::Class, key))
            .copied()
            .or_else(|| self.inputs.index.class_owners.get(&key).copied());
        let Some(owner) = owner else {
            self.state.active_class_member_promotions.remove(&key);
            return;
        };
        let effect_owner = self.state.current_top_owner.unwrap_or(owner);
        // Class expressions may have only their containing statement owner,
        // while their constructor and methods still contribute lazy reads to
        // that owner's summary.
        if self.state.world.reachability.incomplete {
            let (reads, rebinds) = self
                .inputs
                .source_unit_read_closure
                .lazy_effects_for_source_unit(owner);
            self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
        } else {
            if !self.state.class_member_effects.contains_key(&owner) {
                let reached = SourceUnitReadClosure::extend_synchronous_source_units(
                    &self.inputs.synchronous_owner_adjacency,
                    [owner],
                    &mut BTreeSet::new(),
                );
                let mut reads = BTreeSet::new();
                let mut rebinds = BTreeSet::new();
                for reached_owner in reached {
                    let (owner_reads, owner_rebinds) =
                        self.inputs.synchronous_effects(reached_owner, false);
                    reads.extend(owner_reads.iter().cloned());
                    rebinds.extend(owner_rebinds.iter().cloned());
                }
                self.state
                    .class_member_effects
                    .insert(owner, (reads, rebinds));
            }
            let (reads, rebinds) = &self.state.class_member_effects[&owner];
            self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
        }
        self.promote_captured_superclass(key, owner, outcome);
        // The opaque caller may invoke a method that changes a top-level
        // binding. Subsequent prefix calls must include its new possible
        // value, just as they do after an ordinary callback invocation.
        self.apply_rebind_effects(&outcome.effects.rebinds);
        self.state.active_class_member_promotions.remove(&key);
    }

    fn promote_captured_superclass(
        &mut self,
        key: u32,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let Some(class) = self.inputs.index.class_values.get(&key) else {
            return;
        };
        if class.super_class.is_none() {
            return;
        }
        let values = self
            .state
            .world
            .classes
            .superclasses
            .get(&key)
            .cloned()
            .unwrap_or_else(unknown);
        let mut has_unknown = values.is_empty();
        for value in values {
            let (function, captured) = match value {
                AbstractValue::Value(Value::Alloc(Kind::Class, base))
                | AbstractValue::ClassMembers(base) => {
                    self.promote_class_members(base, outcome);
                    continue;
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, function)) => (function, None),
                AbstractValue::Closure(
                    HeapAddress {
                        callable: function, ..
                    },
                    captured,
                ) => (function, Some(captured)),
                value
                    if value.is_primitive()
                        || matches!(value, AbstractValue::Value(Value::Unavailable)) =>
                {
                    continue;
                }
                _ => {
                    has_unknown = true;
                    continue;
                }
            };
            let count = self
                .inputs
                .index
                .callables
                .get(&function)
                .map_or(0, |callable| callable.params.len());
            let mut nested = self.invoke(function, vec![unknown(); count], captured, false);
            self.apply_rebind_effects(&nested.effects.rebinds);
            outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
            outcome.executed_owners.extend(nested.executed_owners);
            outcome.unknown_call |= nested.unknown_call;
            outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
        }
        if has_unknown {
            outcome.unsupported(ExecutionOperation::SuperclassConstruction, owner, DUMMY_SP);
            outcome.unknown_call = true;
            let effect_owner = self.state.current_top_owner.unwrap_or(owner);
            self.note_unknown_execution(effect_owner);
            self.invoke_possible_callbacks(&[unknown()], false, outcome);
        }
    }

    pub(super) fn closure_captures(&mut self, key: u32, captures: &Context) -> CapturedContext {
        let captured = lexical_captures(
            self.inputs.index,
            &self.state.world.active_cells,
            captures,
            key,
            self.inputs.capture_policy(),
        );
        self.intern_captures(captured)
    }

    fn available_export_callbacks(&self) -> Values {
        if !self.inputs.export_reentry_enabled {
            return Values::new();
        }
        self.export_surface_callback_values(ExportSurface::Reentry)
    }

    fn export_callback_values(&self) -> Values {
        self.export_surface_callback_values(ExportSurface::Exported)
    }

    fn export_surface_callback_values(&self, surface: ExportSurface) -> Values {
        let world: &World = &self.state.world;
        if let Some(callbacks) = self.state.export_callback_memos.get(surface, world) {
            return callbacks;
        }
        let (bindings, default) = match surface {
            ExportSurface::Reentry => (&self.inputs.reentry_bindings, self.inputs.reentry_default),
            ExportSurface::Exported => (&self.inputs.index.exported_bindings, true),
        };
        // Discover the whole export surface in one traversal. Exported aliases,
        // unknown values, and shared objects must not each re-expand the same
        // escaped callback/capture graph independently.
        let mut values = Values::new();
        self.visit_export_surface_callback_roots(bindings, default, |_, value| {
            values.insert(value);
        });
        let callbacks = self.callback_values(&values);
        self.state
            .export_callback_memos
            .store(surface, world, &callbacks);
        callbacks
    }

    fn visit_export_surface_callback_roots(
        &self,
        bindings: &BTreeSet<Id>,
        default: bool,
        mut visit: impl FnMut(Option<&Id>, AbstractValue),
    ) {
        for binding in bindings {
            if let Some(exported) = self.state.world.module.prefix_values.get(binding) {
                for value in exported {
                    visit(Some(binding), value.clone());
                }
            }
        }
        if default {
            for key in &self.inputs.index.exported_hoisted_functions {
                visit(
                    None,
                    AbstractValue::Value(Value::Alloc(Kind::Function, *key)),
                );
            }
            for value in &self.state.world.publication.exported_runtime_values {
                visit(None, value.clone());
            }
        }
    }

    fn visit_available_export_callback_roots(&self, visit: impl FnMut(Option<&Id>, AbstractValue)) {
        if self.inputs.export_reentry_enabled {
            self.visit_export_surface_callback_roots(
                &self.inputs.reentry_bindings,
                self.inputs.reentry_default,
                visit,
            );
        }
    }

    fn invoke_possible_callbacks(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        outcome: &mut Outcome,
    ) {
        self.invoke_possible_callbacks_at(args, no_sync_arguments, None, outcome);
    }

    pub(super) fn invoke_possible_callbacks_at(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        publication_span: Option<Span>,
        outcome: &mut Outcome,
    ) {
        let prior_outcome = std::mem::take(outcome);
        // Opaque code may or may not invoke the pool: what it reaches is assumed.
        let result = self.assuming(AssumedEffectReason::OpaqueCallbackInvocation, |this| {
            this.request_callback_operation(CallbackOperation::InvokePossible {
                arguments: args.to_vec(),
                no_sync_arguments,
                publication_span,
                outcome: Box::new(prior_outcome),
            })
        });
        let CallbackResult::Outcome(result) = result else {
            unreachable!("possible callback invocation returns its updated outcome");
        };
        *outcome = *result;
    }

    pub(super) fn invoke_possible_callbacks_transfer(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        publication_span: Option<Span>,
        outcome: &mut Outcome,
    ) {
        // Opaque code may run each available callback any number of times and
        // in any order. One fixed-order pass would let a member run before a
        // later member's write and miss the value it publishes. Repeat the
        // pass over the joined state until nothing a member can observe
        // changes. Each extra pass spends the shared invocation budget, whose
        // exhaustion takes the order-insensitive fallback summary.
        let mut foreign_promoted = false;
        loop {
            let callbacks = self.possible_callback_pool(
                args,
                no_sync_arguments,
                &mut foreign_promoted,
                outcome,
            );
            let before = self.callback_pool_observation();
            if self.invoke_callback_pool_once(
                &callbacks,
                args,
                no_sync_arguments,
                publication_span,
                outcome,
            ) || self.callback_pool_observation() == before
            {
                break;
            }
            // Invocation memos keyed by the pre-pass revision describe the
            // smaller state; changes outside prefix values do not bump it.
            self.state.world_mut().revision.bump_prefix();
            self.state.invocation_count += 1;
        }
        // An opaque callee can retain any callback passed to it and invoke it
        // during a later prefix event.
        let values = ordered_values(args.iter().flatten().cloned());
        self.taint_objects(&values);
        let callbacks = self.callback_values(&values);
        self.insert_escaped_callback_values(
            &callbacks,
            ClassMembersPublicationWriter::PossibleCallbackRetention,
            publication_span,
            None,
        );
    }

    fn possible_callback_pool(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        foreign_promoted: &mut bool,
        outcome: &mut Outcome,
    ) -> Values {
        let mut callbacks = self
            .state
            .world
            .publication
            .global_escaped_callbacks
            .clone();
        callbacks.extend_absent(&self.available_export_callbacks());
        // A callback-argument exemption does not certify foreign getters or
        // direct live-cell captures reachable through a returned record.
        let supplied = ordered_values(args.iter().flatten().cloned());
        let supplied_callbacks = self.callback_values(&supplied);
        if !*foreign_promoted
            && (callbacks.contains(&AbstractValue::ForeignDeferredRecord)
                || supplied_callbacks.contains(&AbstractValue::ForeignDeferredRecord))
        {
            self.promote_foreign_record(outcome);
            *foreign_promoted = true;
        }
        if !no_sync_arguments {
            callbacks.extend_absent(&supplied_callbacks);
        }
        // Activate the foreign pool before any callback-size fallback, which
        // otherwise only expands callables with local source owners.
        callbacks.remove(&AbstractValue::ForeignDeferredRecord);
        callbacks
    }

    /// Everything a pool member's invocation can depend on. The prefix
    /// revision covers binding values; heap maps, taints, escapes and native
    /// capability state can change without bumping it.
    fn callback_pool_observation(&self) -> CallbackPoolObservation {
        let state = &*self.state;
        CallbackPoolObservation {
            prefix_revision: state.world.revision.prefix,
            reachability_revision: state.world.revision.reachability,
            prefix_object_properties: state.world.surface.prefix_object_properties.clone(),
            dense_arrays: state.world.arrays.dense.clone(),
            primitive_arrays: state.world.arrays.primitive.clone(),
            accumulated_values: state.world.arrays.accumulated_values.clone(),
            applied_accessors: state.world.surface.applied_accessors.clone(),
            class_superclasses: state.world.classes.superclasses.clone(),
            set_sizes: [
                state.world.publication.global_escaped_callbacks.len(),
                state.world.arrays.unknown_contents.len(),
                state.world.publication.exported_runtime_values.len(),
                state.world.surface.tainted_objects.len(),
                state.world.native.tainted_sets.len(),
                state.world.native.tainted_functions.len(),
                state.world.native.revoked_audited_results.len(),
                state.world.classes.tainted.len(),
                state.world.classes.escaped_capabilities.len(),
                state.world.surface.ordinary_spread_copies.len(),
                state.world.surface.descriptor_copy_results.len(),
                state
                    .world
                    .descriptors
                    .values()
                    .filter(|map| map.valid)
                    .count(),
            ],
            array_intrinsic_mutated: state.world.arrays.intrinsic_mutated,
            uncertain_native_execution: state.world.native.uncertain_execution,
        }
    }

    /// Runs every pool member once against the evolving state. Returns whether
    /// the bounded fallback summarized the pool instead.
    fn invoke_callback_pool_once(
        &mut self,
        callbacks: &Values,
        source_arguments: &[Values],
        no_sync_arguments: bool,
        diagnostic_callsite_span: Option<Span>,
        outcome: &mut Outcome,
    ) -> bool {
        // Unknown host code may invoke any callback it received with another
        // currently available callback, so each formal gets the conservative
        // callback set plus a non-callable/unknown possibility.
        // Allocate only when an invoked function consumes a formal. The input
        // pool is immutable; constructing this set reads no live heap state.
        let possible_argument = std::cell::OnceCell::new();
        let args_for = |key: u32, index: &ExecutionIndex| {
            let count = index
                .callables
                .get(&key)
                .map_or(0, |callable| callable.params.len());
            if count == 0 {
                return Vec::new();
            }
            let possible_argument = possible_argument.get_or_init(|| {
                let mut values = callbacks.clone();
                values.insert(AbstractValue::Value(Value::Top));
                values
            });
            vec![possible_argument.clone(); count]
        };
        let reentrant_pool = callbacks.iter().any(|callback| {
            let key = match callback {
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => *key,
                _ => return false,
            };
            self.state.active.contains(&ActiveInvocation {
                callable: key,
                top_owner: self.state.current_top_owner,
            })
        });
        for callback in callbacks.iter().cloned() {
            if reentrant_pool
                || callbacks.len() > MAX_SYNCHRONOUS_CALLBACK_POOL
                || self.state.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS
                || self.state.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            {
                // A recursive member already falls back to a closure over the
                // entire pool in its arguments. Summarize that pool once rather
                // than enumerate permutations of callbacks around the cycle.
                // The same batch fallback bounds exhausted acyclic expansions.
                let reason = if reentrant_pool {
                    ExecutionPrecisionLossReason::RecursiveInvocation
                } else if callbacks.len() > MAX_SYNCHRONOUS_CALLBACK_POOL {
                    ExecutionPrecisionLossReason::CallbackPoolLimit {
                        limit: MAX_SYNCHRONOUS_CALLBACK_POOL,
                        pool_size: callbacks.len(),
                    }
                } else if self.state.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS {
                    ExecutionPrecisionLossReason::InvocationExpansionLimit {
                        limit: MAX_SYNCHRONOUS_INVOCATIONS,
                    }
                } else {
                    ExecutionPrecisionLossReason::InvocationDepthLimit {
                        limit: MAX_SYNCHRONOUS_INVOCATION_DEPTH,
                    }
                };
                let mut nested =
                    self.bounded_invocation_fallback(BoundedInvocationFallbackRequest {
                        owner: self
                            .state
                            .current_top_owner
                            .expect("callback call has a top owner"),
                        arguments: std::slice::from_ref(callbacks).to_vec(),
                        captured: Context::new(),
                        reason,
                        reach: HavocReach::Global {
                            cause: GlobalReachCause::EscapedCallbackPool,
                        },
                        span: DUMMY_SP,
                        diagnostics: BoundedInvocationFallbackDiagnostics {
                            callsite_span: diagnostic_callsite_span,
                            arguments: Some(if no_sync_arguments {
                                Vec::new()
                            } else {
                                source_arguments.to_vec()
                            }),
                        },
                    });
                self.apply_rebind_effects(&nested.effects.rebinds);
                outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
                outcome.executed_owners.extend(nested.executed_owners);
                outcome.unknown_call |= nested.unknown_call;
                outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
                return true;
            }
            let writable_before = self.state.world.surface.writable_prefix.clone();
            let nested = match callback {
                AbstractValue::ClassMembers(key) => {
                    self.promote_class_members(key, outcome);
                    None
                }
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured) => {
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    None
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key)) => {
                    Some(self.invoke(key, args_for(key, self.inputs.index), None, false))
                }
                AbstractValue::Closure(HeapAddress { callable: key, .. }, captured) => {
                    Some(self.invoke(key, args_for(key, self.inputs.index), Some(captured), false))
                }
                _ => None,
            };
            if let Some(mut nested) = nested {
                // Foreign code can retain and invoke a callback's synchronous
                // return value as well. This includes a descriptor getter that
                // returns another callable; stopping at the getter loses the
                // returned callable's importer reads. Prefix coverage preserves
                // the ordinary boundary at an unconditional await. Whatever the
                // member returns reaches that code too.
                self.taint_objects(&nested.returns);
                self.promote_returned_callbacks(&nested.returns);
                self.apply_rebind_effects(&nested.effects.rebinds);
                outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
                outcome.executed_owners.extend(nested.executed_owners);
                outcome.unknown_call |= nested.unknown_call;
                outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
            }
            // Opaque code can omit or repeat this member. A changed exact
            // post-state cannot replace the entry witness as though the call
            // occurred once. Widen before another member or fixed-point pass
            // observes it; otherwise pushes grow exact lengths to the budget.
            if !self
                .state
                .world
                .surface
                .writable_prefix
                .ptr_eq(&writable_before)
            {
                let changed: Vec<_> = self
                    .state
                    .world
                    .surface
                    .writable_prefix
                    .iter()
                    .filter_map(|(object, state)| {
                        (writable_before.get(object) != Some(state)).then_some(*object)
                    })
                    .collect();
                for object in changed {
                    self.invalidate_writable_object(object);
                }
            }
        }
        false
    }

    pub(super) fn apply_rebind_effects(&mut self, rebinds: &BTreeSet<(Id, SourceUnitOrdinal)>) {
        for (id, _) in rebinds {
            self.state
                .world_mut()
                .module
                .prefix_callable_selections
                .remove(id);
            if !self.inputs.top_bindings.contains(id) {
                continue;
            }
            let changed = self
                .state
                .world_mut()
                .module
                .prefix_values
                .entry_or_default(id)
                .insert(AbstractValue::Value(Value::Top));
            if changed {
                self.state.world_mut().revision.bump_prefix();
            }
        }
    }

    fn bounded_invocation_fallback(
        &mut self,
        request: BoundedInvocationFallbackRequest,
    ) -> Outcome {
        let result = self
            .request_callback_operation(CallbackOperation::BoundedInvocationFallback { request });
        let CallbackResult::Outcome(result) = result else {
            unreachable!("bounded callback fallback returns an outcome");
        };
        *result
    }

    pub(super) fn bounded_invocation_fallback_transfer(
        &mut self,
        request: BoundedInvocationFallbackRequest,
    ) -> Outcome {
        let BoundedInvocationFallbackRequest {
            owner,
            arguments,
            captured,
            reason,
            reach,
            span,
            diagnostics,
        } = request;
        let args = &arguments;
        let BoundedInvocationFallbackDiagnostics {
            callsite_span: diagnostic_callsite_span,
            arguments: diagnostic_arguments,
        } = diagnostics;
        let captured = &captured;
        let effect_owner = self.state.current_top_owner.unwrap_or(owner);
        if self.state.array_iteration_depth > 0 {
            self.state.array_iteration_unsupported = true;
        }
        // Within one source prefix initialization is fixed. Keep a monotone
        // summary so each reachable owner's lazy effects are promoted once,
        // even when recursion or changing abstract arguments hit the bound
        // repeatedly. Reusing earlier fallback roots only adds constraints.
        let mut outcome = std::mem::take(&mut self.state.fallback_outcome);
        let rebinds_before = outcome.effects.rebinds.len();
        outcome.lose_precision(reason.clone(), reach.clone(), owner, span);
        outcome.unknown_call = true;
        self.widen_native_effects();
        outcome.executed_owners.insert(owner);

        // The recursive summary is no longer being expanded. Preserve reads
        // from this callable and callbacks actually present in its arguments,
        // captures, exports, or escaped globals. Do not turn ordinary
        // recursion into a whole-chunk reachability root.
        let export_callbacks = self.available_export_callbacks();
        let possible_values = ordered_values(
            args.iter()
                .flatten()
                .chain(captured.values().flatten())
                .chain(&self.state.world.publication.global_escaped_callbacks)
                .chain(&export_callbacks)
                .cloned(),
        );

        let callbacks = self.callback_values(&possible_values);
        let first_class_candidates: BTreeSet<_> = callbacks
            .iter()
            .filter_map(|callback| match callback {
                AbstractValue::ClassMembers(key)
                    if !self
                        .state
                        .world
                        .publication
                        .global_escaped_callbacks
                        .contains(callback) =>
                {
                    Some(*key)
                }
                _ => None,
            })
            .collect();
        let input_paths = if first_class_candidates.is_empty() {
            BTreeMap::new()
        } else {
            // Only the first-publication diagnostic needs seed labels. Keep
            // this path-only work out of fallback calls with no new class
            // capability, and do not change the deduplicated value union.
            // Callback-pool truncation passes a synthetic `[callbacks]` value
            // to the semantic fallback; trace from the original call inputs
            // instead so the report never calls that synthetic vector arg 0.
            let path_arguments = diagnostic_arguments.as_deref().unwrap_or(args);
            let mut path_roots = Values::new();
            let mut fallback_seeds = BTreeMap::new();
            for (position, values) in path_arguments.iter().enumerate() {
                for value in values {
                    path_roots.insert(value.clone());
                    fallback_seeds
                        .entry(value.clone())
                        .or_insert(BoundedInvocationFallbackSeed::Argument { position });
                }
            }
            for (binding, values) in captured {
                for value in values {
                    path_roots.insert(value.clone());
                    fallback_seeds.entry(value.clone()).or_insert_with(|| {
                        BoundedInvocationFallbackSeed::CapturedBinding {
                            binding: binding.0.to_string(),
                        }
                    });
                }
            }
            for value in &self.state.world.publication.global_escaped_callbacks {
                path_roots.insert(value.clone());
                fallback_seeds
                    .entry(value.clone())
                    .or_insert(BoundedInvocationFallbackSeed::EscapedCallbackPool);
            }
            self.visit_available_export_callback_roots(|binding, value| {
                path_roots.insert(value.clone());
                fallback_seeds
                    .entry(value.clone())
                    .or_insert_with(|| match binding {
                        Some(binding) => BoundedInvocationFallbackSeed::AvailableExportBinding {
                            binding: binding.0.to_string(),
                        },
                        None => BoundedInvocationFallbackSeed::AvailableExportCallbacks,
                    });
            });
            let (_, paths) = self.callback_values_with_paths(
                &path_roots,
                &fallback_seeds,
                &first_class_candidates,
            );
            paths
        };
        let diagnostic_callsite = diagnostic_callsite_span
            .filter(|span| *span != DUMMY_SP)
            .map(|span| BoundedInvocationFallbackCallSite {
                top_level_owner: self.state.current_top_owner,
                span_start: span.lo.0,
                span_end: span.hi.0,
                source_location: None,
            });
        let publication_diagnostic = BoundedFallbackPublicationDiagnostic {
            input_paths,
            callsite: diagnostic_callsite,
        };

        if callbacks.contains(&AbstractValue::ForeignDeferredRecord) {
            self.promote_foreign_record(&mut outcome);
        }
        // The summarized bodies run on these inputs without the interpreter
        // following what they do to them.
        let reached = ordered_values(args.iter().flatten().chain(&callbacks).cloned());
        self.unsettle(&reached);
        let mut roots = BTreeSet::new();
        for callback in &callbacks {
            let key = match callback {
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => Some(*key),
                _ => None,
            };
            if let Some(callback_owner) =
                key.and_then(|key| self.inputs.index.callables.get(&key).map(|c| c.owner))
            {
                roots.insert(callback_owner);
            }
            if let AbstractValue::ClassMembers(key) = callback
                && let Some(&owner) = self
                    .inputs
                    .flow
                    .callable_source_unit
                    .get(&Value::Alloc(Kind::Class, *key))
            {
                roots.insert(owner);
            }
        }
        // The summarized bodies can also reach a callable through the heap
        // rather than a lexical read: a stored closure they call or return,
        // or an accessor their property access runs. Storing it there took
        // an effect: a prior impure owner or a callable run earlier in this
        // statement, whose lexical closure covers the stored callable's body.
        // A pure owner's fresh object is reachable only through a lexical
        // read of its binding, which the closure already follows.
        roots.insert(effect_owner);
        roots.extend(outcome.executed_owners.iter().copied());
        roots.extend(self.state.statement_executed_owners.iter().copied());
        roots.extend(
            self.state
                .prior_effect_owners
                .iter()
                .copied()
                .filter(|candidate| {
                    !self
                        .state
                        .world
                        .publication
                        .accepted_deferred_global_writes
                        .contains(candidate)
                }),
        );
        self.extend_reachable_owners(roots.iter().copied().chain([owner]));
        let state = &mut *self.state;
        let mut reached = self.inputs.extend_available_source_units(
            roots.iter().copied(),
            &state.world.module.initialized_bindings,
            state.world.reachability.incomplete,
            &mut state.callback_fallback.expanded,
            &mut state.callback_fallback.deferred,
        );
        if self.state.world.reachability.incomplete {
            // Dynamic lexical scope defeats the normal binding-read closure.
            // Revisit even previously expanded owners: their cached prefix
            // effects may have omitted reads that eval or with can reach.
            if !self.state.callback_fallback.incomplete_expanded {
                self.state.callback_fallback.incomplete_expanded = true;
                for fact in self.inputs.facts {
                    self.state.callback_fallback.expanded.insert(fact.ordinal);
                    reached.push(fact.ordinal);
                }
            }
        }
        for reached_owner in reached {
            let (reads, rebinds) = self
                .inputs
                .synchronous_effects(reached_owner, self.state.world.reachability.incomplete);
            for id in reads
                .iter()
                .filter(|id| self.inputs.top_bindings.contains(*id))
            {
                if self.state.callback_fallback.reads.insert(id.clone()) {
                    self.state.callback_fallback.read_additions.push(id.clone());
                }
            }
            for id in rebinds
                .iter()
                .filter(|id| self.inputs.top_bindings.contains(*id))
            {
                if self.state.callback_fallback.rebinds.insert(id.clone()) {
                    self.state
                        .callback_fallback
                        .rebind_additions
                        .push(id.clone());
                }
            }
        }
        let callback_sizes = (
            self.state.callback_fallback.reads.len(),
            self.state.callback_fallback.rebinds.len(),
        );
        if let Some((reads_before, rebinds_before)) = self.state.fallback_callback_sizes {
            // New escaped roots can appear during a statement. Apply only
            // their newly discovered IDs, rather than repeatedly reapplying
            // the entire growing closure at the same initialization prefix.
            let reads = self.state.callback_fallback.read_additions[reads_before..]
                .iter()
                .cloned()
                .collect();
            let rebinds = self.state.callback_fallback.rebind_additions[rebinds_before..]
                .iter()
                .cloned()
                .collect();
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
        } else {
            self.promote_lazy_effects(
                &self.state.callback_fallback.reads,
                &self.state.callback_fallback.rebinds,
                effect_owner,
                &mut outcome,
            );
        }
        self.state.fallback_callback_sizes = Some(callback_sizes);
        // A directly truncated callable need not escape. Follow its same
        // synchronous prefix graph without publishing private roots into the
        // persistent callback cache. Expand each local owner once per source
        // statement; earlier promotions remain in fallback_outcome.
        if !self.state.callback_fallback.expanded.contains(&owner) {
            let state = &mut *self.state;
            let reached = self.inputs.extend_available_source_units(
                [owner],
                &state.world.module.initialized_bindings,
                state.world.reachability.incomplete,
                &mut state.fallback_local_owners,
                &mut state.fallback_local_deferred,
            );
            for reached_owner in reached {
                let (reads, rebinds) = self
                    .inputs
                    .synchronous_effects(reached_owner, self.state.world.reachability.incomplete);
                let reads = reads
                    .difference(&self.state.callback_fallback.reads)
                    .cloned()
                    .collect();
                let rebinds = rebinds
                    .difference(&self.state.callback_fallback.rebinds)
                    .cloned()
                    .collect();
                self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
            }
        }

        // The truncated body may have passed one of these callback values to
        // opaque code. Retain only this proven escape set for later calls.
        self.insert_escaped_callback_values(
            &callbacks,
            ClassMembersPublicationWriter::BoundedInvocationFallback,
            Some(span),
            Some(&publication_diagnostic),
        );
        if outcome.effects.rebinds.len() != rebinds_before {
            self.apply_rebind_effects(&outcome.effects.rebinds);
        }
        // Fallback effects are attributed to the active top-level statement,
        // not the lexical callee. Accumulate them there once instead of copying
        // and re-merging the whole summary through every nested invocation.
        self.state.fallback_outcome = outcome;
        let mut result = Outcome {
            returns: unknown(),
            unknown_call: true,
            executed_owners: BTreeSet::from([owner]),
            ..Outcome::default()
        };
        result.lose_precision(reason, reach, owner, span);
        result
    }

    /// The summary's native constructions hold in every analyzed chunk's
    /// census, this one included.
    pub(super) fn source_record_factory(&self, callee: &ValueExpr, args: &[ValueExpr]) -> bool {
        args.is_empty()
            && matches!(callee, ValueExpr::Binding(id)
            if self.inputs.index.returned_record_summaries.contains_key(id))
    }

    /// A single-execution imported call may carry a fresh ordinary descriptor
    /// graph. The author certifies the provider's native slots and return-only
    /// retention; the source index proves the exact input and key collisions.
    pub(super) fn descriptor_copy_projection(
        &mut self,
        callee: &ValueExpr,
        args: &[Values],
        site: CallSite,
    ) -> Option<OwnPropertyProof> {
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let certificate = self.inputs.index.descriptor_copy_calls.get(&site)?;
        if &certificate.provider_binding != binding {
            return None;
        }
        let [source] = args else { return None };
        let [AbstractValue::Value(Value::Alloc(Kind::Object, actual))] =
            source.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if actual != &certificate.source_object {
            return None;
        }
        let proof = self.own_property_projection(
            source,
            OwnPropertyRequest::AuditedDescriptorCopy {
                semantic_call: SemanticKey::DescriptorCopyCall(site.source_span().lo.0),
            },
        )?;
        self.queries.assume(&proof.dependencies);
        self.observations
            .record_inference_use(proof.inference_use());
        Some(proof)
    }

    pub(super) fn install_descriptor_copy_result(
        &mut self,
        proof: OwnPropertyProof,
        args: &[Values],
        site: CallSite,
    ) -> Values {
        let result = site.allocation_id();
        let OwnPropertyProjection::Descriptors(projection) = proof.projection else {
            return Values::from([AbstractValue::Value(Value::Unavailable)]);
        };
        // The audited certificate admits this definition onto the factory's
        // fresh result; the commit is the same one every native row uses.
        self.descriptor_heap().capture(site, projection);
        let application = self
            .state
            .world
            .descriptors
            .get(&site)
            .and_then(|map| DescriptorApplication::from_map(result, map));
        let commit = match application {
            Some(application) => NativeOperationCommit::DefineProperties(application),
            None => NativeOperationCommit::Integrity {
                targets: BTreeSet::from([result]),
            },
        };
        native_operation::commit(&mut self.descriptor_heap(), commit);
        let Some(certificate) = self.inputs.index.descriptor_copy_calls.get(&site) else {
            unreachable!()
        };
        let keys = certificate.signature.assigned_data_keys.clone();
        let mut captured = args[0].clone();
        captured.extend(self.callback_values(&args[0]));
        let method = Values::from([self.deferred_callbacks(captured)]);
        let properties = self
            .state
            .world_mut()
            .surface
            .prefix_object_properties
            .entry(result)
            .or_default();
        for key in keys {
            properties.insert(Some(key.clone()), method.clone());
        }
        if let Some(accessors) = self
            .state
            .world_mut()
            .surface
            .applied_accessors
            .get_mut(&result)
        {
            for key in &certificate.signature.assigned_data_keys {
                accessors.remove(key);
            }
        }
        self.state
            .world_mut()
            .surface
            .descriptor_copy_results
            .insert(result);
        self.state.world_mut().revision.bump_prefix();
        // Provider code and native operations may still throw after argument
        // evaluation. Keep normal-return identity and abrupt completion apart.
        Values::from([
            AbstractValue::Value(Value::Alloc(Kind::Object, result)),
            AbstractValue::Value(Value::Unavailable),
        ])
    }

    /// Instantiates a provider's checked wrapper timing proof for this exact
    /// caller-owned ordinary object. The raw source getter remains deferred in
    /// the returned capability; only the wrapper's eager phase is certified.
    pub(super) fn source_namespace_wrapper(
        &mut self,
        callee: &ValueExpr,
        args: &[Values],
        site: CallSite,
    ) -> Option<u32> {
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let summary = self.inputs.index.namespace_wrapper_summaries.get(binding)?;
        let [source, flag] = args else {
            return None;
        };
        if !self
            .inputs
            .index
            .literal_true_second_arguments
            .contains(&site)
            || flag.is_empty()
            || !self.ordinary_object_prototype_intact()
        {
            return None;
        }
        let input = self.ordinary_source_object(source)?;
        let OwnPropertyProjection::Ordinary { source_object } = &input.projection else {
            return None;
        };
        self.observations
            .record_inference_use(input.inference_use());
        self.record_inference_use(
            NativeIntegrityGuard::NativeCallIdentity,
            summary.inference.clone(),
            &summary.dependencies,
        );
        Some(*source_object)
    }

    pub(super) fn promote_foreign_record(&mut self, outcome: &mut Outcome) {
        // An opaque foreign capability can read or rebind importer cells,
        // through a direct ESM capture or a retained callback. Precision is
        // gained at construction only; later use preserves both obligations.
        self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
            // Foreign code cannot assign an ESM import binding directly. A
            // retained importer callback can perform any source rebind; eval
            // or with defeats that finite source census.
            let rebinds: BTreeSet<_> = if this.state.world.reachability.incomplete {
                this.inputs.binding_owner.keys().cloned().collect()
            } else {
                this.inputs
                    .facts
                    .iter()
                    .flat_map(|fact| fact.rebinds.eager.iter().chain(&fact.rebinds.lazy))
                    .filter(|binding| this.inputs.top_bindings.contains(*binding))
                    .cloned()
                    .collect()
            };
            this.promote_top_effects(
                this.inputs.binding_owner.keys().collect(),
                rebinds.iter().collect(),
                this.state
                    .current_top_owner
                    .expect("foreign record use has a top owner"),
                outcome,
            )
        });
        self.apply_rebind_effects(&outcome.effects.rebinds);
        let reached: Values = self
            .state
            .world
            .module
            .prefix_values
            .values()
            .flatten()
            .chain(self.state.world.publication.exported_runtime_values.iter())
            .chain(self.state.world.publication.global_escaped_callbacks.iter())
            .cloned()
            .collect();
        self.unsettle(&reached);
        outcome.unknown_call = true;
        self.widen_native_effects();
        outcome.lose_precision(
            ExecutionPrecisionLossReason::UnknownCallTarget,
            HavocReach::Global {
                cause: GlobalReachCause::ForeignRecord,
            },
            self.state
                .current_top_owner
                .expect("foreign record use has a top owner"),
            DUMMY_SP,
        );
    }

    pub(super) fn evaluate(
        &mut self,
        expr: &ValueExpr,
        env: &Context,
        captures: &Context,
    ) -> Values {
        self.evaluate_allocation_child(expr, env, captures).values
    }

    pub(super) fn evaluate_allocation_child(
        &mut self,
        expr: &ValueExpr,
        env: &Context,
        captures: &Context,
    ) -> super::allocation_transfer::AllocationValue {
        let (result, judgment) =
            self.request_with_allocation_judgment(RuntimeOperation::Evaluate {
                expression: expr.clone(),
                environment: env.clone(),
                captures: captures.clone(),
                source_covered: self.source_covered,
            });
        let RuntimeValue::Values(values) = result else {
            unreachable!("expression transfers produce values");
        };
        super::allocation_transfer::AllocationValue { values, judgment }
    }

    /// Availability of a module-local declarative cell at this exact prefix.
    /// Imported cells need a separate provider initialization proof; a global
    /// name may resolve through an accessor on the host's global object.
    pub(super) fn initialized_local_binding(&self, id: &Id) -> bool {
        self.inputs.top_bindings.contains(id)
            && !self.inputs.index.imported_bindings.contains(id)
            && self.state.world.module.initialized_bindings.contains(id)
    }

    /// The flattened call pass and later initializer projection see the same
    /// prefix only when this owner has no intervening operation. Compound
    /// initializers still need ordered result transport through the runtime.
    pub(super) fn root_own_data_read(&self, expression: &ValueExpr) -> Option<OwnDataRead> {
        let ValueExpr::Member(receiver, Some(property), key, site) = expression else {
            return None;
        };
        if !key.supports_own_data_read(property) {
            return None;
        }
        let ValueExpr::Binding(receiver) = receiver.as_ref() else {
            return None;
        };
        let owner = *self.inputs.index.member_owners.get(site)?;
        let fact = &self.inputs.facts[owner.0];
        if self.state.current_top_owner != Some(owner)
            || !self.initialized_local_binding(receiver)
            || !self.inputs.index.dynamic_scope_owners.is_empty()
            || !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || !fact.rebinds.eager.is_empty()
            || !fact.global_writes.is_empty()
            || self.inputs.index.opaque_top.contains(&owner)
            || !self.inputs.index.top_property_writes[owner.0].is_empty()
            || !self.inputs.index.top_member_value_writes[owner.0].is_empty()
            || !self.inputs.index.top_callback_result_writes[owner.0].is_empty()
            || !self.inputs.index.top_exported_values[owner.0].is_empty()
        {
            return None;
        }
        let [
            (
                binding,
                ValueExpr::Member(_, Some(initializer_property), initializer_key, initializer_site),
            ),
        ] = self.inputs.index.top_assignments[owner.0].as_slice()
        else {
            return None;
        };
        let [call] = self.inputs.index.top_calls[owner.0].as_slice() else {
            return None;
        };
        if !initializer_key.supports_own_data_read(initializer_property)
            || initializer_property != property
            || *initializer_site != *site
            || !fact.declared.contains(binding)
            || !self.inputs.index.top_definite_initializers[owner.0].contains(binding)
            || call.kind != CallKind::LocalMemberRead
            || call.caller.is_some()
            || CallSite::from(call.span) != *site
        {
            return None;
        }
        let receivers = self.state.world.module.prefix_values.get(receiver)?;
        self.present_own_data_read(receivers, property)
    }

    pub(super) fn present_own_data_read(
        &self,
        receivers: &Values,
        property: &str,
    ) -> Option<OwnDataRead> {
        let slot = own_data_transfer::resolve_present_read(
            self,
            receivers,
            property,
            OwnDataOwnership::CurrentPrefix,
        )
        .ok()?;
        if !slot.is_array_specific()
            && !self
                .inputs
                .index
                .plain_data_objects
                .contains(&slot.object())
            || self
                .state
                .world
                .surface
                .applied_accessors
                .get(&slot.object())
                .is_some_and(|accessors| accessors.contains_key(property))
        {
            return None;
        }
        Some(slot.read(self))
    }

    pub(super) fn local_call_target(&self, targets: &Values) -> Option<(u32, &Callable)> {
        if targets.len() != 1 {
            return None;
        }
        let key = match targets.first()? {
            AbstractValue::Value(Value::Alloc(Kind::Function, key))
            | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => key,
            _ => return None,
        };
        self.inputs
            .index
            .callables
            .get(key)
            .map(|callable| (*key, callable))
    }

    pub(super) fn local_body_identity_for_call(&self, call: &Call) -> Option<LocalBodyIdentity> {
        match call.caller {
            Some(key) => {
                let callable = self.inputs.index.callables.get(&key)?;
                Some(LocalBodyIdentity::Callable {
                    key,
                    owner: callable.owner,
                })
            }
            None => {
                let body = self.inputs.index.ordered_initializers.get(&call.owner)?;
                Some(LocalBodyIdentity::RootInitializer {
                    owner: call.owner,
                    initializer_span: body.root_span()?,
                })
            }
        }
    }

    /// The exact root `new C(...)` of a stable local class whose checked
    /// constructor body the kernel's construct rule can run: no other root
    /// operation and no call in the class body.
    pub(super) fn ordered_root_construction(&mut self, body: &LocalBody, calls: &[Call]) -> bool {
        let Some((callee, span)) = body.single_construction_initializer() else {
            return false;
        };
        let [call] = calls else {
            return false;
        };
        if call.kind != CallKind::New
            || call.span != span
            || call.has_spread
            || !matches!(&call.callee, ValueExpr::Binding(binding) if binding == callee)
        {
            return false;
        }
        let Some(key) = self.stable_local_class_key(callee) else {
            return false;
        };
        let inputs = self.inputs;
        !inputs
            .index
            .retained_constructor_calls
            .contains_key(&CallSite::from(call.span))
            && self
                .eligible_local_constructor(key, call.args.len())
                .is_ok_and(|constructor| {
                    kernel_constructor_body(inputs.index, key, constructor).is_some()
                })
    }

    /// Every call of the root is an exact intact intrinsic, so the ordered
    /// lane runs no application code. Identity is decided here; the ordered
    /// call transfer admits or refuses the operands.
    pub(super) fn ordered_root_calls_are_native(&self, calls: &[Call]) -> bool {
        let mut calls = calls
            .iter()
            .filter(|call| call.kind == CallKind::Call)
            .peekable();
        calls.peek().is_some() && calls.all(|call| self.exact_native_root_call(call))
    }

    fn exact_native_root_call(&self, call: &Call) -> bool {
        call.kind == CallKind::Call
            && !call.has_spread
            && matches!(
                call.callee,
                ValueExpr::Member(_, Some(_), MemberKey::Static, _)
            )
            && matches!(
                self.native_call_identity(CallKind::Call, &call.callee, CallSite::from(call.span)),
                Some(NativeCallIdentity::Proved(..))
            )
    }

    pub(super) fn evaluate_binding(&mut self, id: &Id, env: &Context) -> Values {
        if self.inputs.index.callback_factory_imports.contains(id) {
            return Values::from([AbstractValue::CallbackFactory]);
        }
        if self
            .inputs
            .index
            .inline_callback_factory_imports
            .contains(id)
        {
            return Values::from([AbstractValue::InlineCallbackFactory]);
        }
        if self.inputs.index.imported_bindings.contains(id)
            && let Some(value) = self.inputs.imported_values.get(id.0.as_ref())
        {
            // Preserve the source-proved exported value through the
            // ordinary value transfers, including local copies. Reads
            // and import ordering remain separate statement facts.
            return Values::from([AbstractValue::Value(*value)]);
        }
        if self.inputs.top_bindings.contains(id) {
            if let Some(prefix_values) = self.state.world.module.prefix_values.get(id) {
                let mut values = prefix_values.clone();
                if !self.state.world.module.initialized_bindings.contains(id) {
                    values.insert(if self.inputs.var_bindings.contains(id) {
                        AbstractValue::UNDEFINED
                    } else {
                        AbstractValue::Value(Value::Unavailable)
                    });
                }
                values
            } else if self.inputs.var_bindings.contains(id) {
                Values::from([AbstractValue::UNDEFINED])
            } else {
                // Whole-chunk flow includes future initializers. A
                // missing prefix value for a local binding is TDZ.
                Values::from([AbstractValue::Value(Value::Unavailable)])
            }
        } else if let Some(cell) = self.state.world.active_cells.get(id) {
            self.state.world.cells[cell.0].values.clone()
        } else if let Some(values) = env.get(id) {
            values.clone()
        } else if self
            .inputs
            .index
            .global_object_names
            .contains(id.0.as_ref())
            && !self.inputs.flow.is_declared(id)
            && !self.inputs.shadowed.contains(id.0.as_ref())
        {
            Values::from([AbstractValue::GlobalObject])
        } else {
            let values = from_flow(self.inputs.flow.values_of_binding(id));
            if values.is_empty() { unknown() } else { values }
        }
    }

    pub(super) fn evaluate_new(
        &mut self,
        callee: &ValueExpr,
        args: &[ValueExpr],
        span: CallSite,
        env: &Context,
        captures: &Context,
    ) -> NewEvaluation {
        let mut operation_coverage = None;
        let values = (|| {
            let targets = self.evaluate(callee, env, captures);
            let evaluated_args: Vec<_> = args
                .iter()
                .map(|arg| self.evaluate(arg, env, captures))
                .collect();
            let mut successful_args = evaluated_args.clone();
            let mut args_may_throw = targets.contains(&AbstractValue::Value(Value::Unavailable));
            for values in &mut successful_args {
                args_may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
            }
            if let Some(summary) = self.foreign_call_summary(CallKind::New, callee) {
                if (targets.len() == 1
                    && targets.contains(&AbstractValue::Value(Value::Unavailable)))
                    || evaluated_args.iter().any(|values| {
                        values.len() == 1
                            && values.contains(&AbstractValue::Value(Value::Unavailable))
                    })
                {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                let mut result = self.foreign_call_result(&summary, &successful_args);
                if args_may_throw {
                    result.insert(AbstractValue::Value(Value::Unavailable));
                }
                return result;
            }
            if let Some(returns) =
                self.resolve_native_call(CallKind::New, callee, span, args, &successful_args)
            {
                self.record_native_initializer_transfer(
                    callee,
                    args,
                    &successful_args,
                    args_may_throw,
                    span,
                    &returns,
                );
                if !args_may_throw {
                    operation_coverage = returns.operation_coverage.clone();
                }
                return self.execute_native_resolution(
                    returns,
                    &successful_args,
                    args_may_throw,
                    span,
                );
            }
            // The iterable assertion does not bypass native-row checks:
            // if this exact request and indexed SetIterableProof do not
            // admit the Set New row, retain the conservative value.
            if self
                .inputs
                .set_iterable_proof
                .author_input_for_constructor(span.source_span())
                .is_some()
            {
                return Values::from([
                    AbstractValue::Value(Value::Alloc(Kind::Set, span.allocation_id())),
                    AbstractValue::Value(Value::Unavailable),
                ]);
            }
            if self
                .inputs
                .index
                .retained_constructor_calls
                .contains_key(&span)
            {
                if targets.len() == 1 && targets.contains(&AbstractValue::Value(Value::Unavailable))
                {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                if evaluated_args.iter().any(|values| {
                    values.len() == 1 && values.contains(&AbstractValue::Value(Value::Unavailable))
                }) {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                let mut successful_args = evaluated_args.clone();
                let mut may_throw = false;
                for values in &mut successful_args {
                    may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
                }
                if let Some(callbacks) =
                    self.retained_constructor_callbacks(span, args, &successful_args)
                {
                    let result =
                        self.audited_retained_result(RetainedResultKind::Constructor, callbacks);
                    return if may_throw {
                        Values::from([result, AbstractValue::Value(Value::Unavailable)])
                    } else {
                        Values::from([result])
                    };
                }
            }
            let mut results = Values::new();
            if self.state.current_top_owner.is_some_and(|owner| {
                self.is_proven_map_entry_array(callee, args, env, captures, owner)
            }) {
                let mut can_complete = true;
                let mut may_throw = false;
                let mut successful_args = Values::new();
                for values in &evaluated_args {
                    if values.contains(&AbstractValue::Value(Value::Unavailable)) {
                        may_throw = true;
                        can_complete &= values.len() > 1;
                    }
                    successful_args.extend(
                        values
                            .iter()
                            .filter(|value| **value != AbstractValue::Value(Value::Unavailable))
                            .cloned(),
                    );
                }
                if can_complete {
                    let callbacks = self.callback_values(&successful_args);
                    results.insert(self.deferred_callbacks(callbacks));
                }
                if may_throw || !can_complete {
                    results.insert(AbstractValue::Value(Value::Unavailable));
                }
                return results;
            }
            for target in targets {
                match target {
                    AbstractValue::Value(Value::Alloc(Kind::Class, key)) => {
                        if let Ok((_, instance)) = self.construct_local_class(key, &evaluated_args)
                        {
                            results.insert(instance);
                        } else {
                            // Re-evaluation may lose the private instance proof after
                            // constructor execution taints one of its inputs. Keep the
                            // result opaque, but do not drop callbacks reachable from
                            // the arguments: the unknown instance may retain them.
                            let refused = self.refused_construction_values(&successful_args);
                            results.extend(refused);
                        }
                    }
                    AbstractValue::Value(Value::Unavailable) => {
                        results.insert(AbstractValue::Value(Value::Unavailable));
                    }
                    // `new` of a plain function yields the instance, unknown
                    // here, or an object the body returns. Unknown alone
                    // reaches no unescaped callable: keep what the body can
                    // return and what the arguments can reach.
                    AbstractValue::Value(Value::Alloc(Kind::Function, function))
                    | AbstractValue::Closure(
                        HeapAddress {
                            callable: function, ..
                        },
                        _,
                    ) if self.inputs.index.callables.contains_key(&function) => {
                        let captured = match &target {
                            AbstractValue::Closure(_, captured) => Some(captured.clone()),
                            _ => None,
                        };
                        let returned = self
                            .invoke(function, successful_args.clone(), captured, false)
                            .returns;
                        results.extend(returned.into_iter().filter(|value| !value.is_primitive()));
                        results.extend(self.refused_construction_values(&successful_args));
                    }
                    _ => {
                        results.insert(AbstractValue::Value(Value::Top));
                    }
                }
            }
            if results.is_empty() {
                unknown()
            } else {
                results
            }
        })();
        NewEvaluation {
            values,
            operation_coverage,
        }
    }

    /// Evaluate one source-checked root. A native operation returns its own
    /// effect receipt; the owner later seals that receipt against source identity.
    pub(super) fn evaluate_initializer_transfer(
        &mut self,
        source: &OriginalInitializer,
        expression: &ValueExpr,
        env: &Context,
        captures: &Context,
    ) -> (
        Values,
        super::allocation_transfer::AllocationJudgment,
        Option<NativeConstructorTransfer>,
        Option<EffectCoverageFragment>,
    ) {
        let expression = if let Some(covered) = source.composed_expression() {
            self.cover_source_expression();
            covered
        } else {
            expression
        };
        self.native_initializer_candidate = source.native_constructor_candidate(expression);
        self.native_initializer_transfer = None;
        let (values, operation_coverage) = match expression {
            ValueExpr::New(callee, arguments, site)
                if source.operation_source(expression).is_some() =>
            {
                self.mark_allocation_opaque();
                let result = self.evaluate_new(callee, arguments, *site, env, captures);
                (result.values, result.operation_coverage)
            }
            _ => (self.evaluate_transfer(expression, env, captures), None),
        };
        let transfer = self.native_initializer_transfer.take();
        self.native_initializer_candidate = None;
        (
            values,
            self.allocation_judgment.clone(),
            transfer,
            operation_coverage,
        )
    }

    /// Stop the frame without replaying its earlier operations. An unproved
    /// read, store or call may publish capabilities the remainder can reach,
    /// including objects whose local binding has since been replaced.
    pub(super) fn local_operation_fallback(
        &mut self,
        frame: &LocalFrame,
        mut reached: Values,
        owner: SourceUnitOrdinal,
        reason: ExecutionPrecisionLossReason,
        span: Span,
        outcome: &mut Outcome,
    ) {
        reached.extend(frame.inputs.iter().cloned());
        reached.extend(frame.environment.values().flatten().cloned());
        reached.extend(
            self.state
                .world
                .active_cells
                .values()
                .flat_map(|cell| self.state.world.cells[cell.0].values.iter().cloned()),
        );
        reached.extend(
            frame
                .fresh_records
                .iter()
                .map(|object| AbstractValue::Value(Value::Alloc(Kind::Object, *object))),
        );
        let cells: Vec<_> = self.state.world.active_cells.values().copied().collect();
        self.havoc_cells(cells);
        self.publish_values(
            &reached,
            ClassMembersPublicationWriter::LocalOperationFallback,
            Some(span),
        );
        let mut fallback = self.bounded_invocation_fallback(BoundedInvocationFallbackRequest {
            owner,
            arguments: vec![reached],
            captured: frame.environment.clone(),
            reason,
            reach: HavocReach::Global {
                cause: GlobalReachCause::UnmodelledOperation,
            },
            span,
            diagnostics: BoundedInvocationFallbackDiagnostics::default(),
        });
        outcome.absorb_call_effects(&mut fallback.effects, self.state.assumption.as_ref());
        outcome.executed_owners.extend(fallback.executed_owners);
        outcome.unknown_call |= fallback.unknown_call;
        outcome.inherit_completeness(fallback.complete, fallback.effects.precision_losses);
        outcome.returns.extend(fallback.returns);
        outcome
            .returns
            .insert(AbstractValue::Value(Value::Unavailable));
    }

    pub(super) fn execute_local_body(
        &mut self,
        body: &LocalBody,
        environment: Context,
        parameters: Option<Vec<SelectedValue>>,
        owner: SourceUnitOrdinal,
        activation: LocalActivation,
    ) -> Outcome {
        let scope = activation.scope();
        let mut frame = LocalFrame::new(environment);
        if let Some(parameters) = &parameters {
            frame.inputs.extend(
                parameters
                    .iter()
                    .flat_map(|parameter| parameter.values.iter())
                    .cloned(),
            );
        }
        frame.parameter_inputs = parameters;
        let root_binding_effect_identity = match activation {
            LocalActivation::RootInitializer(identity) => Some(identity),
            LocalActivation::Constructor { class, instance } => {
                construct::bind_receiver(&mut frame, class, instance);
                None
            }
            LocalActivation::Callable(_) | LocalActivation::RootStatement => None,
        };
        frame.borrowed_records = self.reachable_objects(&frame.inputs);
        let mut outcome = Outcome {
            complete: true,
            root_binding_effect_identity,
            ..Outcome::default()
        };
        if let Some(identity) = &outcome.root_binding_effect_identity {
            outcome.effects.binding_effect_coverage =
                BindingEffectCoverage::OrderedRootInitializer {
                    identity: identity.clone(),
                    receipt: None,
                };
        }
        outcome.executed_owners.insert(owner);
        // The source-covered application consumer supplies access effects and
        // conservative fallback. The shared kernel controls operation order.
        let family = driver::drive(
            &mut LocalExecutionContext {
                executor: self,
                outcome: &mut outcome,
                owner,
                scope,
                body,
            },
            body,
            LocalContinuation::new(frame),
        );
        match family {
            Family::One(leaf) => {
                self.adopt_local_leaf(body, *leaf, &mut outcome, owner, scope);
            }
            Family::Forked(leaves) => {
                // Each leaf is adopted in the path state it completed in;
                // the body's caller resumes from their join.
                let mut frames = Vec::with_capacity(leaves.len());
                let mut paths = Vec::with_capacity(leaves.len());
                for (leaf, path) in leaves {
                    let mut context = LocalExecutionContext {
                        executor: self,
                        outcome: &mut outcome,
                        owner,
                        scope,
                        body,
                    };
                    context.enter(path);
                    let frame = self.adopt_local_leaf(body, leaf, &mut outcome, owner, scope);
                    paths.push(self.state.path(&self.allocation_judgment, &outcome));
                    frames.push(frame);
                }
                let JoinedPath { path, refused } = ExecutionPath::join(paths);
                let mut context = LocalExecutionContext {
                    executor: self,
                    outcome: &mut outcome,
                    owner,
                    scope,
                    body,
                };
                context.enter(path);
                if refused.is_some() {
                    // The leaves left worlds no single caller can resume.
                    // Everything any leaf could reach is summarized once
                    // against the pointwise join.
                    let span = self.local_body_span(body, scope);
                    LocalExecutionContext {
                        executor: self,
                        outcome: &mut outcome,
                        owner,
                        scope,
                        body,
                    }
                    .fallback(
                        &join_frames(frames),
                        Values::new(),
                        ExecutionPrecisionLossReason::UnsupportedOperation {
                            operation: ExecutionOperation::OpaqueExecution,
                            ordered_field_read_refusal: None,
                            ordered_call_admission_refusal: None,
                        },
                        span,
                    );
                    outcome.effect_coverage = EffectCoverageFragment::default();
                    outcome.return_selection = None;
                }
            }
        }
        outcome
    }

    fn local_body_span(&self, body: &LocalBody, scope: LocalExecutionScope) -> Span {
        match scope {
            LocalExecutionScope::Callable(key) => self
                .inputs
                .index
                .callable_spans
                .get(&key)
                .copied()
                .unwrap_or(DUMMY_SP),
            LocalExecutionScope::Root => body.root_span().unwrap_or(DUMMY_SP),
            LocalExecutionScope::Constructor(class) => self
                .inputs
                .index
                .class_spans
                .get(&class)
                .copied()
                .unwrap_or(DUMMY_SP),
        }
    }

    /// Record one completed path of a body in the live path state: the cells
    /// it touched, the values it threw, the summary of a refused remainder
    /// and its results. Returns the leaf's frame.
    fn adopt_local_leaf(
        &mut self,
        body: &LocalBody,
        leaf: Leaf,
        outcome: &mut Outcome,
        owner: SourceUnitOrdinal,
        scope: LocalExecutionScope,
    ) -> LocalFrame {
        let Leaf { frame, attempt } = leaf;
        // The kernel returns the cells it touched and the values it threw;
        // this scope records them as the owner's effects and publications.
        let non_assumed = self.state.assumption.is_none();
        for cell in frame.ledger.heap_accesses.reads.iter().cloned() {
            outcome.effects.record_heap_read(cell, non_assumed);
        }
        for cell in frame.ledger.heap_accesses.writes.iter().cloned() {
            outcome.effects.record_heap_write(cell, non_assumed);
        }
        for thrown in frame.ledger.completion.exact_abrupt() {
            self.publish_values(
                &thrown.values,
                ClassMembersPublicationWriter::ThrowValuePublication,
                None,
            );
        }
        match attempt {
            LocalExecutionAttempt::Completed(result) => {
                outcome.effect_coverage = result.ledger.effects;
            }
            LocalExecutionAttempt::Refused(RemainderDisposition::AlreadySummarized) => {}
            LocalExecutionAttempt::Refused(RemainderDisposition::NeedsSummary) => {
                // The kernel stopped at an operation it cannot represent and no
                // adapter path summarized the remainder on the way out.
                let span = self.local_body_span(body, scope);
                LocalExecutionContext {
                    executor: self,
                    outcome,
                    owner,
                    scope,
                    body,
                }
                .fallback(
                    &frame,
                    Values::new(),
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: ExecutionOperation::OpaqueExecution,
                        ordered_field_read_refusal: None,
                        ordered_call_admission_refusal: None,
                    },
                    span,
                );
            }
        }
        let returns = frame.returns();
        // A callable's caller cannot track a returned map; a root
        // initializer's completion value is the declared binding's, whose
        // map identity the prefix keeps.
        if let LocalExecutionScope::Callable(_) = scope {
            for value in &returns {
                if let AbstractValue::OwnDescriptorMap(site) = value {
                    self.invalidate_own_descriptor_map(*site);
                }
            }
        }
        outcome.returns.extend(returns);
        outcome.return_selection = frame
            .return_selection()
            .filter(|selection| selection.matches(&outcome.returns));
        frame
    }

    /// A source-summary lane flattens control flow, and a value may leave
    /// a set the lane tracks only once it is published: reading unknown
    /// then reaches it through the escaped-callback pool. A local another
    /// callable writes, one an assignment reads before a later assignment,
    /// or a loop-head binding reads as unknown in addition to the lane's own
    /// assignments; stores to `untracked_locals` and iterated values publish.
    /// Only widens an entry the environment or an assignment gives. Kept out
    /// of line for `invoke`'s frame size.
    #[inline(never)]
    fn widen_untracked_locals(&self, callable: &Callable, env: &mut Context) {
        for (id, _) in &callable.local_assignments {
            if self.inputs.stale_locals.contains(id) || callable.head_bindings.contains(id) {
                env.entry(id.clone()).or_default().extend(unknown());
            }
        }
        let stale: Vec<Id> = env
            .keys()
            .filter(|id| {
                self.inputs.stale_locals.contains(*id) || callable.head_bindings.contains(*id)
            })
            .cloned()
            .collect();
        for id in stale {
            env.entry(id).or_default().extend(unknown());
        }
    }

    /// A class value created now: its members read the enclosing scope's
    /// bindings as unknown later, so publish what those hold here. Its
    /// superclass can also be called after this environment has gone away.
    pub(super) fn escape_class_captures(&mut self, class: u32, env: &Context, captures: &Context) {
        let mut values: Values = self
            .inputs
            .class_captured_reads
            .get(&class)
            .into_iter()
            .flatten()
            .filter(|binding| !self.inputs.top_bindings.contains(*binding))
            .flat_map(|binding| env.get(binding).or_else(|| captures.get(binding)))
            .flatten()
            .cloned()
            .collect();

        let facts = self.inputs.index.class_values.get(&class);
        match facts.and_then(|facts| facts.super_class.as_ref()) {
            Some(ValueExpr::Binding(binding))
                if facts.is_some_and(|facts| facts.lexical_heritage.is_some()) =>
            {
                if !self.inputs.top_bindings.contains(binding) {
                    values.extend(
                        env.get(binding)
                            .or_else(|| captures.get(binding))
                            .into_iter()
                            .flatten()
                            .cloned(),
                    );
                }
            }
            None => {}
            Some(heritage) => {
                // The eventual base retains none of this environment. Publish
                // its inputs without evaluating the expression again: a
                // heritage factory may have effects already accounted for by
                // the flattened call pass.
                let mut inputs = facts
                    .map(|facts| {
                        facts
                            .discarded_heritage_call
                            .as_ref()
                            .filter(|call| {
                                call.argument_reads.iter().all(|binding| {
                                    self.inputs.flow.is_declared(binding)
                                        && (self.inputs.top_bindings.contains(binding)
                                            || env.contains_key(binding)
                                            || captures.contains_key(binding))
                                }) && self
                                    .inputs
                                    .index
                                    .inert_heritage_callee(&call.callee, self.inputs.flow)
                                    && self
                                        .inputs
                                        .index
                                        .stable_heritage_function(&call.base, self.inputs.flow)
                                        .is_some()
                            })
                            .map_or_else(
                                || facts.super_class_inputs.clone(),
                                |call| call.remaining_inputs.clone(),
                            )
                    })
                    .unwrap_or_default();
                heritage.bindings(&mut inputs);
                let mut incomplete = heritage.bindings_may_be_incomplete()
                    || facts.is_some_and(|facts| facts.super_class_inputs_incomplete);
                for binding in inputs {
                    let possible = if self.inputs.top_bindings.contains(&binding) {
                        self.state.world.module.prefix_values.get(&binding)
                    } else {
                        env.get(&binding).or_else(|| captures.get(&binding))
                    };
                    if let Some(possible) = possible {
                        values.extend(possible.iter().cloned());
                    } else {
                        incomplete = true;
                    }
                }
                if incomplete {
                    // An untracked local factory or lowered-away input can
                    // return a base reaching any of these capabilities.
                    values.extend(
                        env.iter()
                            .chain(captures)
                            .filter(|(binding, _)| !self.inputs.top_bindings.contains(*binding))
                            .flat_map(|(_, possible)| possible.iter().cloned()),
                    );
                }
                if incomplete && let Some(owner) = self.inputs.index.class_owners.get(&class) {
                    let reads = &self.inputs.facts[owner.0].reads;
                    values.extend(
                        reads
                            .eager
                            .iter()
                            .chain(&reads.lazy)
                            .filter_map(|binding| {
                                self.state.world.module.prefix_values.get(binding)
                            })
                            .flatten()
                            .cloned(),
                    );
                }
            }
        }
        if !values.is_empty() {
            self.escape_untracked_values(&values);
        }
    }

    /// Values flowing where a later read sees only unknown. Unknown reaches
    /// only published callbacks, so a value may leave a tracked set only once
    /// what it can reach is published.
    pub(super) fn escape_untracked_values(&mut self, values: &Values) {
        self.publish_values(
            values,
            ClassMembersPublicationWriter::UntrackedLocalStore,
            None,
        );
    }

    /// A parameter's incoming value, or a captured value, enters the
    /// environment without a store; publish it where the binding is
    /// untracked, as a store would. A pattern parameter's bindings have no
    /// environment entry: an untracked one reads unknown, and the whole
    /// argument is published.
    #[inline(never)]
    fn escape_untracked_environment(
        &mut self,
        callable: &Callable,
        reads_arguments: bool,
        args: &[Values],
        env: &mut Context,
    ) {
        for (id, values) in env.iter() {
            if self.inputs.untracked_locals.contains(id) {
                self.escape_untracked_values(values);
            }
        }
        // `arguments` reads every incoming value as unknown, also from an
        // arrow nested in the callable, whose `arguments` is this one's.
        if reads_arguments {
            for values in args {
                self.escape_untracked_values(values);
            }
        }
        for (index, parameter) in callable.params.iter().enumerate() {
            if parameter.binding.is_some() {
                continue;
            }
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED]));
            if let Some(default) = &parameter.default
                && may_be_undefined(&values)
            {
                values.remove(&AbstractValue::UNDEFINED);
                values.extend(self.evaluate(default, env, env));
            }
            // Bind each pattern binding to its projection of the incoming
            // value. A part the pattern or the projection loses to unknown
            // is published with the whole value.
            env.insert(pattern_source(), values.clone());
            let mut loses_values = parameter.pattern_loses_values;
            for (binding, projection) in &parameter.pattern_assignments {
                let projected = self.evaluate(projection, env, env);
                loses_values |= projected.contains(&AbstractValue::Value(Value::Top));
                env.entry(binding.clone()).or_default().extend(projected);
            }
            env.remove(&pattern_source());
            if loses_values {
                self.escape_untracked_values(&values);
            }
            let untracked: Vec<&Id> = parameter
                .bindings
                .iter()
                .filter(|binding| self.inputs.untracked_locals.contains(*binding))
                .collect();
            if !untracked.is_empty() {
                self.escape_untracked_values(&values);
                for binding in untracked {
                    env.entry(binding.clone()).or_default().extend(unknown());
                }
            }
        }
    }

    /// Binding an array-destructuring parameter runs the iterator protocol
    /// on its incoming value, the default included.
    #[inline(never)]
    fn iterate_parameters(
        &mut self,
        callable: &Callable,
        args: &[Values],
        env: &Context,
        outcome: &mut Outcome,
    ) {
        for (index, parameter) in callable.params.iter().enumerate() {
            if !parameter.pattern_iterates {
                continue;
            }
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED]));
            if let Some(default) = &parameter.default
                && may_be_undefined(&values)
            {
                values.remove(&AbstractValue::UNDEFINED);
                values.extend(self.evaluate(default, env, env));
            }
            self.run_iterator_protocol(&values, callable.owner, outcome);
            if parameter.pattern_iterates_part {
                self.run_iterator_protocol(&unknown(), callable.owner, outcome);
            }
            for default in &parameter.iterated_defaults {
                let values = self.evaluate(default, env, env);
                self.run_iterator_protocol(&values, callable.owner, outcome);
            }
        }
    }

    /// The iterator protocol a top-level owner runs on its
    /// `top_iterated_sources`.
    pub(super) fn iterate_top_sources(
        &mut self,
        owner: SourceUnitOrdinal,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) {
        let Some(sources) = self.inputs.index.top_iterated_sources.get(&owner) else {
            return;
        };
        for source in sources {
            let values = self.evaluate(source, env, captures);
            self.run_iterator_protocol(&values, owner, outcome);
        }
    }

    /// Calling generator `key` creates an opaque generator object. Its body
    /// runs when some later code resumes the object (`next()`, `for`-`of`,
    /// spread, destructuring): the body is a published callback, so each
    /// resumption, an unknown call, promotes the body's effects to the
    /// resuming owner. What the body can reach (the arguments and captures,
    /// which the frame's environment holds) is published with it.
    #[inline(never)]
    fn create_generator(
        &mut self,
        key: u32,
        args: &[Values],
        env: &Context,
        outcome: &mut Outcome,
    ) {
        let reached: Values = args.iter().chain(env.values()).flatten().cloned().collect();
        self.publish_values(
            &reached,
            ClassMembersPublicationWriter::GeneratorCreation,
            None,
        );
        let body = self.deferred_callbacks(Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Function,
            key,
        ))]));
        self.insert_escaped_callback_values(
            &Values::from([body]),
            ClassMembersPublicationWriter::GeneratorCreation,
            None,
            None,
        );
        outcome.returns.insert(AbstractValue::Value(Value::Top));
    }

    /// The iterator protocol a callable's body runs on its
    /// `iterated_sources`.
    #[inline(never)]
    fn iterate_callable_sources(
        &mut self,
        callable: &Callable,
        env: &Context,
        outcome: &mut Outcome,
    ) {
        for iterated in &callable.iterated_sources {
            let values = self.evaluate(iterated, env, env);
            self.run_iterator_protocol(&values, callable.owner, outcome);
        }
    }

    /// Iterating `values` calls their `[Symbol.iterator]` and the iterator's
    /// `next`. Over anything but a proven native iterable those are unknown
    /// callees: the iterated value is published and the operation may run
    /// any published callback (a generator body, a hand-written `next`),
    /// charged to the operation's owner. A primitive's method comes from its
    /// prototype, where only a published function can have been installed:
    /// the operation may run the published callbacks, and nothing else.
    #[inline(never)]
    fn run_iterator_protocol(
        &mut self,
        values: &Values,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        match self.iteration_proof(values) {
            IterationProof::Native => {}
            IterationProof::PrototypeLookup => {
                self.invoke_possible_callbacks_at(&[], false, None, outcome);
            }
            IterationProof::Unknown => {
                outcome.lose_call_target(owner, DUMMY_SP);
                outcome.unknown_call = true;
                self.invoke_possible_callbacks_at(
                    std::slice::from_ref(values),
                    false,
                    None,
                    outcome,
                );
            }
        }
    }

    /// Which user code iterating `values` can run. `Native`: none, for
    /// `null`/`undefined` (which throw), a throwing read, or an array
    /// allocation no opaque code reached under intact Array intrinsics.
    /// `PrototypeLookup`: what another primitive's prototype holds.
    fn iteration_proof(&mut self, values: &Values) -> IterationProof {
        if values.is_empty() {
            return IterationProof::Unknown;
        }
        let mut arrays = false;
        let mut primitives = false;
        for value in values {
            match value {
                AbstractValue::Value(Value::Prim(kinds))
                    if kinds.is_only(PrimKinds::UNDEFINED.union(PrimKinds::NULL)) => {}
                AbstractValue::Value(Value::Prim(_)) => primitives = true,
                AbstractValue::Value(Value::Unavailable) => {}
                AbstractValue::Value(Value::Alloc(kind, object))
                    if (*kind == Kind::Array
                        || (*kind == Kind::Object
                            && self.inputs.index.array_elements.contains_key(object)))
                        && !self.state.world.surface.tainted_objects.contains(object) =>
                {
                    arrays = true;
                }
                _ => return IterationProof::Unknown,
            }
        }
        if arrays && !self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS) {
            return IterationProof::Unknown;
        }
        if primitives {
            IterationProof::PrototypeLookup
        } else {
            IterationProof::Native
        }
    }

    /// A top-level owner's values whose parts reach a binding only as
    /// unknown, published like a callable's `published_sources`.
    pub(super) fn publish_top_sources(
        &mut self,
        owner: SourceUnitOrdinal,
        env: &Context,
        captures: &Context,
    ) {
        let Some(sources) = self.inputs.index.top_published_sources.get(&owner) else {
            return;
        };
        for source in sources {
            let values = self.evaluate(source, env, captures);
            self.escape_untracked_values(&values);
        }
    }

    /// Values whose parts reach a binding only as unknown (an iterated
    /// value's elements, a destructured value's parts, a thrown or yielded
    /// value); publish what they can reach.
    #[inline(never)]
    fn publish_sources(&mut self, callable: &Callable, env: &Context) {
        for iterated in &callable.published_sources {
            let values = self.evaluate(iterated, env, env);
            self.escape_untracked_values(&values);
        }
    }

    pub(super) fn invoke(
        &mut self,
        key: u32,
        args: Vec<Values>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        let argument_selections = vec![None; args.len()];
        self.invoke_selected(
            key,
            args,
            argument_selections,
            captured,
            fresh_checked_constructor,
        )
    }

    fn invoke_selected(
        &mut self,
        key: u32,
        args: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        // The invocation's outcome (and its memo) records provenance relative
        // to the invocation; the caller's merge applies its own assumption.
        let assumption = self.state.assumption.take();
        let result = self
            .request_transfer(
                RuntimeOperation::Invoke {
                    callable: key,
                    arguments: args,
                    argument_selections,
                    captured,
                    fresh_checked_constructor,
                },
                assumption.clone(),
            )
            .0;
        self.state.assumption = assumption;
        let RuntimeValue::Outcome(outcome) = result else {
            unreachable!("invocation transfers produce outcomes");
        };
        *outcome
    }

    pub(super) fn invoke_transfer(
        &mut self,
        key: u32,
        args: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        let outer_cells = std::mem::replace(
            &mut self.state.world_mut().active_cells,
            captured
                .as_ref()
                .map(|context| context.values.cells.clone())
                .unwrap_or_default(),
        );
        let mut outcome = self.invoke_with_cells(
            key,
            args,
            argument_selections,
            captured,
            fresh_checked_constructor,
        );
        self.record_callable_heap_contents(key, &mut outcome);
        if self.callable_has_no_normal_return(key)
            && outcome
                .returns
                .iter()
                .any(|value| *value != AbstractValue::Value(Value::Unavailable))
        {
            // An opaque nested operation can carry values for effect reach.
            // They are not successful returns of a source throw-only body.
            // Preserve their publication while retaining the abrupt result.
            let reached = std::mem::take(&mut outcome.returns);
            self.publish_values(
                &reached,
                ClassMembersPublicationWriter::ThrowValuePublication,
                None,
            );
            outcome
                .returns
                .insert(AbstractValue::Value(Value::Unavailable));
        }
        if !outcome.complete || outcome.unknown_call {
            let cells: Vec<_> = self.state.world.active_cells.values().copied().collect();
            self.havoc_cells(cells);
        }
        self.state.world_mut().active_cells = outer_cells;
        outcome
    }

    pub(super) fn callable_has_no_normal_return(&self, key: u32) -> bool {
        self.inputs
            .index
            .callables
            .get(&key)
            .is_some_and(|callable| {
                !callable.generator
                    && !self.inputs.index.async_callables.contains(&key)
                    && !callable.may_fall_through
                    && callable.body_facts.value_flow().returns().is_empty()
            })
    }

    fn invoke_with_cells(
        &mut self,
        key: u32,
        args: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        let Some((callable_owner, uses_arguments)) =
            self.inputs.index.callables.get(&key).map(|callable| {
                (
                    callable.owner,
                    callable.reads.iter().any(|id| id.0.as_ref() == "arguments"),
                )
            })
        else {
            return Outcome::default();
        };
        let unsupported_inputs = uses_arguments
            || self.inputs.index.callables[&key]
                .params
                .iter()
                .any(|parameter| parameter.binding.is_none());
        // Only ordinary calls with no live captures can acquire parameter
        // receipts. Other invocation kinds retain their compatibility path.
        let callable = &self.inputs.index.callables[&key];
        let covered_prologue = !uses_arguments
            && !callable.implicit_capture
            && !callable.generator
            && !callable.opaque
            && !self.inputs.index.async_callables.contains(&key)
            && !fresh_checked_constructor
            && captured
                .as_ref()
                .is_none_or(|context| context.values.is_empty());
        // Implicit arguments can alias any actual input even without a
        // corresponding formal parameter. Invalidate input capabilities before
        // consulting cached outcomes, including calls outside array iteration.
        if unsupported_inputs {
            for values in &args {
                self.taint_objects(values);
            }
            if self.state.array_iteration_depth > 0 {
                self.state.array_iteration_unsupported = true;
            }
        }
        let captured_context =
            self.materialize_captures(captured.as_ref().map(|context| context.values.as_ref()));
        self.record_executed_owner(callable_owner);
        self.state.statement_invoked_callables.insert(key);
        // Memoized outcomes still represent an invocation at this point.
        self.activate_native_effects(callable_owner, Some(key));
        let signature = InvocationSignature {
            callable: key,
            arguments: args.clone(),
            argument_selections: argument_selections.clone(),
            captured_context: captured.clone(),
            fresh_checked_constructor,
            reachability_revision: self.state.world.revision.reachability,
            prefix_revision: self.state.world.revision.prefix,
            top_owner: self.state.current_top_owner,
        };
        if self.state.array_iteration_depth == 0
            && !self.state.world.native.tainted_functions.contains(&key)
            && let Some(point) = self.state.invocation_memos.get(&signature)
        {
            let outcome = self.queries.invocation_memo(point);
            self.evaluation_effects
                .extend(&outcome.effects.evaluation_effects);
            return outcome;
        }
        let active_invocation = ActiveInvocation {
            callable: key,
            top_owner: self.state.current_top_owner,
        };
        if self.state.active.contains(&active_invocation)
            || self.state.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            || self.state.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS
        {
            let reason = if self.state.active.contains(&active_invocation) {
                ExecutionPrecisionLossReason::RecursiveInvocation
            } else if self.state.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH {
                ExecutionPrecisionLossReason::InvocationDepthLimit {
                    limit: MAX_SYNCHRONOUS_INVOCATION_DEPTH,
                }
            } else {
                ExecutionPrecisionLossReason::InvocationExpansionLimit {
                    limit: MAX_SYNCHRONOUS_INVOCATIONS,
                }
            };
            let reach = self.invocation_reach(key, callable_owner, &args, &captured_context);
            return self.bounded_invocation_fallback(BoundedInvocationFallbackRequest {
                owner: callable_owner,
                arguments: args,
                captured: captured_context,
                reason,
                reach,
                span: DUMMY_SP,
                diagnostics: BoundedInvocationFallbackDiagnostics::default(),
            });
        }
        self.state.active.insert(active_invocation);
        self.state.invocation_depth += 1;
        self.state.invocation_count += 1;
        let Some(callable) = self.inputs.index.callables.get(&key) else {
            self.state.active.remove(&active_invocation);
            self.state.invocation_depth -= 1;
            return Outcome::default();
        };
        let mut env = captured_context.clone();
        // A callable reading `arguments` (itself or through a nested arrow)
        // runs on the source-summary lane, which publishes its arguments.
        let reads_arguments = self.inputs.reads_arguments.contains(&key);
        let ordered_prologue = covered_prologue
            && !unsupported_inputs
            && !reads_arguments
            && self.state.array_iteration_depth == 0
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && callable.member_value_writes.is_empty()
            && callable.body_facts.ordered_plan().is_some_and(|body| {
                !body.requires_keyed_collection_new()
                    && body
                        .captured_writes()
                        .iter()
                        .all(|binding| self.state.world.active_cells.contains_key(binding))
                    && body.free_reads().iter().all(|binding| {
                        env.contains_key(binding)
                            || self.inputs.top_bindings.contains(binding)
                            || self.inputs.imported_primitive_read(binding).is_some()
                            || self.inputs.index.foreign_call_imports.contains_key(binding)
                            || self.substitutes_factory_calls(&callable.calls, binding)
                            || (!self.inputs.flow.is_declared(binding)
                                && !self.inputs.shadowed.contains(binding.0.as_ref())
                                && NativeIntrinsic::for_global_binding(binding.0.as_ref())
                                    .is_some())
                    })
            });
        for (index, param) in callable
            .params
            .iter()
            .enumerate()
            .filter(|_| !ordered_prologue)
        {
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED]));
            if let Some(default) = &param.default
                && may_be_undefined(&values)
            {
                values.remove(&AbstractValue::UNDEFINED);
                values.extend(self.evaluate(default, &env, &env));
            }
            if let Some(binding) = &param.binding {
                env.insert(binding.clone(), values);
            }
        }
        let unsupported_before = self.state.array_iteration_unsupported;
        if self.state.array_iteration_depth > 0 {
            if callable.reads.iter().any(|id| id.0.as_ref() == "arguments") {
                self.state.array_iteration_unsupported = true;
            }
            if let Some(reads) = self.inputs.index.callable_member_reads.get(&key) {
                for (receiver, property) in reads.clone() {
                    let values = self.evaluate(&receiver, &env, &env);
                    if property.is_none()
                        || values.is_empty()
                        || values.iter().any(|value| match value {
                            AbstractValue::Value(Value::Prim(_)) => false,
                            AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                                (!self.inputs.index.plain_data_objects.contains(object)
                                    && !self
                                        .state
                                        .world
                                        .surface
                                        .ordinary_spread_copies
                                        .contains(object)
                                    && !self.state.world.arrays.dense.contains_key(object))
                                    || !self.state.world.object_properties_are_settled(*object)
                                    || !self
                                        .state
                                        .world
                                        .surface
                                        .prefix_object_properties
                                        .get(object)
                                        .is_some_and(|properties| {
                                            properties.contains_key(&property)
                                        })
                            }
                            AbstractValue::Instance(key, _, InstanceOrigin::Constructed) => {
                                self.class_may_run_on_borrowed_receiver(*key)
                                    || !self.known_local_instance_member(*key, property.as_deref())
                            }
                            _ => true,
                        })
                    {
                        self.state.array_iteration_unsupported = true;
                    }
                }
            }
        }
        // CLEANUP(added 2026-11-09): the source-summary lane after this block
        // is the flat lane for bodies that have an ordered plan. Each gate
        // below routes such a body there until the kernel has the rule named;
        // remove the gate with that rule, and the lane with the last gate.
        // - `requires_keyed_collection_new`: an application keyed-collection
        //   lane.
        // - captured writes to bindings without an active cell: a kernel
        //   store to a module binding that records the rebind
        //   (`Unproved::CaptureStore` summarizes the remainder today).
        // - free reads outside module, summarized-import, imported-primitive
        //   and intact-native bindings: global-object reads, and reads of a
        //   callback-factory import the ordered lane does not substitute,
        //   with their global-cell provenance.
        // - `established_writes`: member-write proofs (fresh local records,
        //   borrowed parameters, array iteration) in the kernel's own-data
        //   store.
        if !unsupported_inputs
            && !reads_arguments
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && let Some(body) = callable.body_facts.ordered_plan()
            // A default-bearing plan must execute its prologue in the shared
            // kernel. Pre-evaluating its value would omit the default's calls.
            && (ordered_prologue || body.parameters.iter().all(|parameter| parameter.default.is_none()))
            && !body.requires_keyed_collection_new()
            && body
                .captured_writes()
                .iter()
                .all(|binding| self.state.world.active_cells.contains_key(binding))
            && body.free_reads().iter().all(|binding| {
                env.contains_key(binding)
                    || self.inputs.top_bindings.contains(binding)
                    || self.inputs.imported_primitive_read(binding).is_some()
                    || self.inputs.index.foreign_call_imports.contains_key(binding)
                    || self.substitutes_factory_calls(&callable.calls, binding)
                    || (!self.inputs.flow.is_declared(binding)
                        && !self.inputs.shadowed.contains(binding.0.as_ref())
                        && NativeIntrinsic::for_global_binding(binding.0.as_ref()).is_some_and(
                            |intrinsic| {
                                self.native_guard_intact(
                                    NativeIntegrityGuard::NativeCallIdentity,
                                    &[IntrinsicSlot::Global(intrinsic)],
                                )
                            },
                        ))
            })
        {
            // Keep existing record/input and array-iteration write proofs on
            // their established path until they consume ordered operations.
            // An unrebound parameter remains a borrowed receiver. Reads use
            // the existing application proofs when the fresh-record check fails.
            let established_writes =
                !callable.member_value_writes.is_empty()
                    && (self.state.array_iteration_depth > 0
                        || self.fresh_local_record_receiver(key, callable).is_some()
                        || callable.member_value_writes.iter().any(|write| {
                            callable.params.iter().any(|parameter| {
                                parameter.binding.as_ref() == Some(&write.receiver)
                            }) && !callable
                                .local_assignments
                                .iter()
                                .any(|(binding, _)| binding == &write.receiver)
                        }));
            if !established_writes {
                let mut outcome = self.execute_local_body(
                    body,
                    env,
                    ordered_prologue.then(|| {
                        args.iter()
                            .cloned()
                            .zip(argument_selections.iter().cloned())
                            .map(|(values, selection)| SelectedValue { values, selection })
                            .collect()
                    }),
                    callable.owner,
                    LocalActivation::Callable(key),
                );
                if !covered_prologue || (!callable.params.is_empty() && !ordered_prologue) {
                    outcome.effect_coverage = EffectCoverageFragment::default();
                }
                outcome.body_invoked = true;
                self.state.active.remove(&active_invocation);
                self.state.invocation_depth -= 1;
                self.remember_invocation(signature, &outcome);
                return outcome;
            }
        }
        // Source-summary lanes pre-evaluate assignments and flatten control flow.
        // They cannot preserve a live mutable capture's ordered value proof.
        let cells: Vec<_> = self.state.world.active_cells.values().copied().collect();
        self.havoc_cells(cells);
        for (binding, cell) in &self.state.world.active_cells {
            env.insert(
                binding.clone(),
                self.state.world.cells[cell.0].values.clone(),
            );
        }
        seed_undefined_before_assignment(
            &mut env,
            &callable.undefined_before_assignment,
            &callable.local_assignments,
        );
        self.widen_untracked_locals(callable, &mut env);
        self.escape_untracked_environment(callable, reads_arguments, &args, &mut env);
        for (id, value) in &callable.local_assignments {
            let values = self.evaluate(value, &env, &env);
            // A chunk binding read later in this prefix must see the assigned
            // callable, not only the opaque value `apply_rebind_effects` adds:
            // calling that opaque value cannot reach an unescaped closure.
            // Like a top-level owner, publish possible values before the body's
            // calls; a lexical binding still in TDZ throws instead of storing.
            if self.inputs.top_bindings.contains(id)
                && (self.inputs.var_bindings.contains(id)
                    || self.state.world.module.initialized_bindings.contains(id))
            {
                let possible = self
                    .state
                    .world_mut()
                    .module
                    .prefix_values
                    .entry_or_default(id);
                let before = possible.len();
                possible.extend(values.iter().cloned());
                if possible.len() != before {
                    self.state.world_mut().revision.bump_prefix();
                }
            }
            if self.inputs.untracked_locals.contains(id) {
                self.escape_untracked_values(&values);
            }
            env.entry(id.clone()).or_default().extend(values);
        }
        self.publish_sources(callable, &env);
        let mut outcome = Outcome {
            complete: true,
            ..Outcome::default()
        };
        outcome.executed_owners.insert(callable.owner);
        self.iterate_parameters(callable, &args, &env, &mut outcome);
        if callable.generator {
            // Generator parameter initializers run at call time, although
            // the body waits for the first iteration. Their effects are not
            // separated in the index, so retain the conservative path.
            outcome.unsupported(
                ExecutionOperation::GeneratorParameters,
                callable.owner,
                DUMMY_SP,
            );
            self.create_generator(key, &args, &env, &mut outcome);
            self.state.active.remove(&active_invocation);
            self.state.invocation_depth -= 1;
            self.remember_invocation(signature, &outcome);
            return outcome;
        }
        outcome.body_invoked = true;
        if callable.opaque {
            outcome.unsupported(
                ExecutionOperation::OpaqueExecution,
                callable.owner,
                DUMMY_SP,
            );
        }
        for id in &callable.reads {
            if self.inputs.top_bindings.contains(id) {
                outcome.effects.reads.insert((id.clone(), callable.owner));
                if !self.state.world.module.initialized_bindings.contains(id) {
                    outcome.effects.uninitialized_reads.insert(id.clone());
                }
            }
        }
        for id in &callable.rebinds {
            if self.inputs.top_bindings.contains(id) {
                outcome.effects.rebinds.insert((id.clone(), callable.owner));
            }
        }
        // The visitor stores value-bearing member writes separately from call
        // nodes. Make same-owner callback publication visible before nested
        // opaque calls; if source order put a write afterward, this only adds
        // a conservative callback possibility.
        let fresh_data_writes = self.fresh_data_writes(callable, &env);
        if let Some(objects) = &fresh_data_writes {
            self.state
                .world_mut()
                .publication
                .parameter_initialized_objects
                .extend(objects.iter().copied());
        }
        let mut certified_data_receivers = self.fresh_primitive_write_receivers(callable, &env);
        let fresh_record_receiver = self.fresh_local_record_receiver(key, callable);
        if let Some(object) = fresh_record_receiver {
            certified_data_receivers.insert(object);
        }
        self.apply_member_value_writes(
            &callable.member_value_writes,
            &env,
            &env,
            &certified_data_receivers,
            false,
        );
        self.publish_destructuring_target_receivers(
            &callable.destructuring_member_receivers,
            &env,
            &env,
        );
        // local_constructor checked every assignment as a static property
        // write to a fresh `this`, with a safe right-hand value and no
        // prototype getter collision. That receiver cannot publish a stored
        // callback or invoke a setter. The generic lanes represent `this` as
        // unknown and would otherwise promote lazy callback bodies after any
        // earlier opaque call.
        if !fresh_checked_constructor {
            self.promote_callback_result_writes(
                &callable.callback_result_writes,
                &env,
                &env,
                callable.owner,
                &mut outcome,
                &certified_data_receivers,
            );
        }
        for call in &callable.calls {
            if !self.execute_call(call, &env, &env, &mut outcome) {
                // Local initializers are pre-evaluated for capability reach.
                // A skipped operation may precede their real evaluation and
                // change even a primitive member value into an object.
                // Preserve retained capabilities, but forfeit exact aliases.
                let mut widened = false;
                for (binding, _) in &callable.local_assignments {
                    widened |= env
                        .entry(binding.clone())
                        .or_default()
                        .insert(AbstractValue::Value(Value::Top));
                    if self.inputs.top_bindings.contains(binding)
                        && let Some(values) =
                            self.state.world_mut().module.prefix_values.get_mut(binding)
                        && values.insert(AbstractValue::Value(Value::Top))
                    {
                        widened = true;
                    }
                }
                if widened {
                    self.state.world_mut().revision.bump_prefix();
                }
            }
        }
        // The lane is flow-insensitive: iterate after the body's writes and
        // calls, so the published pool holds what they install.
        self.iterate_callable_sources(callable, &env, &mut outcome);
        if !certified_data_receivers.is_empty()
            && (self.state.array_iteration_unsupported
                || !self.array_intrinsics_intact()
                || certified_data_receivers
                    .iter()
                    .any(|object| !self.state.world.object_properties_are_settled(*object)))
        {
            self.state
                .world_mut()
                .surface
                .tainted_objects
                .extend(certified_data_receivers.iter().copied());
            certified_data_receivers.clear();
        }
        for expr in callable.body_facts.value_flow().returns() {
            let returned = self.evaluate(expr, &env, &env);
            for value in &returned {
                if let AbstractValue::OwnDescriptorMap(site) = value {
                    self.invalidate_own_descriptor_map(*site);
                }
            }
            outcome.returns.extend(returned);
        }
        if callable.may_fall_through {
            outcome.returns.insert(AbstractValue::UNDEFINED);
        }

        let mut writes: Vec<_> = callable
            .member_writes
            .iter()
            .filter(|(receiver, _)| !self.stable_fresh_function_target(key, callable, receiver))
            .map(|(receiver, property)| (Some(receiver.clone()), Some(property.clone())))
            .collect();
        if callable.dynamic_property_effect {
            writes.push((None, None));
        }
        if !fresh_checked_constructor
            && fresh_data_writes.is_none()
            && certified_data_receivers.is_empty()
        {
            self.promote_property_writes(&writes, callable.owner, &mut outcome);
        }
        if unsupported_inputs
            || (self.state.array_iteration_depth > 0
                && self.state.array_iteration_unsupported
                && !unsupported_before)
        {
            let inputs = self.inputs;
            let fact = &inputs.facts[callable.owner.0];
            let operation = if unsupported_inputs {
                ExecutionOperation::CallableInputs
            } else {
                ExecutionOperation::ArrayIteration
            };
            // What this execution reached stays observed; the rest of the
            // body is assumed.
            let effect_owner = self.state.current_top_owner.unwrap_or(callable.owner);
            self.assuming(
                AssumedEffectReason::WholeBodyPromotion { operation },
                |this| {
                    this.promote_lazy_effects(
                        &fact.reads.lazy,
                        &fact.rebinds.lazy,
                        effect_owner,
                        &mut outcome,
                    )
                },
            );
            outcome.unsupported(operation, callable.owner, DUMMY_SP);
        }
        self.state.active.remove(&active_invocation);
        self.state.invocation_depth -= 1;
        self.remember_invocation(signature, &outcome);
        outcome
    }

    pub(super) fn execute_call(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) -> bool {
        // Completeness is cumulative in the returned outcome, but only this
        // transfer's skipped effects can invalidate its own inputs here.
        let complete_before = outcome.complete;
        outcome.complete = true;
        let result = self.request(RuntimeOperation::Call {
            call: call.clone(),
            environment: env.clone(),
            captures: captures.clone(),
            outcome: Box::new(std::mem::take(outcome)),
            operands: None,
        });
        let RuntimeValue::Call {
            outcome: result, ..
        } = result
        else {
            unreachable!("call transfers produce outcomes");
        };
        *outcome = *result;
        if !outcome.complete {
            self.widen_native_effects();
            // A truncated transfer can mutate its inputs before the next
            // flattened operation in this same top-level statement or body.
            let mut reached = self.evaluate(&call.callee, env, captures);
            for argument in &call.args {
                reached.extend(self.evaluate(argument, env, captures));
            }
            reached.extend(
                outcome
                    .effects
                    .reads
                    .iter()
                    .filter_map(|(binding, _)| self.state.world.module.prefix_values.get(binding))
                    .flatten()
                    .cloned(),
            );
            self.unsettle(&reached);
        }
        let transfer_complete = outcome.complete;
        outcome.complete &= complete_before;
        transfer_complete
    }

    pub(super) fn method_receiver_is_unused(&self, targets: &Values) -> bool {
        self.inputs.index.dynamic_scope_owners.is_empty()
            && !targets.is_empty()
            && targets.iter().all(|target| {
                let key = match target {
                    AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => key,
                    _ => return false,
                };
                self.inputs.index.callables.get(key).is_some_and(|body| {
                    !body.opaque
                        && !body.generator
                        && body.calls.is_empty()
                        && !body.reads.iter().any(|id| id.0.as_ref() == "arguments")
                        && self
                            .inputs
                            .index
                            .callables
                            .values()
                            .filter(|nested| nested.owner == body.owner)
                            .all(|nested| !nested.implicit_capture)
                })
            })
    }

    /// Whether a method call to `targets` can read its receiver through
    /// `this` (including from an arrow inside the method), which the summary
    /// does not bind.
    fn method_may_read_receiver(&self, targets: &Values) -> bool {
        let index = self.inputs.index;
        !index.dynamic_scope_owners.is_empty()
            || targets.iter().any(|target| match target {
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                    index.reads_this(*key)
                }
                _ => false,
            })
    }

    /// A destructuring assignment to a member target is a [[Set]]: it may run
    /// a setter reading `this`.
    pub(super) fn publish_destructuring_target_receivers(
        &mut self,
        receivers: &[ValueExpr],
        env: &Context,
        captures: &Context,
    ) {
        for receiver in receivers {
            let values = self.evaluate(receiver, env, captures);
            self.publish_accessor_receivers(&values, DUMMY_SP);
        }
    }

    /// A member read or write on `receivers` may run an accessor that reads
    /// `this`, which the summary does not bind: publish those receivers.
    /// Kept out of line for the call transfer's frame size.
    #[inline(never)]
    pub(super) fn publish_accessor_receivers(&mut self, receivers: &Values, span: Span) {
        let index = self.inputs.index;
        let reached: Values = receivers
            .iter()
            .filter(|receiver| {
                let object = match receiver {
                    AbstractValue::Value(Value::Alloc(Kind::Object, object)) => object,
                    AbstractValue::Value(Value::Alloc(Kind::Class, class))
                    | AbstractValue::ClassMembers(class)
                    | AbstractValue::Instance(class, _, _) => {
                        return self
                            .class_chain(*class)
                            .iter()
                            .any(|class| index.this_reading_accessor_classes.contains(class));
                    }
                    _ => return false,
                };
                index.this_reading_accessor_objects.contains(object)
                    || self
                        .state
                        .world
                        .surface
                        .applied_accessors
                        .get(object)
                        .is_some_and(|accessors| {
                            accessors.values().any(|accessor| {
                                accessor.getter.iter().chain(accessor.setter.iter()).any(
                                    |function| match function {
                                        AbstractValue::Value(Value::Alloc(Kind::Function, key))
                                        | AbstractValue::Closure(
                                            HeapAddress { callable: key, .. },
                                            _,
                                        ) => index.reads_this(*key),
                                        AbstractValue::Value(
                                            Value::Prim(_) | Value::Unavailable,
                                        ) => false,
                                        _ => true,
                                    },
                                )
                            })
                        })
            })
            .cloned()
            .collect();
        if !reached.is_empty() {
            self.publish_values(
                &reached,
                ClassMembersPublicationWriter::AccessorReceiver,
                Some(span),
            );
        }
    }

    /// The `Call`/`New` transfer over already evaluated targets and arguments.
    /// `supplied` carries the kernel's operand selections; `None` is the
    /// self-evaluating mode, which requests no value.
    pub(super) fn transfer_evaluated_call(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
        operands: EvaluatedCallOperands,
        supplied: Option<SuppliedCallSelections>,
    ) -> CallTransfer {
        let needs_value = supplied.is_some();
        let SuppliedCallSelections {
            selected_pure_initializer_call,
            selected_member_dependencies,
            selected_value,
            argument_selections,
        } = supplied.unwrap_or_else(|| SuppliedCallSelections {
            selected_pure_initializer_call: false,
            selected_member_dependencies: None,
            selected_value: None,
            argument_selections: vec![None; call.args.len()],
        });
        let EvaluatedCallOperands {
            targets,
            mut args,
            prepared_receiver,
        } = operands;
        let operands_may_throw = targets.contains(&AbstractValue::Value(Value::Unavailable))
            || args
                .iter()
                .any(|values| values.contains(&AbstractValue::Value(Value::Unavailable)));
        if args.iter().any(|values| {
            values.len() == 1 && values.contains(&AbstractValue::Value(Value::Unavailable))
        }) {
            return CallTransfer::requested(needs_value, || {
                Values::from([AbstractValue::Value(Value::Unavailable)])
            });
        }
        for values in &mut args {
            values.remove(&AbstractValue::Value(Value::Unavailable));
        }
        // The provider body runs no other code and writes only its own
        // allocations. The call stays impure for purity and S-chain order.
        if !call.has_spread
            && let Some(summary) = self.foreign_call_summary(call.kind, &call.callee)
        {
            self.retain_foreign_arguments(&summary, &args);
            self.record_foreign_call_use(&summary);
            return CallTransfer::requested(needs_value, || {
                let mut values = self.foreign_call_result(&summary, &args);
                if operands_may_throw {
                    values.insert(AbstractValue::Value(Value::Unavailable));
                }
                values
            });
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && self.source_record_factory(&call.callee, &call.args)
        {
            self.record_returned_record_use(
                &call.callee,
                crate::reports::schema::ReturnedRecordUseKind::CallTiming,
                call.owner,
                call.span,
            );
            return CallTransfer::requested(needs_value, || {
                Values::from([AbstractValue::ForeignDeferredRecord])
            });
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(source_object) =
                self.source_namespace_wrapper(&call.callee, &args, call.span.into())
        {
            return CallTransfer::requested(needs_value, || {
                let mut retained = Values::from([AbstractValue::Value(Value::Alloc(
                    Kind::Object,
                    source_object,
                ))]);
                retained.extend(self.callback_values(&retained));
                Values::from([
                    self.deferred_callbacks(retained),
                    AbstractValue::Value(Value::Top),
                    AbstractValue::Value(Value::Unavailable),
                ])
            });
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(proof) =
                self.descriptor_copy_projection(&call.callee, &args, call.span.into())
        {
            return CallTransfer::requested(needs_value, || {
                self.install_descriptor_copy_result(proof, &args, call.span.into())
            });
        }
        if !call.has_spread
            && let Some(resolution) = self.resolve_native_call(
                call.kind,
                &call.callee,
                CallSite::from(call.span),
                &call.args,
                &args,
            )
        {
            // This branch is the consumed runtime transfer: operands have
            // completed, and the admitted row's checked plan is committed to
            // this prefix whether or not the value is used.
            if needs_value {
                // The kernel's per-call outcome: the admitted row is the one
                // operation it performed, with no application callback,
                // accessor, host operation or exterior write.
                let mut coverage =
                    EffectCoverageFragment::native_operation(&resolution.dependencies);
                if let (Some(caller), Some(intrinsic)) = (
                    self.local_body_identity_for_call(call),
                    resolution.row.family(),
                ) {
                    coverage.selected_local_call(LocalCallReceipt {
                        caller,
                        call_span: call.span,
                        target: LocalBodyIdentity::Native {
                            intrinsic,
                            property: resolution.row.op.callee().1.map(str::to_owned),
                        },
                        dependencies: Box::new(resolution.dependencies.clone()),
                    });
                }
                outcome.effect_coverage = coverage;
            }
            for cell in &resolution.projected_reads {
                outcome
                    .effects
                    .record_heap_read(cell.clone(), self.state.assumption.is_none());
            }
            if let Some(receipt) = &resolution.iterable {
                outcome.effects.record_heap_contents(
                    &receipt.external_heap_contents(),
                    self.state.assumption.is_none(),
                );
                outcome.effect_coverage.compose(&receipt.coverage);
            }
            let values = self.execute_native_resolution(
                resolution,
                &args,
                operands_may_throw,
                call.span.into(),
            );
            return CallTransfer::requested(needs_value, || values);
        }
        if self.host_call_does_not_reenter(call) {
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(callbacks) = self.no_reentry_callbacks(call.span.into(), &call.args, &args)
        {
            // Argument evaluation has completed. The provider cannot reenter
            // during this call, but may retain callbacks anywhere, including a
            // private cross-call registry. Every subsequent opaque operation
            // must therefore see them. Do not give the result a known shape.
            self.insert_escaped_callback_values(
                &callbacks,
                ClassMembersPublicationWriter::NoReentryCallRetention,
                Some(call.span),
                None,
            );
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(callbacks) =
                self.retained_factory_callbacks(call.span.into(), &call.args, &args)
        {
            return CallTransfer::requested(needs_value, || {
                Values::from([self.audited_retained_result(RetainedResultKind::Factory, callbacks)])
            });
        }
        if call.kind == CallKind::New
            && !call.has_spread
            && self
                .retained_constructor_callbacks(call.span.into(), &call.args, &args)
                .is_some()
        {
            return CallTransfer::EffectsOnly;
        }
        // The author assertion covers this direct imported call with checked
        // primitive arguments. Its result remains Unknown; only opaque
        // callback promotion is suppressed.
        if call.kind == CallKind::Call
            && call.no_reentry_candidate
            && let ValueExpr::Binding(binding) = &call.callee
            && self.inputs.index.no_reentry_exports.contains(binding)
        {
            return CallTransfer::EffectsOnly;
        }
        // Distinct author assertions cover primitive and fresh-record inputs.
        // Both use the same member identity and lookup guards. The provider
        // may have private effects; this only suppresses callback reentry.
        if call.kind == CallKind::Call
            && let ValueExpr::Member(receiver, Some(property), MemberKey::Static, _) = &call.callee
            && let ValueExpr::Binding(namespace) = receiver.as_ref()
            && ((call.no_reentry_candidate
                && self
                    .inputs
                    .index
                    .no_reentry_members
                    .get(namespace)
                    .is_some_and(|members| members.contains(property)))
                || (call.no_reentry_record_candidate
                    && self
                        .inputs
                        .index
                        .no_reentry_record_members
                        .get(namespace)
                        .is_some_and(|members| members.contains(property))))
        {
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && self.execute_dense_array_push(
                &call.callee,
                &args,
                env,
                captures,
                prepared_receiver.as_ref(),
            )
        {
            return CallTransfer::requested(needs_value, || {
                // Contents and multiplicity stay unknown after a push. Its
                // completing result is numeric; a length overflow may throw.
                Values::from([
                    AbstractValue::Value(Value::Prim(PrimKinds::NUMBER)),
                    AbstractValue::Value(Value::Unavailable),
                ])
            });
        }
        // Proofs that reevaluate source expressions have not migrated to
        // evaluated operands. A missing result transfer truncates the ordered
        // caller; it must never replay those expressions to recover a value.
        let prepared_receiver_unused = prepared_receiver
            .as_ref()
            .map(|_| self.method_receiver_is_unused(&targets));
        // The callback-factory evidence supplies a fresh ordinary carrier
        // retaining this callback. The exact root's separate purity premise
        // excludes provider throws. Keep the carrier opaque and use the
        // already evaluated callee and argument without replaying either.
        if selected_pure_initializer_call
            && needs_value
            && call.kind == CallKind::Call
            && !call.has_spread
            && !operands_may_throw
            && prepared_receiver.is_none()
            && matches!(&call.callee, ValueExpr::Binding(_))
            && let [ValueExpr::Alloc(Kind::Function, source)] = call.args.as_slice()
            && targets.len() == 1
            && targets.iter().all(|target| {
                matches!(
                    target,
                    AbstractValue::CallbackFactory | AbstractValue::InlineCallbackFactory
                )
            })
            && matches!(args.as_slice(), [values] if values.len() == 1
            && values.iter().all(|value| matches!(
                value,
                    AbstractValue::Value(Value::Alloc(Kind::Function, actual))
                        | AbstractValue::Closure(HeapAddress { callable: actual, .. }, _)
                        if actual == source
            )))
        {
            let records_evidence = targets.contains(&AbstractValue::CallbackFactory);
            return CallTransfer::Values(self.retained_factory_carrier(
                call,
                records_evidence,
                &args[0],
            ));
        }
        if needs_value {
            // Publication can change properties and captured state, but cannot
            // replace an ordinary source function's [[Call]]. Invocation reads
            // live capture cells and retains their existing havoc/heap guards.
            let local_target = self.local_call_target(&targets).is_some();
            let abrupt = !targets.is_empty()
                && targets.iter().all(|target| {
                    matches!(
                        target,
                        AbstractValue::Value(Value::Unavailable)
                            | AbstractValue::Value(Value::Prim(_))
                    )
                });
            let admission_refusal = if !local_target && !abrupt {
                let refusal = if targets.len() > 1 {
                    OrderedCallAdmissionRefusal::MultipleTargets
                } else {
                    match targets.iter().next() {
                        None
                        | Some(AbstractValue::Value(Value::Top))
                        | Some(AbstractValue::SymbolicInput(_)) => {
                            OrderedCallAdmissionRefusal::UnknownTarget
                        }
                        Some(_) => OrderedCallAdmissionRefusal::NonLocalTarget,
                    }
                };
                Some(refusal)
            } else if !abrupt && prepared_receiver_unused == Some(false) {
                Some(OrderedCallAdmissionRefusal::UnsupportedReceiver)
            } else {
                None
            };
            if let Some(refusal) = admission_refusal {
                return CallTransfer::UnsupportedOperands(refusal);
            }
        }
        let from_entries_source_callbacks = if call.kind == CallKind::Call && !needs_value {
            if self
                .proven_from_entries_callbacks(call.span.into(), env, captures)
                .is_some()
            {
                return CallTransfer::EffectsOnly;
            }
            self.callbacks_in_from_entries_source(call.span.into(), env, captures)
        } else {
            None
        };
        if call.kind == CallKind::New
            && self.is_proven_map_entry_array(&call.callee, &call.args, env, captures, call.owner)
        {
            // The Map retains its entries, and the interpreter does not track
            // what it holds: whoever reaches the Map reaches them.
            let entries: Values = args.iter().flatten().cloned().collect();
            self.taint_objects(&entries);
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call
            && !needs_value
            && self.execute_array_iteration(call.span.into(), env, captures, outcome)
        {
            return CallTransfer::EffectsOnly;
        }
        // Known local methods still receive an implicit `this` capability.
        // Its effects are not resolved by invoke, so it can mutate any retained
        // array reachable from the receiver before a later intrinsic iteration.
        if call.kind == CallKind::Call
            && let Some(receiver) = method_callee_receiver(&call.callee)
        {
            let unused_receiver = prepared_receiver_unused
                .unwrap_or_else(|| self.method_receiver_is_unused(&targets));
            if !unused_receiver {
                let values = prepared_receiver
                    .clone()
                    .unwrap_or_else(|| self.evaluate(receiver, env, captures));
                let contains_array = self.contains_dense_array(&values);
                if self.method_may_read_receiver(&targets) {
                    // The summary reads `this` as unknown, which stands only
                    // for published values.
                    self.publish_values(
                        &values,
                        ClassMembersPublicationWriter::MethodReceiver,
                        Some(call.span),
                    );
                } else {
                    self.taint_objects(&values);
                }
                if contains_array {
                    // A dense receiver preserves possible callback reach, but
                    // does not prove that the method invoked those callbacks.
                    self.promote_escaped_callbacks(&targets, outcome);
                }
            }
        }
        let mut opaque_args = args.clone();
        if let Some(receiver) = method_callee_receiver(&call.callee) {
            let receiver_values = prepared_receiver
                .clone()
                .unwrap_or_else(|| self.evaluate(receiver, env, captures));
            // The receiver is an input capability too: an opaque method can
            // invoke its stored callbacks through `this`, including callbacks
            // nested in collections or retained wrapper/instance values.
            // Keep the original receiver taint above; callback reachability
            // does not prove that the foreign call preserves its contents.
            opaque_args.push(receiver_values);
        }
        if !needs_value
            && let Some(proof) = self.inputs.index.array_iterations.get(&call.span.into())
        {
            let source = self.evaluate(&proof.source.clone(), env, captures);
            self.promote_escaped_callbacks(&source, outcome);
            opaque_args.push(source);
        }

        if let Some(callbacks) = from_entries_source_callbacks {
            opaque_args.push(callbacks);
        }
        if targets.is_empty()
            || targets.iter().any(|target| {
                matches!(
                    target,
                    AbstractValue::Value(Value::Top)
                        | AbstractValue::DeferredCallbacks(_)
                        | AbstractValue::AuditedRetainedResult(_, _)
                        | AbstractValue::ClassMembers(_)
                        | AbstractValue::Instance(_, _, _)
                )
            })
        {
            // Opaque calls can mutate objects passed as arguments or used as
            // method receivers. Preserve that possibility for later narrow
            // construction proofs (including changes to an entry's own data
            // properties or an array's iterator/species surface).
            let mut possible_objects: Values = args.iter().flatten().cloned().collect();
            if let Some(receiver) = method_callee_receiver(&call.callee) {
                possible_objects.extend(
                    prepared_receiver
                        .clone()
                        .unwrap_or_else(|| self.evaluate(receiver, env, captures)),
                );
            }
            self.expose_class_capabilities(&possible_objects);
            self.taint_objects(&possible_objects);
        }
        let retained_callback = matches!(args.as_slice(), [values] if !values.is_empty()
            && values.iter().all(|value| matches!(value, AbstractValue::AuditedRetainedResult(RetainedResultKind::Factory, captured) if self.audited_result_is_clean(captured))));
        if targets.is_empty() {
            outcome.lose_call_target(call.owner, call.span);
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            self.invoke_possible_callbacks_at(
                &opaque_args,
                call.no_sync_arguments,
                Some(call.span),
                outcome,
            );
        }
        let mut invoked_unknown_callbacks = targets.is_empty();
        let mut result_values = Values::new();
        if needs_value
            && (operands_may_throw
                || targets
                    .iter()
                    .any(|target| matches!(target, AbstractValue::Value(Value::Prim(_)))))
        {
            result_values.insert(AbstractValue::Value(Value::Unavailable));
        }
        // The receipt belongs to this exact selected invocation. A member
        // target is admitted only when LocalBody already consumed its
        // initialized-module own-data selection before evaluating arguments;
        // An alias instead needs the actual value-selection evidence carried
        // by its ordered producer. Native/imported targets and raw known
        // return values do not manufacture a selection receipt.
        let direct_binding = match &call.callee {
            ValueExpr::Binding(binding)
                if self
                    .inputs
                    .index
                    .direct_top_level_functions
                    .contains(binding)
                    && prepared_receiver.is_none()
                    && self.inputs.index.hoisted_bindings.contains_key(binding) =>
            {
                Some(binding)
            }
            _ => None,
        };
        let member_selection = matches!(
            (&call.callee, prepared_receiver.as_ref(), selected_member_dependencies.as_ref()),
            (
                ValueExpr::Member(receiver, Some(_), MemberKey::Static, _),
                Some(_),
                Some(_)
            ) if matches!(receiver.as_ref(), ValueExpr::Binding(_))
        );
        let coverage_target = if needs_value
            && call.kind == CallKind::Call
            && !call.has_spread
            && !operands_may_throw
            && (direct_binding.is_some() || member_selection || selected_value.is_some())
            && let Some((key, callable)) = self.local_call_target(&targets)
            && let Some(caller) = self.local_body_identity_for_call(call)
            && direct_binding
                .is_none_or(|binding| self.inputs.index.hoisted_bindings.get(binding) == Some(&key))
        {
            let mut prerequisites = self.boxed_settled_prefix_dependencies();
            if let Some(selection) = &selected_member_dependencies {
                prerequisites.extend(selection);
            }
            if let Some(selection) = &selected_value {
                prerequisites.extend(&selection.dependencies);
            }
            Some((
                key,
                caller,
                LocalBodyIdentity::Callable {
                    key,
                    owner: callable.owner,
                },
                prerequisites,
            ))
        } else {
            None
        };
        let single_target = targets.len() == 1;
        let mut result_selection = None;
        // `new` of a local plain function runs its body with an untracked
        // `this`: what it stores through `this` or into other bindings is
        // part of the call's effects. The instance stays unknown (the
        // unknown-callee arm below).
        if call.kind == CallKind::New {
            self.construct_plain_functions(&targets, &args, outcome);
        }
        for target in targets {
            match target {
                AbstractValue::CallbackFactory
                    if call.kind == CallKind::Call
                        && !call.has_spread
                        && (retained_callback
                            || matches!(
                                call.args.as_slice(),
                                [ValueExpr::Alloc(Kind::Function, _)]
                            )) =>
                {
                    if let ValueExpr::Binding(imported_local) = &call.callee {
                        self.record_callback_factory_use(
                            imported_local,
                            CallbackFactoryUseKind::CallTiming,
                            call.owner,
                            call.span,
                        );
                    }
                }
                AbstractValue::InlineCallbackFactory
                    if call.kind == CallKind::Call
                        && !call.has_spread
                        && matches!(
                            call.args.as_slice(),
                            [ValueExpr::Alloc(Kind::Function, _)]
                        ) => {}
                AbstractValue::ForeignDeferredRecord => {
                    self.promote_foreign_record(outcome);
                    self.note_unknown_call(call);
                    self.invoke_possible_callbacks_at(
                        &opaque_args,
                        false,
                        Some(call.span),
                        outcome,
                    );
                }
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured) => {
                    outcome.lose_call_target(call.owner, call.span);
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    let mut possible_args = opaque_args.clone();
                    possible_args.push((*captured.values).clone());
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    self.invoke_possible_callbacks_at(
                        &possible_args,
                        false,
                        Some(call.span),
                        outcome,
                    );
                }
                AbstractValue::ClassMembers(key) => {
                    outcome.lose_call_target(call.owner, call.span);
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
                        this.promote_class_members(key, outcome)
                    });
                    self.invoke_possible_callbacks_at(
                        &opaque_args,
                        false,
                        Some(call.span),
                        outcome,
                    );
                }
                AbstractValue::Instance(key, captured, _) => {
                    outcome.lose_call_target(call.owner, call.span);
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
                        this.promote_class_members(key, outcome)
                    });
                    let mut possible_args = opaque_args.clone();
                    possible_args.push((*captured.values).clone());
                    self.invoke_possible_callbacks_at(
                        &possible_args,
                        false,
                        Some(call.span),
                        outcome,
                    );
                }
                AbstractValue::Value(Value::Unavailable) | AbstractValue::Value(Value::Prim(_)) => {
                }
                AbstractValue::ObjectAssign
                    if call.kind == CallKind::Call
                        && call.plain_object_sources
                        && matches!(call.args.first(), Some(ValueExpr::Binding(id)) if call.caller
                        .and_then(|caller| self.inputs.index.callables.get(&caller).map(|body| (caller, body)))
                        .is_some_and(|(caller, body)| self.stable_fresh_function_target(caller, body, id))) =>
                    {}
                AbstractValue::ObjectAssign if call.kind == CallKind::Call => {
                    // Once the target's descriptor/prototype integrity is
                    // unproven, copying a property may synchronously call a
                    // setter. Include nested lazy bodies of owners that ran
                    // before this call, as well as the owners of the source
                    // and target values. The first-order fallback excludes
                    // those bodies because merely creating a closure does
                    // not execute it.
                    self.note_unknown_call(call);
                    let possible_objects: Values = args.iter().flatten().cloned().collect();
                    // The target may be reachable by anyone (an intrinsic
                    // prototype): what is copied onto it escapes.
                    self.publish_values(
                        &possible_objects,
                        ClassMembersPublicationWriter::ObjectAssignCopy,
                        Some(call.span),
                    );
                    let mut possible_accessors: BTreeSet<_> = self
                        .state
                        .world
                        .reachability
                        .owners
                        .iter()
                        .copied()
                        .filter(|candidate| self.may_be_available_accessor_owner(*candidate))
                        .collect();
                    possible_accessors.extend(outcome.executed_owners.iter().copied());
                    if self.state.world.reachability.incomplete {
                        possible_accessors.extend(self.all_owners());
                    }
                    let effect_owner = self.state.current_top_owner.unwrap_or(call.owner);
                    self.assuming(AssumedEffectReason::PossibleSetter, |this| {
                        for accessor_owner in possible_accessors {
                            this.promote_lazy_owner(accessor_owner, effect_owner, outcome);
                        }
                    });
                    outcome.unsupported(ExecutionOperation::ObjectAssign, call.owner, call.span);
                    outcome.unknown_call = true;
                    self.widen_native_effects();
                }
                AbstractValue::Value(Value::Alloc(Kind::Class, key))
                    if call.kind == CallKind::New =>
                {
                    let constructed = if call.has_spread {
                        Err(ConstructionRejection::SpreadArguments)
                    } else {
                        self.construct_local_class(key, &args)
                    };
                    match constructed {
                        Ok((mut nested, _)) => {
                            self.apply_rebind_effects(&nested.effects.rebinds);
                            outcome.absorb_call_effects(
                                &mut nested.effects,
                                self.state.assumption.as_ref(),
                            );
                            outcome.executed_owners.extend(nested.executed_owners);
                            outcome.unknown_call |= nested.unknown_call;
                            outcome.inherit_completeness(
                                nested.complete,
                                nested.effects.precision_losses,
                            );
                        }
                        Err(rejection) => {
                            self.reject_local_construction(key, &args, call, rejection, outcome);
                            if !invoked_unknown_callbacks {
                                self.invoke_possible_callbacks_at(
                                    &args,
                                    false,
                                    Some(call.span),
                                    outcome,
                                );
                                invoked_unknown_callbacks = true;
                            }
                        }
                    }
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    if call.kind == CallKind::Call =>
                {
                    self.execute_local_call_target(
                        LocalCallTarget {
                            key,
                            captured: None,
                            args: &args,
                            argument_selections: &argument_selections,
                            coverage_target: coverage_target.as_ref(),
                            single_target,
                            needs_value,
                            call_span: call.span,
                        },
                        &mut result_values,
                        &mut result_selection,
                        outcome,
                    );
                }
                AbstractValue::Closure(HeapAddress { callable: key, .. }, captured)
                    if call.kind == CallKind::Call =>
                {
                    self.execute_local_call_target(
                        LocalCallTarget {
                            key,
                            captured: Some(captured),
                            args: &args,
                            argument_selections: &argument_selections,
                            coverage_target: coverage_target.as_ref(),
                            single_target,
                            needs_value,
                            call_span: call.span,
                        },
                        &mut result_values,
                        &mut result_selection,
                        outcome,
                    );
                }
                _ => {
                    outcome.lose_call_target(call.owner, call.span);
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    if !invoked_unknown_callbacks {
                        self.invoke_possible_callbacks_at(
                            &opaque_args,
                            call.no_sync_arguments,
                            Some(call.span),
                            outcome,
                        );
                        invoked_unknown_callbacks = true;
                    }
                }
            }
        }
        outcome.return_selection =
            result_selection.filter(|selection| selection.matches(&result_values));
        CallTransfer::requested(needs_value, || result_values)
    }
}

/// Source units with a call that may run code the source does not name
/// (SPEC.md § Synchronous execution diagnostics): a call or `new` whose callee
/// is neither a builtin row nor local callables by value flow, a protocol
/// operation on a value rooted in an import, a global or another unknown
/// binding, or an opaque body.
fn unknown_callee_owners(
    index: &ExecutionIndex,
    flow: &ChunkValueFlow,
) -> BTreeSet<SourceUnitOrdinal> {
    let local_callables = |values: FlowValues| {
        !values.is_empty()
            && values
                .iter()
                .all(|value| matches!(value, Value::Alloc(Kind::Function | Kind::Class, _)))
    };
    let names_local_callables = |callee: &ValueExpr| match callee {
        ValueExpr::Alloc(Kind::Function | Kind::Class, _) => true,
        ValueExpr::Binding(binding) => local_callables(flow.values_of_binding(binding)),
        ValueExpr::Member(receiver, Some(property), _, _) => {
            let ValueExpr::Binding(receiver) = receiver.as_ref() else {
                return false;
            };
            let receivers = flow.values_of_binding(receiver);
            !receivers.is_empty()
                && receivers.iter().all(|receiver| {
                    matches!(receiver, Value::Alloc(Kind::Object, object)
                        if flow.values_of_property(object, property)
                            .is_some_and(|values| local_callables(values.clone())))
                })
        }
        _ => false,
    };
    fn unknown_root(expression: &ValueExpr, flow: &ChunkValueFlow) -> bool {
        match expression {
            ValueExpr::Binding(binding) => {
                let values = flow.values_of_binding(binding);
                values.is_empty() || values.iter().any(|value| value == Value::Top)
            }
            ValueExpr::Member(receiver, _, _, _) => unknown_root(receiver, flow),
            _ => false,
        }
    }
    let unknown = |call: &Call| match call.kind {
        CallKind::Call | CallKind::New => {
            !native_call_identity_key(call.kind, &call.callee)
                .is_some_and(|(_, callee)| !flow.is_declared(&callee.binding))
                && !names_local_callables(&call.callee)
        }
        _ => unknown_root(&call.callee, flow),
    };
    index
        .top_calls
        .iter()
        .flatten()
        .chain(
            index
                .callables
                .values()
                .flat_map(|callable| &callable.calls),
        )
        .filter(|call| unknown(call))
        .map(|call| call.owner)
        .chain(
            index
                .callables
                .values()
                .filter(|callable| callable.opaque)
                .map(|callable| callable.owner),
        )
        .collect()
}

/// Marks each source unit whose `adjacency` closure contains a `target`.
fn owners_reaching(
    adjacency: &[Vec<SourceUnitOrdinal>],
    targets: &BTreeSet<SourceUnitOrdinal>,
) -> Vec<bool> {
    let mut predecessors = vec![Vec::new(); adjacency.len()];
    for (source, targets) in adjacency.iter().enumerate() {
        for target in targets {
            predecessors[target.0].push(source);
        }
    }
    let mut reaches = vec![false; adjacency.len()];
    let mut pending: Vec<usize> = targets
        .iter()
        .map(|target| target.0)
        .filter(|&target| target < adjacency.len())
        .collect();
    for &target in &pending {
        reaches[target] = true;
    }
    while let Some(target) = pending.pop() {
        for &source in &predecessors[target] {
            if !reaches[source] {
                reaches[source] = true;
                pending.push(source);
            }
        }
    }
    reaches
}

fn callback_source_units_for_signature(
    callback_argument_exprs: &BTreeMap<usize, ValueExpr>,
    callback_signature: &spec::RetainedCallbackSignature,
    flow: &ChunkValueFlow,
) -> Option<BTreeSet<SourceUnitOrdinal>> {
    let mut source_units = BTreeSet::new();
    for position in &callback_signature.callback_arguments {
        let expression = callback_argument_exprs.get(position)?;
        let values: BTreeSet<_> = match expression {
            ValueExpr::Binding(binding) => flow.values_of_binding(binding).into_iter().collect(),
            ValueExpr::Alloc(kind, site) => BTreeSet::from([Value::Alloc(*kind, *site)]),
            // Calls, object members and aggregate expressions can return or
            // expose callbacks through paths this local witness does not link.
            _ => return None,
        };
        if values.len() != 1 {
            return None;
        }
        for value in values {
            if !matches!(value, Value::Alloc(Kind::Function, _)) {
                return None;
            }
            source_units.insert(*flow.callable_source_unit.get(&value)?);
        }
    }
    Some(source_units)
}

/// Prove the narrow return-only wrapper shape using the same execution index
/// as call promotion. Whole-chunk flow must contain exactly that function;
/// aliases with unknown/reassigned targets and dynamic scopes remain opaque.
pub(crate) struct SynchronousCallProofs<'a> {
    pub(crate) set_iterable: &'a SetIterableProof,
    pub(crate) graph: &'a ChunkCodeGraph,
    pub(crate) purity: &'a StatementPurityContext<'a>,
    pub(crate) callable_heap_contents: &'a BTreeMap<u32, HeapContentCells>,
    pub(crate) index: PreparedSynchronousCallIndex,
}

pub(crate) struct PreparedSynchronousCallIndex {
    index: ExecutionIndex,
    set_iterable_proof: SetIterableProof,
    native_own_data_read_candidates: BTreeMap<SourceUnitOrdinal, Vec<NativeOwnDataReadCandidate>>,
}

enum NativeOwnDataReadCandidate {
    Exact {
        span: Span,
        slots: [IntrinsicSlot; 2],
    },
    Unrecognized {
        span: Span,
        detail: String,
    },
}

/// Exact native calls whose own heap action cannot observe an existing array
/// or Set. Argument evaluation remains the collector's responsibility.
pub(crate) struct FreshRecordFreezeHeapProof {
    /// Each certified call site and the `native_call_identity_inputs` entry
    /// that certified it.
    sites: BTreeMap<(u32, u32), crate::analysis_inputs::NativeOperationInputEvidence>,
}

impl FreshRecordFreezeHeapProof {
    /// The author input certifying `call`, if the proof covers it.
    pub(crate) fn input_evidence(
        &self,
        call: &CallExpr,
    ) -> Option<&crate::analysis_inputs::NativeOperationInputEvidence> {
        self.sites.get(&(call.span.lo.0, call.span.hi.0))
    }
}

impl PreparedSynchronousCallIndex {
    pub(crate) fn native_own_data_read_diagnostics(
        &self,
        owner: SourceUnitOrdinal,
    ) -> Vec<NativeOwnDataReadDiagnostic> {
        self.native_own_data_read_candidates
            .get(&owner)
            .into_iter()
            .flatten()
            .map(|candidate| match candidate {
                NativeOwnDataReadCandidate::Exact { span, slots } => self
                    .native_own_data_read(*span)
                    .map(|(_, _, dependencies)| NativeOwnDataReadDiagnostic::Proven {
                        dependencies,
                    })
                    .unwrap_or_else(|| NativeOwnDataReadDiagnostic::MissingProof {
                        span: *span,
                        detail: self.native_own_data_read_failure_detail(*span, slots),
                    }),
                NativeOwnDataReadCandidate::Unrecognized { span, detail } => {
                    NativeOwnDataReadDiagnostic::Unrecognized {
                        span: *span,
                        detail: detail.clone(),
                    }
                }
            })
            .collect()
    }

    fn native_own_data_read_failure_detail(
        &self,
        span: Span,
        slots: &[IntrinsicSlot; 2],
    ) -> String {
        let required = slots
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join(", ");
        let dynamic_scope = !self.index.dynamic_scope_owners.is_empty();
        let missing = if dynamic_scope {
            slots.to_vec()
        } else {
            slots
                .iter()
                .copied()
                .filter(|slot| {
                    self.index
                        .integrity
                        .require(std::slice::from_ref(slot), Some(span.lo.0))
                        .dependencies()
                        .is_none()
                })
                .collect()
        };
        if dynamic_scope {
            format!(
                "Native own-data read requires intact slots {required}; dynamic scope leaves native binding identity unresolved. Use `debundle explain-integrity` with `--slot` for each listed slot to inspect the program census witnesses."
            )
        } else if !missing.is_empty() {
            let missing = missing
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
                .join(", ");
            format!(
                "Native own-data read requires intact slots {required}; this run has no proof for {missing}. Use `debundle explain-integrity` with `--slot` for each missing slot to inspect the program native-integrity census witnesses."
            )
        } else {
            format!(
                "Native own-data read requires one proof for slots {required}; no proof was accepted for this complete slot set. Use `debundle explain-integrity` with `--slot` for each listed slot to inspect the program census side."
            )
        }
    }

    pub(crate) fn resolved_native_operation_inputs(
        &self,
    ) -> Vec<crate::analysis_inputs::ResolvedNativeOperationInput> {
        self.index.validated_native_operation_inputs()
    }

    pub(crate) fn locate_native_integrity_witnesses(
        &mut self,
        locate: impl FnMut(Span) -> Option<crate::execution_evidence::SourceLocation>,
    ) {
        self.index.locate_native_integrity_witnesses(locate);
    }

    pub(crate) fn set_iterable_proof(&self) -> &SetIterableProof {
        &self.set_iterable_proof
    }

    pub(crate) fn local_class_construction_proof(&self) -> &LocalClassConstructionProof {
        &self.index.local_class_construction_proof
    }

    pub(crate) fn local_class_heap_proof(&self) -> crate::facts::LocalClassHeapProof {
        self.index.local_class_construction_proof.heap_proof()
    }

    pub(crate) fn fresh_record_freeze_heap_proof(&self) -> FreshRecordFreezeHeapProof {
        let sites = self
            .index
            .resolved_native_operation_inputs
            .iter()
            .filter(|input| {
                input.evidence.operation
                    == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
            })
            .filter_map(|input| {
                let call = self
                    .index
                    .direct_freeze_return_call(&input.binding, input.selected_statement_span)?;
                let [ValueExpr::ObjectLiteral(object, _)] = call.args.as_slice() else {
                    return None;
                };
                (call.span == input.call_span && self.index.plain_data_objects.contains(object))
                    .then(|| ((call.span.lo.0, call.span.hi.0), input.evidence.clone()))
            })
            .collect();
        FreshRecordFreezeHeapProof { sites }
    }

    pub(crate) fn primitive_class_key_read(&self, span: Span) -> bool {
        self.index.primitive_class_key_read_premise(span).is_some()
    }

    pub(crate) fn native_own_data_read(
        &self,
        span: Span,
    ) -> Option<(
        crate::analysis_provenance::InferenceUse,
        Option<crate::analysis_inputs::NativeOperationInputEvidence>,
        ProofDependencies,
    )> {
        let premise = self.index.native_own_data_read_premise(span)?;
        let evidence = if premise.census {
            None
        } else {
            Some(
                self.index
                    .native_operation_input_evidence
                    .get(&CallSite::from(span))?
                    .clone(),
            )
        };
        Some((premise.inference_use, evidence, premise.dependencies))
    }
}

pub(crate) fn prepare_synchronous_call_index(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    hints: &AnalysisInputs,
    plain_array_bindings: &BTreeSet<String>,
    local_class_construction: &LocalClassConstructionShapes,
    set_iterable_shapes: SetIterableShapes,
    shadowed: &BTreeSet<&'static str>,
) -> PreparedSynchronousCallIndex {
    prepare_synchronous_call_index_with_source_facts(
        body,
        flow,
        hints,
        plain_array_bindings,
        PreparedSyncShapeInputs {
            local_class_construction,
            set_iterable_shapes,
            stable_array_spread_shapes: StableArraySpreadShapes::default(),
            shadowed,
            source_facts: None,
        },
    )
}

pub(crate) struct PreparedSyncShapeInputs<'a> {
    pub(crate) local_class_construction: &'a LocalClassConstructionShapes,
    pub(crate) set_iterable_shapes: SetIterableShapes,
    pub(crate) stable_array_spread_shapes: StableArraySpreadShapes,
    pub(crate) shadowed: &'a BTreeSet<&'static str>,
    pub(crate) source_facts: Option<&'a SemanticFacts>,
}

pub(crate) fn prepare_synchronous_call_index_with_source_facts(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    hints: &AnalysisInputs,
    plain_array_bindings: &BTreeSet<String>,
    shapes: PreparedSyncShapeInputs<'_>,
) -> PreparedSynchronousCallIndex {
    let PreparedSyncShapeInputs {
        local_class_construction,
        set_iterable_shapes,
        stable_array_spread_shapes,
        shadowed,
        source_facts,
    } = shapes;
    let mut index = ExecutionIndex::collect(
        body,
        hints,
        Some(flow),
        plain_array_bindings,
        local_class_construction,
        &stable_array_spread_shapes,
    );
    // The census is now complete. Publish its binding and mutation premises
    // into one immutable context shared by timing, purity and heap consumers.
    index.integrity = index.integrity_flags();
    let classes = std::mem::take(&mut index.local_class_construction_shapes);
    let facts = index.prepare_semantic_facts(
        flow,
        shadowed,
        super::execution_index::SemanticShapeInputs {
            classes,
            sets: set_iterable_shapes,
            array_spreads: &stable_array_spread_shapes,
            callables: CallableInferenceInputs::default(),
        },
        source_facts,
    );
    index.semantic_facts = Some(Arc::clone(&facts));
    index.local_class_construction_proof = LocalClassConstructionProof::new(Arc::clone(&facts));
    let author_set_inputs = index
        .native_operation_input_evidence
        .iter()
        .filter(|(site, evidence)| {
            evidence.operation
                == NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
                || (evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::SetFromSetSpread)
                    && hints.stable_set_spreads.contains_key(&site.allocation_id()))
        })
        .map(|(site, evidence)| (site.allocation_id(), evidence.clone()))
        .collect();
    let constructor_operands: BTreeMap<_, _> = index
        .top_calls
        .iter()
        .flatten()
        .filter_map(|call| {
            if call.kind != CallKind::New
                || call.caller.is_some()
                || call.has_spread
                || !matches!(&call.callee, ValueExpr::Binding(binding) if binding.0.as_ref() == "Set")
            {
                return None;
            }
            let [ValueExpr::ObjectLiteral(allocation_site, _)] = call.args.as_slice() else {
                return None;
            };
            // ObjectLiteral is the value-flow node for arrays and objects;
            // only its array-elements index establishes the source kind.
            if !index.array_elements.contains_key(allocation_site) {
                return None;
            }
            Some((
                call.span.lo.0,
                TopLevelNativeIterableOperandSite {
                    operation_span: call.span,
                    operand: NativeIterableOperandOrigin::ImmediateArrayLiteral {
                        allocation_site: *allocation_site,
                    },
                    owner: call.owner,
                },
            ))
        })
        .collect();
    let set_iterable_proof = SetIterableProof::new(
        facts,
        author_set_inputs,
        hints,
        stable_array_spread_shapes.clone(),
        &constructor_operands,
    );
    index.local_constructors.retain(|key, constructor| {
        !constructor.checked_default_fields
            || index.local_class_construction_proof.contains_class(*key)
    });
    let mut native_own_data_read_candidates: BTreeMap<
        SourceUnitOrdinal,
        Vec<NativeOwnDataReadCandidate>,
    > = BTreeMap::new();
    let mut native_own_data_reads = BTreeMap::new();
    for call in index.top_calls.iter().flatten() {
        if call.kind != CallKind::LocalMemberRead {
            continue;
        }
        let Some(candidate) = native_own_data_read_candidate(call, flow, shadowed) else {
            continue;
        };
        if let NativeOwnDataReadCandidate::Exact { span, slots } = &candidate {
            native_own_data_reads.insert(span.lo.0, *slots);
        }
        native_own_data_read_candidates
            .entry(call.owner)
            .or_default()
            .push(candidate);
    }
    index.native_own_data_reads = native_own_data_reads;
    PreparedSynchronousCallIndex {
        index,
        set_iterable_proof,
        native_own_data_read_candidates,
    }
}

fn native_own_data_read_candidate(
    call: &Call,
    flow: &ChunkValueFlow,
    shadowed: &BTreeSet<&'static str>,
) -> Option<NativeOwnDataReadCandidate> {
    let ValueExpr::Member(receiver, property, member_key, _) = &call.callee else {
        return None;
    };
    let ValueExpr::Binding(binding) = receiver.as_ref() else {
        return None;
    };
    let family = NativeIntrinsic::for_global_binding(binding.0.as_ref())?;
    let name = binding.0.as_ref();
    if flow.is_declared(binding) || shadowed.contains(name) {
        return Some(NativeOwnDataReadCandidate::Unrecognized {
            span: call.span,
            detail: format!(
                "`{name}` resolves to a chunk binding here, so this read does not resolve to its native binding; no native slot proof applies."
            ),
        });
    }
    if !matches!(member_key, MemberKey::Static) {
        return Some(NativeOwnDataReadCandidate::Unrecognized {
            span: call.span,
            detail: format!(
                "`{name}[...]` uses computed member access, so this read does not identify an exact native own-data slot to query."
            ),
        });
    }
    let Some(slots) = native_own_data_read_slots(&call.callee, |_| false) else {
        let property = property.as_deref().unwrap_or("<unknown>");
        let global = family.global_binding().unwrap_or(name);
        return Some(NativeOwnDataReadCandidate::Unrecognized {
            span: call.span,
            detail: format!(
                "`{global}.{property}` is not a recognized native own-data slot; there is no exact slot proof to query."
            ),
        });
    };
    Some(NativeOwnDataReadCandidate::Exact {
        span: call.span,
        slots,
    })
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum PrefixNeededKey {
    Binding(Id),
    Object(Value),
}

struct PrefixNeededRules<'a> {
    dependencies: &'a BTreeMap<Id, BTreeSet<Id>>,
    member_write_receivers: &'a BTreeMap<Value, BTreeSet<Id>>,
    flow: &'a ChunkValueFlow,
}

impl IterativeRules for PrefixNeededRules<'_> {
    type Key = PrefixNeededKey;
    type Fact = ();
    type Rule = PrefixNeededKey;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        match rule {
            PrefixNeededKey::Binding(binding) => {
                if context.read(rule.clone()).is_none() {
                    return;
                }
                if let Some(inputs) = self.dependencies.get(binding) {
                    for input in inputs {
                        context.propose(PrefixNeededKey::Binding(input.clone()), ());
                    }
                }
                for origin in self.flow.values_of_binding(binding) {
                    if matches!(origin, Value::Alloc(Kind::Object, _))
                        && self.member_write_receivers.contains_key(&origin)
                    {
                        context.propose(PrefixNeededKey::Object(origin), ());
                    }
                }
            }
            PrefixNeededKey::Object(origin) => {
                if context.read(rule.clone()).is_none() {
                    return;
                }
                if let Some(receivers) = self.member_write_receivers.get(origin) {
                    for receiver in receivers {
                        context.propose(PrefixNeededKey::Binding(receiver.clone()), ());
                    }
                }
            }
        }
    }

    fn merge(_: &Self::Key, _: &mut Self::Fact, _: Self::Fact) -> bool {
        false
    }
}

fn close_prefix_needed_bindings(
    seeds: &BTreeSet<Id>,
    dependencies: &BTreeMap<Id, BTreeSet<Id>>,
    member_write_receivers: &BTreeMap<Value, BTreeSet<Id>>,
    flow: &ChunkValueFlow,
) -> BTreeSet<Id> {
    let mut producers = BTreeSet::new();
    for (binding, inputs) in dependencies {
        producers.insert(PrefixNeededKey::Binding(binding.clone()));
        producers.extend(inputs.iter().cloned().map(PrefixNeededKey::Binding));
    }
    for (origin, receivers) in member_write_receivers {
        producers.insert(PrefixNeededKey::Object(*origin));
        producers.extend(receivers.iter().cloned().map(PrefixNeededKey::Binding));
    }
    producers.extend(seeds.iter().cloned().map(PrefixNeededKey::Binding));

    let mut inference = IterativeInference::new(PrefixNeededRules {
        dependencies,
        member_write_receivers,
        flow,
    });
    for binding in seeds {
        inference.seed(PrefixNeededKey::Binding(binding.clone()), ());
    }
    inference
        .solve(producers)
        .into_values()
        .into_keys()
        .filter_map(|key| match key {
            PrefixNeededKey::Binding(binding) => Some(binding),
            PrefixNeededKey::Object(_) => None,
        })
        .collect()
}

pub(crate) fn annotate_synchronous_calls(
    flow: &ChunkValueFlow,
    proofs: SynchronousCallProofs<'_>,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    export_reentry_policy: &ExportReentryPolicy,
    facts: &mut [StatementFacts],
) -> Vec<crate::facts::CompletedPurity> {
    let index = proofs.index.index;
    for candidate in index.retained_callback_calls.values() {
        if candidate.caller.is_some() {
            continue;
        }
        let Some((namespace, member)) = &candidate.call.namespace_member else {
            continue;
        };
        if index
            .audited_factory_members
            .get(namespace)
            .is_some_and(|members| members.contains(member))
        {
            continue;
        }
        let witness = index
            .rejected_namespace_member_writes
            .get(&(namespace.clone(), member.clone()))
            .cloned();
        let dynamic_write_witness = index
            .rejected_namespace_dynamic_property_writes
            .get(&(namespace.clone(), member.clone()))
            .cloned();
        let call_argument_witness = index
            .rejected_namespace_call_arguments
            .get(namespace)
            .cloned();
        for callback_signature in candidate
            .call
            .signatures
            .iter()
            .filter(|callback_signature| {
                // `retained_callback_invocation` already applies these guards to
                // the parsed call. Keep them explicit here so a configured but
                // inapplicable overload can never be reported as the match.
                callback_signature.argument_count == candidate.call.argument_count
                    && callback_signature
                        .callback_arguments
                        .iter()
                        .all(|position| {
                            *position < candidate.call.argument_count
                                && candidate
                                    .call
                                    .callback_argument_exprs
                                    .contains_key(position)
                        })
            })
        {
            let callback_source_units = callback_source_units_for_signature(
                &candidate.call.callback_argument_exprs,
                callback_signature,
                flow,
            );
            facts[candidate.owner.0]
                .callback_factory_barriers
                .push(CallbackFactoryBarrierFact {
                    namespace_binding: namespace.0.to_string(),
                    member: member.clone(),
                    argument_count: callback_signature.argument_count,
                    callback_arguments: callback_signature.callback_arguments.clone(),
                    callback_source_units,
                    rejection: match &witness {
                        Some(witness) => CallbackFactoryBarrierRejection::NamespaceMemberWrite {
                            witness: witness.clone(),
                        },
                        None => match &dynamic_write_witness {
                            Some(witness) => {
                                CallbackFactoryBarrierRejection::NamespaceDynamicPropertyWrite {
                                    witness: witness.clone(),
                                }
                            }
                            None => match &call_argument_witness {
                                Some(witness) => {
                                    CallbackFactoryBarrierRejection::NamespaceCallArgument {
                                        witness: witness.clone(),
                                    }
                                }
                                None => CallbackFactoryBarrierRejection::UnsupportedGuard,
                            },
                        },
                    },
                });
        }
    }
    // Every top-level call is executed by `top_calls`, regardless of whether
    // its result is retained. Keep an initializer's value only if a later
    // call may read it, directly or through another initializer/function.
    // This avoids re-evaluating thousands of unused factory results.
    let mut needed: BTreeSet<Id> = index
        .callables
        .values()
        .flat_map(|callable| callable.reads.iter().cloned())
        .collect();
    for calls in &index.top_calls {
        for call in calls {
            call.callee.bindings(&mut needed);
            for arg in &call.args {
                arg.bindings(&mut needed);
            }
        }
    }
    // Property-write analysis also needs the receiver's prefix identity, even
    // when only another alias is passed to a later call. In particular, a
    // callable's custom fields/prototype are outside the object-record model.
    for write in index.top_callback_result_writes.iter().flatten() {
        write.receiver.bindings(&mut needed);
        write.value.bindings(&mut needed);
    }
    // Retain prefix values for direct heritage reachability and captured bases.
    for key in index.class_bindings.keys() {
        if let Some(super_class) = index
            .class_values
            .get(key)
            .and_then(|class| class.super_class.as_ref())
        {
            super_class.bindings(&mut needed);
        }
    }
    // A destructuring member target may run a setter on its receiver.
    for receivers in &index.top_destructuring_member_receivers {
        for receiver in receivers {
            receiver.bindings(&mut needed);
        }
    }
    needed.extend(index.exported_bindings.iter().cloned());
    for exported in &index.top_exported_values {
        for expression in exported {
            expression.bindings(&mut needed);
        }
    }
    let mut dependencies: BTreeMap<Id, BTreeSet<Id>> = BTreeMap::new();
    for assignments in &index.top_assignments {
        for (id, expression) in assignments {
            expression.bindings(dependencies.entry(id.clone()).or_default());
        }
    }
    let mut member_write_receivers = BTreeMap::<Value, BTreeSet<Id>>::new();
    for writes in &index.top_member_value_writes {
        for write in writes {
            let receiver = &write.receiver;
            let expression = &write.value;
            if index.global_object_names.contains(receiver.0.as_ref()) {
                // Global-object writes are potential callback escape roots at
                // every later opaque call, even when the receiver alias is
                // not mentioned by a statically visible callee.
                expression.bindings(&mut needed);
            } else {
                expression.bindings(dependencies.entry(receiver.clone()).or_default());
                // A later call may read an object through a different local
                // alias than the one used for this property write. Retain
                // that receiver's prefix value whenever its known object
                // identity also reaches a needed binding.
                for origin in flow.values_of_binding(receiver) {
                    if matches!(origin, Value::Alloc(Kind::Object, _)) {
                        member_write_receivers
                            .entry(origin)
                            .or_default()
                            .insert(receiver.clone());
                    }
                }
            }
        }
    }
    // A coercion resolves against the values of its operand bindings at the
    // statement's program point.
    let statement_effects: Vec<Effects> = facts
        .iter()
        .map(|fact| proofs.purity.statement_effects(fact))
        .collect();
    for effects in &statement_effects {
        effects.coerce_bindings(&mut needed);
    }
    // A plain array's settled elements decide its at-init fallback.
    needed.extend(
        facts
            .iter()
            .flat_map(|fact| fact.reads.eager.iter())
            .filter(|binding| proofs.graph.is_plain_array(binding.0.as_ref()))
            .cloned(),
    );
    // A statement whose reads a global-object alias may reach resolves them
    // against their settled values.
    needed.extend(
        facts
            .iter()
            .filter(|fact| {
                fact.havoc
                    .contains(&DataflowOpacityCause::GlobalObjectAliasEscape)
            })
            .flat_map(|fact| fact.reads.eager.iter())
            .cloned(),
    );
    // Member reads of a `PlainData` binding resolve against its settled
    // allocations' own properties.
    needed.extend(
        facts
            .iter()
            .flat_map(|fact| fact.declared.iter())
            .filter(|binding| proofs.graph.is_plain_data(binding.0.as_ref()))
            .cloned(),
    );
    needed = close_prefix_needed_bindings(&needed, &dependencies, &member_write_receivers, flow);
    // These indexes only build the needed-binding closure. Release them before
    // retaining the interpreter's prefix state and per-statement outcomes.
    drop(dependencies);
    drop(member_write_receivers);
    let mut writers = BTreeMap::<&Id, BTreeSet<SourceUnitOrdinal>>::new();
    for fact in facts.iter() {
        for binding in fact
            .declared
            .iter()
            .chain(&fact.rebinds.eager)
            .chain(&fact.rebinds.lazy)
        {
            writers.entry(binding).or_default().insert(fact.ordinal);
        }
    }
    let single_writer_bindings: BTreeSet<Id> = writers
        .into_iter()
        .filter(|(_, owners)| owners.len() == 1)
        .map(|(binding, _)| binding.clone())
        .collect();
    let source_unit_read_closure = SourceUnitReadClosure::new(facts);
    let prefix_effects: Vec<_> = facts
        .iter()
        .map(|fact| {
            index.owner_callable_prefix_effects[fact.ordinal.0]
                .as_ref()
                .filter(|effects| {
                    !index.dynamic_scope_owners.contains(&fact.ordinal)
                        && !index
                            .uncovered_callable_effect_owners
                            .contains(&fact.ordinal)
                        && fact.reads.first_order_lazy.is_subset(&effects.reads)
                        && fact.rebinds.first_order_lazy.is_subset(&effects.rebinds)
                })
        })
        .collect();
    let synchronous_owner_adjacency = source_unit_read_closure.synchronous_adjacency(
        facts,
        &prefix_effects
            .iter()
            .map(|effects| effects.map(|effects| &effects.reads))
            .collect::<Vec<_>>(),
    );
    let mut declaration_counts = BTreeMap::<Id, usize>::new();
    for binding in facts.iter().flat_map(|fact| &fact.declared) {
        *declaration_counts.entry(binding.clone()).or_default() += 1;
    }
    let deferred_read_binding = index
        .single_top_lexical_binding
        .iter()
        .enumerate()
        .map(|(ordinal, binding)| {
            binding
                .as_ref()
                .filter(|binding| {
                    declaration_counts.get(*binding) == Some(&1)
                        && facts[ordinal].declared.len() == 1
                        && facts[ordinal].declared.contains(*binding)
                        && index.top_definite_initializers[ordinal].contains(*binding)
                        && !index.imported_bindings.contains(*binding)
                        && !index.hoisted_bindings.contains_key(*binding)
                        && !facts[ordinal].var_declared.contains(*binding)
                })
                .cloned()
        })
        .collect();
    let (reentry_bindings, reentry_default) = match export_reentry_policy {
        ExportReentryPolicy::Conservative => (index.exported_bindings.clone(), true),
        ExportReentryPolicy::NoStaticImporterReentry => (BTreeSet::new(), false),
        ExportReentryPolicy::ReentryThrough(names) => (
            names
                .iter()
                .filter_map(|name| index.exported_names.get(name).cloned())
                .collect(),
            names.contains("default"),
        ),
    };
    let mut exported_owners = BTreeSet::new();
    if *export_reentry_policy == ExportReentryPolicy::Conservative {
        exported_owners.extend(index.exported_declaration_owners.iter().copied());
    }
    if reentry_default {
        exported_owners.extend(index.default_export_owners.iter().copied());
    }
    exported_owners.extend(source_unit_read_closure.source_units_for_bindings(&reentry_bindings));
    let initial_export_roots = exported_owners
        .intersection(&index.hoisted_function_owners)
        .copied()
        .collect();
    let top_bindings: FxHashSet<_> = facts
        .iter()
        .flat_map(|fact| fact.declared.iter().cloned())
        .collect();
    let var_bindings: BTreeSet<_> = facts
        .iter()
        .flat_map(|fact| fact.var_declared.iter().cloned())
        .collect();
    let changed_member_roots: BTreeSet<_> = if index.top_metadata_decorations.iter().any(|call| {
        call.as_ref()
            .is_some_and(|call| !call.member_decorators.is_empty())
    }) {
        facts
            .iter()
            .flat_map(|fact| {
                fact.member_write_roots
                    .iter()
                    .chain(&fact.rebinds.eager)
                    .chain(&fact.rebinds.lazy)
                    .cloned()
            })
            .collect()
    } else {
        BTreeSet::new()
    };
    let mut initial_values = PrefixValues::new(Arc::new(TopBindings::new(
        top_bindings
            .iter()
            .chain(index.hoisted_bindings.keys())
            .cloned(),
    )));
    let mut initialized_bindings = BTreeSet::new();
    for (binding, key) in &index.hoisted_bindings {
        initial_values.insert(
            binding,
            Values::from([AbstractValue::Value(Value::Alloc(Kind::Function, *key))]),
        );
        initialized_bindings.insert(binding.clone());
    }
    let stale_locals: BTreeSet<Id> = index
        .callables
        .values()
        .flat_map(|callable| {
            let (readers, _) = callable.reads_before_assignment();
            callable
                .captured_writes()
                .into_iter()
                .chain(readers)
                .cloned()
                .collect::<Vec<_>>()
        })
        .filter(|binding| !top_bindings.contains(binding))
        .collect();
    let untracked_locals: BTreeSet<Id> = {
        let rebound: BTreeSet<&Id> = index
            .callables
            .values()
            .flat_map(|callable| &callable.rebinds)
            .collect();
        index
            .callables
            .values()
            .flat_map(|callable| {
                let (_, sources) = callable.reads_before_assignment();
                callable
                    .captured_writes()
                    .into_iter()
                    .cloned()
                    .chain(
                        callable
                            .captured_reads()
                            .into_iter()
                            .filter(|binding| rebound.contains(binding))
                            .cloned(),
                    )
                    .chain(sources)
                    .collect::<Vec<_>>()
            })
            .filter(|binding| !top_bindings.contains(binding))
            .collect()
    };
    let inputs = ExecutionInputs {
        index: &index,
        graph: proofs.graph,
        facts,
        statement_effects,
        flow,
        set_iterable_proof: proofs.set_iterable,
        shadowed,
        imported_values: &hints.imported_values,
        imported_primitive_reads: &hints.imported_primitive_reads,
        initializer_call_effect_inputs: &hints.assumptions.initializer_call_effect_inputs,
        declared_pure_initializers: &hints.assumptions.declared_pure_initializers,
        callable_heap_contents: proofs.callable_heap_contents,
        top_bindings,
        binding_owner: facts
            .iter()
            .flat_map(|fact| fact.declared.iter().cloned().map(|id| (id, fact.ordinal)))
            .collect(),
        source_unit_read_closure,
        synchronous_owner_adjacency,
        deferred_read_binding,
        synchronous_owner_effects: prefix_effects,
        exported_owners,
        reentry_bindings,
        reentry_default,
        call_site_owners: index
            .top_calls
            .iter()
            .flatten()
            .chain(
                index
                    .callables
                    .values()
                    .flat_map(|callable| callable.calls.iter()),
            )
            .map(|call| (CallSite::from(call.span), (call.owner, call.span)))
            .collect(),
        reads_arguments: index
            .callable_spans
            .iter()
            .filter(|(_, span)| {
                index.callable_spans.iter().any(|(inner, inner_span)| {
                    span.lo <= inner_span.lo
                        && inner_span.hi <= span.hi
                        && index.callables.get(inner).is_some_and(|callable| {
                            callable.reads.iter().any(|id| id.0 == "arguments")
                        })
                })
            })
            .map(|(key, _)| *key)
            .collect(),
        class_captured_reads: index
            .class_spans
            .iter()
            .map(|(class, span)| {
                let reads = index
                    .callable_spans
                    .iter()
                    .filter(|(_, callable)| span.lo <= callable.lo && callable.hi <= span.hi)
                    .filter_map(|(key, _)| index.callables.get(key))
                    .flat_map(|callable| callable.captured_reads().into_iter().cloned())
                    .collect();
                (*class, reads)
            })
            .collect(),
        mutable_captures: index
            .callables
            .values()
            .flat_map(|callable| callable.rebinds.iter().cloned())
            .collect(),
        stale_locals,
        untracked_locals,
        captured_local_bindings: captured_local_bindings(&index),
        var_bindings,
        changed_member_roots,
        export_reentry_enabled: *export_reentry_policy
            != ExportReentryPolicy::NoStaticImporterReentry,
        needed_bindings: needed,
        single_writer_bindings,
        unknown_callee_owners: unknown_callee_owners(&index, flow),
        reaches_unknown_callee: std::sync::OnceLock::new(),
    };
    let mut world = World::new(WorldScope::Application);
    world.module.prefix_values = initial_values;
    world.module.initialized_bindings = initialized_bindings;
    world.publication.exported_runtime_values = index
        .exported_hoisted_functions
        .iter()
        .copied()
        .map(|key| AbstractValue::Value(Value::Alloc(Kind::Function, key)))
        .collect();
    let state = ExecutionState {
        world: Arc::new(world),
        native_point_effects: index.initial_native_point_effects.clone(),
        activated_native_effects: BTreeSet::new(),
        author_host_contract_uses: Vec::new(),
        class_member_effects: BTreeMap::new(),
        active_class_member_promotions: BTreeSet::new(),
        prior_reachability_roots: Some(initial_export_roots),
        current_top_owner: None,
        proof_uses: Vec::new(),
        returned_record_proof_uses: Vec::new(),
        checking_record_factory_contract: false,
        prior_effect_owners: BTreeSet::new(),
        statement_executed_owners: BTreeSet::new(),
        statement_invoked_callables: BTreeSet::new(),
        array_iteration_depth: 0,
        iteration_element_records: BTreeSet::new(),
        array_iteration_protected_objects: BTreeSet::new(),
        array_iteration_unsupported: false,
        invocation_depth: 0,
        invocation_count: 0,
        callback_fallback: CallbackFallbackSummary::default(),
        fallback_outcome: Outcome::default(),
        assumption: None,
        fallback_callback_sizes: None,
        fallback_local_owners: BTreeSet::new(),
        fallback_local_deferred: BTreeMap::new(),
        capture_contexts: CaptureInterner::default(),
        callback_sets: CaptureInterner::default(),
        active: BTreeSet::new(),
        invocation_memos: OrdMap::new(),
        export_callback_memos: ExportCallbackMemos::default(),
    };
    let results =
        super::runtime_inference::infer_synchronous_execution(&inputs, state, facts.len());
    drop(inputs);
    results
        .into_iter()
        .zip(facts.iter_mut())
        .map(|((calls, purity), fact)| {
            fact.synchronous_calls = Some(calls);
            purity
        })
        .collect()
}

impl Executor<'_, '_> {
    pub(super) fn widen_native_effects(&mut self) {
        if !self.state.world.native.uncertain_execution {
            self.state.native_point_effects = self.inputs.index.all_native_point_effects.clone();
            self.state.world_mut().native.uncertain_execution = true;
        }
    }

    pub(super) fn activate_native_effects(
        &mut self,
        owner: SourceUnitOrdinal,
        callable: Option<u32>,
    ) {
        if self.state.world.native.uncertain_execution
            || !self
                .state
                .activated_native_effects
                .insert((owner, callable))
        {
            return;
        }
        if let Some(effects) = self
            .inputs
            .index
            .native_point_effects
            .get(&(owner, callable))
        {
            self.state.native_point_effects.extend(effects);
        }
    }

    fn observe_native_prefix(&mut self, guard: NativeIntegrityGuard, property: Option<&str>) {
        let prior_blocking_prototype_mutations = self
            .state
            .native_point_effects
            .blocking_prototype_mutations(property);
        self.observations
            .observe_native_prefix(NativePrefixObservation {
                guard,
                program_point_path: self.queries.point_path(),
                property: property.map(str::to_owned),
                prior_affected_roots: self.state.native_point_effects.roots.clone(),
                prior_blocking_prototype_mutations,
                uncertain_execution: self.state.world.native.uncertain_execution,
            });
    }
    /// The elements of each plain-array binding `fact` reads, settled at its
    /// program point: no element is tainted, and none is reachable from any
    /// other binding the statement reads, eagerly or in a nested body.
    pub(super) fn settled_array_elements(
        &self,
        fact: &StatementFacts,
    ) -> BTreeMap<Id, SettledElements> {
        let inputs = self.inputs;
        let reachable = |exclude: &Id| {
            let mut pending: Vec<AbstractValue> = fact
                .reads
                .eager
                .iter()
                .chain(&fact.reads.lazy)
                .filter(|binding| *binding != exclude)
                .filter_map(|binding| self.state.world.module.prefix_values.get(binding))
                .flatten()
                .cloned()
                .collect();
            let mut seen = BTreeSet::new();
            while let Some(value) = pending.pop() {
                if seen.insert(value.clone()) {
                    self.visit_stored_references(&value, true, |reference| {
                        pending.push(reference.clone());
                    });
                }
            }
            seen
        };
        let mut settled = BTreeMap::new();
        for root in &fact.reads.eager {
            if !inputs.graph.is_plain_array(root.0.as_ref())
                || !inputs.single_writer_bindings.contains(root)
                || !inputs.needed_bindings.contains(root)
                || !self.state.world.module.initialized_bindings.contains(root)
            {
                continue;
            }
            let Some(values) = self.state.world.module.prefix_values.get(root) else {
                continue;
            };
            let mut elements = Values::new();
            let arrays = values.iter().all(|value| match value {
                AbstractValue::Value(Value::Alloc(Kind::Object, array)) => {
                    match self.state.world.arrays.dense.get(array) {
                        Some(dense) if self.state.world.object_properties_are_settled(*array) => {
                            elements.extend(dense.slots.iter().flatten().cloned());
                            true
                        }
                        _ => false,
                    }
                }
                _ => false,
            });
            if !arrays || values.is_empty() {
                continue;
            }
            if elements.iter().all(AbstractValue::is_primitive) {
                settled.insert(root.clone(), SettledElements::Primitive);
                continue;
            }
            let others = reachable(root);
            let mut shared_keys: Option<BTreeSet<String>> = None;
            // A primitive element's keys live on its prototype, so a record
            // array has only object elements.
            let records = elements.iter().all(|element| match element {
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    let Some(properties) = self
                        .state
                        .world
                        .surface
                        .prefix_object_properties
                        .get(object)
                    else {
                        return false;
                    };
                    if !inputs.index.plain_data_objects.contains(object)
                        || !self.state.world.object_properties_are_settled(*object)
                        || self
                            .state
                            .world
                            .surface
                            .applied_accessors
                            .contains_key(object)
                        || self.state.world.arrays.dense.contains_key(object)
                        || others.contains(element)
                        || properties.contains_key(&None)
                    {
                        return false;
                    }
                    let keys: BTreeSet<String> = properties
                        .iter()
                        .filter(|(_, values)| values.iter().all(AbstractValue::is_primitive))
                        .filter_map(|(key, _)| key.clone())
                        .collect();
                    shared_keys = Some(match shared_keys.take() {
                        Some(shared) => shared.intersection(&keys).cloned().collect(),
                        None => keys,
                    });
                    true
                }
                _ => false,
            });
            if records {
                settled.insert(
                    root.clone(),
                    SettledElements::Records {
                        shared_keys: shared_keys.unwrap_or_default(),
                    },
                );
            }
        }
        settled
    }

    /// An execution that could not complete may have run code the interpreter
    /// did not follow on any value the statement or its summarized callees
    /// read (purity_soundness § Settled heap taint).
    pub(super) fn unsettle_incomplete_reach(
        &mut self,
        fact: &StatementFacts,
        effects: &SynchronousCalls,
    ) {
        let values: Values = fact
            .reads
            .eager
            .iter()
            .chain(&fact.reads.lazy)
            .chain(effects.reads.iter().map(|(binding, _)| binding))
            .filter_map(|binding| self.state.world.module.prefix_values.get(binding))
            .flatten()
            .cloned()
            .collect();
        self.unsettle(&values);
    }

    /// The top-level bindings `fact` reads eagerly that may hold the global
    /// object at its program point: their settled values include it or a
    /// value the interpreter does not know, or are not complete.
    pub(super) fn global_object_aliases(&self, fact: &StatementFacts) -> Option<BTreeSet<Id>> {
        fact.havoc
            .contains(&DataflowOpacityCause::GlobalObjectAliasEscape)
            .then(|| {
                fact.reads
                    .eager
                    .iter()
                    .filter(|binding| self.inputs.top_bindings.contains(*binding))
                    .filter(|binding| {
                        !self.inputs.single_writer_bindings.contains(*binding)
                            || !self.inputs.needed_bindings.contains(*binding)
                            || self
                                .state
                                .world
                                .module
                                .prefix_values
                                .get(binding)
                                .is_some_and(|values| {
                                    values.iter().any(|value| {
                                        matches!(
                                            value,
                                            AbstractValue::GlobalObject
                                                | AbstractValue::Value(Value::Top)
                                        )
                                    })
                                })
                    })
                    .cloned()
                    .collect()
            })
    }
}
