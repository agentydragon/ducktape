//! Positive lexical-access witnesses over the completed possible-call graph.
//! Unknown calls keep their existing havoc. An empty witness set never proves
//! absence of effects, completion, purity or initialization availability.

use super::*;
use inference::IterativeFacts;
use petgraph::{
    algo::kosaraju_scc,
    graph::{DiGraph, NodeIndex},
    graphmap::DiGraphMap,
    visit::EdgeRef,
};
use rustc_hash::{FxHashMap, FxHashSet};

#[derive(Default)]
pub(super) struct CallEffectBody {
    pub(super) reads: BTreeSet<BindingSlot>,
    pub(super) writes: BTreeSet<BindingSlot>,
    pub(super) calls: Vec<usize>,
    pub(super) descendants: BTreeSet<u32>,
    pub(super) read_only_returns: Option<BTreeSet<BindingSlot>>,
}

#[derive(Debug, Clone, Default)]
#[cfg_attr(test, derive(PartialEq, Eq))]
pub(super) struct KnownCellAccesses {
    pub(super) reads: Arc<RoaringBitmap>,
    pub(super) writes: Arc<RoaringBitmap>,
}

impl KnownCellAccesses {
    pub(super) fn union_with(&mut self, other: &Self) -> bool {
        let reads = union_bitmap(&mut self.reads, &other.reads);
        union_bitmap(&mut self.writes, &other.writes) || reads
    }
}

/// Value flow is complete before this stratum: effects never refine values.
/// Resolve the exact static graph once, then close it by strongly connected
/// component so cyclic call graphs do not revisit access bitmaps per edge.
pub(super) struct CallEffectRules {
    direct: BTreeMap<FlowKey, KnownCellAccesses>,
    successors: FxHashMap<FlowKey, FxHashSet<FlowKey>>,
    callbacks: BTreeMap<u32, ReadOnlyCallback<BindingSlot>>,
}

impl CallEffectRules {
    pub(super) fn new(
        bodies: BTreeMap<u32, CallEffectBody>,
        events: &[FlowEvent],
        values: &IterativeFacts<FlowKey, FlowFact>,
        declared: &[bool],
        origins: &Arc<OriginTable>,
        returns: &ReturnInputs,
    ) -> Self {
        let mut rules = Self {
            direct: BTreeMap::new(),
            successors: FxHashMap::default(),
            callbacks: BTreeMap::new(),
        };
        for (function, body) in bodies {
            if let Some(returned) = body.read_only_returns {
                rules.callbacks.insert(
                    function,
                    ReadOnlyCallback {
                        reads: body.reads.clone(),
                        returned,
                    },
                );
            }
            let invoked = FlowKey::InvokedCells(function);
            let retained = FlowKey::RetainedCells(function);
            let source = FlowKey::SourceCells(function);
            let direct = KnownCellAccesses {
                reads: Arc::new(body.reads.into_iter().map(|slot| slot.0).collect()),
                writes: Arc::new(body.writes.into_iter().map(|slot| slot.0).collect()),
            };
            rules.direct.insert(invoked, direct.clone());
            rules.direct.insert(source, direct);
            rules.direct.insert(retained, KnownCellAccesses::default());
            rules
                .successors
                .entry(retained)
                .or_default()
                .insert(invoked);
            rules.successors.entry(retained).or_default().insert(source);
            for descendant in body.descendants {
                rules
                    .successors
                    .entry(source)
                    .or_default()
                    .insert(FlowKey::SourceCells(descendant));
            }
            for event in body.calls {
                let FlowEvent::Invoke { callee, .. } = &events[event] else {
                    unreachable!("call effect references a prepared invocation");
                };
                let mut targets = ValuesBuilder::new(origins.clone());
                append_value_expr(
                    callee,
                    &mut targets,
                    declared,
                    returns,
                    &mut |key| match values.get(&key)?.value() {
                        FlowFact::Values(values) => Some(values.clone()),
                        _ => unreachable!("value-expression key"),
                    },
                    &mut |_| {},
                );
                for target in targets.finish() {
                    if let Value::Alloc(Kind::Function, target) = target {
                        rules
                            .successors
                            .entry(invoked)
                            .or_default()
                            .insert(FlowKey::InvokedCells(target));
                        rules
                            .successors
                            .entry(retained)
                            .or_default()
                            .insert(FlowKey::RetainedCells(target));
                    }
                }
            }
        }
        rules
    }

