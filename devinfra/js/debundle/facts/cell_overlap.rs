//! Shared overlap law for current effect-cell addresses.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use super::heap_contents::is_array_index;
use super::statement_facts::EffectCell;

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CellOverlapLaw {
    /// The two effects name the same current effect cell.
    ExactCell,
    /// Same-origin array contents and a numeric array index or `length` key.
    ArrayContentsIndexOrLength,
}

/// A source-backed pair of effect-cell footprints joined by an overlap law.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct CellOverlap {
    left: EffectCell,
    right: EffectCell,
    law: CellOverlapLaw,
}

impl CellOverlap {
    /// Returns the footprint stored in the index.
    pub fn left(&self) -> &EffectCell {
        &self.left
    }
    /// Returns the footprint queried against the index.
    pub fn right(&self) -> &EffectCell {
        &self.right
    }
    /// Returns the law that establishes overlap between the two footprints.
    pub fn law(&self) -> CellOverlapLaw {
        self.law
    }
}

/// Returns the symmetric address overlap, preserving the source footprints.
pub(crate) fn may_overlap(left: &EffectCell, right: &EffectCell) -> Option<CellOverlap> {
    let law = if left == right {
        CellOverlapLaw::ExactCell
    } else if array_contents_alias(left, right) {
        CellOverlapLaw::ArrayContentsIndexOrLength
    } else {
        return None;
    };
    Some(CellOverlap {
        left: left.clone(),
        right: right.clone(),
        law,
    })
}

/// Addresses conflict only when they overlap and at least one access writes.
pub(crate) fn may_conflict(
    left: &EffectCell,
    left_writes: bool,
    right: &EffectCell,
    right_writes: bool,
) -> Option<CellOverlap> {
    (left_writes || right_writes)
        .then(|| may_overlap(left, right))
        .flatten()
}

fn array_contents_alias(left: &EffectCell, right: &EffectCell) -> bool {
    match (left, right) {
        (
            EffectCell::ArrayContents(origin),
            EffectCell::OwnProperty {
                origin: property_origin,
                property,
            },
        )
        | (
            EffectCell::OwnProperty {
                origin: property_origin,
                property,
            },
            EffectCell::ArrayContents(origin),
        ) => origin == property_origin && (property == "length" || is_array_index(property)),
        _ => false,
    }
}

/// A newer write may discard an older footprint only when it covers all of
/// that footprint. Overlap by itself is not an equivalence relation.
fn dominates(newer: &EffectCell, older: &EffectCell) -> bool {
    if newer == older {
        return true;
    }
    matches!((newer, older),
        (EffectCell::ArrayContents(origin), EffectCell::OwnProperty { origin: property_origin, property })
            if origin == property_origin && (property == "length" || is_array_index(property)))
}

/// A keyed participant map with only the current cross-kind array alias index.
/// Queries visit exact keys and matching array index/length keys for one origin.
#[derive(Debug)]
pub struct CellOverlapIndex<V> {
    by_cell: BTreeMap<EffectCell, V>,
    array_properties: BTreeMap<u32, BTreeSet<String>>,
}

impl<V> Default for CellOverlapIndex<V> {
    fn default() -> Self {
        Self {
            by_cell: BTreeMap::new(),
            array_properties: BTreeMap::new(),
        }
    }
}

impl<V> CellOverlapIndex<V> {
    pub(crate) fn insert(&mut self, cell: EffectCell, value: V) -> Option<V> {
        if !self.by_cell.contains_key(&cell) {
            self.index_cell(&cell);
        }
        self.by_cell.insert(cell, value)
    }

    /// Gets the value for `cell`, inserting its default value when absent.
    pub fn get_or_default(&mut self, cell: EffectCell) -> &mut V
    where
        V: Default,
    {
        if !self.by_cell.contains_key(&cell) {
            self.index_cell(&cell);
        }
        self.by_cell.entry(cell).or_default()
    }

    pub(crate) fn remove_dominated(&mut self, query: &EffectCell) -> Vec<(EffectCell, V)> {
        let cells = self
            .overlapping(query)
            .into_iter()
            .filter(|(overlap, _)| dominates(query, overlap.left()))
            .map(|(overlap, _)| overlap.left().clone())
            .collect::<BTreeSet<_>>();
        cells
            .into_iter()
            .filter_map(|cell| self.remove(&cell).map(|value| (cell, value)))
            .collect()
    }

    pub(crate) fn clear(&mut self) {
        self.by_cell.clear();
        self.array_properties.clear();
    }

