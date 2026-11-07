use super::*;
use crate::execution_evidence::{AssumedEffectReason, ExecutionPrecisionLoss};
use source_identity::{ModuleCellId, SourceUnitOrdinal};

/// One value per syntactic position bucket. Collapses the repeated
/// eager / lazy / first-order-lazy triples (reads, rebinds, calls)
/// into a single shape so the "first-order ⊆ lazy" subset invariant
/// lives in one place ([`Self::record`]) instead of at every
/// construction site.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PositionBucketed<T> {
    /// At-init position: outside any function/arrow/method/getter/
    /// setter body, constructor body, or instance class-field
    /// initializer.
    pub eager: T,
    /// Inside a lazy syntactic position, at any nesting depth. May
    /// overlap with `eager` if the same name appears in both eager
    /// and lazy positions of the statement.
    pub lazy: T,
    /// Subset of `lazy` whose sites sit in a function's
    /// **first-order, pre-await** body (depth 1 from this statement).
    /// Used by at-init call promotion: a synchronous call to the
    /// function only runs its immediate pre-await body, so sites
    /// inside nested function/arrow definitions or past an `await`
    /// don't promote to the caller.
    pub first_order_lazy: T,
}

impl PositionBucketed<BTreeSet<Id>> {
    /// Record `id` in the bucket the cursor position selects: `eager`
    /// at depth 0; `lazy` at depth ≥ 1, additionally `first_order_lazy`
    /// at depth 1 before the body's first `await`. Maintains the
    /// `first_order_lazy ⊆ lazy` invariant structurally.
    pub(crate) fn record(&mut self, id: &Id, lazy_depth: u32, past_await: bool) {
        if lazy_depth == 0 {
            self.eager.insert(id.clone());
            return;
        }
        self.lazy.insert(id.clone());
        if lazy_depth == 1 && !past_await {
            self.first_order_lazy.insert(id.clone());
        }
    }

    pub(crate) fn extend(&mut self, other: Self) {
        self.eager.extend(other.eager);
        self.lazy.extend(other.lazy);
        self.first_order_lazy.extend(other.first_order_lazy);
    }
}

