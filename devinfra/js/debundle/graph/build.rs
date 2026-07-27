use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use swc_ecma_ast::Id;

use crate::facts::EffectCell;
use crate::{StatementFacts, StatementKind, StatementOrdinal};

// `OwnerGraphOptions` lives in `spec.rs` — both the spec YAML surface
// and the graph-build API consume the same type. Re-exported from
// crate root via `lib.rs` so `analysis::OwnerGraphOptions` continues
// to be the canonical path for external callers.
pub use spec::OwnerGraphOptions;

use super::edge::{EdgeReason, OwnerEdge, OwnerEdgeId};
use super::owner_graph::{OwnerGraph, OwnerId, OwnerNode};

/// Two distinct top-level statements declare the same binding
/// (`var x = 1; var x = 2;` — legal JS, but the owner graph models
/// each binding as having exactly one owning statement). Letting the
/// last declaration win would silently drop every edge into the
/// earlier owner, so the earlier statement could be ordered after its
/// readers. Rejecting the chunk is the accepted over-restriction
/// (<docs/design.md> "Soundness over completeness").
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DuplicateTopLevelDeclaration {
    pub binding: swc_atoms::Atom,
    pub first: StatementOrdinal,
    pub second: StatementOrdinal,
}

impl fmt::Display for DuplicateTopLevelDeclaration {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "duplicate top-level declaration of binding `{}`: statements #{} and #{} both \
             declare it; the owner graph requires a single owning statement per binding. \
             Rewrite the chunk so the binding is declared once (e.g. merge the declarations \
             or rename one of them).",
            self.binding, self.first.0, self.second.0,
        )
    }
}

impl std::error::Error for DuplicateTopLevelDeclaration {}

/// Build the fine owner graph from per-statement facts. Pure IR
/// construction: no module assignment, no quotient. Module-level
/// dependencies are derived later by [`build_module_quotient`]
/// given a [`Partition`] mapping owners to destination modules.
///
/// Uses default (strictly-conservative) [`OwnerGraphOptions`]. Call
/// [`build_owner_graph_with`] when the chunk spec opts into
/// conditionally-correct refinements.
///
/// [`build_module_quotient`]: super::quotient::build_module_quotient
/// [`Partition`]: crate::partition::Partition
pub fn build_owner_graph(
    facts: &[StatementFacts],
) -> Result<OwnerGraph, DuplicateTopLevelDeclaration> {
    build_owner_graph_with(facts, OwnerGraphOptions::default())
}

