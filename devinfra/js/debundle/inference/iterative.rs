//! Growing facts over a finite domain. Producers may contribute to the same key;
//! value consumers are revisited when values grow. Typed proof additions flow
//! through recorded premise edges without evaluating semantic rules again.
//!
//! Only the completed closure is exposed outside an evaluation. Rules must be
//! monotone in the order defined by `merge`; absence during evaluation is bottom,
//! not evidence of impossibility. Optimistic or position-sensitive conclusions
//! require a separate completion boundary before becoming positive certificates.

use super::Judgment;
use analysis_vocabulary::inputs::ProofDependencies;
use rustc_hash::{FxHashMap, FxHashSet};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::hash::Hash;
use std::num::NonZeroU32;
use work_metrics::{Batch, EventKind, KeyFamily, Operation, WorkMetrics};

mod cells;
mod context;

use cells::*;
use context::*;

/// Independent semantic and typed-evidence changes from one contribution.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FactChange {
    pub value: bool,
    pub proof: bool,
}

/// Typed access to facts during one producer evaluation. A family adapter can
/// project this interface onto a shared program store without copying facts or
/// hiding reads from the dependency worklist.
pub trait FactContext<Key, Fact> {
    fn read(&mut self, key: Key) -> Option<&Fact>;
    fn propose(&mut self, key: Key, contribution: Fact) -> FactChange;
    /// Deliver only atoms this activated edge has not delivered before. The
    /// caller owns one initially-bottom frontier per bounded source/target edge.
    /// Exact source contributions may shrink; delivered atoms never retract.
    /// An empty value delta still installs/backfills the current proof reads.
    fn propose_delta(&mut self, key: Key, contribution: Fact, delivered: &mut Fact) -> FactChange;
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
pub trait CertificateContext<Key, Fact> {
    fn require(&mut self, key: Key) -> Option<&Fact>;
    fn assume(&mut self, dependencies: &ProofDependencies);
}

pub struct GuardedContext<'a, Context> {
    inner: &'a mut Context,
    missing: bool,
}

impl<'a, Context> GuardedContext<'a, Context> {
    pub fn new(inner: &'a mut Context) -> Self {
        Self {
            inner,
            missing: false,
        }
    }

