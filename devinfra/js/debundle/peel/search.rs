//! Complete polynomial inventory for the append-one-fresh-owner policy.

use serde::Serialize;
use spec::ModulePath;

use super::assignment::SourceUnitAssignment;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SearchStatus {
    Found,
    ExhaustedNoPeel,
    UnsupportedResidualRouting,
    InventoryOnly,
}

pub struct SearchOptions {
    /// The only supported family is one new owner appended to the current
    /// global priority. A supplied path names that owner.
    pub into: Vec<String>,
    pub required: Vec<String>,
    pub only_units: Option<Vec<String>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SeedAtomReport {
    /// Every source unit in this structural atom.
    pub source_units: Vec<String>,
    /// Atoms in the same C/rebinding SCC must join this seed as one owner.
    pub co_located_with: Vec<String>,
    /// Whether the minimal successor closure has a nonempty baseline window.
    pub feasible: bool,
    /// Constraints that make this seed's closure impossible under the policy.
    pub blockers: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SearchWorkReport {
    /// Canonical I-adjacency rows scanned once to derive event bounds.
    pub baseline_insertion_rows: u64,
    /// I/C and rebinding edges scanned once to derive closure and bounds.
    pub baseline_insertion_edges: u64,
    /// SCC seed roots assessed by the dynamic program.
    pub seed_roots_assessed: u64,
    /// One selected successor closure materialized for the executable plan.
    pub selected_closures_materialized: u64,
    /// The selected complete owner map checked by the shared validator.
    pub final_certifications: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct UnsupportedResidualOwnerReport {
    /// Existing generated/catchall owner whose routing family is not handled
    /// by the append-one-owner proposer.
    pub module: String,
    /// Residual source units currently routed to this owner.
    pub source_units: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SearchScope {
    pub policy: &'static str,
    pub destination: String,
    pub required_units: Vec<String>,
    pub only_units: Option<Vec<String>>,
}

#[derive(Serialize)]
pub struct SearchReport {
    pub status: SearchStatus,
    pub scope: SearchScope,
    pub movable_atoms: usize,
    pub residual_units_extracted: usize,
    /// The per-atom inventory is polynomial. Companion unit lists are
    /// materialized only for the selected plan, avoiding a quadratic report.
    pub seed_atoms: Vec<SeedAtomReport>,
    pub selected_source_units: Vec<String>,
    pub blockers: Vec<String>,
    /// Residual units already routed outside entry are explicit exclusions,
    /// never evidence that the supported entry-residual family was exhausted.
    pub unsupported_residual_routing: Vec<UnsupportedResidualOwnerReport>,
    pub work: SearchWorkReport,
    #[serde(skip)]
    pub changes: Vec<SourceUnitAssignment>,
    #[serde(skip)]
    pub import_order: Vec<ModulePath>,
}
