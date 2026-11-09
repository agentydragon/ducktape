//! Shared value transfers for closed inputs and literal allocations.
//!
//! A covered result additionally requires complete canonical source coverage.
//! Lowered values alone can have erased calls, writes or coercions.

use super::effect_coverage::EffectCoverageFragment;
use super::native_operation::{CheckedNativePlan, NativeOperationCommit, NativeOperationResult};
use super::object_surface::{
    AppliedAccessor, ObjectSurfaceStore, OwnPropertyLayout, OwnPropertyLayoutDescriptor,
    OwnPropertyLayoutKind,
};
use super::own_data_transfer::{self, OwnDataContext, OwnDataCopySource};
use super::value_model::{
    AbstractValue, CallSite, Context, DenseArray, LiteralPropertyKind, PrivateFieldId,
    PropertyDefinition, ProviderScopeId, ValueExpr, Values,
};
use super::{
    AbruptAlternative, Completion, NonNormalPossibilities, TransferLedger, UnknownThrowValues,
};
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::execution_evidence::ObjectSpreadRefusal;
use crate::facts::abstract_domain::builtins::{
    ArgSel, IntrinsicOp, Invocation, NativeBodyEffects, Return, Row, Throws,
};
use crate::facts::abstract_domain::effect::CoerceOp;
use crate::facts::abstract_domain::heap::Mutation;
use crate::facts::abstract_domain::integrity::IntrinsicSlot;
use crate::facts::abstract_domain::primitive;
use crate::facts::abstract_domain::value::{HeapAddress, Kind, SourceHeapResolver, Value};
use analysis_model::PrimKinds;
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::{BinaryOp, Id};

#[derive(Debug, Clone)]
pub(crate) enum AllocationJudgment {
    Opaque,
    /// No external access, write, publication, invocation or throwing operation.
    CoveredAllocationOnly(Box<ProofDependencies>),
}

impl AllocationJudgment {
    pub(super) fn compose(&mut self, other: &Self) {
        match (&mut *self, other) {
            (Self::CoveredAllocationOnly(current), Self::CoveredAllocationOnly(next)) => {
                current.as_mut().extend(next.as_ref());
            }
            _ => *self = Self::Opaque,
        }
    }
}

pub(super) struct AllocationValue {
    pub(super) ledger: TransferLedger<Values, ()>,
}

impl AllocationValue {
    pub(super) fn with_completion(
        completion: Completion<Values, ()>,
        judgment: AllocationJudgment,
        mut dependencies: ProofDependencies,
    ) -> Self {
        if let AllocationJudgment::CoveredAllocationOnly(allocation_dependencies) = &judgment {
            dependencies.extend(allocation_dependencies.as_ref());
        }
        let (normal, abrupt, mut non_normal) = allocation_alternatives(completion);
        let uncertain_normal = normal.as_ref().is_some_and(|values| {
            values.is_empty() || values.contains(&AbstractValue::Value(Value::Unavailable))
        });
        if uncertain_normal {
            non_normal.may_diverge = true;
            non_normal.unresolved = true;
        }
        let completion =
            Completion::from_alternatives(normal.map(normal_values), abrupt, non_normal);
        Self {
            ledger: TransferLedger::new(
                completion,
                // AllocationContext callbacks are the active-frame effect
                // writer. This carrier holds operation-local value evidence.
                EffectCoverageFragment::no_effects(),
                Box::new(dependencies),
                judgment,
            ),
        }
    }

    pub(super) fn normal(values: Values, judgment: AllocationJudgment) -> Self {
        Self::normal_with_dependencies(values, judgment, ProofDependencies::default())
    }

    fn normal_with_dependencies(
        values: Values,
        judgment: AllocationJudgment,
        dependencies: ProofDependencies,
    ) -> Self {
        Self::with_completion(Completion::Normal(values), judgment, dependencies)
    }

    pub(super) fn opaque(values: Values) -> Self {
        Self::with_completion(
            Completion::OpaqueMayNormal(normal_values(values)),
            AllocationJudgment::Opaque,
            ProofDependencies::default(),
        )
    }

    fn opaque_with(
        values: Values,
        judgment: AllocationJudgment,
        dependencies: ProofDependencies,
    ) -> Self {
        Self::with_completion(
            Completion::OpaqueMayNormal(normal_values(values)),
            judgment,
            dependencies,
        )
    }
}

fn add_judgment_dependencies(dependencies: &mut ProofDependencies, judgment: &AllocationJudgment) {
    if let AllocationJudgment::CoveredAllocationOnly(allocation_dependencies) = judgment {
        dependencies.extend(allocation_dependencies.as_ref());
    }
}

fn force_opaque(
    mut value: AllocationValue,
    extra_dependencies: &ProofDependencies,
) -> AllocationValue {
    let mut dependencies = value.ledger.dependencies.as_ref().clone();
    dependencies.extend(extra_dependencies);
    let (normal, abrupt, mut non_normal) = allocation_alternatives(value.ledger.completion);
    let normal = normal.map(normal_values);
    if normal.is_some() {
        non_normal.may_diverge = true;
        non_normal.unresolved = true;
    }
    let completion = Completion::from_alternatives(normal, abrupt, non_normal);
    value.ledger.completion = completion;
    value.ledger.dependencies = Box::new(dependencies);
    value.ledger.allocation = AllocationJudgment::Opaque;
    AllocationValue {
        ledger: value.ledger,
    }
}

fn invalidate_partial_literal(
    context: &mut impl AllocationContext,
    allocation: u32,
    spread: bool,
    array: bool,
) {
    let allocation = context.heap_resolver().allocation(Kind::Object, allocation);
    let mut heap = context.heap();
    heap.invalidate_writable(allocation);
    heap.mark_unknown_enumerability(allocation);
    heap.invalidate_surface(allocation);
    if spread {
        heap.invalidate_spread_copy(allocation);
    }
    if array {
        heap.invalidate_dense_array(allocation);
    }
}

fn top_values() -> Values {
    Values::from([AbstractValue::Value(Value::Top)])
}

fn normal_values(mut values: Values) -> Values {
    if values.remove(&AbstractValue::Value(Value::Unavailable)) {
        // Unavailable does not mean “no normal result.” Its possible normal
        // value is unbounded, so retain Top in the normal projection.
        return top_values();
    }
    if values.is_empty() {
        top_values()
    } else {
        values
    }
}

/// Allocation evaluation has a unit abrupt marker and no source-operation
/// witness. Convert it to an explicit unknown-payload throw before composing
/// paths; never treat it as a known source throw.
fn allocation_alternatives(
    completion: Completion<Values, ()>,
) -> (
    Option<Values>,
    Vec<AbruptAlternative<()>>,
    NonNormalPossibilities,
) {
    let (normal, abrupt, non_normal) = completion.into_alternatives();
    let abrupt = abrupt
        .into_iter()
        .map(|alternative| match alternative {
            AbruptAlternative::Exact(()) => {
                AbruptAlternative::Unknown(Some(UnknownThrowValues::new(top_values())))
            }
            unknown => unknown,
        })
        .collect();
    (normal, abrupt, non_normal)
}

fn sequence_completion(
    prefix: Completion<Values, ()>,
    next: Completion<Values, ()>,
) -> Completion<Values, ()> {
    let (prefix_normal, prefix_abrupt, prefix_non_normal) = allocation_alternatives(prefix);
    let (next_normal, next_abrupt, next_non_normal) = allocation_alternatives(next);
    let prefix = Completion::from_alternatives(prefix_normal, prefix_abrupt, prefix_non_normal);
    let next =
        Completion::from_alternatives(next_normal.map(normal_values), next_abrupt, next_non_normal);
    prefix.then(next)
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

/// A compositor distinguishes an unentered child from a value or a selected
/// remainder disposition. It must never replay a reached prefix.
pub(super) enum ChildTransfer<T> {
    Value(T),
    /// The operation has not entered; the caller may use its ordinary path.
    BeforeExecution,
    /// A child or its prefix ran and a conservative remainder was selected.
    /// Callers stop and preserve the execution-boundary disposition.
    RemainderAfterPrefix {
        summarized: bool,
    },
}

/// Whether the application evaluator may use its legacy compatibility path
/// after this shared transfer refuses before entering a child. Keep this
/// exhaustive over `ValueExpr`: a new expression shape must choose its
/// execution owner explicitly instead of accidentally entering a legacy arm.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum RefusalDisposition {
    /// The shared transfer owns this expression (or it is an abstract branch
    /// set with no source-order execution). Preserve the refusal boundary.
    PreserveRefusal,
    /// No shared child was entered, and the compatibility evaluator may enter
    /// this operation once. Its value is not evidence of normal completion.
    LegacyOpaqueFallback,
}

pub(super) fn refusal_disposition(expression: &ValueExpr) -> RefusalDisposition {
    match expression {
        ValueExpr::Binding(_)
        | ValueExpr::Alloc(Kind::Function | Kind::RegExp, _)
        | ValueExpr::ObjectLiteral(..)
        | ValueExpr::ObjectSpreadLiteral(..)
        | ValueExpr::Undefined
        | ValueExpr::NonCallable(_)
        | ValueExpr::Primitive(_)
        | ValueExpr::Binary(..)
        | ValueExpr::StrictEquality(..)
        | ValueExpr::Sequence(_)
        | ValueExpr::LogicalNot(_)
        | ValueExpr::Void(_)
        | ValueExpr::Conditional { .. }
        | ValueExpr::Alternatives(_) => RefusalDisposition::PreserveRefusal,
        ValueExpr::Alloc(_, _)
        | ValueExpr::Member(..)
        | ValueExpr::Call(..)
        | ValueExpr::New(..)
        | ValueExpr::ArrayIteratorPrototype(..)
        | ValueExpr::SetIteratorPrototype(..)
        | ValueExpr::Unknown => RefusalDisposition::LegacyOpaqueFallback,
    }
}

