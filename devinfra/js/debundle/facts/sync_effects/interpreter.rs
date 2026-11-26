//! Call-site execution effects distinguish function values from function bodies: returning a closure never executes it.

mod callback_promotion;
mod dense_arrays;
mod factory_results;
mod local_construction;
mod native_calls;
mod read_only_callbacks;
pub mod synchronous_calls;

use super::adapters::application::{LocalExecutionContext, LocalExecutionScope};
use super::allocation_transfer::MAX_EXACT_ARRAY_SPREAD_SLOTS;
use super::alternatives::execution_state::{ExecutionPath, JoinedPath};
use super::alternatives::{Family, Leaf, join_frames};
use super::array_transfer::ArrayContext;
use super::effect_coverage::EffectCoverageFragment;
use super::execution_index::{
    Call, CallKind, Callable, CallablePrefixEffects, CallbackResultWrite, ExecutionIndex,
    LocalConstructor, MemberValueWrite, NativePointEffects, Parameter, RetainedCallbackCall,
    native_own_data_read_slots, pattern_source,
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
use super::world::World;
use super::world::cells::{CellInitialization, CellTrace};
use super::world::descriptor_heap::{
    self, DescriptorApplication, DescriptorContext, DescriptorHeap, OwnDescriptorMapState,
};
use super::world::object_surface::{AppliedAccessor, ObjectSurfaceStore};
use super::world::prefix_values::{PrefixValues, TopBindingOrdinal};
use crate::property_inference::{
    OwnPropertyProjection, OwnPropertyProof, OwnPropertyRequest, infer_own_property_projection,
};
use analysis::analysis_inputs::{
    ImportedPrimitiveRead, NativeIntrinsic, ProofDependencies, ResolvedInitializerCallEffectInput,
};
use analysis::analysis_provenance::InferenceRule;
use analysis::execution_evidence::{
    AssumedEffectReason, ConstructionRejection, ExecutionOperation, ExecutionPrecisionLoss,
    ExecutionPrecisionLossReason, GlobalReachCause, HavocReach, OrderedCallAdmissionRefusal,
    PossibleGetterLookupSite, PossibleGetterPromotion,
};
use analysis::facts::abstract_domain::builtins::{
    ARRAY_PROTOTYPE_SLOTS, ArgSel, IntrinsicOp, Invocation, IterationResult, Return, Row, Throws,
};
use analysis::facts::abstract_domain::effect::{CoerceOp, CoerceOperand};
use analysis::facts::abstract_domain::heap::Mutation;
use analysis::facts::abstract_domain::integrity::{Integrity, IntrinsicSlot};
use analysis::facts::abstract_domain::value::{Kind, Value};
use analysis::facts::binding_effect_coverage::{
    BindingEffectCoverage, LocalBodyIdentity, LocalCallReceipt, RootInitializerIdentity,
};
use analysis::facts::evaluation_effects::{EvaluationEffects, GlobalAccessKind};
use analysis::facts::local_effect_targets::MetadataDecorateCall;
use analysis::facts::semantic_inference::{
    NativePremise, SemanticKey, names_intrinsic, native_premise,
};
use analysis::facts::source_unit_read_closure::SourceUnitReadClosure;
use analysis::facts::statement_facts::{
    CallbackFactoryUseFact, HeapContentCells, ReturnedRecordUseFact, StatementFacts, StatementKind,
    SynchronousCalls,
};
use analysis::facts::value_flow::{ChunkValueFlow, Values as FlowValues};
use analysis::facts::{
    DataflowOpacityCause, EffectCell, NativeIterableOperandOrigin, SetIterableProof,
};
use analysis::purity::{ChunkCodeGraph, Effects};
use analysis::reports::schema::{
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
        evidence: Box<analysis::analysis_inputs::NativeOperationInputEvidence>,
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
        metrics: &work_metrics::WorkMetrics,
        reason: ExecutionPrecisionLossReason,
        reach: HavocReach,
        owner: SourceUnitOrdinal,
        span: Span,
    ) {
        if metrics.enabled() {
            let (operation, detail) = match &reason {
                ExecutionPrecisionLossReason::UnsupportedOperation {
                    operation,
                    ordered_field_read_refusal,
                    ordered_call_admission_refusal,
                } => (
                    Some(operation.into()),
                    ordered_field_read_refusal
                        .as_ref()
                        .map(Into::into)
                        .or_else(|| ordered_call_admission_refusal.as_ref().map(Into::into)),
                ),
                ExecutionPrecisionLossReason::UnsupportedConstruction { rejection } => {
                    (None, Some(rejection.into()))
                }
                _ => (None, None),
            };
            let global_cause = match &reach {
                HavocReach::Bounded => None,
                HavocReach::Global { cause } => Some(cause.into()),
            };
            metrics.execution_precision_loss((&reason).into(), operation, global_cause, detail);
        }
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
        metrics: &work_metrics::WorkMetrics,
        operation: ExecutionOperation,
        owner: SourceUnitOrdinal,
        span: Span,
    ) {
        self.lose_precision(
            metrics,
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

    pub(super) fn lose_call_target(
        &mut self,
        metrics: &work_metrics::WorkMetrics,
        owner: SourceUnitOrdinal,
        span: Span,
    ) {
        self.lose_precision(
            metrics,
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
            self.effects
                .local_record_factory_uses
                .append(&mut nested.local_record_factory_uses);
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

    fn properties_for(&self, class: u32, object: u32) -> Vec<String> {
        self.constructor_fields
            .iter()
            .filter(|(checked, _)| *checked == class)
            .chain(
                self.missing_input_fields
                    .iter()
                    .filter(|(checked, _)| *checked == object),
            )
            .map(|(_, property)| property.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

pub(super) struct ExecutionInputs<'source> {
    pub(super) metrics: work_metrics::WorkMetrics,
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
    /// Callable reads projected once onto the run's prefix-value ordinals,
    /// retaining binding order. Captured local cells are traversed separately.
    callable_prefix_reads: FxHashMap<u32, Vec<TopBindingOrdinal>>,
    prefix_binding_count: usize,
    binding_owner: BTreeMap<Id, SourceUnitOrdinal>,
    source_unit_read_closure: SourceUnitReadClosure,
    synchronous_owner_adjacency: Vec<Vec<SourceUnitOrdinal>>,
    /// Sealed source-only effect unions, shared across callback requests and
    /// prefixes. Runtime reach, captures and initialization are not cached.
    synchronous_effect_closure: std::sync::OnceLock<SourceUnitReadClosure>,
    /// The only declaration in this owner is one direct lexical binding,
    /// with no other declaration site. Only its incoming read edge may wait.
    deferred_read_binding: Vec<Option<Id>>,
    synchronous_owner_effects: Vec<Option<&'source CallablePrefixEffects>>,
    /// Sorted, borrowed top-level views of the canonical lazy effects used by
    /// static owner promotion. Each view is built once from its source sets.
    synchronous_owner_top_effects: Vec<synchronous_calls::OwnerLazyEffectViews<'source>>,
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

    fn closed_synchronous_effects(
        &self,
        owner: SourceUnitOrdinal,
    ) -> (&BTreeSet<Id>, &BTreeSet<Id>) {
        self.synchronous_effect_closure
            .get_or_init(|| {
                let effects: Vec<_> = (0..self.facts.len())
                    .map(|source| self.synchronous_effects(SourceUnitOrdinal(source), false))
                    .collect();
                self.source_unit_read_closure
                    .with_effects(&self.synchronous_owner_adjacency, &effects)
            })
            .lazy_effects_for_source_unit(owner)
    }

    fn synchronous_top_effects(
        &self,
        owner: SourceUnitOrdinal,
        incomplete_reachability: bool,
    ) -> (&[&Id], &[&Id]) {
        let views = &self.synchronous_owner_top_effects[owner.0];
        let effects = if incomplete_reachability {
            &views.incomplete
        } else {
            views.synchronous.as_ref().unwrap_or(&views.incomplete)
        };
        (&effects.reads, &effects.rebinds)
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
    pub(super) author_host_contract_uses: Vec<analysis::reports::schema::ReentryFreeOperationUse>,
    active_class_member_promotions: BTreeSet<u32>,
    pub(super) prior_reachability_roots: Option<BTreeSet<SourceUnitOrdinal>>,
    pub(super) current_top_owner: Option<SourceUnitOrdinal>,
    pub(super) proof_uses: Vec<CallbackFactoryUseFact>,
    pub(super) returned_record_proof_uses: Vec<ReturnedRecordUseFact>,
    pub(super) checking_record_factory_contract: bool,
    /// Scoped to the exact nested annotated call. It cannot license another
    /// construction of the same class after this call returns.
    nested_record_factory_premise: Option<(u32, u32)>,
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
    native_integrity_uses: BTreeSet<analysis::reports::schema::NativeIntegrityUse>,
    native_operation_input_uses:
        BTreeMap<(u32, u32), analysis::reports::schema::NativeOperationInputUse>,
    imported_primitive_array_spread_input_uses:
        Vec<analysis::analysis_inputs::ImportedPrimitiveArraySpreadInputUse>,
    /// Premises consumed by this owner's proofs so far: the conservative
    /// prerequisite set of its native transfers and record-factory certificate.
    dependencies: ProofDependencies,
    inference_uses: BTreeSet<analysis::analysis_provenance::InferenceUse>,
    /// Exact source-checked receivers whose prototype premises the author supplies.
    record_factory_prototype_premises: RecordFactoryPrototypePremises,
    /// The typed reason an operation of this owner's execution withheld its
    /// effect receipt. Report-only.
    owner_coverage_refusal: Option<analysis::facts::OwnerInitializationCoverageRefusalReason>,
}

impl Observations {
    pub(super) fn refuse_owner_coverage(
        &mut self,
        reason: analysis::facts::OwnerInitializationCoverageRefusalReason,
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

    fn record_native_integrity_use(&mut self, use_: analysis::reports::schema::NativeIntegrityUse) {
        self.native_integrity_uses.insert(use_);
    }

    pub(super) fn record_native_operation_input_use(
        &mut self,
        span: Span,
        use_: analysis::reports::schema::NativeOperationInputUse,
    ) {
        self.native_operation_input_uses
            .insert((span.lo.0, span.hi.0), use_);
    }

    /// One report entry per distinct use; a spread site can run repeatedly.
    fn record_imported_primitive_array_spread_input_use(
        &mut self,
        use_: analysis::analysis_inputs::ImportedPrimitiveArraySpreadInputUse,
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

    pub(super) fn record_inference_use(
        &mut self,
        use_: analysis::analysis_provenance::InferenceUse,
    ) {
        self.inference_uses.insert(use_);
    }

    fn record_inference_uses(
        &mut self,
        uses: impl IntoIterator<Item = analysis::analysis_provenance::InferenceUse>,
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
            nested_record_factory_premise: _,
            array_iteration_depth: _,
            iteration_element_records: _,
            invocation_depth: _,
            active: _,
            assumption: _,
            // Run-wide may-accumulators. A native write, an activated native
            // body or a protected object recorded by one leaf only makes a
            // sibling's native-intact and iteration proofs weaker; executed
            // owners and invoked callables only add fallback roots; the
            // remaining ones are reports.
            native_point_effects: _,
            activated_native_effects: _,
            author_host_contract_uses: _,
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
    author_host_contract_uses: Vec<analysis::reports::schema::ReentryFreeOperationUse>,
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
            // Control state: an owner's body leaves it as it found it.
            active_class_member_promotions: _,
            current_top_owner: _,
            checking_record_factory_contract: _,
            nested_record_factory_premise: _,
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
    /// The checked bodies only store values or read own fields of fresh
    /// plain-data inputs. This is a separate heap judgment: completing a
    /// pure call alone says nothing about its array-content accesses.
    pub(super) heap_local: bool,
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
    pub(super) selected_const_factory_result: bool,
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
        kind: analysis::reports::schema::ReturnedRecordUseKind,
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
        if self
            .state
            .nested_record_factory_premise
            .is_some_and(|(checked, _)| checked == class)
        {
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
        if self
            .state
            .nested_record_factory_premise
            .is_some_and(|(_, checked)| checked == object)
        {
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
        dependencies: &analysis::analysis_inputs::ProofDependencies,
    ) {
        self.consume_proof_dependencies(dependencies);
        if !dependencies.value_inputs.is_empty()
            || !dependencies.no_accessor_install_callables.is_empty()
            || !dependencies.program_premises.is_empty()
        {
            self.observations.record_native_integrity_use(
                analysis::reports::schema::NativeIntegrityUse {
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
        dependencies: &analysis::analysis_inputs::ProofDependencies,
    ) {
        self.queries.assume(dependencies);
        self.observations.consume_dependencies(dependencies);
    }

    pub(super) fn record_inference_use(
        &mut self,
        guard: NativeIntegrityGuard,
        proof: analysis::analysis_provenance::InferenceUse,
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
                    analysis::reports::schema::NativeOperationInputUse {
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

    /// A private fresh instance has not been reachable through any other
    /// source use of its binding. Its checked class field remains an own data
    /// slot, so a different accessor on the class cannot receive this value.
    pub(super) fn private_own_field_read(
        &self,
        receiver: &ValueExpr,
        property: Option<&str>,
        call: &Call,
        values: &Values,
    ) -> bool {
        let (ValueExpr::Binding(binding), Some(property)) = (receiver, property) else {
            return false;
        };
        if !self
            .state
            .world
            .classes
            .private_fresh_instance_bindings
            .contains(binding)
            || !self
                .inputs
                .index
                .sole_binding_reference_at(binding, call.span)
            || !self.inputs.index.single_eager_initializer_read(call)
            || !self.initialized_local_binding(binding)
        {
            return false;
        }
        let [AbstractValue::Instance(class, _, InstanceOrigin::Constructed)] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return false;
        };
        self.inputs
            .index
            .class_values
            .get(class)
            .is_some_and(|facts| {
                facts
                    .instance_fields
                    .get(property)
                    .is_some_and(|value| !matches!(value, ValueExpr::Unknown))
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
            .synchronous_top_effects(owner, self.state.world.reachability.incomplete);
        self.promote_top_effects(reads, rebinds, effect_owner, outcome);
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
        let promoted_reads = self.top_restricted(reads);
        let promoted_rebinds = self.top_restricted(rebinds);
        self.promote_top_effects(&promoted_reads, &promoted_rebinds, effect_owner, outcome);
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
        promoted_reads: &[&Id],
        promoted_rebinds: &[&Id],
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        if self.promotes_nothing(promoted_reads, promoted_rebinds, effect_owner, outcome) {
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
                note_assumed(&mut assumed.reads, promoted_reads, held, reason);
                let held = HeldBindings::Plain(&effects.uninitialized_reads);
                note_assumed(
                    &mut assumed.uninitialized_reads,
                    &uninitialized,
                    held,
                    reason,
                );
                let held = HeldBindings::ByOwner(&effects.rebinds);
                note_assumed(&mut assumed.rebinds, promoted_rebinds, held, reason);
            }
            None => {
                note_observed(&mut assumed.reads, promoted_reads);
                note_observed(&mut assumed.uninitialized_reads, &uninitialized);
                note_observed(&mut assumed.rebinds, promoted_rebinds);
            }
        }
        insert_effects(&mut effects.reads, promoted_reads, |id| {
            (id.clone(), effect_owner)
        });
        insert_effects(&mut effects.uninitialized_reads, &uninitialized, Id::clone);
        insert_effects(&mut effects.rebinds, promoted_rebinds, |id| {
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
            outcome.unsupported(
                &self.inputs.metrics,
                ExecutionOperation::PropertyWrite,
                owner,
                DUMMY_SP,
            );
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
        outcome.unsupported(
            &self.inputs.metrics,
            ExecutionOperation::MemberRead,
            call.owner,
            call.span,
        );
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
    ) -> Option<analysis::analysis_inputs::ForeignCallSummary> {
        use analysis::analysis_inputs::ForeignInvocation;
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
        summary: &analysis::analysis_inputs::ForeignCallSummary,
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
        summary: &analysis::analysis_inputs::ForeignCallSummary,
        args: &[Values],
    ) -> Values {
        use analysis::analysis_inputs::{ForeignInvocation, ForeignReturn};
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
        summary: &analysis::analysis_inputs::ForeignCallSummary,
    ) {
        use analysis::analysis_provenance::{InferenceRule, InferenceUse};
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
                if let ValueExpr::Binding(binding) = expression {
                    let rebound = self.inputs.facts.iter().any(|fact| {
                        fact.rebinds.eager.contains(binding) || fact.rebinds.lazy.contains(binding)
                    });
                    let multiple_origins =
                        self.inputs.flow.values_of_binding(binding).iter().count() > 1;
                    // Whole-chunk flow may retain conservative alternatives
                    // for an imported factory result. At this call, the
                    // synchronous value is still an audited factory carrier;
                    // its cleanliness and non-mutation are checked below.
                    // Do not let that carrier erase binding rebind evidence
                    // or widen ordinary/mixed callback values.
                    let all_factory_carriers = !args[*position].is_empty()
                        && args[*position].iter().all(|value| {
                            matches!(
                                value,
                                AbstractValue::AuditedRetainedResult(
                                    RetainedResultKind::Factory,
                                    _
                                )
                            )
                        });
                    if rebound || (multiple_origins && !all_factory_carriers) {
                        return false;
                    }
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
            outcome.unsupported(
                &self.inputs.metrics,
                ExecutionOperation::PropertyWrite,
                owner,
                DUMMY_SP,
            );
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
        let mut expanded_prefix_reads = vec![false; self.inputs.prefix_binding_count];
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
                    if let Some(reads) = self.inputs.callable_prefix_reads.get(key) {
                        pending.extend(
                            reads
                                .iter()
                                .filter(|binding| {
                                    !std::mem::replace(
                                        &mut expanded_prefix_reads[binding.index()],
                                        true,
                                    )
                                })
                                .filter_map(|binding| {
                                    self.state.world.module.prefix_values.get_ordinal(*binding)
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
        let mut visited = FxHashSet::default();
        while let Some(value) = pending.pop() {
            if visited.contains(&value) {
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
            // Retained children are queued and processed only after this loop
            // iteration, so recording the owned value here is cycle-safe and
            // avoids cloning it into the membership set.
            visited.insert(value);
        }
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
            let reads: Vec<&Id> = this.inputs.binding_owner.keys().collect();
            let rebinds: Vec<&Id> = rebinds.iter().collect();
            this.promote_top_effects(
                &reads,
                &rebinds,
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
            &self.inputs.metrics,
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
    pub(super) fn ordered_root_construction(
        &mut self,
        body: &LocalBody,
        calls: &[Arc<Call>],
    ) -> bool {
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
    pub(super) fn ordered_root_calls_are_native(&self, calls: &[Arc<Call>]) -> bool {
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
                outcome.lose_call_target(&self.inputs.metrics, owner, DUMMY_SP);
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
                &self.inputs.metrics,
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
                &self.inputs.metrics,
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
            outcome.unsupported(&self.inputs.metrics, operation, callable.owner, DUMMY_SP);
        }
        self.state.active.remove(&active_invocation);
        self.state.invocation_depth -= 1;
        self.remember_invocation(signature, &outcome);
        outcome
    }

    pub(super) fn promote_importer_cell_reads(&mut self, call: &Call, outcome: &mut Outcome) {
        if call.kind != CallKind::Call {
            return;
        }
        let ValueExpr::Binding(callee) = &call.callee else {
            return;
        };
        let Some(reads) = self.inputs.index.importer_cell_reads.get(callee) else {
            return;
        };
        // A syntactic descendant may be conditional or deferred. This is a
        // may-read constraint, never observed execution or an effect receipt.
        self.assuming(AssumedEffectReason::OpaqueCallbackInvocation, |this| {
            let reads = this.top_restricted(reads);
            this.promote_top_effects(
                &reads,
                &[],
                this.state.current_top_owner.unwrap_or(call.owner),
                outcome,
            );
        });
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

    /// Whether an object literal, or a prototype its `__proto__` names, has an
    /// accessor that reads `this`. A prototype that is not a known literal
    /// may have one.
    fn literal_chain_may_read_this(&self, object: u32) -> bool {
        let index = self.inputs.index;
        let mut pending = vec![object];
        let mut visited = BTreeSet::new();
        while let Some(object) = pending.pop() {
            if !visited.insert(object) {
                continue;
            }
            if index.this_reading_accessor_objects.contains(&object) {
                return true;
            }
            let Some(prototype) = index.literal_prototypes.get(&object) else {
                continue;
            };
            let values = match prototype {
                ValueExpr::ObjectLiteral(literal, _) => vec![Value::Alloc(Kind::Object, *literal)],
                ValueExpr::Binding(binding) if self.inputs.flow.is_declared(binding) => self
                    .inputs
                    .flow
                    .values_of_binding(binding)
                    .into_iter()
                    .collect(),
                _ => return true,
            };
            for value in values {
                match value {
                    Value::Alloc(Kind::Object, prototype) => pending.push(prototype),
                    Value::Prim(_) => {}
                    _ => return true,
                }
            }
        }
        false
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
                self.literal_chain_may_read_this(*object)
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
            selected_const_factory_result,
            selected_member_dependencies,
            selected_value,
            argument_selections,
        } = supplied.unwrap_or_else(|| SuppliedCallSelections {
            selected_pure_initializer_call: false,
            selected_const_factory_result: false,
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
                analysis::reports::schema::ReturnedRecordUseKind::CallTiming,
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
        // An exact author-pinned root call returns normally with an opaque
        // callable. Its separately asserted no-reentry and purity premises
        // cover timing and completion; argument evaluation has already run.
        // Keep later invocation, member reads and escape conservative.
        if selected_pure_initializer_call
            && selected_const_factory_result
            && needs_value
            && call.kind == CallKind::Call
            && !call.has_spread
            && !operands_may_throw
            && prepared_receiver.is_none()
            && let ValueExpr::Binding(callee) = &call.callee
            && self.inputs.index.no_reentry_exports.contains(callee)
            && matches!(call.args.as_slice(), [ValueExpr::Binding(binding)]
                if self.inputs.index.dynamic_scope_owners.is_empty()
                    && self.closed_primitive_binding(binding))
            && matches!(args.as_slice(), [values]
                if !values.is_empty() && values.iter().all(AbstractValue::is_primitive))
        {
            return CallTransfer::Values(Values::from([self.deferred_callbacks(args[0].clone())]));
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
            outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
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
        let nested_record_factory_premise = if single_target && !operands_may_throw {
            self.checked_nested_record_factory_call(call, &args, env, captures)
        } else {
            None
        };
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
                    outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
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
                    outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
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
                    outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
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
                    outcome.unsupported(
                        &self.inputs.metrics,
                        ExecutionOperation::ObjectAssign,
                        call.owner,
                        call.span,
                    );
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
                    let premises_before = self
                        .observations
                        .record_factory_prototype_premises()
                        .clone();
                    let previous_premise = std::mem::replace(
                        &mut self.state.nested_record_factory_premise,
                        nested_record_factory_premise
                            .as_ref()
                            .filter(|(factory, _, _, _)| *factory == key)
                            .map(|(_, class, object, _)| (*class, *object)),
                    );
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
                    self.state.nested_record_factory_premise = previous_premise;
                    let scoped_factory = nested_record_factory_premise
                        .as_ref()
                        .filter(|(factory_key, _, _, _)| *factory_key == key);
                    if let Some((_, _, _, properties)) = scoped_factory.filter(|_| outcome.complete)
                        && !properties.is_empty()
                    {
                        let ValueExpr::Binding(factory) = &call.callee else {
                            unreachable!()
                        };
                        outcome.effects.local_record_factory_uses.push(
                            analysis::reports::schema::LocalRecordFactoryUse {
                                factory_binding: factory.0.to_string(),
                                prototype_properties: properties.clone(),
                                lexical_statement_ordinal: self
                                    .state
                                    .current_top_owner
                                    .unwrap_or(call.owner),
                                source_location: None,
                                span: call.span,
                            },
                        );
                    }
                    if scoped_factory.is_some() {
                        *self.observations.record_factory_prototype_premises_mut() =
                            premises_before;
                    }
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
                    outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
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
