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
use std::num::NonZeroU32;
use work_metrics::{Batch, EventKind, KeyFamily, Operation, WorkMetrics};

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

    /// Test-only cost of the actual delta operand submitted to the fact join.
    #[cfg(test)]
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

pub(crate) struct IterativeInference<'input, R: IterativeRules> {
    rules: R,
    sealed: Option<&'input SealedFacts<R::Key, R::Fact>>,
    facts: InferenceCells<R::Key, Judgment<R::Key, R::Fact>>,
    metrics: Option<Batch>,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct CellId(u32);

impl CellId {
    fn index(self) -> usize {
        usize::try_from(self.0).expect("inference cell ID does not fit usize")
    }
}

#[derive(Clone, Copy)]
struct FactId(NonZeroU32);

impl FactId {
    fn new(index: usize) -> Self {
        let one_based = u32::try_from(index)
            .expect("inference fact ID overflow")
            .checked_add(1)
            .expect("inference fact ID overflow");
        Self(NonZeroU32::new(one_based).expect("one-based inference fact ID is nonzero"))
    }

    fn index(self) -> usize {
        usize::try_from(self.0.get() - 1).expect("inference fact ID does not fit usize")
    }
}

struct InferenceCell {
    fact: Option<FactId>,
    readers: Vec<usize>,
}

/// Stable IDs let rule-local known/changed sets share one key-to-cell index.
/// The lookup map and cell-key vector both retain a key because lookups need
/// hashing while changed IDs need their original key for deterministic order.
struct InferenceCells<Key, Fact> {
    ids: FxHashMap<Key, CellId>,
    keys: Vec<Option<Key>>,
    cells: Vec<InferenceCell>,
    free_cells: Vec<CellId>,
    facts: Vec<Option<Fact>>,
    free_facts: Vec<FactId>,
    fact_count: usize,
    created_cells: usize,
    retired_cells: usize,
    peak_live_cells: usize,
    fact_removals: usize,
}

impl<Key, Fact> Default for InferenceCells<Key, Fact> {
    fn default() -> Self {
        Self {
            ids: FxHashMap::default(),
            keys: Vec::new(),
            cells: Vec::new(),
            free_cells: Vec::new(),
            facts: Vec::new(),
            free_facts: Vec::new(),
            fact_count: 0,
            created_cells: 0,
            retired_cells: 0,
            peak_live_cells: 0,
            fact_removals: 0,
        }
    }
}

impl<Key: Clone + Eq + Hash, Fact> InferenceCells<Key, Fact> {
    fn intern(&mut self, key: Key) -> CellId {
        if let Some(&id) = self.ids.get(&key) {
            return id;
        }
        let id = if let Some(id) = self.free_cells.pop() {
            let index = id.index();
            debug_assert!(self.keys[index].is_none());
            debug_assert!(self.cells[index].fact.is_none());
            debug_assert!(self.cells[index].readers.is_empty());
            self.keys[index] = Some(key.clone());
            id
        } else {
            let id = CellId(u32::try_from(self.cells.len()).expect("inference cell ID overflow"));
            self.keys.push(Some(key.clone()));
            self.cells.push(InferenceCell {
                fact: None,
                readers: Vec::new(),
            });
            id
        };
        self.ids.insert(key, id);
        self.created_cells += 1;
        self.peak_live_cells = self.peak_live_cells.max(self.ids.len());
        id
    }

    fn id(&self, key: &Key) -> Option<CellId> {
        self.ids.get(key).copied()
    }

    fn key(&self, id: CellId) -> &Key {
        self.keys[id.index()]
            .as_ref()
            .expect("live inference cell has a key")
    }

    fn cell(&self, id: CellId) -> &InferenceCell {
        &self.cells[id.index()]
    }

    fn cell_mut(&mut self, id: CellId) -> &mut InferenceCell {
        &mut self.cells[id.index()]
    }

    fn fact(&self, id: CellId) -> Option<&Fact> {
        self.cell(id)
            .fact
            .and_then(|fact_id| self.facts[fact_id.index()].as_ref())
    }

    fn fact_mut(&mut self, id: CellId) -> Option<&mut Fact> {
        let fact_id = self.cell(id).fact?;
        self.facts[fact_id.index()].as_mut()
    }

    fn get(&self, key: &Key) -> Option<&Fact> {
        self.id(key).and_then(|id| self.fact(id))
    }

    fn get_mut(&mut self, key: &Key) -> Option<&mut Fact> {
        let id = self.id(key)?;
        self.fact_mut(id)
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.fact_count
    }

    #[cfg(test)]
    fn values(&self) -> impl Iterator<Item = &Fact> {
        self.facts.iter().filter_map(Option::as_ref)
    }

    fn iter(&self) -> impl Iterator<Item = (&Key, &Fact)> {
        self.keys.iter().zip(&self.cells).filter_map(|(key, cell)| {
            let key = key.as_ref()?;
            let fact_id = cell.fact?;
            Some((key, self.facts[fact_id.index()].as_ref()?))
        })
    }

