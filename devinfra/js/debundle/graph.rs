use std::collections::{BTreeMap, BTreeSet, HashMap};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};
use swc_ecma_ast::Id;

use crate::facts::EffectCell;
use crate::partition::Partition;
use crate::purity::Purity;
use crate::{ModuleId, SourceLocation, StatementFacts, StatementKind, StatementOrdinal};

/// Per-chunk owner-graph build options. Each field defaults to the
/// strictly-conservative behavior; opt-ins enable conditionally-correct
/// inferences that hold only when the input satisfies a checkable
/// precondition (see `devinfra/js/debundle/AGENTS.md` →
/// "Conditionally-correct optimizations"). The materializer reads
/// these from the per-chunk spec entry in
/// `TransformSpec::chunk_analysis_options`.
#[derive(Debug, Clone, Copy, Default)]
pub struct OwnerGraphOptions {
    /// Emit the side-effect ordering chain using per-statement
    /// (writes, reads) summaries instead of the adjacent-impure
    /// transitive reduction. See the S-chain block in
    /// `build_owner_graph_with` and `dataflow_audit.md`.
    pub dataflow_aware_s_chain: bool,
}

/// One reason an edge `(from, to)` exists, with the source
/// statement ordinal that produced it. This is the single source of
/// truth for edge semantics:
///
/// - `EagerUse` constrains ESM evaluation order under TDZ
///   semantics (`R ⊆ I`).
/// - `LazyUse` contributes to the imports graph `I`, but does not
///   constrain realizability inside an SCC because the read fires
///   after module evaluation.
/// - `EagerRebind` / `LazyRebind` describe rebinding writes. A
///   cross-destination write is rejected outright because ESM imports
///   are read-only in the importing module; same-destination writes
///   are represented only at owner level and don't become module
///   imports.
/// - `Sequenced` contributes to `S` and constrains
///   realizability because source-order side effects require a
///   topological order.
/// - `LocalEffect` is a trusted target-local mutation (for example
///   a TypeScript `__decorate` helper application) that must
///   co-locate with the target owner but should not impose global
///   side-effect ordering on unrelated owners.
#[derive(Debug, Clone)]
pub struct EdgeReason {
    pub(crate) kind: DepKind,
    pub(crate) statement_ordinal: StatementOrdinal,
    pub(crate) binding: Option<Id>,
}

