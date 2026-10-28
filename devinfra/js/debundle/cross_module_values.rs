//! Positive primitive-result facts across resolved import and re-export edges.
//!
//! Source classification supplies direct seeds and candidate dependencies.
//! Unsupported cycles have no seed and remain unknown. Function purity and
//! runtime interpretation consume these facts without changing the candidates.

use crate::cross_module_purity::{ModuleKey, ResolvedImport};
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use std::collections::{BTreeMap, BTreeSet};

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
