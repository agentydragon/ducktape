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

/// Identity of a module the graph/schedule analysis reasons about.
/// Its dense index points into the schedule's `logical_modules` list.
/// A default-assignment output is a logical module with
/// [`ModuleRole::Unassigned`],
/// synthesized by the materializer
/// before `AssignmentAnalysis::build_with` for chunks that need a default
/// destination (every `InlineInEntry` and `CatchallFile` chunk; the
/// `PerAtomicGroup` synthesizer handles assignments itself).
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
#[serde(transparent)]
pub struct ModuleId(pub usize);

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
    /// chunk; the spec assigns it to a logical module (or the
    /// residual entry). `module` is the owning logical module — named
    /// `module` (not `source_unit`) to avoid colliding with the "source_unit =
    /// dependency graph node" sense of `source_unit` used throughout the crate.
    Owned { module: ModuleId },
    /// Introduced by an `import { imported_name as <local> } from
    /// "<source>"` in the chunk's top-level body. The value lives in
    /// another chunk; exactly one logical module re-exports it under
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
    /// catch-all, generated atomic-group outputs, and the anonymous sentinel;
    /// it describes assignment policy, not the physical DFS root selected by
    /// [`crate::ModuleAssignment::residual`].
    Unassigned,
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
    /// physical DFS root selected by [`crate::ModuleAssignment::residual`]. Reports
    /// project `Unassigned` to their legacy residual boolean, including for
    /// generated atomic-group outputs.
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
