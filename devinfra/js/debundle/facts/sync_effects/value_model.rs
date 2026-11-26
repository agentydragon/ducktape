//! Abstract value vocabulary and value relationships used by synchronous execution analysis.

use super::world::WorldScope;
use analysis::facts::abstract_domain::builtins::IterationRow;
use analysis::facts::abstract_domain::value::{Kind, Value};
use analysis::facts::value_flow::static_prop as static_string_prop;
use analysis::inference::{FactContext, IterativeFacts, IterativeInference, IterativeRules};
use analysis_model::PrimKinds;
use imbl::OrdMap;
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::hash::{DefaultHasher, Hash, Hasher};
use std::ops::Bound::{Excluded, Unbounded};
use std::sync::{Arc, Weak};
use swc_common::{BytePos, Span};
use swc_ecma_ast::*;

pub(super) fn static_prop(property: &MemberProp) -> Option<String> {
    static_string_prop(property).or_else(|| match property {
        MemberProp::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Num(number))
                if number.value >= 0.0
                    && number.value <= u32::MAX as f64
                    && number.value.fract() == 0.0 =>
            {
                Some((number.value as u32).to_string())
            }
            _ => None,
        },
        _ => None,
    })
}

/// Sorted unique values. `None` is the only empty representation, so derived
/// ordering remains the lexicographic element order. Clones share immutable
/// storage until a write, including snapshots used by pointer witnesses.
#[derive(Clone, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct Values(Option<Arc<Vec<AbstractValue>>>);

impl std::fmt::Debug for Values {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Values(")?;
        f.debug_set().entries(self.iter()).finish()?;
        f.write_str(")")
    }
}

impl Hash for Values {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.len().hash(state);
        for value in self.iter() {
            value.hash(state);
        }
    }
}

impl Values {
    pub(super) fn new() -> Self {
        Self::default()
    }

    pub(super) fn first(&self) -> Option<&AbstractValue> {
        self.as_slice().first()
    }

    fn as_slice(&self) -> &[AbstractValue] {
        self.0.as_deref().map_or(&[], Vec::as_slice)
    }

    pub(super) fn len(&self) -> usize {
        self.as_slice().len()
    }

    pub(super) fn iter(&self) -> std::slice::Iter<'_, AbstractValue> {
        self.as_slice().iter()
    }

    pub(super) fn contains(&self, value: &AbstractValue) -> bool {
        self.as_slice().binary_search(value).is_ok()
    }

    pub(super) fn ptr_eq(&self, other: &Self) -> bool {
        match (&self.0, &other.0) {
            (None, None) => true,
            (Some(left), Some(right)) => Arc::ptr_eq(left, right),
            _ => false,
        }
    }

    pub(super) fn insert(&mut self, value: AbstractValue) -> bool {
        let Err(index) = self.as_slice().binary_search(&value) else {
            return false;
        };
        match &mut self.0 {
            Some(values) => Arc::make_mut(values).insert(index, value),
            None => self.0 = Some(Arc::new(vec![value])),
        }
        true
    }

    /// Insert the values not already present and report whether any was.
    /// Duplicate-only inputs leave shared storage untouched.
    pub(super) fn extend_absent<'v>(
        &mut self,
        values: impl IntoIterator<Item = &'v AbstractValue>,
    ) -> bool {
        let absent: Vec<_> = values
            .into_iter()
            .filter(|value| !self.contains(value))
            .cloned()
            .collect();
        let changed = !absent.is_empty();
        self.extend(absent);
        changed
    }

    pub(super) fn remove(&mut self, value: &AbstractValue) -> bool {
        let Ok(index) = self.as_slice().binary_search(value) else {
            return false;
        };
        if self.len() == 1 {
            self.0 = None;
        } else {
            Arc::make_mut(self.0.as_mut().unwrap()).remove(index);
        }
        true
    }

    pub(super) fn is_empty(&self) -> bool {
        self.0.is_none()
    }

    pub(super) fn is_disjoint(&self, other: &Self) -> bool {
        self.iter().all(|value| !other.contains(value))
    }
}

/// A transient value set as a sorted, deduplicated vector: the elements and
/// iteration order of the `Values` the same items would build, without
/// persistent-set insertion. For inputs that are only iterated.
pub(super) fn ordered_values(
    values: impl IntoIterator<Item = AbstractValue>,
) -> Vec<AbstractValue> {
    let mut values: Vec<_> = values.into_iter().collect();
    values.sort_unstable();
    values.dedup();
    values
}

impl Extend<AbstractValue> for Values {
    fn extend<T: IntoIterator<Item = AbstractValue>>(&mut self, values: T) {
        let incoming = ordered_values(values);
        if incoming.iter().all(|value| self.contains(value)) {
            return;
        }
        let Some(existing) = self.0.take() else {
            self.0 = Some(Arc::new(incoming));
            return;
        };
        let mut merged = Vec::with_capacity(existing.len() + incoming.len());
        let mut existing = Arc::unwrap_or_clone(existing).into_iter().peekable();
        let mut incoming = incoming.into_iter().peekable();
        while let (Some(left), Some(right)) = (existing.peek(), incoming.peek()) {
            match left.cmp(right) {
                Ordering::Less => merged.push(existing.next().unwrap()),
                Ordering::Equal => {
                    incoming.next();
                    merged.push(existing.next().unwrap());
                }
                Ordering::Greater => merged.push(incoming.next().unwrap()),
            }
        }
        merged.extend(existing);
        merged.extend(incoming);
        self.0 = Some(Arc::new(merged));
    }
}

impl FromIterator<AbstractValue> for Values {
    fn from_iter<T: IntoIterator<Item = AbstractValue>>(values: T) -> Self {
        let values = ordered_values(values);
        Self((!values.is_empty()).then(|| Arc::new(values)))
    }
}

impl<const N: usize> From<[AbstractValue; N]> for Values {
    fn from(values: [AbstractValue; N]) -> Self {
        values.into_iter().collect()
    }
}

impl IntoIterator for Values {
    type Item = AbstractValue;
    type IntoIter = ValuesIntoIter;

    fn into_iter(self) -> Self::IntoIter {
        match self.0.map(Arc::try_unwrap) {
            None => ValuesIntoIter::Owned(Vec::new().into_iter()),
            Some(Ok(values)) => ValuesIntoIter::Owned(values.into_iter()),
            Some(Err(values)) => ValuesIntoIter::Shared {
                remaining: 0..values.len(),
                values,
            },
        }
    }
}

impl<'a> IntoIterator for &'a Values {
    type Item = &'a AbstractValue;
    type IntoIter = std::slice::Iter<'a, AbstractValue>;

    fn into_iter(self) -> Self::IntoIter {
        self.iter()
    }
}

/// A shared consuming iterator clones only the elements it yields.
#[derive(Clone)]
pub enum ValuesIntoIter {
    Owned(std::vec::IntoIter<AbstractValue>),
    Shared {
        remaining: std::ops::Range<usize>,
        values: Arc<Vec<AbstractValue>>,
    },
}

impl Iterator for ValuesIntoIter {
    type Item = AbstractValue;

    fn next(&mut self) -> Option<Self::Item> {
        match self {
            Self::Owned(values) => values.next(),
            Self::Shared { remaining, values } => {
                remaining.next().map(|index| values[index].clone())
            }
        }
    }

    fn size_hint(&self) -> (usize, Option<usize>) {
        match self {
            Self::Owned(values) => values.size_hint(),
            Self::Shared { remaining, .. } => remaining.size_hint(),
        }
    }
}

impl DoubleEndedIterator for ValuesIntoIter {
    fn next_back(&mut self) -> Option<Self::Item> {
        match self {
            Self::Owned(values) => values.next_back(),
            Self::Shared { remaining, values } => {
                remaining.next_back().map(|index| values[index].clone())
            }
        }
    }
}

impl ExactSizeIterator for ValuesIntoIter {}
impl std::iter::FusedIterator for ValuesIntoIter {}

pub(super) type Context = OrdMap<Id, Values>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct CallSite {
    start: u32,
    end: u32,
}

impl From<Span> for CallSite {
    fn from(span: Span) -> Self {
        Self {
            start: span.lo.0,
            end: span.hi.0,
        }
    }
}

impl AbstractValue {
    pub(super) const UNDEFINED: Self = Self::Value(Value::Prim(PrimKinds::UNDEFINED));
    /// A primitive of untracked type.
    pub(super) const PRIMITIVE: Self = Self::Value(Value::Prim(PrimKinds::ANY));

    /// Whether this value is a primitive. `Top` and `Unavailable` are
    /// excluded: either can represent a non-primitive result or a throwing
    /// evaluation.
    pub(super) fn is_primitive(&self) -> bool {
        matches!(self, Self::Value(Value::Prim(_)))
    }
}

impl CallSite {
    pub(super) fn allocation_id(self) -> u32 {
        self.start
    }

    pub(super) fn source_span(self) -> Span {
        Span::new(BytePos(self.start), BytePos(self.end))
    }
}

/// Contexts are interned bottom-up by the executor. Structural equality of
/// captures becomes identity equality, so comparing value sets and cache keys
/// never recursively compares a shared capture DAG along every path.
#[derive(Debug, Clone)]
pub struct Captured<T> {
    identity: usize,
    pub(super) values: Arc<T>,
}

