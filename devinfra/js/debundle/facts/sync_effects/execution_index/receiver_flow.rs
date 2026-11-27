//! Receiver-flow analysis: which native roots a receiver value can denote.

use super::ExecutionIndex;
use super::intrinsic_roots::{NativeReceiverRoot, NativeReceiverRoots, intrinsic_roots};
use analysis_vocabulary::inputs::{NativeIntrinsic, ValueInputRef};
use analysis_vocabulary::value::{Kind, Value};
use inference::{FactContext, IterativeInference, IterativeRules};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::*;
use value_flow::ChunkValueFlow;
use value_model::ValueExpr;

#[derive(Debug, Clone, Default)]
pub(super) struct CallableReturnInputs {
    pub(super) by_binding: BTreeMap<Id, NativeReceiverRoots>,
    pub(super) by_origin: BTreeMap<u32, NativeReceiverRoots>,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub(super) enum ReceiverFlowKey {
    Assignment(usize),
    Binding(Id),
    BindingSeed(Id),
    ReturnSite(u32, usize),
    Callable(u32),
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub(super) enum CallableReturnOutcome {
    Safe,
    Descendant(ValueInputRef, SourceUnitOrdinal),
    Intrinsic(NativeIntrinsic),
    Unknown,
}

#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub(super) struct ReceiverFlowFact {
    pub(super) roots: NativeReceiverRoots,
    pub(super) callable_targets: BTreeSet<u32>,
    pub(super) callable_dependencies: BTreeSet<u32>,
    pub(super) capture_dependencies: BTreeSet<(ValueInputRef, SourceUnitOrdinal)>,
    pub(super) outcomes: BTreeSet<CallableReturnOutcome>,
    pub(super) opaque: bool,
    pub(super) safe: bool,
    pub(super) complete: bool,
}

impl ReceiverFlowFact {
    pub(super) fn merge(&mut self, contribution: Self) -> bool {
        let previous = self.clone();
        self.roots.extend(contribution.roots);
        self.callable_targets.extend(contribution.callable_targets);
        self.callable_dependencies
            .extend(contribution.callable_dependencies);
        self.capture_dependencies
            .extend(contribution.capture_dependencies);
        self.outcomes.extend(contribution.outcomes);
        self.opaque |= contribution.opaque;
        self.safe |= contribution.safe;
        self.complete |= contribution.complete;
        *self != previous
    }
}

#[derive(Debug, Clone, Default)]
pub(super) struct ReceiverFlowValue {
    pub(super) fact: ReceiverFlowFact,
    pub(super) pending: bool,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub(super) enum ReceiverFlowRule {
    Assignment(usize),
    Binding(Id),
    ReturnSite(u32, usize),
    Callable(u32),
}

pub(super) struct ReceiverFlowRules<'a> {
    pub(super) assignments: &'a [&'a (Id, ValueExpr)],
    pub(super) assignment_indices: &'a BTreeMap<Id, BTreeSet<usize>>,
    pub(super) index: &'a ExecutionIndex,
    pub(super) declared: &'a BTreeSet<Id>,
    pub(super) pending_bindings: &'a BTreeSet<Id>,
}

pub(super) fn receiver_member_roots(
    base_roots: &NativeReceiverRoots,
    property: Option<&str>,
) -> NativeReceiverRoots {
    let mut out = NativeReceiverRoots::new();
    for root in base_roots {
        match root {
            NativeReceiverRoot::ArrayIteratorPrototype
            | NativeReceiverRoot::SetIteratorPrototype
            | NativeReceiverRoot::BundleGlobal => {
                out.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
            }
            NativeReceiverRoot::OrdinaryDirect {
                reference,
                available_after,
            }
            | NativeReceiverRoot::OrdinaryDescendant {
                reference,
                available_after,
            } => {
                out.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
                out.insert(NativeReceiverRoot::OrdinaryDescendant {
                    reference: reference.clone(),
                    available_after: *available_after,
                });
            }
            NativeReceiverRoot::Intrinsic(NativeIntrinsic::GlobalObject) => {
                out.insert(NativeReceiverRoot::Intrinsic(
                    property
                        .and_then(NativeIntrinsic::for_global_binding)
                        .unwrap_or(NativeIntrinsic::UnknownGlobalValue),
                ));
            }
            NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue) => {
                out.insert(NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
            }
            NativeReceiverRoot::Intrinsic(root) => {
                out.insert(if property == Some("prototype") {
                    NativeReceiverRoot::IntrinsicPrototype(*root)
                } else {
                    NativeReceiverRoot::IntrinsicDescendant(*root)
                });
                if property.is_none_or(|property| matches!(property, "__proto__" | "constructor")) {
                    out.insert(NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::UnknownGlobalValue,
                    ));
                }
            }
            NativeReceiverRoot::IntrinsicPrototype(root)
            | NativeReceiverRoot::IntrinsicDescendant(root) => {
                out.insert(NativeReceiverRoot::IntrinsicDescendant(*root));
                if property.is_none_or(|property| matches!(property, "__proto__" | "constructor")) {
                    out.insert(NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::UnknownGlobalValue,
                    ));
                }
            }
        }
    }
    out
}

