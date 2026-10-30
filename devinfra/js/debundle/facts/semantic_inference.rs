//! Positive semantic rules over one completed source/native census.
//!
//! Syntax candidates are not certificates. The queue derives certificates only
//! after their mandatory premises have been proved, and unions their captured
//! input dependencies. Purity, timing and heap ordering share this same store.
//! Native premises are not keys: a rule reads the callee's binding identity and
//! the integrity flags of the slots it needs (`abstract_domain::integrity`).

use super::abstract_domain::builtins::Row;
use super::abstract_domain::integrity::{Integrity, IntegrityFlags, IntrinsicSlot};
use super::abstract_domain::value::{Kind, Value};
use super::callable_inference::{CallableInferenceInputs, FunctionBindingFacts};
use super::local_class_construction::LocalClassConstructionShapes;
use super::set_iterables::SetIterableShapes;
use super::statement_facts::StructuralStatementFacts;
use super::value_flow::ChunkValueFlow;
use crate::analysis_inputs::{
    ArgumentInputRef, DescriptorCopyAuthorRef, DescriptorCopyFactorySummary, NativeIntrinsic,
    ProofDependencies, ResolvedArgumentInput,
};
use crate::inference::{
    CertificateContext, FactContext, GuardedContext, IterativeFacts, IterativeInference,
    IterativeRules, Judgment,
};
use crate::reports::schema::{InferenceRule, InferenceUse};
use spec::DescriptorCopyFactorySignature;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum SemanticKey {
    LocalClass(u32),
    PrimitiveSetIteration(u32),
    StablePrimitiveArraySpread(u32),
    StableFunction {
        binding: Id,
        function: u32,
    },
    StableProvider {
        binding: Id,
        function: u32,
    },
    ArgumentNoEscape {
        binding: Id,
        function: u32,
        parameter_index: usize,
        exact_arity: usize,
    },
    NamespaceWrapper {
        binding: Id,
        function: u32,
    },
    DescriptorCopyProvider(Id),
    DescriptorCopyCall(u32),
}

impl SemanticKey {
    fn rule(&self) -> InferenceRule {
        match self {
            Self::LocalClass(_) => InferenceRule::LocalClassConstruction,
            Self::PrimitiveSetIteration(_) => InferenceRule::PrimitiveSetIteration,
            Self::StablePrimitiveArraySpread(_) => InferenceRule::StablePrimitiveArraySpread,
            Self::StableFunction { .. } => InferenceRule::StableFunction,
            Self::StableProvider { .. } => InferenceRule::StableProvider,
            Self::ArgumentNoEscape { .. } => InferenceRule::ArgumentNoEscape,
            Self::NamespaceWrapper { .. } => InferenceRule::NamespaceWrapper,
            Self::DescriptorCopyProvider(_) => InferenceRule::DescriptorCopyProvider,
            Self::DescriptorCopyCall(_) => InferenceRule::DescriptorCopyFactory,
        }
    }
}

/// A positive proposition with no cross-domain "safe" flag. The key names the
/// exact capability; its presence is the certificate.
#[derive(Debug, Clone)]
pub(crate) enum SemanticFact {
    Certificate,
    Function(u32),
    NamespaceWrapper(u32),
}

/// Binding-resolution facts captured alongside the canonical source analysis.
#[derive(Debug, Clone, Default)]
pub(crate) struct NativeBindingContext {
    pub(crate) declared_bindings: BTreeSet<Id>,
    pub(crate) shadowed_globals: BTreeSet<&'static str>,
    pub(crate) global_object_names: BTreeSet<&'static str>,
}

/// The source spelling of a native callee: a global binding, or a member of an
/// unshadowed `globalThis`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct NativeCallee {
    pub(crate) binding: Id,
    pub(crate) global_this_qualified: bool,
}

