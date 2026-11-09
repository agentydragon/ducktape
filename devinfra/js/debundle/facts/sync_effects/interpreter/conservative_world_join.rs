//! Conservative executable-state joining after alternative callbacks have run.
//!
//! This join never certifies a property, descriptor, normal-completion, or
//! publication fact. It weakly joins semantic state and separately reports
//! whether the live control cursor can resume or needs a remainder fallback.

use super::super::runtime_inference::AlternativeBranchId;
use super::transfer_alternatives::TransferJoinRefusal;
use super::{
    CallbackFallbackSummary, CellInitialization, ExecutionState, LexicalCell, LexicalCellStore,
    LexicalCells, ReachabilityState, SemanticWorld, SemanticWorldData, unknown,
};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::LexicalCellId;
use crate::facts::abstract_domain::value::HeapAddress;
use crate::facts::evaluation_effects::EvaluationEffects;
use crate::facts::sync_effects::Completion;
use crate::facts::sync_effects::allocation_transfer::WritablePrefixState;
use crate::facts::sync_effects::descriptor_transfer::{AppliedAccessor, OwnDescriptorMapState};
use crate::facts::sync_effects::native_operation::NativeRetainedReferences;
use crate::facts::sync_effects::object_surface::OwnPropertyCertificate;
use crate::facts::sync_effects::property_inference::{
    CapturedPropertyDescriptor, DescriptorAttributes,
};
use crate::facts::sync_effects::value_model::{
    CallSite, Captured, Context, DenseArray, OwnPropertyShape, Values,
};
use imbl::OrdMap;
use source_identity::SourceUnitOrdinal;
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_ecma_ast::Id;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ConservativeWorldJoinDisposition {
    /// The semantic world was joined soundly and the active control cursor is
    /// compatible. Completion remains a separate decision for the caller.
    Resume,
    /// A live continuation/control value could not be represented by the
    /// joined state. The caller must run one conservative remainder fallback.
    RequiresRemainderFallback,
}

pub(super) struct ConservativeWorldJoin {
    pub(super) state: ExecutionState,
    pub(super) disposition: ConservativeWorldJoinDisposition,
}

