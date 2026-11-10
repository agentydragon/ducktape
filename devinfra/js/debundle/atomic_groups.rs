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

use analysis_model::DepKind;
use std::collections::BTreeSet;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitId;

use crate::analysis_inputs::ResolvedCommutingStatements;
use crate::facts::StatementFacts;
use crate::graph::{
    CommutingStatementsUnused, DependencyEdge, DependencyGraph, DependencyGraphBuildError,
    GraphOptions, build_dependency_graph_with,
};

/// One atomic factor group: a set of source units that any valid
/// factorization must keep co-located, plus the `DepKind`s of edges that
/// contribute arcs to `G_atomic` inside the group — used by
/// [`crate::assignment_assembly`] to explain *why* the group is forced
/// together when its claims conflict.
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
/// the result through the residual-group synthesis in `ChunkPlanBuilder` →
/// `AssignmentAnalysis::build_with` (gate crate) and atomic-DAG report emission.
#[derive(Debug, Clone)]
pub struct DependencyGraphAndAtomicGroups {
    pub dependency_graph: DependencyGraph,
    pub atomic_groups: Vec<AtomicGroup>,
    /// Authored `commuting_statements` entries that inerted no edge: warnings
    /// the run reports, not inputs to the gate.
    pub unused_commuting_statements: Vec<CommutingStatementsUnused>,
}

/// How one dependency edge contributes to the atomic-group path projection.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AtomicGroupPathProjection {
    /// A directed at-init read or sequencing constraint.
    DirectedInitOrder,
    /// A two-way ownership constraint, such as a rebind or co-location edge.
    SymmetricColocation,
}

/// How a witness path traverses the dependency edge's original endpoints.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AtomicGroupPathOrientation {
    /// The path follows `edge.from → edge.to`.
    Forward,
    /// The path follows `edge.to → edge.from`; valid only for a symmetric
    /// co-location projection.
    Reverse,
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct AtomicConstraintArc {
    pub from: SourceUnitId,
    pub to: SourceUnitId,
    pub projection: AtomicGroupPathProjection,
    pub orientation: AtomicGroupPathOrientation,
}

/// Project a dependency edge into the exact directed arcs used by `G_atomic`.
/// Both SCC construction and conflict-witness traversal consume this helper so
/// inert and non-forcing edge kinds cannot enter one projection but not the
/// other.
pub(crate) fn atomic_constraint_arcs(
    edge: &DependencyEdge,
) -> impl Iterator<Item = AtomicConstraintArc> {
    let none = [None, None];
    if edge.from == edge.to {
        return none.into_iter().flatten();
    }
    let forward = |projection, orientation| {
        Some(AtomicConstraintArc {
            from: edge.from,
            to: edge.to,
            projection,
            orientation,
        })
    };
    let reverse = |projection| {
        Some(AtomicConstraintArc {
            from: edge.to,
            to: edge.from,
            projection,
            orientation: AtomicGroupPathOrientation::Reverse,
        })
    };
    match edge.reason.kind {
        DepKind::EagerUse | DepKind::Sequenced if edge.reason.constrains_init_order() => [
            forward(
                AtomicGroupPathProjection::DirectedInitOrder,
                AtomicGroupPathOrientation::Forward,
            ),
            None,
        ],
        DepKind::EagerUse | DepKind::Sequenced | DepKind::LazyUse | DepKind::HoistedUse => none,
        DepKind::EagerRebind
        | DepKind::LazyRebind
        | DepKind::DeferredRebind
        | DepKind::LocalEffect
        | DepKind::CoDeclaration
        | DepKind::CoLocation => {
            let projection = AtomicGroupPathProjection::SymmetricColocation;
            [
                forward(projection, AtomicGroupPathOrientation::Forward),
                reverse(projection),
            ]
        }
    }
    .into_iter()
    .flatten()
}

/// Build a dependency graph from chunk facts under the per-chunk
/// [`GraphOptions`] and immediately compute its atomic groups.
pub fn compute_dependency_graph_and_atomic_groups_with(
    facts: &[StatementFacts],
    options: GraphOptions,
    commuting_statements: &[ResolvedCommutingStatements],
) -> Result<DependencyGraphAndAtomicGroups, DependencyGraphBuildError> {
    let (dependency_graph, unused_commuting_statements) =
        build_dependency_graph_with(facts, options, commuting_statements)?;
    let atomic_groups = compute_atomic_groups(&dependency_graph);
    Ok(DependencyGraphAndAtomicGroups {
        dependency_graph,
        atomic_groups,
        unused_commuting_statements,
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
        for arc in atomic_constraint_arcs(edge) {
            g_atomic.add_edge(arc.from, arc.to, ());
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
        if atomic_constraint_arcs(edge).next().is_none() {
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::CommutingStatementsRef;
    use crate::graph::{DependencyEdgeId, EdgeReason, EdgeRole};
    use source_identity::SourceUnitOrdinal;

    fn edge(kind: DepKind, inert: bool) -> DependencyEdge {
        let reason = EdgeReason::synthetic(kind, SourceUnitOrdinal(0), EdgeRole::Direct);
        let reason = if inert {
            reason.with_commuting_premise(Some(CommutingStatementsRef {
                defining_chunk: "static/app".to_owned(),
                input_index: 0,
            }))
        } else {
            reason
        };
        DependencyEdge {
            id: DependencyEdgeId(0),
            from: SourceUnitId(0),
            to: SourceUnitId(1),
            reason,
        }
    }

    #[test]
    fn atomic_projection_emits_only_the_canonical_forcing_arcs() {
        for kind in [DepKind::LazyUse, DepKind::HoistedUse] {
            assert_eq!(atomic_constraint_arcs(&edge(kind, false)).count(), 0);
        }
        assert_eq!(
            atomic_constraint_arcs(&edge(DepKind::Sequenced, true)).count(),
            0,
            "an inert sequenced edge contributes no forcing evidence"
        );

        for kind in [DepKind::EagerUse, DepKind::Sequenced] {
            let arcs = atomic_constraint_arcs(&edge(kind, false)).collect::<Vec<_>>();
            assert_eq!(arcs.len(), 1, "{kind:?}");
            assert_eq!(
                arcs[0].projection,
                AtomicGroupPathProjection::DirectedInitOrder
            );
            assert_eq!(arcs[0].orientation, AtomicGroupPathOrientation::Forward);
        }

        for kind in [
            DepKind::EagerRebind,
            DepKind::LazyRebind,
            DepKind::DeferredRebind,
            DepKind::LocalEffect,
            DepKind::CoDeclaration,
            DepKind::CoLocation,
        ] {
            let arcs = atomic_constraint_arcs(&edge(kind, false)).collect::<Vec<_>>();
            assert_eq!(arcs.len(), 2, "{kind:?}");
            assert!(
                arcs.iter()
                    .all(|arc| arc.projection == AtomicGroupPathProjection::SymmetricColocation),
                "{kind:?}: {arcs:?}"
            );
            assert_eq!(arcs[0].orientation, AtomicGroupPathOrientation::Forward);
            assert_eq!(arcs[1].orientation, AtomicGroupPathOrientation::Reverse);
        }
    }
}
