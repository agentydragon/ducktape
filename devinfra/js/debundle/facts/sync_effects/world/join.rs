//! The one join of execution worlds: strict when every alternative agrees,
//! otherwise a conservative pointwise join that never certifies a property,
//! descriptor, completion or publication fact and never panics.

use super::cells::{CellInitialization, LexicalCell, LexicalCells};
use super::descriptor_heap::OwnDescriptorMapState;
use super::object_surface::{AppliedAccessor, ObjectSurfaceStore};
use super::prefix_values::PrefixValues;
use super::{
    ClassState, DenseArrays, ModuleBindings, NativeRetained, Publication, ReachabilityState,
    Revision, World, WorldScope,
};
use crate::facts::abstract_domain::value::Value;
use crate::facts::sync_effects::allocation_transfer::WritablePrefixState;
use crate::facts::sync_effects::property_inference::CapturedPropertyDescriptor;
use crate::facts::sync_effects::value_model::{AbstractValue, DenseArray, LexicalCellId, Values};
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};

/// The result of joining a family of alternative worlds.
#[must_use]
#[derive(Debug, Clone, PartialEq)]
pub(in crate::facts::sync_effects) struct Joined {
    pub(in crate::facts::sync_effects) world: World,
    /// Some alternative lost precision in the join.
    pub(in crate::facts::sync_effects) widened: bool,
    /// The joined world cannot resume the live control cursor; the caller
    /// runs one conservative remainder fallback instead.
    pub(in crate::facts::sync_effects) refused: Option<JoinRefusal>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(in crate::facts::sync_effects) enum JoinRefusal {
    EmptySet,
    DivergentActiveCells,
    DivergentScope,
}

/// Pointwise join of one owner group over every alternative. `widened` is set
/// when the result is weaker than some input.
trait Join: Sized {
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self;
}

impl World {
    /// Whether two worlds describe the same prefix. A shared allocation is a
    /// cheap sufficient check; detached copy-on-write roots can still be equal.
    pub(in crate::facts::sync_effects) fn same_contents(&self, other: &Self) -> bool {
        std::ptr::eq(self, other) || self == other
    }

    /// Join every alternative into one world. Refusal is an outcome, never a
    /// panic: the returned world is the pointwise join of the remaining
    /// owners with `reachability.incomplete` set.
    pub(in crate::facts::sync_effects) fn join(worlds: &[&World]) -> Joined {
        let Some(first) = worlds.first().copied() else {
            // Nothing ran, so no scope is known; the caller never resumes
            // from this world.
            let mut world = World::new(WorldScope::Application);
            world.reachability.incomplete = true;
            return Joined {
                world,
                widened: true,
                refused: Some(JoinRefusal::EmptySet),
            };
        };
        if worlds.iter().all(|world| first.same_contents(world)) {
            return Joined {
                world: first.clone(),
                widened: false,
                refused: None,
            };
        }
        // Destructure so that a new owner without a join arm fails to compile.
        let World {
            scope,
            cells: _,
            active_cells: _,
            module: _,
            surface: _,
            descriptors: _,
            arrays: _,
            native: _,
            classes: _,
            publication: _,
            reachability: _,
            prerequisites: _,
            revision: _,
        } = first;
        let mut widened = true;
        let mut refused = None;
        if worlds.iter().any(|world| world.scope != *scope) {
            refused = Some(JoinRefusal::DivergentScope);
        }
        let active_cells = intersect_ord_maps(&each(worlds, |world| &world.active_cells));
        if worlds
            .iter()
            .any(|world| world.active_cells != first.active_cells)
        {
            refused = refused.or(Some(JoinRefusal::DivergentActiveCells));
        }
        let mut arrays = DenseArrays::join(&each(worlds, |world| &world.arrays), &mut widened);
        let mut surface =
            ObjectSurfaceStore::join(&each(worlds, |world| &world.surface), &mut widened);
        join_dense_arrays(worlds, &mut arrays, &mut surface);
        let mut reachability =
            ReachabilityState::join(&each(worlds, |world| &world.reachability), &mut widened);
        reachability.incomplete |= refused.is_some();
        let mut revision = Revision::join(&each(worlds, |world| &world.revision), &mut widened);
        revision.bump_prefix();
        revision.bump_reachability();
        let world = World {
            scope: *scope,
            cells: LexicalCells::join(&each(worlds, |world| &world.cells), &mut widened),
            active_cells,
            module: ModuleBindings::join(&each(worlds, |world| &world.module), &mut widened),
            surface,
            descriptors: join_descriptor_maps(&each(worlds, |world| &world.descriptors)),
            arrays,
            native: NativeRetained::join(&each(worlds, |world| &world.native), &mut widened),
            classes: ClassState::join(&each(worlds, |world| &world.classes), &mut widened),
            publication: Publication::join(&each(worlds, |world| &world.publication), &mut widened),
            reachability,
            // A joined prefix holds only under every alternative's conditions.
            prerequisites: worlds.iter().fold(
                crate::analysis_inputs::ProofDependencies::default(),
                |mut prerequisites, world| {
                    prerequisites.extend(&world.prerequisites);
                    prerequisites
                },
            ),
            revision,
        };
        Joined {
            world,
            widened,
            refused,
        }
    }
}

fn each<'a, T>(worlds: &[&'a World], field: impl Fn(&'a World) -> &'a T) -> Vec<&'a T> {
    worlds.iter().map(|world| field(world)).collect()
}