    fn insert(&mut self, id: CellId, fact: Fact) {
        assert!(
            self.cell(id).fact.is_none(),
            "inference fact already exists"
        );
        let fact_id = if let Some(fact_id) = self.free_facts.pop() {
            debug_assert!(self.facts[fact_id.index()].is_none());
            self.facts[fact_id.index()] = Some(fact);
            fact_id
        } else {
            let fact_id = FactId::new(self.facts.len());
            self.facts.push(Some(fact));
            fact_id
        };
        self.cell_mut(id).fact = Some(fact_id);
        self.fact_count += 1;
    }

    fn remove(&mut self, id: CellId) -> Option<Fact> {
        let fact_id = self.cell_mut(id).fact.take()?;
        let fact = self.facts[fact_id.index()]
            .take()
            .expect("present inference fact slot");
        self.free_facts.push(fact_id);
        self.fact_count -= 1;
        self.fact_removals += 1;
        Some(fact)
    }

    /// Retire an unwatched consumed key after its activation-local IDs are gone.
    /// A reverse reader is the durable proof that some watcher still owns its ID.
    fn retire_unwatched(&mut self, id: CellId) {
        let cell = self.cell(id);
        if cell.fact.is_some() || !cell.readers.is_empty() {
            return;
        }
        let key = self.keys[id.index()]
            .take()
            .expect("live inference cell has a key");
        assert_eq!(self.ids.remove(&key), Some(id));
        self.cells[id.index()] = InferenceCell {
            fact: None,
            readers: Vec::new(),
        };
        self.free_cells.push(id);
        self.retired_cells += 1;
    }

    fn into_facts(self) -> FxHashMap<Key, Fact> {
        let Self {
            ids,
            keys,
            cells,
            facts,
            ..
        } = self;
        drop(ids);
        let mut facts = facts;
        let mut completed = FxHashMap::default();
        for (key, cell) in keys.into_iter().zip(cells) {
            let Some(fact_id) = cell.fact else {
                continue;
            };
            completed.insert(
                key.expect("present inference fact has a key"),
                facts[fact_id.index()]
                    .take()
                    .expect("present inference fact slot"),
            );
        }
        completed
    }
}

impl<Key: Clone + Eq + Hash, Fact> std::ops::Index<&Key> for InferenceCells<Key, Fact> {
    type Output = Fact;

    fn index(&self, key: &Key) -> &Self::Output {
        self.get(key).expect("inference fact missing")
    }
}

struct WatchedRule<Rule> {
    rule: Rule,
    evaluated: bool,
    keys: FxHashSet<CellId>,
    scheduled: Option<usize>,
    changed: Option<FxHashSet<CellId>>,
}

/// IDs address bookkeeping only. Evaluation order always compares `Rule`.
struct ValueWatchers<Rule> {
    ids: FxHashMap<Rule, usize>,
    rules: Vec<WatchedRule<Rule>>,
}

impl<Rule: Clone + Eq + Hash> ValueWatchers<Rule> {
    fn new() -> Self {
        Self {
            ids: FxHashMap::default(),
            rules: Vec::new(),
        }
    }

    fn register(&mut self, rule: Rule) -> (usize, bool) {
        use std::collections::hash_map::Entry;
        match self.ids.entry(rule) {
            Entry::Occupied(entry) => (*entry.get(), false),
            Entry::Vacant(entry) => {
                let id = self.rules.len();
                self.rules.push(WatchedRule {
                    rule: entry.key().clone(),
                    evaluated: false,
                    keys: FxHashSet::default(),
                    scheduled: None,
                    changed: None,
                });
                entry.insert(id);
                (id, true)
            }
        }
    }
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
    pub(crate) fn new(rules: R) -> Self {
        Self {
            rules,
            sealed: None,
            facts: InferenceCells::default(),
            metrics: None,
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
            facts: InferenceCells::default(),
            metrics: None,
        }
    }

    pub(crate) fn observed(mut self, metrics: &WorkMetrics) -> Self {
        self.metrics = metrics.batch();
        self
    }

