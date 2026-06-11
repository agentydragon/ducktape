use serde::{Deserialize, Serialize};
use swc_atoms::Atom;

use crate::factor_assembly::AtomicUnitConflict;
use crate::ids::ModuleId;
use crate::purity::Purity;
use crate::{DepKind, StatementKind, StatementOrdinal};

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct SourceLocation {
    pub source_path: String,
    pub start_line: usize,
    pub end_line: usize,
}

impl SourceLocation {
    /// Expand this location's line range to include `other`.
    pub fn expand_to(&mut self, other: &SourceLocation) {
        self.start_line = self.start_line.min(other.start_line);
        self.end_line = self.end_line.max(other.end_line);
    }
}

/// Accumulates the minimum start-line and maximum end-line across a
/// collection of `SourceLocation`s. Used to compute the
/// `source_line_range` field of `AtomicUnitReport` and similar.
pub struct LineRange {
    start: usize,
    end: usize,
    size_estimate: usize,
    found: bool,
}

impl Default for LineRange {
    fn default() -> Self {
        Self::new()
    }
}

impl LineRange {
    pub fn new() -> Self {
        Self {
            start: usize::MAX,
            end: 0,
            size_estimate: 0,
            found: false,
        }
    }

    pub fn expand(&mut self, location: &SourceLocation) {
        self.found = true;
        self.start = self.start.min(location.start_line);
        self.end = self.end.max(location.end_line);
        self.size_estimate += location.end_line + 1 - location.start_line;
    }

    pub fn size_estimate(&self) -> usize {
        self.size_estimate
    }

    pub fn into_array(self) -> Option<[usize; 2]> {
        self.found.then_some([self.start, self.end])
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct BindingReport {
    pub binding: Atom,
    pub export_name: Atom,
}

/// Node-link JSON side output for downstream graph analysis.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphReport {
    pub chunk_id: String,
    pub nodes: Vec<OwnerGraphNodeReport>,
    pub edges: Vec<OwnerGraphEdgeReport>,
    #[serde(rename = "module_graph")]
    pub quotient: OwnerGraphQuotientReport,
    pub atomic_graph: AtomicGraphReport,
}

impl OwnerGraphReport {
    /// Resolve a [`ModuleKey`] to its module-table entry. The table
    /// (`quotient.nodes`) is the single source of truth for a module's
    /// path + residual flag; consumers look up here rather than reading
    /// a duplicated field off the reference.
    pub fn module(&self, key: &ModuleKey) -> Option<&ModuleEntry> {
        self.quotient.nodes.iter().find(|entry| &entry.key == key)
    }

