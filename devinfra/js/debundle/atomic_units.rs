//! Stage 1 of factorize: structural atomic factor units.
//!
//! Computes the strongly-connected components of the constraining-edge
//! subgraph over all owners. Each SCC is one **atomic unit** — a set
//! of owners that any valid factorization of the chunk must keep
//! co-located, because the bundle's original init order is otherwise
//! unrealizable as ESM.
//!
//! Atomic units are mode-independent. They depend only on the chunk's
//! owner graph, not on the spec or any chunk-level config. Stage 2
//! (factor assembly, in a later module) consumes atomic units +
//! YAML claims + the `unassigned_mode` setting to produce the final
//! partition. See `FACTORIZE.md` for the architecture.
//!
//! # Closure rules for the constraining-edge subgraph `G_atomic`
//!
//! For each owner-graph edge `e: u → v` with `e.from != e.to`:
//!
//! * `EagerUse`: add `u → v` to `G_atomic`. (u depends on v at init
//!   time.)
//! * `LazyUse`: skip. Lazy reads happen at call time and don't
//!   constrain co-location.
//! * `EagerRebind` / `LazyRebind`: add both `u → v` and `v → u`
//!   (LazyRebind gate — declarer and assigner must co-locate).
//! * `Sequenced`: add `u → v` (directed source-order dependency —
//!   the owner-graph encodes the edge as `later_stmt → earlier_stmt`
//!   meaning "the later side-effect depends on the earlier side-
//!   effect having run"; linker order resolves a directed Sequenced
//!   alone). Co-location is only forced when Sequenced combines with
//!   another constraining edge in the reverse direction — Tarjan's
//!   SCC handles that automatically.
//!
//! Tarjan-SCC on `G_atomic` produces the atomic units. Inter-unit
//! edges form a DAG by Tarjan's construction.

use std::collections::BTreeSet;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::graph::{DepKind, OwnerGraph, OwnerId};

/// One atomic factor unit: a set of owners that any valid
/// factorization must keep co-located.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AtomicUnit {
    pub members: BTreeSet<OwnerId>,
}

/// Compute atomic factor units for an owner graph. Returns one unit
/// per SCC of the constraining-edge subgraph. Singleton owners with
/// no constraining edges still get their own unit, so the result
/// covers every owner in the graph exactly once.
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
            DepKind::EagerUse => {
                g_atomic.add_edge(edge.from, edge.to, ());
            }
            DepKind::LazyUse => {
                // Lazy reads happen at call time; they don't force
                // co-location for init-order purposes.
            }
            DepKind::EagerRebind | DepKind::LazyRebind => {
                g_atomic.add_edge(edge.from, edge.to, ());
                g_atomic.add_edge(edge.to, edge.from, ());
            }
            DepKind::Sequenced => {
                g_atomic.add_edge(edge.from, edge.to, ());
            }
        }
    }
    tarjan_scc(&g_atomic)
        .into_iter()
        .map(|scc| AtomicUnit {
            members: scc.into_iter().collect(),
        })
        .collect()
}
