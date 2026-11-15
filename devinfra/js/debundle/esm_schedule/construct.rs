//! Derive an actual ESM traversal, committing successful subtrees rather than
//! enumerating import-list permutations. See docs/module_placement.md for the
//! successor-closure and completion-preservation proofs.

use std::collections::{BTreeMap, BTreeSet, HashSet, VecDeque};

use analysis::ids::ModuleId;
use petgraph::{
    algo::{kosaraju_scc, toposort},
    graphmap::DiGraphMap,
};

use super::EsmImportSchedule;
use crate::esm_import_order::EsmImportOrder;

/// An intrinsically unschedulable component, with all nonentry modules it can
/// reach. Contracting only outside this closure cannot repair its traversal.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ScheduleObstruction {
    pub component: BTreeSet<ModuleId>,
    pub forward_closure: BTreeSet<ModuleId>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Color {
    White,
    Active,
    Finished,
}

struct Frame {
    root: usize,
    checkpoint: usize,
    pending: VecDeque<usize>,
    failures: usize,
}

struct Constructor<'a, E> {
    modules: Vec<ModuleId>,
    indices: BTreeMap<ModuleId, usize>,
    imports: Vec<Vec<usize>>,
    consumers: Vec<Vec<usize>>,
    colors: Vec<Color>,
    white: Vec<u64>,
    ordered: Vec<Vec<usize>>,
    trail: Vec<usize>,
    seen: Vec<u64>,
    forbidden: Vec<u64>,
    generation: u64,
    work: Vec<usize>,
    failures: Vec<HashSet<Vec<u64>>>,
    cached_failures: usize,
    failure_cache_limit: usize,
    poll: &'a mut dyn FnMut() -> Result<(), E>,
}

impl EsmImportSchedule {
    /// Preserve the preferred policy if valid, otherwise derive a schedule from
    /// the complete canonical graph. Failure is exact, including alternate
    /// roots and import orders. Module names only affect the preferred witness.
    pub fn construct(
        entry: ModuleId,
        pairs: &BTreeSet<(ModuleId, ModuleId)>,
        successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        nodes: &BTreeSet<ModuleId>,
        preferred: &EsmImportOrder,
    ) -> Result<Self, ScheduleObstruction> {
        match Self::construct_with_poll(entry, pairs, successors, nodes, preferred, || {
            Ok::<(), std::convert::Infallible>(())
        }) {
            Ok(result) => result,
            Err(never) => match never {},
        }
    }