/// Like [`build_owner_graph`] but takes per-chunk [`OwnerGraphOptions`].
pub fn build_owner_graph_with(
    facts: &[StatementFacts],
    options: OwnerGraphOptions,
) -> Result<OwnerGraph, DuplicateTopLevelDeclaration> {
    let mut binding_owner = HashMap::<Id, OwnerId>::new();
    let mut nodes = Vec::<OwnerNode>::with_capacity(facts.len());
    for stmt in facts {
        for binding in &stmt.declared {
            if let Some(prev) = binding_owner.insert(binding.clone(), OwnerId(stmt.ordinal.0)) {
                return Err(DuplicateTopLevelDeclaration {
                    binding: binding.0.clone(),
                    first: StatementOrdinal(prev.0),
                    second: stmt.ordinal,
                });
            }
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
        for binding in &stmt.reads.eager {
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
        for binding in &stmt.reads.lazy {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::lazy_use,
                stmt.ordinal,
            );
        }
        for binding in &stmt.rebinds.eager {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::eager_rebind,
                stmt.ordinal,
            );
        }
        // Only first-order body rebinds emit a constraining
        // `LazyRebind` edge: a rebind inside a nested closure (e.g.
        // an arrow stashed on `globalThis` by the body) doesn't fire
        // when the function is invoked synchronously, so it must not
        // constrain init order or feed at-init call promotion. See
        // the e2e test `at_init_promotion_nested_closure_test`.
        for binding in &stmt.rebinds.first_order_lazy {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::lazy_rebind,
                stmt.ordinal,
            );
        }
        // Deeper-nested (or post-await) rebinds still rebind the
        // binding cell whenever they DO fire — and ESM imports are
        // read-only at any time, not just during init. Emit a
        // non-init-constraining `DeferredRebind` edge so
        // cross-destination splits are rejected and `G_atomic`
        // forces co-location, without manufacturing an init-order
        // constraint nothing fires at init.
        for binding in &stmt.rebinds.lazy {
            if stmt.rebinds.first_order_lazy.contains(binding) {
                continue;
            }
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::deferred_rebind,
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

    // At-init call promotion (docs/design.md "At-init call promotion").
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
    let mut callee_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
    for edge in &edges {
        if let Some(slot) = out_edges.get_mut(edge.from.0) {
            slot.push(edge.id);
        }
        if let Some(slot) = in_edges.get_mut(edge.to.0) {
            slot.push(edge.id);
        }
        if let Some(callee) = edge.reason.role.promoted_callee()
            && let Some(slot) = callee_edges.get_mut(callee.0)
        {
            slot.push(edge.id);
        }
    }

    Ok(OwnerGraph {
        nodes,
        edges,
        out_edges,
        in_edges,
        callee_edges,
    })
}

/// Side-effect ordering edges (`S` per docs/design.md "Module dep graphs").
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
///   every prior impure owner. See `README.md` →
///   "Conditionally-correct optimizations" for the precondition this
///   relaxation requires.
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

    // Dataflow-aware emission. For each impure `curr`, emit an
    // incoming Sequenced edge from:
    //
    // - the most recent prior impure owner that wrote any cell in
    //   `curr.reads ∪ curr.writes` (read-after-write /
    //   write-after-write), and
    // - every prior impure owner that READ a cell `curr` writes
    //   since that cell's last write (write-after-read — without
    //   this, a later writer could be scheduled before an earlier
    //   reader and the reader would observe the new value).
    //
    // Statements with `dataflow_summarizable = false` are treated as
    // touching every cell — they get edges to every prior impure
    // owner and become a barrier for subsequent statements.
    let mut last_writer: BTreeMap<EffectCell, OwnerId> = BTreeMap::new();
    let mut readers_since_last_write: BTreeMap<EffectCell, BTreeSet<OwnerId>> = BTreeMap::new();
    let mut prior_impure_owners: Vec<OwnerId> = Vec::new();
    let mut opaque_barrier: Option<OwnerId> = None;
    for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
        let from = OwnerId(stmt.ordinal.0);
        let effects = stmt.effects();
        let mut targets: BTreeSet<OwnerId> = BTreeSet::new();
        if stmt.dataflow_summarizable {
            for cell in effects.reads.iter().chain(effects.writes.iter()) {
                if let Some(&to) = last_writer.get(cell) {
                    targets.insert(to);
                }
            }
            for cell in &effects.writes {
                if let Some(readers) = readers_since_last_write.get(cell) {
                    targets.extend(readers.iter().copied());
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
        if stmt.dataflow_summarizable {
            for cell in effects.writes {
                readers_since_last_write.remove(&cell);
                last_writer.insert(cell, from);
            }
            for cell in effects.reads {
                readers_since_last_write
                    .entry(cell)
                    .or_default()
                    .insert(from);
            }
        }
        prior_impure_owners.push(from);
    }
}

/// Promote function-body lazy reads/rebinds to eager owner edges from
/// every statement that at-init-calls the function. Transitive over
/// the call graph among chunk-declared functions: a top-level
/// `f()` whose `f` calls `g` in its body promotes through `g`'s lazy
/// reads/rebinds too. See docs/design.md "At-init call promotion".
///
/// Per-statement dedup: at most one promoted eager edge per
/// (caller, target-owner) pair. Rebind targets are also promoted as
/// eager order edges; the write site's direct rebind edge enforces
/// write legality. Without dedup, a single at-init call to a
/// function with N transitive lazy reads would emit N edges from the
/// caller, and multiple at-init calls in the same statement would
/// multiply that further.
fn promote_at_init_calls(
    facts: &[StatementFacts],
    binding_owner: &HashMap<Id, OwnerId>,
    raw_edges: &mut Vec<(OwnerId, OwnerId, EdgeReason)>,
) {
    let mut stmt_by_owner: BTreeMap<OwnerId, &StatementFacts> = BTreeMap::new();
    for stmt in facts {
        stmt_by_owner.insert(OwnerId(stmt.ordinal.0), stmt);
    }

    // Bindings rebound anywhere in the chunk: their value at call
    // time may not be the function lexically defined at their owner,
    // so calls to them are not precisely resolvable.
    let rebound: BTreeSet<&Id> = facts
        .iter()
        .flat_map(|s| s.rebinds.eager.iter().chain(s.rebinds.lazy.iter()))
        .collect();

    // A callee binding is precisely resolvable iff (a) its declaring
    // statement binds it directly to a function value (`function f`,
    // `const f = () => ...`) and (b) it is never rebound. Everything
    // else — aliases (`const g = readB`), object-literal methods,
    // conditional initializers, rebound functions — takes the
    // conservative read-closure fallback below.
    let resolvable_callee = |id: &Id| -> Option<OwnerId> {
        let owner = binding_owner.get(id)?;
        let stmt = stmt_by_owner.get(owner)?;
        (stmt.declares_direct_function && !rebound.contains(id)).then_some(*owner)
    };

    // 1. Build the call graph: owner → owner edges for each
    //    resolvable callee reachable via *first-order* calls.lazy.
    //    Nested-closure calls (e.g. inside an arrow returned by the
    //    body) don't fire when the body is invoked synchronously, so
    //    they don't belong on the promotion call graph — see
    //    docs/design.md "At-init call promotion" and the e2e test
    //    `at_init_promotion_nested_closure_test`.
    //
    //    Add every owner whose body has any first-order lazy reads /
    //    rebinds / calls — or an unresolvable first-order callee — as
    //    a node, so the closure pass below covers it even if it makes
    //    no resolvable calls (e.g. `function readB() { return B; }`).
    //
    //    Per-owner "fallback roots": owners through which a function
    //    value could reach a call the owner's first-order body makes
    //    but promotion can't follow — the owners of bindings the
    //    unresolved call mentions, the owners of chunk-declared
    //    Ident callees that aren't direct never-rebound functions,
    //    and the owner itself when the unresolved call carries an
    //    inline function expression. At-init callers inherit these
    //    roots transitively through the call graph and expand them
    //    via [`UnresolvedCallFallback`].
    let fallback_roots_of =
        |sources: &BTreeSet<Id>, inline_fn: bool, self_owner: OwnerId| -> BTreeSet<OwnerId> {
            let mut roots: BTreeSet<OwnerId> = sources
                .iter()
                .filter_map(|id| binding_owner.get(id).copied())
                .collect();
            if inline_fn {
                roots.insert(self_owner);
            }
            roots
        };
    let owner_first_order_roots = |stmt: &StatementFacts| -> BTreeSet<OwnerId> {
        let owner = OwnerId(stmt.ordinal.0);
        let mut roots = fallback_roots_of(
            &stmt.first_order_unresolved_sources,
            stmt.first_order_unresolved_inline_fn,
            owner,
        );
        for callee_id in &stmt.calls.first_order_lazy {
            if resolvable_callee(callee_id).is_none()
                && let Some(&callee_owner) = binding_owner.get(callee_id)
            {
                roots.insert(callee_owner);
            }
        }
        roots
    };
    let mut call_graph: DiGraphMap<OwnerId, ()> = DiGraphMap::new();
    for stmt in facts {
        let owner = OwnerId(stmt.ordinal.0);
        if !stmt.calls.first_order_lazy.is_empty()
            || !stmt.reads.first_order_lazy.is_empty()
            || !stmt.rebinds.first_order_lazy.is_empty()
            || !stmt.first_order_unresolved_sources.is_empty()
            || stmt.first_order_unresolved_inline_fn
        {
            call_graph.add_node(owner);
        }
    }
    for stmt in facts {
        if stmt.calls.first_order_lazy.is_empty() {
            continue;
        }
        let caller = OwnerId(stmt.ordinal.0);
        for callee_id in &stmt.calls.first_order_lazy {
            let Some(callee_owner) = resolvable_callee(callee_id) else {
                continue;
            };
            call_graph.add_node(callee_owner);
            call_graph.add_edge(caller, callee_owner, ());
        }
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

    // 3. Per-owner seeds: own reads.lazy / rebinds.lazy resolved to
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
    let mut scc_fallback_roots: Vec<BTreeSet<OwnerId>> = vec![BTreeSet::new(); sccs.len()];

    // 4. Closure over the call graph. Iterate SCCs in
    //    reverse-topological order (leaves first). For each SCC,
    //    union members' own seeds plus successor SCC closures, and
    //    propagate fallback roots the same way.
    for (scc_idx, scc) in sccs.iter().enumerate() {
        let mut reads: BTreeSet<Id> = BTreeSet::new();
        let mut rebinds: BTreeSet<Id> = BTreeSet::new();
        let mut roots: BTreeSet<OwnerId> = BTreeSet::new();
        for owner in scc {
            let Some(stmt) = stmt_by_owner.get(owner) else {
                continue;
            };
            roots.extend(owner_first_order_roots(stmt));
            for id in &stmt.reads.first_order_lazy {
                if binding_owner.contains_key(id) && !target_is_hoisted(id) {
                    reads.insert(id.clone());
                }
            }
            for id in &stmt.rebinds.first_order_lazy {
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
                roots.extend(scc_fallback_roots[target_scc].iter().copied());
            }
        }
        scc_reads[scc_idx] = reads;
        scc_rebinds[scc_idx] = rebinds;
        scc_fallback_roots[scc_idx] = roots;
    }

    // 5. Emit promoted edges with per-statement, per-kind dedup.
    //    Each emitted reason carries `EdgeRole::PromotedAtInit {
    //    callee_owner }` so the realizability gate can drop the edge
    //    when caller and callee land in different partition slots —
    //    the body read fires inside a call into a different module,
    //    after that callee module (and its imports) have already
    //    evaluated, so the manufactured constraint from R to the
    //    target's module is redundant with the already-recorded
    //    R -> callee-module edge. See [`EdgeRole`].
    //
    //    Statements whose at-init calls can't all be resolved take
    //    the conservative fallback in addition: see
    //    [`UnresolvedCallFallback`].
    let mut fallback: Option<UnresolvedCallFallback> = None;
    for stmt in facts {
        if stmt.calls.eager.is_empty()
            && stmt.at_init_unresolved_sources.is_empty()
            && !stmt.at_init_unresolved_inline_fn
        {
            continue;
        }
        let caller = OwnerId(stmt.ordinal.0);
        let mut promoted_read_targets: BTreeSet<OwnerId> = BTreeSet::new();
        let mut fallback_roots = fallback_roots_of(&stmt.at_init_unresolved_sources, false, caller);
        if stmt.at_init_unresolved_inline_fn
            && let Some(&scc_idx) = scc_of.get(&caller)
        {
            fallback_roots.extend(scc_fallback_roots[scc_idx].iter().copied());
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
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(caller),
                ));
            }
            for target_binding in &scc_rebinds[scc_idx] {
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
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(caller),
                ));
            }
        }
        for callee_id in &stmt.calls.eager {
            let Some(callee_owner) = resolvable_callee(callee_id) else {
                // A bare-Ident callee that isn't chunk-declared is a
                // global/import — out of single-chunk analysis scope
                // (documented precondition in docs/design.md). A
                // chunk-declared but unresolvable callee falls back
                // through the read closure of its own owner.
                if let Some(&callee_owner) = binding_owner.get(callee_id) {
                    fallback_roots.insert(callee_owner);
                }
                continue;
            };
            let Some(&scc_idx) = scc_of.get(&callee_owner) else {
                continue;
            };
            fallback_roots.extend(scc_fallback_roots[scc_idx].iter().copied());
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
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(callee_owner),
                ));
            }
            for target_binding in &scc_rebinds[scc_idx] {
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
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(callee_owner),
                ));
            }
        }
        if fallback_roots.is_empty() {
            continue;
        }
        let fallback =
            fallback.get_or_insert_with(|| UnresolvedCallFallback::build(facts, binding_owner));
        for root in fallback_roots {
            let (closure_reads, closure_rebinds) = fallback.closures_for(root);
            // Rebind targets get an *order* constraint only (EagerUse,
            // not EagerRebind): assignment to a TDZ-locked binding
            // throws until its statement runs, so the target must
            // evaluate before the caller — but write LEGALITY
            // (read-only imports) is already enforced by the direct
            // `LazyRebind` / `DeferredRebind` edge from the statement
            // lexically containing the write, which forces the write
            // site to co-locate with the declarer. Emitting
            // `EagerRebind` here would force the *caller* into the
            // co-location unit too, rejecting realizable shapes
            // (`c.bump(1)` calling a co-located setter).
            for target_binding in closure_reads.iter().chain(closure_rebinds.iter()) {
                if target_is_hoisted(target_binding) {
                    continue;
                }
                let Some(target_owner) = binding_owner.get(target_binding) else {
                    continue;
                };
                if caller == *target_owner || !promoted_read_targets.insert(*target_owner) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_owner,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(caller),
                ));
            }
        }
    }
}

