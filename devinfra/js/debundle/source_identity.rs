//! Source and chunk identities used by the debundler.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

/// Interned chunk identifier. Created by `ChunkTable::intern` during chunk
/// loading and used throughout the pipeline in place of `String` chunk names.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ChunkId(pub usize);

/// Identity of one declarative module binding cell in a source chunk.
/// `declaration_origin` is an AST source offset; `chunk` scopes it to the
/// chunk where that declaration was found. This is a runtime identity only:
/// persisted facts encode the checked chunk name instead of the interned ID.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct ModuleCellId {
    pub chunk: ChunkId,
    pub declaration_origin: u32,
}

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

    pub fn try_name(&self, id: ChunkId) -> Option<&str> {
        self.names.get(id.0).map(String::as_str)
    }

    pub fn len(&self) -> usize {
        self.names.len()
    }

    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }
}

/// Position of an original item in a parsed chunk's `Module.body`.
/// Several movable source units may originate from the same statement.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct StatementId(pub usize);

/// Position in the source-unit sequence after splitting comma declarations.
/// This is a source coordinate, not a graph's dense `SourceUnitId`: a graph
/// reconstructed from a report may allocate its nodes in another order.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SourceUnitOrdinal(pub usize);

/// Dense in-graph identity of one dependency-graph source unit. A source unit is
/// one post-comma-list `StatementFacts` row, so it is not necessarily one
/// original AST statement. Unlike [`SourceUnitOrdinal`], this identifies a
/// node in a particular graph; reconstructing a graph from a report may assign
/// different IDs to the same source units.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SourceUnitId(pub usize);

impl std::fmt::Display for StatementId {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(formatter)
    }
}

impl std::fmt::Display for SourceUnitOrdinal {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(formatter)
    }
}
