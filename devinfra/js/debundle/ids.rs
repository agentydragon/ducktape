use source_identity::SourceUnitOrdinal;
use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use swc_atoms::Atom;
use swc_common::{Mark, SyntaxContext};
use swc_ecma_ast::Id;

/// Construct the hygiene-aware `Id` for a chunk-top-level binding.
/// SWC's `resolver` pass assigns `ctxt = SyntaxContext::empty().apply_mark(top_level_mark)`
/// to every top-level binding in a parsed `Module`. Spec-derived
/// String names (which carry no ctxt) are resolved to their `Id` by
/// pairing the sym with this canonical ctxt.
pub fn top_level_id(sym: &str, top_level_mark: Mark) -> Id {
    (
        Atom::from(sym),
        SyntaxContext::empty().apply_mark(top_level_mark),
    )
}

/// Identity of a destination in the module-quotient graph and schedule.
/// Planned outputs use dense indices into `planned_modules`; a materialized
/// analysis wraps the entry destination in [`EntryDestination`] after those
/// outputs. Generic quotient projections may use other dense destination IDs.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
#[serde(transparent)]
pub struct ModuleId(pub usize);

/// Destination used for unclaimed source units and as the ESM DFS root.
/// In a materialized chunk this follows all planned output modules; generic
/// quotient projections may use any dense module identity as their root.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct EntryDestination(ModuleId);

impl EntryDestination {
    /// Construct a destination for a generic assignment or projected graph.
    pub fn from_module_id(module_id: ModuleId) -> Self {
        Self(module_id)
    }

    /// Construct the destination immediately following the planned outputs.
    pub fn after_planned_modules(planned_module_count: usize) -> Self {
        Self(ModuleId(planned_module_count))
    }

    pub fn module_id(self) -> ModuleId {
        self.0
    }
}

/// Interned chunk identifier. Created by `ChunkTable::intern` during chunk
/// loading and used throughout the pipeline in place of `String` chunk names.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ChunkId(pub usize);

#[derive(Debug, Clone, Default)]
pub struct ChunkTable {
    names: Vec<String>,
    ids_by_name: HashMap<String, ChunkId>,
}

impl ChunkTable {
    pub fn intern(&mut self, name: String) -> ChunkId {
        if let Some(id) = self.ids_by_name.get(&name) {
            return *id;
        }
        let id = ChunkId(self.names.len());
        self.names.push(name.clone());
        self.ids_by_name.insert(name, id);
        id
    }

    pub fn get(&self, name: &str) -> Option<ChunkId> {
        self.ids_by_name.get(name).copied()
    }

    pub fn name(&self, id: ChunkId) -> &str {
        &self.names[id.0]
    }

    pub fn len(&self) -> usize {
        self.names.len()
    }

    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }
}

/// How a top-level binding in the chunk relates to the split. See
/// docs/design.md "Two binding kinds".
#[derive(Debug, Clone)]
pub enum BindingKind {
    /// Declared by a top-level `var/let/const/function/class` in this
    /// chunk; an explicit spec claim assigns it to a planned output,
    /// while unclaimed bindings use the entry destination. The field is
    /// named `module` to distinguish it from `source_unit`, the crate's term
    /// for a dependency-graph node.
    Owned { module: ModuleId },
    /// Introduced by an `import { imported_name as <local> } from
    /// "<source>"` in the chunk's top-level body. The value lives in
    /// another chunk; exactly one planned output re-exports it under
    /// its chosen public name (the spec's duplicate-claim check
    /// rejects two modules claiming the same import).
    Imported {
        /// The original imported name from the source chunk (e.g. "j"
        /// for `import { j as a } from "..."`). An `Atom` rather than
        /// `Id`: export names are pure labels, no hygiene context
        /// applies.
        imported_name: Atom,
        /// Output-tree-rooted absolute path of the import source
        /// (e.g. `"static/vendor.js"`). Already resolved against the
        /// chunk's directory + the artifact's source-chunk index;
        /// emit-time path resolution is just `relative(dest_dir,
        /// imported_from)`.
        imported_from: String,
        /// Logical module that claimed this imported binding via a
        /// `kind: import_specifier` member.
        re_exporter: ModuleId,
        /// Public export name that re-exporter assigned to it.
        public_name: Atom,
    },
}

