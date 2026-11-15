//! Complete assignment checks shared by residual discovery and explicit batches.

use std::collections::{BTreeMap, BTreeSet};

use analysis::assignment_assembly::detect_atomic_group_conflict;
use analysis::{
    AtomicGroup, DependencyGraphInput, EntryDestination, ModuleAssignment, ModuleDestinationKind,
    ModuleId, StatementKind, compute_atomic_groups,
};
use anyhow::{Context, Result, ensure};
use gate::{AssignmentValidation, validate_assignment_with_verdict};
use serde::{Deserialize, Serialize};
use spec::{ModulePath, is_residual_namespace_path};

/// An explicit destination change. Every omitted source unit keeps its owner.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceUnitAssignment {
    pub unit_id: String,
    pub module: String,
}

/// Identity of the exact analyzed program, assumptions and starting assignment.
/// Provenance verification still checks these artifacts against current inputs.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AssignmentSnapshot {
    dependency_graph_sha256: String,
    program_graph_sha256: String,
}

impl AssignmentSnapshot {
    pub fn read(graph: &std::path::Path) -> Result<Self> {
        let header = graph_provenance::read_graph_header(graph)?;
        let program = graph_provenance::program_graph_path(graph, &header.program_graph);
        Ok(Self {
            dependency_graph_sha256: graph_provenance::sha256_reader(std::fs::File::open(graph)?)?,
            program_graph_sha256: graph_provenance::sha256_reader(std::fs::File::open(program)?)?,
        })
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AssignmentPlan {
    pub snapshot: AssignmentSnapshot,
    pub changes: Vec<SourceUnitAssignment>,
}

impl AssignmentPlan {
    pub fn verify(&self, graph: &std::path::Path) -> Result<()> {
        ensure!(
            self.snapshot == AssignmentSnapshot::read(graph)?,
            "assignment plan is stale for this graph or program context; propose or check a new plan"
        );
        Ok(())
    }
}

/// Prepared graph and accepted baseline. Candidate construction never contracts
/// source units through speculative intermediate assignments.
pub struct AssignmentProblem<'a> {
    pub(super) input: &'a DependencyGraphInput,
    pub(super) baseline: Vec<Option<ModulePath>>,
    pub(super) existing_modules: BTreeSet<ModulePath>,
    pub(super) residual_origins: Vec<bool>,
    pub(super) residual_modules: BTreeSet<ModulePath>,
    pub(super) atoms: Vec<AtomicGroup>,
}

impl<'a> AssignmentProblem<'a> {
    pub fn new(input: &'a DependencyGraphInput) -> Result<Self> {
        let problem = Self::prepare(input)?;
        let baseline = problem.validate(&problem.baseline)?;
        ensure!(
            accepted(&baseline),
            "the current complete assignment is rejected: {:?}",
            baseline.report
        );
        Ok(problem)
    }

    fn prepare(input: &'a DependencyGraphInput) -> Result<Self> {
        let report = input.report();
        let baseline = report
            .nodes
            .iter()
            .map(|node| {
                let module = report
                    .module(&node.destination)
                    .with_context(|| format!("source unit {} has no destination", node.id))?;
                Ok((module.kind != ModuleDestinationKind::Entry).then(|| module.path.clone()))
            })
            .collect::<Result<Vec<_>>>()?;
        let problem = Self {
            input,
            baseline,
            existing_modules: report
                .quotient
                .nodes
                .iter()
                .filter(|module| module.kind != ModuleDestinationKind::Entry)
                .map(|module| module.path.clone())
                .collect(),
            residual_origins: report
                .nodes
                .iter()
                .map(|node| report.is_unassigned_or_entry(&node.destination))
                .collect(),
            residual_modules: report
                .quotient
                .nodes
                .iter()
                .filter(|module| module.kind == ModuleDestinationKind::Unassigned)
                .map(|module| module.path.clone())
                .collect(),
            atoms: compute_atomic_groups(input.graph()),
        };
        Ok(problem)
    }

