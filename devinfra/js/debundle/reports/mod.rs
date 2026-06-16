pub mod schema;

use std::collections::BTreeSet;

use crate::graph::{OwnerEdgeId, OwnerId};
use crate::ids::{LogicalModuleIndex, ModuleId};
use crate::reports::schema::ModuleKey;

/// The shared "modules in the SCC + the edges in the SCC" shape — the
/// one core carried, in different encodings, by every type describing
/// an unrealizable / cyclic module-quotient SCC:
///
/// - `gate::realizability::SccDiagnosis` — the in-memory primitive:
///   this core plus an `SccRejection` decoration. Carries it verbatim.
/// - `gate::validation::CycleReport` — the validator's rendered
///   projection: module names stringified to `ModulePath`, plus FAS
///   `cut` / `lazy_closure` decorations.
/// - [`schema::QuotientSccReport`] — the wire projection: wire-stable
///   string ids in place of typed [`ModuleId`] / [`OwnerEdgeId`],
///   covering every dep-graph SCC (not only the unrealizable ones).
///
/// The rendered/wire projections re-encode these two fields with their
/// own id spelling and decorations, so they point here rather than
/// re-describing the shape. [`crate::factor_assembly::AtomicUnitConflict`]
/// is the atom-level sibling — same "unrealizable, here's why" framing
/// but a different domain (owners in an atomic unit, not modules in an
/// SCC), so it does not reuse this core.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SccCore {
    /// Modules participating in the cycle.
    pub modules: BTreeSet<ModuleId>,
    /// Constraining cross-module owner-edge evidence backing the SCC,
    /// in stable [`OwnerEdgeId`] order.
    pub constraining_owner_edges: Vec<OwnerEdgeId>,
}

pub fn owner_key(id: OwnerId) -> String {
    format!("owner:{}", id.0)
}

pub fn module_key(id: ModuleId) -> ModuleKey {
    let LogicalModuleIndex(idx) = id.0;
    ModuleKey(format!("logical:{idx}"))
}

pub fn atomic_unit_key(idx: usize) -> String {
    format!("atomic:{idx}")
}

pub fn module_id_from_key(key: &ModuleKey) -> Option<ModuleId> {
    key.as_str()
        .strip_prefix("logical:")
        .and_then(|idx| idx.parse::<usize>().ok())
        .map(|idx| ModuleId(LogicalModuleIndex(idx)))
}
