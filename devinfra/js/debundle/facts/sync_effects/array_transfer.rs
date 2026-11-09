//! Native array operations over an uninterrupted literal's descriptor state.

use super::effect_coverage::EffectCoverageFragment;
use super::own_data_transfer::OwnDataContext;
use super::value_model::{AbstractValue, CallSite, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::OrderedCallAdmissionRefusal;
use crate::facts::EffectCell;
use crate::facts::abstract_domain::builtins::{
    ARRAY_PROTOTYPE_SLOTS, ArgSel, IterationResult, IterationRow,
};
use crate::facts::abstract_domain::integrity::IntrinsicSlot;
use crate::facts::abstract_domain::value::{HeapAddress, Kind, Value};
use analysis_model::PrimKinds;

pub(super) trait ArrayContext: OwnDataContext {
    /// Whether this array still gets `property` from its original
    /// `%Array.prototype%` at the method GetValue prefix. This witness is
    /// checked before arguments, independently of the later receiver-shape
    /// and species checks.
    fn array_method_selected_from_original_prototype(
        &self,
        object: HeapAddress,
        property: &str,
    ) -> bool;
    /// Exact length, writable own length, extensibility, original prototype,
    /// and no named own overrides. Reused allocation sites and descriptor changes lose
    /// this witness even if their pooled element values are still available.
    fn literal_array_length(&self, object: HeapAddress, absent: &[&str]) -> Option<u32>;
    fn array_slots_intact(&self, slots: &[IntrinsicSlot]) -> bool;
    fn dense_array_slots(&self, object: HeapAddress) -> Option<Vec<Values>>;
    fn array_dependencies(&self) -> ProofDependencies;
    fn append_primitives(&mut self, object: HeapAddress, length: u32, arguments: &[Values]);
}

/// Captured GetValue identity, before argument evaluation. Body admission
/// checks the receiver and indexed-setter protocol again after the arguments.
#[derive(Clone)]
pub(super) struct ArrayPushSelection {
    pub(super) object: HeapAddress,
    dependencies: ProofDependencies,
}

#[derive(Clone)]
pub(super) struct ArrayIterationSelection {
    pub(super) object: HeapAddress,
    pub(super) site: CallSite,
    pub(super) row: &'static IterationRow,
    /// Fresh result identity, present only for rows that create an array.
    pub(super) result: Option<HeapAddress>,
    /// Qualified operation identity used to join `reduce` accumulator values
    /// across repeated execution of the same prepared operation.
    pub(super) accumulator: Option<HeapAddress>,
    pub(super) dependencies: ProofDependencies,
}

#[derive(Clone)]
pub(super) enum ArrayMethodSelection {
    Push(ArrayPushSelection),
    Iteration(ArrayIterationSelection),
}

impl ArrayMethodSelection {
    pub(super) fn object(&self) -> HeapAddress {
        match self {
            Self::Push(selection) => selection.object,
            Self::Iteration(selection) => selection.object,
        }
    }
}

pub(super) fn select_push(
    context: &impl ArrayContext,
    receivers: &Values,
    property: &str,
) -> Option<(ArrayPushSelection, EffectCoverageFragment)> {
    let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
        receivers.iter().collect::<Vec<_>>().as_slice()
    else {
        return None;
    };
    if property != "push" || !context.array_slots_intact(ARRAY_PROTOTYPE_SLOTS) {
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

pub(super) fn select_iteration(
    context: &impl ArrayContext,
    receivers: &Values,
    site: CallSite,
    row: &'static IterationRow,
    result: Option<HeapAddress>,
) -> Result<(ArrayIterationSelection, EffectCoverageFragment), OrderedCallAdmissionRefusal> {
    let mut receiver_values = receivers.iter();
    let receiver = receiver_values
        .next()
        .ok_or(OrderedCallAdmissionRefusal::ArrayIterationReceiverNotSingleton)?;
    if receiver_values.next().is_some() {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationReceiverNotSingleton);
    }
    let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = receiver else {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationReceiverNotObject);
    };
    if !matches!(row.invokes, ArgSel::Arg(0))
        || (row.result == IterationResult::Boolean && !matches!(row.member, "some" | "every"))
    {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationRowShapeUnsupported);
    }
    // The method and own-property shadow check happen before its arguments.
    // Receiver descriptors and result-array species are body premises and
    // are rechecked by `iteration_snapshot` after operand evaluation.
    let method_slots: Vec<_> = row
        .requires
        .iter()
        .filter(|slot| {
            matches!(slot,
                IntrinsicSlot::PrototypeProperty(_, member) if *member == row.member)
        })
        .copied()
        .collect();
    if method_slots.is_empty() {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationMethodSlotUnavailable);
    }
    if !context.array_slots_intact(&method_slots) {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationMethodSlotUnavailable);
    }
    if !context.array_method_selected_from_original_prototype(*object, row.member) {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationOriginalMethodSelectionUnavailable);
    }
    let result_identity = result;
    let result = match row.result {
        IterationResult::Mapped | IterationResult::Filtered | IterationResult::FlatMapped => Some(
            result_identity
                .ok_or(OrderedCallAdmissionRefusal::ArrayIterationResultAllocationUnavailable)?,
        ),
        IterationResult::Undefined
        | IterationResult::Boolean
        | IterationResult::Element
        | IterationResult::Index
        | IterationResult::Accumulated => None,
    };
    let accumulator = if row.result == IterationResult::Accumulated {
        Some(
            result_identity
                .ok_or(OrderedCallAdmissionRefusal::ArrayIterationResultAllocationUnavailable)?,
        )
    } else {
        None
    };
    let dependencies = context.array_dependencies();
    let mut effects = EffectCoverageFragment::no_effects();
    effects.depend_on(&dependencies);
    Ok((
        ArrayIterationSelection {
            object: *object,
            site,
            row,
            result,
            accumulator,
            dependencies,
        },
        effects,
    ))
}

/// GetValue already selected the method. Only the mutable receiver and
/// ArraySpeciesCreate protocol are consulted after argument evaluation.
pub(super) fn iteration_snapshot(
    context: &impl ArrayContext,
    selection: &ArrayIterationSelection,
) -> Result<(Vec<Values>, ProofDependencies), OrderedCallAdmissionRefusal> {
    let required: Vec<_> = selection
        .row
        .requires
        .iter()
        .filter(|slot| {
            !matches!(slot,
                IntrinsicSlot::PrototypeProperty(_, member) if *member == selection.row.member)
        })
        .copied()
        .collect();
    if !context.array_slots_intact(&required) {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationBodyIntrinsicsUnavailable);
    }
    let absent = if selection.result.is_some() {
        &["constructor"][..]
    } else {
        &[][..]
    };
    let length = context
        .literal_array_length(selection.object, absent)
        .ok_or(OrderedCallAdmissionRefusal::ArrayIterationReceiverDescriptorUnavailable)?;
    let slots = context
        .dense_array_slots(selection.object)
        .ok_or(OrderedCallAdmissionRefusal::ArrayIterationDenseSnapshotUnavailable)?;
    let length = usize::try_from(length)
        .map_err(|_| OrderedCallAdmissionRefusal::ArrayIterationLengthUnrepresentable)?;
    if length != slots.len() {
        return Err(OrderedCallAdmissionRefusal::ArrayIterationLengthMismatch);
    }
    let mut dependencies = selection.dependencies.clone();
    dependencies.extend(&context.array_dependencies());
    Ok((slots, dependencies))
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
