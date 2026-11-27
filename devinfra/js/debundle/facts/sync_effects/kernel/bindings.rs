use super::cells;
use super::context::KernelContext;
use super::continuation::{
    absorb, absorb_primitive_transfer, completing_selected, unproved_operator,
};
use super::frame::{LocalFrame, RemainderDisposition, SelectedValue};
use analysis_vocabulary::value::{Kind, Value};
use swc_common::Span;
use swc_ecma_ast::Id;
use value_model::local_operations::{LocalBinding, LocalBody};
use value_model::{AbstractValue, HeapAddress, Values};
use world::allocation_transfer::{self, AllocationJudgment};
use world::effect_coverage::EffectCoverageFragment;

pub(super) fn store_binding(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    binding: &Id,
    selected: SelectedValue,
    span: Span,
) -> Result<SelectedValue, RemainderDisposition> {
    let values = selected.values;
    if values.is_empty() {
        // No store happens, but the ledger stays open for a binding outside
        // the body exactly as if the store had been attempted: a join over
        // this leaf must not see a closed fragment for it.
        if !body.bindings.contains_key(binding) {
            frame
                .ledger
                .effects
                .compose(&EffectCoverageFragment::default());
        }
        frame.stop_normal_path();
        return Ok(SelectedValue::raw(values));
    }
    let Some(kind) = body.bindings.get(binding) else {
        frame.binding_selections.remove(binding);
        if !context.world().active_cells.contains_key(binding)
            && let Some(effects) = context.store_module_binding(binding, &values)
        {
            frame.ledger.effects.compose(&effects);
            return Ok(completing_selected(SelectedValue::raw(values), frame));
        }
        frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        let result = cells::store_captured(context, frame, binding, &values, span)?;
        frame.ledger.allocation.compose(&result.judgment);
        return Ok(completing_selected(
            SelectedValue::raw(result.values),
            frame,
        ));
    };
    let uninitialized_let = match kind {
        LocalBinding::Let => {
            let Some(current) = frame.environment.get(binding) else {
                return Err(RemainderDisposition::NeedsSummary);
            };
            current.contains(&AbstractValue::Value(Value::Unavailable))
        }
        _ => false,
    };
    if matches!(kind, LocalBinding::Const | LocalBinding::NamedSelf(_))
        || (*kind == LocalBinding::Parameter
            && frame.parameter_inputs.is_some()
            && !frame.initialized_bindings.contains(binding))
        || uninitialized_let
    {
        // Assignment to a const, a named self binding or a cell still in TDZ
        // throws before storing anything.
        frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        frame.record_unknown_abrupt();
        frame.stop_normal_path();
        return Ok(SelectedValue::raw(Values::new()));
    }
    if matches!(kind, LocalBinding::Parameter | LocalBinding::Let)
        && frame.initialized_bindings.contains(binding)
    {
        // Only an initialized activation-local mutable binding can carry
        // this receipt. The shared cell policy rejects live captured cells;
        // composing the store never closes an unknown RHS effect.
        let effects = cells::declarative_binding_operation(context, body, binding);
        frame.ledger.effects.compose(&effects);
    } else {
        frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
    }
    frame.environment.insert(binding.clone(), values.clone());
    if let Some(selection) = selected
        .selection
        .filter(|selection| selection.matches(&values))
    {
        frame.binding_selections.insert(binding.clone(), selection);
    } else {
        frame.binding_selections.remove(binding);
    }
    cells::declared_binding_stored(context, body, binding, frame);
    Ok(SelectedValue {
        values,
        selection: frame.binding_selections.get(binding).cloned(),
    })
}

pub(super) fn update_binding_expression(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    binding: &Id,
    span: Span,
) -> Result<SelectedValue, RemainderDisposition> {
    let result = context.read_binding(binding, frame)?;
    let operand = absorb(frame, result);
    if operand.values.is_empty() {
        return Ok(operand);
    }
    let Some(result) = allocation_transfer::update_result(&operand.values) else {
        frame.ledger.allocation.compose(&AllocationJudgment::Opaque);
        frame
            .ledger
            .effects
            .compose(&EffectCoverageFragment::default());
        return Err(unproved_operator(context, frame, &operand.values, span));
    };
    absorb_primitive_transfer(frame, &result);
    let selected = completing_selected(SelectedValue::raw(result.values), frame);
    store_binding(context, body, frame, binding, selected, span)
}

pub(super) fn bind_parameter(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
    binding: &Id,
    selected: SelectedValue,
) {
    if selected.values.is_empty() {
        frame.stop_normal_path();
        return;
    }
    let selection = selected
        .selection
        .filter(|selection| selection.matches(&selected.values));
    frame.inputs.extend(selected.values.iter().cloned());
    frame.environment.insert(binding.clone(), selected.values);
    match selection {
        Some(selection) => {
            frame.binding_selections.insert(binding.clone(), selection);
        }
        None => {
            frame.binding_selections.remove(binding);
        }
    }
    cells::declared_binding_stored(context, body, binding, frame);
    frame.initialized_bindings.insert(binding.clone());
    let effects = cells::declarative_binding_operation(context, body, binding);
    frame.ledger.effects.compose(&effects);
}

pub(super) fn prepare_body_bindings(
    context: &mut impl KernelContext,
    body: &LocalBody,
    frame: &mut LocalFrame,
) {
    for (binding, kind) in &body.bindings {
        match kind {
            LocalBinding::Parameter => {}
            LocalBinding::NamedSelf(function) | LocalBinding::HoistedFunction(function) => {
                // A pre-prologue parameter/var value is not the hoisted
                // function. Preserve only an already-established exact value;
                // an unsupported activation value remains absent until use.
                if frame.environment.get(binding).is_some_and(|values| {
                    values.is_empty()
                        || values.iter().any(|value| {
                            !matches!(value,
                                AbstractValue::Closure(HeapAddress { callable: key, .. }, _)
                                | AbstractValue::Value(Value::Alloc(Kind::Function, key))
                                    if key == function)
                        })
                }) {
                    frame.environment.remove(binding);
                    frame.binding_selections.remove(binding);
                }
            }
            LocalBinding::Var => {
                let initialized = frame
                    .environment
                    .entry(binding.clone())
                    .or_insert_with(|| Values::from([AbstractValue::UNDEFINED]));
                if frame
                    .binding_selections
                    .get(binding)
                    .is_some_and(|selection| !selection.matches(initialized))
                {
                    frame.binding_selections.remove(binding);
                }
            }
            LocalBinding::Let | LocalBinding::Const => {
                frame.environment.insert(
                    binding.clone(),
                    Values::from([AbstractValue::Value(Value::Unavailable)]),
                );
                frame.binding_selections.remove(binding);
            }
        }
    }
    cells::instantiate_body_cells(context, body, frame);
    for (binding, kind) in &body.bindings {
        if *kind == LocalBinding::Parameter && frame.initialized_bindings.contains(binding) {
            continue;
        }
        // Let/const cells have actually been created in TDZ above. Hoisted
        // values and compatibility parameters retain their separate boundary.
        let effects = if matches!(kind, LocalBinding::Let | LocalBinding::Const) {
            cells::declarative_binding_operation(context, body, binding)
        } else {
            EffectCoverageFragment::default()
        };
        frame.ledger.effects.compose(&effects);
    }
}