impl<T> Captured<T> {
    pub(super) fn with_identity(identity: usize, values: Arc<T>) -> Self {
        Self { identity, values }
    }

    pub(super) fn cache_identity(&self) -> usize {
        self.identity
    }
}

/// Canonical descriptions stay interned while an actual value or semantic
/// root owns them. The table itself never keeps their payloads alive.
pub(super) struct CaptureInterner<T> {
    buckets: BTreeMap<u64, Vec<WeakCapture<T>>>,
    next_identity: usize,
    sweep_after: Option<u64>,
}

struct WeakCapture<T> {
    identity: usize,
    values: Weak<T>,
}

impl<T> Default for CaptureInterner<T> {
    fn default() -> Self {
        Self {
            buckets: BTreeMap::new(),
            next_identity: 0,
            sweep_after: None,
        }
    }
}

impl<T: Eq + Hash> CaptureInterner<T> {
    pub(super) fn intern(&mut self, description: T) -> Captured<T> {
        let mut hash = DefaultHasher::new();
        description.hash(&mut hash);
        let bucket = self.buckets.entry(hash.finish()).or_default();
        bucket.retain(|entry| entry.values.strong_count() != 0);
        for entry in bucket.iter() {
            if let Some(values) = entry.values.upgrade()
                && values.as_ref() == &description
            {
                return Captured::with_identity(entry.identity, values);
            }
        }
        let identity = self.next_identity;
        self.next_identity = identity.checked_add(1).expect("capture identity exhausted");
        let values = Arc::new(description);
        bucket.push(WeakCapture {
            identity,
            values: Arc::downgrade(&values),
        });
        Captured::with_identity(identity, values)
    }

    /// Payloads drop with their last strong owner. Retire a bounded batch of
    /// dead table entries too, without scanning all live captures per owner.
    pub(super) fn sweep_dead(&mut self) {
        const BATCH: usize = 64;
        let lower = self.sweep_after.map_or(Unbounded, Excluded);
        let keys: Vec<_> = self
            .buckets
            .range((lower, Unbounded))
            .map(|(&key, _)| key)
            .take(BATCH)
            .collect();
        for key in &keys {
            let bucket = self.buckets.get_mut(key).expect("collected bucket");
            bucket.retain(|entry| entry.values.strong_count() != 0);
            if bucket.is_empty() {
                self.buckets.remove(key);
            }
        }
        self.sweep_after = keys.last().copied();
    }
}

impl<T> Hash for Captured<T> {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.identity.hash(state);
    }
}

/// One instantiated lexical cell, not a source binding or structural cache key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) struct LexicalCellId(pub(super) usize);

#[derive(Debug, Clone, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct LexicalCaptures {
    pub(super) snapshots: Context,
    pub(super) cells: OrdMap<Id, LexicalCellId>,
    /// Source containment only, never a trusted receiver or instance identity.
    pub(super) home_object: Option<u32>,
}

impl Hash for LexicalCaptures {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.snapshots.len().hash(state);
        for (binding, values) in &self.snapshots {
            binding.hash(state);
            values.hash(state);
        }
        self.home_object.hash(state);
        self.cells.len().hash(state);
        for (binding, cell) in &self.cells {
            binding.hash(state);
            cell.hash(state);
        }
    }
}

impl LexicalCaptures {
    pub(super) fn is_empty(&self) -> bool {
        self.snapshots.is_empty() && self.cells.is_empty() && self.home_object.is_none()
    }
}

pub(super) type CapturedContext = Captured<LexicalCaptures>;

impl<T> PartialEq for Captured<T> {
    fn eq(&self, other: &Self) -> bool {
        self.identity == other.identity
    }
}

impl<T> Eq for Captured<T> {}

impl<T> PartialOrd for Captured<T> {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl<T> Ord for Captured<T> {
    fn cmp(&self, other: &Self) -> Ordering {
        self.identity.cmp(&other.identity)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RetainedResultKind {
    Factory,
    Constructor,
}

/// Query ownership, not a stable provider or runtime allocation identity. These
/// opaque values become argument positions and never enter importer state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) struct ProviderScopeId(u64);

impl ProviderScopeId {
    pub(super) fn fresh() -> Option<Self> {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        NEXT.try_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
            next.checked_add(1)
        })
        .ok()
        .map(Self)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) enum ProviderInput {
    Argument(usize),
    Capture(usize),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SymbolicInputId {
    pub(super) scope: ProviderScopeId,
    pub(super) input: ProviderInput,
}

/// A callable's function allocation in one world. The scope keeps an
/// isolated provider query's closures apart from the application's, even when
/// both create the same source callable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct HeapAddress {
    pub(super) scope: WorldScope,
    pub(super) callable: u32,
}

/// Provenance of a local class capability; neither variant proves allocation identity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum InstanceOrigin {
    Constructed,
    Projected,
}

/// The interpreter's values: the shared lattice plus carriers for the
/// callable and heap summaries the lattice does not express yet.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum AbstractValue {
    Value(Value),
    /// Unknown kind/capabilities; only forwarding, storing and returning this
    /// query-local input is covered. It proves no shape or native identity.
    SymbolicInput(SymbolicInputId),
    /// A function object: its callable's address in the creating world and
    /// the lexical captures it retains.
    Closure(HeapAddress, CapturedContext),
    /// A class's static or prototype surface can expose its deferred methods.
    /// Merely creating the class does not execute those methods.
    ClassMembers(u32),
    /// A constructed local class instance, or an opaque value read from one,
    /// retains its class methods and constructor argument capabilities. Only
    /// Constructed proves the value itself has the class's instance kind.
    /// Neither origin identifies a unique runtime allocation.
    Instance(u32, Captured<Values>, InstanceOrigin),
    CallbackFactory,
    InlineCallbackFactory,
    /// An opaque returned capability retaining live input values. Its capture
    /// identity is only an abstract cache key: it proves no runtime equality,
    /// fresh allocation, object shape or privacy of the retained inputs.
    DeferredCallbacks(Captured<Values>),
    /// An audited factory's ordinary graph or a constructor's instance graph.
    /// Factory inputs require Factory provenance. This admission capability
    /// is revocable on exposure or mutation; its capture key is not a runtime
    /// allocation identity. Member projection and invocation remain opaque.
    AuditedRetainedResult(RetainedResultKind, Captured<Values>),
    /// Exact own-descriptor entries captured from one source-proved ordinary
    /// object at a native call site. The executor may apply the certificate
    /// through the paired defineProperties call; all other uses keep the
    /// carrier opaque.
    OwnDescriptorMap(CallSite),
    /// A foreign source factory's deferred object graph. Provider captures are
    /// retained in its imported summary; using this token activates the full
    /// conservative importer binding/callback pool, never foreign SWC IDs.
    ForeignDeferredRecord,
    ObjectAssign,
    /// Array constructor/prototype capability obtained from a proven array.
    ArrayIntrinsic,
    GlobalObject,
}

#[derive(Debug, Clone)]
pub(super) struct ArrayIteration {
    pub(super) row: &'static IterationRow,
    pub(super) source: ValueExpr,
    pub(super) callback: ValueExpr,
    /// `reduce`'s initial accumulator.
    pub(super) initial: Option<ValueExpr>,
    pub(super) result: Option<u32>,
}

#[derive(Debug, Clone)]
pub(super) struct ArrayElement {
    pub(super) spread: Option<swc_common::Span>,
    pub(super) value: ValueExpr,
}

/// Slot values are an ordered overapproximation. The ordinary Object origin
/// remains the identity used by every mutation, publication and taint lane.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct DenseArray {
    pub(super) slots: Vec<Values>,
}

/// Result of transferring the ordinary Array iterator over a settled dense
/// array at one interpreter prefix. Operation adapters check their own
/// iterator-protocol premises; this transfer supplies the exact receiver and
/// ordered heap slots. Operand evaluation keeps possible throws in its
/// existing `Unavailable` path; a transfer refuses any non-singleton value.
#[derive(Debug, Clone, PartialEq)]
pub(super) enum DenseArrayIterationTransfer {
    Refused,
    Yielded { source: u32, slots: Vec<Values> },
}

#[derive(Debug, Clone)]
pub(super) struct FromEntriesMappedProof {
    pub(super) source: ValueExpr,
    pub(super) key: String,
    pub(super) map_call_site: CallSite,
    pub(super) object_intrinsic: Id,
}

/// Ordered literal definitions keep accessor payloads distinct from data values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum LiteralPropertyKind {
    Data,
    Method,
    Getter,
    Setter,
    Opaque,
}

#[derive(Debug, Clone)]
pub(super) enum PropertyDefinition<T> {
    Data { name: Option<String>, value: T },
    Method { name: Option<String>, value: T },
    Getter { name: Option<String>, value: T },
    Setter { name: Option<String>, value: T },
    Opaque { name: Option<String>, value: T },
}

