//! Structural atomic factor groups: SCCs of the constraining-edge
//! subgraph `G_atomic` over a dependency graph. Each SCC is one set of
//! source units any valid factorization must keep co-located, because the
//! bundle's original init order is otherwise unrealizable as ESM.
//! [`crate::assignment_assembly::assemble_assignment`] consumes the groups
//! plus YAML claims and `unassigned_mode` to produce the final
//! assignment. See `docs/design.md` §"Two classes of atom" + §"Factor
//! assembly inside `debundle run`" for the architecture.
//!
//! Closure rules for `G_atomic` — for each edge `e: u → v` with
//! `e.from != e.to`:
//!
//! * `EagerUse`: add `u → v` (u depends on v at init time).
//! * `LazyUse`: skip (call-time read).
//! * `HoistedUse`: skip (function binding initialized during instantiation).
//! * `EagerRebind` / `LazyRebind` / `DeferredRebind`: add both
//!   directions (declarer and assigner of a mutable binding must
//!   co-locate — ESM imports are read-only whenever the write
//!   fires, init-time or later).
//! * `CoDeclaration`: add both directions (all declaration sites of one
//!   module-scoped var binding share a destination).
//! * `CoLocation`: add both directions (a stable array spread's source
//!   producers share its destination).
//! * `LocalEffect`: add both directions (target-local mutation must
//!   co-locate with its target source unit).
//! * `Sequenced`: add `u → v`. Co-location is only forced when
//!   another constraining edge runs the reverse direction —
//!   Tarjan's SCC handles that automatically.

use std::collections::BTreeSet;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};

use crate::analysis_inputs::ResolvedCommutingStatements;
use crate::facts::StatementFacts;
use crate::graph::{
    DepKind, DependencyGraph, DependencyGraphBuildError, GraphOptions, SourceUnitId,
    build_dependency_graph_with,
};

/// One atomic factor group: a set of source units that any valid
/// factorization must keep co-located, plus the `DepKind`s of the
/// constraining edges *inside* the group (everything except
/// `LazyUse` and `HoistedUse`) — used by [`crate::assignment_assembly`] to explain *why*
/// the group is forced together when its claims conflict.
///
/// `causes` is a `BTreeSet<DepKind>` so iteration order is the
/// `DepKind` `Ord` order — consumers that serialise the field can
/// drop their own post-collection sort.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AtomicGroup {
    pub members: BTreeSet<SourceUnitId>,
    pub causes: BTreeSet<DepKind>,
}

/// Dependency graph plus its precomputed atomic groups. Bundled so a single
/// chunk-level computation pays the Tarjan/SCC cost once and threads
/// the result through `ChunkPlanBuilder::synthesize_per_atomic_group` →
/// `AssignmentAnalysis::build_with` (gate crate) and atomic-DAG report emission.
#[derive(Debug, Clone)]
pub struct DependencyGraphAndAtomicGroups {
    pub dependency_graph: DependencyGraph,
    pub atomic_groups: Vec<AtomicGroup>,
}

/// Build a dependency graph from chunk facts under the per-chunk
/// [`GraphOptions`] and immediately compute its atomic groups.
pub fn compute_dependency_graph_and_atomic_groups_with(
    facts: &[StatementFacts],
    options: GraphOptions,
    commuting_statements: &[ResolvedCommutingStatements],
) -> Result<DependencyGraphAndAtomicGroups, DependencyGraphBuildError> {
    let dependency_graph = build_dependency_graph_with(facts, options, commuting_statements)?;
    let atomic_groups = compute_atomic_groups(&dependency_graph);
    Ok(DependencyGraphAndAtomicGroups {
        dependency_graph,
        atomic_groups,
    })
}

/// Compute atomic factor groups for a dependency graph. Returns one group
/// per SCC of the constraining-edge subgraph; singleton source units with
/// no constraining edges get their own (empty-cause) group so every
/// owner is covered exactly once.
pub fn compute_atomic_groups(dependency_graph: &DependencyGraph) -> Vec<AtomicGroup> {
    let mut g_atomic = DiGraphMap::<SourceUnitId, ()>::new();
    for node in dependency_graph.iter_nodes() {
        g_atomic.add_node(node.id);
    }
    for edge in dependency_graph.iter_edges() {
        if edge.from == edge.to {
            continue;
        }
        match edge.reason.kind {
            // A `Sequenced` edge an author `commuting_statements` premise
            // made inert constrains nothing, so it must not close a group.
            DepKind::EagerUse | DepKind::Sequenced if edge.reason.constrains_init_order() => {
                g_atomic.add_edge(edge.from, edge.to, ());
            }
            DepKind::EagerUse | DepKind::Sequenced => {}
            DepKind::LazyUse | DepKind::HoistedUse => {}
            DepKind::EagerRebind
            | DepKind::LazyRebind
            | DepKind::DeferredRebind
            | DepKind::LocalEffect
            | DepKind::CoDeclaration
            | DepKind::CoLocation => {
                g_atomic.add_edge(edge.from, edge.to, ());
                g_atomic.add_edge(edge.to, edge.from, ());
            }
        }
    }
    let sccs = tarjan_scc(&g_atomic);
    let mut atomic_group_of = vec![None::<usize>; dependency_graph.num_nodes()];
    let mut atomic_groups: Vec<AtomicGroup> = sccs
        .into_iter()
        .enumerate()
        .map(|(idx, scc)| {
            let members: BTreeSet<SourceUnitId> = scc.into_iter().collect();
            for source_unit in &members {
                atomic_group_of[source_unit.0] = Some(idx);
            }
            AtomicGroup {
                members,
                causes: BTreeSet::new(),
            }
        })
        .collect();
    for edge in dependency_graph.iter_edges() {
        if edge.from == edge.to
            || matches!(edge.reason.kind, DepKind::LazyUse | DepKind::HoistedUse)
        {
            continue;
        }
        let (Some(from_group), Some(to_group)) =
            (atomic_group_of[edge.from.0], atomic_group_of[edge.to.0])
        else {
            continue;
        };
        if from_group == to_group {
            atomic_groups[from_group].causes.insert(edge.reason.kind);
        }
    }
    atomic_groups
}
