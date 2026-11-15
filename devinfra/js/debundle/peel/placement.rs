//! Constructive residual placement over coarsenings of fixed owner anchors.
//!
//! Branches are only the compatible contractions required by a domain,
//! cardinality or exact ESM-schedule obstruction. Constraining cycles force
//! co-location, including cycles with fallback-promoted edges. The
//! concrete full assignment validator is reserved for final certification.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::time::Instant;

use analysis::graph::{EndpointView, chunk_constraining_module_edges, project_endpoints};
use analysis::{EntryDestination, ModuleAssignment, ModuleId, StatementKind};
use anyhow::{Context, Result, bail, ensure};
use gate::{EsmImportOrder, EsmImportSchedule, ScheduleObstruction};
use petgraph::{algo::tarjan_scc, graphmap::DiGraphMap};
use source_identity::SourceUnitId;
use spec::{ModulePath, is_residual_namespace_path};

use super::assignment::{AssignmentProblem, SourceUnitAssignment, accepted};
use super::search::{
    Destinations, SearchOptions, SearchReport, SearchScope, SearchStatus, SearchWorkReport,
    StopReason,
};

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
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

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum Token {
    Owner(Owner),
    Atom(usize),
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
struct Block {
    tokens: BTreeSet<Token>,
    units: BTreeSet<usize>,
    owner: Option<Owner>,
    allowed_owners: BTreeSet<Owner>,
    allow_fresh: bool,
}

impl Block {
    fn key(&self) -> Token {
        self.tokens
            .first()
            .expect("every placement block retains an initial token")
            .clone()
    }

    fn occupied(&self) -> bool {
        !self.units.is_empty()
    }
}

#[derive(Debug, Clone, Default, Eq, PartialEq)]
struct Partition {
    blocks: Vec<Block>,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
struct BlockKey {
    tokens: BTreeSet<Token>,
    owner: Option<Owner>,
    allowed_owners: BTreeSet<Owner>,
    allow_fresh: bool,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
struct PartitionKey(Vec<BlockKey>);

impl Partition {
    fn new(mut blocks: Vec<Block>) -> Self {
        blocks.sort_by_key(Block::key);
        Self { blocks }
    }

    fn key(&self) -> PartitionKey {
        PartitionKey(
            self.blocks
                .iter()
                .map(|block| BlockKey {
                    tokens: block.tokens.clone(),
                    owner: block.owner.clone(),
                    allowed_owners: block.allowed_owners.clone(),
                    allow_fresh: block.allow_fresh,
                })
                .collect(),
        )
    }

    fn block(&self, key: &Token) -> Option<&Block> {
        self.blocks.iter().find(|block| block.key() == *key)
    }

    fn merge(&self, left: &Token, right: &Token) -> Option<Self> {
        if left == right {
            return Some(self.clone());
        }
        let left_index = self.blocks.iter().position(|block| block.key() == *left)?;
        let right_index = self.blocks.iter().position(|block| block.key() == *right)?;
        let left_block = &self.blocks[left_index];
        let right_block = &self.blocks[right_index];

        let owner = match (&left_block.owner, &right_block.owner) {
            (Some(left), Some(right)) if left != right => return None,
            (Some(owner), _) | (_, Some(owner)) => Some(owner.clone()),
            (None, None) => None,
        };
        let allowed_owners: BTreeSet<_> = left_block
            .allowed_owners
            .intersection(&right_block.allowed_owners)
            .cloned()
            .collect();
        if owner
            .as_ref()
            .is_some_and(|owner| !allowed_owners.contains(owner))
        {
            return None;
        }
        let allow_fresh = left_block.allow_fresh && right_block.allow_fresh;
        if owner.is_none() && allowed_owners.is_empty() && !allow_fresh {
            return None;
        }

        let mut merged = Block {
            tokens: left_block
                .tokens
                .union(&right_block.tokens)
                .cloned()
                .collect(),
            units: left_block
                .units
                .union(&right_block.units)
                .copied()
                .collect(),
            owner,
            allowed_owners,
            allow_fresh,
        };
        // Anchored blocks are a selected owner identity. Their effective domain
        // is exactly that anchor, even when the members could have chosen other
        // destinations in a sibling search branch.
        if let Some(owner) = &merged.owner {
            merged.allowed_owners = BTreeSet::from([owner.clone()]);
            merged.allow_fresh = false;
        }

        let (low, high) = if left_index < right_index {
            (left_index, right_index)
        } else {
            (right_index, left_index)
        };
        let mut blocks = self.blocks.clone();
        blocks.remove(high);
        blocks.remove(low);
        blocks.push(merged);
        Some(Self::new(blocks))
    }
}

#[derive(Clone)]
struct MoveAtom {
    members: BTreeSet<usize>,
    required: bool,
}

struct Scope {
    atoms: Vec<MoveAtom>,
    movable_units: BTreeSet<usize>,
    required_units: BTreeSet<usize>,
    allowed_existing_targets: BTreeSet<ModulePath>,
    explicit_fresh_targets: BTreeSet<ModulePath>,
    baseline_owners: BTreeSet<Owner>,
    residual_modules: BTreeSet<ModulePath>,
    effective_new_limit: usize,
    allow_arbitrary_fresh: bool,
}

struct Candidate {
    owners: BTreeMap<Token, Owner>,
    occupied_fresh_owners: BTreeSet<Owner>,
    assignment: Vec<Option<ModulePath>>,
}

struct Projection {
    candidate: Candidate,
    module_assignment: ModuleAssignment,
    block_by_module: BTreeMap<ModuleId, Token>,
    block_by_unit: Vec<Token>,
    nodes: BTreeSet<ModuleId>,
    entry: ModuleId,
    pairs: BTreeSet<(ModuleId, ModuleId)>,
    successors: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    preferred: EsmImportOrder,
}

struct Solver<'p, 'i, 'o> {
    problem: &'p AssignmentProblem<'i>,
    options: &'o SearchOptions,
    scope: Scope,
    started: Instant,
    visited: HashSet<PartitionKey>,
    report: SearchReport,
}

pub(super) fn search(
    problem: &AssignmentProblem<'_>,
    options: &SearchOptions,
) -> Result<SearchReport> {
    let started = Instant::now();
    let (scope, required_names) = prepare_scope(problem, options)?;
    let movable_atoms = scope.atoms.len();
    let report = SearchReport {
        status: SearchStatus::ExhaustedNoPeel,
        stop_reason: None,
        checks: 0,
        work: SearchWorkReport::default(),
        movable_atoms,
        residual_units_extracted: 0,
        existing_units_moved: 0,
        scope: SearchScope {
            destinations: options.destinations,
            into: options.into.clone(),
            required_units: required_names,
            only_units: options.only_units.clone(),
            max_new_modules: options.max_new_modules,
        },
        changes: Vec::new(),
    };
    if scope.atoms.is_empty() {
        return Ok(report);
    }

    let initial = initial_partition(problem, &scope);
    let mut solver = Solver {
        problem,
        options,
        scope,
        started,
        visited: HashSet::new(),
        report,
    };
    solver.visit(initial)?;
    let report = solver.report;
    Ok(report)
}

fn prepare_scope(
    problem: &AssignmentProblem<'_>,
    options: &SearchOptions,
) -> Result<(Scope, Vec<String>)> {
    let resolve = |ids: &[String]| -> Result<BTreeSet<SourceUnitId>> {
        ids.iter()
            .map(|id| {
                problem
                    .input
                    .source_unit_id(id)
                    .with_context(|| format!("unknown source unit {id:?}"))
            })
            .collect()
    };
    let required = resolve(&options.required)?;
    let only = options.only_units.as_deref().map(resolve).transpose()?;
    let mut atoms = Vec::new();
    for atom in &problem.atoms {
        let permitted = atom.members.iter().all(|unit| {
            problem.residual_origins[unit.0]
                && !matches!(
                    problem.input.source_unit_report(*unit).statement_kind,
                    StatementKind::Import | StatementKind::Export
                )
        });
        if let Some(only) = &only {
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
                "--only-units contains authored units or import/export scaffolding"
            );
        }
        if permitted {
            atoms.push(MoveAtom {
                members: atom.members.iter().map(|unit| unit.0).collect(),
                required: false,
            });
        }
    }
    let movable_units: BTreeSet<_> = atoms
        .iter()
        .flat_map(|atom| atom.members.iter().copied())
        .collect();
    let movable_ids: BTreeSet<_> = movable_units
        .iter()
        .map(|unit| SourceUnitId(*unit))
        .collect();
    ensure!(
        required.is_subset(&movable_ids),
        "--require must name movable residual body units inside the permitted scope"
    );
    let required_units: BTreeSet<_> = required.iter().map(|unit| unit.0).collect();
    for atom in &mut atoms {
        atom.required = atom
            .members
            .iter()
            .any(|unit| required_units.contains(unit));
    }

    let existing_targets: BTreeSet<_> = problem
        .existing_modules
        .difference(&problem.residual_modules)
        .cloned()
        .collect();
    let new_limit = options.max_new_modules.unwrap_or(atoms.len());
    let mut into = BTreeSet::new();
    let mut explicit_fresh_targets = BTreeSet::new();
    if options.into.is_empty() {
        if !matches!(options.destinations, Destinations::New) {
            into.extend(existing_targets.iter().cloned());
        }
    } else {
        for raw in &options.into {
            ensure!(
                !std::path::Path::new(raw).is_absolute() && !raw.contains('\0'),
                "destination must be a relative module path"
            );
            let path = ModulePath::parse(raw, "")?;
            ensure!(
                !path.as_str().ends_with(".yaml") && !path.as_str().ends_with(".yml"),
                "destination must be extensionless"
            );
            let is_existing = existing_targets.contains(&path);
            ensure!(
                is_existing || new_limit > 0,
                "--into names a fresh owner but --max-new-modules is zero"
            );
            ensure!(
                !problem.residual_modules.contains(&path)
                    && (!is_residual_namespace_path(path.as_str()) || is_existing),
                "destination is reserved for generated residual output"
            );
            ensure!(
                match options.destinations {
                    Destinations::New => !is_existing,
                    Destinations::Existing => is_existing,
                    Destinations::Both => true,
                },
                "--into conflicts with --destinations"
            );
            if is_existing {
                into.insert(path);
            } else {
                explicit_fresh_targets.insert(path);
            }
        }
    }
    let allow_arbitrary_fresh = options.into.is_empty()
        && !matches!(options.destinations, Destinations::Existing)
        && new_limit > 0;
    let effective_new_limit = if options.into.is_empty() {
        new_limit
    } else {
        new_limit.min(explicit_fresh_targets.len())
    };
    let baseline_owners: BTreeSet<_> = problem
        .existing_modules
        .iter()
        .cloned()
        .map(Owner::Module)
        .chain(std::iter::once(Owner::Entry))
        .collect();
    let mut permitted_targets = into;
    permitted_targets.extend(explicit_fresh_targets.iter().cloned());

    let final_atoms = atoms;
    let scope = Scope {
        atoms: final_atoms,
        movable_units,
        required_units,
        allowed_existing_targets: permitted_targets
            .iter()
            .filter(|path| !explicit_fresh_targets.contains(*path))
            .cloned()
            .collect(),
        explicit_fresh_targets,
        baseline_owners,
        residual_modules: problem.residual_modules.clone(),
        effective_new_limit,
        allow_arbitrary_fresh,
    };
    Ok((scope, options.required.clone()))
}

fn owner_for_path(path: Option<&ModulePath>) -> Owner {
    path.map_or(Owner::Entry, |path| Owner::Module(path.clone()))
}

fn initial_partition(problem: &AssignmentProblem<'_>, scope: &Scope) -> Partition {
    let mut atom_units = BTreeSet::new();
    let mut blocks = Vec::new();
    let mut owners: BTreeSet<_> = scope.baseline_owners.iter().cloned().collect();
    owners.extend(
        scope
            .explicit_fresh_targets
            .iter()
            .cloned()
            .map(Owner::Module),
    );
    let mut fixed_units = BTreeMap::<Owner, BTreeSet<usize>>::new();
    for unit in 0..problem.baseline.len() {
        if scope.movable_units.contains(&unit) {
            atom_units.insert(unit);
            continue;
        }
        let owner = owner_for_path(problem.baseline[unit].as_ref());
        fixed_units.entry(owner).or_default().insert(unit);
    }
    for owner in owners {
        let mut members = fixed_units.remove(&owner).unwrap_or_default();
        if owner == Owner::Entry {
            // None is a complete baseline owner, even if every entry-origin
            // atom is movable in the query.
            members.extend(fixed_units.remove(&owner).unwrap_or_default());
        }
        blocks.push(Block {
            tokens: BTreeSet::from([Token::Owner(owner.clone())]),
            units: members,
            owner: Some(owner.clone()),
            allowed_owners: BTreeSet::from([owner]),
            allow_fresh: false,
        });
    }
    debug_assert!(fixed_units.is_empty());

    for (index, atom) in scope.atoms.iter().enumerate() {
        let baseline_unit = *atom
            .members
            .first()
            .expect("every movable atom contains at least one unit");
        let baseline_owner = owner_for_path(problem.baseline[baseline_unit].as_ref());
        let mut allowed_owners: BTreeSet<_> = scope
            .allowed_existing_targets
            .iter()
            .cloned()
            .chain(scope.explicit_fresh_targets.iter().cloned())
            .map(Owner::Module)
            .collect();
        allowed_owners.insert(baseline_owner.clone());
        if atom.required {
            allowed_owners.remove(&baseline_owner);
        }
        blocks.push(Block {
            tokens: BTreeSet::from([Token::Atom(index)]),
            units: atom.members.clone(),
            owner: None,
            allowed_owners,
            allow_fresh: scope.allow_arbitrary_fresh,
        });
    }
    Partition::new(blocks)
}

impl Solver<'_, '_, '_> {
    fn visit(&mut self, mut state: Partition) -> Result<()> {
        loop {
            let Some(normalized) = self.propagate_owner_domains(state)? else {
                return Ok(());
            };
            state = normalized;
            let key = state.key();
            if self.visited.contains(&key) {
                return Ok(());
            }
            if self.stop_if_limited() {
                return Ok(());
            }
            self.visited.insert(key);
            self.report.checks = self
                .report
                .checks
                .checked_add(1)
                .context("placement quotient-state count overflow")?;
            self.report.work.partition_states = self.report.checks;

            self.report.work.quotient_projections =
                self.report.work.quotient_projections.saturating_add(1);
            let Some(projection) = self.project(&state)? else {
                return Ok(());
            };

            if let Some((left, right)) = self.cross_rebind_force(&projection) {
                let Some(next) = self.try_forced_merge(&state, &left, &right) else {
                    return Ok(());
                };
                state = next;
                continue;
            }

            if let Some((consumer, _dependency)) = projection
                .pairs
                .iter()
                .copied()
                .find(|(from, to)| *to == projection.entry && *from != projection.entry)
            {
                let Some(consumer_block) = projection.block_by_module.get(&consumer).cloned()
                else {
                    bail!("entry constraint has no source owner block");
                };
                let entry_block = Token::Owner(Owner::Entry);
                let Some(next) = self.try_forced_merge(&state, &consumer_block, &entry_block)
                else {
                    return Ok(());
                };
                if next == state {
                    return Ok(());
                }
                state = next;
                continue;
            }

            if let Some(blocks) = self.constraining_cycle(&projection)? {
                let first = blocks.first().expect("a constraining cycle has blocks");
                for block in blocks.iter().skip(1) {
                    if self.stop_if_time() {
                        return Ok(());
                    }
                    let Some(next) = self.try_forced_merge(&state, first, block) else {
                        return Ok(());
                    };
                    state = next;
                }
                continue;
            }

            if self.stop_if_time() {
                return Ok(());
            }
            let schedule = EsmImportSchedule::construct(
                projection.entry,
                &projection.pairs,
                &projection.successors,
                &projection.nodes,
                &projection.preferred,
            );
            if let Err(obstruction) = &schedule {
                let branches =
                    self.schedule_obstruction_branches(&state, &projection, obstruction)?;
                self.visit_branches(branches)?;
                return Ok(());
            }

            let cap_branches = self.new_owner_cap_branches(&state)?;
            if let Some(branches) = cap_branches {
                self.visit_branches(branches)?;
                return Ok(());
            }

            if let Some(block) = state
                .blocks
                .iter()
                .find(|block| block.owner.is_none() && !block.allow_fresh)
            {
                self.report.work.branch_clauses.owner_domain = self
                    .report
                    .work
                    .branch_clauses
                    .owner_domain
                    .saturating_add(1);
                let mut branches = Vec::new();
                for owner in &block.allowed_owners {
                    if self.stop_if_time() {
                        return Ok(());
                    }
                    let owner_block = Token::Owner(owner.clone());
                    if let Some(next) = self.try_branch_merge(&state, &block.key(), &owner_block)
                        && next != state
                    {
                        branches.push(next);
                    }
                }
                self.visit_branches(branches)?;
                return Ok(());
            }

            self.certify_candidate(projection.candidate)?;
            return Ok(());
        }
    }

    fn propagate_owner_domains(&mut self, mut state: Partition) -> Result<Option<Partition>> {
        loop {
            if self.stop_if_time() {
                return Ok(None);
            }
            if state.blocks.iter().any(|block| {
                block.owner.is_none() && !block.allow_fresh && block.allowed_owners.is_empty()
            }) {
                return Ok(None);
            }
            let forced = state.blocks.iter().find_map(|block| {
                if block.owner.is_none() && !block.allow_fresh && block.allowed_owners.len() == 1 {
                    Some((block.key(), block.allowed_owners.first()?.clone()))
                } else {
                    None
                }
            });
            let Some((block, owner)) = forced else {
                return Ok(Some(state));
            };
            let owner_block = Token::Owner(owner);
            let Some(next) = self.try_forced_merge(&state, &block, &owner_block) else {
                return Ok(None);
            };
            if next == state {
                return Ok(None);
            }
            state = next;
        }
    }

    fn stop_if_limited(&mut self) -> bool {
        if self.stop_if_time() {
            return true;
        }
        if self
            .options
            .max_checks
            .is_some_and(|limit| self.report.checks >= limit)
        {
            self.report.status = SearchStatus::Incomplete;
            self.report.stop_reason = Some(StopReason::MaxChecks);
            return true;
        }
        false
    }

    fn stop_if_time(&mut self) -> bool {
        if self
            .options
            .time_budget
            .is_some_and(|budget| self.started.elapsed() >= budget)
        {
            self.report.status = SearchStatus::Incomplete;
            self.report.stop_reason = Some(StopReason::TimeBudget);
            return true;
        }
        false
    }

    fn try_forced_merge(
        &mut self,
        state: &Partition,
        left: &Token,
        right: &Token,
    ) -> Option<Partition> {
        let merged = state.merge(left, right)?;
        if merged != *state {
            self.report.work.forced_contractions =
                self.report.work.forced_contractions.saturating_add(1);
        }
        Some(merged)
    }

    fn try_branch_merge(
        &mut self,
        state: &Partition,
        left: &Token,
        right: &Token,
    ) -> Option<Partition> {
        self.report.work.contraction_attempts =
            self.report.work.contraction_attempts.saturating_add(1);
        state.merge(left, right)
    }

    fn visit_branches(&mut self, branches: Vec<Partition>) -> Result<()> {
        for branch in branches {
            if self.report.status != SearchStatus::ExhaustedNoPeel {
                break;
            }
            if self.stop_if_time() {
                break;
            }
            self.visit(branch)?;
        }
        Ok(())
    }

    fn project(&mut self, state: &Partition) -> Result<Option<Projection>> {
        let Some(candidate) = self.materialize_provisional(state)? else {
            return Ok(None);
        };
        let mut paths = self.problem.ordered_modules(&candidate.assignment);
        let mut path_set: BTreeSet<_> = paths.iter().cloned().collect();
        for owner in candidate.owners.values() {
            if let Some(path) = owner.path()
                && path_set.insert(path.clone())
            {
                paths.push(path.clone());
            }
        }
        let module_ids: BTreeMap<_, _> = paths
            .iter()
            .enumerate()
            .map(|(index, path)| (path.clone(), ModuleId(index)))
            .collect();
        let entry = ModuleId(paths.len());
        let module_assignment = ModuleAssignment::from_assignments(
            candidate
                .assignment
                .iter()
                .map(|path| path.as_ref().map_or(entry, |path| module_ids[path]))
                .collect(),
            EntryDestination::from_module_id(entry),
        );
        let nodes: BTreeSet<_> = (0..=paths.len()).map(ModuleId).collect();
        let canonical =
            chunk_constraining_module_edges(self.problem.input.graph(), &module_assignment);
        let pairs: BTreeSet<_> = canonical.pairs().collect();
        let successors = canonical.i_successors.clone();
        let preferred = EsmImportOrder::build_for_assignment(
            &canonical,
            self.problem.input.graph(),
            &module_assignment,
            &nodes,
        );
        let mut block_by_module = BTreeMap::new();
        let Some(first_block) = state.blocks.first() else {
            return Ok(None);
        };
        let mut block_by_unit = vec![first_block.key(); candidate.assignment.len()];
        for block in &state.blocks {
            for &unit in &block.units {
                block_by_unit[unit] = block.key();
            }
            let owner = candidate
                .owners
                .get(&block.key())
                .expect("every partition block has a provisional owner");
            let module = owner.path().map_or(entry, |path| module_ids[path]);
            block_by_module.insert(module, block.key());
        }

        Ok(Some(Projection {
            candidate,
            module_assignment,
            block_by_module,
            block_by_unit,
            nodes,
            entry,
            pairs,
            successors,
            preferred,
        }))
    }

    fn materialize_provisional(&mut self, state: &Partition) -> Result<Option<Candidate>> {
        let mut reserved: BTreeSet<_> = self
            .scope
            .baseline_owners
            .iter()
            .filter_map(Owner::path)
            .cloned()
            .chain(self.scope.explicit_fresh_targets.iter().cloned())
            .collect();
        let mut owners = BTreeMap::new();
        for block in &state.blocks {
            if let Some(owner) = &block.owner {
                owners.insert(block.key(), owner.clone());
            }
        }
        let mut suffix = 0usize;
        for block in &state.blocks {
            if block.owner.is_none() {
                if self.stop_if_time() {
                    return Ok(None);
                }
                let path = loop {
                    if self.stop_if_time() {
                        return Ok(None);
                    }
                    let raw = format!("extracted/peel_{suffix:04}");
                    suffix = suffix
                        .checked_add(1)
                        .context("fresh owner index overflow")?;
                    let path = ModulePath::parse(&raw, "")?;
                    if !reserved.contains(&path) {
                        break path;
                    }
                };
                reserved.insert(path.clone());
                owners.insert(block.key(), Owner::Module(path));
            }
        }
        let mut assignment = self.problem.baseline.clone();
        for block in &state.blocks {
            let owner = owners
                .get(&block.key())
                .expect("every partition block has a provisional owner");
            for &unit in &block.units {
                assignment[unit] = owner.path().cloned();
            }
        }
        let occupied_fresh_owners = state
            .blocks
            .iter()
            .filter(|block| block.occupied())
            .filter_map(|block| {
                let owner = owners.get(&block.key())?;
                match owner {
                    Owner::Module(path)
                        if self.scope.explicit_fresh_targets.contains(path)
                            || !self.scope.baseline_owners.contains(owner) =>
                    {
                        Some(owner.clone())
                    }
                    _ => None,
                }
            })
            .collect();
        Ok(Some(Candidate {
            owners,
            occupied_fresh_owners,
            assignment,
        }))
    }

    fn cross_rebind_force(&self, projection: &Projection) -> Option<(Token, Token)> {
        self.problem
            .input
            .graph()
            .iter_edges()
            .filter(|edge| edge.reason.is_rebind())
            .find_map(|edge| {
                let endpoints =
                    project_endpoints(edge, projection.entry, EndpointView::Gate, |unit| {
                        projection.module_assignment.of(unit)
                    })?;
                if endpoints.0 == endpoints.1 {
                    return None;
                }
                let left = projection.block_by_unit.get(edge.from.0)?.clone();
                let right = projection.block_by_unit.get(edge.to.0)?.clone();
                (left != right).then_some((left, right))
            })
    }

    /// Every current C edge preserves nonincreasing final postorder rank.
    /// A fallback edge can disappear only when its caller becomes true entry,
    /// which finishes last. Both directed paths in an SCC therefore force equal
    /// ranks, hence the same final owner, even for fallback-promoted edges.
    fn constraining_cycle(&self, projection: &Projection) -> Result<Option<BTreeSet<Token>>> {
        let mut graph = DiGraphMap::<ModuleId, ()>::new();
        for &node in &projection.nodes {
            graph.add_node(node);
        }
        for &(from, to) in &projection.pairs {
            graph.add_edge(from, to, ());
        }
        let mut components: Vec<BTreeSet<_>> = tarjan_scc(&graph)
            .into_iter()
            .filter(|component| component.len() > 1)
            .map(|component| component.into_iter().collect())
            .collect();
        components.sort_by_key(|component| component.iter().next().copied());
        let Some(component) = components.into_iter().next() else {
            return Ok(None);
        };
        let block_keys: BTreeSet<_> = component
            .iter()
            .filter_map(|module| projection.block_by_module.get(module).cloned())
            .collect();
        if block_keys.len() != component.len() {
            bail!("constraining SCC has no unique placement block for each module");
        }
        Ok(Some(block_keys))
    }

    fn schedule_obstruction_branches(
        &mut self,
        state: &Partition,
        projection: &Projection,
        obstruction: &ScheduleObstruction,
    ) -> Result<Vec<Partition>> {
        self.report.work.branch_clauses.schedule_closure = self
            .report
            .work
            .branch_clauses
            .schedule_closure
            .saturating_add(1);
        let mut in_closure = BTreeSet::new();
        for module in &obstruction.forward_closure {
            if self.stop_if_time() {
                return Ok(Vec::new());
            }
            if let Some(block) = projection.block_by_module.get(module) {
                in_closure.insert(block.clone());
            }
        }
        let mut branches = Vec::new();
        for (index, left) in state.blocks.iter().enumerate() {
            if self.stop_if_time() {
                break;
            }
            for right in state.blocks.iter().skip(index + 1) {
                if self.stop_if_time() {
                    break;
                }
                let left_key = left.key();
                let right_key = right.key();
                if !in_closure.contains(&left_key) && !in_closure.contains(&right_key) {
                    continue;
                }
                if let Some(merged) = self.try_branch_merge(state, &left_key, &right_key)
                    && merged != *state
                {
                    branches.push(merged);
                }
            }
        }
        branches.sort_by_key(Partition::key);
        branches.dedup();
        Ok(branches)
    }

    fn new_owner_cap_branches(&mut self, state: &Partition) -> Result<Option<Vec<Partition>>> {
        let occupied_fresh: Vec<_> = state
            .blocks
            .iter()
            .filter(|block| {
                block.occupied()
                    && match &block.owner {
                        Some(Owner::Entry) => false,
                        Some(Owner::Module(path)) => {
                            self.scope.explicit_fresh_targets.contains(path)
                        }
                        None => block.allow_fresh,
                    }
            })
            .map(Block::key)
            .collect();
        if occupied_fresh.len() <= self.scope.effective_new_limit {
            return Ok(None);
        }
        self.report.work.branch_clauses.fresh_owner_cap = self
            .report
            .work
            .branch_clauses
            .fresh_owner_cap
            .saturating_add(1);
        let selected: Vec<_> = occupied_fresh
            .into_iter()
            .take(self.scope.effective_new_limit.saturating_add(1))
            .collect();
        let selected_set: BTreeSet<_> = selected.iter().cloned().collect();
        let mut branches = Vec::new();
        for (index, left) in selected.iter().enumerate() {
            if self.stop_if_time() {
                break;
            }
            for right in selected.iter().skip(index + 1) {
                if self.stop_if_time() {
                    break;
                }
                if let Some(merged) = self.try_branch_merge(state, left, right)
                    && merged != *state
                {
                    branches.push(merged);
                }
            }
        }
        let nonnew_owners: Vec<_> = self
            .scope
            .baseline_owners
            .iter()
            .filter(|&owner| !matches!(owner, Owner::Module(path) if self.scope.explicit_fresh_targets.contains(path)))
            .cloned()
            .collect();
        for key in &selected_set {
            if self.stop_if_time() {
                break;
            }
            let Some(block) = state.block(key) else {
                continue;
            };
            if block.owner.is_some() {
                continue;
            }
            for owner in &nonnew_owners {
                if self.stop_if_time() {
                    break;
                }
                let target = Token::Owner(owner.clone());
                if let Some(merged) = self.try_branch_merge(state, key, &target)
                    && merged != *state
                {
                    branches.push(merged);
                }
            }
        }
        branches.sort_by_key(Partition::key);
        branches.dedup();
        Ok(Some(branches))
    }

    fn certify_candidate(&mut self, candidate: Candidate) -> Result<()> {
        if self.stop_if_time() {
            return Ok(());
        }
        ensure!(
            self.in_scope(&candidate),
            "constructed placement disagreed with its move scope"
        );
        let gain = self.problem.residual_gain(&candidate.assignment);
        if gain == 0 {
            return Ok(());
        }
        self.report.work.final_certifications =
            self.report.work.final_certifications.saturating_add(1);
        let validation = self.problem.validate(&candidate.assignment)?;
        ensure!(
            accepted(&validation),
            "constructive placement disagreed with final assignment certification"
        );
        self.report.status = SearchStatus::Found;
        self.report.residual_units_extracted = gain;
        self.report.existing_units_moved = self.problem.existing_units_moved(&candidate.assignment);
        self.report.changes = candidate
            .assignment
            .iter()
            .zip(&self.problem.baseline)
            .enumerate()
            .filter(|(_, (after, before))| after != before)
            .map(|(index, (after, _))| SourceUnitAssignment {
                unit_id: self
                    .problem
                    .input
                    .source_unit_report(SourceUnitId(index))
                    .id
                    .clone(),
                module: after
                    .as_ref()
                    .expect("a changed movable unit cannot be assigned to entry")
                    .as_str()
                    .to_string(),
            })
            .collect();
        Ok(())
    }

    fn in_scope(&self, candidate: &Candidate) -> bool {
        if candidate.occupied_fresh_owners.len() > self.scope.effective_new_limit {
            return false;
        }
        self.scope.required_units.iter().all(|unit| {
            candidate.assignment[*unit]
                .as_ref()
                .is_some_and(|path| !self.scope.residual_modules.contains(path))
        })
    }
}
