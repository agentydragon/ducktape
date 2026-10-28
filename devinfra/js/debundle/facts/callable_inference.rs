//! Callable conclusions composed from canonical identity and body candidates.

use crate::analysis_inputs::NativeIntrinsic;
use crate::facts::semantic_inference::{SemanticFact, SemanticKey};
use crate::facts::value_flow::{ValueOrigin, Values};
use crate::inference::RuleContext;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_ecma_ast::Id;

#[derive(Debug)]
pub(crate) struct FunctionBindingFacts {
    pub(crate) values: Values,
    pub(crate) hoisted: bool,
    pub(crate) definite_initializer: bool,
    pub(crate) rebound: bool,
}

#[derive(Debug)]
pub(crate) struct ReturnedRecordFacts {
    pub(crate) function_start: u32,
    pub(crate) members: Vec<(String, u32, BTreeSet<String>)>,
}

#[derive(Debug)]
pub(crate) struct ReturnedRecordCandidateFacts {
    pub(crate) constructor: Id,
    pub(crate) members: Arc<ReturnedRecordFacts>,
}

#[derive(Debug)]
pub(crate) struct NamespaceWrapperCandidateFacts {
    pub(crate) object: Id,
    pub(crate) weak_map: Id,
}

#[derive(Debug, Default)]
pub(crate) struct CallableInferenceInputs {
    pub(crate) functions: BTreeMap<Id, FunctionBindingFacts>,
    pub(crate) callback_factories: BTreeSet<u32>,
    pub(crate) records: BTreeMap<u32, ReturnedRecordCandidateFacts>,
    pub(crate) namespace_wrappers: BTreeMap<u32, NamespaceWrapperCandidateFacts>,
}

impl CallableInferenceInputs {
    pub(crate) fn keys(&self) -> Vec<SemanticKey> {
        self.functions
            .iter()
            .flat_map(|(binding, facts)| {
                facts.values.iter().filter_map(move |value| {
                    let ValueOrigin::Function(function) = value else {
                        return None;
                    };
                    Some([
                        SemanticKey::CallbackFactory {
                            binding: binding.clone(),
                            function,
                        },
                        SemanticKey::ReturnedRecord {
                            binding: binding.clone(),
                            function,
                        },
                        SemanticKey::NamespaceWrapper {
                            binding: binding.clone(),
                            function,
                        },
                    ])
                })
            })
            .flatten()
            .collect()
    }

    /// Source candidates identify possible bodies. Only a required identity
    /// judgment and, for a record, its native allocation can certify one.
    pub(crate) fn evaluate(
        &self,
        key: &SemanticKey,
        context: &mut RuleContext<'_, SemanticKey, SemanticFact>,
    ) -> Option<SemanticFact> {
        match key {
            SemanticKey::StableFunction { binding, function } => self
                .functions
                .get(binding)?
                .values
                .iter()
                .eq([ValueOrigin::Function(*function)])
                .then_some(SemanticFact::Function(*function)),
            SemanticKey::CallbackFactory { binding, function } => {
                let identity = context.require(SemanticKey::StableFunction {
                    binding: binding.clone(),
                    function: *function,
                })?;
                let SemanticFact::Function(identity) = identity else {
                    return None;
                };
                let source = self.functions.get(binding)?;
                (*identity == *function
                    && (source.hoisted || source.definite_initializer)
                    && self.callback_factories.contains(function))
                .then_some(SemanticFact::CallbackFactory(*function))
            }
            SemanticKey::ReturnedRecord { binding, function } => {
                let identity = context.require(SemanticKey::StableFunction {
                    binding: binding.clone(),
                    function: *function,
                })?;
                let SemanticFact::Function(identity) = identity else {
                    return None;
                };
                let source = self.functions.get(binding)?;
                if *identity != *function || !source.hoisted || source.rebound {
                    return None;
                }
                let record = self.records.get(function)?;
                context.require(SemanticKey::EmptyCollection(record.constructor.clone()))?;
                Some(SemanticFact::ReturnedRecord(Arc::clone(&record.members)))
            }
            SemanticKey::NamespaceWrapper { binding, function } => {
                let identity = context.require(SemanticKey::StableFunction {
                    binding: binding.clone(),
                    function: *function,
                })?;
                let SemanticFact::Function(identity) = identity else {
                    return None;
                };
                let source = self.functions.get(binding)?;
                if *identity != *function || !source.hoisted || source.rebound {
                    return None;
                }
                let candidate = self.namespace_wrappers.get(function)?;
                context.require(SemanticKey::EmptyCollection(candidate.weak_map.clone()))?;
                context.require(SemanticKey::NativeCallIdentity {
                    binding: candidate.object.clone(),
                    intrinsic: NativeIntrinsic::Object,
                    global_this_qualified: false,
                })?;
                context.require(SemanticKey::NativeIntegrity(NativeIntrinsic::Array))?;
                context.require(SemanticKey::NativeIntegrity(NativeIntrinsic::Function))?;
                Some(SemanticFact::NamespaceWrapper(*function))
            }
            _ => None,
        }
    }
}
