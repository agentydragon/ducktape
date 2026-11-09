//! Shared own-property and object-surface state for source and provider transfers.
//!
//! This store owns only may-state. A successful mutation here does not prove
//! that the JavaScript operation completed normally; completion remains with
//! the operation transfer that preflighted it.

use super::property_inference::DescriptorAttributes;
use super::value_model::{
    AbstractValue, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values,
    ordered_own_property_names,
};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::value::HeapAddress;
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum WritablePrefixState {
    InProgress,
    Proven(BTreeSet<String>),
    DenseArray { length: u32 },
    Unknown,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub(super) struct AppliedAccessor {
    pub(super) getter: Values,
    pub(super) setter: Values,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum PrototypeKnowledge {
    Unknown,
    /// The prototype is known to have no further own-property behavior in the
    /// modeled domain. No current source path establishes this state yet.
    KnownNull,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ExtensibilityKnowledge {
    Unknown,
    Extensible,
}

#[derive(Debug, Clone, PartialEq)]
pub(super) enum OwnPropertyKnowledge {
    KnownAbsent,
    Data {
        values: Values,
        attributes: DescriptorAttributes,
    },
    Accessor {
        getter: Values,
        setter: Values,
        attributes: DescriptorAttributes,
    },
    Unknown {
        retained_values: Values,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum OwnPropertyLayoutKind {
    Data,
    Accessor,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct OwnPropertyLayoutDescriptor {
    pub(super) kind: OwnPropertyLayoutKind,
    pub(super) attributes: DescriptorAttributes,
}

/// A complete ordered own-property descriptor surface. `ordered_keys` uses
/// ECMAScript string-key order (array indices first in numeric order, then
/// other strings in insertion order); symbols remain outside this model.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OwnPropertyLayout {
    pub(super) ordered_keys: Vec<String>,
    pub(super) descriptors: BTreeMap<String, OwnPropertyLayoutDescriptor>,
}

impl OwnPropertyLayout {
    pub(super) fn from_creation_order(
        creation_order: &[String],
        descriptors: BTreeMap<String, OwnPropertyLayoutDescriptor>,
    ) -> Option<Self> {
        let ordered_keys = ordered_own_property_names(creation_order);
        let layout = Self {
            ordered_keys,
            descriptors,
        };
        layout.is_complete().then_some(layout)
    }

    pub(super) fn is_complete(&self) -> bool {
        let keys = self.ordered_keys.iter().cloned().collect::<BTreeSet<_>>();
        if keys.len() != self.ordered_keys.len()
            || keys != self.descriptors.keys().cloned().collect()
            || ordered_own_property_names(&self.ordered_keys) != self.ordered_keys
        {
            return false;
        }
        self.descriptors
            .values()
            .all(|descriptor| match descriptor.kind {
                OwnPropertyLayoutKind::Data => {
                    descriptor.attributes.writable.is_some()
                        && descriptor.attributes.enumerable.is_some()
                        && descriptor.attributes.configurable.is_some()
                }
                OwnPropertyLayoutKind::Accessor => {
                    descriptor.attributes.writable.is_none()
                        && descriptor.attributes.enumerable.is_some()
                        && descriptor.attributes.configurable.is_some()
                }
            })
    }

    pub(super) fn keys(&self) -> BTreeSet<String> {
        self.ordered_keys.iter().cloned().collect()
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct OwnPropertyCertificate {
    pub(super) must_present: BTreeSet<String>,
    pub(super) exact_keys: Option<BTreeSet<String>>,
    pub(super) layout: Option<OwnPropertyLayout>,
    /// Premises consumed by the surviving own-property evidence. These are
    /// unioned across represented generations even when stronger layout
    /// evidence disagrees.
    pub(super) dependencies: ProofDependencies,
}

impl OwnPropertyCertificate {
    fn unknown() -> Self {
        Self::default()
    }
}

#[derive(Debug, Clone, PartialEq)]
pub(super) enum CurrentOwnPropertyDescriptor {
    Data {
        values: Values,
        attributes: DescriptorAttributes,
    },
    Accessor {
        getter: Values,
        setter: Values,
        attributes: DescriptorAttributes,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub(super) struct CurrentOwnPropertyProjection {
    pub(super) layout: OwnPropertyLayout,
    pub(super) descriptors: BTreeMap<String, CurrentOwnPropertyDescriptor>,
    pub(super) dependencies: ProofDependencies,
}

/// COW-owned property state shared by application and provider transfers.
/// Existing side maps are grouped here so every surface read, write, native
/// descriptor operation, and invalidation uses one owner and one revision.
#[derive(Clone, Default, PartialEq)]
pub(super) struct ObjectSurfaceStore {
    pub(super) prefix_object_properties: OrdMap<HeapAddress, BTreeMap<Option<String>, Values>>,
    pub(super) writable_prefix: OrdMap<HeapAddress, WritablePrefixState>,
    pub(super) applied_accessors: OrdMap<HeapAddress, BTreeMap<String, AppliedAccessor>>,
    pub(super) property_attributes: OrdMap<HeapAddress, BTreeMap<String, DescriptorAttributes>>,
    pub(super) prototypes: OrdMap<HeapAddress, PrototypeKnowledge>,
    pub(super) extensibility: OrdMap<HeapAddress, ExtensibilityKnowledge>,
    /// Historical allocation-site execution used only to widen values across
    /// repeated literal copies. Unlike `ordinary_spread_copies`, this is not
    /// a current surface certificate and survives invalidation.
    pub(super) spread_copy_history: BTreeSet<HeapAddress>,
    pub(super) ordinary_spread_copies: BTreeSet<HeapAddress>,
    pub(super) copy_enumerability_unknown: BTreeSet<HeapAddress>,
    pub(super) spread_shapes: OrdMap<HeapAddress, Option<OwnPropertyShape>>,
    /// Independent own-property facets for pooled allocations. Must-presence
    /// survives a weak join when it is common to every path; exact absence and
    /// ordered descriptor projection require their stronger certificates.
    /// An empty certificate is sticky uncertainty after an unmodeled mutation.
    pub(super) own_property_certificates: OrdMap<HeapAddress, OwnPropertyCertificate>,
    /// Exact own index sets observed for repeated array-spread allocations.
    /// `None` means generations disagreed or one generation had unknown slots.
    pub(super) array_spread_shapes: OrdMap<HeapAddress, Option<BTreeSet<String>>>,
    pub(super) tainted_objects: BTreeSet<HeapAddress>,
    pub(super) unsettled_objects: BTreeSet<HeapAddress>,
}

impl ObjectSurfaceStore {
    pub(super) fn has_accessor_property(&self, object: HeapAddress, property: &str) -> bool {
        self.applied_accessors
            .get(&object)
            .is_some_and(|properties| properties.contains_key(property))
    }

    pub(super) fn has_any_accessors(&self, object: HeapAddress) -> bool {
        self.applied_accessors
            .get(&object)
            .is_some_and(|properties| !properties.is_empty())
    }

    pub(super) fn own_properties(
        &self,
        object: HeapAddress,
    ) -> Option<&BTreeMap<Option<String>, Values>> {
        self.prefix_object_properties.get(&object)
    }

    pub(super) fn has_surface_state(&self, object: HeapAddress) -> bool {
        self.prefix_object_properties.contains_key(&object)
            || self.applied_accessors.contains_key(&object)
            || self.property_attributes.contains_key(&object)
            || self.prototypes.contains_key(&object)
            || self.extensibility.contains_key(&object)
            || self.spread_copy_history.contains(&object)
            || self.ordinary_spread_copies.contains(&object)
            || self.copy_enumerability_unknown.contains(&object)
            || self.spread_shapes.contains_key(&object)
            || self.own_property_certificates.contains_key(&object)
            || self.array_spread_shapes.contains_key(&object)
            || self.writable_prefix.contains_key(&object)
            || self.tainted_objects.contains(&object)
            || self.unsettled_objects.contains(&object)
    }

    pub(super) fn retained_values(&self, object: HeapAddress) -> Values {
        let mut retained = self
            .prefix_object_properties
            .get(&object)
            .into_iter()
            .flat_map(BTreeMap::values)
            .flatten()
            .cloned()
            .collect::<Values>();
        if let Some(accessors) = self.applied_accessors.get(&object) {
            for accessor in accessors.values() {
                retained.extend(accessor.getter.iter().cloned());
                retained.extend(accessor.setter.iter().cloned());
            }
        }
        retained
    }

    pub(super) fn property_payloads(&self, object: HeapAddress, property: Option<&str>) -> Values {
        let Some(properties) = self.prefix_object_properties.get(&object) else {
            return Values::new();
        };
        match property {
            Some(property) => properties
                .get(&Some(property.to_owned()))
                .into_iter()
                .flatten()
                .chain(properties.get(&None).into_iter().flatten())
                .cloned()
                .collect(),
            None => properties.values().flatten().cloned().collect(),
        }
    }

    pub(super) fn accessor_getters(&self, object: HeapAddress, property: Option<&str>) -> Values {
        let Some(accessors) = self.applied_accessors.get(&object) else {
            return Values::new();
        };
        match property {
            Some(property) => accessors
                .get(property)
                .map_or_else(Values::new, |accessor| accessor.getter.clone()),
            None => accessors
                .values()
                .flat_map(|accessor| accessor.getter.iter().cloned())
                .collect(),
        }
    }

    pub(super) fn data_values(&self, object: HeapAddress, property: &str) -> Option<&Values> {
        if self.tainted_objects.contains(&object)
            || self.unsettled_objects.contains(&object)
            || !self.own_property_is_guaranteed(object, property)
            || self
                .applied_accessors
                .get(&object)
                .is_some_and(|properties| properties.contains_key(property))
        {
            return None;
        }
        self.prefix_object_properties
            .get(&object)?
            .get(&Some(property.to_owned()))
    }

    pub(super) fn attributes(
        &self,
        object: HeapAddress,
        property: &str,
    ) -> Option<DescriptorAttributes> {
        self.property_attributes
            .get(&object)?
            .get(property)
            .copied()
    }

    pub(super) fn own_property(
        &self,
        object: HeapAddress,
        property: &str,
        known_absent: bool,
    ) -> OwnPropertyKnowledge {
        let values = self
            .prefix_object_properties
            .get(&object)
            .and_then(|properties| properties.get(&Some(property.to_owned())))
            .cloned();
        let dynamic_values = self
            .prefix_object_properties
            .get(&object)
            .and_then(|properties| properties.get(&None))
            .cloned()
            .unwrap_or_default();
        let accessors = self
            .applied_accessors
            .get(&object)
            .and_then(|properties| properties.get(property));
        let attributes = self
            .property_attributes
            .get(&object)
            .and_then(|properties| properties.get(property))
            .copied()
            .unwrap_or_default();
        let definitely_present = self.own_property_is_guaranteed(object, property);
        let definitely_absent = self
            .own_property_certificates
            .get(&object)
            .and_then(|certificate| certificate.exact_keys.as_ref())
            .is_some_and(|keys| !keys.contains(property));
        if self.tainted_objects.contains(&object) || self.unsettled_objects.contains(&object) {
            let mut retained_values = values.unwrap_or_default();
            retained_values.extend(dynamic_values);
            if let Some(accessors) = accessors {
                retained_values.extend(accessors.getter.iter().cloned());
                retained_values.extend(accessors.setter.iter().cloned());
            }
            return OwnPropertyKnowledge::Unknown { retained_values };
        }
        match (accessors, values, definitely_present) {
            (Some(accessors), None, true) => OwnPropertyKnowledge::Accessor {
                getter: accessors.getter.clone(),
                setter: accessors.setter.clone(),
                attributes,
            },
            (Some(accessors), values, _) => OwnPropertyKnowledge::Unknown {
                retained_values: values
                    .unwrap_or_default()
                    .into_iter()
                    .chain(dynamic_values)
                    .chain(accessors.getter.iter().chain(&accessors.setter).cloned())
                    .collect(),
            },
            (None, Some(values), true) => OwnPropertyKnowledge::Data { values, attributes },
            (None, None, _) if known_absent && definitely_absent && dynamic_values.is_empty() => {
                OwnPropertyKnowledge::KnownAbsent
            }
            (None, values, _) => OwnPropertyKnowledge::Unknown {
                retained_values: values
                    .unwrap_or_default()
                    .into_iter()
                    .chain(dynamic_values)
                    .collect(),
            },
        }
    }

    fn own_property_is_guaranteed(&self, object: HeapAddress, property: &str) -> bool {
        self.own_property_certificates
            .get(&object)
            .is_some_and(|certificate| certificate.must_present.contains(property))
    }

    /// Register one allocation generation's exact own keys and, when
    /// available, its complete ordered descriptor layout. Weakly different
    /// known generations retain only keys present in every generation; exact
    /// absence and full layout remain available only while each facet agrees.
    /// An unmodeled generation retires all surface authority permanently.
    pub(super) fn observe_own_property_evidence(
        &mut self,
        object: HeapAddress,
        exact_keys: Option<BTreeSet<String>>,
        layout: Option<OwnPropertyLayout>,
        dependencies: ProofDependencies,
    ) -> bool {
        let layout_keys = match layout.as_ref() {
            Some(layout) if layout.is_complete() => Some(layout.keys()),
            Some(_) => None,
            None => None,
        };
        let exact_keys = match (exact_keys, layout_keys) {
            (Some(exact), Some(layout)) if exact == layout => Some(exact),
            (Some(exact), None) if layout.is_none() => Some(exact),
            (None, Some(layout)) => Some(layout),
            (None, None) if layout.is_none() => None,
            _ => None,
        };
        let layout = layout.filter(OwnPropertyLayout::is_complete);
        if exact_keys.is_none() {
            return self.invalidate_surface(object);
        }
        let exact_keys = exact_keys.expect("checked exact-key evidence");
        let Some(previous) = self.own_property_certificates.get(&object).cloned() else {
            self.own_property_certificates.insert(
                object,
                OwnPropertyCertificate {
                    must_present: exact_keys.clone(),
                    exact_keys: Some(exact_keys),
                    dependencies,
                    layout,
                },
            );
            return true;
        };

        let mut next = OwnPropertyCertificate {
            must_present: previous
                .must_present
                .intersection(&exact_keys)
                .cloned()
                .collect(),
            exact_keys: previous
                .exact_keys
                .as_ref()
                .filter(|previous| *previous == &exact_keys)
                .cloned(),
            layout: previous
                .layout
                .as_ref()
                .filter(|previous| Some(*previous) == layout.as_ref())
                .cloned(),
            dependencies: {
                let mut combined = previous.dependencies.clone();
                combined.extend(&dependencies);
                combined
            },
        };
        // Layout disagreement is sticky, even if a later generation happens
        // to return to the original order or descriptor attributes.
        if previous.layout.is_none() {
            next.layout = None;
        }
        // Attribute state is an independent facet of the property store, so
        // dropping the ordered layout cannot leave attributes from an older
        // pooled generation looking current. Retain only attributes shared by
        // both complete generations for keys that remain must-present. If
        // either generation lacks a complete layout, those attributes become
        // unknown while the separately proven presence can survive.
        let mut attributes_changed = false;
        if (layout.is_none() || previous.layout.as_ref() != layout.as_ref())
            && let Some(attributes) = self.property_attributes.get_mut(&object)
        {
            for property in &next.must_present {
                let generation_attributes = previous
                    .layout
                    .as_ref()
                    .and_then(|layout| layout.descriptors.get(property))
                    .zip(
                        layout
                            .as_ref()
                            .and_then(|layout| layout.descriptors.get(property)),
                    )
                    .map_or(DescriptorAttributes::default(), |(previous, current)| {
                        join_attributes(previous.attributes, current.attributes)
                    });
                if let Some(current_attributes) = attributes.get_mut(property) {
                    let joined = join_attributes(*current_attributes, generation_attributes);
                    attributes_changed |= *current_attributes != joined;
                    *current_attributes = joined;
                }
            }
        }
        let changed =
            attributes_changed || self.own_property_certificates.get(&object) != Some(&next);
        if changed {
            self.own_property_certificates.insert(object, next);
        }
        changed
    }

    /// Record a complete checked surface directly when the producer has no
    /// weaker separate key-set observation.
    pub(super) fn observe_own_property_layout(
        &mut self,
        object: HeapAddress,
        layout: Option<OwnPropertyLayout>,
        dependencies: ProofDependencies,
    ) -> bool {
        let keys = layout.as_ref().map(OwnPropertyLayout::keys);
        self.observe_own_property_evidence(object, keys, layout, dependencies)
    }

    #[cfg(test)]
    pub(super) fn observe_own_property_keys(
        &mut self,
        object: HeapAddress,
        exact_keys: Option<BTreeSet<String>>,
    ) -> bool {
        self.observe_own_property_evidence(object, exact_keys, None, ProofDependencies::default())
    }

    pub(super) fn current_own_property_projection(
        &self,
        object: HeapAddress,
    ) -> Option<CurrentOwnPropertyProjection> {
        if self.tainted_objects.contains(&object) || self.unsettled_objects.contains(&object) {
            return None;
        }
        let certificate = self.own_property_certificates.get(&object)?;
        let layout = certificate.layout.clone()?;
        if certificate.exact_keys.as_ref() != Some(&layout.keys())
            || certificate.must_present != layout.keys()
        {
            return None;
        }
        let empty_properties = BTreeMap::new();
        let properties = self
            .prefix_object_properties
            .get(&object)
            .unwrap_or(&empty_properties);
        if properties
            .get(&None)
            .is_some_and(|values| !values.is_empty())
        {
            return None;
        }
        let data_keys = properties
            .iter()
            .filter_map(|(property, values)| {
                property.as_ref().filter(|_| !values.is_empty()).cloned()
            })
            .collect::<BTreeSet<_>>();
        let accessors = self.applied_accessors.get(&object);
        let accessor_keys = accessors
            .into_iter()
            .flat_map(|properties| properties.keys().cloned())
            .collect::<BTreeSet<_>>();
        if !data_keys.is_disjoint(&accessor_keys)
            || data_keys
                .union(&accessor_keys)
                .cloned()
                .collect::<BTreeSet<_>>()
                != layout.keys()
        {
            return None;
        }
        let empty_attributes = BTreeMap::new();
        let attributes = self
            .property_attributes
            .get(&object)
            .unwrap_or(&empty_attributes);
        if attributes.keys().cloned().collect::<BTreeSet<_>>() != layout.keys() {
            return None;
        }
        let mut descriptors = BTreeMap::new();
        for (name, expected) in &layout.descriptors {
            let current_attributes = attributes.get(name).copied()?;
            if current_attributes != expected.attributes {
                return None;
            }
            let current = match expected.kind {
                OwnPropertyLayoutKind::Data => CurrentOwnPropertyDescriptor::Data {
                    values: properties.get(&Some(name.clone()))?.clone(),
                    attributes: current_attributes,
                },
                OwnPropertyLayoutKind::Accessor => {
                    let accessor = accessors?.get(name)?;
                    CurrentOwnPropertyDescriptor::Accessor {
                        getter: accessor.getter.clone(),
                        setter: accessor.setter.clone(),
                        attributes: current_attributes,
                    }
                }
            };
            descriptors.insert(name.clone(), current);
        }
        Some(CurrentOwnPropertyProjection {
            layout,
            descriptors,
            dependencies: certificate.dependencies.clone(),
        })
    }

    /// Premises supporting the surviving own-property facets, even when their
    /// disagreement has retired the stronger ordered layout certificate.
    pub(super) fn own_property_dependencies(&self, object: HeapAddress) -> ProofDependencies {
        self.own_property_certificates
            .get(&object)
            .map(|certificate| certificate.dependencies.clone())
            .unwrap_or_default()
    }

    /// Retire descriptor authority while retaining all values and callbacks.
    /// Invalidation itself is evidence that this allocation's complete shape
    /// can no longer be used to prove presence or absence.
    pub(super) fn invalidate_surface(&mut self, object: HeapAddress) -> bool {
        let mut aliases = self.tracked_addresses();
        aliases.insert(object);
        aliases.retain(|candidate| object.may_overlap_with_provider_family(*candidate));
        let mut changed = false;
        for address in aliases {
            changed |= self.invalidate_one_surface(address);
        }
        changed
    }

    pub(super) fn tracked_addresses(&self) -> BTreeSet<HeapAddress> {
        let mut addresses = BTreeSet::new();
        addresses.extend(self.prefix_object_properties.keys().copied());
        addresses.extend(self.writable_prefix.keys().copied());
        addresses.extend(self.applied_accessors.keys().copied());
        addresses.extend(self.property_attributes.keys().copied());
        addresses.extend(self.prototypes.keys().copied());
        addresses.extend(self.extensibility.keys().copied());
        addresses.extend(self.spread_copy_history.iter().copied());
        addresses.extend(self.ordinary_spread_copies.iter().copied());
        addresses.extend(self.copy_enumerability_unknown.iter().copied());
        addresses.extend(self.spread_shapes.keys().copied());
        addresses.extend(self.own_property_certificates.keys().copied());
        addresses.extend(self.array_spread_shapes.keys().copied());
        addresses.extend(self.tainted_objects.iter().copied());
        addresses.extend(self.unsettled_objects.iter().copied());
        addresses
    }

    fn invalidate_one_surface(&mut self, object: HeapAddress) -> bool {
        let mut changed = self.tainted_objects.insert(object);
        changed |= self.unsettled_objects.insert(object);
        changed |= self.writable_prefix.get(&object) != Some(&WritablePrefixState::Unknown);
        self.writable_prefix
            .insert(object, WritablePrefixState::Unknown);
        changed |= self.copy_enumerability_unknown.insert(object);
        changed |= self.spread_shapes.get(&object) != Some(&None);
        self.spread_shapes.insert(object, None);
        changed |= self.ordinary_spread_copies.remove(&object);
        changed |= self.extensibility.get(&object) != Some(&ExtensibilityKnowledge::Unknown);
        self.extensibility
            .insert(object, ExtensibilityKnowledge::Unknown);
        changed |= self.prototypes.get(&object) != Some(&PrototypeKnowledge::Unknown);
        self.prototypes.insert(object, PrototypeKnowledge::Unknown);
        changed |=
            self.own_property_certificates.get(&object) != Some(&OwnPropertyCertificate::unknown());
        self.own_property_certificates
            .insert(object, OwnPropertyCertificate::unknown());
        if self.array_spread_shapes.contains_key(&object) {
            changed |= self.array_spread_shapes.get(&object) != Some(&None);
            self.array_spread_shapes.insert(object, None);
        }
        changed |= self.property_attributes.remove(&object).is_some();
        changed
    }
    pub(super) fn prototype(&self, object: HeapAddress) -> PrototypeKnowledge {
        self.prototypes
            .get(&object)
            .copied()
            .unwrap_or(PrototypeKnowledge::Unknown)
    }

    pub(super) fn extensibility(&self, object: HeapAddress) -> ExtensibilityKnowledge {
        self.extensibility
            .get(&object)
            .copied()
            .unwrap_or(ExtensibilityKnowledge::Unknown)
    }

    pub(super) fn set_data_values(
        &mut self,
        object: HeapAddress,
        property: Option<String>,
        values: &Values,
    ) -> bool {
        let slot = self
            .prefix_object_properties
            .entry(object)
            .or_default()
            .entry(property)
            .or_default();
        let before = slot.len();
        slot.extend(values.iter().cloned());
        slot.len() != before
    }

    /// Retain possible array payload without claiming which own index was
    /// created. A generic append therefore retires the whole surface first.
    pub(super) fn append_array_values(&mut self, object: HeapAddress, values: &Values) -> bool {
        let mut changed = self.invalidate_surface(object);
        changed |= self.set_data_values(object, None, values);
        changed
    }

    pub(super) fn record_data_properties(
        &mut self,
        object: HeapAddress,
        properties: BTreeMap<Option<String>, Values>,
        layout: Option<&OwnPropertyLayout>,
    ) -> bool {
        let mut changed = false;
        self.prefix_object_properties.entry(object).or_default();
        for (property, values) in properties {
            if let Some(name) = property {
                changed |= self.set_data_values(object, Some(name.clone()), &values);
                if let Some(descriptor) = layout.and_then(|layout| layout.descriptors.get(&name))
                    && descriptor.kind == OwnPropertyLayoutKind::Data
                {
                    changed |= self.set_attributes(object, name, descriptor.attributes);
                }
            } else {
                if !values.is_empty() {
                    changed |= self.unknown_dynamic_write(object, &values);
                }
            }
        }
        changed
    }

    pub(super) fn record_accessor_properties(
        &mut self,
        object: HeapAddress,
        properties: BTreeMap<String, AppliedAccessor>,
        layout: Option<&OwnPropertyLayout>,
    ) -> bool {
        let mut changed = false;
        for (name, accessor) in properties {
            let has_accessor_layout = layout
                .and_then(|layout| layout.descriptors.get(&name))
                .is_some_and(|descriptor| descriptor.kind == OwnPropertyLayoutKind::Accessor);
            let current = self
                .applied_accessors
                .entry(object)
                .or_default()
                .entry(name.clone())
                .or_default();
            let before = current.clone();
            current.getter.extend(accessor.getter);
            current.setter.extend(accessor.setter);
            changed |= *current != before;
            if has_accessor_layout {
                let attributes = layout
                    .and_then(|layout| layout.descriptors.get(&name))
                    .expect("checked accessor layout")
                    .attributes;
                changed |= self.set_attributes(object, name, attributes);
            }
        }
        changed
    }

    pub(super) fn record_spread_copy(
        &mut self,
        object: HeapAddress,
        properties: &[(String, Values)],
        track_shape: bool,
        dependencies: ProofDependencies,
    ) -> bool {
        let shape_keys = properties
            .iter()
            .map(|(name, _)| name.clone())
            .collect::<BTreeSet<_>>();
        let names: Vec<_> = properties.iter().map(|(name, _)| name.clone()).collect();
        let layout = track_shape.then(|| {
            OwnPropertyLayout::from_creation_order(
                &names,
                names
                    .iter()
                    .cloned()
                    .map(|name| {
                        (
                            name,
                            OwnPropertyLayoutDescriptor {
                                kind: OwnPropertyLayoutKind::Data,
                                attributes: DescriptorAttributes::literal(true),
                            },
                        )
                    })
                    .collect(),
            )
        });
        let layout = layout.flatten();
        let mut changed = self.observe_own_property_evidence(
            object,
            track_shape.then_some(shape_keys),
            layout,
            dependencies,
        );
        let previously_evaluated = self.spread_copy_history.contains(&object);
        let has_accessor_collision = properties
            .iter()
            .any(|(name, _)| self.has_accessor_property(object, name));
        if has_accessor_collision {
            changed |= self.invalidate_surface(object);
        }
        let can_certify_surface = track_shape
            && !self.tainted_objects.contains(&object)
            && !self.unsettled_objects.contains(&object);
        let next: BTreeMap<_, _> = properties
            .iter()
            .map(|(name, values)| (Some(name.clone()), values.clone()))
            .collect();
        {
            let current = self.prefix_object_properties.entry(object).or_default();
            if previously_evaluated {
                for (property, values) in current.iter_mut() {
                    if !next.contains_key(property) {
                        changed |= values.insert(AbstractValue::UNDEFINED);
                    }
                }
            }
            for (property, values) in &next {
                let new_property = !current.contains_key(property);
                let slot = current.entry(property.clone()).or_default();
                let before = slot.len();
                if previously_evaluated && new_property {
                    slot.insert(AbstractValue::UNDEFINED);
                }
                slot.extend(values.iter().cloned());
                changed |= slot.len() != before;
            }
        }
        for (name, _) in properties {
            changed |=
                self.set_attributes(object, name.clone(), DescriptorAttributes::literal(true));
        }
        let mut shape_diverged = false;
        if can_certify_surface {
            let shape = OwnPropertyShape {
                properties: names
                    .iter()
                    .cloned()
                    .map(|name| (name, OwnPropertyDescriptorSyntax::Data))
                    .collect(),
                creation_order: names,
            };
            match self.spread_shapes.get(&object) {
                Some(Some(existing)) if existing != &shape => {
                    self.spread_shapes.insert(object, None);
                    shape_diverged = true;
                    changed = true;
                }
                Some(_) => {}
                None => {
                    self.spread_shapes.insert(object, Some(shape));
                    changed = true;
                }
            }
        } else if track_shape {
            changed |= self.spread_shapes.get(&object) != Some(&None);
            self.spread_shapes.insert(object, None);
        }
        changed |= self.spread_copy_history.insert(object);
        if can_certify_surface {
            changed |= self.ordinary_spread_copies.insert(object);
        }
        if shape_diverged {
            // A pooled copy with changing own keys cannot treat a value from
            // one generation as an own data property in every generation.
            // The certificate observer has retained only common presence;
            // retire the stronger enumeration witness while keeping its
            // may-values and independent surface facts.
            changed |= self.ordinary_spread_copies.remove(&object);
            changed |= self.copy_enumerability_unknown.insert(object);
        } else if !track_shape {
            changed |= self.invalidate_surface(object);
        }
        changed
    }

    pub(super) fn observe_array_spread_shape(
        &mut self,
        object: HeapAddress,
        shape: Option<BTreeSet<String>>,
        track_shape: bool,
    ) -> bool {
        let matches_prior = self
            .array_spread_shapes
            .get(&object)
            .is_some_and(|previous| previous.as_ref() == shape.as_ref());
        let first = !self.array_spread_shapes.contains_key(&object);
        let stable = track_shape
            && shape.is_some()
            && !self.tainted_objects.contains(&object)
            && !self.unsettled_objects.contains(&object)
            && (first || matches_prior);
        if first && track_shape && shape.is_some() {
            self.array_spread_shapes.insert(object, shape);
            return true;
        }
        if matches_prior && stable {
            return true;
        }
        let known_shape_disagreement = track_shape
            && shape.is_some()
            && matches!(self.array_spread_shapes.get(&object), Some(Some(_)))
            && !self.tainted_objects.contains(&object)
            && !self.unsettled_objects.contains(&object);
        self.array_spread_shapes.insert(object, None);
        if known_shape_disagreement {
            // The complete layout and dense-array witness already lose their
            // authority through the independent generation joins. Keep
            // common own-property presence and may-values from those known
            // generations instead of erasing them as though a key were
            // dynamically unknown.
            self.writable_prefix
                .insert(object, WritablePrefixState::Unknown);
            self.copy_enumerability_unknown.insert(object);
            self.ordinary_spread_copies.remove(&object);
        } else {
            self.invalidate_surface(object);
        }
        false
    }

    pub(super) fn set_attributes(
        &mut self,
        object: HeapAddress,
        property: String,
        attributes: DescriptorAttributes,
    ) -> bool {
        if self.tainted_objects.contains(&object) || self.unsettled_objects.contains(&object) {
            return false;
        }
        let current = self
            .property_attributes
            .entry(object)
            .or_default()
            .entry(property);
        match current {
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(attributes);
                true
            }
            std::collections::btree_map::Entry::Occupied(mut entry) => {
                let joined = join_attributes(*entry.get(), attributes);
                let changed = *entry.get() != joined;
                entry.insert(joined);
                changed
            }
        }
    }

    pub(super) fn weak_set(
        &mut self,
        object: HeapAddress,
        property: String,
        values: &Values,
    ) -> bool {
        if self.has_accessor_property(object, &property) {
            return self.unknown_write(object, property, values);
        }
        self.set_data_values(object, Some(property), values)
    }

    pub(super) fn unknown_write(
        &mut self,
        object: HeapAddress,
        property: String,
        values: &Values,
    ) -> bool {
        let mut changed = self.invalidate_surface(object);
        let mut retained = values.clone();
        retained.insert(AbstractValue::Value(
            crate::facts::abstract_domain::value::Value::Top,
        ));
        changed |= self.set_data_values(object, Some(property), &retained);
        changed
    }

    pub(super) fn define_data_property(
        &mut self,
        object: HeapAddress,
        property: String,
        values: &Values,
        attributes: DescriptorAttributes,
    ) -> bool {
        let mut changed = if self.own_property_is_guaranteed(object, &property) {
            false
        } else {
            self.invalidate_surface(object)
        };
        if let Some(certificate) = self.own_property_certificates.get_mut(&object) {
            let layout_matches = certificate.layout.as_ref().is_some_and(|layout| {
                layout.descriptors.get(&property)
                    == Some(&OwnPropertyLayoutDescriptor {
                        kind: OwnPropertyLayoutKind::Data,
                        attributes,
                    })
            });
            if certificate.layout.is_some() && !layout_matches {
                certificate.layout = None;
                changed = true;
            }
        }
        if self.has_accessor_property(object, &property) {
            changed |= self.invalidate_surface(object);
        }
        changed |= self.set_data_values(object, Some(property.clone()), values);
        changed |= self.set_attributes(object, property, attributes);
        changed
    }

    /// Weak Set for an operation whose own-data/writability preflight has
    /// already succeeded. Existing descriptor state is preserved.
    pub(super) fn set_existing_data_property(
        &mut self,
        object: HeapAddress,
        property: String,
        values: &Values,
    ) -> bool {
        if matches!(
            self.own_property(object, &property, false),
            OwnPropertyKnowledge::Data {
                attributes: DescriptorAttributes {
                    writable: Some(true),
                    ..
                },
                ..
            }
        ) {
            self.weak_set(object, property, values)
        } else {
            self.unknown_write(object, property, values)
        }
    }

    /// Ordinary [[Set]] after the caller proves no own property and a safe
    /// inherited-data/extensible path. Unknown current descriptors still
    /// revoke the complete surface instead of being upgraded by the write.
    pub(super) fn set_new_own_data_property(
        &mut self,
        object: HeapAddress,
        property: String,
        values: &Values,
        attributes: DescriptorAttributes,
    ) -> bool {
        match self.own_property(object, &property, true) {
            OwnPropertyKnowledge::KnownAbsent => {
                self.define_data_property(object, property, values, attributes)
            }
            OwnPropertyKnowledge::Data {
                attributes: current,
                ..
            } if current.writable == Some(true) => self.weak_set(object, property, values),
            OwnPropertyKnowledge::Accessor { .. } | OwnPropertyKnowledge::Unknown { .. } => {
                self.unknown_write(object, property, values)
            }
            OwnPropertyKnowledge::Data { .. } => self.unknown_write(object, property, values),
        }
    }

    pub(super) fn unknown_dynamic_write(&mut self, object: HeapAddress, values: &Values) -> bool {
        let mut changed = self.invalidate_surface(object);
        let mut retained = values.clone();
        retained.insert(AbstractValue::Value(
            crate::facts::abstract_domain::value::Value::Top,
        ));
        changed |= self.set_data_values(object, None, &retained);
        changed
    }

    /// Create or weakly join one own data property after the caller has
    /// completed its independent operation preflight.
    pub(super) fn create_data_property(
        &mut self,
        object: HeapAddress,
        property: String,
        values: &Values,
        attributes: DescriptorAttributes,
    ) -> bool {
        self.define_data_property(object, property, values, attributes)
    }

    pub(super) fn define_accessor_property(
        &mut self,
        object: HeapAddress,
        property: String,
        getter: &Values,
        setter: &Values,
        attributes: DescriptorAttributes,
    ) -> bool {
        let mut changed = if self.own_property_is_guaranteed(object, &property) {
            false
        } else {
            self.invalidate_surface(object)
        };
        if let Some(certificate) = self.own_property_certificates.get_mut(&object) {
            let layout_matches = certificate.layout.as_ref().is_some_and(|layout| {
                layout.descriptors.get(&property)
                    == Some(&OwnPropertyLayoutDescriptor {
                        kind: OwnPropertyLayoutKind::Accessor,
                        attributes,
                    })
            });
            if certificate.layout.is_some() && !layout_matches {
                certificate.layout = None;
                changed = true;
            }
        }
        let has_prior_data = self
            .prefix_object_properties
            .get(&object)
            .is_some_and(|properties| properties.contains_key(&Some(property.clone())))
            && !self.has_accessor_property(object, &property);
        if has_prior_data {
            changed |= self.invalidate_surface(object);
        }
        let accessor = self
            .applied_accessors
            .entry(object)
            .or_default()
            .entry(property.clone())
            .or_default();
        accessor.getter.extend(getter.iter().cloned());
        accessor.setter.extend(setter.iter().cloned());
        changed |= self.set_attributes(object, property, attributes);
        changed
    }
}

fn join_attributes(
    left: DescriptorAttributes,
    right: DescriptorAttributes,
) -> DescriptorAttributes {
    DescriptorAttributes {
        writable: (left.writable == right.writable)
            .then_some(left.writable)
            .flatten(),
        enumerable: (left.enumerable == right.enumerable)
            .then_some(left.enumerable)
            .flatten(),
        configurable: (left.configurable == right.configurable)
            .then_some(left.configurable)
            .flatten(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::abstract_domain::value::{
        HeapModuleScope, Kind, LocalAnalysisScopeId, ProviderCallableId,
    };

    fn object() -> HeapAddress {
        HeapAddress::local_family(LocalAnalysisScopeId::fresh(), Kind::Object, 19)
    }

    #[test]
    fn known_absence_requires_a_settled_static_surface() {
        let object = object();
        let mut store = ObjectSurfaceStore::default();
        store.observe_own_property_keys(object, Some(BTreeSet::new()));

        assert_eq!(
            store.own_property(object, "missing", true),
            OwnPropertyKnowledge::KnownAbsent
        );

        store.unknown_dynamic_write(object, &Values::from([AbstractValue::PRIMITIVE]));
        assert!(matches!(
            store.own_property(object, "missing", true),
            OwnPropertyKnowledge::Unknown { .. }
        ));
    }

    #[test]
    fn unknown_surface_keeps_prior_values_and_accessor_roots() {
        let object = object();
        let original = Values::from([AbstractValue::PRIMITIVE]);
        let getter = Values::from([AbstractValue::Value(
            crate::facts::abstract_domain::value::Value::Top,
        )]);
        let setter = Values::from([AbstractValue::UNDEFINED]);
        let mut store = ObjectSurfaceStore::default();
        store.define_data_property(
            object,
            "slot".to_owned(),
            &original,
            DescriptorAttributes::literal(true),
        );
        store.define_accessor_property(
            object,
            "slot".to_owned(),
            &getter,
            &setter,
            DescriptorAttributes::default(),
        );

        let OwnPropertyKnowledge::Unknown { retained_values } =
            store.own_property(object, "slot", false)
        else {
            panic!("data/accessor collision must remain unknown");
        };
        assert!(original.is_subset(&retained_values));
        assert!(getter.is_subset(&retained_values));
        assert!(setter.is_subset(&retained_values));
    }

    #[test]
    fn unknown_property_lookup_retains_dynamic_values_and_accessors() {
        let object = object();
        let getter = Values::from([AbstractValue::Value(
            crate::facts::abstract_domain::value::Value::Top,
        )]);
        let setter = Values::from([AbstractValue::UNDEFINED]);
        let dynamic_value = Values::from([AbstractValue::PRIMITIVE]);
        let mut store = ObjectSurfaceStore::default();
        store.define_accessor_property(
            object,
            "slot".to_owned(),
            &getter,
            &setter,
            DescriptorAttributes::default(),
        );
        store.unknown_dynamic_write(object, &dynamic_value);

        let OwnPropertyKnowledge::Unknown { retained_values } =
            store.own_property(object, "slot", false)
        else {
            panic!("an unknown-key write must keep property lookup unknown");
        };
        assert!(getter.is_subset(&retained_values));
        assert!(setter.is_subset(&retained_values));
        assert!(dynamic_value.is_subset(&retained_values));
    }

    #[test]
    fn producer_family_write_invalidates_provider_surface_without_dropping_values() {
        let producer = HeapModuleScope::Chunk(source_identity::ChunkId(4));
        let recipe = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(source_identity::ChunkId(9)),
            call_origin: 31,
            producer: ProviderCallableId {
                module: producer,
                origin: 42,
            },
            producer_birth: None,
            slot: 0,
            kind: Kind::Object,
        };
        let other_recipe = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(source_identity::ChunkId(10)),
            call_origin: 91,
            producer: ProviderCallableId {
                module: producer,
                origin: 99,
            },
            producer_birth: None,
            slot: 3,
            kind: Kind::Object,
        };
        let source = HeapAddress::module_family(source_identity::ChunkId(4), Kind::Object, 77);
        let unrelated = HeapAddress::module_family(source_identity::ChunkId(5), Kind::Object, 77);
        let recipe_values = Values::from([AbstractValue::PRIMITIVE]);
        let source_values = Values::from([AbstractValue::UNDEFINED]);
        let unrelated_values = Values::from([AbstractValue::Value(
            crate::facts::abstract_domain::value::Value::Top,
        )]);
        let mut store = ObjectSurfaceStore::default();
        for (object, values) in [
            (recipe, &recipe_values),
            (other_recipe, &source_values),
            (source, &recipe_values),
            (unrelated, &unrelated_values),
        ] {
            store.define_data_property(
                object,
                "slot".to_owned(),
                values,
                DescriptorAttributes::literal(true),
            );
            store.observe_own_property_keys(object, Some(BTreeSet::from(["slot".into()])));
        }

        store.invalidate_surface(source);

        for address in [recipe, other_recipe, source] {
            assert!(store.tainted_objects.contains(&address));
            assert_eq!(store.data_values(address, "slot"), None);
        }
        assert!(!store.tainted_objects.contains(&unrelated));
        assert_eq!(
            store.property_payloads(recipe, Some("slot")),
            recipe_values,
            "invalidation retires shape authority but preserves may-values"
        );
    }

    #[test]
    fn verified_provider_birth_invalidates_only_matching_recipe_surfaces() {
        let producer = HeapModuleScope::Chunk(source_identity::ChunkId(4));
        let make_recipe = |consumer, call_origin, callable_origin, slot, birth| {
            HeapAddress::ProviderAllocationFamily {
                consumer: HeapModuleScope::Chunk(consumer),
                call_origin,
                producer: ProviderCallableId {
                    module: producer,
                    origin: callable_origin,
                },
                producer_birth: Some(birth),
                slot,
                kind: crate::facts::abstract_domain::value::Kind::Object,
            }
        };
        let target = make_recipe(source_identity::ChunkId(9), 31, 10, 0, 42);
        let same_birth = make_recipe(source_identity::ChunkId(12), 81, 20, 3, 42);
        let other_birth = make_recipe(source_identity::ChunkId(13), 82, 20, 3, 43);
        let values = Values::from([AbstractValue::PRIMITIVE]);
        let mut store = ObjectSurfaceStore::default();
        for object in [target, same_birth, other_birth] {
            store.define_data_property(
                object,
                "slot".to_owned(),
                &values,
                DescriptorAttributes::literal(true),
            );
            store.observe_own_property_keys(object, Some(BTreeSet::from(["slot".into()])));
        }

        store.invalidate_surface(target);

        assert!(store.data_values(target, "slot").is_none());
        assert!(store.data_values(same_birth, "slot").is_none());
        assert!(store.data_values(other_birth, "slot").is_some());
        assert_eq!(store.property_payloads(same_birth, Some("slot")), values);
    }

    #[test]
    fn unverified_provider_write_invalidates_every_verified_birth_surface() {
        let producer = HeapModuleScope::Chunk(source_identity::ChunkId(4));
        let make_recipe = |consumer, call_origin, callable_origin, slot, birth| {
            HeapAddress::ProviderAllocationFamily {
                consumer: HeapModuleScope::Chunk(consumer),
                call_origin,
                producer: ProviderCallableId {
                    module: producer,
                    origin: callable_origin,
                },
                producer_birth: birth,
                slot,
                kind: crate::facts::abstract_domain::value::Kind::Object,
            }
        };
        let unverified = make_recipe(source_identity::ChunkId(9), 31, 10, 0, None);
        let first_birth = make_recipe(source_identity::ChunkId(12), 81, 20, 3, Some(42));
        let second_birth = make_recipe(source_identity::ChunkId(13), 82, 21, 4, Some(43));
        let unrelated = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(source_identity::ChunkId(14)),
            call_origin: 83,
            producer: ProviderCallableId {
                module: HeapModuleScope::Chunk(source_identity::ChunkId(5)),
                origin: 20,
            },
            producer_birth: Some(42),
            slot: 3,
            kind: crate::facts::abstract_domain::value::Kind::Object,
        };
        let values = Values::from([AbstractValue::PRIMITIVE]);
        let mut store = ObjectSurfaceStore::default();
        for object in [first_birth, second_birth, unrelated] {
            store.define_data_property(
                object,
                "slot".to_owned(),
                &values,
                DescriptorAttributes::literal(true),
            );
            store.observe_own_property_keys(object, Some(BTreeSet::from(["slot".into()])));
        }

        store.invalidate_surface(unverified);

        assert!(store.data_values(first_birth, "slot").is_none());
        assert!(store.data_values(second_birth, "slot").is_none());
        assert!(store.data_values(unrelated, "slot").is_some());
    }

    #[test]
    fn pooled_spread_keyset_change_keeps_only_common_presence() {
        let object = object();
        let first = Values::from([AbstractValue::PRIMITIVE]);
        let second = Values::from([AbstractValue::UNDEFINED]);
        let mut store = ObjectSurfaceStore::default();
        store.record_spread_copy(
            object,
            &[("first".to_owned(), first.clone())],
            true,
            ProofDependencies::default(),
        );
        store.record_spread_copy(
            object,
            &[("second".to_owned(), second.clone())],
            true,
            ProofDependencies::default(),
        );

        assert!(!store.tainted_objects.contains(&object));
        assert!(!store.ordinary_spread_copies.contains(&object));
        assert!(store.spread_copy_history.contains(&object));
        let OwnPropertyKnowledge::Unknown { retained_values } =
            store.own_property(object, "first", true)
        else {
            panic!("different pooled keysets must not certify the selected own property");
        };
        assert!(first.is_subset(&retained_values));
        assert!(second.is_subset(&retained_values));
        assert_eq!(store.data_values(object, "first"), None);
    }

    #[test]
    fn pooled_spread_with_stable_keyset_retains_data_certificate() {
        let object = object();
        let first = Values::from([AbstractValue::PRIMITIVE]);
        let second = Values::from([AbstractValue::UNDEFINED]);
        let mut store = ObjectSurfaceStore::default();
        store.record_spread_copy(
            object,
            &[("slot".to_owned(), first.clone())],
            true,
            ProofDependencies::default(),
        );
        store.record_spread_copy(
            object,
            &[("slot".to_owned(), second.clone())],
            true,
            ProofDependencies::default(),
        );

        assert!(!store.tainted_objects.contains(&object));
        assert!(store.ordinary_spread_copies.contains(&object));
        assert!(matches!(
            store.own_property(object, "slot", false),
            OwnPropertyKnowledge::Data { values, .. }
                if first.is_subset(&values) && second.is_subset(&values)
        ));
        let projection = store.current_own_property_projection(object).unwrap();
        assert_eq!(projection.layout.ordered_keys, vec!["slot".to_owned()]);
        assert!(matches!(
            &projection.descriptors["slot"],
            CurrentOwnPropertyDescriptor::Data { values, attributes }
                if first.is_subset(values)
                    && second.is_subset(values)
                    && *attributes == DescriptorAttributes::literal(true)
        ));
    }

    #[test]
    fn ordered_layout_projection_checks_key_order_roles_and_attributes() {
        let object = object();
        let order = vec!["10".to_owned(), "2".to_owned(), "alpha".to_owned()];
        let attributes = DescriptorAttributes::literal(true);
        let descriptors = order
            .iter()
            .cloned()
            .map(|name| {
                (
                    name,
                    OwnPropertyLayoutDescriptor {
                        kind: OwnPropertyLayoutKind::Data,
                        attributes,
                    },
                )
            })
            .collect();
        let layout = OwnPropertyLayout::from_creation_order(&order, descriptors).unwrap();
        assert_eq!(
            layout.ordered_keys,
            vec!["2".to_owned(), "10".to_owned(), "alpha".to_owned()]
        );

        let mut store = ObjectSurfaceStore::default();
        assert!(store.observe_own_property_layout(
            object,
            Some(layout.clone()),
            ProofDependencies::default(),
        ));
        assert!(store.record_data_properties(
            object,
            BTreeMap::from([
                (
                    Some("10".to_owned()),
                    Values::from([AbstractValue::PRIMITIVE])
                ),
                (
                    Some("2".to_owned()),
                    Values::from([AbstractValue::UNDEFINED])
                ),
                (
                    Some("alpha".to_owned()),
                    Values::from([AbstractValue::CallbackFactory])
                ),
            ]),
            Some(&layout),
        ));

        let projection = store.current_own_property_projection(object).unwrap();
        assert_eq!(projection.layout, layout);
        assert_eq!(
            projection.layout.ordered_keys,
            vec!["2".to_owned(), "10".to_owned(), "alpha".to_owned()]
        );
        assert!(matches!(
            &projection.descriptors["alpha"],
            CurrentOwnPropertyDescriptor::Data {
                values,
                attributes: actual_attributes
            }
                if values.contains(&AbstractValue::CallbackFactory)
                    && *actual_attributes == attributes
        ));

        store.set_attributes(
            object,
            "alpha".to_owned(),
            DescriptorAttributes {
                enumerable: Some(false),
                ..attributes
            },
        );
        assert!(store.current_own_property_projection(object).is_none());
    }

    #[test]
    fn layout_projection_unions_dependencies_across_matching_generations() {
        use crate::analysis_inputs::ProgramPremiseKey;

        let object = object();
        let order = vec!["slot".to_owned()];
        let layout = OwnPropertyLayout::from_creation_order(
            &order,
            BTreeMap::from([(
                "slot".to_owned(),
                OwnPropertyLayoutDescriptor {
                    kind: OwnPropertyLayoutKind::Data,
                    attributes: DescriptorAttributes::literal(true),
                },
            )]),
        )
        .unwrap();
        let left = ProofDependencies {
            program_premises: BTreeSet::from([ProgramPremiseKey::ProcessPrototypeChainAccessors]),
            ..ProofDependencies::default()
        };
        let right = ProofDependencies {
            program_premises: BTreeSet::from([ProgramPremiseKey::IntrinsicsNeverWritten]),
            ..ProofDependencies::default()
        };
        let mut store = ObjectSurfaceStore::default();
        store.observe_own_property_layout(object, Some(layout.clone()), left.clone());
        store.record_data_properties(
            object,
            BTreeMap::from([(
                Some("slot".to_owned()),
                Values::from([AbstractValue::PRIMITIVE]),
            )]),
            Some(&layout),
        );
        store.observe_own_property_layout(object, Some(layout), right.clone());
        let current = store.current_own_property_projection(object).unwrap();
        assert_eq!(
            current.dependencies,
            ProofDependencies {
                program_premises: left
                    .program_premises
                    .union(&right.program_premises)
                    .copied()
                    .collect(),
                ..ProofDependencies::default()
            }
        );
    }

    #[test]
    fn common_presence_keeps_dependencies_when_generation_layout_disagrees() {
        use crate::analysis_inputs::ProgramPremiseKey;

        let object = object();
        let left_dependencies = ProofDependencies {
            program_premises: BTreeSet::from([ProgramPremiseKey::ProcessPrototypeChainAccessors]),
            ..ProofDependencies::default()
        };
        let right_dependencies = ProofDependencies {
            program_premises: BTreeSet::from([ProgramPremiseKey::IntrinsicsNeverWritten]),
            ..ProofDependencies::default()
        };
        let left_keys = BTreeSet::from(["left".to_owned(), "slot".to_owned()]);
        let right_keys = BTreeSet::from(["right".to_owned(), "slot".to_owned()]);
        let mut store = ObjectSurfaceStore::default();
        store.observe_own_property_evidence(
            object,
            Some(left_keys),
            None,
            left_dependencies.clone(),
        );
        store.observe_own_property_evidence(
            object,
            Some(right_keys),
            None,
            right_dependencies.clone(),
        );

        let certificate = &store.own_property_certificates[&object];
        assert_eq!(
            certificate.must_present,
            BTreeSet::from(["slot".to_owned()])
        );
        assert_eq!(certificate.exact_keys, None);
        assert_eq!(certificate.layout, None);
        assert_eq!(
            store.own_property_dependencies(object),
            ProofDependencies {
                program_premises: left_dependencies
                    .program_premises
                    .union(&right_dependencies.program_premises)
                    .copied()
                    .collect(),
                ..ProofDependencies::default()
            }
        );
    }

    #[test]
    fn pooled_alias_layout_disagreement_keeps_payloads_and_never_revives() {
        let object = object();
        let keys = ["slot".to_owned()];
        let data_layout = |attributes| {
            OwnPropertyLayout::from_creation_order(
                &keys,
                BTreeMap::from([(
                    "slot".to_owned(),
                    OwnPropertyLayoutDescriptor {
                        kind: OwnPropertyLayoutKind::Data,
                        attributes,
                    },
                )]),
            )
            .unwrap()
        };
        let writable = DescriptorAttributes::literal(true);
        let readonly = DescriptorAttributes {
            writable: Some(false),
            ..writable
        };
        let first_layout = data_layout(writable);
        let second_layout = data_layout(readonly);
        let first = Values::from([AbstractValue::PRIMITIVE]);
        let second = Values::from([AbstractValue::UNDEFINED]);
        let mut store = ObjectSurfaceStore::default();

        // Two recipes resolve to the same pooled address. Keep both possible
        // payloads, but do not report either generation's writable bit as
        // universal and do not keep the now-mismatched ordered certificate.
        store.observe_own_property_layout(
            object,
            Some(first_layout.clone()),
            ProofDependencies::default(),
        );
        store.record_data_properties(
            object,
            BTreeMap::from([("slot".to_owned().into(), first.clone())]),
            Some(&first_layout),
        );
        store.observe_own_property_layout(
            object,
            Some(second_layout.clone()),
            ProofDependencies::default(),
        );
        store.record_data_properties(
            object,
            BTreeMap::from([("slot".to_owned().into(), second.clone())]),
            Some(&second_layout),
        );

        assert!(store.current_own_property_projection(object).is_none());
        assert!(matches!(
            store.own_property(object, "slot", false),
            OwnPropertyKnowledge::Data { values, attributes }
                if first.is_subset(&values)
                    && second.is_subset(&values)
                    && attributes.writable.is_none()
        ));

        // A later generation matching the first one cannot upgrade a pooled
        // certificate after disagreement has already been observed.
        store.observe_own_property_layout(
            object,
            Some(first_layout.clone()),
            ProofDependencies::default(),
        );
        store.record_data_properties(
            object,
            BTreeMap::from([("slot".to_owned().into(), first.clone())]),
            Some(&first_layout),
        );
        assert!(store.current_own_property_projection(object).is_none());
        assert!(matches!(
            store.own_property(object, "slot", false),
            OwnPropertyKnowledge::Data { values, attributes }
                if first.is_subset(&values)
                    && second.is_subset(&values)
                    && attributes.writable.is_none()
        ));

        store.unknown_dynamic_write(object, &Values::from([AbstractValue::CallbackFactory]));
        store.observe_own_property_layout(
            object,
            Some(first_layout.clone()),
            ProofDependencies::default(),
        );
        store.record_data_properties(
            object,
            BTreeMap::from([("slot".to_owned().into(), first)]),
            Some(&first_layout),
        );
        assert!(store.current_own_property_projection(object).is_none());
        assert!(
            store
                .retained_values(object)
                .contains(&AbstractValue::CallbackFactory)
        );
    }

    #[test]
    fn empty_layout_has_a_current_projection() {
        let object = object();
        let layout = OwnPropertyLayout::from_creation_order(&[], BTreeMap::new()).unwrap();
        let mut store = ObjectSurfaceStore::default();
        store.observe_own_property_layout(
            object,
            Some(layout.clone()),
            ProofDependencies::default(),
        );
        store.record_data_properties(object, BTreeMap::new(), Some(&layout));

        let projection = store.current_own_property_projection(object).unwrap();
        assert!(projection.layout.ordered_keys.is_empty());
        assert!(projection.descriptors.is_empty());
    }

    #[test]
    fn accessor_layout_projection_retains_getter_and_setter_roots() {
        let object = object();
        let attributes = DescriptorAttributes {
            writable: None,
            enumerable: Some(true),
            configurable: Some(true),
        };
        let layout = OwnPropertyLayout::from_creation_order(
            &["value".to_owned()],
            BTreeMap::from([(
                "value".to_owned(),
                OwnPropertyLayoutDescriptor {
                    kind: OwnPropertyLayoutKind::Accessor,
                    attributes,
                },
            )]),
        )
        .unwrap();
        let getter = Values::from([AbstractValue::InlineCallbackFactory]);
        let setter = Values::from([AbstractValue::CallbackFactory]);
        let mut store = ObjectSurfaceStore::default();
        store.observe_own_property_layout(object, Some(layout), ProofDependencies::default());
        store.applied_accessors.insert(
            object,
            BTreeMap::from([(
                "value".to_owned(),
                AppliedAccessor {
                    getter: getter.clone(),
                    setter: setter.clone(),
                },
            )]),
        );
        store
            .property_attributes
            .insert(object, BTreeMap::from([("value".to_owned(), attributes)]));

        let projection = store.current_own_property_projection(object).unwrap();
        assert!(matches!(
            &projection.descriptors["value"],
            CurrentOwnPropertyDescriptor::Accessor { getter: actual_getter, setter: actual_setter, .. }
                if actual_getter == &getter && actual_setter == &setter
        ));

        store.unknown_dynamic_write(object, &Values::from([AbstractValue::UNDEFINED]));
        assert!(store.current_own_property_projection(object).is_none());
        assert!(
            store
                .retained_values(object)
                .contains(&AbstractValue::InlineCallbackFactory)
        );
        assert!(
            store
                .retained_values(object)
                .contains(&AbstractValue::CallbackFactory)
        );
    }

    #[test]
    fn complete_pooled_records_keep_data_when_generation_keys_match() {
        let object = object();
        let first = Values::from([AbstractValue::PRIMITIVE]);
        let second = Values::from([AbstractValue::UNDEFINED]);
        let keys = BTreeSet::from(["stable".to_owned()]);
        let mut store = ObjectSurfaceStore::default();

        assert!(store.observe_own_property_keys(object, Some(keys.clone())));
        store.define_data_property(
            object,
            "stable".to_owned(),
            &first,
            DescriptorAttributes::literal(true),
        );
        assert!(!store.observe_own_property_keys(object, Some(keys)));
        store.define_data_property(
            object,
            "stable".to_owned(),
            &second,
            DescriptorAttributes::literal(true),
        );

        assert!(matches!(
            store.own_property(object, "stable", false),
            OwnPropertyKnowledge::Data { values, attributes }
                if first.is_subset(&values)
                    && second.is_subset(&values)
                    && attributes.writable.is_none()
        ));
    }

    #[test]
    fn an_explicit_property_on_only_one_pooled_generation_retires_presence() {
        let object = object();
        let stable = Values::from([AbstractValue::PRIMITIVE]);
        let added = Values::from([AbstractValue::UNDEFINED]);
        let mut store = ObjectSurfaceStore::default();
        store.observe_own_property_keys(object, Some(BTreeSet::from(["stable".to_owned()])));
        store.define_data_property(
            object,
            "stable".to_owned(),
            &stable,
            DescriptorAttributes::literal(true),
        );

        // The local write ran for one represented instance. The pooled store
        // has no all-generations witness for the new key, so it must retain
        // values while revoking both presence and absence claims.
        store.define_data_property(
            object,
            "added".to_owned(),
            &added,
            DescriptorAttributes::literal(true),
        );

        let certificate = &store.own_property_certificates[&object];
        assert!(certificate.must_present.is_empty());
        assert_eq!(certificate.exact_keys, None);
        assert_eq!(certificate.layout, None);
        for (property, retained) in [("stable", &stable), ("added", &added)] {
            let OwnPropertyKnowledge::Unknown { retained_values } =
                store.own_property(object, property, true)
            else {
                panic!("unproven pooled property {property:?} acquired authority");
            };
            assert!(retained.is_subset(&retained_values));
        }
    }

    #[test]
    fn pooled_array_spread_shape_must_match_across_generations() {
        let object = object();
        let mut store = ObjectSurfaceStore::default();
        assert!(store.observe_array_spread_shape(
            object,
            Some(BTreeSet::from(["0".to_owned(), "1".to_owned()])),
            true,
        ));
        assert!(!store.observe_array_spread_shape(
            object,
            Some(BTreeSet::from(["0".to_owned()])),
            true,
        ));

        assert!(!store.tainted_objects.contains(&object));
        assert_eq!(store.array_spread_shapes.get(&object), Some(&None));
        assert!(matches!(
            store.own_property(object, "1", true),
            OwnPropertyKnowledge::Unknown { .. }
        ));
    }

    #[test]
    fn varying_array_spread_length_keeps_only_common_own_presence() {
        let object = object();
        let attributes = DescriptorAttributes::literal(true);
        let array_layout = |names: &[&str]| {
            let creation_order = names
                .iter()
                .map(|name| (*name).to_owned())
                .collect::<Vec<_>>();
            let descriptors = creation_order
                .iter()
                .cloned()
                .map(|name| {
                    let attributes = if name == "length" {
                        DescriptorAttributes {
                            writable: Some(true),
                            enumerable: Some(false),
                            configurable: Some(false),
                        }
                    } else {
                        attributes
                    };
                    (
                        name,
                        OwnPropertyLayoutDescriptor {
                            kind: OwnPropertyLayoutKind::Data,
                            attributes,
                        },
                    )
                })
                .collect();
            OwnPropertyLayout::from_creation_order(&creation_order, descriptors).unwrap()
        };
        let first_layout = array_layout(&["0", "1", "length"]);
        let second_layout = array_layout(&["0", "length"]);
        let mut store = ObjectSurfaceStore::default();
        store.observe_own_property_layout(
            object,
            Some(first_layout.clone()),
            ProofDependencies::default(),
        );
        store.record_data_properties(
            object,
            BTreeMap::from([
                (
                    Some("0".to_owned()),
                    Values::from([AbstractValue::PRIMITIVE]),
                ),
                (
                    Some("1".to_owned()),
                    Values::from([AbstractValue::CallbackFactory]),
                ),
                (
                    Some("length".to_owned()),
                    Values::from([AbstractValue::PRIMITIVE]),
                ),
            ]),
            Some(&first_layout),
        );
        store.observe_array_spread_shape(
            object,
            Some(BTreeSet::from(["0".to_owned(), "1".to_owned()])),
            true,
        );
        store.observe_own_property_layout(
            object,
            Some(second_layout.clone()),
            ProofDependencies::default(),
        );
        store.record_data_properties(
            object,
            BTreeMap::from([
                (
                    Some("0".to_owned()),
                    Values::from([AbstractValue::UNDEFINED]),
                ),
                (
                    Some("length".to_owned()),
                    Values::from([AbstractValue::PRIMITIVE]),
                ),
            ]),
            Some(&second_layout),
        );
        assert!(!store.observe_array_spread_shape(
            object,
            Some(BTreeSet::from(["0".to_owned()])),
            true,
        ));

        assert!(!store.tainted_objects.contains(&object));
        assert!(matches!(
            store.own_property(object, "0", false),
            OwnPropertyKnowledge::Data { values, attributes: actual }
                if values.contains(&AbstractValue::PRIMITIVE)
                    && values.contains(&AbstractValue::UNDEFINED)
                    && actual == attributes
        ));
        assert!(matches!(
            store.own_property(object, "1", true),
            OwnPropertyKnowledge::Unknown { retained_values }
                if retained_values.contains(&AbstractValue::CallbackFactory)
        ));
        assert!(store.current_own_property_projection(object).is_none());
    }

    #[test]
    fn pooled_array_spread_with_stable_index_set_keeps_shape_certificate() {
        let object = object();
        let shape = BTreeSet::from(["0".to_owned(), "1".to_owned()]);
        let mut store = ObjectSurfaceStore::default();
        assert!(store.observe_array_spread_shape(object, Some(shape.clone()), true));
        assert!(store.observe_array_spread_shape(object, Some(shape), true));
        assert!(!store.tainted_objects.contains(&object));
        assert!(store.array_spread_shapes.contains_key(&object));
    }

    #[test]
    fn unknown_array_spread_shape_permanently_retires_prior_index_certificate() {
        let object = object();
        let shape = BTreeSet::from(["0".to_owned(), "1".to_owned()]);
        let mut store = ObjectSurfaceStore::default();
        assert!(store.observe_array_spread_shape(object, Some(shape.clone()), true));
        assert!(!store.observe_array_spread_shape(object, None, true));
        assert!(!store.observe_array_spread_shape(object, Some(shape), true));

        assert!(store.tainted_objects.contains(&object));
        assert_eq!(store.array_spread_shapes.get(&object), Some(&None));
        assert!(matches!(
            store.own_property(object, "0", false),
            OwnPropertyKnowledge::Unknown { .. }
        ));
    }

    #[test]
    fn value_only_set_preserves_a_writable_data_descriptor() {
        let object = object();
        let mut store = ObjectSurfaceStore::default();
        store.observe_own_property_keys(object, Some(BTreeSet::from(["slot".to_owned()])));
        store.define_data_property(
            object,
            "slot".to_owned(),
            &Values::from([AbstractValue::PRIMITIVE]),
            DescriptorAttributes::literal(true),
        );
        store.set_existing_data_property(
            object,
            "slot".to_owned(),
            &Values::from([AbstractValue::UNDEFINED]),
        );

        assert!(matches!(
            store.own_property(object, "slot", false),
            OwnPropertyKnowledge::Data {
                attributes: DescriptorAttributes {
                    writable: Some(true),
                    enumerable: Some(true),
                    configurable: Some(true),
                },
                ..
            }
        ));
    }

    #[test]
    fn surface_invalidation_revokes_spread_proof_but_keeps_repeat_history() {
        let object = object();
        let mut store = ObjectSurfaceStore::default();
        store.record_spread_copy(
            object,
            &[("first".to_owned(), Values::from([AbstractValue::PRIMITIVE]))],
            true,
            ProofDependencies::default(),
        );
        assert!(store.ordinary_spread_copies.contains(&object));

        store.invalidate_surface(object);
        assert!(!store.ordinary_spread_copies.contains(&object));
        assert!(store.spread_copy_history.contains(&object));

        store.record_spread_copy(
            object,
            &[(
                "second".to_owned(),
                Values::from([AbstractValue::UNDEFINED]),
            )],
            true,
            ProofDependencies::default(),
        );
        assert!(!store.ordinary_spread_copies.contains(&object));
        assert_eq!(store.spread_shapes.get(&object), Some(&None));
        assert!(
            store.prefix_object_properties[&object][&Some("first".to_owned())]
                .contains(&AbstractValue::UNDEFINED)
        );
        assert!(matches!(
            store.own_property(object, "first", true),
            OwnPropertyKnowledge::Unknown { .. }
        ));
    }
}