pub(super) fn evaluate_receiver_value(
    value: &ValueExpr,
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    rules: &ReceiverFlowRules<'_>,
) -> ReceiverFlowValue {
    let mut result = ReceiverFlowValue::default();
    match value {
        ValueExpr::ArrayIteratorPrototype(_) | ValueExpr::SetIteratorPrototype(_) => {
            result.fact.roots = intrinsic_roots(
                value,
                &BTreeMap::new(),
                &rules.index.census_globals,
                rules.declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            result.fact.complete = true;
            return result;
        }
        ValueExpr::Binding(binding) => {
            result = evaluate_binding_receiver(value, binding, context, rules);
        }
        ValueExpr::Alloc(Kind::Function, function) => {
            result.fact.callable_targets.insert(*function);
            result.fact.complete = true;
            read_callable_capture_dependencies(
                &BTreeSet::from([*function]),
                context,
                &mut result.fact,
            );
        }
        ValueExpr::Alloc(..) => {
            result.fact.opaque = true;
            result.fact.complete = true;
        }
        ValueExpr::ObjectLiteral(_, fields) => {
            result = evaluate_object_literal_receiver(fields, context, rules);
        }
        // One arm for every result that cannot denote an object. The variants
        // stay written out because this match has no fallback: a new value kind
        // has to say what it does to receiver safety.
        ValueExpr::Undefined
        | ValueExpr::NonCallable(_)
        | ValueExpr::Primitive(_)
        | ValueExpr::StrictEquality(..)
        | ValueExpr::LogicalNot(_)
        | ValueExpr::Void(_) => {
            result.fact.safe = true;
            result.fact.complete = true;
        }
        ValueExpr::Member(base, property, _, _) => {
            result = evaluate_member_receiver(base, property.as_deref(), context, rules);
        }
        ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } | ValueExpr::Sequence(_) => {
            result = evaluate_branching_receiver(value, context, rules);
        }
        ValueExpr::Call(callee, args, _) => {
            result = evaluate_call_receiver(callee, args, context, rules);
        }
        ValueExpr::New(callee, args, _) => {
            result = evaluate_new_receiver(callee, args, context, rules);
        }
        ValueExpr::Unknown => {
            result.fact.opaque = true;
            result.fact.complete = true;
        }
    }
    result
}

fn evaluate_binding_receiver(
    value: &ValueExpr,
    binding: &Id,
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    rules: &ReceiverFlowRules<'_>,
) -> ReceiverFlowValue {
    let mut result = ReceiverFlowValue::default();
    if let Some((reference, available_after)) = rules.index.ordinary_input_refs.get(binding) {
        result
            .fact
            .roots
            .insert(NativeReceiverRoot::OrdinaryDirect {
                reference: reference.clone(),
                available_after: *available_after,
            });
        result
            .fact
            .capture_dependencies
            .insert((reference.clone(), *available_after));
        return result;
    }
    let builtin_roots = intrinsic_roots(
        value,
        &BTreeMap::new(),
        &rules.index.census_globals,
        rules.declared,
        &BTreeMap::new(),
        &CallableReturnInputs::default(),
    );
    if !builtin_roots.is_empty() {
        result.fact.roots = builtin_roots;
        result.fact.complete = true;
        return result;
    }
    if let Some(fact) = context.read(ReceiverFlowKey::Binding(binding.clone())) {
        result.fact = fact.clone();
        result.pending = !fact.complete;
    } else if rules.pending_bindings.contains(binding) {
        result.pending = true;
    } else {
        result.fact.opaque = true;
        result.fact.complete = true;
    }
    let targets = result.fact.callable_targets.clone();
    read_callable_capture_dependencies(&targets, context, &mut result.fact);
    result
}