#[derive(Debug, Clone)]
pub struct StatementFacts {
    pub ordinal: SourceUnitOrdinal,
    /// Local linkage executes no value expression. Dependency-bearing
    /// re-exports additionally require their dependency's evaluation order.
    pub(crate) export_linkage: Option<ExportLinkage>,
    /// Exact protected iterable producers that must remain in this module.
    pub(crate) stable_iterable_producers: BTreeSet<SourceUnitOrdinal>,
    pub source_location: Option<SourceLocation>,
    /// Matched retained callback-factory contracts rejected by a visible
    /// namespace guard for a top-level call in this source unit. These facts
    /// explain conservative fallback; they never affect the analysis result.
    pub(crate) callback_factory_barriers: Vec<CallbackFactoryBarrierFact>,
    pub declared: BTreeSet<Id>,
    /// Bindings introduced inside this statement's lexical scopes (parameters,
    /// local declarations). Used to distinguish callback calls through local
    /// values from unresolved globals during at-init promotion.
    pub local_bindings: BTreeSet<Id>,
    /// Module-scoped bindings declared with `var`, including inside control flow.
    pub var_declared: BTreeSet<Id>,
    /// Actual initializer writes; a bare `var x;` does not reset x.
    pub var_initializers: BTreeSet<Id>,
    /// Identifier reads, bucketed by syntactic position.
    pub reads: PositionBucketed<BTreeSet<Id>>,
    /// Rebinding writes, bucketed by syntactic position. Member
    /// writes (`obj.x = ...`) are intentionally excluded: mutating an
    /// imported object is legal, but rebinding the imported binding
    /// cell is not.
    pub rebinds: PositionBucketed<BTreeSet<Id>>,
    /// Direct member writes on binding roots, across eager and lazy syntax.
    pub(crate) member_write_roots: BTreeSet<Id>,
    /// Target-local mutations produced by recognized trusted helper
    /// calls. Each binding is the class/prototype source_unit that must
    /// co-locate with the mutating statement.
    pub local_effects: BTreeSet<Id>,
    /// Statically identified call and constructor targets, bucketed by
    /// syntactic position. The collector records bare identifiers; the
    /// chunk value-flow pass adds identities reached through aliases,
    /// parameters, returns and static object properties. The dependency graph
    /// build uses `eager` for conservative at-init promotion when call-site
    /// execution cannot be bounded, and `lazy` to reconstruct the chunk
    /// call graph so promotion follows call chains (e.g.
    /// `function f() { g(); } f();` promotes through `g`'s body too).
    /// `first_order_lazy` keeps calls nested inside a closure of the
    /// body from appearing as direct callees of the outer function.
    pub calls: PositionBucketed<BTreeSet<Id>>,
    /// The subset of a class statement's lazy effects that `new` executes:
    /// constructor body, instance fields and superclass constructor. Method
    /// bodies remain lazy until invoked by that constructor or another call.
    pub(crate) constructor: Option<ConstructorFacts>,
    /// Effects of function bodies that can run while this statement evaluates.
    /// Proven effects are retained even when some targets remain unresolved;
    /// `complete` decides whether the conservative fallback also runs.
    pub(crate) synchronous_calls: Option<SynchronousCalls>,
    /// Bindings referenced in the callee or arguments of at-init
    /// calls promotion can't follow (member calls `api.read()`,
    /// optional-chain calls, tagged templates). A function value
    /// invoked by such a call must have flowed through one of these
    /// bindings — or through an inline function expression (see
    /// `at_init_unresolved_inline_fn`) — so `promote_at_init_calls`
    /// makes the statement eagerly depend on the transitive lazy
    /// closures of the chunk-declared subset.
    pub at_init_unresolved_sources: BTreeSet<Id>,
    /// `true` when an unresolved at-init call carries an inline
    /// function/arrow/class expression: the statement's own lazy
    /// closures may fire synchronously (IIFE,
    /// `arr.forEach(x => ...)`).
    pub at_init_unresolved_inline_fn: bool,
    /// First-order pre-await body counterpart of
    /// `at_init_unresolved_sources`; propagated to at-init callers
    /// through the promotion call graph.
    pub first_order_unresolved_sources: BTreeSet<Id>,
    /// First-order pre-await body counterpart of
    /// `at_init_unresolved_inline_fn`.
    pub first_order_unresolved_inline_fn: bool,
    /// `true` when the statement's declared binding is directly a
    /// function value: a `function` declaration (incl. exported and
    /// `export default` forms) or a single-declarator
    /// `var/let/const` whose initializer is a function/arrow
    /// expression. Promotion treats only at-init calls to such
    /// bindings (when never rebound) as precisely resolvable;
    /// everything else takes the conservative fallback.
    pub declares_direct_function: bool,
    /// Static-key `globalThis.<prop>` cells the statement writes
    /// at-init (`globalThis.tag = ...` records `"tag"`). A dynamic key is a
    /// `DynamicCellWrites` havoc instead.
    pub global_writes: BTreeSet<String>,
    /// Static-key `globalThis.<prop>` cells the statement reads
    /// at-init.
    pub global_reads: BTreeSet<String>,
    /// Source-classifier reasons the statement's cells may be incomplete.
    /// `dataflow_summarizable` also checks exact operation authority before
    /// retaining the every-cell fallback, and the graph retains
    /// the causes on the edges that fallback emits. See `README.md` →
    /// "Conditionally-correct optimizations" for the soundness
    /// precondition.
    pub havoc: BTreeSet<DataflowOpacityCause>,
    /// Heap-content cells of the statement's own at-init syntax. These are
    /// distinct from binding effects: mutating an array changes its contents,
    /// not the binding that names it. Its calls' cells are
    /// `SynchronousCalls::heap_contents`.
    pub(crate) heap_contents: HeapContentCells,
    /// The whole initializer of a single-declarator top-level declaration,
    /// when it is an array literal or a proved fresh native Set construction:
    /// this direct eager occurrence allocates at this statement.
    pub(crate) heap_one_shot_allocation: Option<EffectCell>,
    /// Allocation sites in this statement's deferred positions, at any depth.
    pub(crate) deferred_heap_allocations: BTreeSet<EffectCell>,
    /// Native premises consumed by proved heap construction at this source unit.
    pub heap_native_integrity_uses: Vec<crate::reports::schema::NativeIntegrityUse>,
    pub purity: Purity,
    pub kind: StatementKind,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CallbackFactoryBarrierFact {
    pub namespace_binding: String,
    pub member: String,
    pub argument_count: usize,
    pub callback_arguments: BTreeSet<usize>,
    /// Exact callback argument roots known from whole-chunk value flow. `None`
    /// means the argument shape was not statically resolvable for this query.
    pub callback_source_units: Option<BTreeSet<SourceUnitOrdinal>>,
    pub rejection: CallbackFactoryBarrierRejection,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum CallbackFactoryBarrierRejection {
    NamespaceMemberWrite {
        witness: CallbackFactoryWriteWitness,
    },
    NamespaceDynamicPropertyWrite {
        witness: CallbackFactoryWriteWitness,
    },
    NamespaceCallArgument {
        witness: CallbackFactoryCallArgumentWitness,
    },
    UnsupportedGuard,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CallbackFactoryCallArgumentWitness {
    pub statement_ordinal: SourceUnitOrdinal,
    pub call_span: swc_common::Span,
    pub argument_index: usize,
    pub namespace_binding: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CallbackFactoryWriteWitness {
    pub statement_ordinal: SourceUnitOrdinal,
    pub receiver_binding: String,
    pub property: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct SynchronousCalls {
    pub initializer_binding_effects:
        Option<Box<crate::reports::schema::InitializerBindingEffectsUse>>,
    /// Whole-original-owner coverage, sealed after its checked root evaluation.
    pub(crate) completed_initialization:
        Option<super::sync_effects::noninvoking_initialization::CompletedInitialization>,
    /// Keep proof dependencies off recursive `Outcome`/`SynchronousCalls`
    /// frames. The box is transparent in saved analysis serialization.
    /// Exact-owner normal-completion evidence for the initialization control
    /// fence is independent of effect-domain closure.
    pub(crate) root_normal_completion:
        Option<Box<super::sync_effects::noninvoking_initialization::RootNormalCompletion>>,
    /// Exact root-level outward known throw, distinct from potential
    /// suppression inferred from missing normal-completion evidence.
    /// Boxed so its proof dependencies do not inflate recursive outcome frames.
    pub(crate) root_outward_throw:
        Option<Box<super::sync_effects::noninvoking_initialization::RootOutwardThrow>>,
    /// Report-only reason the original owner's whole-initialization proof was
    /// refused. This never changes the conservative effect projection.
    pub(crate) initialization_coverage_refusal:
        Option<crate::facts::OwnerInitializationCoverageRefusalReason>,
    /// Report-only, source-site may-accesses; never a legacy graph input.
    pub(crate) evaluation_effects: super::evaluation_effects::EvaluationEffects,
    // SavedSynchronousCalls transports both verdicts through the artifact's
    // shared purity graph; the report projects this runtime field separately.
    pub purity_disagreement: Option<crate::reports::schema::PurityCompletionDisagreement>,
    pub local_record_factory_uses: Vec<crate::reports::schema::LocalRecordFactoryUse>,
    pub local_record_factory_rejections: Vec<crate::reports::schema::LocalRecordFactoryRejection>,
    /// Internal call-site certificate produced only after the checked local
    /// constructor-factory proof and ordinary synchronous execution both
    /// succeed. It is deliberately separate from the serialized proof-use
    /// report: report data is not an analysis input.
    pub local_record_factory_purity: Option<LocalRecordFactoryPurityCertificate>,
    pub reads: BTreeSet<(Id, SourceUnitOrdinal)>,
    pub rebinds: BTreeSet<(Id, SourceUnitOrdinal)>,
    /// Bindings a synchronous callee can observe before their source-order
    /// initializer has executed. This is a subset of `reads`; it distinguishes
    /// a real prefix value from a TDZ/initial-`undefined` observation so the
    /// dependency graph can preserve that observation point.
    pub uninitialized_reads: BTreeSet<Id>,
    /// Bindings in `reads`, `rebinds` and `uninitialized_reads` that only
    /// conservative paths contributed, each with the first such path.
    pub fallback_only: FallbackOnlyEffects,
    pub complete: bool,
    /// Engine-owned authority transported with staged `ChunkFactAnalysis`.
    /// Its saved representation requires this field so a cache missing typed
    /// authority cannot regain `.complete` as legacy authority. Public report
    /// DTOs are separate diagnostics.
    pub(crate) binding_effect_coverage: super::sync_effects::BindingEffectCoverage,
    /// What each plain-array binding the statement reads holds at its program
    /// point, when its elements are known.
    pub(crate) settled_array_elements: BTreeMap<Id, SettledElements>,
    /// For a statement with the `GlobalObjectAliasEscape` havoc: the
    /// top-level bindings it reads eagerly that may hold the global object
    /// where it starts (`None` when execution did not settle them).
    pub(crate) global_object_aliases: Option<BTreeSet<Id>>,
    /// Heap-content cells of the callables this statement invoked.
    pub(crate) heap_contents: HeapContentCells,
    /// Conditional imported bodies whose checked module-cell reads and writes
    /// are included in this execution require their source initialization to
    /// precede every candidate invocation.
    pub(crate) required_initialized_module_cells: BTreeSet<ModuleCellId>,
    /// Conditional imported-body transfers require the emitted candidate to
    /// preserve the exact imported binding route to the analyzed provider,
    /// even when the body has no module-cell footprint.
    pub(crate) conditional_provider_targets: Vec<ConditionalProviderTargetRequirement>,
    /// Heap reads and writes reached without an assumed callback or fallback
    /// path. This is a subset of `heap_contents` and is used only to check
    /// explicit commuting premises; the conservative heap order uses all cells.
    pub(crate) non_assumed_heap_accesses: HeapContentAccesses,
    /// Typed descriptor/shape support for admitted own-data reads and stores.
    /// This is report/read-footprint evidence, not effect-domain authority.
    pub(crate) operation_support_receipts:
        Option<Box<[super::sync_effects::OperationSupportReceipt]>>,
    pub precision_losses: Vec<ExecutionPrecisionLoss>,
    pub native_integrity_checks: Vec<crate::reports::schema::NativeIntegrityCheck>,
    pub native_prototype_property_checks: Vec<crate::reports::schema::NativePrototypePropertyCheck>,
    pub native_prefix_observations: Vec<crate::reports::schema::NativePrefixObservation>,
    pub native_integrity_uses: Vec<crate::reports::schema::NativeIntegrityUse>,
    pub native_operation_input_uses: Vec<crate::reports::schema::NativeOperationInputUse>,
    pub inference_uses: Vec<crate::analysis_provenance::InferenceUse>,
    pub callback_factory_uses: Vec<CallbackFactoryUseFact>,
    pub returned_record_uses: Vec<ReturnedRecordUseFact>,
    pub author_host_contract_uses: Vec<crate::reports::schema::ReentryFreeOperationUse>,
}

/// Source identity for one successfully applied conditional imported-body
/// transfer. This is an obligation to prove the same binding route in the
/// prepared candidate; it does not itself prove target preservation.
/// Source binding and provider identity for an applied conditional imported-body transfer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConditionalProviderTargetRequirement {
    /// Source-unit ordinal containing the callsite.
    pub owner: SourceUnitOrdinal,
    /// Exact source callsite span used to join candidate requests.
    pub call_span: Span,
    /// Hygiene-aware source Id of the imported callee.
    pub source_binding: Id,
    /// Source chunk namespace that owns the analyzed function.
    pub defining_module: String,
    /// Export name through which the function was resolved.
    pub export: String,
    /// Provider-local binding name for the resolved function.
    pub local: String,
    /// Analysis identity for the resolved callable body.
    pub callable_key: u32,
}

/// Binding effects a statement's synchronous summary assumed rather than
/// derived: contributed only by a conservative path that assumes code may run
/// at the statement ([`AssumedEffectReason`]). An effect precise execution of
/// code the statement definitely runs also reached is not listed: an effect
/// both observed and assumed is observed. While the interpreter builds an
/// invocation's outcome the maps are relative to that invocation; the
/// enclosing statement re-tags them when an assumed path merges them.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub(crate) struct FallbackOnlyEffects {
    pub reads: BTreeMap<Id, AssumedEffectReason>,
    pub rebinds: BTreeMap<Id, AssumedEffectReason>,
    pub uninitialized_reads: BTreeMap<Id, AssumedEffectReason>,
}

impl SynchronousCalls {
    pub(crate) fn record_operation_support(
        &mut self,
        receipt: super::sync_effects::OperationSupportReceipt,
    ) {
        if self
            .operation_support_receipts
            .as_deref()
            .is_some_and(|receipts| receipts.contains(&receipt))
        {
            return;
        }
        let mut receipts = self
            .operation_support_receipts
            .take()
            .map(|receipts| receipts.into_vec())
            .unwrap_or_default();
        receipts.push(receipt);
        self.operation_support_receipts = Some(receipts.into_boxed_slice());
    }

    pub(crate) fn operation_support_receipts(
        &self,
    ) -> &[super::sync_effects::OperationSupportReceipt] {
        self.operation_support_receipts.as_deref().unwrap_or(&[])
    }

    pub(crate) fn record_heap_contents(&mut self, cells: &HeapContentCells, non_assumed: bool) {
        self.heap_contents.extend(cells);
        if non_assumed {
            self.non_assumed_heap_accesses.extend_cells(cells);
        }
    }

    pub(crate) fn record_heap_read(&mut self, cell: EffectCell, non_assumed: bool) {
        self.heap_contents.reads.insert(cell.clone());
        if non_assumed {
            self.non_assumed_heap_accesses.reads.insert(cell);
        }
    }

    pub(crate) fn record_heap_write(&mut self, cell: EffectCell, non_assumed: bool) {
        self.heap_contents.writes.insert(cell.clone());
        if non_assumed {
            self.non_assumed_heap_accesses.writes.insert(cell);
        }
    }

    pub(crate) fn binding_effect_authority(&self) -> super::sync_effects::BindingEffectAuthority {
        let author_source_only = matches!(
            self.initializer_binding_effects.as_deref(),
            Some(crate::reports::schema::InitializerBindingEffectsUse::Applied { .. })
        );
        self.binding_effect_coverage
            .authority(self.complete, author_source_only)
    }

    pub(crate) fn binding_effect_authority_report(
        &self,
    ) -> crate::reports::schema::BindingEffectAuthorityReport {
        let author_source_only = matches!(
            self.initializer_binding_effects.as_deref(),
            Some(crate::reports::schema::InitializerBindingEffectsUse::Applied { .. })
        );
        self.binding_effect_coverage
            .authority_report(self.complete, author_source_only)
    }

    /// The assumed path when only a conservative path contributed the read
    /// of `binding`; `None` when it was observed.
    pub(crate) fn fallback_read(&self, binding: &Id) -> Option<&AssumedEffectReason> {
        self.fallback_only.reads.get(binding)
    }

    /// As [`Self::fallback_read`], for a rebind of `binding`.
    pub(crate) fn fallback_rebind(&self, binding: &Id) -> Option<&AssumedEffectReason> {
        self.fallback_only.rebinds.get(binding)
    }

    /// As [`Self::fallback_read`], for an observation of `binding` before
    /// its initializer ran.
    pub(crate) fn fallback_uninitialized_read(&self, binding: &Id) -> Option<&AssumedEffectReason> {
        self.fallback_only.uninitialized_reads.get(binding)
    }
}

/// Source- and execution-checked evidence that one exact factory call only
/// allocates the validated local constructor graph. The settled purity
/// projection consumes this to discharge only the impure-callee effect at
/// `call_span`; factory and class purity summaries remain unchanged. An
/// author assertion may supply otherwise-unproved prototype premises, but
/// native/source evidence can establish them without an assertion.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct LocalRecordFactoryPurityCertificate {
    pub call_span: swc_common::Span,
    pub checked_owners: BTreeSet<SourceUnitOrdinal>,
    pub dependencies: crate::analysis_inputs::ProofDependencies,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CallbackFactoryUseFact {
    pub imported_local: String,
    pub evidence: Vec<crate::analysis_inputs::CallbackFactoryEvidence>,
    pub kind: crate::reports::schema::CallbackFactoryUseKind,
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<crate::execution_evidence::SourceLocation>,
    pub span: swc_common::Span,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ReturnedRecordUseFact {
    pub imported_local: String,
    pub evidence: crate::analysis_inputs::ReturnedRecordSummary,
    pub kind: crate::reports::schema::ReturnedRecordUseKind,
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<crate::execution_evidence::SourceLocation>,
    pub span: swc_common::Span,
}

impl StatementFacts {
    /// Module cells whose source initialization the statement's checked
    /// conditional imported-body transfer requires. These are identities
    /// only; the iterator grants no authority over cell contents or writes.
    pub fn required_initialized_module_cells(&self) -> impl Iterator<Item = ModuleCellId> + '_ {
        self.synchronous_calls
            .iter()
            .flat_map(|calls| calls.required_initialized_module_cells.iter().copied())
    }

    /// Binding and provider identities consumed by applied conditional body
    /// transfers. These remain required even when no module cells were read.
    pub fn required_conditional_provider_targets(
        &self,
    ) -> impl Iterator<Item = &ConditionalProviderTargetRequirement> + '_ {
        self.synchronous_calls
            .iter()
            .flat_map(|calls| calls.conditional_provider_targets.iter())
    }

    /// What one synchronous call of the function or class this statement
    /// declares runs first-order (before nested closures and `await`):
    /// a class runs its constructor. The view at-init call promotion uses.
    pub fn first_order_reads(&self) -> &BTreeSet<Id> {
        self.constructor
            .as_ref()
            .map_or(&self.reads.first_order_lazy, |ctor| &ctor.reads)
    }

    /// See [`Self::first_order_reads`].
    pub fn first_order_rebinds(&self) -> &BTreeSet<Id> {
        self.constructor
            .as_ref()
            .map_or(&self.rebinds.first_order_lazy, |ctor| &ctor.rebinds)
    }

    /// See [`Self::first_order_reads`].
    pub fn first_order_calls(&self) -> &BTreeSet<Id> {
        self.constructor
            .as_ref()
            .map_or(&self.calls.first_order_lazy, |ctor| &ctor.calls)
    }

    /// See [`Self::first_order_reads`].
    pub fn first_order_unresolved_sources(&self) -> &BTreeSet<Id> {
        self.constructor
            .as_ref()
            .map_or(&self.first_order_unresolved_sources, |ctor| {
                &ctor.unresolved_sources
            })
    }

    /// See [`Self::first_order_reads`].
    pub fn first_order_unresolved_inline_fn(&self) -> bool {
        self.constructor
            .as_ref()
            .map_or(self.first_order_unresolved_inline_fn, |ctor| {
                ctor.unresolved_inline_fn
            })
    }

    /// Per-statement (writes, reads) cell summary used by the
    /// dataflow-aware S-chain emission in `graph/` and the vendor
    /// strip's swap-privacy gate. Derived on demand: the
    /// `Binding`-cell half combines declaration initialization, eager reads,
    /// and rebinds. Bare `var` redeclarations are not writes; `var_initializers`
    /// records their actual initialization sites. The `GlobalProp` half (`global_writes` /
    /// `global_reads`) is stored state.
    pub fn effects(&self) -> StatementEffectSummary {
        let mut writes = BTreeSet::<EffectCell>::new();
        for name in self
            .declared
            .difference(&self.var_declared)
            .chain(self.var_initializers.iter())
            .chain(self.rebinds.eager.iter())
        {
            writes.insert(EffectCell::Binding(name.clone()));
        }
        for key in &self.global_writes {
            writes.insert(EffectCell::GlobalProp(key.clone()));
        }
        let mut reads = BTreeSet::<EffectCell>::new();
        for name in &self.reads.eager {
            reads.insert(EffectCell::Binding(name.clone()));
        }
        for key in &self.global_reads {
            reads.insert(EffectCell::GlobalProp(key.clone()));
        }
        writes.extend(self.heap_contents.writes.iter().cloned());
        reads.extend(self.heap_contents.reads.iter().cloned());
        StatementEffectSummary { writes, reads }
    }

    /// Preserve the correspondence between this chunk's lexical names and
    /// resolved module storage. These are may-accesses, never coverage or
    /// initialized-value evidence, and retain the original binding effects.
    pub(crate) fn record_module_cell_accesses(&mut self, cells: &BTreeMap<Id, ModuleCellId>) {
        let effects = self.effects();
        for (accesses, target) in [
            (&effects.reads, &mut self.heap_contents.reads),
            (&effects.writes, &mut self.heap_contents.writes),
        ] {
            target.extend(accesses.iter().filter_map(|access| {
                let EffectCell::Binding(binding) = access else {
                    return None;
                };
                cells.get(binding).copied().map(EffectCell::ModuleCell)
            }));
        }
        if let Some(calls) = &mut self.synchronous_calls {
            calls.heap_contents.reads.extend(
                calls
                    .reads
                    .iter()
                    .map(|(binding, _)| binding)
                    .chain(&calls.uninitialized_reads)
                    .filter_map(|binding| cells.get(binding).copied().map(EffectCell::ModuleCell)),
            );
            calls.heap_contents.writes.extend(
                calls
                    .rebinds
                    .iter()
                    .map(|(binding, _)| binding)
                    .filter_map(|binding| cells.get(binding).copied().map(EffectCell::ModuleCell)),
            );
        }
    }
}

/// Identity in a chunk's declarative-cell arena. Slots are never reused; saved
/// effects preserve the identity without retaining its runtime value graph.
/// Immutable function-name views are not new activation-cell effects.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct LexicalCellId(pub(crate) usize);

impl std::fmt::Display for LexicalCellId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// One outer-observable storage location a statement can read or
/// write. `EffectCell::Binding(name)` covers identifier reads
/// (`Foo` or `Foo.bar` triggers a read of `Foo`) and rebind writes;
/// `EffectCell::GlobalProp(key)` covers static-key writes/reads on
/// `globalThis` (`globalThis.tag = ...` is `GlobalProp("tag")`).
/// Dynamic-keyed accesses (`globalThis[expr]`) are deliberately not
/// representable here — statements containing them are marked
/// non-summarizable.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub enum EffectCell {
    Binding(Id),
    /// The actual captured cell, with its canonical declaration offset.
    /// Identity does not prove initialization, contents, or private ownership.
    LexicalCell {
        origin: u32,
        instance: LexicalCellId,
    },
    /// One declarative module binding cell, scoped to its source chunk.
    /// Persisted analysis uses a checked chunk name instead of this interned ID.
    #[serde(skip)]
    ModuleCell(ModuleCellId),
    GlobalProp(String),
    /// Shared synchronous module-initialization progress across all logical
    /// modules in one exact per-chunk relocation sweep.
    ExecutionProgress,
    /// Heap contents of one array allocation, keyed by its canonical object
    /// origin from chunk value flow.
    ArrayContents(u32),
    /// Mutable contents of one proved fresh native Set construction.
    SetContents(u32),
    /// Shared agent-local ECMAScript GlobalSymbolRegistry used by
    /// `Symbol.for`. Its entries are not enumerable or removable from source,
    /// but repeated lookups observe the same key-unique symbol.
    GlobalSymbolRegistry,
    /// Own keys and descriptor attributes, shared by every generation of
    /// this source allocation. Distinct from stored data values.
    ObjectShape(u32),
    /// A captured native descriptor carrier, including its property records.
    DescriptorMapContents(u32),
    /// One static own property, conservatively shared by every runtime
    /// generation of the source allocation. This supplies no strong update.
    OwnProperty {
        origin: u32,
        property: String,
    },
    /// One private element, keyed independently by receiver allocation source,
    /// class brand source, and private name. Source origins still aggregate
    /// runtime generations and supply no strong-update or privacy proof.
    PrivateSlot {
        origin: u32,
        class: u32,
        name: String,
    },
}

impl EffectCell {
    pub(crate) fn is_heap_content(&self) -> bool {
        match self {
            Self::Binding(_)
            | Self::ModuleCell(_)
            | Self::GlobalProp(_)
            | Self::ExecutionProgress => false,
            Self::ArrayContents(_)
            | Self::LexicalCell { .. }
            | Self::SetContents(_)
            | Self::GlobalSymbolRegistry
            | Self::ObjectShape(_)
            | Self::DescriptorMapContents(_)
            | Self::OwnProperty { .. }
            | Self::PrivateSlot { .. } => true,
        }
    }
}

/// The (writes, reads) cell view of one statement, derived by
/// [`StatementFacts::effects`]. Its completeness is
/// [`StatementFacts::havoc`] (`cell_writes_summarizable`,
/// `dataflow_summarizable`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StatementEffectSummary {
    pub writes: BTreeSet<EffectCell>,
    pub reads: BTreeSet<EffectCell>,
}

