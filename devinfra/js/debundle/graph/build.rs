use analysis_model::DepKind;
use source_identity::{SourceUnitId, SourceUnitOrdinal};
use std::collections::{BTreeMap, BTreeSet, HashMap};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};
use swc_ecma_ast::Id;

use analysis::facts::{
    CellOverlap, CellOverlapIndex, EffectCell, HeapContentCells, SourceUnitReadClosure,
    first_overlapping, may_conflict, may_overlap,
};
use analysis::reports::spec_lint::{
    CommutingStatementsUnused, UnusedCommutingEndpoint, UnusedPremiseReason,
};
use analysis::{StatementEffectSummary, StatementFacts, StatementKind};
use analysis_vocabulary::execution_evidence::SourceLocation;
use analysis_vocabulary::inputs::{CommutingEndpoint, ResolvedCommutingStatements};
use analysis_vocabulary::report_schema::EffectCellReport;

mod at_init;
mod cell_order;
mod commuting;
mod diagnostics;

use at_init::*;
use cell_order::*;
pub use commuting::*;
pub use diagnostics::*;

/// The per-chunk options dependency-graph construction reads.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct GraphOptions {
    /// `chunk_analysis_options.<chunk>.dataflow_aware_s_chain`.
    pub dataflow_aware_s_chain: bool,
}

use super::dependency_graph::{AppliedCommutingPremise, DependencyGraph, SourceUnit};
use super::edge::{
    BindingTimelineOrder, CellConflict, CellEffectClass, CellOverlapReport, DependencyEdge,
    DependencyEdgeId, EdgeReason, EdgeRole, EffectProvenance, HeapBarrierCause, SequencedCause,
};
use analysis::facts::DataflowOpacityCause;
use analysis_vocabulary::execution_evidence::AssumedEffectReason;

const HEAP_CELL_SAMPLE_LIMIT: usize = 8;

fn report_heap_frontier(
    own_at_init: &HeapContentCells,
    composed_synchronous_calls: &HeapContentCells,
    non_assumed_reads: &BTreeSet<EffectCell>,
    non_assumed_writes: &BTreeSet<EffectCell>,
) -> Option<analysis_vocabulary::report_schema::HeapFrontierEvidence> {
    use analysis_vocabulary::report_schema::{
        HeapCellSetReport, HeapContentSummaryReport, HeapFrontierEvidence,
        NonAssumedHeapAccessReport,
    };

    let has_content = |cells: &HeapContentCells| {
        cells.opaque
            || !cells.allocations.is_empty()
            || !cells.reads.is_empty()
            || !cells.writes.is_empty()
    };
    if !has_content(own_at_init)
        && !has_content(composed_synchronous_calls)
        && non_assumed_reads.is_empty()
        && non_assumed_writes.is_empty()
    {
        return None;
    }

    let report_cells = |cells: &BTreeSet<EffectCell>| HeapCellSetReport {
        count: cells.len(),
        sample: cells
            .iter()
            .take(HEAP_CELL_SAMPLE_LIMIT)
            .map(EffectCellReport::from)
            .collect(),
    };
    let report_content = |cells: &HeapContentCells| HeapContentSummaryReport {
        opaque: cells.opaque,
        allocations: report_cells(&cells.allocations),
        reads: report_cells(&cells.reads),
        writes: report_cells(&cells.writes),
    };

    Some(HeapFrontierEvidence {
        own_at_init: report_content(own_at_init),
        composed_synchronous_calls: report_content(composed_synchronous_calls),
        non_assumed_heap_accesses: NonAssumedHeapAccessReport {
            reads: report_cells(non_assumed_reads),
            writes: report_cells(non_assumed_writes),
        },
    })
}