fn evaluate_object_literal_receiver(
    fields: &[(Option<String>, ValueExpr)],
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    rules: &ReceiverFlowRules<'_>,
) -> ReceiverFlowValue {
    let mut result = ReceiverFlowValue::default();
    let mut ordinary_refs = BTreeSet::new();
    result.fact.safe = true;
    result.fact.complete = true;
    for (_, field) in fields.iter() {
        let field = evaluate_receiver_value(field, context, rules);
        result.pending |= field.pending;
        result.fact.safe &= field.fact.safe;
        result.fact.opaque |= field.fact.opaque;
        result
            .fact
            .callable_targets
            .extend(field.fact.callable_targets);
        result
            .fact
            .callable_dependencies
            .extend(field.fact.callable_dependencies);
        result
            .fact
            .capture_dependencies
            .extend(field.fact.capture_dependencies);
        result.fact.complete &= field.fact.complete;
        for root in field.fact.roots {
            match root {
                NativeReceiverRoot::OrdinaryDirect {
                    reference,
                    available_after,
                }
                | NativeReceiverRoot::OrdinaryDescendant {
                    reference,
                    available_after,
                } => {
                    ordinary_refs.insert((reference, available_after));
                }
                NativeReceiverRoot::Intrinsic(_)
                | NativeReceiverRoot::IntrinsicPrototype(_)
                | NativeReceiverRoot::IntrinsicDescendant(_)
                | NativeReceiverRoot::ArrayIteratorPrototype
                | NativeReceiverRoot::SetIteratorPrototype
                | NativeReceiverRoot::BundleGlobal => {}
            }
        }
    }
    if !ordinary_refs.is_empty() {
        result.fact.safe = false;
        result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
            NativeIntrinsic::UnknownGlobalValue,
        ));
    }
    result.fact.roots.extend(
        ordinary_refs
            .into_iter()
            .map(
                |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                    reference,
                    available_after,
                },
            ),
    );
    // A returned object containing a callable can expose that callable's
    // later native return through a projection. This bounded model does
    // not inspect arbitrary object protocols, so keep that escape opaque.
    if !result.fact.callable_targets.is_empty() {
        result.fact.safe = false;
        result.fact.opaque = true;
    }
    result
}

fn evaluate_member_receiver(
    base: &ValueExpr,
    property: Option<&str>,
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    rules: &ReceiverFlowRules<'_>,
) -> ReceiverFlowValue {
    let mut result = ReceiverFlowValue::default();
    let base = evaluate_receiver_value(base, context, rules);
    result.pending = base.pending;
    // A primitive or fresh-container receiver does not prove that its
    // inherited property is primitive or fresh. Keep arbitrary member
    // reads opaque; captured-rooted reads retain descendant taint via
    // `receiver_member_roots` below.
    result.fact.safe = false;
    result.fact.opaque = base.fact.opaque;
    result.fact.callable_dependencies = base.fact.callable_dependencies;
    result.fact.capture_dependencies = base.fact.capture_dependencies;
    result.fact.complete = base.fact.complete;
    result.fact.roots = receiver_member_roots(&base.fact.roots, property);
    if base.fact.roots.is_empty() || !base.fact.callable_targets.is_empty() {
        result.fact.opaque = true;
    }
    if result.fact.opaque {
        result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
            NativeIntrinsic::UnknownGlobalValue,
        ));
    }
    result
}

fn evaluate_branching_receiver(
    value: &ValueExpr,
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    rules: &ReceiverFlowRules<'_>,
) -> ReceiverFlowValue {
    let mut result = ReceiverFlowValue::default();
    let options = value.branching_alternatives();
    result.fact.safe = true;
    result.fact.complete = true;
    for option in options {
        let option = evaluate_receiver_value(option, context, rules);
        result.pending |= option.pending;
        result.fact.safe &= option.fact.safe;
        result.fact.opaque |= option.fact.opaque;
        result.fact.roots.extend(option.fact.roots);
        result
            .fact
            .callable_targets
            .extend(option.fact.callable_targets);
        result
            .fact
            .callable_dependencies
            .extend(option.fact.callable_dependencies);
        result
            .fact
            .capture_dependencies
            .extend(option.fact.capture_dependencies);
        result.fact.complete &= option.fact.complete;
    }
    if result.fact.opaque {
        result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
            NativeIntrinsic::UnknownGlobalValue,
        ));
    }
    result
}