    /// Close the finite, already-resolved graph without inference watchers.
    /// Only keys with an original direct producer are returned; successor-only
    /// keys have the same absent-as-bottom behavior as the former fact reads.
    pub(super) fn solve(self) -> BTreeMap<FlowKey, FlowFact> {
        self.solve_inner(
            #[cfg(test)]
            &mut ClosureWork::default(),
        )
    }

    #[cfg(test)]
    fn solve_with_work(self, work: &mut ClosureWork) -> BTreeMap<FlowKey, FlowFact> {
        self.solve_inner(work)
    }

    fn solve_inner(self, #[cfg(test)] work: &mut ClosureWork) -> BTreeMap<FlowKey, FlowFact> {
        let Self {
            direct,
            mut successors,
            callbacks,
        } = self;
        // The former fact solver never evaluated a successor-only key. Keep
        // such keys as bottom sinks, even if an unexpected edge names them
        // as a source too.
        successors.retain(|source, _| direct.contains_key(source));
        let output_keys: Vec<_> = direct.keys().copied().collect();
        let mut node_ids = FxHashMap::default();
        for key in direct.keys() {
            insert_node_key(*key, &mut node_ids);
        }
        for (source, targets) in &successors {
            insert_node_key(*source, &mut node_ids);
            for target in targets {
                insert_node_key(*target, &mut node_ids);
            }
        }
        let edge_capacity = successors.values().map(FxHashSet::len).sum();
        let node_count = node_ids.len();
        let mut graph = DiGraph::<(), ()>::with_capacity(node_count, edge_capacity);
        for _ in 0..node_count {
            graph.add_node(());
        }
        #[cfg(test)]
        let mut edge_count = 0;
        for (source, targets) in successors {
            let source = NodeIndex::new(node_ids[&source]);
            for target in targets {
                graph.add_edge(source, NodeIndex::new(node_ids[&target]), ());
                #[cfg(test)]
                {
                    edge_count += 1;
                }
            }
        }
        #[cfg(test)]
        {
            work.nodes = node_count;
            work.edges = edge_count;
        }

        let components = kosaraju_scc(&graph);
        let mut component_of = vec![usize::MAX; node_count];
        for (component, members) in components.iter().enumerate() {
            for member in members {
                component_of[member.index()] = component;
            }
        }
        let mut summaries = vec![KnownCellAccesses::default(); components.len()];
        for (key, accesses) in direct {
            summaries[component_of[node_ids[&key]]].union_with(&accesses);
            #[cfg(test)]
            {
                work.direct_unions += 1;
            }
        }

        let mut component_graph = DiGraphMap::<usize, ()>::new();
        for component in 0..components.len() {
            component_graph.add_node(component);
        }
        for edge in graph.edge_references() {
            let source = component_of[edge.source().index()];
            let target = component_of[edge.target().index()];
            if source != target {
                component_graph.add_edge(source, target, ());
            }
        }
        drop(graph);
        drop(components);
        #[cfg(test)]
        {
            work.components = summaries.len();
            work.component_edges = component_graph.edge_count();
        }

        // Kosaraju numbers SCCs in reverse topological order.
        for component in 0..summaries.len() {
            let (dependencies, current) = summaries.split_at_mut(component);
            for successor in component_graph.neighbors(component) {
                current[0].union_with(&dependencies[successor]);
                #[cfg(test)]
                {
                    work.successor_unions += 1;
                }
            }
        }
        drop(component_graph);

        let mut facts = BTreeMap::new();
        for key in output_keys {
            let summary = summaries[component_of[node_ids[&key]]].clone();
            facts.insert(key, FlowFact::Cells(summary));
        }
        facts.extend(callbacks.into_iter().map(|(function, callback)| {
            (
                FlowKey::ReadOnlyCallback(function),
                FlowFact::ReadOnlyCallback(callback),
            )
        }));
        facts
    }
}