impl Join for ModuleBindings {
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self {
        let first = alternatives[0];
        let joined = Self {
            prefix_values: PrefixValues::join(
                &each_field(alternatives, |module| &module.prefix_values),
                join_value_maps,
            ),
            // Only an agreed selection survives; a value present on one path
            // only is unknown on the others.
            prefix_callable_selections: intersect_ord_maps(&each_field(alternatives, |module| {
                &module.prefix_callable_selections
            })),
            initialized_bindings: intersect_sets(&each_field(alternatives, |module| {
                &module.initialized_bindings
            })),
            top_env: join_value_maps(&each_field(alternatives, |module| &module.top_env)),
            top_captures: join_value_maps(&each_field(alternatives, |module| &module.top_captures)),
        };
        *widened |= joined != *first;
        joined
    }
}

impl Join for ObjectSurfaceStore {
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self {
        let first = alternatives[0];
        let joined = Self {
            prefix_object_properties: join_nested_property_maps(&each_field(
                alternatives,
                |surface| &surface.prefix_object_properties,
            )),
            writable_prefix: join_writable_prefix(alternatives),
            applied_accessors: join_applied_accessors(alternatives),
            ordinary_spread_copies: intersect_sets(&each_field(alternatives, |surface| {
                &surface.ordinary_spread_copies
            })),
            descriptor_copy_results: intersect_sets(&each_field(alternatives, |surface| {
                &surface.descriptor_copy_results
            })),
            tainted_objects: union_sets(&each_field(alternatives, |surface| {
                &surface.tainted_objects
            })),
            unsettled_objects: union_sets(&each_field(alternatives, |surface| {
                &surface.unsettled_objects
            })),
        };
        *widened |= joined != *first;
        joined
    }
}

impl Join for DenseArrays {
    /// `dense` is joined afterwards by `join_dense_arrays`, which may also
    /// invalidate surface facts of arrays whose layout diverged.
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self {
        let first = alternatives[0];
        let joined = Self {
            dense: OrdMap::new(),
            primitive: OrdMap::new(),
            unknown_contents: union_sets(&each_field(alternatives, |arrays| {
                &arrays.unknown_contents
            })),
            accumulated_values: join_value_maps(&each_field(alternatives, |arrays| {
                &arrays.accumulated_values
            })),
            intrinsic_mutated: alternatives.iter().any(|arrays| arrays.intrinsic_mutated),
        };
        *widened |= joined.unknown_contents != first.unknown_contents
            || joined.accumulated_values != first.accumulated_values
            || joined.intrinsic_mutated != first.intrinsic_mutated;
        joined
    }
}