/// Semantic role of a planned module destination.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum ModuleRole {
    /// A destination explicitly specified by a module request.
    Specified,
    /// A destination used by the default policy for source owners that
    /// were not assigned by an explicit module request. This includes the
    /// catch-all and generated atomic-group outputs. It describes assignment
    /// policy, not the physical DFS root selected by
    /// [`crate::ModuleAssignment::entry_destination`].
    Unassigned,
}

/// Identity of a binding within the parsed program.
///
/// The SWC `Id` retains both the symbol and its `SyntaxContext`; `chunk_id`
/// distinguishes equal SWC identities from different source chunks. This is a
/// runtime identity and is intentionally not serialized into reports or specs.
#[derive(Debug, Clone, Eq, PartialEq, Hash)]
pub struct BindingId {
    chunk_id: ChunkId,
    swc_id: Id,
}

impl BindingId {
    pub fn new(chunk_id: ChunkId, swc_id: Id) -> Self {
        Self { chunk_id, swc_id }
    }

    pub fn chunk_id(&self) -> ChunkId {
        self.chunk_id
    }

    pub fn swc_id(&self) -> &Id {
        &self.swc_id
    }
}

/// Error returned when a binding identity or local catalogue view belongs to
/// a different chunk than the catalogue's scope.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct BindingScopeError {
    expected: ChunkId,
    actual: ChunkId,
}

impl std::fmt::Display for BindingScopeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "binding belongs to chunk {}, but catalogue is scoped to chunk {}",
            self.actual.0, self.expected.0
        )
    }
}

impl std::error::Error for BindingScopeError {}

/// Per-chunk binding metadata, keyed by the original hygiene-aware SWC ID.
///
/// The chunk identity is stored once for the whole catalogue, avoiding a
/// chunk-ID rekeying of every local binding. External identity lookups use
/// [`BindingId`]; same-chunk graph and AST algorithms obtain a checked local
/// view with [`BindingCatalogue::local_view`].
#[derive(Debug, Clone)]
pub struct BindingCatalogue {
    chunk_id: ChunkId,
    bindings_by_swc_id: HashMap<Id, BindingKind>,
}

impl BindingCatalogue {
    pub fn new(chunk_id: ChunkId, bindings_by_swc_id: HashMap<Id, BindingKind>) -> Self {
        Self {
            chunk_id,
            bindings_by_swc_id,
        }
    }

    pub fn chunk_id(&self) -> ChunkId {
        self.chunk_id
    }

    /// Look up a program-scoped identity, rejecting IDs from another chunk.
    pub fn get(&self, binding_id: &BindingId) -> Result<Option<&BindingKind>, BindingScopeError> {
        self.check_binding_id(binding_id)?;
        Ok(self.bindings_by_swc_id.get(binding_id.swc_id()))
    }

    pub fn check_binding_id(&self, binding_id: &BindingId) -> Result<(), BindingScopeError> {
        self.check_chunk(binding_id.chunk_id)
    }

    /// Borrow the local SWC-ID map after checking the caller's chunk scope.
    pub fn local_view(&self, chunk_id: ChunkId) -> Result<LocalBindings<'_>, BindingScopeError> {
        self.check_chunk(chunk_id)?;
        Ok(LocalBindings {
            bindings_by_swc_id: &self.bindings_by_swc_id,
        })
    }

    fn check_chunk(&self, chunk_id: ChunkId) -> Result<(), BindingScopeError> {
        if chunk_id == self.chunk_id {
            Ok(())
        } else {
            Err(BindingScopeError {
                expected: self.chunk_id,
                actual: chunk_id,
            })
        }
    }
}

