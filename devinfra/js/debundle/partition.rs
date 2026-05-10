use std::collections::HashMap;

use crate::{BindingName, ModuleId, OwnerGraph, OwnerId};

/// Per-owner module assignment for one chunk's owner graph.
///
/// Conceptually the **partition** of the chunk's program-dependence
/// graph into output modules. Indexing is dense by [`OwnerId`]; every
/// owner has an assignment (defaulting to [`ModuleId::ResidualEntry`]).
///
/// The partition is the spec's primary input to the realizability gate,
/// the quotient construction, and the peelability search. It's stored
/// separately from the IR so the IR can stay immutable across
/// hypothetical refinements (peelability evaluates many candidate
/// partitions per chunk).
#[derive(Debug, Clone)]
pub struct Partition {
    of: Vec<ModuleId>,
}

impl Partition {
    /// Build a partition keyed by `owner_graph.nodes.len()` slots,
    /// each defaulting to `ModuleId::ResidualEntry`.
    pub fn new(owner_graph: &OwnerGraph) -> Self {
        Self {
            of: vec![ModuleId::ResidualEntry; owner_graph.nodes.len()],
        }
    }

    /// Build a partition that assigns each owner the module of any
    /// `Owned` binding it declares, looked up by name in
    /// `binding_assignment`. Owners with no declared bindings — or
    /// whose declared bindings are absent from the assignment — stay
    /// at [`ModuleId::ResidualEntry`].
    ///
    /// Bindings get the first declaring owner's destination if more
    /// than one owner declares the same name (which the chunk
    /// shouldn't ever do under JS scoping rules, but mirrors the
    /// previous in-graph assignment behaviour).
    pub fn from_binding_assignment(
        owner_graph: &OwnerGraph,
        binding_assignment: &HashMap<BindingName, ModuleId>,
    ) -> Self {
        let mut p = Self::new(owner_graph);
        for node in &owner_graph.nodes {
            for binding_id in &node.declared {
                let Some(name) = owner_graph.binding_table.name(*binding_id) else {
                    continue;
                };
                if let Some(module) = binding_assignment.get(name) {
                    p.of[node.id.0] = *module;
                    break;
                }
            }
        }
        p
    }

    /// Module assignment for `owner`. Panics if `owner` is out of
    /// bounds — every `OwnerId` constructed from the same
    /// `OwnerGraph` must have a slot.
    pub fn of(&self, owner: OwnerId) -> ModuleId {
        self.of[owner.0]
    }

    /// Reassign `owner` to `module`.
    pub fn set(&mut self, owner: OwnerId, module: ModuleId) {
        self.of[owner.0] = module;
    }

    /// `(OwnerId, ModuleId)` pairs in `OwnerId` order.
    pub fn iter(&self) -> impl Iterator<Item = (OwnerId, ModuleId)> + '_ {
        self.of
            .iter()
            .enumerate()
            .map(|(idx, &m)| (OwnerId(idx), m))
    }

    pub fn len(&self) -> usize {
        self.of.len()
    }

    pub fn is_empty(&self) -> bool {
        self.of.is_empty()
    }
}
