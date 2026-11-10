//! Descriptor snapshots and their application over one prefix heap.
//!
//! A captured map is a may-state snapshot of one object's own descriptors at
//! one call. Applying it grants no writable-field proof and never proves that
//! the defining operation completed normally; those obligations stay with the
//! planning transfer (`native::operation`) and the realizability gate.

use super::super::allocation_transfer::LiteralHeap;
use super::super::own_data_transfer::OwnDataContext;
use super::super::property_inference::{
    CapturedPropertyDescriptor, OwnPropertyDescriptorProjection,
};
use super::super::value_model::{AbstractValue, CallSite, DenseArray, Values};
use super::object_surface::ObjectSurfaceStore;
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::value::{Kind, Value};
use imbl::OrdMap;
use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub(in crate::facts::sync_effects) struct OwnDescriptorMapState {
    pub(in crate::facts::sync_effects) source_object: u32,
    /// A pooled site that captured a different source or shape, or whose
    /// map escaped to unmodeled code, keeps only its retained values.
    pub(in crate::facts::sync_effects) valid: bool,
    pub(in crate::facts::sync_effects) property_order: Vec<String>,
    pub(in crate::facts::sync_effects) properties: BTreeMap<String, CapturedPropertyDescriptor>,
    pub(in crate::facts::sync_effects) retained_values: Values,
}

/// Premises a descriptor application needs from its executing context.
pub(in crate::facts::sync_effects) trait DescriptorContext:
    OwnDataContext
{
    fn descriptor_map(&self, site: CallSite) -> Option<&OwnDescriptorMapState>;
    fn object_surface(&self) -> &ObjectSurfaceStore;
    /// A fresh plain-data literal: its own descriptors are the literal's, so
    /// `[[DefineOwnProperty]]` on it runs no user code.
    fn plain_data_object(&self, object: u32) -> bool;
    /// `ToPropertyDescriptor` reads inherited fields of every descriptor record.
    fn descriptor_conversion_is_ordinary(&mut self) -> bool;
    /// An object literal whose source lies in the executing top-level
    /// statement: no other statement holds it before this one completes.
    fn statement_literal(&self, object: u32) -> bool;
}

/// A checked `Object.defineProperty(target, key, descriptor)` whose key is a
/// primitive the surface cannot name. Commit stores the data value in the
/// target's dynamic-key slot, so every own-key projection of the target
/// refuses from then on, and drops the target's literal writability witness.
#[derive(Debug, Clone)]
pub(in crate::facts::sync_effects) struct DynamicKeyDefinition {
    target: u32,
    value: Values,
}

/// The record fields `ToPropertyDescriptor` reads for a data descriptor.
const DATA_DESCRIPTOR_FIELDS: [&str; 4] = ["value", "writable", "enumerable", "configurable"];

/// Defining one primitive-keyed data property completes normally only on an
/// object literal of the executing statement whose literal writability is
/// still witnessed (extensible, every own property configurable), from an
/// exact settled plain-data descriptor record holding only data-descriptor
/// fields, with the ordinary `Object.prototype` intact for the inherited
/// field lookups. The key's `ToPropertyKey` is silent on every primitive.
pub(in crate::facts::sync_effects) fn dynamic_key_definition(
    context: &mut impl DescriptorContext,
    target: &Values,
    key: &Values,
    descriptor: &Values,
) -> Option<DynamicKeyDefinition> {
    if key.is_empty()
        || !key.iter().all(AbstractValue::is_primitive)
        || target.len() != 1
        || descriptor.len() != 1
    {
        return None;
    }
    let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = target.first()? else {
        return None;
    };
    let AbstractValue::Value(Value::Alloc(Kind::Object, record)) = descriptor.first()? else {
        return None;
    };
    if !context.descriptor_conversion_is_ordinary() {
        return None;
    }
    let surface = context.object_surface();
    if !context.statement_literal(*object)
        || !context.object_properties_are_settled(*object)
        || !surface.literal_writability_intact(*object)
        || !context.plain_data_object(*record)
        || !context.object_properties_are_settled(*record)
        || surface.has_any_accessors(*record)
    {
        return None;
    }
    let fields = surface.own_properties(*record)?;
    if !fields.keys().all(|field| {
        field
            .as_deref()
            .is_some_and(|field| DATA_DESCRIPTOR_FIELDS.contains(&field))
    }) {
        return None;
    }
    let value = fields
        .get(&Some("value".to_owned()))
        .cloned()
        .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED]));
    if value.is_empty() {
        return None;
    }
    Some(DynamicKeyDefinition {
        target: *object,
        value,
    })
}

