//! The owner body: the ordered initializer through the kernel, or the
//! flattened call lane, then the owner's fallback and settlement. An
//! attempted initializer runs through the kernel first and falls back to the
//! flattened lane, rewound, unless that execution seals the owner; so does
//! an admitted root expression statement.

use super::super::effect_coverage::RootInitializerIdentity;
use super::super::execution_index::{Call, CallKind};
use super::super::interpreter::{Executor, LocalActivation, Outcome};
use super::super::kernel::frame::SelectedValue;
use super::super::local_operations::LocalBody;
use super::super::value_model::{Context, ValueExpr, Values};
use super::operands::RootOperands;
use crate::execution_evidence::{AssumedEffectReason, ExecutionOperation};
use crate::facts::{OwnerInitializationCoverageRefusalReason, RootAttempt, RootAttemptRewind};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use swc_common::DUMMY_SP;
use swc_ecma_ast::Id;

pub(super) struct RootBody {
    pub(super) outcome: Outcome,
    /// The ordered initializer's selected result, held until assignment.
    pub(super) ordered_initializer_result: Option<SelectedValue>,
}

pub(super) fn execute(executor: &mut Executor<'_, '_>, root: &mut RootOperands<'_>) -> RootBody {
    let mut attempt = None;
    if let Some((body, source)) = root.attempted_initializer.take() {
        let checkpoint = executor.checkpoint_root_attempt();
        let mut outcome = execute_ordered(executor, root.owner, root.assignments, body);
        match attempt_seal(executor, &outcome, Some(root.calls)) {
            Ok(()) => {
                root.ordered_initializer = Some(body);
                root.effect_initializer_source = Some(source);
                outcome.effects.root_attempt = Some(RootAttempt::Sealed);
                return settle(executor, root, outcome);
            }
            Err(reason) => attempt = Some(RootAttempt::Rewound(reason)),
        }
        // CLEANUP(added 2026-11-09): delete the rewind and this flat-lane
        //   rerun at L4e. The legacy gate reads the flat lane's complete
        //   `LegacyExecution`; once it is gone an unsealed kernel execution
        //   (`execute_ordered`, its open ledger) is the owner's outcome.
        // The flat lane runs the owner as if the attempt never ran.
        executor.rewind_root_attempt(checkpoint);
    }
    if let Some(body) = root.ordered_statement.take() {
        let checkpoint = executor.checkpoint_root_attempt();
        let mut outcome = execute_statement(executor, root.owner, body);
        match attempt_seal(executor, &outcome, None) {
            Ok(()) => {
                root.ordered_statement = Some(body);
                outcome.effects.root_attempt = Some(RootAttempt::Sealed);
                return settle(executor, root, outcome);
            }
            Err(reason) => attempt = Some(RootAttempt::Rewound(reason)),
        }
        // CLEANUP(added 2026-11-09): delete the rewind and this flat-lane
        //   rerun at L4e, with the initializer attempt's above. The legacy gate
        //   reads the flat lane's `LegacyExecution`; once it is gone the
        //   kernel's `store_field` execution (`execute_statement`, its open
        //   ledger) is the owner's outcome.
        executor.rewind_root_attempt(checkpoint);
        root.initialization_coverage_refusal =
            Some(OwnerInitializationCoverageRefusalReason::UnprovedStatementOperation);
    }
    let mut outcome = match root.ordered_initializer {
        Some(body) => execute_ordered(executor, root.owner, root.assignments, body),
        None => execute_flat(executor, root),
    };
    outcome.effects.root_attempt = attempt;
    settle(executor, root, outcome)
}

/// Operands and calls execute once, at their position in the shared local
/// plan; the selected result stays in the outcome until `settle`.
fn execute_ordered(
    executor: &mut Executor<'_, '_>,
    owner: SourceUnitOrdinal,
    assignments: &[(Id, ValueExpr)],
    body: &LocalBody,
) -> Outcome {
    let [(binding, _)] = assignments else {
        unreachable!("ordered root initializer has one assignment");
    };
    executor.execute_local_body(
        body,
        Context::new(),
        None,
        owner,
        LocalActivation::RootInitializer(RootInitializerIdentity {
            owner,
            binding: binding.clone(),
            initializer_span: body.root_span(),
        }),
    )
}

/// An admitted root expression statement; its completion value is discarded.
fn execute_statement(
    executor: &mut Executor<'_, '_>,
    owner: SourceUnitOrdinal,
    body: &LocalBody,
) -> Outcome {
    let mut outcome = executor.execute_local_body(
        body,
        Context::new(),
        None,
        owner,
        LocalActivation::RootStatement,
    );
    outcome.returns = Values::new();
    outcome.return_selection = None;
    outcome
}

