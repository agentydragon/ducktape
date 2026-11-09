//! The per-prefix semantic world shared by application execution and provider
//! queries: the stores a body mutates in place through `&mut World`.
//!
//! Observations (effects, completion, proof dependencies) are not here; they
//! are returned by the kernel in its ledger and composed by the adopter.

pub(super) mod cells;
pub(super) mod descriptor_heap;
pub(super) mod join;
pub(super) mod object_surface;

use super::allocation_transfer::LiteralHeap;
use super::kernel::frame::CallableSelection;
use super::value_model::{CallSite, Captured, Context, DenseArray, LexicalCellId, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::reports::schema::FirstClassMembersPublication;
use cells::{CellTrace, LexicalCells};
use descriptor_heap::{DescriptorHeap, OwnDescriptorMapState};
use imbl::OrdMap;
use object_surface::ObjectSurfaceStore;
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;

/// Who owns this world. The scope decides heap policy as a bundle: whether
/// literal writability is followed and whether a body's heap cells reach a
/// caller's effects.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) enum WorldScope {
    /// The chunk's own prefix: literal writability is tracked and heap cells
    /// become the owner's effects.
    Application,
    /// A fresh heap for one provider query: no writability proof, and heap
    /// cells stay private to the query.
    IsolatedProvider,
}

/// The program prefix's values, heap facts, reachability and identity-bearing
/// cells. One `World` serves both adapters with a different `scope`; the
/// strict provider's private heap is a `World` too.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct World {
    pub(super) scope: WorldScope,
    /// Arena of instantiated lexical cells; ids are never reused.
    pub(super) cells: LexicalCells,
    pub(super) active_cells: OrdMap<Id, LexicalCellId>,
    pub(super) module: ModuleBindings,
    pub(super) surface: ObjectSurfaceStore,
    /// Captured own-descriptor maps by the call site that captured them.
    pub(super) descriptors: OrdMap<CallSite, OwnDescriptorMapState>,
    pub(super) arrays: DenseArrays,
    pub(super) native: NativeRetained,
    pub(super) classes: ClassState,
    pub(super) publication: Publication,
    pub(super) reachability: ReachabilityState,
    /// Conservative conditions under which the settled prefix's values hold,
    /// retained across owner/child boundaries. Prerequisites are not coverage.
    /// An isolated provider query has none.
    pub(super) prerequisites: ProofDependencies,
    pub(super) revision: Revision,
}

/// Module-level binding values of the settled prefix.
#[derive(Debug, Clone, Default, PartialEq)]
pub(super) struct ModuleBindings {
    pub(super) prefix_values: Context,
    /// Published only from an actual ordered initializer result. A flat
    /// projection's singleton value is not evidence of callable selection.
    pub(super) prefix_callable_selections: OrdMap<Id, CallableSelection>,
    pub(super) initialized_bindings: BTreeSet<Id>,
    pub(super) top_env: Context,
    pub(super) top_captures: Context,
}

/// Array contents by allocation site.
#[derive(Debug, Clone, Default, PartialEq)]
pub(super) struct DenseArrays {
    pub(super) dense: OrdMap<u32, DenseArray>,
    pub(super) primitive: OrdMap<u32, ProofDependencies>,
    /// A proven silent primitive push changed contents, not descriptors or prototypes.
    pub(super) unknown_contents: BTreeSet<u32>,
    /// `reduce` results by call site allocation id.
    pub(super) accumulated_values: OrdMap<u32, Values>,
    pub(super) intrinsic_mutated: bool,
}

/// Native values the prefix retains or has tainted.
#[derive(Debug, Clone, Default, PartialEq)]
pub(super) struct NativeRetained {
    pub(super) tainted_sets: BTreeSet<u32>,
    pub(super) tainted_functions: BTreeSet<u32>,
    /// A revoked identity is a semantic root: equal descriptions must retain
    /// that identity even after other references disappear.
    pub(super) revoked_audited_results: BTreeMap<usize, Captured<Values>>,
    pub(super) uncertain_execution: bool,
}

/// Class definitions the prefix constructed, published or tainted.
#[derive(Debug, Clone, Default, PartialEq)]
pub(super) struct ClassState {
    pub(super) superclasses: OrdMap<u32, Values>,
    pub(super) tainted: BTreeSet<u32>,
    pub(super) escaped_capabilities: BTreeSet<u32>,
    /// First actual insertion of each class's members into the escaped pool.
    pub(super) first_members_publications: BTreeMap<u32, FirstClassMembersPublication>,
    pub(super) checked_constructor_inputs: BTreeSet<u32>,
    /// Owners executed while building a certified unpublished instance graph.
    /// Their synchronous effects still run; execution alone does not expose
    /// the wrapper or the classes' deferred callable members to the host.
    pub(super) private_constructor_result_owners: BTreeSet<SourceUnitOrdinal>,
}