/// A checked `Object.defineProperties(target, map)` whose every definition is
/// copied in, so commit cannot fail after the prefix starts changing.
#[derive(Debug, Clone)]
pub(in crate::facts::sync_effects) struct DescriptorApplication {
    pub(in crate::facts::sync_effects) target: u32,
    definitions: Vec<(String, CapturedPropertyDescriptor)>,
}

impl DescriptorApplication {
    /// Every property of a still-valid map, in creation order.
    pub(in crate::facts::sync_effects) fn from_map(
        target: u32,
        map: &OwnDescriptorMapState,
    ) -> Option<Self> {
        if !map.valid {
            return None;
        }
        let definitions = map
            .property_order
            .iter()
            .map(|name| Some((name.clone(), map.properties.get(name)?.clone())))
            .collect::<Option<Vec<_>>>()?;
        Some(Self {
            target,
            definitions,
        })
    }
}

/// Defining captured descriptors completes normally only on an exact fresh
/// plain-data target with no own data or accessors yet, from a valid map,
/// with the ordinary `Object.prototype` intact for descriptor conversion.
pub(in crate::facts::sync_effects) fn application(
    context: &mut impl DescriptorContext,
    target: &Values,
    descriptors: &Values,
) -> Option<DescriptorApplication> {
    if target.len() != 1 || descriptors.len() != 1 {
        return None;
    }
    let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = target.first()? else {
        return None;
    };
    let AbstractValue::OwnDescriptorMap(site) = descriptors.first()? else {
        return None;
    };
    if !context.descriptor_conversion_is_ordinary()
        || !context.plain_data_object(*object)
        || !context.object_properties_are_settled(*object)
    {
        return None;
    }
    let surface = context.object_surface();
    if !surface.has_no_own_data(*object) || surface.has_any_accessors(*object) {
        return None;
    }
    DescriptorApplication::from_map(*object, context.descriptor_map(*site)?)
}

/// A caller's borrowed prefix store. Admission owns native identity, target
/// and completion proofs; these updates grant neither ordinary-property shape
/// nor effect coverage.
pub(in crate::facts::sync_effects) struct DescriptorHeap<'a> {
    pub(in crate::facts::sync_effects) maps: &'a mut OrdMap<CallSite, OwnDescriptorMapState>,
    pub(in crate::facts::sync_effects) surface: &'a mut ObjectSurfaceStore,
    pub(in crate::facts::sync_effects) arrays: &'a mut OrdMap<u32, DenseArray>,
    pub(in crate::facts::sync_effects) primitive_arrays: &'a mut OrdMap<u32, ProofDependencies>,
    pub(in crate::facts::sync_effects) revision: &'a mut u64,
    pub(in crate::facts::sync_effects) track_writable_prefix: bool,
}

