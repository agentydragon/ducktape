//! Shared overlap law for current effect-cell addresses.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use super::abstract_domain::value::HeapAddress;
use super::heap_contents::is_array_index;
use super::statement_facts::EffectCell;

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CellOverlapLaw {
    /// The two effects name the same current effect cell.
    ExactCell,
    /// Same-origin array contents and a numeric array index or `length` key.
    ArrayContentsIndexOrLength,
    /// A whole global-object surface access overlaps named global slots.
    GlobalObjectSurface,
    /// Provider output recipes conservatively overlap source families with
    /// the same producer module and kind until runtime birth identity is
    /// independently established.
    ProviderAllocationFamily,
}

/// A source-backed pair of effect-cell footprints joined by an overlap law.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct CellOverlap {
    /// The footprint stored in the index (the earlier participant in TV).
    pub(crate) left: EffectCell,
    /// The footprint queried against the index (the later participant in TV).
    pub(crate) right: EffectCell,
    /// The law that establishes overlap between the two footprints.
    pub(crate) law: CellOverlapLaw,
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
    } else if global_object_surface_alias(left, right) {
        CellOverlapLaw::GlobalObjectSurface
    } else if provider_allocation_family_alias(left, right) {
        CellOverlapLaw::ProviderAllocationFamily
    } else {
        return None;
    };
    Some(CellOverlap {
        left: left.clone(),
        right: right.clone(),
        law,
    })
}

fn provider_allocation_family_alias(left: &EffectCell, right: &EffectCell) -> bool {
    fn address(cell: &EffectCell) -> Option<HeapAddress> {
        match cell {
            EffectCell::ArrayContents(address)
            | EffectCell::SetContents(address)
            | EffectCell::ObjectShape(address)
            | EffectCell::DescriptorMapContents(address)
            | EffectCell::OwnProperty {
                origin: address, ..
            }
            | EffectCell::PrivateSlot {
                origin: address, ..
            } => Some(*address),
            _ => None,
        }
    }
    let Some(left_address) = address(left) else {
        return false;
    };
    let Some(right_address) = address(right) else {
        return false;
    };
    if !matches!(
        (left_address, right_address),
        (HeapAddress::ProviderAllocationFamily { .. }, _)
            | (_, HeapAddress::ProviderAllocationFamily { .. })
    ) || !left_address.may_overlap_with_provider_family(right_address)
    {
        return false;
    }

    match (left, right) {
        (EffectCell::ObjectShape(_), EffectCell::ObjectShape(_))
        | (EffectCell::ArrayContents(_), EffectCell::ArrayContents(_))
        | (EffectCell::SetContents(_), EffectCell::SetContents(_))
        | (EffectCell::DescriptorMapContents(_), EffectCell::DescriptorMapContents(_)) => true,
        (EffectCell::ObjectShape(_), EffectCell::OwnProperty { .. })
        | (EffectCell::OwnProperty { .. }, EffectCell::ObjectShape(_))
        | (EffectCell::ObjectShape(_), EffectCell::ArrayContents(_))
        | (EffectCell::ArrayContents(_), EffectCell::ObjectShape(_)) => true,
        (
            EffectCell::OwnProperty {
                property: left_property,
                ..
            },
            EffectCell::OwnProperty {
                property: right_property,
                ..
            },
        ) => left_property == right_property,
        (
            EffectCell::PrivateSlot {
                class: left_class,
                name: left_name,
                ..
            },
            EffectCell::PrivateSlot {
                class: right_class,
                name: right_name,
                ..
            },
        ) => left_class == right_class && left_name == right_name,
        _ => false,
    }
}

