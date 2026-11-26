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

/// Why a `commuting_statements` entry inerts nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum UnusedPremiseReason {
    /// No `sequenced` edge joins the two source units.
    NoSequencedEdge,
    /// Every `sequenced` edge between them orders two summarized effects on
    /// disjoint cells (the strict S-chain's adjacency): the analysis already
    /// knows the pair commutes and `dataflow_aware_s_chain` drops the edge
    /// without any premise.
    SummarizedEndpoints,
}

impl UnusedPremiseReason {
    /// Clause completing the diagnostic's endpoint sentence.
    pub fn explanation(self) -> &'static str {
        match self {
            Self::NoSequencedEdge => "are already unordered",
            Self::SummarizedEndpoints => {
                "have summarized effects on disjoint cells, which `dataflow_aware_s_chain` already leaves unordered"
            }
        }
    }
}

/// A `commuting_statements` entry whose endpoints resolve but which inerts
/// no edge: the premise buys nothing. A warning, never a failure — an engine
/// improvement that makes a premise unnecessary must not reject a spec that
/// passed (SPEC.md § Commuting statements). Identifies the entry by what the
/// author wrote, not by analysis ordinals or input binding spellings.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, thiserror::Error)]
#[error(
    "{spec_location} inerts no sequenced edge: endpoints {} and {} {}; the entry is \
     removable — delete it in a reviewed spec edit and revalidate",
    .a,
    .b,
    .reason.explanation()
)]
pub struct CommutingStatementsUnused {
    /// `<spec path>:chunk_analysis_options.<chunk>.commuting_statements[<i>]`.
    pub spec_location: String,
    pub a: UnusedCommutingEndpoint,
    pub b: UnusedCommutingEndpoint,
    pub reason: UnusedPremiseReason,
}

/// One endpoint of an unused `commuting_statements` entry, by authored
/// identity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UnusedCommutingEndpoint {
    /// The endpoint selector exactly as authored.
    pub selector: spec::SourceUnitSelector,
    /// Authored module/member assignments that resolve to this endpoint.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub logical_members: Vec<spec::ResolvedMemberSelector>,
    /// Source location of the endpoint in this run's input snapshot.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
}

impl UnusedCommutingEndpoint {
    pub(super) fn new(nodes: &[SourceUnit], endpoint: &CommutingEndpoint) -> Self {
        Self {
            selector: endpoint.selector.clone(),
            logical_members: endpoint.logical_members.clone(),
            source_location: source_unit_location(nodes, endpoint.source_unit),
        }
    }
}

impl fmt::Display for UnusedCommutingEndpoint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let members = self
            .logical_members
            .iter()
            .map(|member| format!("module `{}` member `{}`", member.module, member.name))
            .collect::<Vec<_>>();
        match (members.as_slice(), &self.selector) {
            ([member], _) => f.write_str(member)?,
            ([], spec::SourceUnitSelector::ResolvedMember(member)) => {
                write!(f, "module `{}` member `{}`", member.module, member.name)?;
            }
            ([], spec::SourceUnitSelector::SourceMatch(claim)) => {
                let source = claim.match_source.split_whitespace().collect::<Vec<_>>();
                let mut preview = source.join(" ");
                if preview.chars().count() > 60 {
                    preview = preview.chars().take(57).collect::<String>() + "...";
                }
                write!(f, "source_match `{preview}`")?;
            }
            (members, _) => write!(f, "members [{}]", members.join(", "))?,
        }
        if let Some(location) = &self.source_location {
            write!(f, " at {}:{}", location.source_path, location.start_line)?;
            if let Some(column) = location.start_column {
                write!(f, ":{column}")?;
            }
        }
        Ok(())
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
