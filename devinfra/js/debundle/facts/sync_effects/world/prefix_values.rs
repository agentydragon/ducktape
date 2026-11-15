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
    ordinals: FxHashMap<Id, TopBindingOrdinal>,
}

/// A binding's position in one run's prefix-value table.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(in crate::facts::sync_effects) struct TopBindingOrdinal(u32);

impl TopBindingOrdinal {
    pub(in crate::facts::sync_effects) fn index(self) -> usize {
        self.0 as usize
    }
}

impl TopBindings {
    pub(in crate::facts::sync_effects) fn new(ids: impl IntoIterator<Item = Id>) -> Self {
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

    pub(in crate::facts::sync_effects) fn ordinal(&self, id: &Id) -> Option<TopBindingOrdinal> {
        self.ordinals.get(id).copied()
    }

    pub(in crate::facts::sync_effects) fn len(&self) -> usize {
        self.ordinals.len()
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
        self.get_ordinal(self.bindings.ordinal(id)?)
    }

    /// The ordinal comes from this run's shared `TopBindings`.
    pub(in crate::facts::sync_effects) fn get_ordinal(
        &self,
        ordinal: TopBindingOrdinal,
    ) -> Option<&Values> {
        self.values.get(&ordinal.0)
    }

    pub(in crate::facts::sync_effects) fn get_mut(&mut self, id: &Id) -> Option<&mut Values> {
        let ordinal = self.bindings.ordinal(id)?;
        self.values.get_mut(&ordinal.0)
    }

    /// The binding's values, inserting an empty set first if it has none.
    /// Only the run's top-level bindings have prefix values.
    pub(in crate::facts::sync_effects) fn entry_or_default(&mut self, id: &Id) -> &mut Values {
        let ordinal = self
            .bindings
            .ordinal(id)
            .unwrap_or_else(|| panic!("prefix value for a non-top-level binding: {id:?}"));
        self.values.entry(ordinal.0).or_default()
    }

    pub(in crate::facts::sync_effects) fn insert(&mut self, id: &Id, values: Values) {
        *self.entry_or_default(id) = values;
    }

    pub(in crate::facts::sync_effects) fn values(&self) -> impl Iterator<Item = &Values> {
        self.values.values()
    }

    pub(in crate::facts::sync_effects) fn map_values(&mut self, mut map: impl FnMut(&mut Values)) {
        self.values = self
            .values
            .iter()
            .map(|(id, values)| {
                let mut values = values.clone();
                map(&mut values);
                (*id, values)
            })
            .collect();
    }

    pub(in crate::facts::sync_effects) fn ptr_eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.bindings, &other.bindings) && self.values.ptr_eq(&other.values)
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

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::SyntaxContext;

    #[test]
    fn pointer_witness_preserves_binding_catalogue_identity() {
        let first = PrefixValues::new(Arc::new(TopBindings::new([(
            "first".into(),
            SyntaxContext::empty(),
        )])));
        let second = PrefixValues::new(Arc::new(TopBindings::new([(
            "second".into(),
            SyntaxContext::empty(),
        )])));
        assert!(first.ptr_eq(&first.clone()));
        // Both ordinal maps are empty. Their equal integer-keyed storage must
        // not authorize reuse under another module's binding catalogue.
        assert!(!first.ptr_eq(&second));
        let same_names_new_source = PrefixValues::new(Arc::new(TopBindings::new([(
            "first".into(),
            SyntaxContext::empty(),
        )])));
        assert!(!first.ptr_eq(&same_names_new_source));
    }
}
