use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use swc_ecma_ast::Id;

use crate::facts::{EffectCell, SourceUnitReadClosure};
use crate::{StatementFacts, StatementKind};

// `DependencyGraphOptions` lives in `spec/spec.rs` — both the spec YAML surface
// and the graph-build API consume the same type. Re-exported from
// crate root via `lib.rs` so `analysis::DependencyGraphOptions` continues
// to be the canonical path for external callers.
pub use spec::DependencyGraphOptions;

use super::dependency_graph::{DependencyGraph, SourceUnit, SourceUnitId};
use super::edge::{DependencyEdge, DependencyEdgeId, EdgeReason, EdgeRole};

/// Multiple declarations of one binding are supported only when every site
/// declares `var`. Other duplicate declaration shapes remain rejected.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DuplicateTopLevelDeclaration {
    pub binding: swc_atoms::Atom,
    pub first: SourceUnitOrdinal,
    pub second: SourceUnitOrdinal,
}

impl fmt::Display for DuplicateTopLevelDeclaration {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "duplicate top-level declaration of binding `{}`: statements #{} and #{} both \
             declare it; only repeated `var` declarations are supported.",
            self.binding, self.first.0, self.second.0,
        )
    }
}

impl std::error::Error for DuplicateTopLevelDeclaration {}

/// Graph construction is intentionally source-free. Captured value inputs
/// must be resolved by the prepared whole-program AST/index pipeline before
/// callers extract StatementFacts and build this graph.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DependencyGraphBuildError {
    DuplicateTopLevelDeclaration(DuplicateTopLevelDeclaration),
    UnresolvedValueInputs { count: usize },
}

impl fmt::Display for DependencyGraphBuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateTopLevelDeclaration(error) => error.fmt(f),
            Self::UnresolvedValueInputs { count } => write!(
                f,
                "cannot build a dependency graph with {count} unresolved captured value input(s); resolve them against the exact provider source first"
            ),
        }
    }
}

impl std::error::Error for DependencyGraphBuildError {}

impl From<DuplicateTopLevelDeclaration> for DependencyGraphBuildError {
    fn from(error: DuplicateTopLevelDeclaration) -> Self {
        Self::DuplicateTopLevelDeclaration(error)
    }
}

