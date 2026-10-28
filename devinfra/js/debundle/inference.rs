//! Positive, demand-driven inference for immutable analysis facts.
//!
//! Rules discover their premises through [`RuleContext::require`]. The engine
//! queues those premises, records reverse watchers, and retries a dependent
//! only when one of its missing premises becomes proven. Facts are immutable
//! certificates: callers create a new inference engine when source or analysis
//! inputs change.
//!
//! The reachable key graph must be finite, with one rule producer per key.
//! This computes positive derivations; it is not a general lattice or SCC
//! solver.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use crate::analysis_inputs::ProofDependencies;

mod iterative;
pub(crate) use iterative::{IterativeContext, IterativeInference, IterativeRules};

/// Evaluates one keyed family of positive inference rules.
///
/// A rule must only return a fact whose truth is stable once its required
/// premises are proven. `None` means that the rule is unsupported or is waiting
/// for one or more required premises.
pub(crate) trait RuleSet {
    type Key: Clone + Ord;
    type Fact;

    fn evaluate(
        &self,
        key: &Self::Key,
        context: &mut RuleContext<'_, Self::Key, Self::Fact>,
    ) -> Option<Self::Fact>;
}

/// A proven fact and the direct premises that certify it.
///
/// Premises are stored as keys rather than nested judgments. This keeps a
/// shared proof graph compact; explanations can follow the keys through
/// [`Inference::get`].
#[derive(Debug, Clone, Eq, PartialEq)]
pub(crate) struct Judgment<Key, Fact> {
    value: Fact,
    dependencies: ProofDependencies,
    premises: BTreeSet<Key>,
}

impl<Key, Fact> Judgment<Key, Fact> {
    /// Returns the immutable fact certified by this judgment.
    pub fn value(&self) -> &Fact {
        &self.value
    }

    /// Returns the union of captured-value dependencies used by the proof.
    pub fn dependencies(&self) -> &ProofDependencies {
        &self.dependencies
    }

    /// Returns the direct inference premises used by the proof.
    pub fn premises(&self) -> &BTreeSet<Key> {
        &self.premises
    }
}

/// Demand-driven positive inference over one immutable rule context.
///
/// An unknown or unsupported key remains unproven. It may be retried if a
/// premise it requested is proven while the current query drains its worklist.
/// A key still waiting when that query returns is quiescent in this context;
/// use a new engine when source or analysis inputs change.
#[derive(Debug)]
pub(crate) struct Inference<R: RuleSet> {
    rules: R,
    judgments: BTreeMap<R::Key, Judgment<R::Key, R::Fact>>,
    attempted: BTreeSet<R::Key>,
    queued: BTreeSet<R::Key>,
    worklist: VecDeque<R::Key>,
    reverse_watchers: BTreeMap<R::Key, BTreeSet<R::Key>>,
    pending: BTreeMap<R::Key, BTreeSet<R::Key>>,
}