    pub(crate) fn remove(&mut self, cell: &EffectCell) -> Option<V> {
        let value = self.by_cell.remove(cell)?;
        if let EffectCell::OwnProperty { origin, property } = cell
            && (property == "length" || is_array_index(property))
        {
            let remove_origin = if let Some(properties) = self.array_properties.get_mut(origin) {
                properties.remove(property);
                properties.is_empty()
            } else {
                false
            };
            if remove_origin {
                self.array_properties.remove(origin);
            }
        }
        Some(value)
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = (&EffectCell, &V)> {
        self.by_cell.iter()
    }

    /// Finds every indexed address that overlaps `query`.
    /// Results preserve both footprints and are sorted deterministically.
    pub fn overlapping(&self, query: &EffectCell) -> Vec<(CellOverlap, &V)> {
        let mut matches = Vec::new();
        if let Some((cell, value)) = self.by_cell.get_key_value(query)
            && let Some(overlap) = may_overlap(cell, query)
        {
            matches.push((overlap, value));
        }
        match query {
            EffectCell::ArrayContents(origin) => {
                if let Some(properties) = self.array_properties.get(origin) {
                    for property in properties {
                        let indexed = EffectCell::OwnProperty {
                            origin: *origin,
                            property: property.clone(),
                        };
                        if let Some((cell, value)) = self.by_cell.get_key_value(&indexed)
                            && let Some(overlap) = may_overlap(cell, query)
                        {
                            matches.push((overlap, value));
                        }
                    }
                }
            }
            EffectCell::OwnProperty { origin, property }
                if property == "length" || is_array_index(property) =>
            {
                let indexed = EffectCell::ArrayContents(*origin);
                if let Some((cell, value)) = self.by_cell.get_key_value(&indexed)
                    && let Some(overlap) = may_overlap(cell, query)
                {
                    matches.push((overlap, value));
                }
            }
            _ => {}
        }
        matches.sort_by(|(left, _), (right, _)| left.cmp(right));
        matches
    }

    fn index_cell(&mut self, cell: &EffectCell) {
        if let EffectCell::OwnProperty { origin, property } = cell
            && (property == "length" || is_array_index(property))
        {
            self.array_properties
                .entry(*origin)
                .or_default()
                .insert(property.clone());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use swc_common::SyntaxContext;

    fn own_property(origin: u32, property: &str) -> EffectCell {
        EffectCell::OwnProperty {
            origin,
            property: property.into(),
        }
    }

    fn cells() -> Vec<EffectCell> {
        vec![
            EffectCell::Binding(("binding".into(), SyntaxContext::empty())),
            EffectCell::GlobalProp("tag".into()),
            EffectCell::ArrayContents(9),
            EffectCell::SetContents(9),
            EffectCell::ObjectShape(9),
            own_property(9, "0"),
            own_property(9, "length"),
            own_property(9, "constructor"),
            own_property(9, "01"),
            own_property(9, "4294967294"),
            own_property(9, "4294967295"),
            EffectCell::ArrayContents(10),
            own_property(10, "0"),
            own_property(8, "0"),
        ]
    }

    #[test]
    fn overlap_is_symmetric_and_limited_to_array_indices_and_length() {
        let cells = cells();
        for left in &cells {
            for right in &cells {
                let forward = may_overlap(left, right);
                let reverse = may_overlap(right, left);
                assert_eq!(forward.is_some(), reverse.is_some());
                if let (Some(forward), Some(reverse)) = (forward, reverse) {
                    assert_eq!(forward.law(), reverse.law());
                    assert_eq!(forward.left(), reverse.right());
                    assert_eq!(forward.right(), reverse.left());
                }
            }
        }
        for property in ["0", "length", "4294967294"] {
            assert_eq!(
                may_overlap(&EffectCell::ArrayContents(9), &own_property(9, property))
                    .unwrap()
                    .law(),
                CellOverlapLaw::ArrayContentsIndexOrLength
            );
        }
        for property in ["constructor", "01", "4294967295"] {
            assert!(
                may_overlap(&EffectCell::ArrayContents(9), &own_property(9, property)).is_none()
            );
        }
        assert!(may_overlap(&EffectCell::ArrayContents(9), &EffectCell::ObjectShape(9)).is_none());
        assert!(may_overlap(&EffectCell::ArrayContents(9), &own_property(10, "0")).is_none());
    }

    #[test]
    fn indexed_overlaps_match_pairwise_reference_law() {
        let cells = cells();
        let mut index = CellOverlapIndex::default();
        for (ordinal, cell) in cells.iter().enumerate() {
            index.insert(cell.clone(), ordinal);
        }
        for query in &cells {
            let mut indexed = index
                .overlapping(query)
                .into_iter()
                .map(|(overlap, _)| overlap)
                .collect::<Vec<_>>();
            let mut reference = cells
                .iter()
                .filter_map(|cell| may_overlap(cell, query))
                .collect::<Vec<_>>();
            indexed.sort();
            reference.sort();
            assert_eq!(indexed, reference, "query {query:?}");
        }
    }

    #[test]
    fn broad_write_dominates_covered_aliases_only() {
        let mut index = CellOverlapIndex::default();
        for cell in [
            EffectCell::ArrayContents(9),
            own_property(9, "0"),
            own_property(9, "length"),
            own_property(9, "constructor"),
            own_property(10, "0"),
        ] {
            index.insert(cell, ());
        }
        let removed = index.remove_dominated(&EffectCell::ArrayContents(9));
        assert_eq!(removed.len(), 3);
        assert!(index.overlapping(&EffectCell::ArrayContents(9)).is_empty());
        assert_eq!(index.overlapping(&EffectCell::ArrayContents(10)).len(), 2);
    }

    #[test]
    fn reads_do_not_conflict_but_any_write_does() {
        let contents = EffectCell::ArrayContents(9);
        let index = own_property(9, "length");
        assert!(may_conflict(&contents, false, &index, false).is_none());
        assert!(may_conflict(&contents, true, &index, false).is_some());
        assert!(may_conflict(&index, false, &contents, true).is_some());
    }

    #[test]
    fn narrowing_a_write_does_not_dominate_the_broad_frontier_entry() {
        assert!(!dominates(
            &own_property(9, "0"),
            &EffectCell::ArrayContents(9)
        ));
        assert!(!dominates(&own_property(9, "1"), &own_property(9, "0")));
        assert!(dominates(
            &EffectCell::ArrayContents(9),
            &own_property(9, "0")
        ));
    }
}
