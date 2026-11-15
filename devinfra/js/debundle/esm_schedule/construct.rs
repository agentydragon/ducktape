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

struct Constructor<'a, E> {
    preferred: &'a EsmImportSchedule,
    pairs: &'a BTreeSet<(ModuleId, ModuleId)>,
    poll: &'a mut dyn FnMut() -> Result<(), E>,
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
        let mut constructor = Constructor {
            preferred: &policy,
            pairs,
            poll: &mut poll,
            trials: 0,
        };
        if constructor.is_tdz_free(&policy)? {
            return Ok(Ok(policy));
        }
        let mut state = Traversal::default();
        if constructor.complete(entry, &mut state)? {
            constructor.poll()?;
            let result = Self::from_imports(entry, successors, nodes, state.imports)
                .expect("constructed traversal covers the complete import graph");
            assert!(
                constructor.is_tdz_free(&result)?,
                "constructor returned a TDZ"
            );
            return Ok(Ok(result));
        }

        // Entry's universal fanout lets it pre-evaluate every downstream SCC.
        // A failed whole traversal therefore contains an SCC that cannot be
        // entered at ANY root, with entry gray and everything outside black.
        let mut graph = DiGraphMap::<ModuleId, ()>::new();
        for &node in nodes {
            constructor.poll()?;
            if node != entry {
                graph.add_node(node);
            }
        }
        for (&from, targets) in successors {
            constructor.poll()?;
            for &to in targets {
                constructor.poll()?;
                if from != entry && to != entry {
                    graph.add_edge(from, to, ());
                }
            }
        }
        constructor.poll()?;
        for component in tarjan_scc(&graph) {
            constructor.poll()?;
            let component: BTreeSet<_> = component.into_iter().collect();
            let mut completable = false;
            for &root in &component {
                constructor.poll()?;
                let mut state = Traversal {
                    active: BTreeSet::from([entry]),
                    finished: nodes
                        .difference(&component)
                        .copied()
                        .filter(|&node| node != entry)
                        .collect(),
                    imports: BTreeMap::new(),
                };
                if constructor.complete(root, &mut state)? {
                    completable = true;
                    break;
                }
            }
            if !completable {
                let mut forward_closure = component.clone();
                let mut work: Vec<_> = component.iter().copied().collect();
                while let Some(node) = work.pop() {
                    constructor.poll()?;
                    for &target in policy.imports(node) {
                        constructor.poll()?;
                        if target != entry && forward_closure.insert(target) {
                            work.push(target);
                        }
                    }
                }
                return Ok(Err(ScheduleObstruction {
                    component,
                    forward_closure,
                }));
            }
        }
        // A C edge into entry is an immediate global obstruction even when
        // its endpoints do not form a source-import SCC.
        let mut component = BTreeSet::new();
        for &(from, to) in pairs {
            constructor.poll()?;
            if to == entry {
                component.insert(from);
            }
        }
        assert!(
            !component.is_empty(),
            "whole traversal failed but every SCC is schedulable"
        );
        let mut forward_closure = component.clone();
        let mut work: Vec<_> = component.iter().copied().collect();
        while let Some(node) = work.pop() {
            constructor.poll()?;
            for &target in policy.imports(node) {
                constructor.poll()?;
                if target != entry && forward_closure.insert(target) {
                    work.push(target);
                }
            }
        }
        Ok(Err(ScheduleObstruction {
            component,
            forward_closure,
        }))
    }
}

impl<E> Constructor<'_, E> {
    fn poll(&mut self) -> Result<(), E> {
        (self.poll)()
    }

    fn is_tdz_free(&mut self, schedule: &EsmImportSchedule) -> Result<bool, E> {
        for &(consumer, dependency) in self.pairs {
            self.poll()?;
            if schedule.post_order()[&dependency] >= schedule.post_order()[&consumer] {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn white(&self, node: ModuleId, state: &Traversal) -> bool {
        !state.active.contains(&node) && !state.finished.contains(&node)
    }

    fn complete(&mut self, root: ModuleId, state: &mut Traversal) -> Result<bool, E> {
        self.poll()?;
        self.trials += 1;
        let mut reachable = BTreeSet::from([root]);
        let mut work = vec![root];
        while let Some(node) = work.pop() {
            self.poll()?;
            for &target in self.preferred.imports(node) {
                self.poll()?;
                if self.white(target, state) && reachable.insert(target) {
                    work.push(target);
                }
            }
        }
        // Every initially-white reachable consumer must finish in this subtree,
        // before root and its active ancestors. Such a read cannot be repaired
        // by a different import order within this trial.
        for &(consumer, dependency) in self.pairs {
            self.poll()?;
            if reachable.contains(&consumer)
                && (dependency == root || state.active.contains(&dependency))
            {
                return Ok(false);
            }
        }

        state.active.insert(root);
        state.imports.insert(root, Vec::new());
        loop {
            self.poll()?;
            let mut candidates = Vec::new();
            for &target in self.preferred.imports(root) {
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
        for &target in self.preferred.imports(root) {
            self.poll()?;
            if !ordered.contains(&target) {
                ordered.push(target);
            }
        }
        state.active.remove(&root);
        state.finished.insert(root);
        Ok(true)
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
}