/// The state of a key after the most recent worklist closure.
#[derive(Debug)]
pub(crate) enum InferenceStatus<'a, Key, Fact> {
    /// The key has a positive immutable judgment.
    Proven(&'a Judgment<Key, Fact>),
    /// The key has one or more unproven premises after the current worklist
    /// reached closure.
    Waiting(&'a BTreeSet<Key>),
    /// The key was evaluated to closure without a proof or missing premise.
    Unsupported,
}

impl<R: RuleSet> Inference<R> {
    /// Creates an inference context that owns its immutable rule set.
    pub fn new(rules: R) -> Self {
        Self {
            rules,
            judgments: BTreeMap::new(),
            attempted: BTreeSet::new(),
            queued: BTreeSet::new(),
            worklist: VecDeque::new(),
            reverse_watchers: BTreeMap::new(),
            pending: BTreeMap::new(),
        }
    }

    /// Discovers and evaluates the requested key and its missing premises.
    ///
    /// Processing continues until no newly discovered rule or newly proven
    /// premise can make progress. The returned judgment is available only when
    /// the requested key has a positive proof.
    pub fn query(&mut self, key: &R::Key) -> Option<&Judgment<R::Key, R::Fact>> {
        self.enqueue(key.clone());
        self.run_worklist();
        self.get(key)
    }

    /// Reads a completed judgment without changing the inference context.
    pub fn get(&self, key: &R::Key) -> Option<&Judgment<R::Key, R::Fact>> {
        self.judgments.get(key)
    }

    /// Consumes a completed store and moves out one proved fact. Callers can
    /// expand its explanation first without cloning the fact's payload.
    pub fn into_value(mut self, key: &R::Key) -> Option<R::Fact> {
        self.judgments.remove(key).map(|judgment| judgment.value)
    }

    /// Reports whether a discovered key is proven, waiting, or unsupported.
    ///
    /// `None` means the key has not been discovered. After [`Self::query`]
    /// returns `None`, `Waiting` lists the premises still unproven at closure;
    /// it neither implies queued work nor proves semantic absence.
    /// `Unsupported` means the key was evaluated to closure without a missing
    /// premise that could wake it.
    pub fn status(&self, key: &R::Key) -> Option<InferenceStatus<'_, R::Key, R::Fact>> {
        if let Some(judgment) = self.judgments.get(key) {
            return Some(InferenceStatus::Proven(judgment));
        }
        if let Some(premises) = self.pending.get(key) {
            return Some(InferenceStatus::Waiting(premises));
        }
        self.attempted
            .contains(key)
            .then_some(InferenceStatus::Unsupported)
    }

    fn enqueue(&mut self, key: R::Key) {
        if self.judgments.contains_key(&key)
            || self.attempted.contains(&key)
            || !self.queued.insert(key.clone())
        {
            return;
        }
        self.worklist.push_back(key);
    }

    fn retry(&mut self, key: R::Key) {
        if !self.judgments.contains_key(&key) {
            self.attempted.remove(&key);
            self.enqueue(key);
        }
    }

    fn run_worklist(&mut self) {
        while let Some(key) = self.worklist.pop_front() {
            self.queued.remove(&key);
            if self.judgments.contains_key(&key) || !self.attempted.insert(key.clone()) {
                continue;
            }

            self.unwatch_pending(&key);

            let (result, premises, missing, dependencies) = {
                let mut context = RuleContext {
                    proven: &self.judgments,
                    premises: BTreeSet::new(),
                    missing: BTreeSet::new(),
                    dependencies: ProofDependencies::default(),
                };
                let result = self.rules.evaluate(&key, &mut context);
                (
                    result,
                    context.premises,
                    context.missing,
                    context.dependencies,
                )
            };

            if let Some(value) = result.filter(|_| missing.is_empty()) {
                self.judgments.insert(
                    key.clone(),
                    Judgment {
                        value,
                        dependencies,
                        premises,
                    },
                );
                if let Some(watchers) = self.reverse_watchers.remove(&key) {
                    for dependent in watchers {
                        self.retry(dependent);
                    }
                }
                continue;
            }

            for premise in missing {
                self.reverse_watchers
                    .entry(premise.clone())
                    .or_default()
                    .insert(key.clone());
                self.pending
                    .entry(key.clone())
                    .or_default()
                    .insert(premise.clone());
                self.enqueue(premise);
            }
        }
    }

    fn unwatch_pending(&mut self, key: &R::Key) {
        let Some(premises) = self.pending.remove(key) else {
            return;
        };
        for premise in premises {
            let remove_entry = self
                .reverse_watchers
                .get_mut(&premise)
                .is_some_and(|watchers| {
                    watchers.remove(key);
                    watchers.is_empty()
                });
            if remove_entry {
                self.reverse_watchers.remove(&premise);
            }
        }
    }
}

/// The facts and dependency accumulator visible during one rule evaluation.
#[derive(Debug)]
pub(crate) struct RuleContext<'a, Key, Fact> {
    proven: &'a BTreeMap<Key, Judgment<Key, Fact>>,
    premises: BTreeSet<Key>,
    missing: BTreeSet<Key>,
    dependencies: ProofDependencies,
}

