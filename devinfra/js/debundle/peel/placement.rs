//! Complete constructive extraction under one declared append-last policy.
//!
//! Existing owners and their global priority stay fixed. The only candidate
//! owner is fresh; it is appended after the fixed priority. `baseline_insertion`
//! computes every seed SCC's minimal successor closure and timing window in a
//! polynomial pass, then this module certifies only the selected complete map.

use std::collections::{BTreeMap, BTreeSet};

use analysis::{EntryDestination, ModuleAssignment, ModuleId, StatementKind};
use anyhow::{Context, Result, ensure};
use gate::project_assignment_constraints;
use source_identity::SourceUnitId;
use spec::{ModulePath, is_residual_namespace_path};

use super::assignment::{AssignmentProblem, SourceUnitAssignment, accepted};
use super::search::{
    SearchOptions, SearchReport, SearchScope, SearchStatus, SearchWorkReport,
    UnsupportedResidualOwnerReport,
};

#[path = "baseline_insertion.rs"]
mod baseline_insertion;

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
enum Owner {
    Entry,
    Module(ModulePath),
}

impl Owner {
    fn path(&self) -> Option<&ModulePath> {
        match self {
            Self::Entry => None,
            Self::Module(path) => Some(path),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
enum Token {
    Owner(Owner),
    Atom(usize),
}

#[derive(Debug, Clone)]
struct Block {
    tokens: BTreeSet<Token>,
    units: BTreeSet<usize>,
    owner: Option<Owner>,
    allowed_owners: BTreeSet<Owner>,
}

impl Block {
    fn key(&self) -> Token {
        self.tokens
            .first()
            .expect("every placement block retains an initial token")
            .clone()
    }
}

#[derive(Debug, Clone)]
struct Partition {
    blocks: Vec<Block>,
}

impl Partition {
    fn new(mut blocks: Vec<Block>) -> Self {
        blocks.sort_by_key(Block::key);
        Self { blocks }
    }
}

struct MoveAtom {
    members: BTreeSet<usize>,
}

pub(super) struct Scope {
    atoms: Vec<MoveAtom>,
    movable_units: BTreeSet<usize>,
    required_units: BTreeSet<usize>,
    required_names: Vec<String>,
    only_names: Option<Vec<String>>,
    baseline_owners: BTreeSet<Owner>,
    target: Owner,
}

pub(super) fn search(
    problem: &AssignmentProblem<'_>,
    options: &SearchOptions,
) -> Result<SearchReport> {
    let scope = prepare_scope(problem, options)?;
    let initial = initial_partition(problem, &scope);
    let module_ids: BTreeMap<_, _> = initial
        .blocks
        .iter()
        .enumerate()
        .map(|(index, block)| (block.key(), ModuleId(index)))
        .collect();
    let entry = module_ids[&Token::Owner(Owner::Entry)];
    let mut assignments = vec![entry; problem.baseline.len()];
    for block in &initial.blocks {
        let module = module_ids[&block.key()];
        for &unit in &block.units {
            assignments[unit] = module;
        }
    }
    let constraints = project_assignment_constraints(
        problem.input.graph(),
        &ModuleAssignment::from_assignments(assignments, EntryDestination::from_module_id(entry)),
    );
    let insertion = baseline_insertion::inventory(
        problem,
        &scope,
        &initial,
        &module_ids,
        &constraints,
        &scope.target,
    )?;

    let target = scope
        .target
        .path()
        .expect("append-last target is a module path")
        .clone();
    let mut candidate = problem.baseline.clone();
    for &unit in &insertion.selected_units {
        candidate[unit] = Some(target.clone());
    }
    let mut import_order = problem.input.report().import_order.clone();
    if !insertion.selected_units.is_empty() {
        import_order.push(target.clone());
    }
    let mut blockers = insertion.blockers;
    let mut unsupported_by_owner = BTreeMap::<ModulePath, Vec<String>>::new();
    if options.only_units.is_none() {
        for unit in 0..problem.baseline.len() {
            if problem.residual_origins[unit]
                && let Some(owner) = problem.baseline[unit].as_ref()
            {
                unsupported_by_owner.entry(owner.clone()).or_default().push(
                    problem
                        .input
                        .source_unit_report(SourceUnitId(unit))
                        .id
                        .clone(),
                );
            }
        }
    }
    let unsupported_residual_routing: Vec<_> = unsupported_by_owner
        .into_iter()
        .map(|(module, source_units)| UnsupportedResidualOwnerReport {
            module: module.as_str().to_owned(),
            source_units,
        })
        .collect();
    let mut changes = Vec::new();
    let mut selected_source_units = Vec::new();
    let mut final_certifications = 0;
    let status = if insertion.selected_units.is_empty() {
        if blockers.is_empty() && !options.required.is_empty() {
            blockers.push("required_seed_has_no_feasible_append_last_closure".to_owned());
        }
        if unsupported_residual_routing.is_empty() {
            SearchStatus::ExhaustedNoPeel
        } else {
            SearchStatus::UnsupportedResidualRouting
        }
    } else {
        let (validation, _) =
            problem.validate_with_priority_and_schedule(&candidate, &import_order)?;
        ensure!(
            accepted(&validation),
            "append-last window theorem disagreed with complete assignment certification: {:?}",
            validation.report
        );
        final_certifications = 1;
        for &unit in &insertion.selected_units {
            let id = problem
                .input
                .source_unit_report(SourceUnitId(unit))
                .id
                .clone();
            selected_source_units.push(id.clone());
            changes.push(SourceUnitAssignment {
                unit_id: id,
                module: target.as_str().to_string(),
            });
        }
        SearchStatus::Found
    };

    Ok(SearchReport {
        status,
        scope: SearchScope {
            policy: "one_fresh_owner_appended_to_fixed_priority",
            destination: target.as_str().to_string(),
            required_units: scope.required_names,
            only_units: scope.only_names,
        },
        movable_atoms: scope.atoms.len(),
        residual_units_extracted: insertion.selected_units.len(),
        seed_atoms: insertion.seed_atoms,
        selected_source_units,
        blockers,
        unsupported_residual_routing,
        work: SearchWorkReport {
            baseline_insertion_rows: insertion.rows,
            baseline_insertion_edges: insertion.edges,
            seed_roots_assessed: insertion.roots,
            selected_closures_materialized: u64::from(!insertion.selected_units.is_empty()),
            final_certifications,
        },
        changes,
        import_order,
    })
}

fn prepare_scope(problem: &AssignmentProblem<'_>, options: &SearchOptions) -> Result<Scope> {
    ensure!(
        options.into.len() <= 1,
        "modules propose accepts at most one fresh --into destination"
    );

    let required_ids: BTreeSet<_> = options
        .required
        .iter()
        .map(|id| {
            problem
                .input
                .source_unit_id(id)
                .with_context(|| format!("unknown source unit {id:?}"))
        })
        .collect::<Result<_>>()?;
    let only_ids: Option<BTreeSet<_>> = options
        .only_units
        .as_deref()
        .map(|ids| {
            ids.iter()
                .map(|id| {
                    problem
                        .input
                        .source_unit_id(id)
                        .with_context(|| format!("unknown source unit {id:?}"))
                })
                .collect()
        })
        .transpose()?;

    let mut atoms = Vec::new();
    for atom in &problem.atoms {
        let permitted = atom.members.iter().all(|unit| {
            problem.residual_origins[unit.0]
                && problem.baseline[unit.0].is_none()
                && !matches!(
                    problem.input.source_unit_report(*unit).statement_kind,
                    StatementKind::Import | StatementKind::Export
                )
        });
        if let Some(only) = &only_ids {
            let selected = atom.members.intersection(only).count();
            ensure!(
                selected == 0 || selected == atom.members.len(),
                "--only-units cuts an atomic group; include its complete companion set: {:?}",
                atom.members
                    .iter()
                    .map(|unit| &problem.input.source_unit_report(*unit).id)
                    .collect::<Vec<_>>()
            );
            if selected == 0 {
                continue;
            }
            ensure!(
                permitted,
                "--only-units contains an authored owner, import/export scaffolding, or a non-entry residual unit"
            );
        }
        if permitted {
            atoms.push(MoveAtom {
                members: atom.members.iter().map(|unit| unit.0).collect(),
            });
        }
    }

    let movable_units: BTreeSet<_> = atoms
        .iter()
        .flat_map(|atom| atom.members.iter().copied())
        .collect();
    let required_units: BTreeSet<_> = required_ids.iter().map(|unit| unit.0).collect();
    ensure!(
        required_units.is_subset(&movable_units),
        "--require must name true-entry residual body units inside the permitted atomic scope"
    );

    let existing_modules = &problem.existing_modules;
    let target = if let Some(raw) = options.into.first() {
        ensure!(
            !std::path::Path::new(raw).is_absolute() && !raw.contains('\0'),
            "destination must be a relative module path"
        );
        let path = ModulePath::parse(raw, "")?;
        ensure!(
            !path.as_str().ends_with(".yaml") && !path.as_str().ends_with(".yml"),
            "destination must be extensionless"
        );
        ensure!(
            !existing_modules.contains(&path)
                && !problem.residual_modules.contains(&path)
                && !is_residual_namespace_path(path.as_str()),
            "--into must name one fresh, non-residual owner; {path} already exists or is reserved"
        );
        path
    } else {
        let mut index = 0usize;
        loop {
            let path = ModulePath::parse(&format!("extracted/peel_{index:04}"), "")?;
            if !existing_modules.contains(&path) && !problem.residual_modules.contains(&path) {
                break path;
            }
            index = index.checked_add(1).context("fresh owner index overflow")?;
        }
    };

    let baseline_owners: BTreeSet<_> = problem
        .existing_modules
        .iter()
        .cloned()
        .map(Owner::Module)
        .chain(std::iter::once(Owner::Entry))
        .collect();
    Ok(Scope {
        atoms,
        movable_units,
        required_units,
        required_names: options.required.clone(),
        only_names: options.only_units.clone(),
        baseline_owners,
        target: Owner::Module(target),
    })
}

fn owner_for_path(path: Option<&ModulePath>) -> Owner {
    path.map_or(Owner::Entry, |path| Owner::Module(path.clone()))
}

fn initial_partition(problem: &AssignmentProblem<'_>, scope: &Scope) -> Partition {
    let mut blocks = Vec::new();
    let mut owners = scope.baseline_owners.clone();
    owners.insert(scope.target.clone());
    let mut fixed_units = BTreeMap::<Owner, BTreeSet<usize>>::new();
    for (unit, baseline_owner) in problem.baseline.iter().enumerate() {
        if scope.movable_units.contains(&unit) {
            continue;
        }
        let owner = owner_for_path(baseline_owner.as_ref());
        fixed_units.entry(owner).or_default().insert(unit);
    }
    for owner in owners {
        let units = fixed_units.remove(&owner).unwrap_or_default();
        blocks.push(Block {
            tokens: BTreeSet::from([Token::Owner(owner.clone())]),
            units,
            owner: Some(owner.clone()),
            allowed_owners: BTreeSet::from([owner]),
        });
    }
    debug_assert!(fixed_units.is_empty());

    for (index, atom) in scope.atoms.iter().enumerate() {
        let mut allowed_owners = BTreeSet::from([Owner::Entry, scope.target.clone()]);
        if atom
            .members
            .iter()
            .any(|unit| scope.required_units.contains(unit))
        {
            allowed_owners.remove(&Owner::Entry);
        }
        blocks.push(Block {
            tokens: BTreeSet::from([Token::Atom(index)]),
            units: atom.members.clone(),
            owner: None,
            allowed_owners,
        });
    }
    Partition::new(blocks)
}
