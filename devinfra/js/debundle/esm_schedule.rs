//! Concrete intra-chunk imports, retained from validation through emission.
//!
//! A linker rank is a policy input, not an ESM schedule. This object records
//! every module's actual ordered imports, including entry's implicit fanout.

use std::collections::{BTreeMap, BTreeSet};

use analysis::ids::ModuleId;
use thiserror::Error;

use crate::esm_import_order::EsmImportOrder;

// General schedule existence is used only by bounded test oracles. Production
// callers supply a priority and verify it with `from_priority`.
#[cfg(test)]
mod construct;
#[cfg(test)]
pub use construct::ScheduleObstruction;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct EsmImportSchedule {
    entry: ModuleId,
    imports: BTreeMap<ModuleId, Vec<ModuleId>>,
    post_order: BTreeMap<ModuleId, usize>,
}

#[derive(Debug, Clone, Eq, PartialEq, Error)]
pub enum ImportScheduleError {
    #[error("import schedule does not contain exactly the emitted modules")]
    ModuleUniverse,
    #[error(
        "import priority must contain every nonentry module exactly once; expected {expected:?}, received {actual:?}"
    )]
    PriorityUniverse {
        expected: Vec<ModuleId>,
        actual: Vec<ModuleId>,
    },
    #[error(
        "import priority must contain every emitted nonentry module exactly once; expected {expected:?}, received {actual:?}"
    )]
    PriorityPaths {
        expected: Vec<spec::ModulePath>,
        actual: Vec<spec::ModulePath>,
    },
    #[error("import schedule for {module:?} expects {expected:?}, received {actual:?}")]
    ImportTargets {
        module: ModuleId,
        expected: Vec<ModuleId>,
        actual: Vec<ModuleId>,
    },
}

impl EsmImportSchedule {
    /// Construct exactly the supplied global priority. No alternative ordering
    /// is tried. Entry is implicit and never appears in `priority`.
    pub fn from_priority(
        entry: ModuleId,
        successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        nodes: &BTreeSet<ModuleId>,
        priority: &[ModuleId],
    ) -> Result<Self, ImportScheduleError> {
        if !nodes.contains(&entry)
            || successors
                .iter()
                .any(|(from, targets)| !nodes.contains(from) || !targets.is_subset(nodes))
        {
            return Err(ImportScheduleError::ModuleUniverse);
        }
        let expected: BTreeSet<_> = nodes.iter().copied().filter(|&id| id != entry).collect();
        if priority.len() != expected.len()
            || priority.iter().copied().collect::<BTreeSet<_>>() != expected
        {
            return Err(ImportScheduleError::PriorityUniverse {
                expected: expected.into_iter().collect(),
                actual: priority.to_vec(),
            });
        }
        let positions: BTreeMap<_, _> = std::iter::once((entry, 0))
            .chain(priority.iter().enumerate().map(|(i, &id)| (id, i + 1)))
            .collect();
        let imports = nodes
            .iter()
            .map(|&module| {
                let mut targets: Vec<_> = required_targets(entry, module, successors, nodes)
                    .into_iter()
                    .collect();
                targets.sort_unstable_by_key(|target| positions[target]);
                (module, targets)
            })
            .collect();
        Self::from_imports(entry, successors, nodes, imports)
    }

    /// Capture the traversal's DFS discovery order, excluding implicit entry.
    /// Sorting every import list by this order reproduces the same DFS: when a
    /// module's imports are visited, its next white target is the next child
    /// discovered in the original walk.
    pub fn discovery_order(&self) -> Vec<ModuleId> {
        let mut seen = BTreeSet::from([self.entry]);
        let mut frames = vec![(self.entry, 0)];
        let mut order = Vec::with_capacity(self.imports.len().saturating_sub(1));
        while let Some((module, next)) = frames.last_mut() {
            if let Some(&target) = self.imports(*module).get(*next) {
                *next += 1;
                if seen.insert(target) {
                    order.push(target);
                    frames.push((target, 0));
                }
            } else {
                frames.pop();
            }
        }
        order
    }