fn evaluate_call_receiver(
    callee: &ValueExpr,
    args: &[ValueExpr],
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    rules: &ReceiverFlowRules<'_>,
) -> ReceiverFlowValue {
    let mut result = ReceiverFlowValue::default();
    let callee_value = evaluate_receiver_value(callee, context, rules);
    result.pending = callee_value.pending;
    result.fact.complete = callee_value.fact.complete;
    result
        .fact
        .roots
        .extend(callee_value.fact.roots.iter().cloned());
    result.fact.opaque |= callee_value.fact.opaque;
    result
        .fact
        .callable_dependencies
        .extend(callee_value.fact.callable_dependencies.iter().copied());
    result
        .fact
        .capture_dependencies
        .extend(callee_value.fact.capture_dependencies.iter().cloned());
    let mut ordinary_refs: BTreeSet<_> = result
        .fact
        .roots
        .iter()
        .filter_map(NativeReceiverRoot::cloned_ordinary_input)
        .collect();
    for argument in args.iter() {
        let argument = evaluate_receiver_value(argument, context, rules);
        result.pending |= argument.pending;
        result.fact.complete &= argument.fact.complete;
        result.fact.opaque |= argument.fact.opaque;
        result
            .fact
            .callable_dependencies
            .extend(argument.fact.callable_dependencies.iter().copied());
        result
            .fact
            .capture_dependencies
            .extend(argument.fact.capture_dependencies.iter().cloned());
        result
            .fact
            .roots
            .extend(argument.fact.roots.iter().cloned());
        for root in argument.fact.roots {
            if let NativeReceiverRoot::OrdinaryDirect {
                reference,
                available_after,
            }
            | NativeReceiverRoot::OrdinaryDescendant {
                reference,
                available_after,
            } = root
            {
                ordinary_refs.insert((reference, available_after));
            }
        }
    }
    if !ordinary_refs.is_empty() {
        result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
            NativeIntrinsic::UnknownGlobalValue,
        ));
        result
            .fact
            .roots
            .extend(ordinary_refs.iter().map(|(reference, available_after)| {
                NativeReceiverRoot::OrdinaryDescendant {
                    reference: reference.clone(),
                    available_after: *available_after,
                }
            }));
    }
    if callee_value.fact.callable_targets.is_empty() {
        result.pending |= callee_value.pending;
        if !callee_value.pending {
            result.fact.opaque = true;
        }
    } else {
        for function in callee_value.fact.callable_targets {
            result.fact.callable_dependencies.insert(function);
            if let Some(summary) = context.read(ReceiverFlowKey::Callable(function)) {
                for outcome in &summary.outcomes {
                    match outcome {
                        CallableReturnOutcome::Safe => result.fact.safe = true,
                        CallableReturnOutcome::Descendant(reference, available_after) => {
                            result
                                .fact
                                .capture_dependencies
                                .insert((reference.clone(), *available_after));
                            result
                                .fact
                                .roots
                                .insert(NativeReceiverRoot::OrdinaryDescendant {
                                    reference: reference.clone(),
                                    available_after: *available_after,
                                });
                        }
                        CallableReturnOutcome::Intrinsic(root) => {
                            result
                                .fact
                                .roots
                                .insert(NativeReceiverRoot::Intrinsic(*root));
                        }
                        CallableReturnOutcome::Unknown => result.fact.opaque = true,
                    }
                }
                result.fact.opaque |= summary.opaque;
                result
                    .fact
                    .capture_dependencies
                    .extend(summary.capture_dependencies.iter().cloned());
                result
                    .fact
                    .callable_dependencies
                    .extend(summary.callable_dependencies.iter().copied());
                result.fact.complete &= summary.complete;
                result.pending |= !summary.complete;
            } else {
                result.fact.complete = false;
                result.pending = true;
            }
        }
    }
    result.fact.complete &= !result.pending;
    if result.fact.opaque {
        result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
            NativeIntrinsic::UnknownGlobalValue,
        ));
    }
    result
}

fn evaluate_new_receiver(
    callee: &ValueExpr,
    args: &[ValueExpr],
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    rules: &ReceiverFlowRules<'_>,
) -> ReceiverFlowValue {
    let mut result = ReceiverFlowValue::default();
    let callee = evaluate_receiver_value(callee, context, rules);
    result.pending = callee.pending;
    result.fact.complete = callee.fact.complete;
    result.fact.roots.extend(callee.fact.roots);
    result
        .fact
        .capture_dependencies
        .extend(callee.fact.capture_dependencies);
    result.fact.opaque = true;
    for argument in args.iter() {
        let argument = evaluate_receiver_value(argument, context, rules);
        result.pending |= argument.pending;
        result.fact.complete &= argument.fact.complete;
        result
            .fact
            .callable_dependencies
            .extend(argument.fact.callable_dependencies);
        result
            .fact
            .capture_dependencies
            .extend(argument.fact.capture_dependencies);
        result.fact.roots.extend(argument.fact.roots);
    }
    result.fact.complete &= !result.pending;
    let ordinary_refs: BTreeSet<_> = result
        .fact
        .roots
        .iter()
        .filter_map(NativeReceiverRoot::cloned_ordinary_input)
        .collect();
    if !ordinary_refs.is_empty() {
        result.fact.roots.insert(NativeReceiverRoot::Intrinsic(
            NativeIntrinsic::UnknownGlobalValue,
        ));
        result.fact.roots.extend(
            ordinary_refs
                .into_iter()
                .map(
                    |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                        reference,
                        available_after,
                    },
                ),
        );
    }
    result
}

pub(super) fn read_callable_capture_dependencies(
    targets: &BTreeSet<u32>,
    context: &mut impl FactContext<ReceiverFlowKey, ReceiverFlowFact>,
    fact: &mut ReceiverFlowFact,
) {
    for function in targets {
        fact.callable_dependencies.insert(*function);
        if let Some(summary) = context.read(ReceiverFlowKey::Callable(*function)) {
            fact.capture_dependencies
                .extend(summary.capture_dependencies.iter().cloned());
        }
    }
}