fn global_object_surface_alias(left: &EffectCell, right: &EffectCell) -> bool {
    fn is_global_surface(cell: &EffectCell) -> bool {
        matches!(cell, EffectCell::ObjectShape(HeapAddress::GlobalObject))
    }
    fn is_global_property(cell: &EffectCell) -> bool {
        matches!(cell, EffectCell::GlobalProp(_))
            || matches!(
                cell,
                EffectCell::OwnProperty {
                    origin: HeapAddress::GlobalObject,
                    ..
                }
            )
    }
    fn named_alias(left: &EffectCell, right: &EffectCell) -> bool {
        match (left, right) {
            (
                EffectCell::GlobalProp(left),
                EffectCell::OwnProperty {
                    origin: HeapAddress::GlobalObject,
                    property,
                },
            )
            | (
                EffectCell::OwnProperty {
                    origin: HeapAddress::GlobalObject,
                    property,
                },
                EffectCell::GlobalProp(left),
            ) => left == property,
            _ => false,
        }
    }

    (is_global_surface(left) && is_global_property(right))
        || (is_global_surface(right) && is_global_property(left))
        || named_alias(left, right)
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
        ) => {
            origin.may_overlap_with_provider_family(*property_origin)
                && is_array_contents_property(property)
        }
        _ => false,
    }
}

fn is_array_contents_property(property: &str) -> bool {
    property == "length" || is_array_index(property)
}

/// A newer write may discard an older footprint only when it covers all of
/// that footprint. Overlap by itself is not an equivalence relation.
fn dominates(newer: &EffectCell, older: &EffectCell) -> bool {
    if newer == older {
        return true;
    }
    matches!(
        (newer, older),
        (
            EffectCell::ArrayContents(origin),
            EffectCell::OwnProperty {
                origin: property_origin,
                property,
            },
        ) if origin == property_origin && is_array_contents_property(property)
    )
}

/// A keyed participant map with only the current cross-kind array alias index.
/// Queries visit exact keys and matching array index/length keys for one origin.
#[derive(Debug)]
pub struct CellOverlapIndex<V> {
    by_cell: BTreeMap<EffectCell, V>,
    array_properties: BTreeMap<HeapAddress, BTreeSet<String>>,
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
            .filter(|(overlap, _)| dominates(query, &overlap.left))
            .map(|(overlap, _)| overlap.left)
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
            && is_array_contents_property(property)
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
    ///
    /// Results preserve both footprints and are sorted by overlap witness for
    /// deterministic graph and report output.
    pub fn overlapping(&self, query: &EffectCell) -> Vec<(CellOverlap, &V)> {
        let mut matches = Vec::new();
        let mut matched_cells = BTreeSet::new();
        if let Some((cell, value)) = self.by_cell.get_key_value(query)
            && let Some(overlap) = may_overlap(cell, query)
        {
            matched_cells.insert(cell.clone());
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
                            matched_cells.insert(cell.clone());
                            matches.push((overlap, value));
                        }
                    }
                }
            }
            EffectCell::OwnProperty { origin, property }
                if is_array_contents_property(property) =>
            {
                let indexed = EffectCell::ArrayContents(*origin);
                if let Some((cell, value)) = self.by_cell.get_key_value(&indexed)
                    && let Some(overlap) = may_overlap(cell, query)
                {
                    matched_cells.insert(cell.clone());
                    matches.push((overlap, value));
                }
            }
            _ => {}
        }
        if cell_has_family_address(query) {
            for (cell, value) in &self.by_cell {
                if matched_cells.contains(cell) || !cell_has_family_address(cell) {
                    continue;
                }
                if let Some(overlap) = may_overlap(cell, query) {
                    matched_cells.insert(cell.clone());
                    matches.push((overlap, value));
                }
            }
        }
        matches.sort_by(|(left, _), (right, _)| left.cmp(right));
        matches
    }

    fn index_cell(&mut self, cell: &EffectCell) {
        if let EffectCell::OwnProperty { origin, property } = cell
            && is_array_contents_property(property)
        {
            self.array_properties
                .entry(*origin)
                .or_default()
                .insert(property.clone());
        }
    }
}

