//! Growing facts over a finite domain. Producers may contribute to the same key;
//! value consumers are revisited when values grow. Typed proof additions flow
//! through recorded premise edges without evaluating semantic rules again.
//!
//! Only the completed closure is exposed outside an evaluation. Rules must be
//! monotone in the order defined by `merge`; absence during evaluation is bottom,
//! not evidence of impossibility. Optimistic or position-sensitive conclusions
//! require a separate completion boundary before becoming positive certificates.

use super::Judgment;
use crate::analysis_inputs::ProofDependencies;
use rustc_hash::{FxHashMap, FxHashSet};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::hash::Hash;

/// Independent semantic and typed-evidence changes from one contribution.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct FactChange {
    pub(crate) value: bool,
    pub(crate) proof: bool,
}

/// Typed access to facts during one producer evaluation. A family adapter can
/// project this interface onto a shared program store without copying facts or
/// hiding reads from the dependency worklist.
pub(crate) trait FactContext<Key, Fact> {
    fn read(&mut self, key: Key) -> Option<&Fact>;
    fn propose(&mut self, key: Key, contribution: Fact) -> FactChange;
    fn assume(&mut self, dependencies: &ProofDependencies);

    /// Read a completed immutable fact that may have multiple consumers. Its
    /// typed dependencies are retained, but no watcher or live premise key is
    /// installed: the caller guarantees this version can never change.
    /// Absence is still missing evidence, never a certificate of absence.
    fn read_final(&mut self, key: Key) -> Option<&Fact>;

    /// Consume a completed single-consumer transfer, retaining its typed proof
    /// dependencies. Only acyclic, immutable transfers can use this operation:
    /// the caller must guarantee no future producer or reader of this key.
    /// Growing facts and certificates needed by later consumers stay in the
    /// store. This bounds memory across one-way program-point transitions.
    fn take_final(&mut self, key: Key) -> Option<Fact>;
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
    type Key: Clone + Ord + Hash;
    type Fact;
    type Rule: Clone + Ord + Hash;

    /// A checked promise that this family cannot introduce or inherit typed
    /// proof dependencies. Dense source-only stores need no proof-edge index.
    const SOURCE_ONLY: bool = false;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>);

    /// Whether the engine records, per rule, the keys that changed after the
    /// rule read them, and evaluates through `evaluate_changed`. These are
    /// value changes only; proof additions are forwarded by the context.
    const EVALUATES_CHANGES: bool = false;

    /// Re-evaluate `rule` given `changed`: every key it has read that changed
    /// after it read it. A rule whose contribution is a
    /// join over its reads may join only the changed ones: the previous
    /// contribution is already merged into its target, so the merged fact
    /// and the change it reports are the same. Called only when
    /// `EVALUATES_CHANGES` holds, and never for a rule's first evaluation.
    fn evaluate_changed(
        &self,
        rule: &Self::Rule,
        _changed: &BTreeSet<Self::Key>,
        context: &mut impl FactContext<Self::Key, Self::Fact>,
    ) {
        self.evaluate(rule, context);
    }

    /// Discover producers for a newly contributed request. Registration is
    /// idempotent; subsequent changes wake a producer through its normal reads.
    fn producers_for(&self, _key: &Self::Key, _fact: &Self::Fact) -> Vec<Self::Rule> {
        Vec::new()
    }

    /// Join a contribution into a fact, returning whether its value changed.
    /// The semantic domain must have finite height and an associative,
    /// commutative, idempotent join. A terminal diagnostic can retain its first
    /// witness in deterministic producer order without changing that semantic
    /// result. Proof dependencies are joined separately by the engine.
    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool;
}

pub(crate) struct IterativeInference<'input, R: IterativeRules> {
    rules: R,
    sealed: Option<&'input SealedFacts<R::Key, R::Fact>>,
    facts: FxHashMap<R::Key, Judgment<R::Key, R::Fact>>,
}

impl<'input, R: IterativeRules> IterativeInference<'input, R> {
    pub(crate) fn new(rules: R) -> Self {
        Self {
            rules,
            sealed: None,
            facts: FxHashMap::default(),
        }
    }

    /// Borrow completed positive evidence without making it writable. Missing
    /// keys remain growing reads in this solve; sealing certifies no absence.
    pub(crate) fn with_sealed(rules: R, sealed: &'input SealedFacts<R::Key, R::Fact>) -> Self {
        assert!(
            !R::SOURCE_ONLY
                || sealed
                    .0
                    .facts
                    .values()
                    .all(|fact| fact.dependencies().is_empty()),
            "source-only inference cannot inherit typed proof dependencies"
        );
        Self {
            rules,
            sealed: Some(sealed),
            facts: FxHashMap::default(),
        }
    }

    pub(crate) fn seed(&mut self, key: R::Key, value: R::Fact) {
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<R>::new(&mut self.facts, &mut proof_edges);
        context.sealed = self.sealed;
        context.propose(key, value);
    }

    /// All producers must be supplied. Source-order rounds preserve clients
    /// whose conservative approximation depends on the initial source sweep.
    /// A rule reading its own output runs again in the next round when needed.
    pub(crate) fn solve(
        self,
        producers: impl IntoIterator<Item = R::Rule>,
    ) -> IterativeFacts<R::Key, R::Fact> {
        self.solve_with_premises::<true>(producers)
    }