/// Conservative fallback for at-init calls promotion can't resolve
/// (member calls, IIFEs, aliases, tagged templates, calls into
/// functions whose own bodies contain such calls).
///
/// Premise: whatever function value an unresolvable at-init call
/// invokes, it must have reached the call site through one of the
/// bindings the call expression mentions (callee root, arguments,
/// computed keys) or through an inline function expression at the
/// call. Each such "root" owner is expanded to the **full lazy
/// closure** of every owner reachable from it through the chunk's
/// read graph: edges `O → owner(b)` for every chunk binding `b` the
/// owner `O` reads (eagerly or lazily), with every reachable owner
/// contributing its `reads.lazy` / `rebinds.lazy` at any nesting
/// depth. The caller statement then eagerly depends on every
/// collected target.
///
/// Documented residual preconditions (see docs/design.md "At-init
/// call promotion" → Limitations): function values that reach the
/// call site through global/object property stashes, through rebound
/// bindings (`let g; g = readB; g()`), through parameters of
/// chunk-declared functions (`function h(cb) { cb(); }`), via `new`,
/// or from other chunks are not modelled.
///
/// Fallback-only edges carry `EdgeRole::PromotedAtInit` with
/// `callee_owner = caller`, so neither projection view ever drops
/// them — there is no precisely-known callee module whose evaluation
/// could make the constraint redundant.
struct UnresolvedCallFallback {
    scc_of: BTreeMap<OwnerId, usize>,
    scc_reads: Vec<BTreeSet<Id>>,
    scc_rebinds: Vec<BTreeSet<Id>>,
}

