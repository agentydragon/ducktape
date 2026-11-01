//! Shared value transfers for closed inputs and literal allocations.
//!
//! A covered result additionally requires complete canonical source coverage.
//! Lowered values alone can have erased calls, writes or coercions.

use super::value_model::{AbstractValue, Context, DenseArray, ProviderScopeId, ValueExpr, Values};
use crate::analysis_inputs::ProofDependencies;
use crate::facts::abstract_domain::value::{Kind, Value};
use imbl::OrdMap;
use std::collections::BTreeMap;
use swc_ecma_ast::Id;

#[derive(Clone)]
pub(super) enum AllocationJudgment {
    Opaque,
    /// No external access, write, publication, invocation or throwing operation.
    CoveredAllocationOnly(ProofDependencies),
}

impl AllocationJudgment {
    pub(super) fn compose(&mut self, other: &Self) {
        match (&mut *self, other) {
            (Self::CoveredAllocationOnly(current), Self::CoveredAllocationOnly(next)) => {
                current.extend(next);
            }
            _ => *self = Self::Opaque,
        }
    }
}

pub(super) struct AllocationValue {
    pub(super) values: Values,
    pub(super) judgment: AllocationJudgment,
}

impl AllocationValue {
    pub(super) fn opaque(values: Values) -> Self {
        Self {
            values,
            judgment: AllocationJudgment::Opaque,
        }
    }
}

pub(super) enum LiteralLayout {
    Record,
    DenseArray(Vec<ValueExpr>),
    Unsupported,
}

pub(super) trait AllocationContext {
    /// Source coverage is necessary because ValueExpr may erase syntax.
    fn source_covered(&self) -> bool;
    /// Resolve an actual closed input, or return opaque access evidence.
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue>;
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue>;
    fn literal_layout(&self, allocation: u32) -> LiteralLayout;
    fn heap(&mut self) -> LiteralHeap<'_>;
}

/// The real shared allocation store; neither consumer supplies app/native facts.
pub(super) struct LiteralHeap<'a> {
    pub(super) properties: &'a mut OrdMap<u32, BTreeMap<Option<String>, Values>>,
    pub(super) arrays: &'a mut OrdMap<u32, DenseArray>,
    pub(super) revision: &'a mut u64,
}

impl LiteralHeap<'_> {
    pub(super) fn record(&mut self, allocation: u32, properties: BTreeMap<Option<String>, Values>) {
        let current = self.properties.entry(allocation).or_default();
        let mut changed = false;
        for (property, values) in properties {
            let slot = current.entry(property).or_default();
            let before = slot.len();
            slot.extend(values);
            changed |= slot.len() != before;
        }
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn dense_array(&mut self, allocation: u32, slots: Vec<Values>) {
        let before = self.properties.get(&allocation).cloned();
        let dense = self.arrays.entry(allocation).or_insert_with(|| DenseArray {
            slots: slots.clone(),
        });
        let length = dense.slots.len().max(slots.len());
        dense
            .slots
            .resize_with(length, || Values::from([AbstractValue::UNDEFINED]));
        for (index, current) in dense.slots.iter_mut().enumerate() {
            if let Some(values) = slots.get(index) {
                current.extend(values.iter().cloned());
            } else {
                current.insert(AbstractValue::UNDEFINED);
            }
        }
        let properties = self.properties.entry(allocation).or_default();
        properties
            .entry(Some("length".into()))
            .or_default()
            .insert(AbstractValue::PRIMITIVE);
        for (index, values) in dense.slots.iter().enumerate() {
            properties
                .entry(Some(index.to_string()))
                .or_default()
                .extend(values.iter().cloned());
            properties
                .entry(None)
                .or_default()
                .extend(values.iter().cloned());
        }
        if before.as_ref() != Some(properties) {
            *self.revision = self.revision.wrapping_add(1);
        }
    }
}

