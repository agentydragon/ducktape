//! Structural atomic factor groups: SCCs of the constraining-edge
//! subgraph `G_atomic` over an owner graph. Each SCC is one set of
//! owners any valid factorization must keep co-located, because the
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
//! * `LocalEffect`: add both directions (target-local mutation must
//!   co-locate with its target owner).
//! * `Sequenced`: add `u → v`. Co-location is only forced when
//!   another constraining edge runs the reverse direction —
//!   Tarjan's SCC handles that automatically.

use std::collections::BTreeSet;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};

use crate::facts::StatementFacts;
use crate::graph::{
    DepKind, DuplicateTopLevelDeclaration, OwnerGraph, OwnerGraphOptions, OwnerId,
    build_owner_graph_with,
};

/// One atomic factor group: a set of owners that any valid
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
    pub members: BTreeSet<OwnerId>,
    pub causes: BTreeSet<DepKind>,
}

/// Owner graph plus its precomputed atomic groups. Bundled so a single
/// chunk-level computation pays the Tarjan/SCC cost once and threads
/// the result through `ChunkPlanBuilder::synthesize_mini_factors` →
/// `ChunkFactorization::build_with` (gate crate) and atomic-DAG report emission.
#[derive(Debug, Clone)]
pub struct OwnerGraphAndAtomicGroups {
    pub owner_graph: OwnerGraph,
    pub atomic_groups: Vec<AtomicGroup>,
}

/// Build an owner graph from chunk facts under the per-chunk
/// [`OwnerGraphOptions`] and immediately compute its atomic groups.
pub fn compute_owner_graph_and_atomic_groups_with(
    facts: &[StatementFacts],
    options: OwnerGraphOptions,
) -> Result<OwnerGraphAndAtomicGroups, DuplicateTopLevelDeclaration> {
    let owner_graph = build_owner_graph_with(facts, options)?;
    let atomic_groups = compute_atomic_groups(&owner_graph);
    Ok(OwnerGraphAndAtomicGroups {
        owner_graph,
        atomic_groups,
    })
}

/// Compute atomic factor groups for an owner graph. Returns one group
/// per SCC of the constraining-edge subgraph; singleton owners with
/// no constraining edges get their own (empty-cause) group so every
/// owner is covered exactly once.
pub fn compute_atomic_groups(owner_graph: &OwnerGraph) -> Vec<AtomicGroup> {
    let mut g_atomic = DiGraphMap::<OwnerId, ()>::new();
    for node in owner_graph.iter_nodes() {
        g_atomic.add_node(node.id);
    }
    for edge in owner_graph.iter_edges() {
        if edge.from == edge.to {
            continue;
        }
        match edge.reason.kind {
            DepKind::EagerUse | DepKind::Sequenced => {
                g_atomic.add_edge(edge.from, edge.to, ());
            }
            DepKind::LazyUse | DepKind::HoistedUse => {}
            DepKind::EagerRebind
            | DepKind::LazyRebind
            | DepKind::DeferredRebind
            | DepKind::LocalEffect
            | DepKind::CoDeclaration => {
                g_atomic.add_edge(edge.from, edge.to, ());
                g_atomic.add_edge(edge.to, edge.from, ());
            }
        }
    }
    let sccs = tarjan_scc(&g_atomic);
    let mut atomic_group_of = vec![None::<usize>; owner_graph.num_nodes()];
    let mut atomic_groups: Vec<AtomicGroup> = sccs
        .into_iter()
        .enumerate()
        .map(|(idx, scc)| {
            let members: BTreeSet<OwnerId> = scc.into_iter().collect();
            for owner in &members {
                atomic_group_of[owner.0] = Some(idx);
            }
            AtomicGroup {
                members,
                causes: BTreeSet::new(),
            }
        })
        .collect();
    for edge in owner_graph.iter_edges() {
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
