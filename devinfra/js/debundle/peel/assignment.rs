//! Complete assignment checks shared by residual discovery and explicit batches.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use analysis::{EntryDestination, ModuleId, StatementKind};
use anyhow::{Context, Result, ensure};
use gate::{AssignmentValidation, EsmImportSchedule};
use graph::assignment_assembly::detect_atomic_group_conflict;
use graph::{
    AtomicGroup, DependencyGraphInput, ModuleAssignment, ModuleDestinationKind,
    compute_atomic_groups,
};
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
    /// Complete nonentry owner priority for the resulting assignment.
    pub import_order: Vec<ModulePath>,
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
    generated_modules: BTreeSet<ModulePath>,
    pub(super) residual_origins: Vec<bool>,
    pub(super) residual_modules: BTreeSet<ModulePath>,
    pub(super) atoms: Vec<AtomicGroup>,
    pub(super) baseline_certificate: Option<(PlacementAssignment, EsmImportSchedule)>,
}

/// A complete placement with explicit owner identities. Construction can use a
/// stable sparse arena while checking/emission uses canonical path order. Entry
/// is a distinct identity, never a generated residual module or a path alias.
pub(super) struct PlacementAssignment {
    pub assignment: ModuleAssignment,
    /// Canonical path at each nonentry `ModuleId`.
    pub modules: Vec<ModulePath>,
}

impl PlacementAssignment {
    pub fn entry(&self) -> ModuleId {
        self.assignment.entry_destination().module_id()
    }

    pub fn nodes(&self) -> BTreeSet<ModuleId> {
        (0..self.modules.len())
            .map(ModuleId)
            .chain([self.entry()])
            .collect()
    }
}

impl<'a> AssignmentProblem<'a> {
    pub fn new(input: &'a DependencyGraphInput) -> Result<Self> {
        let mut problem = Self::prepare(input)?;
        let placement = problem.canonical_assignment(&problem.baseline)?;
        let (baseline, schedule) =
            problem.validate_placement(&placement, &input.report().import_order)?;
        ensure!(
            accepted(&baseline),
            "the current complete assignment is rejected: {:?}",
            baseline.report
        );
        problem.baseline_certificate = Some((placement, schedule));
        Ok(problem)
    }

    pub fn prepare(input: &'a DependencyGraphInput) -> Result<Self> {
        let report = input.report();
        let mut modules_by_key = HashMap::with_capacity(report.quotient.nodes.len());
        for module in &report.quotient.nodes {
            // Match the report's module lookup when duplicate keys appear.
            modules_by_key.entry(&module.key).or_insert(module);
        }
        let mut baseline = Vec::with_capacity(report.nodes.len());
        let mut residual_origins = Vec::with_capacity(report.nodes.len());
        for node in &report.nodes {
            let module = modules_by_key
                .get(&node.destination)
                .with_context(|| format!("source unit {} has no destination", node.id))?;
            baseline
                .push((module.kind != ModuleDestinationKind::Entry).then(|| module.path.clone()));
            residual_origins.push(matches!(
                module.kind,
                ModuleDestinationKind::Entry | ModuleDestinationKind::Unassigned
            ));
        }
        let baseline_owners: BTreeSet<_> = baseline.iter().flatten().cloned().collect();
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
            generated_modules: report
                .quotient
                .nodes
                .iter()
                .filter(|module| {
                    module.kind == ModuleDestinationKind::Unassigned
                        || module.path.as_str().starts_with("__auto/atomic_groups/")
                })
                .filter(|module| baseline_owners.contains(&module.path))
                .map(|module| module.path.clone())
                .collect(),
            residual_origins,
            residual_modules: report
                .quotient
                .nodes
                .iter()
                .filter(|module| module.kind == ModuleDestinationKind::Unassigned)
                .map(|module| module.path.clone())
                .collect(),
            atoms: compute_atomic_groups(input.graph()),
            baseline_certificate: None,
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
        reindex_generated_atomic_group_owners(&mut candidate)?;
        Ok(candidate)
    }

    pub fn validate(&self, candidate: &[Option<ModulePath>]) -> Result<AssignmentValidation> {
        self.validate_with_priority(candidate, &self.input.report().import_order)
    }

    fn canonical_assignment(
        &self,
        candidate: &[Option<ModulePath>],
    ) -> Result<PlacementAssignment> {
        ensure!(
            candidate.len() == self.baseline.len(),
            "candidate must assign every source unit"
        );
        let modules = self.ordered_modules(candidate);
        let ids: BTreeMap<_, _> = modules
            .iter()
            .enumerate()
            .map(|(index, path)| (path, ModuleId(index)))
            .collect();
        let entry = ModuleId(modules.len());
        Ok(PlacementAssignment {
            assignment: ModuleAssignment::from_assignments(
                candidate
                    .iter()
                    .map(|path| path.as_ref().map_or(entry, |path| ids[path]))
                    .collect(),
                EntryDestination::from_module_id(entry),
            ),
            modules,
        })
    }

    /// Validate the complete assignment under exactly the supplied global
    /// priority. This path never searches for an alternative schedule.
    pub fn validate_with_priority(
        &self,
        candidate: &[Option<ModulePath>],
        import_order: &[ModulePath],
    ) -> Result<AssignmentValidation> {
        let placement = self.canonical_assignment(candidate)?;
        self.validate_placement(&placement, import_order)
            .map(|(validation, _)| validation)
    }