/// The elements a plain-array binding holds where a statement starts, as
/// synchronous execution settled them. Every element is untainted at that
/// point (design § Heap taint).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) enum SettledElements {
    /// Every element is a primitive.
    Primitive,
    /// Every element is an untainted plain-data object literal with static
    /// keys; `shared_keys` are the own keys every object element
    /// has and holds a primitive under. No value the statement otherwise reads
    /// reaches those objects.
    Records { shared_keys: BTreeSet<String> },
}

/// Heap-content cells one body touches: allocations it performs, cells it
/// reads and writes, and whether an operation may touch contents the cells do
/// not name (a heap-frontier barrier in the graph builder).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct HeapContentCells {
    /// Fresh allocations seed later observations without overwriting a prior
    /// content timeline: source-span identities can denote many runtime arrays.
    pub allocations: BTreeSet<EffectCell>,
    pub reads: BTreeSet<EffectCell>,
    pub writes: BTreeSet<EffectCell>,
    pub opaque: bool,
    /// An observation of pre-existing external storage, with no unique source
    /// allocation birth. Its heap ordering cannot be discharged by completion.
    pub observes_external_heap: bool,
}

impl HeapContentCells {
    pub(crate) fn extend(&mut self, other: &Self) {
        self.allocations.extend(other.allocations.iter().cloned());
        self.reads.extend(other.reads.iter().cloned());
        self.writes.extend(other.writes.iter().cloned());
        self.opaque |= other.opaque;
        self.observes_external_heap |= other.observes_external_heap;
    }
}

