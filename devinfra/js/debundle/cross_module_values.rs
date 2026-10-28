//! Positive primitive-result facts across resolved import and re-export edges.
//!
//! Source classification supplies direct seeds and candidate dependencies.
//! Unsupported cycles have no seed and remain unknown. Function purity and
//! runtime interpretation consume these facts without changing the candidates.

use crate::cross_module_purity::{ModuleKey, ResolvedImport};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;

struct PrimitiveBindingProducer {
    binding: Id,
    requirements: BTreeSet<Id>,
}

struct PrimitiveBindingRules<'a> {
    producers: Vec<PrimitiveBindingProducer>,
    imported_bindings: &'a BTreeMap<Id, String>,
}

impl IterativeRules for PrimitiveBindingRules<'_> {
    type Key = Id;
    type Fact = BTreeSet<String>;
    type Rule = usize;

    fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Id, BTreeSet<String>>) {
        let producer = &self.producers[*rule];
        let mut imports = BTreeSet::new();
        for requirement in &producer.requirements {
            if let Some(local) = self.imported_bindings.get(requirement) {
                imports.insert(local.clone());
            } else if let Some(dependencies) = context.read(requirement.clone()) {
                imports.extend(dependencies.iter().cloned());
            } else {
                return;
            }
        }
        context.propose(producer.binding.clone(), imports);
    }

    fn merge(_: &Id, current: &mut BTreeSet<String>, contribution: BTreeSet<String>) -> bool {
        let previous_len = current.len();
        current.extend(contribution);
        current.len() != previous_len
    }
}

/// Close local primitive-result bindings over earlier local bindings and
/// direct imports. The import-name sets are retained for the cross-module
/// export closure, which decides whether each imported value is primitive.
pub fn resolve_primitive_binding_import_dependencies(
    requirements: BTreeMap<Id, BTreeSet<Id>>,
    imported_bindings: &BTreeMap<Id, String>,
) -> BTreeMap<Id, BTreeSet<String>> {
    let producers: Vec<_> = requirements
        .into_iter()
        .map(|(binding, requirements)| PrimitiveBindingProducer {
            binding,
            requirements,
        })
        .collect();
    let count = producers.len();
    IterativeInference::new(PrimitiveBindingRules {
        producers,
        imported_bindings,
    })
    .solve(0..count)
    .into_values()
}

#[derive(Clone, Eq, Ord, PartialEq, PartialOrd)]
struct ExportKey {
    module: ModuleKey,
    export: String,
}

impl From<ResolvedImport> for ExportKey {
    fn from(import: ResolvedImport) -> Self {
        Self {
            module: import.module,
            export: import.export,
        }
    }
}

struct PrimitiveExportProducer {
    target: ExportKey,
    dependencies: Vec<ExportKey>,
}

struct PrimitiveExportRules(Vec<PrimitiveExportProducer>);

impl IterativeRules for PrimitiveExportRules {
    type Key = ExportKey;
    type Fact = ();
    type Rule = usize;

    fn evaluate(&self, rule: &usize, context: &mut impl FactContext<ExportKey, ()>) {
        let producer = &self.0[*rule];
        if producer
            .dependencies
            .iter()
            .all(|key| context.read(key.clone()).is_some())
        {
            context.propose(producer.target.clone(), ());
        }
    }

    fn merge(_: &ExportKey, _: &mut (), _: ()) -> bool {
        false
    }
}

/// Close source-supported primitive results without interpreting their value
/// or certifying that evaluating their initializer is pure.
pub fn resolve_primitive_exports(
    mut direct: BTreeMap<ModuleKey, BTreeSet<String>>,
    imports: BTreeMap<(ModuleKey, String), Vec<ResolvedImport>>,
    reexports: BTreeMap<(ModuleKey, String), ResolvedImport>,
) -> BTreeMap<ModuleKey, BTreeSet<String>> {
    let producers: Vec<_> = imports
        .into_iter()
        .map(|((module, export), dependencies)| PrimitiveExportProducer {
            target: ExportKey { module, export },
            dependencies: dependencies.into_iter().map(ExportKey::from).collect(),
        })
        .chain(reexports.into_iter().map(|((module, export), dependency)| {
            PrimitiveExportProducer {
                target: ExportKey { module, export },
                dependencies: vec![dependency.into()],
            }
        }))
        .collect();
    let count = producers.len();
    let mut inference = IterativeInference::new(PrimitiveExportRules(producers));
    for (module, exports) in &direct {
        for export in exports {
            inference.seed(
                ExportKey {
                    module: module.clone(),
                    export: export.clone(),
                },
                (),
            );
        }
    }
    for key in inference.solve(0..count).into_values().into_keys() {
        direct.entry(key.module).or_default().insert(key.export);
    }
    direct
}

#[cfg(test)]
mod tests {
    use super::resolve_primitive_binding_import_dependencies;
    use std::collections::{BTreeMap, BTreeSet};
    use swc_common::SyntaxContext;
    use swc_ecma_ast::Id;

    fn id(name: &str) -> Id {
        (name.into(), SyntaxContext::empty())
    }

    #[test]
    fn local_primitive_import_dependencies_follow_backward_chains_and_leave_cycles_unknown() {
        let requirements = BTreeMap::from([
            (id("a"), BTreeSet::from([id("z")])),
            (id("cycle_a"), BTreeSet::from([id("cycle_b")])),
            (id("cycle_b"), BTreeSet::from([id("cycle_a")])),
            (id("literal"), BTreeSet::new()),
            (id("z"), BTreeSet::from([id("source_import")])),
        ]);
        let imported = BTreeMap::from([(id("source_import"), "source_local".to_string())]);

        let closed = resolve_primitive_binding_import_dependencies(requirements, &imported);

        let imported_dependency = BTreeSet::from(["source_local".to_string()]);
        assert_eq!(closed.get(&id("a")), Some(&imported_dependency));
        assert_eq!(closed.get(&id("z")), Some(&imported_dependency));
        assert_eq!(closed.get(&id("literal")), Some(&BTreeSet::new()));
        assert!(!closed.contains_key(&id("cycle_a")));
        assert!(!closed.contains_key(&id("cycle_b")));
    }
}