impl<'a> DescriptorHeap<'a> {
    /// The literal-allocation view over the same prefix store.
    pub(in crate::facts::sync_effects) fn into_literal(self) -> LiteralHeap<'a> {
        LiteralHeap {
            surface: self.surface,
            arrays: self.arrays,
            primitive_arrays: self.primitive_arrays,
            revision: self.revision,
            track_writable_prefix: self.track_writable_prefix,
        }
    }

    fn literal(&mut self) -> LiteralHeap<'_> {
        LiteralHeap {
            surface: self.surface,
            arrays: self.arrays,
            primitive_arrays: self.primitive_arrays,
            revision: self.revision,
            track_writable_prefix: self.track_writable_prefix,
        }
    }

    /// Record one inspection's snapshot. A pooled site that observes a
    /// different source or shape loses its map but keeps every value it may
    /// retain.
    pub(in crate::facts::sync_effects) fn capture(
        &mut self,
        site: CallSite,
        projection: OwnPropertyDescriptorProjection,
    ) -> AbstractValue {
        let OwnPropertyDescriptorProjection {
            source_object,
            ordered_names: property_order,
            properties,
            retained_values,
        } = projection;
        let changed = match self.maps.get_mut(&site) {
            Some(existing) => {
                if existing.source_object != source_object
                    || existing.property_order != property_order
                    || existing.properties != properties
                {
                    let was_valid = existing.valid;
                    let retained_before = existing.retained_values.len();
                    existing.valid = false;
                    existing.retained_values.extend(retained_values);
                    was_valid || existing.retained_values.len() != retained_before
                } else {
                    false
                }
            }
            None => {
                self.maps.insert(
                    site,
                    OwnDescriptorMapState {
                        source_object,
                        valid: true,
                        property_order,
                        properties,
                        retained_values,
                    },
                );
                true
            }
        };
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
        AbstractValue::OwnDescriptorMap(site)
    }

    pub(in crate::facts::sync_effects) fn invalidate_writable(&mut self, object: u32) {
        self.literal().invalidate_writable(object);
    }

    pub(in crate::facts::sync_effects) fn dense_array(
        &mut self,
        allocation: u32,
        slots: Vec<Values>,
    ) {
        self.literal().dense_array(allocation, slots);
    }

    /// A fresh array whose elements are some of `contents`, in unknown
    /// positions. Like a qualitative spread literal, it keeps only their
    /// reachability: no slot, length or primitive-contents claim.
    pub(in crate::facts::sync_effects) fn contents_array(
        &mut self,
        allocation: u32,
        contents: Values,
    ) {
        let mut literal = self.literal();
        literal.invalidate_dense_array(allocation);
        literal.invalidate_primitive_array(allocation);
        literal.record(allocation, BTreeMap::from([(None, contents)]));
    }

    /// Commit a checked primitive-keyed definition: the payload joins the
    /// target's dynamic-key slot and the literal writability witness is gone.
    pub(in crate::facts::sync_effects) fn define_dynamic_key(
        &mut self,
        definition: &DynamicKeyDefinition,
    ) {
        self.literal().invalidate_writable(definition.target);
        if self
            .surface
            .define_dynamic_key_data(definition.target, &definition.value)
        {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// A map that reached unmodeled code may be mutated there.
    pub(in crate::facts::sync_effects) fn invalidate(&mut self, site: CallSite) {
        if let Some(map) = self.maps.get_mut(&site)
            && map.valid
        {
            map.valid = false;
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Commit a prevalidated application. The target's literal writability
    /// witness is gone whether or not any payload changed.
    pub(in crate::facts::sync_effects) fn apply(&mut self, application: &DescriptorApplication) {
        self.literal().invalidate_writable(application.target);
        let mut changed = false;
        for (name, descriptor) in &application.definitions {
            changed |= match descriptor {
                CapturedPropertyDescriptor::Data(values) => {
                    self.surface
                        .define_data_property(application.target, name.clone(), values)
                }
                CapturedPropertyDescriptor::Accessor { getter, setter } => {
                    // A lookup through the accessor yields `undefined` without
                    // a getter and an unknown value otherwise.
                    let lookup = if getter.is_empty() {
                        Values::from([AbstractValue::UNDEFINED])
                    } else {
                        Values::from([AbstractValue::Value(Value::Top)])
                    };
                    self.surface.define_accessor_property(
                        application.target,
                        name.clone(),
                        getter,
                        setter,
                        &lookup,
                    )
                }
            };
        }
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }
}