/// Build the fine dependency graph from per-statement facts under the
/// per-chunk [`GraphOptions`]. Module-level dependencies are derived later
/// by projecting edges through a [`ModuleAssignment`].
///
/// `commuting_statements` are the chunk's resolved author premises; each is
/// applied by [`apply_commuting_statements`] after every edge exists, so the
/// emitted graph still records what source analysis found and marks the
/// edges a premise made inert. Entries that inert nothing are returned beside
/// the graph as warnings.
///
/// [`ModuleAssignment`]: crate::module_assignment::ModuleAssignment
pub fn build_dependency_graph_with(
    facts: &[StatementFacts],
    options: GraphOptions,
    commuting_statements: &[ResolvedCommutingStatements],
) -> Result<(DependencyGraph, Vec<CommutingStatementsUnused>), DependencyGraphBuildError> {
    let mut declaration_sites = BTreeMap::<Id, Vec<SourceUnitId>>::new();
    let mut nodes = Vec::<SourceUnit>::with_capacity(facts.len());
    for stmt in facts {
        for binding in &stmt.declared {
            declaration_sites
                .entry(binding.clone())
                .or_default()
                .push(SourceUnitId(stmt.ordinal.0));
        }
        nodes.push(source_unit_node(stmt));
    }

    // Keep every declaration site. A representative is only an address for a
    // binding; the co-declaration edges below make all sites inseparable.
    let binding_source_unit: HashMap<Id, SourceUnitId> = declaration_sites
        .iter()
        .map(|(binding, sites)| (binding.clone(), sites[0]))
        .collect();
    let shared_vars = shared_var_bindings(facts, &declaration_sites)?;

    // Collect (from, to, reason) triples; the final `edges` Vec is
    // sorted at the end so `DependencyEdgeId` indices are stable.
    let mut raw_edges = Vec::<(SourceUnitId, SourceUnitId, EdgeReason)>::new();
    emit_co_location_edges(facts, &declaration_sites, &shared_vars, &mut raw_edges);
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
    emit_direct_binding_edges(
        facts,
        &binding_source_unit,
        target_is_hoisted,
        &mut raw_edges,
    );

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
    let fallback_eager_uses = promote_at_init_calls(
        facts,
        &binding_source_unit,
        &shared_vars,
        &stmt_by_source_unit,
        target_is_hoisted,
        &mut raw_edges,
    );
    emit_shared_var_access_order(facts, &shared_vars, &fallback_eager_uses, &mut raw_edges);

    emit_s_chain(facts, options, &mut raw_edges);
    emit_heap_content_order(facts, &mut raw_edges);

    let mut edges = sorted_dependency_edges(raw_edges);
    let premise_effects = premise_effects(&stmt_by_source_unit);
    retain_premise_verdicts(&mut edges, &stmt_by_source_unit, &premise_effects);
    let analyzed_edge_count = edges.len();
    let (commuting_premises, unused_commuting_premises) =
        apply_commuting_statements(&nodes, &mut edges, commuting_statements, &premise_effects)?;
    retain_premise_verdicts(
        &mut edges[analyzed_edge_count..],
        &stmt_by_source_unit,
        &premise_effects,
    );
    Ok((
        DependencyGraph::from_parts(nodes, edges, commuting_premises),
        unused_commuting_premises,
    ))
}

/// The graph node for one top-level statement, carrying its report-facing facts.
fn source_unit_node(stmt: &StatementFacts) -> SourceUnit {
    SourceUnit {
        id: SourceUnitId(stmt.ordinal.0),
        statement_ordinal: stmt.ordinal,
        source_location: stmt.source_location.clone(),
        declared: stmt.declared.clone(),
        kind: stmt.kind,
        purity: stmt.purity.clone(),
        callback_factory_barriers: stmt.callback_factory_barriers.clone(),
        heap_native_integrity_uses: stmt.heap_native_integrity_uses.clone(),
        synchronous_execution: stmt.synchronous_calls.as_ref().map(|calls| {
            analysis_vocabulary::report_schema::SynchronousExecutionReport {
                initializer_binding_effects: calls.initializer_binding_effects.clone(),
                initializer_external_effects: calls.initializer_external_effects.clone(),
                purity_disagreement: calls.purity_disagreement.clone(),
                binding_effect_authority: Some(calls.binding_effect_authority_report()),
                local_record_factory_uses: calls.local_record_factory_uses.clone(),
                local_record_factory_rejections: calls.local_record_factory_rejections.clone(),
                complete: calls.complete,
                native_integrity_checks: calls.native_integrity_checks.clone(),
                native_prototype_property_checks: calls.native_prototype_property_checks.clone(),
                native_prefix_observations: calls.native_prefix_observations.clone(),
                native_integrity_uses: calls.native_integrity_uses.clone(),
                native_operation_input_uses: calls.native_operation_input_uses.clone(),
                imported_primitive_array_spread_input_uses: calls
                    .imported_primitive_array_spread_input_uses
                    .clone(),
                inference_uses: calls
                    .inference_uses
                    .iter()
                    .map(analysis_vocabulary::report_schema::InferenceUseReport::from)
                    .collect(),
                precision_losses: calls.precision_losses.clone(),
                callback_factory_uses: Vec::new(),
                returned_record_uses: Vec::new(),
                author_host_contract_uses: calls.author_host_contract_uses.clone(),
                heap_frontier: report_heap_frontier(
                    &stmt.heap_contents,
                    &calls.heap_contents,
                    &calls.non_assumed_heap_accesses.reads,
                    &calls.non_assumed_heap_accesses.writes,
                ),
            }
        }),
        callback_factory_uses: stmt
            .synchronous_calls
            .as_ref()
            .map_or_else(Vec::new, |calls| calls.callback_factory_uses.clone()),
        returned_record_uses: stmt
            .synchronous_calls
            .as_ref()
            .map_or_else(Vec::new, |calls| calls.returned_record_uses.clone()),
    }
}

