//! Abstract value vocabulary and value relationships used by synchronous execution analysis.

pub mod callable_body_facts;
pub mod local_operations;
mod namespace_facts;
pub mod own_data_transfer;
pub mod property_inference;
mod value_expr;

use analysis_domain::builtins::IterationRow;
use analysis_model::PrimKinds;
use analysis_vocabulary::inputs::ProofDependencies;
use analysis_vocabulary::value::{Kind, Value};
use imbl::OrdMap;
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::hash::{DefaultHasher, Hash, Hasher};
use std::ops::Bound::{Excluded, Unbounded};
use std::sync::{Arc, Weak};
use swc_common::{BytePos, Span};
use swc_ecma_ast::*;
use value_flow::static_prop as static_string_prop;

pub fn static_prop(property: &MemberProp) -> Option<String> {
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
    pub fn new() -> Self {
        Self::default()
    }

    pub fn first(&self) -> Option<&AbstractValue> {
        self.as_slice().first()
    }

    fn as_slice(&self) -> &[AbstractValue] {
        self.0.as_deref().map_or(&[], Vec::as_slice)
    }

    pub fn len(&self) -> usize {
        self.as_slice().len()
    }

    pub fn iter(&self) -> std::slice::Iter<'_, AbstractValue> {
        self.as_slice().iter()
    }

    pub fn contains(&self, value: &AbstractValue) -> bool {
        self.as_slice().binary_search(value).is_ok()
    }

    pub fn ptr_eq(&self, other: &Self) -> bool {
        match (&self.0, &other.0) {
            (None, None) => true,
            (Some(left), Some(right)) => Arc::ptr_eq(left, right),
            _ => false,
        }
    }

    pub fn insert(&mut self, value: AbstractValue) -> bool {
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
    pub fn extend_absent<'v>(
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

    pub fn remove(&mut self, value: &AbstractValue) -> bool {
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

    pub fn is_empty(&self) -> bool {
        self.0.is_none()
    }

    pub fn is_disjoint(&self, other: &Self) -> bool {
        self.iter().all(|value| !other.contains(value))
    }
}

/// A transient value set as a sorted, deduplicated vector: the elements and
/// iteration order of the `Values` the same items would build, without
/// persistent-set insertion. For inputs that are only iterated.
pub fn ordered_values(values: impl IntoIterator<Item = AbstractValue>) -> Vec<AbstractValue> {
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

pub type Context = OrdMap<Id, Values>;

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
    pub const UNDEFINED: Self = Self::Value(Value::Prim(PrimKinds::UNDEFINED));
    /// A primitive of untracked type.
    pub const PRIMITIVE: Self = Self::Value(Value::Prim(PrimKinds::ANY));

    /// Whether this value is a primitive. `Top` and `Unavailable` are
    /// excluded: either can represent a non-primitive result or a throwing
    /// evaluation.
    pub fn is_primitive(&self) -> bool {
        matches!(self, Self::Value(Value::Prim(_)))
    }
}

impl CallSite {
    pub fn allocation_id(self) -> u32 {
        self.start
    }

    pub fn source_span(self) -> Span {
        Span::new(BytePos(self.start), BytePos(self.end))
    }
}

/// Contexts are interned bottom-up by the executor. Structural equality of
/// captures becomes identity equality, so comparing value sets and cache keys
/// never recursively compares a shared capture DAG along every path.
#[derive(Debug, Clone)]
pub struct Captured<T> {
    identity: usize,
    pub values: Arc<T>,
}

impl<T> Captured<T> {
    pub(crate) fn with_identity(identity: usize, values: Arc<T>) -> Self {
        Self { identity, values }
    }

    pub fn cache_identity(&self) -> usize {
        self.identity
    }
}

/// Canonical descriptions stay interned while an actual value or semantic
/// root owns them. The table itself never keeps their payloads alive.
pub struct CaptureInterner<T> {
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
    pub fn intern(&mut self, description: T) -> Captured<T> {
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
    pub fn sweep_dead(&mut self) {
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
pub struct LexicalCellId(pub usize);

#[derive(Debug, Clone, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct LexicalCaptures {
    pub snapshots: Context,
    pub cells: OrdMap<Id, LexicalCellId>,
    /// Source containment only, never a trusted receiver or instance identity.
    pub home_object: Option<u32>,
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
    pub fn is_empty(&self) -> bool {
        self.snapshots.is_empty() && self.cells.is_empty() && self.home_object.is_none()
    }
}

pub type CapturedContext = Captured<LexicalCaptures>;

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
pub struct ProviderScopeId(u64);

impl ProviderScopeId {
    pub fn fresh() -> Option<Self> {
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
pub enum ProviderInput {
    Argument(usize),
    Capture(usize),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SymbolicInputId {
    pub scope: ProviderScopeId,
    pub input: ProviderInput,
}

/// A callable's function allocation in one world. The scope keeps an
/// isolated provider query's closures apart from the application's, even when
/// both create the same source callable.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct HeapAddress {
    pub scope: WorldScope,
    pub callable: u32,
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
pub struct ArrayIteration {
    pub row: &'static IterationRow,
    pub source: ValueExpr,
    pub callback: ValueExpr,
    /// `reduce`'s initial accumulator.
    pub initial: Option<ValueExpr>,
    pub result: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct ArrayElement {
    pub spread: Option<swc_common::Span>,
    pub value: ValueExpr,
}

/// Slot values are an ordered overapproximation. The ordinary Object origin
/// remains the identity used by every mutation, publication and taint lane.
#[derive(Debug, Clone, PartialEq)]
pub struct DenseArray {
    pub slots: Vec<Values>,
}

/// Result of transferring the ordinary Array iterator over a settled dense
/// array at one interpreter prefix. Operation adapters check their own
/// iterator-protocol premises; this transfer supplies the exact receiver and
/// ordered heap slots. Operand evaluation keeps possible throws in its
/// existing `Unavailable` path; a transfer refuses any non-singleton value.
#[derive(Debug, Clone, PartialEq)]
pub enum DenseArrayIterationTransfer {
    Refused,
    Yielded { source: u32, slots: Vec<Values> },
}

#[derive(Debug, Clone)]
pub struct FromEntriesMappedProof {
    pub source: ValueExpr,
    pub key: String,
    pub map_call_site: CallSite,
    pub object_intrinsic: Id,
}

/// Ordered literal definitions keep accessor payloads distinct from data values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LiteralPropertyKind {
    Data,
    Method,
    Getter,
    Setter,
    Opaque,
}

#[derive(Debug, Clone)]
pub enum PropertyDefinition<T> {
    Data { name: Option<String>, value: T },
    Method { name: Option<String>, value: T },
    Getter { name: Option<String>, value: T },
    Setter { name: Option<String>, value: T },
    Opaque { name: Option<String>, value: T },
}

impl<T> PropertyDefinition<T> {
    pub fn kind(&self) -> LiteralPropertyKind {
        match self {
            Self::Data { .. } => LiteralPropertyKind::Data,
            Self::Method { .. } => LiteralPropertyKind::Method,
            Self::Getter { .. } => LiteralPropertyKind::Getter,
            Self::Setter { .. } => LiteralPropertyKind::Setter,
            Self::Opaque { .. } => LiteralPropertyKind::Opaque,
        }
    }
    pub fn new(kind: LiteralPropertyKind, name: Option<String>, value: T) -> Self {
        match kind {
            LiteralPropertyKind::Data => Self::Data { name, value },
            LiteralPropertyKind::Method => Self::Method { name, value },
            LiteralPropertyKind::Getter => Self::Getter { name, value },
            LiteralPropertyKind::Setter => Self::Setter { name, value },
            LiteralPropertyKind::Opaque => Self::Opaque { name, value },
        }
    }
    pub fn name(&self) -> Option<&str> {
        match self {
            Self::Data { name, .. }
            | Self::Method { name, .. }
            | Self::Getter { name, .. }
            | Self::Setter { name, .. }
            | Self::Opaque { name, .. } => name.as_deref(),
        }
    }
    pub fn value(&self) -> &T {
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
pub enum OwnPropertyDescriptorSyntax {
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
pub struct OwnPropertyShape {
    pub properties: BTreeMap<String, OwnPropertyDescriptorSyntax>,
    pub creation_order: Vec<String>,
}

/// Returns ECMAScript's own string-key order for a source-ordered ordinary
/// object shape: array-index keys numerically first, followed by other string
/// keys in their original insertion order. `BTreeMap` order is never a proxy
/// for JavaScript property order.
pub(crate) fn ordered_own_property_names(creation_order: &[String]) -> Vec<String> {
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
pub fn canonical_array_index(name: &str) -> Option<u32> {
    name.parse::<u32>()
        .ok()
        .filter(|index| *index != u32::MAX && index.to_string() == name)
}

/// How a member read names its property.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MemberKey {
    /// `object.name`.
    Static,
    /// `object[expression]`; the identifier when the expression is one.
    Computed(Option<Id>),
}

impl MemberKey {
    pub fn of(property: &MemberProp) -> Self {
        match property {
            MemberProp::Ident(_) => Self::Static,
            MemberProp::Computed(key) => match key.expr.unwrap_parens() {
                Expr::Ident(binding) => Self::Computed(Some(binding.to_id())),
                _ => Self::Computed(None),
            },
            MemberProp::PrivateName(_) => Self::Computed(None),
        }
    }

    pub fn is_static(&self) -> bool {
        *self == Self::Static
    }

    /// A literal index needs no key evaluation. Computed identifiers and
    /// other dynamic keys cannot use the present own-data read proof.
    pub fn supports_own_data_read(&self, property: &str) -> bool {
        matches!(self, Self::Static)
            || (matches!(self, Self::Computed(None)) && canonical_array_index(property).is_some())
    }
}

// Lowered syntax is immutable. Cloned facts share recursive children while
// each evaluation keeps its own interpreter state.
#[derive(Debug, Clone)]
pub enum ValueExpr {
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
pub struct ClassValueFacts {
    pub instance_fields: BTreeMap<String, ValueExpr>,
    pub ordinary_instance_methods: BTreeSet<String>,
    pub default_has_instance: bool,
    pub static_fields: BTreeMap<String, ValueExpr>,
    pub super_class: Option<ValueExpr>,
    /// Reads in the original heritage, including operands omitted by value lowering.
    pub super_class_inputs: BTreeSet<Id>,
    /// Inline callables and classes need the enclosing-environment fallback.
    pub super_class_inputs_incomplete: bool,
    /// A discarded direct call followed by a lexical superclass read. Its
    /// argument inputs can be dropped only after proving the callee inert.
    pub discarded_heritage_call: Option<DiscardedHeritageCall>,
    pub lexical_heritage: Option<LexicalClassHeritage>,
    pub opaque_instance: bool,
    pub opaque_static: bool,
    pub allocation_without_user_code: bool,
}

#[derive(Debug, Clone)]
pub struct DiscardedHeritageCall {
    pub callee: Id,
    pub base: Id,
    pub argument_reads: BTreeSet<Id>,
    pub remaining_inputs: BTreeSet<Id>,
}

impl DiscardedHeritageCall {
    pub fn collect(expression: &Expr) -> Option<Self> {
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
                argument.spread.is_some() || {
                    let operand = argument.expr.unwrap_parens();
                    !matches!(operand, Expr::Ident(_)) && !js_ast::is_primitive_literal(operand)
                }
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
pub struct LexicalClassHeritage {
    binding: Id,
}

impl LexicalClassHeritage {
    pub fn collect(expression: &Expr) -> Option<Self> {
        let Expr::Ident(binding) = expression.unwrap_parens() else {
            return None;
        };
        Some(Self {
            binding: binding.to_id(),
        })
    }

    pub fn binding(&self) -> &Id {
        &self.binding
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum ClassSurface {
    Constructor,
    Instance,
}

#[derive(Debug, Clone, Default)]
pub struct NamespaceValueFacts {
    classes: BTreeMap<u32, ClassValueFacts>,
    verified_data_objects: BTreeSet<u32>,
    /// A binding is resolved only when the retained syntax has exactly one
    /// assignment for it. Reassignments and alternatives remain opaque.
    bindings: BTreeMap<Id, ValueExpr>,
    binding_assignments: BTreeMap<Id, Vec<ValueExpr>>,
    unstable_class_bindings: BTreeSet<Id>,
}

pub fn infer_namespace_aliases(
    namespace: &Id,
    assignments: &[&(Id, ValueExpr)],
    facts: &NamespaceValueFacts,
) -> BTreeSet<Id> {
    use inference::{FactContext, IterativeInference, IterativeRules};
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

pub fn static_property_name(name: &PropName) -> Option<String> {
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

pub fn object_data_property_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) if ident.sym == "__proto__" => None,
        PropName::Str(string) if string.value.to_string_lossy() == "__proto__" => None,
        _ => static_property_name(name),
    }
}

/// Who owns this world. The scope decides heap policy as a bundle: whether
/// literal writability is followed and whether a body's heap cells reach a
/// caller's effects.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum WorldScope {
    /// The chunk's own prefix: literal writability is tracked and heap cells
    /// become the owner's effects.
    Application,
    /// A fresh heap for one provider query: no writability proof, and heap
    /// cells stay private to the query.
    IsolatedProvider,
}

/// Nonterminal provenance of an actually selected abstract callable. This
/// proves neither fresh allocation nor complete invocation effects.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct CallableSelection {
    pub target: AbstractValue,
    pub dependencies: Box<ProofDependencies>,
}

impl CallableSelection {
    pub fn observed(values: &Values, dependencies: Box<ProofDependencies>) -> Option<Self> {
        if values.len() != 1 {
            return None;
        }
        let target = values.first()?.clone();
        matches!(
            &target,
            AbstractValue::Closure(..) | AbstractValue::Value(Value::Alloc(Kind::Function, _))
        )
        .then_some(Self {
            target,
            dependencies,
        })
    }

    pub fn matches(&self, values: &Values) -> bool {
        values.len() == 1 && values.first() == Some(&self.target)
    }
}

#[cfg(test)]
mod tests {
    use super::WorldScope;
    use super::{
        AbstractValue, CaptureInterner, HeapAddress, InstanceOrigin, LexicalCaptures, Values,
    };
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
