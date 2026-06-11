pub mod schema;

use crate::graph::OwnerId;
use crate::ids::{LogicalModuleIndex, ModuleId};
use crate::reports::schema::ModuleKey;

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
