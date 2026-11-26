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
pub(crate) struct TopBindings {
    ordinals: FxHashMap<Id, TopBindingOrdinal>,
}

/// A binding's position in one run's prefix-value table.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct TopBindingOrdinal(u32);

impl TopBindingOrdinal {
    pub(crate) fn index(self) -> usize {
        self.0 as usize
    }
}

impl TopBindings {
    pub(crate) fn new(ids: impl IntoIterator<Item = Id>) -> Self {
        let ids: BTreeSet<Id> = ids.into_iter().collect();
        let ordinals = ids
            .into_iter()
            .enumerate()
            .map(|(ordinal, id)| {
                let ordinal = u32::try_from(ordinal).expect("top-level binding count fits u32");
                (id, TopBindingOrdinal(ordinal))
            })
            .collect();
        Self { ordinals }
    }

    pub(crate) fn ordinal(&self, id: &Id) -> Option<TopBindingOrdinal> {
        self.ordinals.get(id).copied()
    }

    pub(crate) fn len(&self) -> usize {
        self.ordinals.len()
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub(crate) struct PrefixValues {
    bindings: Arc<TopBindings>,
    values: OrdMap<u32, Values>,
}

impl PrefixValues {
    pub(crate) fn new(bindings: Arc<TopBindings>) -> Self {
        Self {
            bindings,
            values: OrdMap::new(),
        }
    }

    pub(crate) fn get(&self, id: &Id) -> Option<&Values> {
        self.get_ordinal(self.bindings.ordinal(id)?)
    }

    /// The ordinal comes from this run's shared `TopBindings`.
    pub(crate) fn get_ordinal(&self, ordinal: TopBindingOrdinal) -> Option<&Values> {
        self.values.get(&ordinal.0)
    }

    pub(crate) fn get_mut(&mut self, id: &Id) -> Option<&mut Values> {
        let ordinal = self.bindings.ordinal(id)?;
        self.values.get_mut(&ordinal.0)
    }

    /// The binding's values, inserting an empty set first if it has none.
    /// Only the run's top-level bindings have prefix values.
    pub(crate) fn entry_or_default(&mut self, id: &Id) -> &mut Values {
        let ordinal = self
            .bindings
            .ordinal(id)
            .unwrap_or_else(|| panic!("prefix value for a non-top-level binding: {id:?}"));
        self.values.entry(ordinal.0).or_default()
    }

    pub(crate) fn insert(&mut self, id: &Id, values: Values) {
        *self.entry_or_default(id) = values;
    }

    pub(crate) fn values(&self) -> impl Iterator<Item = &Values> {
        self.values.values()
    }

    pub(crate) fn ptr_eq(&self, other: &Self) -> bool {
        self.values.ptr_eq(&other.values)
    }

    /// Joins alternatives forked from one run: each binding present on any
    /// path gets the union of its values, plus `unknown` from a path that
    /// lacks it.
    pub(crate) fn join(
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
