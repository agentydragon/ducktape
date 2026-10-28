//! Positive construction rules over one completed source/native census.
//!
//! Syntax candidates are not certificates. The queue derives certificates only
//! after their mandatory premises have been proved, and unions their captured
//! input dependencies. Purity, timing and heap ordering share this same store.

use super::local_class_construction::LocalClassConstructionShapes;
use super::set_iterables::SetIterableShapes;
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::inference::{Inference, InferenceStatus, RuleContext, RuleSet};
use crate::reports::schema::{InferenceRule, InferenceUse};
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum ConstructionKey {
    NativeIntegrity(NativeIntrinsic),
    EmptyCollection(Id),
    LocalClass(u32),
    PrimitiveSetIteration(u32),
}

impl ConstructionKey {
    fn rule(&self) -> InferenceRule {
        match self {
            Self::NativeIntegrity(_) => InferenceRule::NativeIntegrity,
            Self::EmptyCollection(_) => InferenceRule::EmptyCollectionConstruction,
            Self::LocalClass(_) => InferenceRule::LocalClassConstruction,
            Self::PrimitiveSetIteration(_) => InferenceRule::PrimitiveSetIteration,
        }
    }
}

/// Immutable inputs from the canonical structural analysis and completed native
/// census. A new source, policy or author-input context needs a new instance.
#[derive(Debug, Default)]
pub(crate) struct ConstructionRules {
    pub(crate) classes: LocalClassConstructionShapes,
    pub(crate) sets: SetIterableShapes,
    /// Only unresolved global bindings with a fresh-allocation native model.
    pub(crate) collection_bindings: BTreeMap<Id, NativeIntrinsic>,
    pub(crate) mutated_intrinsics: BTreeSet<NativeIntrinsic>,
    pub(crate) dynamic_scope: bool,
    pub(crate) native_dependencies: ProofDependencies,
}

impl RuleSet for ConstructionRules {
    type Key = ConstructionKey;
    // The typed key is the proposition. Presence is its positive certificate;
    // there is no universal boolean combining purity, reentry and heap effects.
    type Fact = ();

    fn evaluate(
        &self,
        key: &ConstructionKey,
        context: &mut RuleContext<'_, ConstructionKey, ()>,
    ) -> Option<()> {
        match key {
            ConstructionKey::NativeIntegrity(root) => {
                if self.dynamic_scope
                    || self
                        .mutated_intrinsics
                        .contains(&NativeIntrinsic::GlobalObject)
                    || self.mutated_intrinsics.contains(root)
                {
                    return None;
                }
                context.assume(&self.native_dependencies);
            }
            ConstructionKey::EmptyCollection(binding) => {
                let root = self.collection_bindings.get(binding)?;
                context.require(ConstructionKey::NativeIntegrity(*root))?;
            }
            ConstructionKey::LocalClass(class) => {
                for field in self.classes.classes.get(class)? {
                    context.require(ConstructionKey::EmptyCollection(field.clone()))?;
                }
            }
            ConstructionKey::PrimitiveSetIteration(span) => {
                if !self.sets.constructor_starts.contains(span) {
                    return None;
                }
                context.require(ConstructionKey::NativeIntegrity(NativeIntrinsic::Set))?;
                context.require(ConstructionKey::NativeIntegrity(NativeIntrinsic::Array))?;
            }
        }
        Some(())
    }
}

#[derive(Debug)]
pub(super) struct ConstructionFacts {
    inference: Inference<ConstructionRules>,
    constructions: BTreeMap<u32, u32>,
}

impl Default for ConstructionFacts {
    fn default() -> Self {
        Self::solve(ConstructionRules::default())
    }
}

impl ConstructionFacts {
    pub(crate) fn solve(mut rules: ConstructionRules) -> Self {
        let constructions = std::mem::take(&mut rules.classes.constructions);
        let roots: Vec<_> = rules
            .classes
            .classes
            .keys()
            .copied()
            .map(ConstructionKey::LocalClass)
            .chain(
                rules
                    .sets
                    .constructor_starts
                    .iter()
                    .copied()
                    .map(ConstructionKey::PrimitiveSetIteration),
            )
            .collect();
        let mut inference = Inference::new(rules);
        for root in roots {
            inference.query(&root);
        }
        Self {
            inference,
            constructions,
        }
    }

    pub(super) fn class_for_construction(&self, span: Span) -> Option<u32> {
        self.constructions.get(&span.lo.0).copied()
    }

    pub(super) fn dependencies(&self, key: &ConstructionKey) -> Option<&ProofDependencies> {
        match self.inference.status(key)? {
            InferenceStatus::Proven(judgment) => Some(judgment.dependencies()),
            InferenceStatus::Waiting(premises) => {
                debug_assert!(!premises.is_empty());
                None
            }
            InferenceStatus::Unsupported => None,
        }
    }

    /// Expand shared premise IDs only for a consumed diagnostic. The analysis
    /// stores no recursively copied derivation trees.
    pub(super) fn explain(&self, key: &ConstructionKey) -> Option<InferenceUse> {
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
        })
    }
}
