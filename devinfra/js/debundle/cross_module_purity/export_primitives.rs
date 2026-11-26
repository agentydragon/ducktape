use analysis_model::PrimKinds;
use analysis_vocabulary::inputs::PrimitiveExportCell;
use inference::{FactContext, IterativeInference, IterativeRules};
use std::collections::{BTreeMap, BTreeSet};
use value_flow::program_bindings::{ExportId, ProgramBindings, ResolvedImport, SourceModuleKey};

/// A stable export after its imports resolved: its own types and the exports
/// it copies.
#[derive(Debug, Clone)]
pub struct ExportValueSource {
    pub kinds: PrimKinds,
    pub copies: Vec<ResolvedImport>,
    /// Present only for a local export binding. Re-export aliases have no
    /// cell of their own and retain the cells from their target instead.
    pub cell: Option<PrimitiveExportCell>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportPrimitiveValue {
    pub kinds: PrimKinds,
    pub source_cells: BTreeSet<PrimitiveExportCell>,
}

/// Close stable export values over copied imports while preserving all local
/// module-cell provenance along re-export and copy chains.
pub fn resolve_export_primitives(
    bindings: &ProgramBindings,
    sources: BTreeMap<(SourceModuleKey, String), ExportValueSource>,
) -> BTreeMap<(SourceModuleKey, String), ExportPrimitiveValue> {
    let rules = ExportPrimitiveRules(
        sources
            .into_iter()
            .map(|((module, export), source)| {
                (
                    bindings
                        .export_id(&module, &export)
                        .expect("prepared primitive export"),
                    PrimitiveSource {
                        kinds: source.kinds,
                        copies: source
                            .copies
                            .into_iter()
                            .map(|copy| {
                                bindings
                                    .export_id(&copy.module, &copy.export)
                                    .expect("prepared primitive dependency")
                            })
                            .collect(),
                        cell: source.cell,
                    },
                )
            })
            .collect(),
    );
    let count = rules.0.len();
    IterativeInference::new(rules)
        .solve_values(0..count)
        .into_iter()
        .map(|(export, fact)| {
            (
                bindings.export_name(export).clone(),
                ExportPrimitiveValue {
                    kinds: fact.kinds,
                    source_cells: fact.source_cells,
                },
            )
        })
        .collect()
}

#[derive(Clone)]
struct ExportPrimitiveFact {
    kinds: PrimKinds,
    source_cells: BTreeSet<PrimitiveExportCell>,
}

struct PrimitiveSource {
    kinds: PrimKinds,
    copies: Vec<ExportId>,
    cell: Option<PrimitiveExportCell>,
}

struct ExportPrimitiveRules(Vec<(ExportId, PrimitiveSource)>);

impl IterativeRules for ExportPrimitiveRules {
    type Key = ExportId;
    type Fact = ExportPrimitiveFact;
    type Rule = usize;

    type MergeScratch = ();
    fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let (export, source) = &self.0[*rule];
        let mut fact = ExportPrimitiveFact {
            kinds: source.kinds,
            source_cells: source.cell.iter().cloned().collect(),
        };
        for copy in &source.copies {
            let Some(copied) = context.read(*copy) else {
                return;
            };
            fact.kinds = fact.kinds.union(copied.kinds);
            fact.source_cells
                .extend(copied.source_cells.iter().cloned());
        }
        context.propose(
            *export,
            ExportPrimitiveFact {
                kinds: fact.kinds,
                source_cells: fact.source_cells,
            },
        );
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        let kinds = current.kinds.union(contribution.kinds);
        let changed = kinds != current.kinds
            || contribution
                .source_cells
                .iter()
                .any(|cell| !current.source_cells.contains(cell));
        current.kinds = kinds;
        current.source_cells.extend(contribution.source_cells);
        changed
    }
}
