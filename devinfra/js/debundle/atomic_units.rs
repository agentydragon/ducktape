//! Structural atomic factor units: SCCs of the constraining-edge
//! subgraph `G_atomic` over an owner graph. Each SCC is one set of
//! owners any valid factorization must keep co-located, because the
//! bundle's original init order is otherwise unrealizable as ESM.
//! [`crate::factor_assembly::assemble_partition`] consumes the units
//! plus YAML claims and `unassigned_mode` to produce the final
//! partition. See `FACTORIZE.md` for the architecture.
//!
//! Closure rules for `G_atomic` — for each edge `e: u → v` with
//! `e.from != e.to`:
//!
//! * `EagerUse`: add `u → v` (u depends on v at init time).
//! * `LazyUse`: skip (call-time read).
//! * `EagerRebind` / `LazyRebind`: add both directions (declarer
//!   and assigner of a mutable binding must co-locate).
//! * `Sequenced`: add `u → v`. Co-location is only forced when
//!   another constraining edge runs the reverse direction —
//!   Tarjan's SCC handles that automatically.

use std::collections::{BTreeSet, HashSet};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::graph::{DepKind, OwnerGraph, OwnerId};

/// One atomic factor unit: a set of owners that any valid
/// factorization must keep co-located, plus the `DepKind`s of the
/// constraining edges *inside* the unit (everything except
/// `LazyUse`) — used by [`crate::factor_assembly`] to explain *why*
/// a unit is forced together when its claims conflict.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AtomicUnit {
    pub members: BTreeSet<OwnerId>,
    pub causes: HashSet<DepKind>,
}

/// Compute atomic factor units for an owner graph. Returns one unit
/// per SCC of the constraining-edge subgraph; singleton owners with
/// no constraining edges get their own (empty-cause) unit so every
/// owner is covered exactly once.
pub fn compute_atomic_units(owner_graph: &OwnerGraph) -> Vec<AtomicUnit> {
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
            DepKind::LazyUse => {}
            DepKind::EagerRebind | DepKind::LazyRebind => {
                g_atomic.add_edge(edge.from, edge.to, ());
                g_atomic.add_edge(edge.to, edge.from, ());
            }
        }
    }
    let sccs = tarjan_scc(&g_atomic);
    let mut unit_of = vec![None::<usize>; owner_graph.nodes.len()];
    let mut units: Vec<AtomicUnit> = sccs
        .into_iter()
        .enumerate()
        .map(|(idx, scc)| {
            let members: BTreeSet<OwnerId> = scc.into_iter().collect();
            for owner in &members {
                unit_of[owner.0] = Some(idx);
            }
            AtomicUnit {
                members,
                causes: HashSet::new(),
            }
        })
        .collect();
    for edge in owner_graph.iter_edges() {
        if edge.from == edge.to || edge.reason.kind == DepKind::LazyUse {
            continue;
        }
        let (Some(from_unit), Some(to_unit)) = (unit_of[edge.from.0], unit_of[edge.to.0]) else {
            continue;
        };
        if from_unit == to_unit {
            units[from_unit].causes.insert(edge.reason.kind);
        }
    }
    units
}
