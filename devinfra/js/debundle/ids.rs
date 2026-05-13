use std::collections::HashMap;

use serde::{Deserialize, Serialize};

/// Index into the materializer's `module_plans` list, identifying a
/// logical module produced by the spec.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct LogicalModuleIndex(pub usize);

/// Identity of a module the graph/schedule analysis reasons about. The
/// residual entry is a first-class variant rather than a sentinel
/// index, so callers can't accidentally treat it as a normal logical
/// module.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub enum ModuleId {
    Logical(LogicalModuleIndex),
    ResidualEntry,
}

/// Position of a top-level statement in a chunk's source body.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct StatementOrdinal(pub usize);

/// Local name of a binding in a chunk's top-level scope. Stays a
/// plain `String` (the actual JavaScript identifier text); the alias
/// is documentation. See DESIGN.md "Identifiers and types".
pub type BindingName = String;

/// Stable per-chunk interned binding id. Reports and specs still use
/// `BindingName`; graph algorithms can use this compact key for maps
/// built during one chunk analysis.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct BindingId(pub usize);

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

#[derive(Debug, Clone, Default)]
pub struct BindingTable {
    names: Vec<BindingName>,
    ids_by_name: HashMap<BindingName, BindingId>,
}

impl BindingTable {
    pub fn intern(&mut self, name: BindingName) -> BindingId {
        if let Some(id) = self.ids_by_name.get(&name) {
            return *id;
        }
        let id = BindingId(self.names.len());
        self.names.push(name.clone());
        self.ids_by_name.insert(name, id);
        id
    }

    pub fn get(&self, name: &str) -> Option<BindingId> {
        self.ids_by_name.get(name).copied()
    }

    pub fn name(&self, id: BindingId) -> Option<&BindingName> {
        self.names.get(id.0)
    }

    pub fn required_name(&self, id: BindingId) -> &BindingName {
        self.name(id)
            .expect("BindingId should come from this BindingTable")
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
        /// for `import { j as a } from "..."`).
        imported_name: BindingName,
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
        public_name: BindingName,
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
    pub rename_map: HashMap<BindingName, BindingName>,
    /// Source-chunk top-level statement ordinals this module claims
    /// as anonymous-statement members (owners with empty
    /// `declared_bindings` that the spec resolves by AST shape via
    /// `spec::LogicalModule::anonymous_statements`). Schedule uses
    /// these to override the otherwise-residual destination of those
    /// owners so cross-destination/cycle checks see the closure as
    /// the materializer will emit it.
    pub anonymous_statement_ordinals: Vec<usize>,
}
