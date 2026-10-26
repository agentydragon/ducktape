use crate::{DependencyGraph, EntryDestination, ModuleId, SourceUnitId};

/// Per-source_unit module assignment for one chunk's dependency graph.
///
/// Maps every source_unit in the chunk's program-dependence graph to an
/// output destination. Indexing is dense by [`SourceUnitId`]; every
/// source unit has an assignment (defaulting to the caller-supplied entry
/// destination).
///
/// The assignment is the spec's primary input to the realizability gate and
/// the quotient construction. It's stored separately from the IR so the IR can
/// stay immutable while validation and reports derive graph views.
#[derive(Debug, Clone)]
pub struct ModuleAssignment {
    of: Vec<ModuleId>,
    /// The default entry destination and ESM DFS root.
    entry_destination: EntryDestination,
}

impl ModuleAssignment {
    /// Build an assignment keyed by `dependency_graph.num_nodes()` slots,
    /// each defaulting to `entry_destination`. Per-atomic-group callers can
    /// pass any placeholder because every source unit gets a concrete claim
    /// from the per-atomic-group synthesizer before the assignment is used.
    pub fn new(dependency_graph: &DependencyGraph, entry_destination: EntryDestination) -> Self {
        Self {
            of: vec![entry_destination.module_id(); dependency_graph.num_nodes()],
            entry_destination,
        }
    }

    /// The assignment's default destination and ESM DFS root.
    pub fn entry_destination(&self) -> EntryDestination {
        self.entry_destination
    }

    /// Module assignment for `source_unit`. Panics if `source_unit` is out of
    /// bounds — every `SourceUnitId` constructed from the same
    /// `DependencyGraph` must have a slot.
    pub fn of(&self, source_unit: SourceUnitId) -> ModuleId {
        self.of[source_unit.0]
    }

    /// Reassign `source_unit` to `module`.
    pub fn set(&mut self, source_unit: SourceUnitId, module: ModuleId) {
        self.of[source_unit.0] = module;
    }

    /// `(SourceUnitId, ModuleId)` pairs in `SourceUnitId` order.
    pub fn iter(&self) -> impl Iterator<Item = (SourceUnitId, ModuleId)> + '_ {
        self.of
            .iter()
            .enumerate()
            .map(|(idx, &m)| (SourceUnitId(idx), m))
    }

    /// Construct an assignment from a dense `Vec<ModuleId>` indexed by
    /// `SourceUnitId.0`. Caller-supplied entry destination; intended for the
    /// peel planner's projection of `QuotientGraph` class assignments back
    /// to `ModuleAssignment` slots before running `check_realizability`.
    pub fn from_assignments(of: Vec<ModuleId>, entry_destination: EntryDestination) -> Self {
        Self {
            of,
            entry_destination,
        }
    }
}