    pub fn candidate(&self, changes: &[SourceUnitAssignment]) -> Result<Vec<Option<ModulePath>>> {
        let mut candidate = self.baseline.clone();
        let mut seen = BTreeSet::new();
        for change in changes {
            let unit = self
                .input
                .source_unit_id(&change.unit_id)
                .with_context(|| format!("unknown source unit {:?}", change.unit_id))?;
            ensure!(
                seen.insert(unit),
                "duplicate source unit {:?}",
                change.unit_id
            );
            let node = self.input.source_unit_report(unit);
            ensure!(
                !matches!(
                    node.statement_kind,
                    StatementKind::Import | StatementKind::Export
                ),
                "import/export source unit {:?} must stay at its current destination",
                change.unit_id
            );
            ensure!(
                !std::path::Path::new(&change.module).is_absolute()
                    && !change.module.contains('\0'),
                "destination must be relative: {:?}",
                change.module
            );
            let module = ModulePath::parse(&change.module, "")?;
            ensure!(
                !module.as_str().ends_with(".yaml") && !module.as_str().ends_with(".yml"),
                "use an extensionless logical module destination: {:?}",
                change.module
            );
            ensure!(
                !self.residual_modules.contains(&module)
                    && (!is_residual_namespace_path(module.as_str())
                        || self.existing_modules.contains(&module)),
                "destination {:?} is reserved for residual output",
                change.module
            );
            candidate[unit.0] = Some(module);
        }
        Ok(candidate)
    }

    pub fn validate(&self, candidate: &[Option<ModulePath>]) -> Result<AssignmentValidation> {
        ensure!(
            candidate.len() == self.baseline.len(),
            "candidate must assign every source unit"
        );
        let paths = self.ordered_modules(candidate);
        let ids: BTreeMap<_, _> = paths
            .iter()
            .enumerate()
            .map(|(index, path)| (path, ModuleId(index)))
            .collect();
        let residual = ModuleId(paths.len());
        let assignment = ModuleAssignment::from_assignments(
            candidate
                .iter()
                .map(|path| path.as_ref().map_or(residual, |path| ids[path]))
                .collect(),
            EntryDestination::from_module_id(residual),
        );
        let residual_path = ModulePath::parse("residual", "")?;
        let mut validation =
            validate_assignment_with_verdict(self.input.graph(), &assignment, &|id| {
                if id == residual {
                    residual_path.clone()
                } else {
                    paths[id.0].clone()
                }
            });
        validation.report.atomic_group_conflicts = self
            .atoms
            .iter()
            .filter_map(|atom| {
                detect_atomic_group_conflict(
                    atom,
                    |unit| assignment.of(unit),
                    |unit| {
                        self.input
                            .source_unit_report(unit)
                            .declared_bindings
                            .iter()
                            .map(|binding| binding.binding.clone())
                            .collect()
                    },
                )
            })
            .collect();
        Ok(validation)
    }

    /// Numeric module identities used by the full gate, in emitter order.
    /// The residual entry follows this list.
    pub fn ordered_modules(&self, candidate: &[Option<ModulePath>]) -> Vec<ModulePath> {
        // Retain empty authored modules too. Emission assigns module identities
        // in path order, with the residual entry following the planned modules.
        let paths: BTreeSet<_> = self
            .existing_modules
            .iter()
            .cloned()
            .chain(candidate.iter().flatten().cloned())
            .collect();
        // Authored modules precede generated residual-layout modules in the
        // normal pipeline. Keep that distinction instead of collapsing every
        // unclaimed destination into the entry module.
        paths
            .iter()
            .filter(|path| !self.residual_modules.contains(*path))
            .chain(
                paths
                    .iter()
                    .filter(|path| self.residual_modules.contains(*path)),
            )
            .cloned()
            .collect()
    }

    pub fn residual_gain(&self, candidate: &[Option<ModulePath>]) -> usize {
        self.residual_origins
            .iter()
            .zip(candidate)
            .filter(|(was_residual, after)| {
                **was_residual
                    && after
                        .as_ref()
                        .is_some_and(|module| !self.residual_modules.contains(module))
            })
            .count()
    }

    pub fn existing_units_moved(&self, candidate: &[Option<ModulePath>]) -> usize {
        self.baseline
            .iter()
            .zip(candidate)
            .zip(&self.residual_origins)
            .filter(|((before, after), was_residual)| !**was_residual && before != after)
            .count()
    }
}

/// The exact resolved-map edit gate also supports repairing a rejected starting
/// spec. Discovery/check commands require an accepted baseline via `new`; a
/// legacy repair still checks its complete final assignment with the same gate.
pub fn check_resolved_edit(
    input: &DependencyGraphInput,
    changes: &[SourceUnitAssignment],
) -> Result<(Vec<ModulePath>, AssignmentValidation)> {
    let problem = AssignmentProblem::prepare(input)?;
    let candidate = problem.candidate(changes)?;
    Ok((
        problem.ordered_modules(&candidate),
        problem.validate(&candidate)?,
    ))
}

pub fn accepted(validation: &AssignmentValidation) -> bool {
    validation.verdict.is_realizable() && validation.report.atomic_group_conflicts.is_empty()
}
