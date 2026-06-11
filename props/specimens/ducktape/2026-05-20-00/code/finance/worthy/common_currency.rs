use denomination::Denomination;
use exchange_rate::ExchangeRate;
use log::{trace, warn};
use petgraph::{
    algo::FloatMeasure,
    // algo::bellman_ford,
    prelude::*,
    visit::{IntoEdges, IntoNodeIdentifiers, NodeCount, NodeIndexable},
};
use rust_decimal::prelude::*;
use rust_decimal_macros::*;
use std::collections::{HashMap, HashSet};
use std::{
    cmp::{Ord, Ordering, Ordering::*},
    default::Default,
    fmt::Debug,
    ops::Add,
};

#[derive(Copy, Clone, Debug, PartialEq, Default)]
enum MultiplyDecimal {
    Finite(Decimal),
    #[default]
    Infinite,
}

use MultiplyDecimal::*;

impl PartialOrd for MultiplyDecimal {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(match (self, other) {
            (Infinite, Finite(_)) => Greater,
            (Finite(_), Infinite) => Less,
            (Infinite, Infinite) => Equal,
            (Finite(a), Finite(b)) => a.cmp(b),
        })
    }
}

impl FloatMeasure for MultiplyDecimal {
    fn zero() -> Self {
        Finite(dec!(1))
    }
    fn infinite() -> Self {
        Infinite
    }
    fn from_f32(val: f32) -> Self {
        Decimal::try_from(val).map(Finite).unwrap_or(Infinite)
    }
    fn from_f64(val: f64) -> Self {
        Decimal::try_from(val).map(Finite).unwrap_or(Infinite)
    }
}

impl Add for MultiplyDecimal {
    type Output = Self;

    // Intentionally uses multiplication: this type represents multiplicative
    // edge weights for shortest-path algorithms that need a monoid.
    #[allow(clippy::suspicious_arithmetic_impl)]
    fn add(self, other: Self) -> Self {
        match (self, other) {
            (Finite(x), Finite(y)) => Finite(x * y),
            _ => Infinite,
        }
    }
}

pub fn bellman_ford<G>(g: G, source: G::NodeId) -> Vec<G::EdgeWeight>
where
    G: NodeCount + IntoNodeIdentifiers + IntoEdges + NodeIndexable,
    G::EdgeWeight: FloatMeasure,
    G::NodeId: Debug,
{
    let mut predecessor = vec![None; g.node_bound()];
    let mut distance = vec![<_>::infinite(); g.node_bound()];

    let ix = |i| g.to_index(i);

    distance[ix(source)] = FloatMeasure::zero();
    // scan up to |V| - 1 times.
    for _ in 1..g.node_count() {
        let mut did_update = false;
        for edge in g.edge_references() {
            let i = edge.source();
            let j = edge.target();
            let w = *edge.weight();
            if distance[ix(i)] + w < distance[ix(j)] {
                distance[ix(j)] = distance[ix(i)] + w;
                predecessor[ix(j)] = Some(i);
                did_update = true;
            }
        }
        if !did_update {
            break;
        }
    }

    for i in g.node_identifiers() {
        for edge in g.edges(i) {
            let j = edge.target();
            let w = *edge.weight();
            if distance[ix(i)] + w < distance[ix(j)] {
                warn!("neg cycle, detected from {i:?} to {j:?}, weight={w:?}");
                //break true;
            }
        }
    }

    distance
}

/// From petgraph, modified to use multiplication instead of addition.
/// https://docs.rs/petgraph/0.4.0/src/petgraph/.cargo/registry/src/github.com-1ecc6299db9ec823/petgraph-0.4.0/src/algo.rs.html#550-592,
///
/// TODO(agentydragon): send PR to upstream petgraph for custom binary function
//pub fn bellman_ford<G>(
//    g: G,
//    source: G::NodeId,
//) -> Result<(Vec<f64>, Vec<Option<G::NodeId>>), petgraph::algo::NegativeCycle>
//where
//    G: NodeCount + IntoNodeIdentifiers + IntoEdges + NodeIndexable,
//    G::EdgeWeight: f64,
//{
//    let mut predecessor = vec![None; g.node_bound()];
//    let mut distance = vec![<_>::infinite(); g.node_bound()];
//
//    let ix = |i| g.to_index(i);
//
//    distance[ix(source)] = /* zero */;
//    // scan up to |V| - 1 times.
//    for _ in 1..g.node_count() {
//        let mut did_update = false;
//        for edge in g.edge_references() {
//            let i = edge.source();
//            let j = edge.target();
//            let w = *edge.weight();
//            if distance[ix(i)] + w < distance[ix(j)] {
//                distance[ix(j)] = distance[ix(i)] + w;
//                predecessor[ix(j)] = Some(i);
//                did_update = true;
//            }
//        }
//        if !did_update {
//            break;
//        }
//    }
//
//    // check for negative weight cycle
//    for i in g.node_identifiers() {
//        for edge in g.edges(i) {
//            let j = edge.target();
//            let w = *edge.weight();
//            if distance[ix(i)] * w < distance[ix(j)] {
//                //println!("neg cycle, detected from {} to {}, weight={}", i, j, w);
//                return Err(NegativeCycle(()));
//            }
//        }
//    }
//
//    Ok((distance, predecessor))
//}
//
pub fn in_common_currency(
    all_conversions: &[ExchangeRate],
    base: &Denomination,
) -> HashMap<Denomination, Decimal> {
    let mut g = Graph::new();
    // Exchanges might have created even more denominations.
    let unique_denominations: HashSet<Denomination> = all_conversions
        .iter()
        .cloned()
        .flat_map(|c| vec![c.from.clone(), c.to])
        .collect();
    // TODO: if base not in unique_denominations, fail
    let denomination_to_node: HashMap<Denomination, petgraph::graph::NodeIndex<_>> =
        unique_denominations
            .iter()
            .cloned()
            .map(|denomination| {
                (
                    denomination.clone(),
                    g.add_node(/* weight */ Some(denomination)),
                )
            })
            .collect();
    let conversion_tuples: Vec<_> = all_conversions
        .iter()
        .flat_map(|conversion| {
            vec![
                (
                    denomination_to_node[&conversion.to],
                    denomination_to_node[&conversion.from],
                    Finite(conversion.rate),
                ),
                // Reverse edges, if needed:
                (
                    denomination_to_node[&conversion.from],
                    denomination_to_node[&conversion.to],
                    Finite(dec!(1.0) / conversion.rate),
                ),
            ]
        })
        .collect();
    trace!("{conversion_tuples:?}");
    g.extend_with_edges(&conversion_tuples);

    // println!("{:?}", petgraph::dot::Dot::with_config(&g, &[]));

    // TODO: from config
    let start = denomination_to_node[base];
    trace!("Start: {:?}", &start);
    let costs = bellman_ford(&g, start);
    trace!("costs={costs:?}");

    // On success, return one vec with path costs, and another one which points
    // out the predecessor of a node along a shortest path.
    denomination_to_node
        .into_iter()
        .filter_map(|(denomination, node)| {
            let cost = costs[node.index()];
            match cost {
                Infinite => None,
                Finite(x) => Some((denomination, x)),
            }
        })
        .collect()
}