    /// Like [`Self::construct`], but checks `poll` throughout graph traversal.
    /// A poll error is kept separate from an intrinsic schedule obstruction so
    /// callers can report cancellation or deadlines without false exhaustion.
    pub fn construct_with_poll<E>(
        entry: ModuleId,
        pairs: &BTreeSet<(ModuleId, ModuleId)>,
        successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        nodes: &BTreeSet<ModuleId>,
        preferred: &EsmImportOrder,
        mut poll: impl FnMut() -> Result<(), E>,
    ) -> Result<Result<Self, ScheduleObstruction>, E> {
        poll()?;
        let policy = Self::from_policy(entry, successors, nodes, preferred);
        if is_tdz_free(&policy, pairs, &mut poll)? {
            return Ok(Ok(policy));
        }
        for &(consumer, dependency) in pairs {
            poll()?;
            if dependency == entry {
                return Ok(Err(obstruction(
                    BTreeSet::from([consumer]),
                    entry,
                    &policy,
                    &mut poll,
                )?));
            }
        }

        // Entry can enter every component after its dependencies have finished.
        // Keep completion trials local: downstream components cannot affect the
        // existence of an internal schedule, and need not be traversed again.
        let mut graph = DiGraphMap::<ModuleId, ()>::new();
        for &node in nodes {
            poll()?;
            if node != entry {
                graph.add_node(node);
            }
        }
        for (&from, targets) in successors {
            poll()?;
            for &to in targets {
                poll()?;
                if from != entry && to != entry {
                    graph.add_edge(from, to, ());
                }
            }
        }
        poll()?;
        // Kosaraju's explicit walkers also avoid recursion on deep I graphs.
        let components = kosaraju_scc(&graph);
        let mut component_of = BTreeMap::new();
        let mut condensation = DiGraphMap::<usize, ()>::new();
        for (index, component) in components.iter().enumerate() {
            poll()?;
            condensation.add_node(index);
            for &module in component {
                poll()?;
                component_of.insert(module, index);
            }
        }
        let mut internal_imports = vec![BTreeMap::new(); components.len()];
        for (&module, &index) in &component_of {
            poll()?;
            let mut internal = Vec::new();
            for &target in policy.imports(module) {
                poll()?;
                if target == entry {
                    continue;
                }
                let target_index = component_of[&target];
                if index == target_index {
                    internal.push(target);
                } else {
                    condensation.add_edge(index, target_index, ());
                }
            }
            internal_imports[index].insert(module, internal);
        }
        let mut internal_constraints = vec![BTreeMap::<_, Vec<_>>::new(); components.len()];
        for &(consumer, dependency) in pairs {
            poll()?;
            if consumer != entry && component_of[&consumer] == component_of[&dependency] {
                internal_constraints[component_of[&consumer]]
                    .entry(consumer)
                    .or_default()
                    .push(dependency);
            }
        }
        let entry_rank: BTreeMap<_, _> = policy
            .imports(entry)
            .iter()
            .copied()
            .enumerate()
            .map(|(rank, module)| (module, rank))
            .collect();
        poll()?;
        let order = toposort(&condensation, None).expect("SCC condensation is acyclic");
        let mut imports = policy.imports.clone();
        let mut roots = Vec::with_capacity(components.len());
        for index in order.into_iter().rev() {
            poll()?;
            let mut candidates = components[index].clone();
            candidates.sort_by_key(|module| entry_rank[module]);
            let constraints = &internal_constraints[index];
            if constraints.is_empty() {
                // With no internal constraints every traversal of this SCC is
                // valid. Its external dependencies are already finished.
                roots.push(candidates[0]);
                continue;
            }
            let mut constrained_dependencies = BTreeSet::new();
            for &dependency in constraints.values().flatten() {
                poll()?;
                constrained_dependencies.insert(dependency);
            }
            let chosen = {
                let mut constructor =
                    Constructor::new(&internal_imports[index], constraints, &mut poll)?;
                let mut chosen = None;
                for root in candidates {
                    constructor.poll()?;
                    // Every SCC member must finish before its first root. A
                    // constraining incoming edge therefore forbids this root.
                    if constrained_dependencies.contains(&root) {
                        continue;
                    }
                    if constructor.complete(root)? {
                        chosen = Some((root, constructor.schedule()?));
                        break;
                    }
                }
                chosen
            };
            let Some((root, local_imports)) = chosen else {
                return Ok(Err(obstruction(
                    components[index].iter().copied().collect(),
                    entry,
                    &policy,
                    &mut poll,
                )?));
            };
            roots.push(root);
            for (module, mut targets) in local_imports {
                poll()?;
                for &target in policy.imports(module) {
                    poll()?;
                    if target == entry || component_of[&target] != index {
                        targets.push(target);
                    }
                }
                imports.insert(module, targets);
            }
        }
        // The first root visits its complete SCC; later entry targets within
        // that component are mandatory imports but cause no additional DFS.
        let selected: BTreeSet<_> = roots.iter().copied().collect();
        for &target in policy.imports(entry) {
            poll()?;
            if !selected.contains(&target) {
                roots.push(target);
            }
        }
        imports.insert(entry, roots);
        poll()?;
        let result = Self::from_imports(entry, successors, nodes, imports)
            .expect("component schedules contain exactly the canonical targets");
        assert!(
            is_tdz_free(&result, pairs, &mut poll)?,
            "constructor returned a TDZ"
        );
        Ok(Ok(result))
    }
}

