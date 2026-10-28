//! Growing facts over a finite domain. Producers may contribute to the same key;
//! consumers are revisited whenever its value or proof dependencies grow.
//!
//! Only the completed closure is exposed outside an evaluation. Rules must be
//! monotone in the order defined by `merge`; absence during evaluation is bottom,
//! not evidence of impossibility. Optimistic or position-sensitive conclusions
//! require a separate completion boundary before becoming positive certificates.

use super::Judgment;
use crate::analysis_inputs::ProofDependencies;
use std::collections::{BTreeMap, BTreeSet};

/// Typed access to facts during one producer evaluation. A family adapter can
/// project this interface onto a shared program store without copying facts or
/// hiding reads from the dependency worklist.
pub(crate) trait FactContext<Key, Fact> {
    fn read(&mut self, key: Key) -> Option<&Fact>;
    fn propose(&mut self, key: Key, contribution: Fact) -> bool;
    fn assume(&mut self, dependencies: &ProofDependencies);
}

/// Positive requirements over the same store used by growing facts.
/// Requiring an absent fact must never allow a certificate to escape.
pub(crate) trait CertificateContext<Key, Fact> {
    fn require(&mut self, key: Key) -> Option<&Fact>;
    fn assume(&mut self, dependencies: &ProofDependencies);
}

pub(crate) struct GuardedContext<'a, Context> {
    inner: &'a mut Context,
    missing: bool,
}

impl<'a, Context> GuardedContext<'a, Context> {
    pub(crate) fn new(inner: &'a mut Context) -> Self {
        Self {
            inner,
            missing: false,
        }
    }

    pub(crate) fn finish<Fact>(self, contribution: Option<Fact>) -> Option<Fact> {
        contribution.filter(|_| !self.missing)
    }
}

impl<Key, Fact, Context: FactContext<Key, Fact>> CertificateContext<Key, Fact>
    for GuardedContext<'_, Context>
{
    fn require(&mut self, key: Key) -> Option<&Fact> {
        let fact = self.inner.read(key);
        self.missing |= fact.is_none();
        fact
    }

    fn assume(&mut self, dependencies: &ProofDependencies) {
        self.inner.assume(dependencies);
    }
}

pub(crate) trait IterativeRules: Sized {
    type Key: Clone + Ord;
    type Fact;
    type Rule: Clone + Ord;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>);

    /// Join a contribution into a fact, returning whether its value changed.
    /// The semantic domain must have finite height and an associative,
    /// commutative, idempotent join. A terminal diagnostic can retain its first
    /// witness in deterministic producer order without changing that semantic
    /// result. Proof dependencies are joined separately by the engine.
    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool;
}

pub(crate) struct IterativeInference<R: IterativeRules> {
    rules: R,
    facts: BTreeMap<R::Key, Judgment<R::Key, R::Fact>>,
}

impl<R: IterativeRules> IterativeInference<R> {
    pub(crate) fn new(rules: R) -> Self {
        Self {
            rules,
            facts: BTreeMap::new(),
        }
    }

    pub(crate) fn seed(&mut self, key: R::Key, value: R::Fact) {
        let mut context = IterativeContext::<R>::new(&mut self.facts);
        context.propose(key, value);
    }