/// Whether an attempted execution seals its owner, else the first condition
/// it fails. The ledger is the authority: an exactly normal completion with
/// every effect domain covered and no loss, unknown call or effect only a
/// fallback summary saw. An initializer also needs its root binding's
/// lexical receipt; a statement has no binding and needs none.
fn attempt_seal(
    executor: &Executor<'_, '_>,
    outcome: &Outcome,
    lexical_root_calls: Option<&[Call]>,
) -> Result<(), RootAttemptRewind> {
    use RootAttemptRewind::*;
    let seal = |failed: bool, reason| if failed { Err(reason) } else { Ok(()) };
    // An initializer attempt is judged by its ledger alone (#15262).
    seal(
        lexical_root_calls.is_none() && !outcome.complete,
        IncompleteExecution,
    )?;
    seal(
        outcome.unknown_call || executor.state.fallback_outcome.unknown_call,
        UnknownCall,
    )?;
    seal(!outcome.effects.precision_losses.is_empty(), PrecisionLoss)?;
    seal(
        !outcome.effects.uninitialized_reads.is_empty(),
        UninitializedRead,
    )?;
    seal(
        !outcome.effects.fallback_only.reads.is_empty()
            || !outcome.effects.fallback_only.rebinds.is_empty(),
        FallbackOnlyEffect,
    )?;
    seal(
        outcome.effect_coverage.normal_dependencies().is_none(),
        OpenEffectLedger,
    )?;
    if let Some(calls) = lexical_root_calls {
        seal(
            !outcome
                .root_binding_effect_identity
                .as_ref()
                .is_some_and(|identity| {
                    outcome
                        .effect_coverage
                        .seal_lexical_binding_effects(identity.clone(), &expected_root_calls(calls))
                        .is_some()
                }),
            LexicalReceiptUnsealed,
        )?;
    }
    Ok(())
}

/// The root's own call and construction sites, which its lexical receipt
/// must cover.
pub(super) fn expected_root_calls(calls: &[Call]) -> BTreeSet<(u32, u32)> {
    calls
        .iter()
        .filter(|call| matches!(call.kind, CallKind::Call | CallKind::New))
        .map(|call| (call.span.lo.0, call.span.hi.0))
        .collect()
}

fn execute_flat(executor: &mut Executor<'_, '_>, root: &mut RootOperands<'_>) -> Outcome {
    let inputs = executor.inputs;
    let index = inputs.index;
    let needed = &inputs.needed_bindings;
    let RootOperands {
        fact,
        calls,
        assignments,
        metadata_application,
        prevalidated_metadata,
        certified_deferred_write,
        ref mut top_env,
        ref top_captures,
        ..
    } = *root;
    let mut outcome = Outcome {
        complete: true,
        ..Outcome::default()
    };
    // A source owner is indivisible, but can assign an existing binding
    // before one of its calls, e.g. `(reader = () => later, host())`.
    // Join these possible values before the flattened calls. Keep the
    // previous values and do not mark initialization definite until the
    // owner finishes; assignments on skipped/later paths are only may
    // values. A lexical TDZ still blocks publication through that binding.
    for (id, expression) in assignments {
        if !needed.contains(id)
            || !fact.rebinds.eager.contains(id)
            || !(executor.inputs.var_bindings.contains(id)
                || executor
                    .state
                    .world
                    .module
                    .initialized_bindings
                    .contains(id))
        {
            continue;
        }
        let values = executor.evaluate(expression, top_env, top_captures);
        top_env
            .entry(id.clone())
            .or_default()
            .extend(values.iter().cloned());
        let possible = executor
            .state
            .world_mut()
            .module
            .prefix_values
            .entry_or_default(id);
        let before = possible.len();
        possible.extend(values);
        if possible.len() != before {
            executor.state.world_mut().revision.bump_prefix();
        }
    }
    // Like callable writes above, top-level property writes are kept in a
    // separate visitor lane. Applying all of this statement's writes
    // before its flattened call list may expose a later write early, which
    // is conservative and avoids missing a callback assigned before an
    // opaque call in the same statement.
    let member_writes = &index.top_member_value_writes[fact.ordinal.0];
    executor.apply_member_value_writes(
        member_writes,
        top_env,
        top_captures,
        &BTreeSet::new(),
        certified_deferred_write,
    );
    executor.publish_destructuring_target_receivers(
        &index.top_destructuring_member_receivers[fact.ordinal.0],
        top_env,
        top_captures,
    );
    if !certified_deferred_write {
        executor.promote_callback_result_writes(
            &index.top_callback_result_writes[fact.ordinal.0],
            top_env,
            top_captures,
            fact.ordinal,
            &mut outcome,
            &BTreeSet::new(),
        );
    }
    let validated_metadata = prevalidated_metadata.or_else(|| {
        metadata_application.and_then(|application| {
            executor.validated_metadata_decoration(application, top_env, top_captures)
        })
    });
    if let (Some(application), Some(validated)) = (metadata_application, validated_metadata) {
        executor.summarize_metadata_decoration(application, validated, fact.ordinal, &mut outcome);
    } else {
        for call in calls {
            executor.execute_call(call, top_env, top_captures, &mut outcome);
        }
    }
    // The fresh-instance proof applies only to this whole top-level
    // assignment. Nested callable writes owned by the same statement
    // still take the normal setter fallback when invoked later.
    if !certified_deferred_write && !index.fresh_instance_field_writes.contains(&fact.ordinal) {
        executor.promote_property_writes(
            &index.top_property_writes[fact.ordinal.0],
            fact.ordinal,
            &mut outcome,
        );
    }
    outcome
}