impl<T> PropertyDefinition<T> {
    pub(super) fn kind(&self) -> LiteralPropertyKind {
        match self {
            Self::Data { .. } => LiteralPropertyKind::Data,
            Self::Method { .. } => LiteralPropertyKind::Method,
            Self::Getter { .. } => LiteralPropertyKind::Getter,
            Self::Setter { .. } => LiteralPropertyKind::Setter,
            Self::Opaque { .. } => LiteralPropertyKind::Opaque,
        }
    }
    pub(super) fn new(kind: LiteralPropertyKind, name: Option<String>, value: T) -> Self {
        match kind {
            LiteralPropertyKind::Data => Self::Data { name, value },
            LiteralPropertyKind::Method => Self::Method { name, value },
            LiteralPropertyKind::Getter => Self::Getter { name, value },
            LiteralPropertyKind::Setter => Self::Setter { name, value },
            LiteralPropertyKind::Opaque => Self::Opaque { name, value },
        }
    }
    pub(super) fn name(&self) -> Option<&str> {
        match self {
            Self::Data { name, .. }
            | Self::Method { name, .. }
            | Self::Getter { name, .. }
            | Self::Setter { name, .. }
            | Self::Opaque { name, .. } => name.as_deref(),
        }
    }
    pub(super) fn value(&self) -> &T {
        match self {
            Self::Data { value, .. }
            | Self::Method { value, .. }
            | Self::Getter { value, .. }
            | Self::Setter { value, .. }
            | Self::Opaque { value, .. } => value,
        }
    }
}

/// Syntax-only shape of one statically named own property. Evaluated values
/// stay in the executor's ordinary object identity store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum OwnPropertyDescriptorSyntax {
    Data,
    Accessor {
        getter: Option<u32>,
        setter: Option<u32>,
    },
}

/// Source-ordered string properties of one supported ordinary object literal.
/// The map carries descriptor syntax; `creation_order` preserves insertion
/// order for ECMAScript own-key enumeration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OwnPropertyShape {
    pub(super) properties: BTreeMap<String, OwnPropertyDescriptorSyntax>,
    pub(super) creation_order: Vec<String>,
}

/// Returns ECMAScript's own string-key order for a source-ordered ordinary
/// object shape: array-index keys numerically first, followed by other string
/// keys in their original insertion order. `BTreeMap` order is never a proxy
/// for JavaScript property order.
pub(super) fn ordered_own_property_names(creation_order: &[String]) -> Vec<String> {
    let mut indices = Vec::new();
    let mut strings = Vec::new();
    let mut seen = BTreeSet::new();
    for name in creation_order {
        if !seen.insert(name.as_str()) {
            continue;
        }
        if let Some(index) = canonical_array_index(name) {
            indices.push((index, name.clone()));
        } else {
            strings.push(name.clone());
        }
    }
    indices.sort_unstable_by_key(|(index, _)| *index);
    indices
        .into_iter()
        .map(|(_, name)| name)
        .chain(strings)
        .collect()
}

/// Whether a property name has the canonical spelling and range of an ECMAScript
/// array index. `u32::MAX` is reserved and is not an array index.
pub(super) fn canonical_array_index(name: &str) -> Option<u32> {
    name.parse::<u32>()
        .ok()
        .filter(|index| *index != u32::MAX && index.to_string() == name)
}

/// How a member read names its property.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum MemberKey {
    /// `object.name`.
    Static,
    /// `object[expression]`; the identifier when the expression is one.
    Computed(Option<Id>),
}

impl MemberKey {
    pub(super) fn of(property: &MemberProp) -> Self {
        match property {
            MemberProp::Ident(_) => Self::Static,
            MemberProp::Computed(key) => match key.expr.unwrap_parens() {
                Expr::Ident(binding) => Self::Computed(Some(binding.to_id())),
                _ => Self::Computed(None),
            },
            MemberProp::PrivateName(_) => Self::Computed(None),
        }
    }

    pub(super) fn is_static(&self) -> bool {
        *self == Self::Static
    }

    /// A literal index needs no key evaluation. Computed identifiers and
    /// other dynamic keys cannot use the present own-data read proof.
    pub(super) fn supports_own_data_read(&self, property: &str) -> bool {
        matches!(self, Self::Static)
            || (matches!(self, Self::Computed(None)) && canonical_array_index(property).is_some())
    }
}

// Lowered syntax is immutable. Cloned facts share recursive children while
// each evaluation keeps its own interpreter state.
#[derive(Debug, Clone)]
pub(super) enum ValueExpr {
    Binding(Id),
    /// A source allocation site. RegExp literals use this form but do not
    /// imply ordinary own properties, prototype methods, or protocol behavior.
    Alloc(Kind, u32),
    ObjectLiteral(u32, Arc<[(Option<String>, Self)]>),
    Undefined,
    /// A primitive of the given types: a literal, a template, or a value
    /// known only to be primitive.
    NonCallable(PrimKinds),
    Member(Arc<Self>, Option<String>, MemberKey, CallSite),
    Call(Arc<Self>, Arc<[Self]>, CallSite),
    New(Arc<Self>, Arc<[Self]>, CallSite),
    /// Source spelling of `Object.getPrototypeOf([][Symbol.iterator]())`.
    /// Native identity remains conditional on Object, Array, and Symbol.
    ArrayIteratorPrototype(Arc<Self>),
    /// Source spelling of `Object.getPrototypeOf(new Set()[Symbol.iterator]())`.
    /// Its path is conditional on the Set constructor and iterator protocol.
    SetIteratorPrototype(Arc<Self>),
    /// An operator result is primitive on successful evaluation. This is a
    /// value-kind fact, not a purity claim: keep operands so calls and
    /// unavailable reads inside them still flow through the evaluator.
    Primitive(Arc<[Self]>),
    /// Both strict equality operators produce an unknown Boolean without coercion.
    StrictEquality(Arc<Self>, Arc<Self>),
    Sequence(Arc<[Self]>),
    LogicalNot(Arc<Self>),
    Void(Arc<Self>),
    Conditional {
        test: Arc<Self>,
        alternatives: Arc<[Self; 2]>,
    },
    Alternatives(Arc<[Self]>),
    Unknown,
}

#[derive(Debug, Clone, Default)]
pub(super) struct ClassValueFacts {
    pub(super) instance_fields: BTreeMap<String, ValueExpr>,
    pub(super) ordinary_instance_methods: BTreeSet<String>,
    pub(super) default_has_instance: bool,
    pub(super) static_fields: BTreeMap<String, ValueExpr>,
    pub(super) super_class: Option<ValueExpr>,
    /// Reads in the original heritage, including operands omitted by value lowering.
    pub(super) super_class_inputs: BTreeSet<Id>,
    /// Inline callables and classes need the enclosing-environment fallback.
    pub(super) super_class_inputs_incomplete: bool,
    /// A discarded direct call followed by a lexical superclass read. Its
    /// argument inputs can be dropped only after proving the callee inert.
    pub(super) discarded_heritage_call: Option<DiscardedHeritageCall>,
    pub(super) lexical_heritage: Option<LexicalClassHeritage>,
    pub(super) opaque_instance: bool,
    pub(super) opaque_static: bool,
    pub(super) allocation_without_user_code: bool,
}

#[derive(Debug, Clone)]
pub(super) struct DiscardedHeritageCall {
    pub(super) callee: Id,
    pub(super) base: Id,
    pub(super) argument_reads: BTreeSet<Id>,
    pub(super) remaining_inputs: BTreeSet<Id>,
}

impl DiscardedHeritageCall {
    pub(super) fn collect(expression: &Expr) -> Option<Self> {
        let Expr::Seq(sequence) = expression.unwrap_parens() else {
            return None;
        };
        let [discarded, base] = sequence.exprs.as_slice() else {
            return None;
        };
        let Expr::Call(call) = discarded.unwrap_parens() else {
            return None;
        };
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let (Expr::Ident(callee), Expr::Ident(base)) =
            (callee.unwrap_parens(), base.unwrap_parens())
        else {
            return None;
        };
        // Argument evaluation remains on the ordinary timing path. Limit the
        // projection to bare reads and literals, with no spread or nested
        // user code that could retain a value or change the selected base.
        if call.type_args.is_some()
            || call.args.iter().any(|argument| {
                argument.spread.is_some()
                    || !matches!(
                        argument.expr.unwrap_parens(),
                        Expr::Ident(_)
                            | Expr::Lit(
                                Lit::Str(_)
                                    | Lit::Bool(_)
                                    | Lit::Null(_)
                                    | Lit::Num(_)
                                    | Lit::BigInt(_)
                            )
                    )
            })
        {
            return None;
        }
        Some(Self {
            callee: callee.to_id(),
            base: base.to_id(),
            argument_reads: call
                .args
                .iter()
                .filter_map(|argument| match argument.expr.unwrap_parens() {
                    Expr::Ident(binding) => Some(binding.to_id()),
                    _ => None,
                })
                .collect(),
            remaining_inputs: [callee.to_id(), base.to_id()].into_iter().collect(),
        })
    }
}

/// The complete canonical heritage expression is one lexical read. Lowered
/// values alone cannot establish this: a sequence or assignment can retain
/// its final binding while erasing earlier evaluation.
#[derive(Debug, Clone)]
pub(super) struct LexicalClassHeritage {
    binding: Id,
}

