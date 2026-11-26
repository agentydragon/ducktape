//! Completed candidate containment over immutable value-flow facts.
//!
//! The value stratum is quiescent before this closure runs. Its reverse graph
//! borrows each completed Values bitmap as one adjacency row, so graph storage
//! scales with nodes rather than with candidate edges.

use super::{FlowFact, FlowKey, OriginId, OriginTable, Values};
use analysis_vocabulary::inputs::ProofDependencies;
use analysis_vocabulary::value::{Kind, Value};
use inference::IterativeFacts;
use petgraph::algo::tarjan_scc;
use petgraph::visit::{GraphBase, IntoNeighbors, IntoNodeIdentifiers, NodeIndexable};
use roaring::{RoaringBitmap, bitmap::Iter as RoaringIter};
#[cfg(test)]
use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
use std::ops::Range;
use std::sync::Arc;

pub(super) struct CandidateReachabilityClosure {
    origin_table: Arc<OriginTable>,
    native_set_allocations: BTreeSet<u32>,
    candidates: Vec<Value>,
}

#[derive(Clone)]
pub(super) struct CandidateReachabilityFact {
    pub(super) bits: Arc<RoaringBitmap>,
    pub(super) dependencies: ProofDependencies,
}

impl CandidateReachabilityClosure {
    pub(super) fn new(
        origin_table: Arc<OriginTable>,
        native_set_allocations: BTreeSet<u32>,
        candidates: &[Value],
    ) -> Self {
        Self {
            origin_table,
            native_set_allocations,
            candidates: candidates.to_vec(),
        }
    }

    pub(super) fn solve(
        self,
        values: &IterativeFacts<FlowKey, FlowFact>,
    ) -> BTreeMap<FlowKey, CandidateReachabilityFact> {
        self.solve_inner(
            values,
            #[cfg(test)]
            &mut CandidateClosureWork::default(),
        )
    }

    #[cfg(test)]
    fn solve_with_work(
        self,
        values: &IterativeFacts<FlowKey, FlowFact>,
        work: &mut CandidateClosureWork,
    ) -> BTreeMap<FlowKey, CandidateReachabilityFact> {
        self.solve_inner(values, work)
    }

    fn solve_inner(
        self,
        values: &IterativeFacts<FlowKey, FlowFact>,
        #[cfg(test)] work: &mut CandidateClosureWork,
    ) -> BTreeMap<FlowKey, CandidateReachabilityFact> {
        let Self {
            origin_table,
            native_set_allocations,
            candidates,
        } = self;

        let mut may_reach = RoaringBitmap::new();
        for key in values.keys() {
            #[cfg(test)]
            {
                work.completed_keys_scanned += 1;
            }
            if let Some(origin) = carrier_origin(key, &native_set_allocations) {
                may_reach.insert(origin_table.id(origin).0);
            }
        }
        for candidate in &candidates {
            may_reach.insert(origin_table.id(*candidate).0);
        }

        // CandidateReachable ids are dense bitmap positions, so use them as
        // dense graph nodes. The array also filters values outside may_reach
        // in O(1) while the graph borrows each source bitmap.
        let mut origin_to_node = vec![None; origin_table.origins.len()];
        let mut node_keys = Vec::with_capacity(may_reach.len() as usize);
        for origin in may_reach.iter() {
            let node = node_keys.len();
            origin_to_node[origin as usize] = Some(node);
            node_keys.push(FlowKey::CandidateReachable(OriginId(origin)));
        }
        let mut adjacency: Vec<_> = (0..node_keys.len())
            .map(|_| CandidateAdjacency::Empty)
            .collect();
        let mut local_dependencies = vec![None; node_keys.len()];
        let mut seeds: Vec<_> = (0..node_keys.len()).map(|_| None).collect();

        for (index, candidate) in candidates.into_iter().enumerate() {
            let origin = origin_table.id(candidate).0 as usize;
            let node =
                origin_to_node[origin].expect("every candidate origin was added to may_reach");
            seeds[node]
                .get_or_insert_with(RoaringBitmap::new)
                .insert(u32::try_from(index).expect("candidate object count exceeds u32"));
        }

        for key in values.keys() {
            #[cfg(test)]
            {
                work.completed_facts_scanned += 1;
            }
            let Some(judgment) = values.get(key) else {
                continue;
            };
            let FlowFact::Values(carried) = judgment.value() else {
                continue;
            };
            if carried.is_empty() {
                continue;
            }

            let target = if let FlowKey::Binding(binding) = key {
                let node = node_keys.len();
                node_keys.push(FlowKey::CandidateBinding(*binding));
                adjacency.push(CandidateAdjacency::Empty);
                local_dependencies.push(None);
                seeds.push(None);
                node
            } else if let Some(origin) = carrier_origin(key, &native_set_allocations) {
                origin_to_node[origin_table.id(origin).0 as usize]
                    .expect("every carrier origin was added to may_reach")
            } else {
                continue;
            };

            let unknown_node = carried
                .unknown
                .then(|| origin_to_node[origin_table.id(Value::Top).0 as usize])
                .flatten();
            adjacency[target] = CandidateAdjacency::Values {
                values: carried,
                unknown_node,
            };
            local_dependencies[target] = Some(judgment.dependencies().clone());
        }

        let graph = CandidateGraph {
            node_keys,
            adjacency,
            origin_to_node,
            #[cfg(test)]
            edge_visits: Cell::new(0),
            #[cfg(test)]
            membership_visits: Cell::new(0),
        };
        close_graph(
            &graph,
            seeds,
            local_dependencies,
            #[cfg(test)]
            work,
        )
    }
}

