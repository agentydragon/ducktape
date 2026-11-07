//! Emitter-facing maps from the shared, checked source-placement projection.

use source_identity::StatementId;
use std::collections::{BTreeMap, HashMap};

use analysis::{ModuleId, PlacementProjection, PlacementProjectionViolation};
use anyhow::{Context, Result, ensure};
use gate::{AssignmentAnalysis, CandidatePreflight};
use swc_ecma_ast::Id;

/// Immutable placement instructions tied to the exact assignment the shared
/// analysis kernel checked. A rejected but structurally mappable
/// candidate can use these maps for inspection output; an unmappable result
/// has no emitter placement.
pub(super) struct EmissionPlacement<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    bindings: HashMap<Id, usize>,
    statements: BTreeMap<StatementId, usize>,
}

impl<'a> EmissionPlacement<'a> {
    pub(super) fn from_preflight(
        preflight: &CandidatePreflight<'a>,
        projection: &PlacementProjection<'_>,
    ) -> Result<Self> {
        Self::from_projection(preflight.assignment_analysis(), projection)
    }

    fn from_projection(
        assignment_analysis: &'a AssignmentAnalysis,
        projection: &PlacementProjection<'_>,
    ) -> Result<Self> {
        ensure!(
            std::ptr::eq(assignment_analysis.assignment(), projection.assignment()),
            "reconstruction: placement maps belong to a different immutable assignment"
        );
        let maps = projection.emission_maps().with_context(|| {
            let violations = projection
                .assessment()
                .violations()
                .iter()
                .map(|violation| diagnostic_with_destination_context(assignment_analysis, violation))
                .collect::<Vec<_>>()
                .join("; ");
            format!(
                "reconstruction: source/destination placement projection is unmappable; typed violation(s): {violations}; unresolved checks: {:?}",
                projection.assessment().unresolved_checks(),
            )
        })?;
        Ok(Self {
            assignment_analysis,
            bindings: maps.bindings().clone(),
            statements: maps.statements().clone(),
        })
    }

    pub(super) fn assignment_analysis(&self) -> &'a AssignmentAnalysis {
        self.assignment_analysis
    }

    pub(super) fn bindings(&self) -> &HashMap<Id, usize> {
        &self.bindings
    }

    pub(super) fn statements(&self) -> &BTreeMap<StatementId, usize> {
        &self.statements
    }
}

pub(super) fn diagnostic_with_destination_context(
    assignment_analysis: &AssignmentAnalysis,
    violation: &PlacementProjectionViolation,
) -> String {
    let mut destinations = match violation {
        PlacementProjectionViolation::DestinationOutOfRange { destination, .. }
        | PlacementProjectionViolation::OwnedBindingMissingSourceProjection {
            destination, ..
        } => vec![*destination],
        PlacementProjectionViolation::BindingDestinationMismatch { destinations, .. }
        | PlacementProjectionViolation::DestructuringDestinationSplit { destinations, .. }
        | PlacementProjectionViolation::NestedVarDestinationSplit { destinations, .. }
        | PlacementProjectionViolation::StatementSourceDestinationSplit { destinations, .. }
        | PlacementProjectionViolation::StableSetIntervalDestinationSplit {
            destinations, ..
        } => destinations.clone(),
        PlacementProjectionViolation::OwnedBindingDestinationMismatch {
            declared_destination,
            source_destination,
            ..
        } => vec![*declared_destination, *source_destination],
        PlacementProjectionViolation::WholeStatementDestinationMismatch {
            requested_destination,
            source_destination,
            ..
        } => vec![ModuleId(*requested_destination), *source_destination],
        PlacementProjectionViolation::MissingWholeStatementClaim { destination, .. } => {
            vec![*destination]
        }
        PlacementProjectionViolation::WholeStatementWithoutSourceUnit {
            requested_destination,
            ..
        } => vec![ModuleId(*requested_destination)],
        PlacementProjectionViolation::SourceUnitLayout { .. }
        | PlacementProjectionViolation::DeclaratorGroupLocationMismatch { .. }
        | PlacementProjectionViolation::DeclaratorGroupMissingLayout { .. }
        | PlacementProjectionViolation::DeclaratorGroupBindingsMismatch { .. }
        | PlacementProjectionViolation::NestedVarMissingOwner { .. }
        | PlacementProjectionViolation::NestedVarMissingSourceProjection { .. }
        | PlacementProjectionViolation::NestedVarMissingWholeStatementClaim { .. }
        | PlacementProjectionViolation::StableSetIntervalMissingSourceProjection { .. } => {
            Vec::new()
        }
    };
    destinations.sort_unstable();
    destinations.dedup();

    let diagnostic = violation.diagnostic();
    if destinations.is_empty() {
        return diagnostic;
    }

    let destination_context = destinations
        .into_iter()
        .map(|destination| {
            let is_entry = destination
                == assignment_analysis
                    .assignment()
                    .entry_destination()
                    .module_id();
            let is_planned_output = assignment_analysis
                .inputs()
                .planned_modules()
                .get(destination.0)
                .is_some();
            let path = if is_entry || is_planned_output {
                assignment_analysis.module_path(destination).to_string()
            } else {
                format!("<out-of-range ModuleId({})>", destination.0)
            };
            format!("ModuleId({})=`{path}`", destination.0)
        })
        .collect::<Vec<_>>()
        .join(", ");
    format!("{diagnostic} [referenced destinations: {destination_context}]")
}
