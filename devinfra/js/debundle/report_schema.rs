use serde::{Deserialize, Serialize};

use crate::purity::Purity;
use crate::{BindingName, DepKind, StatementKind, StatementOrdinal};

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct SourceLocation {
    pub source_path: String,
    pub start_line: usize,
    pub end_line: usize,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct BindingReport {
    pub binding: BindingName,
    pub export_name: BindingName,
}

/// Node-link JSON side output for downstream graph analysis.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphReport {
    pub chunk_id: String,
    pub nodes: Vec<OwnerGraphNodeReport>,
    pub edges: Vec<OwnerGraphEdgeReport>,
    pub quotient: OwnerGraphQuotientReport,
    pub peelability: OwnerGraphPeelabilityReport,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphNodeReport {
    pub id: String,
    pub statement_ordinal: StatementOrdinal,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    pub declared_bindings: Vec<BindingReport>,
    pub statement_kind: StatementKind,
    /// At-init purity classification, with structured reasons on
    /// any non-`Pure` verdict. Replaces the legacy
    /// `has_purity: bool` — consumers that want the boolean
    /// can use `purity.kind == "pure"`.
    pub purity: Purity,
    pub destination: ModuleReportRef,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphEdgeReport {
    pub id: String,
    pub source: String,
    pub target: String,
    pub edge_kind: DepKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<BindingName>,
    pub statement_ordinal: StatementOrdinal,
    pub constrains_init_order: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphQuotientReport {
    pub nodes: Vec<ModuleReportRef>,
    pub edges: Vec<QuotientEdgeReport>,
    pub sccs: Vec<QuotientSccReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuotientEdgeReport {
    pub id: String,
    pub source: String,
    pub target: String,
    pub edge_kinds: Vec<DepKind>,
    pub constrains_init_order: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuotientSccReport {
    pub id: String,
    pub modules: Vec<String>,
    pub labels: Vec<String>,
    pub is_cycle: bool,
    pub realizable: bool,
    pub module_edge_ids: Vec<String>,
    pub constraining_module_edge_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphPeelabilityReport {
    pub residual_destinations: Vec<ModuleReportRef>,
    pub minimal_peel_sets: Vec<OwnerGraphPeelSetReport>,
    pub residual_owner_horizon: Vec<ResidualOwnerPeelHorizonReport>,
    /// Every peel candidate the analyzer evaluated, with its
    /// terminal status. `minimal_peel_sets[]` is the subset where
    /// `status == peelable_now`; this list also surfaces blocked
    /// candidates so downstream tooling (peel inventory, lane
    /// dispatchers) can see WHY a candidate was rejected — including
    /// the new `blocked_emit_resolvability` projection that lifts
    /// `materialize_logical_modules`'s "moved module references
    /// residual entry binding(s) … not exported by entry" rejection
    /// into peelability.
    #[serde(default)]
    pub evaluated_owner_sets: Vec<EvaluatedPeelCandidateReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvaluatedPeelCandidateReport {
    pub candidate_id: String,
    pub owner_ids: Vec<String>,
    pub members: Vec<BindingReport>,
    pub status: PeelCandidateStatus,
    /// Owner-edge ids that close the cycle for `BlockedCycle`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub cycle_blockers: Vec<String>,
    /// Owner ids whose residual dependency forces `BlockedResidualDependency`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub residual_dependency_blockers: Vec<String>,
    /// Residual binding names referenced by the candidate's moved
    /// bodies that aren't on entry's export list (post-peel). Empty
    /// unless `status == blocked_emit_resolvability`. Mirrors the
    /// materializer's "moved module references residual entry
    /// binding(s) … not exported by entry" rejection so agents can
    /// pre-filter unpeelable candidates without invoking the
    /// materializer.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub emit_blocked_residual_bindings: Vec<BindingName>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResidualOwnerPeelHorizonReport {
    pub owner_id: String,
    pub statement_ordinal: StatementOrdinal,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    pub statement_kind: StatementKind,
    pub purity: Purity,
    pub current_destination: ModuleReportRef,
    pub members: Vec<BindingReport>,
    pub status: ResidualOwnerPeelStatus,
    pub peel_set_ids: Vec<String>,
    pub companion_options: Vec<ResidualOwnerCompanionOptionReport>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResidualOwnerPeelStatus {
    Direct,
    WithCompanions,
    Blocked,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResidualOwnerCompanionOptionReport {
    pub peel_set_id: String,
    pub companion_owner_ids: Vec<String>,
    pub companion_members: Vec<BindingReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphPeelSetReport {
    pub candidate_id: String,
    pub owner_ids: Vec<String>,
    pub members: Vec<BindingReport>,
    /// Always empty for entries in `minimal_peel_sets[]` (those are
    /// the `peelable_now` candidates; if this list weren't empty the
    /// candidate would be `blocked_emit_resolvability` instead). Kept
    /// on the schema so JSON consumers see a stable shape next to
    /// `evaluated_owner_sets[].emit_blocked_residual_bindings`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub emit_blocked_residual_bindings: Vec<BindingName>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PeelCandidateStatus {
    PeelableNow,
    BlockedCycle,
    BlockedResidualDependency,
    /// The candidate's moved bodies reference residual entry
    /// binding(s) that aren't on entry's post-peel export list.
    /// Lifted from `materialize_logical_modules`'s
    /// "moved module references residual entry binding(s) … not
    /// exported by entry" rejection — agents read this to skip
    /// candidates the materializer would reject.
    BlockedEmitResolvability,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModuleReportRef {
    pub id: String,
    pub label: String,
    pub residual: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub index: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_file: Option<String>,
}