impl<'a, Key: Clone + Ord, Fact> RuleContext<'a, Key, Fact> {
    /// Records a required premise and returns its fact only after it is proven.
    ///
    /// If the premise is not yet known, the engine schedules it during the
    /// current query and retries this rule if that premise becomes proven.
    pub fn require(&mut self, key: Key) -> Option<&'a Fact> {
        self.premises.insert(key.clone());
        let Some(judgment) = self.proven.get(&key) else {
            self.missing.insert(key);
            return None;
        };
        self.dependencies.extend(judgment.dependencies());
        Some(judgment.value())
    }

    /// Borrows a proved premise from an earlier immutable context and inherits
    /// its dependencies. A local bridge rule must retain that external proof's
    /// identity for explanations; its consumers require the bridge's key.
    pub fn import<'b, OtherKey, OtherFact>(
        &mut self,
        premise: &'b Judgment<OtherKey, OtherFact>,
    ) -> &'b OtherFact {
        self.dependencies.extend(premise.dependencies());
        premise.value()
    }

    /// Adds dependencies from a completed author or runtime premise.
    ///
    /// Call this only after the rule has established that the supplied premise
    /// is complete. Inferred facts use [`Self::require`] within this context or
    /// [`Self::import`] for a completed earlier context.
    pub fn assume(&mut self, dependencies: &ProofDependencies) {
        self.dependencies.extend(dependencies);
    }
}

#[cfg(test)]
mod tests {
    use std::{cell::RefCell, rc::Rc};

    use super::*;
    use crate::analysis_inputs::ValueInputRef;

    #[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd)]
    enum Key {
        A,
        B,
        C,
        D,
        E,
        Unknown,
    }

    enum Rule {
        Prove {
            premises: Vec<Key>,
            value: i32,
            assumptions: ProofDependencies,
        },
        Premature {
            premise: Key,
            value: i32,
        },
        Unsupported,
    }

    struct Rules {
        rules: BTreeMap<Key, Rule>,
        evaluations: Rc<RefCell<BTreeMap<Key, usize>>>,
    }

    impl Rules {
        fn new(rules: impl IntoIterator<Item = (Key, Rule)>) -> Self {
            Self {
                rules: rules.into_iter().collect(),
                evaluations: Rc::new(RefCell::new(BTreeMap::new())),
            }
        }

        fn add(&mut self, key: Key, premises: &[Key], value: i32) {
            self.rules.insert(
                key,
                Rule::Prove {
                    premises: premises.to_vec(),
                    value,
                    assumptions: ProofDependencies::default(),
                },
            );
        }

        fn set_assumptions(&mut self, key: Key, assumptions: ProofDependencies) {
            let Some(Rule::Prove {
                assumptions: slot, ..
            }) = self.rules.get_mut(&key)
            else {
                panic!("expected a proving rule for {key:?}");
            };
            *slot = assumptions;
        }

        fn unsupported(key: Key) -> (Key, Rule) {
            (key, Rule::Unsupported)
        }
    }

    impl RuleSet for Rules {
        type Key = Key;
        type Fact = i32;

        fn evaluate(&self, key: &Key, context: &mut RuleContext<'_, Key, i32>) -> Option<i32> {
            *self.evaluations.borrow_mut().entry(*key).or_default() += 1;
            let rule = self.rules.get(key)?;
            match rule {
                Rule::Prove {
                    premises,
                    value,
                    assumptions,
                } => {
                    let mut all_proven = true;
                    for premise in premises {
                        all_proven &= context.require(*premise).is_some();
                    }
                    if !all_proven {
                        return None;
                    }
                    context.assume(assumptions);
                    Some(*value)
                }
                Rule::Premature { premise, value } => {
                    let _ = context.require(*premise);
                    Some(*value)
                }
                Rule::Unsupported => None,
            }
        }
    }

    struct TextFact(String);

    struct TextRules;