/// `Env(binding) = Intrinsic(family)`: the binding is neither declared nor
/// shadowed and names the family's global, or is the unshadowed `globalThis`
/// qualifying it. Integrity of the slots it reads is a separate premise.
pub(crate) fn names_intrinsic(
    callee: &NativeCallee,
    family: NativeIntrinsic,
    declared: bool,
    shadowed_globals: &BTreeSet<&'static str>,
    global_object_names: &BTreeSet<&'static str>,
) -> bool {
    let name = callee.binding.0.as_ref();
    !declared
        && !shadowed_globals.contains(name)
        && if callee.global_this_qualified {
            name == "globalThis" && global_object_names.contains("globalThis")
        } else {
            NativeIntrinsic::for_global_binding(name) == Some(family)
        }
}

/// A consumed native premise: its typed dependencies and report entry.
#[derive(Debug, Clone)]
pub(crate) struct NativePremise {
    pub(crate) dependencies: ProofDependencies,
    pub(crate) inference_use: InferenceUse,
    /// The census, not an author axiom, established it.
    pub(crate) census: bool,
}

/// Resolve `slots` at `site` into a premise reported as `conclusion`. A census
/// proof derives it through native integrity; an author axiom names its input.
pub(crate) fn native_premise(
    integrity: &IntegrityFlags,
    slots: &[IntrinsicSlot],
    site: Option<u32>,
    conclusion: InferenceRule,
) -> Option<NativePremise> {
    let resolved = integrity.require(slots, site);
    let dependencies = resolved.dependencies()?;
    let census = matches!(resolved, Integrity::Intact(_));
    Some(NativePremise {
        inference_use: InferenceUse {
            conclusion,
            derivation_rules: [conclusion]
                .into_iter()
                .chain(census.then_some(InferenceRule::NativeIntegrity))
                .collect(),
            value_input_refs: dependencies.value_inputs.clone(),
            descriptor_copy_author_refs: BTreeSet::new(),
            argument_input_refs: BTreeSet::new(),
            argument_input_spec_locations: BTreeSet::new(),
            native_operation_input_refs: dependencies.native_operation_inputs.clone(),
        },
        census,
        dependencies,
    })
}

/// Native premises a semantic rule consults. Each records what it used in
/// `consulted` so the derivation report names the same rules as the proof.
#[derive(Debug, Default)]
pub(crate) struct NativePremises {
    pub(crate) integrity: IntegrityFlags,
    pub(crate) bindings: NativeBindingContext,
}

impl NativePremises {
    /// Every slot intact (or asserted at `site`).
    pub(crate) fn require_slots(
        &self,
        slots: &[IntrinsicSlot],
        site: Option<u32>,
        context: &mut impl CertificateContext<SemanticKey, SemanticFact>,
        consulted: &mut BTreeSet<InferenceRule>,
    ) -> Option<()> {
        let resolved = self.integrity.require(slots, site);
        context.assume(&resolved.dependencies()?);
        if matches!(resolved, Integrity::Intact(_)) {
            consulted.insert(InferenceRule::NativeIntegrity);
        }
        Some(())
    }

    /// `callee` names the original `family` global and `slots` are intact.
    pub(crate) fn require_binding(
        &self,
        callee: &NativeCallee,
        family: NativeIntrinsic,
        slots: &[IntrinsicSlot],
        site: Option<u32>,
        context: &mut impl CertificateContext<SemanticKey, SemanticFact>,
        consulted: &mut BTreeSet<InferenceRule>,
    ) -> Option<()> {
        if !names_intrinsic(
            callee,
            family,
            self.bindings.declared_bindings.contains(&callee.binding),
            &self.bindings.shadowed_globals,
            &self.bindings.global_object_names,
        ) {
            return None;
        }
        let resolved = self.integrity.require(slots, site);
        context.assume(&resolved.dependencies()?);
        if matches!(resolved, Integrity::Intact(_)) {
            consulted.extend([
                InferenceRule::NativeCallIdentity,
                InferenceRule::NativeIntegrity,
            ]);
        }
        Some(())
    }