fn carrier_origin(key: &FlowKey, native_set_allocations: &BTreeSet<u32>) -> Option<Value> {
    match key {
        FlowKey::Properties(object) if native_set_allocations.contains(object) => {
            Some(Value::Alloc(Kind::Set, *object))
        }
        FlowKey::Properties(object) => Some(Value::Alloc(Kind::Object, *object)),
        FlowKey::Return(function) => Some(Value::Alloc(Kind::Function, *function)),
        _ => None,
    }
}

enum CandidateAdjacency<'a> {
    Empty,
    Values {
        values: &'a Values,
        unknown_node: Option<usize>,
    },
    #[cfg(test)]
    Test(Vec<usize>),
}

struct CandidateGraph<'a> {
    node_keys: Vec<FlowKey>,
    adjacency: Vec<CandidateAdjacency<'a>>,
    /// Dense origin id to graph node mapping. Non-candidate origins are None.
    origin_to_node: Vec<Option<usize>>,
    #[cfg(test)]
    edge_visits: Cell<usize>,
    #[cfg(test)]
    membership_visits: Cell<usize>,
}

enum CandidateNeighborSource<'a> {
    Empty,
    Values {
        ids: RoaringIter<'a>,
        origin_to_node: &'a [Option<usize>],
        unknown_node: Option<usize>,
        yielded_unknown: bool,
    },
    #[cfg(test)]
    Test(std::iter::Copied<std::slice::Iter<'a, usize>>),
}

struct CandidateNeighbors<'a> {
    source: CandidateNeighborSource<'a>,
    #[cfg(test)]
    edge_visits: &'a Cell<usize>,
    #[cfg(test)]
    membership_visits: &'a Cell<usize>,
}

impl Iterator for CandidateNeighbors<'_> {
    type Item = usize;

    fn next(&mut self) -> Option<Self::Item> {
        let node = match &mut self.source {
            CandidateNeighborSource::Empty => None,
            CandidateNeighborSource::Values {
                ids,
                origin_to_node,
                unknown_node,
                yielded_unknown,
            } => loop {
                if let Some(origin) = ids.next() {
                    #[cfg(test)]
                    self.membership_visits.set(self.membership_visits.get() + 1);
                    if let Some(node) = origin_to_node.get(origin as usize).copied().flatten() {
                        break Some(node);
                    }
                    continue;
                }
                if !*yielded_unknown {
                    *yielded_unknown = true;
                    break *unknown_node;
                }
                break None;
            },
            #[cfg(test)]
            CandidateNeighborSource::Test(nodes) => nodes.next(),
        };
        #[cfg(test)]
        if node.is_some() {
            self.edge_visits.set(self.edge_visits.get() + 1);
        }
        node
    }
}

impl GraphBase for CandidateGraph<'_> {
    type NodeId = usize;
    type EdgeId = ();
}

impl<'graph, 'facts> IntoNeighbors for &'graph CandidateGraph<'facts> {
    type Neighbors = CandidateNeighbors<'graph>;

    fn neighbors(self, node: Self::NodeId) -> Self::Neighbors {
        let source = match &self.adjacency[node] {
            CandidateAdjacency::Empty => CandidateNeighborSource::Empty,
            CandidateAdjacency::Values {
                values,
                unknown_node,
            } => CandidateNeighborSource::Values {
                ids: values.bitmap.iter(),
                origin_to_node: &self.origin_to_node,
                unknown_node: *unknown_node,
                yielded_unknown: false,
            },
            #[cfg(test)]
            CandidateAdjacency::Test(nodes) => CandidateNeighborSource::Test(nodes.iter().copied()),
        };
        CandidateNeighbors {
            source,
            #[cfg(test)]
            edge_visits: &self.edge_visits,
            #[cfg(test)]
            membership_visits: &self.membership_visits,
        }
    }
}