impl LexicalClassHeritage {
    pub(super) fn collect(expression: &Expr) -> Option<Self> {
        let Expr::Ident(binding) = expression.unwrap_parens() else {
            return None;
        };
        Some(Self {
            binding: binding.to_id(),
        })
    }

    pub(super) fn binding(&self) -> &Id {
        &self.binding
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum ClassSurface {
    Constructor,
    Instance,
}

#[derive(Debug, Clone, Default)]
pub(super) struct NamespaceValueFacts {
    classes: BTreeMap<u32, ClassValueFacts>,
    verified_data_objects: BTreeSet<u32>,
    /// A binding is resolved only when the retained syntax has exactly one
    /// assignment for it. Reassignments and alternatives remain opaque.
    bindings: BTreeMap<Id, ValueExpr>,
    binding_assignments: BTreeMap<Id, Vec<ValueExpr>>,
    unstable_class_bindings: BTreeSet<Id>,
}

/// Class provenance is reachability, not a traversal of assignment paths.
/// Each edge reads one Boolean fact, so a shared tail is evaluated once per
/// incoming edge and an unseeded binding cycle stays false at completion.
struct LocalClassProvenance {
    edges: Vec<(Id, Id)>,
    #[cfg(test)]
    expression_visits: usize,
}

impl LocalClassProvenance {
    fn compile(facts: &NamespaceValueFacts) -> (Self, BTreeSet<Id>) {
        let mut rules = Self {
            edges: Vec::new(),
            #[cfg(test)]
            expression_visits: 0,
        };
        let mut seeds = BTreeSet::new();
        for (target, values) in &facts.binding_assignments {
            for value in values {
                rules.collect(target, value, facts, &mut seeds);
            }
        }
        (rules, seeds)
    }

    fn collect(
        &mut self,
        target: &Id,
        value: &ValueExpr,
        facts: &NamespaceValueFacts,
        seeds: &mut BTreeSet<Id>,
    ) {
        #[cfg(test)]
        {
            self.expression_visits += 1;
        }
        match value {
            ValueExpr::Alloc(Kind::Class, _) => {
                seeds.insert(target.clone());
            }
            ValueExpr::Binding(source) => self.edges.push((target.clone(), source.clone())),
            ValueExpr::Alternatives(_) | ValueExpr::Conditional { .. } => {
                for alternative in value.result_alternatives().expect("alternative expression") {
                    self.collect(target, alternative, facts, seeds);
                }
            }
            ValueExpr::Member(object, _, _, _) => {
                // A uniquely resolved instance also has a class surface. Other
                // receivers contribute precisely their possible class values.
                if facts.resolve_class(object, &mut BTreeSet::new()).is_some() {
                    seeds.insert(target.clone());
                } else {
                    self.collect(target, object, facts, seeds);
                }
            }
            _ => {}
        }
    }

    fn solve(self, seeds: BTreeSet<Id>) -> IterativeFacts<Id, ()> {
        let edge_count = self.edges.len();
        let mut inference = IterativeInference::new(self);
        for binding in seeds {
            inference.seed(binding, ());
        }
        inference.solve_values_resumable(0..edge_count)
    }
}

impl IterativeRules for LocalClassProvenance {
    type Key = Id;
    type Fact = ();
    type Rule = usize;
    type MergeScratch = ();
    const SOURCE_ONLY: bool = true;

    fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Id, ()>) {
        let (target, source) = &self.edges[*rule];
        if context.read(source.clone()).is_some() {
            context.propose(target.clone(), ());
        }
    }

    fn merge(_: &Id, _: &mut (), _: ()) -> bool {
        false
    }
}

fn iterator_prototype_receiver(call: &CallExpr) -> Option<&Expr> {
    let Callee::Expr(outer_callee) = &call.callee else {
        return None;
    };
    let Expr::Member(outer_member) = outer_callee.unwrap_parens() else {
        return None;
    };
    if !matches!(outer_member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object")
        || !matches!(&outer_member.prop, MemberProp::Ident(id) if id.sym == "getPrototypeOf")
    {
        return None;
    }
    let [argument] = call.args.as_slice() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Call(iterator_call) = argument.expr.unwrap_parens() else {
        return None;
    };
    if !iterator_call.args.is_empty() {
        return None;
    }
    let Callee::Expr(iterator_callee) = &iterator_call.callee else {
        return None;
    };
    let Expr::Member(iterator_member) = iterator_callee.unwrap_parens() else {
        return None;
    };
    if !matches!(&iterator_member.prop, MemberProp::Computed(key)
        if matches!(key.expr.unwrap_parens(), Expr::Member(symbol)
            if matches!(symbol.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Symbol")
                && matches!(&symbol.prop, MemberProp::Ident(id) if id.sym == "iterator")))
    {
        return None;
    }
    Some(iterator_member.obj.unwrap_parens())
}

fn array_iterator_prototype_expression(call: &CallExpr) -> bool {
    let Some(Expr::Array(array)) = iterator_prototype_receiver(call) else {
        return false;
    };
    array.elems.is_empty()
}

fn set_iterator_prototype_expression(call: &CallExpr) -> bool {
    let Some(Expr::New(set)) = iterator_prototype_receiver(call) else {
        return false;
    };
    matches!(set.callee.unwrap_parens(), Expr::Ident(id) if id.sym == "Set")
        && set.args.as_deref().is_none_or(|args| args.is_empty())
}

impl ValueExpr {
    /// Possible returned values; the conditional test is evaluated separately.
    pub(super) fn result_alternatives(&self) -> Option<&[Self]> {
        match self {
            Self::Alternatives(values) => Some(values),
            Self::Sequence(values) => Some(&values[values.len().saturating_sub(1)..]),
            Self::Conditional { alternatives, .. } => Some(alternatives.as_slice()),
            _ => None,
        }
    }

    pub(super) fn from_expr(expr: &Expr) -> Self {
        Self::from_expr_with_coverage(expr).0
    }

    /// Preserve coverage in the same recursive lowering that produces values.
    /// This is syntax coverage only; input access and runtime transfer evidence
    /// are required before any allocation-only judgment may be issued.
    pub(super) fn from_expr_with_coverage(expr: &Expr) -> (Self, bool) {
        let mut covered = true;
        let value = Self::lower(expr, &mut covered);
        (value, covered)
    }

    fn lower(expr: &Expr, covered: &mut bool) -> Self {
        if !matches!(
            expr.unwrap_parens(),
            Expr::Ident(_)
                | Expr::Object(_)
                | Expr::Array(_)
                | Expr::Cond(_)
                | Expr::Seq(_)
                | Expr::Lit(
                    Lit::Str(_)
                        | Lit::Num(_)
                        | Lit::Bool(_)
                        | Lit::Null(_)
                        | Lit::BigInt(_)
                        | Lit::Regex(_)
                )
        ) && !matches!(expr.unwrap_parens(), Expr::Bin(bin)
            if matches!(bin.op, BinaryOp::EqEqEq | BinaryOp::NotEqEq))
            && !matches!(expr.unwrap_parens(), Expr::Unary(unary)
                if matches!(unary.op, UnaryOp::Void | UnaryOp::Bang))
        {
            *covered = false;
        }
        match expr.unwrap_parens() {
            Expr::Ident(ident) => Self::Binding(ident.to_id()),
            Expr::Fn(function) => Self::Alloc(Kind::Function, function.function.span.lo.0),
            Expr::Arrow(arrow) => Self::Alloc(Kind::Function, arrow.span.lo.0),
            Expr::Class(class) => Self::Alloc(Kind::Class, class.class.span.lo.0),
            Expr::Object(object) => Self::ObjectLiteral(
                object.span.lo.0,
                object
                    .props
                    .iter()
                    .map(|property| match property {
                        PropOrSpread::Spread(spread) => {
                            *covered = false;
                            (None, Self::lower(&spread.expr, covered))
                        }
                        PropOrSpread::Prop(property) => match property.as_ref() {
                            Prop::KeyValue(pair) => {
                                if !matches!(
                                    pair.key,
                                    PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                                ) || static_property_name(&pair.key).as_deref()
                                    == Some("__proto__")
                                {
                                    *covered = false;
                                }
                                (
                                    object_data_property_name(&pair.key),
                                    Self::lower(&pair.value, covered),
                                )
                            }
                            Prop::Shorthand(ident) => {
                                (Some(ident.sym.to_string()), Self::Binding(ident.to_id()))
                            }
                            Prop::Assign(assign) => {
                                *covered = false;
                                (
                                    Some(assign.key.sym.to_string()),
                                    Self::lower(&assign.value, covered),
                                )
                            }
                            Prop::Method(method) => {
                                *covered = false;
                                (
                                    static_property_name(&method.key),
                                    Self::Alloc(Kind::Function, method.function.span.lo.0),
                                )
                            }
                            Prop::Getter(getter) => {
                                *covered = false;
                                (
                                    static_property_name(&getter.key),
                                    Self::Alloc(Kind::Function, getter.function.span.lo.0),
                                )
                            }
                            Prop::Setter(setter) => {
                                *covered = false;
                                (
                                    static_property_name(&setter.key),
                                    Self::Alloc(Kind::Function, setter.function.span.lo.0),
                                )
                            }
                        },
                    })
                    .collect(),
            ),
            Expr::Array(array) => Self::ObjectLiteral(
                array.span.lo.0,
                array
                    .elems
                    .iter()
                    .filter_map(|element| {
                        let Some(element) = element else {
                            *covered = false;
                            return None;
                        };
                        if element.spread.is_some() {
                            *covered = false;
                        }
                        Some((None, Self::lower(&element.expr, covered)))
                    })
                    .collect(),
            ),
            Expr::Member(member) => Self::Member(
                Arc::new(Self::lower(&member.obj, covered)),
                static_prop(&member.prop),
                MemberKey::of(&member.prop),
                member.span.into(),
            ),
            Expr::OptChain(chain) => match chain.base.as_ref() {
                OptChainBase::Call(call) if call.args.iter().all(|arg| arg.spread.is_none()) => {
                    // Keep a known local call's returned callable reachable.
                    // The undefined alternative overapproximates optional
                    // short-circuiting without dropping the invocation path.
                    Self::Alternatives(
                        [
                            Self::Undefined,
                            // Preserve the prior unknown publication capability;
                            // timing precision must not admit namespace contracts.
                            Self::Unknown,
                            Self::Call(
                                Arc::new(Self::lower(&call.callee, covered)),
                                call.args
                                    .iter()
                                    .map(|arg| Self::lower(&arg.expr, covered))
                                    .collect(),
                                call.span.into(),
                            ),
                        ]
                        .into(),
                    )
                }
                OptChainBase::Member(member) => {
                    let mut options = vec![
                        Self::Undefined,
                        Self::Member(
                            Arc::new(Self::lower(&member.obj, covered)),
                            static_prop(&member.prop),
                            MemberKey::of(&member.prop),
                            member.span.into(),
                        ),
                    ];
                    if !matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                        || !matches!(member.prop, MemberProp::Ident(_))
                    {
                        options.push(Self::Unknown);
                    }
                    Self::Alternatives(options.into())
                }
                // Spread calls have no modeled result shape.
                _ => Self::Unknown,
            },
            Expr::Call(call) if array_iterator_prototype_expression(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    unreachable!("recognized iterator-prototype expression is an ordinary call")
                };
                Self::ArrayIteratorPrototype(Arc::new(Self::Call(
                    Arc::new(Self::lower(callee, covered)),
                    call.args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, covered))
                        .collect(),
                    call.span.into(),
                )))
            }
            Expr::Call(call) if set_iterator_prototype_expression(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    unreachable!("recognized iterator-prototype expression is an ordinary call")
                };
                Self::SetIteratorPrototype(Arc::new(Self::Call(
                    Arc::new(Self::lower(callee, covered)),
                    call.args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, covered))
                        .collect(),
                    call.span.into(),
                )))
            }
            Expr::Call(call) if call.args.iter().all(|arg| arg.spread.is_none()) => {
                match &call.callee {
                    Callee::Expr(callee) => Self::Call(
                        Arc::new(Self::lower(callee, covered)),
                        call.args
                            .iter()
                            .map(|arg| Self::lower(&arg.expr, covered))
                            .collect(),
                        call.span.into(),
                    ),
                    Callee::Import(_) | Callee::Super(_) => Self::Unknown,
                }
            }
            Expr::New(new_expr)
                if new_expr
                    .args
                    .as_deref()
                    .unwrap_or_default()
                    .iter()
                    .all(|arg| arg.spread.is_none()) =>
            {
                Self::New(
                    Arc::new(Self::lower(&new_expr.callee, covered)),
                    new_expr
                        .args
                        .as_deref()
                        .unwrap_or_default()
                        .iter()
                        .map(|arg| Self::lower(&arg.expr, covered))
                        .collect(),
                    new_expr.span.into(),
                )
            }
            Expr::Cond(cond) => Self::Conditional {
                test: Arc::new(Self::lower(&cond.test, covered)),
                alternatives: Arc::new([
                    Self::lower(&cond.cons, covered),
                    Self::lower(&cond.alt, covered),
                ]),
            },
            Expr::Bin(bin) if matches!(bin.op, BinaryOp::EqEqEq | BinaryOp::NotEqEq) => {
                Self::StrictEquality(
                    Arc::new(Self::lower(&bin.left, covered)),
                    Arc::new(Self::lower(&bin.right, covered)),
                )
            }
            Expr::Bin(bin) if bin.op.may_short_circuit() => Self::Alternatives(
                [
                    Self::lower(&bin.left, covered),
                    Self::lower(&bin.right, covered),
                ]
                .into(),
            ),
            Expr::Bin(bin) => Self::Primitive(
                [
                    Self::lower(&bin.left, covered),
                    Self::lower(&bin.right, covered),
                ]
                .into(),
            ),
            Expr::Unary(unary) if unary.op == UnaryOp::Void => {
                Self::Void(Arc::new(Self::lower(&unary.arg, covered)))
            }
            Expr::Unary(unary) if unary.op == UnaryOp::Bang => {
                Self::LogicalNot(Arc::new(Self::lower(&unary.arg, covered)))
            }
            Expr::Unary(unary) => Self::Primitive([Self::lower(&unary.arg, covered)].into()),
            Expr::Update(update) => Self::Primitive([Self::lower(&update.arg, covered)].into()),
            Expr::Seq(seq) => {
                let mut sequence_covered = true;
                let expressions: Vec<_> = seq
                    .exprs
                    .iter()
                    .map(|expression| Self::lower(expression, &mut sequence_covered))
                    .collect();
                *covered &= sequence_covered;
                if sequence_covered {
                    Self::Sequence(expressions.into())
                } else {
                    // Uncovered calls/writes are executed by the indexed
                    // timing path. Do not replay them to reconstruct a value.
                    expressions.into_iter().last().unwrap_or(Self::Unknown)
                }
            }
            Expr::Assign(assign) => Self::lower(&assign.right, covered),
            Expr::Lit(Lit::Regex(regex)) => Self::Alloc(Kind::RegExp, regex.span.lo.0),
            Expr::Lit(literal) => Self::NonCallable(match literal {
                Lit::Str(_) => PrimKinds::STRING,
                Lit::Num(_) => PrimKinds::NUMBER,
                Lit::Bool(_) => PrimKinds::BOOLEAN,
                Lit::Null(_) => PrimKinds::NULL,
                Lit::BigInt(_) => PrimKinds::BIGINT,
                Lit::Regex(_) | Lit::JSXText(_) => PrimKinds::ANY,
            }),
            Expr::Tpl(_) => Self::NonCallable(PrimKinds::STRING),
            _ => Self::Unknown,
        }
    }

    pub(super) fn bindings(&self, result: &mut BTreeSet<Id>) {
        match self {
            Self::Binding(id) => {
                result.insert(id.clone());
            }
            Self::ArrayIteratorPrototype(expression)
            | Self::SetIteratorPrototype(expression)
            | Self::LogicalNot(expression)
            | Self::Void(expression) => expression.bindings(result),
            Self::StrictEquality(left, right) => {
                left.bindings(result);
                right.bindings(result);
            }
            Self::Conditional { test, alternatives } => {
                test.bindings(result);
                for alternative in alternatives.iter() {
                    alternative.bindings(result);
                }
            }
            Self::Member(object, _, _, _) => object.bindings(result),
            Self::Call(callee, args, _) | Self::New(callee, args, _) => {
                callee.bindings(result);
                for arg in args.iter() {
                    arg.bindings(result);
                }
            }
            Self::Alternatives(options) | Self::Sequence(options) => {
                for option in options.iter() {
                    option.bindings(result);
                }
            }
            Self::Primitive(operands) => {
                for operand in operands.iter() {
                    operand.bindings(result);
                }
            }
            Self::ObjectLiteral(_, properties) => {
                for (_, value) in properties.iter() {
                    value.bindings(result);
                }
            }
            Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) | Self::Unknown => {}
        }
    }

    /// Whether lowering may have omitted inputs used to produce this value.
    /// A function or class allocation can capture locals without naming them
    /// in the value expression; computed keys and unknown syntax can also
    /// hide their operands.
    pub(super) fn bindings_may_be_incomplete(&self) -> bool {
        match self {
            Self::Unknown | Self::Alloc(Kind::Function | Kind::Class, _) => true,
            Self::Binding(_) | Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) => false,
            Self::ArrayIteratorPrototype(value)
            | Self::SetIteratorPrototype(value)
            | Self::LogicalNot(value)
            | Self::Void(value) => value.bindings_may_be_incomplete(),
            Self::StrictEquality(left, right) => {
                left.bindings_may_be_incomplete() || right.bindings_may_be_incomplete()
            }
            Self::Conditional { test, alternatives } => {
                test.bindings_may_be_incomplete()
                    || alternatives.iter().any(Self::bindings_may_be_incomplete)
            }
            Self::Member(object, _, key, _) => {
                !matches!(key, MemberKey::Static) || object.bindings_may_be_incomplete()
            }
            Self::Call(callee, args, _) | Self::New(callee, args, _) => {
                callee.bindings_may_be_incomplete()
                    || args.iter().any(Self::bindings_may_be_incomplete)
            }
            Self::Alternatives(values) | Self::Sequence(values) | Self::Primitive(values) => {
                values.iter().any(Self::bindings_may_be_incomplete)
            }
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(name, value)| name.is_none() || value.bindings_may_be_incomplete()),
        }
    }

    /// Whether this value has an explicit namespace path through its syntax or
    /// a binding already proven to carry that path. Unrelated call/constructor
    /// results are covered by the namespace contract's author audit, while
    /// their use as mutation receivers is handled separately below.
    pub(super) fn carries_namespace(&self, id: &Id, aliases: &BTreeSet<Id>) -> bool {
        match self {
            Self::Binding(binding) => binding == id || aliases.contains(binding),
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.carries_namespace(id, aliases)
            }
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.carries_namespace(id, aliases)),
            Self::Primitive(_) | Self::StrictEquality(..) | Self::LogicalNot(_) | Self::Void(_) => {
                false
            }
            Self::Alternatives(_) | Self::Conditional { .. } | Self::Sequence(_) => self
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| value.carries_namespace(id, aliases)),
            Self::Member(object, _, _, _) => {
                matches!(object.as_ref(), Self::Call(callee, _, _)
                    if Self::is_direct_namespace_member_callee(callee, id))
                    || object.carries_namespace(id, aliases)
            }
            Self::Call(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || (!Self::is_direct_namespace_member_callee(callee, id)
                        && callee.carries_namespace(id, aliases))
            }
            Self::New(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || callee.carries_namespace(id, aliases)
            }
            // Unknown syntax is not evidence that a value aliases the
            // namespace. Sinks still handle unknown receivers and
            // publications conservatively below.
            Self::Unknown => false,
            Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) => false,
        }
    }

    fn is_direct_namespace_member_callee(callee: &Self, id: &Id) -> bool {
        matches!(callee, Self::Member(object, Some(_), MemberKey::Static, _)
            if matches!(object.as_ref(), Self::Binding(namespace) if namespace == id))
    }

    /// Whether a value being published may expose the namespace. Unsupported
    /// syntax and unsupported projections remain possible namespace values;
    /// aliases to those values are propagated by `infer_namespace_aliases` so
    /// the uncertainty reaches a later sink.
    pub(super) fn may_publish_namespace(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        self.may_publish_namespace_inner(
            id,
            aliases,
            facts,
            &mut BTreeSet::new(),
            &mut BTreeSet::new(),
        )
    }

    /// Equality with the namespace differs from publishing an object that
    /// contains it. A fresh literal is not the namespace, while a read of one
    /// of its ordinary own data properties can recover the namespace.
    fn may_equal_namespace(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
        visiting: &mut BTreeSet<Id>,
    ) -> bool {
        match self {
            Self::ObjectLiteral(_, _) | Self::Alloc(Kind::Function, _) => false,
            // Class metadata may contain a namespace path, including a
            // self-referential static initializer. Keep the existing
            // conservative class-surface fallback.
            Self::Alloc(Kind::Class, _) => self.may_publish_namespace(id, aliases, facts),
            Self::Binding(binding) => {
                if binding == id
                    || aliases.contains(binding)
                    || facts.unstable_class_bindings.contains(binding)
                {
                    return true;
                }
                if !visiting.insert(binding.clone()) {
                    return false;
                }
                let possible = facts
                    .binding_assignments
                    .get(binding)
                    .is_some_and(|values| {
                        values
                            .iter()
                            .any(|value| value.may_equal_namespace(id, aliases, facts, visiting))
                    });
                visiting.remove(binding);
                possible
            }
            Self::Alternatives(_) | Self::Conditional { .. } | Self::Sequence(_) => self
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| value.may_equal_namespace(id, aliases, facts, visiting)),
            Self::Member(object, Some(property), MemberKey::Static, _) => {
                if let Some(value) = facts.own_data_member(object, property, id, aliases) {
                    value.may_equal_namespace(id, aliases, facts, visiting)
                } else {
                    self.may_publish_namespace(id, aliases, facts)
                }
            }
            _ => self.may_publish_namespace(id, aliases, facts),
        }
    }

    fn may_publish_namespace_inner(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
        visiting_bindings: &mut BTreeSet<Id>,
        visiting_classes: &mut BTreeSet<(u32, ClassSurface)>,
    ) -> bool {
        match self {
            Self::Unknown => true,
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }
            Self::Primitive(_) | Self::StrictEquality(..) | Self::LogicalNot(_) | Self::Void(_) => {
                false
            }
            Self::ObjectLiteral(_, properties) => properties.iter().any(|(_, value)| {
                value.may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }),
            Self::Alternatives(_) | Self::Conditional { .. } | Self::Sequence(_) => self
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| {
                    value.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    )
                }),
            Self::Member(object, property, is_static, _) => {
                let projected = property
                    .as_deref()
                    .filter(|_| is_static.is_static())
                    .and_then(|property| facts.own_data_member(object, property, id, aliases));
                projected.unwrap_or(object).may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }
            Self::Call(callee, args, _) => {
                args.iter().any(|value| {
                    value.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    )
                }) || (!Self::is_direct_namespace_member_callee(callee, id)
                    && callee.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    ))
            }
            Self::New(callee, args, _) => {
                let local_class = facts
                    .resolve_class(callee, &mut BTreeSet::new())
                    .is_some_and(|(_, surface)| surface == ClassSurface::Constructor);
                args.iter().any(|value| {
                    value.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    )
                }) || (!local_class
                    && callee.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    ))
                    || facts.class_may_publish(
                        self,
                        id,
                        aliases,
                        visiting_bindings,
                        visiting_classes,
                    )
            }
            Self::Binding(binding) => {
                if binding == id
                    || aliases.contains(binding)
                    || facts.unstable_class_bindings.contains(binding)
                {
                    true
                } else if let Some(values) = facts.binding_assignments.get(binding) {
                    if !visiting_bindings.insert(binding.clone()) {
                        // A binding cycle contributes no namespace provenance
                        // without a namespace or unknown-value seed. The
                        // enclosing traversal still checks every alternative.
                        false
                    } else {
                        let result = values.iter().any(|value| {
                            value.may_publish_namespace_inner(
                                id,
                                aliases,
                                facts,
                                visiting_bindings,
                                visiting_classes,
                            )
                        });
                        visiting_bindings.remove(binding);
                        result
                    }
                } else {
                    false
                }
            }
            Self::Alloc(Kind::Class, _) => {
                facts.class_may_publish(self, id, aliases, visiting_bindings, visiting_classes)
            }
            Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) => false,
        }
    }

    /// Whether this value could be the namespace object used as a property
    /// write receiver. Unknown call and constructor results remain possible
    /// receivers here, even though merely storing or returning those unrelated
    /// results does not establish namespace provenance.
    pub(super) fn may_be_namespace_mutation_receiver(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        receiver_aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        self.may_be_namespace_mutation_receiver_with_alias_query(
            id,
            aliases,
            &mut |binding| receiver_aliases.contains(binding),
            facts,
        )
    }

    pub(super) fn may_be_namespace_mutation_receiver_with_alias_query(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        receiver_alias: &mut impl FnMut(&Id) -> bool,
        facts: &NamespaceValueFacts,
    ) -> bool {
        match self {
            Self::Binding(binding) => {
                binding == id || aliases.contains(binding) || receiver_alias(binding)
            }
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.may_be_namespace_mutation_receiver_with_alias_query(
                    id,
                    aliases,
                    receiver_alias,
                    facts,
                )
            }
            Self::Primitive(_) | Self::StrictEquality(..) | Self::LogicalNot(_) | Self::Void(_) => {
                false
            }
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.carries_namespace(id, aliases)),
            Self::Alternatives(_) | Self::Conditional { .. } | Self::Sequence(_) => self
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| {
                    value.may_be_namespace_mutation_receiver_with_alias_query(
                        id,
                        aliases,
                        receiver_alias,
                        facts,
                    )
                }),
            Self::Member(object, property, is_static, _) => {
                if let Some(value) = property
                    .as_deref()
                    .filter(|_| is_static.is_static())
                    .and_then(|property| facts.own_data_member(object, property, id, aliases))
                {
                    return value.may_be_namespace_mutation_receiver_with_alias_query(
                        id,
                        aliases,
                        receiver_alias,
                        facts,
                    );
                }
                if facts.unique_literal(object).is_some() {
                    // A getter, computed key, spread, or prototype setter can
                    // return the namespace even when the literal is fresh.
                    return true;
                }
                matches!(object.as_ref(), Self::Call(callee, _, _)
                    if Self::is_direct_namespace_member_callee(callee, id))
                    || matches!(object.as_ref(), Self::Unknown | Self::New(..))
                    || object.may_be_namespace_mutation_receiver_with_alias_query(
                        id,
                        aliases,
                        receiver_alias,
                        facts,
                    )
            }
            Self::Call(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || !Self::is_direct_namespace_member_callee(callee, id)
            }
            Self::New(..) | Self::Unknown => true,
            Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) => false,
        }
    }

    /// A callback result may contain one-hop namespace member reads as
    /// ordinary data-property values. That exception applies only to the
    /// direct returned object graph; aliases and other unknown shapes remain
    /// subject to the ordinary publication check.
    pub(super) fn may_publish_namespace_in_factory_result(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        match self {
            Self::ObjectLiteral(_, properties) => properties.iter().any(|(name, value)| {
                let direct_member_value = name.is_some()
                    && matches!(value, Self::Member(object, Some(_), MemberKey::Static, _)
                        if matches!(object.as_ref(), Self::Binding(namespace) if namespace == id));
                !direct_member_value
                    && value.may_publish_namespace_in_factory_result(id, aliases, facts)
            }),
            _ => self.may_publish_namespace(id, aliases, facts),
        }
    }
}