fn is_tdz_free<E>(
    schedule: &EsmImportSchedule,
    pairs: &BTreeSet<(ModuleId, ModuleId)>,
    poll: &mut impl FnMut() -> Result<(), E>,
) -> Result<bool, E> {
    for &(consumer, dependency) in pairs {
        poll()?;
        if schedule.post_order()[&dependency] >= schedule.post_order()[&consumer] {
            return Ok(false);
        }
    }
    Ok(true)
}

fn obstruction<E>(
    component: BTreeSet<ModuleId>,
    entry: ModuleId,
    policy: &EsmImportSchedule,
    poll: &mut impl FnMut() -> Result<(), E>,
) -> Result<ScheduleObstruction, E> {
    let mut forward_closure = component.clone();
    let mut work: Vec<_> = component.iter().copied().collect();
    while let Some(node) = work.pop() {
        poll()?;
        for &target in policy.imports(node) {
            poll()?;
            if target != entry && forward_closure.insert(target) {
                work.push(target);
            }
        }
    }
    Ok(ScheduleObstruction {
        component,
        forward_closure,
    })
}

impl<'a, E> Constructor<'a, E> {
    fn new(
        imports: &BTreeMap<ModuleId, Vec<ModuleId>>,
        constraints: &BTreeMap<ModuleId, Vec<ModuleId>>,
        poll: &'a mut dyn FnMut() -> Result<(), E>,
    ) -> Result<Self, E> {
        let modules: Vec<_> = imports.keys().copied().collect();
        let indices: BTreeMap<_, _> = modules
            .iter()
            .enumerate()
            .map(|(index, &module)| (module, index))
            .collect();
        let count = modules.len();
        let mut adjacency = vec![Vec::new(); count];
        let mut consumers = vec![Vec::new(); count];
        for (&module, targets) in imports {
            poll()?;
            for target in targets {
                poll()?;
                adjacency[indices[&module]].push(indices[target]);
            }
        }
        for (consumer, dependencies) in constraints {
            poll()?;
            for dependency in dependencies {
                poll()?;
                consumers[indices[dependency]].push(indices[consumer]);
            }
        }
        let mut white = vec![u64::MAX; count.div_ceil(64)];
        if count % 64 != 0 {
            *white.last_mut().expect("nonempty component") = (1 << (count % 64)) - 1;
        }
        let payload_bytes = white.len().saturating_mul(size_of::<u64>()).max(1);
        Ok(Self {
            modules,
            indices,
            imports: adjacency,
            consumers,
            colors: vec![Color::White; count],
            white,
            ordered: vec![Vec::new(); count],
            trail: Vec::new(),
            seen: vec![0; count],
            forbidden: vec![0; count],
            generation: 0,
            work: Vec::new(),
            failures: vec![HashSet::new(); count],
            cached_failures: 0,
            failure_cache_limit: 4_096.min((8 * 1024 * 1024) / payload_bytes),
            poll,
        })
    }

    fn poll(&mut self) -> Result<(), E> {
        (self.poll)()
    }

    fn next_generation(&mut self) -> u64 {
        self.generation = self.generation.wrapping_add(1);
        if self.generation == 0 {
            self.seen.fill(0);
            self.forbidden.fill(0);
            self.generation = 1;
        }
        self.generation
    }

    fn remember_failure(&mut self, root: usize) {
        // Cache capacity only bounds storage: every miss still runs the exact
        // constructor. Bound both entry overhead and stored white-bit payload.
        if self.cached_failures < self.failure_cache_limit
            && self.failures[root].insert(self.white.clone())
        {
            self.cached_failures += 1;
        }
    }

