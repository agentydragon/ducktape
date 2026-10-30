//! Monotone, intra-chunk value identities for local calls and object aliases.
//!
//! The sets deliberately over-approximate: an unsupported expression yields
//! `Unknown`, and every assignment adds possible values rather than replacing
//! them. The same facts feed at-init call promotion and PlainData's escape
//! check, so an alias/return cannot be understood by one pass and invisible to
//! the other. This is context-insensitive; it makes no claim about values from
//! other chunks.

use source_identity::SourceUnitOrdinal;
#[cfg(test)]
use std::collections::VecDeque;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;
#[cfg(test)]
use std::{cell::Cell, rc::Rc};

use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use crate::inference::{FactContext, IterativeInference, IterativeRules};

use super::{
    LazyBoundary, PositionBucketed, StatementFacts, TopLevelItemView, lazy_visit_arrow_expr,
    lazy_visit_bin_expr, lazy_visit_class, lazy_visit_class_member, lazy_visit_cond_expr,
    lazy_visit_function, lazy_visit_getter_prop, lazy_visit_if_stmt, lazy_visit_may_skip,
    lazy_visit_method_prop, lazy_visit_setter_prop,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) enum ValueOrigin {
    Function(u32),
    Class(u32),
    Object(u32),
    /// A fresh native Set identity, without plain-object or iterable shape.
    NativeSet(u32),
    Unknown,
}

pub(crate) fn native_set_allocation_roots(
    inputs: &[crate::analysis_inputs::ResolvedNativeOperationInput],
    stable_spreads: &BTreeMap<u32, crate::analysis_inputs::StableSetIterableUse>,
) -> BTreeMap<u32, crate::analysis_inputs::NativeOperationInputRef> {
    inputs
        .iter()
        .filter(|input| {
            input.evidence.operation
                == crate::analysis_inputs::NativeInputKind::Operation(
                    spec::NativeOperationKind::SetFromPrimitiveArray,
                )
                || (input.evidence.operation
                    == crate::analysis_inputs::NativeInputKind::Operation(
                        spec::NativeOperationKind::SetFromSetSpread,
                    )
                    && stable_spreads.contains_key(&input.call_span.lo.0))
        })
        .map(|input| (input.call_span.lo.0, input.evidence.reference.clone()))
        .collect()
}

pub(crate) struct CallTargets {
    pub bindings: BTreeSet<Id>,
    /// Every possible value has a canonical callable binding. Otherwise a
    /// direct identifier's syntactic target must retain its opaque fallback.
    pub complete: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
struct OriginId(u32);

/// Per-chunk, deterministic mapping between semantic origins and dense bitmap
/// positions. The vector order preserves `ValueOrigin::Ord` when decoding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OriginTable {
    origins: Vec<ValueOrigin>,
    ids: HashMap<ValueOrigin, OriginId>,
}

impl OriginTable {
    pub(super) fn from_origins(origins: impl IntoIterator<Item = ValueOrigin>) -> Self {
        let mut origins: Vec<_> = origins.into_iter().chain([ValueOrigin::Unknown]).collect();
        origins.sort_unstable();
        origins.dedup();
        let ids = origins
            .iter()
            .copied()
            .enumerate()
            .map(|(index, origin)| {
                (
                    origin,
                    OriginId(u32::try_from(index).expect("chunk origin count exceeds u32")),
                )
            })
            .collect();
        Self { origins, ids }
    }

    fn id(&self, origin: ValueOrigin) -> OriginId {
        *self
            .ids
            .get(&origin)
            .expect("ValueOrigin must belong to this chunk")
    }

    fn origin(&self, id: OriginId) -> ValueOrigin {
        self.origins[id.0 as usize]
    }

    #[cfg(test)]
    pub(super) fn for_expr(expr: &Expr) -> Arc<Self> {
        let mut collector = ValueOriginCollector::default();
        expr.visit_with(&mut collector);
        Arc::new(Self::from_origins(collector.origins))
    }
}

#[cfg(test)]
#[derive(Default)]
struct ValueOriginCollector {
    origins: BTreeSet<ValueOrigin>,
}

#[cfg(test)]
impl Visit for ValueOriginCollector {
    fn visit_function(&mut self, node: &Function) {
        self.origins.insert(ValueOrigin::Function(node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.origins.insert(ValueOrigin::Function(node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_class(&mut self, node: &Class) {
        self.origins.insert(ValueOrigin::Class(node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        self.origins.insert(ValueOrigin::Object(node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        self.origins.insert(ValueOrigin::Object(node.span.lo.0));
        node.visit_children_with(self);
    }
}

impl Default for OriginTable {
    fn default() -> Self {
        Self::from_origins([])
    }
}

/// A dense, ordered set of semantic origins. All sets in one flow analysis
/// share its immutable origin table, so word positions have identical meaning.
#[derive(Clone)]
pub(crate) struct Values {
    bitmap: Arc<ValueBitmap>,
    unknown: bool,
    origins: Arc<OriginTable>,
}

#[derive(Clone, Default)]
struct ValueBitmap {
    bits: Vec<u64>,
    /// Nonzero word indices, in sorted order. `bits` stays dense so rank
    /// lookup is constant-time, while unions touch only words that can add
    /// origins instead of scanning long zero prefixes.
    occupied_words: Vec<u32>,
}

impl PartialEq for Values {
    fn eq(&self, other: &Self) -> bool {
        self.bitmap.bits == other.bitmap.bits
            && self.unknown == other.unknown
            && self.origins == other.origins
    }
}

impl Eq for Values {}

impl std::fmt::Debug for Values {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_set().entries(self.iter()).finish()
    }
}

impl Values {
    pub(super) fn empty(origins: Arc<OriginTable>) -> Self {
        Self {
            bitmap: Arc::new(ValueBitmap::default()),
            unknown: false,
            origins,
        }
    }

    pub(super) fn singleton(origins: Arc<OriginTable>, value: ValueOrigin) -> Self {
        let mut values = Self::empty(origins);
        values.insert(value);
        values
    }

    fn is_empty(&self) -> bool {
        // Insertions and unions only grow the vector through a set bit, so
        // trailing zero words are never introduced.
        self.bitmap.bits.is_empty() && !self.unknown
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.bitmap
            .bits
            .iter()
            .map(|word| word.count_ones() as usize)
            .sum::<usize>()
            + usize::from(self.unknown)
    }

    #[cfg(test)]
    pub(super) fn occupied_word_indices(&self) -> &[u32] {
        &self.bitmap.occupied_words
    }

    #[cfg(test)]
    pub(super) fn shares_bitmap(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.bitmap, &other.bitmap)
    }

    #[cfg(test)]
    pub(super) fn contains(&self, value: &ValueOrigin) -> bool {
        if *value == ValueOrigin::Unknown {
            return self.unknown;
        }
        let Some(id) = self.origins.ids.get(value) else {
            return false;
        };
        let word = id.0 as usize / u64::BITS as usize;
        let bit = id.0 % u64::BITS;
        self.bitmap
            .bits
            .get(word)
            .is_some_and(|word| word & (1_u64 << bit) != 0)
    }

    pub(super) fn insert(&mut self, value: ValueOrigin) -> bool {
        if value == ValueOrigin::Unknown {
            return !std::mem::replace(&mut self.unknown, true);
        }
        let id = self.origins.id(value).0;
        let word_index = id as usize / u64::BITS as usize;
        let mask = 1_u64 << (id % u64::BITS);
        if self
            .bitmap
            .bits
            .get(word_index)
            .is_some_and(|word| word & mask != 0)
        {
            return false;
        }
        let bitmap = Arc::make_mut(&mut self.bitmap);
        if bitmap.bits.len() <= word_index {
            bitmap.bits.resize(word_index + 1, 0);
        }
        let was_empty = bitmap.bits[word_index] == 0;
        if was_empty {
            bitmap.insert_occupied_word(
                u32::try_from(word_index).expect("chunk origin word index exceeds u32"),
            );
        }
        bitmap.bits[word_index] |= mask;
        true
    }

    /// Ids of this set's origins whose bit is set in `mask` (indexed by
    /// `OriginId`), without decoding the other origins.
    fn ids_within<'a>(&'a self, mask: &'a [u64]) -> impl Iterator<Item = OriginId> + 'a {
        let unknown = self.origins.id(ValueOrigin::Unknown);
        let unknown = (self.unknown && bits_contain_id(mask, unknown)).then_some(unknown);
        BitIndices::new(
            self.bitmap
                .bits
                .iter()
                .zip(mask)
                .map(|(word, mask)| word & mask),
        )
        .map(OriginId)
        .chain(unknown)
    }

    pub(super) fn iter(&self) -> ValuesIter<'_> {
        ValuesIter {
            bits: BitIndices::new(self.bitmap.bits.iter().copied()),
            origins: &self.origins,
            unknown: self.unknown,
            yielded_unknown: false,
        }
    }

    /// Merge another value set, returning whether it contributed new origins.
    pub(super) fn union_with(&mut self, other: &Self) -> bool {
        assert!(Arc::ptr_eq(&self.origins, &other.origins));
        if self.bitmap.bits.is_empty() {
            let changed = !other.bitmap.bits.is_empty() || (other.unknown && !self.unknown);
            self.bitmap = other.bitmap.clone();
            self.unknown |= other.unknown;
            return changed;
        }
        if other.bitmap.bits.is_empty() || Arc::ptr_eq(&self.bitmap, &other.bitmap) {
            let changed = other.unknown && !self.unknown;
            self.unknown |= other.unknown;
            return changed;
        }
        if Arc::strong_count(&self.bitmap) > 1 && self.bitmap.contains_all(&other.bitmap) {
            let changed = other.unknown && !self.unknown;
            self.unknown |= other.unknown;
            return changed;
        }
        // Reuse an existing superset instead of materializing the same wide
        // union separately for every object or property. Unknown is tracked
        // independently: it never replaces known origins in the bitmap.
        if other.bitmap.bits.len() >= self.bitmap.bits.len()
            && other.bitmap.contains_all(&self.bitmap)
        {
            let changed = self.bitmap.bits != other.bitmap.bits || (other.unknown && !self.unknown);
            self.bitmap = other.bitmap.clone();
            self.unknown |= other.unknown;
            return changed;
        }
        let mut changed = if let Some(changed) = self.union_dense_words(other) {
            changed
        } else {
            self.union_occupied_words(other)
        };
        if other.unknown && !self.unknown {
            self.unknown = true;
            changed = true;
        }
        changed
    }

    fn union_occupied_words(&mut self, other: &Self) -> bool {
        let mut changed = false;
        let bitmap = Arc::make_mut(&mut self.bitmap);
        if bitmap.bits.len() < other.bitmap.bits.len() {
            bitmap.bits.resize(other.bitmap.bits.len(), 0);
        }
        let mut added_words = Vec::new();
        for word_index in &other.bitmap.occupied_words {
            let index = *word_index as usize;
            let into = bitmap.bits[index];
            let from = other.bitmap.bits[index];
            let merged = into | from;
            changed |= merged != into;
            if into == 0 {
                added_words.push(*word_index);
            }
            bitmap.bits[index] = merged;
        }
        bitmap.merge_occupied_words(&added_words);
        changed
    }

    fn union_without_change_tracking(&mut self, other: &Self) {
        self.union_with(other);
    }

    /// Preserve the vectorized wordwise path when neither operand contains
    /// zero words. Growing a dense destination adds the dense source's tail
    /// ranks to the already sorted occupied-word index.
    fn union_dense_words(&mut self, other: &Self) -> Option<bool> {
        if self.bitmap.occupied_words.len() != self.bitmap.bits.len()
            || other.bitmap.occupied_words.len() != other.bitmap.bits.len()
        {
            return None;
        }

        let bitmap = Arc::make_mut(&mut self.bitmap);
        let old_len = bitmap.bits.len();
        if old_len < other.bitmap.bits.len() {
            bitmap.bits.resize(other.bitmap.bits.len(), 0);
            bitmap.occupied_words.extend(
                (old_len..other.bitmap.bits.len()).map(|index| {
                    u32::try_from(index).expect("chunk origin word index exceeds u32")
                }),
            );
        }

        let mut changed = false;
        for (into, from) in bitmap.bits.iter_mut().zip(&other.bitmap.bits) {
            let merged = *into | *from;
            changed |= merged != *into;
            *into = merged;
        }
        Some(changed)
    }

    fn union(mut self, other: Self) -> Self {
        self.union_with(&other);
        self
    }
}

impl ValueBitmap {
    fn contains_all(&self, other: &Self) -> bool {
        other.occupied_words.iter().all(|word_index| {
            let index = *word_index as usize;
            self.bits.get(index).copied().unwrap_or(0) & other.bits[index] == other.bits[index]
        })
    }

    fn insert_occupied_word(&mut self, word: u32) {
        let Err(index) = self.occupied_words.binary_search(&word) else {
            unreachable!("an empty value word cannot already be marked occupied");
        };
        self.occupied_words.insert(index, word);
    }

    /// Merge a sorted, duplicate-free list of newly occupied words while
    /// keeping iteration metadata ordered independently of insertion order.
    fn merge_occupied_words(&mut self, added: &[u32]) {
        if added.is_empty() {
            return;
        }
        debug_assert!(added.windows(2).all(|pair| pair[0] < pair[1]));
        let Some(&last_existing) = self.occupied_words.last() else {
            self.occupied_words.extend_from_slice(added);
            return;
        };
        if added[0] > last_existing {
            self.occupied_words.extend_from_slice(added);
            return;
        }

        let mut merged = Vec::with_capacity(self.occupied_words.len() + added.len());
        let mut existing = self.occupied_words.iter().copied().peekable();
        let mut additions = added.iter().copied().peekable();
        while existing.peek().is_some() && additions.peek().is_some() {
            if existing.peek() < additions.peek() {
                merged.push(existing.next().expect("peeked existing word"));
            } else if existing.peek() > additions.peek() {
                merged.push(additions.next().expect("peeked added word"));
            } else {
                unreachable!("newly occupied words are disjoint from existing words");
            }
        }
        merged.extend(existing);
        merged.extend(additions);
        self.occupied_words = merged;
    }
}

impl Extend<ValueOrigin> for Values {
    fn extend<T: IntoIterator<Item = ValueOrigin>>(&mut self, iter: T) {
        for origin in iter {
            self.insert(origin);
        }
    }
}

struct BitIndices<I> {
    words: I,
    word_index: usize,
    remaining: u64,
}

impl<I: Iterator<Item = u64>> BitIndices<I> {
    fn new(words: I) -> Self {
        Self {
            words,
            word_index: 0,
            remaining: 0,
        }
    }
}

impl<I: Iterator<Item = u64>> Iterator for BitIndices<I> {
    type Item = u32;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if self.remaining != 0 {
                let bit = self.remaining.trailing_zeros();
                self.remaining &= self.remaining - 1;
                return Some(((self.word_index - 1) * u64::BITS as usize + bit as usize) as u32);
            }
            self.remaining = self.words.next()?;
            self.word_index += 1;
        }
    }
}

pub(crate) struct ValuesIter<'a> {
    bits: BitIndices<std::iter::Copied<std::slice::Iter<'a, u64>>>,
    origins: &'a OriginTable,
    unknown: bool,
    yielded_unknown: bool,
}

impl Iterator for ValuesIter<'_> {
    type Item = ValueOrigin;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(id) = self.bits.next() {
            return Some(self.origins.origin(OriginId(id)));
        }
        if self.unknown && !self.yielded_unknown {
            self.yielded_unknown = true;
            return Some(ValueOrigin::Unknown);
        }
        None
    }
}

pub(crate) struct ValuesIntoIter {
    bits: BitIndices<ArcWords>,
    origins: Arc<OriginTable>,
    unknown: bool,
    yielded_unknown: bool,
}

struct ArcWords {
    bitmap: Arc<ValueBitmap>,
    next: usize,
}

impl Iterator for ArcWords {
    type Item = u64;