pub(super) enum ArrayElement<'a, E> {
    Hole,
    Value(&'a E),
    Spread(&'a E, Span),
}

/// The shared ordered literal-store state. The local continuation owns this
/// state while source children suspend; allocation-only consumers use the
/// same methods around their own child adapter.
#[derive(Clone)]
pub(super) struct RecordTransfer {
    allocation: u32,
    heap_address: HeapAddress,
    covered_creation: bool,
    definitions: Vec<(LiteralPropertyKind, Option<String>)>,
    writable_properties: BTreeSet<String>,
    evaluated: BTreeMap<Option<String>, Values>,
    evaluated_accessors: BTreeMap<String, AppliedAccessor>,
    retained_children: Values,
}

impl RecordTransfer {
    pub(super) fn begin<T>(
        context: &mut impl AllocationContext,
        allocation: u32,
        properties: &[PropertyDefinition<T>],
    ) -> Option<Self> {
        let definitions = properties
            .iter()
            .map(|property| (property.kind(), property.name().map(str::to_owned)))
            .collect::<Vec<_>>();
        // Validate every descriptor before executing any child. A late
        // mismatch cannot leave a reached prefix before the caller's fallback.
        if definitions.iter().enumerate().any(|(position, (kind, _))| {
            let expected = context.literal_property_kind(allocation, position);
            expected != LiteralPropertyKind::Opaque && expected != *kind
        }) {
            return None;
        }
        let source_covered = context.source_covered();
        let covered_creation = source_covered
            && definitions
                .iter()
                .enumerate()
                .all(|(position, (kind, name))| {
                    context.literal_property_kind(allocation, position) == *kind
                    // Shorthand `__proto__` is named own-data syntax. The
                    // legacy colon form remains opaque.
                    && name.is_some()
                    && !matches!(kind, LiteralPropertyKind::Opaque)
                });
        context.literal_creation_operation(covered_creation);
        let heap_address = context.heap_resolver().allocation(Kind::Object, allocation);
        context.heap().begin_literal(heap_address);
        let mut writable_properties = BTreeSet::new();
        let mut non_writable_properties = BTreeSet::new();
        for (kind, name) in &definitions {
            if let Some(property) = name {
                match kind {
                    LiteralPropertyKind::Data | LiteralPropertyKind::Method => {
                        writable_properties.insert(property.clone());
                        non_writable_properties.remove(property);
                    }
                    _ => {
                        writable_properties.remove(property);
                        non_writable_properties.insert(property.clone());
                    }
                }
            }
        }
        Some(Self {
            allocation,
            heap_address,
            covered_creation,
            definitions,
            writable_properties,
            evaluated: BTreeMap::new(),
            evaluated_accessors: BTreeMap::new(),
            retained_children: Values::new(),
        })
    }

    pub(super) fn record_child(&mut self, index: usize, values: &Values) -> Option<()> {
        let (kind, name) = self.definitions.get(index)?;
        let values = normal_values(values.clone());
        self.retained_children.extend(values.iter().cloned());
        match kind {
            LiteralPropertyKind::Data | LiteralPropertyKind::Method => {
                self.evaluated
                    .entry(name.clone())
                    .or_default()
                    .extend(values);
            }
            LiteralPropertyKind::Getter | LiteralPropertyKind::Setter => {
                let accessor = self.evaluated_accessors.entry(name.clone()?).or_default();
                if *kind == LiteralPropertyKind::Getter {
                    accessor.getter.extend(values);
                } else {
                    accessor.setter.extend(values);
                }
            }
            LiteralPropertyKind::Opaque => {
                self.evaluated.entry(None).or_default().extend(values);
            }
        }
        Some(())
    }

    pub(super) fn retained_values(&self) -> Values {
        let mut values = Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            self.heap_address,
        ))]);
        values.extend(self.retained_children.iter().cloned());
        values
    }

    pub(super) fn finish(
        self,
        context: &mut impl AllocationContext,
        complete: bool,
        exact_normal: bool,
        dependencies: ProofDependencies,
    ) -> Option<Values> {
        if !complete {
            self.abort(context);
            return None;
        }
        let complete_generation = self.covered_creation
            && exact_normal
            && !self.evaluated.contains_key(&None)
            && self.definitions.iter().all(|(kind, name)| {
                name.is_some()
                    && matches!(
                        kind,
                        LiteralPropertyKind::Data
                            | LiteralPropertyKind::Method
                            | LiteralPropertyKind::Getter
                            | LiteralPropertyKind::Setter
                    )
            });
        let (exact_keys, layout) = if complete_generation {
            let creation_order = self
                .definitions
                .iter()
                .filter_map(|(_, name)| name.clone())
                .collect::<Vec<_>>();
            let descriptors = self
                .definitions
                .iter()
                .filter_map(|(kind, name)| {
                    let name = name.as_ref()?;
                    let (layout_kind, attributes) = match kind {
                        LiteralPropertyKind::Data | LiteralPropertyKind::Method => (
                            OwnPropertyLayoutKind::Data,
                            super::property_inference::DescriptorAttributes::literal(true),
                        ),
                        LiteralPropertyKind::Getter | LiteralPropertyKind::Setter => (
                            OwnPropertyLayoutKind::Accessor,
                            super::property_inference::DescriptorAttributes {
                                writable: None,
                                enumerable: Some(true),
                                configurable: Some(true),
                            },
                        ),
                        LiteralPropertyKind::Opaque => return None,
                    };
                    Some((
                        name.clone(),
                        OwnPropertyLayoutDescriptor {
                            kind: layout_kind,
                            attributes,
                        },
                    ))
                })
                .collect();
            let layout = OwnPropertyLayout::from_creation_order(&creation_order, descriptors);
            let keys = layout.as_ref().map(OwnPropertyLayout::keys);
            (keys, layout)
        } else {
            (None, None)
        };
        let mut evaluated = BTreeMap::new();
        for (property, values) in self.evaluated {
            let keep = property.as_ref().is_none_or(|name| {
                layout
                    .as_ref()
                    .and_then(|layout| layout.descriptors.get(name))
                    .is_none_or(|descriptor| descriptor.kind == OwnPropertyLayoutKind::Data)
            });
            if keep {
                evaluated.insert(property, values);
            }
        }
        let accessors = self
            .evaluated_accessors
            .into_iter()
            .filter(|(name, _)| {
                layout
                    .as_ref()
                    .and_then(|layout| layout.descriptors.get(name))
                    .is_none_or(|descriptor| descriptor.kind == OwnPropertyLayoutKind::Accessor)
            })
            .collect();
        let mut heap = context.heap();
        heap.record_layout(
            self.heap_address,
            evaluated,
            exact_keys,
            layout.clone(),
            dependencies,
        );
        heap.record_accessor_properties(self.heap_address, accessors, layout.as_ref());
        if self.covered_creation && exact_normal {
            heap.complete_record(self.heap_address, self.writable_properties);
        } else {
            heap.invalidate_writable(self.heap_address);
        }
        Some(Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            self.heap_address,
        ))]))
    }

    pub(super) fn abort(self, context: &mut impl AllocationContext) {
        invalidate_partial_literal(context, self.allocation, false, false);
    }
}

/// A source-ordered array literal accumulator shared by the continuation and
/// allocation-only adapters. The continuation holds it across child calls.
#[derive(Clone)]
pub(super) struct ArrayTransfer {
    allocation: u32,
    heap_address: HeapAddress,
    source_covered: bool,
    properties: BTreeMap<Option<String>, Values>,
    dense: Vec<Values>,
    has_hole: bool,
    has_spread: bool,
    primitive_only: bool,
    exact_slots: bool,
    qualitative_spread: bool,
    next_index: Option<usize>,
    iterable_dependencies: ProofDependencies,
    dependencies: ProofDependencies,
    judgment: AllocationJudgment,
}

impl ArrayTransfer {
    pub(super) fn begin(context: &mut impl AllocationContext, allocation: u32) -> Self {
        let address = context.heap_resolver().allocation(Kind::Object, allocation);
        Self::begin_at(context, allocation, address)
    }

    fn begin_at(
        context: &mut impl AllocationContext,
        allocation: u32,
        heap_address: HeapAddress,
    ) -> Self {
        let source_covered = context.source_covered();
        context.heap().begin_literal(heap_address);
        context.array_creation_started(allocation);
        context.literal_creation_operation(source_covered);
        Self {
            allocation,
            heap_address,
            source_covered,
            properties: BTreeMap::new(),
            dense: Vec::new(),
            has_hole: false,
            has_spread: false,
            primitive_only: true,
            exact_slots: true,
            qualitative_spread: false,
            next_index: Some(0),
            iterable_dependencies: ProofDependencies::default(),
            dependencies: ProofDependencies::default(),
            judgment: AllocationJudgment::CoveredAllocationOnly(Box::default()),
        }
    }

    pub(super) fn hole(&mut self) {
        self.has_hole = true;
        self.primitive_only = false;
        self.next_index = self.next_index.map(|index| index + 1);
    }

    pub(super) fn start_spread(&mut self) {
        self.has_spread = true;
    }

