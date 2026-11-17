//! Completed candidate containment over immutable value-flow facts.
//!
//! The value stratum is quiescent before this graph is compiled. Candidate
//! reachability therefore has a fixed edge set and is a union over the graph's
//! seeded roots, rather than a second changing-fact problem.

#[cfg(test)]
use super::OriginId;
use super::{FlowFact, FlowKey, OriginTable};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::inference::IterativeFacts;
use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use roaring::RoaringBitmap;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
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
            if let Some(origin) = match key {
                FlowKey::Properties(object) if native_set_allocations.contains(object) => {
                    Some(Value::Alloc(Kind::Set, *object))
                }
                FlowKey::Properties(object) => Some(Value::Alloc(Kind::Object, *object)),
                FlowKey::Return(function) => Some(Value::Alloc(Kind::Function, *function)),
                _ => None,
            } {
                may_reach.insert(origin_table.id(origin).0);
            }
        }
        for candidate in &candidates {
            may_reach.insert(origin_table.id(*candidate).0);
        }

        let mut graph = DiGraphMap::<FlowKey, ()>::new();
        let mut seeds = BTreeMap::<FlowKey, RoaringBitmap>::new();
        for (index, candidate) in candidates.into_iter().enumerate() {
            let mut bit = RoaringBitmap::new();
            bit.insert(u32::try_from(index).expect("candidate object count exceeds u32"));
            *seeds
                .entry(FlowKey::CandidateReachable(origin_table.id(candidate)))
                .or_default() |= &bit;
        }
        for key in seeds.keys() {
            graph.add_node(*key);
        }

        let mut local_dependencies = BTreeMap::<FlowKey, ProofDependencies>::new();
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
                FlowKey::CandidateBinding(*binding)
            } else if let Some(origin) = carrier_origin(key, &native_set_allocations) {
                FlowKey::CandidateReachable(origin_table.id(origin))
            } else {
                continue;
            };

            let candidate_origins: Vec<_> = carried.ids_within(&may_reach).collect();
            if candidate_origins.is_empty() {
                continue;
            }
            graph.add_node(target);
            for origin in candidate_origins {
                graph.add_edge(FlowKey::CandidateReachable(origin), target, ());
                #[cfg(test)]
                {
                    work.candidate_edges_considered += 1;
                }
            }
            local_dependencies
                .entry(target)
                .or_default()
                .extend(judgment.dependencies());
        }

        close_graph(
            graph,
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

fn close_graph(
    graph: DiGraphMap<FlowKey, ()>,
    seeds: BTreeMap<FlowKey, RoaringBitmap>,
    local_dependencies: BTreeMap<FlowKey, ProofDependencies>,
    #[cfg(test)] work: &mut CandidateClosureWork,
) -> BTreeMap<FlowKey, CandidateReachabilityFact> {
    #[cfg(test)]
    {
        work.graph_nodes = graph.node_count();
        work.graph_edges = graph.edge_count();
    }

    let components = tarjan_scc(&graph);
    let mut component_of = BTreeMap::<FlowKey, usize>::new();
    for (component, members) in components.iter().enumerate() {
        for member in members {
            component_of.insert(*member, component);
        }
    }
    let mut summaries = vec![CandidateSummary::default(); components.len()];
    for (key, bits) in seeds {
        let component = component_of[&key];
        *Arc::make_mut(&mut summaries[component].bits) |= &bits;
    }

    let mut component_successors = vec![BTreeSet::new(); components.len()];
    let mut internal_edges = vec![false; components.len()];
    for source in graph.nodes() {
        for target in graph.neighbors(source) {
            let source_component = component_of[&source];
            let target_component = component_of[&target];
            if source_component == target_component {
                internal_edges[source_component] = true;
            } else {
                component_successors[source_component].insert(target_component);
            }
        }
    }

    let mut component_local_dependencies = vec![ProofDependencies::default(); components.len()];
    for (key, dependencies) in local_dependencies {
        component_local_dependencies[component_of[&key]].extend(&dependencies);
    }
    let mut remaining_predecessors = vec![0usize; components.len()];
    for successors in &component_successors {
        for &target in successors {
            remaining_predecessors[target] += 1;
        }
    }

    let mut ready: VecDeque<_> = remaining_predecessors
        .iter()
        .enumerate()
        .filter_map(|(component, remaining)| (*remaining == 0).then_some(component))
        .collect();
    let mut local_dependencies_applied = vec![false; components.len()];
    let mut completed = 0;
    while let Some(source) = ready.pop_front() {
        completed += 1;
        if !summaries[source].bits.is_empty() && internal_edges[source] {
            summaries[source]
                .dependencies
                .extend(&component_local_dependencies[source]);
            local_dependencies_applied[source] = true;
        }
        for target in component_successors[source].iter().copied() {
            let source_summary = summaries[source].clone();
            let target_summary = &mut summaries[target];
            *Arc::make_mut(&mut target_summary.bits) |= source_summary.bits.as_ref();
            if !source_summary.bits.is_empty() {
                target_summary
                    .dependencies
                    .extend(&source_summary.dependencies);
                if !local_dependencies_applied[target] {
                    target_summary
                        .dependencies
                        .extend(&component_local_dependencies[target]);
                    local_dependencies_applied[target] = true;
                }
            }
            #[cfg(test)]
            {
                work.summary_edge_transfers += 1;
                work.bitmap_union_calls += 1;
            }
            remaining_predecessors[target] -= 1;
            if remaining_predecessors[target] == 0 {
                ready.push_back(target);
            }
        }
    }
    assert_eq!(completed, components.len(), "SCC condensation is acyclic");

    #[cfg(test)]
    {
        work.components = components.len();
        work.condensation_edges = component_successors.iter().map(BTreeSet::len).sum();
    }

    let mut output = BTreeMap::new();
    for key in graph.nodes() {
        let summary = &summaries[component_of[&key]];
        if summary.bits.is_empty() {
            continue;
        }
        #[cfg(test)]
        {
            work.output_bitmap_cardinality += summary.bits.len();
            work.emitted_facts += 1;
        }
        output.insert(
            key,
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
    candidate_edges_considered: usize,
    graph_nodes: usize,
    graph_edges: usize,
    components: usize,
    condensation_edges: usize,
    summary_edge_transfers: usize,
    bitmap_union_calls: usize,
    emitted_facts: usize,
    output_bitmap_cardinality: u64,
}

#[cfg(test)]
mod tests {
    use super::super::{BindingSlot, Values};
    use super::*;
    use crate::analysis_inputs::ValueInputRef;
    use crate::inference::{FactContext, IterativeInference, IterativeRules};

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
        BTreeMap<FlowKey, (RoaringBitmap, ProofDependencies)>,
        CandidateClosureWork,
    ) {
        // Feed the graph closure directly so these tests can exercise graph
        // shapes independently from AST lowering and value-domain semantics.
        let mut work = CandidateClosureWork::default();
        let actual = close_graph(graph, seeds, local_dependencies, &mut work)
            .into_iter()
            .map(|(key, fact)| (key, (fact.bits.as_ref().clone(), fact.dependencies)))
            .collect();
        (actual, work)
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
        let (actual, work) = close_for_test(graph, seeds, local_dependencies);
        assert_eq!(actual, expected);
        assert_eq!(work.summary_edge_transfers, work.condensation_edges);
        assert_eq!(work.bitmap_union_calls, work.condensation_edges);
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
        assert_eq!(work.candidate_edges_considered, work.graph_edges);
        assert_eq!(work.summary_edge_transfers, work.condensation_edges);
        assert_eq!(work.bitmap_union_calls, work.condensation_edges);
        assert_eq!(work.emitted_facts, output.len());
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
    fn many_seed_bits_are_unioned_once_per_static_condensation_edge() {
        let mut graph = DiGraphMap::new();
        const ROOTS: u32 = 96;
        for index in 0..ROOTS {
            graph.add_edge(node(index), node(index + 1), ());
        }
        let seeds: BTreeMap<_, _> = (0..ROOTS).map(|index| (node(index), seed(index))).collect();
        let (actual, work) = close_for_test(graph.clone(), seeds.clone(), BTreeMap::new());
        assert_eq!(actual, oracle(&graph, &seeds, &BTreeMap::new()));
        assert_eq!(work.summary_edge_transfers, ROOTS as usize);
        assert_eq!(work.bitmap_union_calls, ROOTS as usize);
        assert_eq!(actual[&node(ROOTS)].0.len(), u64::from(ROOTS));
    }

    #[test]
    fn nodes_in_one_scc_share_the_completed_bitmap() {
        let mut graph = DiGraphMap::new();
        graph.add_edge(node(0), node(1), ());
        graph.add_edge(node(1), node(2), ());
        graph.add_edge(node(2), node(0), ());
        graph.add_edge(node(2), node(3), ());
        let output = close_graph(
            graph,
            BTreeMap::from([(node(0), seed(0))]),
            BTreeMap::new(),
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
        assert_eq!(actual[&node(0)].0, seed(0));
        assert!(actual[&node(0)].1.is_empty());
        assert!(!actual.contains_key(&node(9)));
    }
}