fn insert_node_key(key: FlowKey, node_ids: &mut FxHashMap<FlowKey, usize>) {
    let id = node_ids.len();
    if let std::collections::hash_map::Entry::Vacant(entry) = node_ids.entry(key) {
        entry.insert(id);
    }
}

#[cfg(test)]
#[derive(Default)]
struct ClosureWork {
    nodes: usize,
    edges: usize,
    components: usize,
    component_edges: usize,
    direct_unions: usize,
    successor_unions: usize,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn accesses(reads: &[u32], writes: &[u32]) -> KnownCellAccesses {
        KnownCellAccesses {
            reads: Arc::new(reads.iter().copied().collect()),
            writes: Arc::new(writes.iter().copied().collect()),
        }
    }

    fn rules(
        direct: BTreeMap<FlowKey, KnownCellAccesses>,
        successors: BTreeMap<FlowKey, BTreeSet<FlowKey>>,
    ) -> CallEffectRules {
        CallEffectRules {
            direct,
            successors: successors
                .into_iter()
                .map(|(source, targets)| (source, targets.into_iter().collect()))
                .collect(),
            callbacks: BTreeMap::new(),
        }
    }

    /// Deliberately independent reference: walk each producer's reachability
    /// set and union direct facts for the visited keys.
    fn assert_matches_reachability_oracle(rules: CallEffectRules) {
        let direct = rules.direct.clone();
        let successors = rules.successors.clone();
        let actual = rules.solve();
        assert_eq!(actual.len(), direct.len());
        for (source, expected) in direct.keys().map(|key| {
            let mut pending = vec![*key];
            let mut visited = BTreeSet::new();
            let mut expected = KnownCellAccesses::default();
            while let Some(key) = pending.pop() {
                if !visited.insert(key) {
                    continue;
                }
                if let Some(accesses) = direct.get(&key) {
                    expected.union_with(accesses);
                    if let Some(targets) = successors.get(&key) {
                        pending.extend(targets.iter().copied());
                    }
                }
            }
            (*key, expected)
        }) {
            let FlowFact::Cells(actual) = &actual[&source] else {
                panic!("cell access fact");
            };
            assert_eq!(actual, &expected, "closure for {source:?}");
        }
        assert!(
            actual
                .keys()
                .all(|key| direct.contains_key(key) || matches!(key, FlowKey::ReadOnlyCallback(_))),
            "successor-only missing facts are not emitted"
        );
    }

    #[test]
    fn cycles_self_edges_and_shared_descendants_match_bfs_oracle() {
        let direct = BTreeMap::from([
            (FlowKey::InvokedCells(1), accesses(&[1], &[])),
            (FlowKey::InvokedCells(2), accesses(&[2], &[])),
            (FlowKey::InvokedCells(3), accesses(&[], &[3])),
            (FlowKey::RetainedCells(1), accesses(&[], &[])),
            (FlowKey::RetainedCells(2), accesses(&[4], &[])),
            (FlowKey::SourceCells(1), accesses(&[5], &[])),
            (FlowKey::SourceCells(2), accesses(&[], &[6])),
            (FlowKey::SourceCells(3), accesses(&[7], &[])),
        ]);
        let successors = BTreeMap::from([
            (
                FlowKey::InvokedCells(1),
                BTreeSet::from([FlowKey::InvokedCells(2)]),
            ),
            (
                FlowKey::InvokedCells(2),
                BTreeSet::from([FlowKey::InvokedCells(3)]),
            ),
            (
                FlowKey::InvokedCells(3),
                BTreeSet::from([FlowKey::InvokedCells(2), FlowKey::InvokedCells(3)]),
            ),
            (
                FlowKey::RetainedCells(1),
                BTreeSet::from([
                    FlowKey::InvokedCells(1),
                    FlowKey::SourceCells(1),
                    FlowKey::RetainedCells(2),
                ]),
            ),
            (
                FlowKey::RetainedCells(2),
                BTreeSet::from([FlowKey::RetainedCells(1)]),
            ),
            (
                FlowKey::SourceCells(1),
                BTreeSet::from([FlowKey::SourceCells(2), FlowKey::SourceCells(3)]),
            ),
            (
                FlowKey::SourceCells(2),
                BTreeSet::from([FlowKey::SourceCells(3)]),
            ),
            (
                FlowKey::SourceCells(3),
                BTreeSet::from([FlowKey::SourceCells(3)]),
            ),
        ]);
        assert_matches_reachability_oracle(rules(direct, successors));
    }