pub(super) fn conservative_join_execution_states(
    states: &[&ExecutionState],
    parent_lineage: &[AlternativeBranchId],
) -> Result<ConservativeWorldJoin, TransferJoinRefusal> {
    let Some(first) = states.first().copied() else {
        return Err(TransferJoinRefusal::EmptyAlternativeSet);
    };
    // Keep the schema exhaustive: a new state field requires an explicit
    // join policy here and in the assignments below. Never inherit a new field
    // from one branch silently.
    let ExecutionState {
        world: _,
        branch_lineage: _,
        ledger_dependencies: _,
        native_integrity_checks: _,
        native_prototype_property_checks: _,
        native_prefix_observations: _,
        native_point_effects: _,
        activated_native_effects: _,
        author_host_contract_uses: _,
        native_integrity_uses: _,
        native_operation_input_uses: _,
        imported_primitive_array_spread_input_uses: _,
        proof_dependencies: _,
        native_initializer_candidate: _,
        native_initializer_transfer: _,
        inference_uses: _,
        class_member_effects: _,
        active_class_member_promotions: _,
        prior_reachability_roots: _,
        current_top_owner: _,
        proof_uses: _,
        returned_record_proof_uses: _,
        record_factory_prototype_premises: _,
        checking_record_factory_contract: _,
        prior_effect_owners: _,
        statement_executed_owners: _,
        statement_invoked_callables: _,
        selected_array_call_results: _,
        array_iteration_depth: _,
        array_iteration_protected_objects: _,
        array_iteration_unsupported: _,
        invocation_depth: _,
        invocation_budget: _,
        callback_fallback: _,
        fallback_outcome: _,
        assumption: _,
        heap_access_origin: _,
        fallback_callback_sizes: _,
        fallback_local_owners: _,
        fallback_local_deferred: _,
        capture_identities: _,
        capture_contexts: _,
        active_cell_scopes: _,
        callback_sets: _,
        provider_private_graphs: _,
        provider_closures: _,
        active: _,
        invocation_memos: _,
    } = first;
    let mut branches = BTreeSet::new();
    for state in states {
        if state.branch_lineage.len() <= parent_lineage.len()
            || !state.branch_lineage.starts_with(parent_lineage)
        {
            return Err(TransferJoinRefusal::WrongChildLineage);
        }
        if !branches.insert(state.branch_lineage.last().copied().unwrap()) {
            return Err(TransferJoinRefusal::DuplicateBranchIdentity);
        }
        if state.current_top_owner != first.current_top_owner || !same_run_arenas(first, state) {
            return Err(TransferJoinRefusal::IncompatibleExecutionState);
        }
    }
    for (index, state) in states.iter().enumerate() {
        if states[index + 1..].iter().any(|other| {
            state.branch_lineage.starts_with(&other.branch_lineage)
                || other.branch_lineage.starts_with(&state.branch_lineage)
        }) {
            return Err(TransferJoinRefusal::DuplicateBranchIdentity);
        }
    }

    let mut state = first.clone();
    state.branch_lineage = parent_lineage.to_vec();
    let worlds = states
        .iter()
        .map(|state| state.world.get())
        .collect::<Vec<_>>();
    let same_world = states
        .iter()
        .all(|state| state.world.same_contents(&first.world));
    let controls_match = states.iter().all(|state| {
        state.invocation_depth == first.invocation_depth
            && state.array_iteration_depth == first.array_iteration_depth
            && state.active_cell_scopes == first.active_cell_scopes
            && state.active == first.active
            && state.active_class_member_promotions == first.active_class_member_promotions
            && state.array_iteration_protected_objects == first.array_iteration_protected_objects
            && state.array_iteration_unsupported == first.array_iteration_unsupported
            && state.current_top_owner == first.current_top_owner
            && state.assumption == first.assumption
            && state.heap_access_origin == first.heap_access_origin
            && state.checking_record_factory_contract == first.checking_record_factory_contract
            && state.fallback_callback_sizes == first.fallback_callback_sizes
            && state.native_initializer_candidate == first.native_initializer_candidate
            && state.native_initializer_transfer == first.native_initializer_transfer
    });
    let world_controls_match = worlds.iter().all(|world| {
        world.active_construction == worlds[0].active_construction
            && world.active_cells == worlds[0].active_cells
    });
    let mut requires_fallback = !(controls_match && world_controls_match);

    let fallback_outcomes = states
        .iter()
        .map(|state| &state.fallback_outcome)
        .collect::<Vec<_>>();
    let evaluation_defaults = vec![EvaluationEffects::default(); states.len()];
    let evaluation_refs = evaluation_defaults.iter().collect::<Vec<_>>();
    state.fallback_outcome =
        super::transfer_alternatives::join_outcomes(&fallback_outcomes, &evaluation_refs);

    let (mut world, _) = join_worlds(&worlds, first, same_world);
    state.world = SemanticWorld(Arc::new(world.clone()));

    state.ledger_dependencies = ProofDependencies::default();
    for branch in states {
        state
            .ledger_dependencies
            .extend(&branch.ledger_dependencies);
    }
    state.native_integrity_checks = RefCell::new(intersect_sets(
        &states
            .iter()
            .map(|state| state.native_integrity_checks.borrow().clone())
            .collect::<Vec<_>>(),
    ));
    state.native_prototype_property_checks = RefCell::new(intersect_maps(
        &states
            .iter()
            .map(|state| state.native_prototype_property_checks.borrow().clone())
            .collect::<Vec<_>>(),
    ));
    state.native_prefix_observations = RefCell::new(union_vectors(
        &states
            .iter()
            .map(|state| state.native_prefix_observations.borrow().clone())
            .collect::<Vec<_>>(),
    ));
    state.native_point_effects = super::NativePointEffects::join_alternatives(
        &states
            .iter()
            .map(|state| &state.native_point_effects)
            .collect::<Vec<_>>(),
    );
    state.activated_native_effects = union_sets(
        &states
            .iter()
            .map(|state| state.activated_native_effects.clone())
            .collect::<Vec<_>>(),
    );
    state.author_host_contract_uses = union_vectors(
        &states
            .iter()
            .map(|state| state.author_host_contract_uses.clone())
            .collect::<Vec<_>>(),
    );
    state.native_integrity_uses = RefCell::new(union_sets(
        &states
            .iter()
            .map(|state| state.native_integrity_uses.borrow().clone())
            .collect::<Vec<_>>(),
    ));
    state.native_operation_input_uses = RefCell::new(intersect_maps(
        &states
            .iter()
            .map(|state| state.native_operation_input_uses.borrow().clone())
            .collect::<Vec<_>>(),
    ));
    state.imported_primitive_array_spread_input_uses = RefCell::new(union_vectors(
        &states
            .iter()
            .map(|state| {
                state
                    .imported_primitive_array_spread_input_uses
                    .borrow()
                    .clone()
            })
            .collect::<Vec<_>>(),
    ));
    let mut proof_dependencies = ProofDependencies::default();
    for branch in states {
        proof_dependencies.extend(&branch.proof_dependencies.borrow());
    }
    state.proof_dependencies = RefCell::new(proof_dependencies);
    state.native_initializer_candidate =
        common_option(states, |state| state.native_initializer_candidate);
    state.native_initializer_transfer =
        common_option(states, |state| state.native_initializer_transfer.clone());
    state.inference_uses = RefCell::new(union_sets(
        &states
            .iter()
            .map(|state| state.inference_uses.borrow().clone())
            .collect::<Vec<_>>(),
    ));
    state.class_member_effects = join_class_member_effects(states);
    state.active_class_member_promotions = intersect_sets(
        &states
            .iter()
            .map(|state| state.active_class_member_promotions.clone())
            .collect::<Vec<_>>(),
    );
    // These are may-roots for a later unknown call, so union them when every
    // branch is still tracking a finite root set; `None` retains the existing
    // whole-prefix fallback meaning.
    state.prior_reachability_roots = if states
        .iter()
        .all(|branch| branch.prior_reachability_roots.is_some())
    {
        Some(union_sets(
            &states
                .iter()
                .filter_map(|branch| branch.prior_reachability_roots.clone())
                .collect::<Vec<_>>(),
        ))
    } else {
        None
    };
    state.proof_uses = states
        .iter()
        .flat_map(|state| state.proof_uses.iter().cloned())
        .collect();
    state.returned_record_proof_uses = states
        .iter()
        .flat_map(|state| state.returned_record_proof_uses.iter().cloned())
        .collect();
    state.record_factory_prototype_premises =
        RefCell::new(intersect_record_factory_premises(states));
    state.checking_record_factory_contract = first.checking_record_factory_contract;
    state.prior_effect_owners = union_sets(
        &states
            .iter()
            .map(|state| state.prior_effect_owners.clone())
            .collect::<Vec<_>>(),
    );
    state.statement_executed_owners = union_sets(
        &states
            .iter()
            .map(|state| state.statement_executed_owners.clone())
            .collect::<Vec<_>>(),
    );
    state.statement_invoked_callables = union_sets(
        &states
            .iter()
            .map(|state| state.statement_invoked_callables.clone())
            .collect::<Vec<_>>(),
    );
    state.selected_array_call_results =
        join_selected_array_call_results(states).unwrap_or_else(|| {
            requires_fallback = true;
            intersect_maps(
                &states
                    .iter()
                    .map(|state| state.selected_array_call_results.clone())
                    .collect::<Vec<_>>(),
            )
        });
    state.array_iteration_depth = states
        .iter()
        .map(|state| state.array_iteration_depth)
        .max()
        .unwrap_or_default();
    state.array_iteration_protected_objects = intersect_sets(
        &states
            .iter()
            .map(|state| state.array_iteration_protected_objects.clone())
            .collect::<Vec<_>>(),
    );
    state.array_iteration_unsupported =
        states.iter().any(|state| state.array_iteration_unsupported);
    state.invocation_depth = states
        .iter()
        .map(|state| state.invocation_depth)
        .max()
        .unwrap_or_default();
    state.invocation_budget = Arc::clone(&first.invocation_budget);
    state.callback_fallback = join_callback_fallback(states);
    state.assumption = states.iter().find_map(|state| state.assumption.clone());
    state.fallback_callback_sizes = if requires_fallback {
        None
    } else {
        first.fallback_callback_sizes
    };
    state.fallback_local_owners = intersect_sets(
        &states
            .iter()
            .map(|state| state.fallback_local_owners.clone())
            .collect::<Vec<_>>(),
    );
    state.fallback_local_deferred = union_maps(
        &states
            .iter()
            .map(|state| state.fallback_local_deferred.clone())
            .collect::<Vec<_>>(),
    );
    state.active = intersect_sets(
        &states
            .iter()
            .map(|state| state.active.clone())
            .collect::<Vec<_>>(),
    );
    state.invocation_memos = OrdMap::new();

    if requires_fallback {
        let mut retained = collect_world_values(&worlds);
        for branch in states {
            for (_, values) in branch.selected_array_call_results.values() {
                retained.extend(values.iter().cloned());
            }
        }
        retained.extend(world.fallback_retained_values.iter().cloned());
        retained.extend(state.fallback_outcome.reached.iter().cloned());
        if let Some(normal) = state.fallback_outcome.ledger.may_normal() {
            retained.extend(normal.values.iter().cloned());
        }
        if let Completion::Abrupt(thrown) = &state.fallback_outcome.ledger.completion {
            retained.extend(thrown.value.values.iter().cloned());
        }
        world.fallback_retained_values.extend(retained);
        world
            .fallback_retained_cells
            .extend(worlds.iter().flat_map(|world| {
                world
                    .lexical_cells
                    .store
                    .occupied
                    .iter()
                    .copied()
                    .chain(world.active_cells.values().copied())
            }));
        world.prefix_revision = world.prefix_revision.wrapping_add(1);
        state.world = SemanticWorld(Arc::new(world));
    }

    let disposition = if requires_fallback {
        // The joined remainder has no single executed-operation receipt.
        // Keep its effects as may-ordering evidence, but prevent operations
        // produced by the fallback remainder from becoming premise authority.
        state.heap_access_origin = crate::facts::HeapAccessOrigin::ConservativeSummary;
        ConservativeWorldJoinDisposition::RequiresRemainderFallback
    } else {
        ConservativeWorldJoinDisposition::Resume
    };
    Ok(ConservativeWorldJoin { state, disposition })
}

fn same_run_arenas(left: &ExecutionState, right: &ExecutionState) -> bool {
    Arc::ptr_eq(&left.invocation_budget, &right.invocation_budget)
        && Arc::ptr_eq(
            &left.world.get().lexical_cells.next_id,
            &right.world.get().lexical_cells.next_id,
        )
        && left
            .capture_identities
            .shares_arena(&right.capture_identities)
        && left.capture_contexts.shares_arena(&right.capture_contexts)
        && left.callback_sets.shares_arena(&right.callback_sets)
        && left
            .provider_private_graphs
            .shares_arena(&right.provider_private_graphs)
        && left
            .provider_closures
            .shares_arena(&right.provider_closures)
}