pub(super) fn infer_namespace_aliases(
    namespace: &Id,
    assignments: &[&(Id, ValueExpr)],
    facts: &NamespaceValueFacts,
) -> BTreeSet<Id> {
    use analysis::inference::{FactContext, IterativeInference, IterativeRules};
    struct AliasRules<'a> {
        namespace: &'a Id,
        assignments: &'a [&'a (Id, ValueExpr)],
        facts: &'a NamespaceValueFacts,
    }
    impl IterativeRules for AliasRules<'_> {
        type Key = ();
        type Fact = BTreeSet<Id>;
        type Rule = usize;
        type MergeScratch = ();
        fn evaluate(&self, rule: &usize, context: &mut impl FactContext<Self::Key, Self::Fact>) {
            let (target, value) = self.assignments[*rule];
            if target == self.namespace {
                return;
            }
            let is_alias = context.read(()).is_some_and(|aliases| {
                value.may_equal_namespace(self.namespace, aliases, self.facts, &mut BTreeSet::new())
            });
            if is_alias {
                context.propose((), BTreeSet::from([target.clone()]));
            }
        }
        fn merge(_: &(), current: &mut BTreeSet<Id>, contribution: BTreeSet<Id>) -> bool {
            let before = current.len();
            current.extend(contribution);
            current.len() != before
        }
    }
    let mut inference = IterativeInference::new(AliasRules {
        namespace,
        assignments,
        facts,
    });
    inference.seed((), BTreeSet::new());
    inference
        .solve(0..assignments.len())
        .into_values()
        .remove(&())
        .expect("alias seed")
}

