//! Shared value transfers for closed inputs and literal allocations.
//!
//! A covered result additionally requires complete canonical source coverage.
//! Lowered values alone can have erased calls, writes or coercions.

use super::effect_coverage::EffectCoverageFragment;
use super::value_model::{
    AbstractValue, CallSite, Context, DenseArray, LiteralPropertyKind, PrivateFieldId,
    PropertyDefinition, ProviderScopeId, ValueExpr, Values,
};
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::facts::abstract_domain::builtins::{
    ArgSel, IntrinsicOp, Invocation, NativeBodyEffects, Return, Row, Throws,
};
use crate::facts::abstract_domain::effect::CoerceOp;
use crate::facts::abstract_domain::heap::Mutation;
use crate::facts::abstract_domain::integrity::IntrinsicSlot;
use crate::facts::abstract_domain::primitive;
use crate::facts::abstract_domain::value::{Kind, Value};
use analysis_model::PrimKinds;
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
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

pub(super) enum ArraySpreadYields {
    ExactSlots(Vec<Values>),
    PrimitiveOnly,
}

pub(super) struct ArraySpreadResult {
    pub(super) yields: ArraySpreadYields,
    pub(super) dependencies: ProofDependencies,
}

pub(super) enum ArrayElement<'a, E> {
    Hole,
    Value(&'a E),
    Spread(&'a E, Span),
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
    /// Fresh literal creation and checked own definitions, excluding child
    /// evaluation. The caller checks canonical record tags or no-spread array
    /// syntax before supplying this operation proof.
    fn literal_creation_operation(&mut self, _covered: bool) {
        self.unmodeled_effect_operation();
    }
    /// Resolve an actual closed input, or return opaque access evidence.
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue>;
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue>;
    fn literal_layout(&self, allocation: u32) -> LiteralLayout;
    fn literal_property_kind(&self, allocation: u32, position: usize) -> LiteralPropertyKind;
    fn array_creation_started(&mut self, _allocation: u32) {}
    fn array_created(&mut self, _allocation: u32, _sparse: bool) {}
    /// A pooled source-site allocation may represent an earlier generation.
    /// Primitive-only contents are certified only when this site is new in the
    /// current execution state.
    fn array_allocation_is_fresh(&self, _allocation: u32) -> bool {
        false
    }
    fn array_spread(&mut self, _span: Span, _operand: &Values) -> Option<ArraySpreadResult> {
        None
    }
    fn primitive_array_created(&mut self, _allocation: u32, _dependencies: &ProofDependencies) {}
    fn heap(&mut self) -> LiteralHeap<'_>;
    /// The adapter proves original constructor identity for this canonical New
    /// expression. The operation's row and result semantics are shared.
    fn keyed_collection_new(
        &mut self,
        _callee: &ValueExpr,
        _arguments: &[ValueExpr],
        _site: CallSite,
    ) -> Option<KeyedCollectionOperationResult> {
        None
    }
    /// Compose a checked operation separately from its allocation result.
    /// Allocation-only consumers have no whole-body effect channel.
    fn operation_effects(&mut self, _effects: EffectCoverageFragment) {
        self.unmodeled_effect_operation();
    }
}

/// Transfer the exact zero-argument keyed-collection constructor row. Provider
/// and application adapters both use this kernel after proving callee identity.
pub(super) struct KeyedCollectionOperationResult {
    pub(super) allocation: AllocationValue,
    /// Normal operation effects only. The caller still owns callee/operand
    /// evaluation and whole-source completion.
    pub(super) effects: EffectCoverageFragment,
}

/// The canonical row closes its body only after every actual primitive operand
/// satisfies its coercions without an abrupt completion. It certifies neither
/// callee selection nor any caller operation.
pub(super) fn primitive_native_call_effects(
    row: &'static Row,
    arguments: &[Values],
    dependencies: &ProofDependencies,
) -> Option<EffectCoverageFragment> {
    let symbol_registry = row.body_effects == NativeBodyEffects::SymbolRegistryReadWrite;
    if !matches!(
        row.body_effects,
        NativeBodyEffects::NoExteriorAccess | NativeBodyEffects::SymbolRegistryReadWrite
    ) || row.invocation != [Invocation::Call]
        || !row.arity.contains(&arguments.len())
        || !row.invokes.is_empty()
        || row.allocates.is_some()
        || !row.mutates.is_empty()
        || !matches!(row.returns, Return::Prim(_))
        || row.throws != Throws::Never
        || arguments
            .iter()
            .any(|values| values.is_empty() || !values.iter().all(AbstractValue::is_primitive))
    {
        return None;
    }
    if symbol_registry
        && (row.op != IntrinsicOp::SymbolFor
            || row.family() != Some(NativeIntrinsic::Symbol)
            || row.requires
                != [
                    IntrinsicSlot::Global(NativeIntrinsic::Symbol),
                    IntrinsicSlot::Static(NativeIntrinsic::Symbol, "for"),
                ]
            || row.arity != (0..=1)
            || row.coerces != [(ArgSel::Arg(0), CoerceOp::ToString)]
            || row.returns != Return::Prim(PrimKinds::SYMBOL)
            || arguments.len() != 1
            || !arguments[0].iter().all(|value| {
                matches!(value, AbstractValue::Value(Value::Prim(kinds))
                    if kinds.is_only(PrimKinds::STRING))
            }))
    {
        return None;
    }
    for (operand, coercion) in row.coerces {
        let ArgSel::Arg(index) = operand else {
            return None;
        };
        let admits = match coercion {
            CoerceOp::ToNumber => {
                PrimKinds::ANY.without(PrimKinds::BIGINT.union(PrimKinds::SYMBOL))
            }
            CoerceOp::ToString => PrimKinds::ANY.without(PrimKinds::SYMBOL),
            _ => return None,
        };
        if let Some(values) = arguments.get(*index)
            && !values.iter().all(|value| matches!(value, AbstractValue::Value(Value::Prim(kinds)) if kinds.is_only(admits)))
        {
            return None;
        }
    }
    let mut effects = EffectCoverageFragment::no_effects();
    effects.depend_on(dependencies);
    Some(effects)
}

/// The original FinalizationRegistry NewTarget has an immutable own data
/// prototype. Its constructor writes only fresh internal slots and records a
/// callback, without invoking it (ECMA-262 26.2.1.1 / 26.2.2.1).
pub(super) fn retained_callback_constructor_kind(row: &Row) -> Option<Kind> {
    (row.op == IntrinsicOp::FinalizationRegistry
        && row.family() == Some(NativeIntrinsic::FinalizationRegistry)
        && row.invocation == [Invocation::Construct]
        && row.arity == (1..=1)
        && row.requires == [IntrinsicSlot::Global(NativeIntrinsic::FinalizationRegistry)]
        && row.coerces.is_empty()
        && row.invokes.is_empty()
        && row.allocates == Some(Kind::Registry)
        && row.mutates == [Mutation::FreshRetains(ArgSel::Arg(0))]
        && row.returns == Return::Fresh
        && row.throws == Throws::UnlessCallable(ArgSel::Arg(0)))
    .then_some(Kind::Registry)
}

pub(super) fn retained_callback_constructor_effects(
    row: &Row,
    arguments: &[Values],
    dependencies: &ProofDependencies,
) -> Option<EffectCoverageFragment> {
    retained_callback_constructor_kind(row)?;
    let [callbacks] = arguments else {
        return None;
    };
    if callbacks.len() != 1
        || !matches!(
            callbacks.first()?,
            AbstractValue::Closure(..) | AbstractValue::Value(Value::Alloc(Kind::Function, _))
        )
    {
        return None;
    }
    // IsCallable cannot throw for this actual source function. The default
    // HostMakeJobCallback and HTML's operation only capture execution metadata
    // in the fresh hidden record. The ordinary callback establishes its own
    // script context when invoked; builtin eval is not an admitted operand.
    // This scoped proof neither changes the table's general host-effect flag
    // nor supplies a result shape or discards the retained callback graph.
    let mut effects = EffectCoverageFragment::no_effects();
    effects.depend_on(dependencies);
    Some(effects)
}

pub(super) fn empty_keyed_collection_kind(row: &Row) -> Option<Kind> {
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
    Some(kind)
}

/// Native Set construction from one iterable. The caller must still prove the
/// exact callee, operand source, and consumed iterator receipt.
pub(super) fn set_iterable_constructor_kind(row: &Row) -> Option<Kind> {
    (row.op == IntrinsicOp::Set
        && row.family() == Some(NativeIntrinsic::Set)
        && row.invocation == [Invocation::Construct]
        && row.arity == (0..=1)
        && row.requires == [IntrinsicSlot::Global(NativeIntrinsic::Set)]
        && row.coerces == [(ArgSel::Arg(0), CoerceOp::Iterate)]
        && row.invokes.is_empty()
        && row.allocates == Some(Kind::Set)
        && row.mutates.is_empty()
        && row.returns == Return::Fresh
        && row.throws == Throws::Never)
        .then_some(Kind::Set)
}

pub(super) fn keyed_collection_new(
    row: &'static Row,
    site: CallSite,
    dependencies: ProofDependencies,
) -> Option<KeyedCollectionOperationResult> {
    let kind = empty_keyed_collection_kind(row)?;
    // There are no operand expressions in this operation. Callee selection
    // remains the adapter's independent identity/GetValue obligation.
    let mut effects = EffectCoverageFragment::no_effects();
    effects.depend_on(&dependencies);
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
    pub(super) private_properties: Option<&'a mut OrdMap<u32, BTreeMap<PrivateFieldId, Values>>>,
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

    /// Private names never enter the ordinary own-property namespace. Like
    /// public slots, pooled source sites retain every stored value.
    pub(super) fn write_private_property(
        &mut self,
        allocation: u32,
        property: PrivateFieldId,
        values: &Values,
    ) -> Option<()> {
        let slot = self
            .private_properties
            .as_deref_mut()?
            .entry(allocation)
            .or_default()
            .entry(property)
            .or_default();
        let before = slot.len();
        slot.extend(values.iter().cloned());
        if slot.len() != before {
            *self.revision = self.revision.wrapping_add(1);
        }
        Some(())
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
            let Some(operation) = context.keyed_collection_new(callee, arguments, *site) else {
                context.unmodeled_effect_operation();
                return None;
            };
            context.operation_effects(operation.effects);
            return Some(operation.allocation);
        }
        ValueExpr::Undefined => {
            context.noninvoking_operation();
            Values::from([AbstractValue::UNDEFINED])
        }
        ValueExpr::NonCallable(kinds) => {
            context.noninvoking_operation();
            Values::from([AbstractValue::Value(Value::Prim(*kinds))])
        }
        ValueExpr::Binary(operator, left, right) => {
            let left = context.child(left)?;
            judgment.compose(&left.judgment);
            if left
                .values
                .iter()
                .all(|value| *value == AbstractValue::Value(Value::Unavailable))
            {
                context.unmodeled_effect_operation();
                return Some(AllocationValue::opaque(Values::from([
                    AbstractValue::Value(Value::Unavailable),
                ])));
            }
            let right = context.child(right)?;
            judgment.compose(&right.judgment);
            return Some(
                match binary_result(*operator, &left.values, &right.values) {
                    PrimitiveOperationResult::Covered(values) => {
                        context.noninvoking_operation();
                        AllocationValue { values, judgment }
                    }
                    PrimitiveOperationResult::MayThrow(values) => {
                        context.unmodeled_effect_operation();
                        AllocationValue::opaque(values)
                    }
                    PrimitiveOperationResult::CoercionRequired => {
                        context.unmodeled_effect_operation();
                        AllocationValue::opaque(Values::from([
                            AbstractValue::PRIMITIVE,
                            AbstractValue::Value(Value::Unavailable),
                        ]))
                    }
                },
            );
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
            // GetValue(child) is transferred below. Discarding that value has
            // no coercion, invocation or abrupt completion of its own.
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
                        elements.iter().map(ArrayElement::Value),
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

/// One scalar-operation result shared by ordered execution and value/provider
/// transfers. Coverage is nonterminal and says nothing about operand accesses.
pub(super) enum PrimitiveOperationResult {
    Covered(Values),
    MayThrow(Values),
    CoercionRequired,
}

fn primitive_operand_kinds(values: &Values) -> Option<PrimKinds> {
    let mut result = PrimKinds::NONE;
    for value in values {
        match value {
            AbstractValue::Value(Value::Prim(kinds)) => result = result.union(*kinds),
            AbstractValue::Value(Value::Unavailable) => {}
            _ => return None,
        }
    }
    Some(result)
}

pub(super) fn update_result(operand: &Values) -> PrimitiveOperationResult {
    let Some(kinds) = primitive_operand_kinds(operand) else {
        return PrimitiveOperationResult::CoercionRequired;
    };
    let result = crate::facts::abstract_domain::primitive::update(Some(kinds));
    let mut values = Values::new();
    if !result.is_empty() {
        values.insert(AbstractValue::Value(Value::Prim(result)));
    }
    if operand.contains(&AbstractValue::Value(Value::Unavailable))
        || crate::facts::abstract_domain::primitive::update_throws(kinds)
        || result.is_empty()
    {
        values.insert(AbstractValue::Value(Value::Unavailable));
        PrimitiveOperationResult::MayThrow(values)
    } else {
        PrimitiveOperationResult::Covered(values)
    }
}

pub(super) fn binary_result(
    operator: BinaryOp,
    left: &Values,
    right: &Values,
) -> PrimitiveOperationResult {
    let (Some(left_kinds), Some(right_kinds)) = (
        primitive_operand_kinds(left),
        primitive_operand_kinds(right),
    ) else {
        return PrimitiveOperationResult::CoercionRequired;
    };
    let mut values = Values::new();
    if left_kinds.is_empty() || right_kinds.is_empty() {
        values.insert(AbstractValue::Value(Value::Unavailable));
        return PrimitiveOperationResult::MayThrow(values);
    }
    let Some(result) = primitive::binary_result(operator, left_kinds, right_kinds) else {
        return PrimitiveOperationResult::CoercionRequired;
    };
    if !result.is_empty() {
        values.insert(AbstractValue::Value(Value::Prim(result)));
    }
    let may_throw = left.contains(&AbstractValue::Value(Value::Unavailable))
        || right.contains(&AbstractValue::Value(Value::Unavailable))
        || crate::facts::abstract_domain::primitive::binary_throws(
            operator,
            left_kinds,
            right_kinds,
        )
        || result.is_empty();
    if may_throw {
        values.insert(AbstractValue::Value(Value::Unavailable));
        PrimitiveOperationResult::MayThrow(values)
    } else {
        PrimitiveOperationResult::Covered(values)
    }
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
    let covered_creation = context.source_covered()
        && properties.iter().enumerate().all(|(position, definition)| {
            context.literal_property_kind(allocation, position) == definition.kind()
                && definition
                    .name()
                    .is_some_and(|name| match definition.kind() {
                        LiteralPropertyKind::Data => name != "__proto__",
                        // Static definitions install fresh own descriptors; their
                        // callable child proves creation without running its body.
                        LiteralPropertyKind::Method
                        | LiteralPropertyKind::Getter
                        | LiteralPropertyKind::Setter => true,
                        LiteralPropertyKind::Opaque => false,
                    })
        });
    context.literal_creation_operation(covered_creation);
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
    elements: impl IntoIterator<Item = ArrayElement<'a, E>>,
    mut judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E) -> Option<AllocationValue>,
) -> Option<AllocationValue> {
    // ArrayCreate and own index/length definitions execute no user code.
    // Elements compose their effects below; a hole installs no property and
    // supplies no iterator or inherited-property proof.
    context.heap().begin_literal(allocation);
    context.array_creation_started(allocation);
    context.literal_creation_operation(context.source_covered());
    let mut properties = BTreeMap::<Option<String>, Values>::new();
    let mut dense = Vec::new();
    let mut has_hole = false;
    let mut has_spread = false;
    let mut primitive_only = true;
    let mut exact_slots = true;
    let mut qualitative_spread = false;
    let mut next_index = Some(0usize);
    let mut iterable_dependencies = ProofDependencies::default();
    let mut may_throw = false;
    let mut can_complete = true;
    for element in elements {
        if !can_complete {
            break;
        }
        match element {
            ArrayElement::Hole => {
                has_hole = true;
                primitive_only = false;
                next_index = next_index.map(|index| index + 1);
            }
            ArrayElement::Value(element) => {
                let evaluated = child(context, element)?;
                judgment.compose(&evaluated.judgment);
                let mut values = evaluated.values;
                may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
                can_complete &= !values.is_empty();
                primitive_only &= values.iter().all(AbstractValue::is_primitive);
                properties
                    .entry(None)
                    .or_default()
                    .extend(values.iter().cloned());
                if let Some(index) = next_index {
                    properties.insert(Some(index.to_string()), values.clone());
                    next_index = Some(index + 1);
                }
                dense.push(values);
            }
            ArrayElement::Spread(element, span) => {
                has_spread = true;
                let evaluated = child(context, element)?;
                judgment.compose(&evaluated.judgment);
                let mut operand = evaluated.values;
                may_throw |= operand.remove(&AbstractValue::Value(Value::Unavailable));
                can_complete &= !operand.is_empty();
                if !can_complete {
                    continue;
                }
                let spread = context.array_spread(span, &operand)?;
                iterable_dependencies.extend(&spread.dependencies);
                if let AllocationJudgment::CoveredAllocationOnly(dependencies) = &mut judgment {
                    dependencies.extend(&spread.dependencies);
                } else {
                    judgment = AllocationJudgment::Opaque;
                }
                match spread.yields {
                    ArraySpreadYields::ExactSlots(slots) => {
                        for mut slot in slots {
                            may_throw |= slot.remove(&AbstractValue::Value(Value::Unavailable));
                            can_complete &= !slot.is_empty();
                            primitive_only &= slot.iter().all(AbstractValue::is_primitive);
                            properties
                                .entry(None)
                                .or_default()
                                .extend(slot.iter().cloned());
                            if let Some(index) = next_index {
                                properties.insert(Some(index.to_string()), slot.clone());
                                next_index = Some(index + 1);
                            }
                            dense.push(slot);
                        }
                    }
                    ArraySpreadYields::PrimitiveOnly => {
                        let primitive =
                            Values::from([AbstractValue::Value(Value::Prim(PrimKinds::ANY))]);
                        properties.entry(None).or_default().extend(primitive);
                        exact_slots = false;
                        qualitative_spread = true;
                        next_index = None;
                    }
                }
            }
        }
    }
    if can_complete {
        let array_allocation_is_fresh = context.array_allocation_is_fresh(allocation);
        properties.entry(None).or_default();
        properties.insert(
            Some("length".into()),
            Values::from([AbstractValue::PRIMITIVE]),
        );
        let mut heap = context.heap();
        heap.record(allocation, properties);
        if exact_slots && !has_hole {
            // A pooled iterable's slot vector can bound several generations;
            // its length is not necessarily the actual current length.
            if has_spread {
                heap.invalidate_writable(allocation);
            } else {
                heap.complete_array(allocation, dense.len());
            }
            heap.dense_array(allocation, dense);
        } else {
            heap.invalidate_writable(allocation);
            heap.invalidate_dense_array(allocation);
        }
        context.array_created(allocation, has_hole);
        if array_allocation_is_fresh && primitive_only && qualitative_spread && !has_hole {
            context.primitive_array_created(allocation, &iterable_dependencies);
        }
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
        || !values.iter().all(|value| {
            matches!(value, AbstractValue::SymbolicInput(input) if input.scope == scope)
                || matches!(value, AbstractValue::Value(Value::Prim(_)))
        })
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
                private_properties: None,
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
                private_properties: None,
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