impl IterativeRules for ReceiverFlowRules<'_> {
    type Key = ReceiverFlowKey;
    type Fact = ReceiverFlowFact;
    type Rule = ReceiverFlowRule;

    type MergeScratch = ();
    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        match rule {
            ReceiverFlowRule::Assignment(index) => {
                let (target, value) = self.assignments[*index];
                if self.index.ordinary_input_refs.contains_key(target) {
                    return;
                }
                let evaluated = evaluate_receiver_value(value, context, self);
                let mut fact = evaluated.fact;
                fact.complete = !evaluated.pending;
                let ordinary_refs: BTreeSet<_> = fact
                    .roots
                    .iter()
                    .filter_map(NativeReceiverRoot::cloned_ordinary_input)
                    .collect();
                if let Some(copy_owner) = self.index.stable_top_level_copy_owner(target, value) {
                    fact.roots = fact
                        .roots
                        .into_iter()
                        .map(|root| match root {
                            NativeReceiverRoot::OrdinaryDirect {
                                reference,
                                available_after,
                            } if copy_owner > available_after => {
                                NativeReceiverRoot::OrdinaryDirect {
                                    reference,
                                    available_after: copy_owner,
                                }
                            }
                            NativeReceiverRoot::OrdinaryDirect { .. } => {
                                NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                            }
                            NativeReceiverRoot::OrdinaryDescendant {
                                reference,
                                available_after,
                            } if copy_owner > available_after => {
                                NativeReceiverRoot::OrdinaryDescendant {
                                    reference,
                                    available_after,
                                }
                            }
                            NativeReceiverRoot::OrdinaryDescendant { .. } => {
                                NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                            }
                            other => other,
                        })
                        .collect();
                } else if !ordinary_refs.is_empty() {
                    fact.roots.insert(NativeReceiverRoot::Intrinsic(
                        NativeIntrinsic::UnknownGlobalValue,
                    ));
                    fact.roots.extend(ordinary_refs.into_iter().map(
                        |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        },
                    ));
                }
                context.propose(ReceiverFlowKey::Assignment(*index), fact);
            }
            ReceiverFlowRule::Binding(binding) => {
                let mut fact = ReceiverFlowFact::default();
                let mut complete = true;
                let mut has_producer = false;
                if let Some(seed) = context.read(ReceiverFlowKey::BindingSeed(binding.clone())) {
                    fact.merge(seed.clone());
                    complete &= seed.complete;
                    has_producer = true;
                }
                if let Some(indices) = self.assignment_indices.get(binding) {
                    for index in indices {
                        if let Some(producer) = context.read(ReceiverFlowKey::Assignment(*index)) {
                            fact.merge(producer.clone());
                            complete &= producer.complete;
                            has_producer = true;
                        } else {
                            // Keep partial roots and capture dependencies from
                            // ready producers, but do not treat one completed
                            // assignment as proof that every reaching write is
                            // closed.
                            complete = false;
                        }
                    }
                }
                fact.complete = has_producer && complete;
                context.propose(ReceiverFlowKey::Binding(binding.clone()), fact);
            }
            ReceiverFlowRule::ReturnSite(function, site) => {
                let Some(callable) = self.index.callables.get(function) else {
                    return;
                };
                let Some(value) = callable.body_facts.value_flow().returns().get(*site) else {
                    return;
                };
                let evaluated = evaluate_receiver_value(value, context, self);
                let mut outcomes = BTreeSet::new();
                if evaluated.fact.safe {
                    outcomes.insert(CallableReturnOutcome::Safe);
                }
                for root in evaluated.fact.roots {
                    outcomes.insert(match root {
                        NativeReceiverRoot::OrdinaryDirect {
                            reference,
                            available_after,
                        }
                        | NativeReceiverRoot::OrdinaryDescendant {
                            reference,
                            available_after,
                        } => CallableReturnOutcome::Descendant(reference, available_after),
                        NativeReceiverRoot::Intrinsic(root) => {
                            CallableReturnOutcome::Intrinsic(root)
                        }
                        // The captured-return summary has no exact native path
                        // guarantee. Keep non-constructor native returns opaque.
                        NativeReceiverRoot::IntrinsicPrototype(_)
                        | NativeReceiverRoot::IntrinsicDescendant(_)
                        | NativeReceiverRoot::ArrayIteratorPrototype
                        | NativeReceiverRoot::SetIteratorPrototype
                        | NativeReceiverRoot::BundleGlobal => CallableReturnOutcome::Unknown,
                    });
                }
                if evaluated.fact.opaque || !evaluated.fact.callable_targets.is_empty() {
                    outcomes.insert(CallableReturnOutcome::Unknown);
                }
                if outcomes.is_empty() && !evaluated.pending {
                    outcomes.insert(CallableReturnOutcome::Unknown);
                }
                context.propose(
                    ReceiverFlowKey::ReturnSite(*function, *site),
                    ReceiverFlowFact {
                        callable_dependencies: evaluated.fact.callable_dependencies,
                        capture_dependencies: evaluated.fact.capture_dependencies,
                        outcomes,
                        complete: !evaluated.pending,
                        ..ReceiverFlowFact::default()
                    },
                );
            }
            ReceiverFlowRule::Callable(function) => {
                let Some(callable) = self.index.callables.get(function) else {
                    return;
                };
                let mut outcomes = BTreeSet::new();
                let mut callable_dependencies = BTreeSet::new();
                let mut capture_dependencies = callable
                    .reads
                    .iter()
                    .filter_map(|binding| {
                        self.index.ordinary_input_refs.get(binding).map(
                            |(reference, available_after)| (reference.clone(), *available_after),
                        )
                    })
                    .collect::<BTreeSet<_>>();
                let mut complete = true;
                if callable.generator || self.index.async_callables.contains(function) {
                    outcomes.insert(CallableReturnOutcome::Unknown);
                } else if callable.body_facts.value_flow().returns().is_empty() {
                    outcomes.insert(CallableReturnOutcome::Safe);
                } else {
                    for site in 0..callable.body_facts.value_flow().returns().len() {
                        let Some(site_fact) =
                            context.read(ReceiverFlowKey::ReturnSite(*function, site))
                        else {
                            complete = false;
                            continue;
                        };
                        if site_fact.outcomes.is_empty() && !site_fact.complete {
                            complete = false;
                        }
                        outcomes.extend(site_fact.outcomes.iter().cloned());
                        callable_dependencies
                            .extend(site_fact.callable_dependencies.iter().copied());
                        capture_dependencies.extend(site_fact.capture_dependencies.iter().cloned());
                        complete &= site_fact.complete;
                    }
                    if callable.may_fall_through {
                        outcomes.insert(CallableReturnOutcome::Safe);
                    }
                }
                // Dependencies in returned call expressions are read from the
                // same worklist. Their capture facts wake wrappers and aliases
                // as summaries become available, including recursive SCCs.
                for dependency in &callable_dependencies {
                    if let Some(summary) = context.read(ReceiverFlowKey::Callable(*dependency)) {
                        capture_dependencies.extend(summary.capture_dependencies.iter().cloned());
                    }
                }
                if outcomes.is_empty() && complete {
                    outcomes.insert(CallableReturnOutcome::Unknown);
                }
                context.propose(
                    ReceiverFlowKey::Callable(*function),
                    ReceiverFlowFact {
                        callable_dependencies,
                        capture_dependencies,
                        outcomes,
                        opaque: callable.generator || self.index.async_callables.contains(function),
                        complete,
                        ..ReceiverFlowFact::default()
                    },
                );
            }
        }
    }

    fn merge(_: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        current.merge(contribution)
    }
}