/// Source-known heap may-accesses whose invocation path was not assumed.
/// Allocations are excluded: a fresh birth is not a content write.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct HeapContentAccesses {
    pub reads: BTreeSet<EffectCell>,
    pub writes: BTreeSet<EffectCell>,
}

impl HeapContentAccesses {
    pub(crate) fn extend(&mut self, other: &Self) {
        self.reads.extend(other.reads.iter().cloned());
        self.writes.extend(other.writes.iter().cloned());
    }

    pub(crate) fn extend_cells(&mut self, cells: &HeapContentCells) {
        self.reads.extend(cells.reads.iter().cloned());
        self.writes.extend(cells.writes.iter().cloned());
    }
}

#[derive(Debug, Clone)]
pub struct ChunkFactAnalysis {
    pub facts: Vec<StatementFacts>,
    pub top_level_await: Option<SourceUnitOrdinal>,
    /// Author-declared `purity: pure` spec hints the analyzer
    /// determines would be inferred automatically by recursive
    /// purity — e.g. the callee's body classifies `Pure` even
    /// without the override, or the binding admits as `PlainData`
    /// such that the callsite hint is a no-op. Surfaced so the spec
    /// author can prune the hint and shrink the trust surface.
    pub redundant_purity_hints: Vec<RedundantPurityHint>,
    /// Author-declared `pure_members: [<prop>, …]` entries the
    /// analyzer would classify pure without the hint — currently
    /// limited to intrinsic members whose builtin row admits any
    /// operands (e.g. `pure_members: [isArray]` on a binding named
    /// `Array`). Surfaced for the same trust-surface-shrinking
    /// reason as `redundant_purity_hints`.
    pub redundant_pure_member_hints: Vec<RedundantPureMemberHint>,
}

