use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use swc_atoms::Atom;
use swc_common::{Mark, SyntaxContext};
use swc_ecma_ast::Id;

// `swc_ecma_ast::Id = (Atom, SyntaxContext)` is the canonical
// hygiene-preserving binding identity. The analysis stores `Id`s
// directly; reports drop `SyntaxContext` at the JSON boundary by
// serializing only the `Atom` (so wire shape stays a bare string).
//
// Previously this module defined `pub type BindingName = String` plus
// a per-chunk `BindingTable` interner that mapped strings to
// `BindingId(usize)`. Both are gone: swc's `Atom` is globally
// interned (equality is pointer comparison), and analysis cells in
// `graph.rs` now key by `Id` directly via `HashMap<Id, _>` instead
// of dense-vec storage indexed by `BindingId.0`.

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

/// Index into the materializer's `module_plans` list, identifying a
/// logical module produced by the spec.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
#[serde(transparent)]
pub struct LogicalModuleIndex(pub usize);

/// Identity of a module the graph/schedule analysis reasons about.
/// Wraps a [`LogicalModuleIndex`] pointing into the schedule's
/// `logical_modules` list. The residual catch-all is just a logical
/// module flagged `residual: true` — synthesized by the materializer
/// before `ChunkFactorization::build` for chunks that need a default
/// destination (every `InlineInEntry` and `CatchallFile` chunk; the
/// `MiniFactors` synthesizer handles assignments itself).
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
#[serde(transparent)]
pub struct ModuleId(pub LogicalModuleIndex);

impl ModuleId {
    pub fn logical(idx: usize) -> Self {
        Self(LogicalModuleIndex(idx))
    }

    pub fn index(self) -> LogicalModuleIndex {
        self.0
    }
}

/// Position of a top-level statement in a chunk's source body.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct StatementOrdinal(pub usize);

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
/// DESIGN.md "Two binding kinds".
#[derive(Debug, Clone)]
pub enum BindingKind {
    /// Declared by a top-level `var/let/const/function/class` in this
    /// chunk; the spec assigns it to a logical module (or the
    /// residual entry).
    Owned { owner: ModuleId },
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

/// A logical module produced by the spec for the current chunk.
/// Projection of `ModulePlan` carrying the fields downstream emit
/// helpers consume (`cross_module_imports_for_body`,
/// `source_chunk_imports_for_moved_body`, etc.).
#[derive(Debug, Clone)]
pub struct LogicalModule {
    pub id: String,
    /// Chunk-relative path the module emits to (e.g. `"runtime/foo.js"`).
    pub target_file: String,
    /// True for the generated residual catch-all module. Peelability
    /// diagnostics use this to identify the remaining unpeeled owner
    /// set.
    pub residual: bool,
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
    /// as anonymous-statement members (owners with empty
    /// `declared_bindings` that the spec resolves by AST shape via
    /// `spec::LogicalModule::anonymous_statements`). ChunkFactorization uses
    /// these to override the otherwise-residual destination of those
    /// owners so cross-destination/cycle checks see the closure as
    /// the materializer will emit it.
    pub anonymous_statement_ordinals: Vec<usize>,
}
