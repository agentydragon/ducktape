//! Shared value transfers for closed inputs and literal allocations.
//!
//! A covered result additionally requires complete canonical source coverage.
//! Lowered values alone can have erased calls, writes or coercions.

use super::effect_coverage::EffectCoverageFragment;
use super::runtime_heap::{NativeIterableReceipt, NativeIterableYields};
use super::value_model::{
    AbstractValue, ArrayElement, CallSite, DenseArray, LiteralPropertyKind, PropertyDefinition,
    ValueExpr, Values,
};
use super::world::object_surface::ObjectSurfaceStore;
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
use swc_common::Span;
use swc_ecma_ast::{BinaryOp, Id};

#[derive(Clone)]
pub(super) enum AllocationJudgment {
    Opaque,
    /// No external access, write, publication, invocation or throwing operation.
    CoveredAllocationOnly(Box<ProofDependencies>),
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
    Array(Vec<Option<ArrayElement>>),
    Unsupported,
}

// CLEANUP(added 2026-11-09): remove this trait and the kernel's
// `LocalValueContext` bridge once L2c moves the self-evaluating legacy
// evaluator (`Executor::evaluate_transfer`, its last non-kernel implementor
// through `ApplicationAllocationContext`) into `legacy/`; the kernel then
// passes its frame to these transfers directly.
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
    /// `tainted`: a hole or an unproved or non-primitive spread leaves the
    /// literal without a settled own layout.
    fn array_created(&mut self, _allocation: u32, _tainted: bool) {}
    /// The native Array iterator protocol over an already evaluated spread
    /// operand, as a receipt the adapter proves. `None` is an unproved
    /// spread; the shared transfer taints the literal and the context decides
    /// whether execution continues.
    fn array_spread(&mut self, _span: Span, _operand: &Values) -> Option<NativeIterableReceipt> {
        None
    }
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
        AllocationJudgment::CoveredAllocationOnly(Box::default())
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
            judgment: AllocationJudgment::CoveredAllocationOnly(Box::new(dependencies)),
        },
        effects,
    })
}

