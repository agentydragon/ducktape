//! Derive an actual ESM traversal, committing successful subtrees rather than
//! enumerating import-list permutations. See docs/module_placement.md for the
//! successor-closure and completion-preservation proofs.

use std::collections::{BTreeMap, BTreeSet};

use analysis::ids::ModuleId;
use petgraph::{algo::tarjan_scc, graphmap::DiGraphMap};

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

struct Constructor<'a> {
    preferred: &'a EsmImportSchedule,
    pairs: &'a BTreeSet<(ModuleId, ModuleId)>,
    trials: usize,
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
        let policy = Self::from_policy(entry, successors, nodes, preferred);
        if policy.tdz_pairs(pairs).next().is_none() {
            return Ok(policy);
        }
        let mut constructor = Constructor {
            preferred: &policy,
            pairs,
            trials: 0,
        };
        let mut state = Traversal::default();
        if constructor.complete(entry, &mut state) {
            let result = Self::from_imports(entry, successors, nodes, state.imports)
                .expect("constructed traversal covers the complete import graph");
            assert!(
                result.tdz_pairs(pairs).next().is_none(),
                "constructor returned a TDZ"
            );
            return Ok(result);
        }

        // Entry's universal fanout lets it pre-evaluate every downstream SCC.
        // A failed whole traversal therefore contains an SCC that cannot be
        // entered at ANY root, with entry gray and everything outside black.
        let mut graph = DiGraphMap::<ModuleId, ()>::new();
        for &node in nodes {
            if node != entry {
                graph.add_node(node);
            }
        }
        for (&from, targets) in successors {
            for &to in targets {
                if from != entry && to != entry {
                    graph.add_edge(from, to, ());
                }
            }
        }
        for component in tarjan_scc(&graph) {
            let component: BTreeSet<_> = component.into_iter().collect();
            let completable = component.iter().any(|&root| {
                let mut state = Traversal {
                    active: BTreeSet::from([entry]),
                    finished: nodes
                        .difference(&component)
                        .copied()
                        .filter(|&node| node != entry)
                        .collect(),
                    imports: BTreeMap::new(),
                };
                constructor.complete(root, &mut state)
            });
            if !completable {
                let mut forward_closure = component.clone();
                let mut work: Vec<_> = component.iter().copied().collect();
                while let Some(node) = work.pop() {
                    for &target in policy.imports(node) {
                        if target != entry && forward_closure.insert(target) {
                            work.push(target);
                        }
                    }
                }
                return Err(ScheduleObstruction {
                    component,
                    forward_closure,
                });
            }
        }
        // A C edge into entry is an immediate global obstruction even when
        // its endpoints do not form a source-import SCC.
        let component: BTreeSet<_> = pairs
            .iter()
            .filter_map(|&(from, to)| (to == entry).then_some(from))
            .collect();
        assert!(
            !component.is_empty(),
            "whole traversal failed but every SCC is schedulable"
        );
        let mut forward_closure = component.clone();
        let mut work: Vec<_> = component.iter().copied().collect();
        while let Some(node) = work.pop() {
            for &target in policy.imports(node) {
                if target != entry && forward_closure.insert(target) {
                    work.push(target);
                }
            }
        }
        Err(ScheduleObstruction {
            component,
            forward_closure,
        })
    }
}

impl Constructor<'_> {
    fn white(&self, node: ModuleId, state: &Traversal) -> bool {
        !state.active.contains(&node) && !state.finished.contains(&node)
    }

    fn complete(&mut self, root: ModuleId, state: &mut Traversal) -> bool {
        self.trials += 1;
        let mut reachable = BTreeSet::from([root]);
        let mut work = vec![root];
        while let Some(node) = work.pop() {
            for &target in self.preferred.imports(node) {
                if self.white(target, state) && reachable.insert(target) {
                    work.push(target);
                }
            }
        }
        // Every initially-white reachable consumer must finish in this subtree,
        // before root and its active ancestors. Such a read cannot be repaired
        // by a different import order within this trial.
        if self.pairs.iter().any(|(consumer, dependency)| {
            reachable.contains(consumer)
                && (*dependency == root || state.active.contains(dependency))
        }) {
            return false;
        }

        state.active.insert(root);
        state.imports.insert(root, Vec::new());
        loop {
            let candidates: Vec<_> = self
                .preferred
                .imports(root)
                .iter()
                .copied()
                .filter(|&target| self.white(target, state))
                .collect();
            if candidates.is_empty() {
                break;
            }
            let mut success = false;
            for target in candidates {
                let mut trial = state.clone();
                trial
                    .imports
                    .get_mut(&root)
                    .expect("active root")
                    .push(target);
                if self.complete(target, &mut trial) {
                    // A completed subtree is successor-closed among the old
                    // white vertices. Finishing it earlier preserves every
                    // possible outside completion; no sibling backtracking.
                    *state = trial;
                    success = true;
                    break;
                }
            }
            if !success {
                return false;
            }
        }
        let ordered = state.imports.get_mut(&root).expect("active root");
        for &target in self.preferred.imports(root) {
            if !ordered.contains(&target) {
                ordered.push(target);
            }
        }
        state.active.remove(&root);
        state.finished.insert(root);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
