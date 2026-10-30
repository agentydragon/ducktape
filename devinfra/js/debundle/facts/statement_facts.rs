use super::*;
use source_identity::SourceUnitOrdinal;

/// One value per syntactic position bucket. Collapses the repeated
/// eager / lazy / first-order-lazy triples (reads, rebinds, calls)
/// into a single shape so the "first-order ⊆ lazy" subset invariant
/// lives in one place ([`Self::record`]) instead of at every
/// construction site.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
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
    /// Exact stable-array spread producers that must remain in this module.
    pub(crate) stable_array_spread_producers: BTreeSet<SourceUnitOrdinal>,
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
    /// at-init (`globalThis.tag = ...` records `"tag"`). Dynamic-key
    /// accesses bail `cell_writes_summarizable` instead.
    pub global_writes: BTreeSet<String>,
    /// Static-key `globalThis.<prop>` cells the statement reads
    /// at-init.
    pub global_reads: BTreeSet<String>,
    /// `false` when the statement contains a shape that defeats any
    /// static reasoning about which cells it WRITES (`with`, direct
    /// or indirect `eval`, `Function(...)`, dynamic-key
    /// `globalThis[expr]`, `defineProperty`/`Proxy` on the global).
    /// Consumed by the vendor strip's swap-privacy gate, whose call
    /// side effects are covered by its own island-reachability
    /// analysis.
    pub cell_writes_summarizable: bool,
    /// Every reason the dataflow-aware S-chain's stronger "which cells
    /// does this statement TOUCH" question cannot be answered; non-empty
    /// whenever `cell_writes_summarizable` is `false`. Downstream passes
    /// treat a statement with any cause as touching every cell
    /// ([`StatementFacts::dataflow_summarizable`]) and the graph retains
    /// the causes on the edges that fallback emits. See `README.md` →
    /// "Conditionally-correct optimizations" for the soundness
    /// precondition.
    pub dataflow_opacity: BTreeSet<DataflowOpacityCause>,
    /// Heap-content effects on uniquely identified array allocations. These
    /// are distinct from binding effects: mutating an array changes its
    /// contents, not the binding that names it.
    pub(crate) heap_contents: HeapContentEffects,
    /// Author-conditioned native premises consumed by certified local-class
    /// heap construction at this source unit.
    pub heap_native_integrity_uses: Vec<crate::reports::schema::NativeIntegrityUse>,
    pub purity: Purity,
    pub kind: StatementKind,
}

#[derive(Debug, Clone)]
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

#[derive(Debug, Clone)]
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

#[derive(Debug, Clone)]
pub struct CallbackFactoryCallArgumentWitness {
    pub statement_ordinal: SourceUnitOrdinal,
    pub call_span: swc_common::Span,
    pub argument_index: usize,
    pub namespace_binding: String,
}

#[derive(Debug, Clone)]
pub struct CallbackFactoryWriteWitness {
    pub statement_ordinal: SourceUnitOrdinal,
    pub receiver_binding: String,
    pub property: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct SynchronousCalls {
    pub local_record_factory_uses: Vec<crate::reports::schema::LocalRecordFactoryUse>,
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
    /// Bindings in `reads`, `rebinds` and `uninitialized_reads` that only the
    /// statement's fallback summary contributed; `None` when no fallback ran.
    pub fallback_only: Option<FallbackOnlyEffects>,
    pub complete: bool,
    pub precision_losses: Vec<crate::reports::schema::ExecutionPrecisionLoss>,
    pub native_integrity_checks: Vec<crate::reports::schema::NativeIntegrityCheck>,
    pub native_prototype_property_checks: Vec<crate::reports::schema::NativePrototypePropertyCheck>,
    pub native_prefix_observations: Vec<crate::reports::schema::NativePrefixObservation>,
    pub native_prototype_mutations:
        std::sync::Arc<Vec<crate::reports::schema::NativePrototypeMutation>>,
    pub native_integrity_uses: Vec<crate::reports::schema::NativeIntegrityUse>,
    pub native_operation_input_uses: Vec<crate::reports::schema::NativeOperationInputUse>,
    pub inference_uses: Vec<crate::reports::schema::InferenceUse>,
    pub native_integrity_witnesses:
        std::sync::Arc<Vec<crate::reports::schema::NativeMutationWitness>>,
    pub callback_factory_uses: Vec<CallbackFactoryUseFact>,
    pub returned_record_uses: Vec<ReturnedRecordUseFact>,
    pub author_host_contract_uses: Vec<crate::reports::schema::ReentryFreeOperationUse>,
}

/// Binding effects an incomplete statement's fallback summary assumed rather
/// than derived: callbacks the interpreter stopped expanding
/// (`bounded_invocation_fallback`) or returned to opaque code are assumed to
/// run, so their whole synchronous read/rebind closure is attributed to the
/// statement. A binding precise execution also reached is not listed: an
/// effect both observed and assumed is observed.
#[derive(Debug, Clone)]
pub(crate) struct FallbackOnlyEffects {
    /// The precision loss that opened the statement's fallback summary.
    pub reason: crate::reports::schema::ExecutionPrecisionLossReason,
    pub reads: BTreeSet<Id>,
    pub rebinds: BTreeSet<Id>,
    pub uninitialized_reads: BTreeSet<Id>,
}

impl SynchronousCalls {
    /// The fallback reason when only the fallback summary contributed the
    /// read of `binding`; `None` when precise execution observed it.
    pub(crate) fn fallback_read(
        &self,
        binding: &Id,
    ) -> Option<&crate::reports::schema::ExecutionPrecisionLossReason> {
        self.fallback_only
            .as_ref()
            .filter(|fallback| fallback.reads.contains(binding))
            .map(|fallback| &fallback.reason)
    }

