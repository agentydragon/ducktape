//! Native array operations over an uninterrupted literal's descriptor state.

use super::effect_coverage::EffectCoverageFragment;
use super::value_model::{AbstractValue, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::EffectCell;
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;

pub(super) trait ArrayContext {
    /// Exact length, writable own length, extensibility, original prototype,
    /// and no own `push`. Reused allocation sites and descriptor changes lose
    /// this witness even if their pooled element values are still available.
    fn writable_array_length(&self, object: u32) -> Option<u32>;
    /// Records the prototype check it makes as an admission fact.
    fn array_prototypes_intact(&mut self) -> bool;
    fn array_dependencies(&self) -> ProofDependencies;
    fn append_primitives(&mut self, object: u32, length: u32, arguments: &[Values]);
}

/// Captured GetValue identity, before argument evaluation. Body admission
/// checks the receiver and indexed-setter protocol again after the arguments.
#[derive(Clone)]
pub(super) struct ArrayPushSelection {
    pub(super) object: u32,
    dependencies: ProofDependencies,
}

pub(super) fn select_push(
    context: &mut impl ArrayContext,
    receivers: &Values,
    property: &str,
) -> Option<(ArrayPushSelection, EffectCoverageFragment)> {
    let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
        receivers.iter().collect::<Vec<_>>().as_slice()
    else {
        return None;
    };
    if property != "push" || !context.array_prototypes_intact() {
        return None;
    }
    context.writable_array_length(*object)?;
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
    if !context.array_prototypes_intact()
        || arguments
            .iter()
            .any(|values| values.is_empty() || !values.iter().all(AbstractValue::is_primitive))
    {
        return None;
    }
    let object = selection.object;
    let length = context.writable_array_length(object)?;
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