impl NamespaceValueFacts {
    pub(super) fn class_allocation_without_user_code(&self, span: u32) -> bool {
        self.classes
            .get(&span)
            .is_some_and(|class| class.allocation_without_user_code)
    }

    pub(super) fn has_instance_field(&self, span: u32, property: &str) -> bool {
        self.classes
            .get(&span)
            .is_some_and(|class| class.instance_fields.contains_key(property))
    }

    pub(super) fn from_assignments(
        classes: BTreeMap<u32, ClassValueFacts>,
        verified_data_objects: BTreeSet<u32>,
        assignments: &[&(Id, ValueExpr)],
    ) -> Self {
        let mut values = BTreeMap::<Id, Vec<ValueExpr>>::new();
        for (binding, value) in assignments {
            values
                .entry(binding.clone())
                .or_default()
                .push(value.clone());
        }
        let mut facts = Self {
            classes,
            verified_data_objects,
            bindings: values
                .iter()
                .filter(|(_, values)| values.len() == 1)
                .map(|(binding, values)| (binding.clone(), values[0].clone()))
                .collect(),
            binding_assignments: values,
            unstable_class_bindings: BTreeSet::new(),
        };
        let (class_provenance, class_seeds) = LocalClassProvenance::compile(&facts);
        let class_bindings = class_provenance.solve(class_seeds);
        facts.unstable_class_bindings = facts
            .binding_assignments
            .iter()
            .filter_map(|(binding, values)| {
                let may_be_class = class_bindings.get(binding).is_some();
                let uniquely_resolved = values.len() == 1
                    && facts
                        .resolve_class(&values[0], &mut BTreeSet::new())
                        .is_some();
                (may_be_class && !uniquely_resolved).then(|| binding.clone())
            })
            .collect();
        facts
    }