impl<'graph, 'facts> IntoNodeIdentifiers for &'graph CandidateGraph<'facts> {
    type NodeIdentifiers = Range<usize>;

    fn node_identifiers(self) -> Self::NodeIdentifiers {
        0..self.node_keys.len()
    }
}

impl NodeIndexable for CandidateGraph<'_> {
    fn node_bound(&self) -> usize {
        self.node_keys.len()
    }

    fn to_index(&self, node: Self::NodeId) -> usize {
        node
    }

    fn from_index(&self, index: usize) -> Self::NodeId {
        index
    }
}

fn close_graph(
    graph: &CandidateGraph<'_>,
    seeds: Vec<Option<RoaringBitmap>>,
    local_dependencies: Vec<Option<ProofDependencies>>,
    #[cfg(test)] work: &mut CandidateClosureWork,
) -> BTreeMap<FlowKey, CandidateReachabilityFact> {
    #[cfg(test)]
    {
        work.graph_nodes = graph.node_keys.len();
        work.graph_adjacency_slots = graph.adjacency.len();
        work.graph_origin_index_slots = graph.origin_to_node.len();
        work.graph_source_fact_adjacencies = graph
            .adjacency
            .iter()
            .filter(|adjacency| matches!(adjacency, CandidateAdjacency::Values { .. }))
            .count();
        work.graph_materialized_edge_records = graph
            .adjacency
            .iter()
            .map(|adjacency| match adjacency {
                #[cfg(test)]
                CandidateAdjacency::Test(nodes) => nodes.len(),
                _ => 0,
            })
            .sum();
    }

    // Tarjan returns SCCs in reverse topological order. This graph reverses
    // source-fact edges, so each target's producer components appear first.
    let components = tarjan_scc(graph);
    #[cfg(test)]
    {
        work.graph_edges = graph.edge_visits.get();
    }
    let mut component_of = vec![0usize; graph.node_keys.len()];
    for (component, members) in components.iter().enumerate() {
        for member in members {
            component_of[*member] = component;
        }
    }
    let mut summaries = vec![CandidateSummary::default(); components.len()];
    for (node, bits) in seeds.into_iter().enumerate() {
        if let Some(bits) = bits {
            *Arc::make_mut(&mut summaries[component_of[node]].bits) |= &bits;
        }
    }

    // One epoch-mark vector deduplicates external producer components while
    // scanning members. No condensation edges are retained.
    let mut seen_predecessor_epoch = vec![0usize; components.len()];
    for (component, members) in components.iter().enumerate() {
        let epoch = component + 1;
        let mut has_internal_edge = false;
        let mut has_nonempty_predecessor = false;
        for member in members {
            for predecessor in graph.neighbors(*member) {
                let predecessor_component = component_of[predecessor];
                if predecessor_component == component {
                    has_internal_edge = true;
                    continue;
                }
                assert!(
                    predecessor_component < component,
                    "tarjan_scc must return reverse-topological SCC order for reversed candidate edges"
                );
                if seen_predecessor_epoch[predecessor_component] == epoch {
                    continue;
                }
                seen_predecessor_epoch[predecessor_component] = epoch;

                #[cfg(test)]
                {
                    work.condensation_edges += 1;
                }

                let (earlier, current_and_later) = summaries.split_at_mut(component);
                let predecessor_summary = &earlier[predecessor_component];
                let current_summary = &mut current_and_later[0];
                *Arc::make_mut(&mut current_summary.bits) |= predecessor_summary.bits.as_ref();
                if !predecessor_summary.bits.is_empty() {
                    current_summary
                        .dependencies
                        .extend(&predecessor_summary.dependencies);
                    has_nonempty_predecessor = true;
                }
            }
        }

        // Local dependencies activate only when bits traverse an incoming
        // edge, including an edge internal to a seeded SCC. Isolated seeds
        // retain their prior bottom-dependency behavior.
        if !summaries[component].bits.is_empty() && (has_internal_edge || has_nonempty_predecessor)
        {
            for member in members {
                if let Some(dependencies) = &local_dependencies[*member] {
                    summaries[component].dependencies.extend(dependencies);
                }
            }
        }
    }

    #[cfg(test)]
    {
        work.components = components.len();
        work.graph_neighbor_iteration_visits = graph.edge_visits.get();
        work.candidate_value_memberships_examined = graph.membership_visits.get();
        assert_eq!(
            graph.edge_visits.get(),
            work.graph_edges * 2,
            "Tarjan and closure should each visit every graph edge once"
        );
    }

    let mut output = BTreeMap::new();
    for (node, key) in graph.node_keys.iter().enumerate() {
        let summary = &summaries[component_of[node]];
        if summary.bits.is_empty() {
            continue;
        }
        #[cfg(test)]
        {
            work.emitted_facts += 1;
        }
        output.insert(
            *key,
            CandidateReachabilityFact {
                bits: Arc::clone(&summary.bits),
                dependencies: summary.dependencies.clone(),
            },
        );
    }
    output
}

