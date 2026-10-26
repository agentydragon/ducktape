//! Project syntax placement from the exact assignment accepted by the gate.
//! Requested whole-statement moves are checked against that assignment before
//! becoming emission instructions; binding destinations are read from source units.

use source_identity::{SourceUnitOrdinal, StatementId};
use std::collections::{BTreeMap, HashMap};

use analysis::{BindingKind, ChunkId, ModuleId, SourceUnitClass};
use anyhow::{Context, Result, bail, ensure};
use gate::{AssignmentAnalysis, ValidatedAssignment};
use js_ast::statements_for_source_unit_ordinals;
use swc_ecma_ast::{Id, ModuleItem};

/// Immutable placement instructions tied to the assignment that produced them.
/// Only projection from a validated assignment constructs this type.
pub(super) struct EmissionPlacement<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    bindings: HashMap<Id, usize>,
    statements: BTreeMap<StatementId, usize>,
}

impl<'a> EmissionPlacement<'a> {
    pub(super) fn from_validated(
        validated: &'a ValidatedAssignment<'_>,
        chunk_identity: ChunkId,
        body: &[ModuleItem],
        module_count: usize,
        requested_statements: &BTreeMap<StatementId, usize>,
    ) -> Result<Self> {
        let assignment_analysis = validated.assignment_analysis();
        let graph = assignment_analysis.inputs().dependency_graph();
        let assignment = assignment_analysis.assignment();
        let residual = assignment.entry_destination().module_id();
        let ordinals: Vec<SourceUnitOrdinal> = graph
            .iter_nodes()
            .map(|unit| unit.statement_ordinal)
            .collect();
        let body_indices = statements_for_source_unit_ordinals(body, &ordinals);
        let mut binding_destinations = HashMap::<Id, ModuleId>::new();
        let mut statement_destinations = BTreeMap::<StatementId, ModuleId>::new();
        for (unit, body_index) in graph.iter_nodes().zip(body_indices) {
            let body_index = body_index.with_context(|| {
                format!(
                    "reconstruction: source unit {} has no original body item",
                    unit.id.0
                )
            })?;
            let destination = assignment.of(unit.id);
            ensure!(
                destination == residual || destination.0 < module_count,
                "reconstruction: source unit {} targets unknown module {}",
                unit.id.0,
                destination.0
            );
            for binding in &unit.declared {
                if let Some(previous) = binding_destinations.insert(binding.clone(), destination) {
                    ensure!(
                        previous == destination,
                        "reconstruction: declaration sites of `{}` have different validated destinations",
                        binding.0
                    );
                }
            }
            if requested_statements.contains_key(&body_index) {
                if let Some(previous) = statement_destinations.insert(body_index, destination) {
                    ensure!(
                        previous == destination,
                        "reconstruction: whole-statement claim at body index {body_index} spans differently assigned source units"
                    );
                }
            } else if unit.class() == SourceUnitClass::AnonymousStatement && destination != residual
            {
                bail!(
                    "reconstruction: source unit {} targets module {} without a whole-statement plan",
                    unit.id.0,
                    destination.0
                );
            }
        }
        // Import construction reads this catalogue too. Prove its binding
        // destinations agree with syntax placement, including shared var sites.
        let local_bindings = assignment_analysis
            .inputs()
            .local_bindings(chunk_identity)?;
        for (binding, kind) in local_bindings.as_map() {
            if let BindingKind::Owned { module } = kind {
                ensure!(
                    binding_destinations.get(binding) == Some(module),
                    "reconstruction: binding `{}` disagrees with its validated source-unit destination",
                    binding.0
                );
            }
        }
        let mut statements = BTreeMap::new();
        for (&body_index, &requested) in requested_statements {
            let destination = statement_destinations.get(&body_index).with_context(|| format!(
                "reconstruction: whole-statement claim at body index {body_index} has no source unit"
            ))?;
            ensure!(
                *destination != residual && destination.0 == requested,
                "reconstruction: whole-statement claim at body index {body_index} disagrees with its validated destination"
            );
            statements.insert(body_index, destination.0);
        }
        let bindings = binding_destinations
            .into_iter()
            .filter(|(_, destination)| *destination != residual)
            .map(|(binding, destination)| (binding, destination.0))
            .collect();
        Ok(Self {
            assignment_analysis,
            bindings,
            statements,
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