    /// A zero-argument `new binding()` of a keyed collection: a fresh, empty
    /// allocation that runs no user code.
    pub(crate) fn require_empty_collection(
        &self,
        row: &Row,
        binding: &Id,
        site: Option<u32>,
        context: &mut impl CertificateContext<SemanticKey, SemanticFact>,
        consulted: &mut BTreeSet<InferenceRule>,
    ) -> Option<()> {
        self.require_binding(
            &NativeCallee {
                binding: binding.clone(),
                global_this_qualified: false,
            },
            row.family()?,
            row.requires,
            site,
            context,
            consulted,
        )?;
        consulted.insert(InferenceRule::EmptyCollectionConstruction);
        Some(())
    }
}

/// Source-only candidate. The rule checks the authored provider, exact keys,
/// and the completed census before this becomes a certificate.
#[derive(Debug, Clone)]
pub(crate) struct DescriptorCopyCallCandidate {
    pub(crate) source_object: u32,
    pub(crate) provider_binding: Id,
    pub(crate) signature: DescriptorCopyFactorySignature,
    pub(crate) data_keys: BTreeSet<String>,
    pub(crate) accessor_keys: BTreeSet<String>,
}

/// Native-premise rules each certificate consulted, beyond its key premises.
type Consulted = Rc<RefCell<BTreeMap<SemanticKey, BTreeSet<InferenceRule>>>>;

/// Immutable inputs from the canonical structural analysis and completed native
/// census. A new source, policy or author-input context needs a new instance.
#[derive(Debug, Default)]
pub(crate) struct SemanticRules {
    pub(crate) classes: LocalClassConstructionShapes,
    pub(crate) sets: SetIterableShapes,
    /// Fresh-allocation candidates: the empty keyed-collection row each binding
    /// constructs when it is the original native.
    pub(crate) collection_bindings: BTreeMap<Id, &'static Row>,
    pub(crate) native: NativePremises,
    pub(crate) dynamic_scope: bool,
    pub(crate) callables: CallableInferenceInputs,
    pub(crate) descriptor_copy_providers: BTreeMap<Id, DescriptorCopyFactorySummary>,
    pub(crate) descriptor_copy_calls: BTreeMap<u32, DescriptorCopyCallCandidate>,
    pub(crate) argument_inputs: BTreeMap<(Id, u32, usize, usize), ArgumentInputRef>,
    pub(crate) array_spreads: BTreeSet<u32>,
    /// Filled while solving; a caller leaves it at its default.
    pub(crate) consulted: Consulted,
}