    pub(super) fn record_child(&mut self, values: &Values) {
        let values = normal_values(values.clone());
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

    pub(super) fn record_dependencies(&mut self, dependencies: &ProofDependencies) {
        self.dependencies.extend(dependencies);
    }

    pub(super) fn record_spread(
        &mut self,
        context: &mut impl AllocationContext,
        span: Span,
        operand: &Values,
    ) -> ChildTransfer<()> {
        let spread = match context.array_spread(span, &normal_values(operand.clone())) {
            ChildTransfer::Value(spread) => spread,
            ChildTransfer::BeforeExecution => return ChildTransfer::BeforeExecution,
            ChildTransfer::RemainderAfterPrefix { summarized } => {
                return ChildTransfer::RemainderAfterPrefix { summarized };
            }
        };
        self.iterable_dependencies.extend(&spread.dependencies);
        self.dependencies.extend(&spread.dependencies);
        if let AllocationJudgment::CoveredAllocationOnly(allocation_dependencies) =
            &mut self.judgment
        {
            allocation_dependencies
                .as_mut()
                .extend(&spread.dependencies);
        } else {
            self.judgment = AllocationJudgment::Opaque;
        }
        match spread.yields {
            ArraySpreadYields::ExactSlots(slots) => {
                for slot in slots {
                    let values = normal_values(slot);
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
            }
            ArraySpreadYields::PrimitiveOnly => {
                let primitive = Values::from([AbstractValue::Value(Value::Prim(PrimKinds::ANY))]);
                self.properties.entry(None).or_default().extend(primitive);
                self.exact_slots = false;
                self.qualitative_spread = true;
                self.next_index = None;
            }
        }
        ChildTransfer::Value(())
    }

    pub(super) fn retained_values(&self) -> Values {
        let mut values = Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Object,
            self.heap_address,
        ))]);
        values.extend(
            self.properties
                .values()
                .chain(self.dense.iter())
                .flat_map(|values| values.iter().cloned()),
        );
        values
    }

    pub(super) fn finish(
        self,
        context: &mut impl AllocationContext,
        exact_normal: bool,
        layout_dependencies: ProofDependencies,
    ) -> (Values, AllocationJudgment, ProofDependencies) {
        let exact_layout = self.source_covered && exact_normal;
        let array_allocation_is_fresh = context.array_allocation_is_fresh(self.allocation);
        let mut dependencies = self.dependencies;
        dependencies.extend(&layout_dependencies);
        let mut properties = self.properties;
        let complete_generation = self.source_covered && self.exact_slots && exact_normal;
        if !complete_generation {
            properties.entry(None).or_default();
        } else {
            // In the exact array transfer every payload is already attached
            // to its canonical index. The `None` bucket is reserved for
            // unknown-key writes and would incorrectly block an own-layout
            // projection if used as a redundant aggregate cache.
            properties.remove(&None);
        }
        properties.insert(
            Some("length".into()),
            Values::from([AbstractValue::PRIMITIVE]),
        );
        let spread_shape = if self.has_spread {
            Some((exact_layout && self.exact_slots).then(|| {
                properties
                    .keys()
                    .filter_map(|property| property.as_ref())
                    .filter(|property| property.as_str() != "length")
                    .cloned()
                    .collect()
            }))
        } else {
            None
        };
        let mut heap = context.heap();
        let (exact_keys, layout) = if complete_generation {
            let creation_order = properties
                .keys()
                .filter_map(|property| property.as_ref().cloned())
                .collect::<Vec<_>>();
            let descriptors = creation_order
                .iter()
                .cloned()
                .map(|name| {
                    let attributes = if name == "length" {
                        super::property_inference::DescriptorAttributes {
                            writable: Some(true),
                            enumerable: Some(false),
                            configurable: Some(false),
                        }
                    } else {
                        super::property_inference::DescriptorAttributes::literal(true)
                    };
                    (
                        name,
                        OwnPropertyLayoutDescriptor {
                            kind: OwnPropertyLayoutKind::Data,
                            attributes,
                        },
                    )
                })
                .collect();
            let layout = OwnPropertyLayout::from_creation_order(&creation_order, descriptors);
            let keys = layout.as_ref().map(OwnPropertyLayout::keys);
            (keys, layout)
        } else {
            (None, None)
        };
        heap.record_layout(
            self.heap_address,
            properties,
            exact_keys,
            layout,
            dependencies.clone(),
        );
        let spread_shape_stable = if self.has_spread && exact_layout {
            heap.observe_array_spread_shape(self.heap_address, spread_shape.flatten())
        } else if self.has_spread {
            heap.invalidate_writable(self.heap_address);
            heap.invalidate_dense_array(self.heap_address);
            false
        } else {
            true
        };
        if exact_layout && self.exact_slots && !self.has_hole {
            if self.has_spread {
                heap.invalidate_writable(self.heap_address);
            } else {
                heap.complete_array(self.heap_address, self.dense.len());
            }
            heap.dense_array(self.heap_address, self.dense);
            if self.has_spread && !spread_shape_stable {
                heap.invalidate_dense_array(self.heap_address);
            }
        } else {
            heap.invalidate_writable(self.heap_address);
            heap.invalidate_dense_array(self.heap_address);
        }
        context.array_created(self.allocation, self.has_hole);
        if exact_layout
            && array_allocation_is_fresh
            && self.primitive_only
            && self.qualitative_spread
            && !self.has_hole
        {
            context.primitive_array_created(self.allocation, &self.iterable_dependencies);
        }
        (
            Values::from([AbstractValue::Value(Value::Alloc(
                Kind::Object,
                self.heap_address,
            ))]),
            if exact_layout {
                self.judgment
            } else {
                AllocationJudgment::Opaque
            },
            dependencies,
        )
    }

    pub(super) fn abort(self, context: &mut impl AllocationContext) {
        invalidate_partial_literal(context, self.allocation, false, true);
    }
}

