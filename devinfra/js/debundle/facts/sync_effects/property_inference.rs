//! Call-local own-property certificates composed with the shared semantic context.
//!
//! The semantic facts are a completed source/native census. Prefix values have
//! a shorter lifetime: callers provide one object's current state for one
//! native call, and the projection borrows it only while deriving that result.

use super::value_model::{
    AbstractValue, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values,
    ordered_own_property_names,
};
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::facts::semantic_inference::{SemanticFact, SemanticFacts, SemanticKey};
use crate::reports::schema::{InferenceRule, InferenceUse};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone)]
pub(super) enum OwnPropertyRequest {
    Ordinary,
    /// Provider identity and exact Object/prototype slots are an independent
    /// author premise checked against the whole-program raw write census.
    AuditedDescriptorCopy {
        semantic_call: SemanticKey,
    },
    Names {
        native_call_identity: SemanticKey,
    },
    Descriptors {
        native_call_identity: SemanticKey,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum CapturedPropertyDescriptor {
    Data(Values),
    Accessor { getter: Values, setter: Values },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OwnPropertyNamesProjection {
    pub(super) names: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OwnPropertyDescriptorProjection {
    pub(super) source_object: u32,
    /// ECMAScript own-key order, including canonical integer indices first.
    pub(super) ordered_names: Vec<String>,
    pub(super) properties: BTreeMap<String, CapturedPropertyDescriptor>,
    pub(super) retained_values: Values,
}

#[derive(Debug, Clone)]
pub(super) enum OwnPropertyProjection {
    Ordinary { source_object: u32 },
    Names(OwnPropertyNamesProjection),
    Descriptors(OwnPropertyDescriptorProjection),
}

#[derive(Debug, Clone)]
pub(super) struct OwnPropertyProof {
    pub(super) projection: OwnPropertyProjection,
    conclusion: InferenceRule,
    derivation_rules: BTreeSet<InferenceRule>,
    pub(super) dependencies: ProofDependencies,
}

impl OwnPropertyProof {
    pub(super) fn inference_use(&self) -> InferenceUse {
        InferenceUse {
            conclusion: self.conclusion,
            derivation_rules: self.derivation_rules.clone(),
            value_input_refs: self.dependencies.value_inputs.clone(),
            descriptor_copy_author_refs: self.dependencies.descriptor_copy_authors.clone(),
        }
    }
}

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
    ordered_names: &[String],
    shape: &OwnPropertyShape,
    prefix_properties: &BTreeMap<Option<String>, Values>,
) -> Option<OwnPropertyDescriptorProjection> {
    let mut properties = BTreeMap::new();
    for name in ordered_names {
        let values = prefix_properties.get(&Some(name.clone()))?;
        let descriptor = match shape.properties.get(name)? {
            OwnPropertyDescriptorSyntax::Data => CapturedPropertyDescriptor::Data(values.clone()),
            OwnPropertyDescriptorSyntax::Accessor { getter, setter } => {
                let expected: BTreeSet<_> = getter.iter().chain(setter.iter()).copied().collect();
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
        ordered_names: ordered_names.to_vec(),
        properties,
        retained_values,
    })
}

fn require_native_certificate(
    semantic_facts: &SemanticFacts,
    key: &SemanticKey,
    dependencies: &mut ProofDependencies,
    derivation_rules: &mut BTreeSet<InferenceRule>,
) -> Option<()> {
    let judgment = semantic_facts.judgment(key)?;
    if !matches!(judgment.value(), SemanticFact::Certificate) {
        return None;
    }
    // The typed dependencies come directly from the settled judgment. Its
    // expanded report contributes explanation rules only.
    dependencies.extend(judgment.dependencies());
    let inference_use = semantic_facts.inference_use(key)?;
    derivation_rules.extend(inference_use.derivation_rules);
    Some(())
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
    let source_is_exact_object = source_values.len() == 1
        && matches!(
            source_values.first(),
            Some(AbstractValue::Origin(crate::facts::ValueOrigin::Object(object)))
                if *object == source_object
        );
    if !source_is_exact_object || !valid_shape(shape) || tainted {
        return None;
    }
    if !prefix_properties
        .keys()
        .map(Option::as_deref)
        .eq(shape.properties.keys().map(|name| Some(name.as_str())))
        || prefix_properties.values().any(Values::is_empty)
    {
        return None;
    }
    let ordered_names = ordered_own_property_names(&shape.creation_order);
    if ordered_names.len() != shape.properties.len() {
        return None;
    }

    let mut dependencies = ProofDependencies::default();
    let mut derivation_rules = BTreeSet::from([
        InferenceRule::OwnPropertySourceOrigin,
        InferenceRule::OwnPropertyShape,
        InferenceRule::OwnPropertyPrefixState,
    ]);
    let (conclusion, projection) = match request {
        OwnPropertyRequest::Ordinary => (
            InferenceRule::OwnPropertyPrefixState,
            OwnPropertyProjection::Ordinary { source_object },
        ),
        OwnPropertyRequest::Names {
            native_call_identity,
        } => {
            require_native_certificate(
                semantic_facts,
                &native_call_identity,
                &mut dependencies,
                &mut derivation_rules,
            )?;
            derivation_rules.insert(InferenceRule::OwnPropertyNamesProjection);
            (
                InferenceRule::OwnPropertyNamesProjection,
                OwnPropertyProjection::Names(OwnPropertyNamesProjection {
                    names: ordered_names,
                }),
            )
        }
        OwnPropertyRequest::AuditedDescriptorCopy { semantic_call } => {
            require_native_certificate(
                semantic_facts,
                &semantic_call,
                &mut dependencies,
                &mut derivation_rules,
            )?;
            let projection =
                descriptor_projection(source_object, &ordered_names, shape, prefix_properties)?;
            derivation_rules.insert(InferenceRule::DescriptorCopyFactory);
            (
                InferenceRule::DescriptorCopyFactory,
                OwnPropertyProjection::Descriptors(projection),
            )
        }
        OwnPropertyRequest::Descriptors {
            native_call_identity,
        } => {
            for key in [
                native_call_identity,
                SemanticKey::NativeIntegrity(NativeIntrinsic::GlobalObject),
                SemanticKey::NativeIntegrity(NativeIntrinsic::Object),
            ] {
                require_native_certificate(
                    semantic_facts,
                    &key,
                    &mut dependencies,
                    &mut derivation_rules,
                )?;
            }
            let projection =
                descriptor_projection(source_object, &ordered_names, shape, prefix_properties)?;
            derivation_rules.insert(InferenceRule::OwnPropertyDescriptorProjection);
            (
                InferenceRule::OwnPropertyDescriptorProjection,
                OwnPropertyProjection::Descriptors(projection),
            )
        }
    };
    Some(OwnPropertyProof {
        projection,
        conclusion,
        derivation_rules,
        dependencies,
    })
}

#[cfg(test)]
mod tests {
    use super::super::value_model::{
        AbstractValue, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values,
        ordered_own_property_names,
    };
    use super::{OwnPropertyProjection, OwnPropertyRequest, infer_own_property_projection};
    use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies, ValueInputRef};
    use crate::facts::semantic_inference::{SemanticFacts, SemanticKey, SemanticRules};
    use crate::facts::value_flow::ValueOrigin;
    use crate::reports::schema::InferenceRule;
    use std::collections::{BTreeMap, BTreeSet};
    use swc_common::SyntaxContext;
    use swc_ecma_ast::Id;

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

    #[test]
    fn ordinary_projection_has_no_native_call_premise() {
        let source_object = 7;
        let source_values =
            Values::from([AbstractValue::Origin(ValueOrigin::Object(source_object))]);
        let shape = OwnPropertyShape {
            properties: BTreeMap::from([("field".to_owned(), OwnPropertyDescriptorSyntax::Data)]),
            creation_order: vec!["field".to_owned()],
        };
        let prefix_properties = BTreeMap::from([(
            Some("field".to_owned()),
            Values::from([AbstractValue::NonCallable]),
        )]);
        let proof = infer_own_property_projection(
            source_object,
            &source_values,
            &shape,
            &prefix_properties,
            false,
            &SemanticFacts::default(),
            OwnPropertyRequest::Ordinary,
        )
        .expect("ordinary source projection");

        assert!(matches!(
            proof.projection,
            OwnPropertyProjection::Ordinary { source_object: 7 }
        ));
        assert_eq!(
            proof.inference_use().conclusion,
            InferenceRule::OwnPropertyPrefixState
        );
        assert_eq!(
            proof.inference_use().derivation_rules,
            BTreeSet::from([
                InferenceRule::OwnPropertySourceOrigin,
                InferenceRule::OwnPropertyShape,
                InferenceRule::OwnPropertyPrefixState,
            ])
        );
        assert!(proof.dependencies.is_empty());
    }

    #[test]
    fn native_projection_imports_completed_judgment_provenance() {
        let source_object = 7;
        let source_values =
            Values::from([AbstractValue::Origin(ValueOrigin::Object(source_object))]);
        let shape = OwnPropertyShape {
            properties: BTreeMap::from([("field".to_owned(), OwnPropertyDescriptorSyntax::Data)]),
            creation_order: vec!["field".to_owned()],
        };
        let prefix_properties = BTreeMap::from([(
            Some("field".to_owned()),
            Values::from([AbstractValue::NonCallable]),
        )]);
        let native_call_identity = SemanticKey::NativeCallIdentity {
            binding: Id::from(("Object".into(), SyntaxContext::empty())),
            intrinsic: NativeIntrinsic::Object,
            global_this_qualified: false,
        };
        let value_input = ValueInputRef {
            defining_chunk: "native-census".to_owned(),
            input_index: 3,
        };
        let semantic_facts = SemanticFacts::solve(SemanticRules {
            native_census_complete: true,
            native_dependencies: ProofDependencies {
                value_inputs: BTreeSet::from([value_input.clone()]),
                ..Default::default()
            },
            native_call_identity_candidates: BTreeSet::from([native_call_identity.clone()]),
            ..SemanticRules::default()
        });
        assert!(semantic_facts.judgment(&native_call_identity).is_some());

        let proof = infer_own_property_projection(
            source_object,
            &source_values,
            &shape,
            &prefix_properties,
            false,
            &semantic_facts,
            OwnPropertyRequest::Names {
                native_call_identity: native_call_identity.clone(),
            },
        )
        .expect("completed native and property certificates");

        let inference_use = proof.inference_use();
        assert_eq!(
            inference_use.derivation_rules,
            BTreeSet::from([
                InferenceRule::NativeIntegrity,
                InferenceRule::NativeCallIdentity,
                InferenceRule::OwnPropertySourceOrigin,
                InferenceRule::OwnPropertyShape,
                InferenceRule::OwnPropertyPrefixState,
                InferenceRule::OwnPropertyNamesProjection,
            ])
        );
        assert_eq!(
            inference_use.value_input_refs,
            BTreeSet::from([value_input.clone()])
        );
        assert_eq!(
            proof.dependencies,
            semantic_facts
                .judgment(&native_call_identity)
                .expect("native call identity judgment")
                .dependencies()
                .clone()
        );

        // Runtime consumers must use the typed proof premise. A diagnostic is
        // serializable report data and may differ without changing the proof.
        let mut reported_dependencies = proof.inference_use().value_input_refs;
        reported_dependencies.clear();
        assert_ne!(reported_dependencies, proof.dependencies.value_inputs);
        assert_eq!(
            proof.dependencies.value_inputs,
            BTreeSet::from([value_input])
        );
    }
}