fn apply_initializer_binding_effects(
    fact: &mut StatementFacts,
    source_only_bindings: &BTreeSet<String>,
) {
    use crate::reports::schema::InitializerBindingEffectsUse;

    let Some(binding) = fact
        .declared
        .iter()
        .find(|binding| source_only_bindings.contains(binding.0.as_ref()))
    else {
        return;
    };
    // Authoring admission requires one simple initialized declarator in
    // this canonical source unit, including split comma-list declarations.
    if fact.kind != StatementKind::VarDecl
        || fact.declared.len() != 1
        || (fact.var_declared.contains(binding) && !fact.var_initializers.contains(binding))
    {
        return;
    }
    let Some(effects) = fact.synchronous_calls.as_mut() else {
        return;
    };
    let observed_reads: BTreeSet<String> = effects
        .reads
        .iter()
        .map(|(id, _)| id)
        .filter(|id| !fact.reads.eager.contains(*id) && effects.fallback_read(id).is_none())
        .chain(effects.uninitialized_reads.iter().filter(|id| {
            !fact.reads.eager.contains(*id) && effects.fallback_uninitialized_read(id).is_none()
        }))
        .map(|id| id.0.to_string())
        .collect();
    let observed_rebinds: BTreeSet<String> = effects
        .rebinds
        .iter()
        .filter(|(id, _)| !fact.rebinds.eager.contains(id) && effects.fallback_rebind(id).is_none())
        .map(|(id, _)| id.0.to_string())
        .collect();
    if !observed_reads.is_empty() || !observed_rebinds.is_empty() {
        effects.initializer_binding_effects =
            Some(Box::new(InitializerBindingEffectsUse::Contradicted {
                binding: binding.0.to_string(),
                reads: observed_reads,
                rebinds: observed_rebinds,
            }));
        return;
    }
    // Every out-of-footprint effect left here has fallback provenance.
    // Keep execution state, values, heap effects and completeness unchanged.
    let reads_before = effects.reads.len();
    let rebinds_before = effects.rebinds.len();
    let uninitialized_before = effects.uninitialized_reads.len();
    effects
        .reads
        .retain(|(id, _)| fact.reads.eager.contains(id));
    effects
        .rebinds
        .retain(|(id, _)| fact.rebinds.eager.contains(id));
    effects
        .uninitialized_reads
        .retain(|id| fact.reads.eager.contains(id));
    effects
        .fallback_only
        .reads
        .retain(|id, _| fact.reads.eager.contains(id));
    effects
        .fallback_only
        .rebinds
        .retain(|id, _| fact.rebinds.eager.contains(id));
    effects
        .fallback_only
        .uninitialized_reads
        .retain(|id, _| fact.reads.eager.contains(id));
    effects.initializer_binding_effects = Some(Box::new(InitializerBindingEffectsUse::Applied {
        binding: binding.0.to_string(),
        narrowed_reads: reads_before - effects.reads.len(),
        narrowed_rebinds: rebinds_before - effects.rebinds.len(),
        narrowed_uninitialized_reads: uninitialized_before - effects.uninitialized_reads.len(),
    }));
}