impl SemanticRules {
    fn derive(
        &self,
        key: &SemanticKey,
        context: &mut impl CertificateContext<SemanticKey, SemanticFact>,
        consulted: &mut BTreeSet<InferenceRule>,
    ) -> Option<SemanticFact> {
        match key {
            SemanticKey::LocalClass(class) => {
                for (binding, site) in self.classes.classes.get(class)? {
                    let row = self.collection_bindings.get(binding)?;
                    self.native.require_empty_collection(
                        row,
                        binding,
                        Some(*site),
                        context,
                        consulted,
                    )?;
                }
            }
            SemanticKey::PrimitiveSetIteration(span) => {
                if !self.sets.constructor_starts.contains(span) {
                    return None;
                }
                self.native.require_slots(
                    &[
                        IntrinsicSlot::Global(NativeIntrinsic::Set),
                        IntrinsicSlot::Prototype(NativeIntrinsic::Set),
                        IntrinsicSlot::Prototype(NativeIntrinsic::Array),
                        IntrinsicSlot::ArrayIteratorPrototype,
                    ],
                    None,
                    context,
                    consulted,
                )?;
            }
            SemanticKey::DescriptorCopyProvider(binding) => {
                let census = self.native.integrity.closed_census()?;
                if self.dynamic_scope {
                    return None;
                }
                let provider = self.descriptor_copy_providers.get(binding)?;
                if provider.signatures.len() != 1 {
                    return None;
                }
                // Exact-slot contradiction checks consume the completed raw
                // census, including any authored captured-value refinements.
                context.assume(census);
                context.assume(&ProofDependencies {
                    descriptor_copy_authors: BTreeSet::from([DescriptorCopyAuthorRef {
                        defining_module: provider.defining_module.clone(),
                        export: provider.export.clone(),
                        spec_location: provider.author_location.clone(),
                    }]),
                    ..Default::default()
                });
            }
            SemanticKey::DescriptorCopyCall(span) => {
                let candidate = self.descriptor_copy_calls.get(span)?;
                let provider = self
                    .descriptor_copy_providers
                    .get(&candidate.provider_binding)?;
                if !provider.signatures.contains(&candidate.signature)
                    || candidate.signature.argument_count != 1
                    || candidate.signature.object_argument != 0
                    || candidate.source_object == 0
                    || !candidate
                        .signature
                        .eager_own_data_keys
                        .iter()
                        .all(|key| candidate.data_keys.contains(key))
                    || candidate
                        .signature
                        .assigned_data_keys
                        .iter()
                        .any(|key| candidate.accessor_keys.contains(key))
                {
                    return None;
                }
                context.require(SemanticKey::DescriptorCopyProvider(
                    candidate.provider_binding.clone(),
                ))?;
            }
            SemanticKey::StablePrimitiveArraySpread(initializer) => {
                if !self.array_spreads.contains(initializer) || self.dynamic_scope {
                    return None;
                }
                self.native.require_slots(
                    &[
                        IntrinsicSlot::Prototype(NativeIntrinsic::Array),
                        IntrinsicSlot::ArrayIteratorPrototype,
                    ],
                    Some(*initializer),
                    context,
                    consulted,
                )?;
            }
            SemanticKey::ArgumentNoEscape {
                binding,
                function,
                parameter_index,
                exact_arity,
            } => {
                if self.dynamic_scope {
                    return None;
                }
                let reference = self.argument_inputs.get(&(
                    binding.clone(),
                    *function,
                    *parameter_index,
                    *exact_arity,
                ))?;
                context.require(SemanticKey::StableFunction {
                    binding: binding.clone(),
                    function: *function,
                })?;
                let source = self.callables.functions.get(binding)?;
                if source.rebound {
                    return None;
                }
                context.assume(&ProofDependencies {
                    argument_inputs: BTreeSet::from([reference.clone()]),
                    ..ProofDependencies::default()
                });
                return Some(SemanticFact::Certificate);
            }
            SemanticKey::StableFunction { .. }
            | SemanticKey::StableProvider { .. }
            | SemanticKey::NamespaceWrapper { .. } => {
                if self.dynamic_scope {
                    return None;
                }
                return self.callables.evaluate(
                    key,
                    context,
                    &self.native,
                    &self.collection_bindings,
                    consulted,
                );
            }
        }
        Some(SemanticFact::Certificate)
    }
}

impl IterativeRules for SemanticRules {
    type Key = SemanticKey;
    type Fact = SemanticFact;
    type Rule = SemanticKey;

    fn evaluate(&self, key: &Self::Rule, context: &mut impl FactContext<Self::Key, Self::Fact>) {
        let mut guarded = GuardedContext::new(context);
        let mut consulted = BTreeSet::new();
        let result = self.derive(key, &mut guarded, &mut consulted);
        if let Some(fact) = guarded.finish(result) {
            if !consulted.is_empty() {
                self.consulted
                    .borrow_mut()
                    .entry(key.clone())
                    .or_default()
                    .extend(consulted);
            }
            context.propose(key.clone(), fact);
        }
    }

    fn merge(_: &Self::Key, _: &mut Self::Fact, _: Self::Fact) -> bool {
        // These are immutable certificates over one source/native context;
        // only their consumed proof dependencies can grow.
        false
    }
}

