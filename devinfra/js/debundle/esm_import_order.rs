//! Single source of truth for per-module ESM import-directive
//! ordering, shared by the emitter (`lowering`) and the
//! realizability gate's ESM evaluation simulator (`realizability`).
//!
//! Both consumers must order import targets identically or the gate's
//! verdict stops describing the bundle the emitter produces: the
//! simulator would predict one ECMA-262 Phase-2 evaluation order while
//! the emitted source steers the runtime linker DFS into another, and
//! an accepted spec could TDZ under Node (or a runnable spec could be
//! rejected). The contract:
//!
//! - **Entry imports** (the chunk entry file's `import` directives,
//!   one per emitted logical module): ordered by
//!   [`EsmImportOrder::sort_entry_imports`] —
//!   `source_import_position` ascending (docs/design.md "Lemma 2":
//!   SCC dep rank ascending, intra-SCC linker position descending),
//!   `ModuleId` ascending on ties.
//! - **Module imports** (every emitted logical module's intra-chunk
//!   `import` directives: cross-module binding imports, phantom
//!   side-effect imports, and the residual-entry import, merged into
//!   ONE list): ordered by [`EsmImportOrder::sort_module_imports`] —
//!   `linker_position` ascending (dependency-first toposort of the
//!   constraining-edge subgraph; missing position sorts last),
//!   `ModuleId` ascending on ties.
//!
//! The emitter renders import declarations in exactly these orders
//! (`lowering/lower.rs`); the simulator uses the same orders as DFS
//! neighbor order (`realizability::EsmIGraph`). Do not reintroduce
//! per-side ordering rules (e.g. "phantom imports first") — encode
//! any ordering requirement here so both sides inherit it.

use std::collections::{BTreeMap, BTreeSet};

use analysis::graph::{
    chunk_linker_order_from_pairs, chunk_source_import_order_from_adjacency, position_lookup,
};
use analysis::ids::ModuleId;

/// Sort key for one import target: `(position, target)` with missing
/// positions falling to `usize::MAX` so unconstrained modules sort
/// last, deterministically by `ModuleId`.
type ImportSortKey = (usize, ModuleId);

#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct EsmImportOrder {
    /// Canonical linker order (dependency-first toposort of the
    /// constraining-edge subgraph). Empty when the constraining
    /// subgraph is cyclic (the gate's Pass 1 rejects the spec).
    linker_order: Vec<ModuleId>,
    linker_position: BTreeMap<ModuleId, usize>,
    source_import_position: BTreeMap<ModuleId, usize>,
}

impl EsmImportOrder {
    /// Build from the canonical I-graph views (see
    /// `graph::chunk_constraining_module_edges`): the constraining
    /// `(from, to)` pairs drive the linker toposort, the full
    /// `i_successors` adjacency drives Lemma 2's SCC computation, and
    /// `extra_nodes` get a deterministic source-order slot even when
    /// they participate in no canonical edge (the emitter passes
    /// every logical module; the simulator passes its node universe).
    pub fn build(
        constraining_pairs: &BTreeSet<(ModuleId, ModuleId)>,
        i_successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        extra_nodes: &BTreeSet<ModuleId>,
    ) -> Self {
        let linker_order = chunk_linker_order_from_pairs(constraining_pairs.iter().copied());
        let linker_position = position_lookup(&linker_order);
        let source_import_order = chunk_source_import_order_from_adjacency(
            constraining_pairs.iter().copied(),
            i_successors,
            extra_nodes,
        );
        let source_import_position = position_lookup(&source_import_order);
        Self {
            linker_order,
            linker_position,
            source_import_position,
        }
    }

    pub fn linker_order(&self) -> &[ModuleId] {
        &self.linker_order
    }

    pub fn linker_position(&self, id: ModuleId) -> Option<usize> {
        self.linker_position.get(&id).copied()
    }

    pub fn source_import_position(&self, id: ModuleId) -> Option<usize> {
        self.source_import_position.get(&id).copied()
    }

    /// Sort key of `target` among the chunk entry's import directives.
    pub fn entry_import_sort_key(&self, target: ModuleId) -> ImportSortKey {
        (
            self.source_import_position(target).unwrap_or(usize::MAX),
            target,
        )
    }

    /// Sort key of `target` among a non-entry module's import
    /// directives.
    pub fn module_import_sort_key(&self, target: ModuleId) -> ImportSortKey {
        (self.linker_position(target).unwrap_or(usize::MAX), target)
    }

    /// Order the chunk entry's import directives (one per emitted
    /// logical module, binding or side-effect-only).
    pub fn sort_entry_imports<T>(&self, imports: &mut [(ModuleId, T)]) {
        imports.sort_by_key(|(target, _)| self.entry_import_sort_key(*target));
    }

    /// Order a non-entry module's intra-chunk import directives
    /// (cross-module binding imports, phantom side-effect imports,
    /// and the residual-entry import, as one merged list).
    pub fn sort_module_imports<T>(&self, imports: &mut [(ModuleId, T)]) {
        imports.sort_by_key(|(target, _)| self.module_import_sort_key(*target));
    }
}
