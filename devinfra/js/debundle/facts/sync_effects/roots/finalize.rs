//! Owner finalization: the owner's reports, certificates, prior-effect
//! frontier and binding-effect coverage, from its settled execution.
//! It executes no source operation: it reads the inputs and queries and
//! writes only the execution state and the run-level observations it drains.

use super::super::effect_coverage::BindingEffectCoverage;
use super::super::execution_index::CallKind;
use super::super::interpreter::{ExecutionInputs, ExecutionState, Observations, Outcome};
use super::super::noninvoking_initialization::CompletedInitialization;
use super::super::runtime_inference::RuntimeQueries;
use super::super::value_model::AbstractValue;
use super::assignment::RootAssignment;
use super::operands::RootOperands;
use crate::facts::OwnerInitializationCoverageRefusalReason;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::evaluation_effects::EvaluationEffects;
use crate::facts::statement_facts::SynchronousCalls;
use std::collections::BTreeSet;

pub(super) fn finalize(
    inputs: &ExecutionInputs<'_>,
    state: &mut ExecutionState,
    observations: &mut Observations,
    queries: &RuntimeQueries<'_, '_>,
    evaluation_effects: &EvaluationEffects,
    root: RootOperands<'_>,
    assigned: RootAssignment,
) -> SynchronousCalls {
    let index = inputs.index;
    let RootOperands {
        owner,
        fact,
        calls,
        assignments,
        initializer_source,
        mut initialization_coverage_refusal,
        top_env,
        top_captures,
        annotated_record_factory,
        record_factory_rejection,
        checked_local_constructor_factory,
        provisional_roots,
        ordered_statement,
        effect_initializer_source,
        ..
    } = root;
    let RootAssignment {
        mut outcome,
        evaluated_initializer,
    } = assigned;
    if !state
        .world
        .classes
        .private_constructor_result_owners
        .is_empty()
        && let Some((factory, span)) = annotated_record_factory
        && !observations.record_factory_prototype_premises().is_empty()
    {
        outcome.effects.local_record_factory_uses.push(
            crate::reports::schema::LocalRecordFactoryUse {
                factory_binding: factory.0.to_string(),
                prototype_properties: observations
                    .record_factory_prototype_premises()
                    .properties(),
                lexical_statement_ordinal: fact.ordinal,
                source_location: None,
                span,
            },
        );
    }
    outcome
        .effects
        .local_record_factory_rejections
        .extend(record_factory_rejection);
    // The certificate is minted from the source proof above only after
    // the ordinary executor has completed this exact initializer without
    // falling back. The settled purity projection consumes it to drop
    // only the impure-callee effect at this call site.
    outcome.effects.callback_factory_uses = std::mem::take(&mut state.proof_uses);
    outcome.effects.returned_record_uses = std::mem::take(&mut state.returned_record_proof_uses);
    outcome.effects.author_host_contract_uses =
        std::mem::take(&mut state.author_host_contract_uses);
    observations.drain_reports_into(&mut outcome.effects);
    if !state
        .world
        .classes
        .private_constructor_result_owners
        .is_empty()
        && outcome.complete
        && !outcome.unknown_call
        && outcome.effects.precision_losses.is_empty()
        && outcome.effects.uninitialized_reads.is_empty()
        && let Some(proof) = checked_local_constructor_factory
    {
        let dependencies = observations.dependencies().clone();
        outcome.effects.local_record_factory_purity =
            Some(crate::facts::LocalRecordFactoryPurityCertificate {
                call_span: proof.call_span,
                checked_owners: proof.checked_owners,
                dependencies,
            });
    }
    state.world_mut().module.top_env = top_env;
    state.world_mut().module.top_captures = top_captures;
    state.clear_invocation_memos(queries);
    state.invocation_count = 0;
    state.fallback_outcome = Outcome::default();
    state.fallback_callback_sizes = None;
    state.fallback_local_owners.clear();
    state.fallback_local_deferred.clear();
    for key in std::mem::take(&mut state.statement_invoked_callables) {
        if let Some(cells) = inputs.callable_heap_contents.get(&key) {
            outcome.effects.heap_contents.extend(cells);
        }
    }
    outcome.effects.purity_disagreement =
        queries.complete_owner_purity(owner, outcome.effects.local_record_factory_purity.as_ref());
    let mut prior_effect_owners = outcome.executed_owners.clone();
    prior_effect_owners.retain(|owner| {
        !state
            .world
            .classes
            .private_constructor_result_owners
            .contains(owner)
    });
    // Ordered execution records the root too; the completed private-result proof covers it.
    if outcome.effects.local_record_factory_purity.is_some() {
        prior_effect_owners.remove(&fact.ordinal);
    }
    if !queries.completed_statement_is_pure(fact.ordinal)
        && state
            .world
            .classes
            .private_constructor_result_owners
            .is_empty()
    {
        prior_effect_owners.insert(fact.ordinal);
    }
    state.prior_effect_owners.extend(prior_effect_owners);
    state.statement_executed_owners.clear();
    state
        .world_mut()
        .publication
        .returned_callback_owners
        .clear();
    state
        .world_mut()
        .classes
        .private_constructor_result_owners
        .clear();
    observations.clear_record_factory_prototype_premises();
    state.checking_record_factory_contract = false;
    state.current_top_owner = None;
    outcome
        .effects
        .evaluation_effects
        .extend(evaluation_effects);
    outcome
        .effects
        .evaluation_effects
        .depend_on(&state.world.prerequisites);
    outcome
        .effects
        .evaluation_effects
        .depend_on(&queries.dependencies());
    let initializer_result_unproved = evaluated_initializer.is_none();
    if outcome.complete
        && !outcome.unknown_call
        && outcome.effects.precision_losses.is_empty()
        && outcome.effects.uninitialized_reads.is_empty()
        && let (Some(source), Some(evaluated)) = (initializer_source, evaluated_initializer)
    {
        outcome.effects.completed_initialization =
            source.complete(evaluated, owner, &state.world.module.initialized_bindings);
    }
    if outcome.effects.completed_initialization.is_none()
        && outcome.complete
        && !outcome.unknown_call
        && outcome.effects.precision_losses.is_empty()
        && outcome.effects.uninitialized_reads.is_empty()
        && let Some((source, binding, span)) = effect_initializer_source
    {
        outcome.effects.completed_initialization = CompletedInitialization::from_root_effects(
            source,
            owner,
            binding,
            span,
            &state.world.module.initialized_bindings,
            &outcome.effect_coverage,
        );
    }
    if outcome.effects.completed_initialization.is_none()
        && ordered_statement.is_some()
        && outcome.complete
        && !outcome.unknown_call
        && outcome.effects.precision_losses.is_empty()
        && outcome.effects.uninitialized_reads.is_empty()
    {
        outcome.effects.completed_initialization =
            CompletedInitialization::from_root_statement_effects(owner, &outcome.effect_coverage);
    }
    // A private declaration whose every operation the ledger covers ran
    // no user code and exposed no capability: its own callables and the
    // bindings it read into its result stay as unpublished as under a
    // direct function declaration, provided the result is a literal of
    // this statement or a primitive, so every stored callable is reached
    // through its tracked own-property surface. A callable retained by an
    // opaque carrier (a descriptor map, a native collection) is reached
    // only through the roots and keeps them. Only roots this statement
    // added are withdrawn, and only while no opaque call consumed them.
    if !provisional_roots.is_empty()
        && !state.world.reachability.unknown_call_seen
        && !inputs.exported_owners.contains(&fact.ordinal)
        && outcome.complete
        && !outcome.unknown_call
        && outcome.executed_owners.is_empty()
        && outcome.effects.precision_losses.is_empty()
        && outcome.effect_coverage.normal_dependencies().is_some()
        && let [(binding, _)] = assignments.as_slice()
        && state
            .world
            .module
            .prefix_values
            .get(binding)
            .is_some_and(|values| {
                !values.is_empty() && values.iter().all(|value| {
                    value.is_primitive()
                        || matches!(value, AbstractValue::Value(Value::Alloc(Kind::Object, object))
                            if state.statement_literal(index, *object))
                })
            })
        && let Some(roots) = &mut state.prior_reachability_roots
    {
        for root in &provisional_roots {
            roots.remove(root);
        }
    }
    if outcome.effects.completed_initialization.is_none() {
        if initialization_coverage_refusal.is_none() {
            initialization_coverage_refusal = Some(if outcome.unknown_call {
                OwnerInitializationCoverageRefusalReason::UnknownCall
            } else if !outcome.effects.precision_losses.is_empty() {
                OwnerInitializationCoverageRefusalReason::PrecisionLoss
            } else if !outcome.effects.uninitialized_reads.is_empty() {
                OwnerInitializationCoverageRefusalReason::UninitializedRead
            } else if !outcome.complete {
                OwnerInitializationCoverageRefusalReason::IncompleteExecution
            } else if ordered_statement.is_some() {
                OwnerInitializationCoverageRefusalReason::UnprovedStatementOperation
            } else if initializer_result_unproved {
                OwnerInitializationCoverageRefusalReason::InitializerResultUnproved
            } else {
                OwnerInitializationCoverageRefusalReason::BindingNotInitialized
            });
        }
        outcome.effects.initialization_coverage_refusal = initialization_coverage_refusal;
    }
    if let Some(identity) = outcome.root_binding_effect_identity.take() {
        let expected_root_calls: BTreeSet<_> = calls
            .iter()
            .filter(|call| matches!(call.kind, CallKind::Call | CallKind::New))
            .map(|call| (call.span.lo.0, call.span.hi.0))
            .collect();
        let receipt = effect_initializer_source.and_then(|(source, binding, span)| {
            (identity.owner == owner
                && identity.binding == *binding
                && identity.initializer_span == Some(span)
                && source.matches(owner, binding, span)
                && state.world.module.initialized_bindings.contains(binding)
                // A later fallback/precision path may be absorbed after
                // the ordered kernel returned an apparently covered
                // fragment. Whole-owner lexical authority requires the
                // final execution outcome, not just that fragment, to
                // remain fully settled.
                && outcome.complete
                && !outcome.unknown_call
                && outcome.effects.precision_losses.is_empty()
                && outcome.effects.uninitialized_reads.is_empty()
                && outcome.effects.fallback_only.reads.is_empty()
                && outcome.effects.fallback_only.rebinds.is_empty())
            .then(|| {
                outcome
                    .effect_coverage
                    .seal_lexical_binding_effects(identity.clone(), &expected_root_calls)
            })
            .flatten()
        });
        outcome.effects.binding_effect_coverage = BindingEffectCoverage::OrderedRootInitializer {
            identity,
            receipt: receipt.map(Box::new),
        };
    } else if matches!(
        &outcome.effects.binding_effect_coverage,
        BindingEffectCoverage::Unclassified
    ) {
        // All executions outside the selected ordered-root path retain
        // the explicitly named transitional authority. A selected root
        // was marked before its first operation and can never regain this
        // bool-based path if its receipt or identity is lost.
        outcome.effects.binding_effect_coverage = BindingEffectCoverage::LegacyExecution;
    }
    outcome.effects
}
