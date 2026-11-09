//! Owner operands: the source facts, prefix environment, proofs and the
//! selected ordered initializer an owner executes with. Preparatory
//! classifier proofs that read the prefix run here, before the body.

use super::super::execution_index::{Call, CallKind, object_own_data_read_property};
use super::super::interpreter::{
    CheckedLocalConstructorFactory, Executor, Outcome, ValidatedMetadataDecoration, unknown,
};
use super::super::local_operations::LocalBody;
use super::super::noninvoking_initialization::{InitializationSourceIdentity, OriginalInitializer};
use super::super::value_model::{Context, ValueExpr};
use crate::execution_evidence::ConstructionRejection;
use crate::facts::OwnerInitializationCoverageRefusalReason;
use crate::facts::local_effect_targets::MetadataDecorateCall;
use crate::facts::statement_facts::{SettledElements, StatementFacts, StatementKind};
use crate::reports::schema::{LocalRecordFactoryRejection, RecordFactoryRejectionReason};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::*;

pub(super) struct RootOperands<'source> {
    pub(super) owner: SourceUnitOrdinal,
    pub(super) fact: &'source StatementFacts,
    pub(super) settled_array_elements: BTreeMap<Id, SettledElements>,
    pub(super) global_object_aliases: Option<BTreeSet<Id>>,
    pub(super) calls: &'source Vec<Call>,
    pub(super) assignments: &'source Vec<(Id, ValueExpr)>,
    pub(super) initializer_source: Option<&'source OriginalInitializer>,
    pub(super) initialization_coverage_refusal: Option<OwnerInitializationCoverageRefusalReason>,
    pub(super) top_env: Context,
    pub(super) top_captures: Context,
    pub(super) annotated_record_factory: Option<(Id, Span)>,
    pub(super) record_factory_rejection: Option<LocalRecordFactoryRejection>,
    pub(super) checked_local_constructor_factory: Option<CheckedLocalConstructorFactory>,
    pub(super) metadata_application: Option<&'source MetadataDecorateCall>,
    pub(super) prevalidated_metadata: Option<ValidatedMetadataDecoration>,
    pub(super) certified_deferred_write: bool,
    /// Roots this owner added while no opaque call consumed them; a fully
    /// covered execution withdraws them again at finalization.
    pub(super) provisional_roots: BTreeSet<SourceUnitOrdinal>,
    pub(super) ordered_initializer: Option<&'source LocalBody>,
    /// The canonical source identity of the selected initializer body.
    pub(super) effect_initializer_source:
        Option<(&'source InitializationSourceIdentity, &'source Id, Span)>,
}