    pub(crate) fn seed(&mut self, key: R::Key, value: R::Fact) {
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
        #[cfg(test)]
        let mut evaluations = 0usize;
        #[cfg(test)]
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
            #[cfg(test)]
            {
                evaluations += 1;
                delta_deliveries += context.delta_deliveries;
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
                    let (producer_id, added) = watchers.register(producer.clone());
                    if added {
                        let next_round = if producer > rule {
                            round
                        } else {
                            round.checked_add(1).expect("inference round overflow")
                        };
                        watchers.rules[producer_id].scheduled = Some(next_round);
                        if let Some(metrics) = &mut metrics {
                            metrics.add(Operation::Schedule, 1);
                        }
                        #[cfg(test)]
                        {
                            work.schedules += 1;
                        }
                        pending.insert((next_round, producer));
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
                    let dependent = &state.rule;
                    let next_round = if dependent > &rule {
                        round
                    } else {
                        round.checked_add(1).expect("inference round overflow")
                    };
                    if state
                        .scheduled
                        .is_some_and(|scheduled| scheduled <= next_round)
                    {
                        if let Some(metrics) = &mut metrics {
                            metrics.add(Operation::CoalescedWake, 1);
                        }
                        continue;
                    }
                    if let Some(previous) = state.scheduled.replace(next_round) {
                        pending.remove(&(previous, dependent.clone()));
                    }
                    pending.insert((next_round, dependent.clone()));
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
        if std::env::var_os("DEBUNDLE_CELL_SUMMARY").is_some() {
            let reader_capacity = facts
                .cells
                .iter()
                .map(|cell| cell.readers.capacity())
                .sum::<usize>();
            eprintln!(
                "inference-cells key={} created={} retired={} retained={} peak_retained={} live_facts={} absent_retained={} fact_removals={} cell_slots={} cell_capacity={} key_capacity={} id_capacity={} free_cells={} fact_slots={} fact_capacity={} free_facts={} reader_capacity={}",
                std::any::type_name::<R::Key>(),
                facts.created_cells,
                facts.retired_cells,
                facts.ids.len(),
                facts.peak_live_cells,
                facts.fact_count,
                facts.ids.len().saturating_sub(facts.fact_count),
                facts.fact_removals,
                facts.cells.len(),
                facts.cells.capacity(),
                facts.keys.capacity(),
                facts.ids.capacity(),
                facts.free_cells.len(),
                facts.facts.len(),
                facts.facts.capacity(),
                facts.free_facts.len(),
                reader_capacity,
            );
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
            #[cfg(test)]
            evaluations,
            #[cfg(test)]
            proof_deliveries,
            #[cfg(test)]
            delta_deliveries,
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
    facts: &'a mut InferenceCells<R::Key, Judgment<R::Key, R::Fact>>,
    sealed: Option<&'a SealedFacts<R::Key, R::Fact>>,
    sealed_reads: Vec<R::Key>,
    proof_edges: &'a mut ProofTransfers<R::Key>,
    /// In read order, with repeats: appending is cheaper than a set insert
    /// for every read, and the solver skips keys the rule already watches.
    reads: Vec<CellId>,
    changed: FxHashSet<CellId>,
    dependencies: ProofDependencies,
    merge_scratch: R::MergeScratch,
    metrics: Option<&'a mut Batch>,
    #[cfg(test)]
    delta_deliveries: usize,
    #[cfg(test)]
    read_attempts: usize,
    #[cfg(test)]
    proposals: usize,
    #[cfg(test)]
    value_changes: usize,
    #[cfg(test)]
    peak_facts: usize,
}

impl<'a, R: IterativeRules, const RETAIN_PREMISES: bool> IterativeContext<'a, R, RETAIN_PREMISES> {
    #[cfg(test)]
    fn new(
        facts: &'a mut InferenceCells<R::Key, Judgment<R::Key, R::Fact>>,
        proof_edges: &'a mut ProofTransfers<R::Key>,
    ) -> Self {
        Self::new_with_scratch(facts, proof_edges, R::MergeScratch::default())
    }

    fn new_with_scratch(
        facts: &'a mut InferenceCells<R::Key, Judgment<R::Key, R::Fact>>,
        proof_edges: &'a mut ProofTransfers<R::Key>,
        merge_scratch: R::MergeScratch,
    ) -> Self {
        Self {
            facts,
            sealed: None,
            sealed_reads: Vec::new(),
            proof_edges,
            reads: Vec::new(),
            changed: FxHashSet::default(),
            dependencies: ProofDependencies::default(),
            merge_scratch,
            metrics: None,
            #[cfg(test)]
            delta_deliveries: 0,
            #[cfg(test)]
            read_attempts: 0,
            #[cfg(test)]
            proposals: 0,
            #[cfg(test)]
            value_changes: 0,
            #[cfg(test)]
            peak_facts: 0,
        }
    }

    fn extend_proof(
        &mut self,
        id: CellId,
        key: &R::Key,
        dependencies: &ProofDependencies,
    ) -> ProofDependencies {
        debug_assert!(self.facts.key(id) == key);
        let fact = self.facts.fact_mut(id).expect("proposed fact exists");
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

    fn attach_proof(&mut self, id: CellId, key: &R::Key) -> bool {
        if RETAIN_PREMISES && (!self.reads.is_empty() || !self.sealed_reads.is_empty()) {
            let InferenceCells {
                keys, cells, facts, ..
            } = &mut *self.facts;
            let fact_id = cells[id.index()].fact.expect("proposed fact exists");
            let premises = facts[fact_id.index()]
                .as_mut()
                .expect("proposed fact slot")
                .premises
                .get_or_insert_with(Default::default);
            for &read in &self.reads {
                premises.insert(
                    keys[read.index()]
                        .as_ref()
                        .expect("read inference cell has a key")
                        .clone(),
                );
            }
            premises.extend(self.sealed_reads.iter().cloned());
        }
        if R::SOURCE_ONLY {
            return false;
        }
        let mut dependencies = (!self.dependencies.is_empty()).then(|| self.dependencies.clone());
        for &source_id in &self.reads {
            let source = self.facts.key(source_id);
            if source_id != id
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
                    .fact(source_id)
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
            return false;
        };
        let added = self.extend_proof(id, key, &dependencies);
        let changed = !added.is_empty();
        if changed {
            self.propagate_proof(key.clone(), added);
        }
        changed
    }
}

impl<R: IterativeRules, const RETAIN_PREMISES: bool> FactContext<R::Key, R::Fact>
    for IterativeContext<'_, R, RETAIN_PREMISES>
{
    fn read(&mut self, key: R::Key) -> Option<&R::Fact> {
        #[cfg(test)]
        {
            self.read_attempts += 1;
        }
        if let Some(fact) = self.sealed.and_then(|sealed| sealed.get(&key)) {
            if let Some(metrics) = &mut self.metrics {
                metrics.read(R::metric_key_family(&key), false);
            }
            self.sealed_reads.push(key);
            self.dependencies.extend(fact.dependencies());
            return Some(fact.value());
        }
        let id = self.facts.intern(key.clone());
        self.reads.push(id);
        let fact = self.facts.fact(id);
        if let Some(metrics) = &mut self.metrics {
            metrics.read(R::metric_key_family(&key), fact.is_none());
        }
        let fact = fact?;
        self.dependencies.extend(fact.dependencies());
        Some(fact.value())
    }

    /// Writes are immediately visible to subsequent reads in this producer.
    fn propose(&mut self, key: R::Key, contribution: R::Fact) -> FactChange {
        assert!(
            self.sealed.is_none_or(|sealed| sealed.get(&key).is_none()),
            "cannot produce a sealed input"
        );
        let id = self.facts.intern(key.clone());
        let value_changed = if self.facts.fact(id).is_none() {
            if let Some(metrics) = &mut self.metrics {
                metrics.new_fact(R::metric_key_family(&key));
            }
            self.facts.insert(
                id,
                Judgment {
                    value: contribution,
                    dependencies: None,
                    premises: None,
                },
            );
            true
        } else {
            let fact = self.facts.fact_mut(id).expect("present inference fact");
            if let Some(metrics) = &mut self.metrics {
                R::merge_observed_with_scratch(
                    &key,
                    &mut fact.value,
                    contribution,
                    metrics,
                    &mut self.merge_scratch,
                )
            } else {
                R::merge_with_scratch(&key, &mut fact.value, contribution, &mut self.merge_scratch)
            }
        };
        let mut change = FactChange {
            value: value_changed,
            proof: false,
        };
        if let Some(metrics) = &mut self.metrics {
            metrics.add(
                if change.value {
                    Operation::ChangedProposal
                } else {
                    Operation::UnchangedProposal
                },
                1,
            );
        }
        if change.value {
            self.changed.insert(id);
        }
        #[cfg(test)]
        {
            self.proposals += 1;
            self.value_changes += usize::from(change.value);
            self.peak_facts = self.peak_facts.max(self.facts.len());
        }
        change.proof = self.attach_proof(id, &key);
        change
    }

    fn propose_delta(
        &mut self,
        key: R::Key,
        contribution: R::Fact,
        delivered: &mut R::Fact,
    ) -> FactChange {
        assert!(
            self.sealed.is_none_or(|sealed| sealed.get(&key).is_none()),
            "cannot produce a sealed input"
        );
        let delta = R::difference(&key, &contribution, delivered);
        if let Some(metrics) = &mut self.metrics {
            R::merge_observed_with_scratch(
                &key,
                delivered,
                contribution,
                metrics,
                &mut self.merge_scratch,
            );
        } else {
            R::merge_with_scratch(&key, delivered, contribution, &mut self.merge_scratch);
        }
        if let Some(delta) = delta {
            #[cfg(test)]
            {
                self.delta_deliveries += R::delta_atoms(&delta);
            }
            self.propose(key, delta)
        } else if let Some(id) = self
            .facts
            .id(&key)
            .filter(|&id| self.facts.fact(id).is_some())
        {
            if let Some(metrics) = &mut self.metrics {
                metrics.add(Operation::UnchangedProposal, 1);
            }
            #[cfg(test)]
            {
                self.proposals += 1;
            }
            FactChange {
                value: false,
                proof: self.attach_proof(id, &key),
            }
        } else {
            if let Some(metrics) = &mut self.metrics {
                metrics.add(Operation::UnchangedProposal, 1);
            }
            #[cfg(test)]
            {
                self.proposals += 1;
            }
            // No value has crossed this edge and there is no target judgment
            // to certify. A later nonempty delivery installs its proof edges.
            FactChange::default()
        }
    }

    fn assume(&mut self, dependencies: &ProofDependencies) {
        assert!(
            !R::SOURCE_ONLY || dependencies.is_empty(),
            "source-only inference cannot introduce typed proof dependencies"
        );
        self.dependencies.extend(dependencies);
    }

    fn read_final(&mut self, key: R::Key) -> Option<&R::Fact> {
        #[cfg(test)]
        {
            self.read_attempts += 1;
        }
        let fact = self.facts.get(&key)?;
        self.dependencies.extend(fact.dependencies());
        Some(fact.value())
    }

    fn take_final(&mut self, key: R::Key) -> Option<R::Fact> {
        #[cfg(test)]
        {
            self.read_attempts += 1;
        }
        assert!(
            self.sealed.is_none_or(|sealed| sealed.get(&key).is_none()),
            "cannot consume a sealed shared input"
        );
        let id = self.facts.id(&key)?;
        let fact = self.facts.remove(id)?;
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
        self.reads.retain(|&read| read != id);
        self.changed.remove(&id);
        // `readers` is the durable reverse of every rule-local `keys` entry
        // and every pending changed-ID entry. The current activation's two
        // ID collections were just cleared above, so an empty reader list
        // means the consumed ID is no longer held anywhere.
        self.facts.retire_unwatched(id);
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
    work: InferenceWork,
    #[cfg(test)]
    evaluations: usize,
    #[cfg(test)]
    proof_deliveries: usize,
    #[cfg(test)]
    delta_deliveries: usize,
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

    #[cfg(test)]
    pub(crate) fn delta_deliveries(&self) -> usize {
        self.delta_deliveries
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ValueInputRef;

    struct LateTransferRules;

    impl IterativeRules for LateTransferRules {
        type Key = usize;
        type Fact = BTreeSet<usize>;
        type Rule = usize;
        type MergeScratch = ();
        const SOURCE_ONLY: bool = true;

        fn evaluate(&self, rule: &usize, context: &mut impl FactContext<usize, Self::Fact>) {
            match rule {
                0 => {
                    context.read(2);
                    if context.read(0).is_some()
                        && let Some(value) = context.take_final(0)
                    {
                        context.propose(1, value);
                    }
                }
                1 => {
                    context.propose(0, BTreeSet::from([7]));
                }
                _ => unreachable!(),
            }
        }

        fn merge(key: &usize, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
            Rules::merge(key, current, contribution)
        }
    }

    #[test]
    fn missing_read_can_be_filled_and_consumed_without_leaking_placeholders() {
        let facts = IterativeInference::new(LateTransferRules).solve([0, 1]);

        assert_eq!(
            facts.keys().copied().collect::<BTreeSet<_>>(),
            BTreeSet::from([1])
        );
        assert_eq!(facts.get(&1).unwrap().value(), &BTreeSet::from([7]));
    }

    #[test]
    fn unwatched_consumed_cells_reuse_ids_after_activation_references_clear() {
        let mut facts = InferenceCells::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);

        assert!(context.read(10).is_none());
        let retired = context.facts.id(&10).unwrap();
        context.propose(10, BTreeSet::from([10]));
        assert_eq!(context.take_final(10), Some(BTreeSet::from([10])));
        assert!(context.facts.id(&10).is_none());
        assert!(!context.reads.contains(&retired));
        assert!(!context.changed.contains(&retired));

        let reused = context.facts.intern(11);
        assert_eq!(reused, retired);
        context.propose(11, BTreeSet::from([11]));
        assert_eq!(context.facts.key(reused), &11);
        assert_eq!(
            context.facts.get(&11).unwrap().value(),
            &BTreeSet::from([11])
        );

        let watched = context.facts.intern(20);
        context.facts.cell_mut(watched).readers.push(0);
        context.propose(20, BTreeSet::from([20]));
        assert_eq!(context.take_final(20), Some(BTreeSet::from([20])));
        assert_eq!(context.facts.id(&20), Some(watched));
        assert_ne!(context.facts.intern(21), watched);
    }

    struct RepeatedReads;

    impl IterativeRules for RepeatedReads {
        type Key = usize;
        type Fact = BTreeSet<usize>;
        type Rule = usize;
        type MergeScratch = ();
        const SOURCE_ONLY: bool = true;

        fn evaluate(&self, rule: &usize, context: &mut impl FactContext<usize, Self::Fact>) {
            match rule {
                0 => {
                    let mut values = BTreeSet::new();
                    for _ in 0..8 {
                        values.extend(context.read(10).into_iter().flatten().copied());
                    }
                    values.extend(context.read(11).into_iter().flatten().copied());
                    context.propose(20, values);
                }
                1 => {
                    context.propose(10, BTreeSet::from([1]));
                    context.propose(10, BTreeSet::from([1]));
                    context.propose(11, BTreeSet::from([2]));
                }
                2 => {
                    let values = context.read(20).cloned().unwrap();
                    context.propose(21, values);
                }
                _ => unreachable!(),
            }
        }

        fn merge(key: &usize, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
            Rules::merge(key, current, contribution)
        }
    }

    #[test]
    fn repeated_and_absent_reads_register_one_edge_and_coalesce_wakeups() {
        // Registration IDs deliberately disagree with source order. Rule 2
        // must still observe rule 0's immediately visible initial proposal.
        let facts = IterativeInference::new(RepeatedReads).solve([2, 1, 0]);
        assert_eq!(facts.get(&21).unwrap().value(), &BTreeSet::from([1, 2]));
        let work = &facts.work;
        assert_eq!(
            (work.reads, work.registration_attempts, work.new_edges),
            (20, 20, 3)
        );
        assert_eq!((work.proposals, work.value_changes), (7, 6));
        assert_eq!(
            (work.wake_attempts, work.schedules, facts.evaluations),
            (3, 5, 5)
        );
        assert_eq!((work.live_facts, work.peak_facts), (4, 4));
        // Count allocated adjacency slots as well as logical edges. Each
        // singleton adjacency may reserve the vector's minimum allocation.
        assert!((work.new_edges..=4 * work.new_edges).contains(&work.reverse_capacity));
    }

    #[test]
    fn production_metrics_count_absence_repeated_reads_and_coalesced_wakes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("work.jsonl");
        let session = telemetry::Session::start(Some(&path)).unwrap();
        let metrics = WorkMetrics::new(session.meter());
        let facts = IterativeInference::new(RepeatedReads)
            .observed(&metrics)
            .solve([2, 1, 0]);
        assert_eq!(facts.get(&21).unwrap().value(), &BTreeSet::from([1, 2]));
        session.finish().unwrap();
        let text = std::fs::read_to_string(path).unwrap();
        let record: serde_json::Value = serde_json::from_str(text.lines().last().unwrap()).unwrap();
        let metrics = record["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
            .as_array()
            .unwrap();
        let operations = metrics
            .iter()
            .find(|metric| metric["name"] == "debundle.inference.operations")
            .unwrap();
        let count = |operation: &str| -> u64 {
            operations["sum"]["dataPoints"]
                .as_array()
                .unwrap()
                .iter()
                .find(|point| {
                    point["attributes"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|attribute| {
                            attribute["key"] == "operation"
                                && attribute["value"]["stringValue"] == operation
                        })
                })
                .unwrap()["asInt"]
                .as_str()
                .unwrap()
                .parse()
                .unwrap()
        };
        for (operation, expected) in [
            ("read", 20),
            ("absent_read", 9),
            ("new_fact", 4),
            ("changed_proposal", 6),
            ("unchanged_proposal", 1),
            ("new_subscription", 3),
            ("repeated_subscription", 17),
            ("wake_attempt", 3),
            ("coalesced_wake", 1),
            ("schedule", 5),
        ] {
            assert_eq!(count(operation), expected, "{operation}");
        }
    }

    #[test]
    fn historical_reads_keep_waking_a_rule_that_reuses_cached_dependencies() {
        struct CachedReads(std::rc::Rc<std::cell::RefCell<Vec<usize>>>);

        impl IterativeRules for CachedReads {
            type Key = usize;
            type Fact = BTreeSet<usize>;
            type Rule = usize;
            type MergeScratch = ();
            const SOURCE_ONLY: bool = true;

            fn evaluate(&self, rule: &usize, context: &mut impl FactContext<usize, Self::Fact>) {
                let mut trace = self.0.borrow_mut();
                if *rule == 0 {
                    if trace.is_empty() {
                        context.read(10);
                    } else {
                        // An invocation can retain an earlier dependency
                        // without repeating its reads on every evaluation.
                        context.read(11);
                    }
                } else {
                    let mut values = context.read(10).cloned().unwrap_or_default();
                    if values.len() < 3 {
                        values.insert(values.len());
                    }
                    context.propose(10, values);
                }
                trace.push(*rule);
            }

            fn merge(key: &usize, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
                Rules::merge(key, current, contribution)
            }
        }

        let trace = std::rc::Rc::default();
        let facts = IterativeInference::new(CachedReads(std::rc::Rc::clone(&trace))).solve([1, 0]);
        assert_eq!(*trace.borrow(), [0, 1, 0, 1, 0, 1, 0, 1]);
        assert_eq!(facts.work.new_edges, 3);
        assert_eq!(facts.get(&10).unwrap().value(), &BTreeSet::from([0, 1, 2]));
    }

    #[test]
    fn premises_are_allocated_only_when_read_and_keep_growing_without_value_changes() {
        let mut facts = InferenceCells::default();
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

        type MergeScratch = ();
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
        let mut facts = InferenceCells::default();
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
        assert!(facts.get(&TransferKey::Input).is_some());
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

        type MergeScratch = ();
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
        type MergeScratch = ();
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
        fn difference(
            _: &usize,
            contribution: &Self::Fact,
            delivered: &Self::Fact,
        ) -> Option<Self::Fact> {
            let delta: BTreeSet<_> = contribution.difference(delivered).copied().collect();
            (!delta.is_empty()).then_some(delta)
        }

        fn delta_atoms(delta: &Self::Fact) -> usize {
            delta.len()
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
        type MergeScratch = ();
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

    #[derive(Clone)]
    struct CountedKey {
        id: usize,
        comparisons: std::rc::Rc<std::cell::Cell<usize>>,
    }

    impl PartialEq for CountedKey {
        fn eq(&self, other: &Self) -> bool {
            self.id == other.id
        }
    }

    impl Eq for CountedKey {}

    impl std::hash::Hash for CountedKey {
        fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
            std::hash::Hash::hash(&self.id, state);
        }
    }

    impl PartialOrd for CountedKey {
        fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
            Some(self.cmp(other))
        }
    }

    impl Ord for CountedKey {
        fn cmp(&self, other: &Self) -> std::cmp::Ordering {
            self.comparisons
                .set(self.comparisons.get().saturating_add(1));
            self.id.cmp(&other.id)
        }
    }

    #[test]
    #[expect(
        clippy::mutable_key_type,
        reason = "only the comparison counter is mutable; Eq, Hash and Ord use the immutable id"
    )]
    fn coalesced_wakes_freeze_unique_dirty_keys_in_order() {
        const WRITERS: usize = 36;

        struct Rules {
            comparisons: std::rc::Rc<std::cell::Cell<usize>>,
            delivered: std::rc::Rc<std::cell::RefCell<Vec<Vec<usize>>>>,
        }

        impl Rules {
            fn key(&self, id: usize) -> CountedKey {
                CountedKey {
                    id,
                    comparisons: std::rc::Rc::clone(&self.comparisons),
                }
            }
        }

        impl IterativeRules for Rules {
            type Key = CountedKey;
            type Fact = BTreeSet<usize>;
            type Rule = usize;
            type MergeScratch = ();
            const SOURCE_ONLY: bool = true;
            const EVALUATES_CHANGES: bool = true;

            fn evaluate(
                &self,
                rule: &usize,
                context: &mut impl FactContext<Self::Key, Self::Fact>,
            ) {
                if *rule == 0 {
                    // These are absent on the first read; later writes must
                    // wake this rule and remain visible in its changed set.
                    for id in [10, 11, 12] {
                        assert!(context.read(self.key(id)).is_none());
                    }
                    context.propose(self.key(20), BTreeSet::new());
                } else {
                    let id = 10 + ((*rule - 1) % 3);
                    context.propose(self.key(id), BTreeSet::from([*rule]));
                }
            }

            fn evaluate_changed(
                &self,
                rule: &usize,
                changed: &BTreeSet<Self::Key>,
                context: &mut impl FactContext<Self::Key, Self::Fact>,
            ) {
                assert_eq!(*rule, 0);
                self.delivered
                    .borrow_mut()
                    .push(changed.iter().map(|key| key.id).collect());
                let values = changed
                    .iter()
                    .filter_map(|key| context.read(self.key(key.id)).cloned())
                    .flatten()
                    .collect();
                context.propose(self.key(20), values);
            }

            fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
                let before = current.len();
                current.extend(contribution);
                current.len() != before
            }
        }

        let comparisons = std::rc::Rc::new(std::cell::Cell::new(0));
        let mut ordered_attempts = BTreeSet::new();
        for writer in 1..=WRITERS {
            ordered_attempts.insert(CountedKey {
                id: 10 + ((writer - 1) % 3),
                comparisons: std::rc::Rc::clone(&comparisons),
            });
        }
        let repeated_btree_comparisons = comparisons.get();
        drop(ordered_attempts);
        comparisons.set(0);

        let delivered = std::rc::Rc::default();
        let facts = IterativeInference::new(Rules {
            comparisons: std::rc::Rc::clone(&comparisons),
            delivered: std::rc::Rc::clone(&delivered),
        })
        .solve((0..=WRITERS).rev());
        let expected = (1..=WRITERS).collect::<BTreeSet<_>>();

        assert_eq!(
            *delivered.borrow(),
            [vec![10, 11, 12]],
            "repeated wakes coalesce to sorted unique late keys"
        );
        assert_eq!(
            facts
                .get(&CountedKey {
                    id: 20,
                    comparisons: std::rc::Rc::clone(&comparisons),
                })
                .unwrap()
                .value(),
            &expected
        );
        assert_eq!(facts.work.wake_attempts, WRITERS);
        assert_eq!(facts.work.schedules, WRITERS + 2);
        assert_eq!(facts.evaluations(), WRITERS + 2);
        assert!(
            comparisons.get() < repeated_btree_comparisons,
            "activation freeze compares only unique keys, not every wake attempt"
        );
    }

    #[test]
    fn positive_certificates_wait_for_all_required_growing_facts() {
        struct Certificates;
        impl IterativeRules for Certificates {
            type Key = usize;
            type Fact = BTreeSet<usize>;
            type Rule = usize;
            type MergeScratch = ();
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

        type MergeScratch = ();
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
        let mut facts = InferenceCells::default();
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
        let mut facts = InferenceCells::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
        context.propose(0, ());
        context.read(0);
        // Another live premise acquires evidence before the output is proposed.
        let dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([input(0)]),
            ..Default::default()
        };
        let id = context.facts.id(&0).unwrap();
        let added = context.extend_proof(id, &0, &dependencies);
        context.propagate_proof(0, added);
        context.propose(1, ());
        assert_eq!(facts[&1].dependencies(), &dependencies);
    }

    struct SourceOnly;

    impl IterativeRules for SourceOnly {
        type Key = usize;
        type Fact = ();
        type Rule = bool;
        type MergeScratch = ();
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
        let mut facts = InferenceCells::default();
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
        let mut facts = InferenceCells::default();
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
        let mut facts = InferenceCells::default();
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
        let mut facts = InferenceCells::default();
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
        let mut facts = InferenceCells::default();
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
    #[test]
    fn delta_edges_deliver_each_atom_once_and_backfill_current_exact_values() {
        let mut facts = InferenceCells::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
        let mut early = BTreeSet::new();
        for exact in [
            BTreeSet::from([1]),
            BTreeSet::from([2]),
            BTreeSet::from([1]),
        ] {
            context.propose_delta(10, exact, &mut early);
        }
        assert_eq!(context.delta_deliveries, 2);
        assert_eq!(early, BTreeSet::from([1, 2]));
        // A late edge receives the current result, not historical transient
        // alternatives from the earlier edge's delivered frontier.
        let mut late = BTreeSet::new();
        context.propose_delta(11, BTreeSet::from([1]), &mut late);
        assert!(
            !context
                .propose_delta(11, BTreeSet::from([1]), &mut late)
                .value
        );
        assert_eq!(facts[&10].value(), &BTreeSet::from([1, 2]));
        assert_eq!(facts[&11].value(), &BTreeSet::from([1]));
    }

    #[test]
    fn empty_value_deltas_preserve_proposal_scope_and_late_proof_backfill() {
        let mut facts = InferenceCells::default();
        let mut proof_edges = ProofTransfers::default();
        {
            let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
            for key in 0..4 {
                context.propose(key, BTreeSet::from([1]));
            }
        }
        {
            let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
            context.read(0);
            assert_eq!(
                context.propose_delta(2, BTreeSet::from([1]), &mut BTreeSet::from([1])),
                FactChange::default()
            );
            context.read(1);
            context.propose_delta(3, BTreeSet::from([1]), &mut BTreeSet::from([1]));
        }
        let proof = ProofDependencies {
            value_inputs: BTreeSet::from([input(0)]),
            ..Default::default()
        };
        {
            let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
            context.assume(&proof);
            context.propose(1, BTreeSet::from([1]));
        }
        assert!(facts[&2].dependencies().is_empty());
        assert_eq!(facts[&3].dependencies(), &proof);
        {
            let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
            context.read(1);
            // Already delivered value, newly activated proof edge.
            assert_eq!(
                context.propose_delta(2, BTreeSet::from([1]), &mut BTreeSet::from([1])),
                FactChange {
                    value: false,
                    proof: true
                }
            );
        }
        assert_eq!(facts[&2].dependencies(), &proof);
    }

    #[test]
    #[should_panic(expected = "this rule family does not define value deltas")]
    fn unsupported_delta_algebra_is_not_an_empty_delta() {
        let mut facts = InferenceCells::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
        context.propose_delta(0, (), &mut ());
    }
    #[test]
    fn an_empty_delta_retains_sealed_evidence_without_live_watchers() {
        let source = IterativeInference::new(Rules).solve([2]).seal();
        let mut facts = InferenceCells::default();
        let mut proof_edges = ProofTransfers::default();
        {
            let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
            context.propose(3, BTreeSet::from([20]));
        }
        {
            let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
            context.sealed = Some(&source);
            context.read(2);
            assert_eq!(
                context.propose_delta(3, BTreeSet::from([20]), &mut BTreeSet::from([20])),
                FactChange {
                    value: false,
                    proof: true
                }
            );
            assert!(context.reads.is_empty());
        }
        assert_eq!(
            facts[&3].dependencies(),
            source.get(&2).unwrap().dependencies()
        );
        assert_eq!(facts[&3].premises().copied().collect::<Vec<_>>(), [2]);
        assert!(proof_edges.edges.is_empty());
    }

    #[test]
    #[should_panic(expected = "cannot produce a sealed input")]
    fn a_value_empty_delta_cannot_bypass_sealed_input_ownership() {
        let mut source = IterativeInference::new(Rules);
        source.seed(0, BTreeSet::from([1]));
        let source = source.solve([]).seal();
        let mut facts = InferenceCells::default();
        let mut proof_edges = ProofTransfers::default();
        let mut context = IterativeContext::<Rules>::new(&mut facts, &mut proof_edges);
        context.sealed = Some(&source);
        context.propose_delta(0, BTreeSet::from([1]), &mut BTreeSet::from([1]));
    }
}
