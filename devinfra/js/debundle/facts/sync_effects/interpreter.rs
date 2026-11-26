//! Call-site execution effects distinguish function values from function bodies: returning a closure never executes it.

mod call_transfer;
mod callback_promotion;
mod dense_arrays;
mod escapes;
mod evaluation;
mod factory_results;
mod invocation;
mod local_construction;
mod native_calls;
mod promotion;
mod proof_records;
mod read_only_callbacks;
mod retained_callbacks;
pub mod synchronous_calls;

use super::adapters::application::{LocalExecutionContext, LocalExecutionScope};
use super::alternatives::execution_state::{ExecutionPath, JoinedPath};
use super::execution_index::{
    Call, CallKind, Callable, CallablePrefixEffects, CallbackResultWrite, ExecutionIndex,
    LocalConstructor, MemberValueWrite, NativePointEffects, Parameter, RetainedCallbackCall,
    native_own_data_read_slots, pattern_source,
};
use super::native::operation::{
    self as native_operation, CheckedNativePlan, NativeOperationCommit, NativeOperationContext,
};
use super::noninvoking_initialization::{
    NativeConstructorCandidate, NativeConstructorForm, NativeConstructorKind,
    NativeConstructorTransfer, OriginalInitializer,
};
use super::runtime_callbacks::{
    BoundedInvocationFallbackDiagnostics, BoundedInvocationFallbackRequest, CallbackOperation,
    CallbackResult,
};
use super::runtime_heap::{HeapOperation, HeapResult};
use super::runtime_inference::{
    CallTransfer, ProgramPoint, RuntimeOperation, RuntimeQueries, RuntimeValue,
};
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
    CallbackFactoryUseFact, HeapContentCells, ReturnedRecordUseFact, StatementFacts,
    SynchronousCalls,
};
use analysis::facts::value_flow::{ChunkValueFlow, Values as FlowValues};
use analysis::facts::{
    DataflowOpacityCause, EffectCell, NativeIterableOperandOrigin, SetIterableProof,
};
use analysis::purity::ChunkCodeGraph;
use analysis_domain::builtins::Mutation;
use analysis_domain::builtins::{
    ARRAY_PROTOTYPE_SLOTS, ArgSel, IntrinsicOp, Invocation, IterationResult, Return, Row, Throws,
};
use analysis_domain::integrity::{Integrity, IntrinsicSlot};
use analysis_domain::item_views::StatementKind;
use analysis_model::PrimKinds;
use analysis_vocabulary::effect::{CoerceOp, CoerceOperand};
use analysis_vocabulary::execution_evidence::{
    AssumedEffectReason, ConstructionRejection, ExecutionOperation, ExecutionPrecisionLoss,
    ExecutionPrecisionLossReason, GlobalReachCause, HavocReach, OrderedCallAdmissionRefusal,
    PossibleGetterLookupSite, PossibleGetterPromotion,
};
use analysis_vocabulary::inputs::{
    ImportedPrimitiveRead, NativeIntrinsic, ProofDependencies, ResolvedInitializerCallEffectInput,
};
use analysis_vocabulary::provenance::InferenceRule;
use analysis_vocabulary::purity::Effects;
use analysis_vocabulary::report_schema::{
    BoundedInvocationFallbackCallSite, BoundedInvocationFallbackInputPath,
    BoundedInvocationFallbackPathEdge, BoundedInvocationFallbackSeed,
    BoundedInvocationFallbackUnknownPathReason, CallbackFactoryUseKind,
    ClassMembersPublicationWriter, FirstClassMembersPublication, LocalRecordFactoryRejection,
    NativeIntegrityCheck, NativeIntegrityGuard, NativePrefixObservation,
    NativePrototypePropertyCheck, RecordFactoryRejectionReason,
};
use analysis_vocabulary::value::{Kind, Value};
use imbl::OrdMap;
use kernel::alternatives::{Family, Leaf, join_frames};
use kernel::closure::{CapturePolicy, lexical_captures};
use kernel::construct;
use kernel::context::{ControlContext, IndexReads};
use kernel::continuation::LocalContinuation;
use kernel::driver;
use kernel::frame::{LocalExecutionAttempt, LocalFrame, RemainderDisposition, SelectedValue};
use rustc_hash::{FxHashMap, FxHashSet};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use swc_common::{DUMMY_SP, Span, SyntaxContext};
use swc_ecma_ast::*;
use value_model::local_operations::{LocalBinding, LocalBody};
use value_model::own_data_transfer::{self, OwnDataContext, OwnDataOwnership, OwnDataRead};
use value_model::property_inference::{
    OwnPropertyProjection, OwnPropertyProof, OwnPropertyRequest, infer_own_property_projection,
};
use value_model::{
    AbstractValue, ArrayIteration, CallSite, CallableSelection, CaptureInterner, Captured,
    CapturedContext, Context, DenseArray, DenseArrayIterationTransfer, HeapAddress, InstanceOrigin,
    LexicalCaptures, LexicalCellId, MemberKey, OwnPropertyShape, RetainedResultKind, ValueExpr,
    Values, canonical_array_index, ordered_values,
};
use world::World;
use world::allocation_transfer::MAX_EXACT_ARRAY_SPREAD_SLOTS;
use world::allocation_transfer::NativeIterableUse;
use world::allocation_transfer::{
    NativeIterableInput, NativeIterableReceipt, NativeIterableYields,
};
use world::array_transfer::ArrayContext;
use world::cells::{CellInitialization, CellTrace};
use world::descriptor_heap::{
    self, DescriptorApplication, DescriptorContext, DescriptorHeap, OwnDescriptorMapState,
};
use world::effect_coverage::EffectCoverageFragment;
use world::object_surface::{AppliedAccessor, ObjectSurfaceStore};
use world::prefix_values::{PrefixValues, TopBindingOrdinal};

