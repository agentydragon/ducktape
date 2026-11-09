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
use crate::facts::abstract_domain::value::{Kind, Value};
use imbl::OrdMap;
use std::collections::BTreeMap;

#[derive(Debug, Clone)]
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
    fn descriptor_conversion_is_ordinary(&self) -> bool;
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
    context: &impl DescriptorContext,
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
    let surface = context.object_surface();
    if !context.descriptor_conversion_is_ordinary()
        || !context.plain_data_object(*object)
        || !context.object_properties_are_settled(*object)
        || !surface.has_no_own_data(*object)
        || surface.has_any_accessors(*object)
    {
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
    pub(in crate::facts::sync_effects) revision: &'a mut u64,
    pub(in crate::facts::sync_effects) track_writable_prefix: bool,
}

impl<'a> DescriptorHeap<'a> {
    /// The literal-allocation view over the same prefix store.
    pub(in crate::facts::sync_effects) fn into_literal(self) -> LiteralHeap<'a> {
        LiteralHeap {
            surface: self.surface,
            arrays: self.arrays,
            revision: self.revision,
            track_writable_prefix: self.track_writable_prefix,
        }
    }

    fn literal(&mut self) -> LiteralHeap<'_> {
        LiteralHeap {
            surface: self.surface,
            arrays: self.arrays,
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
