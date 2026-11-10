//! Path selection for conditional and logical operators: which paths an
//! abstract test value leaves feasible, and what it is on each of them.

use super::super::effect_coverage::EffectCoverageFragment;
use super::super::value_model::{AbstractValue, Values};
use super::frame::LocalFrame;
use crate::facts::abstract_domain::value::Value;
use analysis_model::PrimKinds;

/// `ToBoolean` and the nullish test read no property and run no user code.
/// The operand supplied its own receipt when it ran.
pub(super) fn test_selected(frame: &mut LocalFrame) {
    frame
        .ledger
        .effects
        .compose(&EffectCoverageFragment::no_effects());
}

/// The parts of an operand's normal values that are truthy and falsy. A
/// primitive kind set keeps no literal, so every kind but `null`, `undefined`
/// (always falsy) and symbols (always truthy) is on both sides. Objects and
/// functions are truthy; a value of unknown kind is on both sides.
pub(in crate::facts::sync_effects) fn split_truthiness(values: &Values) -> (Values, Values) {
    let mut truthy = Values::new();
    let mut falsy = Values::new();
    for value in values {
        match value {
            AbstractValue::Value(Value::Prim(kinds)) => {
                let yes = kinds.without(PrimKinds::NULL.union(PrimKinds::UNDEFINED));
                let no = kinds.without(PrimKinds::SYMBOL);
                if !yes.is_empty() {
                    truthy.insert(AbstractValue::Value(Value::Prim(yes)));
                }
                if !no.is_empty() {
                    falsy.insert(AbstractValue::Value(Value::Prim(no)));
                }
            }
            _ if is_object(value) => {
                truthy.insert(value.clone());
            }
            _ => {
                truthy.insert(value.clone());
                falsy.insert(value.clone());
            }
        }
    }
    (truthy, falsy)
}

/// The non-nullish part of an operand's normal values, and whether some
/// value may be `null` or `undefined`.
pub(super) fn split_nullish(values: &Values) -> (Values, bool) {
    let nullish = PrimKinds::NULL.union(PrimKinds::UNDEFINED);
    let mut retained = Values::new();
    let mut may_be_nullish = false;
    for value in values {
        match value {
            AbstractValue::Value(Value::Prim(kinds)) => {
                may_be_nullish |= kinds.intersects(nullish);
                let kinds = kinds.without(nullish);
                if !kinds.is_empty() {
                    retained.insert(AbstractValue::Value(Value::Prim(kinds)));
                }
            }
            _ if is_object(value) => {
                retained.insert(value.clone());
            }
            _ => {
                may_be_nullish = true;
                retained.insert(value.clone());
            }
        }
    }
    (retained, may_be_nullish)
}

/// Values whose every runtime denotation is an object. Carriers that only
/// summarize what a value may retain prove no kind.
fn is_object(value: &AbstractValue) -> bool {
    match value {
        AbstractValue::Value(Value::Alloc(..))
        | AbstractValue::Closure(..)
        | AbstractValue::ClassMembers(_)
        | AbstractValue::ObjectAssign
        | AbstractValue::ArrayIntrinsic
        | AbstractValue::GlobalObject => true,
        AbstractValue::Value(Value::Prim(_) | Value::Unavailable | Value::Top)
        | AbstractValue::SymbolicInput(_)
        | AbstractValue::Instance(..)
        | AbstractValue::CallbackFactory
        | AbstractValue::InlineCallbackFactory
        | AbstractValue::DeferredCallbacks(_)
        | AbstractValue::AuditedRetainedResult(..)
        | AbstractValue::OwnDescriptorMap(_)
        | AbstractValue::ForeignDeferredRecord => false,
    }
}

#[cfg(test)]
mod tests {
    use super::{split_nullish, split_truthiness};
    use crate::facts::abstract_domain::value::{Kind, Value};
    use crate::facts::sync_effects::value_model::{AbstractValue, Values};
    use analysis_model::PrimKinds;

    fn prim(kinds: PrimKinds) -> AbstractValue {
        AbstractValue::Value(Value::Prim(kinds))
    }

    #[test]
    fn primitive_kinds_are_refined_per_path() {
        let kinds = PrimKinds::NULL
            .union(PrimKinds::STRING)
            .union(PrimKinds::SYMBOL);
        let (truthy, falsy) = split_truthiness(&Values::from([prim(kinds)]));
        assert_eq!(
            truthy,
            Values::from([prim(PrimKinds::STRING.union(PrimKinds::SYMBOL))])
        );
        assert_eq!(
            falsy,
            Values::from([prim(PrimKinds::NULL.union(PrimKinds::STRING))])
        );

        let (retained, may_be_nullish) = split_nullish(&Values::from([prim(kinds)]));
        assert!(may_be_nullish);
        assert_eq!(
            retained,
            Values::from([prim(PrimKinds::STRING.union(PrimKinds::SYMBOL))])
        );
    }

    #[test]
    fn objects_decide_the_test_and_unknown_values_do_not() {
        let object = AbstractValue::Value(Value::Alloc(Kind::Object, 1));
        let (truthy, falsy) = split_truthiness(&Values::from([object.clone()]));
        assert_eq!(truthy, Values::from([object.clone()]));
        assert!(falsy.is_empty());
        assert_eq!(
            split_nullish(&Values::from([object.clone()])),
            (Values::from([object]), false)
        );

        let unknown = AbstractValue::Value(Value::Top);
        let (truthy, falsy) = split_truthiness(&Values::from([unknown.clone()]));
        assert_eq!(truthy, falsy);
        assert_eq!(
            split_nullish(&Values::from([unknown.clone()])),
            (Values::from([unknown]), true)
        );
    }
}
