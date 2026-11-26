use std::collections::BTreeMap;

use swc_common::SyntaxContext;
use swc_ecma_ast::{Id, Module};

use analysis::{AnalysisAssumptions, AnalysisInputs, KnownEffect, StatementFacts};

/// Construct a test binding ID using the empty syntax context. The shared parser
/// does not run SWC's resolver; production chunks use their top-level mark instead.
pub(crate) fn test_id(name: &str) -> Id {
    (name.into(), SyntaxContext::empty())
}

pub(crate) fn hints_with_decorate_helper(name: &str) -> AnalysisInputs {
    AnalysisInputs {
        assumptions: AnalysisAssumptions {
            known_effects: BTreeMap::from([(
                name.to_string(),
                KnownEffect::TypescriptDecorateHelper,
            )]),
            ..AnalysisAssumptions::default()
        },
        ..AnalysisInputs::default()
    }
}

pub(crate) fn analyze_facts_with_hints(
    module: &Module,
    hints: &AnalysisInputs,
) -> Vec<StatementFacts> {
    analyze::analyze_chunk(module, hints, None).facts
}

pub(crate) fn analyze_facts(module: &Module) -> Vec<StatementFacts> {
    analyze_facts_with_hints(module, &AnalysisInputs::default())
}

pub(crate) fn build_dependency_graph(
    facts: &[StatementFacts],
) -> Result<crate::DependencyGraph, crate::DependencyGraphBuildError> {
    crate::build_dependency_graph_with(facts, crate::GraphOptions::default(), &[])
        .map(|(graph, _)| graph)
}