pub(super) fn prepare<'source>(
    executor: &mut Executor<'source, '_>,
    owner: SourceUnitOrdinal,
) -> RootOperands<'source> {
    let inputs = executor.inputs;
    let index = inputs.index;
    let fact = &inputs.facts[owner.0];
    let settled_array_elements = executor.settled_array_elements(fact);
    let global_object_aliases = executor.global_object_aliases(fact);
    let calls = &index.top_calls[owner.0];
    let assignments = &index.top_assignments[owner.0];
    let (initializer_source, initialization_coverage_refusal) = match index
        .noninvoking_initializers
        .get(&owner)
    {
        Some(source) if matches!(assignments.as_slice(), [(binding, _)] if source.matches_binding(owner, binding)) => {
            (Some(source), None)
        }
        Some(_) => (
            None,
            Some(OwnerInitializationCoverageRefusalReason::OwnerBindingMismatch),
        ),
        None => (
            None,
            Some(
                index.initialization_coverage_refusals[owner.0]
                    .unwrap_or(OwnerInitializationCoverageRefusalReason::RefusalUnavailable),
            ),
        ),
    };
    let needed = &inputs.needed_bindings;
    let top_env = std::mem::take(&mut executor.state.world_mut().module.top_env);
    let top_captures = std::mem::take(&mut executor.state.world_mut().module.top_captures);
    executor.state.current_top_owner = Some(fact.ordinal);
    executor.activate_native_effects(fact.ordinal, None);
    executor.state.proof_uses.clear();
    // Cache keys include this owner; previous statement snapshots cannot
    // be reused and should not consume this statement's bounded budget.
    executor.state.clear_invocation_memos(&executor.queries);
    executor.state.invocation_count = 0;
    executor.state.fallback_outcome = Outcome::default();
    executor.state.fallback_callback_sizes = None;
    executor.state.fallback_local_owners.clear();
    executor.state.fallback_local_deferred.clear();
    executor
        .state
        .world_mut()
        .publication
        .returned_callback_owners
        .clear();
    let (annotated_record_factory, mut record_factory_rejection) =
        match executor.annotated_record_factory(fact) {
            Some(Ok(factory)) => (Some(factory), None),
            Some(Err(rejection)) => (None, Some(rejection)),
            None => (None, None),
        };
    executor.state.checking_record_factory_contract = annotated_record_factory.is_some();
    let checked_local_constructor_factory = executor.closed_local_constructor_factory_result(fact);
    if let Err(reason) = &checked_local_constructor_factory
        && let Some((factory, span)) = &annotated_record_factory
    {
        let (construction_class_key, construction) =
            if *reason == RecordFactoryRejectionReason::ConstructorProof {
                executor
                    .returned_class_construction_rejection(factory)
                    .map_or((None, None), |(class_key, rejection)| {
                        (class_key, Some(rejection))
                    })
            } else {
                (None, None)
            };
        let (reason, property) = if *reason == RecordFactoryRejectionReason::UnsupportedValue {
            executor
                .record_factory_input_rejection(fact)
                .unwrap_or((*reason, None))
        } else {
            (*reason, None)
        };
        let first_callback_publication_owner =
            if reason == RecordFactoryRejectionReason::FactoryEscaped {
                executor
                    .inputs
                    .index
                    .hoisted_bindings
                    .get(factory)
                    .and_then(|key| {
                        executor
                            .state
                            .world
                            .publication
                            .first_callback_publication_owners
                            .get(key)
                    })
                    .copied()
            } else {
                None
            };
        let first_class_members_publication = if matches!(
            construction.as_ref(),
            Some(ConstructionRejection::ClassMembersEscaped)
        ) {
            construction_class_key.and_then(|key| {
                executor
                    .state
                    .world
                    .classes
                    .first_members_publications
                    .get(&key)
                    .cloned()
            })
        } else {
            None
        };
        record_factory_rejection = Some(LocalRecordFactoryRejection {
            factory_binding: factory.0.to_string(),
            reason,
            property,
            construction,
            first_callback_publication_owner,
            first_class_members_publication,
            lexical_statement_ordinal: fact.ordinal,
            source_location: None,
            span: *span,
        });
    }
    let checked_local_constructor_factory = checked_local_constructor_factory.ok();
    executor
        .state
        .world_mut()
        .classes
        .private_constructor_result_owners = checked_local_constructor_factory
        .as_ref()
        .map_or_else(BTreeSet::new, |proof| proof.checked_owners.clone());
    executor.state.checking_record_factory_contract = false;
    if executor
        .state
        .world
        .classes
        .private_constructor_result_owners
        .is_empty()
    {
        executor
            .observations
            .clear_record_factory_prototype_premises();
    }
    // A recognized metadata statement is a whole call expression. Its
    // eligibility proof is stable across this statement's preparatory
    // lanes, so use the same proof for reachability and call execution.
    let metadata_application = index.top_metadata_decorations[fact.ordinal.0].as_ref();
    let prevalidated_metadata = metadata_application
        .filter(|_| {
            !index.opaque_top.contains(&fact.ordinal)
                && index.top_assignments[fact.ordinal.0].is_empty()
                && index.top_member_value_writes[fact.ordinal.0].is_empty()
                && index.top_callback_result_writes[fact.ordinal.0].is_empty()
        })
        .and_then(|application| {
            executor.validated_metadata_decoration(application, &top_env, &top_captures)
        });
    let certified_deferred_write = executor.accepts_deferred_global_write(fact.ordinal, &top_env);
    if certified_deferred_write {
        executor
            .state
            .world_mut()
            .publication
            .accepted_deferred_global_writes
            .insert(fact.ordinal);
    }
    let closed_result = executor.private_callback_factory_result(fact);
    let private_constructor_result_owners = executor
        .state
        .world
        .classes
        .private_constructor_result_owners
        .clone();
    let fact_roots = executor.fact_roots(
        fact,
        prevalidated_metadata.map(|validated| validated.target_owner),
        &private_constructor_result_owners,
        closed_result.as_ref(),
    );
    if let Some(proof) = closed_result {
        executor.record_callback_factory_use_fact(proof.proof_use);
    }
    // Roots this statement adds while no opaque call has consumed them.
    // A fully covered execution withdraws them again below.
    let mut provisional_roots = BTreeSet::new();
    if executor.state.world.reachability.unknown_call_seen {
        executor.extend_reachable_owners(fact_roots.iter().copied());
    } else if let Some(roots) = &mut executor.state.prior_reachability_roots {
        provisional_roots.extend(fact_roots.difference(roots).copied());
        // Include the active call site before any nested unknown call can
        // consume the prefix roots.
        roots.extend(fact_roots.iter().copied());
    }
    // A class object keeps the superclass value from its definition.
    // Snapshot a simple lexical heritage read from this source prefix so
    // a later assignment to the binding cannot replace the base whose
    // constructor the emitted class actually inherited.
    for (key, class_binding) in &index.class_bindings {
        let owns_needed_class =
            index.class_owners.get(key) == Some(&fact.ordinal) && needed.contains(class_binding);
        if !owns_needed_class {
            continue;
        }
        let Some(class) = index.class_values.get(key) else {
            continue;
        };
        let Some(super_class) = &class.super_class else {
            continue;
        };
        let values = match super_class {
            ValueExpr::Binding(_) => executor.evaluate(super_class, &top_env, &top_captures),
            _ => unknown(),
        };
        executor
            .state
            .world_mut()
            .classes
            .superclasses
            .insert(*key, values);
    }
    let checked_split_binding_copy = initializer_source
        .and_then(|source| source.composed_expression())
        .is_some_and(|expression| matches!(expression, ValueExpr::Binding(_)))
        && index.ordered_initializers.get(&owner).is_some_and(|body| {
            let [(binding, _)] = assignments.as_slice() else {
                return false;
            };
            body.initializer_span().is_some_and(|span| {
                index
                    .initialization_source_identities
                    .get(&owner)
                    .is_some_and(|identity| identity.matches(owner, binding, span))
            })
        });
    let selected_audited_initializer = index.ordered_initializers.get(&owner).and_then(|body| {
        let [(binding, _)] = assignments.as_slice() else {
            return None;
        };
        let (callee, span) = body.single_identifier_call_with_function_argument_initializer()?;
        let source = index.initialization_source_identities.get(&owner)?;
        if !source.matches(owner, binding, body.initializer_span()?) {
            return None;
        }
        let [call] = calls.as_slice() else {
            return None;
        };
        if call.kind != CallKind::Call
            || call.span != span
            || call.has_spread
            || call.args.len() != 1
            || !matches!(&call.callee, ValueExpr::Binding(actual) if actual == callee)
        {
            return None;
        }
        inputs.initializer_call_effect_input(owner, binding, callee, span)
    });
    let native_root_calls = executor.ordered_root_calls_are_native(calls);
    let provider_root_calls = executor.ordered_root_calls_are_provider_summaries(calls);
    let root_construction = index
        .ordered_initializers
        .get(&owner)
        .is_some_and(|body| executor.ordered_root_construction(body, calls));
    let ordered_initializer = ((initializer_source.is_none()
        || checked_split_binding_copy
        || selected_audited_initializer.is_some())
        && matches!(fact.kind, StatementKind::VarDecl)
        && fact.declared.len() == 1
        && matches!(assignments.as_slice(), [(binding, _)]
            if fact.declared.contains(binding)
                && index.top_definite_initializers[owner.0].contains(binding))
        && fact.rebinds.eager.is_empty()
        && fact.global_writes.is_empty()
        && !inputs.exported_owners.contains(&owner)
        && index.dynamic_scope_owners.is_empty()
        && !index.opaque_top.contains(&owner)
        && index.top_property_writes[owner.0].is_empty()
        && index.top_member_value_writes[owner.0].is_empty()
        && index.top_callback_result_writes[owner.0].is_empty()
        && index.top_exported_values[owner.0].is_empty())
    .then(|| index.ordered_initializers.get(&owner))
    .flatten()
    .filter(|body| {
        !body.requires_keyed_collection_new()
            && body.captured_writes().is_empty()
            && (executor.ordered_root_calls_are_local(calls)
                || native_root_calls
                || provider_root_calls
                || selected_audited_initializer.is_some())
            && (calls
                .iter()
                .filter(|call| call.kind == CallKind::Call)
                .nth(1)
                .is_some()
                || (calls.iter().any(|call| call.kind == CallKind::Call)
                    && calls
                        .iter()
                        .any(|call| call.kind == CallKind::LocalMemberRead))
                || body
                    .single_identifier_call_initializer()
                    .is_some_and(|span| {
                        matches!(calls.as_slice(), [call]
                        if call.kind == CallKind::Call
                            && call.span == span
                            && !call.has_spread
                            && call.args.is_empty()
                            && matches!(call.callee, ValueExpr::Binding(_)))
                    })
                || checked_split_binding_copy
                || selected_audited_initializer.is_some()
                // A spread's iterator protocol runs only through the
                // kernel's `IterateSpread` rule; the flattened lane has
                // no receipt for it.
                || calls.iter().any(|call| {
                    call.kind == CallKind::ArraySpread && call.caller.is_none()
                })
                || native_root_calls
                || provider_root_calls
                || root_construction
                || calls.iter().any(|call| {
                    call.kind == CallKind::LocalMemberRead
                        && call.owner == owner
                        && call.caller.is_none()
                        && body.initializer_span() == Some(call.span)
                        && object_own_data_read_property(&call.callee, |id| {
                            inputs.flow.is_declared(id)
                                || inputs.shadowed.contains(id.0.as_ref())
                        })
                        .is_some()
                        && index.native_own_data_read_premise(call.span).is_some()
                }))
    });
    // Canonical source identity is independent of leaf/value admission.
    // Retain the exact selected body span, not a lowered-value guess.
    let effect_initializer_source = ordered_initializer
        .and_then(|body| {
            let [(binding, _)] = assignments.as_slice() else {
                return None;
            };
            let span = body.initializer_span()?;
            let source = index.initialization_source_identities.get(&owner)?;
            source
                .matches(owner, binding, span)
                .then_some((source, binding, span))
        })
        .or_else(|| {
            let [(binding, expression)] = assignments.as_slice() else {
                return None;
            };
            let source = initializer_source?.operation_source(expression)?;
            source
                .matches(owner, binding, source.initializer_span())
                .then_some((source, binding, source.initializer_span()))
        });
    RootOperands {
        owner,
        fact,
        settled_array_elements,
        global_object_aliases,
        calls,
        assignments,
        initializer_source,
        initialization_coverage_refusal,
        top_env,
        top_captures,
        annotated_record_factory,
        record_factory_rejection,
        checked_local_constructor_factory,
        metadata_application,
        prevalidated_metadata,
        certified_deferred_write,
        provisional_roots,
        ordered_initializer,
        effect_initializer_source,
    }
}
