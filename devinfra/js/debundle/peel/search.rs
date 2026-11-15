//! Constructive residual placement over owner coarsenings.
//!
//! See docs/module_placement.md for the obstruction clauses and coverage proof.

use std::time::Duration;

use clap::ValueEnum;
use serde::Serialize;

use super::assignment::{AssignmentProblem, SourceUnitAssignment};

#[derive(Debug, Clone, Copy, ValueEnum, Serialize, Default)]
#[value(rename_all = "snake_case")]
#[serde(rename_all = "snake_case")]
pub enum Destinations {
    New,
    Existing,
    #[default]
    Both,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SearchStatus {
    Found,
    ExhaustedNoPeel,
    Incomplete,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    MaxChecks,
    TimeBudget,
}

pub struct SearchOptions {
    pub destinations: Destinations,
    pub into: Vec<String>,
    pub required: Vec<String>,
    pub only_units: Option<Vec<String>>,
    pub max_new_modules: Option<usize>,
    pub max_checks: Option<u64>,
    pub time_budget: Option<Duration>,
}

#[derive(Serialize)]
pub struct SearchReport {
    pub status: SearchStatus,
    pub stop_reason: Option<StopReason>,
    /// Number of distinct quotient states analyzed.
    pub checks: u64,
    /// Structural work performed by the constructive placement search.
    pub work: SearchWorkReport,
    pub movable_atoms: usize,
    pub residual_units_extracted: usize,
    pub existing_units_moved: usize,
    pub scope: SearchScope,
    #[serde(skip)]
    pub changes: Vec<SourceUnitAssignment>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SearchWorkReport {
    /// Unique placement partitions analyzed; equal to `checks`.
    pub partition_states: u64,
    /// Direct C/I graph projections constructed from typed source edges.
    pub quotient_projections: u64,
    /// Equality contractions forced by domains, structural edges, C SCCs, or C->entry.
    pub forced_contractions: u64,
    /// Candidate equality contractions considered by obstruction clauses.
    pub contraction_attempts: u64,
    /// Final full-assignment certifications.
    pub final_certifications: u64,
    pub branch_clauses: PlacementBranchClauseCounts,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct PlacementBranchClauseCounts {
    pub owner_domain: u64,
    pub schedule_closure: u64,
    pub fresh_owner_cap: u64,
}

#[derive(Serialize)]
pub struct SearchScope {
    pub destinations: Destinations,
    pub into: Vec<String>,
    pub required_units: Vec<String>,
    pub only_units: Option<Vec<String>>,
    pub max_new_modules: Option<usize>,
}

pub fn search(
    problem: &AssignmentProblem<'_>,
    options: &SearchOptions,
) -> anyhow::Result<SearchReport> {
    super::placement::search(problem, options)
}
