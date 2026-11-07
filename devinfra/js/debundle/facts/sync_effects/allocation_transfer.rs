//! Shared value transfers for closed inputs and literal allocations.
//!
//! A covered result additionally requires complete canonical source coverage.
//! Lowered values alone can have erased calls, writes or coercions.

use super::effect_coverage::EffectCoverageFragment;
use super::value_model::{
    AbstractValue, CallSite, Context, DenseArray, LiteralPropertyKind, PropertyDefinition,
    ProviderScopeId, ValueExpr, Values,
};
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::facts::abstract_domain::builtins::{
    ArgSel, IntrinsicOp, Invocation, Return, Row, Throws,
};
use crate::facts::abstract_domain::integrity::IntrinsicSlot;
use crate::facts::abstract_domain::primitive;
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::{BinaryOp, Id};

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
    /// No effect receipt exists for this executed value operation yet. The
    /// allocation channel remains independent, including successful transfers.
    fn unmodeled_effect_operation(&mut self) {}
    /// Resolve an actual closed input, or return opaque access evidence.
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue>;
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue>;
    fn literal_layout(&self, allocation: u32) -> LiteralLayout;
    fn literal_property_kind(&self, allocation: u32, position: usize) -> LiteralPropertyKind;
    fn array_created(&mut self, _allocation: u32, _sparse: bool) {}
    fn heap(&mut self) -> LiteralHeap<'_>;
    /// The adapter proves original constructor identity for this canonical New
    /// expression. The operation's row and result semantics are shared.
    fn keyed_collection_new(
        &mut self,
        _callee: &ValueExpr,
        _arguments: &[ValueExpr],
        _site: CallSite,
    ) -> Option<AllocationValue> {
        None
    }
}

/// Transfer a binary operation over values whose kinds are already known to
/// be primitive. An object or unknown value declines so the caller can use its
/// application fallback after evaluating both operands exactly once.
pub(super) fn binary(
    op: BinaryOp,
    left: &Values,
    right: &Values,
    mut judgment: AllocationJudgment,
) -> Option<AllocationValue> {
    let primitive_kinds = |values: &Values| {
        values.iter().try_fold(PrimKinds::NONE, |kinds, value| {
            let AbstractValue::Value(Value::Prim(operand)) = value else {
                return None;
            };
            Some(kinds.union(*operand))
        })
    };
    let left_kinds = primitive_kinds(left)?;
    let right_kinds = primitive_kinds(right)?;
    let result_kinds = primitive::binary_result(op, left_kinds, right_kinds)?;
    let may_throw = primitive::binary_throws(op, left_kinds, right_kinds);

    let mut values = Values::new();
    if result_kinds != PrimKinds::NONE {
        values.insert(AbstractValue::Value(Value::Prim(result_kinds)));
    }
    if may_throw || values.is_empty() {
        judgment = AllocationJudgment::Opaque;
        values.insert(AbstractValue::Value(Value::Unavailable));
    }
    Some(AllocationValue { values, judgment })
}

/// Transfer the exact zero-argument keyed-collection constructor row. Provider
/// and application adapters both use this kernel after proving callee identity.
pub(super) struct KeyedCollectionOperationResult {
    pub(super) allocation: AllocationValue,
    /// Normal operation effects only. The caller still owns callee/operand
    /// evaluation and whole-source completion.
    pub(super) effects: EffectCoverageFragment,
}