    fn admissible(&mut self, root: usize) -> Result<bool, E> {
        self.poll()?;
        if !self.failures[root].is_empty() && self.failures[root].contains(&self.white) {
            return Ok(false);
        }
        let generation = self.next_generation();
        let mut has_white_consumer = false;
        for index in 0..self.consumers[root].len() {
            self.poll()?;
            let consumer = self.consumers[root][index];
            if self.colors[consumer] == Color::White {
                self.forbidden[consumer] = generation;
                has_white_consumer = true;
            }
        }
        if !has_white_consumer {
            return Ok(true);
        }
        // Ancestors already rejected all then-white reachable reads into
        // themselves. White vertices/reachability only shrink, so the only new
        // forbidden dependency is this root. No incoming white C means no walk.
        self.work.clear();
        self.work.push(root);
        self.seen[root] = generation;
        if self.forbidden[root] == generation {
            self.remember_failure(root);
            return Ok(false);
        }
        while let Some(node) = self.work.pop() {
            self.poll()?;
            for index in 0..self.imports[node].len() {
                self.poll()?;
                let target = self.imports[node][index];
                if self.colors[target] == Color::White && self.seen[target] != generation {
                    if self.forbidden[target] == generation {
                        self.remember_failure(root);
                        return Ok(false);
                    }
                    self.seen[target] = generation;
                    self.work.push(target);
                }
            }
        }
        Ok(true)
    }

    fn enter(&mut self, root: usize) -> Frame {
        let checkpoint = self.trail.len();
        self.trail.push(root);
        self.colors[root] = Color::Active;
        self.white[root / 64] &= !(1 << (root % 64));
        Frame {
            root,
            checkpoint,
            pending: self.imports[root].iter().copied().collect(),
            failures: 0,
        }
    }

    fn rollback(&mut self, checkpoint: usize) -> Result<(), E> {
        while self.trail.len() > checkpoint {
            self.poll()?;
            let node = self.trail.pop().expect("entry after checkpoint");
            self.colors[node] = Color::White;
            self.white[node / 64] |= 1 << (node % 64);
            self.ordered[node].clear();
        }
        Ok(())
    }

    fn finish(&mut self, root: usize) -> Result<(), E> {
        // Chosen white children go first; every other canonical target is now
        // nonwhite. Stamp chosen targets instead of quadratic Vec::contains.
        let generation = self.next_generation();
        for index in 0..self.ordered[root].len() {
            self.poll()?;
            self.seen[self.ordered[root][index]] = generation;
        }
        for index in 0..self.imports[root].len() {
            self.poll()?;
            let target = self.imports[root][index];
            if self.seen[target] != generation {
                self.ordered[root].push(target);
            }
        }
        self.colors[root] = Color::Finished;
        Ok(())
    }

    fn complete(&mut self, root: ModuleId) -> Result<bool, E> {
        let root = self.indices[&root];
        debug_assert!(self.colors[root] == Color::White);
        if !self.admissible(root)? {
            return Ok(false);
        }
        let mut frames = vec![self.enter(root)];
        while let Some(frame) = frames.last_mut() {
            self.poll()?;
            let root = frame.root;
            let failed = frame.failures > 0 && frame.failures == frame.pending.len();
            if !failed && let Some(target) = frame.pending.pop_front() {
                if self.colors[target] == Color::White {
                    if self.admissible(target)? {
                        frames.push(self.enter(target));
                    } else {
                        frame.pending.push_back(target);
                        frame.failures += 1;
                    }
                }
                continue;
            }
            let frame = frames.pop().expect("current completion frame");
            if failed {
                self.rollback(frame.checkpoint)?;
                self.remember_failure(root);
                if let Some(parent) = frames.last_mut() {
                    parent.pending.push_back(root);
                    parent.failures += 1;
                } else {
                    return Ok(false);
                }
            } else {
                self.finish(root)?;
                if let Some(parent) = frames.last_mut() {
                    self.ordered[parent.root].push(root);
                    // Failed children may now be feasible with the successful
                    // successor-closed subtree black. Only pending imports need
                    // retrying; completed imports never return to the queue.
                    parent.failures = 0;
                } else {
                    return Ok(true);
                }
            }
        }
        unreachable!("the root frame returns its completion result")
    }