/// Bindings declared at more than one site. Every site must declare `var`.
fn shared_var_bindings(
    facts: &[StatementFacts],
    declaration_sites: &BTreeMap<Id, Vec<SourceUnitId>>,
) -> Result<BTreeSet<Id>, DependencyGraphBuildError> {
    let mut shared_vars = BTreeSet::new();
    for (binding, sites) in declaration_sites {
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
    Ok(shared_vars)
}

/// Two-way edges that keep co-declared `var` sites and certified stable
/// spread producers with their consumers in one module.
fn emit_co_location_edges(
    facts: &[StatementFacts],
    declaration_sites: &BTreeMap<Id, Vec<SourceUnitId>>,
    shared_vars: &BTreeSet<Id>,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) {
    for binding in shared_vars {
        let sites = &declaration_sites[binding];
        for &source_unit in &sites[1..] {
            raw_edges.push((
                source_unit,
                sites[0],
                EdgeReason::for_binding(
                    DepKind::CoDeclaration,
                    SourceUnitOrdinal(source_unit.0),
                    binding.clone(),
                ),
            ));
            raw_edges.push((
                sites[0],
                source_unit,
                EdgeReason::for_binding(
                    DepKind::CoDeclaration,
                    SourceUnitOrdinal(sites[0].0),
                    binding.clone(),
                ),
            ));
        }
    }
    for stmt in facts {
        for producer in &stmt.stable_iterable_producers {
            let consumer = SourceUnitId(stmt.ordinal.0);
            let producer = SourceUnitId(producer.0);
            let binding = facts[producer.0]
                .declared
                .iter()
                .next()
                .expect("certified spread producer declares one binding")
                .clone();
            raw_edges.push((
                consumer,
                producer,
                EdgeReason::for_binding(DepKind::CoLocation, stmt.ordinal, binding.clone()),
            ));
            raw_edges.push((
                producer,
                consumer,
                EdgeReason::for_binding(
                    DepKind::CoLocation,
                    SourceUnitOrdinal(producer.0),
                    binding,
                ),
            ));
        }
    }
}

/// Edges for each statement's own reads, rebinds and local effects of
/// chunk-declared bindings.
fn emit_direct_binding_edges(
    facts: &[StatementFacts],
    binding_source_unit: &HashMap<Id, SourceUnitId>,
    target_is_hoisted: impl Fn(&Id) -> bool,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) {
    for stmt in facts {
        let from = SourceUnitId(stmt.ordinal.0);
        let mut push_binding_edge = |binding: &Id, kind: DepKind| {
            if let Some(&to) = binding_source_unit.get(binding)
                && from != to
            {
                raw_edges.push((
                    from,
                    to,
                    EdgeReason::for_binding(kind, stmt.ordinal, binding.clone()),
                ));
            }
        };
        for binding in &stmt.reads.eager {
            push_binding_edge(
                binding,
                if target_is_hoisted(binding) {
                    DepKind::HoistedUse
                } else {
                    DepKind::EagerUse
                },
            );
        }
        for binding in &stmt.reads.lazy {
            push_binding_edge(binding, DepKind::LazyUse);
        }
        for binding in &stmt.rebinds.eager {
            push_binding_edge(binding, DepKind::EagerRebind);
        }
        // Only first-order body rebinds emit a constraining
        // `LazyRebind` edge: a rebind inside a nested closure (e.g.
        // an arrow stashed on `globalThis` by the body) doesn't fire
        // when the function is invoked synchronously, so it must not
        // constrain init order or feed at-init call promotion. See
        // the e2e test `e2e/cases/initialization/nested_closure_{read,rebind}`.
        for binding in &stmt.rebinds.first_order_lazy {
            push_binding_edge(binding, DepKind::LazyRebind);
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
            push_binding_edge(binding, DepKind::DeferredRebind);
        }
        for binding in &stmt.local_effects {
            push_binding_edge(binding, DepKind::LocalEffect);
        }
    }
}

/// Initializers and eager observations of shared var cells must retain their
/// order even when the expressions are pure. Include promoted reads from
/// at-init calls, not just syntactically direct reads.
fn emit_shared_var_access_order(
    facts: &[StatementFacts],
    shared_vars: &BTreeSet<Id>,
    fallback_eager_uses: &BTreeMap<usize, AssumedEffectReason>,
    raw_edges: &mut Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) {
    let mut accesses = BindingAccesses::new();
    for stmt in facts {
        for binding in stmt
            .reads
            .eager
            .iter()
            .chain(&stmt.rebinds.eager)
            .chain(&stmt.var_initializers)
        {
            if shared_vars.contains(binding) {
                record_access(
                    &mut accesses,
                    binding,
                    SourceUnitId(stmt.ordinal.0),
                    EffectProvenance::Observed,
                );
            }
        }
    }
    for (index, (from, _, reason)) in raw_edges.iter().enumerate() {
        if reason.kind == DepKind::EagerUse
            && let Some(binding) = reason.binding()
            && shared_vars.contains(binding)
        {
            record_access(
                &mut accesses,
                binding,
                *from,
                EffectProvenance::from_fallback(fallback_eager_uses.get(&index)),
            );
        }
    }
    for (binding, source_units) in &accesses {
        let source_units: Vec<_> = source_units.iter().collect();
        for pair in source_units.windows(2) {
            let (&earlier, earlier_access) = pair[0];
            let (&later, later_access) = pair[1];
            raw_edges.push((
                later,
                earlier,
                EdgeReason::sequenced(
                    SourceUnitOrdinal(later.0),
                    SequencedCause::SharedVarAccess {
                        binding: binding.0.clone(),
                        source: later_access.clone(),
                        target: earlier_access.clone(),
                    },
                ),
            ));
        }
    }
}

/// Sort and assign stable `DependencyEdgeId` indices.
fn sorted_dependency_edges(
    mut raw_edges: Vec<(SourceUnitId, SourceUnitId, EdgeReason)>,
) -> Vec<DependencyEdge> {
    raw_edges.sort_by(|(from_a, to_a, reason_a), (from_b, to_b, reason_b)| {
        from_a
            .cmp(from_b)
            .then(to_a.cmp(to_b))
            .then(reason_a.kind.cmp(&reason_b.kind))
            .then(reason_a.statement_ordinal.cmp(&reason_b.statement_ordinal))
            .then(reason_a.binding.cmp(&reason_b.binding))
    });
    raw_edges
        .into_iter()
        .enumerate()
        .map(|(idx, (from, to, reason))| DependencyEdge {
            id: DependencyEdgeId(idx),
            from,
            to,
            reason,
        })
        .collect()
}

/// Each statement's effects plus the non-assumed heap accesses of its
/// synchronous calls, as commuting premises compare them.
fn premise_effects(
    stmt_by_source_unit: &HashMap<SourceUnitId, &StatementFacts>,
) -> HashMap<SourceUnitId, StatementEffectSummary> {
    stmt_by_source_unit
        .iter()
        .map(|(id, stmt)| {
            let mut effects = stmt.effects();
            if let Some(calls) = &stmt.synchronous_calls {
                effects
                    .reads
                    .extend(calls.non_assumed_heap_accesses.reads.iter().cloned());
                effects
                    .writes
                    .extend(calls.non_assumed_heap_accesses.writes.iter().cloned());
            }
            (*id, effects)
        })
        .collect()
}