impl Join for NativeRetained {
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self {
        let first = alternatives[0];
        // A revoked key is its capture identity, which interns one payload per
        // run; alternatives can only add keys.
        let mut revoked_audited_results = BTreeMap::new();
        for native in alternatives {
            for (key, captured) in &native.revoked_audited_results {
                let existing = revoked_audited_results
                    .entry(*key)
                    .or_insert_with(|| captured.clone());
                *widened |= existing.values != captured.values;
            }
        }
        let joined = Self {
            tainted_sets: union_sets(&each_field(alternatives, |native| &native.tainted_sets)),
            tainted_functions: union_sets(&each_field(alternatives, |native| {
                &native.tainted_functions
            })),
            revoked_audited_results,
            uncertain_execution: alternatives.iter().any(|native| native.uncertain_execution),
        };
        *widened |= joined != *first;
        joined
    }
}

impl Join for ClassState {
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self {
        let first = alternatives[0];
        let joined = Self {
            superclasses: join_value_maps(&each_field(alternatives, |classes| {
                &classes.superclasses
            })),
            tainted: union_sets(&each_field(alternatives, |classes| &classes.tainted)),
            escaped_capabilities: union_sets(&each_field(alternatives, |classes| {
                &classes.escaped_capabilities
            })),
            first_members_publications: intersect_maps(&each_field(alternatives, |classes| {
                &classes.first_members_publications
            })),
            checked_constructor_inputs: intersect_sets(&each_field(alternatives, |classes| {
                &classes.checked_constructor_inputs
            })),
            // An all-path capability, never a may-set: it suppresses possible
            // source-unit effects for private construction results.
            private_constructor_result_owners: intersect_sets(&each_field(
                alternatives,
                |classes| &classes.private_constructor_result_owners,
            )),
            private_fresh_instance_bindings: intersect_sets(&each_field(alternatives, |classes| {
                &classes.private_fresh_instance_bindings
            })),
        };
        *widened |= joined != *first;
        joined
    }
}

impl Join for Publication {
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self {
        let first = alternatives[0];
        let joined = Self {
            global_escaped_callbacks: union_values(&each_field(alternatives, |publication| {
                &publication.global_escaped_callbacks
            })),
            exported_runtime_values: union_values(&each_field(alternatives, |publication| {
                &publication.exported_runtime_values
            })),
            first_callback_publication_owners: intersect_maps(&each_field(
                alternatives,
                |publication| &publication.first_callback_publication_owners,
            )),
            returned_callback_owners: union_sets(&each_field(alternatives, |publication| {
                &publication.returned_callback_owners
            })),
            accepted_deferred_global_writes: intersect_sets(&each_field(
                alternatives,
                |publication| &publication.accepted_deferred_global_writes,
            )),
            parameter_initialized_objects: intersect_sets(&each_field(
                alternatives,
                |publication| &publication.parameter_initialized_objects,
            )),
        };
        *widened |= joined != *first;
        joined
    }
}

impl Join for ReachabilityState {
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self {
        let first = alternatives[0];
        let joined = Self {
            unknown_call_seen: alternatives.iter().any(|state| state.unknown_call_seen),
            incomplete: alternatives.iter().any(|state| state.incomplete),
            expanded: intersect_sets(&each_field(alternatives, |state| &state.expanded)),
            deferred: union_maps(&each_field(alternatives, |state| &state.deferred)),
            owners: union_sets(&each_field(alternatives, |state| &state.owners)),
        };
        *widened |= joined != *first;
        joined
    }
}

impl Join for Revision {
    fn join(alternatives: &[&Self], _widened: &mut bool) -> Self {
        Self {
            prefix: alternatives
                .iter()
                .map(|revision| revision.prefix)
                .max()
                .unwrap_or_default(),
            reachability: alternatives
                .iter()
                .map(|revision| revision.reachability)
                .max()
                .unwrap_or_default(),
        }
    }
}