/// The real shared allocation store; neither consumer supplies app/native facts.
pub(super) struct LiteralHeap<'a> {
    pub(super) surface: &'a mut ObjectSurfaceStore,
    pub(super) arrays: &'a mut OrdMap<u32, DenseArray>,
    /// Arrays whose contents are known only to be primitive, with the
    /// iterator-protocol premises that established them. Never a slot,
    /// cardinality or order claim.
    pub(super) primitive_arrays: &'a mut OrdMap<u32, ProofDependencies>,
    pub(super) revision: &'a mut u64,
    /// Only application execution follows a literal's writability; provider
    /// queries never prove a normally completing write.
    pub(super) track_writable_prefix: bool,
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
        if !self.track_writable_prefix {
            return;
        }
        let writable_prefix = &mut self.surface.writable_prefix;
        let next = if !self
            .surface
            .prefix_object_properties
            .contains_key(&allocation)
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
        if !self.track_writable_prefix {
            return;
        }
        let writable_prefix = &mut self.surface.writable_prefix;
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
        if !self.track_writable_prefix {
            return;
        }
        let writable_prefix = &mut self.surface.writable_prefix;
        if writable_prefix.get(&allocation) == Some(&WritablePrefixState::InProgress) {
            writable_prefix.insert(allocation, WritablePrefixState::Proven(writable_properties));
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn invalidate_writable(&mut self, allocation: u32) {
        if !self.track_writable_prefix {
            return;
        }
        let writable_prefix = &mut self.surface.writable_prefix;
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
        let current = self
            .surface
            .prefix_object_properties
            .entry(allocation)
            .or_default();
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
            .surface
            .prefix_object_properties
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

    /// One source origin may represent several allocations. Drop exact
    /// slots but retain the reachability properties; qualitative contents
    /// make ordinary property projection refuse this origin.
    pub(super) fn primitive_array(&mut self, allocation: u32, dependencies: ProofDependencies) {
        self.arrays.remove(&allocation);
        self.primitive_arrays.insert(allocation, dependencies);
        *self.revision = self.revision.wrapping_add(1);
    }

    pub(super) fn invalidate_primitive_array(&mut self, allocation: u32) {
        if self.primitive_arrays.remove(&allocation).is_some() {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn dense_array(&mut self, allocation: u32, slots: Vec<Values>) {
        let before = self
            .surface
            .prefix_object_properties
            .get(&allocation)
            .cloned();
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
        let properties = self
            .surface
            .prefix_object_properties
            .entry(allocation)
            .or_default();
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
        AllocationJudgment::CoveredAllocationOnly(Box::default())
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
                LiteralLayout::Array(elements) => {
                    return array(
                        context,
                        *allocation,
                        elements
                            .iter()
                            .map(|element| ArrayOperand::from_element(element.as_ref())),
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
pub(super) struct SequenceTransfer {
    values: Values,
    may_throw: bool,
    judgment: AllocationJudgment,
}

impl SequenceTransfer {
    pub(super) fn new(judgment: AllocationJudgment) -> Self {
        Self {
            values: Values::from([AbstractValue::UNDEFINED]),
            may_throw: false,
            judgment,
        }
    }

    /// Record the next child; false means no later child can run.
    pub(super) fn child(&mut self, evaluated: AllocationValue) -> bool {
        self.judgment.compose(&evaluated.judgment);
        self.values = evaluated.values;
        self.may_throw |= self
            .values
            .remove(&AbstractValue::Value(Value::Unavailable));
        !self.values.is_empty()
    }

    pub(super) fn finish(self) -> AllocationValue {
        let Self {
            mut values,
            may_throw,
            mut judgment,
        } = self;
        if may_throw || values.is_empty() {
            judgment = AllocationJudgment::Opaque;
            values.insert(AbstractValue::Value(Value::Unavailable));
        }
        AllocationValue { values, judgment }
    }
}

pub(super) fn sequence<'a, C, E: 'a>(
    context: &mut C,
    expressions: impl IntoIterator<Item = &'a E>,
    judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E) -> Option<AllocationValue>,
) -> Option<AllocationValue> {
    let mut transfer = SequenceTransfer::new(judgment);
    for expression in expressions {
        if !transfer.child(child(context, expression)?) {
            break;
        }
    }
    Some(transfer.finish())
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

/// One record literal in progress. Children are recorded in source order;
/// the heap entry exists only once every child completed normally.
pub(super) struct RecordTransfer {
    allocation: u32,
    names: Vec<Option<String>>,
    writable_properties: BTreeSet<String>,
    evaluated: BTreeMap<Option<String>, Values>,
    judgment: AllocationJudgment,
    may_throw: bool,
    can_complete: bool,
}

impl RecordTransfer {
    /// Every position's canonical kind is checked before any child runs.
    pub(super) fn begin<E>(
        context: &mut impl AllocationContext,
        allocation: u32,
        properties: &[PropertyDefinition<E>],
        judgment: AllocationJudgment,
    ) -> Option<Self> {
        // ObjectCreate plus static-key data, method and accessor definitions
        // run no user code; every child value supplies its own receipt.
        context.noninvoking_operation();
        let mut writable_properties = BTreeSet::new();
        let mut non_writable_properties = BTreeSet::new();
        for definition in properties {
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
        for (position, definition) in properties.iter().enumerate() {
            let expected = context.literal_property_kind(allocation, position);
            if expected != LiteralPropertyKind::Opaque && expected != definition.kind() {
                return None;
            }
        }
        context.heap().begin_literal(allocation);
        Some(Self {
            allocation,
            names: properties
                .iter()
                .map(|definition| definition.name().map(str::to_owned))
                .collect(),
            writable_properties,
            evaluated: BTreeMap::new(),
            judgment,
            may_throw: false,
            can_complete: true,
        })
    }

    pub(super) fn can_complete(&self) -> bool {
        self.can_complete
    }

    pub(super) fn child(&mut self, position: usize, child: AllocationValue) {
        self.judgment.compose(&child.judgment);
        let mut values = child.values;
        self.may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
        self.can_complete &= !values.is_empty();
        // This is descriptor payload containment. Its ordinary-data eligibility
        // remains guarded by the canonical descriptor tag at every read.
        self.evaluated
            .entry(self.names.get(position).cloned().flatten())
            .or_default()
            .extend(values);
    }

    pub(super) fn finish(self, context: &mut impl AllocationContext) -> AllocationValue {
        let Self {
            allocation,
            writable_properties,
            evaluated,
            mut judgment,
            may_throw,
            can_complete,
            ..
        } = self;
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
        AllocationValue { values, judgment }
    }
}

/// Shared record evaluation, including the application's opaque-key fallback.
pub(super) fn record<'a, C: AllocationContext, E: 'a>(
    context: &mut C,
    allocation: u32,
    properties: impl IntoIterator<Item = PropertyDefinition<&'a E>>,
    judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E) -> Option<AllocationValue>,
) -> Option<AllocationValue> {
    let properties: Vec<_> = properties.into_iter().collect();
    let mut transfer = RecordTransfer::begin(context, allocation, &properties, judgment)?;
    for (position, definition) in properties.iter().enumerate() {
        if !transfer.can_complete() {
            break;
        }
        let child = child(context, *definition.value())?;
        transfer.child(position, child);
    }
    Some(transfer.finish(context))
}

/// One array literal in progress. Holes are absent own properties and never
/// acquire a dense-array proof. A spread expands the native iterator receipt
/// the context proves: exact slots keep the literal dense, primitive-only
/// yields leave a primitive array with no slot or length claim, and an
/// unproved or non-primitive spread taints the literal.
pub(super) struct ArrayTransfer {
    allocation: u32,
    has_spread: bool,
    properties: BTreeMap<Option<String>, Values>,
    dense: Vec<Values>,
    /// Absent once a qualitative spread made later positions unknown.
    next_index: Option<usize>,
    has_hole: bool,
    primitive_only: bool,
    qualitative_spread: bool,
    unproved_spread: bool,
    /// Protocol premises of every expanded spread; the premises a later
    /// spread of this array inherits.
    iterable_dependencies: ProofDependencies,
    may_throw: bool,
    can_complete: bool,
    judgment: AllocationJudgment,
}

impl ArrayTransfer {
    /// A literal with a spread has no exact `ArrayCreate` length, so it
    /// never seeds a writable-prefix proof.
    pub(super) fn begin(
        context: &mut impl AllocationContext,
        allocation: u32,
        has_spread: bool,
        judgment: AllocationJudgment,
    ) -> Self {
        // `ArrayCreate` and `CreateDataProperty` on the fresh array run no
        // user code; each element and spread supplies its own evidence.
        context.noninvoking_operation();
        if !has_spread {
            context.heap().begin_literal(allocation);
        }
        Self {
            allocation,
            has_spread,
            properties: BTreeMap::new(),
            dense: Vec::new(),
            next_index: Some(0),
            has_hole: false,
            primitive_only: true,
            qualitative_spread: false,
            unproved_spread: false,
            iterable_dependencies: ProofDependencies::default(),
            may_throw: false,
            can_complete: true,
            judgment,
        }
    }

    pub(super) fn can_complete(&self) -> bool {
        self.can_complete
    }

    pub(super) fn hole(&mut self) {
        self.has_hole = true;
        self.primitive_only = false;
        self.next_index = self.next_index.map(|index| index + 1);
    }

    fn slot(&mut self, values: Values) {
        self.primitive_only &= values.iter().all(AbstractValue::is_primitive);
        self.properties
            .entry(None)
            .or_default()
            .extend(values.iter().cloned());
        if let Some(index) = self.next_index {
            self.properties
                .insert(Some(index.to_string()), values.clone());
            self.next_index = Some(index + 1);
        }
        self.dense.push(values);
    }

    pub(super) fn child(&mut self, evaluated: AllocationValue) {
        self.judgment.compose(&evaluated.judgment);
        let mut values = evaluated.values;
        self.may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
        self.can_complete &= !values.is_empty();
        self.slot(values);
    }

    /// Expand one spread whose operand has already been evaluated. The
    /// operand stays reachable from the literal whatever the protocol yields.
    /// Reading another object's contents is not an allocation-only operation.
    pub(super) fn spread(
        &mut self,
        context: &mut impl AllocationContext,
        span: Span,
        operand: AllocationValue,
    ) {
        self.judgment = AllocationJudgment::Opaque;
        let mut values = operand.values;
        self.may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
        self.can_complete &= !values.is_empty();
        if !self.can_complete {
            return;
        }
        self.properties
            .entry(None)
            .or_default()
            .extend(values.iter().cloned());
        let Some(receipt) = context.array_spread(span, &values) else {
            self.unproved_spread = true;
            return;
        };
        self.iterable_dependencies.extend(
            &receipt
                .coverage
                .normal_dependencies()
                .expect("a yielded native protocol receipt is normal"),
        );
        match receipt.yields {
            NativeIterableYields::ExactSlots(slots) => {
                for slot in slots {
                    self.slot(slot);
                }
            }
            NativeIterableYields::PrimitiveOnly => {
                self.qualitative_spread = true;
                self.next_index = None;
            }
        }
    }

    pub(super) fn finish(self, context: &mut impl AllocationContext) -> AllocationValue {
        let Self {
            allocation,
            has_spread,
            mut properties,
            dense,
            has_hole,
            primitive_only,
            qualitative_spread,
            unproved_spread,
            iterable_dependencies,
            may_throw,
            can_complete,
            mut judgment,
            ..
        } = self;
        if can_complete {
            let exact_slots = !qualitative_spread && !unproved_spread;
            if has_spread && !exact_slots {
                // Only reachability survives: the slots and length of a
                // qualitative or tainted literal are unknown.
                properties.retain(|property, _| property.is_none());
            }
            properties.entry(None).or_default();
            if exact_slots {
                properties.insert(
                    Some("length".into()),
                    Values::from([AbstractValue::PRIMITIVE]),
                );
            }
            let mut heap = context.heap();
            heap.record(allocation, properties);
            let tainted = has_hole || unproved_spread || (qualitative_spread && !primitive_only);
            if tainted {
                heap.invalidate_writable(allocation);
                heap.invalidate_dense_array(allocation);
                heap.invalidate_primitive_array(allocation);
            } else if qualitative_spread {
                heap.invalidate_writable(allocation);
                heap.primitive_array(allocation, iterable_dependencies);
            } else if has_spread {
                heap.invalidate_writable(allocation);
                heap.dense_array(allocation, dense);
            } else {
                // This ordered literal has no spread operands, so dense.len() is
                // the exact ArrayCreate length for its first uninterrupted run.
                heap.complete_array(allocation, dense.len());
                heap.dense_array(allocation, dense);
            }
            context.array_created(allocation, tainted);
        }
        let mut values = Values::new();
        if can_complete {
            values.insert(AbstractValue::Value(Value::Alloc(Kind::Object, allocation)));
        }
        if may_throw || !can_complete {
            judgment = AllocationJudgment::Opaque;
            values.insert(AbstractValue::Value(Value::Unavailable));
        }
        AllocationValue { values, judgment }
    }
}

pub(super) enum ArrayOperand<'a, E> {
    Hole,
    Value(&'a E),
    Spread(&'a E, Span),
}

impl<'a> ArrayOperand<'a, ValueExpr> {
    pub(super) fn from_element(element: Option<&'a ArrayElement>) -> Self {
        match element {
            None => Self::Hole,
            Some(ArrayElement {
                spread: None,
                value,
            }) => Self::Value(value),
            Some(ArrayElement {
                spread: Some(span),
                value,
            }) => Self::Spread(value, *span),
        }
    }
}

/// Array creation shares ordered child transfer with local execution.
pub(super) fn array<'a, C: AllocationContext, E: 'a>(
    context: &mut C,
    allocation: u32,
    elements: impl IntoIterator<Item = ArrayOperand<'a, E>>,
    judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E) -> Option<AllocationValue>,
) -> Option<AllocationValue> {
    let elements: Vec<_> = elements.into_iter().collect();
    let has_spread = elements
        .iter()
        .any(|element| matches!(element, ArrayOperand::Spread(..)));
    let mut transfer = ArrayTransfer::begin(context, allocation, has_spread, judgment);
    for element in elements {
        if !transfer.can_complete() {
            break;
        }
        match element {
            ArrayOperand::Hole => transfer.hole(),
            ArrayOperand::Value(element) => transfer.child(child(context, element)?),
            ArrayOperand::Spread(element, span) => {
                let operand = child(context, element)?;
                transfer.spread(context, span, operand);
            }
        }
    }
    Some(transfer.finish(context))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repeated_pooled_literal_site_cannot_restore_writable_prefix() {
        let mut surface = ObjectSurfaceStore::default();
        let mut arrays = OrdMap::new();
        let mut primitive_arrays = OrdMap::new();
        let mut revision = 0;
        let allocation = 7;
        let stored = BTreeMap::from([(
            Some("value".to_owned()),
            Values::from([AbstractValue::PRIMITIVE]),
        )]);

        {
            let mut heap = LiteralHeap {
                surface: &mut surface,
                arrays: &mut arrays,
                primitive_arrays: &mut primitive_arrays,
                revision: &mut revision,
                track_writable_prefix: true,
            };
            heap.begin_literal(allocation);
            assert_eq!(
                heap.surface.writable_prefix.get(&allocation),
                Some(&WritablePrefixState::InProgress)
            );
            heap.record(allocation, stored.clone());
            heap.complete_record(allocation, BTreeSet::from(["value".to_owned()]));
        }
        assert_eq!(
            surface.writable_prefix.get(&allocation),
            Some(&WritablePrefixState::Proven(BTreeSet::from([
                "value".to_owned()
            ])))
        );
        let proven_revision = revision;

        {
            let mut heap = LiteralHeap {
                surface: &mut surface,
                arrays: &mut arrays,
                primitive_arrays: &mut primitive_arrays,
                revision: &mut revision,
                track_writable_prefix: true,
            };
            heap.begin_literal(allocation);
            assert_eq!(
                heap.surface.writable_prefix.get(&allocation),
                Some(&WritablePrefixState::Unknown)
            );
            heap.complete_record(allocation, BTreeSet::from(["value".to_owned()]));
        }
        assert_eq!(
            surface.writable_prefix.get(&allocation),
            Some(&WritablePrefixState::Unknown)
        );
        assert_eq!(revision, proven_revision + 1);
    }
}