pub(super) trait AllocationContext: OwnDataContext {
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
    fn binding(&mut self, binding: &Id) -> ChildTransfer<AllocationValue>;
    /// Build the callable value for a canonical function allocation. Contexts
    /// without the lexical environment needed to preserve its captures refuse
    /// this operation and let their caller take the conservative path.
    fn function_value(
        &mut self,
        _key: u32,
        _source_covered: bool,
    ) -> ChildTransfer<AllocationValue> {
        ChildTransfer::BeforeExecution
    }
    fn child(
        &mut self,
        expression: &ValueExpr,
        prefix_reached: bool,
    ) -> ChildTransfer<AllocationValue>;
    /// Summarize an unentered child and remaining body after a sibling or
    /// composite operation ran. Implementations must not replay the prefix.
    fn abandon_remainder(&mut self) -> bool {
        self.unmodeled_effect_operation();
        false
    }
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
    fn array_spread(&mut self, _span: Span, _operand: &Values) -> ChildTransfer<ArraySpreadResult> {
        ChildTransfer::BeforeExecution
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
    ) -> ChildTransfer<KeyedCollectionOperationResult> {
        ChildTransfer::BeforeExecution
    }
    /// Compose a checked operation separately from its allocation result.
    /// Allocation-only consumers have no whole-body effect channel.
    fn operation_effects(&mut self, _effects: EffectCoverageFragment) {
        self.unmodeled_effect_operation();
    }
    /// The prepared index confirms this exact single-spread literal shape.
    /// This is syntax coverage only; the shared transfer still proves the
    /// source's settled own-data descriptors and complete key set.
    fn exact_object_spread_copy(&self, _allocation: u32) -> bool {
        false
    }
    /// Record source property/shape support for this copy. Local execution
    /// emits receipts; allocation-only adapters remain opaque by default.
    fn object_spread_copy_support(
        &mut self,
        _source: &OwnDataCopySource,
        _dependencies: &ProofDependencies,
    ) -> bool {
        self.unmodeled_effect_operation();
        false
    }
    /// Conservative handling after the source operand has already run once.
    /// The refusal is diagnostic only; adapters still follow the same fallback.
    fn refused_object_spread(
        &mut self,
        site: CallSite,
        _source: &Values,
        _refusal: ObjectSpreadRefusal,
    ) -> ChildTransfer<AllocationValue> {
        self.unmodeled_effect_operation();
        ChildTransfer::Value(AllocationValue::opaque(Values::from([
            AbstractValue::Value(Value::Alloc(
                Kind::Object,
                self.heap_resolver()
                    .allocation(Kind::Object, site.allocation_id()),
            )),
            AbstractValue::Value(Value::Unavailable),
        ])))
    }
    /// The source is noninvoking own data, but its enumerable subset is
    /// unknown. Keep value containment without certifying owner coverage.
    fn incomplete_object_spread(
        &mut self,
        site: CallSite,
        source: &Values,
        refusal: ObjectSpreadRefusal,
    ) -> ChildTransfer<AllocationValue> {
        self.refused_object_spread(site, source, refusal)
    }
    fn object_spread_dependencies(&self) -> ProofDependencies {
        ProofDependencies::default()
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
pub(super) fn primitive_native_call_plan(
    row: &'static Row,
    arguments: &[Values],
    dependencies: &ProofDependencies,
    expected_revision: u64,
) -> Option<CheckedNativePlan> {
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
    let mut heap = crate::facts::HeapContentCells::default();
    if symbol_registry {
        let registry = crate::facts::EffectCell::GlobalSymbolRegistry;
        heap.reads.insert(registry.clone());
        heap.writes.insert(registry);
    }
    let Return::Prim(kinds) = row.returns else {
        return None;
    };
    Some(CheckedNativePlan {
        expected_revision,
        commit: NativeOperationCommit::None,
        result: NativeOperationResult {
            values: Values::from([AbstractValue::Value(Value::Prim(kinds))]),
            postcondition: None,
        },
        effects,
        dependencies: Box::new(dependencies.clone()),
        heap,
        parameter_reads: BTreeSet::new(),
        allocation: if symbol_registry {
            AllocationJudgment::Opaque
        } else {
            AllocationJudgment::CoveredAllocationOnly(Box::new(dependencies.clone()))
        },
    })
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

pub(super) fn retained_callback_constructor_plan(
    resolver: SourceHeapResolver,
    row: &Row,
    arguments: &[Values],
    dependencies: &ProofDependencies,
    site: CallSite,
    expected_revision: u64,
) -> Option<CheckedNativePlan> {
    let kind = retained_callback_constructor_kind(row)?;
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
    Some(CheckedNativePlan {
        expected_revision,
        commit: NativeOperationCommit::RetainNativeReferences {
            allocation: resolver.allocation(kind, site.allocation_id()),
            values: callbacks.clone(),
        },
        result: NativeOperationResult {
            values: Values::from([AbstractValue::Value(Value::Alloc(
                kind,
                resolver.allocation(kind, site.allocation_id()),
            ))]),
            postcondition: None,
        },
        effects,
        dependencies: Box::new(dependencies.clone()),
        heap: crate::facts::HeapContentCells::default(),
        parameter_reads: BTreeSet::new(),
        allocation: AllocationJudgment::CoveredAllocationOnly(Box::new(dependencies.clone())),
    })
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

/// Prepare an empty keyed-collection constructor without claiming it ran.
/// The caller must execute this plan against the current prefix and retain
/// the resulting ledger before using its normal completion or value.
pub(super) fn empty_keyed_collection_plan(
    resolver: SourceHeapResolver,
    row: &'static Row,
    site: CallSite,
    dependencies: &ProofDependencies,
    expected_revision: u64,
) -> Option<CheckedNativePlan> {
    let kind = empty_keyed_collection_kind(row)?;
    let mut effects = EffectCoverageFragment::no_effects();
    effects.depend_on(dependencies);
    Some(CheckedNativePlan {
        expected_revision,
        commit: NativeOperationCommit::None,
        result: NativeOperationResult {
            values: Values::from([AbstractValue::Value(Value::Alloc(
                kind,
                resolver.allocation(kind, site.allocation_id()),
            ))]),
            postcondition: None,
        },
        effects,
        dependencies: Box::new(dependencies.clone()),
        heap: crate::facts::HeapContentCells::default(),
        parameter_reads: BTreeSet::new(),
        allocation: AllocationJudgment::CoveredAllocationOnly(Box::new(dependencies.clone())),
    })
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
    resolver: SourceHeapResolver,
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
        allocation: AllocationValue::normal_with_dependencies(
            Values::from([AbstractValue::Value(Value::Alloc(
                kind,
                resolver.allocation(kind, site.allocation_id()),
            ))]),
            AllocationJudgment::CoveredAllocationOnly(Box::new(dependencies.clone())),
            dependencies,
        ),
        effects,
    })
}

/// The real shared allocation store; neither consumer supplies app/native facts.
pub(super) struct LiteralHeap<'a> {
    pub(super) surface: &'a mut ObjectSurfaceStore,
    pub(super) private_properties:
        Option<&'a mut OrdMap<HeapAddress, BTreeMap<PrivateFieldId, Values>>>,
    pub(super) arrays: &'a mut OrdMap<HeapAddress, DenseArray>,
    pub(super) revision: &'a mut u64,
    pub(super) track_writable_prefix: bool,
    pub(super) track_spread_shape: bool,
}

pub(super) use super::object_surface::WritablePrefixState;

impl LiteralHeap<'_> {
    pub(super) fn invalidate_surface(&mut self, allocation: HeapAddress) {
        if self.surface.invalidate_surface(allocation) {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Descriptor changes revoke exact enumeration independently of named
    /// values. Keep the loss sticky and version it even if values are equal.
    pub(super) fn mark_unknown_enumerability(&mut self, allocation: HeapAddress) {
        if self.track_spread_shape {
            let mut changed = self.surface.invalidate_surface(allocation);
            changed |= self.surface.copy_enumerability_unknown.insert(allocation);
            if changed {
                *self.revision = self.revision.wrapping_add(1);
            }
        }
    }

    /// A source allocation site can seed writability only on its first
    /// execution and only while its pooled heap has no prior object state.
    /// Reentry, a prior value, or any later execution permanently loses the
    /// per-site proof because the heap does not distinguish allocation
    /// generations.
    pub(super) fn begin_literal(&mut self, allocation: HeapAddress) {
        if !self.track_writable_prefix {
            return;
        }
        let next = if !self
            .surface
            .prefix_object_properties
            .contains_key(&allocation)
            && !self.surface.writable_prefix.contains_key(&allocation)
        {
            WritablePrefixState::InProgress
        } else {
            WritablePrefixState::Unknown
        };
        if self.surface.writable_prefix.get(&allocation) != Some(&next) {
            self.surface.writable_prefix.insert(allocation, next);
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn complete_array(&mut self, allocation: HeapAddress, length: usize) {
        if !self.track_writable_prefix {
            return;
        }
        if self.surface.writable_prefix.get(&allocation) == Some(&WritablePrefixState::InProgress) {
            let next = u32::try_from(length)
                .map(|length| WritablePrefixState::DenseArray { length })
                .unwrap_or(WritablePrefixState::Unknown);
            self.surface.writable_prefix.insert(allocation, next);
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Complete only an uninterrupted first record execution. The sticky
    /// Unknown state is never restored by a later execution of this site.
    pub(super) fn complete_record(
        &mut self,
        allocation: HeapAddress,
        writable_properties: BTreeSet<String>,
    ) {
        if !self.track_writable_prefix {
            return;
        }
        if self.surface.writable_prefix.get(&allocation) == Some(&WritablePrefixState::InProgress) {
            self.surface.writable_prefix.insert(
                allocation,
                WritablePrefixState::Proven(writable_properties.clone()),
            );
            self.surface.extensibility.insert(
                allocation,
                super::object_surface::ExtensibilityKnowledge::Extensible,
            );
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Commit one checked CopyDataProperties result using the same pooled
    /// allocation store as ordinary record literals. Repeated executions join
    /// missing properties with `undefined`; a changing key set loses the
    /// dynamic shape witness permanently.
    pub(super) fn record_spread_copy(
        &mut self,
        allocation: HeapAddress,
        properties: &[(String, Values)],
        complete_generation: bool,
        dependencies: ProofDependencies,
    ) {
        if self.surface.record_spread_copy(
            allocation,
            properties,
            self.track_spread_shape && complete_generation,
            dependencies,
        ) {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Keep an unsupported execution sticky at this pooled destination site.
    /// A later successful invocation cannot recover one exact shape after an
    /// earlier generation may have copied different keys or values.
    pub(super) fn invalidate_spread_copy(&mut self, allocation: HeapAddress) {
        let mut changed = false;
        if self.track_spread_shape {
            changed |= self.surface.spread_copy_history.insert(allocation);
            changed |= self.surface.invalidate_surface(allocation);
        }
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn invalidate_writable(&mut self, allocation: HeapAddress) {
        if !self.track_writable_prefix {
            return;
        }
        let aliases = self
            .surface
            .writable_prefix
            .keys()
            .copied()
            .filter(|candidate| allocation.may_overlap_with_provider_family(*candidate))
            .collect::<Vec<_>>();
        let mut changed = false;
        for candidate in aliases {
            if matches!(
                self.surface.writable_prefix.get(&candidate),
                Some(
                    WritablePrefixState::InProgress
                        | WritablePrefixState::Proven(_)
                        | WritablePrefixState::DenseArray { .. }
                )
            ) {
                self.surface
                    .writable_prefix
                    .insert(candidate, WritablePrefixState::Unknown);
                changed = true;
            }
        }
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn record(
        &mut self,
        allocation: HeapAddress,
        properties: BTreeMap<Option<String>, Values>,
        exact_shape: Option<BTreeSet<String>>,
    ) {
        self.record_layout(
            allocation,
            properties,
            exact_shape,
            None,
            ProofDependencies::default(),
        );
    }

    pub(super) fn record_layout(
        &mut self,
        allocation: HeapAddress,
        properties: BTreeMap<Option<String>, Values>,
        exact_keys: Option<BTreeSet<String>>,
        layout: Option<OwnPropertyLayout>,
        layout_dependencies: ProofDependencies,
    ) {
        let complete_layout = layout
            .as_ref()
            .filter(|layout| exact_keys.as_ref() == Some(&layout.keys()))
            .cloned();
        let mut changed = if let Some(layout) = complete_layout {
            self.surface.observe_own_property_layout(
                allocation,
                Some(layout),
                layout_dependencies.clone(),
            )
        } else {
            self.surface.observe_own_property_evidence(
                allocation,
                exact_keys,
                layout.clone(),
                layout_dependencies,
            )
        };
        changed |= self
            .surface
            .record_data_properties(allocation, properties, layout.as_ref());
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn record_accessor_properties(
        &mut self,
        allocation: HeapAddress,
        properties: BTreeMap<String, super::object_surface::AppliedAccessor>,
        layout: Option<&OwnPropertyLayout>,
    ) {
        if self
            .surface
            .record_accessor_properties(allocation, properties, layout)
        {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Allocation sites may represent several records. Field stores retain
    /// earlier values and version the same heap used by ordinary execution.
    pub(super) fn write_property(
        &mut self,
        allocation: HeapAddress,
        property: Option<String>,
        values: &Values,
    ) {
        let changed = if let Some(property) = property {
            self.surface.unknown_write(allocation, property, values)
        } else {
            self.surface.unknown_dynamic_write(allocation, values)
        };
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn append_array_values(&mut self, allocation: HeapAddress, values: &Values) {
        if self.surface.append_array_values(allocation, values) {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn set_new_own_data_property(
        &mut self,
        allocation: HeapAddress,
        property: String,
        values: &Values,
        attributes: super::property_inference::DescriptorAttributes,
    ) {
        if self
            .surface
            .set_new_own_data_property(allocation, property, values, attributes)
        {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn create_data_property(
        &mut self,
        allocation: HeapAddress,
        property: String,
        values: &Values,
        attributes: super::property_inference::DescriptorAttributes,
    ) {
        if self
            .surface
            .create_data_property(allocation, property, values, attributes)
        {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Join values into an already-proven own data slot without changing its
    /// descriptor or enumerable layout. The caller must supply a current
    /// writable-own-slot proof; this remains a pooled-site value join.
    pub(super) fn write_existing_data_value(
        &mut self,
        allocation: HeapAddress,
        property: String,
        values: &Values,
    ) {
        if self
            .surface
            .set_existing_data_property(allocation, property, values)
        {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Private names never enter the ordinary own-property namespace. Like
    /// public slots, pooled source sites retain every stored value.
    pub(super) fn write_private_property(
        &mut self,
        allocation: HeapAddress,
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

    pub(super) fn invalidate_dense_array(&mut self, allocation: HeapAddress) {
        let aliases = self
            .arrays
            .keys()
            .copied()
            .filter(|candidate| allocation.may_overlap_with_provider_family(*candidate))
            .collect::<Vec<_>>();
        let mut changed = false;
        for candidate in aliases {
            changed |= self.arrays.remove(&candidate).is_some();
        }
        if changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    pub(super) fn dense_array(&mut self, allocation: HeapAddress, slots: Vec<Values>) {
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
        let current_shape = {
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
            }
            properties
                .keys()
                .filter_map(|property| property.as_ref().cloned())
                .collect::<BTreeSet<_>>()
        };
        let shape_mismatch = self
            .surface
            .own_property_certificates
            .get(&allocation)
            .and_then(|certificate| certificate.exact_keys.as_ref())
            .is_some_and(|known| known != &current_shape);
        let surface_changed = shape_mismatch && self.surface.invalidate_surface(allocation);
        let properties_changed =
            before.as_ref() != self.surface.prefix_object_properties.get(&allocation);
        if surface_changed || properties_changed {
            *self.revision = self.revision.wrapping_add(1);
        }
    }

    /// Preserve an index-presence certificate only while every exact spread
    /// generation at this pooled allocation site has the same own index set.
    pub(super) fn observe_array_spread_shape(
        &mut self,
        allocation: HeapAddress,
        shape: Option<BTreeSet<String>>,
    ) -> bool {
        let before = self.surface.array_spread_shapes.get(&allocation).cloned();
        let stable =
            self.surface
                .observe_array_spread_shape(allocation, shape, self.track_spread_shape);
        if self.surface.array_spread_shapes.get(&allocation) != before.as_ref() {
            *self.revision = self.revision.wrapping_add(1);
        }
        stable
    }
}

/// One semantic implementation. A strict consumer delegates children back here;
/// application execution dispatches children through the normal runtime queries.
fn remainder_after_prefix<C: AllocationContext>(context: &mut C) -> ChildTransfer<AllocationValue> {
    ChildTransfer::RemainderAfterPrefix {
        summarized: context.abandon_remainder(),
    }
}

pub(super) fn transfer(
    context: &mut impl AllocationContext,
    expression: &ValueExpr,
) -> ChildTransfer<AllocationValue> {
    let source_covered = context.source_covered();
    let mut judgment = if source_covered {
        AllocationJudgment::CoveredAllocationOnly(Box::default())
    } else {
        AllocationJudgment::Opaque
    };
    match expression {
        ValueExpr::Binding(binding) => match context.binding(binding) {
            ChildTransfer::Value(mut value) => {
                value.ledger.allocation.compose(&judgment);
                add_judgment_dependencies(&mut value.ledger.dependencies, &judgment);
                ChildTransfer::Value(value)
            }
            ChildTransfer::BeforeExecution => ChildTransfer::BeforeExecution,
            remainder @ ChildTransfer::RemainderAfterPrefix { .. } => remainder,
        },
        ValueExpr::Alloc(Kind::RegExp, allocation) => {
            context.unmodeled_effect_operation();
            // A validated literal evaluates through RegExpCreate without
            // looking up the mutable global binding. Track only its fresh
            // allocation-site identity; prototype and internal-slot behavior
            // stay outside this transfer.
            ChildTransfer::Value(AllocationValue::opaque(Values::from([
                AbstractValue::Value(Value::Alloc(
                    Kind::RegExp,
                    context
                        .heap_resolver()
                        .allocation(Kind::RegExp, *allocation),
                )),
            ])))
        }
        ValueExpr::Alloc(Kind::Function, key) => context.function_value(*key, source_covered),
        ValueExpr::New(callee, arguments, site) if source_covered => {
            let operation = match context.keyed_collection_new(callee, arguments, *site) {
                ChildTransfer::Value(operation) => operation,
                ChildTransfer::BeforeExecution => {
                    context.unmodeled_effect_operation();
                    return ChildTransfer::BeforeExecution;
                }
                ChildTransfer::RemainderAfterPrefix { summarized } => {
                    return ChildTransfer::RemainderAfterPrefix { summarized };
                }
            };
            context.operation_effects(operation.effects);
            ChildTransfer::Value(operation.allocation)
        }
        ValueExpr::Undefined => {
            let values = Values::from([AbstractValue::UNDEFINED]);
            if source_covered {
                context.noninvoking_operation();
                ChildTransfer::Value(AllocationValue::normal(values, judgment))
            } else {
                context.unmodeled_effect_operation();
                ChildTransfer::Value(AllocationValue::opaque_with(
                    values,
                    judgment,
                    ProofDependencies::default(),
                ))
            }
        }
        ValueExpr::NonCallable(kinds) => {
            let values = Values::from([AbstractValue::Value(Value::Prim(*kinds))]);
            if source_covered {
                context.noninvoking_operation();
                ChildTransfer::Value(AllocationValue::normal(values, judgment))
            } else {
                context.unmodeled_effect_operation();
                ChildTransfer::Value(AllocationValue::opaque_with(
                    values,
                    judgment,
                    ProofDependencies::default(),
                ))
            }
        }
        ValueExpr::Binary(operator, left, right) => {
            let left = match context.child(left, false) {
                ChildTransfer::Value(left) => left,
                ChildTransfer::BeforeExecution => return ChildTransfer::BeforeExecution,
                remainder @ ChildTransfer::RemainderAfterPrefix { .. } => return remainder,
            };
            judgment.compose(&left.ledger.allocation);
            let mut dependencies = left.ledger.dependencies.as_ref().clone();
            add_judgment_dependencies(&mut dependencies, &judgment);
            let left_completion = left.ledger.completion.clone();
            let Some(left_values) = left_completion.may_normal().cloned() else {
                let (normal, abrupt, non_normal) = allocation_alternatives(left_completion);
                return ChildTransfer::Value(AllocationValue::with_completion(
                    Completion::from_alternatives(normal, abrupt, non_normal),
                    judgment,
                    dependencies,
                ));
            };
            if left_completion.requires_path_partition() {
                return remainder_after_prefix(context);
            }
            let right = match context.child(right, true) {
                ChildTransfer::Value(right) => right,
                ChildTransfer::BeforeExecution => return remainder_after_prefix(context),
                remainder @ ChildTransfer::RemainderAfterPrefix { .. } => return remainder,
            };
            judgment.compose(&right.ledger.allocation);
            dependencies.extend(right.ledger.dependencies.as_ref());
            let right_completion = right.ledger.completion.clone();
            let Some(right_values) = right_completion.may_normal().cloned() else {
                return ChildTransfer::Value(AllocationValue::with_completion(
                    sequence_completion(left_completion, right_completion),
                    judgment,
                    dependencies,
                ));
            };
            if right_completion.requires_path_partition() {
                return remainder_after_prefix(context);
            }
            let prefix = sequence_completion(left_completion, right_completion);
            if prefix.has_no_normal_path() {
                return ChildTransfer::Value(AllocationValue::with_completion(
                    prefix,
                    judgment,
                    dependencies,
                ));
            }
            ChildTransfer::Value(
                match binary_result(*operator, &left_values, &right_values) {
                    PrimitiveOperationResult::Covered(values) if source_covered => {
                        context.noninvoking_operation();
                        let (normal, abrupt, non_normal) = allocation_alternatives(prefix);
                        let completion = Completion::from_alternatives(
                            normal.map(|_| values),
                            abrupt,
                            non_normal,
                        );
                        AllocationValue::with_completion(completion, judgment, dependencies)
                    }
                    PrimitiveOperationResult::Covered(values) => {
                        context.unmodeled_effect_operation();
                        let (normal, abrupt, mut non_normal) = allocation_alternatives(prefix);
                        if normal.is_some() {
                            non_normal.may_diverge = true;
                            non_normal.unresolved = true;
                        }
                        AllocationValue::with_completion(
                            Completion::from_alternatives(
                                normal.map(|_| values),
                                abrupt,
                                non_normal,
                            ),
                            AllocationJudgment::Opaque,
                            dependencies,
                        )
                    }
                    PrimitiveOperationResult::MayThrow(values) => {
                        context.unmodeled_effect_operation();
                        let (normal, abrupt, mut non_normal) = allocation_alternatives(prefix);
                        if normal.is_some() {
                            non_normal.may_throw_without_payload = true;
                        }
                        AllocationValue::with_completion(
                            Completion::from_alternatives(
                                normal.map(|_| values),
                                abrupt,
                                non_normal,
                            ),
                            AllocationJudgment::Opaque,
                            dependencies,
                        )
                    }
                    PrimitiveOperationResult::Abrupt
                        if prefix.is_exact_normal() && source_covered =>
                    {
                        context.unmodeled_effect_operation();
                        AllocationValue::with_completion(
                            Completion::UnknownThrow(UnknownThrowValues::new(top_values())),
                            judgment,
                            dependencies,
                        )
                    }
                    PrimitiveOperationResult::Abrupt
                    | PrimitiveOperationResult::CoercionRequired => {
                        context.unmodeled_effect_operation();
                        AllocationValue::opaque_with(top_values(), judgment, dependencies)
                    }
                },
            )
        }
        ValueExpr::StrictEquality(left, right) => {
            // Strict comparison accepts opaque values without coercion.
            primitive_operation(
                context,
                [left.as_ref(), right.as_ref()],
                AbstractValue::Value(Value::Prim(PrimKinds::BOOLEAN)),
                judgment,
                source_covered,
            )
        }
        ValueExpr::Void(child) => {
            // GetValue(child) is transferred below. Discarding that value has
            // no coercion, invocation or abrupt completion of its own.
            primitive_operation(
                context,
                [child.as_ref()],
                AbstractValue::UNDEFINED,
                judgment,
                source_covered,
            )
        }
        ValueExpr::LogicalNot(child) => {
            // ToBoolean does not invoke object hooks.
            primitive_operation(
                context,
                [child.as_ref()],
                AbstractValue::Value(Value::Prim(PrimKinds::BOOLEAN)),
                judgment,
                source_covered,
            )
        }
        ValueExpr::Sequence(expressions) => {
            context.unmodeled_effect_operation();
            sequence(
                context,
                expressions.iter(),
                judgment,
                |context, expression, prefix_reached| context.child(expression, prefix_reached),
            )
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
                return ChildTransfer::BeforeExecution;
            }
            let mut uncertain = !source_covered;
            let mut dependencies = ProofDependencies::default();
            let mut abrupt_alternatives = Vec::new();
            let mut non_normal = NonNormalPossibilities::default();
            match context.child(test, false) {
                ChildTransfer::Value(test) => {
                    judgment.compose(&test.ledger.allocation);
                    dependencies.extend(test.ledger.dependencies.as_ref());
                    add_judgment_dependencies(&mut dependencies, &judgment);
                    let (test_normal, test_abrupt, test_non_normal) =
                        allocation_alternatives(test.ledger.completion.clone());
                    abrupt_alternatives.extend(test_abrupt);
                    non_normal.may_throw_without_payload |=
                        test_non_normal.may_throw_without_payload;
                    non_normal.may_diverge |= test_non_normal.may_diverge;
                    non_normal.unresolved |= test_non_normal.unresolved;
                    let Some(_) = test_normal else {
                        return ChildTransfer::Value(AllocationValue::with_completion(
                            Completion::from_alternatives(None, abrupt_alternatives, non_normal),
                            judgment,
                            dependencies,
                        ));
                    };
                    if test.ledger.completion.requires_path_partition() {
                        return remainder_after_prefix(context);
                    }
                    uncertain |= !test.ledger.completion.is_exact_normal();
                }
                ChildTransfer::BeforeExecution => return ChildTransfer::BeforeExecution,
                remainder @ ChildTransfer::RemainderAfterPrefix { .. } => return remainder,
            }
            let mut values = Values::new();
            for arm in alternatives.iter() {
                match context.child(arm, true) {
                    ChildTransfer::Value(arm) => {
                        judgment.compose(&arm.ledger.allocation);
                        dependencies.extend(arm.ledger.dependencies.as_ref());
                        let (arm_normal, arm_abrupt, arm_non_normal) =
                            allocation_alternatives(arm.ledger.completion.clone());
                        if arm.ledger.completion.requires_path_partition() {
                            return remainder_after_prefix(context);
                        }
                        if let Some(arm_values) = arm_normal {
                            values.extend(arm_values);
                        }
                        uncertain |= arm_non_normal != NonNormalPossibilities::default();
                        non_normal.may_throw_without_payload |=
                            arm_non_normal.may_throw_without_payload;
                        non_normal.may_diverge |= arm_non_normal.may_diverge;
                        non_normal.unresolved |= arm_non_normal.unresolved;
                        abrupt_alternatives.extend(arm_abrupt);
                    }
                    ChildTransfer::BeforeExecution => return remainder_after_prefix(context),
                    remainder @ ChildTransfer::RemainderAfterPrefix { .. } => return remainder,
                }
            }
            if uncertain && !values.is_empty() {
                non_normal.may_diverge = true;
                non_normal.unresolved = true;
            }
            let completion = Completion::from_alternatives(
                (!values.is_empty()).then_some(values),
                abrupt_alternatives,
                non_normal,
            );
            if uncertain {
                context.unmodeled_effect_operation();
                ChildTransfer::Value(AllocationValue::with_completion(
                    completion,
                    AllocationJudgment::Opaque,
                    dependencies,
                ))
            } else {
                context.noninvoking_operation();
                ChildTransfer::Value(AllocationValue::with_completion(
                    completion,
                    judgment,
                    dependencies,
                ))
            }
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
                    record(
                        context,
                        *allocation,
                        definitions,
                        judgment,
                        |context, expression, prefix_reached| {
                            context.child(expression, prefix_reached)
                        },
                    )
                }
                LiteralLayout::DenseArray(elements) => array(
                    context,
                    *allocation,
                    elements.iter().map(ArrayElement::Value),
                    judgment,
                    |context, expression, prefix_reached| context.child(expression, prefix_reached),
                ),
                _ => ChildTransfer::BeforeExecution,
            }
        }
        ValueExpr::ObjectSpreadLiteral(site, source_expression) => {
            let allocation = site.allocation_id();
            let heap_address = context.heap_resolver().allocation(Kind::Object, allocation);
            context.heap().begin_literal(heap_address);
            let source = match context.child(source_expression, true) {
                ChildTransfer::Value(source) => source,
                ChildTransfer::BeforeExecution => {
                    invalidate_partial_literal(context, allocation, true, false);
                    return remainder_after_prefix(context);
                }
                remainder @ ChildTransfer::RemainderAfterPrefix { .. } => {
                    invalidate_partial_literal(context, allocation, true, false);
                    return remainder;
                }
            };
            let judgment = source.ledger.allocation.clone();
            let mut dependencies = source.ledger.dependencies.as_ref().clone();
            let source_completion = source.ledger.completion.clone();
            let Some(normal_source) = source_completion.may_normal() else {
                invalidate_partial_literal(context, allocation, true, false);
                context.unmodeled_effect_operation();
                return ChildTransfer::Value(AllocationValue::with_completion(
                    source_completion,
                    judgment,
                    dependencies,
                ));
            };
            let normal_source = normal_source.clone();
            let (copy, mut refusal) = if context.exact_object_spread_copy(allocation) {
                match own_data_transfer::copy_source(context, &normal_source) {
                    Ok(copy) => (Some(copy), ObjectSpreadRefusal::SupportReceiptUnavailable),
                    Err(refusal) => (None, refusal),
                }
            } else {
                (None, ObjectSpreadRefusal::ExactCopySyntaxUnavailable)
            };
            dependencies.extend(&context.object_spread_dependencies());
            if let Some(copy) = &copy
                && !copy.enumerability_settled
            {
                // Enumeration may omit any of these data values. Preserve
                // possible containment under an unknown key without invoking
                // a stored callback or emitting exact shape/property reads.
                let payload = copy
                    .properties
                    .iter()
                    .flat_map(|(_, values)| values)
                    .cloned()
                    .collect();
                {
                    let mut heap = context.heap();
                    heap.invalidate_spread_copy(heap_address);
                    heap.mark_unknown_enumerability(heap_address);
                    heap.invalidate_writable(heap_address);
                    heap.record(heap_address, BTreeMap::from([(None, payload)]), None);
                }
                context.unmodeled_effect_operation();
                let incomplete = match context.incomplete_object_spread(
                    *site,
                    &normal_source,
                    ObjectSpreadRefusal::EnumerationNotSettled,
                ) {
                    ChildTransfer::Value(incomplete) => incomplete,
                    ChildTransfer::BeforeExecution => {
                        invalidate_partial_literal(context, allocation, true, false);
                        return remainder_after_prefix(context);
                    }
                    remainder @ ChildTransfer::RemainderAfterPrefix { .. } => {
                        invalidate_partial_literal(context, allocation, true, false);
                        return remainder;
                    }
                };
                add_judgment_dependencies(&mut dependencies, &judgment);
                return ChildTransfer::Value(force_opaque(incomplete, &dependencies));
            }
            if let Some(copy) = copy {
                if context.object_spread_copy_support(&copy, &dependencies) {
                    let complete_generation = context.source_covered()
                        && source_completion.is_exact_normal()
                        && copy.enumerability_settled;
                    let mut copy_judgment =
                        AllocationJudgment::CoveredAllocationOnly(Box::new(dependencies.clone()));
                    copy_judgment.compose(&judgment);
                    let mut writable = BTreeSet::new();
                    for (name, _) in &copy.properties {
                        writable.insert(name.clone());
                    }
                    {
                        let mut heap = context.heap();
                        heap.record_spread_copy(
                            heap_address,
                            &copy.properties,
                            complete_generation,
                            dependencies.clone(),
                        );
                        if complete_generation {
                            heap.complete_record(heap_address, writable);
                        } else {
                            heap.invalidate_surface(heap_address);
                        }
                    }
                    let values = Values::from([AbstractValue::Value(Value::Alloc(
                        Kind::Object,
                        heap_address,
                    ))]);
                    let completion = source_completion.map_normal(|_| values);
                    return ChildTransfer::Value(AllocationValue::with_completion(
                        completion,
                        copy_judgment,
                        dependencies,
                    ));
                }
                refusal = ObjectSpreadRefusal::SupportReceiptUnavailable;
            }
            invalidate_partial_literal(context, allocation, true, false);
            match context.refused_object_spread(*site, &normal_source, refusal) {
                ChildTransfer::Value(refused) => {
                    add_judgment_dependencies(&mut dependencies, &judgment);
                    ChildTransfer::Value(force_opaque(refused, &dependencies))
                }
                ChildTransfer::BeforeExecution => remainder_after_prefix(context),
                remainder @ ChildTransfer::RemainderAfterPrefix { .. } => remainder,
            }
        }
        _ => {
            context.unmodeled_effect_operation();
            ChildTransfer::BeforeExecution
        }
    }
}

/// One scalar-operation result shared by ordered execution and value/provider
/// transfers. Coverage is nonterminal and says nothing about operand accesses.
pub(super) enum PrimitiveOperationResult {
    Covered(Values),
    MayThrow(Values),
    /// The shared primitive transfer proves every represented operand pair
    /// throws before producing a normal value.
    Abrupt,
    CoercionRequired,
}

/// Abstract `ToBoolean` outcomes for values that still have a normal
/// completion. Callers split `Unavailable` first so truthiness never erases
/// an abrupt alternative.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Truthiness {
    AlwaysTruthy,
    AlwaysFalsy,
    Maybe,
}

/// Classify only truthiness implied by an abstract value's runtime kind.
/// Primitive type summaries do not retain literal booleans, numbers or
/// strings, so those kinds can be either truthy or falsy. Unknown carriers
/// preserve both alternatives.
pub(super) fn truthiness(values: &Values) -> Option<Truthiness> {
    let mut may_be_truthy = false;
    let mut may_be_falsy = false;
    let maybe_truthy_primitive = PrimKinds::BOOLEAN
        .union(PrimKinds::NUMBER)
        .union(PrimKinds::BIGINT)
        .union(PrimKinds::STRING);
    let maybe_falsy_primitive = maybe_truthy_primitive
        .union(PrimKinds::NULL)
        .union(PrimKinds::UNDEFINED);

    for value in values {
        match value {
            AbstractValue::Value(Value::Prim(kinds)) => {
                may_be_truthy |= kinds.intersects(maybe_truthy_primitive.union(PrimKinds::SYMBOL));
                may_be_falsy |= kinds.intersects(maybe_falsy_primitive);
            }
            AbstractValue::Value(Value::Alloc(_, _))
            | AbstractValue::Closure(_, _)
            | AbstractValue::ProviderClosure(_, _)
            | AbstractValue::ClassMembers(_)
            | AbstractValue::Instance(_, _, _)
            | AbstractValue::ArrayIntrinsic
            | AbstractValue::GlobalObject
            | AbstractValue::ObjectAssign => may_be_truthy = true,
            AbstractValue::Value(Value::Unavailable) => return None,
            AbstractValue::Value(Value::Top)
            | AbstractValue::SymbolicInput(_)
            | AbstractValue::CallbackFactory
            | AbstractValue::InlineCallbackFactory
            | AbstractValue::DeferredCallbacks(_)
            | AbstractValue::AuditedRetainedResult(_, _)
            | AbstractValue::OwnDescriptorMap(_)
            | AbstractValue::ForeignDeferredRecord
            | AbstractValue::ProviderPrivateGraph(_) => {
                may_be_truthy = true;
                may_be_falsy = true;
            }
        }
    }

    match (may_be_truthy, may_be_falsy) {
        (true, false) => Some(Truthiness::AlwaysTruthy),
        (false, true) => Some(Truthiness::AlwaysFalsy),
        (true, true) => Some(Truthiness::Maybe),
        (false, false) => None,
    }
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
    (!result.is_empty()).then_some(result)
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
    let input_may_throw = operand.contains(&AbstractValue::Value(Value::Unavailable));
    let operation_may_throw = crate::facts::abstract_domain::primitive::update_throws(kinds);
    if input_may_throw {
        PrimitiveOperationResult::MayThrow(normal_values(values))
    } else if result.is_empty() && operation_may_throw {
        PrimitiveOperationResult::Abrupt
    } else if operation_may_throw {
        PrimitiveOperationResult::MayThrow(values)
    } else if result.is_empty() {
        PrimitiveOperationResult::CoercionRequired
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
    if left.is_empty() || right.is_empty() || left_kinds.is_empty() || right_kinds.is_empty() {
        return PrimitiveOperationResult::CoercionRequired;
    }
    let Some(result) = primitive::binary_result(operator, left_kinds, right_kinds) else {
        return PrimitiveOperationResult::CoercionRequired;
    };
    let mut values = Values::new();
    if !result.is_empty() {
        values.insert(AbstractValue::Value(Value::Prim(result)));
    }
    let has_unavailable = left.contains(&AbstractValue::Value(Value::Unavailable))
        || right.contains(&AbstractValue::Value(Value::Unavailable));
    let always_throws = result.is_empty()
        || matches!(operator, BinaryOp::In | BinaryOp::InstanceOf)
        || (matches!(
            operator,
            BinaryOp::Lt | BinaryOp::LtEq | BinaryOp::Gt | BinaryOp::GtEq
        ) && (left_kinds.is_only(PrimKinds::SYMBOL) || right_kinds.is_only(PrimKinds::SYMBOL)));
    if always_throws && !has_unavailable {
        return PrimitiveOperationResult::Abrupt;
    }
    if always_throws {
        return PrimitiveOperationResult::CoercionRequired;
    }
    let may_throw = has_unavailable
        || crate::facts::abstract_domain::primitive::binary_throws(
            operator,
            left_kinds,
            right_kinds,
        );
    if may_throw {
        PrimitiveOperationResult::MayThrow(normal_values(values))
    } else {
        PrimitiveOperationResult::Covered(values)
    }
}

/// For primitive operands only, template ToString has the same completion
/// and result kinds as concatenation with a String. Objects remain refused:
/// templates use a string hint, whereas addition uses the default hint.
pub(super) fn template_substitution_result(operand: &Values) -> PrimitiveOperationResult {
    binary_result(
        BinaryOp::Add,
        &Values::from([AbstractValue::Value(Value::Prim(PrimKinds::STRING))]),
        operand,
    )
}

/// Source-ordered composition retains every child's effects and possible
/// abrupt completion even though only the final completing value survives.
pub(super) fn sequence<'a, C: AllocationContext, E: 'a>(
    context: &mut C,
    expressions: impl IntoIterator<Item = &'a E>,
    mut judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E, bool) -> ChildTransfer<AllocationValue>,
) -> ChildTransfer<AllocationValue> {
    let source_covered = context.source_covered();
    let mut completion = Completion::Normal(Values::from([AbstractValue::UNDEFINED]));
    let mut dependencies = ProofDependencies::default();
    let mut prefix_reached = false;
    for expression in expressions {
        if completion.has_no_normal_path() {
            break;
        }
        let evaluated = match child(context, expression, prefix_reached) {
            ChildTransfer::Value(value) => value,
            ChildTransfer::BeforeExecution if prefix_reached => {
                return remainder_after_prefix(context);
            }
            ChildTransfer::BeforeExecution => return ChildTransfer::BeforeExecution,
            remainder @ ChildTransfer::RemainderAfterPrefix { .. } => return remainder,
        };
        prefix_reached = true;
        judgment.compose(&evaluated.ledger.allocation);
        dependencies.extend(evaluated.ledger.dependencies.as_ref());
        let child_completion = evaluated.ledger.completion;
        completion = sequence_completion(completion, child_completion.clone());
        if child_completion.has_no_normal_path() {
            break;
        }
        if child_completion.requires_path_partition() {
            return remainder_after_prefix(context);
        }
    }
    if !source_covered {
        let (normal, abrupt, mut non_normal) = allocation_alternatives(completion);
        if normal.is_some() {
            non_normal.may_diverge = true;
            non_normal.unresolved = true;
        }
        completion = Completion::from_alternatives(normal, abrupt, non_normal);
    }
    ChildTransfer::Value(AllocationValue::with_completion(
        completion,
        judgment,
        dependencies,
    ))
}

/// Noncoercing primitive operations discard operand values, never their access
/// evidence or abrupt outcomes. Later operands run only on a completing path.
fn primitive_operation<'a>(
    context: &mut impl AllocationContext,
    operands: impl IntoIterator<Item = &'a ValueExpr>,
    result: AbstractValue,
    mut judgment: AllocationJudgment,
    source_covered: bool,
) -> ChildTransfer<AllocationValue> {
    let mut completion = Completion::Normal(Values::from([AbstractValue::UNDEFINED]));
    let mut dependencies = ProofDependencies::default();
    let mut prefix_reached = false;
    let mut reaches_operation = true;
    for operand in operands {
        if completion.has_no_normal_path() {
            break;
        }
        let operand = match context.child(operand, prefix_reached) {
            ChildTransfer::Value(value) => value,
            ChildTransfer::BeforeExecution if prefix_reached => {
                return remainder_after_prefix(context);
            }
            ChildTransfer::BeforeExecution => return ChildTransfer::BeforeExecution,
            remainder @ ChildTransfer::RemainderAfterPrefix { .. } => return remainder,
        };
        prefix_reached = true;
        judgment.compose(&operand.ledger.allocation);
        dependencies.extend(operand.ledger.dependencies.as_ref());
        let operand_completion = operand.ledger.completion;
        completion = sequence_completion(completion, operand_completion.clone());
        if operand_completion.has_no_normal_path() {
            reaches_operation = false;
            break;
        }
        if operand_completion.requires_path_partition() {
            return remainder_after_prefix(context);
        }
    }
    let (normal, abrupt, mut non_normal) = allocation_alternatives(completion);
    let normal = if reaches_operation {
        normal.map(|_| Values::from([result]))
    } else {
        None
    };
    if reaches_operation && !source_covered && normal.is_some() {
        non_normal.may_diverge = true;
        non_normal.unresolved = true;
    }
    let completion = Completion::from_alternatives(normal, abrupt, non_normal);
    if reaches_operation {
        if source_covered {
            context.noninvoking_operation();
        } else {
            context.unmodeled_effect_operation();
        }
    }
    ChildTransfer::Value(AllocationValue::with_completion(
        completion,
        judgment,
        dependencies,
    ))
}

/// Shared record evaluation, including the application's opaque-key fallback.
pub(super) fn record<'a, C: AllocationContext, E: 'a>(
    context: &mut C,
    allocation: u32,
    properties: impl IntoIterator<Item = PropertyDefinition<&'a E>>,
    mut judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E, bool) -> ChildTransfer<AllocationValue>,
) -> ChildTransfer<AllocationValue> {
    let properties: Vec<_> = properties.into_iter().collect();
    let source_covered = context.source_covered();
    let Some(mut store) = RecordTransfer::begin(context, allocation, &properties) else {
        return ChildTransfer::BeforeExecution;
    };
    let mut completion = Completion::Normal(Values::from([AbstractValue::UNDEFINED]));
    let mut dependencies = ProofDependencies::default();
    let mut may_complete = true;
    for (index, definition) in properties.iter().enumerate() {
        if completion.has_no_normal_path() {
            break;
        }
        let child_value = match child(context, *definition.value(), true) {
            ChildTransfer::Value(value) => value,
            ChildTransfer::BeforeExecution => {
                store.abort(context);
                return remainder_after_prefix(context);
            }
            remainder @ ChildTransfer::RemainderAfterPrefix { .. } => {
                store.abort(context);
                return remainder;
            }
        };
        let child_completion = child_value.ledger.completion.clone();
        judgment.compose(&child_value.ledger.allocation);
        dependencies.extend(child_value.ledger.dependencies.as_ref());
        completion = sequence_completion(completion, child_completion.clone());
        if let Some(values) = child_completion.may_normal() {
            // This is descriptor payload containment. Its ordinary-data
            // eligibility remains guarded by the canonical descriptor tag.
            store
                .record_child(index, values)
                .expect("record state matches validated source properties");
        }
        // Even when an earlier opaque child makes the joined completion
        // opaque, a definitely abrupt child prevents every later initializer.
        if child_completion.has_no_normal_path() {
            may_complete = false;
            break;
        }
        if child_completion.requires_path_partition() {
            store.abort(context);
            return remainder_after_prefix(context);
        }
    }
    let exact_normal = source_covered && completion.is_exact_normal();
    add_judgment_dependencies(&mut dependencies, &judgment);
    let result = store.finish(context, may_complete, exact_normal, dependencies.clone());
    if !exact_normal {
        judgment = AllocationJudgment::Opaque;
    }
    let (prior_normal, abrupt, mut non_normal) = allocation_alternatives(completion);
    let normal = if may_complete {
        prior_normal.map(|_| result.expect("completed record has a value"))
    } else {
        None
    };
    if may_complete && !source_covered {
        non_normal.may_diverge = true;
        non_normal.unresolved = true;
    }
    let completion = Completion::from_alternatives(normal, abrupt, non_normal);
    ChildTransfer::Value(AllocationValue::with_completion(
        completion,
        judgment,
        dependencies,
    ))
}

/// No-spread array creation shares ordered child transfer with local execution.
/// Holes are absent own properties, and never acquire a dense-array proof.
pub(super) fn array<'a, C: AllocationContext, E: 'a>(
    context: &mut C,
    allocation: u32,
    elements: impl IntoIterator<Item = ArrayElement<'a, E>>,
    judgment: AllocationJudgment,
    child: impl FnMut(&mut C, &'a E, bool) -> ChildTransfer<AllocationValue>,
) -> ChildTransfer<AllocationValue> {
    let address = context.heap_resolver().allocation(Kind::Object, allocation);
    array_at(context, allocation, address, elements, judgment, child)
}

/// Same dense-array transfer for a caller that already owns the source- or
/// provider-qualified heap address for this allocation site.
pub(super) fn array_at<'a, C: AllocationContext, E: 'a>(
    context: &mut C,
    allocation: u32,
    heap_address: HeapAddress,
    elements: impl IntoIterator<Item = ArrayElement<'a, E>>,
    mut judgment: AllocationJudgment,
    mut child: impl FnMut(&mut C, &'a E, bool) -> ChildTransfer<AllocationValue>,
) -> ChildTransfer<AllocationValue> {
    // ArrayCreate and own index/length definitions execute no user code.
    // Elements compose their effects below; a hole installs no property and
    // supplies no iterator or inherited-property proof.
    let mut transfer = ArrayTransfer::begin_at(context, allocation, heap_address);
    let source_covered = context.source_covered();
    let mut dependencies = ProofDependencies::default();
    let mut completion = Completion::Normal(Values::from([AbstractValue::UNDEFINED]));
    let mut may_complete = true;
    for element in elements {
        if completion.has_no_normal_path() {
            break;
        }
        match element {
            ArrayElement::Hole => transfer.hole(),
            ArrayElement::Value(element) => {
                let evaluated = match child(context, element, true) {
                    ChildTransfer::Value(value) => value,
                    ChildTransfer::BeforeExecution => {
                        transfer.abort(context);
                        return remainder_after_prefix(context);
                    }
                    remainder @ ChildTransfer::RemainderAfterPrefix { .. } => {
                        transfer.abort(context);
                        return remainder;
                    }
                };
                let child_completion = evaluated.ledger.completion.clone();
                judgment.compose(&evaluated.ledger.allocation);
                dependencies.extend(evaluated.ledger.dependencies.as_ref());
                transfer.record_dependencies(evaluated.ledger.dependencies.as_ref());
                completion = sequence_completion(completion, child_completion.clone());
                if let Some(values) = child_completion.may_normal() {
                    transfer.record_child(values);
                }
                if child_completion.has_no_normal_path() {
                    may_complete = false;
                    break;
                }
                if child_completion.requires_path_partition() {
                    transfer.abort(context);
                    return remainder_after_prefix(context);
                }
            }
            ArrayElement::Spread(element, span) => {
                transfer.start_spread();
                let evaluated = match child(context, element, true) {
                    ChildTransfer::Value(value) => value,
                    ChildTransfer::BeforeExecution => {
                        transfer.abort(context);
                        return remainder_after_prefix(context);
                    }
                    remainder @ ChildTransfer::RemainderAfterPrefix { .. } => {
                        transfer.abort(context);
                        return remainder;
                    }
                };
                let operand_completion = evaluated.ledger.completion.clone();
                judgment.compose(&evaluated.ledger.allocation);
                dependencies.extend(evaluated.ledger.dependencies.as_ref());
                transfer.record_dependencies(evaluated.ledger.dependencies.as_ref());
                completion = sequence_completion(completion, operand_completion.clone());
                let Some(operand) = operand_completion.may_normal() else {
                    may_complete = false;
                    break;
                };
                if operand_completion.requires_path_partition() {
                    transfer.abort(context);
                    return remainder_after_prefix(context);
                }
                match transfer.record_spread(context, span, operand) {
                    ChildTransfer::Value(()) => {}
                    ChildTransfer::BeforeExecution => {
                        transfer.abort(context);
                        return remainder_after_prefix(context);
                    }
                    ChildTransfer::RemainderAfterPrefix { summarized } => {
                        transfer.abort(context);
                        return ChildTransfer::RemainderAfterPrefix { summarized };
                    }
                }
            }
        }
    }
    let result = if may_complete {
        add_judgment_dependencies(&mut dependencies, &judgment);
        let (result, transfer_judgment, transfer_dependencies) = transfer.finish(
            context,
            source_covered && completion.is_exact_normal(),
            dependencies.clone(),
        );
        judgment.compose(&transfer_judgment);
        dependencies.extend(&transfer_dependencies);
        result
    } else {
        transfer.abort(context);
        Values::new()
    };
    let (prior_normal, abrupt, mut non_normal) = allocation_alternatives(completion);
    let normal = if may_complete {
        prior_normal.map(|_| result)
    } else {
        None
    };
    if may_complete && !source_covered {
        non_normal.may_diverge = true;
        non_normal.unresolved = true;
    }
    let completion = Completion::from_alternatives(normal, abrupt, non_normal);
    ChildTransfer::Value(AllocationValue::with_completion(
        completion,
        judgment,
        dependencies,
    ))
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
    Some(AllocationValue::normal(
        values.clone(),
        AllocationJudgment::CoveredAllocationOnly(Box::default()),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::abstract_domain::value::{HeapModuleScope, ProviderCallableId};

    #[test]
    fn primitive_operation_completion_keeps_unknown_and_mixed_inputs_conservative() {
        let symbol = Values::from([AbstractValue::Value(Value::Prim(PrimKinds::SYMBOL))]);
        let number = Values::from([AbstractValue::Value(Value::Prim(PrimKinds::NUMBER))]);
        let unavailable = Values::from([AbstractValue::Value(Value::Unavailable)]);
        let mixed = Values::from([AbstractValue::Value(Value::Prim(
            PrimKinds::SYMBOL.union(PrimKinds::NUMBER),
        ))]);

        assert!(matches!(
            binary_result(BinaryOp::Add, &symbol, &number),
            PrimitiveOperationResult::Abrupt
        ));
        assert!(matches!(
            binary_result(BinaryOp::Add, &mixed, &number),
            PrimitiveOperationResult::MayThrow(values)
                if values.contains(&AbstractValue::Value(Value::Prim(PrimKinds::NUMBER)))
                    && !values.contains(&AbstractValue::Value(Value::Unavailable))
        ));
        assert!(matches!(
            binary_result(BinaryOp::Add, &unavailable, &number),
            PrimitiveOperationResult::CoercionRequired
        ));
        assert_eq!(
            normal_values(Values::from([
                AbstractValue::Value(Value::Prim(PrimKinds::NUMBER)),
                AbstractValue::Value(Value::Unavailable),
            ])),
            Values::from([AbstractValue::Value(Value::Top)])
        );
        assert!(matches!(
            binary_result(BinaryOp::Add, &Values::new(), &number),
            PrimitiveOperationResult::CoercionRequired
        ));
        assert!(matches!(
            update_result(&symbol),
            PrimitiveOperationResult::Abrupt
        ));
        assert!(matches!(
            update_result(&mixed),
            PrimitiveOperationResult::MayThrow(values)
                if values.contains(&AbstractValue::Value(Value::Prim(PrimKinds::NUMBER)))
                    && !values.contains(&AbstractValue::Value(Value::Unavailable))
        ));
        assert!(matches!(
            update_result(&unavailable),
            PrimitiveOperationResult::CoercionRequired
        ));
        assert!(matches!(
            template_substitution_result(&symbol),
            PrimitiveOperationResult::Abrupt
        ));
        assert!(matches!(
            template_substitution_result(&unavailable),
            PrimitiveOperationResult::CoercionRequired
        ));
    }

    #[test]
    fn opaque_prefix_never_becomes_an_exact_abrupt_or_normal_result() {
        let completion = sequence_completion(
            Completion::OpaqueMayNormal(Values::from([AbstractValue::PRIMITIVE])),
            Completion::Abrupt(()),
        );
        let (normal, abrupt, non_normal) = completion.into_alternatives();
        assert!(normal.is_none());
        assert!(matches!(
            abrupt.as_slice(),
            [AbruptAlternative::Unknown(Some(values))] if values.0 == top_values()
        ));
        assert!(non_normal.unresolved);
        assert!(non_normal.may_diverge);
        assert!(!non_normal.may_throw_without_payload);
        assert_eq!(
            sequence_completion(
                Completion::Normal(Values::from([AbstractValue::UNDEFINED])),
                Completion::Abrupt(()),
            ),
            Completion::UnknownThrow(UnknownThrowValues::new(top_values()))
        );
    }

    #[test]
    fn opaque_normal_projection_does_not_create_typed_throw_evidence() {
        let (normal, abrupt, non_normal) =
            Completion::<Values, ()>::OpaqueMayNormal(top_values()).into_alternatives();
        assert_eq!(normal, Some(top_values()));
        assert!(abrupt.is_empty());
        assert!(non_normal.unresolved);
        assert!(non_normal.may_diverge);
        assert!(!non_normal.may_throw_without_payload);
    }

    #[test]
    fn repeated_pooled_literal_site_cannot_restore_writable_prefix() {
        let mut surface = ObjectSurfaceStore::default();
        let mut arrays = OrdMap::new();
        let mut revision = 0;
        let allocation = HeapAddress::local_family(
            crate::facts::abstract_domain::value::LocalAnalysisScopeId::fresh(),
            Kind::Object,
            7,
        );
        let stored = BTreeMap::from([(
            Some("value".to_owned()),
            Values::from([AbstractValue::PRIMITIVE]),
        )]);

        {
            let mut heap = LiteralHeap {
                surface: &mut surface,
                private_properties: None,
                arrays: &mut arrays,
                revision: &mut revision,
                track_writable_prefix: true,
                track_spread_shape: true,
            };
            heap.begin_literal(allocation);
            assert_eq!(
                heap.surface.writable_prefix.get(&allocation),
                Some(&WritablePrefixState::InProgress)
            );
            heap.record(
                allocation,
                stored.clone(),
                Some(BTreeSet::from(["value".to_owned()])),
            );
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
                private_properties: None,
                arrays: &mut arrays,
                revision: &mut revision,
                track_writable_prefix: true,
                track_spread_shape: true,
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

    #[test]
    fn provider_alias_invalidation_retires_writable_and_dense_array_proofs() {
        let producer = HeapModuleScope::Chunk(source_identity::ChunkId(4));
        let recipe = HeapAddress::ProviderAllocationFamily {
            consumer: HeapModuleScope::Chunk(source_identity::ChunkId(9)),
            call_origin: 31,
            producer: ProviderCallableId {
                module: producer,
                origin: 42,
            },
            producer_birth: None,
            slot: 0,
            kind: Kind::Object,
        };
        let source = HeapAddress::module_family(source_identity::ChunkId(4), Kind::Object, 27);
        let unrelated = HeapAddress::module_family(source_identity::ChunkId(5), Kind::Object, 27);
        let mut surface = ObjectSurfaceStore::default();
        surface.writable_prefix.insert(
            recipe,
            WritablePrefixState::Proven(BTreeSet::from(["slot".into()])),
        );
        surface.writable_prefix.insert(
            source,
            WritablePrefixState::Proven(BTreeSet::from(["slot".into()])),
        );
        surface.writable_prefix.insert(
            unrelated,
            WritablePrefixState::Proven(BTreeSet::from(["slot".into()])),
        );
        let mut arrays = OrdMap::new();
        for address in [recipe, source, unrelated] {
            arrays.insert(
                address,
                DenseArray {
                    slots: vec![Values::from([AbstractValue::PRIMITIVE])],
                },
            );
        }
        let mut revision = 0;
        let mut heap = LiteralHeap {
            surface: &mut surface,
            private_properties: None,
            arrays: &mut arrays,
            revision: &mut revision,
            track_writable_prefix: true,
            track_spread_shape: true,
        };

        heap.invalidate_writable(source);
        heap.invalidate_dense_array(source);

        assert_eq!(
            heap.surface.writable_prefix.get(&recipe),
            Some(&WritablePrefixState::Unknown)
        );
        assert_eq!(
            heap.surface.writable_prefix.get(&source),
            Some(&WritablePrefixState::Unknown)
        );
        assert!(heap.arrays.get(&recipe).is_none());
        assert!(heap.arrays.get(&source).is_none());
        assert!(heap.arrays.get(&unrelated).is_some());
        assert!(revision >= 2);
    }
}