impl UnresolvedCallFallback {
    fn build(facts: &[StatementFacts], binding_owner: &HashMap<Id, OwnerId>) -> Self {
        let mut read_graph: DiGraphMap<OwnerId, ()> = DiGraphMap::new();
        for stmt in facts {
            let owner = OwnerId(stmt.ordinal.0);
            read_graph.add_node(owner);
            for id in stmt.reads.eager.iter().chain(stmt.reads.lazy.iter()) {
                if let Some(&target) = binding_owner.get(id) {
                    read_graph.add_edge(owner, target, ());
                }
            }
        }
        let mut stmt_by_owner: BTreeMap<OwnerId, &StatementFacts> = BTreeMap::new();
        for stmt in facts {
            stmt_by_owner.insert(OwnerId(stmt.ordinal.0), stmt);
        }
        let sccs = tarjan_scc(&read_graph);
        let mut scc_of: BTreeMap<OwnerId, usize> = BTreeMap::new();
        for (idx, scc) in sccs.iter().enumerate() {
            for owner in scc {
                scc_of.insert(*owner, idx);
            }
        }
        let mut scc_reads: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
        let mut scc_rebinds: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
        for (scc_idx, scc) in sccs.iter().enumerate() {
            let mut reads: BTreeSet<Id> = BTreeSet::new();
            let mut rebinds: BTreeSet<Id> = BTreeSet::new();
            for owner in scc {
                let Some(stmt) = stmt_by_owner.get(owner) else {
                    continue;
                };
                for id in &stmt.reads.lazy {
                    if binding_owner.contains_key(id) {
                        reads.insert(id.clone());
                    }
                }
                for id in &stmt.rebinds.lazy {
                    if binding_owner.contains_key(id) {
                        rebinds.insert(id.clone());
                    }
                }
            }
            for owner in scc {
                for (_, target, _) in read_graph.edges(*owner) {
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
        Self {
            scc_of,
            scc_reads,
            scc_rebinds,
        }
    }

    fn closures_for(&self, owner: OwnerId) -> (&BTreeSet<Id>, &BTreeSet<Id>) {
        static EMPTY: std::sync::OnceLock<BTreeSet<Id>> = std::sync::OnceLock::new();
        let empty = EMPTY.get_or_init(BTreeSet::new);
        match self.scc_of.get(&owner) {
            Some(&idx) => (&self.scc_reads[idx], &self.scc_rebinds[idx]),
            None => (empty, empty),
        }
    }
}