impl Join for LexicalCells {
    /// The same id names the same binding on every path: sibling leaves mint
    /// disjoint ids (`alternatives::explore`). A cell missing on some path
    /// may hold anything there.
    fn join(alternatives: &[&Self], widened: &mut bool) -> Self {
        let mut by_id = BTreeMap::<LexicalCellId, Vec<&LexicalCell>>::new();
        for cells in alternatives {
            for (id, cell) in live_cells(cells) {
                by_id.entry(id).or_default().push(cell);
            }
        }
        let mut joined = LexicalCells {
            slots: (0..alternatives
                .iter()
                .map(|cells| cells.slots.len())
                .max()
                .unwrap_or_default())
                .map(|_| None)
                .collect(),
            occupied: Vec::new(),
            next_collection_at: alternatives
                .iter()
                .map(|cells| cells.next_collection_at)
                .max()
                .unwrap_or(1),
        };
        for (id, present) in by_id {
            let first = present[0];
            let mut values = Values::new();
            for cell in &present {
                values.extend(cell.values.iter().cloned());
            }
            let everywhere = present.len() == alternatives.len();
            if !everywhere {
                values.extend(unknown());
            }
            let all_same = everywhere
                && present.iter().all(|cell| {
                    cell.values == first.values && cell.initialization == first.initialization
                });
            let initialization = if everywhere
                && present
                    .iter()
                    .all(|cell| cell.initialization == first.initialization)
            {
                first.initialization
            } else if everywhere
                && present
                    .iter()
                    .all(|cell| cell.initialization == CellInitialization::Initialized)
            {
                CellInitialization::Initialized
            } else {
                CellInitialization::MaybeInitialized
            };
            *widened |= !all_same || present.iter().any(|cell| cell.kind != first.kind);
            joined.slots[id.0] = Some(Box::new(LexicalCell {
                values,
                kind: first.kind,
                initialization,
            }));
            joined.occupied.push(id);
        }
        joined
    }
}

/// Every live cell with its id, in allocation order.
fn live_cells(cells: &LexicalCells) -> impl Iterator<Item = (LexicalCellId, &LexicalCell)> {
    cells
        .occupied
        .iter()
        .filter_map(|id| Some((*id, cells.slots.get(id.0)?.as_deref()?)))
}

/// Arrays whose exact layout agrees keep it; equal lengths join element-wise;
/// anything else loses the dense carrier and the object's settled surface.
fn join_dense_arrays(
    worlds: &[&World],
    arrays: &mut DenseArrays,
    surface: &mut ObjectSurfaceStore,
) {
    let keys = worlds
        .iter()
        .flat_map(|world| world.arrays.dense.keys().copied())
        .collect::<BTreeSet<_>>();
    for key in keys {
        let present = worlds
            .iter()
            .filter_map(|world| world.arrays.dense.get(&key))
            .collect::<Vec<_>>();
        let first = present[0];
        if present.len() == worlds.len() && present.iter().all(|array| *array == first) {
            arrays.dense.insert(key, first.clone());
            continue;
        }
        if present.len() == worlds.len()
            && present
                .iter()
                .all(|array| array.slots.len() == first.slots.len())
        {
            // The exact length is a layout fact; only element values differ.
            let slots = (0..first.slots.len())
                .map(|index| {
                    present
                        .iter()
                        .flat_map(|array| array.slots[index].iter().cloned())
                        .collect::<Values>()
                })
                .collect();
            arrays.dense.insert(key, DenseArray { slots });
        } else {
            arrays.unknown_contents.insert(key);
            surface.unsettled_objects.insert(key);
        }
    }
    let primitive_keys = worlds
        .iter()
        .flat_map(|world| world.arrays.primitive.keys().copied())
        .collect::<BTreeSet<_>>();
    for key in primitive_keys {
        let first = worlds[0].arrays.primitive.get(&key);
        if first.is_some()
            && worlds
                .iter()
                .all(|world| world.arrays.primitive.get(&key) == first)
        {
            if let Some(marker) = first {
                arrays.primitive.insert(key, marker.clone());
            }
            continue;
        }
        arrays.unknown_contents.insert(key);
        surface.unsettled_objects.insert(key);
    }
}

fn join_writable_prefix(surfaces: &[&ObjectSurfaceStore]) -> OrdMap<u32, WritablePrefixState> {
    let keys = surfaces
        .iter()
        .flat_map(|surface| surface.writable_prefix.keys().copied())
        .collect::<BTreeSet<_>>();
    keys.into_iter()
        .map(|key| {
            let first = surfaces[0].writable_prefix.get(&key);
            let state = match first {
                Some(state)
                    if surfaces
                        .iter()
                        .all(|surface| surface.writable_prefix.get(&key) == Some(state)) =>
                {
                    state.clone()
                }
                _ => WritablePrefixState::Unknown,
            };
            (key, state)
        })
        .collect()
}