impl ChunkFactAnalysis {
    /// Apply author lexical-effect boundaries after raw source execution and
    /// receipt sealing. Source-owned coverage is never changed by this overlay.
    pub fn apply_initializer_binding_effects(&mut self, source_only_bindings: &BTreeSet<String>) {
        for fact in &mut self.facts {
            apply_initializer_binding_effects(fact, source_only_bindings);
        }
    }

    pub fn validate_initializer_binding_effects(
        &self,
        source_only_bindings: &BTreeSet<String>,
    ) -> anyhow::Result<()> {
        use crate::reports::schema::InitializerBindingEffectsUse;

        let mut consumed = BTreeSet::new();
        for fact in &self.facts {
            let Some(effect_use) = fact
                .synchronous_calls
                .as_ref()
                .and_then(|calls| calls.initializer_binding_effects.as_deref())
            else {
                continue;
            };
            match effect_use {
                InitializerBindingEffectsUse::Applied { binding, .. } => {
                    anyhow::ensure!(
                        consumed.insert(binding.clone()),
                        "initializer_binding_effects for `{binding}` was applied to multiple source units"
                    );
                }
                InitializerBindingEffectsUse::Contradicted {
                    binding,
                    reads,
                    rebinds,
                } => anyhow::bail!(
                    "initializer_binding_effects: source_only for `{binding}` contradicts observed lexical effects at source unit {}: reads {reads:?}, rebinds {rebinds:?}",
                    fact.ordinal.0
                ),
            }
        }
        anyhow::ensure!(
            &consumed == source_only_bindings,
            "initializer_binding_effects consumption mismatch: configured {source_only_bindings:?}, applied {consumed:?}"
        );
        Ok(())
    }
}

