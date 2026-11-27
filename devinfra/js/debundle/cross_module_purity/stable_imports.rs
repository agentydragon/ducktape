use super::module_facts::ModulePurityFacts;
use inference::{FactContext, IterativeInference, IterativeRules};
use std::collections::{BTreeMap, BTreeSet};
use value_flow::program_bindings::{ExportId, ProgramBindings, SourceModuleKey};

/// Per module, the named and default imports bound to a provider cell that is
/// never rebound (`AnalysisInputs::stable_imports`): the export resolves,
/// through local re-exports of imports, to a top-level binding of an analyzed
/// module that is declared once and never written, or to an export an author
/// asserted pure (`asserted_pure`, the `resolve_imported_purities` axioms).
/// Every other import, including one of a module outside the program, stays
/// a cell its provider can change.
pub fn resolve_stable_imports(
    bindings: &ProgramBindings,
    modules: &BTreeMap<SourceModuleKey, ModulePurityFacts<'_>>,
    asserted_pure: &BTreeMap<SourceModuleKey, BTreeSet<String>>,
) -> BTreeMap<SourceModuleKey, BTreeSet<String>> {
    let never_rebound: BTreeMap<_, BTreeSet<_>> = modules
        .iter()
        .map(|(module, facts)| {
            (
                module,
                purity::immutable_declarations(facts.body, |_| true)
                    .into_iter()
                    .map(|(name, _)| name.to_string())
                    .collect(),
            )
        })
        .collect();
    let asserted_or_immutable = bindings
        .exports()
        .filter(|export| {
            let (module, name) = bindings.export_name(*export);
            asserted_pure
                .get(module)
                .is_some_and(|exports| exports.contains(name))
                || bindings.local_export(*export).is_some_and(|local| {
                    let (module, local) = bindings.binding_name(local);
                    never_rebound
                        .get(module)
                        .is_some_and(|names| names.contains(local))
                })
        })
        .collect();
    let rules = StableImportRules {
        bindings,
        asserted_or_immutable,
    };
    let solved = IterativeInference::new(rules).solve_values(bindings.exports());
    let mut stable_imports: BTreeMap<_, BTreeSet<_>> = bindings
        .modules()
        .map(|module| (module.clone(), BTreeSet::new()))
        .collect();
    for (export, stable) in solved {
        if stable {
            for local in bindings.importers(export) {
                let (module, name) = bindings.binding_name(*local);
                stable_imports
                    .get_mut(module)
                    .expect("prepared importer")
                    .insert(name.clone());
            }
        }
    }
    stable_imports
}

/// Stability is a separate growing judgment over shared source identities.
/// Missing facts are false; only an assertion, immutable cell or stable local
/// re-export can establish it. Direct source re-exports remain unsupported.
pub(super) struct StableImportRules<'a> {
    pub(super) bindings: &'a ProgramBindings,
    pub(super) asserted_or_immutable: BTreeSet<ExportId>,
}

impl IterativeRules for StableImportRules<'_> {
    type Key = ExportId;
    type Fact = bool;
    type Rule = ExportId;

    type MergeScratch = ();
    fn evaluate(&self, rule: &ExportId, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        if self.asserted_or_immutable.contains(rule) {
            context.propose(*rule, true);
            return;
        }
        let Some(provider) = self.bindings.reexport_target(*rule) else {
            return;
        };
        if context.read(provider).copied().unwrap_or(false) {
            context.propose(*rule, true);
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let changed = !*current && contribution;
        *current |= contribution;
        changed
    }
}