    /// Reify the existing policy without changing its target order. `nodes`
    /// includes all emitted modules, empty owners, and the true entry.
    pub fn from_policy(
        entry: ModuleId,
        successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        nodes: &BTreeSet<ModuleId>,
        policy: &EsmImportOrder,
    ) -> Self {
        let imports = nodes
            .iter()
            .map(|&module| {
                let mut targets: Vec<_> = required_targets(entry, module, successors, nodes)
                    .into_iter()
                    .map(|target| (target, ()))
                    .collect();
                if module == entry {
                    policy.sort_entry_imports(&mut targets);
                } else {
                    policy.sort_module_imports(&mut targets);
                }
                (
                    module,
                    targets.into_iter().map(|(target, ())| target).collect(),
                )
            })
            .collect();
        Self::from_imports(entry, successors, nodes, imports)
            .expect("policy schedules exactly the canonical import targets")
    }

    /// Check an externally supplied concrete schedule. Never trust a supplied
    /// postorder or omit an import to make an otherwise invalid traversal pass.
    pub fn from_imports(
        entry: ModuleId,
        successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        nodes: &BTreeSet<ModuleId>,
        imports: BTreeMap<ModuleId, Vec<ModuleId>>,
    ) -> Result<Self, ImportScheduleError> {
        let mut schedule = Self {
            entry,
            imports,
            post_order: BTreeMap::new(),
        };
        schedule.check_targets(entry, successors, nodes)?;
        schedule.post_order = schedule.traverse();
        Ok(schedule)
    }

    pub fn check_targets(
        &self,
        entry: ModuleId,
        successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        nodes: &BTreeSet<ModuleId>,
    ) -> Result<(), ImportScheduleError> {
        if entry != self.entry
            || !nodes.contains(&entry)
            || self.imports.keys().copied().collect::<BTreeSet<_>>() != *nodes
            || successors
                .iter()
                .any(|(from, targets)| !nodes.contains(from) || !targets.is_subset(nodes))
        {
            return Err(ImportScheduleError::ModuleUniverse);
        }
        for (&module, targets) in &self.imports {
            let actual: BTreeSet<_> = targets.iter().copied().collect();
            if actual.len() != targets.len()
                || actual != required_targets(entry, module, successors, nodes)
            {
                return Err(ImportScheduleError::ImportTargets {
                    module,
                    expected: required_targets(entry, module, successors, nodes)
                        .into_iter()
                        .collect(),
                    actual: targets.clone(),
                });
            }
        }
        Ok(())
    }

    pub fn modules(&self) -> BTreeSet<ModuleId> {
        self.imports.keys().copied().collect()
    }

    pub fn imports(&self, module: ModuleId) -> &[ModuleId] {
        &self.imports[&module]
    }

    pub fn post_order(&self) -> &BTreeMap<ModuleId, usize> {
        &self.post_order
    }

    /// All violated initialization constraints, including dependencies into
    /// entry and violations outside a changed owner's SCC.
    pub fn tdz_pairs<'a>(
        &'a self,
        pairs: &'a BTreeSet<(ModuleId, ModuleId)>,
    ) -> impl Iterator<Item = (ModuleId, ModuleId)> + 'a {
        pairs
            .iter()
            .copied()
            .filter(|(from, to)| self.post_order[to] >= self.post_order[from])
    }

    /// Sort the emitter's merged binding/phantom imports by this exact list.
    /// Several declarations may import one target; their stable relative order
    /// does not change ESM traversal.
    pub fn sort_imports<T>(
        &self,
        module: ModuleId,
        imports: &mut [(ModuleId, T)],
    ) -> Result<(), ImportScheduleError> {
        let actual: BTreeSet<_> = imports.iter().map(|(target, _)| *target).collect();
        if actual != self.imports(module).iter().copied().collect() {
            return Err(ImportScheduleError::ImportTargets {
                module,
                expected: self.imports(module).to_vec(),
                actual: actual.into_iter().collect(),
            });
        }
        let positions: BTreeMap<_, _> = self
            .imports(module)
            .iter()
            .enumerate()
            .map(|(position, &target)| (target, position))
            .collect();
        imports.sort_by_key(|(target, _)| positions[target]);
        Ok(())
    }

    fn traverse(&self) -> BTreeMap<ModuleId, usize> {
        // Explicit frames avoid stack overflow on long module chains. Mark a
        // target on entry; imports to active or completed modules are no-ops.
        let mut seen = BTreeSet::from([self.entry]);
        let mut frames = vec![(self.entry, 0)];
        let mut post_order = BTreeMap::new();
        while let Some((module, next)) = frames.last_mut() {
            if let Some(&target) = self.imports(*module).get(*next) {
                *next += 1;
                if seen.insert(target) {
                    frames.push((target, 0));
                }
            } else {
                let (module, _) = frames.pop().expect("active frame");
                post_order.insert(module, post_order.len());
            }
        }
        post_order
    }
}

