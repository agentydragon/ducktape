//! Bottom-up function effects over local and imported call obligations.
//!
//! Bodies are positive completion facts. Effect facts grow from their direct
//! witnesses as calls become effectful; an empty intermediate fact is never
//! projected as a purity certificate. The final projection happens only after
//! the shared worklist reaches closure.

use super::{Purity, PurityReason, PurityRule};
use crate::analysis_inputs::ProofDependencies;
use crate::inference::{FactContext, IterativeInference, IterativeRules};
use petgraph::algo::{tarjan_scc, toposort};
use petgraph::graph::DiGraph;
use petgraph::graphmap::DiGraphMap;
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use swc_common::Span;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) enum PurityMode {
    /// Per-chunk analysis, where chunk-local authored purity hints apply.
    Local,
    /// Export inference, which deliberately excludes chunk-local hints.
    Export,
}

/// Stable identity for a function body across modules and policy contexts.
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) struct PurityFunctionKey {
    pub(crate) module: String,
    pub(crate) binding: String,
    pub(crate) source_key: u32,
    pub(crate) mode: PurityMode,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) enum PurityKey {
    Body(PurityFunctionKey),
    Effects(PurityFunctionKey),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PurityBodyTransfer {
    pub(crate) events: BTreeMap<EffectEventKey, PurityEffectEvent>,
    pub(crate) dependencies: ProofDependencies,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) struct EffectEventKey {
    /// Explicit canonical classifier traversal order. Outer calls precede
    /// their arguments even though their spans contain those argument spans.
    pub(crate) order: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum PurityEffectEvent {
    Direct(PurityReason),
    Call {
        callee: PurityFunctionKey,
        reason: PurityReason,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PurityCallObligation {
    pub(crate) callee: PurityFunctionKey,
    pub(crate) name: String,
    pub(crate) span: Span,
    pub(crate) source_location: Option<crate::SourceLocation>,
}

impl PurityBodyTransfer {
    pub(crate) fn from_classification(
        purity: Purity,
        obligations: Vec<PurityCallObligation>,
    ) -> Self {
        let (direct_reasons, dependencies) = match purity {
            Purity::Pure { dependencies } => (Vec::new(), dependencies),
            Purity::NotPure { reasons } => (reasons, ProofDependencies::default()),
        };
        let mut events = Vec::with_capacity(direct_reasons.len() + obligations.len());
        for (index, reason) in direct_reasons.into_iter().enumerate() {
            events.push((reason.span, 0u8, index, PurityEffectEvent::Direct(reason)));
        }
        for (index, obligation) in obligations.into_iter().enumerate() {
            let mut reason = call_reason(&obligation.name, obligation.span);
            reason.source_location = obligation.source_location;
            events.push((
                obligation.span,
                1u8,
                index,
                PurityEffectEvent::Call {
                    callee: obligation.callee,
                    reason,
                },
            ));
        }
        // Composite classifier results are emitted in expression traversal
        // order. Sorting by source extent reconstructs that order for the
        // interleaving of direct effects and deferred calls (outer calls precede
        // calls nested in their arguments).
        events.sort_by_key(|(span, kind, index, _)| (span.lo.0, Reverse(span.hi.0), *kind, *index));
        let events = events
            .into_iter()
            .enumerate()
            .map(|(order, (_, _, _, event))| (EffectEventKey { order }, event))
            .collect();
        Self {
            events,
            dependencies,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PurityEffectFact {
    /// Reasons local to this function: direct witnesses and call-site
    /// witnesses whose callee has a non-empty effect fact.
    pub(crate) reasons: BTreeMap<EffectEventKey, PurityEffectReason>,
}

impl PurityEffectFact {
    pub(crate) fn is_effectful(&self) -> bool {
        !self.reasons.is_empty()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PurityEffectReason {
    pub(crate) reason: PurityReason,
    /// The inferred callee behind an `impure_function_call` witness. This is a
    /// key, not an Arc, so recursive effects can never create cyclic causes.
    pub(crate) callee: Option<PurityFunctionKey>,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) enum PurityRuleKey {
    Body(PurityFunctionKey),
    Effects(PurityFunctionKey),
}

/// Typed adapter seam for registering purity rules in a larger shared store.
/// A dispatcher can implement this by projecting its own enum key/fact types
/// onto these purity-specific variants.
pub(crate) trait PurityFactContext {
    fn read_purity(&mut self, key: PurityKey) -> Option<&PurityFact>;
    fn propose_purity(&mut self, key: PurityKey, contribution: PurityFact) -> bool;
    fn assume_purity(&mut self, dependencies: &ProofDependencies);
}

impl<C> PurityFactContext for C
where
    C: FactContext<PurityKey, PurityFact>,
{
    fn read_purity(&mut self, key: PurityKey) -> Option<&PurityFact> {
        self.read(key)
    }

    fn propose_purity(&mut self, key: PurityKey, contribution: PurityFact) -> bool {
        self.propose(key, contribution)
    }

    fn assume_purity(&mut self, dependencies: &ProofDependencies) {
        self.assume(dependencies)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum PurityFact {
    Body(Arc<PurityBodyTransfer>),
    Effects(PurityEffectFact),
}

#[derive(Clone, Debug, Default)]
pub(crate) struct PurityEffectRules {
    bodies: BTreeMap<PurityFunctionKey, Arc<PurityBodyTransfer>>,
}

impl PurityEffectRules {
    pub(crate) fn new(
        bodies: impl IntoIterator<Item = (PurityFunctionKey, PurityBodyTransfer)>,
    ) -> Self {
        let bodies: BTreeMap<_, _> = bodies
            .into_iter()
            .map(|(key, body)| (key, Arc::new(body)))
            .collect();
        for (caller, body) in &bodies {
            for event in body.events.values() {
                if let PurityEffectEvent::Call { callee, .. } = event {
                    assert!(
                        bodies.contains_key(callee),
                        "purity call obligation from {caller:?} has no completed body producer: {callee:?}"
                    );
                }
            }
        }
        Self { bodies }
    }

    /// Rules to register in a shared growing-fact store. Body facts are
    /// positive completion certificates; each Effects rule then joins direct
    /// effects and grows call-site effects as callees become effectful.
    pub(crate) fn producers(&self) -> Vec<PurityRuleKey> {
        self.bodies
            .keys()
            .flat_map(|key| {
                [
                    PurityRuleKey::Body(key.clone()),
                    PurityRuleKey::Effects(key.clone()),
                ]
            })
            .collect()
    }

    pub(crate) fn evaluate(&self, rule: &PurityRuleKey, context: &mut impl PurityFactContext) {
        match rule {
            PurityRuleKey::Body(key) => {
                let Some(body) = self.bodies.get(key) else {
                    return;
                };
                context.assume_purity(&body.dependencies);
                context.propose_purity(
                    PurityKey::Body(key.clone()),
                    PurityFact::Body(Arc::clone(body)),
                );
            }
            PurityRuleKey::Effects(key) => {
                let Some(PurityFact::Body(body)) =
                    context.read_purity(PurityKey::Body(key.clone())).cloned()
                else {
                    return;
                };
                let mut reasons = BTreeMap::new();
                for (event_key, event) in &body.events {
                    match event {
                        PurityEffectEvent::Direct(reason) => {
                            reasons.insert(
                                event_key.clone(),
                                PurityEffectReason {
                                    reason: reason.clone(),
                                    callee: None,
                                },
                            );
                        }
                        PurityEffectEvent::Call { callee, reason } => {
                            if matches!(
                                context.read_purity(PurityKey::Effects(callee.clone())),
                                Some(PurityFact::Effects(fact)) if fact.is_effectful()
                            ) {
                                reasons.insert(
                                    event_key.clone(),
                                    PurityEffectReason {
                                        reason: reason.clone(),
                                        callee: Some(callee.clone()),
                                    },
                                );
                            }
                        }
                    }
                }
                context.propose_purity(
                    PurityKey::Effects(key.clone()),
                    PurityFact::Effects(PurityEffectFact { reasons }),
                );
            }
        }
    }

    /// Compatibility path for one module. The same typed rules are used by
    /// callers that register them in a larger shared inference universe.
    pub(crate) fn solve_to_purities(self) -> BTreeMap<PurityFunctionKey, Arc<Purity>> {
        let producers = self.producers();
        let keys = self.bodies.keys().cloned().collect::<Vec<_>>();
        let solved = IterativeInference::new(self).solve(producers);
        project_purity_effects(keys, |key| {
            let judgment = solved.get(&PurityKey::Effects(key.clone()))?;
            let PurityFact::Effects(effect) = judgment.value() else {
                return None;
            };
            Some((effect.clone(), judgment.dependencies().clone()))
        })
    }
}

impl IterativeRules for PurityEffectRules {
    type Key = PurityKey;
    type Fact = PurityFact;
    type Rule = PurityRuleKey;

    fn evaluate(&self, rule: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        PurityEffectRules::evaluate(self, rule, context)
    }

    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        match (key, current, contribution) {
            (PurityKey::Body(_), PurityFact::Body(current), PurityFact::Body(contribution)) => {
                assert_eq!(
                    current.as_ref(),
                    contribution.as_ref(),
                    "body transfer is immutable"
                );
                false
            }
            (
                PurityKey::Effects(_),
                PurityFact::Effects(current),
                PurityFact::Effects(contribution),
            ) => {
                let before = current.reasons.len();
                for (key, reason) in contribution.reasons {
                    current.reasons.entry(key).or_insert(reason);
                }
                current.reasons.len() != before
            }
            _ => unreachable!("purity producer proposed a fact for the wrong key"),
        }
    }
}

/// Convert a saturated effect store to the established immutable Purity API.
/// Cause edges only point toward a strictly shorter route to a direct witness,
/// so recursive call SCCs produce a finite acyclic Arc cause graph.
pub(crate) fn project_purity_effects(
    keys: impl IntoIterator<Item = PurityFunctionKey>,
    mut effects_for: impl FnMut(&PurityFunctionKey) -> Option<(PurityEffectFact, ProofDependencies)>,
) -> BTreeMap<PurityFunctionKey, Arc<Purity>> {
    let mut effects = BTreeMap::new();
    let mut dependencies = BTreeMap::new();
    for key in keys {
        let Some((effect, deps)) = effects_for(&key) else {
            continue;
        };
        effects.insert(key.clone(), effect);
        dependencies.insert(key, deps);
    }
    project_effect_maps(&effects, &dependencies)
}

fn project_effect_maps(
    effects: &BTreeMap<PurityFunctionKey, PurityEffectFact>,
    dependencies: &BTreeMap<PurityFunctionKey, ProofDependencies>,
) -> BTreeMap<PurityFunctionKey, Arc<Purity>> {
    let mut reverse_callers = BTreeMap::<PurityFunctionKey, BTreeSet<PurityFunctionKey>>::new();
    let mut callees_by_caller = BTreeMap::<PurityFunctionKey, BTreeSet<PurityFunctionKey>>::new();
    let mut direct_witnesses = BTreeSet::new();
    let mut distances = BTreeMap::new();
    let mut pending = VecDeque::new();
    for (key, effect) in effects {
        if effect
            .reasons
            .values()
            .any(|reason| reason.callee.is_none())
        {
            direct_witnesses.insert(key.clone());
            distances.insert(key.clone(), 0usize);
            pending.push_back(key.clone());
        }
        for reason in effect.reasons.values() {
            if let Some(callee) = &reason.callee {
                callees_by_caller
                    .entry(key.clone())
                    .or_default()
                    .insert(callee.clone());
                reverse_callers
                    .entry(callee.clone())
                    .or_default()
                    .insert(key.clone());
            }
        }
    }
    let mut reaches_direct_witness = direct_witnesses.clone();
    while let Some(callee) = pending.pop_front() {
        let next_distance = distances[&callee] + 1;
        for caller in reverse_callers.get(&callee).into_iter().flatten() {
            if reaches_direct_witness.insert(caller.clone()) {
                pending.push_back(caller.clone());
            }
            if distances
                .get(caller)
                .is_none_or(|distance| next_distance < *distance)
            {
                distances.insert(caller.clone(), next_distance);
                pending.push_back(caller.clone());
            }
        }
    }
    for (key, effect) in effects {
        assert!(
            !effect.is_effectful() || reaches_direct_witness.contains(key),
            "effectful purity fact for {key:?} has no direct source witness"
        );
    }

    // Project callees before callers across SCC boundaries. Inside one
    // recursive component, keep only links that decrease the distance to a
    // direct witness. Those links explain recursive callers without making
    // the public Arc diagnostic graph cyclic.
    let keys: Vec<_> = effects.keys().cloned().collect();
    let mut call_graph = DiGraph::<(), ()>::new();
    let node_by_key: BTreeMap<_, _> = keys
        .iter()
        .cloned()
        .map(|key| (key, call_graph.add_node(())))
        .collect();
    for (caller, callees) in &callees_by_caller {
        for callee in callees {
            call_graph.add_edge(node_by_key[caller], node_by_key[callee], ());
        }
    }
    let components = tarjan_scc(&call_graph);
    let mut component_by_node = vec![0; keys.len()];
    let mut keys_by_component = vec![Vec::new(); components.len()];
    for (component_index, nodes) in components.iter().enumerate() {
        for node in nodes {
            component_by_node[node.index()] = component_index;
            keys_by_component[component_index].push(keys[node.index()].clone());
        }
        keys_by_component[component_index].sort_by(|left, right| {
            distances
                .get(left)
                .cmp(&distances.get(right))
                .then_with(|| left.cmp(right))
        });
    }
    let mut component_graph = DiGraphMap::<usize, ()>::new();
    for component in 0..components.len() {
        component_graph.add_node(component);
    }
    for (caller, callees) in &callees_by_caller {
        let caller_component = component_by_node[node_by_key[caller].index()];
        for callee in callees {
            let callee_component = component_by_node[node_by_key[callee].index()];
            if caller_component != callee_component {
                component_graph.add_edge(callee_component, caller_component, ());
            }
        }
    }
    let component_order = toposort(&component_graph, None).expect("SCC condensation is acyclic");
    let mut projected = BTreeMap::new();
    for component in component_order {
        for key in &keys_by_component[component] {
            let effect = &effects[key];
            if !effect.is_effectful() {
                projected.insert(
                    key.clone(),
                    Arc::new(
                        Purity::pure()
                            .with_dependencies(&dependencies.get(key).cloned().unwrap_or_default()),
                    ),
                );
                continue;
            }
            let mut reasons = Vec::with_capacity(effect.reasons.len());
            for effect_reason in effect.reasons.values() {
                let mut reason = effect_reason.reason.clone();
                if let Some(callee) = &effect_reason.callee {
                    let caller_component = component_by_node[node_by_key[key].index()];
                    let callee_component = component_by_node[node_by_key[callee].index()];
                    if caller_component != callee_component
                        || distances.get(callee) < distances.get(key)
                    {
                        reason.cause = Some(
                            projected
                                .get(callee)
                                .expect("callee SCC is projected before caller SCC")
                                .clone(),
                        );
                    }
                }
                reasons.push(reason);
            }
            projected.insert(key.clone(), Arc::new(Purity::NotPure { reasons }));
        }
    }
    projected
}

pub(crate) fn call_reason(name: &str, span: Span) -> PurityReason {
    PurityReason::new(
        PurityRule::ImpureFunctionCall,
        span,
        Some(format!("function `{name}` has an impure body")),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ValueInputRef;
    use swc_common::{BytePos, DUMMY_SP};

    fn function(module: &str, binding: &str, source_key: u32) -> PurityFunctionKey {
        PurityFunctionKey {
            module: module.to_string(),
            binding: binding.to_string(),
            source_key,
            mode: PurityMode::Export,
        }
    }

    fn direct_reason() -> PurityEffectReason {
        PurityEffectReason {
            reason: PurityReason::new(PurityRule::UnknownCall, DUMMY_SP, None),
            callee: None,
        }
    }

    fn call_effect(callee: PurityFunctionKey) -> PurityEffectReason {
        PurityEffectReason {
            reason: call_reason(&callee.binding, DUMMY_SP),
            callee: Some(callee),
        }
    }

    #[test]
    fn pure_callee_dependencies_flow_through_a_call_obligation() {
        let caller = function("app", "wrapper", 1);
        let callee = function("provider", "make", 2);
        let dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: "provider.js".to_string(),
                input_index: 4,
            }]),
        };
        let caller_body = PurityBodyTransfer {
            events: BTreeMap::from([(
                EffectEventKey { order: 0 },
                PurityEffectEvent::Call {
                    callee: callee.clone(),
                    reason: call_reason("make", DUMMY_SP),
                },
            )]),
            dependencies: ProofDependencies::default(),
        };
        let callee_body = PurityBodyTransfer {
            events: BTreeMap::new(),
            dependencies: dependencies.clone(),
        };
        let solved =
            PurityEffectRules::new([(caller.clone(), caller_body), (callee.clone(), callee_body)])
                .solve_to_purities();

        assert!(solved[&caller].is_pure());
        assert_eq!(solved[&caller].dependencies(), Some(&dependencies));
    }

    #[test]
    fn event_ordinals_keep_outer_calls_before_nested_argument_calls() {
        let outer = function("m", "outer", 1);
        let inner = function("m", "inner", 2);
        let transfer = PurityBodyTransfer::from_classification(
            Purity::pure(),
            vec![
                PurityCallObligation {
                    callee: inner,
                    name: "inner".to_string(),
                    span: Span::new(BytePos(10), BytePos(20)),
                    source_location: None,
                },
                PurityCallObligation {
                    callee: outer,
                    name: "outer".to_string(),
                    span: Span::new(BytePos(1), BytePos(30)),
                    source_location: None,
                },
            ],
        );

        let names: Vec<_> = transfer
            .events
            .values()
            .map(|event| match event {
                PurityEffectEvent::Call { reason, .. } => reason.detail.as_deref().unwrap(),
                PurityEffectEvent::Direct(_) => panic!("fixture has no direct effects"),
            })
            .collect();
        assert_eq!(
            names,
            [
                "function `outer` has an impure body",
                "function `inner` has an impure body"
            ]
        );
    }

    #[test]
    fn cyclic_causes_keep_only_witness_decreasing_links() {
        let a = function("m", "a", 1);
        let b = function("m", "b", 2);
        let c = function("m", "c", 3);
        let effects = BTreeMap::from([
            (
                a.clone(),
                PurityEffectFact {
                    reasons: BTreeMap::from([(
                        EffectEventKey { order: 0 },
                        call_effect(b.clone()),
                    )]),
                },
            ),
            (
                b.clone(),
                PurityEffectFact {
                    reasons: BTreeMap::from([
                        (EffectEventKey { order: 0 }, call_effect(a.clone())),
                        (EffectEventKey { order: 1 }, direct_reason()),
                    ]),
                },
            ),
            (
                c.clone(),
                PurityEffectFact {
                    reasons: BTreeMap::from([
                        (EffectEventKey { order: 0 }, call_effect(a.clone())),
                        (EffectEventKey { order: 1 }, direct_reason()),
                    ]),
                },
            ),
        ]);
        let dependencies = effects
            .keys()
            .cloned()
            .map(|key| (key, ProofDependencies::default()))
            .collect();
        let projected = project_effect_maps(&effects, &dependencies);

        let Purity::NotPure { reasons: a_reasons } = projected[&a].as_ref() else {
            panic!("a should be effectful through b");
        };
        let Purity::NotPure { reasons: b_reasons } = projected[&b].as_ref() else {
            panic!("b has a direct effect");
        };
        let Purity::NotPure { reasons: c_reasons } = projected[&c].as_ref() else {
            panic!("c has a direct effect");
        };
        assert!(
            a_reasons[0].cause.is_some(),
            "a points to b, which has the direct witness"
        );
        assert!(
            b_reasons[0].cause.is_none(),
            "b must not point back to the more distant a"
        );
        assert!(
            c_reasons[0].cause.is_some(),
            "cross-component cause should remain even when c has its own direct effect"
        );

        fn assert_acyclic(
            purity: &Arc<Purity>,
            active: &mut BTreeSet<usize>,
            visited: &mut BTreeSet<usize>,
        ) {
            let pointer = Arc::as_ptr(purity) as usize;
            assert!(active.insert(pointer), "cause graph contains a cycle");
            if visited.insert(pointer)
                && let Purity::NotPure { reasons } = purity.as_ref()
            {
                for reason in reasons {
                    if let Some(cause) = &reason.cause {
                        assert_acyclic(cause, active, visited);
                    }
                }
            }
            active.remove(&pointer);
        }
        for purity in projected.values() {
            assert_acyclic(purity, &mut BTreeSet::new(), &mut BTreeSet::new());
        }
    }
}