    fn own_data_member<'a>(
        &'a self,
        object: &'a ValueExpr,
        property: &str,
        id: &Id,
        aliases: &BTreeSet<Id>,
    ) -> Option<&'a ValueExpr> {
        let ValueExpr::ObjectLiteral(span, properties) = self.unique_literal(object)? else {
            return None;
        };
        if !self.verified_data_objects.contains(span) {
            return None;
        }
        // A container with no explicit namespace path keeps the prior
        // conservative receiver rule. Its field might gain an accessor or
        // other value through a call without publishing this namespace.
        if !properties
            .iter()
            .any(|(_, value)| value.carries_namespace(id, aliases))
        {
            return None;
        }
        let mut values = properties
            .iter()
            .filter(|(name, _)| name.as_deref() == Some(property))
            .map(|(_, value)| value);
        let value = values.next()?;
        values.next().is_none().then_some(value)
    }

    fn unique_literal<'a>(&'a self, object: &'a ValueExpr) -> Option<&'a ValueExpr> {
        let object = match object {
            ValueExpr::Binding(binding) => self.bindings.get(binding)?,
            value => value,
        };
        matches!(object, ValueExpr::ObjectLiteral(_, _)).then_some(object)
    }

    pub(super) fn resolve_class(
        &self,
        value: &ValueExpr,
        visiting: &mut BTreeSet<Id>,
    ) -> Option<(u32, ClassSurface)> {
        match value {
            ValueExpr::Alloc(Kind::Class, span) => Some((*span, ClassSurface::Constructor)),
            ValueExpr::Binding(binding) => {
                if !visiting.insert(binding.clone()) {
                    return None;
                }
                let result = self
                    .bindings
                    .get(binding)
                    .and_then(|value| self.resolve_class(value, visiting));
                visiting.remove(binding);
                result
            }
            ValueExpr::New(callee, _, _) => self
                .resolve_class(callee, visiting)
                .filter(|(_, surface)| *surface == ClassSurface::Constructor)
                .map(|(span, _)| (span, ClassSurface::Instance)),
            _ => None,
        }
    }

    fn class_may_publish(
        &self,
        value: &ValueExpr,
        id: &Id,
        aliases: &BTreeSet<Id>,
        visiting_bindings: &mut BTreeSet<Id>,
        visiting_classes: &mut BTreeSet<(u32, ClassSurface)>,
    ) -> bool {
        let Some((span, surface)) = self.resolve_class(value, &mut BTreeSet::new()) else {
            return false;
        };
        if !visiting_classes.insert((span, surface)) {
            return true;
        }
        let result = self.classes.get(&span).is_none_or(|class| {
            let (opaque, fields) = match surface {
                ClassSurface::Constructor => (class.opaque_static, &class.static_fields),
                ClassSurface::Instance => (class.opaque_instance, &class.instance_fields),
            };
            let inherited_static_may_publish = if surface == ClassSurface::Constructor && !opaque {
                class.super_class.as_ref().is_some_and(|super_class| {
                    let Some((base_span, ClassSurface::Constructor)) =
                        self.resolve_class(super_class, &mut BTreeSet::new())
                    else {
                        return true;
                    };
                    self.class_may_publish(
                        &ValueExpr::Alloc(Kind::Class, base_span),
                        id,
                        aliases,
                        visiting_bindings,
                        visiting_classes,
                    )
                })
            } else {
                false
            };
            opaque
                || inherited_static_may_publish
                || fields.values().any(|field| {
                    field.may_publish_namespace_inner(
                        id,
                        aliases,
                        self,
                        visiting_bindings,
                        visiting_classes,
                    )
                })
        });
        visiting_classes.remove(&(span, surface));
        result
    }
}
pub(super) fn static_property_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(string) => Some(string.value.to_string_lossy().into_owned()),
        PropName::Num(number) => Some(number.value.to_string()),
        PropName::BigInt(number) => Some(number.value.to_string()),
        PropName::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => Some(string.value.to_string_lossy().into_owned()),
            Expr::Lit(Lit::Num(number)) => Some(number.value.to_string()),
            _ => None,
        },
    }
}