    struct ImportedRules<'a> {
        left: &'a Judgment<Key, i32>,
        right: &'a Judgment<Key, i32>,
    }

    impl RuleSet for ImportedRules<'_> {
        type Key = Key;
        type Fact = i32;

        fn evaluate(&self, key: &Key, context: &mut RuleContext<'_, Key, i32>) -> Option<i32> {
            (*key == Key::A).then(|| *context.import(self.left) + *context.import(self.right))
        }
    }

    impl RuleSet for TextRules {
        type Key = Key;
        type Fact = TextFact;

        fn evaluate(
            &self,
            key: &Key,
            context: &mut RuleContext<'_, Key, TextFact>,
        ) -> Option<TextFact> {
            match key {
                Key::A => {
                    let left = context.require(Key::B)?;
                    let right = context.require(Key::C)?;
                    Some(TextFact(format!("{}+{}", left.0, right.0)))
                }
                Key::B => Some(TextFact("left".to_owned())),
                Key::C => Some(TextFact("right".to_owned())),
                _ => None,
            }
        }
    }

    fn dependency(chunk: &str, input_index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: [ValueInputRef {
                defining_chunk: chunk.to_owned(),
                input_index,
            }]
            .into_iter()
            .collect(),
        }
    }

    fn rules_with_chain(omitted: Option<Key>) -> Rules {
        let mut rules = Rules::new(std::iter::empty::<(Key, Rule)>());
        if omitted != Some(Key::A) {
            rules.add(Key::A, &[Key::B], 1);
        }
        if omitted != Some(Key::B) {
            rules.add(Key::B, &[Key::C], 2);
        }
        if omitted != Some(Key::C) {
            rules.add(Key::C, &[], 3);
        }
        rules
    }

    #[test]
    fn importing_completed_judgments_unions_dependencies_without_rebuilding_them() {
        let mut rules = Rules::new(std::iter::empty::<(Key, Rule)>());
        rules.add(Key::B, &[], 2);
        rules.add(Key::C, &[], 3);
        rules.set_assumptions(Key::B, dependency("left", 1));
        rules.set_assumptions(Key::C, dependency("right", 2));
        let mut source = Inference::new(rules);
        source.query(&Key::B);
        source.query(&Key::C);
        assert!(source.get(&Key::Unknown).is_none());

        let mut imported = Inference::new(ImportedRules {
            left: source.get(&Key::B).unwrap(),
            right: source.get(&Key::C).unwrap(),
        });
        let judgment = imported.query(&Key::A).unwrap();
        assert_eq!(judgment.value(), &5);
        assert_eq!(
            judgment.dependencies().value_inputs,
            dependency("left", 1)
                .value_inputs
                .union(&dependency("right", 2).value_inputs)
                .cloned()
                .collect()
        );
    }

    #[test]
    fn discovers_transitive_three_rule_chain_and_each_rule_is_required() {
        let mut complete = Inference::new(rules_with_chain(None));
        assert_eq!(complete.query(&Key::A).map(Judgment::value), Some(&1));
        assert_eq!(complete.get(&Key::C).map(Judgment::value), Some(&3));

        for omitted in [Key::A, Key::B, Key::C] {
            let mut ablated = Inference::new(rules_with_chain(Some(omitted)));
            assert!(ablated.query(&Key::A).is_none(), "omitting {omitted:?}");
        }
    }

    #[test]
    fn rejects_fact_returned_while_a_required_premise_is_missing() {
        let rules = Rules::new([
            (
                Key::A,
                Rule::Premature {
                    premise: Key::B,
                    value: 1,
                },
            ),
            Rules::unsupported(Key::B),
        ]);
        let mut inference = Inference::new(rules);

        assert!(inference.query(&Key::A).is_none());
        assert!(matches!(
            inference.status(&Key::A),
            Some(InferenceStatus::Waiting(premises)) if premises == &BTreeSet::from([Key::B])
        ));
        assert!(matches!(
            inference.status(&Key::B),
            Some(InferenceStatus::Unsupported)
        ));
    }

    #[test]
    fn rule_can_combine_non_clone_facts_through_sequential_references() {
        let mut inference = Inference::new(TextRules);

        assert_eq!(
            inference
                .query(&Key::A)
                .map(|judgment| judgment.value().0.as_str()),
            Some("left+right")
        );
        assert_eq!(inference.into_value(&Key::A).unwrap().0, "left+right");
    }

    #[test]
    fn reverse_root_order_preserves_closure_and_unions_all_assumptions() {
        fn make_rules() -> Rules {
            let mut rules = Rules::new([
                (
                    Key::A,
                    Rule::Prove {
                        premises: vec![Key::B, Key::C],
                        value: 1,
                        assumptions: ProofDependencies::default(),
                    },
                ),
                (
                    Key::E,
                    Rule::Prove {
                        premises: vec![Key::A],
                        value: 5,
                        assumptions: ProofDependencies::default(),
                    },
                ),
            ]);
            rules.add(Key::B, &[Key::D], 2);
            rules.add(Key::C, &[Key::D], 3);
            rules.add(Key::D, &[], 4);
            rules.set_assumptions(Key::B, dependency("provider", 1));
            rules.set_assumptions(Key::C, dependency("provider", 2));
            rules.set_assumptions(Key::D, dependency("provider", 3));
            rules
        }

        fn prove_roots(reverse: bool) -> Inference<Rules> {
            let mut inference = Inference::new(make_rules());
            let roots = if reverse {
                [Key::E, Key::A]
            } else {
                [Key::A, Key::E]
            };
            for root in roots {
                assert!(inference.query(&root).is_some());
            }
            inference
        }

        let first = prove_roots(false);
        let second = prove_roots(true);
        for key in [Key::A, Key::B, Key::C, Key::D, Key::E] {
            let first_judgment = first.get(&key).expect("completed closure contains key");
            let second_judgment = second.get(&key).expect("completed closure contains key");
            assert_eq!(first_judgment.value(), second_judgment.value());
            assert_eq!(
                first_judgment.dependencies(),
                second_judgment.dependencies()
            );
            assert_eq!(first_judgment.premises(), second_judgment.premises());
        }
        assert_eq!(
            first.get(&Key::A).unwrap().premises(),
            &BTreeSet::from([Key::B, Key::C])
        );
        assert_eq!(
            first.get(&Key::A).unwrap().dependencies().value_inputs,
            BTreeSet::from([
                ValueInputRef {
                    defining_chunk: "provider".to_owned(),
                    input_index: 1,
                },
                ValueInputRef {
                    defining_chunk: "provider".to_owned(),
                    input_index: 2,
                },
                ValueInputRef {
                    defining_chunk: "provider".to_owned(),
                    input_index: 3,
                },
            ])
        );
    }

    #[test]
    fn diamond_deduplicates_discovery_and_retries_only_affected_rules() {
        let mut rules = Rules::new([(
            Key::A,
            Rule::Prove {
                premises: vec![Key::B, Key::C],
                value: 1,
                assumptions: ProofDependencies::default(),
            },
        )]);
        rules.add(Key::B, &[Key::D], 2);
        rules.add(Key::C, &[Key::D], 3);
        rules.add(Key::D, &[], 4);
        rules.rules.insert(Key::Unknown, Rule::Unsupported);
        let evaluations = Rc::clone(&rules.evaluations);
        let mut inference = Inference::new(rules);

        assert!(inference.query(&Key::A).is_some());
        let evaluations = evaluations.borrow();
        assert_eq!(evaluations.get(&Key::D), Some(&1));
        assert_eq!(evaluations.get(&Key::B), Some(&2));
        assert_eq!(evaluations.get(&Key::C), Some(&2));
        assert_eq!(evaluations.get(&Key::A), Some(&2));
        assert_eq!(evaluations.get(&Key::Unknown), None);
    }

    #[test]
    fn cycles_and_unknown_rules_terminate_without_positive_facts() {
        let rules = Rules::new([
            (
                Key::A,
                Rule::Prove {
                    premises: vec![Key::B],
                    value: 1,
                    assumptions: ProofDependencies::default(),
                },
            ),
            (
                Key::B,
                Rule::Prove {
                    premises: vec![Key::A],
                    value: 2,
                    assumptions: ProofDependencies::default(),
                },
            ),
            Rules::unsupported(Key::Unknown),
        ]);
        let evaluations = Rc::clone(&rules.evaluations);
        let mut inference = Inference::new(rules);

        assert!(inference.query(&Key::A).is_none());
        assert!(inference.query(&Key::Unknown).is_none());
        assert!(inference.query(&Key::A).is_none());
        assert!(matches!(
            inference.status(&Key::A),
            Some(InferenceStatus::Waiting(premises)) if premises == &BTreeSet::from([Key::B])
        ));
        assert!(matches!(
            inference.status(&Key::Unknown),
            Some(InferenceStatus::Unsupported)
        ));
        let evaluations = evaluations.borrow();
        assert_eq!(evaluations.get(&Key::A), Some(&1));
        assert_eq!(evaluations.get(&Key::B), Some(&1));
        assert_eq!(evaluations.get(&Key::Unknown), Some(&1));
    }

    #[test]
    fn facts_do_not_leak_between_inference_contexts() {
        let complete = rules_with_chain(None);
        let mut first = Inference::new(complete);
        assert!(first.query(&Key::A).is_some());

        let incomplete = rules_with_chain(Some(Key::C));
        let mut second = Inference::new(incomplete);
        assert!(second.query(&Key::A).is_none());
        assert!(second.get(&Key::C).is_none());
    }
}