    /// As [`Self::fallback_read`], for a rebind of `binding`.
    pub(crate) fn fallback_rebind(
        &self,
        binding: &Id,
    ) -> Option<&crate::reports::schema::ExecutionPrecisionLossReason> {
        self.fallback_only
            .as_ref()
            .filter(|fallback| fallback.rebinds.contains(binding))
            .map(|fallback| &fallback.reason)
    }

    /// As [`Self::fallback_read`], for an observation of `binding` before
    /// its initializer ran.
    pub(crate) fn fallback_uninitialized_read(
        &self,
        binding: &Id,
    ) -> Option<&crate::reports::schema::ExecutionPrecisionLossReason> {
        self.fallback_only
            .as_ref()
            .filter(|fallback| fallback.uninitialized_reads.contains(binding))
            .map(|fallback| &fallback.reason)
    }
}

/// Source- and execution-checked evidence that one exact factory call only
/// allocates the validated local constructor graph. The call-site purity pass
/// consumes this to discharge only the generic impure-callee reason at
/// `call_span`; factory and class purity summaries remain unchanged. An
/// author assertion may supply otherwise-unproved prototype premises, but
/// native/source evidence can establish them without an assertion.
#[derive(Debug, Clone)]
pub(crate) struct LocalRecordFactoryPurityCertificate {
    pub call_span: swc_common::Span,
    pub checked_owners: BTreeSet<SourceUnitOrdinal>,
    pub dependencies: crate::analysis_inputs::ProofDependencies,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct CallbackFactoryUseFact {
    pub imported_local: String,
    pub evidence: Vec<crate::analysis_inputs::CallbackFactoryEvidence>,
    pub kind: crate::reports::schema::CallbackFactoryUseKind,
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<crate::reports::schema::SourceLocation>,
    pub span: swc_common::Span,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ReturnedRecordUseFact {
    pub imported_local: String,
    pub evidence: crate::analysis_inputs::ReturnedRecordSummary,
    pub kind: crate::reports::schema::ReturnedRecordUseKind,
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<crate::reports::schema::SourceLocation>,
    pub span: swc_common::Span,
}

impl StatementFacts {
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
        writes.extend(self.heap_contents.writes.eager.iter().cloned());
        reads.extend(self.heap_contents.reads.eager.iter().cloned());
        StatementEffectSummary { writes, reads }
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
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub enum EffectCell {
    Binding(Id),
    GlobalProp(String),
    /// Heap contents of one array allocation, keyed by its canonical object
    /// origin from chunk value flow.
    ArrayContents(u32),
    /// Mutable contents of one author-certified fresh native Set.
    SetContents(u32),
}

/// The (writes, reads) cell view of one statement, derived by
/// [`StatementFacts::effects`]. The summarizability bits live on
/// [`StatementFacts`] directly (`cell_writes_summarizable`,
/// `dataflow_summarizable`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StatementEffectSummary {
    pub writes: BTreeSet<EffectCell>,
    pub reads: BTreeSet<EffectCell>,
}

/// Heap-content effects attributed to one source unit. First-order lazy
/// effects are inherited by synchronous at-init calls; nested closures remain
/// deferred. Opaque effects become heap-frontier barriers in the graph builder,
/// rather than being copied onto every canonical array identity.
#[derive(Debug, Clone, Default)]
pub(crate) struct HeapContentEffects {
    /// Canonical local call targets whose heap summaries still need promotion.
    /// Certified construction sites are excluded before sites are unioned.
    pub calls: PositionBucketed<BTreeSet<Id>>,
    /// Fresh allocations seed later observations without overwriting a prior
    /// content timeline: source-span identities can denote many runtime arrays.
    pub allocations: PositionBucketed<BTreeSet<EffectCell>>,
    /// One eager, whole top-level literal site whose allocation cannot have
    /// run before this source unit. Used only to discharge an older heap
    /// frontier for precise observations of that newly born cell.
    pub one_shot_allocations: BTreeSet<EffectCell>,
    pub reads: PositionBucketed<BTreeSet<EffectCell>>,
    pub writes: PositionBucketed<BTreeSet<EffectCell>>,
    pub opaque: PositionBucketed<bool>,
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
    /// limited to `(WHITELIST_RECEIVERS, PURE_STATIC_CALLS-prop)`
    /// pairs (e.g. `pure_members: [isArray]` on a binding named
    /// `Array`). Surfaced for the same trust-surface-shrinking
    /// reason as `redundant_purity_hints`.
    pub redundant_pure_member_hints: Vec<RedundantPureMemberHint>,
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
    pub cell_writes_summarizable: bool,
    pub dataflow_opacity: BTreeSet<DataflowOpacityCause>,
}

/// One shape that defeats the dataflow-aware S-chain's cell tracking for
/// a statement ([`StatementFacts::dataflow_opacity`]). The first three
/// are policy-independent source shapes; `OpaqueAtInitCall` consults the
/// purity classifier, so a declared-pure hint can lift it.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DataflowOpacityCause {
    /// A member write through a tracked binding or an alias shape: the
    /// written heap cell cannot be attributed.
    MemberWriteOrAlias,
    /// `with`, direct or indirect `eval`, `Function(...)`, a dynamic-key
    /// global access, or `defineProperty`/`Proxy` on the global object:
    /// the statement may write arbitrary cells (also clears
    /// `cell_writes_summarizable`).
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
        self.dataflow_opacity.is_empty()
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
        BTreeMap<String, BTreeSet<crate::reports::schema::InferenceUse>>,
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