    /// Complete the same closure when the caller consumes only values. Keep
    /// read watchers and typed dependencies, but do not build a direct-premise
    /// graph that would immediately be discarded by `into_values`.
    pub(crate) fn solve_values(
        self,
        producers: impl IntoIterator<Item = R::Rule>,
    ) -> BTreeMap<R::Key, R::Fact> {
        self.solve_with_premises::<false>(producers).into_values()
    }

    /// Keep values and typed dependencies without explanatory premises when
    /// downstream consumers still need the completed judgments.
    pub(crate) fn solve_values_resumable(
        self,
        producers: impl IntoIterator<Item = R::Rule>,
    ) -> IterativeFacts<R::Key, R::Fact> {
        self.solve_with_premises::<false>(producers)
    }

    fn solve_with_premises<const RETAIN_PREMISES: bool>(
        self,
        producers: impl IntoIterator<Item = R::Rule>,
    ) -> IterativeFacts<R::Key, R::Fact> {
        let Self {
            rules,
            sealed,
            mut facts,
        } = self;
        // Only `pending` orders evaluation; the other worklist maps are
        // looked up per key and rule, never iterated in an order that matters.
        let mut registered: FxHashSet<_> = producers.into_iter().collect();
        for (key, fact) in &facts {
            registered.extend(rules.producers_for(key, fact.value()));
        }
        let mut pending: BTreeSet<_> = registered
            .iter()
            .cloned()
            .map(|rule| (0usize, rule))
            .collect();
        let mut scheduled: FxHashMap<_, _> = pending
            .iter()
            .map(|(round, rule)| (rule.clone(), *round))
            .collect();
        let mut watchers: FxHashMap<R::Key, FxHashSet<R::Rule>> = FxHashMap::default();
        // Each rule's keys in `watchers`. A re-evaluated rule mostly repeats
        // its reads; checking its own small set first spares a search of the
        // whole watcher map for every repeated read.
        let mut watched: FxHashMap<R::Rule, FxHashSet<R::Key>> = FxHashMap::default();
        // Live proof transfers are separate from value watchers. Each new edge
        // backfills existing atoms; later additions cross it without a rule run.
        let mut proof_edges = ProofTransfers::default();
        // With `EVALUATES_CHANGES`, each evaluated rule's watched keys that
        // changed after its last evaluation read them.
        let mut changed_since: FxHashMap<R::Rule, BTreeSet<R::Key>> = FxHashMap::default();
        #[cfg(test)]
        let mut evaluations = 0usize;
        while let Some((round, rule)) = pending.pop_first() {
            scheduled.remove(&rule);
            let mut context =
                IterativeContext::<R, RETAIN_PREMISES>::new(&mut facts, &mut proof_edges);
            context.sealed = sealed;
            match changed_since.remove(&rule) {
                Some(changed) => rules.evaluate_changed(&rule, &changed, &mut context),
                None => rules.evaluate(&rule, &mut context),
            }
            #[cfg(test)]
            {
                evaluations += 1;
            }
            let known = watched.entry(rule.clone()).or_default();
            for key in context.reads {
                if known.insert(key.clone()) {
                    watchers.entry(key).or_default().insert(rule.clone());
                }
            }
            for key in context.changed {
                let fact = facts.get(&key).expect("changed fact exists");
                for producer in rules.producers_for(&key, fact.value()) {
                    if registered.insert(producer.clone()) {
                        let next_round = if producer > rule {
                            round
                        } else {
                            round.checked_add(1).expect("inference round overflow")
                        };
                        scheduled.insert(producer.clone(), next_round);
                        pending.insert((next_round, producer));
                    }
                }
                let Some(dependents) = watchers.get(&key) else {
                    continue;
                };
                for dependent in dependents {
                    if R::EVALUATES_CHANGES {
                        changed_since
                            .entry(dependent.clone())
                            .or_default()
                            .insert(key.clone());
                    }
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
            #[cfg(test)]
            proof_deliveries: proof_edges.atom_deliveries,
        }
    }
}

/// A reverse premise index, owned and retired with one inference closure.
/// Source-only families leave it empty, including values-only projections.
struct ProofTransfers<Key> {
    edges: FxHashMap<Key, FxHashSet<Key>>,
    // With explanatory premises omitted, keep just the incoming live edges
    // needed to retire a completed transfer without scanning the whole graph.
    inputs: FxHashMap<Key, FxHashSet<Key>>,
    #[cfg(test)]
    atom_deliveries: usize,
}

impl<Key> Default for ProofTransfers<Key> {
    fn default() -> Self {
        Self {
            edges: FxHashMap::default(),
            inputs: FxHashMap::default(),
            #[cfg(test)]
            atom_deliveries: 0,
        }
    }
}

impl<Key: Eq + Hash> ProofTransfers<Key> {
    fn remove_target(&mut self, source: &Key, target: &Key) {
        let Some(targets) = self.edges.get_mut(source) else {
            return;
        };
        targets.remove(target);
        if targets.is_empty() {
            self.edges.remove(source);
        }
    }
}

#[cfg(test)]
fn proof_atom_count(dependencies: &ProofDependencies) -> usize {
    dependencies.value_inputs.len()
        + dependencies.native_operation_inputs.len()
        + dependencies.initializer_call_effect_inputs.len()
        + dependencies.imported_primitive_array_spread_inputs.len()
        + dependencies.descriptor_copy_authors.len()
        + dependencies.argument_inputs.len()
        + dependencies.host_behavior.len()
        + dependencies.program_premises.len()
        + dependencies.no_accessor_install_callables.len()
}

/// Mutable access is scoped to one monotone producer. Both absent and present
/// reads install watchers, including reads made before a producer exists.
pub(crate) struct IterativeContext<'a, R: IterativeRules, const RETAIN_PREMISES: bool = true> {
    facts: &'a mut FxHashMap<R::Key, Judgment<R::Key, R::Fact>>,
    sealed: Option<&'a SealedFacts<R::Key, R::Fact>>,
    sealed_reads: Vec<R::Key>,
    proof_edges: &'a mut ProofTransfers<R::Key>,
    /// In read order, with repeats: appending is cheaper than a set insert
    /// for every read, and the solver skips keys the rule already watches.
    reads: Vec<R::Key>,
    changed: BTreeSet<R::Key>,
    dependencies: ProofDependencies,
}

impl<'a, R: IterativeRules, const RETAIN_PREMISES: bool> IterativeContext<'a, R, RETAIN_PREMISES> {
    fn new(
        facts: &'a mut FxHashMap<R::Key, Judgment<R::Key, R::Fact>>,
        proof_edges: &'a mut ProofTransfers<R::Key>,
    ) -> Self {
        Self {
            facts,
            sealed: None,
            sealed_reads: Vec::new(),
            proof_edges,
            reads: Vec::new(),
            changed: BTreeSet::new(),
            dependencies: ProofDependencies::default(),
        }
    }