    fn validate_placement(
        &self,
        canonical: &PlacementAssignment,
        import_order: &[ModulePath],
    ) -> Result<(AssignmentValidation, EsmImportSchedule)> {
        let assignment = &canonical.assignment;
        let residual = canonical.entry();
        let residual_path = ModulePath::parse("residual", "")?;
        let module_path = |id: ModuleId| {
            if id == residual {
                residual_path.clone()
            } else {
                canonical.modules[id.0].clone()
            }
        };
        let (mut validation, schedule) = gate::validate_assignment_with_priority(
            self.input.graph(),
            assignment,
            &module_path,
            &canonical.nodes(),
            import_order,
        )?;
        self.add_atomic_conflicts(&mut validation, assignment);
        Ok((validation, schedule))
    }

    fn add_atomic_conflicts(
        &self,
        validation: &mut AssignmentValidation,
        assignment: &ModuleAssignment,
    ) {
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
    }

    /// Numeric module identities used by the full gate, in emitter order.
    /// The residual entry follows this list.
    fn ordered_modules(&self, candidate: &[Option<ModulePath>]) -> Vec<ModulePath> {
        // Retain empty authored modules, but omit generated/residual owners
        // drained by this candidate. The materializer emits those owners only
        // while at least one source unit remains assigned to them.
        let assigned: BTreeSet<_> = candidate.iter().flatten().cloned().collect();
        let mut paths: BTreeSet<_> = self
            .existing_modules
            .iter()
            .filter(|path| !self.generated_modules.contains(*path) || assigned.contains(*path))
            .cloned()
            .collect();
        paths.extend(assigned);
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

/// Validate every source unit's final effective owner, not merely authored
/// claims. `None` means true entry; generated residual owners retain their paths.
/// Preparing without baseline acceptance permits repairs of rejected specs,
/// while the complete candidate still passes the shared placement checks.
pub fn check_resolved_owners(
    input: &DependencyGraphInput,
    owners: &BTreeMap<String, Option<ModulePath>>,
    emitted_modules: &BTreeSet<ModulePath>,
    import_order: &[ModulePath],
) -> Result<(Vec<ModulePath>, AssignmentValidation)> {
    let mut problem = AssignmentProblem::prepare(input)?;
    problem.existing_modules = emitted_modules.clone();
    ensure!(
        owners.len() == input.report().nodes.len(),
        "resolved owners must cover every source unit exactly once"
    );
    let candidate = input
        .report()
        .nodes
        .iter()
        .map(|node| {
            owners
                .get(&node.id)
                .cloned()
                .with_context(|| format!("resolved owners omit source unit {:?}", node.id))
        })
        .collect::<Result<Vec<_>>>()?;
    ensure!(
        candidate
            .iter()
            .flatten()
            .all(|module| emitted_modules.contains(module)),
        "resolved source-unit owner is absent from the emitted module universe"
    );
    let placement = problem.canonical_assignment(&candidate)?;
    let (validation, _) = problem.validate_placement(&placement, import_order)?;
    Ok((placement.modules, validation))
}

/// Compact surviving `per_atomic_group` owners after a whole group is claimed.
/// The materializer enumerates wholly unclaimed atomic groups by their smallest
/// `SourceUnitId`, then emits those groups densely under
/// `__auto/atomic_groups/NNNN`. The baseline paths already encode that stable
/// order, so removing a claimed group requires the same dense projection over
/// the remaining owner paths. This only handles the per-atomic-group namespace;
/// coarse residual synthesis has different regrouping rules and uses another
/// namespace.
pub fn reindex_generated_atomic_group_owners(
    owners: &mut [Option<ModulePath>],
) -> Result<BTreeMap<ModulePath, ModulePath>> {
    const PREFIX: &str = "__auto/atomic_groups/";
    let mut existing = BTreeMap::<usize, ModulePath>::new();
    for path in owners.iter().flatten() {
        let Some(suffix) = path.as_str().strip_prefix(PREFIX) else {
            continue;
        };
        ensure!(
            suffix.len() >= 4
                && suffix.bytes().all(|byte| byte.is_ascii_digit())
                && (suffix.len() == 4 || !suffix.starts_with('0')),
            "noncanonical per-atomic-group owner path {:?}",
            path
        );
        let index = suffix
            .parse::<usize>()
            .with_context(|| format!("invalid generated atomic-group index in {path}"))?;
        ensure!(
            existing.insert(index, path.clone()).is_none(),
            "duplicate generated atomic-group owner index {index}"
        );
    }
    let remap: BTreeMap<_, _> = existing
        .into_values()
        .enumerate()
        .map(|(index, old)| {
            let new = ModulePath::parse(&format!("{PREFIX}{index:04}"), "")?;
            Ok((old, new))
        })
        .collect::<Result<_>>()?;
    for owner in owners.iter_mut().flatten() {
        if let Some(new) = remap.get(owner) {
            *owner = new.clone();
        }
    }
    Ok(remap)
}

pub fn accepted(validation: &AssignmentValidation) -> bool {
    validation.verdict.is_realizable() && validation.report.atomic_group_conflicts.is_empty()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_atomic_group_indices_are_compacted_numerically() {
        let path = |suffix: &str| {
            ModulePath::parse(&format!("__auto/atomic_groups/{suffix}"), "").unwrap()
        };
        let mut owners = vec![
            Some(path("10000")),
            Some(path("9999")),
            Some(path("0000")),
            Some(ModulePath::parse("authored", "").unwrap()),
            None,
        ];

        let remap = reindex_generated_atomic_group_owners(&mut owners).unwrap();

        assert_eq!(owners[0], Some(path("0002")));
        assert_eq!(owners[1], Some(path("0001")));
        assert_eq!(owners[2], Some(path("0000")));
        assert_eq!(owners[3], Some(ModulePath::parse("authored", "").unwrap()));
        assert_eq!(owners[4], None);
        assert_eq!(remap.get(&path("9999")), Some(&path("0001")));
        assert_eq!(remap.get(&path("10000")), Some(&path("0002")));
    }
}