    pub fn finish<Fact>(self, contribution: Option<Fact>) -> Option<Fact> {
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

pub trait IterativeRules: Sized {
    type Key: Clone + Ord + Hash;
    type Fact;
    type Rule: Clone + Ord + Hash;
    /// Scratch retained for one rule activation. Implementations can use it to
    /// share exact intermediate join results without retaining state across
    /// events. The default hooks preserve the existing merge behavior.
    type MergeScratch: Default;

    fn new_merge_scratch(&self) -> Self::MergeScratch {
        Self::MergeScratch::default()
    }

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

    /// Optional family-specific atom subtraction for `propose_delta`. Families
    /// opting in return `None` only for an empty difference. Unsupported algebra
    /// is an error, never an empty delta. A returned delta must join with the
    /// frontier to equal its join with the complete contribution.
    fn difference(
        _key: &Self::Key,
        _contribution: &Self::Fact,
        _delivered: &Self::Fact,
    ) -> Option<Self::Fact> {
        panic!("this rule family does not define value deltas")
    }

    /// Cost of the actual delta operand submitted to the fact join, which
    /// tests read through `IterativeFacts::delta_deliveries`.
    fn delta_atoms(_delta: &Self::Fact) -> usize {
        0
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

    fn metric_key_family(_key: &Self::Key) -> KeyFamily {
        KeyFamily::Other
    }

    fn metric_event_kind(&self, _rule: &Self::Rule) -> EventKind {
        EventKind::Other
    }

    /// Observe the same join decisions without repeating subset queries.
    fn merge_observed(
        key: &Self::Key,
        current: &mut Self::Fact,
        contribution: Self::Fact,
        _metrics: &mut Batch,
    ) -> bool {
        Self::merge(key, current, contribution)
    }

    fn merge_with_scratch(
        key: &Self::Key,
        current: &mut Self::Fact,
        contribution: Self::Fact,
        _scratch: &mut Self::MergeScratch,
    ) -> bool {
        Self::merge(key, current, contribution)
    }

    fn merge_observed_with_scratch(
        key: &Self::Key,
        current: &mut Self::Fact,
        contribution: Self::Fact,
        metrics: &mut Batch,
        _scratch: &mut Self::MergeScratch,
    ) -> bool {
        Self::merge_observed(key, current, contribution, metrics)
    }
}

pub struct IterativeInference<'input, R: IterativeRules> {
    rules: R,
    sealed: Option<&'input SealedFacts<R::Key, R::Fact>>,
    facts: InferenceCells<R::Key, Judgment<R::Key, R::Fact>>,
    metrics: Option<Batch>,
}

#[cfg(test)]
#[derive(Clone, Debug, Default)]
struct InferenceWork {
    reads: usize,
    proposals: usize,
    value_changes: usize,
    registration_attempts: usize,
    new_edges: usize,
    wake_attempts: usize,
    schedules: usize,
    live_facts: usize,
    peak_facts: usize,
    reverse_capacity: usize,
}

impl<'input, R: IterativeRules> IterativeInference<'input, R> {
    pub fn new(rules: R) -> Self {
        Self {
            rules,
            sealed: None,
            facts: InferenceCells::default(),
            metrics: None,
        }
    }

    /// Borrow completed positive evidence without making it writable. Missing
    /// keys remain growing reads in this solve; sealing certifies no absence.
    pub fn with_sealed(rules: R, sealed: &'input SealedFacts<R::Key, R::Fact>) -> Self {
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
            facts: InferenceCells::default(),
            metrics: None,
        }
    }

    pub fn observed(mut self, metrics: &WorkMetrics) -> Self {
        self.metrics = metrics.batch();
        self
    }

    pub fn seed(&mut self, key: R::Key, value: R::Fact) {
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<R>::new_with_scratch(
            &mut self.facts,
            &mut proof_edges,
            self.rules.new_merge_scratch(),
        );
        context.sealed = self.sealed;
        context.propose(key, value);
    }

    /// All producers must be supplied. Source-order rounds preserve clients
    /// whose conservative approximation depends on the initial source sweep.
    /// A rule reading its own output runs again in the next round when needed.
    pub fn solve(
        self,
        producers: impl IntoIterator<Item = R::Rule>,
    ) -> IterativeFacts<R::Key, R::Fact> {
        self.solve_with_premises::<true>(producers)
    }

    /// Complete the same closure when the caller consumes only values. Keep
    /// read watchers and typed dependencies, but do not build a direct-premise
    /// graph that would immediately be discarded by `into_values`.
    pub fn solve_values(
        self,
        producers: impl IntoIterator<Item = R::Rule>,
    ) -> BTreeMap<R::Key, R::Fact> {
        self.solve_with_premises::<false>(producers).into_values()
    }

    /// Keep values and typed dependencies without explanatory premises when
    /// downstream consumers still need the completed judgments.
    pub fn solve_values_resumable(
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
            mut metrics,
        } = self;
        // Only `pending` orders evaluation; the other worklist maps are
        // looked up per key and rule, never iterated in an order that matters.
        let mut watchers = ValueWatchers::new();
        for producer in producers {
            watchers.register(producer);
        }
        // Registration IDs only index bookkeeping; the BTreeSet below alone
        // determines execution order, so cell insertion order is immaterial.
        for (key, fact) in facts.iter() {
            for producer in rules.producers_for(key, fact.value()) {
                watchers.register(producer);
            }
        }
        let mut pending = BTreeSet::new();
        for state in &mut watchers.rules {
            state.scheduled = Some(0);
            pending.insert((0usize, state.rule.clone()));
        }
        if let Some(metrics) = &mut metrics {
            metrics.add(Operation::Schedule, pending.len() as u64);
            metrics.pending(pending.len());
            metrics.flush();
        }
        // Live proof transfers are separate from value watchers. Each new edge
        // backfills existing atoms; later additions cross it without a rule run.
        let mut proof_edges = ProofTransfers::default();
        let mut evaluations = 0usize;
        let mut delta_deliveries = 0usize;
        #[cfg(test)]
        let mut work = InferenceWork {
            schedules: pending.len(),
            peak_facts: facts.len(),
            ..Default::default()
        };
        while let Some((round, rule)) = pending.pop_first() {
            let rule_id = watchers.ids[&rule];
            watchers.rules[rule_id].scheduled = None;
            if let Some(metrics) = &mut metrics {
                metrics.pending(pending.len());
                metrics.execution(
                    rules.metric_event_kind(&rule),
                    watchers.rules[rule_id].evaluated,
                );
                watchers.rules[rule_id].evaluated = true;
            }
            // Freeze the semantic key set before the mutable context borrows
            // the arena, preserving exact `evaluate_changed` ordering.
            let changed = watchers.rules[rule_id].changed.take().map(|ids| {
                ids.into_iter()
                    .map(|id| facts.key(id).clone())
                    .collect::<BTreeSet<_>>()
            });
            let mut context = IterativeContext::<R, RETAIN_PREMISES>::new_with_scratch(
                &mut facts,
                &mut proof_edges,
                rules.new_merge_scratch(),
            );
            context.sealed = sealed;
            context.metrics = metrics.as_mut();
            match changed {
                Some(changed) => rules.evaluate_changed(&rule, &changed, &mut context),
                None => rules.evaluate(&rule, &mut context),
            }
            evaluations += 1;
            delta_deliveries += context.delta_deliveries;
            #[cfg(test)]
            {
                work.reads += context.read_attempts;
                work.proposals += context.proposals;
                work.value_changes += context.value_changes;
                work.peak_facts = work.peak_facts.max(context.peak_facts);
                work.registration_attempts += context.reads.len();
            }
            // Reader-local ID membership avoids scanning reverse readers for
            // duplicates and appends each adjacency at most once.
            let IterativeContext { reads, changed, .. } = context;
            let known = &mut watchers.rules[rule_id].keys;
            for id in reads {
                let added = known.insert(id);
                if let Some(metrics) = &mut metrics {
                    metrics.subscription(added);
                }
                if added {
                    facts.cell_mut(id).readers.push(rule_id);
                    #[cfg(test)]
                    {
                        work.new_edges += 1;
                    }
                }
            }
            let mut changed: Vec<_> = changed.into_iter().collect();
            changed.sort_unstable_by(|left, right| facts.key(*left).cmp(facts.key(*right)));
            for id in changed {
                let cell = facts.cell(id);
                let key = facts.key(id);
                let fact = facts.fact(id).expect("changed fact exists");
                for producer in rules.producers_for(key, fact.value()) {
                    let (producer_id, added) = watchers.register(producer);
                    if added
                        && watchers.rules[producer_id].schedule_after(round, &rule, &mut pending)
                    {
                        if let Some(metrics) = &mut metrics {
                            metrics.add(Operation::Schedule, 1);
                        }
                        #[cfg(test)]
                        {
                            work.schedules += 1;
                        }
                    }
                }
                for &dependent_id in &cell.readers {
                    if let Some(metrics) = &mut metrics {
                        metrics.add(Operation::WakeAttempt, 1);
                    }
                    #[cfg(test)]
                    {
                        work.wake_attempts += 1;
                    }
                    let state = &mut watchers.rules[dependent_id];
                    if R::EVALUATES_CHANGES {
                        state
                            .changed
                            .get_or_insert_with(Default::default)
                            .insert(id);
                    }
                    if !state.schedule_after(round, &rule, &mut pending) {
                        if let Some(metrics) = &mut metrics {
                            metrics.add(Operation::CoalescedWake, 1);
                        }
                        continue;
                    }
                    if let Some(metrics) = &mut metrics {
                        metrics.add(Operation::Schedule, 1);
                    }
                    #[cfg(test)]
                    {
                        work.schedules += 1;
                    }
                }
            }
        }
        if let Some(metrics) = &mut metrics {
            metrics.pending(0);
        }
        #[cfg(test)]
        {
            work.live_facts = facts.len();
            work.reverse_capacity = facts.cells.iter().map(|cell| cell.readers.capacity()).sum();
        }
        #[cfg(test)]
        let proof_deliveries = proof_edges.atom_deliveries;
        drop(watchers);
        drop(proof_edges);
        drop(pending);
        let facts = facts.into_facts();
        IterativeFacts {
            facts,
            #[cfg(test)]
            work,
            evaluations,
            #[cfg(test)]
            proof_deliveries,
            delta_deliveries,
        }
    }
}

