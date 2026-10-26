use crate::{ModuleId, OwnerGraph, OwnerId};

/// Per-owner module assignment for one chunk's owner graph.
///
/// Maps every owner in the chunk's program-dependence graph to an
/// output module. Indexing is dense by [`OwnerId`]; every
/// owner has an assignment (defaulting to the caller-supplied
/// `default_destination` — typically the chunk's synthesized residual
/// module).
///
/// The assignment is the spec's primary input to the realizability gate and
/// the quotient construction. It's stored separately from the IR so the IR can
/// stay immutable while validation and reports derive graph views.
#[derive(Debug, Clone)]
pub struct ModuleAssignment {
    of: Vec<ModuleId>,
    /// The chunk's residual logical module — where unassigned owners
    /// default to, and which the materializer emits as the chunk's
    /// runtime entry. Realizability uses this to identify the ESM
    /// DFS root: any cycle in `I` that contains `residual` with a
    /// constraining edge whose target is `residual` is unsound,
    /// because ESM post-order DFS evaluates `residual` LAST and the
    /// reading module sees `residual`'s bindings in TDZ.
    residual: ModuleId,
}

impl ModuleAssignment {
    /// Build an assignment keyed by `owner_graph.num_nodes()` slots,
    /// each defaulting to `default_destination`. Callers pass the
    /// chunk's residual logical-module id (synthesized by the
    /// materializer); PerAtomicGroup callers can pass any placeholder
    /// because every owner gets a concrete claim from the per-group
    /// synthesizer before the assignment is consulted.
    pub fn new(owner_graph: &OwnerGraph, default_destination: ModuleId) -> Self {
        Self {
            of: vec![default_destination; owner_graph.num_nodes()],
            residual: default_destination,
        }
    }

    /// The chunk's residual module — the ESM DFS root for the
    /// emitted entry. Equal to the `default_destination` the
    /// assignment was constructed with.
    pub fn residual(&self) -> ModuleId {
        self.residual
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

    /// Construct an assignment from a dense `Vec<ModuleId>` indexed by
    /// `OwnerId.0`. Caller-supplied residual; intended for the peel
    /// planner's projection of `QuotientGraph` class assignments back
    /// to `ModuleAssignment` slots before running `check_realizability`.
    pub fn from_assignments(of: Vec<ModuleId>, residual: ModuleId) -> Self {
        Self { of, residual }
    }
}