    /// All producers must be supplied. Source-order rounds preserve clients
    /// whose conservative approximation depends on the initial source sweep.
    /// A rule reading its own output runs again in the next round when needed.
    pub(crate) fn solve(
        self,
        producers: impl IntoIterator<Item = R::Rule>,
    ) -> IterativeFacts<R::Key, R::Fact> {
        let Self { rules, mut facts } = self;
        let mut pending: BTreeSet<_> = producers.into_iter().map(|rule| (0usize, rule)).collect();
        let mut scheduled: BTreeMap<_, _> = pending
            .iter()
            .map(|(round, rule)| (rule.clone(), *round))
            .collect();
        let mut watchers: BTreeMap<R::Key, BTreeSet<R::Rule>> = BTreeMap::new();
        #[cfg(test)]
        let mut evaluations = 0usize;
        while let Some((round, rule)) = pending.pop_first() {
            scheduled.remove(&rule);
            let mut context = IterativeContext::<R>::new(&mut facts);
            rules.evaluate(&rule, &mut context);
            #[cfg(test)]
            {
                evaluations += 1;
            }
            for key in context.reads {
                watchers.entry(key).or_default().insert(rule.clone());
            }
            for key in context.changed {
                let Some(dependents) = watchers.get(&key) else {
                    continue;
                };
                for dependent in dependents {
                    let next_round = if dependent > &rule {
                        round
                    } else {
                        round.checked_add(1).expect("inference round overflow")
                    };
                    if scheduled
                        .get(dependent)
                        .is_some_and(|scheduled| *scheduled <= next_round)
                    {
                        continue;
                    }
                    if let Some(previous) = scheduled.insert(dependent.clone(), next_round) {
                        pending.remove(&(previous, dependent.clone()));
                    }
                    pending.insert((next_round, dependent.clone()));
                }
            }
        }
        IterativeFacts {
            facts,
            #[cfg(test)]
            evaluations,
        }
    }
}

/// Mutable access is scoped to one monotone producer. Both absent and present
/// reads install watchers, including reads made before a producer exists.
pub(crate) struct IterativeContext<'a, R: IterativeRules> {
    facts: &'a mut BTreeMap<R::Key, Judgment<R::Key, R::Fact>>,
    reads: BTreeSet<R::Key>,
    changed: BTreeSet<R::Key>,
    dependencies: ProofDependencies,
}

impl<'a, R: IterativeRules> IterativeContext<'a, R> {
    fn new(facts: &'a mut BTreeMap<R::Key, Judgment<R::Key, R::Fact>>) -> Self {
        Self {
            facts,
            reads: BTreeSet::new(),
            changed: BTreeSet::new(),
            dependencies: ProofDependencies::default(),
        }
    }
}

impl<R: IterativeRules> FactContext<R::Key, R::Fact> for IterativeContext<'_, R> {
    fn read(&mut self, key: R::Key) -> Option<&R::Fact> {
        self.reads.insert(key.clone());
        let fact = self.facts.get(&key)?;
        self.dependencies.extend(fact.dependencies());
        Some(fact.value())
    }

    /// Writes are immediately visible to subsequent reads in this producer.
    fn propose(&mut self, key: R::Key, contribution: R::Fact) -> bool {
        use std::collections::btree_map::Entry;
        let changed = match self.facts.entry(key.clone()) {
            Entry::Vacant(entry) => {
                entry.insert(Judgment {
                    value: contribution,
                    dependencies: self.dependencies.clone(),
                    premises: self.reads.clone(),
                });
                true
            }
            Entry::Occupied(mut entry) => {
                let fact = entry.get_mut();
                let changed = R::merge(&key, &mut fact.value, contribution);
                let dependencies = fact.dependencies.clone();
                fact.dependencies.extend(&self.dependencies);
                fact.premises.extend(self.reads.iter().cloned());
                changed || fact.dependencies != dependencies
            }
        };
        if changed {
            self.changed.insert(key);
        }
        changed
    }

    fn assume(&mut self, dependencies: &ProofDependencies) {
        self.dependencies.extend(dependencies);
    }
}

/// Immutable results after every supplied producer and its dependents settle.
#[derive(Debug)]
pub(crate) struct IterativeFacts<Key, Fact> {
    facts: BTreeMap<Key, Judgment<Key, Fact>>,
    #[cfg(test)]
    evaluations: usize,
}

impl<Key: Ord, Fact> IterativeFacts<Key, Fact> {
    pub(crate) fn get(&self, key: &Key) -> Option<&Judgment<Key, Fact>> {
        self.facts.get(key)
    }

    pub(crate) fn into_values(self) -> BTreeMap<Key, Fact> {
        self.facts
            .into_iter()
            .map(|(key, judgment)| (key, judgment.value))
            .collect()
    }

