//! The per-prefix semantic world shared by application execution and provider
//! queries: the stores a body mutates in place through `&mut World`.
//!
//! Observations (effects, completion, proof dependencies) are not stored in
//! the `World`; the kernel returns them in its ledger and the adopter composes
//! them. Their types (`completion`, `effect_coverage`) and the allocation and
//! array transfers live in this crate because the world's stores use them.

pub mod allocation_transfer;
pub mod array_transfer;
pub mod cells;
pub mod completion;
pub mod descriptor_heap;
pub mod effect_coverage;
pub mod join;
pub mod object_surface;
pub mod prefix_values;

use crate::allocation_transfer::LiteralHeap;
use analysis_vocabulary::inputs::ProofDependencies;
use analysis_vocabulary::report_schema::FirstClassMembersPublication;
use cells::{CellTrace, LexicalCells};
use descriptor_heap::{DescriptorHeap, OwnDescriptorMapState};
use imbl::OrdMap;
use object_surface::ObjectSurfaceStore;
use prefix_values::PrefixValues;
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;
use value_model::{CallSite, Captured, Context, DenseArray, LexicalCellId, Values};
use value_model::{CallableSelection, WorldScope};

/// The program prefix's values, heap facts, reachability and identity-bearing
/// cells. One `World` serves both adapters with a different `scope`; the
/// strict provider's private heap is a `World` too.
#[derive(Debug, Clone, PartialEq)]
pub struct World {
    pub scope: WorldScope,
    /// Arena of instantiated lexical cells; ids are never reused.
    pub cells: LexicalCells,
    pub active_cells: OrdMap<Id, LexicalCellId>,
    pub module: ModuleBindings,
    pub surface: ObjectSurfaceStore,
    /// Captured own-descriptor maps by the call site that captured them.
    pub descriptors: OrdMap<CallSite, OwnDescriptorMapState>,
    pub arrays: DenseArrays,
    pub native: NativeRetained,
    pub classes: ClassState,
    pub publication: Publication,
    pub reachability: ReachabilityState,
    /// Conservative conditions under which the settled prefix's values hold,
    /// retained across owner/child boundaries. Prerequisites are not coverage.
    /// An isolated provider query has none.
    pub prerequisites: ProofDependencies,
    pub revision: Revision,
}

/// Module-level binding values of the settled prefix.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ModuleBindings {
    pub prefix_values: PrefixValues,
    /// Published only from an actual ordered initializer result. A flat
    /// projection's singleton value is not evidence of callable selection.
    pub prefix_callable_selections: OrdMap<Id, CallableSelection>,
    pub initialized_bindings: BTreeSet<Id>,
    pub top_env: Context,
    pub top_captures: Context,
}

/// Array contents by allocation site.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct DenseArrays {
    pub dense: OrdMap<u32, DenseArray>,
    pub primitive: OrdMap<u32, ProofDependencies>,
    /// A proven silent primitive push changed contents, not descriptors or prototypes.
    pub unknown_contents: BTreeSet<u32>,
    /// `reduce` results by call site allocation id.
    pub accumulated_values: OrdMap<u32, Values>,
    pub intrinsic_mutated: bool,
}

/// Native values the prefix retains or has tainted.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct NativeRetained {
    pub tainted_sets: BTreeSet<u32>,
    pub tainted_functions: BTreeSet<u32>,
    /// A revoked identity is a semantic root: equal descriptions must retain
    /// that identity even after other references disappear.
    pub revoked_audited_results: BTreeMap<usize, Captured<Values>>,
    pub uncertain_execution: bool,
}

/// Class definitions the prefix constructed, published or tainted.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ClassState {
    pub superclasses: OrdMap<u32, Values>,
    pub tainted: BTreeSet<u32>,
    pub escaped_capabilities: BTreeSet<u32>,
    /// First actual insertion of each class's members into the escaped pool.
    pub first_members_publications: BTreeMap<u32, FirstClassMembersPublication>,
    pub checked_constructor_inputs: BTreeSet<u32>,
    /// Owners executed while building a certified unpublished instance graph.
    /// Their synchronous effects still run; execution alone does not expose
    /// the wrapper or the classes' deferred callable members to the host.
    pub private_constructor_result_owners: BTreeSet<SourceUnitOrdinal>,
    /// Bindings whose completed initializer returned a checked fresh instance
    /// without publishing it. This is a binding certificate, not an identity
    /// claim about the abstract Instance value shared by other allocations.
    pub private_fresh_instance_bindings: BTreeSet<Id>,
}

/// What the prefix has exposed to the host or to later owners.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Publication {
    pub global_escaped_callbacks: Values,
    pub exported_runtime_values: Values,
    /// First top-level owner observed inserting each function callback into
    /// `global_escaped_callbacks` through `publish_values`.
    pub first_callback_publication_owners: BTreeMap<u32, SourceUnitOrdinal>,
    /// Returned callable source closures are static within one top-level
    /// initialization prefix. Incomplete reachability requires a fresh raw
    /// closure even when the precise prefix was already summarized.
    pub returned_callback_owners: BTreeSet<(bool, SourceUnitOrdinal)>,
    pub accepted_deferred_global_writes: BTreeSet<SourceUnitOrdinal>,
    /// Fresh object literals a parameter-mutation initializer filled with
    /// primitive data properties (`fresh_data_writes`).
    pub parameter_initialized_objects: BTreeSet<u32>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct ReachabilityState {
    pub unknown_call_seen: bool,
    pub incomplete: bool,
    pub expanded: BTreeSet<SourceUnitOrdinal>,
    pub deferred: BTreeMap<Id, SourceUnitOrdinal>,
    pub owners: BTreeSet<SourceUnitOrdinal>,
}

/// Monotone change counters; memo keys compare them instead of the stores.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Revision {
    pub prefix: u64,
    pub reachability: u64,
}

impl Revision {
    pub fn bump_prefix(&mut self) {
        self.prefix = self.prefix.wrapping_add(1);
    }

    pub fn bump_reachability(&mut self) {
        self.reachability = self.reachability.wrapping_add(1);
    }
}

impl World {
    pub fn new(scope: WorldScope) -> Self {
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
    pub fn literal_heap(&mut self) -> LiteralHeap<'_> {
        self.descriptor_heap().into_literal()
    }

    pub fn descriptor_heap(&mut self) -> DescriptorHeap<'_> {
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
    pub fn object_properties_are_settled(&self, object: u32) -> bool {
        self.surface.object_accessors_are_settled(object)
            && !self.arrays.unknown_contents.contains(&object)
            && !self.arrays.primitive.contains_key(&object)
    }

    /// Every value the world keeps reachable, as roots of a cell trace. Keep
    /// all value-bearing fields in this list when extending `World`.
    pub fn trace_roots<'a>(&'a self, trace: &mut CellTrace<'a>) {
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
                    value_model::property_inference::CapturedPropertyDescriptor::Data(values) => {
                        trace.values(values)
                    }
                    value_model::property_inference::CapturedPropertyDescriptor::Accessor {
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
