//! Assignment and export: the owner's bindings receive their values in
//! source order, then its exported values are published.

use super::super::interpreter::{Executor, Outcome};
use super::super::noninvoking_initialization::EvaluatedInitializer;
use super::super::runtime_inference::{RuntimeOperation, RuntimeValue};
use super::super::value_model::AbstractValue;
use super::body::RootBody;
use super::operands::RootOperands;
use crate::facts::abstract_domain::value::Value;

pub(super) struct RootAssignment {
    pub(super) outcome: Outcome,
    pub(super) evaluated_initializer: Option<EvaluatedInitializer>,
}

pub(super) fn assign(
    executor: &mut Executor<'_, '_>,
    root: &mut RootOperands<'_>,
    body: RootBody,
) -> RootAssignment {
    let inputs = executor.inputs;
    let index = inputs.index;
    let needed = &inputs.needed_bindings;
    let RootOperands {
        owner,
        fact,
        assignments,
        initializer_source,
        ordered_initializer,
        ref mut initialization_coverage_refusal,
        ref mut top_env,
        ref mut top_captures,
        ..
    } = *root;
    let RootBody {
        mut outcome,
        mut ordered_initializer_result,
    } = body;
    let mut evaluated_initializer = None;
    for (id, expression) in assignments {
        if !needed.contains(id) && initializer_source.is_none() {
            continue;
        }
        let (values, selection) = if let Some(result) = ordered_initializer_result.take() {
            let selection = result.selection.filter(|selection| {
                selection.matches(&result.values)
                    && outcome.complete
                    && !outcome.unknown_call
                    && outcome.effects.precision_losses.is_empty()
                    && outcome
                        .root_binding_effect_identity
                        .as_ref()
                        .is_some_and(|identity| {
                            identity.owner == owner
                                && identity.binding == *id
                                && ordered_initializer.is_some_and(|body| {
                                    identity.initializer_span == body.root_span()
                                })
                        })
            });
            // This declaration is reached only on the module's normal
            // continuation. Unavailable denotes an abrupt alternative of
            // the initializer, not a value that can be stored in its
            // lexical cell. Keep the initializer's incomplete outcome and
            // retain Unavailable when it has no normal result at all.
            let mut values = result.values;
            if values.len() > 1 {
                values.remove(&AbstractValue::Value(Value::Unavailable));
            }
            (values, selection)
        } else if let Some(source) = initializer_source {
            let RuntimeValue::Initializer {
                values,
                evaluated,
                operation_coverage,
                refusal,
            } = executor.request(RuntimeOperation::EvaluateInitializer {
                source: source.clone(),
                expression: expression.clone(),
                environment: top_env.clone(),
                captures: top_captures.clone(),
            })
            else {
                unreachable!("checked initializer transfers produce initializer results");
            };
            evaluated_initializer = evaluated.map(|evaluated| *evaluated);
            if let Some(coverage) = operation_coverage {
                outcome.effect_coverage = *coverage;
            }
            *initialization_coverage_refusal = initialization_coverage_refusal.or(refusal);
            (values, None)
        } else {
            (executor.evaluate(expression, top_env, top_captures), None)
        };
        // A dead initializer is evaluated for coverage, while the normal
        // needed-binding policy still owns retained prefix values.
        if !needed.contains(id) {
            continue;
        }
        if !executor.inputs.top_bindings.contains(id) {
            top_captures
                .entry(id.clone())
                .or_default()
                .extend(values.clone());
        }
        top_env
            .entry(id.clone())
            .or_default()
            .extend(values.clone());
        if executor.inputs.top_bindings.contains(id) {
            executor
                .state
                .world_mut()
                .module
                .prefix_callable_selections
                .remove(id);
            executor
                .state
                .world_mut()
                .module
                .prefix_values
                .entry_or_default(id)
                .extend(values);
            if let Some(selection) = selection
                && selection.matches(&executor.state.world.module.prefix_values[id])
            {
                executor
                    .state
                    .world_mut()
                    .module
                    .prefix_callable_selections
                    .insert(id.clone(), selection);
            }
            executor.state.world_mut().revision.bump_prefix();
        }
    }
    executor
        .state
        .world_mut()
        .module
        .initialized_bindings
        .extend(
            index.top_definite_initializers[fact.ordinal.0]
                .iter()
                .cloned(),
        );
    for expression in &index.top_exported_values[fact.ordinal.0] {
        let values = executor.evaluate(expression, top_env, top_captures);
        executor
            .state
            .world_mut()
            .publication
            .exported_runtime_values
            .extend(values);
        executor.state.world_mut().revision.bump_prefix();
    }
    RootAssignment {
        outcome,
        evaluated_initializer,
    }
}
