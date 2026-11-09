//! The owner body: the ordered initializer through the kernel, or the
//! flattened call lane, then the owner's fallback and settlement.

use super::super::effect_coverage::RootInitializerIdentity;
use super::super::interpreter::{Executor, LocalActivation, Outcome};
use super::super::kernel::frame::SelectedValue;
use super::super::value_model::{Context, Values};
use super::operands::RootOperands;
use crate::execution_evidence::{AssumedEffectReason, ExecutionOperation};
use std::collections::BTreeSet;
use swc_common::DUMMY_SP;

pub(super) struct RootBody {
    pub(super) outcome: Outcome,
    /// The ordered initializer's selected result, held until assignment.
    pub(super) ordered_initializer_result: Option<SelectedValue>,
}

pub(super) fn execute(executor: &mut Executor<'_, '_>, root: &mut RootOperands<'_>) -> RootBody {
    let inputs = executor.inputs;
    let index = inputs.index;
    let needed = &inputs.needed_bindings;
    let RootOperands {
        owner,
        fact,
        calls,
        assignments,
        metadata_application,
        prevalidated_metadata,
        certified_deferred_write,
        ordered_initializer,
        ordered_statement,
        ref mut top_env,
        ref top_captures,
        ref mut settled_array_elements,
        ref mut global_object_aliases,
        ..
    } = *root;
    let mut outcome = Outcome {
        complete: true,
        ..Outcome::default()
    };
    // Keep the selected result in this owner until publication. Operands
    // and calls execute once, at their position in the shared local plan.
    let ordered_initializer_result = if let Some(body) = ordered_initializer {
        let [(binding, _)] = assignments.as_slice() else {
            unreachable!("ordered root initializer has one assignment");
        };
        outcome = executor.execute_local_body(
            body,
            Context::new(),
            None,
            owner,
            LocalActivation::RootInitializer(RootInitializerIdentity {
                owner,
                binding: binding.clone(),
                initializer_span: body.root_span(),
            }),
        );
        Some(SelectedValue {
            values: std::mem::take(&mut outcome.returns),
            selection: outcome.return_selection.take(),
        })
    } else {
        if let Some(body) = ordered_statement {
            // The statement's completion value is discarded.
            outcome = executor.execute_local_body(
                body,
                Context::new(),
                None,
                owner,
                LocalActivation::RootStatement,
            );
            outcome.returns = Values::new();
            outcome.return_selection = None;
        }
        None
    };
    // CLEANUP(added 2026-11-09): delete this flattened lane with L4e. It
    //   still decides every root owner outside the kernel's admitted roots
    //   (`ordered_initializer`, and `ordered_statement` through the kernel's
    //   `store_field` rule), and the legacy gate reads what it computes.
    if ordered_initializer.is_none() && ordered_statement.is_none() {
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
                .entry(id.clone())
                .or_default();
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
            executor.summarize_metadata_decoration(
                application,
                validated,
                fact.ordinal,
                &mut outcome,
            );
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
    }
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