impl EdgeReason {
    pub(crate) fn eager_use(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::EagerUse,
            statement_ordinal: so,
            binding: Some(b),
        }
    }
    pub(crate) fn lazy_use(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LazyUse,
            statement_ordinal: so,
            binding: Some(b),
        }
    }
    pub(crate) fn eager_rebind(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::EagerRebind,
            statement_ordinal: so,
            binding: Some(b),
        }
    }
    pub(crate) fn lazy_rebind(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LazyRebind,
            statement_ordinal: so,
            binding: Some(b),
        }
    }
    pub(crate) fn sequenced(so: StatementOrdinal) -> Self {
        Self {
            kind: DepKind::Sequenced,
            statement_ordinal: so,
            binding: None,
        }
    }
    pub(crate) fn local_effect(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LocalEffect,
            statement_ordinal: so,
            binding: Some(b),
        }
    }

    pub fn is_eager_use(&self) -> bool {
        self.kind == DepKind::EagerUse
    }
    pub fn is_rebind(&self) -> bool {
        matches!(self.kind, DepKind::EagerRebind | DepKind::LazyRebind)
    }
    pub fn is_sequenced(&self) -> bool {
        self.kind == DepKind::Sequenced
    }
    /// Every kind except `LazyUse` constrains realizability.
    /// Stated as exclusion so adding a new `DepKind` variant
    /// forces an explicit decision here.
    pub fn constrains_init_order(&self) -> bool {
        self.kind != DepKind::LazyUse
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DepKind {
    EagerUse,
    LazyUse,
    EagerRebind,
    LazyRebind,
    Sequenced,
    LocalEffect,
}

/// Stable-in-run identity of an owner graph vertex. V1 owner
/// vertices are post-comma-list `StatementFacts` rows, so the id
/// is the row's source-order ordinal.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
#[serde(transparent)]
pub struct OwnerId(pub usize);

/// Fine-grained graph before logical modules are formed. Nodes are
/// top-level owners/statements; edges are owner-level reads and
/// source-order side-effect constraints. The module dependency graph
/// is the quotient of this graph by a [`Partition`].
///
/// Storage is **flat-edges + CSR adjacency**, the canonical compiler-IR
/// shape: one [`OwnerEdge`] per reason, indexed by [`OwnerEdgeId`]
/// (= position in `edges`), with per-node `out_edges` / `in_edges`
/// adjacency lists for O(deg) traversal. The previous representation
/// kept two parallel views — a `petgraph::DiGraphMap` for random
/// access by `(from, to)` and a separate `Vec<OwnerEdge>` for
/// stable indices into edges; this collapses them.
#[derive(Debug, Clone, Default)]
pub struct OwnerGraph {
    pub nodes: Vec<OwnerNode>,
    pub edges: Vec<OwnerEdge>,
    /// CSR adjacency by source owner. `out_edges[owner.0]` is a list
    /// of `OwnerEdgeId` indices into `edges`.
    pub out_edges: Vec<Vec<OwnerEdgeId>>,
    /// CSR adjacency by target owner.
    pub in_edges: Vec<Vec<OwnerEdgeId>>,
}

#[derive(Debug, Clone)]
pub struct OwnerNode {
    pub id: OwnerId,
    pub statement_ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<Id>,
    pub kind: StatementKind,
    pub purity: Purity,
}

impl OwnerGraph {
    /// Iterate `&OwnerEdge` in `OwnerEdgeId` order. Each row is one
    /// reason — multiple reasons between the same `(from, to)` pair
    /// appear as separate entries.
    pub fn iter_edges(&self) -> impl Iterator<Item = &OwnerEdge> + '_ {
        self.edges.iter()
    }

    pub fn node(&self, id: OwnerId) -> Option<&OwnerNode> {
        self.nodes.get(id.0).filter(|node| node.id == id)
    }

    pub fn iter_nodes(&self) -> impl Iterator<Item = &OwnerNode> {
        self.nodes.iter()
    }

    /// Edges originating at `owner`.
    pub fn out_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.out_edges
            .get(owner.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Edges terminating at `owner`.
    pub fn in_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.in_edges.get(owner.0).map(Vec::as_slice).unwrap_or(&[])
    }
}

/// Per-edge metadata. One physical `(from, to)` ESM `import`
/// directive can be backed by multiple reasons (e.g. several
/// at-init reads of bindings owned by the same target module);
/// they're all kept here so cycle reports can show every
/// triggering statement.
#[derive(Debug, Clone, Default)]
pub struct EdgeMetadata {
    pub reasons: Vec<EdgeReason>,
}

impl EdgeMetadata {
    /// `true` if at least one reason is an at-init read. The
    /// realizability gate uses this to decide whether an
    /// `I ∪ S` SCC contains an `R` cross-module edge.
    pub fn has_eager_use(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_eager_use)
    }

    /// `true` if at least one reason is a side-effect ordering
    /// edge. `S` edges in an SCC make it unrealizable: the
    /// constraint is "predecessor must evaluate before
    /// successor", and a cycle has no topological emit order
    /// satisfying every such edge.
    pub fn has_sequenced(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_sequenced)
    }

    /// `true` if at least one reason is a rebinding write. These
    /// edges are rejected outright when they cross destination
    /// modules because imported ESM bindings are read-only.
    pub fn has_rebind(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_rebind)
    }

    /// `true` if this edge constrains realizability — at least one
    /// of its reasons is realizability-constraining (an at-init
    /// read `R`, a side-effect ordering `S` edge, or a rebinding
    /// write). Lazy read-only edges don't, because the reads they
    /// represent fire after every module in the cycle has finished
    /// evaluating.
    ///
    /// Delegates to `EdgeReason::constrains_init_order` to keep
    /// the per-edge and per-reason definitions in lockstep.
    pub fn constrains_init_order(&self) -> bool {
        self.reasons.iter().any(EdgeReason::constrains_init_order)
    }
}