pub(super) enum NativeIdentitySource {
    Inferred {
        premise: Box<NativePremise>,
    },
    AuthorInput {
        evidence: Box<analysis_vocabulary::inputs::NativeOperationInputEvidence>,
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
    pub(super) author_host_contract_uses:
        Vec<analysis_vocabulary::report_schema::ReentryFreeOperationUse>,
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
    native_integrity_uses: BTreeSet<analysis_vocabulary::report_schema::NativeIntegrityUse>,
    native_operation_input_uses:
        BTreeMap<(u32, u32), analysis_vocabulary::report_schema::NativeOperationInputUse>,
    imported_primitive_array_spread_input_uses:
        Vec<analysis_vocabulary::inputs::ImportedPrimitiveArraySpreadInputUse>,
    /// Premises consumed by this owner's proofs so far: the conservative
    /// prerequisite set of its native transfers and record-factory certificate.
    dependencies: ProofDependencies,
    inference_uses: BTreeSet<analysis_vocabulary::provenance::InferenceUse>,
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

    fn record_native_integrity_use(
        &mut self,
        use_: analysis_vocabulary::report_schema::NativeIntegrityUse,
    ) {
        self.native_integrity_uses.insert(use_);
    }

    pub(super) fn record_native_operation_input_use(
        &mut self,
        span: Span,
        use_: analysis_vocabulary::report_schema::NativeOperationInputUse,
    ) {
        self.native_operation_input_uses
            .insert((span.lo.0, span.hi.0), use_);
    }

    /// One report entry per distinct use; a spread site can run repeatedly.
    fn record_imported_primitive_array_spread_input_use(
        &mut self,
        use_: analysis_vocabulary::inputs::ImportedPrimitiveArraySpreadInputUse,
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
        use_: analysis_vocabulary::provenance::InferenceUse,
    ) {
        self.inference_uses.insert(use_);
    }

    fn record_inference_uses(
        &mut self,
        uses: impl IntoIterator<Item = analysis_vocabulary::provenance::InferenceUse>,
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
        allocation_judgment: &world::allocation_transfer::AllocationJudgment,
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
    ) -> (world::allocation_transfer::AllocationJudgment, Outcome) {
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
    author_host_contract_uses: Vec<analysis_vocabulary::report_schema::ReentryFreeOperationUse>,
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
        kernel::alternatives::separate_from_abandoned(&attempted, self.state.world_mut());
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
    pub(super) allocation_judgment: world::allocation_transfer::AllocationJudgment,
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
            Some(world::allocation_transfer::WritablePrefixState::Proven(properties)) => {
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
            Some(world::allocation_transfer::WritablePrefixState::DenseArray { length }) => {
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
            world::allocation_transfer::WritablePrefixState::DenseArray { length } => Some(*length),
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
            world::allocation_transfer::WritablePrefixState::DenseArray { length },
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
            allocation_judgment: world::allocation_transfer::AllocationJudgment::Opaque,
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
        world::allocation_transfer::AllocationJudgment,
        EvaluationEffects,
    ) {
        if let world::allocation_transfer::AllocationJudgment::CoveredAllocationOnly(dependencies) =
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

    pub(super) fn observe_native_prefix(
        &mut self,
        guard: NativeIntegrityGuard,
        property: Option<&str>,
    ) {
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