pub(super) struct ReceiverFlowClosure {
    pub(super) bindings: BTreeMap<Id, NativeReceiverRoots>,
    pub(super) callable_returns: CallableReturnInputs,
    pub(super) facts: BTreeMap<ReceiverFlowKey, ReceiverFlowFact>,
}

pub(super) fn solve_receiver_flow(
    index: &ExecutionIndex,
    assignments: &[&(Id, ValueExpr)],
    flow: Option<&ChunkValueFlow>,
    unresolved_callable_fallbacks: &BTreeMap<u32, ReceiverFlowFact>,
) -> BTreeMap<ReceiverFlowKey, ReceiverFlowFact> {
    let mut producer_bindings = assignments
        .iter()
        .map(|(binding, _)| binding.clone())
        .chain(index.hoisted_bindings.keys().cloned())
        .chain(index.property_key_declarations.iter().cloned())
        .collect::<BTreeSet<_>>();
    producer_bindings.extend(index.ordinary_input_refs.keys().cloned());
    let binding_rules = producer_bindings
        .iter()
        .cloned()
        .map(ReceiverFlowRule::Binding)
        .collect::<Vec<_>>();
    let mut assignment_indices = BTreeMap::<Id, BTreeSet<usize>>::new();
    for (assignment_index, (binding, _)) in assignments.iter().enumerate() {
        if !index.ordinary_input_refs.contains_key(binding) {
            assignment_indices
                .entry(binding.clone())
                .or_default()
                .insert(assignment_index);
        }
    }
    let declared = index.declared_bindings();
    let rules = ReceiverFlowRules {
        assignments,
        assignment_indices: &assignment_indices,
        index,
        declared: &declared,
        pending_bindings: &producer_bindings,
    };
    let mut inference = IterativeInference::new(rules);
    let mut producers = (0..assignments.len())
        .map(ReceiverFlowRule::Assignment)
        .collect::<Vec<_>>();
    producers.extend(binding_rules);
    for (function, callable) in &index.callables {
        producers.extend(
            (0..callable.body_facts.value_flow().returns().len())
                .map(|site| ReceiverFlowRule::ReturnSite(*function, site)),
        );
        producers.push(ReceiverFlowRule::Callable(*function));
    }
    for input in &index.resolved_value_inputs {
        inference.seed(
            ReceiverFlowKey::BindingSeed(input.binding.clone()),
            ReceiverFlowFact {
                roots: BTreeSet::from([NativeReceiverRoot::OrdinaryDirect {
                    reference: input.evidence.reference.clone(),
                    available_after: input.initializer_ordinal,
                }]),
                complete: true,
                ..ReceiverFlowFact::default()
            },
        );
    }
    for (binding, function) in &index.hoisted_bindings {
        inference.seed(
            ReceiverFlowKey::BindingSeed(binding.clone()),
            ReceiverFlowFact {
                callable_targets: BTreeSet::from([*function]),
                complete: true,
                ..ReceiverFlowFact::default()
            },
        );
    }
    if let Some(flow) = flow {
        for binding in &index.property_key_declarations {
            let values = flow
                .values_of_binding(binding)
                .into_iter()
                .collect::<Vec<_>>();
            if let [Value::Alloc(Kind::Function, function)] = values.as_slice() {
                inference.seed(
                    ReceiverFlowKey::BindingSeed(binding.clone()),
                    ReceiverFlowFact {
                        callable_targets: BTreeSet::from([*function]),
                        complete: true,
                        ..ReceiverFlowFact::default()
                    },
                );
            }
        }
    }
    for (function, fallback) in unresolved_callable_fallbacks {
        inference.seed(ReceiverFlowKey::Callable(*function), fallback.clone());
    }
    inference.solve_values(producers)
}