/// Module dep graph built from per-statement facts and a binding →
/// module assignment.
///
/// Thin newtype around `petgraph::DiGraphMap<ModuleId,
/// EdgeMetadata>`: one edge per directed `(from, to)` pair, weight =
/// `EdgeMetadata`. Multiple reasons for the same physical edge (e.g.
/// several at-init reads of bindings owned by the same target
/// module) accumulate into the edge's reason list. Cycle detection
/// runs through petgraph's `tarjan_scc`.
///
/// `Deref` / `DerefMut` to the inner graph lets callers reach
/// `petgraph` methods (`all_edges`, `edge_weight`, `nodes`,
/// `contains_edge`, …) directly: `dep_graph.all_edges()` instead of
/// `dep_graph.graph.all_edges()`. The newtype is kept (rather than a
/// bare type alias) so the semantic name "the I∪S module-dep
/// quotient" stays distinct from arbitrary
/// `DiGraphMap<ModuleId, EdgeMetadata>` instances.
///
/// For `petgraph::algo::tarjan_scc` (a generic function whose
/// inference doesn't trigger `Deref` coercion), callers reach for
/// the inner graph with `&dep_graph.0` or `&*dep_graph`.
#[derive(Debug, Clone, Default)]
pub struct ModuleQuotient(pub DiGraphMap<ModuleId, EdgeMetadata>);

impl std::ops::Deref for ModuleQuotient {
    type Target = DiGraphMap<ModuleId, EdgeMetadata>;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl std::ops::DerefMut for ModuleQuotient {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}

impl ModuleQuotient {
    fn record_reason(&mut self, from: ModuleId, to: ModuleId, reason: EdgeReason) {
        if from == to {
            return;
        }
        if !self.contains_edge(from, to) {
            self.add_edge(from, to, EdgeMetadata::default());
        }
        self.edge_weight_mut(from, to).unwrap().reasons.push(reason);
    }

    /// `true` if the edge `(from, to)` exists and constrains
    /// realizable evaluation order (at-init read or side-effect
    /// ordering). Used by the realizability gate to decide
    /// whether an `I ∪ S` SCC is unrealizable.
    pub fn has_init_order_constraining_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.edge_weight(from, to)
            .is_some_and(EdgeMetadata::constrains_init_order)
    }
}

/// Build the fine owner graph from per-statement facts. Pure IR
/// construction: no module assignment, no quotient. Module-level
/// dependencies are derived later by [`build_module_quotient`]
/// given a [`Partition`] mapping owners to destination modules.
///
/// Uses default (strictly-conservative) [`OwnerGraphOptions`]. Call
/// [`build_owner_graph_with`] when the chunk spec opts into
/// conditionally-correct refinements.
pub fn build_owner_graph(facts: &[StatementFacts]) -> OwnerGraph {
    build_owner_graph_with(facts, OwnerGraphOptions::default())
}