#[derive(
    Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize, strum::IntoStaticStr, strum::Display,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum StatementKind {
    /// `var X = ...`, `let X = ...`, `const X = ...`. RHS reads at-init.
    VarDecl,
    /// `function X() { ... }`. Hoisted; no at-init reads from body.
    FnDecl,
    /// `class X { ... }`. Extends, decorators, computed keys, and
    /// static blocks read at-init.
    ClassDecl,
    /// `export { ... }`, `export X`, etc. Lazy reads (re-exports).
    Export,
    /// `import { ... } from ...`. Linked, no at-init body code.
    Import,
    /// Bare expression / control-flow / etc. that doesn't declare a
    /// top-level binding.
    SideEffect,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ExportLinkage {
    Local,
    Dependency,
}

/// Policy-independent per-statement facts: everything the analyzer
/// can compute about a top-level statement from the module text alone,
/// without consulting `AnalysisInputs`. Produced by
/// [`analyze_chunk_structural`] and consumed by
/// [`analyze_chunk_with_policy`] to assemble the full
/// [`StatementFacts`]. See the doc comment on
/// [`analyze_chunk_structural`] for why this layer exists.
#[derive(Debug, Clone)]
pub struct StructuralStatementFacts {
    pub ordinal: SourceUnitOrdinal,
    pub(crate) export_linkage: Option<ExportLinkage>,
    pub source_location: Option<SourceLocation>,
    pub kind: StatementKind,
    pub declared: BTreeSet<Id>,
    pub local_bindings: BTreeSet<Id>,
    /// Module-scoped bindings declared with `var`, including inside control flow.
    pub var_declared: BTreeSet<Id>,
    /// Actual initializer writes; a bare `var x;` does not reset x.
    pub var_initializers: BTreeSet<Id>,
    /// All `var` declaration and initializer occurrences in this statement,
    /// including nested lazy syntax. Retained to prove unique stable bindings
    /// without another AST census.
    pub(crate) var_declaration_counts: BTreeMap<Id, usize>,
    pub(crate) var_initializer_counts: BTreeMap<Id, usize>,
    /// Direct top-level `var name = ...` candidates. Initializer type is
    /// checked later; this records only source shape and hygienic identity.
    pub(crate) top_level_var_initializer_candidates: BTreeSet<Id>,
    pub reads: PositionBucketed<BTreeSet<Id>>,
    pub rebinds: PositionBucketed<BTreeSet<Id>>,
    pub(crate) member_write_roots: BTreeSet<Id>,
    pub(crate) argument_value_escapes: BTreeMap<Id, BTreeSet<usize>>,
    pub calls: PositionBucketed<BTreeSet<Id>>,
    pub(crate) constructor: Option<ConstructorFacts>,
    pub at_init_unresolved_sources: BTreeSet<Id>,
    pub at_init_unresolved_inline_fn: bool,
    pub first_order_unresolved_sources: BTreeSet<Id>,
    pub first_order_unresolved_inline_fn: bool,
    pub declares_direct_function: bool,
    pub global_writes: BTreeSet<String>,
    pub global_reads: BTreeSet<String>,
    pub havoc: BTreeSet<DataflowOpacityCause>,
}

/// One shape that defeats the dataflow-aware S-chain's cell tracking for
/// a statement ([`StatementFacts::havoc`]). The first three
/// are policy-independent source shapes; `OpaqueAtInitCall` consults the
/// purity classifier, so a declared-pure hint or exact completed operation
/// coverage can discharge it without erasing the source provenance.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DataflowOpacityCause {
    /// A member write through a tracked binding or an alias shape: the
    /// written heap cell cannot be attributed.
    MemberWriteOrAlias,
    /// `with`, direct or indirect `eval`, `Function(...)`, a dynamic-key
    /// global access, or `defineProperty`/`Proxy` on the global object:
    /// the statement may write arbitrary cells
    /// ([`StatementFacts::cell_writes_summarizable`]).
    DynamicCellWrites,
    /// The statement reads a binding a bare global-object alias flowed
    /// into (`facts/global_object.rs`), so its summary misses global
    /// property cells.
    GlobalObjectAliasEscape,
    /// An at-init call or `new` the purity classifier does not prove
    /// `Pure`: I/O is not a cell and callee bodies may touch globals.
    OpaqueAtInitCall,
}

impl StatementFacts {
    /// Whether the dataflow-aware S-chain may use this statement's exact
    /// cell summary instead of the strict every-prior-impure fallback.
    pub fn dataflow_summarizable(&self) -> bool {
        self.havoc.is_empty()
            || (self.havoc.len() == 1
                && self.havoc.contains(&DataflowOpacityCause::OpaqueAtInitCall)
                && self.has_complete_operation_effects())
    }

    /// Remaining dataflow obligations after applying exact operation authority.
    /// The source-classifier provenance in `havoc` remains unchanged.
    pub fn dataflow_opacity(&self) -> BTreeSet<DataflowOpacityCause> {
        let mut opacity = self.havoc.clone();
        if self.has_complete_operation_effects() {
            opacity.remove(&DataflowOpacityCause::OpaqueAtInitCall);
        }
        opacity
    }

    /// Complete lexical effects and initialization domains for this exact
    /// owner. Neither timing completion nor the source-only initializer proof
    /// by itself supplies the operation authority needed to refine havoc.
    fn has_complete_operation_effects(&self) -> bool {
        self.synchronous_calls.as_ref().is_some_and(|calls| {
            calls.binding_effect_authority()
                == super::sync_effects::BindingEffectAuthority::OperationReceipt
                && calls
                    .completed_initialization
                    .as_ref()
                    .is_some_and(|proof| proof.covers(self.ordinal))
        })
    }

    /// Whether the cells the statement WRITES are all in its summary: no
    /// `with`, `eval`, `Function(...)`, dynamic-key `globalThis[expr]`, or
    /// `defineProperty`/`Proxy` on the global. Read by the vendor strip's
    /// swap-privacy gate, whose call side effects are covered by its own
    /// island-reachability analysis.
    pub fn cell_writes_summarizable(&self) -> bool {
        !self
            .havoc
            .contains(&DataflowOpacityCause::DynamicCellWrites)
    }
}

impl StructuralStatementFacts {
    /// Source-only contradiction to a selected direct parameter-borrow input.
    pub fn contradicts_argument_borrow(&self, function: &Id, parameter: &Id, index: usize) -> bool {
        self.member_write_roots.contains(parameter)
            || self
                .argument_value_escapes
                .get(function)
                .is_some_and(|indices| indices.contains(&index))
    }
}

#[derive(Debug, Clone, Default)]
pub(crate) struct ConstructorFacts {
    pub reads: BTreeSet<Id>,
    pub rebinds: BTreeSet<Id>,
    pub calls: BTreeSet<Id>,
    pub unresolved_sources: BTreeSet<Id>,
    pub unresolved_inline_fn: bool,
}