    fn next(&mut self) -> Option<Self::Item> {
        let word = self.bitmap.bits.get(self.next).copied()?;
        self.next += 1;
        Some(word)
    }
}

impl Iterator for ValuesIntoIter {
    type Item = ValueOrigin;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(id) = self.bits.next() {
            return Some(self.origins.origin(OriginId(id)));
        }
        if self.unknown && !self.yielded_unknown {
            self.yielded_unknown = true;
            return Some(ValueOrigin::Unknown);
        }
        None
    }
}

impl IntoIterator for Values {
    type Item = ValueOrigin;
    type IntoIter = ValuesIntoIter;

    fn into_iter(self) -> Self::IntoIter {
        ValuesIntoIter {
            bits: BitIndices::new(ArcWords {
                bitmap: self.bitmap,
                next: 0,
            }),
            origins: self.origins,
            unknown: self.unknown,
            yielded_unknown: false,
        }
    }
}

impl<'a> IntoIterator for &'a Values {
    type Item = ValueOrigin;
    type IntoIter = ValuesIter<'a>;

    fn into_iter(self) -> Self::IntoIter {
        ValuesIter {
            bits: BitIndices::new(self.bitmap.bits.iter().copied()),
            origins: &self.origins,
            unknown: self.unknown,
            yielded_unknown: false,
        }
    }
}

/// Accumulate one expression directly into the compressed set. In particular,
/// a union does not materialize the result of each child union along the way.
struct ValuesBuilder {
    values: Values,
    #[cfg(test)]
    work: Rc<Cell<usize>>,
}

impl ValuesBuilder {
    fn new(origins: Arc<OriginTable>) -> Self {
        Self {
            values: Values::empty(origins),
            #[cfg(test)]
            work: Rc::new(Cell::new(0)),
        }
    }

    #[cfg(test)]
    fn with_work(origins: Arc<OriginTable>, work: Rc<Cell<usize>>) -> Self {
        Self {
            values: Values::empty(origins),
            work,
        }
    }

    fn child(&self) -> Self {
        #[cfg(test)]
        {
            Self {
                values: Values::empty(self.values.origins.clone()),
                work: self.work.clone(),
            }
        }
        #[cfg(not(test))]
        {
            Self::new(self.values.origins.clone())
        }
    }

    fn push(&mut self, value: ValueOrigin) {
        self.values.insert(value);
        #[cfg(test)]
        {
            self.work.set(self.work.get() + 1);
        }
    }

    fn extend(&mut self, values: &Values) {
        if values.is_empty() {
            return;
        }
        assert!(Arc::ptr_eq(&self.values.origins, &values.origins));
        self.values.union_without_change_tracking(values);
        #[cfg(test)]
        {
            self.work.set(self.work.get() + values.len());
        }
    }

    fn finish(self) -> Values {
        self.values
    }
}

/// A filtered view over candidate-reachability facts in the completed flow
/// store. Reachability itself is solved with value flow; this view only limits
/// results to the roots requested by one consumer.
pub(crate) struct CandidateObjectIndex<'a> {
    flow: &'a ChunkValueFlow,
    mask: Vec<u64>,
}

impl CandidateObjectIndex<'_> {
    fn visit_bits(&self, bits: &[u64], mut visit: impl FnMut(ValueOrigin)) {
        for (word_index, word) in bits.iter().copied().enumerate() {
            let mut remaining = word & self.mask.get(word_index).copied().unwrap_or_default();
            while remaining != 0 {
                let bit = remaining.trailing_zeros() as usize;
                visit(self.flow.candidate_objects[word_index * 64 + bit]);
                remaining &= remaining - 1;
            }
        }
    }

    pub(crate) fn for_binding(&self, id: &Id, visit: impl FnMut(ValueOrigin)) {
        if let Some(bits) = self.flow.candidate_bindings.get(id) {
            self.visit_bits(bits, visit);
        }
    }

    pub(crate) fn for_values(&self, values: &Values, mut visit: impl FnMut(ValueOrigin)) {
        for value in values {
            if let Some(bits) = self.flow.candidate_reachability.get(&value) {
                self.visit_bits(bits, &mut visit);
            }
        }
    }
}

fn bits_contain_id(bits: &[u64], id: OriginId) -> bool {
    bits.get(id.0 as usize / 64)
        .is_some_and(|word| word & (1 << (id.0 % 64)) != 0)
}

fn bits_contain_all(into: &[u64], from: &[u64]) -> bool {
    debug_assert_eq!(into.len(), from.len());
    into.iter()
        .zip(from)
        .all(|(into, from)| into & from == *from)
}

fn union_bits(into: &mut [u64], from: &[u64]) -> bool {
    debug_assert_eq!(into.len(), from.len());
    let mut changed = false;
    for (slot, bits) in into.iter_mut().zip(from) {
        let merged = *slot | bits;
        changed |= merged != *slot;
        *slot = merged;
    }
    changed
}

#[derive(Debug, Clone, Default)]
#[cfg_attr(test, derive(PartialEq, Eq))]
pub(crate) struct ChunkValueFlow {
    origin_table: Arc<OriginTable>,
    declared_ids: BTreeSet<Id>,
    bindings: BTreeMap<Id, Values>,
    properties: BTreeMap<u32, HashMap<String, Values>>,
    /// Cached union of all known properties, used by dynamic property reads.
    property_unions: BTreeMap<u32, Values>,
    returns: BTreeMap<u32, Values>,
    params: BTreeMap<u32, Vec<Option<Id>>>,
    /// Candidate roots and their transitive reachability are ordinary facts in
    /// the same store as bindings, properties, and returns.
    candidate_objects: Vec<ValueOrigin>,
    candidate_reachability: BTreeMap<ValueOrigin, Arc<Vec<u64>>>,
    candidate_bindings: BTreeMap<Id, Arc<Vec<u64>>>,
    /// Callable identity to its chunk-top declaration binding, if any.
    canonical: BTreeMap<ValueOrigin, Id>,
    /// Structural successful-return shapes, kept separate from the
    /// over-approximating return-origin fixed point. A missing entry says
    /// nothing about the value returned by the function.
    fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
    pub(crate) callable_source_unit: BTreeMap<ValueOrigin, SourceUnitOrdinal>,
    /// Exact native Set allocation sites, each backed by a validated author
    /// operation input. Other constructor results remain unknown.
    native_set_allocations: BTreeMap<u32, crate::analysis_inputs::NativeOperationInputRef>,
}

