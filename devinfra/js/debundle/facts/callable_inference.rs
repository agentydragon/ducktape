//! Callable conclusions composed from canonical identity and body candidates.

use crate::analysis_inputs::NativeIntrinsic;
use crate::facts::abstract_domain::builtins::Row;
use crate::facts::abstract_domain::integrity::IntrinsicSlot;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::semantic_inference::{NativeCallee, NativePremises, SemanticFact, SemanticKey};
use crate::facts::value_flow::Values;
use crate::inference::CertificateContext;
use crate::reports::schema::InferenceRule;
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
        let mut keys = Vec::new();
        for (binding, facts) in &self.functions {
            for value in facts.values.iter() {
                let Value::Alloc(Kind::Function, function) = value else {
                    continue;
                };
                keys.push(SemanticKey::StableFunction {
                    binding: binding.clone(),
                    function,
                });
                keys.push(SemanticKey::StableProvider {
                    binding: binding.clone(),
                    function,
                });
                if self.callback_factories.contains(&function) {
                    keys.push(SemanticKey::CallbackFactory {
                        binding: binding.clone(),
                        function,
                    });
                }
                if self.records.contains_key(&function) {
                    keys.push(SemanticKey::ReturnedRecord {
                        binding: binding.clone(),
                        function,
                    });
                }
                if self.namespace_wrappers.contains_key(&function) {
                    keys.push(SemanticKey::NamespaceWrapper {
                        binding: binding.clone(),
                        function,
                    });
                }
            }
        }
        keys
    }

    /// Source candidates identify possible bodies. Only a required identity
    /// judgment and, for a record, its native allocation can certify one.
    pub(crate) fn evaluate(
        &self,
        key: &SemanticKey,
        context: &mut impl CertificateContext<SemanticKey, SemanticFact>,
        native: &NativePremises,
        collections: &BTreeMap<Id, &'static Row>,
        consulted: &mut BTreeSet<InferenceRule>,
    ) -> Option<SemanticFact> {
        match key {
            SemanticKey::StableFunction { binding, function } => self
                .functions
                .get(binding)?
                .values
                .iter()
                .eq([Value::Alloc(Kind::Function, *function)])
                .then_some(SemanticFact::Function(*function)),
            SemanticKey::StableProvider { binding, function } => {
                let source = self.functions.get(binding)?;
                if !source.hoisted || source.rebound {
                    return None;
                }
                let identity = context.require(SemanticKey::StableFunction {
                    binding: binding.clone(),
                    function: *function,
                })?;
                matches!(identity, SemanticFact::Function(found) if *found == *function)
                    .then_some(SemanticFact::Function(*function))
            }
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
                native.require_empty_collection(
                    collections.get(&record.constructor)?,
                    &record.constructor,
                    None,
                    context,
                    consulted,
                )?;
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
                native.require_empty_collection(
                    collections.get(&candidate.weak_map)?,
                    &candidate.weak_map,
                    None,
                    context,
                    consulted,
                )?;
                native.require_binding(
                    &NativeCallee {
                        binding: candidate.object.clone(),
                        global_this_qualified: false,
                    },
                    NativeIntrinsic::Object,
                    &[IntrinsicSlot::Global(NativeIntrinsic::Object)],
                    None,
                    context,
                    consulted,
                )?;
                native.require_slots(
                    &[
                        IntrinsicSlot::Prototype(NativeIntrinsic::Array),
                        IntrinsicSlot::Prototype(NativeIntrinsic::Function),
                    ],
                    None,
                    context,
                    consulted,
                )?;
                Some(SemanticFact::NamespaceWrapper(*function))
            }
            _ => None,
        }
    }
}