    /// Whether `key` denotes the residual catch-all, per the module
    /// table's authoritative `residual` flag. Unknown keys are not
    /// residual.
    pub fn is_residual(&self, key: &ModuleKey) -> bool {
        self.module(key).is_some_and(|entry| entry.residual)
    }
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
    /// The module this owner is assigned to, as an interned
    /// [`ModuleKey`]. Resolve to a path / residual flag via the module
    /// table (`quotient.nodes`).
    pub destination: ModuleKey,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphEdgeReport {
    pub id: String,
    pub source: String,
    pub target: String,
    pub edge_kind: DepKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<Atom>,
    pub statement_ordinal: StatementOrdinal,
    pub constrains_init_order: bool,
    /// Role the edge was emitted with. Mirrors `EdgeReason::role`
    /// (see `crate::EdgeRole`) through the wire format so the peel
    /// planner's `OwnerGraph::from_report` reapplies the same
    /// cross-module at-init promotion filter the materializer's gate
    /// does. `None` is shorthand for `EdgeRole::Direct` (omitted when
    /// serializing to keep direct edges compact).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub role: Option<EdgeRoleReport>,
}

/// Wire-format projection of [`crate::EdgeRole`]. The typed variant
/// avoids the previous `at_init_callee_owner: Option<String>`
/// side-channel by routing the same data through a tagged enum, so
/// adding new edge roles is a single-source-of-truth change.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EdgeRoleReport {
    /// Owner id (e.g. `"owner:42"`) of the at-init callee whose body
    /// produced this edge.
    PromotedAtInit { callee_owner: String },
}

impl EdgeRoleReport {
    /// Resolve back to a [`crate::EdgeRole`] using a `report-id ->
    /// OwnerId` lookup table. Unknown owner ids fall back to
    /// `EdgeRole::Direct` (the lenient view treats the edge as a
    /// normal direct edge) — same shape as the pre-refactor
    /// `at_init_callee_owner.and_then(by_id.get)` fallback.
    pub fn resolve(
        &self,
        by_id: &std::collections::HashMap<String, crate::OwnerId>,
    ) -> crate::EdgeRole {
        match self {
            EdgeRoleReport::PromotedAtInit { callee_owner } => match by_id.get(callee_owner) {
                Some(&owner) => crate::EdgeRole::PromotedAtInit {
                    callee_owner: owner,
                },
                None => crate::EdgeRole::Direct,
            },
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerGraphQuotientReport {
    /// The module table: one [`ModuleEntry`] per logical module. The
    /// single source of truth for each module's path + residual flag;
    /// every other reference is a [`ModuleKey`] into this list.
    pub nodes: Vec<ModuleEntry>,
    pub edges: Vec<QuotientEdgeReport>,
    pub sccs: Vec<QuotientSccReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuotientEdgeReport {
    pub id: String,
    pub source: ModuleKey,
    pub target: ModuleKey,
    pub edge_kinds: Vec<DepKind>,
    pub constrains_init_order: bool,
}

/// Wire-format projection of one quotient SCC. The in-memory
/// primitive for **unrealizable** SCCs is
/// [`crate::realizability::SccDiagnosis`]; this shape covers **every**
/// SCC the dep graph turns up (single-module non-self-loops are
/// filtered out at the builder), with `realizable` distinguishing the
/// offending ones, and uses wire-stable string ids (`module_key`,
/// `quotient_edge:N`) instead of typed `ModuleId` / `OwnerEdgeId`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuotientSccReport {
    pub id: String,
    /// Modules in this SCC, as interned [`ModuleKey`]s. Resolve to
    /// paths via the module table; the former parallel `labels` field
    /// (the same modules spelled as paths) is gone.
    pub modules: Vec<ModuleKey>,
    pub is_cycle: bool,
    pub realizable: bool,
    pub module_edge_ids: Vec<String>,
    pub constraining_module_edge_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AtomicGraphReport {
    pub nodes: Vec<AtomicUnitReport>,
    pub edges: Vec<AtomicUnitEdgeReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AtomicUnitReport {
    pub id: String,
    pub owner_ids: Vec<String>,
    pub members: Vec<BindingReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub anonymous_statement_owner_ids: Vec<String>,
    /// Distinct modules the unit's owners are assigned to, as interned
    /// [`ModuleKey`]s. Resolve to paths/residual via the module table.
    pub destinations: Vec<ModuleKey>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub causes: Vec<DepKind>,
    pub size_lines_estimate: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AtomicUnitEdgeReport {
    pub id: String,
    pub source: String,
    pub target: String,
    pub edge_kinds: Vec<DepKind>,
    pub owner_edge_ids: Vec<String>,
    pub constrains_init_order: bool,
}

/// Wire shape for `atomic_unit_conflicts.json` (one entry per
/// conflicting atomic unit). Projects the in-memory
/// [`AtomicUnitConflict`] onto the shared entity-key formats: owners
/// as `"owner:N"` strings (joining `owner_graph.json`'s `nodes[].id`)
/// and modules as canonical [`spec::ModulePath`]s (joining the module
/// table's `path`). The raw `OwnerId` / `ModuleId` indices never hit
/// the wire.
#[derive(Debug, Clone, Serialize)]
pub struct AtomicUnitConflictReport {
    /// Members as `"owner:N"` keys, sorted by owner id.
    pub members: Vec<String>,
    pub claims: Vec<ConflictingClaimReport>,
    pub causes: Vec<DepKind>,
}

/// One claim row in [`AtomicUnitConflictReport`].
#[derive(Debug, Clone, Serialize)]
pub struct ConflictingClaimReport {
    /// Claiming owner as an `"owner:N"` key.
    pub owner: String,
    pub binding_names: Vec<Atom>,
    /// Claimed destination, by canonical [`spec::ModulePath`].
    pub module: spec::ModulePath,
}

impl AtomicUnitConflictReport {
    pub fn from_conflicts(
        conflicts: &[AtomicUnitConflict],
        module_path: &dyn Fn(ModuleId) -> spec::ModulePath,
    ) -> Vec<Self> {
        conflicts
            .iter()
            .map(|conflict| Self {
                members: conflict
                    .members
                    .iter()
                    .copied()
                    .map(crate::reports::owner_key)
                    .collect(),
                claims: conflict
                    .claims
                    .iter()
                    .map(|claim| ConflictingClaimReport {
                        owner: crate::reports::owner_key(claim.owner),
                        binding_names: claim.binding_names.clone(),
                        module: module_path(claim.module),
                    })
                    .collect(),
                causes: conflict.causes.iter().copied().collect(),
            })
            .collect()
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PeelCandidateStatus {
    PeelableNow,
    BlockedCycle,
    BlockedResidualDependency,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[serde(rename_all = "snake_case")]
pub enum FactorizeDiagnosticReason {
    ExceedsSizeCap,
    NoExactRepair,
    ActiveModuleConflict,
    RepeatedFrontier,
}

/// Interned reference to a logical module.
///
/// This is the **one** encoding of module identity on the owner-graph
/// wire. Everything that points at a module — an owner's
/// `destination`, a quotient edge's endpoints, an SCC's members, an
/// atomic unit's `destinations` — carries a `ModuleKey`, never a
/// path or a parallel label. The key resolves to a [`ModuleEntry`] in
/// the module table (`OwnerGraphQuotientReport::nodes`), which is the
/// sole place a module's `path` and `residual` flag are stored.
///
/// The key is `module_key(ModuleId)` — `"logical:N"` — so it stays
/// compact and stable; the human-readable path lives once, in the
/// table entry.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ModuleKey(pub String);

impl ModuleKey {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for ModuleKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// One entry in the module table (`OwnerGraphQuotientReport::nodes`).
/// The single source of truth mapping a [`ModuleKey`] to its canonical
/// path and residual flag. No other wire field duplicates `path` or
/// `residual`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ModuleEntry {
    pub key: ModuleKey,
    /// Canonical module identity/destination path (see
    /// [`spec::ModulePath`]).
    pub path: spec::ModulePath,
    /// `true` for the synthesized residual catch-all. Authoritative —
    /// not derivable from `path` (a spec author may legitimately name
    /// a non-residual module `residual/...`).
    pub residual: bool,
}