pub(super) fn close_receiver_flow(
    index: &ExecutionIndex,
    assignments: &[&(Id, ValueExpr)],
    flow: Option<&ChunkValueFlow>,
    raw_aliases: &BTreeMap<Id, NativeReceiverRoots>,
) -> ReceiverFlowClosure {
    let initial_facts = solve_receiver_flow(index, assignments, flow, &BTreeMap::new());
    let selected_bindings = index
        .resolved_value_inputs
        .iter()
        .map(|input| input.binding.clone())
        .collect::<BTreeSet<_>>();
    let selected_refs = index
        .resolved_value_inputs
        .iter()
        .map(|input| input.evidence.reference.clone())
        .collect::<BTreeSet<_>>();
    let is_capture_connected = |fact: &ReceiverFlowFact| {
        fact.capture_dependencies
            .iter()
            .any(|(reference, _)| selected_refs.contains(reference))
    };
    // Capture connectivity is propagated by ReceiverFlowRules in the same
    // worklist as aliases and return summaries. Unrelated opaque helpers never
    // enter this projection or the conservative fallback below.
    let mut unresolved_callable_fallbacks = BTreeMap::new();
    for (function, callable) in &index.callables {
        let existing = initial_facts.get(&ReceiverFlowKey::Callable(*function));
        if !existing.is_some_and(is_capture_connected) {
            continue;
        }
        if existing.is_some_and(|fact| fact.complete) {
            continue;
        }
        // The first solve has reached quiescence. An absent or incomplete
        // capture-connected summary means its return flow is unresolved
        // (including a recursive bottom cycle), never a safe primitive.
        // Widen that same rule domain with an opaque outcome, retain partial
        // descendants/dependencies, and rerun all binding/callable watchers.
        let mut outcomes = BTreeSet::from([CallableReturnOutcome::Unknown]);
        let mut callable_dependencies = BTreeSet::new();
        let mut capture_dependencies = BTreeSet::new();
        if let Some(fact) = existing {
            outcomes.extend(fact.outcomes.iter().cloned());
            callable_dependencies.extend(fact.callable_dependencies.iter().copied());
            capture_dependencies.extend(fact.capture_dependencies.iter().cloned());
        }
        for site in 0..callable.body_facts.value_flow().returns().len() {
            if let Some(site_fact) =
                initial_facts.get(&ReceiverFlowKey::ReturnSite(*function, site))
            {
                outcomes.extend(site_fact.outcomes.iter().cloned());
                callable_dependencies.extend(site_fact.callable_dependencies.iter().copied());
                capture_dependencies.extend(site_fact.capture_dependencies.iter().cloned());
            }
        }
        unresolved_callable_fallbacks.insert(
            *function,
            ReceiverFlowFact {
                callable_dependencies,
                capture_dependencies,
                outcomes,
                opaque: true,
                complete: true,
                ..ReceiverFlowFact::default()
            },
        );
    }
    let facts = if unresolved_callable_fallbacks.is_empty() {
        initial_facts
    } else {
        solve_receiver_flow(index, assignments, flow, &unresolved_callable_fallbacks)
    };
    let bindings = facts
        .iter()
        .filter_map(|(key, fact)| match key {
            ReceiverFlowKey::Binding(binding) => {
                let mut roots = raw_aliases.get(binding).cloned().unwrap_or_default();
                let refined_roots = captured_roots(&fact.roots, &selected_refs);
                if selected_bindings.contains(binding) {
                    roots.clear();
                } else {
                    let has_refined_direct_identity = refined_roots.iter().any(|root| {
                        matches!(
                            root,
                            NativeReceiverRoot::OrdinaryDirect { reference, .. }
                                if selected_refs.contains(reference)
                        )
                    });
                    let stable_direct_copy = has_refined_direct_identity
                        && assignments.iter().any(|assignment| {
                            let (target, value) = *assignment;
                            target == binding
                                && index.stable_top_level_copy_owner(target, value).is_some()
                        });
                    if stable_direct_copy {
                        // The raw source-only alias records the original
                        // unknown-global possibility. A stable copy after the
                        // selected initializer is the same ordinary object, so
                        // retain its OrdinaryDirect timing fact without also
                        // charging the discharged raw root. Pre-capture copies
                        // and every descendant projection lack this proof.
                        roots.retain(|root| {
                            !matches!(
                                root,
                                NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                            )
                        });
                    }
                }
                roots.extend(refined_roots);
                (!roots.is_empty()).then_some((binding.clone(), roots))
            }
            _ => None,
        })
        .collect::<BTreeMap<_, _>>();
    let callable_roots = facts
        .iter()
        .filter_map(|(key, fact)| {
            let ReceiverFlowKey::Callable(function) = key else {
                return None;
            };
            if !is_capture_connected(fact) {
                return None;
            }
            let roots = outcomes_to_receiver_roots(&fact.outcomes, &fact.capture_dependencies);
            Some((*function, roots))
        })
        .collect::<BTreeMap<_, _>>();
    let by_binding = facts
        .iter()
        .filter_map(|(key, fact)| {
            let ReceiverFlowKey::Binding(binding) = key else {
                return None;
            };
            let roots = fact
                .callable_targets
                .iter()
                .filter_map(|function| callable_roots.get(function))
                .flatten()
                .cloned()
                .collect::<NativeReceiverRoots>();
            (!roots.is_empty()).then_some((binding.clone(), roots))
        })
        .collect();
    ReceiverFlowClosure {
        bindings,
        callable_returns: CallableReturnInputs {
            by_binding,
            by_origin: callable_roots,
        },
        facts,
    }
}