    fn extend_proof(
        &mut self,
        key: &R::Key,
        dependencies: &ProofDependencies,
    ) -> ProofDependencies {
        let fact = self.facts.get_mut(key).expect("proposed fact exists");
        let added = dependencies.difference(fact.dependencies());
        if !added.is_empty() {
            fact.dependencies
                .get_or_insert_with(Default::default)
                .extend(&added);
        }
        added
    }

    fn propagate_proof(&mut self, key: R::Key, added: ProofDependencies) {
        let mut pending = VecDeque::new();
        if !added.is_empty() {
            pending.push_back((key, added));
        }
        while let Some((source, added)) = pending.pop_front() {
            // Borrow the edge index separately so extending a target's proof
            // does not clone a potentially large fanout list.
            let Some(targets) = self.proof_edges.edges.get(&source) else {
                continue;
            };
            for target in targets {
                #[cfg(test)]
                {
                    self.proof_edges.atom_deliveries += proof_atom_count(&added);
                }
                let Some(fact) = self.facts.get_mut(target) else {
                    continue;
                };
                let delta = added.difference(fact.dependencies());
                if delta.is_empty() {
                    continue;
                }
                fact.dependencies
                    .get_or_insert_with(Default::default)
                    .extend(&delta);
                pending.push_back((target.clone(), delta));
            }
        }
    }
}

impl<R: IterativeRules, const RETAIN_PREMISES: bool> FactContext<R::Key, R::Fact>
    for IterativeContext<'_, R, RETAIN_PREMISES>
{
    fn read(&mut self, key: R::Key) -> Option<&R::Fact> {
        if let Some(fact) = self.sealed.and_then(|sealed| sealed.get(&key)) {
            self.sealed_reads.push(key);
            self.dependencies.extend(fact.dependencies());
            return Some(fact.value());
        }
        self.reads.push(key.clone());
        let fact = self.facts.get(&key)?;
        self.dependencies.extend(fact.dependencies());
        Some(fact.value())
    }

    /// Writes are immediately visible to subsequent reads in this producer.
    fn propose(&mut self, key: R::Key, contribution: R::Fact) -> FactChange {
        use std::collections::hash_map::Entry;
        assert!(
            self.sealed.is_none_or(|sealed| sealed.get(&key).is_none()),
            "cannot produce a sealed input"
        );
        let value_changed = match self.facts.entry(key.clone()) {
            Entry::Vacant(entry) => {
                entry.insert(Judgment {
                    value: contribution,
                    dependencies: None,
                    premises: (RETAIN_PREMISES
                        && (!self.reads.is_empty() || !self.sealed_reads.is_empty()))
                    .then(|| {
                        Box::new(
                            self.reads
                                .iter()
                                .chain(&self.sealed_reads)
                                .cloned()
                                .collect(),
                        )
                    }),
                });
                true
            }
            Entry::Occupied(mut entry) => {
                let fact = entry.get_mut();
                let changed = R::merge(&key, &mut fact.value, contribution);
                if RETAIN_PREMISES && (!self.reads.is_empty() || !self.sealed_reads.is_empty()) {
                    fact.premises
                        .get_or_insert_with(Default::default)
                        .extend(self.reads.iter().chain(&self.sealed_reads).cloned());
                }
                changed
            }
        };
        let mut change = FactChange {
            value: value_changed,
            proof: false,
        };
        if change.value {
            self.changed.insert(key.clone());
        }
        if R::SOURCE_ONLY {
            return change;
        }
        let mut dependencies = (!self.dependencies.is_empty()).then(|| self.dependencies.clone());
        for source in &self.reads {
            if *source != key
                && self
                    .proof_edges
                    .edges
                    .entry(source.clone())
                    .or_default()
                    .insert(key.clone())
            {
                if !RETAIN_PREMISES {
                    self.proof_edges
                        .inputs
                        .entry(key.clone())
                        .or_default()
                        .insert(source.clone());
                }
                // A newly activated edge must also receive atoms that
                // appeared after the read, even on an unchanged proposal.
                if let Some(proof) = self
                    .facts
                    .get(source)
                    .and_then(|fact| fact.dependencies.as_ref())
                {
                    #[cfg(test)]
                    {
                        self.proof_edges.atom_deliveries += proof_atom_count(proof);
                    }
                    dependencies
                        .get_or_insert_with(ProofDependencies::default)
                        .extend(proof);
                }
            }
        }
        let Some(dependencies) = dependencies else {
            return change;
        };
        let added = self.extend_proof(&key, &dependencies);
        change.proof = !added.is_empty();
        if change.proof {
            self.propagate_proof(key, added);
        }
        change
    }

    fn assume(&mut self, dependencies: &ProofDependencies) {
        assert!(
            !R::SOURCE_ONLY || dependencies.is_empty(),
            "source-only inference cannot introduce typed proof dependencies"
        );
        self.dependencies.extend(dependencies);
    }

    fn read_final(&mut self, key: R::Key) -> Option<&R::Fact> {
        let fact = self.facts.get(&key)?;
        self.dependencies.extend(fact.dependencies());
        Some(fact.value())
    }

    fn take_final(&mut self, key: R::Key) -> Option<R::Fact> {
        assert!(
            self.sealed.is_none_or(|sealed| sealed.get(&key).is_none()),
            "cannot consume a sealed shared input"
        );
        let fact = self.facts.remove(&key)?;
        self.dependencies.extend(fact.dependencies());
        // Completion freezes both values and their evidence. The proof queue
        // has drained; neither incoming nor outgoing live edges are needed.
        // Reuse the existing premise set when retained, rather than keeping
        // another forward graph beside the explanatory one.
        if RETAIN_PREMISES {
            for source in fact.premises() {
                self.proof_edges.remove_target(source, &key);
            }
        } else if let Some(sources) = self.proof_edges.inputs.remove(&key) {
            for source in sources {
                self.proof_edges.remove_target(&source, &key);
            }
        }
        let outgoing = self.proof_edges.edges.remove(&key);
        if !RETAIN_PREMISES {
            for target in outgoing.into_iter().flatten() {
                if let Some(sources) = self.proof_edges.inputs.get_mut(&target) {
                    sources.remove(&key);
                    if sources.is_empty() {
                        self.proof_edges.inputs.remove(&target);
                    }
                }
            }
        }
        self.reads.retain(|read| *read != key);
        self.changed.remove(&key);
        Some(fact.value)
    }
}

/// Shared read capability for one completed positive closure. Construction
/// consumes the solved store; no API can resume or mutate these judgments.
#[derive(Debug)]
pub(crate) struct SealedFacts<Key, Fact>(IterativeFacts<Key, Fact>);

impl<Key: Ord + Hash, Fact> SealedFacts<Key, Fact> {
    pub(crate) fn get(&self, key: &Key) -> Option<&Judgment<Key, Fact>> {
        self.0.get(key)
    }
}

/// A completed ordered result with exactly one consuming reader. There is no
/// key to republish or index to retain after the payload moves to its consumer.
#[must_use]
pub(crate) struct CompletedTransfer<Fact> {
    value: Fact,
    dependencies: ProofDependencies,
}

impl<Fact> CompletedTransfer<Fact> {
    pub(crate) fn new(value: Fact, dependencies: ProofDependencies) -> Self {
        Self {
            value,
            dependencies,
        }
    }