/// Like [`build_owner_graph`] but takes per-chunk [`OwnerGraphOptions`].
pub fn build_owner_graph_with(facts: &[StatementFacts], options: OwnerGraphOptions) -> OwnerGraph {
    let mut binding_owner = HashMap::<Id, OwnerId>::new();
    let mut nodes = Vec::<OwnerNode>::with_capacity(facts.len());
    for stmt in facts {
        for binding in &stmt.declared {
            binding_owner.insert(binding.clone(), OwnerId(stmt.ordinal.0));
        }
        let id = OwnerId(stmt.ordinal.0);
        nodes.push(OwnerNode {
            id,
            statement_ordinal: stmt.ordinal,
            source_location: stmt.source_location.clone(),
            declared: stmt.declared.clone(),
            kind: stmt.kind,
            purity: stmt.purity.clone(),
        });
    }

    // Collect (from, to, reason) triples; the final `edges` Vec is
    // sorted at the end so `OwnerEdgeId` indices are stable.
    let mut raw_edges = Vec::<(OwnerId, OwnerId, EdgeReason)>::new();
    // Look-aside table for "what statement owns this OwnerId" — shared
    // by the direct eager-read filter below and by
    // `promote_at_init_calls` (which builds its own local copy; the
    // duplicate cost is negligible).
    let stmt_by_owner: std::collections::HashMap<OwnerId, &StatementFacts> = facts
        .iter()
        .map(|stmt| (OwnerId(stmt.ordinal.0), stmt))
        .collect();
    // A top-level eager read of a binding declared by a `function`
    // declaration cannot observe a TDZ: ECMAScript Phase 1 of module
    // linking (`ModuleDeclarationInstantiation`) binds every
    // `FunctionDeclaration` to its hoisted closure before any module
    // body runs. So `const x = f()` where `f` is a chunk-declared
    // FnDecl is safe regardless of which module owns `f` — there is
    // no init-order constraint to record, and emitting an `EagerUse`
    // edge would manufacture a cross-module constraint no realizable
    // trace demands. Same rule as the FnDecl exclusion in
    // `promote_at_init_calls`. Other declared kinds (VarDecl,
    // ClassDecl) are TDZ-locked until their statement runs, so their
    // cross-module reads stay constrained.
    let target_is_hoisted = |id: &Id| -> bool {
        binding_owner
            .get(id)
            .and_then(|owner| stmt_by_owner.get(owner))
            .map(|stmt| stmt.kind == StatementKind::FnDecl)
            .unwrap_or(false)
    };
    let push_binding_edge = |raw_edges: &mut Vec<(OwnerId, OwnerId, EdgeReason)>,
                             from: OwnerId,
                             binding: &Id,
                             make_reason: fn(StatementOrdinal, Id) -> EdgeReason,
                             statement_ordinal: StatementOrdinal| {
        let Some(to) = binding_owner.get(binding) else {
            return; // not declared in this chunk (global, ImportSpecifier, never-declared)
        };
        if from == *to {
            return;
        }
        raw_edges.push((from, *to, make_reason(statement_ordinal, binding.clone())));
    };
    for stmt in facts {
        let from = OwnerId(stmt.ordinal.0);
        for binding in &stmt.eager_reads {
            if target_is_hoisted(binding) {
                continue;
            }
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::eager_use,
                stmt.ordinal,
            );
        }
        for binding in &stmt.lazy_reads {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::lazy_use,
                stmt.ordinal,
            );
        }
        for binding in &stmt.eager_rebinds {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::eager_rebind,
                stmt.ordinal,
            );
        }
        // Only first-order body rebinds emit a constraining
        // `LazyRebind` edge. A rebind inside a nested closure
        // (e.g. an arrow stashed on `globalThis` by the body)
        // doesn't fire when the function is invoked synchronously;
        // emitting an edge for it manufactures a bidirectional
        // G_atomic constraint (atomic_units.rs:82-85) that forces
        // co-location with the rebind target even though no
        // synchronous-trace rebind exists. See the e2e test
        // `at_init_promotion_nested_closure_test` for the
        // rationale; the same first-order narrowing is what
        // promote_at_init_calls uses.
        for binding in &stmt.first_order_lazy_rebinds {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::lazy_rebind,
                stmt.ordinal,
            );
        }
        for binding in &stmt.local_effects {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::local_effect,
                stmt.ordinal,
            );
        }
    }

    // At-init call promotion (DESIGN.md "At-init call promotion").
    //
    // A function body's lazy reads/rebinds fire at-init from the
    // perspective of any caller that invokes the function at-init.
    // Without promotion, the realizability primitive's relaxed
    // clause-3 predicate (constraining-edge subgraph has no
    // multi-module SCC) is unsound for the canonical
    // `console.log(readB())` shape: the lazy read inside `readB`'s
    // body fires when the top-level call evaluates, but only the
    // graph's lazy edge is recorded, so cross-module cycles that
    // close through such a lazy edge look acyclic to the constraining
    // subgraph. After promotion, the primitive's verdict is sound.
    //
    // Promoted edges are added at owner-graph level (partition-
    // independent): intra-module promoted edges are dropped by the
    // quotient automatically. Only direct `f(...)` callees that
    // resolve to chunk-declared bindings are followed; indirect
    // calls (`const g = f; g()`), method calls (`obj.method()`), and
    // dynamic dispatch are conservatively unmodelled.
    promote_at_init_calls(facts, &binding_owner, &mut raw_edges);

    emit_s_chain(facts, options, &mut raw_edges);

    // Sort + assign stable `OwnerEdgeId` indices, then build CSR
    // adjacency in one pass.
    raw_edges.sort_by(|(from_a, to_a, reason_a), (from_b, to_b, reason_b)| {
        from_a
            .cmp(from_b)
            .then(to_a.cmp(to_b))
            .then(reason_a.kind.cmp(&reason_b.kind))
            .then(reason_a.statement_ordinal.cmp(&reason_b.statement_ordinal))
            .then(reason_a.binding.cmp(&reason_b.binding))
    });
    let edges: Vec<OwnerEdge> = raw_edges
        .into_iter()
        .enumerate()
        .map(|(idx, (from, to, reason))| OwnerEdge {
            id: OwnerEdgeId(idx),
            from,
            to,
            reason,
        })
        .collect();
    let mut out_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
    let mut in_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
    for edge in &edges {
        if let Some(slot) = out_edges.get_mut(edge.from.0) {
            slot.push(edge.id);
        }
        if let Some(slot) = in_edges.get_mut(edge.to.0) {
            slot.push(edge.id);
        }
    }

    OwnerGraph {
        nodes,
        edges,
        out_edges,
        in_edges,
    }
}