/// The policy-independent half of [`analyze_chunk`]'s output: the
/// top-level item view, the shadowed-globals set, the top-level-await
/// scan, and the per-statement static facts that depend only on the
/// module text (not on `AnalysisInputs`).
///
/// Owns its top-level item view by lifetime-tying to the source
/// module, so the policy-dependent pass can re-traverse the same views
/// without re-running the multi-declarator split.
pub struct StructuralChunkAnalysis<'a> {
    pub body: Vec<TopLevelItemView<'a>>,
    pub(crate) imported_bindings: BTreeSet<Id>,
    pub(crate) value_flow: super::ChunkValueFlow,
    pub(crate) dynamic_scope: super::DynamicScopeFact,
    pub(crate) set_iterable_shapes: super::SetIterableShapes,
    pub(crate) stable_array_spread_shapes: super::StableArraySpreadShapes,
    pub(crate) local_class_shapes: super::LocalClassConstructionShapes,
    pub(crate) plain_data_bindings: Vec<String>,
    /// Top-level `var` binding IDs that pass the structural stability gates.
    /// Value inference admits only candidates with a proven Number initializer;
    /// at arbitrary module positions the resulting value class is Number or
    /// pre-initialization `undefined`, never exact Number.
    pub(crate) stable_var_number_candidates: BTreeSet<Id>,
    pub(crate) plain_data_dependencies: BTreeMap<String, crate::analysis_inputs::ProofDependencies>,
    pub(crate) plain_data_inference_uses:
        BTreeMap<String, BTreeSet<crate::analysis_provenance::InferenceUse>>,
    pub(crate) source_semantic_facts:
        Option<std::sync::Arc<super::semantic_inference::SemanticFacts>>,
    pub shadowed: BTreeSet<&'static str>,
    pub per_statement: Vec<StructuralStatementFacts>,
    pub top_level_await: Option<SourceUnitOrdinal>,
}

/// Source-only structural census. Selector resolution can consume this before
/// value-flow roots supplied by validated author inputs are known.
pub struct StructuralChunkSourceFacts<'a> {
    pub body: Vec<TopLevelItemView<'a>>,
    pub(crate) stable_array_spread_shapes: super::StableArraySpreadShapes,
    pub(crate) imported_bindings: BTreeSet<Id>,
    pub shadowed: BTreeSet<&'static str>,
    pub per_statement: Vec<StructuralStatementFacts>,
    pub top_level_await: Option<SourceUnitOrdinal>,
}

#[cfg(test)]
mod initializer_overlay_tests {
    use super::*;
    use crate::facts::sync_effects::{BindingEffectAuthority, BindingEffectCoverage};
    use raw_js_test_support::parse;

    #[test]
    fn initializer_overlay_preserves_raw_receipts_and_other_effect_domains() {
        // The author marker hides the source-owned receipt in graph reports;
        // inspect that independent authority directly after real source analysis.
        for (source, has_receipt) in [
            (
                "const seed = 41; function factory() { return seed; } var result = (seed, factory(), factory());",
                true,
            ),
            (
                "const seed = 41; function factory() { globalThis.host?.(() => { const value = later; later = 3; return value; }); return seed; } var result = (seed, factory(), factory()); var later = 0;",
                false,
            ),
        ] {
            let module = parse(source);
            let mut analysis = analyze_chunk(&module, &AnalysisInputs::default(), None, |_| None);
            let result_index = analysis
                .facts
                .iter()
                .position(|fact| fact.declared.iter().any(|binding| binding.0 == "result"))
                .unwrap();
            let before = analysis.facts[result_index]
                .synchronous_calls
                .as_ref()
                .unwrap()
                .clone();
            let BindingEffectCoverage::OrderedRoot { receipt, .. } =
                &before.binding_effect_coverage
            else {
                panic!("the fixture must exercise ordered root receipt sealing: {before:#?}");
            };
            assert_eq!(receipt.is_some(), has_receipt, "{before:#?}");
            if !has_receipt {
                assert!(
                    !before.fallback_only.reads.is_empty(),
                    "raw fallback must block a source receipt"
                );
            }
            let coverage = serde_json::to_value(&before.binding_effect_coverage).unwrap();
            let selected = BTreeSet::from(["result".to_string()]);
            analysis.apply_initializer_binding_effects(&selected);
            analysis
                .validate_initializer_binding_effects(&selected)
                .unwrap();
            let fact = &analysis.facts[result_index];
            let after = fact.synchronous_calls.as_ref().unwrap();
            assert_eq!(
                serde_json::to_value(&after.binding_effect_coverage).unwrap(),
                coverage,
                "an author filter cannot mint, upgrade or reseal a source receipt"
            );
            assert_eq!(
                after.binding_effect_authority(),
                BindingEffectAuthority::AuthorSourceOnly
            );
            assert_eq!(after.complete, before.complete);
            assert_eq!(after.heap_contents, before.heap_contents);
            assert_eq!(
                after.non_assumed_heap_accesses,
                before.non_assumed_heap_accesses
            );
            assert_eq!(
                serde_json::to_value(&after.completed_initialization).unwrap(),
                serde_json::to_value(&before.completed_initialization).unwrap()
            );
            assert_eq!(after.precision_losses, before.precision_losses);
            let effects = fact.initialization_effects();
            assert!(
                effects.reads.keys().any(
                    |cell| matches!(cell, EffectCell::Binding(binding) if binding.0 == "seed")
                ),
                "the syntax-derived footprint remains in initialization effects"
            );
            if !has_receipt {
                assert!(before.reads.iter().any(|(binding, _)| binding.0 == "later"));
                assert!(
                    before
                        .rebinds
                        .iter()
                        .any(|(binding, _)| binding.0 == "later")
                );
                for cells in [&effects.reads, &effects.writes] {
                    assert!(!cells.keys().any(|cell| matches!(cell, EffectCell::Binding(binding) if binding.0 == "later")),
                        "promoted fallback effects must be narrowed before effect translation validation");
                }
            }
        }
    }

    #[test]
    fn initializer_overlay_retains_duplicate_and_consumption_guards() {
        for (source, binding, diagnostic) in [
            (
                "var result = 1; var result = 2;",
                "result",
                "multiple source units",
            ),
            ("var result = 1;", "missing", "consumption mismatch"),
        ] {
            let module = parse(source);
            let mut facts = analyze_chunk(&module, &AnalysisInputs::default(), None, |_| None);
            let configured = BTreeSet::from([binding.to_string()]);
            facts.apply_initializer_binding_effects(&configured);
            let error = facts
                .validate_initializer_binding_effects(&configured)
                .unwrap_err();
            assert!(error.to_string().contains(diagnostic), "{error:#}");
        }
    }
}