/// One semantic implementation. A strict consumer delegates children back here;
/// application execution dispatches children through the normal runtime queries.
pub(super) fn transfer(
    context: &mut impl AllocationContext,
    expression: &ValueExpr,
) -> Option<AllocationValue> {
    let mut judgment = if context.source_covered() {
        AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default())
    } else {
        AllocationJudgment::Opaque
    };
    let values = match expression {
        ValueExpr::Binding(binding) => {
            let mut value = context.binding(binding)?;
            value.judgment.compose(&judgment);
            return Some(value);
        }
        ValueExpr::Undefined => Values::from([AbstractValue::UNDEFINED]),
        ValueExpr::NonCallable(kinds) => Values::from([AbstractValue::Value(Value::Prim(*kinds))]),
        ValueExpr::ObjectLiteral(allocation, properties) => {
            let layout = context.literal_layout(*allocation);
            let mut may_throw = false;
            let mut can_complete = true;
            match layout {
                LiteralLayout::Record => return record(context, *allocation, properties, judgment),
                LiteralLayout::DenseArray(elements) => {
                    let mut slots = Vec::new();
                    let mut containment = Values::new();
                    for expression in elements {
                        if !can_complete {
                            break;
                        }
                        let child = context.child(&expression)?;
                        judgment.compose(&child.judgment);
                        let mut values = child.values;
                        may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
                        if values.is_empty() {
                            can_complete = false;
                            continue;
                        }
                        containment.extend(values.iter().cloned());
                        slots.push(values);
                    }
                    if can_complete {
                        let mut heap = context.heap();
                        // Match the ordinary array lane: containment is filled
                        // before dense_array observes and versions the slots.
                        heap.properties
                            .entry(*allocation)
                            .or_default()
                            .entry(None)
                            .or_default()
                            .extend(containment);
                        heap.dense_array(*allocation, slots);
                    }
                }
                _ => return None,
            }
            let mut values = Values::new();
            if can_complete {
                values.insert(AbstractValue::Value(Value::Alloc(
                    Kind::Object,
                    *allocation,
                )));
            }
            if may_throw || !can_complete {
                judgment = AllocationJudgment::Opaque;
                if may_throw {
                    values.insert(AbstractValue::Value(Value::Unavailable));
                }
            }
            values
        }
        _ => return None,
    };
    Some(AllocationValue { values, judgment })
}

/// Shared record evaluation, including the application's opaque-key fallback.
pub(super) fn record(
    context: &mut impl AllocationContext,
    allocation: u32,
    properties: &[(Option<String>, ValueExpr)],
    mut judgment: AllocationJudgment,
) -> Option<AllocationValue> {
    let mut evaluated = BTreeMap::<Option<String>, Values>::new();
    let mut may_throw = false;
    let mut can_complete = true;
    for (property, expression) in properties {
        let child = context.child(expression)?;
        judgment.compose(&child.judgment);
        let mut values = child.values;
        if values.remove(&AbstractValue::Value(Value::Unavailable)) {
            may_throw = true;
            can_complete &= !values.is_empty();
        }
        evaluated
            .entry(property.clone())
            .or_default()
            .extend(values);
    }
    if can_complete {
        context.heap().record(allocation, evaluated);
    }
    let mut values = Values::new();
    if can_complete {
        values.insert(AbstractValue::Value(Value::Alloc(Kind::Object, allocation)));
    }
    if may_throw || !can_complete {
        judgment = AllocationJudgment::Opaque;
        values.insert(AbstractValue::Value(Value::Unavailable));
    }
    Some(AllocationValue { values, judgment })
}

pub(super) fn closed_binding(
    environment: &Context,
    binding: &Id,
    scope: ProviderScopeId,
) -> Option<AllocationValue> {
    let values = environment.get(binding)?;
    if values.is_empty()
        || !values.iter().all(
            |value| matches!(value, AbstractValue::SymbolicInput(input) if input.scope == scope),
        )
    {
        return None;
    }
    Some(AllocationValue {
        values: values.clone(),
        judgment: AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default()),
    })
}
