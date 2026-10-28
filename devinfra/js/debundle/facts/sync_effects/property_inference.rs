//! Call-local own-property facts composed with the shared semantic context.
//!
//! The semantic facts are a completed source/native census. Prefix values have
//! a shorter lifetime: callers provide one object's current state for one
//! native call, and this engine is discarded after deriving its projection.

use super::value_model::{
    AbstractValue, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values,
    ordered_own_property_names,
};
use crate::analysis_inputs::NativeIntrinsic;
use crate::facts::semantic_inference::{SemanticFact, SemanticFacts, SemanticKey};
use crate::inference::{Inference, RuleContext, RuleSet};
use crate::reports::schema::{InferenceRule, InferenceUse};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone)]
pub(super) enum OwnPropertyRequest {
    Names { native_call_identity: SemanticKey },
    Descriptors { native_call_identity: SemanticKey },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum CapturedPropertyDescriptor {
    Data(Values),
    Accessor { getter: Values, setter: Values },
}

#[derive(Debug, Clone)]
pub(super) struct OwnPropertyNamesProjection {
    pub(super) names: Vec<String>,
}

#[derive(Debug, Clone)]
pub(super) struct OwnPropertyDescriptorProjection {
    pub(super) source_object: u32,
    /// ECMAScript own-key order, including canonical integer indices first.
    pub(super) ordered_names: Vec<String>,
    pub(super) properties: BTreeMap<String, CapturedPropertyDescriptor>,
    pub(super) retained_values: Values,
}

#[derive(Debug, Clone)]
pub(super) enum OwnPropertyProjection {
    Names(OwnPropertyNamesProjection),
    Descriptors(OwnPropertyDescriptorProjection),
}

#[derive(Debug, Clone)]
pub(super) struct OwnPropertyProof {
    pub(super) projection: OwnPropertyProjection,
    pub(super) inference_use: InferenceUse,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum OwnPropertyKey {
    SourceOrigin(u32),
    StaticShape(u32),
    PrefixState(u32),
    Native(SemanticKey),
    NamesProjection(u32),
    DescriptorProjection(u32),
}

impl OwnPropertyKey {
    fn rule(&self) -> Option<InferenceRule> {
        match self {
            Self::SourceOrigin(_) => Some(InferenceRule::OwnPropertySourceOrigin),
            Self::StaticShape(_) => Some(InferenceRule::OwnPropertyShape),
            Self::PrefixState(_) => Some(InferenceRule::OwnPropertyPrefixState),
            Self::Native(_) => None,
            Self::NamesProjection(_) => Some(InferenceRule::OwnPropertyNamesProjection),
            Self::DescriptorProjection(_) => Some(InferenceRule::OwnPropertyDescriptorProjection),
        }
    }
}

#[derive(Debug, Clone)]
struct PrefixState<'a> {
    source_object: u32,
    shape: &'a OwnPropertyShape,
    ordered_names: Vec<String>,
    properties: &'a BTreeMap<Option<String>, Values>,
}

#[derive(Debug, Clone)]
enum OwnPropertyFact<'a> {
    SourceOrigin(u32),
    StaticShape(&'a OwnPropertyShape),
    PrefixState(PrefixState<'a>),
    Native(InferenceUse),
    NamesProjection(OwnPropertyNamesProjection),
    DescriptorProjection(OwnPropertyDescriptorProjection),
}

struct OwnPropertyRules<'a> {
    source_object: u32,
    source_values: &'a Values,
    shape: &'a OwnPropertyShape,
    prefix_properties: &'a BTreeMap<Option<String>, Values>,
    tainted: bool,
    semantic_facts: &'a SemanticFacts,
    request: OwnPropertyRequest,
}

impl OwnPropertyRules<'_> {
    fn valid_shape(shape: &OwnPropertyShape) -> bool {
        let ordered: BTreeSet<_> = shape.creation_order.iter().collect();
        ordered.len() == shape.creation_order.len()
            && ordered.len() == shape.properties.len()
            && ordered
                .iter()
                .all(|name| shape.properties.contains_key(name.as_str()))
    }