/// What the prefix has exposed to the host or to later owners.
#[derive(Debug, Clone, Default, PartialEq)]
pub(super) struct Publication {
    pub(super) global_escaped_callbacks: Values,
    pub(super) exported_runtime_values: Values,
    /// First top-level owner observed inserting each function callback into
    /// `global_escaped_callbacks` through `publish_values`.
    pub(super) first_callback_publication_owners: BTreeMap<u32, SourceUnitOrdinal>,
    /// Returned callable source closures are static within one top-level
    /// initialization prefix. Incomplete reachability requires a fresh raw
    /// closure even when the precise prefix was already summarized.
    pub(super) returned_callback_owners: BTreeSet<(bool, SourceUnitOrdinal)>,
    pub(super) accepted_deferred_global_writes: BTreeSet<SourceUnitOrdinal>,
    /// Fresh object literals a parameter-mutation initializer filled with
    /// primitive data properties (`fresh_data_writes`).
    pub(super) parameter_initialized_objects: BTreeSet<u32>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub(super) struct ReachabilityState {
    pub(super) unknown_call_seen: bool,
    pub(super) incomplete: bool,
    pub(super) expanded: BTreeSet<SourceUnitOrdinal>,
    pub(super) deferred: BTreeMap<Id, SourceUnitOrdinal>,
    pub(super) owners: BTreeSet<SourceUnitOrdinal>,
}

/// Monotone change counters; memo keys compare them instead of the stores.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(super) struct Revision {
    pub(super) prefix: u64,
    pub(super) reachability: u64,
}

impl Revision {
    pub(super) fn bump_prefix(&mut self) {
        self.prefix = self.prefix.wrapping_add(1);
    }

    pub(super) fn bump_reachability(&mut self) {
        self.reachability = self.reachability.wrapping_add(1);
    }
}

impl World {
    pub(super) fn new(scope: WorldScope) -> Self {
        Self {
            scope,
            cells: LexicalCells::default(),
            active_cells: OrdMap::new(),
            module: ModuleBindings::default(),
            surface: ObjectSurfaceStore::default(),
            descriptors: OrdMap::new(),
            arrays: DenseArrays::default(),
            native: NativeRetained::default(),
            classes: ClassState::default(),
            publication: Publication::default(),
            reachability: ReachabilityState::default(),
            prerequisites: ProofDependencies::default(),
            revision: Revision::default(),
        }
    }

    /// The literal-allocation view: only the application scope follows a
    /// literal's writability; a provider query never proves a normally
    /// completing write.
    pub(super) fn literal_heap(&mut self) -> LiteralHeap<'_> {
        self.descriptor_heap().into_literal()
    }

    pub(super) fn descriptor_heap(&mut self) -> DescriptorHeap<'_> {
        DescriptorHeap {
            maps: &mut self.descriptors,
            surface: &mut self.surface,
            arrays: &mut self.arrays.dense,
            primitive_arrays: &mut self.arrays.primitive,
            revision: &mut self.revision.prefix,
            track_writable_prefix: self.scope == WorldScope::Application,
        }
    }

    /// Skipped local code can change descriptors or prototypes even when no
    /// escape to foreign code was observed. Neither set permits prefix reads.
    pub(super) fn object_properties_are_settled(&self, object: u32) -> bool {
        self.surface.object_accessors_are_settled(object)
            && !self.arrays.unknown_contents.contains(&object)
            && !self.arrays.primitive.contains_key(&object)
    }

    /// Every value the world keeps reachable, as roots of a cell trace. Keep
    /// all value-bearing fields in this list when extending `World`.
    pub(super) fn trace_roots<'a>(&'a self, trace: &mut CellTrace<'a>) {
        let World {
            scope: _,
            cells: _,
            active_cells,
            module,
            surface,
            descriptors,
            arrays,
            native,
            classes,
            publication,
            reachability: _,
            prerequisites: _,
            revision: _,
        } = self;
        for values in module
            .prefix_values
            .values()
            .chain(module.top_env.values())
            .chain(module.top_captures.values())
            .chain(classes.superclasses.values())
            .chain(arrays.accumulated_values.values())
        {
            trace.values(values);
        }
        trace.values(&publication.global_escaped_callbacks);
        trace.values(&publication.exported_runtime_values);
        for properties in surface.prefix_object_properties.values() {
            for values in properties.values() {
                trace.values(values);
            }
        }
        for array in arrays.dense.values() {
            for values in &array.slots {
                trace.values(values);
            }
        }
        for properties in surface.applied_accessors.values() {
            for accessor in properties.values() {
                trace.values(&accessor.getter);
                trace.values(&accessor.setter);
            }
        }
        for descriptors in descriptors.values() {
            trace.values(&descriptors.retained_values);
            for descriptor in descriptors.properties.values() {
                match descriptor {
                    super::property_inference::CapturedPropertyDescriptor::Data(values) => {
                        trace.values(values)
                    }
                    super::property_inference::CapturedPropertyDescriptor::Accessor {
                        getter,
                        setter,
                    } => {
                        trace.values(getter);
                        trace.values(setter);
                    }
                }
            }
        }
        for captured in native.revoked_audited_results.values() {
            trace.retained(captured);
        }
        for cell in active_cells.values() {
            trace.cell(*cell);
        }
    }
}
