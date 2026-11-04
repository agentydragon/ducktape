//! Emitter-facing maps from the shared, checked source-placement projection.

use source_identity::StatementId;
use std::collections::{BTreeMap, HashMap};

use analysis::PlacementProjection;
use anyhow::{Context, Result, ensure};
use gate::AssignmentAnalysis;
use swc_ecma_ast::Id;

/// Immutable placement instructions tied to the exact assignment the shared
/// analysis kernel checked. A legacy-rejected but structurally mappable
/// candidate can use these maps for inspection output; an unmappable result
/// has no emitter placement.
pub(super) struct EmissionPlacement<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    bindings: HashMap<Id, usize>,
    statements: BTreeMap<StatementId, usize>,
}

impl<'a> EmissionPlacement<'a> {
    pub(super) fn from_projection(
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
                .map(analysis::PlacementProjectionViolation::diagnostic)
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

    pub(super) fn assignment_analysis(&self) -> &AssignmentAnalysis {
        self.assignment_analysis
    }

    pub(super) fn bindings(&self) -> &HashMap<Id, usize> {
        &self.bindings
    }

    pub(super) fn statements(&self) -> &BTreeMap<StatementId, usize> {
        &self.statements
    }
}
