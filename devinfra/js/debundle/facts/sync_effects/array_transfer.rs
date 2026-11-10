//! Native array operations over an uninterrupted literal's descriptor state.

use super::effect_coverage::EffectCoverageFragment;
use super::value_model::{AbstractValue, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::OrderedCallAdmissionRefusal;
use crate::facts::EffectCell;
use crate::facts::abstract_domain::builtins::{
    ARRAY_PROTOTYPE_SLOTS, ITERATION_ROWS, IterationResult, IterationRow,
};
use crate::facts::abstract_domain::integrity::IntrinsicSlot;
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;

pub(super) trait ArrayContext {
    /// Exact length, writable own length, extensibility, original prototype,
    /// and none of the `absent` own properties. Reused allocation sites and
    /// descriptor changes lose this witness even if their pooled element
    /// values are still available.
    fn literal_array_length(&self, object: u32, absent: &[&str]) -> Option<u32>;
    /// Records the slot check it makes as an admission fact.
    fn array_slots_intact(&mut self, slots: &[IntrinsicSlot]) -> bool;
    /// The exact element values of a settled dense array without holes.
    fn dense_array_slots(&self, object: u32) -> Option<Vec<Values>>;
    fn array_dependencies(&self) -> ProofDependencies;
    fn append_primitives(&mut self, object: u32, length: u32, arguments: &[Values]);
}

/// An `%Array.prototype%` method selected by GetValue on a literal array,
/// before any argument runs.
#[derive(Clone)]
pub(super) enum ArrayMethodSelection {
    Push(ArrayPushSelection),
    Iteration(ArrayIterationSelection),
}

impl ArrayMethodSelection {
    pub(super) fn object(&self) -> u32 {
        match self {
            Self::Push(selection) => selection.object,
            Self::Iteration(selection) => selection.object,
        }
    }

    pub(super) fn property(&self) -> &'static str {
        match self {
            Self::Push(_) => "push",
            Self::Iteration(selection) => selection.row.member,
        }
    }
}

/// The original iteration method, selected from the intact prototype slot of
/// an array with no own property of that name. The body rechecks the
/// receiver and the rest of the row's slots after the arguments.
#[derive(Clone)]
pub(super) struct ArrayIterationSelection {
    pub(super) object: u32,
    pub(super) row: &'static IterationRow,
    dependencies: ProofDependencies,
}

/// Select the original `Array.prototype` method `property` on this receiver.
pub(super) fn select_method(
    context: &mut impl ArrayContext,
    receivers: &Values,
    property: &str,
) -> Option<(ArrayMethodSelection, EffectCoverageFragment)> {
    if property == "push" {
        let (selection, effects) = select_push(context, receivers)?;
        return Some((ArrayMethodSelection::Push(selection), effects));
    }
    let row = ITERATION_ROWS.iter().find(|row| row.member == property)?;
    let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
        receivers.iter().collect::<Vec<_>>().as_slice()
    else {
        return None;
    };
    let method_slots: Vec<_> = row
        .requires
        .iter()
        .filter(|slot| matches!(slot, IntrinsicSlot::PrototypeProperty(_, member) if *member == row.member))
        .copied()
        .collect();
    if method_slots.is_empty() || !context.array_slots_intact(&method_slots) {
        return None;
    }
    context.literal_array_length(*object, &[row.member])?;
    let dependencies = context.array_dependencies();
    let mut effects = EffectCoverageFragment::no_effects();
    effects.depend_on(&dependencies);
    Some((
        ArrayMethodSelection::Iteration(ArrayIterationSelection {
            object: *object,
            row,
            dependencies,
        }),
        effects,
    ))
}