/// The owner's fallback summary and settlement, after either lane.
fn settle(
    executor: &mut Executor<'_, '_>,
    root: &mut RootOperands<'_>,
    mut outcome: Outcome,
) -> RootBody {
    let index = executor.inputs.index;
    let RootOperands {
        fact,
        ordered_initializer,
        private_factory_result,
        ref mut settled_array_elements,
        ref mut global_object_aliases,
        ..
    } = *root;
    // Keep the selected result in this owner until publication.
    let ordered_initializer_result = ordered_initializer.map(|_| SelectedValue {
        values: std::mem::take(&mut outcome.returns),
        selection: outcome.return_selection.take(),
    });
    let mut fallback = std::mem::take(&mut executor.state.fallback_outcome);
    if fallback.unknown_call {
        // Every effect only the fallback summary contributed is assumed
        // by it, whichever path opened the summary.
        let reason = AssumedEffectReason::FallbackSummary {
            loss: fallback
                .effects
                .precision_losses
                .first()
                .expect("a fallback summary records the loss that opened it")
                .reason
                .clone(),
        };
        outcome.absorb_call_effects(&mut fallback.effects, Some(&reason));
        outcome.executed_owners.extend(fallback.executed_owners);
        outcome.unknown_call = true;
        outcome.inherit_completeness(false, fallback.effects.precision_losses);
    }
    if ordered_initializer.is_some()
        && private_factory_result
        && outcome.complete
        && !outcome.unknown_call
    {
        // The kernel records the root as executed. This root ran only the
        // substituted factory call, which retains the callback in its
        // private result, so the owner exposes nothing by itself.
        outcome.executed_owners.remove(&fact.ordinal);
    }
    if index.opaque_top.contains(&fact.ordinal) {
        outcome.unsupported(ExecutionOperation::OpaqueExecution, fact.ordinal, DUMMY_SP);
    }
    outcome.effects.complete = outcome.complete;
    outcome.effects.settled_array_elements = std::mem::take(settled_array_elements);
    if !outcome.complete {
        executor.widen_native_effects();
        executor.unsettle_incomplete_reach(fact, &outcome.effects);
    }
    outcome.effects.global_object_aliases = global_object_aliases.take();
    // A failed runtime path forfeits the prevalidated private-result proof.
    // Keep the actual execution/effect outcome in either case.
    if !executor
        .state
        .world
        .classes
        .private_constructor_result_owners
        .is_empty()
        && (!outcome.complete || outcome.unknown_call)
    {
        let owners = std::mem::take(
            &mut executor
                .state
                .world_mut()
                .classes
                .private_constructor_result_owners,
        );
        executor
            .observations
            .clear_record_factory_prototype_premises();
        executor.extend_reachable_owners(owners.into_iter().chain([fact.ordinal]));
        // The source proof normally makes this unreachable. If an actual
        // executor path fails, restore the current statement's omitted
        // callback effects as well as all future reachability roots.
        let mut restored = Outcome::default();
        let reason = AssumedEffectReason::PrivateConstructorRestore;
        for owner in executor.state.world.reachability.owners.clone() {
            executor.promote_lazy_owner(owner, fact.ordinal, &mut restored);
        }
        outcome.absorb_call_effects(&mut restored.effects, Some(&reason));
        executor.apply_rebind_effects(&outcome.effects.rebinds);
    }
    // These are available roots for the remainder of this owner's value
    // evaluation, independently of the completed prior-effect frontier.
    let state = &mut *executor.state;
    if !state.world.reachability.unknown_call_seen
        && let Some(roots) = &mut state.prior_reachability_roots
    {
        roots.extend(outcome.executed_owners.iter().copied().filter(|owner| {
            !state
                .world
                .classes
                .private_constructor_result_owners
                .contains(owner)
        }));
    }
    RootBody {
        outcome,
        ordered_initializer_result,
    }
}
