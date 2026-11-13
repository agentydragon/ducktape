//! Author contracts from `chunk_export_purity`, projected onto importers.
//!
//! An assertion addresses a defining chunk's export. Each analyzed module that
//! imports that export receives the contract under its local binding name.
//! This is axiom projection, not inference: no fixpoint, no source facts.

use analysis::diagnostics::{Diagnostic, Diagnostics, ExportContract};
use std::collections::{BTreeMap, BTreeSet};

use analysis::cross_module_purity::ModulePurityFacts;
use spec::ChunkExportPurity;

/// One `chunk_export_purity` field for every chunk this run analyzes.
///
/// A chunk outside the analyzed import closure (an unselected chunk, or one
/// without a parseable entry) provides no analyzed import, so its assertions
/// are out of scope for this run rather than dangling. Every contract field
/// goes through this one scope so they agree on that policy.
pub(crate) fn scoped<T: Clone>(
    chunk_export_purity: &BTreeMap<String, ChunkExportPurity>,
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    field: impl Fn(&ChunkExportPurity) -> &T,
) -> BTreeMap<String, T> {
    chunk_export_purity
        .iter()
        .filter(|(chunk, _)| modules.contains_key(*chunk))
        .map(|(chunk, assertion)| (chunk.clone(), field(assertion).clone()))
        .collect()
}

/// Project per-export contract values (`export → T`) onto each analyzed
/// importer's local bindings (`local → T`). `asserted` comes from [`scoped`].
/// An assertion naming an export its analyzed chunk lacks records a diagnostic as
/// dangling (likely a typo or a stale entry) and is ignored.
pub(crate) fn project_onto_importers<T: Clone>(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<String, BTreeMap<String, T>>,
    contract: ExportContract,
    diagnostics: &Diagnostics,
) -> BTreeMap<String, BTreeMap<String, T>> {
    for (module, by_export) in asserted {
        for export in by_export.keys() {
            if !modules
                .get(module)
                .is_some_and(|facts| facts.exports.contains_key(export))
            {
                diagnostics.record(
                    module,
                    Diagnostic::DanglingExportContract {
                        contract,
                        export: export.clone(),
                    },
                );
            }
        }
    }
    modules
        .iter()
        .map(|(importer, facts)| {
            let projected = facts
                .imports
                .iter()
                .filter_map(|(local, target)| {
                    let value = asserted.get(&target.module)?.get(&target.export)?;
                    modules
                        .get(&target.module)?
                        .exports
                        .contains_key(&target.export)
                        .then(|| (local.clone(), value.clone()))
                })
                .collect();
            (importer.clone(), projected)
        })
        .collect()
}

/// [`project_onto_importers`] for contracts that name exports without a
/// per-export value; the result is each importer's set of local bindings.
pub(crate) fn project_export_set(
    modules: &BTreeMap<String, ModulePurityFacts<'_>>,
    asserted: &BTreeMap<String, BTreeSet<String>>,
    contract: ExportContract,
    diagnostics: &Diagnostics,
) -> BTreeMap<String, BTreeSet<String>> {
    let as_map = asserted
        .iter()
        .map(|(chunk, exports)| {
            (
                chunk.clone(),
                exports.iter().map(|export| (export.clone(), ())).collect(),
            )
        })
        .collect();
    project_onto_importers(modules, &as_map, contract, diagnostics)
        .into_iter()
        .map(|(importer, locals)| (importer, locals.into_keys().collect()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::cross_module_purity::ResolvedImport;

    fn module(imports: &[(&str, &str, &str)], exports: &[&str]) -> ModulePurityFacts<'static> {
        ModulePurityFacts {
            body: &[],
            imports: imports
                .iter()
                .map(|(local, module, export)| {
                    (
                        (*local).to_string(),
                        ResolvedImport {
                            module: (*module).to_string(),
                            export: (*export).to_string(),
                            source_specifier: format!("./{module}.js"),
                        },
                    )
                })
                .collect(),
            exports: exports
                .iter()
                .map(|export| ((*export).to_string(), (*export).to_string()))
                .collect(),
            source: None,
            replacement_packages: BTreeMap::new(),
        }
    }

    fn purity(fluent_exports: &[&str]) -> ChunkExportPurity {
        ChunkExportPurity {
            fluent_exports: fluent_exports.iter().map(|e| (*e).to_string()).collect(),
            ..ChunkExportPurity::default()
        }
    }

    fn project_fluent(
        modules: &BTreeMap<String, ModulePurityFacts<'_>>,
        configured: &BTreeMap<String, ChunkExportPurity>,
    ) -> BTreeMap<String, BTreeSet<String>> {
        project_export_set(
            modules,
            &scoped(configured, modules, |p| &p.fluent_exports),
            ExportContract::FluentExports,
            &Diagnostics::default(),
        )
    }

    #[test]
    fn asserted_export_projects_onto_importer_local() {
        let modules = BTreeMap::from([
            ("app".to_string(), module(&[("k", "lib", "z")], &[])),
            ("lib".to_string(), module(&[], &["z"])),
        ]);
        let configured = BTreeMap::from([("lib".to_string(), purity(&["z"]))]);
        let projected = project_fluent(&modules, &configured);
        assert_eq!(projected["app"], BTreeSet::from(["k".to_string()]));
    }

    #[test]
    fn assertion_outside_analyzed_closure_or_naming_missing_export_does_not_project() {
        // `vendor` is not analyzed; `lib` lacks the asserted export `gone`.
        let modules = BTreeMap::from([
            (
                "app".to_string(),
                module(&[("v", "vendor", "e4"), ("g", "lib", "gone")], &[]),
            ),
            ("lib".to_string(), module(&[], &["z"])),
        ]);
        let configured = BTreeMap::from([
            ("vendor".to_string(), purity(&["e4"])),
            ("lib".to_string(), purity(&["gone"])),
        ]);
        let projected = project_fluent(&modules, &configured);
        assert!(projected["app"].is_empty());
    }
}