    pub(crate) fn consume<Key, StoreFact>(
        self,
        context: &mut (impl FactContext<Key, StoreFact> + ?Sized),
    ) -> Fact {
        context.assume(&self.dependencies);
        self.value
    }
}

/// Immutable results after every supplied producer and its dependents settle.
#[derive(Debug, Clone)]
pub(crate) struct IterativeFacts<Key, Fact> {
    facts: FxHashMap<Key, Judgment<Key, Fact>>,
    #[cfg(test)]
    evaluations: usize,
    #[cfg(test)]
    proof_deliveries: usize,
}

impl<Key: Ord + Hash, Fact> IterativeFacts<Key, Fact> {
    pub(crate) fn seal(self) -> SealedFacts<Key, Fact> {
        SealedFacts(self)
    }

    pub(crate) fn get(&self, key: &Key) -> Option<&Judgment<Key, Fact>> {
        self.facts.get(key)
    }

    /// In no particular order.
    pub(crate) fn keys(&self) -> impl Iterator<Item = &Key> {
        self.facts.keys()
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

    #[test]
    fn premises_are_allocated_only_when_read_and_keep_growing_without_value_changes() {
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        {
            let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
            for key in 1..=3 {
                context.propose(key, BTreeSet::from([key]));
            }
        }
        assert!(facts.values().all(|fact| fact.premises.is_none()));
        {
            let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
            context.read(1);
            assert_eq!(
                context.propose(3, BTreeSet::from([3])),
                FactChange::default()
            );
            context.read(2);
            assert_eq!(
                context.propose(3, BTreeSet::from([3])),
                FactChange::default()
            );
        }
        assert_eq!(
            facts[&3].premises().copied().collect::<BTreeSet<_>>(),
            BTreeSet::from([1, 2]),
        );
    }

    #[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
    enum TransferKey {
        Input,
        Intermediate,
        Output,
    }

    struct TransferRules;

    impl IterativeRules for TransferRules {
        type Key = TransferKey;
        type Fact = &'static str;
        type Rule = ();

        fn evaluate(&self, _: &(), context: &mut impl FactContext<Self::Key, Self::Fact>) {
            let Some(value) = context.take_final(TransferKey::Input) else {
                return;
            };
            context.assume(&ProofDependencies {
                value_inputs: BTreeSet::from([ValueInputRef {
                    defining_chunk: "captured".into(),
                    input_index: 0,
                }]),
                ..Default::default()
            });
            context.propose(TransferKey::Intermediate, value);
            let value = context.take_final(TransferKey::Intermediate).unwrap();
            context.propose(TransferKey::Output, value);
        }

        fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
            assert_eq!(*current, contribution);
            false
        }
    }