/// Side-effect ordering edges (`S` per DESIGN.md "Module dep graphs").
/// At owner level, links impure top-level statements so any realizable
/// schedule preserves their observable order.
///
/// Two emission modes selected by [`OwnerGraphOptions`]:
///
/// - **Strict chain** (default): every later impure statement gets one
///   incoming `Sequenced` edge from the immediately previous impure
///   statement. Transitive reduction of the total order; soundest path.
/// - **Dataflow-aware** (`dataflow_aware_s_chain = true`): emit
///   `Sequenced(curr → prev)` only when `curr` reads or writes a cell
///   `prev` wrote (last-writer-precedes-reader). Statements that fail
///   the `dataflow_summarizable` check (dynamic globalThis key, `with`,
///   direct `eval`, `Function(...)` constructor, `defineProperty` on
///   globals, `Proxy` on globals) fall back to the strict edge against
///   every prior impure owner. See `dataflow_audit.md` for the
///   precondition this relaxation requires.
///
/// `purity` is computed upstream (`classify_expr_purity`) so pure
/// literal initializers (`const X = 42`, `const X = { a: 1 }`,
/// function/class declarations without observable static init) don't
/// contribute to S. Without that precision the cross-module S graph
/// would be dense enough to reject realistic specs for trivially pure
/// const sequences.
fn emit_s_chain(
    facts: &[StatementFacts],
    options: OwnerGraphOptions,
    raw_edges: &mut Vec<(OwnerId, OwnerId, EdgeReason)>,
) {
    if !options.dataflow_aware_s_chain {
        let mut prev: Option<OwnerId> = None;
        for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
            let from = OwnerId(stmt.ordinal.0);
            if let Some(to) = prev
                && from != to
            {
                raw_edges.push((from, to, EdgeReason::sequenced(stmt.ordinal)));
            }
            prev = Some(from);
        }
        return;
    }

    // Dataflow-aware emission: last-writer-precedes-reader-or-writer.
    // For each impure `curr`, emit an incoming Sequenced edge from the
    // most recent prior impure owner that wrote any cell in
    // `curr.reads ∪ curr.writes`. Statements with
    // `dataflow_summarizable = false` are treated as touching every
    // cell — they get edges to every prior impure owner and become a
    // barrier for subsequent statements.
    let mut last_writer: BTreeMap<EffectCell, OwnerId> = BTreeMap::new();
    let mut prior_impure_owners: Vec<OwnerId> = Vec::new();
    let mut opaque_barrier: Option<OwnerId> = None;
    for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
        let from = OwnerId(stmt.ordinal.0);
        let mut targets: BTreeSet<OwnerId> = BTreeSet::new();
        if stmt.effects.dataflow_summarizable {
            for cell in stmt.effects.reads.iter().chain(stmt.effects.writes.iter()) {
                if let Some(&to) = last_writer.get(cell) {
                    targets.insert(to);
                }
            }
            // Non-summarizable prior statements are barriers: any later
            // summarizable statement still depends on them, since we
            // don't know what cells they touched.
            if let Some(barrier) = opaque_barrier {
                targets.insert(barrier);
            }
        } else {
            // This statement can't be summarized: treat it as reading
            // and writing every cell. Depend on every prior impure
            // owner, and become the new opaque barrier so later
            // summarizable statements depend on us too.
            targets.extend(prior_impure_owners.iter().copied());
            opaque_barrier = Some(from);
        }
        for to in targets {
            if from != to {
                raw_edges.push((from, to, EdgeReason::sequenced(stmt.ordinal)));
            }
        }
        for cell in &stmt.effects.writes {
            last_writer.insert(cell.clone(), from);
        }
        prior_impure_owners.push(from);
    }
}