    fn schedule(&mut self) -> Result<BTreeMap<ModuleId, Vec<ModuleId>>, E> {
        let mut imports = BTreeMap::new();
        for index in 0..self.modules.len() {
            self.poll()?;
            debug_assert!(self.colors[index] == Color::Finished);
            let mut targets = Vec::new();
            for target in &self.ordered[index] {
                (self.poll)()?;
                targets.push(self.modules[*target]);
            }
            imports.insert(self.modules[index], targets);
        }
        Ok(imports)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_discovered_conflict_skips_the_remaining_reachability_walk() {
        for tail_size in [0, 32, 4_096] {
            let (root, consumer, active) = (ModuleId(0), ModuleId(1), ModuleId(tail_size + 2));
            let mut imports = BTreeMap::from([
                (root, (1..active.0).map(ModuleId).collect()),
                (consumer, vec![root, active]),
                (active, vec![root]),
            ]);
            for node in 2..active.0 {
                imports.insert(ModuleId(node), vec![root]);
            }
            for dependency in [root, active] {
                let constraints = BTreeMap::from([(consumer, vec![dependency])]);
                let checkpoints = std::cell::Cell::new(0);
                let mut poll = || {
                    checkpoints.set(checkpoints.get() + 1);
                    Ok::<_, ()>(())
                };
                let mut constructor = Constructor::new(&imports, &constraints, &mut poll).unwrap();
                checkpoints.set(0);
                // Enter through the actual ancestor, so its entry predicate
                // establishes the invariant inherited by subsequent frames.
                assert!(!constructor.complete(active).unwrap());
                assert!(
                    constructor
                        .colors
                        .iter()
                        .all(|color| *color == Color::White)
                );
                assert!(constructor.ordered.iter().all(Vec::is_empty));
                assert!(
                    checkpoints.get() < 20,
                    "{tail_size}: {} checkpoints",
                    checkpoints.get()
                );
            }
        }
    }

    #[test]
    fn a_trial_does_not_scan_constraints_of_finished_consumers() {
        for finished_count in [2, 32, 4_096] {
            let (root, active) = (ModuleId(0), ModuleId(1));
            let mut imports =
                BTreeMap::from([(root, vec![active]), (active, vec![ModuleId(2), root])]);
            let mut constraints = BTreeMap::new();
            for node in 2..finished_count + 2 {
                let next = if node == finished_count + 1 {
                    active
                } else {
                    ModuleId(node + 1)
                };
                imports.insert(ModuleId(node), vec![next]);
                if next != active {
                    constraints.insert(ModuleId(node), vec![next]);
                }
            }
            let checkpoints = std::cell::Cell::new(0);
            let mut poll = || {
                checkpoints.set(checkpoints.get() + 1);
                Ok::<_, ()>(())
            };
            let mut constructor = Constructor::new(&imports, &constraints, &mut poll).unwrap();
            let active_index = constructor.indices[&active];
            assert!(constructor.admissible(active_index).unwrap());
            constructor.enter(active_index);
            assert!(constructor.complete(ModuleId(2)).unwrap());
            // The successful chain is a committed sibling; only root is white.
            checkpoints.set(0);
            assert!(constructor.complete(root).unwrap());
            assert_eq!(
                constructor.ordered[constructor.indices[&root]],
                vec![active_index]
            );
            assert!(
                checkpoints.get() < 10,
                "{finished_count}: {} checkpoints",
                checkpoints.get()
            );
        }
    }

    #[test]
    fn deep_constrained_scc_and_wide_fanout_take_linear_work() {
        for count in [32, 1_024, 20_000] {
            let mut imports: BTreeMap<_, _> = (0..count)
                .map(|node| (ModuleId(node), vec![ModuleId((node + 1) % count)]))
                .collect();
            imports
                .get_mut(&ModuleId(0))
                .unwrap()
                .push(ModuleId(count - 1));
            let constraints = BTreeMap::from([(ModuleId(0), vec![ModuleId(count - 1)])]);
            let checkpoints = std::cell::Cell::new(0);
            let mut poll = || {
                checkpoints.set(checkpoints.get() + 1);
                Ok::<_, ()>(())
            };
            let mut constructor = Constructor::new(&imports, &constraints, &mut poll).unwrap();
            assert!(constructor.complete(ModuleId(0)).unwrap());
            assert!(
                constructor
                    .colors
                    .iter()
                    .all(|color| *color == Color::Finished)
            );
            assert!(
                checkpoints.get() < 20 * count,
                "deep {count}: {} checkpoints",
                checkpoints.get()
            );
            eprintln!("deep SCC: {count} nodes, {} checkpoints", checkpoints.get());

            // A hub with independent successful children must not rescan its
            // already completed import prefix after each child returns.
            let mut imports = BTreeMap::from([(ModuleId(0), (1..count).map(ModuleId).collect())]);
            for node in 1..count {
                imports.insert(ModuleId(node), vec![ModuleId(0)]);
            }
            let constraints = BTreeMap::from([(ModuleId(0), vec![ModuleId(count - 1)])]);
            checkpoints.set(0);
            let mut constructor = Constructor::new(&imports, &constraints, &mut poll).unwrap();
            assert!(constructor.complete(ModuleId(0)).unwrap());
            assert_eq!(constructor.ordered[0].len(), count - 1);
            assert!(
                checkpoints.get() < 25 * count,
                "wide {count}: {} checkpoints",
                checkpoints.get()
            );
            eprintln!("wide SCC: {count} nodes, {} checkpoints", checkpoints.get());
        }
    }

    #[test]
    fn failed_white_states_are_reused_across_different_active_paths() {
        // Lazy prefix vertices can be visited in many orders, all leading to
        // the same intrinsically impossible C-star. Different path orders with
        // the same white set must not re-solve the same completion problem.
        let prefix_count = 8;
        let (left, right, dependency) = (
            ModuleId(prefix_count),
            ModuleId(prefix_count + 1),
            ModuleId(prefix_count + 2),
        );
        let mut imports = BTreeMap::new();
        for node in 0..prefix_count {
            imports.insert(
                ModuleId(node),
                (0..prefix_count + 3)
                    .filter(|&target| target != node)
                    .map(ModuleId)
                    .collect(),
            );
        }
        imports.insert(left, vec![dependency]);
        imports.insert(right, vec![dependency]);
        imports.insert(dependency, (0..prefix_count + 2).map(ModuleId).collect());
        let constraints = BTreeMap::from([(left, vec![dependency]), (right, vec![dependency])]);
        let measure = |memoize: bool| {
            let mut checkpoints = 0usize;
            let mut poll = || {
                checkpoints += 1;
                Ok::<_, ()>(())
            };
            let mut constructor = Constructor::new(&imports, &constraints, &mut poll).unwrap();
            if !memoize {
                constructor.failure_cache_limit = 0;
            }
            assert!(!constructor.complete(ModuleId(0)).unwrap());
            assert!(
                constructor
                    .colors
                    .iter()
                    .all(|color| *color == Color::White)
            );
            assert!(constructor.ordered.iter().all(Vec::is_empty));
            assert_eq!(constructor.cached_failures > 0, memoize);
            checkpoints
        };
        let cached = measure(true);
        let uncached = measure(false);
        eprintln!("repeated failed states: {cached} cached checkpoints, {uncached} uncached");
        assert!(
            cached * 4 < uncached,
            "cached {cached}, uncached {uncached}"
        );
    }

    #[test]
    fn an_acyclic_constraining_star_can_be_intrinsically_unschedulable() {
        let (left, right, dependency, entry) = (ModuleId(0), ModuleId(1), ModuleId(2), ModuleId(3));
        let nodes = BTreeSet::from([left, right, dependency, entry]);
        let pairs = BTreeSet::from([(left, dependency), (right, dependency)]);
        let mut successors = BTreeMap::from([
            (left, BTreeSet::from([dependency])),
            (right, BTreeSet::from([dependency])),
            (dependency, BTreeSet::from([left, right])),
        ]);
        let policy = EsmImportOrder::build(&pairs, &successors, &nodes);
        let rejected =
            EsmImportSchedule::construct(entry, &pairs, &successors, &nodes, &policy).unwrap_err();
        assert_eq!(
            rejected.component,
            BTreeSet::from([left, right, dependency])
        );
        assert_eq!(rejected.forward_closure, rejected.component);

        // An additional LAZY import can supply the needed nested route; the C
        // graph is unchanged. A C-only scheduler would miss this distinction.
        successors.get_mut(&left).unwrap().insert(right);
        let policy = EsmImportOrder::build(&pairs, &successors, &nodes);
        let schedule =
            EsmImportSchedule::construct(entry, &pairs, &successors, &nodes, &policy).unwrap();
        assert!(schedule.post_order()[&dependency] < schedule.post_order()[&right]);
        assert!(schedule.post_order()[&right] < schedule.post_order()[&left]);
    }

    #[test]
    fn cancellation_is_not_reported_as_an_intrinsic_obstruction() {
        let (left, right, dependency, entry) = (ModuleId(0), ModuleId(1), ModuleId(2), ModuleId(3));
        let nodes = BTreeSet::from([left, right, dependency, entry]);
        let pairs = BTreeSet::from([(left, dependency), (right, dependency)]);
        let successors = BTreeMap::from([
            (left, BTreeSet::from([dependency, right])),
            (right, BTreeSet::from([dependency])),
            (dependency, BTreeSet::from([left, right])),
        ]);
        let policy = EsmImportOrder::build(&pairs, &successors, &nodes);

        // This graph requires changing the preferred route, so successful
        // construction exercises reachability, completion trials, and the SCC
        // fallback checks below.
        let mut total_polls = 0;
        let result = EsmImportSchedule::construct_with_poll(
            entry,
            &pairs,
            &successors,
            &nodes,
            &policy,
            || {
                total_polls += 1;
                Ok::<(), usize>(())
            },
        )
        .unwrap();
        assert!(result.is_ok());
        assert!(total_polls > 8, "expected polling inside scheduler loops");

        for stop_at in [1, total_polls / 2, total_polls] {
            let mut polls = 0;
            let result = EsmImportSchedule::construct_with_poll(
                entry,
                &pairs,
                &successors,
                &nodes,
                &policy,
                || {
                    polls += 1;
                    if polls == stop_at {
                        Err(stop_at)
                    } else {
                        Ok(())
                    }
                },
            );
            assert_eq!(result, Err(stop_at));
            assert_eq!(polls, stop_at);
        }
    }

    #[test]
    fn constrained_components_compose_in_dependency_order() {
        let entry = ModuleId(6);
        let nodes = (0..=entry.0).map(ModuleId).collect();
        let mut pairs = BTreeSet::new();
        let mut successors = BTreeMap::new();
        for offset in [0, 3] {
            let (left, right, dependency) =
                (ModuleId(offset), ModuleId(offset + 1), ModuleId(offset + 2));
            pairs.extend([(left, dependency), (right, dependency)]);
            successors.insert(left, BTreeSet::from([right, dependency]));
            successors.insert(right, BTreeSet::from([dependency]));
            successors.insert(dependency, BTreeSet::from([left, right]));
        }
        pairs.insert((ModuleId(4), ModuleId(2)));
        successors
            .get_mut(&ModuleId(4))
            .unwrap()
            .insert(ModuleId(2));
        let preferred = EsmImportOrder::build(&pairs, &successors, &nodes);
        let schedule =
            EsmImportSchedule::construct(entry, &pairs, &successors, &nodes, &preferred).unwrap();
        assert!(schedule.tdz_pairs(&pairs).next().is_none());
        schedule.check_targets(entry, &successors, &nodes).unwrap();
        for dependency in 0..3 {
            for consumer in 3..6 {
                assert!(
                    schedule.post_order()[&ModuleId(dependency)]
                        < schedule.post_order()[&ModuleId(consumer)]
                );
            }
        }
    }

    #[test]
    fn independent_components_add_only_linear_scheduler_work() {
        for independent_components in [0, 32, 512] {
            let (left, right, dependency) = (ModuleId(0), ModuleId(1), ModuleId(2));
            let entry = ModuleId(3 + 2 * independent_components);
            let nodes = (0..=entry.0).map(ModuleId).collect();
            let pairs = BTreeSet::from([(left, dependency), (right, dependency)]);
            let mut successors = BTreeMap::from([
                (left, BTreeSet::from([right, dependency])),
                (right, BTreeSet::from([dependency])),
                (dependency, BTreeSet::from([left, right])),
            ]);
            for index in 0..independent_components {
                let first = ModuleId(3 + 2 * index);
                let second = ModuleId(first.0 + 1);
                successors.insert(first, BTreeSet::from([second, entry]));
                successors.insert(second, BTreeSet::from([first]));
                // These downstream lazy components must finish before the mixed
                // component. Entry back-imports must stay gray, not join its SCC.
                successors.get_mut(&left).unwrap().insert(first);
            }
            let preferred = EsmImportOrder::build(&pairs, &successors, &nodes);
            let policy = EsmImportSchedule::from_policy(entry, &successors, &nodes, &preferred);
            assert!(policy.tdz_pairs(&pairs).next().is_some());
            let mut checkpoints = 0;
            let schedule = EsmImportSchedule::construct_with_poll(
                entry,
                &pairs,
                &successors,
                &nodes,
                &preferred,
                || {
                    checkpoints += 1;
                    Ok::<_, ()>(())
                },
            )
            .unwrap()
            .unwrap();
            assert!(schedule.tdz_pairs(&pairs).next().is_none());
            schedule.check_targets(entry, &successors, &nodes).unwrap();
            for index in 0..independent_components {
                let first = ModuleId(3 + 2 * index);
                assert!(schedule.post_order()[&first] < schedule.post_order()[&left]);
            }
            assert!(
                checkpoints < 1_000 + 80 * independent_components,
                "independent components enlarged trial work: {independent_components} components, {checkpoints} checkpoints",
            );
        }
    }

    #[test]
    fn obstruction_keeps_forward_dependencies_without_searching_them() {
        let (left, right, dependency) = (ModuleId(0), ModuleId(1), ModuleId(2));
        let downstream_count = 512;
        let entry = ModuleId(3 + downstream_count);
        let nodes = (0..=entry.0).map(ModuleId).collect();
        let pairs = BTreeSet::from([(left, dependency), (right, dependency)]);
        let mut successors = BTreeMap::from([
            (left, BTreeSet::from([dependency, ModuleId(3)])),
            (right, BTreeSet::from([dependency])),
            (dependency, BTreeSet::from([left, right])),
        ]);
        for node in 3..entry.0 - 1 {
            successors.insert(ModuleId(node), BTreeSet::from([ModuleId(node + 1)]));
        }
        let preferred = EsmImportOrder::build(&pairs, &successors, &nodes);
        let mut checkpoints = 0;
        let obstruction = EsmImportSchedule::construct_with_poll(
            entry,
            &pairs,
            &successors,
            &nodes,
            &preferred,
            || {
                checkpoints += 1;
                Ok::<_, ()>(())
            },
        )
        .unwrap()
        .unwrap_err();
        assert_eq!(
            obstruction.component,
            BTreeSet::from([left, right, dependency])
        );
        assert_eq!(
            obstruction.forward_closure,
            (0..entry.0).map(ModuleId).collect()
        );
        assert!(
            checkpoints < 1_000 + 40 * downstream_count,
            "{checkpoints} checkpoints"
        );
    }
}