fn join_worlds(
    worlds: &[&SemanticWorldData],
    interning_state: &ExecutionState,
    same_world: bool,
) -> (SemanticWorldData, bool) {
    let first = worlds[0];
    // Keep the schema exhaustive: a new state field requires an explicit
    // join policy here and in the assignments below. Never inherit a new field
    // from one branch silently.
    let SemanticWorldData {
        uncertain_native_execution: _,
        class_superclasses: _,
        private_constructor_result_owners: _,
        checked_constructor_inputs: _,
        unknown_array_contents: _,
        parameter_initialized_objects: _,
        accepted_deferred_global_writes: _,
        frozen_namespace_calls: _,
        reachability: _,
        prefix_values: _,
        prefix_callable_selections: _,
        initialized_bindings: _,
        global_escaped_callbacks: _,
        fallback_retained_values: _,
        fallback_retained_cells: _,
        first_callback_publication_owners: _,
        first_class_members_publications: _,
        top_env: _,
        top_captures: _,
        object_surface: _,
        prefix_private_properties: _,
        prefix_native_retained_references: _,
        native_retained_references_may_be_unknown: _,
        active_construction: _,
        own_descriptor_maps: _,
        dense_arrays: _,
        primitive_arrays: _,
        accumulated_values: _,
        descriptor_copy_results: _,
        array_intrinsic_mutated: _,
        tainted_native_sets: _,
        tainted_functions: _,
        revoked_audited_results: _,
        tainted_classes: _,
        escaped_class_capabilities: _,
        exported_runtime_values: _,
        prefix_revision: _,
        returned_callback_owners: _,
        lexical_cells: _,
        active_cells: _,
    } = first;

    let mut joined = first.clone();
    let mut widened = !same_world;

    joined.uncertain_native_execution = worlds.iter().any(|world| world.uncertain_native_execution);
    joined.class_superclasses = join_value_maps(
        &worlds
            .iter()
            .map(|world| &world.class_superclasses)
            .collect::<Vec<_>>(),
        true,
    );
    // This set suppresses possible source-unit reads/effects for private
    // construction results; it is an all-path capability, never a may-set.
    joined.private_constructor_result_owners = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.private_constructor_result_owners.clone())
            .collect::<Vec<_>>(),
    );
    joined.checked_constructor_inputs = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.checked_constructor_inputs.clone())
            .collect::<Vec<_>>(),
    );
    joined.unknown_array_contents = union_sets(
        &worlds
            .iter()
            .map(|world| world.unknown_array_contents.clone())
            .collect::<Vec<_>>(),
    );
    joined.parameter_initialized_objects = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.parameter_initialized_objects.clone())
            .collect::<Vec<_>>(),
    );
    joined.accepted_deferred_global_writes = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.accepted_deferred_global_writes.clone())
            .collect::<Vec<_>>(),
    );
    joined.frozen_namespace_calls = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.frozen_namespace_calls.clone())
            .collect::<Vec<_>>(),
    );
    joined.reachability = join_reachability(worlds);
    joined.prefix_values = join_contexts(
        &worlds
            .iter()
            .map(|world| &world.prefix_values)
            .collect::<Vec<_>>(),
    );
    joined.prefix_callable_selections = intersect_ord_maps(
        &worlds
            .iter()
            .map(|world| world.prefix_callable_selections.clone())
            .collect::<Vec<_>>(),
    );
    joined.initialized_bindings = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.initialized_bindings.clone())
            .collect::<Vec<_>>(),
    );
    joined.global_escaped_callbacks = union_values(
        &worlds
            .iter()
            .map(|world| &world.global_escaped_callbacks)
            .collect::<Vec<_>>(),
    );
    joined.fallback_retained_values = union_values(
        &worlds
            .iter()
            .map(|world| &world.fallback_retained_values)
            .collect::<Vec<_>>(),
    );
    joined.first_callback_publication_owners = intersect_maps(
        &worlds
            .iter()
            .map(|world| world.first_callback_publication_owners.clone())
            .collect::<Vec<_>>(),
    );
    joined.first_class_members_publications = intersect_maps(
        &worlds
            .iter()
            .map(|world| world.first_class_members_publications.clone())
            .collect::<Vec<_>>(),
    );
    joined.top_env = join_contexts(
        &worlds
            .iter()
            .map(|world| &world.top_env)
            .collect::<Vec<_>>(),
    );
    joined.top_captures = join_contexts(
        &worlds
            .iter()
            .map(|world| &world.top_captures)
            .collect::<Vec<_>>(),
    );
    joined.prefix_object_properties = join_nested_property_maps(
        &worlds
            .iter()
            .map(|world| &world.prefix_object_properties)
            .collect::<Vec<_>>(),
    );
    joined.prefix_private_properties = join_nested_property_maps(
        &worlds
            .iter()
            .map(|world| &world.prefix_private_properties)
            .collect::<Vec<_>>(),
    );
    joined.active_construction =
        common_map_value(worlds.iter().map(|world| world.active_construction));
    joined.writable_prefix = join_writable_prefix(worlds);
    joined.own_descriptor_maps = join_descriptor_maps(worlds);
    joined.applied_accessors = join_applied_accessors(worlds);
    joined.object_surface.property_attributes = intersect_ord_maps(
        &worlds
            .iter()
            .map(|world| world.object_surface.property_attributes.clone())
            .collect::<Vec<_>>(),
    );
    joined.object_surface.prototypes = intersect_ord_maps(
        &worlds
            .iter()
            .map(|world| world.object_surface.prototypes.clone())
            .collect::<Vec<_>>(),
    );
    joined.object_surface.extensibility = intersect_ord_maps(
        &worlds
            .iter()
            .map(|world| world.object_surface.extensibility.clone())
            .collect::<Vec<_>>(),
    );
    joined.object_surface.spread_copy_history = union_sets(
        &worlds
            .iter()
            .map(|world| world.object_surface.spread_copy_history.clone())
            .collect::<Vec<_>>(),
    );
    joined.accumulated_values = join_value_maps(
        &worlds
            .iter()
            .map(|world| &world.accumulated_values)
            .collect::<Vec<_>>(),
        true,
    );
    joined.ordinary_spread_copies = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.ordinary_spread_copies.clone())
            .collect::<Vec<_>>(),
    );
    joined.copy_enumerability_unknown = union_sets(
        &worlds
            .iter()
            .map(|world| world.copy_enumerability_unknown.clone())
            .collect::<Vec<_>>(),
    );
    joined.spread_shapes = join_spread_shapes(worlds);
    joined.array_spread_shapes = join_array_spread_shapes(worlds);
    joined.object_surface.own_property_certificates = join_own_property_certificates(worlds);
    joined.descriptor_copy_results = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.descriptor_copy_results.clone())
            .collect::<Vec<_>>(),
    );
    joined.array_intrinsic_mutated = worlds.iter().any(|world| world.array_intrinsic_mutated);
    joined.tainted_objects = union_sets(
        &worlds
            .iter()
            .map(|world| world.tainted_objects.clone())
            .collect::<Vec<_>>(),
    );
    joined.unsettled_objects = union_sets(
        &worlds
            .iter()
            .map(|world| world.unsettled_objects.clone())
            .collect::<Vec<_>>(),
    );
    joined.prefix_native_retained_references = join_native_retained_references(worlds);
    joined.native_retained_references_may_be_unknown = worlds
        .iter()
        .any(|world| world.native_retained_references_may_be_unknown);
    joined.tainted_native_sets = union_sets(
        &worlds
            .iter()
            .map(|world| world.tainted_native_sets.clone())
            .collect::<Vec<_>>(),
    );
    joined.tainted_functions = union_sets(
        &worlds
            .iter()
            .map(|world| world.tainted_functions.clone())
            .collect::<Vec<_>>(),
    );
    joined.revoked_audited_results = join_revoked_results(worlds, interning_state);
    joined.tainted_classes = union_sets(
        &worlds
            .iter()
            .map(|world| world.tainted_classes.clone())
            .collect::<Vec<_>>(),
    );
    joined.escaped_class_capabilities = union_sets(
        &worlds
            .iter()
            .map(|world| world.escaped_class_capabilities.clone())
            .collect::<Vec<_>>(),
    );
    joined.exported_runtime_values = union_values(
        &worlds
            .iter()
            .map(|world| &world.exported_runtime_values)
            .collect::<Vec<_>>(),
    );
    joined.prefix_revision = worlds
        .iter()
        .map(|world| world.prefix_revision)
        .max()
        .unwrap_or_default();
    joined.returned_callback_owners = union_sets(
        &worlds
            .iter()
            .map(|world| world.returned_callback_owners.clone())
            .collect::<Vec<_>>(),
    );
    joined.lexical_cells = join_lexical_cells(worlds, &mut widened);
    joined.active_cells = intersect_ord_maps(
        &worlds
            .iter()
            .map(|world| world.active_cells.clone())
            .collect::<Vec<_>>(),
    );
    joined.fallback_retained_cells = union_sets(
        &worlds
            .iter()
            .map(|world| world.fallback_retained_cells.clone())
            .collect::<Vec<_>>(),
    );
    // Apply array-specific invalidations after the ordinary field joins so
    // their object-local loss flags cannot be overwritten by those joins.
    joined.dense_arrays = join_dense_arrays(worlds, &mut joined, &mut widened);
    let uncertain_spread_copies = joined
        .object_surface
        .spread_shapes
        .iter()
        .filter_map(|(object, shape)| shape.is_none().then_some(*object))
        .collect::<Vec<_>>();
    for object in uncertain_spread_copies {
        joined.object_surface.invalidate_surface(object);
    }
    let uncertain_array_spreads = joined
        .object_surface
        .array_spread_shapes
        .iter()
        .filter_map(|(object, shape)| shape.is_none().then_some(*object))
        .collect::<Vec<_>>();
    for object in uncertain_array_spreads {
        joined.object_surface.invalidate_surface(object);
        joined.unknown_array_contents.insert(object);
        joined.dense_arrays.remove(&object);
    }
    joined.primitive_arrays = join_primitive_array_markers(worlds, &mut joined);

    if worlds
        .iter()
        .skip(1)
        .any(|world| world.active_cells != first.active_cells)
    {
        widened = true;
    }
    if worlds.iter().skip(1).any(|world| {
        world.prefix_object_properties != first.prefix_object_properties
            || world.prefix_private_properties != first.prefix_private_properties
            || world.spread_shapes != first.spread_shapes
            || world.writable_prefix != first.writable_prefix
            || world.object_surface.property_attributes != first.object_surface.property_attributes
            || world.object_surface.prototypes != first.object_surface.prototypes
            || world.object_surface.extensibility != first.object_surface.extensibility
            || world.object_surface.spread_copy_history != first.object_surface.spread_copy_history
            || world.object_surface.own_property_certificates
                != first.object_surface.own_property_certificates
            || world.object_surface.array_spread_shapes != first.object_surface.array_spread_shapes
            || world.own_descriptor_maps.keys().collect::<Vec<_>>()
                != first.own_descriptor_maps.keys().collect::<Vec<_>>()
            || world.dense_arrays != first.dense_arrays
            || world.prefix_native_retained_references != first.prefix_native_retained_references
    }) {
        widened = true;
    }

    joined.reachability.revision = worlds
        .iter()
        .map(|world| world.reachability.revision)
        .max()
        .unwrap_or_default();
    joined.reachability.revision = joined
        .reachability
        .revision
        .wrapping_add(u64::from(widened));
    joined.prefix_revision = joined.prefix_revision.wrapping_add(u64::from(widened));
    (joined, widened)
}