    #[test]
    fn completed_transfers_retire_payloads_and_preserve_typed_dependencies() {
        let mut inference = IterativeInference::new(TransferRules);
        inference.seed(TransferKey::Input, "settled input");
        let facts = inference.solve([()]);
        assert!(facts.get(&TransferKey::Input).is_none());
        assert!(facts.get(&TransferKey::Intermediate).is_none());
        let output = facts.get(&TransferKey::Output).unwrap();
        assert_eq!(*output.value(), "settled input");
        assert_eq!(output.dependencies().value_inputs.len(), 1);
        assert!(output.dependencies().value_inputs.contains(&ValueInputRef {
            defining_chunk: "captured".into(),
            input_index: 0,
        }));
    }

    #[test]
    fn a_missing_transfer_input_cannot_produce_a_result() {
        let facts = IterativeInference::new(TransferRules).solve([()]);
        assert!(facts.get(&TransferKey::Output).is_none());
    }

    #[test]
    fn completed_shared_reads_preserve_dependencies_without_live_premises() {
        let dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: "captured".into(),
                input_index: 0,
            }]),
            ..Default::default()
        };
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        {
            let mut context = IterativeContext::<TransferRules>::new(&mut facts, &mut proof_edges);
            context.assume(&dependencies);
            context.propose(TransferKey::Input, "settled input");
        }
        for output in [TransferKey::Intermediate, TransferKey::Output] {
            let mut context = IterativeContext::<TransferRules>::new(&mut facts, &mut proof_edges);
            let value = *context.read_final(TransferKey::Input).unwrap();
            context.propose(output.clone(), value);
            let result = facts.get(&output).unwrap();
            assert_eq!(*result.value(), "settled input");
            assert_eq!(result.dependencies(), &dependencies);
            assert!(result.premises().next().is_none());
        }
        assert!(facts.contains_key(&TransferKey::Input));
        let mut context = IterativeContext::<TransferRules>::new(&mut facts, &mut proof_edges);
        context.take_final(TransferKey::Input);
        assert!(context.read_final(TransferKey::Input).is_none());
    }

    struct RequestedRules;

    #[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
    enum RequestedKey {
        Inputs,
        Projection,
        Result,
    }

    #[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
    enum RequestedProducer {
        InitialInputs,
        ProjectInputs,
        PublishResult,
        AdditionalInputs,
    }

    impl IterativeRules for RequestedRules {
        type Key = RequestedKey;
        type Fact = BTreeSet<&'static str>;
        type Rule = RequestedProducer;

        fn producers_for(&self, key: &RequestedKey, _: &Self::Fact) -> Vec<RequestedProducer> {
            match key {
                RequestedKey::Inputs => vec![RequestedProducer::ProjectInputs],
                RequestedKey::Projection => vec![RequestedProducer::PublishResult],
                RequestedKey::Result => Vec::new(),
            }
        }

        fn evaluate(
            &self,
            rule: &RequestedProducer,
            context: &mut impl FactContext<RequestedKey, Self::Fact>,
        ) {
            match rule {
                RequestedProducer::InitialInputs => {
                    context.propose(RequestedKey::Inputs, BTreeSet::from(["initial"]));
                }
                RequestedProducer::ProjectInputs => {
                    if let Some(values) = context.read(RequestedKey::Inputs).cloned() {
                        context.propose(RequestedKey::Projection, values);
                    }
                }
                RequestedProducer::PublishResult => {
                    if let Some(values) = context.read(RequestedKey::Projection).cloned() {
                        context.propose(RequestedKey::Result, values);
                    }
                }
                RequestedProducer::AdditionalInputs => {
                    context.propose(RequestedKey::Inputs, BTreeSet::from(["additional"]));
                }
            }
        }

        fn merge(_: &RequestedKey, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
            let before = current.len();
            current.extend(contribution);
            current.len() != before
        }
    }

    #[test]
    fn requested_producers_are_discovered_and_wake_on_later_input_growth() {
        let facts = IterativeInference::new(RequestedRules).solve([
            RequestedProducer::InitialInputs,
            RequestedProducer::AdditionalInputs,
        ]);
        assert_eq!(
            facts.get(&RequestedKey::Result).unwrap().value(),
            &BTreeSet::from(["initial", "additional"])
        );
    }

    #[test]
    fn seeded_requests_discover_producers_without_an_initial_rule_list() {
        let mut inference = IterativeInference::new(RequestedRules);
        inference.seed(RequestedKey::Inputs, BTreeSet::from(["initial"]));
        let facts = inference.solve([]);
        assert_eq!(
            facts.get(&RequestedKey::Result).unwrap().value(),
            &BTreeSet::from(["initial"])
        );
        assert_eq!(facts.evaluations(), 2);
    }

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
                        ..Default::default()
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

    /// Rule 0 joins keys 10, 11 and 12 into key 20; rules 1 and 2 grow 10
    /// and 11 after rule 0 first read them.
    struct ChangedJoin {
        changed: std::rc::Rc<std::cell::RefCell<Vec<Vec<usize>>>>,
    }

    impl IterativeRules for ChangedJoin {
        type Key = usize;
        type Fact = BTreeSet<usize>;
        type Rule = usize;
        const EVALUATES_CHANGES: bool = true;

        fn evaluate(&self, rule: &usize, context: &mut impl FactContext<usize, Self::Fact>) {
            match rule {
                0 => {
                    let joined = [10, 11, 12]
                        .into_iter()
                        .filter_map(|key| context.read(key).cloned())
                        .flatten()
                        .collect();
                    context.propose(20, joined);
                }
                1 => {
                    context.propose(10, BTreeSet::from([1]));
                }
                2 => {
                    context.propose(11, BTreeSet::from([2]));
                }
                _ => unreachable!(),
            }
        }

        fn evaluate_changed(
            &self,
            rule: &usize,
            changed: &BTreeSet<usize>,
            context: &mut impl FactContext<usize, Self::Fact>,
        ) {
            assert_eq!(*rule, 0);
            self.changed
                .borrow_mut()
                .push(changed.iter().copied().collect());
            let joined = changed
                .iter()
                .filter_map(|key| context.read(*key).cloned())
                .flatten()
                .collect();
            context.propose(20, joined);
        }

        fn merge(_: &usize, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
            let before = current.len();
            current.extend(contribution);
            current.len() != before
        }
    }

    #[test]
    fn a_rule_evaluating_changes_receives_only_the_keys_that_grew_after_its_read() {
        let changed = std::rc::Rc::default();
        let mut inference = IterativeInference::new(ChangedJoin {
            changed: std::rc::Rc::clone(&changed),
        });
        inference.seed(12, BTreeSet::from([3]));
        let facts = inference.solve_values(0..3);
        assert_eq!(facts[&20], BTreeSet::from([1, 2, 3]));
        assert_eq!(*changed.borrow(), [vec![10, 11]]);
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
        let sealed = IterativeInference::new(Certificates).solve([]).seal();
        let waiting = IterativeInference::with_sealed(Certificates, &sealed).solve([0]);
        assert!(waiting.get(&2).is_none());
        // Absence in the sealed input is not an absence certificate: the
        // later overlay producer must still wake the waiting positive rule.
        let complete = IterativeInference::with_sealed(Certificates, &sealed).solve([0, 1]);
        assert_eq!(complete.get(&2).unwrap().value(), &BTreeSet::from([10]));
        assert_eq!(
            complete
                .get(&2)
                .unwrap()
                .premises()
                .cloned()
                .collect::<BTreeSet<_>>(),
            BTreeSet::from([1])
        );
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
        assert_eq!(
            result.premises().cloned().collect::<BTreeSet<_>>(),
            BTreeSet::from([2])
        );
        assert!(facts.evaluations() < 12);
        let reversed = IterativeInference::new(Rules).solve((0..4).rev());
        for key in [2, 3] {
            let first = facts.get(&key).unwrap();
            let second = reversed.get(&key).unwrap();
            assert_eq!(first.value(), second.value());
            assert_eq!(first.dependencies(), second.dependencies());
            assert_eq!(
                first.premises().collect::<Vec<_>>(),
                second.premises().collect::<Vec<_>>()
            );
        }
        assert_eq!(facts.into_values(), reversed.into_values());
    }

    #[test]
    fn values_only_closure_preserves_requests_cycles_and_dependency_wakeups() {
        let producers = [
            RequestedProducer::InitialInputs,
            RequestedProducer::AdditionalInputs,
        ];
        assert_eq!(
            IterativeInference::new(RequestedRules).solve_values(producers),
            IterativeInference::new(RequestedRules)
                .solve(producers)
                .into_values(),
        );
        assert_eq!(
            IterativeInference::new(Rules).solve_values(0..4),
            IterativeInference::new(Rules).solve(0..4).into_values(),
        );
        let mut inference = IterativeInference::new(Rules);
        inference.seed(2, BTreeSet::from([20]));
        // Inspect the private closure here: the public values-only API does
        // not expose judgments whose explanatory premises were discarded.
        let facts = inference.solve_with_premises::<false>([0, 2]);
        let result = facts.get(&3).unwrap();
        assert_eq!(result.value(), &BTreeSet::from([20]));
        assert_eq!(
            result.dependencies().value_inputs,
            BTreeSet::from([ValueInputRef {
                defining_chunk: "captured".into(),
                input_index: 0,
            }]),
        );
        assert_eq!(facts.evaluations(), 2);
    }

    #[test]
    fn unsupported_cycle_has_no_fact() {
        let facts = IterativeInference::new(Rules).solve([0, 3]);
        assert!(facts.get(&2).is_none());
        assert!(facts.get(&3).is_none());
        assert_eq!(facts.evaluations(), 2);
    }

    #[test]
    fn dependency_growth_reaches_existing_consumers_without_evaluating_them() {
        let mut inference = IterativeInference::new(Rules);
        inference.seed(2, BTreeSet::from([20]));
        // The consumer runs first. Producer 2 contributes the same value with
        // a new author premise, which must propagate through the existing fact.
        let facts = inference.solve([0, 2]);
        assert_eq!(facts.evaluations(), 2, "the consumer executes only once");
        let result = facts.get(&3).unwrap();
        assert_eq!(result.value(), &BTreeSet::from([20]));
        assert_eq!(
            result.dependencies().value_inputs,
            BTreeSet::from([ValueInputRef {
                defining_chunk: "captured".into(),
                input_index: 0,
            }])
        );
        assert_eq!(
            result.premises().cloned().collect::<BTreeSet<_>>(),
            BTreeSet::from([2])
        );
    }

    struct ProofGraphRules;

    #[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
    enum ProofRule {
        Edge(usize, usize),
        Axiom(usize, usize),
    }

    fn input(index: usize) -> ValueInputRef {
        ValueInputRef {
            defining_chunk: "proofs".into(),
            input_index: index,
        }
    }

    impl IterativeRules for ProofGraphRules {
        type Key = usize;
        type Fact = ();
        // The first field lets tests activate axioms before or after edges.
        type Rule = (u8, ProofRule);

        fn evaluate(&self, (_, rule): &Self::Rule, context: &mut impl FactContext<usize, ()>) {
            match rule {
                ProofRule::Edge(source, target) => {
                    assert!(context.read(*source).is_some());
                    context.propose(*target, ());
                }
                ProofRule::Axiom(target, index) => {
                    context.assume(&ProofDependencies {
                        value_inputs: BTreeSet::from([input(*index)]),
                        ..Default::default()
                    });
                    let change = context.propose(*target, ());
                    assert!(!change.value);
                }
            }
        }

        fn merge(_: &usize, _: &mut (), _: ()) -> bool {
            false
        }
    }

    fn check_proof_graph(nodes: usize, edges: &[(usize, usize)], origins: &[usize]) {
        for axioms_first in [false, true] {
            let mut inference = IterativeInference::new(ProofGraphRules);
            for node in 0..nodes {
                inference.seed(node, ());
            }
            let producers: Vec<_> = edges
                .iter()
                .map(|&(source, target)| (u8::from(axioms_first), ProofRule::Edge(source, target)))
                .chain(
                    origins.iter().enumerate().map(|(index, &node)| {
                        (u8::from(!axioms_first), ProofRule::Axiom(node, index))
                    }),
                )
                .collect();
            let facts = inference.solve(producers.clone());
            assert_eq!(
                facts.evaluations(),
                producers.len(),
                "proof growth must not execute value rules"
            );
            assert!(
                facts.proof_deliveries <= edges.len() * origins.len(),
                "one atom delivery per activated edge: {}",
                facts.proof_deliveries
            );
            let mut expected = vec![BTreeSet::new(); nodes];
            // Independent reachability oracle, one traversal per axiom.
            for (index, &origin) in origins.iter().enumerate() {
                let mut pending = vec![origin];
                let mut seen = BTreeSet::new();
                while let Some(node) = pending.pop() {
                    if !seen.insert(node) {
                        continue;
                    }
                    expected[node].insert(input(index));
                    pending.extend(
                        edges
                            .iter()
                            .filter_map(|&(source, target)| (source == node).then_some(target)),
                    );
                }
            }
            for (node, expected) in expected.iter().enumerate() {
                assert_eq!(
                    &facts.get(&node).unwrap().dependencies().value_inputs,
                    expected
                );
            }
        }
    }

    #[test]
    fn proof_deltas_match_reachability_without_value_work_on_shared_tails_and_cycles() {
        for width in [4, 16, 64] {
            let mut edges: Vec<_> = (1..=width)
                .flat_map(|branch| [(0, branch), (branch, width + 1)])
                .collect();
            edges.extend([(width + 1, 0), (width + 1, width + 2)]);
            // The last node is disconnected; three distinct origins include it.
            check_proof_graph(width + 4, &edges, &[0, width, width + 3]);
        }
    }

    #[test]
    fn proof_edges_keep_the_read_scope_of_each_proposal() {
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
        for key in 0..4 {
            context.propose(key, ());
        }
        // Only the second output consumes input 1.
        context.read(0);
        context.propose(2, ());
        context.read(1);
        context.propose(3, ());
        // End the producer before a separate producer supplies the proof.
        let mut context = IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
        context.assume(&ProofDependencies {
            value_inputs: BTreeSet::from([input(0)]),
            ..Default::default()
        });
        assert_eq!(
            context.propose(1, ()),
            FactChange {
                value: false,
                proof: true
            }
        );
        assert_eq!(context.propose(1, ()), FactChange::default());
        assert!(facts[&2].dependencies().is_empty());
        assert_eq!(
            facts[&3].dependencies().value_inputs,
            BTreeSet::from([input(0)])
        );
    }

    #[test]
    fn a_new_edge_backfills_evidence_added_after_the_read() {
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
        context.propose(0, ());
        context.read(0);
        // Another live premise acquires evidence before the output is proposed.
        let dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([input(0)]),
            ..Default::default()
        };
        let added = context.extend_proof(&0, &dependencies);
        context.propagate_proof(0, added);
        context.propose(1, ());
        assert_eq!(facts[&1].dependencies(), &dependencies);
    }

    struct SourceOnly;

    impl IterativeRules for SourceOnly {
        type Key = usize;
        type Fact = ();
        type Rule = bool;
        const SOURCE_ONLY: bool = true;

        fn evaluate(&self, add_proof: &bool, context: &mut impl FactContext<usize, ()>) {
            context.read(0);
            if *add_proof {
                context.assume(&ProofDependencies {
                    value_inputs: BTreeSet::from([input(0)]),
                    ..Default::default()
                });
            }
            context.propose(1, ());
        }

        fn merge(_: &usize, _: &mut (), _: ()) -> bool {
            false
        }
    }

    #[test]
    fn source_only_values_need_no_proof_index() {
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<SourceOnly, false>::new(&mut facts, &mut proof_edges);
        SourceOnly.evaluate(&false, &mut context);
        assert!(proof_edges.edges.is_empty());
        assert_eq!(proof_edges.atom_deliveries, 0);
    }

    #[test]
    #[should_panic(expected = "source-only inference cannot introduce typed proof dependencies")]
    fn source_only_contract_rejects_an_author_dependency() {
        IterativeInference::new(SourceOnly).solve([true]);
    }

    #[test]
    #[should_panic(expected = "source-only inference cannot inherit typed proof dependencies")]
    fn source_only_contract_rejects_inherited_proof_dependencies() {
        let mut inference = IterativeInference::new(ProofGraphRules);
        inference.seed(0, ());
        let facts = inference.solve([(0, ProofRule::Axiom(0, 0))]);
        IterativeInference::with_sealed(SourceOnly, &facts.seal());
    }

    #[test]
    fn sealed_evidence_is_borrowed_without_watchers_and_keeps_typed_premises() {
        let mut inference = IterativeInference::new(ProofGraphRules);
        inference.seed(0, ());
        let source = inference.solve([(0, ProofRule::Axiom(0, 0))]).seal();
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        {
            let mut context =
                IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
            context.sealed = Some(&source);
            let borrowed = context.read(0).unwrap();
            assert!(std::ptr::eq(borrowed, source.get(&0).unwrap().value()));
            assert!(context.reads.is_empty());
            context.propose(1, ());
        }
        assert!(proof_edges.edges.is_empty());
        assert!(proof_edges.inputs.is_empty());
        assert_eq!(
            facts[&1].dependencies(),
            source.get(&0).unwrap().dependencies()
        );
        assert_eq!(facts[&1].premises().copied().collect::<Vec<_>>(), [0]);
        // Each independent consumer retains only its own new judgments.
        for _ in 0..2 {
            let result = IterativeInference::with_sealed(ProofGraphRules, &source)
                .solve([(0, ProofRule::Edge(0, 1)), (1, ProofRule::Edge(1, 2))]);
            assert!(result.get(&0).is_none());
            assert_eq!(
                result.get(&2).unwrap().dependencies(),
                source.get(&0).unwrap().dependencies()
            );
        }
    }

    #[test]
    #[should_panic(expected = "cannot produce a sealed input")]
    fn sealed_input_rejects_a_late_producer() {
        let mut inference = IterativeInference::new(ProofGraphRules);
        inference.seed(0, ());
        let source = inference.solve([]).seal();
        IterativeInference::with_sealed(ProofGraphRules, &source)
            .solve([(0, ProofRule::Axiom(0, 0))]);
    }

    #[test]
    #[should_panic(expected = "cannot consume a sealed shared input")]
    fn sealed_shared_input_cannot_be_taken_by_a_consumer() {
        let mut inference = IterativeInference::new(ProofGraphRules);
        inference.seed(0, ());
        let source = inference.solve([]).seal();
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
        context.sealed = Some(&source);
        context.take_final(0);
    }

    #[test]
    fn completed_transfer_moves_nonclone_payload_and_forwards_its_proof() {
        struct Payload;
        let dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([input(3)]),
            ..Default::default()
        };
        let completed = CompletedTransfer::new(Payload, dependencies.clone());
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        {
            let mut context =
                IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
            let Payload = completed.consume(&mut context);
            context.propose(1, ());
        }
        assert_eq!(facts[&1].dependencies(), &dependencies);
        assert!(facts[&1].premises().next().is_none());
        assert!(proof_edges.edges.is_empty());
        assert!(proof_edges.inputs.is_empty());
    }

    fn check_transfer_retirement<const RETAIN_PREMISES: bool>() {
        let mut facts = FxHashMap::default();
        let mut proof_edges = ProofTransfers::default();
        let dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([input(0)]),
            ..Default::default()
        };
        {
            let mut context = IterativeContext::<ProofGraphRules, RETAIN_PREMISES>::new(
                &mut facts,
                &mut proof_edges,
            );
            context.assume(&dependencies);
            context.propose(0, ());
        }
        for step in 0..256 {
            let intermediate = 2 * step + 1;
            let result = intermediate + 1;
            let mut context = IterativeContext::<ProofGraphRules, RETAIN_PREMISES>::new(
                &mut facts,
                &mut proof_edges,
            );
            // This shared input is complete, but reading it live installs the
            // same edges as the ordered runtime's certificate consumers.
            context.read(0);
            context.propose(intermediate, ());
            context.read(intermediate);
            context.propose(result, ());
            // Taking a transfer freezes the proof along with its value.
            context.take_final(intermediate).unwrap();
            assert!(
                context
                    .proof_edges
                    .inputs
                    .values()
                    .all(|sources| !sources.contains(&intermediate)),
                "consuming a source retires its references in still-live targets too"
            );
            context.take_final(result).unwrap();
            assert_eq!(context.dependencies, dependencies);
            assert_eq!(facts.len(), 1);
            assert!(
                proof_edges.edges.is_empty(),
                "retired transfer edges must not accumulate across prefixes"
            );
            assert!(proof_edges.inputs.is_empty());
        }
    }

    #[test]
    fn completed_transfers_retire_live_proof_edges_with_or_without_explanations() {
        check_transfer_retirement::<true>();
        check_transfer_retirement::<false>();
    }
}
