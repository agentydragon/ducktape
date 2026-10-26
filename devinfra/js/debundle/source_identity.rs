//! Source coordinates within one chunk, independent of graph node allocation.

use serde::{Deserialize, Serialize};

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