fn join_applied_accessors(
    surfaces: &[&ObjectSurfaceStore],
) -> OrdMap<u32, BTreeMap<String, AppliedAccessor>> {
    let objects = surfaces
        .iter()
        .flat_map(|surface| surface.applied_accessors.keys().copied())
        .collect::<BTreeSet<_>>();
    objects
        .into_iter()
        .map(|object| {
            let keys = surfaces
                .iter()
                .filter_map(|surface| surface.applied_accessors.get(&object))
                .flat_map(|properties| properties.keys().cloned())
                .collect::<BTreeSet<_>>();
            let properties = keys
                .into_iter()
                .map(|key| {
                    let mut accessor = AppliedAccessor::default();
                    for surface in surfaces {
                        match surface
                            .applied_accessors
                            .get(&object)
                            .and_then(|properties| properties.get(&key))
                        {
                            Some(value) => {
                                accessor.getter.extend(value.getter.iter().cloned());
                                accessor.setter.extend(value.setter.iter().cloned());
                            }
                            None => {
                                accessor.getter.extend(unknown());
                                accessor.setter.extend(unknown());
                            }
                        }
                    }
                    (key, accessor)
                })
                .collect::<BTreeMap<_, _>>();
            (object, properties)
        })
        .collect()
}

/// A map that agrees on every path keeps its validity; otherwise the joined
/// map is invalid but retains every value any path may hold.
fn join_descriptor_maps<K: Ord + Clone>(
    maps: &[&OrdMap<K, OwnDescriptorMapState>],
) -> OrdMap<K, OwnDescriptorMapState> {
    let keys = maps
        .iter()
        .flat_map(|map| map.keys().cloned())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for key in keys {
        let present = maps
            .iter()
            .filter_map(|map| map.get(&key))
            .collect::<Vec<_>>();
        let first = present[0];
        if present.len() == maps.len() && present.iter().all(|map| *map == first) {
            joined.insert(key, first.clone());
            continue;
        }
        let mut result = first.clone();
        result.valid = false;
        let mut order = Vec::new();
        let mut properties = BTreeMap::<String, CapturedPropertyDescriptor>::new();
        for candidate in &present {
            result
                .retained_values
                .extend(candidate.retained_values.iter().cloned());
            for name in &candidate.property_order {
                if !order.contains(name) {
                    order.push(name.clone());
                }
            }
            for (name, descriptor) in &candidate.properties {
                match (properties.get_mut(name), descriptor) {
                    (
                        Some(CapturedPropertyDescriptor::Data(values)),
                        CapturedPropertyDescriptor::Data(other),
                    ) => values.extend(other.iter().cloned()),
                    (
                        Some(CapturedPropertyDescriptor::Accessor { getter, setter }),
                        CapturedPropertyDescriptor::Accessor {
                            getter: other_getter,
                            setter: other_setter,
                        },
                    ) => {
                        getter.extend(other_getter.iter().cloned());
                        setter.extend(other_setter.iter().cloned());
                    }
                    (None, descriptor) => {
                        properties.insert(name.clone(), descriptor.clone());
                    }
                    _ => {
                        properties.remove(name);
                    }
                }
                match descriptor {
                    CapturedPropertyDescriptor::Data(values) => {
                        result.retained_values.extend(values.iter().cloned())
                    }
                    CapturedPropertyDescriptor::Accessor { getter, setter } => {
                        result.retained_values.extend(getter.iter().cloned());
                        result.retained_values.extend(setter.iter().cloned());
                    }
                }
            }
        }
        result.property_order = order;
        result.properties = properties;
        joined.insert(key, result);
    }
    joined
}

fn each_field<'a, T, F>(alternatives: &[&'a T], field: impl Fn(&'a T) -> &'a F) -> Vec<&'a F> {
    alternatives.iter().map(|item| field(item)).collect()
}

fn unknown() -> Values {
    Values::from([AbstractValue::Value(Value::Top)])
}

/// Pointwise union; a key missing on some path is unknown there.
fn join_value_maps<K: Ord + Clone>(maps: &[&OrdMap<K, Values>]) -> OrdMap<K, Values> {
    let keys = maps
        .iter()
        .flat_map(|map| map.keys().cloned())
        .collect::<BTreeSet<_>>();
    keys.into_iter()
        .map(|key| {
            let mut values = Values::new();
            for map in maps {
                match map.get(&key) {
                    Some(branch) => values.extend(branch.iter().cloned()),
                    None => values.extend(unknown()),
                }
            }
            (key, values)
        })
        .collect()
}

