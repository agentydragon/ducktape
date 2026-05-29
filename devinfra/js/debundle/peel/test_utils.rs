use analysis::{BindingReport, ModuleEntry, ModuleKey};
use spec::ModulePath;

pub fn member(binding: &str, export_name: &str) -> BindingReport {
    BindingReport {
        binding: binding.into(),
        export_name: export_name.into(),
    }
}

pub fn binding(name: &str) -> BindingReport {
    member(name, name)
}

/// A destination key for tests. By convention the test key string is
/// the module's canonical path (e.g. `"residual"`, `"ui/x"`), so the
/// module table built by `module_table` can recover the path directly.
pub fn module_ref(id: &str, _residual: bool) -> ModuleKey {
    ModuleKey(id.to_string())
}

/// Build the module-table (`quotient.nodes`) entries for a set of
/// destination keys. Tests use the key string as the path, so each
/// entry's `path` is `ModulePath::parse(key)` and `residual` is
/// derived from `ModulePath::is_residual` — matching production, where
/// the table is the single source of truth for path + residual.
pub fn module_table<'a>(keys: impl IntoIterator<Item = &'a ModuleKey>) -> Vec<ModuleEntry> {
    let mut seen = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for key in keys {
        if !seen.insert(key.clone()) {
            continue;
        }
        let path = ModulePath::parse(key.as_str(), "")
            .unwrap_or_else(|e| panic!("test module key {key} is not a valid path: {e}"));
        let residual = path.is_residual();
        out.push(ModuleEntry {
            key: key.clone(),
            path,
            residual,
        });
    }
    out
}