/// Promote function-body lazy reads/rebinds to eager owner edges from
/// every statement that at-init-calls the function. Transitive over
/// the call graph among chunk-declared functions: a top-level
/// `f()` whose `f` calls `g` in its body promotes through `g`'s lazy
/// reads/rebinds too. See DESIGN.md "At-init call promotion".
///
/// Per-statement dedup: at most one promoted eager edge per
/// (caller, target-owner) pair, and at most one promoted rebind edge
/// per (caller, target-owner) pair. Without dedup, a single
/// at-init call to a function with N transitive lazy reads would emit
/// N edges from the caller, and multiple at-init calls in the same
/// statement would multiply that further.
fn promote_at_init_calls(
    facts: &[StatementFacts],
    binding_owner: &HashMap<Id, OwnerId>,
    raw_edges: &mut Vec<(OwnerId, OwnerId, EdgeReason)>,
) {
    // 1. Build the call graph: owner → owner edges for each
    //    chunk-declared function callee reachable via *first-order*
    //    body_calls. Nested-closure calls (e.g. inside an arrow
    //    returned by the body) don't fire when the body is invoked
    //    synchronously, so they don't belong on the promotion call
    //    graph — see DESIGN.md "At-init call promotion" and the e2e
    //    test `at_init_promotion_nested_closure_test`.
    //
    //    Add every owner whose body has any first-order lazy reads /
    //    rebinds / calls as a node — those are the callable owners
    //    whose body closures we may need to promote, even if the body
    //    itself makes no calls (e.g. `function readB() { return B; }`).
    let mut call_graph: DiGraphMap<OwnerId, ()> = DiGraphMap::new();
    for stmt in facts {
        let owner = OwnerId(stmt.ordinal.0);
        if !stmt.first_order_body_calls.is_empty()
            || !stmt.first_order_lazy_reads.is_empty()
            || !stmt.first_order_lazy_rebinds.is_empty()
        {
            call_graph.add_node(owner);
        }
    }
    for stmt in facts {
        if stmt.first_order_body_calls.is_empty() {
            continue;
        }
        let caller = OwnerId(stmt.ordinal.0);
        for callee_id in &stmt.first_order_body_calls {
            let Some(callee_owner) = binding_owner.get(callee_id) else {
                continue;
            };
            call_graph.add_node(*callee_owner);
            call_graph.add_edge(caller, *callee_owner, ());
        }
    }
    if call_graph.node_count() == 0 {
        return;
    }

    // 2. Tarjan SCC. `tarjan_scc` returns SCCs in reverse topological
    //    order: leaves (no outgoing edges to other SCCs) first.
    let sccs = tarjan_scc(&call_graph);
    let mut scc_of: BTreeMap<OwnerId, usize> = BTreeMap::new();
    for (idx, scc) in sccs.iter().enumerate() {
        for owner in scc {
            scc_of.insert(*owner, idx);
        }
    }

    // 3. Per-owner seeds: own lazy_reads / lazy_rebinds resolved to
    //    BindingId. Filters out targets whose owner is a function
    //    declaration — function bindings are hoisted at module
    //    instantiation (Phase 1 of ESM linking), so a cross-module
    //    read of a function never observes a TDZ. Promoting such
    //    reads would spuriously close cycles for shapes like mutual
    //    recursion across modules (`function even(){odd()}` /
    //    `function odd(){even()}`), which are actually realizable.
    //    Other declared kinds (VarDecl, ClassDecl) are kept: const /
    //    let / class are TDZ-locked until their statement runs, so a
    //    cross-module read inside an at-init-called function does
    //    fire the realizability hazard. `var` is technically hoisted
    //    too but is rare enough not to warrant a separate distinction
    //    in StatementKind.
    let mut stmt_by_owner: BTreeMap<OwnerId, &StatementFacts> = BTreeMap::new();
    for stmt in facts {
        stmt_by_owner.insert(OwnerId(stmt.ordinal.0), stmt);
    }
    let target_is_hoisted = |id: &Id| -> bool {
        let Some(target_owner) = binding_owner.get(id) else {
            return false;
        };
        stmt_by_owner
            .get(target_owner)
            .map(|stmt| stmt.kind == StatementKind::FnDecl)
            .unwrap_or(false)
    };
    let mut scc_reads: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
    let mut scc_rebinds: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];

    // 4. Closure over the call graph. Iterate SCCs in
    //    reverse-topological order (leaves first). For each SCC,
    //    union members' own seeds plus successor SCC closures.
    for (scc_idx, scc) in sccs.iter().enumerate() {
        let mut reads: BTreeSet<Id> = BTreeSet::new();
        let mut rebinds: BTreeSet<Id> = BTreeSet::new();
        for owner in scc {
            let Some(stmt) = stmt_by_owner.get(owner) else {
                continue;
            };
            for id in &stmt.first_order_lazy_reads {
                if binding_owner.contains_key(id) && !target_is_hoisted(id) {
                    reads.insert(id.clone());
                }
            }
            for id in &stmt.first_order_lazy_rebinds {
                if binding_owner.contains_key(id) {
                    rebinds.insert(id.clone());
                }
            }
        }
        for owner in scc {
            for (_, target, _) in call_graph.edges(*owner) {
                let Some(&target_scc) = scc_of.get(&target) else {
                    continue;
                };
                if target_scc == scc_idx {
                    continue;
                }
                reads.extend(scc_reads[target_scc].iter().cloned());
                rebinds.extend(scc_rebinds[target_scc].iter().cloned());
            }
        }
        scc_reads[scc_idx] = reads;
        scc_rebinds[scc_idx] = rebinds;
    }

    // 5. Emit promoted edges with per-statement, per-kind dedup.
    for stmt in facts {
        if stmt.at_init_calls.is_empty() {
            continue;
        }
        let caller = OwnerId(stmt.ordinal.0);
        let mut promoted_read_targets: BTreeSet<OwnerId> = BTreeSet::new();
        let mut promoted_rebind_targets: BTreeSet<OwnerId> = BTreeSet::new();
        for callee_id in &stmt.at_init_calls {
            let Some(callee_owner) = binding_owner.get(callee_id) else {
                continue;
            };
            let Some(&scc_idx) = scc_of.get(callee_owner) else {
                continue;
            };
            for target_binding in &scc_reads[scc_idx] {
                let Some(target_owner) = binding_owner.get(target_binding) else {
                    continue;
                };
                if caller == *target_owner {
                    continue;
                }
                if !promoted_read_targets.insert(*target_owner) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_owner,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone()),
                ));
            }
            for target_binding in &scc_rebinds[scc_idx] {
                let Some(target_owner) = binding_owner.get(target_binding) else {
                    continue;
                };
                if caller == *target_owner {
                    continue;
                }
                if !promoted_rebind_targets.insert(*target_owner) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_owner,
                    EdgeReason::eager_rebind(stmt.ordinal, target_binding.clone()),
                ));
            }
        }
    }
}