fn join_nested_property_maps<K: Ord + Clone>(
    maps: &[&OrdMap<u32, BTreeMap<K, Values>>],
) -> OrdMap<u32, BTreeMap<K, Values>> {
    let objects = maps
        .iter()
        .flat_map(|map| map.keys().copied())
        .collect::<BTreeSet<_>>();
    objects
        .into_iter()
        .map(|object| {
            let keys = maps
                .iter()
                .filter_map(|map| map.get(&object))
                .flat_map(|properties| properties.keys().cloned())
                .collect::<BTreeSet<_>>();
            let properties = keys
                .into_iter()
                .map(|key| {
                    let mut values = Values::new();
                    for map in maps {
                        match map.get(&object).and_then(|properties| properties.get(&key)) {
                            Some(branch) => values.extend(branch.iter().cloned()),
                            None => values.extend(unknown()),
                        }
                    }
                    (key, values)
                })
                .collect::<BTreeMap<_, _>>();
            (object, properties)
        })
        .collect()
}

fn union_values(values: &[&Values]) -> Values {
    values
        .iter()
        .flat_map(|values| values.iter().cloned())
        .collect()
}

fn union_sets<T: Ord + Clone>(sets: &[&BTreeSet<T>]) -> BTreeSet<T> {
    sets.iter().flat_map(|set| set.iter().cloned()).collect()
}

/// The first path's value wins for a shared key.
fn union_maps<K: Ord + Clone, V: Clone>(maps: &[&BTreeMap<K, V>]) -> BTreeMap<K, V> {
    let mut joined = BTreeMap::new();
    for map in maps {
        for (key, value) in map.iter() {
            joined.entry(key.clone()).or_insert_with(|| value.clone());
        }
    }
    joined
}

fn intersect_sets<T: Ord + Clone>(sets: &[&BTreeSet<T>]) -> BTreeSet<T> {
    let Some(first) = sets.first() else {
        return BTreeSet::new();
    };
    first
        .iter()
        .filter(|value| sets.iter().all(|set| set.contains(*value)))
        .cloned()
        .collect()
}

fn intersect_maps<K: Ord + Clone, V: Clone + PartialEq>(
    maps: &[&BTreeMap<K, V>],
) -> BTreeMap<K, V> {
    let Some(first) = maps.first() else {
        return BTreeMap::new();
    };
    first
        .iter()
        .filter(|(key, value)| maps.iter().all(|map| map.get(*key) == Some(*value)))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect()
}