fn required_targets(
    entry: ModuleId,
    module: ModuleId,
    successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    nodes: &BTreeSet<ModuleId>,
) -> BTreeSet<ModuleId> {
    if module == entry {
        nodes
            .iter()
            .copied()
            .filter(|&target| target != entry)
            .collect()
    } else {
        successors.get(&module).cloned().unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn permutations(values: &[ModuleId]) -> Vec<Vec<ModuleId>> {
        if values.is_empty() {
            return vec![Vec::new()];
        }
        values
            .iter()
            .enumerate()
            .flat_map(|(index, &first)| {
                let mut rest = values.to_vec();
                rest.remove(index);
                permutations(&rest)
                    .into_iter()
                    .map(move |tail| std::iter::once(first).chain(tail).collect())
            })
            .collect()
    }

    // This deliberately uses recursive ECMA traversal, independently of the
    // production explicit-frame walker, policy, graph pruning and cached ranks.
    fn oracle(
        module: ModuleId,
        imports: &BTreeMap<ModuleId, Vec<ModuleId>>,
        seen: &mut BTreeSet<ModuleId>,
        finished: &mut Vec<ModuleId>,
    ) {
        if !seen.insert(module) {
            return;
        }
        for &target in &imports[&module] {
            oracle(target, imports, seen, finished);
        }
        finished.push(module);
    }

    fn oracle_trace(
        module: ModuleId,
        imports: &BTreeMap<ModuleId, Vec<ModuleId>>,
        seen: &mut BTreeSet<ModuleId>,
        events: &mut Vec<(ModuleId, bool)>,
    ) {
        if !seen.insert(module) {
            return;
        }
        events.push((module, true));
        for &target in &imports[&module] {
            oracle_trace(target, imports, seen, events);
        }
        events.push((module, false));
    }

    #[test]
    fn supplied_priority_requires_exactly_the_nonentry_universe() {
        let entry = ModuleId(2);
        let nodes = BTreeSet::from([ModuleId(0), ModuleId(1), entry]);
        for priority in [
            vec![],
            vec![ModuleId(0)],
            vec![ModuleId(0), ModuleId(0)],
            vec![ModuleId(0), entry],
            vec![ModuleId(0), ModuleId(99)],
        ] {
            assert!(matches!(
                EsmImportSchedule::from_priority(entry, &BTreeMap::new(), &nodes, &priority),
                Err(ImportScheduleError::PriorityUniverse { .. })
            ));
        }
        let all_entry = EsmImportSchedule::from_priority(
            entry,
            &BTreeMap::new(),
            &BTreeSet::from([entry]),
            &[],
        )
        .unwrap();
        assert_eq!(all_entry.post_order(), &BTreeMap::from([(entry, 0)]));
        assert!(all_entry.discovery_order().is_empty());
    }

    #[test]
    fn supplied_priority_is_used_even_when_another_priority_would_satisfy_constraints() {
        let (a, b, entry) = (ModuleId(0), ModuleId(1), ModuleId(2));
        let nodes = BTreeSet::from([a, b, entry]);
        let successors = BTreeMap::from([(a, BTreeSet::from([b])), (b, BTreeSet::from([a]))]);
        let pairs = BTreeSet::from([(a, b)]);
        let failed = EsmImportSchedule::from_priority(entry, &successors, &nodes, &[b, a]).unwrap();
        assert_eq!(failed.tdz_pairs(&pairs).collect::<Vec<_>>(), vec![(a, b)]);
        let accepted =
            EsmImportSchedule::from_priority(entry, &successors, &nodes, &[a, b]).unwrap();
        assert!(accepted.tdz_pairs(&pairs).next().is_none());
    }

    #[test]
    fn every_three_module_import_graph_and_import_permutation_matches_raw_dfs() {
        let entry = ModuleId(2);
        let nodes = BTreeSet::from([ModuleId(0), ModuleId(1), entry]);
        let edges: Vec<_> = nodes
            .iter()
            .flat_map(|&from| {
                nodes
                    .iter()
                    .copied()
                    .filter(move |&to| to != from)
                    .map(move |to| (from, to))
            })
            .collect();
        for mask in 0..(1 << edges.len()) {
            let mut successors = BTreeMap::<_, BTreeSet<_>>::new();
            for (bit, &(from, to)) in edges.iter().enumerate() {
                if mask & (1 << bit) != 0 {
                    successors.entry(from).or_default().insert(to);
                }
            }
            let targets: Vec<_> = nodes
                .iter()
                .map(|&module| {
                    let targets = if module == entry {
                        vec![ModuleId(0), ModuleId(1)]
                    } else {
                        successors
                            .get(&module)
                            .into_iter()
                            .flatten()
                            .copied()
                            .collect()
                    };
                    permutations(&targets)
                })
                .collect();
            let mut oracle_orders = Vec::new();
            for first in &targets[0] {
                for second in &targets[1] {
                    for root in &targets[2] {
                        let imports = BTreeMap::from([
                            (ModuleId(0), first.clone()),
                            (ModuleId(1), second.clone()),
                            (entry, root.clone()),
                        ]);
                        let mut finished = Vec::new();
                        oracle(entry, &imports, &mut BTreeSet::new(), &mut finished);
                        oracle_orders.push(finished.clone());
                        let schedule =
                            EsmImportSchedule::from_imports(entry, &successors, &nodes, imports)
                                .unwrap();
                        let mut original_trace = Vec::new();
                        oracle_trace(
                            entry,
                            &schedule.imports,
                            &mut BTreeSet::new(),
                            &mut original_trace,
                        );
                        let replay = EsmImportSchedule::from_priority(
                            entry,
                            &successors,
                            &nodes,
                            &schedule.discovery_order(),
                        )
                        .unwrap();
                        let mut replay_trace = Vec::new();
                        oracle_trace(
                            entry,
                            &replay.imports,
                            &mut BTreeSet::new(),
                            &mut replay_trace,
                        );
                        assert_eq!(replay_trace, original_trace, "graph mask {mask}");
                        let expected: BTreeMap<_, _> = finished
                            .iter()
                            .enumerate()
                            .map(|(n, &module)| (module, n))
                            .collect();
                        assert_eq!(schedule.post_order(), &expected, "graph mask {mask}");
                        let all_pairs = edges.iter().copied().collect();
                        let rejected: BTreeSet<_> = schedule.tdz_pairs(&all_pairs).collect();
                        for &(from, to) in &edges {
                            assert_eq!(
                                rejected.contains(&(from, to)),
                                finished.iter().position(|&m| m == to)
                                    >= finished.iter().position(|&m| m == from)
                            );
                        }
                    }
                }
            }
            let present: Vec<_> = edges
                .iter()
                .enumerate()
                .filter(|(bit, _)| mask & (1 << bit) != 0)
                .map(|(_, &edge)| edge)
                .collect();
            for constraints in 0..(1 << present.len()) {
                let pairs: BTreeSet<_> = present
                    .iter()
                    .enumerate()
                    .filter(|(bit, _)| constraints & (1 << bit) != 0)
                    .map(|(_, &edge)| edge)
                    .collect();
                let exists = oracle_orders.iter().any(|finished| {
                    pairs.iter().all(|(from, to)| {
                        finished.iter().position(|m| m == to)
                            < finished.iter().position(|m| m == from)
                    })
                });
                let policy = EsmImportOrder::build(&pairs, &successors, &nodes);
                let derived =
                    EsmImportSchedule::construct(entry, &pairs, &successors, &nodes, &policy);
                assert_eq!(
                    derived.is_ok(),
                    exists,
                    "I mask {mask}, C mask {constraints}: {derived:?}"
                );
                if let Ok(schedule) = derived {
                    assert!(schedule.tdz_pairs(&pairs).next().is_none());
                }
            }
        }
    }

    #[test]
    fn a_schedule_cannot_omit_phantom_imports_or_entry_fanout() {
        let entry = ModuleId(2);
        let nodes = BTreeSet::from([ModuleId(0), ModuleId(1), entry]);
        let successors = BTreeMap::from([(ModuleId(0), BTreeSet::from([ModuleId(1)]))]);
        let imports = BTreeMap::from([
            (ModuleId(0), vec![ModuleId(1)]),
            (ModuleId(1), vec![]),
            (entry, vec![ModuleId(0), ModuleId(1)]),
        ]);
        for (module, targets) in [
            (ModuleId(0), vec![]),
            (ModuleId(0), vec![ModuleId(1), ModuleId(1)]),
            (ModuleId(1), vec![ModuleId(0)]),
            (entry, vec![ModuleId(0)]),
        ] {
            let mut bad = imports.clone();
            bad.insert(module, targets);
            assert!(matches!(
                EsmImportSchedule::from_imports(entry, &successors, &nodes, bad),
                Err(ImportScheduleError::ImportTargets { module: actual, .. }) if actual == module
            ));
        }
    }

    pub(super) fn raw_schedule_exists(
        entry: ModuleId,
        successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        nodes: &BTreeSet<ModuleId>,
        pairs: &BTreeSet<(ModuleId, ModuleId)>,
    ) -> bool {
        let choices: Vec<_> = nodes
            .iter()
            .map(|&module| {
                let targets = if module == entry {
                    nodes
                        .iter()
                        .copied()
                        .filter(|&node| node != entry)
                        .collect::<Vec<_>>()
                } else {
                    successors
                        .get(&module)
                        .into_iter()
                        .flatten()
                        .copied()
                        .collect()
                };
                (module, permutations(&targets))
            })
            .collect();
        let mut digits = vec![0; choices.len()];
        loop {
            let imports = choices
                .iter()
                .zip(&digits)
                .map(|((module, permutations), &digit)| (*module, permutations[digit].clone()))
                .collect();
            let mut finished = Vec::new();
            oracle(entry, &imports, &mut BTreeSet::new(), &mut finished);
            if pairs.iter().all(|(from, to)| {
                finished.iter().position(|node| node == to)
                    < finished.iter().position(|node| node == from)
            }) {
                return true;
            }
            let mut digit = 0;
            while digit < digits.len() {
                digits[digit] += 1;
                if digits[digit] < choices[digit].1.len() {
                    break;
                }
                digits[digit] = 0;
                digit += 1;
            }
            if digit == digits.len() {
                return false;
            }
        }
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(128))]
        #[test]
        fn four_module_schedule_existence_matches_raw_import_permutations(
            import_mask in 0u16..4096, constraint_mask in 0u16..4096,
        ) {
            let entry = ModuleId(3);
            let nodes = BTreeSet::from([ModuleId(0), ModuleId(1), ModuleId(2), entry]);
            let all_edges: Vec<_> = nodes.iter().flat_map(|&from| {
                nodes.iter().copied().filter(move |&to| to != from).map(move |to| (from, to))
            }).collect();
            let mut successors = BTreeMap::<_, BTreeSet<_>>::new();
            let mut pairs = BTreeSet::new();
            for (bit, &(from, to)) in all_edges.iter().enumerate() {
                if import_mask & (1 << bit) != 0 {
                    successors.entry(from).or_default().insert(to);
                    if constraint_mask & (1 << bit) != 0 { pairs.insert((from, to)); }
                }
            }
            let exists = raw_schedule_exists(entry, &successors, &nodes, &pairs);
            let policy = EsmImportOrder::build(&pairs, &successors, &nodes);
            let result = EsmImportSchedule::construct(entry, &pairs, &successors, &nodes, &policy);
            prop_assert_eq!(result.is_ok(), exists, "{:?}", result);
            if let Ok(schedule) = result {
                prop_assert!(schedule.tdz_pairs(&pairs).next().is_none());
            }
        }
    }
}
