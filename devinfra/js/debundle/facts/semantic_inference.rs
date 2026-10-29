//! Positive semantic rules over one completed source/native census.
//!
//! Syntax candidates are not certificates. The queue derives certificates only
//! after their mandatory premises have been proved, and unions their captured
//! input dependencies. Purity, timing and heap ordering share this same store.

use super::callable_inference::{CallableInferenceInputs, ReturnedRecordFacts};
use super::local_class_construction::LocalClassConstructionShapes;
use super::set_iterables::SetIterableShapes;
use crate::analysis_inputs::{
    DescriptorCopyAuthorRef, DescriptorCopyFactorySummary, NativeInputKind, NativeIntrinsic,
    NativeOperationInputEvidence, ProofDependencies,
};
use crate::inference::{
    CertificateContext, FactContext, GuardedContext, IterativeFacts, IterativeInference,
    IterativeRules, Judgment,
};
use crate::reports::schema::{InferenceRule, InferenceUse};
use spec::DescriptorCopyFactorySignature;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum SemanticKey {
    NativeIntegrity(NativeIntrinsic),
    NativeCallIdentity {
        binding: Id,
        intrinsic: NativeIntrinsic,
        global_this_qualified: bool,
    },
    EmptyCollection(Id),
    EmptyCollectionSite {
        binding: Id,
        site: u32,
    },
    LocalClass(u32),
    PrimitiveSetIteration(u32),
    StableFunction {
        binding: Id,
        function: u32,
    },
    StableProvider {
        binding: Id,
        function: u32,
    },
    CallbackFactory {
        binding: Id,
        function: u32,
    },
    ReturnedRecord {
        binding: Id,
        function: u32,
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
            Self::NativeIntegrity(_) => InferenceRule::NativeIntegrity,
            Self::NativeCallIdentity { .. } => InferenceRule::NativeCallIdentity,
            Self::EmptyCollection(_) | Self::EmptyCollectionSite { .. } => {
                InferenceRule::EmptyCollectionConstruction
            }
            Self::LocalClass(_) => InferenceRule::LocalClassConstruction,
            Self::PrimitiveSetIteration(_) => InferenceRule::PrimitiveSetIteration,
            Self::StableFunction { .. } => InferenceRule::StableFunction,
            Self::StableProvider { .. } => InferenceRule::StableProvider,
            Self::CallbackFactory { .. } => InferenceRule::CallbackFactory,
            Self::ReturnedRecord { .. } => InferenceRule::ReturnedRecord,
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
    CallbackFactory(u32),
    ReturnedRecord(Arc<ReturnedRecordFacts>),
    NamespaceWrapper(u32),
}

/// Binding-resolution facts captured alongside the canonical source analysis.
#[derive(Debug, Default)]
pub(crate) struct NativeBindingContext {
    pub(crate) declared_bindings: BTreeSet<Id>,
    pub(crate) shadowed_globals: BTreeSet<&'static str>,
    pub(crate) global_object_names: BTreeSet<&'static str>,
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

/// Immutable inputs from the canonical structural analysis and completed native
/// census. A new source, policy or author-input context needs a new instance.
#[derive(Debug, Default)]
pub(crate) struct SemanticRules {
    pub(crate) classes: LocalClassConstructionShapes,
    pub(crate) sets: SetIterableShapes,
    /// Fresh-allocation candidates; `NativeCallIdentity` proves their binding.
    pub(crate) collection_bindings: BTreeMap<Id, NativeIntrinsic>,
    /// Exact author-certified zero-argument Set sites in private class fields.
    pub(crate) author_empty_set_fields: BTreeMap<u32, NativeOperationInputEvidence>,
    /// A census is complete only after all analyzed chunks contribute writes.
    pub(crate) native_census_complete: bool,
    pub(crate) mutated_intrinsics: BTreeSet<NativeIntrinsic>,
    pub(crate) dynamic_scope: bool,
    pub(crate) native_dependencies: ProofDependencies,
    pub(crate) binding_context: NativeBindingContext,
    /// Exact syntactic native-call candidates found by the canonical call index.
    pub(crate) native_call_identity_candidates: BTreeSet<SemanticKey>,
    pub(crate) callables: CallableInferenceInputs,
    pub(crate) descriptor_copy_providers: BTreeMap<Id, DescriptorCopyFactorySummary>,
    pub(crate) descriptor_copy_calls: BTreeMap<u32, DescriptorCopyCallCandidate>,
}

impl SemanticRules {
    fn derive(
        &self,
        key: &SemanticKey,
        context: &mut impl CertificateContext<SemanticKey, SemanticFact>,
    ) -> Option<SemanticFact> {
        match key {
            SemanticKey::NativeIntegrity(root) => {
                if !self.native_census_complete
                    || self.dynamic_scope
                    || self
                        .mutated_intrinsics
                        .contains(&NativeIntrinsic::GlobalObject)
                    || self.mutated_intrinsics.contains(root)
                {
                    return None;
                }
                context.assume(&self.native_dependencies);
            }
            SemanticKey::NativeCallIdentity {
                binding,
                intrinsic,
                global_this_qualified,
            } => {
                if !self.native_call_identity_candidates.contains(key)
                    || (!*global_this_qualified
                        && NativeIntrinsic::for_global_binding(binding.0.as_ref())
                            != Some(*intrinsic))
                    || self.binding_context.declared_bindings.contains(binding)
                    || self
                        .binding_context
                        .shadowed_globals
                        .contains(binding.0.as_ref())
                    || (*global_this_qualified
                        && (binding.0.as_ref() != "globalThis"
                            || !self
                                .binding_context
                                .global_object_names
                                .contains("globalThis")))
                {
                    return None;
                }
                context.require(SemanticKey::NativeIntegrity(NativeIntrinsic::GlobalObject))?;
                context.require(SemanticKey::NativeIntegrity(*intrinsic))?;
            }
            SemanticKey::EmptyCollection(binding) => {
                let root = self.collection_bindings.get(binding)?;
                context.require(SemanticKey::NativeCallIdentity {
                    binding: binding.clone(),
                    intrinsic: *root,
                    global_this_qualified: false,
                })?;
            }
            SemanticKey::EmptyCollectionSite { binding, site } => {
                if let Some(evidence) = self.author_empty_set_fields.get(site) {
                    if binding.0.as_ref() != "Set"
                        || evidence.operation
                            != NativeInputKind::CallIdentity(
                                spec::NativeCallIdentityKind::SetEmptyPrivateField,
                            )
                    {
                        return None;
                    }
                    context.assume(&ProofDependencies {
                        native_operation_inputs: BTreeSet::from([evidence.reference.clone()]),
                        ..ProofDependencies::default()
                    });
                    return Some(SemanticFact::Certificate);
                }
                context.require(SemanticKey::EmptyCollection(binding.clone()))?;
            }
            SemanticKey::LocalClass(class) => {
                for (binding, site) in self.classes.classes.get(class)? {
                    context.require(SemanticKey::EmptyCollectionSite {
                        binding: binding.clone(),
                        site: *site,
                    })?;
                }
            }
            SemanticKey::PrimitiveSetIteration(span) => {
                if !self.sets.constructor_starts.contains(span) {
                    return None;
                }
                context.require(SemanticKey::NativeIntegrity(NativeIntrinsic::Set))?;
                context.require(SemanticKey::NativeIntegrity(NativeIntrinsic::Array))?;
            }
            SemanticKey::DescriptorCopyProvider(binding) => {
                if !self.native_census_complete || self.dynamic_scope {
                    return None;
                }
                let provider = self.descriptor_copy_providers.get(binding)?;
                if provider.signatures.len() != 1 {
                    return None;
                }
                // Exact-slot contradiction checks consume the completed raw
                // census, including any authored captured-value refinements.
                context.assume(&self.native_dependencies);
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
            SemanticKey::StableFunction { .. }
            | SemanticKey::StableProvider { .. }
            | SemanticKey::CallbackFactory { .. }
            | SemanticKey::ReturnedRecord { .. }
            | SemanticKey::NamespaceWrapper { .. } => {
                if self.dynamic_scope {
                    return None;
                }
                return self.callables.evaluate(key, context);
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
        let result = self.derive(key, &mut guarded);
        if let Some(fact) = guarded.finish(result) {
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
    constructions: BTreeMap<u32, u32>,
}

impl Default for SemanticFacts {
    fn default() -> Self {
        let rules = SemanticRules {
            // A context without a completed census cannot prove native roots.
            dynamic_scope: true,
            ..SemanticRules::default()
        };
        Self::solve(rules)
    }
}

impl SemanticFacts {
    pub(crate) fn solve(mut rules: SemanticRules) -> Self {
        let constructions = std::mem::take(&mut rules.classes.constructions);
        let native_roots = [
            NativeIntrinsic::GlobalObject,
            NativeIntrinsic::UnknownGlobalValue,
            NativeIntrinsic::Error,
            NativeIntrinsic::FinalizationRegistry,
            NativeIntrinsic::Function,
            NativeIntrinsic::Object,
            NativeIntrinsic::Array,
            NativeIntrinsic::Math,
            NativeIntrinsic::RegExp,
            NativeIntrinsic::Symbol,
            NativeIntrinsic::Promise,
            NativeIntrinsic::Map,
            NativeIntrinsic::Set,
            NativeIntrinsic::WeakMap,
            NativeIntrinsic::WeakSet,
        ]
        .into_iter()
        .map(SemanticKey::NativeIntegrity);
        let roots: Vec<_> = native_roots
            .chain(rules.native_call_identity_candidates.iter().cloned())
            .chain(rules.callables.keys())
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
                    .collection_bindings
                    .keys()
                    .cloned()
                    .map(SemanticKey::EmptyCollection),
            )
            .chain(
                rules
                    .classes
                    .classes
                    .values()
                    .flatten()
                    .cloned()
                    .map(|(binding, site)| SemanticKey::EmptyCollectionSite { binding, site }),
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
            .collect();
        let inference = IterativeInference::new(rules).solve(roots);
        Self {
            inference,
            constructions,
        }
    }

    pub(super) fn class_for_construction(&self, span: Span) -> Option<u32> {
        self.constructions.get(&span.lo.0).copied()
    }

    pub(crate) fn fact(&self, key: &SemanticKey) -> Option<&SemanticFact> {
        self.inference.get(key).map(|judgment| judgment.value())
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
            pending.extend(judgment.premises().iter().cloned());
        }
        Some(InferenceUse {
            conclusion: key.rule(),
            derivation_rules,
            value_input_refs: dependencies.value_inputs.clone(),
            descriptor_copy_author_refs: dependencies.descriptor_copy_authors.clone(),
        })
    }
}
