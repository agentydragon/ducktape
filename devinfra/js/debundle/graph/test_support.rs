use analysis::StatementFacts;

pub(crate) fn build_dependency_graph(
    facts: &[StatementFacts],
) -> Result<crate::DependencyGraph, crate::DependencyGraphBuildError> {
    crate::build_dependency_graph_with(facts, crate::GraphOptions::default(), &[])
        .map(|(graph, _)| graph)
}
