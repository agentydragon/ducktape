//! Positive lexical-access witnesses over the completed possible-call graph.
//! Unknown calls keep their existing havoc. An empty witness set never proves
//! absence of effects, completion, purity or initialization availability.

use super::*;
use crate::inference::IterativeFacts;

#[derive(Default)]
pub(super) struct CallEffectBody {
    pub(super) reads: BTreeSet<BindingSlot>,
    pub(super) writes: BTreeSet<BindingSlot>,
    pub(super) calls: Vec<usize>,
    pub(super) descendants: BTreeSet<u32>,
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
/// Resolve call targets once, then share each callee's growing access fact.
pub(super) struct CallEffectRules {
    direct: BTreeMap<FlowKey, KnownCellAccesses>,
    successors: BTreeMap<FlowKey, BTreeSet<FlowKey>>,
    #[cfg(test)]
    transfers: Rc<Cell<usize>>,
}

impl CallEffectRules {
    pub(super) fn new(
        bodies: BTreeMap<u32, CallEffectBody>,
        events: &[FlowEvent],
        values: &IterativeFacts<FlowKey, FlowFact>,
        declared: &[bool],
        origins: &Arc<OriginTable>,
    ) -> Self {
        let mut rules = Self {
            direct: BTreeMap::new(),
            successors: BTreeMap::new(),
            #[cfg(test)]
            transfers: Rc::default(),
        };
        for (function, body) in bodies {
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
                append_value_expr(callee, &mut targets, declared, &mut |key| match values
                    .get(&key)?
                    .value()
                {
                    FlowFact::Values(values) => Some(values.clone()),
                    _ => unreachable!("value-expression key"),
                });
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

    pub(super) fn producers(&self) -> Vec<FlowKey> {
        self.direct.keys().copied().collect()
    }

    fn propagate(
        &self,
        target: FlowKey,
        sources: impl IntoIterator<Item = FlowKey>,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        let mut accesses = KnownCellAccesses::default();
        for source in sources {
            #[cfg(test)]
            self.transfers.set(self.transfers.get() + 1);
            if let Some(FlowFact::Cells(found)) = context.read(source) {
                accesses.union_with(found);
            }
        }
        context.propose(target, FlowFact::Cells(accesses));
    }
}

impl IterativeRules for CallEffectRules {
    type Key = FlowKey;
    type Fact = FlowFact;
    type Rule = FlowKey;

    const EVALUATES_CHANGES: bool = true;

    fn evaluate(&self, rule: &FlowKey, context: &mut impl FactContext<FlowKey, FlowFact>) {
        context.propose(*rule, FlowFact::Cells(self.direct[rule].clone()));
        self.propagate(
            *rule,
            self.successors.get(rule).into_iter().flatten().copied(),
            context,
        );
    }

    fn evaluate_changed(
        &self,
        rule: &FlowKey,
        changed: &BTreeSet<FlowKey>,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        self.propagate(*rule, changed.iter().copied(), context);
    }

    fn merge(key: &FlowKey, current: &mut FlowFact, contribution: FlowFact) -> bool {
        merge_flow_fact(key, current, contribution)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One bit crossing a shared diamond must not enumerate its paths.
    #[test]
    fn shared_diamond_propagates_only_changed_successors() {
        let layers = 128u32;
        let mut rules = CallEffectRules {
            direct: BTreeMap::new(),
            successors: BTreeMap::new(),
            transfers: Rc::default(),
        };
        for node in 0..=2 * layers {
            let key = FlowKey::InvokedCells(node);
            rules.direct.insert(key, KnownCellAccesses::default());
            if node < 2 * layers {
                let next = 2 * (node / 2 + 1);
                let targets = if next == 2 * layers {
                    vec![next]
                } else {
                    vec![next, next + 1]
                };
                rules.successors.insert(
                    key,
                    targets.into_iter().map(FlowKey::InvokedCells).collect(),
                );
            }
        }
        let leaf = FlowKey::InvokedCells(2 * layers);
        rules.direct.get_mut(&leaf).unwrap().reads = Arc::new([7].into_iter().collect());
        let edge_count = rules.successors.values().map(BTreeSet::len).sum::<usize>();
        let work = rules.transfers.clone();
        let producers = rules.producers();
        let solved = IterativeInference::new(rules).solve_values(producers);
        for fact in solved.values() {
            let FlowFact::Cells(accesses) = fact else {
                panic!("cell access fact");
            };
            assert_eq!(
                accesses.reads.as_ref(),
                &[7].into_iter().collect::<RoaringBitmap>()
            );
        }
        assert!(
            work.get() <= 3 * edge_count,
            "{} transfers across {edge_count} edges",
            work.get()
        );
    }
}