/// Build the fine dependency graph from per-statement facts under the
/// per-chunk [`DependencyGraphOptions`]. Pure IR construction: no module
/// assignment, no quotient. Module-level dependencies are derived later
/// by [`build_module_quotient`] given a [`ModuleAssignment`] mapping source units to
/// destination modules.
///
/// [`build_module_quotient`]: super::quotient::build_module_quotient
/// [`ModuleAssignment`]: crate::module_assignment::ModuleAssignment
pub fn build_dependency_graph_with(
    facts: &[StatementFacts],
    options: DependencyGraphOptions,
) -> Result<DependencyGraph, DependencyGraphBuildError> {
    if !options.value_inputs.is_empty() {
        return Err(DependencyGraphBuildError::UnresolvedValueInputs {
            count: options.value_inputs.len(),
        });
    }
    let mut declaration_sites = BTreeMap::<Id, Vec<SourceUnitId>>::new();
    let mut nodes = Vec::<SourceUnit>::with_capacity(facts.len());
    for stmt in facts {
        for binding in &stmt.declared {
            declaration_sites
                .entry(binding.clone())
                .or_default()
                .push(SourceUnitId(stmt.ordinal.0));
        }
        let id = SourceUnitId(stmt.ordinal.0);
        nodes.push(SourceUnit {
            id,
            statement_ordinal: stmt.ordinal,
            source_location: stmt.source_location.clone(),
            declared: stmt.declared.clone(),
            kind: stmt.kind,
            purity: stmt.purity.clone(),
            callback_factory_barriers: stmt.callback_factory_barriers.clone(),
            heap_native_integrity_uses: stmt.heap_native_integrity_uses.clone(),
            synchronous_execution: stmt.synchronous_calls.as_ref().map(|calls| {
                crate::reports::schema::SynchronousExecutionReport {
                    local_record_factory_uses: calls.local_record_factory_uses.clone(),
                    complete: calls.complete,
                    native_integrity_checks: calls.native_integrity_checks.clone(),
                    native_prototype_property_checks: calls
                        .native_prototype_property_checks
                        .clone(),
                    native_prefix_observations: calls.native_prefix_observations.clone(),
                    native_integrity_uses: calls.native_integrity_uses.clone(),
                    inference_uses: calls.inference_uses.clone(),
                    precision_losses: calls.precision_losses.clone(),
                    callback_factory_uses: Vec::new(),
                    returned_record_uses: Vec::new(),
                    author_host_contract_uses: calls.author_host_contract_uses.clone(),
                }
            }),
            native_prototype_mutations: stmt
                .synchronous_calls
                .as_ref()
                .map(|calls| calls.native_prototype_mutations.clone())
                .unwrap_or_default(),
            native_integrity_witnesses: stmt
                .synchronous_calls
                .as_ref()
                .map(|calls| calls.native_integrity_witnesses.clone())
                .unwrap_or_default(),
            native_receiver_certificates: stmt
                .synchronous_calls
                .as_ref()
                .map(|calls| calls.native_receiver_certificates.clone())
                .unwrap_or_default(),
            callback_factory_uses: stmt
                .synchronous_calls
                .as_ref()
                .map_or_else(Vec::new, |calls| calls.callback_factory_uses.clone()),
            returned_record_uses: stmt
                .synchronous_calls
                .as_ref()
                .map_or_else(Vec::new, |calls| calls.returned_record_uses.clone()),
        });
    }

    // Keep every declaration site. A representative is only an address for a
    // binding; the co-declaration edges below make all sites inseparable.
    let binding_source_unit: HashMap<Id, SourceUnitId> = declaration_sites
        .iter()
        .map(|(binding, sites)| (binding.clone(), sites[0]))
        .collect();
    let mut shared_vars = BTreeSet::new();
    for (binding, sites) in &declaration_sites {
        if sites.len() > 1 {
            for &source_unit in sites {
                if !facts[source_unit.0].var_declared.contains(binding) {
                    return Err(DuplicateTopLevelDeclaration {
                        binding: binding.0.clone(),
                        first: SourceUnitOrdinal(sites[0].0),
                        second: SourceUnitOrdinal(sites[1].0),
                    }
                    .into());
                }
            }
            shared_vars.insert(binding.clone());
        }
    }

    // Collect (from, to, reason) triples; the final `edges` Vec is
    // sorted at the end so `DependencyEdgeId` indices are stable.
    let mut raw_edges = Vec::<(SourceUnitId, SourceUnitId, EdgeReason)>::new();
    for binding in &shared_vars {
        let sites = &declaration_sites[binding];
        for &source_unit in &sites[1..] {
            raw_edges.push((
                source_unit,
                sites[0],
                EdgeReason::co_declaration(SourceUnitOrdinal(source_unit.0), binding.clone()),
            ));
            raw_edges.push((
                sites[0],
                source_unit,
                EdgeReason::co_declaration(SourceUnitOrdinal(sites[0].0), binding.clone()),
            ));
        }
    }
    // Look-aside table for "what statement owns this SourceUnitId" — shared
    // by the direct eager-read filter, at-init call promotion, and heap-content
    // call propagation.
    let stmt_by_source_unit: std::collections::HashMap<SourceUnitId, &StatementFacts> = facts
        .iter()
        .map(|stmt| (SourceUnitId(stmt.ordinal.0), stmt))
        .collect();
    // A top-level eager read of a binding declared by a `function`
    // declaration cannot observe a TDZ: ECMAScript Phase 1 of module
    // linking (`ModuleDeclarationInstantiation`) binds every
    // `FunctionDeclaration` to its hoisted closure before any module
    // body runs. So `const x = f()` where `f` is a chunk-declared
    // FnDecl is safe regardless of which module owns `f` — there is
    // no init-order constraint to record. Retain the import as `HoistedUse`:
    // entering the function's module can still change ESM traversal and expose
    // a TDZ on another binding. Other declaration kinds retain eager
    // constraints: lexical declarations have a TDZ, while var initializers
    // change an already-hoisted cell whose value reads must observe in order.
    let target_is_hoisted = |id: &Id| -> bool {
        binding_source_unit
            .get(id)
            .and_then(|source_unit| stmt_by_source_unit.get(source_unit))
            .map(|stmt| stmt.kind == StatementKind::FnDecl)
            .unwrap_or(false)
    };
    let push_binding_edge = |raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
                             from: SourceUnitId,
                             binding: &Id,
                             make_reason: fn(SourceUnitOrdinal, Id) -> EdgeReason,
                             statement_ordinal: SourceUnitOrdinal| {
        let Some(to) = binding_source_unit.get(binding) else {
            return; // not declared in this chunk (global, ImportSpecifier, never-declared)
        };
        if from == *to {
            return;
        }
        raw_edges.push((from, *to, make_reason(statement_ordinal, binding.clone())));
    };
    for stmt in facts {
        let from = SourceUnitId(stmt.ordinal.0);
        for binding in &stmt.reads.eager {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                if target_is_hoisted(binding) {
                    EdgeReason::hoisted_use
                } else {
                    EdgeReason::eager_use
                },
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
    // Promoted edges are added at dependency graph level (assignment-
    // independent): intra-module promoted edges are dropped by the
    // quotient automatically. Only direct `f(...)` callees that
    // resolve to chunk-declared bindings are followed; indirect
    // calls (`const g = f; g()`), method calls (`obj.method()`), and
    // dynamic dispatch are conservatively unmodelled.
    promote_at_init_calls(
        facts,
        &binding_source_unit,
        &shared_vars,
        &stmt_by_source_unit,
        &mut raw_edges,
    );

    // Initializers and eager observations of shared var cells must retain their
    // order even when the expressions are pure. Include promoted reads from
    // at-init calls, not just syntactically direct reads.
    let mut accesses: BTreeMap<Id, BTreeSet<SourceUnitId>> = shared_vars
        .iter()
        .map(|binding| (binding.clone(), BTreeSet::new()))
        .collect();
    for stmt in facts {
        for binding in stmt
            .reads
            .eager
            .iter()
            .chain(&stmt.rebinds.eager)
            .chain(&stmt.var_initializers)
        {
            if let Some(source_units) = accesses.get_mut(binding) {
                source_units.insert(SourceUnitId(stmt.ordinal.0));
            }
        }
    }
    for (from, _, reason) in &raw_edges {
        if reason.kind == super::DepKind::EagerUse
            && let Some(binding) = reason.binding()
            && let Some(source_units) = accesses.get_mut(binding)
        {
            source_units.insert(*from);
        }
    }
    for source_units in accesses.values() {
        let source_units: Vec<_> = source_units.iter().copied().collect();
        for pair in source_units.windows(2) {
            raw_edges.push((
                pair[1],
                pair[0],
                EdgeReason::sequenced(SourceUnitOrdinal(pair[1].0)),
            ));
        }
    }

    emit_s_chain(facts, options, &mut raw_edges);
    emit_array_content_order(
        facts,
        &binding_source_unit,
        &shared_vars,
        &stmt_by_source_unit,
        &mut raw_edges,
    );

    // Sort + assign stable `DependencyEdgeId` indices, then build CSR
    // adjacency in one pass.
    raw_edges.sort_by(|(from_a, to_a, reason_a), (from_b, to_b, reason_b)| {
        from_a
            .cmp(from_b)
            .then(to_a.cmp(to_b))
            .then(reason_a.kind.cmp(&reason_b.kind))
            .then(reason_a.statement_ordinal.cmp(&reason_b.statement_ordinal))
            .then(reason_a.binding.cmp(&reason_b.binding))
    });
    let edges: Vec<DependencyEdge> = raw_edges
        .into_iter()
        .enumerate()
        .map(|(idx, (from, to, reason))| DependencyEdge {
            id: DependencyEdgeId(idx),
            from,
            to,
            reason,
        })
        .collect();
    Ok(DependencyGraph::from_parts(nodes, edges))
}

/// Preserve source-order observations of mutable array contents. Binding-cell
/// ordering cannot do this: an array initializer writes the object binding,
/// while a later `push` or indexed write changes the object behind it.
fn emit_array_content_order(
    facts: &[StatementFacts],
    binding_source_unit: &HashMap<Id, SourceUnitId>,
    shared_vars: &BTreeSet<Id>,
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) {
    let rebound: BTreeSet<_> = facts
        .iter()
        .flat_map(|stmt| stmt.rebinds.eager.iter().chain(&stmt.rebinds.lazy).cloned())
        .collect();

    let resolved_callee = |binding: &swc_ecma_ast::Id| -> Option<SourceUnitId> {
        resolve_direct_function_source_unit(
            binding,
            binding_source_unit,
            shared_vars,
            stmt_by_source_unit,
            &rebound,
        )
    };

    // A hoisted or escaped callable can allocate before its lexical statement.
    // Keep those origins live from the start; eager-only arrays become live
    // when their allocation or first precise content effect is encountered.
    let mut has_live_array_contents = facts
        .iter()
        .any(|stmt| !stmt.array_contents.allocations.lazy.is_empty());
    let mut lazy_effects: Vec<_> = facts
        .iter()
        .map(|stmt| ArrayContentSummary {
            allocations: stmt.array_contents.allocations.first_order_lazy.clone(),
            effects: array_content_cells(
                &stmt.array_contents.reads.first_order_lazy,
                &stmt.array_contents.writes.first_order_lazy,
            ),
            opaque: stmt.array_contents.opaque.first_order_lazy,
        })
        .collect();
    // Calls in function bodies inherit their callee's first-order heap effects.
    // The monotone closure handles recursion and mutually recursive helpers.
    loop {
        let mut changed = false;
        for stmt in facts {
            let source = stmt.ordinal.0;
            for callee in &stmt.array_contents.calls.first_order_lazy {
                let Some(target) = resolved_callee(callee) else {
                    if !lazy_effects[source].opaque {
                        lazy_effects[source].opaque = true;
                        changed = true;
                    }
                    continue;
                };
                let target_effects = lazy_effects[target.0].clone();
                let allocation_count = lazy_effects[source].allocations.len();
                lazy_effects[source]
                    .allocations
                    .extend(target_effects.allocations);
                changed |= lazy_effects[source].allocations.len() != allocation_count;
                changed |=
                    union_effects(&mut lazy_effects[source].effects, &target_effects.effects);
                if target_effects.opaque && !lazy_effects[source].opaque {
                    lazy_effects[source].opaque = true;
                    changed = true;
                }
            }
        }
        if !changed {
            break;
        }
    }

    let mut events = Vec::new();
    for stmt in facts {
        let mut allocations = stmt.array_contents.allocations.eager.clone();
        let mut effects = array_content_cells(
            &stmt.array_contents.reads.eager,
            &stmt.array_contents.writes.eager,
        );
        let mut opaque = stmt.array_contents.opaque.eager
            || (!stmt.dataflow_summarizable && !stmt.purity.is_pure())
            // The shared synchronous interpreter owns call-target and
            // protocol precision. If it cannot completely summarize this
            // source unit, an otherwise-pure expression may still observe
            // mutable array contents through coercion, a getter, or a
            // callable that the array collector does not resolve. Keep one
            // heap-frontier barrier instead of duplicating that resolver.
            || stmt
                .synchronous_calls
                .as_ref()
                .is_some_and(|calls| !calls.complete);
        // Function declarations are inert; only a recognized synchronous
        // at-init call inherits their first-order effects.
        for callee in &stmt.array_contents.calls.eager {
            if let Some(target) = resolved_callee(callee) {
                allocations.extend(lazy_effects[target.0].allocations.iter().cloned());
                union_effects(&mut effects, &lazy_effects[target.0].effects);
                opaque |= lazy_effects[target.0].opaque;
            } else {
                opaque = true;
            }
        }
        has_live_array_contents |=
            !allocations.is_empty() || !effects.reads.is_empty() || !effects.writes.is_empty();
        if !allocations.is_empty() {
            events.push((
                SourceUnitId(stmt.ordinal.0),
                stmt.ordinal,
                crate::StatementEffectSummary {
                    reads: BTreeSet::new(),
                    writes: allocations,
                },
                CellOrderBarrier::HeapAllocation,
            ));
        }
        if has_live_array_contents
            && (!effects.reads.is_empty() || !effects.writes.is_empty() || opaque)
        {
            events.push((
                SourceUnitId(stmt.ordinal.0),
                stmt.ordinal,
                effects,
                if opaque {
                    CellOrderBarrier::HeapFrontier
                } else {
                    CellOrderBarrier::None
                },
            ));
        }
    }
    let mut sequence_pairs: BTreeSet<_> = raw_edges
        .iter()
        .filter(|(_, _, reason)| reason.kind == super::DepKind::Sequenced)
        .map(|(from, to, _)| (*from, *to))
        .collect();
    emit_cell_order_events(events, raw_edges, &mut sequence_pairs);
}

fn array_content_cells(
    reads: &BTreeSet<EffectCell>,
    writes: &BTreeSet<EffectCell>,
) -> crate::StatementEffectSummary {
    crate::StatementEffectSummary {
        reads: reads
            .iter()
            .filter(|cell| matches!(cell, EffectCell::ArrayContents(_)))
            .cloned()
            .collect(),
        writes: writes
            .iter()
            .filter(|cell| matches!(cell, EffectCell::ArrayContents(_)))
            .cloned()
            .collect(),
    }
}

/// Resolve only chunk bindings initialized directly to a function/class
/// summary and never rebound or split by a shared `var` declaration.
/// Both at-init promotion and heap-content propagation use this boundary so
/// their call graph assumptions stay aligned.
fn resolve_direct_function_source_unit(
    binding: &Id,
    binding_source_unit: &HashMap<Id, SourceUnitId>,
    shared_vars: &BTreeSet<Id>,
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
    rebound: &BTreeSet<Id>,
) -> Option<SourceUnitId> {
    let source_unit = *binding_source_unit.get(binding)?;
    let stmt = stmt_by_source_unit.get(&source_unit)?;
    (stmt.declares_direct_function || stmt.constructor.is_some())
        .then_some(source_unit)
        .filter(|_| !rebound.contains(binding) && !shared_vars.contains(binding))
}

fn union_effects(
    into: &mut crate::StatementEffectSummary,
    from: &crate::StatementEffectSummary,
) -> bool {
    let old_reads = into.reads.len();
    let old_writes = into.writes.len();
    into.reads.extend(from.reads.iter().cloned());
    into.writes.extend(from.writes.iter().cloned());
    old_reads != into.reads.len() || old_writes != into.writes.len()
}

#[derive(Clone, Default)]
struct ArrayContentSummary {
    allocations: BTreeSet<EffectCell>,
    effects: crate::StatementEffectSummary,
    opaque: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum CellOrderBarrier {
    None,
    /// A fresh allocation seeds later heap observations without depending on
    /// prior heap effects or resetting an existing source-origin timeline.
    HeapAllocation,
    /// Preserve the S-chain behavior: an unsummarized impure statement
    /// depends on every preceding impure source unit.
    AllPriorEvents,
    /// Unknown heap effects depend on the current heap-cell frontier and
    /// become the barrier for later heap observations.
    HeapFrontier,
}

impl CellOrderBarrier {
    fn from_summarizable(summarizable: bool) -> Self {
        if summarizable {
            Self::None
        } else {
            Self::AllPriorEvents
        }
    }
}

/// Side-effect ordering edges (`S` per docs/design.md "Module dep graphs").
/// At source unit level, links impure top-level statements so any realizable
/// schedule preserves their observable order.
///
/// Two emission modes selected by [`DependencyGraphOptions`]:
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
///   every prior impure source unit. See `README.md` →
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
    options: DependencyGraphOptions,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) {
    if !options.dataflow_aware_s_chain {
        let mut prev: Option<SourceUnitId> = None;
        for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
            let from = SourceUnitId(stmt.ordinal.0);
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
    // - the most recent prior impure source unit that wrote any cell in
    //   `curr.reads ∪ curr.writes` (read-after-write /
    //   write-after-write), and
    // - every prior impure source unit that READ a cell `curr` writes
    //   since that cell's last write (write-after-read — without
    //   this, a later writer could be scheduled before an earlier
    //   reader and the reader would observe the new value).
    //
    // Statements with `dataflow_summarizable = false` are treated as
    // touching every cell — they get edges to every prior impure
    // source unit and become a barrier for subsequent statements.
    let mut sequence_pairs: BTreeSet<(SourceUnitId, SourceUnitId)> = raw_edges
        .iter()
        .filter(|(_, _, reason)| reason.kind == super::DepKind::Sequenced)
        .map(|(from, to, _)| (*from, *to))
        .collect();
    emit_cell_order_events(
        facts
            .iter()
            .filter(|stmt| !stmt.purity.is_pure())
            .map(|stmt| {
                (
                    SourceUnitId(stmt.ordinal.0),
                    stmt.ordinal,
                    stmt.effects(),
                    CellOrderBarrier::from_summarizable(stmt.dataflow_summarizable),
                )
            }),
        raw_edges,
        &mut sequence_pairs,
    );
}

/// Shared last-writer / reader-since-write timeline for binding, global, and
/// heap-content cells. Write-after-read dependencies preserve the value that
/// an earlier pure observer sees before a later mutation.
fn emit_cell_order_events(
    events: impl IntoIterator<
        Item = (
            SourceUnitId,
            SourceUnitOrdinal,
            crate::StatementEffectSummary,
            CellOrderBarrier,
        ),
    >,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
    sequence_pairs: &mut BTreeSet<(SourceUnitId, SourceUnitId)>,
) {
    let mut last_writer: BTreeMap<EffectCell, SourceUnitId> = BTreeMap::new();
    let mut readers_since_last_write: BTreeMap<EffectCell, BTreeSet<SourceUnitId>> =
        BTreeMap::new();
    let mut prior_source_units: Vec<SourceUnitId> = Vec::new();
    let mut opaque_barrier: Option<SourceUnitId> = None;
    let mut pending_allocations: BTreeMap<EffectCell, BTreeSet<SourceUnitId>> = BTreeMap::new();
    for (from, ordinal, effects, barrier_mode) in events {
        if barrier_mode == CellOrderBarrier::HeapAllocation {
            for cell in effects.writes {
                pending_allocations.entry(cell).or_default().insert(from);
            }
            continue;
        }
        let mut targets: BTreeSet<SourceUnitId> = BTreeSet::new();
        if barrier_mode != CellOrderBarrier::AllPriorEvents {
            for cell in effects.reads.iter().chain(effects.writes.iter()) {
                if let Some(allocations) = pending_allocations.get(cell) {
                    targets.extend(allocations.iter().copied());
                }
                if let Some(&to) = last_writer.get(cell) {
                    targets.insert(to);
                }
            }
            for cell in &effects.writes {
                if let Some(readers) = readers_since_last_write.get(cell) {
                    targets.extend(readers.iter().copied());
                }
            }
            // Unknown effects are barriers for subsequent summarized events.
            if let Some(barrier) = opaque_barrier {
                targets.insert(barrier);
            }
        }
        if barrier_mode == CellOrderBarrier::AllPriorEvents {
            // The S-chain's historical fallback: depend on every prior event
            // and become a barrier for later summarized events.
            targets.extend(prior_source_units.iter().copied());
            opaque_barrier = Some(from);
        } else if barrier_mode == CellOrderBarrier::HeapFrontier {
            // An opaque heap operation only needs the live cell frontier.
            // Every earlier read/write is either on that frontier or already
            // constrained before a frontier event. Reset the per-cell state
            // so later observations depend on the opaque operation itself.
            targets.extend(
                pending_allocations
                    .values()
                    .flat_map(|allocations| allocations.iter().copied()),
            );
            pending_allocations.clear();
            targets.extend(last_writer.values().copied());
            targets.extend(
                readers_since_last_write
                    .values()
                    .flat_map(|readers| readers.iter().copied()),
            );
            opaque_barrier = Some(from);
            last_writer.clear();
            readers_since_last_write.clear();
        }
        for to in targets {
            if from != to && sequence_pairs.insert((from, to)) {
                raw_edges.push((from, to, EdgeReason::sequenced(ordinal)));
            }
        }
        if barrier_mode == CellOrderBarrier::None {
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
        prior_source_units.push(from);
    }
}

/// Promote function-body lazy reads/rebinds to eager source unit edges from
/// every statement that at-init-calls the function. Transitive over
/// the call graph among chunk-declared functions: a top-level
/// `f()` whose `f` calls `g` in its body promotes through `g`'s lazy
/// reads/rebinds too. See docs/design.md "At-init call promotion".
///
/// Per-statement dedup: at most one promoted eager edge per
/// (caller, target-source unit) pair. Rebind targets are also promoted as
/// eager order edges; the write site's direct rebind edge enforces
/// write legality. Without dedup, a single at-init call to a
/// function with N transitive lazy reads would emit N edges from the
/// caller, and multiple at-init calls in the same statement would
/// multiply that further.
fn first_order_reads(stmt: &StatementFacts) -> &BTreeSet<Id> {
    stmt.constructor
        .as_ref()
        .map_or(&stmt.reads.first_order_lazy, |ctor| &ctor.reads)
}

fn first_order_rebinds(stmt: &StatementFacts) -> &BTreeSet<Id> {
    stmt.constructor
        .as_ref()
        .map_or(&stmt.rebinds.first_order_lazy, |ctor| &ctor.rebinds)
}

fn first_order_calls(stmt: &StatementFacts) -> &BTreeSet<Id> {
    stmt.constructor
        .as_ref()
        .map_or(&stmt.calls.first_order_lazy, |ctor| &ctor.calls)
}

fn first_order_unresolved_sources(stmt: &StatementFacts) -> &BTreeSet<Id> {
    stmt.constructor
        .as_ref()
        .map_or(&stmt.first_order_unresolved_sources, |ctor| {
            &ctor.unresolved_sources
        })
}

fn first_order_unresolved_inline_fn(stmt: &StatementFacts) -> bool {
    stmt.constructor
        .as_ref()
        .map_or(stmt.first_order_unresolved_inline_fn, |ctor| {
            ctor.unresolved_inline_fn
        })
}

fn promote_at_init_calls(
    facts: &[StatementFacts],
    binding_source_unit: &HashMap<Id, SourceUnitId>,
    shared_vars: &BTreeSet<Id>,
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) {
    // Index statement source units that can change a chunk binding's value or
    // observable object state when they execute. A callback read must see the
    // source-prefix value, so the callback-read and writer source units form one
    // source-order timeline. This also covers mutations of an already
    // initialized object: an ordinary promoted read dependency on its
    // declaration alone would not stop a later mutation from moving before
    // the callback. Unknown effects still take the existing strict S-chain
    // fallback; this index only records effects with a known binding target.
    let mut writers_by_binding = BTreeMap::<Id, BTreeSet<SourceUnitId>>::new();
    let mut callback_readers_by_binding = BTreeMap::<Id, BTreeSet<SourceUnitId>>::new();
    let mut eager_readers_by_binding = BTreeMap::<Id, BTreeSet<SourceUnitId>>::new();
    for stmt in facts {
        let source_unit = SourceUnitId(stmt.ordinal.0);
        for binding in &stmt.reads.eager {
            eager_readers_by_binding
                .entry(binding.clone())
                .or_default()
                .insert(source_unit);
        }
        // Lexical declarations become observable at their statement. Function
        // declarations are initialized during module instantiation and are
        // therefore not execution-time writes.
        let is_hoisted_function = stmt.kind == StatementKind::FnDecl
            || (stmt.kind == StatementKind::Export && stmt.declares_direct_function);
        // Imports describe module linkage; evaluating the import declaration
        // does not write the imported binding in this chunk. Keep this guard
        // explicit even though `collect_declared_names` currently gives
        // imports no local declarations.
        if stmt.kind != StatementKind::Import && !is_hoisted_function {
            for binding in stmt.declared.difference(&stmt.var_declared) {
                writers_by_binding
                    .entry(binding.clone())
                    .or_default()
                    .insert(source_unit);
            }
        }
        for binding in stmt
            .var_initializers
            .iter()
            .chain(&stmt.rebinds.eager)
            .chain(&stmt.local_effects)
        {
            writers_by_binding
                .entry(binding.clone())
                .or_default()
                .insert(source_unit);
        }
        // A summary's rebinds execute at this statement's callsite, not at
        // the lexical source unit containing the callback body.
        if let Some(synchronous) = &stmt.synchronous_calls {
            for (binding, _) in &synchronous.reads {
                callback_readers_by_binding
                    .entry(binding.clone())
                    .or_default()
                    .insert(source_unit);
            }
            // Keep the graph sound even if an availability-specific read is
            // not duplicated in the general read set.
            for binding in &synchronous.uninitialized_reads {
                callback_readers_by_binding
                    .entry(binding.clone())
                    .or_default()
                    .insert(source_unit);
            }
            for (binding, _) in &synchronous.rebinds {
                writers_by_binding
                    .entry(binding.clone())
                    .or_default()
                    .insert(source_unit);
            }
        }
    }

    // A pair may be observed by several callback callsites or be both a
    // future declaration and a future writer. Emit each temporal ordering
    // constraint only once within this pass.
    let mut temporal_sequence_pairs = BTreeSet::<(SourceUnitId, SourceUnitId)>::new();
    let mut existing_order_pairs: BTreeSet<(SourceUnitId, SourceUnitId)> = raw_edges
        .iter()
        .filter(|(_, _, reason)| reason.constrains_init_order())
        .map(|(from, to, _)| (*from, *to))
        .collect();
    let mut direct_eager_bindings: BTreeSet<(SourceUnitId, SourceUnitId, Id)> = raw_edges
        .iter()
        .filter_map(|(from, to, reason)| {
            (reason.kind == super::DepKind::EagerUse && reason.role == EdgeRole::Direct)
                .then(|| {
                    reason
                        .binding
                        .as_ref()
                        .map(|binding| (*from, *to, binding.clone()))
                })
                .flatten()
        })
        .collect();

    // Preserve each observed binding's writer order and place each reader
    // against only its nearest preceding/following writer. Ordinary eager
    // readers need this timeline even when no callback reads the binding:
    // a pure initializer may otherwise move across a later assignment.
    // Do not add reader-to-reader edges: independent observations of an
    // unchanged cell need no relative order and can form false cycles.
    for (binding, writers) in &writers_by_binding {
        let mut readers: BTreeSet<SourceUnitId> = callback_readers_by_binding
            .get(binding)
            .into_iter()
            .flat_map(|readers| readers.iter().copied())
            .collect();
        if let Some(eager_readers) = eager_readers_by_binding.get(binding) {
            readers.extend(eager_readers.iter().copied());
        }
        if readers.is_empty() {
            continue;
        }
        let writer_events: Vec<_> = writers.iter().copied().collect();
        for pair in writer_events.windows(2) {
            let earlier = pair[0];
            let later = pair[1];
            if existing_order_pairs.insert((later, earlier)) {
                temporal_sequence_pairs.insert((later, earlier));
                raw_edges.push((
                    later,
                    earlier,
                    EdgeReason::sequenced(SourceUnitOrdinal(later.0)),
                ));
            }
        }

        for reader in readers {
            if let Some(&previous_writer) = writers.range(..reader).next_back()
                && existing_order_pairs.insert((reader, previous_writer))
            {
                temporal_sequence_pairs.insert((reader, previous_writer));
                raw_edges.push((
                    reader,
                    previous_writer,
                    EdgeReason::sequenced(SourceUnitOrdinal(reader.0)),
                ));
            }
            if let Some(&next_writer) = writers
                .range((
                    std::ops::Bound::Excluded(reader),
                    std::ops::Bound::Unbounded,
                ))
                .next()
                && existing_order_pairs.insert((next_writer, reader))
            {
                temporal_sequence_pairs.insert((next_writer, reader));
                raw_edges.push((
                    next_writer,
                    reader,
                    EdgeReason::sequenced(SourceUnitOrdinal(next_writer.0)),
                ));
            }
        }
    }

    // Bindings rebound anywhere in the chunk: their value at call
    // time may not be the function lexically defined at their source unit,
    // so calls to them are not precisely resolvable.
    let rebound: BTreeSet<Id> = facts
        .iter()
        .flat_map(|s| s.rebinds.eager.iter().chain(s.rebinds.lazy.iter()).cloned())
        .collect();

    // A callee binding is precisely resolvable iff (a) its declaring
    // statement binds it directly to a function value (`function f`,
    // `const f = () => ...`) and (b) it is never rebound. Everything
    // else — aliases (`const g = readB`), object-literal methods,
    // conditional initializers, rebound functions — takes the
    // conservative read-closure fallback below.
    let resolvable_callee = |id: &Id| {
        resolve_direct_function_source_unit(
            id,
            binding_source_unit,
            shared_vars,
            stmt_by_source_unit,
            &rebound,
        )
    };

    // 1. Build the call graph: source unit → source unit edges for each
    //    resolvable callee reachable via *first-order* calls.lazy.
    //    Nested-closure calls (e.g. inside an arrow returned by the
    //    body) don't fire when the body is invoked synchronously, so
    //    they don't belong on the promotion call graph — see
    //    docs/design.md "At-init call promotion" and the e2e test
    //    `at_init_promotion_nested_closure_test`.
    //
    //    Add every source unit whose body has any first-order lazy reads /
    //    rebinds / calls — or an unresolvable first-order callee — as
    //    a node, so the closure pass below covers it even if it makes
    //    no resolvable calls (e.g. `function readB() { return B; }`).
    //
    //    Per-source unit "fallback roots": source units through which a function
    //    value could reach a call the source unit's first-order body makes
    //    but promotion can't follow — the source units of bindings the
    //    unresolved call mentions, the source units of chunk-declared
    //    Ident callees that aren't direct never-rebound functions,
    //    and the source unit itself when the unresolved call carries an
    //    inline function expression. At-init callers inherit these
    //    roots transitively through the call graph and expand them
    //    via [`UnresolvedCallFallback`].
    let fallback_roots_of = |sources: &BTreeSet<Id>,
                             inline_fn: bool,
                             self_source_unit: SourceUnitId|
     -> BTreeSet<SourceUnitId> {
        let mut roots: BTreeSet<SourceUnitId> = sources
            .iter()
            .filter_map(|id| binding_source_unit.get(id).copied())
            .collect();
        if inline_fn {
            roots.insert(self_source_unit);
        }
        roots
    };
    let source_unit_first_order_roots = |stmt: &StatementFacts| -> BTreeSet<SourceUnitId> {
        let source_unit = SourceUnitId(stmt.ordinal.0);
        let mut roots = fallback_roots_of(
            first_order_unresolved_sources(stmt),
            first_order_unresolved_inline_fn(stmt),
            source_unit,
        );
        for callee_id in first_order_calls(stmt) {
            if resolvable_callee(callee_id).is_none()
                && let Some(&callee_source_unit) = binding_source_unit.get(callee_id)
            {
                roots.insert(callee_source_unit);
            }
        }
        roots
    };
    let mut call_graph: DiGraphMap<SourceUnitId, ()> = DiGraphMap::new();
    for stmt in facts {
        let source_unit = SourceUnitId(stmt.ordinal.0);
        if !first_order_calls(stmt).is_empty()
            || !first_order_reads(stmt).is_empty()
            || !first_order_rebinds(stmt).is_empty()
            || !first_order_unresolved_sources(stmt).is_empty()
            || first_order_unresolved_inline_fn(stmt)
        {
            call_graph.add_node(source_unit);
        }
    }
    for stmt in facts {
        if first_order_calls(stmt).is_empty() {
            continue;
        }
        let caller = SourceUnitId(stmt.ordinal.0);
        for callee_id in first_order_calls(stmt) {
            let Some(callee_source_unit) = resolvable_callee(callee_id) else {
                continue;
            };
            call_graph.add_node(callee_source_unit);
            call_graph.add_edge(caller, callee_source_unit, ());
        }
    }

    // 2. Tarjan SCC. `tarjan_scc` returns SCCs in reverse topological
    //    order: leaves (no outgoing edges to other SCCs) first.
    let sccs = tarjan_scc(&call_graph);
    let mut scc_of: BTreeMap<SourceUnitId, usize> = BTreeMap::new();
    for (idx, scc) in sccs.iter().enumerate() {
        for source_unit in scc {
            scc_of.insert(*source_unit, idx);
        }
    }

    // 3. Per-source unit seeds: own reads.lazy / rebinds.lazy resolved to
    //    SWC Ids. Filters out targets whose source unit is a function
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
        let Some(target_source_unit) = binding_source_unit.get(id) else {
            return false;
        };
        stmt_by_source_unit
            .get(target_source_unit)
            .map(|stmt| stmt.kind == StatementKind::FnDecl)
            .unwrap_or(false)
    };
    let mut scc_reads: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
    let mut scc_rebinds: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
    let mut scc_fallback_roots: Vec<BTreeSet<SourceUnitId>> = vec![BTreeSet::new(); sccs.len()];
    // A first-order call through a parameter/local cannot be mapped to a
    // chunk-top source unit here. The value may have come from an argument at the
    // eventual at-init call site, so carry that obligation through the call
    // graph instead of silently dropping the Id in fallback_roots_of.
    let mut scc_needs_caller_values = vec![false; sccs.len()];

    // 4. Closure over the call graph. Iterate SCCs in
    //    reverse-topological order (leaves first). For each SCC,
    //    union members' own seeds plus successor SCC closures, and
    //    propagate fallback roots the same way.
    for (scc_idx, scc) in sccs.iter().enumerate() {
        let mut reads: BTreeSet<Id> = BTreeSet::new();
        let mut rebinds: BTreeSet<Id> = BTreeSet::new();
        let mut roots: BTreeSet<SourceUnitId> = BTreeSet::new();
        let mut needs_caller_values = false;
        for source_unit in scc {
            let Some(stmt) = stmt_by_source_unit.get(source_unit) else {
                continue;
            };
            roots.extend(source_unit_first_order_roots(stmt));
            needs_caller_values |= first_order_unresolved_sources(stmt)
                .iter()
                .chain(first_order_calls(stmt).iter())
                .any(|id| {
                    stmt.local_bindings.contains(id) && !binding_source_unit.contains_key(id)
                });
            for id in first_order_reads(stmt) {
                if binding_source_unit.contains_key(id) && !target_is_hoisted(id) {
                    reads.insert(id.clone());
                }
            }
            for id in first_order_rebinds(stmt) {
                if binding_source_unit.contains_key(id) {
                    rebinds.insert(id.clone());
                }
            }
        }
        for source_unit in scc {
            for (_, target, _) in call_graph.edges(*source_unit) {
                let Some(&target_scc) = scc_of.get(&target) else {
                    continue;
                };
                if target_scc == scc_idx {
                    continue;
                }
                reads.extend(scc_reads[target_scc].iter().cloned());
                rebinds.extend(scc_rebinds[target_scc].iter().cloned());
                roots.extend(scc_fallback_roots[target_scc].iter().copied());
                needs_caller_values |= scc_needs_caller_values[target_scc];
            }
        }
        scc_reads[scc_idx] = reads;
        scc_rebinds[scc_idx] = rebinds;
        scc_fallback_roots[scc_idx] = roots;
        scc_needs_caller_values[scc_idx] = needs_caller_values;
    }

    // 5. Emit promoted edges with per-statement, per-kind dedup.
    //    Each emitted reason carries `EdgeRole::PromotedAtInit {
    //    callee_source_unit }` so the realizability gate can drop the edge
    //    when caller and callee land in different assignment slots —
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
        if let Some(synchronous) = &stmt.synchronous_calls {
            let caller = SourceUnitId(stmt.ordinal.0);
            let mut emitted = BTreeSet::new();

            // A read known to observe a binding before its source
            // initialization is not a safely-droppable call promotion: moving
            // the declaration ahead of this callback would erase a TDZ or
            // change the initial `undefined` value. Keep a direct dependency
            // on the declaration, and when that declaration is later in the
            // source, pair it with reverse source order. EagerUse alone would
            // merely ask the later provider to run first; the pair makes the
            // two source units atomic so the emitted module retains original order.
            for target_binding in &synchronous.uninitialized_reads {
                let Some(&target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                if caller == target_source_unit {
                    continue;
                }
                if direct_eager_bindings.insert((
                    caller,
                    target_source_unit,
                    target_binding.clone(),
                )) {
                    raw_edges.push((
                        caller,
                        target_source_unit,
                        EdgeReason::eager_use(stmt.ordinal, target_binding.clone()),
                    ));
                }
                if caller < target_source_unit
                    && temporal_sequence_pairs.insert((target_source_unit, caller))
                {
                    raw_edges.push((
                        target_source_unit,
                        caller,
                        EdgeReason::sequenced(SourceUnitOrdinal(target_source_unit.0)),
                    ));
                }
            }

            for (target_binding, callee_ordinal) in synchronous
                .reads
                .iter()
                .filter(|(binding, _)| !synchronous.uninitialized_reads.contains(binding))
                .chain(&synchronous.rebinds)
            {
                if target_is_hoisted(target_binding) {
                    continue;
                }
                let Some(&target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                let callee_source_unit = SourceUnitId(callee_ordinal.0);
                if caller == target_source_unit
                    || !emitted.insert((target_source_unit, callee_source_unit))
                {
                    continue;
                }
                raw_edges.push((
                    caller,
                    target_source_unit,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(callee_source_unit),
                ));
            }
            if synchronous.complete {
                continue;
            }
        }
        if stmt.calls.eager.is_empty()
            && stmt.at_init_unresolved_sources.is_empty()
            && !stmt.at_init_unresolved_inline_fn
        {
            continue;
        }
        let caller = SourceUnitId(stmt.ordinal.0);
        let mut promoted_read_targets: BTreeSet<SourceUnitId> = BTreeSet::new();
        let mut fallback_roots = fallback_roots_of(&stmt.at_init_unresolved_sources, false, caller);
        if stmt.at_init_unresolved_inline_fn
            && let Some(&scc_idx) = scc_of.get(&caller)
        {
            fallback_roots.extend(scc_fallback_roots[scc_idx].iter().copied());
            for target_binding in &scc_reads[scc_idx] {
                let Some(target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                if caller == *target_source_unit {
                    continue;
                }
                if !promoted_read_targets.insert(*target_source_unit) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_source_unit,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(caller),
                ));
            }
            for target_binding in &scc_rebinds[scc_idx] {
                let Some(target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                if caller == *target_source_unit {
                    continue;
                }
                if !promoted_read_targets.insert(*target_source_unit) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_source_unit,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(caller),
                ));
            }
        }
        for callee_id in &stmt.calls.eager {
            let Some(callee_source_unit) = resolvable_callee(callee_id) else {
                // A bare-Ident callee that isn't chunk-declared is a
                // global/import — out of single-chunk analysis scope
                // (documented precondition in docs/design.md). A
                // chunk-declared but unresolvable callee falls back
                // through the read closure of its own source unit.
                if let Some(&callee_source_unit) = binding_source_unit.get(callee_id) {
                    fallback_roots.insert(callee_source_unit);
                }
                continue;
            };
            let Some(&scc_idx) = scc_of.get(&callee_source_unit) else {
                continue;
            };
            fallback_roots.extend(scc_fallback_roots[scc_idx].iter().copied());
            if scc_needs_caller_values[scc_idx] {
                // Include the caller itself (inline callback bodies and
                // local aliases) and every chunk binding it reads (argument
                // values). UnresolvedCallFallback then closes over their
                // lazy bodies. This is deliberately conservative until a
                // parameter's specific value can be proven.
                fallback_roots.insert(caller);
                fallback_roots.extend(fallback_roots_of(&stmt.reads.eager, false, caller));
            }
            for target_binding in &scc_reads[scc_idx] {
                let Some(target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                if caller == *target_source_unit {
                    continue;
                }
                if !promoted_read_targets.insert(*target_source_unit) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_source_unit,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(callee_source_unit),
                ));
            }
            for target_binding in &scc_rebinds[scc_idx] {
                let Some(target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                if caller == *target_source_unit {
                    continue;
                }
                if !promoted_read_targets.insert(*target_source_unit) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_source_unit,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(callee_source_unit),
                ));
            }
        }
        if fallback_roots.is_empty() {
            continue;
        }
        let fallback = fallback.get_or_insert_with(|| UnresolvedCallFallback::build(facts));
        for root in fallback_roots {
            let (closure_reads, closure_rebinds) = fallback.closures_for(SourceUnitOrdinal(root.0));
            // Rebind targets get an *order* constraint only (EagerUse,
            // not EagerRebind): assignment to a TDZ-locked binding
            // throws until its statement runs, so the target must
            // evaluate before the caller — but write LEGALITY
            // (read-only imports) is already enforced by the direct
            // `LazyRebind` / `DeferredRebind` edge from the statement
            // lexically containing the write, which forces the write
            // site to co-locate with the declarer. Emitting
            // `EagerRebind` here would force the *caller* into the
            // co-location group too, rejecting realizable shapes
            // (`c.bump(1)` calling a co-located setter).
            for target_binding in closure_reads.iter().chain(closure_rebinds.iter()) {
                if target_is_hoisted(target_binding) {
                    continue;
                }
                let Some(target_source_unit) = binding_source_unit.get(target_binding) else {
                    continue;
                };
                if caller == *target_source_unit
                    || !promoted_read_targets.insert(*target_source_unit)
                {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_source_unit,
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
/// call. Each such "root" source unit is expanded to the **full lazy
/// closure** of every source unit reachable from it through the chunk's
/// read graph: edges `O → source unit(b)` for every chunk binding `b` the
/// source unit `O` reads (eagerly or lazily), with every reachable source unit
/// contributing its `reads.lazy` / `rebinds.lazy` at any nesting
/// depth. The caller statement then eagerly depends on every
/// collected target.
///
/// Documented residual preconditions (see docs/design.md "At-init
/// call promotion" → Limitations): values from other chunks and dynamic
/// global/property stashes may not resolve to a single callable. Local
/// assignments, parameters, returns and static object properties are
/// followed by the shared chunk value-flow pass before this fallback.
///
/// Fallback-only edges carry `EdgeRole::PromotedAtInit` with
/// `callee_source_unit = caller`, so neither projection view ever drops
/// them — there is no precisely-known callee module whose evaluation
/// could make the constraint redundant.
struct UnresolvedCallFallback {
    closure: SourceUnitReadClosure,
}

impl UnresolvedCallFallback {
    fn build(facts: &[StatementFacts]) -> Self {
        Self {
            closure: SourceUnitReadClosure::new(facts),
        }
    }

    fn closures_for(&self, source_unit: SourceUnitOrdinal) -> (&BTreeSet<Id>, &BTreeSet<Id>) {
        self.closure.lazy_effects_for_source_unit(source_unit)
    }
}

#[cfg(test)]
mod array_heap_frontier_tests {
    use super::*;

    fn heap_event(
        ordinal: usize,
        reads: &[u32],
        writes: &[u32],
        barrier: CellOrderBarrier,
    ) -> (
        SourceUnitId,
        SourceUnitOrdinal,
        crate::StatementEffectSummary,
        CellOrderBarrier,
    ) {
        (
            SourceUnitId(ordinal),
            SourceUnitOrdinal(ordinal),
            crate::StatementEffectSummary {
                reads: reads
                    .iter()
                    .copied()
                    .map(EffectCell::ArrayContents)
                    .collect(),
                writes: writes
                    .iter()
                    .copied()
                    .map(EffectCell::ArrayContents)
                    .collect(),
            },
            barrier,
        )
    }

    #[test]
    fn fresh_allocation_is_pending_without_reading_prior_opaque_effects() {
        let events = [
            heap_event(0, &[], &[], CellOrderBarrier::HeapFrontier),
            heap_event(1, &[], &[10], CellOrderBarrier::HeapAllocation),
            heap_event(2, &[], &[], CellOrderBarrier::HeapFrontier),
        ];
        let mut edges = Vec::new();
        let mut pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut pairs);
        assert!(!pairs.contains(&(SourceUnitId(1), SourceUnitId(0))));
        assert!(pairs.contains(&(SourceUnitId(2), SourceUnitId(1))));
        assert!(pairs.contains(&(SourceUnitId(2), SourceUnitId(0))));
    }

    #[test]
    fn repeated_source_origin_allocation_preserves_content_writer_and_readers() {
        let events = [
            heap_event(0, &[], &[10], CellOrderBarrier::HeapAllocation),
            heap_event(1, &[], &[10], CellOrderBarrier::None),
            heap_event(2, &[10], &[], CellOrderBarrier::None),
            heap_event(3, &[], &[10], CellOrderBarrier::HeapAllocation),
            heap_event(4, &[10], &[], CellOrderBarrier::None),
            heap_event(5, &[], &[10], CellOrderBarrier::None),
        ];
        let mut edges = Vec::new();
        let mut pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut pairs);
        assert!(pairs.contains(&(SourceUnitId(4), SourceUnitId(1))));
        assert!(pairs.contains(&(SourceUnitId(4), SourceUnitId(3))));
        assert!(pairs.contains(&(SourceUnitId(5), SourceUnitId(2))));
        assert!(pairs.contains(&(SourceUnitId(5), SourceUnitId(4))));
    }

    #[test]
    fn allocation_does_not_exempt_same_statement_opaque_execution() {
        let events = [
            heap_event(0, &[], &[10], CellOrderBarrier::HeapAllocation),
            heap_event(1, &[], &[], CellOrderBarrier::HeapFrontier),
            heap_event(2, &[], &[20], CellOrderBarrier::HeapAllocation),
            heap_event(2, &[], &[], CellOrderBarrier::HeapFrontier),
            heap_event(3, &[10], &[], CellOrderBarrier::None),
        ];
        let mut edges = Vec::new();
        let mut pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut pairs);
        assert!(pairs.contains(&(SourceUnitId(1), SourceUnitId(0))));
        assert!(pairs.contains(&(SourceUnitId(2), SourceUnitId(1))));
        assert!(pairs.contains(&(SourceUnitId(3), SourceUnitId(2))));
    }

    #[test]
    fn repeated_opaque_heap_barriers_keep_edges_linear_in_the_live_frontier() {
        const ARRAY_COUNT: usize = 1024;
        const BARRIER_COUNT: usize = 16;

        let mut events = Vec::new();
        for array in 0..ARRAY_COUNT {
            let mut effects = crate::StatementEffectSummary::default();
            effects
                .reads
                .insert(EffectCell::ArrayContents(array as u32));
            events.push((
                SourceUnitId(array),
                SourceUnitOrdinal(array),
                effects,
                CellOrderBarrier::None,
            ));
        }
        for barrier in 0..BARRIER_COUNT {
            let source_unit = ARRAY_COUNT + barrier;
            events.push((
                SourceUnitId(source_unit),
                SourceUnitOrdinal(source_unit),
                crate::StatementEffectSummary::default(),
                CellOrderBarrier::HeapFrontier,
            ));
        }
        let last_barrier = ARRAY_COUNT + BARRIER_COUNT - 1;
        for array in 0..ARRAY_COUNT {
            let source_unit = ARRAY_COUNT + BARRIER_COUNT + array;
            let mut effects = crate::StatementEffectSummary::default();
            effects
                .reads
                .insert(EffectCell::ArrayContents(array as u32));
            events.push((
                SourceUnitId(source_unit),
                SourceUnitOrdinal(source_unit),
                effects,
                CellOrderBarrier::None,
            ));
        }

        let mut edges = Vec::new();
        let mut sequence_pairs = BTreeSet::new();
        emit_cell_order_events(events, &mut edges, &mut sequence_pairs);

        // First barrier follows the 1,024 live readers. The following 15
        // barriers form a chain, and the final readers follow the last one.
        // Retaining the old per-array reader frontier would add 15 * 1,024
        // redundant edges between earlier readers and later barriers.
        assert_eq!(edges.len(), 2 * ARRAY_COUNT + BARRIER_COUNT - 1);
        for array in 0..ARRAY_COUNT {
            assert!(sequence_pairs.contains(&(SourceUnitId(ARRAY_COUNT), SourceUnitId(array),)));
            assert!(sequence_pairs.contains(&(
                SourceUnitId(ARRAY_COUNT + BARRIER_COUNT + array),
                SourceUnitId(last_barrier),
            )));
        }
        for barrier in 1..BARRIER_COUNT {
            assert!(sequence_pairs.contains(&(
                SourceUnitId(ARRAY_COUNT + barrier),
                SourceUnitId(ARRAY_COUNT + barrier - 1),
            )));
        }
    }
}