/// Shared read capability for one completed positive closure. Construction
/// consumes the solved store; no API can resume or mutate these judgments.
#[derive(Debug)]
pub struct SealedFacts<Key, Fact>(IterativeFacts<Key, Fact>);

impl<Key: Ord + Hash, Fact> SealedFacts<Key, Fact> {
    pub fn get(&self, key: &Key) -> Option<&Judgment<Key, Fact>> {
        self.0.get(key)
    }
}

/// A completed ordered result with exactly one consuming reader. There is no
/// key to republish or index to retain after the payload moves to its consumer.
#[must_use]
pub struct CompletedTransfer<Fact> {
    value: Fact,
    dependencies: ProofDependencies,
}

impl<Fact> CompletedTransfer<Fact> {
    pub fn new(value: Fact, dependencies: ProofDependencies) -> Self {
        Self {
            value,
            dependencies,
        }
    }

    pub fn consume<Key, StoreFact>(
        self,
        context: &mut (impl FactContext<Key, StoreFact> + ?Sized),
    ) -> Fact {
        context.assume(&self.dependencies);
        self.value
    }
}

/// Immutable results after every supplied producer and its dependents settle.
#[derive(Debug, Clone)]
pub struct IterativeFacts<Key, Fact> {
    facts: FxHashMap<Key, Judgment<Key, Fact>>,
    #[cfg(test)]
    work: InferenceWork,
    evaluations: usize,
    #[cfg(test)]
    proof_deliveries: usize,
    delta_deliveries: usize,
}

impl<Key: Ord + Hash, Fact> IterativeFacts<Key, Fact> {
    pub fn seal(self) -> SealedFacts<Key, Fact> {
        SealedFacts(self)
    }

    pub fn get(&self, key: &Key) -> Option<&Judgment<Key, Fact>> {
        self.facts.get(key)
    }

    /// In no particular order.
    pub fn keys(&self) -> impl Iterator<Item = &Key> {
        self.facts.keys()
    }

    pub fn into_values(self) -> BTreeMap<Key, Fact> {
        self.facts
            .into_iter()
            .map(|(key, judgment)| (key, judgment.value))
            .collect()
    }

    pub fn evaluations(&self) -> usize {
        self.evaluations
    }

    pub fn delta_deliveries(&self) -> usize {
        self.delta_deliveries
    }
}

#[cfg(test)]
mod tests;
