use super::*;

/// Multiple declarations of one binding are supported only when every site
/// declares `var`. Other duplicate declaration shapes remain rejected.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error(
    "duplicate top-level declaration of binding `{binding}`: statements #{} and #{} both \
     declare it; only repeated `var` declarations are supported.",
    .first.0,
    .second.0
)]
pub struct DuplicateTopLevelDeclaration {
    pub binding: swc_atoms::Atom,
    pub first: SourceUnitOrdinal,
    pub second: SourceUnitOrdinal,
}

/// A derived source fact an author `commuting_statements` premise contradicts.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, strum::Display)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum KnownConflict {
    /// The retained cause of a `Sequenced` edge derived from both
    /// endpoints' recorded effects (a cell relation, a binding timeline, a
    /// shared `var`, an uninitialized observation).
    #[strum(to_string = "{0}")]
    Sequenced(SequencedCause),
    /// The two summaries have overlapping cell footprints with at least one write, whatever
    /// rule emitted the edge between them.
    #[strum(to_string = "both summaries have an overlapping cell effect: {overlap}")]
    SharedCell {
        cell: EffectCellReport,
        overlap: CellOverlapReport,
    },
    /// Both source units declare the shared `var` `binding`.
    #[strum(to_string = "both declare shared `var` `{binding}`")]
    CoDeclaration { binding: swc_atoms::Atom },
    /// A `Sequenced` edge without a retained cause (never produced by this
    /// builder); treated as a conflict so the premise never widens silently.
    #[strum(to_string = "side-effect ordering of unclassified cause")]
    Unclassified,
}

/// One dependency edge `from -> to` between the premise's endpoints whose
/// cause the premise contradicts.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContradictingEdge {
    pub from: SourceUnitOrdinal,
    pub to: SourceUnitOrdinal,
    pub conflict: KnownConflict,
}

/// A `commuting_statements` entry whose endpoints have a modeled effect-set
/// conflict (SPEC.md § Commuting statements).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, thiserror::Error)]
#[error(
    "{spec_location} contradicts a source fact: source units {} and {} have a modeled effect conflict: {}",
    source_unit_label(*.a, .a_binding),
    source_unit_label(*.b, .b_binding),
    .conflicts
        .iter()
        .map(|edge| format!("#{} -> #{}: {}", edge.from.0, edge.to.0, edge.conflict))
        .collect::<Vec<_>>()
        .join("; ")
)]
pub struct CommutingStatementsContradiction {
    pub spec_location: String,
    pub a: SourceUnitOrdinal,
    pub a_binding: Option<String>,
    pub b: SourceUnitOrdinal,
    pub b_binding: Option<String>,
    pub conflicts: Vec<ContradictingEdge>,
}

pub(super) fn source_unit_label(ordinal: SourceUnitOrdinal, binding: &Option<String>) -> String {
    match binding {
        Some(binding) => format!("#{} (`{binding}`)", ordinal.0),
        None => format!("#{} (anonymous statement)", ordinal.0),
    }
}

pub(super) fn source_unit_location(
    nodes: &[SourceUnit],
    ordinal: SourceUnitOrdinal,
) -> Option<SourceLocation> {
    nodes
        .iter()
        .find(|node| node.statement_ordinal == ordinal)
        .and_then(|node| node.source_location.clone())
}

pub(super) fn unused_commuting_endpoint(
    nodes: &[SourceUnit],
    endpoint: &CommutingEndpoint,
) -> UnusedCommutingEndpoint {
    UnusedCommutingEndpoint {
        selector: endpoint.selector.clone(),
        logical_members: endpoint.logical_members.clone(),
        source_location: source_unit_location(nodes, endpoint.source_unit),
    }
}

/// The typed diagnostic of a `commuting_statements` entry the graph build
/// rejected, as `spec validate` reports it beside the selector outcomes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, strum::Display)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CommutingStatementsDiagnostic {
    #[strum(to_string = "{0}")]
    Contradicted(CommutingStatementsContradiction),
    #[strum(to_string = "{0}")]
    Unused(Box<CommutingStatementsUnused>),
}

/// Graph construction is intentionally source-free. Captured value and native
/// author inputs must be resolved by the prepared whole-program AST/index
/// pipeline before callers extract StatementFacts and build this graph.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DependencyGraphBuildError {
    #[error(transparent)]
    DuplicateTopLevelDeclaration(DuplicateTopLevelDeclaration),
    #[error(transparent)]
    CommutingStatementsContradicted(CommutingStatementsContradiction),
}

impl DependencyGraphBuildError {
    /// The rejected `commuting_statements` entry, for the variant that is an
    /// author-premise diagnostic rather than a pipeline fault.
    pub fn commuting_statements_contradiction(&self) -> Option<&CommutingStatementsContradiction> {
        match self {
            Self::CommutingStatementsContradicted(error) => Some(error),
            Self::DuplicateTopLevelDeclaration(_) => None,
        }
    }
}

impl From<DuplicateTopLevelDeclaration> for DependencyGraphBuildError {
    fn from(error: DuplicateTopLevelDeclaration) -> Self {
        Self::DuplicateTopLevelDeclaration(error)
    }
}
