//! Shared test-only helpers for building owner-graph wire fixtures.
//!
//! The owner-graph report interns module identity: a reference is a
//! [`ModuleKey`] (`"logical:N"`) and each module's canonical
//! [`spec::ModulePath`] + residual flag live once in the module table
//! (`OwnerGraphQuotientReport::nodes`, a list of [`ModuleEntry`]). Tests
//! across several crates build that table the same way, so the builders
//! live here instead of being copy-pasted per test module.
//!
//! Convention: a test destination key's string **is** the module's
//! canonical path (e.g. `"residual"`, `"ui/plugins"`), so [`module_table`]
//! recovers the path by parsing the key and derives `residual` from
//! [`spec::ModulePath::is_residual`].

use std::collections::BTreeSet;

use analysis::{ModuleEntry, ModuleKey};
use spec::ModulePath;

/// A destination key whose string is the module's canonical path.
pub fn module_ref(path: &str) -> ModuleKey {
    ModuleKey(path.to_string())
}

/// The module-table entry for a single destination key: the canonical
/// `ModulePath` (parsed from the key) and its residual flag.
pub fn module_entry(key: &ModuleKey) -> ModuleEntry {
    let path = ModulePath::parse(key.as_str(), "").expect("test module key is a valid path");
    let residual = path.is_residual();
    ModuleEntry {
        key: key.clone(),
        path,
        residual,
    }
}

/// Build the module table (`quotient.nodes`) from a set of destination
/// keys, deduplicating. The single source of truth for each module's
/// path + residual flag in a test fixture.
pub fn module_table<'a>(keys: impl IntoIterator<Item = &'a ModuleKey>) -> Vec<ModuleEntry> {
    let mut seen = BTreeSet::new();
    keys.into_iter()
        .filter(|key| seen.insert((*key).clone()))
        .map(module_entry)
        .collect()
}