fn join_reachability(worlds: &[&SemanticWorldData]) -> ReachabilityState {
    let mut joined = worlds[0].reachability.clone();
    joined.unknown_call_seen = worlds
        .iter()
        .any(|world| world.reachability.unknown_call_seen);
    joined.incomplete = worlds.iter().any(|world| world.reachability.incomplete);
    joined.revision = worlds
        .iter()
        .map(|world| world.reachability.revision)
        .max()
        .unwrap_or_default();
    joined.expanded = intersect_sets(
        &worlds
            .iter()
            .map(|world| world.reachability.expanded.clone())
            .collect::<Vec<_>>(),
    );
    joined.deferred = union_maps(
        &worlds
            .iter()
            .map(|world| world.reachability.deferred.clone())
            .collect::<Vec<_>>(),
    );
    joined.owners = union_sets(
        &worlds
            .iter()
            .map(|world| world.reachability.owners.clone())
            .collect::<Vec<_>>(),
    );
    joined
}

fn join_contexts(contexts: &[&Context]) -> Context {
    let maps = contexts
        .iter()
        .map(|context| (*context).clone())
        .collect::<Vec<_>>();
    join_value_maps(&maps.iter().collect::<Vec<_>>(), true)
}

fn join_value_maps<K: Ord + Clone>(
    maps: &[&OrdMap<K, Values>],
    top_missing: bool,
) -> OrdMap<K, Values> {
    let keys = maps
        .iter()
        .flat_map(|map| map.keys().cloned())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for key in keys {
        let mut values = Values::new();
        for map in maps {
            if let Some(branch_values) = map.get(&key) {
                values.extend(branch_values.iter().cloned());
            } else if top_missing {
                values.extend(unknown());
            }
        }
        joined.insert(key, values);
    }
    joined
}