pub(super) fn keyed_collection_new(
    row: &'static Row,
    site: CallSite,
    dependencies: ProofDependencies,
) -> Option<KeyedCollectionOperationResult> {
    let (family, kind) = match row.op {
        IntrinsicOp::Map => (NativeIntrinsic::Map, Kind::Map),
        IntrinsicOp::Set => (NativeIntrinsic::Set, Kind::Set),
        IntrinsicOp::WeakMap => (NativeIntrinsic::WeakMap, Kind::WeakMap),
        IntrinsicOp::WeakSet => (NativeIntrinsic::WeakSet, Kind::WeakSet),
        _ => return None,
    };
    if row.family() != Some(family)
        || row.invocation != [Invocation::Construct]
        || !row.arity.contains(&0)
        || row.requires != [IntrinsicSlot::Global(family)]
        || row
            .coerces
            .iter()
            .any(|(operand, _)| !matches!(operand, ArgSel::Arg(_)))
        || !row.invokes.is_empty()
        || row.allocates != Some(kind)
        || !row.mutates.is_empty()
        || row.returns != Return::Fresh
        || row.throws != Throws::Never
    {
        return None;
    }
    let effects = EffectCoverageFragment::native_operation(&dependencies);
    Some(KeyedCollectionOperationResult {
        allocation: AllocationValue {
            values: Values::from([AbstractValue::Value(Value::Alloc(
                kind,
                site.allocation_id(),
            ))]),
            judgment: AllocationJudgment::CoveredAllocationOnly(dependencies),
        },
        effects,
    })
}

/// The real shared allocation store; neither consumer supplies app/native facts.
pub(super) struct LiteralHeap<'a> {
    pub(super) properties: &'a mut OrdMap<u32, BTreeMap<Option<String>, Values>>,
    pub(super) arrays: &'a mut OrdMap<u32, DenseArray>,
    pub(super) revision: &'a mut u64,
    pub(super) writable_prefix: Option<&'a mut OrdMap<u32, WritablePrefixState>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum WritablePrefixState {
    InProgress,
    Proven(BTreeSet<String>),
    DenseArray { length: u32 },
    Unknown,
}

