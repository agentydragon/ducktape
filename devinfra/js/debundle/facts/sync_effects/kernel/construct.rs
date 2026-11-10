//! Base class construction: one kernel rule for every adapter. The adapter
//! admits the class and names the fresh instance; the kernel runs the lowered
//! class body (instance fields, then the constructor's statements) against
//! that receiver and selects the result.

use super::super::effect_coverage::EffectCoverageFragment;
use super::super::value_model::{AbstractValue, Values};
use super::context::{Disposition, KernelContext, Unproved};
use super::frame::{LocalFrame, RemainderDisposition};
use crate::facts::abstract_domain::value::{Kind, Value};
use swc_common::Span;

/// Bind the activation of a base constructor of `class` to its receiver:
/// `this` is the fresh instance the adapter named for this construction.
pub(in crate::facts::sync_effects) fn bind_receiver(
    frame: &mut LocalFrame,
    class: u32,
    instance: Values,
) {
    frame.inputs.extend(instance.iter().cloned());
    frame.this_values = Some(instance);
    frame.constructed_class = Some(class);
}

/// [[Construct]] step 11: a constructor returning an object replaces the
/// instance. A primitive or undefined return keeps it. An object return is
/// outside this rule; the caller refuses it. `Unavailable` (an abrupt path)
/// is kept beside the instance.
pub(in crate::facts::sync_effects) fn construct_result(
    returns: &Values,
    instance: &Values,
) -> Option<Values> {
    let mut result = Values::new();
    for value in returns {
        match value {
            AbstractValue::Value(Value::Unavailable) => {
                result.insert(value.clone());
            }
            value if value.is_primitive() => result.extend(instance.iter().cloned()),
            _ => return None,
        }
    }
    Some(result)
}

/// The receiver of a field definition or constructor store: the one fresh
/// instance this activation constructs.
fn fresh_receiver(frame: &LocalFrame) -> Option<&AbstractValue> {
    let this_values = frame.this_values.as_ref()?;
    let [receiver] = this_values.iter().collect::<Vec<_>>()[..] else {
        return None;
    };
    matches!(
        receiver,
        AbstractValue::Value(Value::Alloc(Kind::Object, _)) | AbstractValue::Instance(..)
    )
    .then_some(receiver)
}

/// DefineField / PrivateFieldAdd on the fresh instance: an own data property
/// of an object no other code holds yet, so no user code runs and no exterior
/// state changes. An adapter instance identity keeps its own retention model;
/// only a kernel-allocated record retains the value in the literal heap.
pub(in crate::facts::sync_effects) fn define_field(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    property: &str,
    values: &Values,
) -> Result<(), RemainderDisposition> {
    if values.contains(&AbstractValue::Value(Value::Unavailable)) {
        return Err(RemainderDisposition::NeedsSummary);
    }
    let Some(receiver) = fresh_receiver(frame) else {
        return Err(RemainderDisposition::NeedsSummary);
    };
    if let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = receiver {
        // Constructor summaries use this heap entry only to walk values
        // retained by the fresh instance. `ordinary_record_shape` remains
        // the prerequisite for reads, so class slots never become
        // ordinary data fields by being recorded here.
        let object = *object;
        context.world_mut().literal_heap().write_property(
            object,
            Some(property.to_owned()),
            values,
        );
    }
    frame
        .ledger
        .effects
        .compose(&EffectCoverageFragment::no_effects());
    Ok(())
}

/// `this.property = value` in a base constructor is an ordinary [[Set]]. On
/// the fresh instance it creates or overwrites an own data property unless the
/// prototype chain holds an accessor or a non-writable property of that name;
/// the adapter proves that absence, otherwise the store is unproved.
pub(in crate::facts::sync_effects) fn assign_field(
    context: &mut impl KernelContext,
    frame: &mut LocalFrame,
    property: &str,
    values: &Values,
    span: Span,
) -> Result<(), RemainderDisposition> {
    let absence = frame
        .constructed_class
        .and_then(|class| context.constructor_assignment_absence(class, property));
    let Some(dependencies) = absence else {
        let receivers = frame.this_values.clone().unwrap_or_default();
        return Err(
            match context.unproved(
                frame,
                Unproved::FieldWrite {
                    receivers: &receivers,
                    values,
                    span,
                },
            ) {
                Disposition::Summarized => RemainderDisposition::AlreadySummarized,
                Disposition::Resolved(_) | Disposition::Refused => {
                    RemainderDisposition::NeedsSummary
                }
            },
        );
    };
    define_field(context, frame, property, values)?;
    frame.ledger.effects.depend_on(&dependencies);
    Ok(())
}