#[derive(Clone, Default)]
struct CandidateSummary {
    bits: Arc<RoaringBitmap>,
    dependencies: ProofDependencies,
}

#[cfg(test)]
#[derive(Default, Debug)]
struct CandidateClosureWork {
    completed_keys_scanned: usize,
    completed_facts_scanned: usize,
    graph_nodes: usize,
    graph_edges: usize,
    graph_adjacency_slots: usize,
    graph_origin_index_slots: usize,
    graph_source_fact_adjacencies: usize,
    graph_materialized_edge_records: usize,
    graph_neighbor_iteration_visits: usize,
    candidate_value_memberships_examined: usize,
    components: usize,
    condensation_edges: usize,
    emitted_facts: usize,
}

#[cfg(test)]
mod tests {
    use super::super::{BindingSlot, Values};
    use super::*;
    use analysis_vocabulary::inputs::ValueInputRef;
    use inference::{FactContext, IterativeInference, IterativeRules};
    use petgraph::graphmap::DiGraphMap;
    use std::cell::Cell;
    use std::collections::VecDeque;

    fn node(index: u32) -> FlowKey {
        FlowKey::CandidateReachable(OriginId(index))
    }

    fn input_dependency(index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: format!("module{index}"),
                input_index: index,
            }]),
            ..Default::default()
        }
    }

    struct CompletedValueRules {
        dependencies: BTreeMap<FlowKey, ProofDependencies>,
    }

    impl IterativeRules for CompletedValueRules {
        type Key = FlowKey;
        type Fact = FlowFact;
        type Rule = FlowKey;

        type MergeScratch = ();
        fn evaluate(&self, rule: &FlowKey, context: &mut impl FactContext<FlowKey, FlowFact>) {
            let Some(fact) = context.read(*rule).cloned() else {
                return;
            };
            if let Some(dependencies) = self.dependencies.get(rule) {
                context.assume(dependencies);
            }
            context.propose(*rule, fact);
        }

        fn merge(key: &FlowKey, current: &mut FlowFact, contribution: FlowFact) -> bool {
            super::super::merge_flow_fact(key, current, contribution)
        }
    }

    fn oracle(
        graph: &DiGraphMap<FlowKey, ()>,
        seeds: &BTreeMap<FlowKey, RoaringBitmap>,
        local_dependencies: &BTreeMap<FlowKey, ProofDependencies>,
    ) -> BTreeMap<FlowKey, (RoaringBitmap, ProofDependencies)> {
        let mut reachable_from = BTreeMap::<FlowKey, BTreeSet<FlowKey>>::new();
        for source in graph.nodes() {
            let mut seen = BTreeSet::new();
            let mut pending = VecDeque::from([source]);
            while let Some(current) = pending.pop_front() {
                if !seen.insert(current) {
                    continue;
                }
                pending.extend(graph.neighbors(current));
            }
            reachable_from.insert(source, seen);
        }

        let seeded_nodes: BTreeSet<_> = seeds
            .iter()
            .filter_map(|(key, bits)| (!bits.is_empty()).then_some(*key))
            .collect();
        let reachable_from_seed: BTreeSet<_> = seeded_nodes
            .iter()
            .flat_map(|seed| reachable_from[seed].iter().copied())
            .collect();
        let activated_by_input: BTreeSet<_> = reachable_from_seed
            .iter()
            .flat_map(|source| graph.neighbors(*source))
            .collect();
        let mut expected = BTreeMap::new();
        for target in graph.nodes() {
            let mut bits = RoaringBitmap::new();
            for (seed, seed_bits) in seeds {
                if reachable_from[seed].contains(&target) {
                    bits |= seed_bits;
                }
            }
            if bits.is_empty() {
                continue;
            }
            let mut dependencies = ProofDependencies::default();
            for ancestor in graph.nodes() {
                if activated_by_input.contains(&ancestor)
                    && reachable_from[&ancestor].contains(&target)
                    && let Some(local) = local_dependencies.get(&ancestor)
                {
                    dependencies.extend(local);
                }
            }
            expected.insert(target, (bits, dependencies));
        }
        expected
    }

    fn close_for_test(
        graph: DiGraphMap<FlowKey, ()>,
        seeds: BTreeMap<FlowKey, RoaringBitmap>,
        local_dependencies: BTreeMap<FlowKey, ProofDependencies>,
    ) -> (
        BTreeMap<FlowKey, CandidateReachabilityFact>,
        CandidateClosureWork,
    ) {
        // Feed the graph closure directly so these tests can exercise graph
        // shapes independently from AST lowering and value-domain semantics.
        let graph = graph_view_for_test(&graph);
        let mut node_seeds: Vec<_> = (0..graph.node_keys.len()).map(|_| None).collect();
        for (key, bits) in seeds {
            node_seeds[graph
                .node_keys
                .iter()
                .position(|node| *node == key)
                .unwrap()] = Some(bits);
        }
        let mut node_dependencies: Vec<_> = (0..graph.node_keys.len()).map(|_| None).collect();
        for (key, dependencies) in local_dependencies {
            node_dependencies[graph
                .node_keys
                .iter()
                .position(|node| *node == key)
                .unwrap()] = Some(dependencies);
        }
        let mut work = CandidateClosureWork::default();
        let actual = close_graph(&graph, node_seeds, node_dependencies, &mut work);
        (actual, work)
    }

    fn graph_view_for_test(graph: &DiGraphMap<FlowKey, ()>) -> CandidateGraph<'static> {
        let mut node_keys: Vec<_> = graph.nodes().collect();
        node_keys.sort_unstable();
        let node_indices: BTreeMap<_, _> = node_keys
            .iter()
            .enumerate()
            .map(|(index, key)| (*key, index))
            .collect();
        let mut reversed_adjacency = vec![Vec::new(); node_keys.len()];
        for source in graph.nodes() {
            for target in graph.neighbors(source) {
                reversed_adjacency[node_indices[&target]].push(node_indices[&source]);
            }
        }
        CandidateGraph {
            node_keys,
            adjacency: reversed_adjacency
                .into_iter()
                .map(CandidateAdjacency::Test)
                .collect(),
            origin_to_node: Vec::new(),
            edge_visits: Cell::new(0),
            membership_visits: Cell::new(0),
        }
    }

    fn assert_oracle_and_edge_work(
        mut graph: DiGraphMap<FlowKey, ()>,
        seeds: BTreeMap<FlowKey, RoaringBitmap>,
        local_dependencies: BTreeMap<FlowKey, ProofDependencies>,
    ) {
        for (key, bits) in &seeds {
            graph.add_node(*key);
            assert!(!bits.is_empty());
        }
        let expected = oracle(&graph, &seeds, &local_dependencies);
        let (actual, _) = close_for_test(graph, seeds, local_dependencies);
        let actual: BTreeMap<_, _> = actual
            .into_iter()
            .map(|(key, fact)| (key, (fact.bits.as_ref().clone(), fact.dependencies)))
            .collect();
        assert_eq!(actual, expected);
    }

    #[test]
    fn immutable_values_compile_once_into_native_set_top_class_and_binding_edges() {
        let root = Value::Alloc(Kind::Object, 10);
        let native_set = Value::Alloc(Kind::Set, 20);
        let non_native_object = Value::Alloc(Kind::Object, 20);
        let function = Value::Alloc(Kind::Function, 40);
        let class = Value::Alloc(Kind::Class, 30);
        let irrelevant = Value::Alloc(Kind::Object, 777);
        let origin_table = Arc::new(OriginTable::from_origins([
            root,
            native_set,
            non_native_object,
            function,
            class,
            irrelevant,
        ]));

        let mut carrier_values = Values::singleton(origin_table.clone(), root);
        carrier_values.insert(Value::Top);
        let mut facts = BTreeMap::from([
            (FlowKey::Properties(20), FlowFact::Values(carrier_values)),
            (
                FlowKey::Return(40),
                FlowFact::Values(Values::singleton(origin_table.clone(), class)),
            ),
            (
                FlowKey::Binding(BindingSlot(0)),
                FlowFact::Values(Values::singleton(origin_table.clone(), root)),
            ),
            (
                FlowKey::Binding(BindingSlot(1)),
                FlowFact::Values(Values::singleton(origin_table.clone(), Value::Top)),
            ),
            (
                FlowKey::Binding(BindingSlot(2)),
                FlowFact::Values(Values::singleton(origin_table.clone(), class)),
            ),
        ]);
        for slot in 3..67 {
            facts.insert(
                FlowKey::Binding(BindingSlot(slot)),
                FlowFact::Values(Values::singleton(origin_table.clone(), irrelevant)),
            );
        }
        let dependencies = BTreeMap::from([
            (FlowKey::Properties(20), input_dependency(20)),
            (FlowKey::Return(40), input_dependency(40)),
            (FlowKey::Binding(BindingSlot(0)), input_dependency(100)),
            (FlowKey::Binding(BindingSlot(1)), input_dependency(101)),
            (FlowKey::Binding(BindingSlot(2)), input_dependency(102)),
        ]);
        let mut inference = IterativeInference::new(CompletedValueRules { dependencies });
        for (key, fact) in facts.iter() {
            inference.seed(*key, fact.clone());
        }
        let completed = inference.solve_values_resumable(facts.keys().copied());

        let mut work = CandidateClosureWork::default();
        let output = CandidateReachabilityClosure::new(
            origin_table.clone(),
            BTreeSet::from([20]),
            &[root, Value::Top, class],
        )
        .solve_with_work(&completed, &mut work);

        let id = |value| FlowKey::CandidateReachable(origin_table.id(value));
        let bitmap = |indices: &[u32]| RoaringBitmap::from_iter(indices.iter().copied());
        assert_eq!(output[&id(root)].bits.as_ref(), &bitmap(&[0]));
        assert_eq!(output[&id(Value::Top)].bits.as_ref(), &bitmap(&[1]));
        assert_eq!(output[&id(class)].bits.as_ref(), &bitmap(&[2]));
        assert_eq!(output[&id(native_set)].bits.as_ref(), &bitmap(&[0, 1]));
        assert_eq!(output[&id(function)].bits.as_ref(), &bitmap(&[2]));
        assert!(!output.contains_key(&id(non_native_object)));
        for (slot, index) in [(0, 0), (1, 1), (2, 2)] {
            assert_eq!(
                output[&FlowKey::CandidateBinding(BindingSlot(slot))]
                    .bits
                    .as_ref(),
                &bitmap(&[index])
            );
        }
        assert_eq!(output[&id(native_set)].dependencies, input_dependency(20));
        assert_eq!(output[&id(function)].dependencies, input_dependency(40));
        assert_eq!(
            output[&FlowKey::CandidateBinding(BindingSlot(0))].dependencies,
            input_dependency(100)
        );
        assert_eq!(work.completed_keys_scanned, facts.len());
        assert_eq!(work.completed_facts_scanned, facts.len());
        assert_eq!(work.graph_materialized_edge_records, 0);
        assert!(
            work.candidate_value_memberships_examined > 2 * work.graph_edges,
            "irrelevant source origins must be charged as scanned memberships"
        );
        assert_eq!(work.emitted_facts, output.len());
        for slot in 3..67 {
            assert!(!output.contains_key(&FlowKey::CandidateBinding(BindingSlot(slot))));
        }
    }

    fn seed(index: u32) -> RoaringBitmap {
        RoaringBitmap::from_iter([index])
    }

    #[test]
    fn scc_closure_matches_independent_oracle_for_graph_shapes() {
        let mut chain = DiGraphMap::new();
        for index in 0..64 {
            chain.add_edge(node(index), node(index + 1), ());
        }
        let chain_seeds = BTreeMap::from([(node(0), seed(0)), (node(32), seed(1))]);
        let chain_dependencies = BTreeMap::from([
            (node(16), input_dependency(16)),
            (node(48), input_dependency(48)),
        ]);
        assert_oracle_and_edge_work(chain, chain_seeds, chain_dependencies);

        let mut wide = DiGraphMap::new();
        for index in 1..=48 {
            wide.add_edge(node(0), node(index), ());
            wide.add_edge(node(index), node(100), ());
        }
        let wide_seeds = BTreeMap::from([(node(0), seed(0))]);
        let wide_dependencies = BTreeMap::from([(node(100), input_dependency(100))]);
        assert_oracle_and_edge_work(wide, wide_seeds, wide_dependencies);

        let mut shared_tail = DiGraphMap::new();
        for layer in 0..12 {
            for left in 0..2 {
                for right in 0..2 {
                    shared_tail.add_edge(node(layer * 2 + left), node((layer + 1) * 2 + right), ());
                }
            }
        }
        shared_tail.add_edge(node(24), node(25), ());
        let shared_seeds = BTreeMap::from([(node(0), seed(0)), (node(1), seed(1))]);
        let shared_dependencies = BTreeMap::from([
            (node(7), input_dependency(7)),
            (node(25), input_dependency(25)),
        ]);
        assert_oracle_and_edge_work(shared_tail, shared_seeds, shared_dependencies);

        let mut cycle = DiGraphMap::new();
        for index in 0..10 {
            cycle.add_edge(node(index), node((index + 1) % 10), ());
        }
        cycle.add_edge(node(4), node(4), ());
        cycle.add_edge(node(9), node(10), ());
        cycle.add_edge(node(10), node(11), ());
        cycle.add_edge(node(12), node(12), ());
        let cycle_seeds = BTreeMap::from([(node(3), seed(0)), (node(12), seed(1))]);
        let cycle_dependencies = BTreeMap::from([
            (node(5), input_dependency(5)),
            (node(11), input_dependency(11)),
            (node(12), input_dependency(12)),
        ]);
        assert_oracle_and_edge_work(cycle, cycle_seeds, cycle_dependencies);
    }

    #[test]
    fn exhaustive_three_node_graphs_match_bfs_oracle_with_seeds_and_dependencies() {
        const NODE_COUNT: u32 = 3;
        const GRAPH_COUNT: u16 = 1 << (NODE_COUNT * NODE_COUNT);
        for edge_mask in 0..GRAPH_COUNT {
            let mut graph = DiGraphMap::new();
            for index in 0..NODE_COUNT {
                graph.add_node(node(index));
            }
            for source in 0..NODE_COUNT {
                for target in 0..NODE_COUNT {
                    let bit = 1 << (source * NODE_COUNT + target);
                    if edge_mask & bit != 0 {
                        graph.add_edge(node(source), node(target), ());
                    }
                }
            }
            for seed_mask in 0..(1 << NODE_COUNT) {
                for dependency_mask in 0..(1 << NODE_COUNT) {
                    let seeds = (0..NODE_COUNT)
                        .filter(|index| seed_mask & (1 << index) != 0)
                        .map(|index| (node(index), seed(index)))
                        .collect();
                    let dependencies = (0..NODE_COUNT)
                        .filter(|index| dependency_mask & (1 << index) != 0)
                        .map(|index| (node(index), input_dependency(index as usize)))
                        .collect();
                    assert_oracle_and_edge_work(graph.clone(), seeds, dependencies);
                }
            }
        }
    }

    #[test]
    fn many_seed_bits_are_unioned_once_per_static_condensation_edge() {
        let mut graph = DiGraphMap::new();
        const ROOTS: u32 = 96;
        for index in 0..ROOTS {
            graph.add_edge(node(index), node(index + 1), ());
        }
        let seeds: BTreeMap<_, _> = (0..ROOTS).map(|index| (node(index), seed(index))).collect();
        let (actual, work) = close_for_test(graph.clone(), seeds.clone(), BTreeMap::new());
        let actual: BTreeMap<_, _> = actual
            .into_iter()
            .map(|(key, fact)| (key, (fact.bits.as_ref().clone(), fact.dependencies)))
            .collect();
        assert_eq!(actual, oracle(&graph, &seeds, &BTreeMap::new()));
        assert_eq!(work.condensation_edges, ROOTS as usize);
        assert_eq!(actual[&node(ROOTS)].0.len(), u64::from(ROOTS));
    }

    #[test]
    fn dense_shared_source_facts_keep_only_borrowed_per_node_adjacency() {
        const WIDTH: u32 = 48;
        let roots: Vec<_> = (0..WIDTH)
            .map(|index| Value::Alloc(Kind::Object, index))
            .collect();
        let origin_table = Arc::new(OriginTable::from_origins(roots.iter().copied()));
        let mut dense_values = Values::empty(origin_table.clone());
        for root in &roots {
            dense_values.insert(*root);
        }
        let source_bitmap = Arc::clone(&dense_values.bitmap);
        let facts: BTreeMap<_, _> = (0..WIDTH)
            .map(|index| {
                (
                    FlowKey::Properties(index),
                    FlowFact::Values(dense_values.clone()),
                )
            })
            .collect();
        let mut inference = IterativeInference::new(CompletedValueRules {
            dependencies: BTreeMap::new(),
        });
        for (key, fact) in &facts {
            inference.seed(*key, fact.clone());
        }
        let completed = inference.solve_values_resumable(facts.keys().copied());
        for key in facts.keys() {
            let Some(FlowFact::Values(values)) = completed.get(key).map(|fact| fact.value()) else {
                panic!("completed source fact should carry Values");
            };
            assert!(Arc::ptr_eq(&values.bitmap, &source_bitmap));
        }
        let references_before = Arc::strong_count(&source_bitmap);

        let mut work = CandidateClosureWork::default();
        let output =
            CandidateReachabilityClosure::new(origin_table.clone(), BTreeSet::new(), &roots)
                .solve_with_work(&completed, &mut work);

        assert_eq!(Arc::strong_count(&source_bitmap), references_before);
        assert_eq!(work.graph_nodes, WIDTH as usize);
        assert_eq!(work.graph_edges, (WIDTH * WIDTH) as usize);
        assert_eq!(work.graph_adjacency_slots, WIDTH as usize);
        assert_eq!(work.graph_origin_index_slots, origin_table.origins.len());
        assert_eq!(work.graph_source_fact_adjacencies, WIDTH as usize);
        assert_eq!(work.graph_materialized_edge_records, 0);
        assert_eq!(
            work.graph_neighbor_iteration_visits,
            2 * (WIDTH * WIDTH) as usize
        );
        assert_eq!(
            work.candidate_value_memberships_examined,
            2 * (WIDTH * WIDTH) as usize,
            "charge both adjacency scans for every source bitmap membership"
        );
        assert!(
            work.graph_nodes + work.graph_adjacency_slots + work.graph_origin_index_slots
                < work.graph_edges / 10,
            "graph metadata should remain proportional to nodes, not edge count: {work:?}"
        );
        let expected: BTreeSet<_> = (0..WIDTH).collect();
        for root in &roots {
            let key = FlowKey::CandidateReachable(origin_table.id(*root));
            assert_eq!(output[&key].bits.iter().collect::<BTreeSet<_>>(), expected);
        }
        let first = &output[&FlowKey::CandidateReachable(origin_table.id(roots[0]))].bits;
        for root in &roots[1..] {
            let other = &output[&FlowKey::CandidateReachable(origin_table.id(*root))].bits;
            assert!(Arc::ptr_eq(first, other));
        }
    }

    #[test]
    fn nodes_in_one_scc_share_the_completed_bitmap() {
        let mut graph = DiGraphMap::new();
        graph.add_edge(node(0), node(1), ());
        graph.add_edge(node(1), node(2), ());
        graph.add_edge(node(2), node(0), ());
        graph.add_edge(node(2), node(3), ());
        let graph = graph_view_for_test(&graph);
        let mut seeds: Vec<_> = (0..graph.node_keys.len()).map(|_| None).collect();
        let root = graph
            .node_keys
            .iter()
            .position(|key| *key == node(0))
            .unwrap();
        seeds[root] = Some(seed(0));
        let output = close_graph(
            &graph,
            seeds,
            (0..graph.node_keys.len()).map(|_| None).collect(),
            &mut CandidateClosureWork::default(),
        );
        assert!(Arc::ptr_eq(&output[&node(0)].bits, &output[&node(1)].bits));
        assert!(Arc::ptr_eq(&output[&node(1)].bits, &output[&node(2)].bits));
        assert_eq!(output[&node(3)].bits.as_ref(), &seed(0));
    }

    #[test]
    fn candidate_seed_without_a_reachable_input_keeps_bottom_dependencies() {
        let mut graph = DiGraphMap::new();
        graph.add_edge(node(9), node(0), ());
        let seeds = BTreeMap::from([(node(0), seed(0))]);
        let dependencies = BTreeMap::from([(node(0), input_dependency(0))]);
        let (actual, _) = close_for_test(graph, seeds, dependencies);
        assert_eq!(actual[&node(0)].bits.as_ref(), &seed(0));
        assert!(actual[&node(0)].dependencies.is_empty());
        assert!(!actual.contains_key(&node(9)));
    }

    #[test]
    fn unknown_top_is_filtered_when_top_is_not_a_candidate() {
        let root = Value::Alloc(Kind::Object, 10);
        let origin_table = Arc::new(OriginTable::from_origins([root]));
        let key = FlowKey::Binding(BindingSlot(0));
        let facts = BTreeMap::from([(
            key,
            FlowFact::Values(Values::singleton(origin_table.clone(), Value::Top)),
        )]);
        let mut inference = IterativeInference::new(CompletedValueRules {
            dependencies: BTreeMap::new(),
        });
        for (key, fact) in &facts {
            inference.seed(*key, fact.clone());
        }
        let completed = inference.solve_values_resumable(facts.keys().copied());
        let mut work = CandidateClosureWork::default();
        let output =
            CandidateReachabilityClosure::new(origin_table.clone(), BTreeSet::new(), &[root])
                .solve_with_work(&completed, &mut work);

        assert_eq!(output.len(), 1);
        assert_eq!(
            output[&FlowKey::CandidateReachable(origin_table.id(root))]
                .bits
                .as_ref(),
            &seed(0)
        );
        assert_eq!(work.graph_edges, 0);
        assert!(!output.contains_key(&FlowKey::CandidateBinding(BindingSlot(0))));
    }

    #[test]
    fn duplicate_candidate_roots_keep_all_seed_indices() {
        let root = Value::Alloc(Kind::Object, 10);
        let origin_table = Arc::new(OriginTable::from_origins([root]));
        let completed = IterativeInference::new(CompletedValueRules {
            dependencies: BTreeMap::new(),
        })
        .solve_values_resumable(std::iter::empty());
        let output =
            CandidateReachabilityClosure::new(origin_table.clone(), BTreeSet::new(), &[root, root])
                .solve(&completed);

        assert_eq!(
            output[&FlowKey::CandidateReachable(origin_table.id(root))]
                .bits
                .as_ref(),
            &RoaringBitmap::from_iter([0, 1])
        );
    }
}
