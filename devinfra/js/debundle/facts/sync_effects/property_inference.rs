//! Call-local own-property certificates composed with the shared semantic context.
//!
//! The semantic facts are a completed source/native census. Prefix values have
//! a shorter lifetime: callers provide one object's current state for one
//! native call, and the projection borrows it only while deriving that result.

use super::object_surface::{CurrentOwnPropertyDescriptor, CurrentOwnPropertyProjection};
use super::value_model::{
    AbstractValue, OwnPropertyDescriptorSyntax, OwnPropertyShape, SymbolicInputId, Values,
    ordered_own_property_names,
};
use crate::analysis_inputs::{ProofDependencies, RecordInputProperty, RecordInputShape};
use crate::analysis_provenance::{InferenceRule, InferenceUse};
use crate::facts::abstract_domain::value::{HeapAddress, Kind, SourceHeapResolver, Value};
use crate::facts::semantic_inference::{NativePremise, SemanticFact, SemanticFacts, SemanticKey};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone)]
pub(super) enum OwnPropertyRequest {
    Ordinary,
    /// Logical descriptor metadata for a conditional body input. This is no
    /// native call or effect receipt; its consumer owns those proofs.
    DescriptorLayout,
    /// Provider identity and exact Object/prototype slots are an independent
    /// author premise checked against the whole-program raw write census.
    AuditedDescriptorCopy {
        semantic_call: SemanticKey,
    },
    /// The census-proved identity of the native inspecting the object.
    Names {
        native_call: NativePremise,
    },
    Descriptors {
        native_call: NativePremise,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum CapturedPropertyDescriptor {
    Data(Values),
    Accessor { getter: Values, setter: Values },
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(super) struct DescriptorAttributes {
    pub(super) writable: Option<bool>,
    pub(super) enumerable: Option<bool>,
    pub(super) configurable: Option<bool>,
}

impl DescriptorAttributes {
    pub(super) fn literal(data: bool) -> Self {
        Self {
            writable: data.then_some(true),
            enumerable: Some(true),
            configurable: Some(true),
        }
    }
}

#[derive(Clone, Copy)]
pub(super) enum DescriptorCallbackModel {
    CapturedClosure,
    /// Capture cells are retained separately by the isolated provider heap.
    ProviderFunction,
}

trait ResolveAllocationIdentity: Copy {
    fn in_scope_allocation(self, kind: Kind, resolver: SourceHeapResolver) -> Option<HeapAddress>;
}

impl ResolveAllocationIdentity for u32 {
    fn in_scope_allocation(self, kind: Kind, resolver: SourceHeapResolver) -> Option<HeapAddress> {
        Some(resolver.allocation(kind, self))
    }
}

impl ResolveAllocationIdentity for HeapAddress {
    fn in_scope_allocation(self, kind: Kind, resolver: SourceHeapResolver) -> Option<HeapAddress> {
        (self.module() == Some(resolver.scope()) && self.kind() == Some(kind)).then_some(self)
    }
}

impl DescriptorCallbackModel {
    fn key(self, value: &AbstractValue, resolver: SourceHeapResolver) -> Option<HeapAddress> {
        match (self, value) {
            (Self::CapturedClosure, AbstractValue::Closure(key, _)) => {
                key.in_scope_allocation(Kind::Function, resolver)
            }
            (Self::ProviderFunction, AbstractValue::Value(Value::Alloc(Kind::Function, key))) => {
                key.in_scope_allocation(Kind::Function, resolver)
            }
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OwnPropertyNamesProjection {
    pub(super) names: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum DescriptorSource {
    Object(HeapAddress),
    Input(SymbolicInputId),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OwnPropertyDescriptorProjection {
    pub(super) source_object: DescriptorSource,
    /// ECMAScript own-key order, including canonical integer indices first.
    pub(super) ordered_names: Vec<String>,
    pub(super) properties: BTreeMap<String, CapturedPropertyDescriptor>,
    pub(super) attributes: BTreeMap<String, DescriptorAttributes>,
    pub(super) retained_values: Values,
}

#[derive(Debug, Clone)]
pub(super) enum OwnPropertyProjection {
    Ordinary { source_object: HeapAddress },
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

impl RecordInputShape {
    pub(super) fn from_shape(shape: &OwnPropertyShape) -> Option<Self> {
        if shape.properties.contains_key("__proto__") {
            return None;
        }
        Some(Self {
            creation_order: shape.creation_order.clone(),
            properties: shape
                .properties
                .iter()
                .map(|(name, descriptor)| {
                    (
                        name.clone(),
                        match descriptor {
                            OwnPropertyDescriptorSyntax::Data => RecordInputProperty::Data,
                            OwnPropertyDescriptorSyntax::Accessor { getter, setter } => {
                                RecordInputProperty::Accessor {
                                    getter: getter.is_some(),
                                    setter: setter.is_some(),
                                }
                            }
                        },
                    )
                })
                .collect(),
        })
    }

    pub(super) fn matches_projection(&self, projection: &OwnPropertyDescriptorProjection) -> bool {
        ordered_own_property_names(&self.creation_order) == projection.ordered_names
            && self.properties.len() == projection.properties.len()
            && self.properties.iter().all(|(name, kind)| {
                let data = matches!(kind, RecordInputProperty::Data);
                projection.attributes.get(name) == Some(&DescriptorAttributes::literal(data))
                    && match (kind, projection.properties.get(name)) {
                        (RecordInputProperty::Data, Some(CapturedPropertyDescriptor::Data(_))) => {
                            true
                        }
                        (
                            RecordInputProperty::Accessor { getter, setter },
                            Some(CapturedPropertyDescriptor::Accessor {
                                getter: actual_getter,
                                setter: actual_setter,
                            }),
                        ) => {
                            *getter == !actual_getter.is_empty()
                                && *setter == !actual_setter.is_empty()
                        }
                        _ => false,
                    }
            })
    }
}

/// Conditional query evidence. Only a consumer that checks the complete input
/// layout may instantiate its parameterized footprint and body coverage.
pub(super) fn conditional_input_projection(
    shape: &RecordInputShape,
    input: SymbolicInputId,
    request: OwnPropertyRequest,
) -> Option<OwnPropertyProof> {
    use super::value_model::ProviderInput;
    let ProviderInput::Argument(argument) = input.input else {
        return None;
    };
    let (native_call, names_only) = match request {
        OwnPropertyRequest::Names { native_call } => (native_call, true),
        OwnPropertyRequest::Descriptors { native_call } => (native_call, false),
        _ => return None,
    };
    let names = ordered_own_property_names(&shape.creation_order);
    let input_scope = input.scope;
    let symbolic = |input| {
        Values::from([AbstractValue::SymbolicInput(SymbolicInputId {
            scope: input_scope,
            input,
        })])
    };
    let mut properties = BTreeMap::new();
    let mut attributes = BTreeMap::new();
    let mut retained_values = Values::new();
    for (property, (name, kind)) in shape.properties.iter().enumerate() {
        let data = matches!(kind, RecordInputProperty::Data);
        let descriptor = match kind {
            RecordInputProperty::Data => {
                CapturedPropertyDescriptor::Data(symbolic(ProviderInput::Property {
                    argument,
                    property,
                }))
            }
            RecordInputProperty::Accessor { getter, setter } => {
                CapturedPropertyDescriptor::Accessor {
                    getter: if *getter {
                        symbolic(ProviderInput::Getter { argument, property })
                    } else {
                        Values::new()
                    },
                    setter: if *setter {
                        symbolic(ProviderInput::Setter { argument, property })
                    } else {
                        Values::new()
                    },
                }
            }
        };
        match &descriptor {
            CapturedPropertyDescriptor::Data(values) => {
                retained_values.extend(values.iter().cloned())
            }
            CapturedPropertyDescriptor::Accessor { getter, setter } => {
                retained_values.extend(getter.iter().cloned());
                retained_values.extend(setter.iter().cloned());
            }
        }
        properties.insert(name.clone(), descriptor);
        attributes.insert(name.clone(), DescriptorAttributes::literal(data));
    }
    Some(OwnPropertyProof {
        projection: if names_only {
            OwnPropertyProjection::Names(OwnPropertyNamesProjection { names })
        } else {
            OwnPropertyProjection::Descriptors(OwnPropertyDescriptorProjection {
                source_object: DescriptorSource::Input(input),
                ordered_names: names,
                properties,
                attributes,
                retained_values,
            })
        },
        conclusion: InferenceRule::OwnPropertyPrefixState,
        derivation_rules: BTreeSet::from([InferenceRule::NativeCallIdentity]),
        dependencies: native_call.dependencies,
    })
}

impl OwnPropertyProof {
    pub(super) fn inference_use(&self) -> InferenceUse {
        InferenceUse {
            conclusion: self.conclusion,
            derivation_rules: self.derivation_rules.clone(),
            value_input_refs: self.dependencies.value_inputs.clone(),
            descriptor_copy_author_refs: self.dependencies.descriptor_copy_authors.clone(),
            argument_input_refs: self.dependencies.argument_inputs.clone(),
            native_operation_input_refs: self.dependencies.native_operation_inputs.clone(),
            imported_primitive_array_spread_input_refs: self
                .dependencies
                .imported_primitive_array_spread_inputs
                .clone(),
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
    source_object: HeapAddress,
    resolver: SourceHeapResolver,
    ordered_names: &[String],
    shape: &OwnPropertyShape,
    prefix_properties: &BTreeMap<Option<String>, Values>,
    callbacks_model: DescriptorCallbackModel,
    literal_descriptors_intact: bool,
) -> Option<OwnPropertyDescriptorProjection> {
    let mut properties = BTreeMap::new();
    let mut attributes = BTreeMap::new();
    for name in ordered_names {
        let values = prefix_properties.get(&Some(name.clone()))?;
        let descriptor = match shape.properties.get(name)? {
            OwnPropertyDescriptorSyntax::Data => CapturedPropertyDescriptor::Data(values.clone()),
            OwnPropertyDescriptorSyntax::Accessor { getter, setter } => {
                let expected: BTreeSet<_> = getter
                    .iter()
                    .chain(setter.iter())
                    .map(|origin| resolver.allocation(Kind::Function, *origin))
                    .collect();
                let mut callbacks = Values::new();
                for value in values {
                    let key = callbacks_model.key(value, resolver)?;
                    if !expected.contains(&key) {
                        return None;
                    }
                    callbacks.insert(value.clone());
                }
                let callbacks_for = |key: Option<u32>| {
                    key.map_or_else(Values::new, |key| {
                        let key = resolver.allocation(Kind::Function, key);
                        callbacks
                            .iter()
                            .filter(|value| callbacks_model.key(value, resolver) == Some(key))
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
        attributes.insert(
            name.clone(),
            if literal_descriptors_intact {
                DescriptorAttributes::literal(matches!(
                    descriptor,
                    CapturedPropertyDescriptor::Data(_)
                ))
            } else {
                DescriptorAttributes::default()
            },
        );
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
        source_object: DescriptorSource::Object(source_object),
        ordered_names: ordered_names.to_vec(),
        properties,
        attributes,
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

pub(super) struct OwnPropertyPrefix<'a> {
    pub(super) heap_resolver: SourceHeapResolver,
    pub(super) source_object: HeapAddress,
    pub(super) source_values: &'a Values,
    pub(super) shape: &'a OwnPropertyShape,
    pub(super) properties: &'a BTreeMap<Option<String>, Values>,
    pub(super) tainted: bool,
    pub(super) literal_descriptors_intact: bool,
}

pub(super) fn infer_own_property_projection(
    prefix: OwnPropertyPrefix<'_>,
    semantic_facts: Option<&SemanticFacts>,
    callbacks_model: DescriptorCallbackModel,
    request: OwnPropertyRequest,
) -> Option<OwnPropertyProof> {
    let OwnPropertyPrefix {
        heap_resolver,
        source_object,
        source_values,
        shape,
        properties: prefix_properties,
        tainted,
        literal_descriptors_intact,
    } = prefix;
    let source_is_exact_object = source_values.len() == 1
        && matches!(
            source_values.first(),
            Some(AbstractValue::Value(Value::Alloc(Kind::Object, object)))
                if object.in_scope_allocation(Kind::Object, heap_resolver) == Some(source_object)
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
        OwnPropertyRequest::Names { native_call } => {
            dependencies.extend(&native_call.dependencies);
            derivation_rules.extend(native_call.inference_use.derivation_rules);
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
                semantic_facts?,
                &semantic_call,
                &mut dependencies,
                &mut derivation_rules,
            )?;
            let projection = descriptor_projection(
                source_object,
                heap_resolver,
                &ordered_names,
                shape,
                prefix_properties,
                callbacks_model,
                literal_descriptors_intact,
            )?;
            derivation_rules.insert(InferenceRule::DescriptorCopyFactory);
            (
                InferenceRule::DescriptorCopyFactory,
                OwnPropertyProjection::Descriptors(projection),
            )
        }
        // The `%Object.getOwnPropertyDescriptors%` identity already holds
        // the Object family and global object intact.
        request @ (OwnPropertyRequest::Descriptors { .. }
        | OwnPropertyRequest::DescriptorLayout) => {
            if let OwnPropertyRequest::Descriptors { native_call } = request {
                dependencies.extend(&native_call.dependencies);
                derivation_rules.extend(native_call.inference_use.derivation_rules);
            }
            let projection = descriptor_projection(
                source_object,
                heap_resolver,
                &ordered_names,
                shape,
                prefix_properties,
                callbacks_model,
                literal_descriptors_intact,
            )?;
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

/// Project a checked layout from the current shared store into the same
/// descriptor product used by source-shape inference. The store certificate
/// supplies current values and key order; operation-specific native or author
/// premises remain independent requirements of `request`.
pub(super) fn infer_current_own_property_projection(
    source_object: HeapAddress,
    current: &CurrentOwnPropertyProjection,
    semantic_facts: Option<&SemanticFacts>,
    request: OwnPropertyRequest,
) -> Option<OwnPropertyProof> {
    let mut properties = BTreeMap::new();
    let mut attributes = BTreeMap::new();
    let mut retained_values = Values::new();
    for (name, descriptor) in &current.descriptors {
        let (descriptor, descriptor_attributes) = match descriptor {
            CurrentOwnPropertyDescriptor::Data { values, attributes } => {
                retained_values.extend(values.iter().cloned());
                (
                    CapturedPropertyDescriptor::Data(values.clone()),
                    *attributes,
                )
            }
            CurrentOwnPropertyDescriptor::Accessor {
                getter,
                setter,
                attributes,
            } => {
                retained_values.extend(getter.iter().cloned());
                retained_values.extend(setter.iter().cloned());
                (
                    CapturedPropertyDescriptor::Accessor {
                        getter: getter.clone(),
                        setter: setter.clone(),
                    },
                    *attributes,
                )
            }
        };
        properties.insert(name.clone(), descriptor);
        attributes.insert(name.clone(), descriptor_attributes);
    }
    let descriptor_projection = OwnPropertyDescriptorProjection {
        source_object: DescriptorSource::Object(source_object),
        ordered_names: current.layout.ordered_keys.clone(),
        properties,
        attributes,
        retained_values,
    };
    let mut derivation_rules = BTreeSet::from([InferenceRule::OwnPropertyStoreLayout]);
    let mut dependencies = current.dependencies.clone();
    let (conclusion, projection) = match request {
        OwnPropertyRequest::Ordinary => return None,
        OwnPropertyRequest::DescriptorLayout => (
            InferenceRule::OwnPropertyDescriptorProjection,
            OwnPropertyProjection::Descriptors(descriptor_projection),
        ),
        OwnPropertyRequest::AuditedDescriptorCopy { semantic_call } => {
            require_native_certificate(
                semantic_facts?,
                &semantic_call,
                &mut dependencies,
                &mut derivation_rules,
            )?;
            derivation_rules.insert(InferenceRule::DescriptorCopyFactory);
            (
                InferenceRule::DescriptorCopyFactory,
                OwnPropertyProjection::Descriptors(descriptor_projection),
            )
        }
        OwnPropertyRequest::Names { native_call } => {
            dependencies.extend(&native_call.dependencies);
            derivation_rules.extend(native_call.inference_use.derivation_rules);
            derivation_rules.insert(InferenceRule::OwnPropertyNamesProjection);
            (
                InferenceRule::OwnPropertyNamesProjection,
                OwnPropertyProjection::Names(OwnPropertyNamesProjection {
                    names: current.layout.ordered_keys.clone(),
                }),
            )
        }
        OwnPropertyRequest::Descriptors { native_call } => {
            dependencies.extend(&native_call.dependencies);
            derivation_rules.extend(native_call.inference_use.derivation_rules);
            derivation_rules.insert(InferenceRule::OwnPropertyDescriptorProjection);
            (
                InferenceRule::OwnPropertyDescriptorProjection,
                OwnPropertyProjection::Descriptors(descriptor_projection),
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
    use super::super::object_surface::{
        CurrentOwnPropertyDescriptor, CurrentOwnPropertyProjection, OwnPropertyLayout,
        OwnPropertyLayoutDescriptor, OwnPropertyLayoutKind,
    };
    use super::super::value_model::{
        AbstractValue, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values,
        ordered_own_property_names,
    };
    use super::{
        DescriptorCallbackModel, OwnPropertyPrefix, OwnPropertyProjection, OwnPropertyRequest,
        infer_current_own_property_projection, infer_own_property_projection,
    };
    use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies, ValueInputRef};
    use crate::analysis_provenance::InferenceRule;
    use crate::facts::abstract_domain::integrity::{
        IntegrityFlags, IntrinsicSlot, PrototypeWrites,
    };
    use crate::facts::abstract_domain::value::{
        HeapModuleScope, Kind, LocalAnalysisScopeId, SourceHeapResolver, Value,
    };
    use crate::facts::semantic_inference::native_premise;
    use std::collections::{BTreeMap, BTreeSet};

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
    fn current_store_layout_projects_current_values_without_source_rescan() {
        let resolver =
            SourceHeapResolver::new(HeapModuleScope::LocalAnalysis(LocalAnalysisScopeId::fresh()));
        let source_object = resolver.allocation(Kind::Object, 11);
        let attributes = super::DescriptorAttributes::literal(true);
        let layout = OwnPropertyLayout::from_creation_order(
            &["slot".to_owned()],
            BTreeMap::from([(
                "slot".to_owned(),
                OwnPropertyLayoutDescriptor {
                    kind: OwnPropertyLayoutKind::Data,
                    attributes,
                },
            )]),
        )
        .unwrap();
        let current = CurrentOwnPropertyProjection {
            layout,
            descriptors: BTreeMap::from([(
                "slot".to_owned(),
                CurrentOwnPropertyDescriptor::Data {
                    values: Values::from([AbstractValue::PRIMITIVE]),
                    attributes,
                },
            )]),
            dependencies: ProofDependencies::default(),
        };

        let proof = infer_current_own_property_projection(
            source_object,
            &current,
            None,
            OwnPropertyRequest::DescriptorLayout,
        )
        .unwrap();
        assert_eq!(
            proof.inference_use().derivation_rules,
            BTreeSet::from([InferenceRule::OwnPropertyStoreLayout])
        );
        let OwnPropertyProjection::Descriptors(projection) = proof.projection else {
            panic!("current store layout must produce a descriptor projection");
        };
        assert_eq!(
            projection.source_object,
            super::DescriptorSource::Object(source_object)
        );
        assert_eq!(projection.ordered_names, ["slot"]);
        assert_eq!(projection.attributes["slot"], attributes);
        assert!(matches!(
            &projection.properties["slot"],
            super::CapturedPropertyDescriptor::Data(values)
                if values.contains(&AbstractValue::PRIMITIVE)
        ));
    }

    #[test]
    fn ordinary_projection_has_no_native_call_premise() {
        let resolver =
            SourceHeapResolver::new(HeapModuleScope::LocalAnalysis(LocalAnalysisScopeId::fresh()));
        let source_object = resolver.allocation(Kind::Object, 7);
        let source_values = Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            source_object,
        ))]);
        let shape = OwnPropertyShape {
            properties: BTreeMap::from([("field".to_owned(), OwnPropertyDescriptorSyntax::Data)]),
            creation_order: vec!["field".to_owned()],
        };
        let prefix_properties = BTreeMap::from([(
            Some("field".to_owned()),
            Values::from([AbstractValue::PRIMITIVE]),
        )]);
        let proof = infer_own_property_projection(
            OwnPropertyPrefix {
                source_object,
                heap_resolver: resolver,
                source_values: &source_values,
                shape: &shape,
                properties: &prefix_properties,
                tainted: false,
                literal_descriptors_intact: true,
            },
            None,
            DescriptorCallbackModel::CapturedClosure,
            OwnPropertyRequest::Ordinary,
        )
        .expect("ordinary source projection");

        assert!(matches!(
            proof.projection,
            OwnPropertyProjection::Ordinary { .. }
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
    fn equal_allocation_offsets_in_different_chunks_do_not_share_object_identity() {
        let local = SourceHeapResolver::new(HeapModuleScope::Chunk(source_identity::ChunkId(0)));
        let other = SourceHeapResolver::new(HeapModuleScope::Chunk(source_identity::ChunkId(1)));
        let local_object = local.allocation(Kind::Object, 7);
        let other_object = other.allocation(Kind::Object, 7);
        assert_ne!(local_object, other_object);

        let source_values = Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            local_object,
        ))]);
        let shape = OwnPropertyShape {
            properties: BTreeMap::from([("field".to_owned(), OwnPropertyDescriptorSyntax::Data)]),
            creation_order: vec!["field".to_owned()],
        };
        let prefix_properties = BTreeMap::from([(
            Some("field".to_owned()),
            Values::from([AbstractValue::PRIMITIVE]),
        )]);

        assert!(
            infer_own_property_projection(
                OwnPropertyPrefix {
                    heap_resolver: local,
                    source_object: other_object,
                    source_values: &source_values,
                    shape: &shape,
                    properties: &prefix_properties,
                    tainted: false,
                    literal_descriptors_intact: true,
                },
                None,
                DescriptorCallbackModel::CapturedClosure,
                OwnPropertyRequest::Ordinary,
            )
            .is_none()
        );
    }

    #[test]
    fn native_projection_imports_the_call_premise_provenance() {
        let resolver =
            SourceHeapResolver::new(HeapModuleScope::LocalAnalysis(LocalAnalysisScopeId::fresh()));
        let source_object = resolver.allocation(Kind::Object, 7);
        let source_values = Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            source_object,
        ))]);
        let shape = OwnPropertyShape {
            properties: BTreeMap::from([("field".to_owned(), OwnPropertyDescriptorSyntax::Data)]),
            creation_order: vec!["field".to_owned()],
        };
        let prefix_properties = BTreeMap::from([(
            Some("field".to_owned()),
            Values::from([AbstractValue::PRIMITIVE]),
        )]);
        let value_input = ValueInputRef {
            defining_chunk: "native-census".to_owned(),
            input_index: 3,
        };
        let integrity = IntegrityFlags::new(
            true,
            BTreeSet::new(),
            &[],
            ProofDependencies {
                value_inputs: BTreeSet::from([value_input.clone()]),
                ..Default::default()
            },
            BTreeMap::new(),
            PrototypeWrites::default(),
        );
        let native_call = native_premise(
            &integrity,
            &[
                IntrinsicSlot::Global(NativeIntrinsic::Object),
                IntrinsicSlot::Static(NativeIntrinsic::Object, "getOwnPropertyNames"),
            ],
            None,
            InferenceRule::NativeCallIdentity,
        )
        .expect("intact census");

        let proof = infer_own_property_projection(
            OwnPropertyPrefix {
                source_object,
                heap_resolver: resolver,
                source_values: &source_values,
                shape: &shape,
                properties: &prefix_properties,
                tainted: false,
                literal_descriptors_intact: true,
            },
            None,
            DescriptorCallbackModel::CapturedClosure,
            OwnPropertyRequest::Names {
                native_call: native_call.clone(),
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
        assert_eq!(proof.dependencies, native_call.dependencies);
        assert_eq!(
            proof.dependencies.value_inputs,
            BTreeSet::from([value_input])
        );
    }
}
