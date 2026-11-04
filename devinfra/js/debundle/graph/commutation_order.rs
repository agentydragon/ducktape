use std::collections::{HashMap, VecDeque};
use std::ops::ControlFlow;

use source_identity::SourceUnitId;

/// Temporal constraints only: co-location and rebind edges do not order
/// statements within their shared destination.
#[derive(Debug, Clone, Copy)]
pub struct TemporalEdge {
    pub from: SourceUnitId,
    pub to: SourceUnitId,
    pub constraint: TemporalConstraint,
}

#[derive(Debug, Clone, Copy)]
pub enum TemporalConstraint {
    Fixed,
    Inertable,
    Commuting(usize),
}

impl TemporalConstraint {
    fn priority(self) -> u8 {
        match self {
            Self::Fixed => 2,
            Self::Inertable => 1,
            Self::Commuting(_) => 0,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct PreservedOrder {
    pub from: SourceUnitId,
    pub via: SourceUnitId,
    pub to: SourceUnitId,
    pub by_premise: usize,
    pub inerted_by: Option<usize>,
}

/// Shared order preservation for graph construction and hypothetical premises.
/// The base index is immutable; a query allocates only its additional relations.
pub struct CommutationOrder {
    edges: Vec<TemporalEdge>,
    edge_by_pair: HashMap<(SourceUnitId, SourceUnitId), usize>,
    incoming: Vec<Vec<usize>>,
    outgoing: Vec<Vec<usize>>,
    inert: Vec<usize>,
}

fn unordered_pair(a: SourceUnitId, b: SourceUnitId) -> (SourceUnitId, SourceUnitId) {
    if a < b { (a, b) } else { (b, a) }
}

impl CommutationOrder {
    pub fn new(node_count: usize, input: impl Iterator<Item = TemporalEdge>) -> Self {
        let mut edges: Vec<TemporalEdge> = Vec::new();
        let mut edge_by_pair = HashMap::new();
        for edge in input {
            match edge_by_pair.get(&(edge.from, edge.to)) {
                Some(&index) => {
                    let previous: &mut TemporalEdge = &mut edges[index];
                    if edge.constraint.priority() > previous.constraint.priority() {
                        *previous = edge;
                    }
                }
                None => {
                    edge_by_pair.insert((edge.from, edge.to), edges.len());
                    edges.push(edge);
                }
            }
        }
        let mut incoming = vec![Vec::new(); node_count];
        let mut outgoing = vec![Vec::new(); node_count];
        let mut inert = Vec::new();
        for (index, edge) in edges.iter().enumerate() {
            outgoing[edge.from.0].push(index);
            incoming[edge.to.0].push(index);
            if matches!(edge.constraint, TemporalConstraint::Commuting(_)) {
                inert.push(index);
            }
        }
        Self {
            edges,
            edge_by_pair,
            incoming,
            outgoing,
            inert,
        }
    }

    /// Preserve paths through inert edges. `pairs` includes existing premises,
    /// followed by any proposed ones; edge witnesses use indices in that slice.
    /// Returns false only when the consumer stops after a sufficient witness.
    pub fn preserve(
        &self,
        pairs: &[(SourceUnitId, SourceUnitId)],
        emit: impl FnMut(PreservedOrder) -> ControlFlow<()>,
    ) -> bool {
        let premise_by_pair: HashMap<_, _> = pairs
            .iter()
            .enumerate()
            .map(|(index, &(a, b))| (unordered_pair(a, b), index))
            .collect();
        let mut pending: VecDeque<_> = self.inert.iter().copied().collect();
        // Visit only the proposed pairs, not every base edge per proposal.
        for &(a, b) in pairs {
            for pair in [(a, b), (b, a)] {
                if let Some(&index) = self.edge_by_pair.get(&pair)
                    && matches!(self.edges[index].constraint, TemporalConstraint::Inertable)
                {
                    pending.push_back(index);
                }
            }
        }
        Preservation {
            base: self,
            premise_by_pair,
            added: Vec::new(),
            added_by_pair: HashMap::new(),
            incoming: HashMap::new(),
            outgoing: HashMap::new(),
            pending,
            emit,
        }
        .run()
    }
}

struct Preservation<'a, F> {
    base: &'a CommutationOrder,
    premise_by_pair: HashMap<(SourceUnitId, SourceUnitId), usize>,
    added: Vec<TemporalEdge>,
    added_by_pair: HashMap<(SourceUnitId, SourceUnitId), usize>,
    incoming: HashMap<SourceUnitId, Vec<usize>>,
    outgoing: HashMap<SourceUnitId, Vec<usize>>,
    pending: VecDeque<usize>,
    emit: F,
}

impl<F: FnMut(PreservedOrder) -> ControlFlow<()>> Preservation<'_, F> {
    fn edge(&self, id: usize) -> TemporalEdge {
        let mut edge = if id < self.base.edges.len() {
            self.base.edges[id]
        } else {
            self.added[id - self.base.edges.len()]
        };
        if matches!(edge.constraint, TemporalConstraint::Inertable)
            && let Some(&index) = self
                .premise_by_pair
                .get(&unordered_pair(edge.from, edge.to))
        {
            edge.constraint = TemporalConstraint::Commuting(index);
        }
        edge
    }

    fn run(mut self) -> bool {
        while let Some(id) = self.pending.pop_front() {
            let edge = self.edge(id);
            for &incoming in &self.base.incoming[edge.from.0] {
                if self.compose(incoming, id).is_break() {
                    return false;
                }
            }
            let incoming_count = self.incoming.get(&edge.from).map_or(0, Vec::len);
            for index in 0..incoming_count {
                if self
                    .compose(self.incoming[&edge.from][index], id)
                    .is_break()
                {
                    return false;
                }
            }
            for &outgoing in &self.base.outgoing[edge.to.0] {
                if self.compose(id, outgoing).is_break() {
                    return false;
                }
            }
            let outgoing_count = self.outgoing.get(&edge.to).map_or(0, Vec::len);
            for index in 0..outgoing_count {
                if self.compose(id, self.outgoing[&edge.to][index]).is_break() {
                    return false;
                }
            }
        }
        true
    }

    fn compose(&mut self, first: usize, second: usize) -> ControlFlow<()> {
        let first = self.edge(first);
        let second = self.edge(second);
        let by_premise = match (first.constraint, second.constraint) {
            (TemporalConstraint::Commuting(index), _)
            | (_, TemporalConstraint::Commuting(index)) => index,
            _ => return ControlFlow::Continue(()),
        };
        let (from, via, to) = (first.from, first.to, second.to);
        if from == to
            || self.base.edge_by_pair.contains_key(&(from, to))
            || self.added_by_pair.contains_key(&(from, to))
        {
            return ControlFlow::Continue(());
        }
        let inerted_by = self.premise_by_pair.get(&unordered_pair(from, to)).copied();
        let id = self.base.edges.len() + self.added.len();
        self.added.push(TemporalEdge {
            from,
            to,
            constraint: inerted_by.map_or(TemporalConstraint::Fixed, TemporalConstraint::Commuting),
        });
        self.added_by_pair.insert((from, to), id);
        self.outgoing.entry(from).or_default().push(id);
        self.incoming.entry(to).or_default().push(id);
        self.pending.push_back(id);
        (self.emit)(PreservedOrder {
            from,
            via,
            to,
            by_premise,
            inerted_by,
        })
    }
}