fn intersect_ord_maps<K: Ord + Clone, V: Clone + PartialEq>(
    maps: &[&OrdMap<K, V>],
) -> OrdMap<K, V> {
    let Some(first) = maps.first() else {
        return OrdMap::new();
    };
    first
        .iter()
        .filter(|(key, value)| maps.iter().all(|map| map.get(*key) == Some(*value)))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::super::cells::{CellInitialization, LexicalCell};
    use super::super::prefix_values::{PrefixValues, TopBindings};
    use super::super::{World, WorldScope};
    use super::{JoinRefusal, Joined};
    use crate::facts::abstract_domain::value::{Kind, Value};
    use crate::facts::sync_effects::local_operations::LocalBinding;
    use crate::facts::sync_effects::value_model::{AbstractValue, DenseArray, Values};
    use std::sync::Arc;
    use swc_atoms::Atom;
    use swc_common::SyntaxContext;
    use swc_ecma_ast::Id;

    fn binding(name: &str) -> Id {
        (Atom::from(name), SyntaxContext::empty())
    }

    fn object(allocation: u32) -> AbstractValue {
        AbstractValue::Value(Value::Alloc(Kind::Object, allocation))
    }

    fn world_with_prefix(name: &str, value: AbstractValue) -> World {
        let mut world = World::new(WorldScope::Application);
        world.module.prefix_values = PrefixValues::new(Arc::new(TopBindings::new([binding(name)])));
        world
            .module
            .prefix_values
            .insert(&binding(name), Values::from([value]));
        world.module.initialized_bindings.insert(binding(name));
        world
    }

    #[test]
    fn agreeing_alternatives_take_the_strict_path() {
        let world = world_with_prefix("a", object(1));
        let copy = world.clone();

        let Joined {
            world: joined,
            widened,
            refused,
        } = World::join(&[&world, &copy]);

        assert!(!widened);
        assert_eq!(refused, None);
        assert_eq!(joined, world);
    }

    #[test]
    fn divergent_values_widen_pointwise_and_keep_only_agreed_facts() {
        let mut left = world_with_prefix("a", object(1));
        let mut right = world_with_prefix("a", object(2));
        right.module.initialized_bindings.insert(binding("b"));
        left.arrays.dense.insert(
            7,
            DenseArray {
                slots: vec![Values::from([object(1)])],
            },
        );
        right.arrays.dense.insert(
            7,
            DenseArray {
                slots: vec![Values::from([object(2)])],
            },
        );
        left.arrays.dense.insert(
            8,
            DenseArray {
                slots: vec![Values::from([object(1)])],
            },
        );
        right.surface.tainted_objects.insert(9);

        let joined = World::join(&[&left, &right]);

        assert!(joined.widened);
        assert_eq!(joined.refused, None);
        assert_eq!(
            joined.world.module.prefix_values[&binding("a")],
            Values::from([object(1), object(2)])
        );
        assert!(
            !joined
                .world
                .module
                .initialized_bindings
                .contains(&binding("b"))
        );
        assert_eq!(
            joined.world.arrays.dense[&7].slots,
            vec![Values::from([object(1), object(2)])]
        );
        // An array present on one path only loses its exact layout.
        assert!(!joined.world.arrays.dense.contains_key(&8));
        assert!(joined.world.arrays.unknown_contents.contains(&8));
        assert!(joined.world.surface.unsettled_objects.contains(&8));
        assert!(joined.world.surface.tainted_objects.contains(&9));
        assert!(!joined.world.reachability.incomplete);
        assert!(joined.world.revision.prefix > left.revision.prefix);
    }

    #[test]
    fn empty_family_is_refused_without_panicking() {
        let joined = World::join(&[]);

        assert_eq!(joined.refused, Some(JoinRefusal::EmptySet));
        assert!(joined.widened);
        assert!(joined.world.reachability.incomplete);
    }

    #[test]
    fn divergent_active_cells_are_refused_and_the_rest_still_joins() {
        let mut left = world_with_prefix("a", object(1));
        let mut right = left.clone();
        let cell = right.cells.insert(LexicalCell {
            values: Values::from([object(3)]),
            kind: LocalBinding::Let,
            initialization: CellInitialization::Initialized,
        });
        right.active_cells.insert(binding("x"), cell);
        right
            .module
            .prefix_values
            .insert(&binding("a"), Values::from([object(2)]));
        left.publication.global_escaped_callbacks.insert(object(4));

        let joined = World::join(&[&left, &right]);

        assert_eq!(joined.refused, Some(JoinRefusal::DivergentActiveCells));
        assert!(joined.widened);
        assert!(joined.world.active_cells.is_empty());
        assert!(joined.world.reachability.incomplete);
        assert_eq!(
            joined.world.module.prefix_values[&binding("a")],
            Values::from([object(1), object(2)])
        );
        assert!(
            joined
                .world
                .publication
                .global_escaped_callbacks
                .contains(&object(4))
        );
        // A cell missing on one path may hold anything there.
        let joined_cell = joined.world.cells.slots[cell.0]
            .as_deref()
            .expect("cell survives the join");
        assert!(
            joined_cell
                .values
                .contains(&AbstractValue::Value(Value::Top))
        );
        assert_eq!(
            joined_cell.initialization,
            CellInitialization::MaybeInitialized
        );
    }

    #[test]
    fn divergent_scopes_are_refused_without_panicking() {
        let application = world_with_prefix("a", object(1));
        let mut provider = application.clone();
        provider.scope = WorldScope::IsolatedProvider;

        let joined = World::join(&[&application, &provider]);

        assert_eq!(joined.refused, Some(JoinRefusal::DivergentScope));
        assert_eq!(joined.world.scope, WorldScope::Application);
        assert!(joined.world.reachability.incomplete);
    }
}
