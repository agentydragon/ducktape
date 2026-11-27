//! The `propose-commutations` report: proposals, rejections, and per-SCC outcomes.

use ::gate::CycleEdge;
use analysis_model::DepKind;
use analysis_vocabulary::execution_evidence::SourceLocation;
use graph::{KnownConflict, SequencedCause};
use serde::Serialize;
use spec::ModulePath;
use spec::input_contracts::CommutingStatementsContract;
use std::path::PathBuf;
use swc_atoms::Atom;
use synthesis_failure::SynthesisSkipReason;

#[derive(Debug, Serialize)]
pub struct ProposeCommutationsReport {
    pub chunk: String,
    pub graph: PathBuf,
    pub cycles: PathBuf,
    /// One row per blocking SCC: whether its proposals together break it,
    /// and, when they cannot, the edges that keep it cyclic. Never limited.
    pub sccs: Vec<SccOutcome>,
    /// Size of each list before `--limit` cut it.
    pub totals: ReportTotals,
    /// Ranked best first; `rank` is the 1-based position.
    pub proposals: Vec<CommutationProposal>,
    /// Cut edges a premise can never inert: at-init reads (`eager_use`) and
    /// the other non-`sequenced` kinds. The remedy is co-location.
    pub needs_co_location: Vec<CoLocationNeed>,
    /// Pairs the gate would reject and pairs without a unique selector; with
    /// `--all`, every constraining `sequenced` edge inside a blocking SCC
    /// lands either here or in `proposals`.
    pub rejected: Vec<RejectedCandidate>,
    /// The `chunk_analysis_options` YAML fragment with every proposal, in
    /// rank order, `note` left for the author.
    pub draft_yaml: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub draft_written: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct ReportTotals {
    /// Pairs the gate would accept, deduplicated across nested SCCs. A pair
    /// past the limit is counted without proving its declared-binding
    /// endpoints' selectors.
    pub proposals: usize,
    pub rejected: usize,
    pub needs_co_location: usize,
}

#[derive(Debug, Serialize)]
pub struct CommutationProposal {
    pub rank: usize,
    /// The SCC the pair ranks best in; its `sccs` row lists its modules.
    /// `cut_edges`, `single_pair_cut` and `remaining_module_pair_edges`
    /// describe this SCC.
    pub scc_id: usize,
    /// Other blocking SCCs (nested ones) the pair also lies in.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub also_in_sccs: Vec<usize>,
    /// Indices into the SCC's `cut` this pair's edges cover; empty when the
    /// gate's feedback arc set cut the SCC on another arc.
    pub cut_edges: Vec<usize>,
    pub rank_reason: RankReason,
    /// Inerting this pair alone leaves the SCC's constraining subgraph
    /// acyclic (gate view, every parallel unit edge counted).
    pub single_pair_cut: bool,
    /// Constraining unit edges still joining the cut edge's module pair
    /// after this pair is inerted.
    pub remaining_module_pair_edges: usize,
    pub entry: CommutingStatementsContract,
    pub a: ProposedEndpoint,
    pub b: ProposedEndpoint,
    pub inerted_edges: Vec<InertedEdge>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum RankReason {
    /// This pair alone breaks the SCC.
    SinglePairCut,
    /// The SCC stays cyclic; ranked by the parallel edges left on its
    /// module pair.
    PartialCut,
}

impl RankReason {
    pub(super) fn explanation(self) -> &'static str {
        match self {
            Self::SinglePairCut => "single-pair cut",
            Self::PartialCut => "partial cut",
        }
    }
}

#[derive(Debug, Serialize)]
pub struct ProposedEndpoint {
    pub source_unit: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<Atom>,
    pub module: ModulePath,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    /// First source line of the source unit (its own declarator in a
    /// comma-list declaration), truncated.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub excerpt: Option<String>,
    pub selector_origin: SelectorOrigin,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SelectorOrigin {
    /// A logical member of the `--modules` tree, named by module and export.
    ResolvedMember,
    /// A `source_match` synthesized and proven alone with the shared resolve.
    SynthesizedSourceMatch,
}

#[derive(Debug, Serialize)]
pub struct InertedEdge {
    pub edge: String,
    pub from: String,
    pub to: String,
    pub explanation: SequencedCause,
    pub explanation_text: String,
}

#[derive(Debug, Serialize)]
pub struct CoLocationNeed {
    pub scc_id: usize,
    pub cut_index: usize,
    pub edge: CycleEdge,
}

#[derive(Debug, Serialize)]
pub struct RejectedCandidate {
    pub scc_id: usize,
    /// Other blocking SCCs (nested ones) the pair also lies in.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub also_in_sccs: Vec<usize>,
    /// Indices into the SCC's `cut` this pair's edges cover; empty for a
    /// pair whose edges the feedback arc set did not pick.
    pub cut_edges: Vec<usize>,
    /// Source units of the candidate pair; absent when a cut edge matched
    /// no graph edge.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub a: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub b: Option<String>,
    #[serde(flatten)]
    pub reason: RejectionReason,
}

#[derive(Debug, Serialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum RejectionReason {
    /// The gate would reject the pair as contradicting a source fact.
    KnownConflict { conflicts: Vec<ConflictingEdge> },
    /// Every edge between the pair is strict-chain adjacency of two
    /// summarized effects; the gate would reject the pair as unused.
    UnusedSummarized,
    /// The graph predates retained verdicts; regenerate it.
    VerdictNotRecorded { edge: String },
    /// No selector proves `endpoint` alone; `detail` is the nearest miss.
    NoUniqueSelector {
        endpoint: String,
        detail: EndpointSelectorFailure,
    },
    /// `cycles.json` and `dependency_graph.json.gz` disagree.
    CutEdgeNotInGraph,
}

#[derive(Debug, Serialize)]
pub struct ConflictingEdge {
    pub edge: String,
    pub from: String,
    pub to: String,
    pub conflict: KnownConflict,
}

#[derive(Debug, Serialize)]
pub struct SccOutcome {
    pub scc_id: usize,
    pub modules: Vec<ModulePath>,
    /// Declared-binding endpoints of this SCC's pairs past `--limit`, whose
    /// selectors were assumed rather than proven. A failed selector only
    /// keeps a pair's edges constraining: `pairs_cannot_break` holds
    /// whatever they prove, but its counts are then a lower bound and
    /// `pairs_break` is conditional on them. `--all` proves every one.
    pub unproven_endpoints: usize,
    #[serde(flatten)]
    pub verdict: SccVerdict,
}

#[derive(Debug, Serialize)]
#[serde(tag = "verdict", rename_all = "snake_case")]
pub enum SccVerdict {
    /// Inerting every proposal of the SCC together leaves its constraining
    /// subgraph acyclic.
    PairsBreak,
    /// Cycles survive every proposal of the SCC inerted together: pairs alone
    /// cannot break it.
    PairsCannotBreak {
        /// Preservation stopped once a cycle was proved. Counts and cyclic
        /// memberships are then lower bounds, not a complete final inventory.
        counts_are_lower_bounds: bool,
        /// Modules still on a constraining cycle.
        residual_modules: usize,
        /// Remaining nontrivial cyclic components, ordered by their sorted
        /// canonical module paths.
        residual_components: Vec<ResidualComponent>,
        /// The constraining edges between those modules, by kind and by why
        /// no proposal inerts them; largest count first.
        blocking_edges: Vec<BlockingEdgeCount>,
    },
}

#[derive(Debug, Serialize)]
pub struct ResidualComponent {
    /// Canonical module paths in this component, sorted lexicographically.
    pub modules: Vec<ModulePath>,
}

#[derive(Debug, Serialize)]
pub struct BlockingEdgeCount {
    pub kind: DepKind,
    pub reason: BlockingReason,
    pub count: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum BlockingReason {
    /// A third-statement ordering survives the proposed pair assertions.
    PreservedOrder,
    /// A premise never inerts this kind (`eager_use` and the other
    /// non-`sequenced` kinds); the remedy is co-location.
    KindNotInertable,
    /// The pair's premise would contradict a source fact.
    KnownConflict,
    /// Strict-chain adjacency of two summarized effects; a premise inerts
    /// nothing.
    Summarized,
    /// The graph predates retained verdicts.
    VerdictNotRecorded,
    /// An endpoint of the pair has no unique selector.
    NoUniqueSelector,
}

impl BlockingReason {
    pub(super) fn explanation(self) -> &'static str {
        match self {
            Self::PreservedOrder => "third-statement ordering survives the proposed pairs",
            Self::KindNotInertable => "no premise inerts this kind; co-locate",
            Self::KnownConflict => "the pair's premise contradicts a source fact",
            Self::Summarized => "summarized effects; a premise inerts nothing",
            Self::VerdictNotRecorded => "no retained premise verdict; regenerate the graph",
            Self::NoUniqueSelector => "an endpoint has no unique selector",
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EndpointSelectorFailure {
    OutsideParsedSource {
        source_unit: String,
        statement_ordinal: usize,
    },
    WrongBodyIndex {
        actual: usize,
        expected: Option<usize>,
    },
    Synthesis {
        reason: SynthesisSkipReason<selector_outcome::InvalidSelectorReport>,
    },
}

impl RejectionReason {
    /// The blocking reason a rejected pair gives its edges; `None` for a cut
    /// row that names no pair.
    pub(super) fn blocking_reason(&self) -> Option<BlockingReason> {
        match self {
            Self::KnownConflict { .. } => Some(BlockingReason::KnownConflict),
            Self::UnusedSummarized => Some(BlockingReason::Summarized),
            Self::VerdictNotRecorded { .. } => Some(BlockingReason::VerdictNotRecorded),
            Self::NoUniqueSelector { .. } => Some(BlockingReason::NoUniqueSelector),
            Self::CutEdgeNotInGraph => None,
        }
    }
}