/// Immutable facts from a single completed semantic context.
#[derive(Debug)]
pub(crate) struct SemanticFacts {
    inference: IterativeFacts<SemanticKey, SemanticFact>,
    consulted: BTreeMap<SemanticKey, BTreeSet<InferenceRule>>,
    constructions: BTreeMap<u32, u32>,
}

impl Default for SemanticFacts {
    fn default() -> Self {
        let rules = SemanticRules {
            // A context without a completed census cannot prove native slots.
            dynamic_scope: true,
            ..SemanticRules::default()
        };
        Self::solve(rules)
    }
}

impl SemanticFacts {
    pub(crate) fn solve(rules: SemanticRules) -> Self {
        Self::solve_with_source(rules, None)
    }

    pub(crate) fn solve_with_source(
        mut rules: SemanticRules,
        source: Option<&SemanticFacts>,
    ) -> Self {
        let constructions = std::mem::take(&mut rules.classes.constructions);
        if let Some(source) = source {
            rules.consulted = Rc::new(RefCell::new(source.consulted.clone()));
        }
        let consulted = Rc::clone(&rules.consulted);
        let roots: Vec<_> = rules
            .callables
            .keys()
            .into_iter()
            .chain(rules.argument_inputs.keys().map(
                |(binding, function, parameter_index, exact_arity)| SemanticKey::ArgumentNoEscape {
                    binding: binding.clone(),
                    function: *function,
                    parameter_index: *parameter_index,
                    exact_arity: *exact_arity,
                },
            ))
            .chain(
                rules
                    .descriptor_copy_providers
                    .keys()
                    .cloned()
                    .map(SemanticKey::DescriptorCopyProvider),
            )
            .chain(
                rules
                    .descriptor_copy_calls
                    .keys()
                    .copied()
                    .map(SemanticKey::DescriptorCopyCall),
            )
            .chain(
                rules
                    .classes
                    .classes
                    .keys()
                    .copied()
                    .map(SemanticKey::LocalClass),
            )
            .chain(
                rules
                    .sets
                    .constructor_starts
                    .iter()
                    .copied()
                    .map(SemanticKey::PrimitiveSetIteration),
            )
            .chain(
                rules
                    .array_spreads
                    .iter()
                    .copied()
                    .map(SemanticKey::StablePrimitiveArraySpread),
            )
            .collect();
        let inference = match source {
            Some(source) => {
                IterativeInference::from_completed(rules, source.inference.clone()).solve(roots)
            }
            None => IterativeInference::new(rules).solve(roots),
        };
        let consulted = Rc::try_unwrap(consulted)
            .expect("the solved rules released the shared derivation record")
            .into_inner();
        Self {
            inference,
            consulted,
            constructions,
        }
    }

    pub(super) fn class_for_construction(&self, span: Span) -> Option<u32> {
        self.constructions.get(&span.lo.0).copied()
    }

    pub(crate) fn judgment(
        &self,
        key: &SemanticKey,
    ) -> Option<&Judgment<SemanticKey, SemanticFact>> {
        self.inference.get(key)
    }

    pub(crate) fn dependencies(&self, key: &SemanticKey) -> Option<&ProofDependencies> {
        self.inference.get(key).map(Judgment::dependencies)
    }

    /// Expand shared premise IDs only for a consumed diagnostic. The analysis
    /// stores no recursively copied derivation trees.
    pub(crate) fn inference_use(&self, key: &SemanticKey) -> Option<InferenceUse> {
        let dependencies = self.dependencies(key)?;
        let mut pending = vec![key.clone()];
        let mut visited = BTreeSet::new();
        let mut derivation_rules = BTreeSet::new();
        while let Some(key) = pending.pop() {
            if !visited.insert(key.clone()) {
                continue;
            }
            let judgment = self.inference.get(&key)?;
            derivation_rules.insert(key.rule());
            derivation_rules.extend(self.consulted.get(&key).into_iter().flatten().copied());
            pending.extend(judgment.premises().iter().cloned());
        }
        Some(InferenceUse {
            conclusion: key.rule(),
            derivation_rules,
            value_input_refs: dependencies.value_inputs.clone(),
            descriptor_copy_author_refs: dependencies.descriptor_copy_authors.clone(),
            argument_input_refs: dependencies.argument_inputs.clone(),
            argument_input_spec_locations: BTreeSet::new(),
            native_operation_input_refs: dependencies.native_operation_inputs.clone(),
        })
    }
}