    #[cfg(test)]
    pub(crate) fn evaluations(&self) -> usize {
        self.evaluations
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ValueInputRef;

    struct Rules;
    impl IterativeRules for Rules {
        type Key = usize;
        type Fact = BTreeSet<usize>;
        type Rule = usize;
        fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Self::Key, Self::Fact>) {
            match rule {
                0 => {
                    if let Some(values) = context.read(2).cloned() {
                        context.propose(3, values);
                    }
                }
                1 => {
                    context.propose(2, BTreeSet::from([10]));
                }
                2 => {
                    context.assume(&ProofDependencies {
                        value_inputs: BTreeSet::from([ValueInputRef {
                            defining_chunk: "captured".into(),
                            input_index: 0,
                        }]),
                    });
                    context.propose(2, BTreeSet::from([20]));
                }
                3 => {
                    if let Some(values) = context.read(3).cloned() {
                        context.propose(2, values);
                    }
                }
                _ => unreachable!(),
            }
        }
        fn merge(_: &usize, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
            let before = current.len();
            current.extend(contribution);
            current.len() != before
        }
    }

    #[test]
    fn positive_certificates_wait_for_all_required_growing_facts() {
        struct Certificates;
        impl IterativeRules for Certificates {
            type Key = usize;
            type Fact = BTreeSet<usize>;
            type Rule = usize;
            fn evaluate(&self, rule: &usize, context: &mut impl FactContext<usize, Self::Fact>) {
                match rule {
                    0 => {
                        let mut guarded = GuardedContext::new(context);
                        // Even a producer that neglects `?` cannot publish a
                        // positive certificate with an absent requirement.
                        guarded.require(1);
                        if let Some(fact) = guarded.finish(Some(BTreeSet::from([10]))) {
                            context.propose(2, fact);
                        }
                    }
                    1 => {
                        context.propose(1, BTreeSet::from([20]));
                    }
                    _ => unreachable!(),
                }
            }
            fn merge(_: &usize, into: &mut Self::Fact, from: Self::Fact) -> bool {
                let before = into.len();
                into.extend(from);
                into.len() != before
            }
        }
        let waiting = IterativeInference::new(Certificates).solve([0]);
        assert!(waiting.get(&2).is_none());
        let complete = IterativeInference::new(Certificates).solve([0, 1]);
        assert_eq!(complete.get(&2).unwrap().value(), &BTreeSet::from([10]));
        assert_eq!(complete.get(&2).unwrap().premises(), &BTreeSet::from([1]));
    }

    #[test]
    fn shared_producers_and_cycles_converge_with_provenance() {
        let facts = IterativeInference::new(Rules).solve(0..4);
        let result = facts.get(&3).unwrap();
        assert_eq!(result.value(), &BTreeSet::from([10, 20]));
        assert_eq!(
            result.dependencies().value_inputs,
            BTreeSet::from([ValueInputRef {
                defining_chunk: "captured".into(),
                input_index: 0
            }])
        );
        assert_eq!(result.premises(), &BTreeSet::from([2]));
        assert!(facts.evaluations() < 12);
        let reversed = IterativeInference::new(Rules).solve((0..4).rev());
        for key in [2, 3] {
            let first = facts.get(&key).unwrap();
            let second = reversed.get(&key).unwrap();
            assert_eq!(first.value(), second.value());
            assert_eq!(first.dependencies(), second.dependencies());
            assert_eq!(first.premises(), second.premises());
        }
        assert_eq!(facts.into_values(), reversed.into_values());
    }

    #[test]
    fn unsupported_cycle_has_no_fact() {
        let facts = IterativeInference::new(Rules).solve([0, 3]);
        assert!(facts.get(&2).is_none());
        assert!(facts.get(&3).is_none());
        assert_eq!(facts.evaluations(), 2);
    }

    #[test]
    fn dependency_growth_wakes_existing_consumers_without_value_growth() {
        let mut inference = IterativeInference::new(Rules);
        inference.seed(2, BTreeSet::from([20]));
        // The consumer runs first. Producer 2 contributes the same value with
        // a new author premise, which must propagate through the existing fact.
        let facts = inference.solve([0, 2]);
        let result = facts.get(&3).unwrap();
        assert_eq!(result.value(), &BTreeSet::from([20]));
        assert_eq!(
            result.dependencies().value_inputs,
            BTreeSet::from([ValueInputRef {
                defining_chunk: "captured".into(),
                input_index: 0,
            }])
        );
        assert_eq!(result.premises(), &BTreeSet::from([2]));
    }
}
