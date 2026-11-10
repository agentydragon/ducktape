//! Possible values of the chunk's top-level bindings in the current prefix.
//!
//! The map is keyed by each binding's ordinal in the run's `TopBindings`.
//! Ordinals follow `Id` order, so iteration matches an `Id`-keyed map, while
//! lookups compare integers instead of binding names.

use super::super::value_model::Values;
use imbl::OrdMap;
use rustc_hash::FxHashMap;
use std::collections::BTreeSet;
use std::sync::Arc;
use swc_ecma_ast::Id;

/// The run's top-level bindings, numbered in `Id` order.
#[derive(Debug, Default, PartialEq, Eq)]
pub(in crate::facts::sync_effects) struct TopBindings {
    ordinals: FxHashMap<Id, u32>,
}

impl TopBindings {
    pub(in crate::facts::sync_effects) fn new(ids: impl IntoIterator<Item = Id>) -> Self {
        let ids: BTreeSet<Id> = ids.into_iter().collect();
        let ordinals = ids
            .into_iter()
            .enumerate()
            .map(|(ordinal, id)| {
                let ordinal = u32::try_from(ordinal).expect("top-level binding count fits u32");
                (id, ordinal)
            })
            .collect();
        Self { ordinals }
    }

    fn ordinal(&self, id: &Id) -> Option<u32> {
        self.ordinals.get(id).copied()
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub(in crate::facts::sync_effects) struct PrefixValues {
    bindings: Arc<TopBindings>,
    values: OrdMap<u32, Values>,
}

impl PrefixValues {
    pub(in crate::facts::sync_effects) fn new(bindings: Arc<TopBindings>) -> Self {
        Self {
            bindings,
            values: OrdMap::new(),
        }
    }

    pub(in crate::facts::sync_effects) fn get(&self, id: &Id) -> Option<&Values> {
        self.values.get(&self.bindings.ordinal(id)?)
    }

    pub(in crate::facts::sync_effects) fn get_mut(&mut self, id: &Id) -> Option<&mut Values> {
        let ordinal = self.bindings.ordinal(id)?;
        self.values.get_mut(&ordinal)
    }

    /// The binding's values, inserting an empty set first if it has none.
    /// Only the run's top-level bindings have prefix values.
    pub(in crate::facts::sync_effects) fn entry_or_default(&mut self, id: &Id) -> &mut Values {
        let ordinal = self
            .bindings
            .ordinal(id)
            .unwrap_or_else(|| panic!("prefix value for a non-top-level binding: {id:?}"));
        self.values.entry(ordinal).or_default()
    }

    pub(in crate::facts::sync_effects) fn insert(&mut self, id: &Id, values: Values) {
        *self.entry_or_default(id) = values;
    }

    pub(in crate::facts::sync_effects) fn values(&self) -> impl Iterator<Item = &Values> {
        self.values.values()
    }

    pub(in crate::facts::sync_effects) fn ptr_eq(&self, other: &Self) -> bool {
        self.values.ptr_eq(&other.values)
    }

    /// Joins alternatives forked from one run: each binding present on any
    /// path gets the union of its values, plus `unknown` from a path that
    /// lacks it.
    pub(in crate::facts::sync_effects) fn join(
        alternatives: &[&Self],
        join_value_maps: impl FnOnce(&[&OrdMap<u32, Values>]) -> OrdMap<u32, Values>,
    ) -> Self {
        let bindings = alternatives[0].bindings.clone();
        debug_assert!(
            alternatives
                .iter()
                .all(|alternative| alternative.bindings == bindings)
        );
        let maps: Vec<_> = alternatives
            .iter()
            .map(|alternative| &alternative.values)
            .collect();
        Self {
            bindings,
            values: join_value_maps(&maps),
        }
    }
}

impl std::ops::Index<&Id> for PrefixValues {
    type Output = Values;

    fn index(&self, id: &Id) -> &Values {
        self.get(id).expect("binding has prefix values")
    }
}