pub(super) fn object_data_property_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) if ident.sym == "__proto__" => None,
        PropName::Str(string) if string.value.to_string_lossy() == "__proto__" => None,
        _ => static_property_name(name),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        AbstractValue, CaptureInterner, HeapAddress, InstanceOrigin, LexicalCaptures, Values,
    };
    use crate::world::WorldScope;
    use imbl::OrdSet;
    use std::hash::{DefaultHasher, Hash, Hasher};
    use std::sync::Arc;

    fn universe() -> Vec<AbstractValue> {
        let mut contexts = CaptureInterner::<LexicalCaptures>::default();
        let context = contexts.intern(LexicalCaptures::default());
        assert_eq!(context, contexts.intern(LexicalCaptures::default()));
        let mut retained = CaptureInterner::<Values>::default();
        let first = retained.intern(Values::from([AbstractValue::UNDEFINED]));
        let second = retained.intern(Values::from([AbstractValue::PRIMITIVE]));
        assert_ne!(first, second);
        assert_eq!(
            first,
            retained.intern(Values::from([AbstractValue::UNDEFINED]))
        );
        vec![
            AbstractValue::UNDEFINED,
            AbstractValue::PRIMITIVE,
            AbstractValue::Closure(
                HeapAddress {
                    scope: WorldScope::Application,
                    callable: 1,
                },
                context,
            ),
            AbstractValue::DeferredCallbacks(first.clone()),
            AbstractValue::DeferredCallbacks(second),
            AbstractValue::Instance(1, first, InstanceOrigin::Constructed),
        ]
    }

    fn assert_contents(values: &Values, expected: &OrdSet<AbstractValue>) {
        assert_eq!(values.len(), expected.len());
        assert_eq!(values.is_empty(), expected.is_empty());
        assert_eq!(values.first(), expected.get_min());
        assert!(values.iter().eq(expected.iter()));
        assert!(values.into_iter().rev().eq(expected.iter().rev()));
        assert!(values.clone().into_iter().eq(expected.iter().cloned()));
        assert!(
            expected
                .iter()
                .cloned()
                .collect::<Values>()
                .into_iter()
                .rev()
                .eq(expected.iter().rev().cloned())
        );
        assert_eq!(format!("{values:?}"), format!("Values({expected:?})"));
        let mut actual_hash = DefaultHasher::new();
        values.hash(&mut actual_hash);
        let mut expected_hash = DefaultHasher::new();
        expected.len().hash(&mut expected_hash);
        for value in expected {
            value.hash(&mut expected_hash);
        }
        assert_eq!(actual_hash.finish(), expected_hash.finish());
        assert_eq!(values.0.is_none(), expected.is_empty());
    }

    #[test]
    fn flat_values_match_ordered_set_operations_with_interned_captures() {
        let universe = universe();
        let sets: Vec<_> = (0..1 << universe.len())
            .map(|mask| {
                let input: Vec<_> = universe
                    .iter()
                    .enumerate()
                    .filter(|(index, _)| mask & (1 << index) != 0)
                    .map(|(_, value)| value.clone())
                    .rev()
                    .collect();
                let input: Vec<_> = input.iter().chain(&input).cloned().collect();
                let values = input.iter().cloned().collect::<Values>();
                let expected = input.into_iter().collect::<OrdSet<_>>();
                assert_contents(&values, &expected);
                (values, expected)
            })
            .collect();
        assert!(Values::new().ptr_eq(&Values::new()));
        for (values, expected) in &sets {
            for value in &universe {
                assert_eq!(values.contains(value), expected.contains(value));
                let mut inserted = values.clone();
                let mut inserted_expected = expected.clone();
                let changed = inserted_expected.insert(value.clone()).is_none();
                assert_eq!(inserted.insert(value.clone()), changed);
                assert_eq!(inserted.ptr_eq(values), !changed);
                assert_contents(&inserted, &inserted_expected);
                let mut removed = values.clone();
                let mut removed_expected = expected.clone();
                let changed = removed_expected.remove(value).is_some();
                assert_eq!(removed.remove(value), changed);
                assert_eq!(removed.ptr_eq(values), !changed);
                assert_contents(&removed, &removed_expected);
                assert_contents(values, expected);
            }
            for (other, other_expected) in &sets {
                assert_eq!(values == other, expected == other_expected);
                assert_eq!(values.cmp(other), expected.cmp(other_expected));
                assert_eq!(
                    values.is_disjoint(other),
                    expected.iter().all(|value| !other_expected.contains(value))
                );
                let mut extended_expected = expected.clone();
                extended_expected.extend(other_expected.iter().cloned());
                let changed = extended_expected.len() != expected.len();
                let mut extended = values.clone();
                extended.extend(other.iter().chain(other).cloned());
                assert_eq!(extended.ptr_eq(values), !changed);
                assert_contents(&extended, &extended_expected);
                let mut absent = values.clone();
                assert_eq!(absent.extend_absent(other.iter().chain(other)), changed);
                assert_eq!(absent.ptr_eq(values), !changed);
                assert_contents(&absent, &extended_expected);
                assert_contents(values, expected);
            }
        }
    }

    #[test]
    fn shared_owned_iteration_clones_only_yielded_values() {
        let mut interner = CaptureInterner::<Values>::default();
        let captured = interner.intern(Values::from([AbstractValue::UNDEFINED]));
        let values = Values::from([
            AbstractValue::DeferredCallbacks(captured.clone()),
            AbstractValue::UNDEFINED,
        ]);
        let owners = Arc::strong_count(&captured.values);
        let mut iter = values.clone().into_iter();
        assert_eq!(Arc::strong_count(&captured.values), owners);
        assert_eq!(iter.len(), 2);
        let retained = iter.next_back().unwrap();
        assert_eq!(Arc::strong_count(&captured.values), owners + 1);
        assert_eq!(iter.len(), 1);
        assert_eq!(iter.next(), Some(AbstractValue::UNDEFINED));
        assert_eq!(iter.len(), 0);
        assert!(iter.next().is_none());
        assert!(iter.next_back().is_none());
        drop(retained);
        assert_eq!(Arc::strong_count(&captured.values), owners);
    }
}

#[cfg(test)]
mod local_class_provenance_tests {
    use super::*;

    fn facts(source: &str) -> NamespaceValueFacts {
        swc_common::GLOBALS.set(&swc_common::Globals::new(), || {
            let module = js_ast::parse_js_module("class-provenance.js", source)
                .expect("valid provenance fixture")
                .module;
            let assignments = module
                .body
                .iter()
                .flat_map(|item| {
                    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) = item else {
                        panic!("fixture contains only variable declarations");
                    };
                    declaration.decls.iter().map(|declaration| {
                        let Pat::Ident(binding) = &declaration.name else {
                            panic!("fixture uses named bindings");
                        };
                        (
                            binding.to_id(),
                            ValueExpr::from_expr(declaration.init.as_deref().expect("initializer")),
                        )
                    })
                })
                .collect::<Vec<_>>();
            NamespaceValueFacts::from_assignments(
                BTreeMap::new(),
                BTreeSet::new(),
                &assignments.iter().collect::<Vec<_>>(),
            )
        })
    }

    fn assert_classes(source: &str, expected: &[&str], max_visits: usize) {
        let facts = facts(source);
        let (rules, seeds) = LocalClassProvenance::compile(&facts);
        assert!(rules.expression_visits <= max_visits);
        let edges = rules.edges.len();
        let result = rules.solve(seeds);
        // A Boolean source changes at most once, so each edge runs initially
        // and at most once after its source appears, even in shared cycles.
        assert!(result.evaluations() <= 2 * edges);
        let names = result
            .keys()
            .map(|binding| binding.0.as_ref())
            .collect::<BTreeSet<_>>();
        assert_eq!(names, expected.iter().copied().collect());
    }

    #[test]
    #[ignore = "run //devinfra/js/debundle/e2e/bench:analysis_work_bounds_test"]
    fn work_bounds_shared_chains_and_diamonds() {
        for (depth, root) in [(4, "{}"), (8, "{}"), (16, "{}"), (16, "class {}")] {
            let mut source = format!("var root = {root};");
            let mut names = vec!["root".to_owned()];
            for index in 1..=depth {
                let prior = if index == 1 {
                    "root".to_owned()
                } else {
                    format!("join{}", index - 1)
                };
                source.push_str(&format!(
                    "var left{index} = flag ? {prior} : {prior};\n\
                     var right{index} = {prior};\n\
                     var join{index} = flag ? left{index} : right{index};\n"
                ));
                names.extend([
                    format!("left{index}"),
                    format!("right{index}"),
                    format!("join{index}"),
                ]);
            }
            let expected = if root == "{}" {
                vec![]
            } else {
                names.iter().map(String::as_str).collect()
            };
            assert_classes(&source, &expected, 1 + 7 * depth);
        }
    }

    #[test]
    fn cycles_require_a_reachable_class_seed() {
        assert_classes("var a = b; var b = flag ? a : c; var c = a;", &[], 5);
        // The class seed follows the back-edge alternative. An in-progress
        // false answer for b or c must not become a completed negative.
        assert_classes(
            "var a = flag ? b : class {}; var b = c; var c = a; var tail = b;",
            &["a", "b", "c", "tail"],
            6,
        );
    }

    #[test]
    fn class_surfaces_and_opaque_value_variants_keep_their_meaning() {
        let source = "var constructor = class {};\n\
            var instance = new constructor();\n\
            var constructorMember = constructor.member;\n\
            var instanceMember = instance.member;\n\
            var conditional = flag ? constructor : {};\n\
            var member = conditional.member;\n\
            var testOnly = constructor ? {} : {};\n\
            var container = { constructor };\n\
            var sequence = (0, constructor);\n\
            var call = constructor();\n\
            var functionValue = function () {};\n\
            var rebound = constructor; var rebound = {};";
        assert_classes(
            source,
            &[
                "constructor",
                "constructorMember",
                "instanceMember",
                "conditional",
                "member",
                "rebound",
            ],
            20,
        );
        let unstable = facts(source)
            .unstable_class_bindings
            .into_iter()
            .map(|binding| binding.0.to_string())
            .collect::<BTreeSet<_>>();
        assert_eq!(
            unstable,
            [
                "constructorMember",
                "instanceMember",
                "conditional",
                "member",
                "rebound"
            ]
            .into_iter()
            .map(str::to_owned)
            .collect()
        );
    }
}