impl LiteralHeap<'_> {
    /// A source allocation site can seed writability only on its first
    /// execution and only while its pooled heap has no prior object state.
    /// Reentry, a prior value, or any later execution permanently loses the
    /// per-site proof because the heap does not distinguish allocation
    /// generations.
    pub(super) fn begin_literal(&mut self, allocation: u32) {
        let Some(writable_prefix) = self.writable_prefix.as_deref_mut() else {
            return;
        };
        let next = if !self.properties.contains_key(&allocation)
            && !writable_prefix.contains_key(&allocation)
        {
            WritablePrefixState::InProgress
        } else {
            WritablePrefixState::Unknown
        };
        if writable_prefix.get(&allocation) != Some(&next) {
            writable_prefix.insert(allocation, next);
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn complete_array(&mut self, allocation: u32, length: usize) {
        let Some(writable_prefix) = self.writable_prefix.as_deref_mut() else {
            return;
        };
        if writable_prefix.get(&allocation) == Some(&WritablePrefixState::InProgress) {
            let next = u32::try_from(length)
                .map(|length| WritablePrefixState::DenseArray { length })
                .unwrap_or(WritablePrefixState::Unknown);
            writable_prefix.insert(allocation, next);
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Complete only an uninterrupted first record execution. The sticky
    /// Unknown state is never restored by a later execution of this site.
    pub(super) fn complete_record(
        &mut self,
        allocation: u32,
        writable_properties: BTreeSet<String>,
    ) {
        let Some(writable_prefix) = self.writable_prefix.as_deref_mut() else {
            return;
        };
        if writable_prefix.get(&allocation) == Some(&WritablePrefixState::InProgress) {
            writable_prefix.insert(allocation, WritablePrefixState::Proven(writable_properties));
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn invalidate_writable(&mut self, allocation: u32) {
        let Some(writable_prefix) = self.writable_prefix.as_deref_mut() else {
            return;
        };
        if matches!(
            writable_prefix.get(&allocation),
            Some(
                WritablePrefixState::InProgress
                    | WritablePrefixState::Proven(_)
                    | WritablePrefixState::DenseArray { .. }
            )
        ) {
            writable_prefix.insert(allocation, WritablePrefixState::Unknown);
            *self.revision = self.revision.wrapping_add(1);
        }
    }

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

    /// Allocation sites may represent several records. Field stores retain
    /// earlier values and version the same heap used by ordinary execution.
    pub(super) fn write_property(
        &mut self,
        allocation: u32,
        property: Option<String>,
        values: &Values,
    ) {
        let slot = self
            .properties
            .entry(allocation)
            .or_default()
            .entry(property)
            .or_default();
        let before = slot.len();
        slot.extend(values.iter().cloned());
        if slot.len() != before {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn invalidate_dense_array(&mut self, allocation: u32) {
        if self.arrays.remove(&allocation).is_some() {
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
        ValueExpr::Alloc(Kind::RegExp, allocation) => {
            context.unmodeled_effect_operation();
            // A validated literal evaluates through RegExpCreate without
            // looking up the mutable global binding. Track only its fresh
            // allocation-site identity; prototype and internal-slot behavior
            // stay outside this transfer.
            Values::from([AbstractValue::Value(Value::Alloc(
                Kind::RegExp,
                *allocation,
            ))])
        }
        ValueExpr::New(callee, arguments, site) if context.source_covered() => {
            context.unmodeled_effect_operation();
            return context.keyed_collection_new(callee, arguments, *site);
        }
        ValueExpr::Undefined => {
            context.unmodeled_effect_operation();
            Values::from([AbstractValue::UNDEFINED])
        }
        ValueExpr::NonCallable(kinds) => {
            context.unmodeled_effect_operation();
            Values::from([AbstractValue::Value(Value::Prim(*kinds))])
        }
        ValueExpr::StrictEquality(left, right) => {
            context.unmodeled_effect_operation();
            return primitive_operation(
                context,
                [left.as_ref(), right.as_ref()],
                AbstractValue::Value(Value::Prim(PrimKinds::BOOLEAN)),
                judgment,
            );
        }
        ValueExpr::Void(child) => {
            context.unmodeled_effect_operation();
            return primitive_operation(
                context,
                [child.as_ref()],
                AbstractValue::UNDEFINED,
                judgment,
            );
        }
        ValueExpr::LogicalNot(child) => {
            context.unmodeled_effect_operation();
            return primitive_operation(
                context,
                [child.as_ref()],
                AbstractValue::Value(Value::Prim(PrimKinds::BOOLEAN)),
                judgment,
            );
        }
        ValueExpr::Sequence(expressions) => {
            context.unmodeled_effect_operation();
            return sequence(
                context,
                expressions.iter(),
                judgment,
                |context, expression| context.child(expression),
            );
        }
        ValueExpr::Conditional { test, alternatives } => {
            context.unmodeled_effect_operation();
            // Decide support before evaluating any child: application children
            // have real effects and cannot be rolled back before a fallback.
            if !alternatives.iter().all(|arm| {
                matches!(
                    arm,
                    ValueExpr::Binding(_) | ValueExpr::Undefined | ValueExpr::NonCallable(_)
                )
            }) {
                return None;
            }
            let test = context.child(test)?;
            judgment.compose(&test.judgment);
            let mut tested = test.values;
            let may_throw = tested.remove(&AbstractValue::Value(Value::Unavailable));
            if tested.is_empty() {
                return Some(AllocationValue::opaque(Values::from([
                    AbstractValue::Value(Value::Unavailable),
                ])));
            }
            let mut values = Values::new();
            for arm in alternatives.iter() {
                let arm = context.child(arm)?;
                judgment.compose(&arm.judgment);
                if arm.values.is_empty() {
                    values.insert(AbstractValue::Value(Value::Unavailable));
                }
                values.extend(arm.values);
            }
            if may_throw {
                values.insert(AbstractValue::Value(Value::Unavailable));
            }
            if values.contains(&AbstractValue::Value(Value::Unavailable)) {
                judgment = AllocationJudgment::Opaque;
            }
            values
        }
        ValueExpr::ObjectLiteral(allocation, properties) => {
            context.unmodeled_effect_operation();
            let layout = context.literal_layout(*allocation);
            match layout {
                LiteralLayout::Record => {
                    let definitions = properties
                        .iter()
                        .enumerate()
                        .map(|(position, (name, value))| {
                            PropertyDefinition::new(
                                context.literal_property_kind(*allocation, position),
                                name.clone(),
                                value,
                            )
                        })
                        .collect::<Vec<_>>();
                    return record(
                        context,
                        *allocation,
                        definitions,
                        judgment,
                        |context, expression| context.child(expression),
                    );
                }
                LiteralLayout::DenseArray(elements) => {
                    return array(
                        context,
                        *allocation,
                        elements.iter().map(Some),
                        judgment,
                        |context, expression| context.child(expression),
                    );
                }
                _ => return None,
            }
        }
        _ => {
            context.unmodeled_effect_operation();
            return None;
        }
    };
    Some(AllocationValue { values, judgment })
}

/// Source-ordered composition retains every child's effects and possible
/// abrupt completion even though only the final completing value survives.
pub(super) fn sequence<'a, C, E: 'a>(
    context: &mut C,
    expressions: impl IntoIterator<Item = &'a E>,
    mut judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E) -> Option<AllocationValue>,
) -> Option<AllocationValue> {
    let mut values = Values::from([AbstractValue::UNDEFINED]);
    let mut may_throw = false;
    for expression in expressions {
        let evaluated = child(context, expression)?;
        judgment.compose(&evaluated.judgment);
        values = evaluated.values;
        may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
        if values.is_empty() {
            break;
        }
    }
    if may_throw || values.is_empty() {
        judgment = AllocationJudgment::Opaque;
        values.insert(AbstractValue::Value(Value::Unavailable));
    }
    Some(AllocationValue { values, judgment })
}

/// Noncoercing primitive operations discard operand values, never their access
/// evidence or abrupt outcomes. Later operands run only on a completing path.
fn primitive_operation<'a>(
    context: &mut impl AllocationContext,
    operands: impl IntoIterator<Item = &'a ValueExpr>,
    result: AbstractValue,
    mut judgment: AllocationJudgment,
) -> Option<AllocationValue> {
    let mut may_throw = false;
    let mut can_complete = true;
    for operand in operands {
        let operand = context.child(operand)?;
        judgment.compose(&operand.judgment);
        let mut values = operand.values;
        may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
        if values.is_empty() {
            can_complete = false;
            break;
        }
    }
    let mut values = Values::new();
    if can_complete {
        values.insert(result);
    }
    if may_throw || !can_complete {
        judgment = AllocationJudgment::Opaque;
        values.insert(AbstractValue::Value(Value::Unavailable));
    }
    Some(AllocationValue { values, judgment })
}

/// Shared record evaluation, including the application's opaque-key fallback.
pub(super) fn record<'a, C: AllocationContext, E: 'a>(
    context: &mut C,
    allocation: u32,
    properties: impl IntoIterator<Item = PropertyDefinition<&'a E>>,
    mut judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E) -> Option<AllocationValue>,
) -> Option<AllocationValue> {
    context.unmodeled_effect_operation();
    let properties: Vec<_> = properties.into_iter().collect();
    let mut writable_properties = BTreeSet::new();
    let mut non_writable_properties = BTreeSet::new();
    for definition in &properties {
        let property = definition.name().map(str::to_owned);
        if let Some(property) = property {
            match definition.kind() {
                LiteralPropertyKind::Data | LiteralPropertyKind::Method
                    if !non_writable_properties.contains(&property) =>
                {
                    writable_properties.insert(property);
                }
                _ => {
                    writable_properties.remove(&property);
                    non_writable_properties.insert(property);
                }
            }
        }
    }
    context.heap().begin_literal(allocation);
    let mut evaluated = BTreeMap::<Option<String>, Values>::new();
    let mut may_throw = false;
    let mut can_complete = true;
    for (position, definition) in properties.iter().enumerate() {
        let expected = context.literal_property_kind(allocation, position);
        if expected != LiteralPropertyKind::Opaque && expected != definition.kind() {
            return None;
        }
        let property = definition.name().map(str::to_owned);
        if !can_complete {
            break;
        }
        let child = child(context, *definition.value())?;
        judgment.compose(&child.judgment);
        let mut values = child.values;
        may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
        can_complete &= !values.is_empty();
        // This is descriptor payload containment. Its ordinary-data eligibility
        // remains guarded by the canonical descriptor tag at every read.
        evaluated.entry(property).or_default().extend(values);
    }
    if can_complete {
        let mut heap = context.heap();
        heap.record(allocation, evaluated);
        heap.complete_record(allocation, writable_properties);
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

/// No-spread array creation shares ordered child transfer with local execution.
/// Holes are absent own properties, and never acquire a dense-array proof.
pub(super) fn array<'a, C: AllocationContext, E: 'a>(
    context: &mut C,
    allocation: u32,
    elements: impl IntoIterator<Item = Option<&'a E>>,
    mut judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E) -> Option<AllocationValue>,
) -> Option<AllocationValue> {
    context.unmodeled_effect_operation();
    context.heap().begin_literal(allocation);
    let mut properties = BTreeMap::<Option<String>, Values>::new();
    let mut dense = Vec::new();
    let mut has_hole = false;
    let mut may_throw = false;
    let mut can_complete = true;
    for (index, element) in elements.into_iter().enumerate() {
        if !can_complete {
            break;
        }
        let Some(element) = element else {
            has_hole = true;
            continue;
        };
        let evaluated = child(context, element)?;
        judgment.compose(&evaluated.judgment);
        let mut values = evaluated.values;
        may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
        can_complete &= !values.is_empty();
        properties
            .entry(None)
            .or_default()
            .extend(values.iter().cloned());
        properties.insert(Some(index.to_string()), values.clone());
        dense.push(values);
    }
    if can_complete {
        properties.entry(None).or_default();
        properties.insert(
            Some("length".into()),
            Values::from([AbstractValue::PRIMITIVE]),
        );
        let mut heap = context.heap();
        heap.record(allocation, properties);
        if !has_hole {
            // This ordered literal has no spread operands, so dense.len() is
            // the exact ArrayCreate length for its first uninterrupted run.
            heap.complete_array(allocation, dense.len());
            heap.dense_array(allocation, dense);
        } else {
            heap.invalidate_writable(allocation);
            heap.invalidate_dense_array(allocation);
        }
        context.array_created(allocation, has_hole);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repeated_pooled_literal_site_cannot_restore_writable_prefix() {
        let mut properties = OrdMap::new();
        let mut arrays = OrdMap::new();
        let mut writable_prefix = OrdMap::new();
        let mut revision = 0;
        let allocation = 7;
        let stored = BTreeMap::from([(
            Some("value".to_owned()),
            Values::from([AbstractValue::PRIMITIVE]),
        )]);

        {
            let mut heap = LiteralHeap {
                properties: &mut properties,
                arrays: &mut arrays,
                revision: &mut revision,
                writable_prefix: Some(&mut writable_prefix),
            };
            heap.begin_literal(allocation);
            assert_eq!(
                heap.writable_prefix.as_deref().unwrap().get(&allocation),
                Some(&WritablePrefixState::InProgress)
            );
            heap.record(allocation, stored.clone());
            heap.complete_record(allocation, BTreeSet::from(["value".to_owned()]));
        }
        assert_eq!(
            writable_prefix.get(&allocation),
            Some(&WritablePrefixState::Proven(BTreeSet::from([
                "value".to_owned()
            ])))
        );
        let proven_revision = revision;

        {
            let mut heap = LiteralHeap {
                properties: &mut properties,
                arrays: &mut arrays,
                revision: &mut revision,
                writable_prefix: Some(&mut writable_prefix),
            };
            heap.begin_literal(allocation);
            assert_eq!(
                heap.writable_prefix.as_deref().unwrap().get(&allocation),
                Some(&WritablePrefixState::Unknown)
            );
            heap.complete_record(allocation, BTreeSet::from(["value".to_owned()]));
        }
        assert_eq!(
            writable_prefix.get(&allocation),
            Some(&WritablePrefixState::Unknown)
        );
        assert_eq!(revision, proven_revision + 1);
    }
}