/// Source-only author argument capabilities share the same certificate rules
/// as other semantic facts. They are solved after selector resolution and the
/// single canonical value-flow solve, before PlainData and the native census.
pub(crate) struct ArgumentNoEscapeFacts {
    facts: SemanticFacts,
    inputs: BTreeMap<Id, Vec<(u32, usize, usize)>>,
    spec_locations: BTreeMap<crate::analysis_inputs::ArgumentInputRef, String>,
}

pub(crate) struct ArgumentBorrowProof {
    pub(crate) dependencies: ProofDependencies,
    pub(crate) inference_use: InferenceUse,
}

impl ArgumentNoEscapeFacts {
    pub(crate) fn into_semantic_facts(self) -> SemanticFacts {
        self.facts
    }
    pub(crate) fn from_source(
        flow: &ChunkValueFlow,
        statements: &[StructuralStatementFacts],
        inputs: &[ResolvedArgumentInput],
        dynamic_scope: bool,
    ) -> Self {
        let mut callables = CallableInferenceInputs::default();
        let mut selected = BTreeMap::<Id, Vec<(u32, usize, usize)>>::new();
        let mut rules = BTreeMap::new();
        let mut spec_locations = BTreeMap::new();
        for input in inputs {
            let binding = &input.binding;
            let function = input.function_start;
            let values = flow.values_of_binding(binding);
            if !values.iter().eq([Value::Alloc(Kind::Function, function)]) {
                continue;
            }
            let rebound = statements.iter().any(|statement| {
                statement.rebinds.eager.contains(binding)
                    || statement.rebinds.lazy.contains(binding)
            });
            callables
                .functions
                .insert(binding.clone(), FunctionBindingFacts { values, rebound });
            let parameter_index = input.evidence.parameter_index;
            let exact_arity = input.evidence.exact_arity;
            selected.entry(binding.clone()).or_default().push((
                function,
                parameter_index,
                exact_arity,
            ));
            rules.insert(
                (binding.clone(), function, parameter_index, exact_arity),
                input.evidence.reference.clone(),
            );
            spec_locations.insert(
                input.evidence.reference.clone(),
                input.evidence.spec_location.clone(),
            );
        }
        Self {
            facts: SemanticFacts::solve(SemanticRules {
                dynamic_scope,
                callables,
                argument_inputs: rules,
                ..SemanticRules::default()
            }),
            inputs: selected,
            spec_locations,
        }
    }

    pub(crate) fn proof(
        &self,
        binding: &Id,
        argument_index: usize,
        arity: usize,
    ) -> Option<ArgumentBorrowProof> {
        self.inputs
            .get(binding)?
            .iter()
            .find_map(|&(function, index, exact_arity)| {
                (index == argument_index && exact_arity == arity).then(|| {
                    let key = SemanticKey::ArgumentNoEscape {
                        binding: binding.clone(),
                        function,
                        parameter_index: index,
                        exact_arity,
                    };
                    let mut inference_use = self.facts.inference_use(&key)?;
                    for reference in &inference_use.argument_input_refs {
                        if let Some(location) = self.spec_locations.get(reference) {
                            inference_use
                                .argument_input_spec_locations
                                .insert(location.clone());
                        }
                    }
                    Some(ArgumentBorrowProof {
                        dependencies: self.facts.dependencies(&key)?.clone(),
                        inference_use,
                    })
                })?
            })
    }
}
