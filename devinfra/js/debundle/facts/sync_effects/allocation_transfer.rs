//! Shared value transfers for closed inputs and literal allocations.
//!
//! A covered result additionally requires complete canonical source coverage.
//! Lowered values alone can have erased calls, writes or coercions.

use super::effect_coverage::EffectCoverageFragment;
use super::value_model::{
    AbstractValue, CallSite, Context, DenseArray, LiteralPropertyKind, OwnPropertyDescriptorSyntax,
    OwnPropertyShape, PropertyDefinition, ProviderScopeId, ValueExpr, Values,
    ordered_own_property_names,
};
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::facts::EffectCell;
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

#[derive(Clone)]
pub(super) struct OwnDataSpreadSource {
    pub(super) shape: OwnPropertyShape,
    pub(super) properties: BTreeMap<Option<String>, Values>,
    pub(super) enumerability_settled: bool,
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
    /// Exactly one object-literal spread. Mixed records remain unsupported.
    SpreadCopy,
    DenseArray(Vec<ValueExpr>),
    Unsupported,
}

pub(super) trait AllocationContext {
    /// Source coverage is necessary because ValueExpr may erase syntax.
    fn source_covered(&self) -> bool;
    /// No effect receipt exists for this executed value operation yet. The
    /// allocation channel remains independent, including successful transfers.
    fn unmodeled_effect_operation(&mut self) {}
    /// This operation adds no effects or abrupt completion of its own. Its
    /// children still supply their ordered effects and completion evidence.
    fn noninvoking_operation(&mut self) {}
    /// Resolve an actual closed input, or return opaque access evidence.
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue>;
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue>;
    fn literal_layout(&self, allocation: u32) -> LiteralLayout;
    fn literal_property_kind(&self, allocation: u32, position: usize) -> LiteralPropertyKind;
    /// Runtime and syntax evidence for one ordinary own-data source. The
    /// shared spread transfer still checks settlement, descriptors, and all
    /// payload slots before copying.
    fn ordinary_record_shape(&self, _allocation: u32) -> Option<OwnPropertyShape> {
        None
    }
    /// A source-backed own-data payload proof, independent of the child
    /// expression's effect-coverage judgment and exact enumerable order.
    fn own_data_spread_source(&self, _allocation: u32) -> Option<OwnDataSpreadSource> {
        None
    }
    fn object_properties_are_settled(&self, _allocation: u32) -> bool {
        false
    }
    fn property_values(&self, _allocation: u32, _property: &str) -> Option<Values> {
        None
    }
    fn property_is_accessor(&self, _allocation: u32, _property: &str) -> bool {
        true
    }
    fn has_dynamic_properties(&self, _allocation: u32) -> bool {
        true
    }
    fn record_property_read(&mut self, _cell: EffectCell) {}
    /// Application execution uses the normal conservative fallback after the
    /// spread operand has already been evaluated. Strict queries simply
    /// decline this application-specific path.
    fn refused_spread(&mut self, _source: &Values, _span: swc_common::Span) {}
    /// Retain a proven noninvoking data-copy result while recording that its
    /// owner cannot receive complete spread evidence.
    fn incomplete_own_data_spread(&mut self, source: &Values, span: swc_common::Span) {
        self.refused_spread(source, span);
    }
    fn spread_copy_recorded(&mut self, _allocation: u32) {}
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

fn primitive_kinds(values: &Values) -> Option<PrimKinds> {
    values.iter().try_fold(PrimKinds::NONE, |kinds, value| {
        let AbstractValue::Value(Value::Prim(operand)) = value else {
            return None;
        };
        Some(kinds.union(*operand))
    })
}

/// Both postfix's old value and prefix's stored value have these numeric
/// kinds. Exact numbers are outside this domain; object coercion declines.
pub(super) fn update_result(operand: &Values) -> Option<AllocationValue> {
    let kinds = primitive_kinds(operand)?;
    let result = primitive::update(Some(kinds));
    let mut values = Values::new();
    if result != PrimKinds::NONE {
        values.insert(AbstractValue::Value(Value::Prim(result)));
    }
    let judgment = if primitive::update_throws(kinds) || values.is_empty() {
        values.insert(AbstractValue::Value(Value::Unavailable));
        AllocationJudgment::Opaque
    } else {
        AllocationJudgment::CoveredAllocationOnly(ProofDependencies::default())
    };
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
    pub(super) spread_shapes: Option<&'a mut OrdMap<u32, OwnPropertyShape>>,
    pub(super) unknown_enumerability: Option<&'a mut BTreeSet<u32>>,
    pub(super) static_shapes: Option<&'a BTreeMap<u32, OwnPropertyShape>>,
    pub(super) exact_spread_allocations: Option<&'a BTreeSet<u32>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum WritablePrefixState {
    InProgress,
    Proven(BTreeSet<String>),
    DenseArray { length: u32 },
    Unknown,
}

impl LiteralHeap<'_> {
    /// A descriptor/order writer not modeled by the ordinary literal kernel
    /// permanently revokes exact enumeration evidence for this pooled object.
    /// Retained own-data shape metadata may still support named property reads;
    /// callers that enumerate keys must check this marker separately.
    pub(super) fn mark_unknown_enumerability(&mut self, allocation: u32) {
        let already_unknown = self
            .unknown_enumerability
            .as_deref()
            .is_some_and(|unknown| unknown.contains(&allocation));
        let has_static_shape = self
            .static_shapes
            .is_none_or(|shapes| shapes.contains_key(&allocation));
        let has_runtime_shape = self
            .spread_shapes
            .as_deref()
            .is_some_and(|shapes| shapes.contains_key(&allocation));
        let is_spread_allocation = self
            .exact_spread_allocations
            .is_some_and(|allocations| allocations.contains(&allocation));
        if !(already_unknown || has_static_shape || has_runtime_shape || is_spread_allocation) {
            return;
        }
        let Some(unknown) = self.unknown_enumerability.as_deref_mut() else {
            return;
        };
        if unknown.insert(allocation) {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Stores one proven spread copy. Repeated pooled executions retain exact
    /// order only while every observed source has the same complete shape.
    pub(super) fn record_spread_copy(
        &mut self,
        allocation: u32,
        shape: OwnPropertyShape,
        copied_properties: BTreeMap<Option<String>, Values>,
    ) -> bool {
        if self.spread_shapes.is_none() {
            return false;
        }
        if self
            .unknown_enumerability
            .as_deref()
            .is_some_and(|unknown| unknown.contains(&allocation))
        {
            return false;
        }
        let existing_shape = self
            .spread_shapes
            .as_deref()
            .and_then(|shapes| shapes.get(&allocation))
            .cloned();
        if existing_shape
            .as_ref()
            .is_some_and(|previous| previous != &shape)
        {
            self.mark_unknown_enumerability(allocation);
            return false;
        }
        if existing_shape.is_none()
            && self
                .properties
                .get(&allocation)
                .is_some_and(|properties| !properties.is_empty())
        {
            // A legacy or earlier opaque execution already pooled this site;
            // its previous key order cannot be reconstructed safely.
            self.mark_unknown_enumerability(allocation);
            return false;
        }
        let mut changed = false;
        let current = self.properties.entry(allocation).or_default();
        for (property, values) in copied_properties {
            let slot = current.entry(property).or_default();
            let before = slot.len();
            slot.extend(values);
            changed |= slot.len() != before;
        }
        if existing_shape.is_none() {
            self.spread_shapes
                .as_deref_mut()
                .expect("spread shape storage was checked")
                .insert(allocation, shape);
            changed = true;
        }
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
        true
    }

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
        self.mark_unknown_enumerability(allocation);
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
            context.noninvoking_operation();
            Values::from([AbstractValue::Value(Value::Prim(*kinds))])
        }
        ValueExpr::StrictEquality(left, right) => {
            // Strict comparison accepts opaque values without coercion.
            context.noninvoking_operation();
            return primitive_operation(
                context,
                [left.as_ref(), right.as_ref()],
                AbstractValue::Value(Value::Prim(PrimKinds::BOOLEAN)),
                judgment,
            );
        }
        ValueExpr::Void(child) => {
            // GetValue(child) is transferred below before its value is discarded.
            context.noninvoking_operation();
            return primitive_operation(
                context,
                [child.as_ref()],
                AbstractValue::UNDEFINED,
                judgment,
            );
        }
        ValueExpr::LogicalNot(child) => {
            // ToBoolean does not invoke object hooks.
            context.noninvoking_operation();
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
    let properties: Vec<_> = properties.into_iter().collect();
    if matches!(properties.as_slice(), [PropertyDefinition::Spread { .. }]) {
        context.noninvoking_operation();
    } else {
        context.unmodeled_effect_operation();
    }
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
        if matches!(definition, PropertyDefinition::Spread { .. }) && properties.len() != 1 {
            // Mixed literals stay on the indexed effect fallback. Preserve an
            // opaque value in the allocation heap without evaluating the spread
            // operand here; its source operation is handled once by the legacy
            // call path, in source order.
            judgment = AllocationJudgment::Opaque;
            evaluated
                .entry(None)
                .or_default()
                .insert(AbstractValue::Value(Value::Top));
            continue;
        }
        let child = child(context, *definition.value())?;
        let child_covered = matches!(
            &child.judgment,
            AllocationJudgment::CoveredAllocationOnly(_)
        );
        judgment.compose(&child.judgment);
        let mut values = child.values;
        may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
        can_complete &= !values.is_empty();
        if let PropertyDefinition::Spread { span, .. } = definition {
            if properties.len() != 1 || !can_complete || may_throw {
                refuse_spread(context, allocation, &values, *span);
                return None;
            }
            let [AbstractValue::Value(Value::Alloc(Kind::Object, source))] =
                values.iter().collect::<Vec<_>>().as_slice()
            else {
                refuse_spread(context, allocation, &values, *span);
                return None;
            };
            let Some(source_proof) = context.own_data_spread_source(*source) else {
                refuse_spread(context, allocation, &values, *span);
                return None;
            };
            let source_shape = &source_proof.shape;
            if !context.object_properties_are_settled(*source)
                || context.has_dynamic_properties(*source)
                || source_shape
                    .properties
                    .values()
                    .any(|descriptor| !matches!(descriptor, OwnPropertyDescriptorSyntax::Data))
            {
                refuse_spread(context, allocation, &values, *span);
                return None;
            }
            if !source_proof.enumerability_settled {
                let payload = source_proof
                    .properties
                    .values()
                    .flatten()
                    .cloned()
                    .collect();
                retain_incomplete_spread_payload(context, allocation, &values, payload, *span);
                judgment = AllocationJudgment::Opaque;
                continue;
            }
            let source_order = ordered_own_property_names(&source_shape.creation_order);
            if source_order.len() != source_shape.properties.len() {
                refuse_spread(context, allocation, &values, *span);
                return None;
            }
            let mut copied_properties = BTreeMap::new();
            for property in &source_order {
                if context.property_is_accessor(*source, property) {
                    refuse_spread(context, allocation, &values, *span);
                    return None;
                }
                let Some(property_values) = source_proof.properties.get(&Some(property.clone()))
                else {
                    refuse_spread(context, allocation, &values, *span);
                    return None;
                };
                if property_values.is_empty()
                    || property_values.contains(&AbstractValue::Value(Value::Unavailable))
                {
                    refuse_spread(context, allocation, &values, *span);
                    return None;
                }
                copied_properties.insert(Some(property.clone()), property_values.clone());
            }
            context.record_property_read(EffectCell::ObjectShape(*source));
            for property in &source_order {
                context.record_property_read(EffectCell::OwnProperty {
                    origin: *source,
                    property: property.clone(),
                });
            }
            let copied_shape = OwnPropertyShape {
                properties: source_order
                    .iter()
                    .cloned()
                    .map(|property| (property, OwnPropertyDescriptorSyntax::Data))
                    .collect(),
                creation_order: source_order.clone(),
            };
            if !context
                .heap()
                .record_spread_copy(allocation, copied_shape, copied_properties)
            {
                let payload = source_proof
                    .properties
                    .values()
                    .flatten()
                    .cloned()
                    .collect();
                retain_incomplete_spread_payload(context, allocation, &values, payload, *span);
                judgment = AllocationJudgment::Opaque;
                continue;
            }
            writable_properties.extend(source_order.iter().cloned());
            context.spread_copy_recorded(allocation);
            if !child_covered {
                context.incomplete_own_data_spread(&values, *span);
                judgment = AllocationJudgment::Opaque;
            }
            continue;
        }
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

fn refuse_spread(
    context: &mut impl AllocationContext,
    allocation: u32,
    source: &Values,
    span: swc_common::Span,
) {
    {
        let mut heap = context.heap();
        heap.mark_unknown_enumerability(allocation);
        heap.invalidate_writable(allocation);
    }
    context.refused_spread(source, span);
}

fn retain_incomplete_spread_payload(
    context: &mut impl AllocationContext,
    allocation: u32,
    source: &Values,
    payload: Values,
    span: swc_common::Span,
) {
    {
        let mut heap = context.heap();
        heap.mark_unknown_enumerability(allocation);
        heap.invalidate_writable(allocation);
        heap.record(allocation, BTreeMap::from([(None, payload)]));
    }
    context.spread_copy_recorded(allocation);
    context.incomplete_own_data_spread(source, span);
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
                spread_shapes: None,
                unknown_enumerability: None,
                static_shapes: None,
                exact_spread_allocations: None,
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
                spread_shapes: None,
                unknown_enumerability: None,
                static_shapes: None,
                exact_spread_allocations: None,
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