/// Quotient the owner graph by `partition` to build the module
/// dependency graph consumed by validation and emit. The single
/// public construction path; peelability and reports both go through
/// this for any non-hypothetical quotient.
pub fn build_module_quotient(owner_graph: &OwnerGraph, partition: &Partition) -> ModuleQuotient {
    let mut graph = ModuleQuotient(DiGraphMap::new());
    let mut seen_side_effect_module_pairs = BTreeSet::<(ModuleId, ModuleId)>::new();
    for edge in &owner_graph.edges {
        let from = partition.of(edge.from);
        let to = partition.of(edge.to);
        if from == to {
            continue;
        }
        if edge.reason.is_sequenced() && !seen_side_effect_module_pairs.insert((from, to)) {
            continue;
        }
        graph.record_reason(from, to, edge.reason.clone());
    }
    graph
}

/// Stable per-chunk identity of an owner-graph edge. Equal to the
/// edge's position in [`OwnerGraph::edges`]. The previous
/// representation stored the report-shape spelling
/// (`format!("owner_edge:{idx}")`) on every entry; that spelling is
/// `O(n_edges)` strings allocated per chunk and
/// `O(n_blockers × n_candidates)` clones inside the peelability hot
/// loop. Carry the typed index instead and let the report layer do
/// the formatting at its single serialization boundary via
/// [`OwnerEdgeId::report_key`].
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct OwnerEdgeId(pub usize);

impl OwnerEdgeId {
    pub(crate) fn report_key(self) -> String {
        format!("owner_edge:{}", self.0)
    }
}

#[derive(Debug, Clone)]
pub struct OwnerEdge {
    pub id: OwnerEdgeId,
    pub from: OwnerId,
    pub to: OwnerId,
    pub reason: EdgeReason,
}
