//! Concrete intra-chunk imports, retained from validation through emission.
//!
//! A linker rank is a policy input, not an ESM schedule. This object records
//! every module's actual ordered imports, including entry's implicit fanout.

use std::collections::{BTreeMap, BTreeSet};

use analysis::ids::ModuleId;
use thiserror::Error;

use crate::esm_import_order::EsmImportOrder;

mod construct;
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
    #[error("import schedule for {module:?} has missing, extra or duplicate targets")]
    ImportTargets { module: ModuleId },
}

impl EsmImportSchedule {
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
                return Err(ImportScheduleError::ImportTargets { module });
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
    pub fn sort_imports<T>(&self, module: ModuleId, imports: &mut [(ModuleId, T)]) {
        let positions: BTreeMap<_, _> = self
            .imports(module)
            .iter()
            .enumerate()
            .map(|(position, &target)| (target, position))
            .collect();
        imports.sort_by_key(|(target, _)| positions[target]);
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
            assert_eq!(
                EsmImportSchedule::from_imports(entry, &successors, &nodes, bad),
                Err(ImportScheduleError::ImportTargets { module })
            );
        }
    }
}
