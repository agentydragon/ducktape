//! Own-property and object-surface may-state for one execution prefix.
//!
//! This store owns only may-state: a successful mutation here never proves
//! that the JavaScript operation completed normally. Completion stays with the
//! operation transfer that planned it (`native::operation`).

use super::super::allocation_transfer::WritablePrefixState;
use super::super::value_model::Values;
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Default, PartialEq)]
pub(in crate::facts::sync_effects) struct AppliedAccessor {
    pub(in crate::facts::sync_effects) getter: Values,
    pub(in crate::facts::sync_effects) setter: Values,
}

/// Object-surface state of one prefix, grouped so every surface read, write,
/// native descriptor operation and invalidation goes through one owner.
#[derive(Debug, Clone, Default, PartialEq)]
pub(in crate::facts::sync_effects) struct ObjectSurfaceStore {
    /// Stored data payloads by allocation origin; `None` is the dynamic-key
    /// slot. Values only accumulate within a prefix.
    pub(in crate::facts::sync_effects) prefix_object_properties:
        OrdMap<u32, BTreeMap<Option<String>, Values>>,
    /// Independent literal writability witness; see `LiteralHeap`.
    pub(in crate::facts::sync_effects) writable_prefix: OrdMap<u32, WritablePrefixState>,
    /// Accessor callbacks installed by a descriptor definition, kept apart
    /// from data payloads so a lookup never projects a capability as a value.
    pub(in crate::facts::sync_effects) applied_accessors:
        OrdMap<u32, BTreeMap<String, AppliedAccessor>>,
    /// Exact single-spread object literals whose copied data is current.
    pub(in crate::facts::sync_effects) ordinary_spread_copies: BTreeSet<u32>,
    /// Results of audited descriptor-copy factory calls.
    pub(in crate::facts::sync_effects) descriptor_copy_results: BTreeSet<u32>,
    pub(in crate::facts::sync_effects) tainted_objects: BTreeSet<u32>,
    /// Objects code the interpreter did not run may have reached and mutated.
    /// This records possible mutation or escape, not an observed escape to
    /// unknown code. Their properties and prototypes are no longer settled.
    pub(in crate::facts::sync_effects) unsettled_objects: BTreeSet<u32>,
}

impl ObjectSurfaceStore {
    pub(in crate::facts::sync_effects) fn own_properties(
        &self,
        object: u32,
    ) -> Option<&BTreeMap<Option<String>, Values>> {
        self.prefix_object_properties.get(&object)
    }

    pub(in crate::facts::sync_effects) fn data_values(
        &self,
        object: u32,
        property: &str,
    ) -> Option<&Values> {
        self.prefix_object_properties
            .get(&object)?
            .get(&Some(property.to_owned()))
    }

    pub(in crate::facts::sync_effects) fn has_accessor_property(
        &self,
        object: u32,
        property: &str,
    ) -> bool {
        self.applied_accessors
            .get(&object)
            .is_some_and(|properties| properties.contains_key(property))
    }

    pub(in crate::facts::sync_effects) fn has_any_accessors(&self, object: u32) -> bool {
        self.applied_accessors.contains_key(&object)
    }

    /// Skipped local code can change descriptors or prototypes even when no
    /// escape to foreign code was observed. Neither set permits prefix reads.
    pub(in crate::facts::sync_effects) fn object_accessors_are_settled(&self, object: u32) -> bool {
        !self.tainted_objects.contains(&object) && !self.unsettled_objects.contains(&object)
    }

    /// Whether this object was allocated at this prefix and holds no own data
    /// payload yet.
    pub(in crate::facts::sync_effects) fn has_no_own_data(&self, object: u32) -> bool {
        self.prefix_object_properties
            .get(&object)
            .is_some_and(BTreeMap::is_empty)
    }

    /// Define or widen a data property. Returns whether the surface changed.
    pub(in crate::facts::sync_effects) fn define_data_property(
        &mut self,
        object: u32,
        property: String,
        values: &Values,
    ) -> bool {
        let slot = self
            .prefix_object_properties
            .entry(object)
            .or_default()
            .entry(Some(property))
            .or_default();
        let before = slot.len();
        slot.extend(values.iter().cloned());
        slot.len() != before
    }

    /// Define or widen an accessor property. The data slot keeps the lookup
    /// projection; the callbacks themselves stay capabilities. Returns whether
    /// anything changed.
    pub(in crate::facts::sync_effects) fn define_accessor_property(
        &mut self,
        object: u32,
        property: String,
        getter: &Values,
        setter: &Values,
        lookup_projection: &Values,
    ) -> bool {
        let mut changed = self.define_data_property(object, property.clone(), lookup_projection);
        let accessor = self
            .applied_accessors
            .entry(object)
            .or_default()
            .entry(property)
            .or_default();
        let before = accessor.getter.len() + accessor.setter.len();
        accessor.getter.extend(getter.iter().cloned());
        accessor.setter.extend(setter.iter().cloned());
        changed |= accessor.getter.len() + accessor.setter.len() != before;
        changed
    }
}