/// Borrowed view of a catalogue's SWC-ID map, available only after an explicit
/// chunk-scope check.
#[derive(Debug, Clone, Copy)]
pub struct LocalBindings<'a> {
    bindings_by_swc_id: &'a HashMap<Id, BindingKind>,
}

impl LocalBindings<'_> {
    pub fn as_map(&self) -> &HashMap<Id, BindingKind> {
        self.bindings_by_swc_id
    }
}

#[cfg(test)]
mod binding_id_tests {
    use super::*;
    use swc_common::{GLOBALS, Globals};

    #[test]
    fn catalogue_checks_chunk_and_keeps_swc_hygiene() {
        GLOBALS.set(&Globals::new(), || {
            let chunk_a = ChunkId(3);
            let chunk_b = ChunkId(4);
            let ctxt_a = SyntaxContext::empty().apply_mark(Mark::new());
            let ctxt_b = SyntaxContext::empty().apply_mark(Mark::new());
            let swc_id_a = (Atom::from("same"), ctxt_a);
            let swc_id_b = (Atom::from("same"), ctxt_b);
            let catalogue = BindingCatalogue::new(
                chunk_a,
                HashMap::from([(
                    swc_id_a.clone(),
                    BindingKind::Owned {
                        module: ModuleId(0),
                    },
                )]),
            );
            let local_view = catalogue.local_view(chunk_a).unwrap();
            assert!(local_view.as_map().contains_key(&swc_id_a));

            assert_eq!(
                catalogue
                    .get(&BindingId::new(chunk_a, swc_id_a.clone()))
                    .unwrap()
                    .map(|kind| match kind {
                        BindingKind::Owned { module } => *module,
                        BindingKind::Imported { .. } => panic!("unexpected imported binding"),
                    }),
                Some(ModuleId(0))
            );
            assert!(
                catalogue
                    .get(&BindingId::new(chunk_a, swc_id_b))
                    .unwrap()
                    .is_none(),
                "equal names with different syntax contexts are different bindings"
            );
            assert_eq!(
                catalogue
                    .get(&BindingId::new(chunk_b, swc_id_a))
                    .unwrap_err(),
                BindingScopeError {
                    expected: chunk_a,
                    actual: chunk_b,
                }
            );
            assert!(catalogue.local_view(chunk_b).is_err());
        });
    }
}

/// A planned module produced from the spec for the current chunk.
/// Projection of `ModulePlan` carrying the fields downstream emit
/// helpers consume (`cross_module_imports_for_body`,
/// `source_chunk_imports_for_moved_body`, etc.).
#[derive(Debug, Clone)]
pub struct PlannedModule {
    pub id: String,
    /// Chunk-relative path the module emits to (e.g. `"runtime/foo.js"`).
    pub target_file: String,
    /// This policy role distinguishes explicitly specified destinations from
    /// outputs that receive unassigned source units. It is independent of the
    /// physical DFS root selected by
    /// [`crate::ModuleAssignment::entry_destination`]. Reports project
    /// `Unassigned` to their legacy residual boolean, including for generated
    /// atomic-group outputs.
    pub role: ModuleRole,
    /// Local-name → exported-name map for the bindings this module
    /// owns. Empty when the module re-exports only imported
    /// bindings. Iteration order is undefined; report and emit
    /// sites sort by local name before consuming.
    /// Maps each owned binding's hygiene-aware `Id` to its public
    /// exported name (an `Atom`/sym, no ctxt — exports are name-only).
    /// Spec strings are resolved to `Id` at chunk-build time via
    /// `top_level_id(name, top_level_mark)`.
    pub rename_map: HashMap<Id, Atom>,
    /// Source-chunk top-level statement ordinals this module claims
    /// as anonymous-statement members (source_units with empty
    /// `declared_bindings` that the spec resolves by AST shape via
    /// `spec::LogicalModule::anonymous_statements`). AssignmentAnalysis uses
    /// these to override the otherwise-residual destination of those
    /// source_units so cross-destination/cycle checks see the closure as
    /// the materializer will emit it.
    pub anonymous_source_unit_ordinals: Vec<SourceUnitOrdinal>,
}