/// The receiver's elements as the selected method reads them, after the
/// arguments ran: the rest of the row's slots (the element reads and
/// `ArraySpeciesCreate`) are intact, the receiver has its exact own length
/// and no own `constructor` where a species lookup reads one, and every
/// index below the length is a settled own data slot.
pub(super) fn iteration_snapshot(
    context: &mut impl ArrayContext,
    selection: &ArrayIterationSelection,
) -> Result<(Vec<Values>, ProofDependencies), OrderedCallAdmissionRefusal> {
    if !context.array_slots_intact(selection.row.requires) {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationIntrinsicsChanged);
    }
    let absent: &[&str] = match selection.row.result {
        IterationResult::Mapped | IterationResult::Filtered | IterationResult::FlatMapped => {
            &["constructor"]
        }
        _ => &[],
    };
    let length = context
        .literal_array_length(selection.object, absent)
        .ok_or(OrderedCallAdmissionRefusal::ArrayIterationReceiverUnproved)?;
    let slots = context
        .dense_array_slots(selection.object)
        .ok_or(OrderedCallAdmissionRefusal::ArrayIterationReceiverUnproved)?;
    if usize::try_from(length).ok() != Some(slots.len()) {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationReceiverUnproved);
    }
    let mut dependencies = selection.dependencies.clone();
    dependencies.extend(&context.array_dependencies());
    Ok((slots, dependencies))
}

/// Captured GetValue identity, before argument evaluation. Body admission
/// checks the receiver and indexed-setter protocol again after the arguments.
#[derive(Clone)]
pub(super) struct ArrayPushSelection {
    pub(super) object: u32,
    dependencies: ProofDependencies,
}

fn select_push(
    context: &mut impl ArrayContext,
    receivers: &Values,
) -> Option<(ArrayPushSelection, EffectCoverageFragment)> {
    let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
        receivers.iter().collect::<Vec<_>>().as_slice()
    else {
        return None;
    };
    if !context.array_slots_intact(ARRAY_PROTOTYPE_SLOTS) {
        return None;
    }
    context.literal_array_length(*object, &["push"])?;
    let dependencies = context.array_dependencies();
    let mut effects = EffectCoverageFragment::no_effects();
    effects.depend_on(&dependencies);
    Some((
        ArrayPushSelection {
            object: *object,
            dependencies,
        },
        effects,
    ))
}

pub(super) struct ArrayPushResult {
    pub(super) values: Values,
    pub(super) effects: EffectCoverageFragment,
    pub(super) reads: Vec<EffectCell>,
    pub(super) writes: Vec<EffectCell>,
    pub(super) dependencies: ProofDependencies,
}

pub(super) fn push(
    context: &mut impl ArrayContext,
    selection: &ArrayPushSelection,
    arguments: &[Values],
) -> Option<ArrayPushResult> {
    if !context.array_slots_intact(ARRAY_PROTOTYPE_SLOTS)
        || arguments
            .iter()
            .any(|values| values.is_empty() || !values.iter().all(AbstractValue::is_primitive))
    {
        return None;
    }
    let object = selection.object;
    let length = context.literal_array_length(object, &["push"])?;
    let count = u32::try_from(arguments.len()).ok()?;
    let new_length = length.checked_add(count)?;
    let mut dependencies = selection.dependencies.clone();
    dependencies.extend(&context.array_dependencies());
    let mut effects = EffectCoverageFragment::no_effects();
    effects.depend_on(&dependencies);
    let length_cell = EffectCell::OwnProperty {
        origin: object,
        property: "length".into(),
    };
    let reads = vec![EffectCell::ObjectShape(object), length_cell.clone()];
    let mut writes = vec![
        EffectCell::ArrayContents(object),
        EffectCell::ObjectShape(object),
        length_cell,
    ];
    writes.extend((length..new_length).map(|index| EffectCell::OwnProperty {
        origin: object,
        property: index.to_string(),
    }));
    // No exact element snapshot survives mutation. Descriptor/length state is
    // independent, so a later checked push may still complete normally.
    context.append_primitives(object, new_length, arguments);
    Some(ArrayPushResult {
        values: Values::from([AbstractValue::Value(Value::Prim(PrimKinds::NUMBER))]),
        effects,
        reads,
        writes,
        dependencies,
    })
}