    fn descriptor_projection(
        source_object: u32,
        state: &PrefixState<'_>,
    ) -> Option<OwnPropertyDescriptorProjection> {
        let mut properties = BTreeMap::new();
        for name in &state.ordered_names {
            let values = state.properties.get(&Some(name.clone()))?;
            let descriptor = match state.shape.properties.get(name)? {
                OwnPropertyDescriptorSyntax::Data => {
                    CapturedPropertyDescriptor::Data(values.clone())
                }
                OwnPropertyDescriptorSyntax::Accessor { getter, setter } => {
                    let expected: BTreeSet<_> =
                        getter.iter().chain(setter.iter()).copied().collect();
                    let mut callbacks = Values::new();
                    for value in values {
                        let AbstractValue::Closure(key, _) = value else {
                            return None;
                        };
                        if !expected.contains(key) {
                            return None;
                        }
                        callbacks.insert(value.clone());
                    }
                    let callbacks_for = |key: Option<u32>| {
                        key.map_or_else(Values::new, |key| {
                            callbacks
                                .iter()
                                .filter(|value| {
                                    matches!(value, AbstractValue::Closure(found, _) if *found == key)
                                })
                                .cloned()
                                .collect()
                        })
                    };
                    let getter_values = callbacks_for(*getter);
                    let setter_values = callbacks_for(*setter);
                    if (getter.is_some() && getter_values.is_empty())
                        || (setter.is_some() && setter_values.is_empty())
                    {
                        return None;
                    }
                    CapturedPropertyDescriptor::Accessor {
                        getter: getter_values,
                        setter: setter_values,
                    }
                }
            };
            properties.insert(name.clone(), descriptor);
        }

        let mut retained_values = Values::new();
        for descriptor in properties.values() {
            match descriptor {
                CapturedPropertyDescriptor::Data(values) => {
                    retained_values.extend(values.iter().cloned());
                }
                CapturedPropertyDescriptor::Accessor { getter, setter } => {
                    retained_values.extend(getter.iter().cloned());
                    retained_values.extend(setter.iter().cloned());
                }
            }
        }
        Some(OwnPropertyDescriptorProjection {
            source_object,
            ordered_names: state.ordered_names.clone(),
            properties,
            retained_values,
        })
    }
}

impl<'a> RuleSet for OwnPropertyRules<'a> {
    type Key = OwnPropertyKey;
    type Fact = OwnPropertyFact<'a>;

    fn evaluate(
        &self,
        key: &OwnPropertyKey,
        context: &mut RuleContext<'_, OwnPropertyKey, OwnPropertyFact<'a>>,
    ) -> Option<OwnPropertyFact<'a>> {
        match key {
            OwnPropertyKey::SourceOrigin(source_object) => {
                if *source_object != self.source_object
                    || !matches!(
                        self.source_values.iter().collect::<Vec<_>>().as_slice(),
                        [AbstractValue::Origin(crate::facts::ValueOrigin::Object(object))]
                            if object == source_object
                    )
                {
                    return None;
                }
                Some(OwnPropertyFact::SourceOrigin(*source_object))
            }
            OwnPropertyKey::StaticShape(source_object) => {
                if !matches!(
                    context.require(OwnPropertyKey::SourceOrigin(*source_object))?,
                    OwnPropertyFact::SourceOrigin(object) if object == source_object
                ) || !Self::valid_shape(self.shape)
                {
                    return None;
                }
                Some(OwnPropertyFact::StaticShape(self.shape))
            }
            OwnPropertyKey::PrefixState(source_object) => {
                let OwnPropertyFact::StaticShape(shape) =
                    context.require(OwnPropertyKey::StaticShape(*source_object))?
                else {
                    return None;
                };
                if *source_object != self.source_object || self.tainted {
                    return None;
                }
                if !self
                    .prefix_properties
                    .keys()
                    .map(Option::as_deref)
                    .eq(shape.properties.keys().map(|name| Some(name.as_str())))
                    || self.prefix_properties.values().any(Values::is_empty)
                {
                    return None;
                }
                let ordered_names = ordered_own_property_names(&shape.creation_order);
                if ordered_names.len() != shape.properties.len() {
                    return None;
                }
                Some(OwnPropertyFact::PrefixState(PrefixState {
                    source_object: *source_object,
                    shape,
                    ordered_names,
                    properties: self.prefix_properties,
                }))
            }
            OwnPropertyKey::Native(semantic_key) => {
                if !matches!(
                    context.import(self.semantic_facts.judgment(semantic_key)?),
                    SemanticFact::Certificate
                ) {
                    return None;
                }
                let inference_use = self.semantic_facts.inference_use(semantic_key)?;
                Some(OwnPropertyFact::Native(inference_use))
            }
            OwnPropertyKey::NamesProjection(source_object) => {
                let OwnPropertyFact::PrefixState(state) =
                    context.require(OwnPropertyKey::PrefixState(*source_object))?
                else {
                    return None;
                };
                let OwnPropertyRequest::Names {
                    native_call_identity,
                } = &self.request
                else {
                    return None;
                };
                if *source_object != state.source_object
                    || !matches!(
                        context.require(OwnPropertyKey::Native(native_call_identity.clone()))?,
                        OwnPropertyFact::Native(_)
                    )
                {
                    return None;
                }
                Some(OwnPropertyFact::NamesProjection(
                    OwnPropertyNamesProjection {
                        names: state.ordered_names.clone(),
                    },
                ))
            }
            OwnPropertyKey::DescriptorProjection(source_object) => {
                let OwnPropertyFact::PrefixState(state) =
                    context.require(OwnPropertyKey::PrefixState(*source_object))?
                else {
                    return None;
                };
                let OwnPropertyRequest::Descriptors {
                    native_call_identity,
                } = &self.request
                else {
                    return None;
                };
                context.require(OwnPropertyKey::Native(native_call_identity.clone()))?;
                context.require(OwnPropertyKey::Native(SemanticKey::NativeIntegrity(
                    NativeIntrinsic::GlobalObject,
                )))?;
                context.require(OwnPropertyKey::Native(SemanticKey::NativeIntegrity(
                    NativeIntrinsic::Object,
                )))?;
                Self::descriptor_projection(*source_object, state)
                    .map(OwnPropertyFact::DescriptorProjection)
            }
        }
    }
}

