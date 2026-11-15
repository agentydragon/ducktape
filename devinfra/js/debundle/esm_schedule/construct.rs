//! Derive an actual ESM traversal, committing successful subtrees rather than
//! enumerating import-list permutations. See docs/module_placement.md for the
//! successor-closure and completion-preservation proofs.

use std::collections::{BTreeMap, BTreeSet};

use analysis::ids::ModuleId;
use petgraph::{
    algo::{tarjan_scc, toposort},
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

#[derive(Clone, Default)]
struct Traversal {
    active: BTreeSet<ModuleId>,
    finished: BTreeSet<ModuleId>,
    imports: BTreeMap<ModuleId, Vec<ModuleId>>,
}

struct Constructor<'a, E> {
    imports: &'a BTreeMap<ModuleId, Vec<ModuleId>>,
    constraints: &'a BTreeMap<ModuleId, Vec<ModuleId>>,
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
        // Keep recursive trials local: downstream components cannot affect the
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
        let components = tarjan_scc(&graph);
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
                let mut constructor = Constructor {
                    imports: &internal_imports[index],
                    constraints,
                    poll: &mut poll,
                };
                let mut chosen = None;
                for root in candidates {
                    constructor.poll()?;
                    // Every SCC member must finish before its first root. A
                    // constraining incoming edge therefore forbids this root.
                    if constrained_dependencies.contains(&root) {
                        continue;
                    }
                    let mut state = Traversal::default();
                    if constructor.complete(root, &mut state)? {
                        chosen = Some((root, state.imports));
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

impl<E> Constructor<'_, E> {
    fn poll(&mut self) -> Result<(), E> {
        (self.poll)()
    }

    fn white(&self, node: ModuleId, state: &Traversal) -> bool {
        !state.active.contains(&node) && !state.finished.contains(&node)
    }

    fn complete(&mut self, root: ModuleId, state: &mut Traversal) -> Result<bool, E> {
        self.poll()?;
        if self.conflicts_with_ancestor(root, root, state)? {
            return Ok(false);
        }
        let mut reachable = BTreeSet::from([root]);
        let mut work = vec![root];
        while let Some(node) = work.pop() {
            self.poll()?;
            for &target in self.imports[&node].as_slice() {
                self.poll()?;
                if self.white(target, state) && reachable.insert(target) {
                    if self.conflicts_with_ancestor(target, root, state)? {
                        return Ok(false);
                    }
                    work.push(target);
                }
            }
        }
        state.active.insert(root);
        state.imports.insert(root, Vec::new());
        loop {
            self.poll()?;
            let mut candidates = Vec::new();
            for &target in self.imports[&root].as_slice() {
                self.poll()?;
                if self.white(target, state) {
                    candidates.push(target);
                }
            }
            if candidates.is_empty() {
                break;
            }
            let mut success = false;
            for target in candidates {
                self.poll()?;
                let mut trial = state.clone();
                trial
                    .imports
                    .get_mut(&root)
                    .expect("active root")
                    .push(target);
                if self.complete(target, &mut trial)? {
                    // A completed subtree is successor-closed among the old
                    // white vertices. Finishing it earlier preserves every
                    // possible outside completion; no sibling backtracking.
                    self.poll()?;
                    *state = trial;
                    success = true;
                    break;
                }
            }
            if !success {
                return Ok(false);
            }
        }
        let ordered = state.imports.get_mut(&root).expect("active root");
        for &target in self.imports[&root].as_slice() {
            self.poll()?;
            if !ordered.contains(&target) {
                ordered.push(target);
            }
        }
        state.active.remove(&root);
        state.finished.insert(root);
        Ok(true)
    }

    /// Every initially-white reachable consumer finishes before root and its
    /// active ancestors. Check exactly those consumers as they are discovered,
    /// stopping before the rest of an already impossible closure is visited.
    fn conflicts_with_ancestor(
        &mut self,
        consumer: ModuleId,
        root: ModuleId,
        state: &Traversal,
    ) -> Result<bool, E> {
        if let Some(dependencies) = self.constraints.get(&consumer) {
            for &dependency in dependencies {
                self.poll()?;
                if dependency == root || state.active.contains(&dependency) {
                    return Ok(true);
                }
            }
        }
        Ok(false)
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
            // Preserve both terms of the rejection predicate: the current
            // root and any already active ancestor.
            for dependency in [root, active] {
                let constraints = BTreeMap::from([(consumer, vec![dependency])]);
                let mut state = Traversal {
                    active: BTreeSet::from([active]),
                    ..Traversal::default()
                };
                let mut checkpoints = 0;
                let mut poll = || {
                    checkpoints += 1;
                    Ok::<_, ()>(())
                };
                let mut constructor = Constructor {
                    imports: &imports,
                    constraints: &constraints,
                    poll: &mut poll,
                };
                assert!(!constructor.complete(root, &mut state).unwrap());
                assert_eq!(state.active, BTreeSet::from([active]));
                assert!(state.finished.is_empty());
                assert!(state.imports.is_empty());
                assert!(checkpoints < 10, "{tail_size}: {checkpoints} checkpoints");
            }
        }
    }

    #[test]
    fn a_trial_does_not_scan_constraints_of_finished_consumers() {
        for finished_count in [2, 32, 4_096] {
            let (root, active) = (ModuleId(0), ModuleId(1));
            let mut imports =
                BTreeMap::from([(root, vec![active]), (active, vec![root, ModuleId(2)])]);
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
            // The chain was a successful sibling subtree inside this same
            // original SCC. Only root is still white in the parent frame.
            let mut state = Traversal {
                active: BTreeSet::from([active]),
                finished: (2..finished_count + 2).map(ModuleId).collect(),
                ..Traversal::default()
            };
            let mut checkpoints = 0;
            let mut poll = || {
                checkpoints += 1;
                Ok::<_, ()>(())
            };
            let mut constructor = Constructor {
                imports: &imports,
                constraints: &constraints,
                poll: &mut poll,
            };
            assert!(constructor.complete(root, &mut state).unwrap());
            assert!(state.finished.contains(&root));
            assert_eq!(state.imports[&root], vec![active]);
            assert!(
                checkpoints < 10,
                "{finished_count}: {checkpoints} checkpoints"
            );
        }
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
        // construction exercises reachability, recursive trials, and the SCC
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
                "independent components enlarged recursive work: {independent_components} components, {checkpoints} checkpoints",
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