impl ChunkValueFlow {
    #[cfg(test)]
    pub(super) fn evaluate_value_expr_for_test(&self, expr: &Expr) -> (Values, usize) {
        let mut symbols = FlowSymbols::default();
        let value_expr = FlowValueExpr::from_ast(expr, &mut symbols);
        let work = Rc::new(Cell::new(0));
        let mut flow = self.clone();
        if flow.bindings.is_empty() && flow.properties.is_empty() && flow.returns.is_empty() {
            flow.origin_table = OriginTable::for_expr(expr);
        }
        let mut values = ValuesBuilder::with_work(flow.origin_table.clone(), work.clone());
        let declared = symbols.declared(&flow.declared_ids);
        append_value_expr(&value_expr, &mut values, &declared, &mut |key| {
            flow.values_for_key(&symbols, key)
        });
        (values.finish(), work.get())
    }

    pub(crate) fn candidate_object_index(
        &self,
        candidates: &BTreeSet<ValueOrigin>,
    ) -> CandidateObjectIndex<'_> {
        let mut mask = vec![0; self.candidate_objects.len().div_ceil(64)];
        for candidate in candidates {
            let index = self
                .candidate_objects
                .binary_search(candidate)
                .expect("candidate roots must be registered before value-flow solving");
            mask[index / 64] |= 1 << (index % 64);
        }
        CandidateObjectIndex { flow: self, mask }
    }

    /// Return the old ordered-map traversal as semantic maps for differential
    /// tests. This deliberately remains separate from the indexed production
    /// implementation so the test can catch mistakes in its ID layout.
    #[cfg(test)]
    pub(crate) fn candidate_object_index_reference_for_test(
        &self,
        candidates: &BTreeSet<ValueOrigin>,
    ) -> (
        BTreeMap<ValueOrigin, BTreeSet<ValueOrigin>>,
        BTreeMap<Id, BTreeSet<ValueOrigin>>,
    ) {
        let objects: Vec<_> = candidates.iter().copied().collect();
        let mut reverse: BTreeMap<ValueOrigin, BTreeSet<ValueOrigin>> = BTreeMap::new();
        for (function, values) in &self.returns {
            for value in values {
                reverse
                    .entry(value)
                    .or_default()
                    .insert(ValueOrigin::Function(*function));
            }
        }
        for (object, properties) in &self.properties {
            let container = if self.native_set_allocations.contains_key(object) {
                ValueOrigin::NativeSet(*object)
            } else {
                ValueOrigin::Object(*object)
            };
            for values in properties.values() {
                for value in values {
                    reverse.entry(value).or_default().insert(container);
                }
            }
        }
        let mut reachable = BTreeMap::new();
        let mut pending = VecDeque::new();
        let mut queued = BTreeSet::new();
        for object in objects.iter().copied() {
            let bits = reachable.entry(object).or_insert_with(BTreeSet::new);
            bits.insert(object);
            pending.push_back(object);
            queued.insert(object);
        }
        while let Some(value) = pending.pop_front() {
            queued.remove(&value);
            let reached = reachable[&value].clone();
            for predecessor in reverse.get(&value).into_iter().flatten() {
                let target = reachable.entry(*predecessor).or_default();
                let old_len = target.len();
                target.extend(reached.iter().copied());
                if target.len() != old_len && queued.insert(*predecessor) {
                    pending.push_back(*predecessor);
                }
            }
        }
        let bindings = self
            .bindings
            .iter()
            .filter_map(|(id, values)| {
                let mut objects = BTreeSet::new();
                for value in values {
                    if let Some(found) = reachable.get(&value) {
                        objects.extend(found.iter().copied());
                    }
                }
                (!objects.is_empty()).then_some((id.clone(), objects))
            })
            .collect();
        (reachable, bindings)
    }

    #[cfg(test)]
    pub(crate) fn candidate_object_index_maps_for_test(
        &self,
        candidates: &BTreeSet<ValueOrigin>,
    ) -> (
        BTreeMap<ValueOrigin, BTreeSet<ValueOrigin>>,
        BTreeMap<Id, BTreeSet<ValueOrigin>>,
    ) {
        let index = self.candidate_object_index(candidates);
        let reachable = self
            .candidate_reachability
            .iter()
            .filter_map(|(origin, bits)| {
                let mut values = BTreeSet::new();
                index.visit_bits(bits, |object| {
                    values.insert(object);
                });
                (!values.is_empty()).then_some((*origin, values))
            })
            .collect();
        let bindings = self
            .candidate_bindings
            .iter()
            .filter_map(|(id, bits)| {
                let mut values = BTreeSet::new();
                index.visit_bits(bits, |object| {
                    values.insert(object);
                });
                (!values.is_empty()).then_some((id.clone(), values))
            })
            .collect();
        (reachable, bindings)
    }

    pub(crate) fn analyze(body: &[TopLevelItemView<'_>]) -> Self {
        FlowProgram::collect(body, &BTreeSet::new()).solve().0
    }

    pub(crate) fn analyze_with_candidates(
        body: &[TopLevelItemView<'_>],
        candidates: &BTreeSet<ValueOrigin>,
    ) -> Self {
        FlowProgram::collect(body, candidates).solve().0
    }

    pub(crate) fn analyze_with_native_set_allocations(
        body: &[TopLevelItemView<'_>],
        candidates: &BTreeSet<ValueOrigin>,
        allocations: &BTreeMap<u32, crate::analysis_inputs::NativeOperationInputRef>,
    ) -> Self {
        let mut candidates = candidates.clone();
        candidates.extend(allocations.keys().copied().map(ValueOrigin::NativeSet));
        FlowProgram::collect_with_native_set_allocations(body, &candidates, allocations)
            .solve()
            .0
    }

    pub(crate) fn native_set_allocations(
        &self,
    ) -> &BTreeMap<u32, crate::analysis_inputs::NativeOperationInputRef> {
        &self.native_set_allocations
    }

    #[cfg(test)]
    pub(crate) fn analyze_with_stats(body: &[TopLevelItemView<'_>]) -> (Self, FlowWorkStats) {
        Self::analyze_with_candidates_and_stats(body, &BTreeSet::new())
    }

    #[cfg(test)]
    pub(crate) fn analyze_with_candidates_and_stats(
        body: &[TopLevelItemView<'_>],
        candidates: &BTreeSet<ValueOrigin>,
    ) -> (Self, FlowWorkStats) {
        let (flow, event_executions, argument_evaluations) =
            FlowProgram::collect(body, candidates).solve();
        (
            flow,
            FlowWorkStats {
                event_executions,
                argument_evaluations,
            },
        )
    }

    #[cfg(test)]
    pub(crate) fn analyze_reference(body: &[TopLevelItemView<'_>]) -> Self {
        let program = FlowProgram::collect(body, &BTreeSet::new());
        let mut flow = Self {
            origin_table: program.origin_table,
            ..Self::default()
        };
        let mut declarations = DeclaredBindingCollector::default();
        for item in body {
            item.as_module_item().visit_with(&mut declarations);
        }
        flow.declared_ids = declarations.ids;
        loop {
            let mut changed = false;
            for (ordinal, item) in body.iter().enumerate() {
                let mut pass = FlowPass {
                    flow: &mut flow,
                    changed: &mut changed,
                    source_unit: SourceUnitOrdinal(ordinal),
                    current_function: None,
                };
                item.as_module_item().visit_with(&mut pass);
            }
            if !changed {
                break;
            }
        }
        flow
    }

    #[cfg(test)]
    fn merge_binding(&mut self, id: Id, values: Values) -> bool {
        if values.is_empty() {
            return false;
        }
        assert!(Arc::ptr_eq(&self.origin_table, &values.origins));
        match self.bindings.entry(id) {
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(values);
                true
            }
            std::collections::btree_map::Entry::Occupied(mut entry) => {
                entry.get_mut().union_with(&values)
            }
        }
    }

    #[cfg(test)]
    fn merge_property(&mut self, object: u32, prop: String, values: Values) -> bool {
        if values.is_empty() {
            return false;
        }
        assert!(Arc::ptr_eq(&self.origin_table, &values.origins));
        let changed = match self.properties.entry(object).or_default().entry(prop) {
            std::collections::hash_map::Entry::Vacant(entry) => {
                entry.insert(values.clone());
                true
            }
            std::collections::hash_map::Entry::Occupied(mut entry) => {
                entry.get_mut().union_with(&values)
            }
        };
        if changed {
            // A wildcard-property read needs the exact union for this object.
            match self.property_unions.entry(object) {
                std::collections::btree_map::Entry::Vacant(entry) => {
                    entry.insert(values);
                }
                std::collections::btree_map::Entry::Occupied(mut entry) => {
                    entry.get_mut().union_with(&values);
                }
            }
        }
        changed
    }

    #[cfg(test)]
    fn merge_return(&mut self, function: u32, values: Values) -> bool {
        if values.is_empty() {
            return false;
        }
        assert!(Arc::ptr_eq(&self.origin_table, &values.origins));
        match self.returns.entry(function) {
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(values);
                true
            }
            std::collections::btree_map::Entry::Occupied(mut entry) => {
                entry.get_mut().union_with(&values)
            }
        }
    }

    pub(crate) fn values_of_binding(&self, id: &Id) -> Values {
        self.bindings.get(id).cloned().unwrap_or_else(|| {
            if self.declared_ids.contains(id) {
                Values::empty(self.origin_table.clone())
            } else {
                self.unknown_values()
            }
        })
    }

    pub(crate) fn is_declared(&self, id: &Id) -> bool {
        self.declared_ids.contains(id)
    }

    pub(crate) fn values_of_expr(&self, expr: &Expr) -> Values {
        match expr.unwrap_parens() {
            Expr::Ident(ident) => self.values_of_binding(&ident.to_id()),
            Expr::Fn(function) => {
                self.singleton_value(ValueOrigin::Function(function.function.span.lo.0))
            }
            Expr::Arrow(arrow) => self.singleton_value(ValueOrigin::Function(arrow.span.lo.0)),
            Expr::Class(class) => self.singleton_value(ValueOrigin::Class(class.class.span.lo.0)),
            Expr::Object(object) => self.singleton_value(ValueOrigin::Object(object.span.lo.0)),
            Expr::Array(array) => self.singleton_value(ValueOrigin::Object(array.span.lo.0)),
            Expr::New(construction)
                if self
                    .native_set_allocations
                    .contains_key(&construction.span.lo.0) =>
            {
                self.singleton_value(ValueOrigin::NativeSet(construction.span.lo.0))
            }
            Expr::Member(member) => {
                let prop = static_prop(&member.prop);
                let mut out = self.empty_values();
                for origin in self.values_of_expr(&member.obj) {
                    if let ValueOrigin::Object(object) | ValueOrigin::NativeSet(object) = origin {
                        if let Some(prop) = &prop {
                            if let Some(values) = self
                                .properties
                                .get(&object)
                                .and_then(|properties| properties.get(prop))
                            {
                                out.union_with(values);
                            } else {
                                out.insert(ValueOrigin::Unknown);
                            }
                        } else {
                            if let Some(properties) = self.properties.get(&object) {
                                // This path remains independent of the tracked solver's cached union.
                                // Set union is commutative, so HashMap iteration cannot affect facts.
                                for values in properties.values() {
                                    out.union_with(values);
                                }
                            }
                            out.insert(ValueOrigin::Unknown);
                        }
                    } else {
                        out.insert(ValueOrigin::Unknown);
                    }
                }
                out
            }
            Expr::Call(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    return self.unknown_values();
                };
                let mut out = self.empty_values();
                for origin in self.values_of_expr(callee) {
                    if let ValueOrigin::Function(function) = origin {
                        if let Some(values) = self.returns.get(&function) {
                            out.union_with(values);
                        }
                    } else {
                        out.insert(ValueOrigin::Unknown);
                    }
                }
                out
            }
            Expr::Cond(cond) => self
                .values_of_expr(&cond.cons)
                .union(self.values_of_expr(&cond.alt)),
            Expr::Bin(bin)
                if matches!(
                    bin.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) =>
            {
                self.values_of_expr(&bin.left)
                    .union(self.values_of_expr(&bin.right))
            }
            Expr::Seq(seq) => seq
                .exprs
                .last()
                .map_or_else(|| self.unknown_values(), |last| self.values_of_expr(last)),
            Expr::Assign(assign) => self.values_of_expr(&assign.right),
            Expr::Lit(_) | Expr::This(_) | Expr::Tpl(_) => self.empty_values(),
            _ => self.unknown_values(),
        }
    }

    fn empty_values(&self) -> Values {
        Values::empty(self.origin_table.clone())
    }

    fn singleton_value(&self, value: ValueOrigin) -> Values {
        Values::singleton(self.origin_table.clone(), value)
    }

    fn unknown_values(&self) -> Values {
        self.singleton_value(ValueOrigin::Unknown)
    }

    #[cfg(test)]
    fn values_for_key(&self, symbols: &FlowSymbols, key: FlowKey) -> Option<Values> {
        match key {
            FlowKey::Binding(slot) => self.bindings.get(symbols.id(slot)).cloned(),
            FlowKey::Property(object, property) => self
                .properties
                .get(&object)
                .and_then(|properties| properties.get(symbols.name(property)))
                .cloned(),
            FlowKey::Properties(object) => self.property_unions.get(&object).cloned(),
            FlowKey::Return(function) => self.returns.get(&function).cloned(),
            FlowKey::ValueFlowActive
            | FlowKey::CandidateBinding(_)
            | FlowKey::CandidateReachable(_)
            | FlowKey::Parameters(_)
            | FlowKey::ReadyParameters(_) => None,
        }
    }

    pub(crate) fn canonical_callable(&self, origin: ValueOrigin) -> Option<&Id> {
        self.canonical.get(&origin)
    }

    /// An exact local call target whose every normal return is a newly
    /// allocated record with the listed own data fields. Call evaluation and
    /// the function body's effects remain outside this value-shape fact.
    pub(crate) fn fresh_returned_record_keys(&self, call: &CallExpr) -> Option<&BTreeSet<String>> {
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let Expr::Ident(ident) = callee.unwrap_parens() else {
            return None;
        };
        if call.args.iter().any(|arg| arg.spread.is_some()) {
            return None;
        }
        let values = self.values_of_binding(&ident.to_id());
        let mut origins = values.iter();
        let ValueOrigin::Function(function) = origins.next()? else {
            return None;
        };
        if origins.next().is_some()
            || self.canonical_callable(ValueOrigin::Function(function)) != Some(&ident.to_id())
        {
            return None;
        }
        self.fresh_returned_record_keys.get(&function)
    }

    pub(crate) fn values_of_property(&self, object: u32, property: &str) -> Option<&Values> {
        self.properties
            .get(&object)
            .and_then(|properties| properties.get(property))
    }

    pub(crate) fn aliases_by_object(
        &self,
        objects: &BTreeSet<ValueOrigin>,
    ) -> BTreeMap<ValueOrigin, BTreeSet<String>> {
        let mut aliases: BTreeMap<ValueOrigin, BTreeSet<String>> = BTreeMap::new();
        for (id, values) in &self.bindings {
            for object in values.iter().filter(|object| objects.contains(object)) {
                aliases.entry(object).or_default().insert(id.0.to_string());
            }
        }
        aliases
    }

    pub(crate) fn call_targets(&self, callee: &Expr) -> CallTargets {
        let values = self.values_of_expr(callee);
        let complete = !values.is_empty()
            && values
                .iter()
                .all(|origin| self.canonical_callable(origin).is_some());
        CallTargets {
            bindings: values
                .into_iter()
                .filter_map(|origin| self.canonical_callable(origin).cloned())
                .collect(),
            complete,
        }
    }

    /// Add call targets discovered through parameters, aliases, returns and
    /// static object properties to the ordinary syntactic call buckets.
    pub(crate) fn augment_calls(
        &self,
        body: &[TopLevelItemView<'_>],
        facts: &mut [StatementFacts],
    ) {
        for (item, fact) in body.iter().zip(facts) {
            let mut collector = FlowCallCollector {
                flow: self,
                calls: PositionBucketed::default(),
                complete_ident_calls: PositionBucketed::default(),
                incomplete_ident_calls: PositionBucketed::default(),
                lazy_depth: 0,
                past_await: false,
                may_skip_await_depth: 0,
            };
            item.as_module_item().visit_with(&mut collector);
            for id in collector
                .complete_ident_calls
                .eager
                .difference(&collector.incomplete_ident_calls.eager)
            {
                fact.calls.eager.remove(id);
            }
            for id in collector
                .complete_ident_calls
                .lazy
                .difference(&collector.incomplete_ident_calls.lazy)
            {
                fact.calls.lazy.remove(id);
            }
            for id in collector
                .complete_ident_calls
                .first_order_lazy
                .difference(&collector.incomplete_ident_calls.first_order_lazy)
            {
                fact.calls.first_order_lazy.remove(id);
            }
            fact.calls.extend(collector.calls);
            if let (Some(class), Some(constructor)) = (
                super::class_of_item(item.as_module_item()),
                fact.constructor.as_mut(),
            ) {
                let mut ctor_calls = FlowCallCollector {
                    flow: self,
                    calls: PositionBucketed::default(),
                    complete_ident_calls: PositionBucketed::default(),
                    incomplete_ident_calls: PositionBucketed::default(),
                    lazy_depth: 0,
                    past_await: false,
                    may_skip_await_depth: 0,
                };
                for member in &class.body {
                    if matches!(member, ClassMember::Constructor(_))
                        || matches!(member, ClassMember::ClassProp(prop) if !prop.is_static)
                        || matches!(member, ClassMember::PrivateProp(prop) if !prop.is_static)
                        || matches!(member, ClassMember::AutoAccessor(accessor) if !accessor.is_static)
                    {
                        member.visit_with(&mut ctor_calls);
                    }
                }
                for id in ctor_calls
                    .complete_ident_calls
                    .first_order_lazy
                    .difference(&ctor_calls.incomplete_ident_calls.first_order_lazy)
                {
                    constructor.calls.remove(id);
                }
                constructor.calls.extend(ctor_calls.calls.first_order_lazy);
            }
        }
    }
}

fn append_value_expr(
    expr: &FlowValueExpr,
    values: &mut ValuesBuilder,
    declared: &[bool],
    read_values: &mut impl FnMut(FlowKey) -> Option<Values>,
) {
    let mut pending = vec![expr];
    while let Some(expr) = pending.pop() {
        if let FlowValueExpr::Union(left, right) = expr {
            // Visit the left subtree first, as the recursive evaluator did.
            pending.push(right);
            pending.push(left);
            continue;
        }
        match expr {
            FlowValueExpr::Binding(slot) => {
                if let Some(found) = read_values(FlowKey::Binding(*slot)) {
                    values.extend(&found);
                } else if !declared[slot.0 as usize] {
                    values.push(ValueOrigin::Unknown);
                }
            }
            FlowValueExpr::Value(value) => values.push(*value),
            FlowValueExpr::Member { object, property } => {
                let mut objects = values.child();
                append_value_expr(object, &mut objects, declared, read_values);
                for origin in objects.finish() {
                    if let ValueOrigin::Object(object) | ValueOrigin::NativeSet(object) = origin {
                        if let Some(property) = property {
                            // Unknown is durable even when a later write adds a value.
                            if let Some(found) = read_values(FlowKey::Property(object, *property)) {
                                values.extend(&found);
                            } else {
                                values.push(ValueOrigin::Unknown);
                            }
                        } else {
                            if let Some(found) = read_values(FlowKey::Properties(object)) {
                                values.extend(&found);
                            }
                            values.push(ValueOrigin::Unknown);
                        }
                    } else {
                        values.push(ValueOrigin::Unknown);
                    }
                }
            }
            FlowValueExpr::Call(callee) => {
                let mut targets = values.child();
                append_value_expr(callee, &mut targets, declared, read_values);
                for origin in targets.finish() {
                    if let ValueOrigin::Function(function) = origin {
                        if let Some(found) = read_values(FlowKey::Return(function)) {
                            values.extend(&found);
                        }
                    } else {
                        values.push(ValueOrigin::Unknown);
                    }
                }
            }
            FlowValueExpr::Union(_, _) => unreachable!("unions are handled by the work stack"),
            FlowValueExpr::Empty => {}
            FlowValueExpr::Unknown => values.push(ValueOrigin::Unknown),
        }
    }
}

#[derive(Default)]
#[cfg(test)]
struct DeclaredBindingCollector {
    ids: BTreeSet<Id>,
}

#[cfg(test)]
impl Visit for DeclaredBindingCollector {
    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.ids.insert(node.id.to_id());
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        self.ids.insert(node.ident.to_id());
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        self.ids.insert(node.ident.to_id());
        node.class.visit_with(self);
    }
}

/// A binding named anywhere in one flow program. Facts and rules are keyed
/// by slot so the worklist compares integers, not identifier text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
struct BindingSlot(u32);

/// A static property name written or read anywhere in one flow program.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
struct PropertySlot(u32);

#[derive(Default)]
struct FlowSymbols {
    bindings: Vec<Id>,
    binding_slots: HashMap<Id, BindingSlot>,
    properties: Vec<String>,
    property_slots: HashMap<String, PropertySlot>,
}

impl FlowSymbols {
    fn binding(&mut self, id: Id) -> BindingSlot {
        *self.binding_slots.entry(id).or_insert_with_key(|id| {
            self.bindings.push(id.clone());
            BindingSlot(u32::try_from(self.bindings.len() - 1).expect("binding slot overflow"))
        })
    }

    fn property(&mut self, name: String) -> PropertySlot {
        *self.property_slots.entry(name).or_insert_with_key(|name| {
            self.properties.push(name.clone());
            PropertySlot(u32::try_from(self.properties.len() - 1).expect("property slot overflow"))
        })
    }

    fn id(&self, slot: BindingSlot) -> &Id {
        &self.bindings[slot.0 as usize]
    }

    fn name(&self, slot: PropertySlot) -> &str {
        &self.properties[slot.0 as usize]
    }

    /// Whether each slot names a binding declared in the chunk, indexed by slot.
    fn declared(&self, declared_ids: &BTreeSet<Id>) -> Vec<bool> {
        self.bindings
            .iter()
            .map(|id| declared_ids.contains(id))
            .collect()
    }
}

#[derive(Debug, Clone)]
enum FlowValueExpr {
    Binding(BindingSlot),
    Value(ValueOrigin),
    Member {
        object: Box<FlowValueExpr>,
        property: Option<PropertySlot>,
    },
    Call(Box<FlowValueExpr>),
    Union(Box<FlowValueExpr>, Box<FlowValueExpr>),
    Empty,
    Unknown,
}

impl FlowValueExpr {
    fn from_ast(expr: &Expr, symbols: &mut FlowSymbols) -> Self {
        match expr.unwrap_parens() {
            Expr::Cond(cond) => Self::from_union_branches(&cond.cons, &cond.alt, symbols),
            Expr::Bin(bin)
                if matches!(
                    bin.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) =>
            {
                Self::from_union_branches(&bin.left, &bin.right, symbols)
            }
            expr => Self::from_ast_leaf(expr, symbols),
        }
    }

    fn from_union_branches(left: &Expr, right: &Expr, symbols: &mut FlowSymbols) -> Self {
        let mut pending = vec![right, left];
        let mut leaves = Vec::new();
        while let Some(expr) = pending.pop() {
            match expr.unwrap_parens() {
                Expr::Cond(cond) => {
                    pending.push(&cond.alt);
                    pending.push(&cond.cons);
                }
                Expr::Bin(bin)
                    if matches!(
                        bin.op,
                        BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                    ) =>
                {
                    pending.push(&bin.right);
                    pending.push(&bin.left);
                }
                leaf => leaves.push(Self::from_ast_leaf(leaf, symbols)),
            }
        }

        // A balanced tree keeps event cloning shallow while preserving the
        // left-to-right order of all non-union evaluations.
        Self::from_union_level(leaves)
    }

    fn from_union_level(mut leaves: Vec<Self>) -> Self {
        while leaves.len() > 1 {
            let mut next = Vec::with_capacity(leaves.len().div_ceil(2));
            let mut level = leaves.into_iter();
            while let Some(left) = level.next() {
                next.push(if let Some(right) = level.next() {
                    Self::Union(Box::new(left), Box::new(right))
                } else {
                    left
                });
            }
            leaves = next;
        }
        leaves.pop().unwrap_or(Self::Empty)
    }

    fn from_ast_leaf(expr: &Expr, symbols: &mut FlowSymbols) -> Self {
        match expr {
            Expr::Ident(ident) => Self::Binding(symbols.binding(ident.to_id())),
            Expr::Fn(function) => Self::Value(ValueOrigin::Function(function.function.span.lo.0)),
            Expr::Arrow(arrow) => Self::Value(ValueOrigin::Function(arrow.span.lo.0)),
            Expr::Class(class) => Self::Value(ValueOrigin::Class(class.class.span.lo.0)),
            Expr::Object(object) => Self::Value(ValueOrigin::Object(object.span.lo.0)),
            Expr::Array(array) => Self::Value(ValueOrigin::Object(array.span.lo.0)),
            Expr::Member(member) => Self::Member {
                object: Box::new(Self::from_ast(&member.obj, symbols)),
                property: static_prop(&member.prop).map(|name| symbols.property(name)),
            },
            Expr::Call(call) => match &call.callee {
                Callee::Expr(callee) => Self::Call(Box::new(Self::from_ast(callee, symbols))),
                _ => Self::Unknown,
            },
            Expr::Seq(seq) => seq
                .exprs
                .last()
                .map_or(Self::Unknown, |last| Self::from_ast(last, symbols)),
            Expr::Assign(assign) => Self::from_ast(&assign.right, symbols),
            Expr::Lit(_) | Expr::This(_) | Expr::Tpl(_) => Self::Empty,
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone)]
enum FlowEvent {
    FunctionDeclaration {
        id: BindingSlot,
        origin: ValueOrigin,
    },
    ClassDeclaration {
        id: BindingSlot,
        origin: ValueOrigin,
    },
    Variable {
        id: BindingSlot,
        init: FlowValueExpr,
    },
    AssignBinding {
        id: BindingSlot,
        right: FlowValueExpr,
    },
    AssignProperty {
        object: FlowValueExpr,
        property: PropertySlot,
        right: FlowValueExpr,
    },
    WritePropertyExpr {
        object: u32,
        property: PropertySlot,
        value: FlowValueExpr,
    },
    WritePropertyBinding {
        object: u32,
        property: PropertySlot,
        binding: BindingSlot,
    },
    WritePropertyValue {
        object: u32,
        property: PropertySlot,
        value: ValueOrigin,
    },
    FunctionMetadata {
        function: u32,
        params: Vec<Option<BindingSlot>>,
    },
    Return {
        function: u32,
        value: FlowValueExpr,
    },
    Invoke {
        callee: FlowValueExpr,
        args: Vec<FlowValueExpr>,
    },
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum FlowKey {
    /// Opens parameter-driven calls after the first ordinary value fact exists.
    ValueFlowActive,
    Binding(BindingSlot),
    CandidateBinding(BindingSlot),
    CandidateReachable(OriginId),
    Property(u32, PropertySlot),
    Properties(u32),
    Return(u32),
    Parameters(u32),
    /// Calls watch readiness so metadata-only input preserves legacy behavior.
    ReadyParameters(u32),
}

#[derive(Clone)]
enum FlowFact {
    ValueFlowActive,
    Values(Values),
    CandidateReachability(Arc<Vec<u64>>),
    Parameters(Vec<Option<BindingSlot>>),
    ReadyParameters(Vec<Option<BindingSlot>>),
}

/// Candidate reachability is a second stratum over the completed value facts:
/// no event rule reads a candidate key, so it never wakes AST events.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
enum CandidateRule {
    Reachability(OriginId),
    Binding(BindingSlot),
}

struct FlowProgram {
    declared_ids: BTreeSet<Id>,
    symbols: FlowSymbols,
    events: Vec<FlowEvent>,
    origin_table: Arc<OriginTable>,
    candidate_objects: Vec<ValueOrigin>,
    canonical: BTreeMap<ValueOrigin, Id>,
    callable_source_unit: BTreeMap<ValueOrigin, SourceUnitOrdinal>,
    native_set_allocations: BTreeMap<u32, crate::analysis_inputs::NativeOperationInputRef>,
    fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
}

impl FlowProgram {
    fn collect(body: &[TopLevelItemView<'_>], candidate_objects: &BTreeSet<ValueOrigin>) -> Self {
        Self::collect_with_native_set_allocations(body, candidate_objects, &BTreeMap::new())
    }

    fn collect_with_native_set_allocations(
        body: &[TopLevelItemView<'_>],
        candidate_objects: &BTreeSet<ValueOrigin>,
        native_set_allocations: &BTreeMap<u32, crate::analysis_inputs::NativeOperationInputRef>,
    ) -> Self {
        let mut collector = FlowProgramCollector {
            declared_ids: BTreeSet::new(),
            symbols: FlowSymbols::default(),
            events: Vec::new(),
            origins: BTreeSet::from([ValueOrigin::Unknown]),
            source_unit: SourceUnitOrdinal(0),
            current_function: None,
            canonical: BTreeMap::new(),
            callable_source_unit: BTreeMap::new(),
            native_set_allocations: native_set_allocations.clone(),
            fresh_returned_record_keys: BTreeMap::new(),
        };
        for (ordinal, item) in body.iter().enumerate() {
            collector.source_unit = SourceUnitOrdinal(ordinal);
            item.as_module_item().visit_with(&mut collector);
        }
        collector.origins.extend(candidate_objects.iter().copied());
        collector.origins.extend(
            native_set_allocations
                .keys()
                .copied()
                .map(ValueOrigin::NativeSet),
        );
        Self {
            declared_ids: collector.declared_ids,
            symbols: collector.symbols,
            events: collector.events,
            origin_table: Arc::new(OriginTable::from_origins(collector.origins)),
            candidate_objects: candidate_objects.iter().copied().collect(),
            canonical: collector.canonical,
            callable_source_unit: collector.callable_source_unit,
            native_set_allocations: native_set_allocations.clone(),
            fresh_returned_record_keys: collector.fresh_returned_record_keys,
        }
    }

    fn solve(self) -> (ChunkValueFlow, usize, usize) {
        let Self {
            declared_ids,
            symbols,
            events,
            origin_table,
            candidate_objects,
            canonical,
            callable_source_unit,
            native_set_allocations,
            fresh_returned_record_keys,
        } = self;
        let producer_count = events.len();
        #[cfg(test)]
        let argument_evaluations = Rc::new(Cell::new(0));
        #[cfg(test)]
        let event_executions = Rc::new(Cell::new(0));
        let rules = FlowRules {
            events,
            declared: symbols.declared(&declared_ids),
            origin_table: origin_table.clone(),
            #[cfg(test)]
            argument_evaluations: argument_evaluations.clone(),
            #[cfg(test)]
            event_executions: event_executions.clone(),
        };
        let values = IterativeInference::new(rules).solve_values_resumable(0..producer_count);
        let facts = if candidate_objects.is_empty() {
            values.into_values()
        } else {
            let rules = CandidateRules::new(
                origin_table.clone(),
                native_set_allocations.keys().copied().collect(),
                values.keys(),
                &candidate_objects,
            );
            let mut inference = IterativeInference::from_completed(rules, values);
            let candidate_words = candidate_objects.len().div_ceil(64);
            for (index, object) in candidate_objects.iter().copied().enumerate() {
                let mut bits = vec![0; candidate_words];
                bits[index / 64] |= 1 << (index % 64);
                inference.seed(
                    FlowKey::CandidateReachable(origin_table.id(object)),
                    FlowFact::CandidateReachability(Arc::new(bits)),
                );
            }
            inference.solve_values([])
        };
        #[cfg(test)]
        let evaluations = event_executions.get();
        #[cfg(not(test))]
        let evaluations = 0;
        let mut flow = ChunkValueFlow {
            candidate_objects,
            origin_table,
            declared_ids,
            canonical,
            callable_source_unit,
            native_set_allocations,
            fresh_returned_record_keys,
            ..ChunkValueFlow::default()
        };
        for (key, fact) in facts {
            match (key, fact) {
                (FlowKey::ValueFlowActive, FlowFact::ValueFlowActive)
                | (FlowKey::ReadyParameters(_), FlowFact::ReadyParameters(_)) => {}
                (FlowKey::CandidateReachable(origin), FlowFact::CandidateReachability(bits)) => {
                    flow.candidate_reachability
                        .insert(flow.origin_table.origin(origin), bits);
                }
                (FlowKey::CandidateBinding(slot), FlowFact::CandidateReachability(bits)) => {
                    flow.candidate_bindings
                        .insert(symbols.id(slot).clone(), bits);
                }
                (FlowKey::Binding(slot), FlowFact::Values(values)) => {
                    flow.bindings.insert(symbols.id(slot).clone(), values);
                }
                (FlowKey::Property(object, property), FlowFact::Values(values)) => {
                    flow.properties
                        .entry(object)
                        .or_default()
                        .insert(symbols.name(property).to_owned(), values);
                }
                (FlowKey::Properties(object), FlowFact::Values(values)) => {
                    flow.property_unions.insert(object, values);
                }
                (FlowKey::Return(function), FlowFact::Values(values)) => {
                    flow.returns.insert(function, values);
                }
                (FlowKey::Parameters(function), FlowFact::Parameters(params)) => {
                    flow.params.insert(
                        function,
                        params
                            .into_iter()
                            .map(|param| param.map(|slot| symbols.id(slot).clone()))
                            .collect(),
                    );
                }
                _ => unreachable!("flow key and fact variants must agree"),
            }
        }
        #[cfg(test)]
        let argument_evaluations = argument_evaluations.get();
        #[cfg(not(test))]
        let argument_evaluations = 0;
        (flow, evaluations, argument_evaluations)
    }
}

struct FlowProgramCollector {
    declared_ids: BTreeSet<Id>,
    symbols: FlowSymbols,
    events: Vec<FlowEvent>,
    origins: BTreeSet<ValueOrigin>,
    source_unit: SourceUnitOrdinal,
    current_function: Option<u32>,
    canonical: BTreeMap<ValueOrigin, Id>,
    callable_source_unit: BTreeMap<ValueOrigin, SourceUnitOrdinal>,
    native_set_allocations: BTreeMap<u32, crate::analysis_inputs::NativeOperationInputRef>,
    fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
}

impl Visit for FlowProgramCollector {
    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.declared_ids.insert(node.id.to_id());
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        let id = node.ident.to_id();
        let origin = ValueOrigin::Function(node.function.span.lo.0);
        self.declared_ids.insert(id.clone());
        self.canonical.entry(origin).or_insert_with(|| id.clone());
        self.events.push(FlowEvent::FunctionDeclaration {
            id: self.symbols.binding(id),
            origin,
        });
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        let id = node.ident.to_id();
        let origin = ValueOrigin::Class(node.class.span.lo.0);
        self.declared_ids.insert(id.clone());
        self.canonical.entry(origin).or_insert_with(|| id.clone());
        self.callable_source_unit
            .entry(origin)
            .or_insert(self.source_unit);
        self.events.push(FlowEvent::ClassDeclaration {
            id: self.symbols.binding(id),
            origin,
        });
        node.class.visit_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let (Pat::Ident(binding), Some(init)) = (&node.name, node.init.as_deref()) {
            if self.current_function.is_none()
                && let Some(origin) = direct_callable_origin(init)
            {
                self.canonical
                    .entry(origin)
                    .or_insert_with(|| binding.id.to_id());
            }
            self.events.push(FlowEvent::Variable {
                id: self.symbols.binding(binding.id.to_id()),
                init: match init {
                    Expr::New(construction)
                        if self
                            .native_set_allocations
                            .contains_key(&construction.span.lo.0) =>
                    {
                        FlowValueExpr::Value(ValueOrigin::NativeSet(construction.span.lo.0))
                    }
                    other => FlowValueExpr::from_ast(other, &mut self.symbols),
                },
            });
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        match &node.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                self.events.push(FlowEvent::AssignBinding {
                    id: self.symbols.binding(binding.id.to_id()),
                    right: FlowValueExpr::from_ast(&node.right, &mut self.symbols),
                });
            }
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                if let Some(property) = static_prop(&member.prop) {
                    self.events.push(FlowEvent::AssignProperty {
                        object: FlowValueExpr::from_ast(&member.obj, &mut self.symbols),
                        property: self.symbols.property(property),
                        right: FlowValueExpr::from_ast(&node.right, &mut self.symbols),
                    });
                }
            }
            _ => {}
        }
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        let object = node.span.lo.0;
        self.origins.insert(ValueOrigin::Object(object));
        for prop in &node.props {
            if let PropOrSpread::Prop(prop) = prop {
                match prop.as_ref() {
                    Prop::KeyValue(kv) => {
                        if let Some(property) = object_prop(&kv.key) {
                            self.events.push(FlowEvent::WritePropertyExpr {
                                object,
                                property: self.symbols.property(property),
                                value: FlowValueExpr::from_ast(&kv.value, &mut self.symbols),
                            });
                        }
                    }
                    Prop::Shorthand(ident) => {
                        self.events.push(FlowEvent::WritePropertyBinding {
                            object,
                            property: self.symbols.property(ident.sym.to_string()),
                            binding: self.symbols.binding(ident.to_id()),
                        });
                    }
                    Prop::Method(method) => {
                        if let Some(property) = object_prop(&method.key) {
                            self.events.push(FlowEvent::WritePropertyValue {
                                object,
                                property: self.symbols.property(property),
                                value: ValueOrigin::Function(method.function.span.lo.0),
                            });
                        }
                    }
                    _ => {}
                }
            }
        }
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        let object = node.span.lo.0;
        self.origins.insert(ValueOrigin::Object(object));
        for (index, element) in node.elems.iter().enumerate() {
            if let Some(element) = element {
                self.events.push(FlowEvent::WritePropertyExpr {
                    object,
                    property: self.symbols.property(index.to_string()),
                    value: FlowValueExpr::from_ast(&element.expr, &mut self.symbols),
                });
            }
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        let function = node.span.lo.0;
        if !node.is_async
            && !node.is_generator
            && let Some(body) = &node.body
            && let Some(keys) = fresh_returned_record_keys(body)
        {
            self.fresh_returned_record_keys.insert(function, keys);
        }
        self.origins.insert(ValueOrigin::Function(function));
        self.callable_source_unit
            .entry(ValueOrigin::Function(function))
            .or_insert(self.source_unit);
        self.events.push(FlowEvent::FunctionMetadata {
            function,
            params: node
                .params
                .iter()
                .map(|param| match &param.pat {
                    Pat::Ident(binding) => Some(self.symbols.binding(binding.id.to_id())),
                    _ => None,
                })
                .collect(),
        });
        let previous = self.current_function.replace(function);
        node.visit_children_with(self);
        self.current_function = previous;
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let function = node.span.lo.0;
        if !node.is_async
            && let ArrowFunctionBody::FunctionBody(body) = node.body.as_ref()
            && let Some(keys) = fresh_returned_record_keys(body)
        {
            self.fresh_returned_record_keys.insert(function, keys);
        }
        self.origins.insert(ValueOrigin::Function(function));
        self.callable_source_unit
            .entry(ValueOrigin::Function(function))
            .or_insert(self.source_unit);
        self.events.push(FlowEvent::FunctionMetadata {
            function,
            params: node
                .params
                .iter()
                .map(|param| match param {
                    Pat::Ident(binding) => Some(self.symbols.binding(binding.id.to_id())),
                    _ => None,
                })
                .collect(),
        });
        let previous = self.current_function.replace(function);
        if let ArrowFunctionBody::Expr(expr) = node.body.as_ref() {
            self.events.push(FlowEvent::Return {
                function,
                value: FlowValueExpr::from_ast(expr, &mut self.symbols),
            });
        }
        node.visit_children_with(self);
        self.current_function = previous;
    }

    fn visit_class(&mut self, node: &Class) {
        self.origins.insert(ValueOrigin::Class(node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let (Some(function), Some(value)) = (self.current_function, node.arg.as_deref()) {
            self.events.push(FlowEvent::Return {
                function,
                value: FlowValueExpr::from_ast(value, &mut self.symbols),
            });
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.events.push(FlowEvent::Invoke {
                callee: FlowValueExpr::from_ast(callee, &mut self.symbols),
                args: node
                    .args
                    .iter()
                    .map(|arg| FlowValueExpr::from_ast(&arg.expr, &mut self.symbols))
                    .collect(),
            });
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        if let Some(args) = &node.args {
            self.events.push(FlowEvent::Invoke {
                callee: FlowValueExpr::from_ast(&node.callee, &mut self.symbols),
                args: args
                    .iter()
                    .map(|arg| FlowValueExpr::from_ast(&arg.expr, &mut self.symbols))
                    .collect(),
            });
        }
        node.visit_children_with(self);
    }
}

fn direct_callable_origin(expr: &Expr) -> Option<ValueOrigin> {
    match expr.unwrap_parens() {
        Expr::Fn(function) => Some(ValueOrigin::Function(function.function.span.lo.0)),
        Expr::Arrow(arrow) => Some(ValueOrigin::Function(arrow.span.lo.0)),
        Expr::Class(class) => Some(ValueOrigin::Class(class.class.span.lo.0)),
        _ => None,
    }
}

/// A straight-line prefix cannot complete normally before the final return.
/// Throws in a prefix simply prevent the destructuring from running; their
/// ordinary call/throw/effect analysis is deliberately unchanged.
fn fresh_returned_record_keys(body: &FunctionBody) -> Option<BTreeSet<String>> {
    let (last, prefix) = body.stmts.split_last()?;
    if !prefix
        .iter()
        .all(|statement| matches!(statement, Stmt::Decl(Decl::Var(_)) | Stmt::Expr(_)))
    {
        return None;
    }
    let Stmt::Return(ret) = last else {
        return None;
    };
    let Expr::Object(object) = ret.arg.as_deref()?.unwrap_parens() else {
        return None;
    };
    own_data_keys(object)
}

/// Exact own data fields created by one fresh ordinary object literal.
/// Spread, computed names, accessors, methods and `__proto__` retain the
/// conservative getter/proxy classification at the consuming pattern.
pub(crate) fn own_data_keys(object: &ObjectLit) -> Option<BTreeSet<String>> {
    let mut keys = BTreeSet::new();
    for prop in &object.props {
        let PropOrSpread::Prop(prop) = prop else {
            return None;
        };
        let key = match prop.as_ref() {
            Prop::KeyValue(pair) => match &pair.key {
                PropName::Ident(ident) => ident.sym.to_string(),
                PropName::Str(string) => string.value.to_string_lossy().into_owned(),
                _ => return None,
            },
            Prop::Shorthand(ident) => ident.sym.to_string(),
            _ => return None,
        };
        if key == "__proto__" || !keys.insert(key) {
            return None;
        }
    }
    Some(keys)
}

#[cfg(test)]
#[derive(Debug, Default, Clone, Copy, Eq, PartialEq)]
pub(crate) struct FlowWorkStats {
    pub(crate) event_executions: usize,
    pub(crate) argument_evaluations: usize,
}

#[derive(Clone)]
struct CachedArgument {
    values: Values,
    binding_dependencies: BTreeSet<BindingSlot>,
}

struct FlowRules {
    events: Vec<FlowEvent>,
    /// Indexed by binding slot.
    declared: Vec<bool>,
    origin_table: Arc<OriginTable>,
    #[cfg(test)]
    argument_evaluations: Rc<Cell<usize>>,
    #[cfg(test)]
    event_executions: Rc<Cell<usize>>,
}

impl FlowRules {
    fn values_of_expr_tracking(
        &self,
        expr: &FlowValueExpr,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
        binding_dependencies: Option<&mut BTreeSet<BindingSlot>>,
    ) -> Values {
        let mut binding_dependencies = binding_dependencies;
        let mut values = ValuesBuilder::new(self.origin_table.clone());
        append_value_expr(expr, &mut values, &self.declared, &mut |key| {
            if let FlowKey::Binding(slot) = key
                && let Some(binding_dependencies) = binding_dependencies.as_deref_mut()
            {
                binding_dependencies.insert(slot);
            }
            match context.read(key) {
                Some(FlowFact::Values(values)) => Some(values.clone()),
                Some(_) => {
                    unreachable!("value expressions cannot read parameter metadata")
                }
                None => None,
            }
        });
        values.finish()
    }

    fn propose_values(
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
        key: FlowKey,
        values: Values,
    ) -> bool {
        if values.is_empty() {
            return false;
        }
        let changed = context.propose(key, FlowFact::Values(values));
        context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
        changed
    }

    fn propose_property(
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
        object: u32,
        property: PropertySlot,
        values: Values,
    ) {
        if values.is_empty() {
            return;
        }
        context.propose(
            FlowKey::Property(object, property),
            FlowFact::Values(values.clone()),
        );
        context.propose(FlowKey::Properties(object), FlowFact::Values(values));
        context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
    }

    fn singleton_value(&self, value: ValueOrigin) -> Values {
        Values::singleton(self.origin_table.clone(), value)
    }
}

impl IterativeRules for FlowRules {
    type Key = FlowKey;
    type Fact = FlowFact;
    type Rule = usize;

    fn evaluate(
        &self,
        index: &usize,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
    ) {
        #[cfg(test)]
        self.event_executions.set(self.event_executions.get() + 1);
        let event = &self.events[*index];
        match event {
            FlowEvent::FunctionDeclaration { id, origin }
            | FlowEvent::ClassDeclaration { id, origin } => {
                let values = self.singleton_value(*origin);
                Self::propose_values(context, FlowKey::Binding(*id), values);
            }
            FlowEvent::Variable { id, init } => {
                let values = self.values_of_expr_tracking(init, context, None);
                Self::propose_values(context, FlowKey::Binding(*id), values);
            }
            FlowEvent::AssignBinding { id, right } => {
                let values = self.values_of_expr_tracking(right, context, None);
                Self::propose_values(context, FlowKey::Binding(*id), values);
            }
            FlowEvent::AssignProperty {
                object,
                property,
                right,
            } => {
                let values = self.values_of_expr_tracking(right, context, None);
                let objects = self.values_of_expr_tracking(object, context, None);
                for origin in objects {
                    if let ValueOrigin::Object(object) | ValueOrigin::NativeSet(object) = origin {
                        Self::propose_property(context, object, *property, values.clone());
                    }
                }
            }
            FlowEvent::WritePropertyExpr {
                object,
                property,
                value,
            } => {
                let values = self.values_of_expr_tracking(value, context, None);
                Self::propose_property(context, *object, *property, values);
            }
            FlowEvent::WritePropertyBinding {
                object,
                property,
                binding,
            } => {
                let values =
                    self.values_of_expr_tracking(&FlowValueExpr::Binding(*binding), context, None);
                Self::propose_property(context, *object, *property, values);
            }
            FlowEvent::WritePropertyValue {
                object,
                property,
                value,
            } => {
                let values = self.singleton_value(*value);
                Self::propose_property(context, *object, *property, values);
            }
            FlowEvent::FunctionMetadata { function, params } => {
                context.propose(
                    FlowKey::Parameters(*function),
                    FlowFact::Parameters(params.clone()),
                );
                // The legacy solver only woke calls waiting for metadata if
                // some non-empty value fact grew in its initial source sweep.
                if context.read(FlowKey::ValueFlowActive).is_some() {
                    context.propose(
                        FlowKey::ReadyParameters(*function),
                        FlowFact::ReadyParameters(params.clone()),
                    );
                }
            }
            FlowEvent::Return { function, value } => {
                let values = self.values_of_expr_tracking(value, context, None);
                Self::propose_values(context, FlowKey::Return(*function), values);
            }
            FlowEvent::Invoke { callee, args } => {
                let targets = self.values_of_expr_tracking(callee, context, None);
                let mut cached_args: Vec<Option<CachedArgument>> = vec![None; args.len()];

                for target in targets {
                    let ValueOrigin::Function(function) = target else {
                        continue;
                    };
                    let params = match context.read(FlowKey::ReadyParameters(function)).cloned() {
                        Some(FlowFact::ReadyParameters(params)) => params,
                        Some(_) => {
                            unreachable!("parameter keys must contain parameter metadata")
                        }
                        None => continue,
                    };
                    for (arg_index, (param, arg)) in params.into_iter().zip(args).enumerate() {
                        let Some(param) = param else {
                            continue;
                        };
                        let values = if let Some(cached) = &cached_args[arg_index] {
                            cached.values.clone()
                        } else {
                            let mut binding_dependencies = BTreeSet::new();
                            #[cfg(test)]
                            self.argument_evaluations
                                .set(self.argument_evaluations.get() + 1);
                            let values = self.values_of_expr_tracking(
                                arg,
                                context,
                                Some(&mut binding_dependencies),
                            );
                            cached_args[arg_index] = Some(CachedArgument {
                                values: values.clone(),
                                binding_dependencies,
                            });
                            values
                        };
                        // Proposals are immediately visible to later arguments and targets.
                        // Recompute only arguments that read a binding this invocation grows.
                        if Self::propose_values(context, FlowKey::Binding(param), values) {
                            for cached in &mut cached_args {
                                if cached.as_ref().is_some_and(|cached| {
                                    cached.binding_dependencies.contains(&param)
                                }) {
                                    *cached = None;
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        merge_flow_fact(key, current, contribution)
    }
}

struct CandidateRules {
    origin_table: Arc<OriginTable>,
    native_set_allocations: BTreeSet<u32>,
    /// Bitmap over `OriginId` of the origins whose reachability can ever be
    /// present: seeded candidates and origins with a completed carrier fact.
    /// Every other origin's reachability key stays absent, so it is not read.
    may_reach: Vec<u64>,
}

impl CandidateRules {
    fn new<'a>(
        origin_table: Arc<OriginTable>,
        native_set_allocations: BTreeSet<u32>,
        completed_keys: impl IntoIterator<Item = &'a FlowKey>,
        candidates: &[ValueOrigin],
    ) -> Self {
        let mut rules = Self {
            may_reach: vec![0; origin_table.origins.len().div_ceil(64)],
            origin_table,
            native_set_allocations,
        };
        let origins: Vec<_> = completed_keys
            .into_iter()
            .filter_map(|key| rules.carrier_origin(key))
            .chain(candidates.iter().copied())
            .collect();
        for origin in origins {
            let id = rules.origin_table.id(origin).0;
            rules.may_reach[id as usize / 64] |= 1 << (id % 64);
        }
        rules
    }

    /// The origin whose reachability a completed property-union or return fact carries.
    fn carrier_origin(&self, key: &FlowKey) -> Option<ValueOrigin> {
        match key {
            FlowKey::Properties(object) if self.native_set_allocations.contains(object) => {
                Some(ValueOrigin::NativeSet(*object))
            }
            FlowKey::Properties(object) => Some(ValueOrigin::Object(*object)),
            FlowKey::Return(function) => Some(ValueOrigin::Function(*function)),
            _ => None,
        }
    }

    /// Union the reachability of every value, then propose it once.
    fn propose_candidate_values(
        &self,
        context: &mut impl FactContext<FlowKey, FlowFact>,
        target: FlowKey,
        values: &Values,
    ) {
        let mut reachable: Option<Arc<Vec<u64>>> = None;
        for id in values.ids_within(&self.may_reach) {
            let Some(fact) = context.read(FlowKey::CandidateReachable(id)) else {
                continue;
            };
            let FlowFact::CandidateReachability(bits) = fact else {
                unreachable!("candidate reachability keys must contain candidate facts")
            };
            match &mut reachable {
                None => reachable = Some(bits.clone()),
                Some(into) => {
                    if !bits_contain_all(into.as_slice(), bits.as_slice()) {
                        union_bits(Arc::make_mut(into).as_mut_slice(), bits.as_slice());
                    }
                }
            }
        }
        if let Some(bits) = reachable {
            context.propose(target, FlowFact::CandidateReachability(bits));
        }
    }

    /// Carrier values are final in this stratum: read them without watchers.
    fn carrier_values(
        context: &mut impl FactContext<FlowKey, FlowFact>,
        key: FlowKey,
    ) -> Option<Values> {
        match context.read_final(key)? {
            FlowFact::Values(values) => Some(values.clone()),
            _ => unreachable!("binding, property and return keys must contain value facts"),
        }
    }
}

impl IterativeRules for CandidateRules {
    type Key = FlowKey;
    type Fact = FlowFact;
    type Rule = CandidateRule;

    fn evaluate(&self, rule: &CandidateRule, context: &mut impl FactContext<FlowKey, FlowFact>) {
        match rule {
            CandidateRule::Binding(binding) => {
                let Some(values) = Self::carrier_values(context, FlowKey::Binding(*binding)) else {
                    return;
                };
                self.propose_candidate_values(
                    context,
                    FlowKey::CandidateBinding(*binding),
                    &values,
                );
            }
            CandidateRule::Reachability(origin) => {
                let carrier = match self.origin_table.origin(*origin) {
                    ValueOrigin::Object(object) | ValueOrigin::NativeSet(object) => {
                        FlowKey::Properties(object)
                    }
                    ValueOrigin::Function(function) => FlowKey::Return(function),
                    ValueOrigin::Class(_) | ValueOrigin::Unknown => return,
                };
                let Some(values) = Self::carrier_values(context, carrier) else {
                    return;
                };
                self.propose_candidate_values(
                    context,
                    FlowKey::CandidateReachable(*origin),
                    &values,
                );
            }
        }
    }

    fn producers_for(&self, key: &FlowKey, fact: &FlowFact) -> Vec<CandidateRule> {
        let FlowFact::Values(values) = fact else {
            return Vec::new();
        };
        if values.is_empty() {
            return Vec::new();
        }
        if let FlowKey::Binding(binding) = key {
            return vec![CandidateRule::Binding(*binding)];
        }
        self.carrier_origin(key)
            .map(|origin| CandidateRule::Reachability(self.origin_table.id(origin)))
            .into_iter()
            .collect()
    }

    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        merge_flow_fact(key, current, contribution)
    }
}

fn merge_flow_fact(key: &FlowKey, current: &mut FlowFact, contribution: FlowFact) -> bool {
    match (key, current, contribution) {
        (
            FlowKey::Parameters(_),
            FlowFact::Parameters(current),
            FlowFact::Parameters(contribution),
        ) => {
            assert_eq!(current, &contribution, "parameter metadata is immutable");
            false
        }
        (
            FlowKey::ReadyParameters(_),
            FlowFact::ReadyParameters(current),
            FlowFact::ReadyParameters(contribution),
        ) => {
            assert_eq!(
                current, &contribution,
                "ready parameter metadata is immutable"
            );
            false
        }
        (FlowKey::ValueFlowActive, FlowFact::ValueFlowActive, FlowFact::ValueFlowActive) => false,
        (
            FlowKey::CandidateReachable(_) | FlowKey::CandidateBinding(_),
            FlowFact::CandidateReachability(current),
            FlowFact::CandidateReachability(contribution),
        ) => {
            if Arc::ptr_eq(current, &contribution)
                || bits_contain_all(current.as_slice(), contribution.as_slice())
            {
                false
            } else {
                union_bits(
                    Arc::make_mut(current).as_mut_slice(),
                    contribution.as_slice(),
                )
            }
        }
        (
            FlowKey::Binding(_)
            | FlowKey::Property(_, _)
            | FlowKey::Properties(_)
            | FlowKey::Return(_),
            FlowFact::Values(current),
            FlowFact::Values(contribution),
        ) => current.union_with(&contribution),
        _ => unreachable!("flow key and fact variants must agree"),
    }
}

pub(crate) fn static_prop(prop: &MemberProp) -> Option<String> {
    match prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::Computed(computed) => match computed.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(string)) => Some(string.value.to_string_lossy().into_owned()),
            _ => None,
        },
        MemberProp::PrivateName(_) => None,
    }
}

fn object_prop(prop: &PropName) -> Option<String> {
    match prop {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(string) => Some(string.value.to_string_lossy().into_owned()),
        PropName::Num(num) => Some(num.value.to_string()),
        PropName::Computed(_) | PropName::BigInt(_) => None,
    }
}

#[cfg(test)]
struct FlowPass<'a> {
    flow: &'a mut ChunkValueFlow,
    changed: &'a mut bool,
    source_unit: SourceUnitOrdinal,
    current_function: Option<u32>,
}

#[cfg(test)]
impl FlowPass<'_> {
    fn merge_binding(&mut self, id: Id, values: Values) {
        *self.changed |= self.flow.merge_binding(id, values);
    }

    fn invoke(&mut self, callee: &Expr, args: &[ExprOrSpread]) {
        let targets = self.flow.values_of_expr(callee);
        for target in targets {
            let ValueOrigin::Function(function) = target else {
                continue;
            };
            let Some(params) = self.flow.params.get(&function).cloned() else {
                continue;
            };
            for (param, arg) in params.into_iter().zip(args) {
                if let Some(param) = param {
                    let values = self.flow.values_of_expr(&arg.expr);
                    self.merge_binding(param, values);
                }
            }
        }
    }

    fn visit_function_body(&mut self, function: u32, visit: impl FnOnce(&mut Self)) {
        self.flow
            .callable_source_unit
            .entry(ValueOrigin::Function(function))
            .or_insert(self.source_unit);
        let previous = self.current_function.replace(function);
        visit(self);
        self.current_function = previous;
    }
}

#[cfg(test)]
impl Visit for FlowPass<'_> {
    fn visit_fn_decl(&mut self, node: &FnDecl) {
        let key = node.function.span.lo.0;
        let value = ValueOrigin::Function(key);
        self.flow
            .canonical
            .entry(value)
            .or_insert_with(|| node.ident.to_id());
        let values = self.flow.singleton_value(value);
        self.merge_binding(node.ident.to_id(), values);
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        let value = ValueOrigin::Class(node.class.span.lo.0);
        self.flow
            .canonical
            .entry(value)
            .or_insert_with(|| node.ident.to_id());
        self.flow
            .callable_source_unit
            .entry(value)
            .or_insert(self.source_unit);
        let values = self.flow.singleton_value(value);
        self.merge_binding(node.ident.to_id(), values);
        node.class.visit_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let Pat::Ident(binding) = &node.name
            && let Some(init) = node.init.as_deref()
        {
            let values = self.flow.values_of_expr(init);
            if self.current_function.is_none() {
                for value in &values {
                    if matches!(value, ValueOrigin::Function(_) | ValueOrigin::Class(_))
                        && matches!(
                            init.unwrap_parens(),
                            Expr::Fn(_) | Expr::Arrow(_) | Expr::Class(_)
                        )
                    {
                        self.flow
                            .canonical
                            .entry(value)
                            .or_insert_with(|| binding.id.to_id());
                    }
                }
            }
            self.merge_binding(binding.id.to_id(), values);
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        let values = self.flow.values_of_expr(&node.right);
        match &node.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                self.merge_binding(binding.id.to_id(), values);
            }
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                if let Some(prop) = static_prop(&member.prop) {
                    for origin in self.flow.values_of_expr(&member.obj) {
                        if let ValueOrigin::Object(object) = origin {
                            *self.changed |=
                                self.flow
                                    .merge_property(object, prop.clone(), values.clone());
                        }
                    }
                }
            }
            _ => {}
        }
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        let object = node.span.lo.0;
        for prop in &node.props {
            if let PropOrSpread::Prop(prop) = prop {
                match prop.as_ref() {
                    Prop::KeyValue(kv) => {
                        if let Some(name) = object_prop(&kv.key) {
                            let values = self.flow.values_of_expr(&kv.value);
                            *self.changed |= self.flow.merge_property(object, name, values);
                        }
                    }
                    Prop::Shorthand(ident) => {
                        let values = self.flow.values_of_binding(&ident.to_id());
                        *self.changed |=
                            self.flow
                                .merge_property(object, ident.sym.to_string(), values);
                    }
                    Prop::Method(method) => {
                        if let Some(name) = object_prop(&method.key) {
                            let values = self
                                .flow
                                .singleton_value(ValueOrigin::Function(method.function.span.lo.0));
                            *self.changed |= self.flow.merge_property(object, name, values);
                        }
                    }
                    _ => {}
                }
            }
        }
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        let object = node.span.lo.0;
        for (index, element) in node.elems.iter().enumerate() {
            if let Some(element) = element {
                let values = self.flow.values_of_expr(&element.expr);
                *self.changed |= self.flow.merge_property(object, index.to_string(), values);
            }
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        let key = node.span.lo.0;
        self.flow.params.entry(key).or_insert_with(|| {
            node.params
                .iter()
                .map(|param| match &param.pat {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect()
        });
        self.visit_function_body(key, |s| node.visit_children_with(s));
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let key = node.span.lo.0;
        self.flow.params.entry(key).or_insert_with(|| {
            node.params
                .iter()
                .map(|param| match param {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect()
        });
        self.visit_function_body(key, |s| {
            if let ArrowFunctionBody::Expr(expr) = node.body.as_ref() {
                let values = s.flow.values_of_expr(expr);
                *s.changed |= s.flow.merge_return(key, values);
            }
            node.visit_children_with(s);
        });
    }

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let (Some(function), Some(arg)) = (self.current_function, node.arg.as_deref()) {
            let values = self.flow.values_of_expr(arg);
            *self.changed |= self.flow.merge_return(function, values);
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.invoke(callee, &node.args);
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        if let Some(args) = &node.args {
            self.invoke(&node.callee, args);
        }
        node.visit_children_with(self);
    }
}

struct FlowCallCollector<'a> {
    flow: &'a ChunkValueFlow,
    calls: PositionBucketed<BTreeSet<Id>>,
    complete_ident_calls: PositionBucketed<BTreeSet<Id>>,
    incomplete_ident_calls: PositionBucketed<BTreeSet<Id>>,
    lazy_depth: u32,
    past_await: bool,
    may_skip_await_depth: u32,
}

impl LazyBoundary for FlowCallCollector<'_> {
    fn lazy_depth_mut(&mut self) -> &mut u32 {
        &mut self.lazy_depth
    }

    fn past_await_mut(&mut self) -> &mut bool {
        &mut self.past_await
    }
    fn may_skip_await_depth_mut(&mut self) -> Option<&mut u32> {
        Some(&mut self.may_skip_await_depth)
    }
}

impl FlowCallCollector<'_> {
    fn record(&mut self, callee: &Expr) {
        let targets = self.flow.call_targets(callee);
        if let Expr::Ident(ident) = callee.unwrap_parens() {
            let id = ident.to_id();
            let bucket = if targets.complete {
                &mut self.complete_ident_calls
            } else {
                &mut self.incomplete_ident_calls
            };
            bucket.record(&id, self.lazy_depth, self.past_await);
        }
        for binding in targets.bindings {
            self.calls
                .record(&binding, self.lazy_depth, self.past_await);
        }
    }
}

impl Visit for FlowCallCollector<'_> {
    fn visit_opt_chain_expr(&mut self, node: &OptChainExpr) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        if matches!(
            node.op,
            AssignOp::AndAssign | AssignOp::OrAssign | AssignOp::NullishAssign
        ) {
            lazy_visit_may_skip(self, node);
        } else {
            node.visit_children_with(self);
        }
    }
    fn visit_if_stmt(&mut self, node: &IfStmt) {
        lazy_visit_if_stmt(self, node);
    }
    fn visit_cond_expr(&mut self, node: &CondExpr) {
        lazy_visit_cond_expr(self, node);
    }
    fn visit_bin_expr(&mut self, node: &BinExpr) {
        lazy_visit_bin_expr(self, node);
    }
    fn visit_while_stmt(&mut self, node: &WhileStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_stmt(&mut self, node: &ForStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_try_stmt(&mut self, node: &TryStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
        lazy_visit_may_skip(self, node);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.record(callee);
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        self.record(&node.callee);
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        lazy_visit_function(self, node);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        lazy_visit_arrow_expr(self, node);
    }

    fn visit_method_prop(&mut self, node: &MethodProp) {
        lazy_visit_method_prop(self, node);
    }

    fn visit_getter_prop(&mut self, node: &GetterProp) {
        lazy_visit_getter_prop(self, node);
    }

    fn visit_setter_prop(&mut self, node: &SetterProp) {
        lazy_visit_setter_prop(self, node);
    }

    fn visit_class(&mut self, node: &Class) {
        lazy_visit_class(self, node);
    }

    fn visit_class_member(&mut self, node: &ClassMember) {
        lazy_visit_class_member(self, node);
    }

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        node.arg.visit_with(self);
        if self.lazy_depth > 0 && self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }
}