pub(super) fn infer_own_property_projection(
    source_object: u32,
    source_values: &Values,
    shape: &OwnPropertyShape,
    prefix_properties: &BTreeMap<Option<String>, Values>,
    tainted: bool,
    semantic_facts: &SemanticFacts,
    request: OwnPropertyRequest,
) -> Option<OwnPropertyProof> {
    let key = match &request {
        OwnPropertyRequest::Names { .. } => OwnPropertyKey::NamesProjection(source_object),
        OwnPropertyRequest::Descriptors { .. } => {
            OwnPropertyKey::DescriptorProjection(source_object)
        }
    };
    let rules = OwnPropertyRules {
        source_object,
        source_values,
        shape,
        prefix_properties,
        tainted,
        semantic_facts,
        request,
    };
    let mut inference = Inference::new(rules);
    inference.query(&key)?;
    let judgment = inference.get(&key)?;
    let conclusion = key.rule()?;
    let mut pending = vec![key.clone()];
    let mut visited = BTreeSet::new();
    let mut derivation_rules = BTreeSet::new();
    while let Some(premise) = pending.pop() {
        if !visited.insert(premise.clone()) {
            continue;
        }
        let premise_judgment = inference.get(&premise)?;
        if let Some(rule) = premise.rule() {
            derivation_rules.insert(rule);
        }
        if let OwnPropertyFact::Native(proof) = premise_judgment.value() {
            derivation_rules.extend(proof.derivation_rules.iter().copied());
        }
        pending.extend(premise_judgment.premises().iter().cloned());
    }
    let inference_use = InferenceUse {
        conclusion,
        derivation_rules,
        value_input_refs: judgment.dependencies().value_inputs.clone(),
    };
    let projection = match inference.into_value(&key)? {
        OwnPropertyFact::NamesProjection(projection) => OwnPropertyProjection::Names(projection),
        OwnPropertyFact::DescriptorProjection(projection) => {
            OwnPropertyProjection::Descriptors(projection)
        }
        _ => return None,
    };
    Some(OwnPropertyProof {
        projection,
        inference_use,
    })
}

#[cfg(test)]
mod tests {
    use super::super::value_model::ordered_own_property_names;

    #[test]
    fn own_name_order_matches_ecmascript_integer_index_boundary() {
        let source = vec![
            "z".to_owned(),
            "10".to_owned(),
            "2".to_owned(),
            "4294967294".to_owned(),
            "01".to_owned(),
            "4294967295".to_owned(),
        ];
        assert_eq!(
            ordered_own_property_names(&source),
            ["2", "10", "4294967294", "z", "01", "4294967295"]
        );
    }
}