    #[test]
    fn immutable_read_only_callback_facts_are_preserved() {
        let callback = ReadOnlyCallback {
            reads: BTreeSet::from([BindingSlot(11)]),
            returned: BTreeSet::from([BindingSlot(12)]),
        };
        let mut rules = rules(BTreeMap::new(), BTreeMap::new());
        rules.callbacks.insert(7, callback.clone());

        let facts = rules.solve();
        let FlowFact::ReadOnlyCallback(actual) = &facts[&FlowKey::ReadOnlyCallback(7)] else {
            panic!("read-only callback fact");
        };
        assert_eq!(actual, &callback);
    }

    #[test]
    fn absent_callee_facts_are_bottom_and_not_added_as_producers() {
        let direct = BTreeMap::from([
            (FlowKey::InvokedCells(1), accesses(&[11], &[12])),
            (FlowKey::RetainedCells(1), accesses(&[], &[])),
            (FlowKey::SourceCells(1), accesses(&[13], &[])),
        ]);
        let successors = BTreeMap::from([
            (
                FlowKey::InvokedCells(1),
                BTreeSet::from([FlowKey::InvokedCells(99)]),
            ),
            (
                FlowKey::InvokedCells(99),
                BTreeSet::from([FlowKey::InvokedCells(2)]),
            ),
            (
                FlowKey::InvokedCells(2),
                BTreeSet::from([FlowKey::InvokedCells(1)]),
            ),
            (
                FlowKey::SourceCells(1),
                BTreeSet::from([FlowKey::SourceCells(99)]),
            ),
            (
                FlowKey::SourceCells(99),
                BTreeSet::from([FlowKey::SourceCells(2)]),
            ),
        ]);
        assert_matches_reachability_oracle(rules(direct, successors));
    }

    #[test]
    fn shared_diamond_propagates_once_per_condensation_edge() {
        let layers = 128u32;
        let mut direct: BTreeMap<_, _> = (0..=2 * layers)
            .map(|node| (FlowKey::InvokedCells(node), KnownCellAccesses::default()))
            .collect();
        direct
            .get_mut(&FlowKey::InvokedCells(2 * layers))
            .expect("diamond leaf")
            .reads = Arc::new([7].into_iter().collect());
        let successors: BTreeMap<_, _> = (0..2 * layers)
            .map(|node| {
                let next = 2 * (node / 2 + 1);
                let targets = if next == 2 * layers {
                    BTreeSet::from([FlowKey::InvokedCells(next)])
                } else {
                    BTreeSet::from([FlowKey::InvokedCells(next), FlowKey::InvokedCells(next + 1)])
                };
                (FlowKey::InvokedCells(node), targets)
            })
            .collect();
        let mut work = ClosureWork::default();
        let facts = rules(direct, successors).solve_with_work(&mut work);

        assert_eq!(work.components, work.nodes);
        assert_eq!(work.component_edges, work.edges);
        assert_eq!(work.successor_unions, work.component_edges);
        assert!(
            work.nodes + work.edges + work.direct_unions + work.successor_unions
                <= 3 * (work.nodes + work.edges)
        );
        for fact in facts.values() {
            let FlowFact::Cells(accesses) = fact else {
                panic!("cell access fact");
            };
            assert_eq!(
                accesses.reads.as_ref(),
                &[7].into_iter().collect::<RoaringBitmap>()
            );
        }
    }