fn cell_has_family_address(cell: &EffectCell) -> bool {
    let is_family = |address: HeapAddress| {
        matches!(
            address,
            HeapAddress::AllocationFamily { .. } | HeapAddress::ProviderAllocationFamily { .. }
        )
    };
    match cell {
        EffectCell::ArrayContents(address)
        | EffectCell::SetContents(address)
        | EffectCell::ObjectShape(address)
        | EffectCell::DescriptorMapContents(address)
        | EffectCell::OwnProperty {
            origin: address, ..
        }
        | EffectCell::PrivateSlot {
            origin: address, ..
        } => is_family(*address),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use source_identity::{ChunkId, ModuleCellId};
    use swc_common::SyntaxContext;

    use super::super::abstract_domain::value::{
        ClassDefinitionId, HeapModuleScope, Kind, LocalAnalysisScopeId, ProviderCallableId,
    };
    use super::*;
    use crate::facts::LexicalCellId;

    fn provider_family_with_kind(address: HeapAddress, kind: Kind) -> HeapAddress {
        let HeapAddress::ProviderAllocationFamily {
            consumer,
            call_origin,
            producer,
            producer_birth,
            slot,
            ..
        } = address
        else {
            panic!("expected provider allocation family")
        };
        HeapAddress::ProviderAllocationFamily {
            consumer,
            call_origin,
            producer,
            producer_birth,
            slot,
            kind,
        }
    }

    fn cells() -> Vec<EffectCell> {
        let scope = LocalAnalysisScopeId::fresh();
        let object = |origin| HeapAddress::local_family(scope, Kind::Object, origin);
        let set = |origin| HeapAddress::local_family(scope, Kind::Set, origin);
        let class = ClassDefinitionId {
            module: HeapModuleScope::LocalAnalysis(scope),
            origin: 7,
        };
        vec![
            EffectCell::Binding(("binding".into(), SyntaxContext::empty())),
            EffectCell::LexicalCell {
                origin: 1,
                instance: LexicalCellId(2),
            },
            EffectCell::ModuleCell(ModuleCellId {
                chunk: ChunkId(3),
                declaration_origin: 4,
            }),
            EffectCell::GlobalProp("tag".into()),
            EffectCell::ExecutionProgress,
            EffectCell::ArrayContents(object(9)),
            EffectCell::SetContents(set(9)),
            EffectCell::GlobalSymbolRegistry,
            EffectCell::ObjectShape(object(9)),
            EffectCell::DescriptorMapContents(object(9)),
            EffectCell::OwnProperty {
                origin: object(9),
                property: "0".into(),
            },
            EffectCell::OwnProperty {
                origin: object(9),
                property: "length".into(),
            },
            EffectCell::OwnProperty {
                origin: object(9),
                property: "constructor".into(),
            },
            EffectCell::OwnProperty {
                origin: object(9),
                property: "01".into(),
            },
            EffectCell::OwnProperty {
                origin: object(9),
                property: "4294967294".into(),
            },
            EffectCell::OwnProperty {
                origin: object(9),
                property: "4294967295".into(),
            },
            EffectCell::PrivateSlot {
                origin: object(9),
                class,
                name: "field".into(),
            },
            EffectCell::ArrayContents(object(10)),
            EffectCell::OwnProperty {
                origin: object(10),
                property: "0".into(),
            },
            EffectCell::OwnProperty {
                origin: object(8),
                property: "0".into(),
            },
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
                    assert_eq!(forward.law, reverse.law);
                    assert_eq!(forward.left, reverse.right);
                    assert_eq!(forward.right, reverse.left);
                }
            }
        }
        assert_eq!(
            may_overlap(&cells[5], &cells[10]).unwrap().law,
            CellOverlapLaw::ArrayContentsIndexOrLength
        );
        assert_eq!(
            may_overlap(&cells[5], &cells[11]).unwrap().law,
            CellOverlapLaw::ArrayContentsIndexOrLength
        );
        assert_eq!(
            may_overlap(&cells[5], &cells[14]).unwrap().law,
            CellOverlapLaw::ArrayContentsIndexOrLength
        );
        assert!(may_overlap(&cells[5], &cells[12]).is_none());
        assert!(may_overlap(&cells[5], &cells[13]).is_none());
        assert!(may_overlap(&cells[5], &cells[15]).is_none());
        assert!(may_overlap(&cells[5], &cells[17]).is_none());
        assert!(may_overlap(&cells[5], &cells[18]).is_none());
        assert!(may_overlap(&cells[5], &cells[19]).is_none());
        assert_eq!(
            may_overlap(&cells[17], &cells[18]).unwrap().law,
            CellOverlapLaw::ArrayContentsIndexOrLength
        );
        assert!(may_overlap(&cells[8], &cells[10]).is_none());
        assert!(may_overlap(&cells[9], &cells[12]).is_none());
    }

    #[test]
    fn indexed_overlaps_match_the_pairwise_reference_law() {
        let cells = cells();
        let mut indexed_cells = CellOverlapIndex::default();
        for (ordinal, cell) in cells.iter().enumerate() {
            indexed_cells.insert(cell.clone(), ordinal);
        }
        for query in &cells {
            let mut indexed = indexed_cells
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
    fn removing_a_broad_cell_clears_only_its_indexed_aliases() {
        let cells = cells();
        let mut index = CellOverlapIndex::default();
        for (ordinal, cell) in cells.iter().enumerate() {
            index.insert(cell.clone(), ordinal);
        }
        let target = cells[5].clone();
        let target_address = match &target {
            EffectCell::ArrayContents(address) => *address,
            _ => unreachable!(),
        };
        let removed = index.remove_dominated(&target);
        assert!(removed.iter().any(|(cell, _)| *cell == target));
        assert!(removed.iter().any(|(cell, _)| {
            matches!(cell, EffectCell::OwnProperty { origin, property } if *origin == target_address && (property == "0" || property == "length" || property == "4294967294"))
        }));
        assert!(index.overlapping(&target).is_empty());
        assert_eq!(index.overlapping(&cells[17]).len(), 2);
        assert_eq!(index.overlapping(&cells[19]).len(), 1);
    }

    #[test]
    fn read_read_does_not_conflict_even_when_regions_overlap() {
        let contents = cells()[5].clone();
        let EffectCell::ArrayContents(contents_address) = &contents else {
            unreachable!();
        };
        let index = EffectCell::OwnProperty {
            origin: *contents_address,
            property: "length".into(),
        };
        assert!(may_conflict(&contents, false, &index, false).is_none());
        assert!(may_conflict(&contents, true, &index, false).is_some());
        assert!(may_conflict(&index, false, &contents, true).is_some());
    }

    #[test]
    fn global_surface_overlaps_named_global_and_own_property_cells() {
        let global_shape = EffectCell::ObjectShape(HeapAddress::GlobalObject);
        let named_global = EffectCell::GlobalProp("slot".to_owned());
        let global_own_property = EffectCell::OwnProperty {
            origin: HeapAddress::GlobalObject,
            property: "slot".to_owned(),
        };
        let unrelated_global_property = EffectCell::OwnProperty {
            origin: HeapAddress::GlobalObject,
            property: "other".to_owned(),
        };

        assert_eq!(
            may_overlap(&global_shape, &named_global).unwrap().law,
            CellOverlapLaw::GlobalObjectSurface
        );
        assert_eq!(
            may_overlap(&global_shape, &global_own_property)
                .unwrap()
                .law,
            CellOverlapLaw::GlobalObjectSurface
        );
        assert_eq!(
            may_overlap(&named_global, &global_own_property)
                .unwrap()
                .law,
            CellOverlapLaw::GlobalObjectSurface
        );
        assert!(may_overlap(&named_global, &unrelated_global_property).is_none());
        assert!(
            may_overlap(
                &global_shape,
                &EffectCell::ObjectShape(HeapAddress::local_family(
                    LocalAnalysisScopeId::fresh(),
                    Kind::Object,
                    19,
                ))
            )
            .is_none()
        );
    }

    #[test]
    fn same_source_coordinate_in_different_chunks_does_not_overlap() {
        use source_identity::ChunkId;

        let provider = HeapAddress::module_family(ChunkId(3), Kind::Object, 9);
        let other = HeapAddress::module_family(ChunkId(4), Kind::Object, 9);
        let provider_array = EffectCell::ArrayContents(provider);
        let provider_index = EffectCell::OwnProperty {
            origin: provider,
            property: "0".into(),
        };
        let other_index = EffectCell::OwnProperty {
            origin: other,
            property: "0".into(),
        };

        assert!(may_overlap(&provider_array, &provider_index).is_some());
        assert!(may_overlap(&provider_array, &other_index).is_none());

        let provider_class = ClassDefinitionId {
            module: HeapModuleScope::Chunk(ChunkId(3)),
            origin: 7,
        };
        let other_class = ClassDefinitionId {
            module: HeapModuleScope::Chunk(ChunkId(4)),
            origin: 7,
        };
        let provider_private_slot = EffectCell::PrivateSlot {
            origin: HeapAddress::constructor_instances(provider_class),
            class: provider_class,
            name: "field".into(),
        };
        let other_private_slot = EffectCell::PrivateSlot {
            origin: HeapAddress::constructor_instances(other_class),
            class: other_class,
            name: "field".into(),
        };
        assert!(may_overlap(&provider_private_slot, &other_private_slot).is_none());
    }

    #[test]
    fn provider_families_overlap_producer_source_without_using_recipe_labels() {
        let consumer = ChunkId(12);
        let producer_module = ChunkId(4);
        let producer = ProviderCallableId {
            module: HeapModuleScope::Chunk(producer_module),
            origin: 90,
        };
        let first = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(consumer),
            call_origin: 101,
            producer,
            producer_birth: None,
            slot: 0,
            kind: Kind::Object,
        };
        let other_recipe = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(ChunkId(13)),
            call_origin: 202,
            producer: ProviderCallableId {
                module: HeapModuleScope::Chunk(producer_module),
                origin: 99,
            },
            producer_birth: None,
            slot: 7,
            kind: Kind::Object,
        };
        let producer_source = HeapAddress::module_family(producer_module, Kind::Object, 777);
        let unrelated_source = HeapAddress::module_family(ChunkId(5), Kind::Object, 777);
        let other_kind = HeapAddress::module_family(producer_module, Kind::Array, 777);

        let recipe_property = EffectCell::OwnProperty {
            origin: first,
            property: "field".into(),
        };
        let source_property = EffectCell::OwnProperty {
            origin: producer_source,
            property: "field".into(),
        };
        assert_eq!(
            may_overlap(&recipe_property, &source_property).unwrap().law,
            CellOverlapLaw::ProviderAllocationFamily
        );
        assert!(
            may_overlap(
                &recipe_property,
                &EffectCell::OwnProperty {
                    origin: producer_source,
                    property: "other".into(),
                }
            )
            .is_none()
        );
        assert!(
            may_overlap(
                &recipe_property,
                &EffectCell::OwnProperty {
                    origin: unrelated_source,
                    property: "field".into(),
                }
            )
            .is_none()
        );
        assert!(
            may_overlap(
                &recipe_property,
                &EffectCell::OwnProperty {
                    origin: other_kind,
                    property: "field".into(),
                }
            )
            .is_none()
        );
        assert!(first.may_overlap_with_provider_family(other_recipe));

        let mut index = CellOverlapIndex::default();
        index.insert(recipe_property.clone(), 1);
        assert_eq!(
            index.overlapping(&source_property)[0].0.law(),
            CellOverlapLaw::ProviderAllocationFamily
        );
        let mut reverse_index = CellOverlapIndex::default();
        reverse_index.insert(source_property.clone(), 2);
        assert_eq!(
            reverse_index.overlapping(&recipe_property)[0].0.law(),
            CellOverlapLaw::ProviderAllocationFamily
        );

        let recipe_contents = EffectCell::ArrayContents(first);
        let source_index = EffectCell::OwnProperty {
            origin: producer_source,
            property: "0".into(),
        };
        assert_eq!(
            may_overlap(&recipe_contents, &source_index).unwrap().law,
            CellOverlapLaw::ArrayContentsIndexOrLength
        );
    }

    #[test]
    fn verified_provider_birth_refines_cross_module_effect_cell_overlap() {
        let producer = HeapModuleScope::Chunk(ChunkId(4));
        let first = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(ChunkId(9)),
            call_origin: 31,
            producer: ProviderCallableId {
                module: producer,
                origin: 10,
            },
            producer_birth: Some(42),
            slot: 0,
            kind: Kind::Object,
        };
        let same_birth_other_recipe = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(ChunkId(12)),
            call_origin: 81,
            producer: ProviderCallableId {
                module: producer,
                origin: 20,
            },
            producer_birth: Some(42),
            slot: 3,
            kind: Kind::Object,
        };
        let other_birth = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(ChunkId(13)),
            call_origin: 82,
            producer: ProviderCallableId {
                module: producer,
                origin: 20,
            },
            producer_birth: Some(43),
            slot: 3,
            kind: Kind::Object,
        };
        let unverified = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(ChunkId(14)),
            call_origin: 83,
            producer: ProviderCallableId {
                module: producer,
                origin: 20,
            },
            producer_birth: None,
            slot: 3,
            kind: Kind::Object,
        };
        let field = |origin| EffectCell::OwnProperty {
            origin,
            property: "field".into(),
        };
        let set_cell = |origin| EffectCell::SetContents(origin);
        let source_birth = field(HeapAddress::module_family(ChunkId(4), Kind::Object, 42));
        let source_other_birth = field(HeapAddress::module_family(ChunkId(4), Kind::Object, 43));

        assert!(may_overlap(&field(first), &field(same_birth_other_recipe)).is_some());
        assert!(may_overlap(&field(first), &field(unverified)).is_some());
        assert!(may_overlap(&field(first), &source_birth).is_some());
        assert!(may_overlap(&field(other_birth), &source_other_birth).is_some());
        assert!(may_overlap(&field(first), &field(other_birth)).is_none());
        assert!(may_overlap(&field(first), &source_other_birth).is_none());

        let set_first = provider_family_with_kind(first, Kind::Set);
        let set_same_birth = provider_family_with_kind(same_birth_other_recipe, Kind::Set);
        let set_other_birth = provider_family_with_kind(other_birth, Kind::Set);
        let set_unverified = provider_family_with_kind(unverified, Kind::Set);
        assert!(may_overlap(&set_cell(set_first), &set_cell(set_same_birth)).is_some());
        assert!(may_overlap(&set_cell(set_first), &set_cell(set_unverified)).is_some());
        assert!(may_overlap(&set_cell(set_first), &set_cell(set_other_birth)).is_none());

        let mut index = CellOverlapIndex::default();
        index.insert(field(first), 1);
        assert_eq!(index.overlapping(&source_birth).len(), 1);
        assert!(index.overlapping(&source_other_birth).is_empty());
    }

    #[test]
    fn narrowing_a_write_does_not_dominate_the_broad_frontier_entry() {
        let origin = HeapAddress::module_family(ChunkId(3), Kind::Array, 9);
        let index = |property: &str| EffectCell::OwnProperty {
            origin,
            property: property.into(),
        };
        let contents = EffectCell::ArrayContents(origin);
        assert!(!dominates(&index("0"), &contents));
        assert!(!dominates(&index("1"), &index("0")));
        assert!(dominates(&contents, &index("0")));
    }
}
