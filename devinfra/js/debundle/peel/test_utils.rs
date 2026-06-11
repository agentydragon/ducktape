use analysis::BindingReport;

// Owner-graph wire-fixture builders (the module table + interned
// `ModuleKey` references) are shared across crates; re-export them so
// peel's tests keep calling `test_utils::module_ref` / `module_table`.
pub use report_fixtures::{module_ref, module_table};

pub fn member(binding: &str, export_name: &str) -> BindingReport {
    BindingReport {
        binding: binding.into(),
        export_name: export_name.into(),
    }
}

pub fn binding(name: &str) -> BindingReport {
    member(name, name)
}