    #[test]
    fn diamond_condensation_deduplicates_member_edges() {
        let components = [
            [FlowKey::InvokedCells(1), FlowKey::InvokedCells(2)],
            [FlowKey::RetainedCells(3), FlowKey::RetainedCells(4)],
            [FlowKey::SourceCells(5), FlowKey::SourceCells(6)],
            [FlowKey::SourceCells(7), FlowKey::SourceCells(8)],
        ];
        let direct = components
            .into_iter()
            .flatten()
            .enumerate()
            .map(|(index, key)| (key, accesses(&[index as u32], &[])))
            .collect();
        let mut successors = BTreeMap::<FlowKey, BTreeSet<FlowKey>>::new();
        for component in components {
            for source in component {
                for target in component {
                    if source != target {
                        successors.entry(source).or_default().insert(target);
                    }
                }
            }
        }
        for source in components[0] {
            for target in components[1] {
                successors.entry(source).or_default().insert(target);
            }
            for target in components[2] {
                successors.entry(source).or_default().insert(target);
            }
        }
        for source in components[1].into_iter().chain(components[2]) {
            for target in components[3] {
                successors.entry(source).or_default().insert(target);
            }
        }
        let mut work = ClosureWork::default();
        let rules = rules(direct, successors);
        let direct = rules.direct.clone();
        let successors = rules.successors.clone();
        let facts = rules.solve_with_work(&mut work);

        assert_eq!(work.nodes, 8);
        assert_eq!(work.components, 4);
        assert_eq!(work.edges, 24);
        assert_eq!(work.component_edges, 4);
        assert_eq!(work.successor_unions, 4);
        for (source, fact) in facts {
            let FlowFact::Cells(actual) = fact else {
                panic!("cell access fact");
            };
            let mut pending = vec![source];
            let mut visited = BTreeSet::new();
            let mut expected = KnownCellAccesses::default();
            while let Some(key) = pending.pop() {
                if !visited.insert(key) {
                    continue;
                }
                if let Some(accesses) = direct.get(&key) {
                    expected.union_with(accesses);
                    if let Some(targets) = successors.get(&key) {
                        pending.extend(targets.iter().copied());
                    }
                }
            }
            assert_eq!(actual, expected);
        }
    }

    #[test]
    fn large_scc_shares_arc_summaries_with_linear_graph_work() {
        let node_count = 4096u32;
        let direct: BTreeMap<_, _> = (0..node_count)
            .map(|function| {
                (
                    FlowKey::InvokedCells(function),
                    accesses(&[function], &[node_count + function]),
                )
            })
            .collect();
        let successors: BTreeMap<_, _> = (0..node_count)
            .map(|function| {
                (
                    FlowKey::InvokedCells(function),
                    BTreeSet::from([FlowKey::InvokedCells((function + 1) % node_count)]),
                )
            })
            .collect();
        let mut work = ClosureWork::default();
        let facts = rules(direct, successors).solve_with_work(&mut work);

        assert_eq!(work.nodes, node_count as usize);
        assert_eq!(work.edges, node_count as usize);
        assert_eq!(work.components, 1);
        assert_eq!(work.component_edges, 0);
        assert_eq!(work.direct_unions, node_count as usize);
        assert_eq!(work.successor_unions, 0);
        assert!(
            work.nodes + work.edges + work.direct_unions + work.successor_unions
                <= 3 * node_count as usize
        );
        let mut facts = facts.into_values();
        let FlowFact::Cells(first) = facts.next().expect("first SCC member") else {
            panic!("cell access fact");
        };
        for fact in facts {
            let FlowFact::Cells(accesses) = fact else {
                panic!("cell access fact");
            };
            assert!(Arc::ptr_eq(&first.reads, &accesses.reads));
            assert!(Arc::ptr_eq(&first.writes, &accesses.writes));
        }
        assert_eq!(first.reads.len(), node_count as u64);
        assert_eq!(first.writes.len(), node_count as u64);
        assert!(first.reads.contains_range(0..node_count));
        assert!(first.writes.contains_range(node_count..2 * node_count));
    }
}