pub(super) fn captured_roots(
    roots: &NativeReceiverRoots,
    selected_refs: &BTreeSet<ValueInputRef>,
) -> NativeReceiverRoots {
    let has_selected_ref = roots.iter().any(|root| match root {
        NativeReceiverRoot::OrdinaryDirect { reference, .. }
        | NativeReceiverRoot::OrdinaryDescendant { reference, .. } => {
            selected_refs.contains(reference)
        }
        NativeReceiverRoot::Intrinsic(_)
        | NativeReceiverRoot::IntrinsicPrototype(_)
        | NativeReceiverRoot::IntrinsicDescendant(_)
        | NativeReceiverRoot::ArrayIteratorPrototype
        | NativeReceiverRoot::SetIteratorPrototype
        | NativeReceiverRoot::BundleGlobal => false,
    });
    roots
        .iter()
        .filter(|root| match root {
            NativeReceiverRoot::OrdinaryDirect { reference, .. }
            | NativeReceiverRoot::OrdinaryDescendant { reference, .. } => {
                selected_refs.contains(reference)
            }
            NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue) => has_selected_ref,
            NativeReceiverRoot::Intrinsic(_)
            | NativeReceiverRoot::IntrinsicPrototype(_)
            | NativeReceiverRoot::IntrinsicDescendant(_)
            | NativeReceiverRoot::ArrayIteratorPrototype
            | NativeReceiverRoot::SetIteratorPrototype
            | NativeReceiverRoot::BundleGlobal => false,
        })
        .cloned()
        .collect()
}

pub(super) fn outcomes_to_receiver_roots(
    outcomes: &BTreeSet<CallableReturnOutcome>,
    capture_dependencies: &BTreeSet<(ValueInputRef, SourceUnitOrdinal)>,
) -> NativeReceiverRoots {
    let mut roots = outcomes
        .iter()
        .filter_map(|outcome| match outcome {
            CallableReturnOutcome::Safe => None,
            CallableReturnOutcome::Descendant(reference, available_after) => {
                Some(NativeReceiverRoot::OrdinaryDescendant {
                    reference: reference.clone(),
                    available_after: *available_after,
                })
            }
            CallableReturnOutcome::Intrinsic(root) => Some(NativeReceiverRoot::Intrinsic(*root)),
            CallableReturnOutcome::Unknown => Some(NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::UnknownGlobalValue,
            )),
        })
        .collect::<NativeReceiverRoots>();
    if outcomes.contains(&CallableReturnOutcome::Unknown) {
        roots.extend(
            capture_dependencies
                .iter()
                .map(
                    |(reference, available_after)| NativeReceiverRoot::OrdinaryDescendant {
                        reference: reference.clone(),
                        available_after: *available_after,
                    },
                ),
        );
    }
    roots
}
