//! Structural atomic factor units: SCCs of the constraining-edge
//! subgraph `G_atomic` over an owner graph. Each SCC is one set of
//! owners any valid factorization must keep co-located, because the
//! bundle's original init order is otherwise unrealizable as ESM.
//! [`crate::factor_assembly::assemble_partition`] consumes the units
//! plus YAML claims and `unassigned_mode` to produce the final
//! partition. See `docs/design.md` §"Two classes of atom" + §"Factor
//! assembly inside `debundle run`" for the architecture.
//!
//! Closure rules for `G_atomic` — for each edge `e: u → v` with
//! `e.from != e.to`:
//!
//! * `EagerUse`: add `u → v` (u depends on v at init time).
//! * `LazyUse`: skip (call-time read).
//! * `EagerRebind` / `LazyRebind` / `DeferredRebind`: add both
//!   directions (declarer and assigner of a mutable binding must
//!   co-locate — ESM imports are read-only whenever the write
//!   fires, init-time or later).
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
use crate::graph::{DepKind, OwnerGraph, OwnerGraphOptions, OwnerId, build_owner_graph_with};

/// One atomic factor unit: a set of owners that any valid
/// factorization must keep co-located, plus the `DepKind`s of the
/// constraining edges *inside* the unit (everything except
/// `LazyUse`) — used by [`crate::factor_assembly`] to explain *why*
/// a unit is forced together when its claims conflict.
///
/// `causes` is a `BTreeSet<DepKind>` so iteration order is the
/// `DepKind` `Ord` order — consumers that serialise the field can
/// drop their own post-collection sort.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AtomicUnit {
    pub members: BTreeSet<OwnerId>,
    pub causes: BTreeSet<DepKind>,
}

/// Owner graph plus its precomputed atomic units. Bundled so a single
/// chunk-level computation pays the Tarjan/SCC cost once and threads
/// the result through `synthesize_mini_factor_plans` →
/// `ChunkFactorization::build_with` (gate crate) and atomic-DAG report emission.
#[derive(Debug, Clone)]
pub struct OwnerGraphAndUnits {
    pub owner_graph: OwnerGraph,
    pub atomic_units: Vec<AtomicUnit>,
}

/// Build an owner graph from chunk facts and immediately compute its
/// atomic units. Convenience for call sites that need both. Uses the
/// default (strictly-conservative) [`OwnerGraphOptions`] — call
/// [`compute_owner_graph_and_units_with`] when the chunk spec opts
/// into conditionally-correct refinements.
pub fn compute_owner_graph_and_units(facts: &[StatementFacts]) -> OwnerGraphAndUnits {
    compute_owner_graph_and_units_with(facts, OwnerGraphOptions::default())
}

/// Like [`compute_owner_graph_and_units`] but takes per-chunk
/// [`OwnerGraphOptions`].
pub fn compute_owner_graph_and_units_with(
    facts: &[StatementFacts],
    options: OwnerGraphOptions,
) -> OwnerGraphAndUnits {
    let owner_graph = build_owner_graph_with(facts, options);
    let atomic_units = compute_atomic_units(&owner_graph);
    OwnerGraphAndUnits {
        owner_graph,
        atomic_units,
    }
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
            DepKind::EagerRebind
            | DepKind::LazyRebind
            | DepKind::DeferredRebind
            | DepKind::LocalEffect => {
                g_atomic.add_edge(edge.from, edge.to, ());
                g_atomic.add_edge(edge.to, edge.from, ());
            }
        }
    }
    let sccs = tarjan_scc(&g_atomic);
    let mut unit_of = vec![None::<usize>; owner_graph.num_nodes()];
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
                causes: BTreeSet::new(),
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
