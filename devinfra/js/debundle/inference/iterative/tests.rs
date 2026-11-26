use super::*;
use analysis_vocabulary::inputs::ValueInputRef;

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

        fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Self::Key, Self::Fact>) {
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
                origins
                    .iter()
                    .enumerate()
                    .map(|(index, &node)| (u8::from(!axioms_first), ProofRule::Axiom(node, index))),
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
        let mut context = IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
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
    IterativeInference::with_sealed(ProofGraphRules, &source).solve([(0, ProofRule::Axiom(0, 0))]);
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
        let mut context = IterativeContext::<ProofGraphRules>::new(&mut facts, &mut proof_edges);
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
        let mut context =
            IterativeContext::<ProofGraphRules, RETAIN_PREMISES>::new(&mut facts, &mut proof_edges);
        context.assume(&dependencies);
        context.propose(0, ());
    }
    for step in 0..256 {
        let intermediate = 2 * step + 1;
        let result = intermediate + 1;
        let mut context =
            IterativeContext::<ProofGraphRules, RETAIN_PREMISES>::new(&mut facts, &mut proof_edges);
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
