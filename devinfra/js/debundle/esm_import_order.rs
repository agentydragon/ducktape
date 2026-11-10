//! Single source of truth for per-module ESM import-directive
//! ordering, shared by the emitter (`lowering`) and the candidate's
//! exact emitted-order simulation (`realizability`).
//!
//! Both consumers must order import targets identically or the candidate's
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
//!   `ModuleId` ascending on ties. Acyclic import graphs prefer each module's
//!   earliest source unit; DFS still evaluates its dependencies first. Cyclic
//!   graphs retain the base order except for isolated entry-only leaves.
//! - **Module imports** (every emitted logical module's intra-chunk
//!   `import` directives: cross-module binding imports, phantom
//!   side-effect imports, and the residual-entry import, merged into
//!   ONE list): ordered by [`EsmImportOrder::sort_module_imports`] —
//!   `linker_position` ascending (dependency-first toposort of the
//!   constraining-edge subgraph; missing position sorts last),
//!   `ModuleId` ascending on ties.
//!
//! The emitter renders import declarations in exactly these orders
//! (`lowering/lower.rs`); the candidate uses the same orders as DFS
//! neighbor order. The legacy graph gate uses the base order: acyclic imports
//! always evaluate dependencies first regardless of entry import order. In
//! cyclic graphs only entry-only leaves move; they cannot affect other modules'
//! relative order and every constraint into one comes from entry, which runs
//! last. Do not reintroduce
//! per-side ordering rules (e.g. "phantom imports first") — encode
//! any ordering requirement here so both sides inherit it.

use std::collections::{BTreeMap, BTreeSet};

use analysis::graph::{
    ChunkConstrainingEdgeSet, DependencyGraph, chunk_linker_order_from_pairs,
    chunk_source_import_order_from_adjacency, position_lookup,
};
use analysis::ids::ModuleId;
use analysis::module_assignment::ModuleAssignment;
use petgraph::algo::is_cyclic_directed;
use petgraph::graphmap::DiGraphMap;
use source_identity::SourceUnitOrdinal;

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

    /// Build emitted import ordering from the canonical module edges, source
    /// graph, assignment, and complete emitted-module universe. First source
    /// ordinals are derived per assigned module once and shared with the
    /// source-order preference; that preference also accounts for the
    /// runtime entry fanout over every emitted module.
    ///
    /// `canonical` must have been built from `dependency_graph` and
    /// `assignment`. `emitted_modules` must contain every logical module the
    /// entry imports, including isolated modules, and the entry destination.
    pub fn build_for_assignment(
        canonical: &ChunkConstrainingEdgeSet,
        dependency_graph: &DependencyGraph,
        assignment: &ModuleAssignment,
        emitted_modules: &BTreeSet<ModuleId>,
    ) -> Self {
        let entry = assignment.entry_destination().module_id();
        debug_assert!(emitted_modules.contains(&entry));
        debug_assert!(
            assignment
                .iter()
                .all(|(_, module)| emitted_modules.contains(&module))
        );

        let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        let mut import_order = Self::build(
            &constraining_pairs,
            &canonical.i_successors,
            emitted_modules,
        );
        let mut first_units = BTreeMap::<ModuleId, SourceUnitOrdinal>::new();
        for node in dependency_graph.iter_nodes() {
            first_units
                .entry(assignment.of(node.id))
                .and_modify(|first| *first = (*first).min(node.statement_ordinal))
                .or_insert(node.statement_ordinal);
        }
        import_order.preserve_source_order(&canonical.i_successors, &first_units, entry);
        import_order
    }

    pub fn linker_order(&self) -> &[ModuleId] {
        &self.linker_order
    }

    /// Prefer source order when every import dependency is acyclic. DFS still
    /// visits dependencies first. Cyclic graphs retain their rescue ordering;
    /// only entry-only leaves can be interleaved without affecting that order.
    pub(crate) fn preserve_source_order(
        &mut self,
        i_successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        first_units: &BTreeMap<ModuleId, SourceUnitOrdinal>,
        entry: ModuleId,
    ) {
        let mut graph = DiGraphMap::<ModuleId, ()>::from_edges(
            i_successors
                .iter()
                .flat_map(|(from, targets)| targets.iter().map(move |to| (*from, *to))),
        );
        // Entry imports every emitted module even without a source-level
        // dependency edge. A module importing entry therefore forms a runtime
        // cycle and must retain the existing rescue ordering.
        for &module in self.source_import_position.keys() {
            if module != entry {
                graph.add_edge(entry, module, ());
            }
        }
        if !is_cyclic_directed(&graph) {
            let mut order: Vec<_> = self.source_import_position.keys().copied().collect();
            order.sort_by_key(|module| {
                (
                    first_units.get(module).map_or(usize::MAX, |first| first.0),
                    *module,
                )
            });
            self.source_import_position = position_lookup(&order);
            return;
        }
        let mut connected: BTreeSet<_> = i_successors
            .iter()
            .filter(|(from, _)| **from != entry)
            .flat_map(|(from, targets)| std::iter::once(*from).chain(targets.iter().copied()))
            .collect();
        connected.insert(entry);
        let mut order: Vec<_> = self.source_import_position.keys().copied().collect();
        order.sort_by_key(|module| self.source_import_position[module]);
        let (mut isolated, scheduled): (Vec<_>, Vec<_>) = order
            .into_iter()
            .partition(|module| !connected.contains(module) && first_units.contains_key(module));
        isolated.sort_by_key(|module| (first_units[module], *module));
        let mut isolated = isolated.into_iter().peekable();
        let mut ordered = Vec::with_capacity(self.source_import_position.len());
        for module in scheduled {
            if let Some(first) = first_units.get(&module) {
                while isolated
                    .peek()
                    .is_some_and(|next| first_units[next] < *first)
                {
                    ordered.push(isolated.next().expect("peeked isolated module"));
                }
            }
            ordered.push(module);
        }
        ordered.extend(isolated);
        self.source_import_position = position_lookup(&ordered);
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