fn join_nested_property_maps<K: Ord + Clone>(
    maps: &[&OrdMap<HeapAddress, BTreeMap<K, Values>>],
) -> OrdMap<HeapAddress, BTreeMap<K, Values>> {
    let objects = maps
        .iter()
        .flat_map(|map| map.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for object in objects {
        let keys = maps
            .iter()
            .filter_map(|map| map.get(&object))
            .flat_map(|properties| properties.keys().cloned())
            .collect::<BTreeSet<_>>();
        let mut properties = BTreeMap::new();
        for key in keys {
            let mut values = Values::new();
            for map in maps {
                if let Some(branch_values) =
                    map.get(&object).and_then(|properties| properties.get(&key))
                {
                    values.extend(branch_values.iter().cloned());
                } else {
                    values.extend(unknown());
                }
            }
            properties.insert(key, values);
        }
        joined.insert(object, properties);
    }
    joined
}

fn join_writable_prefix(worlds: &[&SemanticWorldData]) -> OrdMap<HeapAddress, WritablePrefixState> {
    let keys = worlds
        .iter()
        .flat_map(|world| world.writable_prefix.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for key in keys {
        let first = worlds[0].writable_prefix.get(&key);
        let same = first.is_some()
            && worlds
                .iter()
                .all(|world| world.writable_prefix.get(&key) == first);
        joined.insert(
            key,
            if same {
                first.unwrap().clone()
            } else {
                WritablePrefixState::Unknown
            },
        );
    }
    joined
}

fn join_spread_shapes(
    worlds: &[&SemanticWorldData],
) -> OrdMap<HeapAddress, Option<OwnPropertyShape>> {
    let keys = worlds
        .iter()
        .flat_map(|world| world.spread_shapes.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for key in keys {
        let first = worlds[0].spread_shapes.get(&key);
        if let Some(first) = first
            && worlds
                .iter()
                .all(|world| world.spread_shapes.get(&key) == Some(first))
        {
            joined.insert(key, first.clone());
            continue;
        }
        joined.insert(key, None);
    }
    joined
}

fn join_array_spread_shapes(
    worlds: &[&SemanticWorldData],
) -> OrdMap<HeapAddress, Option<BTreeSet<String>>> {
    let keys = worlds
        .iter()
        .flat_map(|world| world.object_surface.array_spread_shapes.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for key in keys {
        let first = worlds[0].object_surface.array_spread_shapes.get(&key);
        if let Some(first_shape) = first
            && worlds.iter().all(|world| {
                world.object_surface.array_spread_shapes.get(&key) == Some(first_shape)
            })
        {
            joined.insert(key, first_shape.clone());
        } else {
            joined.insert(key, None);
        }
    }
    joined
}

fn join_own_property_certificates(
    worlds: &[&SemanticWorldData],
) -> OrdMap<HeapAddress, OwnPropertyCertificate> {
    let keys = worlds
        .iter()
        .flat_map(|world| {
            world
                .object_surface
                .own_property_certificates
                .keys()
                .copied()
        })
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for key in keys {
        let certificates = worlds
            .iter()
            .map(|world| world.object_surface.own_property_certificates.get(&key))
            .collect::<Vec<_>>();
        let mut certificate = certificates[0].cloned().unwrap_or_default();
        certificate.must_present.retain(|property| {
            certificates.iter().all(|candidate| {
                candidate.is_some_and(|candidate| candidate.must_present.contains(property))
            })
        });
        certificate.exact_keys = certificate.exact_keys.filter(|keys| {
            certificates.iter().all(|candidate| {
                candidate.is_some_and(|candidate| candidate.exact_keys.as_ref() == Some(keys))
            })
        });
        certificate.layout = certificate.layout.filter(|layout| {
            let layout_keys = layout.keys();
            certificates.iter().all(|candidate| {
                candidate.is_some_and(|candidate| {
                    candidate.layout.as_ref() == Some(layout)
                        && candidate.exact_keys.as_ref() == Some(&layout_keys)
                        && candidate.must_present == layout_keys
                })
            })
        });
        certificate.dependencies = certificates
            .iter()
            .filter_map(|candidate| candidate.as_ref().map(|candidate| &candidate.dependencies))
            .fold(ProofDependencies::default(), |mut dependencies, next| {
                dependencies.extend(next);
                dependencies
            });
        joined.insert(key, certificate);
    }
    joined
}

fn join_descriptor_maps(
    worlds: &[&SemanticWorldData],
) -> OrdMap<HeapAddress, OwnDescriptorMapState> {
    let keys = worlds
        .iter()
        .flat_map(|world| world.own_descriptor_maps.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for key in keys {
        let present = worlds
            .iter()
            .filter_map(|world| world.own_descriptor_maps.get(&key))
            .collect::<Vec<_>>();
        let Some(first) = present.first().copied() else {
            continue;
        };
        if present.len() == worlds.len()
            && present
                .iter()
                .all(|candidate| descriptor_map_equal(candidate, first))
        {
            joined.insert(key, first.clone());
            continue;
        }
        let mut result = first.clone();
        result.valid = false;
        let mut retained = Values::new();
        let mut order = Vec::new();
        let mut properties = BTreeMap::<String, CapturedPropertyDescriptor>::new();
        let mut attributes = BTreeMap::<String, DescriptorAttributes>::new();
        for candidate in &present {
            retained.extend(candidate.retained_values.iter().cloned());
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
            }
            for descriptor in candidate.properties.values() {
                match descriptor {
                    CapturedPropertyDescriptor::Data(values) => {
                        retained.extend(values.iter().cloned())
                    }
                    CapturedPropertyDescriptor::Accessor { getter, setter } => {
                        retained.extend(getter.iter().cloned());
                        retained.extend(setter.iter().cloned());
                    }
                }
            }
            for (name, value) in &candidate.attributes {
                if present
                    .iter()
                    .all(|other| other.attributes.get(name) == Some(value))
                {
                    attributes.insert(name.clone(), *value);
                }
            }
        }
        result.property_order = order;
        result.properties = properties;
        result.attributes = attributes;
        result.retained_values.extend(retained);
        joined.insert(key, result);
    }
    joined
}

fn descriptor_map_equal(left: &OwnDescriptorMapState, right: &OwnDescriptorMapState) -> bool {
    left.source_object == right.source_object
        && left.valid == right.valid
        && left.property_order == right.property_order
        && left.properties == right.properties
        && left.attributes == right.attributes
        && left.retained_values == right.retained_values
}

fn join_applied_accessors(
    worlds: &[&SemanticWorldData],
) -> OrdMap<HeapAddress, BTreeMap<String, AppliedAccessor>> {
    let objects = worlds
        .iter()
        .flat_map(|world| world.applied_accessors.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for object in objects {
        let keys = worlds
            .iter()
            .filter_map(|world| world.applied_accessors.get(&object))
            .flat_map(|props| props.keys().cloned())
            .collect::<BTreeSet<_>>();
        let mut props = BTreeMap::new();
        for key in keys {
            let mut accessor = AppliedAccessor::default();
            for world in worlds {
                if let Some(value) = world
                    .applied_accessors
                    .get(&object)
                    .and_then(|props| props.get(&key))
                {
                    accessor.getter.extend(value.getter.iter().cloned());
                    accessor.setter.extend(value.setter.iter().cloned());
                } else {
                    accessor.getter.extend(unknown());
                    accessor.setter.extend(unknown());
                }
            }
            props.insert(key, accessor);
        }
        joined.insert(object, props);
    }
    joined
}

fn join_dense_arrays(
    worlds: &[&SemanticWorldData],
    joined: &mut SemanticWorldData,
    widened: &mut bool,
) -> OrdMap<HeapAddress, DenseArray> {
    let keys = worlds
        .iter()
        .flat_map(|world| world.dense_arrays.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut arrays = OrdMap::new();
    for key in keys {
        let first = worlds[0].dense_arrays.get(&key);
        if let Some(first) = first
            && worlds
                .iter()
                .all(|world| world.dense_arrays.get(&key) == Some(first))
        {
            arrays.insert(key, first.clone());
            continue;
        }
        *widened = true;
        let present = worlds
            .iter()
            .filter_map(|world| world.dense_arrays.get(&key))
            .collect::<Vec<_>>();
        let first_len = present.first().map(|array| array.slots.len());
        if present.len() == worlds.len()
            && present
                .iter()
                .all(|array| Some(array.slots.len()) == first_len)
        {
            // DenseArray encodes an exact length. With equal lengths, the
            // per-index may-union remains a valid layout; only element
            // values differ, so keep independent descriptor/shape proofs.
            let mut slots = Vec::with_capacity(first_len.unwrap_or_default());
            for index in 0..first_len.unwrap_or_default() {
                let mut values = Values::new();
                for array in &present {
                    values.extend(array.slots[index].iter().cloned());
                }
                slots.push(values);
            }
            arrays.insert(key, DenseArray { slots });
        } else {
            // Missing arrays or differing lengths cannot be represented by
            // the exact DenseArray carrier. Spill all element payloads to
            // the fallback roots before dropping the precise layout.
            for array in present {
                joined
                    .fallback_retained_values
                    .extend(array.slots.iter().flatten().cloned());
            }
            joined.unknown_array_contents.insert(key);
            joined.object_surface.invalidate_surface(key);
        }
    }
    arrays
}

fn join_primitive_array_markers(
    worlds: &[&SemanticWorldData],
    joined: &mut SemanticWorldData,
) -> OrdMap<HeapAddress, ProofDependencies> {
    let keys = worlds
        .iter()
        .flat_map(|world| world.primitive_arrays.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut markers = OrdMap::new();
    for key in keys {
        let first = worlds[0].primitive_arrays.get(&key);
        if let Some(first) = first
            && worlds
                .iter()
                .all(|world| world.primitive_arrays.get(&key) == Some(first))
        {
            markers.insert(key, first.clone());
            continue;
        }
        joined.unknown_array_contents.insert(key);
        joined.object_surface.invalidate_surface(key);
    }
    markers
}

fn join_selected_array_call_results(
    states: &[&ExecutionState],
) -> Option<BTreeMap<CallSite, (SourceUnitOrdinal, Values)>> {
    let keys = states
        .iter()
        .flat_map(|state| state.selected_array_call_results.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = BTreeMap::new();
    for key in keys {
        let present = states
            .iter()
            .filter_map(|state| state.selected_array_call_results.get(&key))
            .collect::<Vec<_>>();
        let Some((owner, _)) = present.first() else {
            continue;
        };
        if present.len() != states.len()
            || present.iter().any(|(other_owner, _)| other_owner != owner)
        {
            // Presence means this source operation already ran and its result
            // is consumed by the flat compatibility path. Never synthesize
            // that execution authority from a may-value/Top join.
            return None;
        }
        let mut values = Values::new();
        for (_, branch_values) in &present {
            values.extend(branch_values.iter().cloned());
        }
        joined.insert(key, (*owner, values));
    }
    Some(joined)
}

fn join_native_retained_references(
    worlds: &[&SemanticWorldData],
) -> OrdMap<HeapAddress, NativeRetainedReferences> {
    let keys = worlds
        .iter()
        .flat_map(|world| world.prefix_native_retained_references.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = OrdMap::new();
    for key in keys {
        let mut retained = NativeRetainedReferences::default();
        let mut present = 0;
        for world in worlds {
            if let Some(other) = world.prefix_native_retained_references.get(&key) {
                retained.weak_union(other);
                present += 1;
            }
        }
        if present != worlds.len() {
            retained.values.extend(unknown());
            retained.may_contain_unknown = true;
        }
        joined.insert(key, retained);
    }
    joined
}

fn join_revoked_results(
    worlds: &[&SemanticWorldData],
    interning_state: &ExecutionState,
) -> BTreeMap<usize, Captured<Values>> {
    let keys = worlds
        .iter()
        .flat_map(|world| world.revoked_audited_results.keys().copied())
        .collect::<BTreeSet<_>>();
    let mut joined = BTreeMap::new();
    for key in keys {
        let captures = worlds
            .iter()
            .filter_map(|world| world.revoked_audited_results.get(&key))
            .collect::<Vec<_>>();
        let mut values = Values::new();
        for captured in &captures {
            values.extend(captured.values.iter().cloned());
        }
        let first = captures
            .first()
            .expect("revoked result key came from at least one world");
        let captured = if captures
            .iter()
            .all(|candidate| candidate.values == first.values)
        {
            (**first).clone()
        } else {
            // A revoked-map key is the original capture identity. Its payload
            // must still obey the run's canonical capture interning rules.
            // Never reuse that identity for a widened value set.
            interning_state
                .callback_sets
                .intern(&interning_state.capture_identities, values)
        };
        joined.insert(key, captured);
    }
    joined
}

fn join_lexical_cells(worlds: &[&SemanticWorldData], widened: &mut bool) -> LexicalCells {
    let mut cells = BTreeMap::<usize, Vec<Option<&LexicalCell>>>::new();
    for (index, world) in worlds.iter().enumerate() {
        for id in &world.lexical_cells.store.occupied {
            let entry = cells
                .entry(id.0)
                .or_insert_with(|| vec![None; worlds.len()]);
            entry[index] = world
                .lexical_cells
                .store
                .slots
                .get(id.0)
                .and_then(Option::as_deref);
        }
    }
    let max_len = worlds
        .iter()
        .map(|world| world.lexical_cells.store.slots.len())
        .max()
        .unwrap_or_default();
    let mut store = LexicalCellStore {
        slots: (0..max_len).map(|_| None).collect(),
        occupied: Vec::new(),
        next_collection_at: worlds
            .iter()
            .map(|world| world.lexical_cells.store.next_collection_at)
            .max()
            .unwrap_or(1),
    };
    for (id, branches) in cells {
        let present = branches.iter().flatten().copied().collect::<Vec<_>>();
        let Some(first) = present.first().copied() else {
            continue;
        };
        let mut cell = first.clone();
        cell.values = Values::new();
        let mut all_same = present.len() == worlds.len();
        let mut all_initialized = true;
        for branch in &present {
            cell.values.extend(branch.values.iter().cloned());
            if branch.values != first.values
                || branch.selection != first.selection
                || branch.initialization != first.initialization
                || branch.kind != first.kind
            {
                all_same = false;
            }
            all_initialized &= branch.initialization == CellInitialization::Initialized;
        }
        if present.len() != worlds.len() {
            cell.values.extend(unknown());
            all_same = false;
            all_initialized = false;
        }
        if !all_same {
            *widened = true;
        }
        cell.selection = present
            .iter()
            .all(|branch| branch.selection == first.selection)
            .then(|| first.selection.clone())
            .flatten();
        cell.initialization = if present
            .iter()
            .all(|branch| branch.initialization == first.initialization)
            && present.len() == worlds.len()
        {
            first.initialization
        } else if all_initialized {
            CellInitialization::Initialized
        } else {
            CellInitialization::MaybeInitialized
        };
        if present.iter().any(|branch| branch.kind != first.kind) {
            *widened = true;
        }
        store.slots[id] = Some(Box::new(cell));
        store.occupied.push(LexicalCellId(id));
    }
    let next_id = Arc::clone(&worlds[0].lexical_cells.next_id);
    LexicalCells {
        store: Arc::new(store),
        next_id,
    }
}

fn collect_world_values(worlds: &[&SemanticWorldData]) -> Values {
    let mut values = Values::new();
    for world in worlds {
        for context in [&world.prefix_values, &world.top_env, &world.top_captures] {
            values.extend(context.values().flatten().cloned());
        }
        values.extend(world.class_superclasses.values().flatten().cloned());
        values.extend(world.global_escaped_callbacks.iter().cloned());
        values.extend(world.fallback_retained_values.iter().cloned());
        values.extend(world.exported_runtime_values.iter().cloned());
        for properties in world.prefix_object_properties.values() {
            values.extend(properties.values().flatten().cloned());
        }
        for properties in world.prefix_private_properties.values() {
            values.extend(properties.values().flatten().cloned());
        }
        for array in world.dense_arrays.values() {
            values.extend(array.slots.iter().flatten().cloned());
        }
        for properties in world.applied_accessors.values() {
            for accessor in properties.values() {
                values.extend(accessor.getter.iter().cloned());
                values.extend(accessor.setter.iter().cloned());
            }
        }
        for descriptor_map in world.own_descriptor_maps.values() {
            values.extend(descriptor_map.retained_values.iter().cloned());
            for descriptor in descriptor_map.properties.values() {
                match descriptor {
                    CapturedPropertyDescriptor::Data(data) => values.extend(data.iter().cloned()),
                    CapturedPropertyDescriptor::Accessor { getter, setter } => {
                        values.extend(getter.iter().cloned());
                        values.extend(setter.iter().cloned());
                    }
                }
            }
        }
        for retained in world.prefix_native_retained_references.values() {
            values.extend(retained.values.iter().cloned());
        }
        values.extend(world.accumulated_values.values().flatten().cloned());
        for captured in world.revoked_audited_results.values() {
            values.extend(captured.values.iter().cloned());
        }
        for cell_id in &world.lexical_cells.store.occupied {
            if let Some(cell) = world
                .lexical_cells
                .store
                .slots
                .get(cell_id.0)
                .and_then(Option::as_deref)
            {
                values.extend(cell.values.iter().cloned());
            }
        }
    }
    values
}

fn union_values(values: &[&Values]) -> Values {
    values
        .iter()
        .flat_map(|values| values.iter().cloned())
        .collect()
}

fn union_sets<T: Ord + Clone>(sets: &[BTreeSet<T>]) -> BTreeSet<T> {
    sets.iter().flat_map(|set| set.iter().cloned()).collect()
}

fn union_maps<K: Ord + Clone, V: Clone>(maps: &[BTreeMap<K, V>]) -> BTreeMap<K, V> {
    let mut joined = BTreeMap::new();
    for map in maps {
        for (key, value) in map {
            joined.entry(key.clone()).or_insert_with(|| value.clone());
        }
    }
    joined
}

fn intersect_sets<T: Ord + Clone>(sets: &[BTreeSet<T>]) -> BTreeSet<T> {
    let Some(first) = sets.first() else {
        return BTreeSet::new();
    };
    first
        .iter()
        .filter(|value| sets.iter().all(|set| set.contains(*value)))
        .cloned()
        .collect()
}

fn intersect_maps<K: Ord + Clone, V: Clone + PartialEq>(maps: &[BTreeMap<K, V>]) -> BTreeMap<K, V> {
    let Some(first) = maps.first() else {
        return BTreeMap::new();
    };
    first
        .iter()
        .filter(|(key, value)| maps.iter().all(|map| map.get(*key) == Some(*value)))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect()
}

fn intersect_ord_maps<K: Ord + Clone, V: Clone + PartialEq>(maps: &[OrdMap<K, V>]) -> OrdMap<K, V> {
    let Some(first) = maps.first() else {
        return OrdMap::new();
    };
    first
        .iter()
        .filter(|(key, value)| maps.iter().all(|map| map.get(*key) == Some(*value)))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect()
}

fn common_map_value<T: Clone + PartialEq>(values: impl Iterator<Item = Option<T>>) -> Option<T> {
    let values = values.collect::<Vec<_>>();
    let first = values.first().cloned().flatten()?;
    values
        .iter()
        .all(|value| value.as_ref() == Some(&first))
        .then_some(first)
}

fn common_option<T: Clone + PartialEq>(
    states: &[&ExecutionState],
    get: impl Fn(&ExecutionState) -> Option<T>,
) -> Option<T> {
    common_map_value(states.iter().map(|state| get(state)))
}

fn union_vectors<T: Clone + PartialEq>(vectors: &[Vec<T>]) -> Vec<T> {
    let mut joined = Vec::new();
    for vector in vectors {
        for value in vector {
            if !joined.contains(value) {
                joined.push(value.clone());
            }
        }
    }
    joined
}

fn join_class_member_effects(
    states: &[&ExecutionState],
) -> BTreeMap<SourceUnitOrdinal, (BTreeSet<Id>, BTreeSet<Id>)> {
    let mut joined = BTreeMap::<SourceUnitOrdinal, (BTreeSet<Id>, BTreeSet<Id>)>::new();
    for state in states {
        for (owner, (reads, writes)) in &state.class_member_effects {
            let entry = joined.entry(*owner).or_default();
            entry.0.extend(reads.iter().cloned());
            entry.1.extend(writes.iter().cloned());
        }
    }
    joined
}

fn join_callback_fallback(states: &[&ExecutionState]) -> CallbackFallbackSummary {
    let mut joined = CallbackFallbackSummary {
        expanded: intersect_sets(
            &states
                .iter()
                .map(|state| state.callback_fallback.expanded.clone())
                .collect::<Vec<_>>(),
        ),
        incomplete_expanded: states
            .iter()
            .all(|state| state.callback_fallback.incomplete_expanded),
        deferred: union_maps(
            &states
                .iter()
                .map(|state| state.callback_fallback.deferred.clone())
                .collect::<Vec<_>>(),
        ),
        ..CallbackFallbackSummary::default()
    };
    for state in states {
        joined
            .reads
            .extend(state.callback_fallback.reads.iter().cloned());
        joined
            .rebinds
            .extend(state.callback_fallback.rebinds.iter().cloned());
        joined
            .read_additions
            .extend(state.callback_fallback.read_additions.iter().cloned());
        joined
            .rebind_additions
            .extend(state.callback_fallback.rebind_additions.iter().cloned());
    }
    joined.read_additions = union_vectors(&[joined.read_additions]);
    joined.rebind_additions = union_vectors(&[joined.rebind_additions]);
    joined
}

fn intersect_record_factory_premises(
    states: &[&ExecutionState],
) -> super::RecordFactoryPrototypePremises {
    let premises = states
        .iter()
        .map(|state| state.record_factory_prototype_premises.borrow().clone())
        .collect::<Vec<_>>();
    let Some(first) = premises.first() else {
        return super::RecordFactoryPrototypePremises::default();
    };
    let constructor_fields = first
        .constructor_fields
        .iter()
        .filter(|field| {
            premises
                .iter()
                .all(|other| other.constructor_fields.contains(*field))
        })
        .cloned()
        .collect();
    let missing_input_fields = first
        .missing_input_fields
        .iter()
        .filter(|field| {
            premises
                .iter()
                .all(|other| other.missing_input_fields.contains(*field))
        })
        .cloned()
        .collect();
    super::RecordFactoryPrototypePremises {
        constructor_fields,
        missing_input_fields,
    }
}

#[cfg(test)]
mod tests {
    use super::{ExecutionState, SemanticWorldData, join_worlds};
    use crate::analysis_inputs::{ProgramPremiseKey, ProofDependencies};
    use crate::facts::abstract_domain::value::{
        HeapModuleScope, Kind, LocalAnalysisScopeId, SourceHeapResolver, Value,
    };
    use crate::facts::sync_effects::allocation_transfer::WritablePrefixState;
    use crate::facts::sync_effects::native_operation::NativeRetainedReferences;
    use crate::facts::sync_effects::object_surface::{
        ExtensibilityKnowledge, OwnPropertyCertificate, PrototypeKnowledge,
    };
    use crate::facts::sync_effects::property_inference::DescriptorAttributes;
    use crate::facts::sync_effects::value_model::{
        AbstractValue, DenseArray, OwnPropertyDescriptorSyntax, OwnPropertyShape, Values,
    };
    use std::collections::{BTreeMap, BTreeSet};

    fn shape() -> Option<OwnPropertyShape> {
        Some(OwnPropertyShape {
            properties: BTreeMap::from([("0".to_string(), OwnPropertyDescriptorSyntax::Data)]),
            creation_order: vec!["0".to_string()],
        })
    }

    fn resolver() -> SourceHeapResolver {
        SourceHeapResolver::new(HeapModuleScope::LocalAnalysis(LocalAnalysisScopeId::fresh()))
    }

    #[test]
    fn equal_length_dense_arrays_union_values_without_losing_layout_facts() {
        let mut left = SemanticWorldData::default();
        let mut right = SemanticWorldData::default();
        let object = resolver().allocation(Kind::Object, 7);
        left.dense_arrays.insert(
            object,
            DenseArray {
                slots: vec![
                    Values::from([AbstractValue::CallbackFactory]),
                    Values::from([AbstractValue::UNDEFINED]),
                ],
            },
        );
        right.dense_arrays.insert(
            object,
            DenseArray {
                slots: vec![
                    Values::from([AbstractValue::InlineCallbackFactory]),
                    Values::from([AbstractValue::PRIMITIVE]),
                ],
            },
        );
        for world in [&mut left, &mut right] {
            world
                .writable_prefix
                .insert(object, WritablePrefixState::DenseArray { length: 2 });
            world.spread_shapes.insert(object, shape());
        }

        let executor_state = ExecutionState::default();
        let (joined, _) = join_worlds(&[&left, &right], &executor_state, false);

        let array = joined
            .dense_arrays
            .get(&object)
            .expect("same-length array retained");
        assert_eq!(array.slots.len(), 2);
        assert_eq!(
            array.slots[0],
            Values::from([
                AbstractValue::CallbackFactory,
                AbstractValue::InlineCallbackFactory,
            ])
        );
        assert_eq!(
            array.slots[1],
            Values::from([AbstractValue::UNDEFINED, AbstractValue::PRIMITIVE])
        );
        assert_eq!(
            joined.writable_prefix.get(&object),
            Some(&WritablePrefixState::DenseArray { length: 2 })
        );
        assert_eq!(joined.spread_shapes.get(&object), Some(&shape()));
        assert!(!joined.unknown_array_contents.contains(&object));
        assert!(!joined.copy_enumerability_unknown.contains(&object));
        assert!(!joined.tainted_objects.contains(&object));
        assert!(!joined.unsettled_objects.contains(&object));
    }

    #[test]
    fn differing_dense_array_lengths_or_presence_spill_payloads_without_fabricating_layout() {
        let mut two_slots = SemanticWorldData::default();
        let mut one_slot = SemanticWorldData::default();
        let absent = SemanticWorldData::default();
        let object = resolver().allocation(Kind::Object, 9);
        two_slots.dense_arrays.insert(
            object,
            DenseArray {
                slots: vec![
                    Values::from([AbstractValue::CallbackFactory]),
                    Values::from([AbstractValue::UNDEFINED]),
                ],
            },
        );
        one_slot.dense_arrays.insert(
            object,
            DenseArray {
                slots: vec![Values::from([AbstractValue::InlineCallbackFactory])],
            },
        );

        let executor_state = ExecutionState::default();
        let (joined, _) = join_worlds(&[&two_slots, &one_slot, &absent], &executor_state, false);

        assert!(!joined.dense_arrays.contains_key(&object));
        assert!(
            joined
                .fallback_retained_values
                .contains(&AbstractValue::CallbackFactory)
        );
        assert!(
            joined
                .fallback_retained_values
                .contains(&AbstractValue::UNDEFINED)
        );
        assert!(
            joined
                .fallback_retained_values
                .contains(&AbstractValue::InlineCallbackFactory)
        );
        assert!(joined.unknown_array_contents.contains(&object));
        assert!(joined.copy_enumerability_unknown.contains(&object));
        assert_eq!(joined.spread_shapes.get(&object), Some(&None));
        assert_eq!(
            joined.writable_prefix.get(&object),
            Some(&WritablePrefixState::Unknown)
        );
    }

    #[test]
    fn differing_pooled_generation_shapes_keep_only_common_presence() {
        let mut left = SemanticWorldData::default();
        let mut right = SemanticWorldData::default();
        let object = resolver().allocation(Kind::Object, 23);
        let slot_attributes = DescriptorAttributes::literal(true);
        let left_keys = BTreeSet::from(["left".to_owned(), "slot".to_owned()]);
        let right_keys = BTreeSet::from(["right".to_owned(), "slot".to_owned()]);
        let left_dependencies = ProofDependencies {
            program_premises: BTreeSet::from([ProgramPremiseKey::ProcessPrototypeChainAccessors]),
            ..ProofDependencies::default()
        };
        let right_dependencies = ProofDependencies {
            program_premises: BTreeSet::from([ProgramPremiseKey::IntrinsicsNeverWritten]),
            ..ProofDependencies::default()
        };
        left.object_surface.own_property_certificates.insert(
            object,
            OwnPropertyCertificate {
                must_present: left_keys.clone(),
                exact_keys: Some(left_keys),
                layout: None,
                dependencies: left_dependencies.clone(),
            },
        );
        right.object_surface.own_property_certificates.insert(
            object,
            OwnPropertyCertificate {
                must_present: right_keys.clone(),
                exact_keys: Some(right_keys),
                layout: None,
                dependencies: right_dependencies.clone(),
            },
        );
        for world in [&mut left, &mut right] {
            world.object_surface.property_attributes.insert(
                object,
                BTreeMap::from([("slot".to_owned(), slot_attributes)]),
            );
            world
                .object_surface
                .prototypes
                .insert(object, PrototypeKnowledge::KnownNull);
            world
                .object_surface
                .extensibility
                .insert(object, ExtensibilityKnowledge::Extensible);
            world.writable_prefix.insert(
                object,
                WritablePrefixState::Proven(BTreeSet::from(["slot".to_owned()])),
            );
        }
        left.prefix_object_properties.insert(
            object,
            BTreeMap::from([(
                Some("slot".to_owned()),
                Values::from([AbstractValue::PRIMITIVE]),
            )]),
        );
        right.prefix_object_properties.insert(
            object,
            BTreeMap::from([(
                Some("slot".to_owned()),
                Values::from([AbstractValue::UNDEFINED]),
            )]),
        );

        let (joined, _) = join_worlds(&[&left, &right], &ExecutionState::default(), false);

        let certificate = &joined.object_surface.own_property_certificates[&object];
        assert_eq!(
            certificate.must_present,
            BTreeSet::from(["slot".to_owned()])
        );
        assert_eq!(certificate.exact_keys, None);
        assert_eq!(certificate.layout, None);
        assert_eq!(
            certificate.dependencies,
            ProofDependencies {
                program_premises: left_dependencies
                    .program_premises
                    .union(&right_dependencies.program_premises)
                    .copied()
                    .collect(),
                ..ProofDependencies::default()
            }
        );
        assert!(!joined.object_surface.tainted_objects.contains(&object));
        assert_eq!(
            joined.object_surface.property_attributes[&object]["slot"],
            slot_attributes
        );
        assert_eq!(
            joined.object_surface.own_property(object, "slot", true),
            crate::facts::sync_effects::object_surface::OwnPropertyKnowledge::Data {
                values: Values::from([AbstractValue::PRIMITIVE, AbstractValue::UNDEFINED]),
                attributes: slot_attributes,
            }
        );
        assert_eq!(
            joined.object_surface.prototype(object),
            PrototypeKnowledge::KnownNull
        );
        assert_eq!(
            joined.object_surface.extensibility(object),
            ExtensibilityKnowledge::Extensible
        );
        assert_eq!(
            joined.writable_prefix.get(&object),
            Some(&WritablePrefixState::Proven(BTreeSet::from([
                "slot".to_owned()
            ])))
        );
        let retained = &joined.prefix_object_properties[&object][&Some("slot".to_owned())];
        assert!(retained.contains(&AbstractValue::PRIMITIVE));
        assert!(retained.contains(&AbstractValue::UNDEFINED));
    }

    #[test]
    fn missing_native_retention_entry_is_path_unknown_not_world_wide_unknown() {
        let mut retained = SemanticWorldData::default();
        let missing = SemanticWorldData::default();
        let object = resolver().allocation(Kind::Array, 11);
        retained.prefix_native_retained_references.insert(
            object,
            NativeRetainedReferences {
                values: Values::from([AbstractValue::CallbackFactory]),
                may_contain_unknown: false,
            },
        );

        let executor_state = ExecutionState::default();
        let (joined, widened) = join_worlds(&[&retained, &missing], &executor_state, false);

        assert!(widened);
        let entry = joined
            .prefix_native_retained_references
            .get(&object)
            .expect("retained allocation remains represented");
        assert!(entry.values.contains(&AbstractValue::CallbackFactory));
        assert!(entry.values.contains(&AbstractValue::Value(Value::Top)));
        assert!(entry.may_contain_unknown);
        assert!(!joined.native_retained_references_may_be_unknown);
    }
}
