//! Monotone, intra-chunk value identities for local calls and object aliases.
//!
//! The sets deliberately over-approximate: an unsupported expression yields
//! `Unknown`, and every assignment adds possible values rather than replacing
//! them. The same facts feed at-init call promotion and PlainData's escape
//! check, so an alias/return cannot be understood by one pass and invisible to
//! the other. This is context-insensitive; it makes no claim about values from
//! other chunks.

use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::sync::Arc;
#[cfg(test)]
use std::{cell::Cell, rc::Rc};

use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

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
    Unknown,
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

/// Candidate-object reachability for repeated escape checks. The value-flow
/// graph is immutable after analysis, so propagate each candidate backwards
/// through return/property edges once instead of walking the whole graph at
/// every call or write site.
pub(crate) struct CandidateObjectIndex {
    objects: Vec<ValueOrigin>,
    origins: Arc<OriginTable>,
    reachable: Vec<Option<Vec<u64>>>,
    bindings: BTreeMap<Id, Vec<u64>>,
}

impl CandidateObjectIndex {
    fn visit_bits(&self, bits: &[u64], mut visit: impl FnMut(ValueOrigin)) {
        for (word_index, word) in bits.iter().copied().enumerate() {
            let mut remaining = word;
            while remaining != 0 {
                let bit = remaining.trailing_zeros() as usize;
                visit(self.objects[word_index * 64 + bit]);
                remaining &= remaining - 1;
            }
        }
    }

    pub(crate) fn for_binding(&self, id: &Id, visit: impl FnMut(ValueOrigin)) {
        if let Some(bits) = self.bindings.get(id) {
            self.visit_bits(bits, visit);
        }
    }

    pub(crate) fn for_values(&self, values: &Values, mut visit: impl FnMut(ValueOrigin)) {
        for value in values {
            let Some(origin_id) = self.origins.ids.get(&value) else {
                continue;
            };
            if let Some(Some(bits)) = self.reachable.get(origin_id.0 as usize) {
                self.visit_bits(bits, &mut visit);
            }
        }
    }
}

fn union_bits(into: &mut [u64], from: &[u64]) -> bool {
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
    /// Callable identity to its chunk-top declaration binding, if any.
    canonical: BTreeMap<ValueOrigin, Id>,
    pub(crate) callable_source_unit: BTreeMap<ValueOrigin, SourceUnitOrdinal>,
}

impl ChunkValueFlow {
    #[cfg(test)]
    pub(super) fn evaluate_value_expr_for_test(&self, expr: &Expr) -> (Values, usize) {
        let value_expr = FlowValueExpr::from_ast(expr);
        let mut dependencies = BTreeSet::new();
        let work = Rc::new(Cell::new(0));
        let mut flow = self.clone();
        if flow.bindings.is_empty() && flow.properties.is_empty() && flow.returns.is_empty() {
            flow.origin_table = OriginTable::for_expr(expr);
        }
        let mut values = ValuesBuilder::with_work(flow.origin_table.clone(), work.clone());
        flow.append_value_expr_tracking(&value_expr, &mut dependencies, &mut values);
        (values.finish(), work.get())
    }

    pub(crate) fn candidate_object_index(
        &self,
        candidates: &BTreeSet<ValueOrigin>,
    ) -> CandidateObjectIndex {
        let objects: Vec<_> = candidates.iter().copied().collect();
        let words = objects.len().div_ceil(64);
        // All fact origins are collected from this chunk's AST. Callers can
        // supply additional candidate origins, though, so extend the table
        // only when needed instead of assuming every query belongs to it.
        let origins = if candidates
            .iter()
            .all(|origin| self.origin_table.ids.contains_key(origin))
        {
            self.origin_table.clone()
        } else {
            Arc::new(OriginTable::from_origins(
                self.origin_table
                    .origins
                    .iter()
                    .copied()
                    .chain(candidates.iter().copied()),
            ))
        };

        // Origin IDs follow ValueOrigin::Ord, so sorting and deduplicating
        // each predecessor list preserves the old BTreeSet traversal order.
        let mut reverse = vec![Vec::<OriginId>::new(); origins.origins.len()];
        for (function, values) in &self.returns {
            for value in values {
                reverse[origins.id(value).0 as usize]
                    .push(origins.id(ValueOrigin::Function(*function)));
            }
        }
        // `merge_property` keeps this monotone union in sync whenever a
        // property's facts grow. Reusing it avoids rescanning all per-property
        // sets and deduplicating the same value across many properties here.
        for (object, values) in &self.property_unions {
            let source_unit = origins.id(ValueOrigin::Object(*object));
            for value in values {
                reverse[origins.id(value).0 as usize].push(source_unit);
            }
        }
        for predecessors in &mut reverse {
            predecessors.sort_unstable();
            predecessors.dedup();
        }

        // Keep the per-origin bitset lazy: most AST origins are not reachable
        // from any candidate, and allocating one dense candidate vector for
        // every origin would turn a sparse graph into O(origins * candidates)
        // memory before propagation starts.
        let mut reachable: Vec<Option<Vec<u64>>> = vec![None; origins.origins.len()];
        let mut pending = VecDeque::new();
        let mut queued = vec![false; origins.origins.len()];
        for (index, object) in objects.iter().copied().enumerate() {
            let origin = origins.id(object);
            let bits = reachable[origin.0 as usize].get_or_insert_with(|| vec![0; words]);
            bits[index / 64] |= 1 << (index % 64);
            pending.push_back(origin);
            queued[origin.0 as usize] = true;
        }
        while let Some(value) = pending.pop_front() {
            queued[value.0 as usize] = false;
            let bits = reachable[value.0 as usize]
                .as_ref()
                .expect("queued candidate origin must be reachable")
                .clone();
            for predecessor in &reverse[value.0 as usize] {
                let target =
                    reachable[predecessor.0 as usize].get_or_insert_with(|| vec![0; words]);
                let predecessor_index = predecessor.0 as usize;
                if union_bits(target, &bits) && !queued[predecessor_index] {
                    pending.push_back(*predecessor);
                    queued[predecessor_index] = true;
                }
            }
        }
        let bindings = self
            .bindings
            .iter()
            .filter_map(|(id, values)| {
                let mut bits = vec![0; words];
                for value in values {
                    if let Some(origin) = origins.ids.get(&value)
                        && let Some(found) = &reachable[origin.0 as usize]
                    {
                        union_bits(&mut bits, found);
                    }
                }
                bits.iter()
                    .any(|word| *word != 0)
                    .then_some((id.clone(), bits))
            })
            .collect();
        CandidateObjectIndex {
            objects,
            origins,
            reachable,
            bindings,
        }
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
            for values in properties.values() {
                for value in values {
                    reverse
                        .entry(value)
                        .or_default()
                        .insert(ValueOrigin::Object(*object));
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
        let reachable = index
            .reachable
            .iter()
            .enumerate()
            .filter_map(|(id, bits)| {
                let bits = bits.as_ref()?;
                let mut values = BTreeSet::new();
                index.visit_bits(bits, |object| {
                    values.insert(object);
                });
                Some((index.origins.origin(OriginId(id as u32)), values))
            })
            .collect();
        let bindings = index
            .bindings
            .iter()
            .map(|(id, bits)| {
                let mut values = BTreeSet::new();
                index.visit_bits(bits, |object| {
                    values.insert(object);
                });
                (id.clone(), values)
            })
            .collect();
        (reachable, bindings)
    }

    pub(crate) fn analyze(body: &[TopLevelItemView<'_>]) -> Self {
        FlowProgram::collect(body).solve()
    }

    #[cfg(test)]
    pub(crate) fn analyze_with_stats(body: &[TopLevelItemView<'_>]) -> (Self, FlowWorkStats) {
        let mut solver = FlowProgram::collect(body).into_solver();
        solver.run();
        (solver.flow, solver.stats)
    }

    #[cfg(test)]
    pub(crate) fn analyze_reference(body: &[TopLevelItemView<'_>]) -> Self {
        let program = FlowProgram::collect(body);
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
            Expr::Member(member) => {
                let prop = static_prop(&member.prop);
                let mut out = self.empty_values();
                for origin in self.values_of_expr(&member.obj) {
                    if let ValueOrigin::Object(object) = origin {
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

    fn values_of_binding_tracking(
        &self,
        id: &Id,
        dependencies: &mut BTreeSet<FlowDependency>,
    ) -> Values {
        dependencies.insert(FlowDependency::Binding(id.clone()));
        self.values_of_binding(id)
    }

    fn values_of_value_expr_tracking(
        &self,
        expr: &FlowValueExpr,
        dependencies: &mut BTreeSet<FlowDependency>,
    ) -> Values {
        let mut values = ValuesBuilder::new(self.origin_table.clone());
        self.append_value_expr_tracking(expr, dependencies, &mut values);
        values.finish()
    }

    fn append_value_expr_tracking(
        &self,
        expr: &FlowValueExpr,
        dependencies: &mut BTreeSet<FlowDependency>,
        values: &mut ValuesBuilder,
    ) {
        let mut pending = vec![expr];
        while let Some(expr) = pending.pop() {
            if let FlowValueExpr::Union(left, right) = expr {
                // Visit the left subtree first, as the recursive evaluator did.
                pending.push(right);
                pending.push(left);
            } else {
                self.append_non_union_value_expr_tracking(expr, dependencies, values);
            }
        }
    }

    fn append_non_union_value_expr_tracking(
        &self,
        expr: &FlowValueExpr,
        dependencies: &mut BTreeSet<FlowDependency>,
        values: &mut ValuesBuilder,
    ) {
        match expr {
            FlowValueExpr::Binding(id) => {
                dependencies.insert(FlowDependency::Binding(id.clone()));
                if let Some(found) = self.bindings.get(id) {
                    values.extend(found);
                } else if !self.declared_ids.contains(id) {
                    values.push(ValueOrigin::Unknown);
                }
            }
            FlowValueExpr::Value(value) => values.push(*value),
            FlowValueExpr::Member { object, property } => {
                let mut objects = values.child();
                self.append_value_expr_tracking(object, dependencies, &mut objects);
                for origin in objects.finish() {
                    if let ValueOrigin::Object(object) = origin {
                        if let Some(property) = property {
                            // Keep watching absent properties. Their Unknown is durable, while a later
                            // write can add concrete values.
                            dependencies.insert(FlowDependency::Property(object, property.clone()));
                            if let Some(found) = self
                                .properties
                                .get(&object)
                                .and_then(|properties| properties.get(property))
                            {
                                values.extend(found);
                            } else {
                                values.push(ValueOrigin::Unknown);
                            }
                        } else {
                            dependencies.insert(FlowDependency::Properties(object));
                            if let Some(found) = self.property_unions.get(&object) {
                                values.extend(found);
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
                self.append_value_expr_tracking(callee, dependencies, &mut targets);
                for origin in targets.finish() {
                    if let ValueOrigin::Function(function) = origin {
                        dependencies.insert(FlowDependency::Return(function));
                        if let Some(found) = self.returns.get(&function) {
                            values.extend(found);
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

    pub(crate) fn canonical_callable(&self, origin: ValueOrigin) -> Option<&Id> {
        self.canonical.get(&origin)
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

#[derive(Debug, Clone)]
enum FlowValueExpr {
    Binding(Id),
    Value(ValueOrigin),
    Member {
        object: Box<FlowValueExpr>,
        property: Option<String>,
    },
    Call(Box<FlowValueExpr>),
    Union(Box<FlowValueExpr>, Box<FlowValueExpr>),
    Empty,
    Unknown,
}

impl FlowValueExpr {
    fn from_ast(expr: &Expr) -> Self {
        match expr.unwrap_parens() {
            Expr::Cond(cond) => Self::from_union_branches(&cond.cons, &cond.alt),
            Expr::Bin(bin)
                if matches!(
                    bin.op,
                    BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
                ) =>
            {
                Self::from_union_branches(&bin.left, &bin.right)
            }
            expr => Self::from_ast_leaf(expr),
        }
    }

    fn from_union_branches(left: &Expr, right: &Expr) -> Self {
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
                leaf => leaves.push(Self::from_ast_leaf(leaf)),
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

    fn from_ast_leaf(expr: &Expr) -> Self {
        match expr {
            Expr::Ident(ident) => Self::Binding(ident.to_id()),
            Expr::Fn(function) => Self::Value(ValueOrigin::Function(function.function.span.lo.0)),
            Expr::Arrow(arrow) => Self::Value(ValueOrigin::Function(arrow.span.lo.0)),
            Expr::Class(class) => Self::Value(ValueOrigin::Class(class.class.span.lo.0)),
            Expr::Object(object) => Self::Value(ValueOrigin::Object(object.span.lo.0)),
            Expr::Array(array) => Self::Value(ValueOrigin::Object(array.span.lo.0)),
            Expr::Member(member) => Self::Member {
                object: Box::new(Self::from_ast(&member.obj)),
                property: static_prop(&member.prop),
            },
            Expr::Call(call) => match &call.callee {
                Callee::Expr(callee) => Self::Call(Box::new(Self::from_ast(callee))),
                _ => Self::Unknown,
            },
            Expr::Seq(seq) => seq
                .exprs
                .last()
                .map_or(Self::Unknown, |last| Self::from_ast(last)),
            Expr::Assign(assign) => Self::from_ast(&assign.right),
            Expr::Lit(_) | Expr::This(_) | Expr::Tpl(_) => Self::Empty,
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone)]
enum FlowEvent {
    FunctionDeclaration {
        id: Id,
        origin: ValueOrigin,
    },
    ClassDeclaration {
        id: Id,
        origin: ValueOrigin,
        source_unit: SourceUnitOrdinal,
    },
    Variable {
        id: Id,
        init: FlowValueExpr,
        canonical: Option<ValueOrigin>,
    },
    AssignBinding {
        id: Id,
        right: FlowValueExpr,
    },
    AssignProperty {
        object: FlowValueExpr,
        property: String,
        right: FlowValueExpr,
    },
    WritePropertyExpr {
        object: u32,
        property: String,
        value: FlowValueExpr,
    },
    WritePropertyBinding {
        object: u32,
        property: String,
        binding: Id,
    },
    WritePropertyValue {
        object: u32,
        property: String,
        value: ValueOrigin,
    },
    FunctionMetadata {
        function: u32,
        params: Vec<Option<Id>>,
        source_unit: SourceUnitOrdinal,
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

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum FlowDependency {
    Binding(Id),
    Property(u32, String),
    Properties(u32),
    Return(u32),
    Parameters(u32),
}

struct FlowProgram {
    declared_ids: BTreeSet<Id>,
    events: Vec<FlowEvent>,
    origin_table: Arc<OriginTable>,
}

impl FlowProgram {
    fn collect(body: &[TopLevelItemView<'_>]) -> Self {
        let mut collector = FlowProgramCollector {
            declared_ids: BTreeSet::new(),
            events: Vec::new(),
            origins: BTreeSet::from([ValueOrigin::Unknown]),
            source_unit: SourceUnitOrdinal(0),
            current_function: None,
        };
        for (ordinal, item) in body.iter().enumerate() {
            collector.source_unit = SourceUnitOrdinal(ordinal);
            item.as_module_item().visit_with(&mut collector);
        }
        Self {
            declared_ids: collector.declared_ids,
            events: collector.events,
            origin_table: Arc::new(OriginTable::from_origins(collector.origins)),
        }
    }

    fn into_solver(self) -> FlowSolver {
        FlowSolver {
            flow: ChunkValueFlow {
                origin_table: self.origin_table,
                declared_ids: self.declared_ids,
                ..ChunkValueFlow::default()
            },
            events: self.events,
            dependencies: Vec::new(),
            watchers: HashMap::new(),
            pending: BTreeSet::new(),
            scheduled_round: Vec::new(),
            round_zero_new_parameters: BTreeSet::new(),
            round_zero_had_fact_change: false,
            #[cfg(test)]
            stats: FlowWorkStats::default(),
        }
        .with_event_slots()
    }

    fn solve(self) -> ChunkValueFlow {
        let mut solver = self.into_solver();
        solver.run();
        solver.flow
    }
}

struct FlowProgramCollector {
    declared_ids: BTreeSet<Id>,
    events: Vec<FlowEvent>,
    origins: BTreeSet<ValueOrigin>,
    source_unit: SourceUnitOrdinal,
    current_function: Option<u32>,
}

impl Visit for FlowProgramCollector {
    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.declared_ids.insert(node.id.to_id());
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        let id = node.ident.to_id();
        let origin = ValueOrigin::Function(node.function.span.lo.0);
        self.declared_ids.insert(id.clone());
        self.events
            .push(FlowEvent::FunctionDeclaration { id, origin });
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        let id = node.ident.to_id();
        let origin = ValueOrigin::Class(node.class.span.lo.0);
        self.declared_ids.insert(id.clone());
        self.events.push(FlowEvent::ClassDeclaration {
            id,
            origin,
            source_unit: self.source_unit,
        });
        node.class.visit_with(self);
    }

    fn visit_var_declarator(&mut self, node: &VarDeclarator) {
        if let (Pat::Ident(binding), Some(init)) = (&node.name, node.init.as_deref()) {
            let canonical = self
                .current_function
                .is_none()
                .then(|| direct_callable_origin(init))
                .flatten();
            self.events.push(FlowEvent::Variable {
                id: binding.id.to_id(),
                init: FlowValueExpr::from_ast(init),
                canonical,
            });
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        match &node.left {
            AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                self.events.push(FlowEvent::AssignBinding {
                    id: binding.id.to_id(),
                    right: FlowValueExpr::from_ast(&node.right),
                });
            }
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                if let Some(property) = static_prop(&member.prop) {
                    self.events.push(FlowEvent::AssignProperty {
                        object: FlowValueExpr::from_ast(&member.obj),
                        property,
                        right: FlowValueExpr::from_ast(&node.right),
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
                                property,
                                value: FlowValueExpr::from_ast(&kv.value),
                            });
                        }
                    }
                    Prop::Shorthand(ident) => {
                        self.events.push(FlowEvent::WritePropertyBinding {
                            object,
                            property: ident.sym.to_string(),
                            binding: ident.to_id(),
                        });
                    }
                    Prop::Method(method) => {
                        if let Some(property) = object_prop(&method.key) {
                            self.events.push(FlowEvent::WritePropertyValue {
                                object,
                                property,
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
                    property: index.to_string(),
                    value: FlowValueExpr::from_ast(&element.expr),
                });
            }
        }
        node.visit_children_with(self);
    }

    fn visit_function(&mut self, node: &Function) {
        let function = node.span.lo.0;
        self.origins.insert(ValueOrigin::Function(function));
        self.events.push(FlowEvent::FunctionMetadata {
            function,
            params: node
                .params
                .iter()
                .map(|param| match &param.pat {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect(),
            source_unit: self.source_unit,
        });
        let previous = self.current_function.replace(function);
        node.visit_children_with(self);
        self.current_function = previous;
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let function = node.span.lo.0;
        self.origins.insert(ValueOrigin::Function(function));
        self.events.push(FlowEvent::FunctionMetadata {
            function,
            params: node
                .params
                .iter()
                .map(|param| match param {
                    Pat::Ident(binding) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect(),
            source_unit: self.source_unit,
        });
        let previous = self.current_function.replace(function);
        if let ArrowFunctionBody::Expr(expr) = node.body.as_ref() {
            self.events.push(FlowEvent::Return {
                function,
                value: FlowValueExpr::from_ast(expr),
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
                value: FlowValueExpr::from_ast(value),
            });
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.events.push(FlowEvent::Invoke {
                callee: FlowValueExpr::from_ast(callee),
                args: node
                    .args
                    .iter()
                    .map(|arg| FlowValueExpr::from_ast(&arg.expr))
                    .collect(),
            });
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        if let Some(args) = &node.args {
            self.events.push(FlowEvent::Invoke {
                callee: FlowValueExpr::from_ast(&node.callee),
                args: args
                    .iter()
                    .map(|arg| FlowValueExpr::from_ast(&arg.expr))
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

#[cfg(test)]
#[derive(Debug, Default, Clone, Copy, Eq, PartialEq)]
pub(crate) struct FlowWorkStats {
    pub(crate) event_executions: usize,
    pub(crate) argument_evaluations: usize,
}

#[derive(Clone)]
struct CachedArgument {
    values: Values,
    binding_dependencies: BTreeSet<Id>,
}

struct FlowSolver {
    flow: ChunkValueFlow,
    events: Vec<FlowEvent>,
    dependencies: Vec<BTreeSet<FlowDependency>>,
    watchers: HashMap<FlowDependency, Vec<usize>>,
    pending: BTreeSet<(usize, usize)>,
    scheduled_round: Vec<Option<usize>>,
    round_zero_new_parameters: BTreeSet<u32>,
    round_zero_had_fact_change: bool,
    #[cfg(test)]
    stats: FlowWorkStats,
}

impl FlowSolver {
    fn with_event_slots(mut self) -> Self {
        self.dependencies = vec![BTreeSet::new(); self.events.len()];
        self.scheduled_round = vec![None; self.events.len()];
        self
    }

    fn run(&mut self) {
        for index in 0..self.events.len() {
            self.run_event(index, 0, true);
        }
        // Legacy convergence only counted value-map merges, not params metadata.
        if self.round_zero_had_fact_change {
            let functions = std::mem::take(&mut self.round_zero_new_parameters);
            for function in functions {
                if let Some(watchers) = self.watchers.get(&FlowDependency::Parameters(function)) {
                    for &watcher in watchers {
                        schedule_event(&mut self.pending, &mut self.scheduled_round, watcher, 1);
                    }
                }
            }
        }
        while let Some((round, index)) = self.pending.pop_first() {
            self.scheduled_round[index] = None;
            self.run_event(index, round, false);
        }
    }

    fn run_event(&mut self, index: usize, round: usize, initial_sweep: bool) {
        #[cfg(test)]
        {
            self.stats.event_executions += 1;
        }

        let event = self.events[index].clone();
        let mut dependencies = BTreeSet::new();
        let mut changed = BTreeSet::new();
        match event {
            FlowEvent::FunctionDeclaration { id, origin } => {
                self.flow
                    .canonical
                    .entry(origin)
                    .or_insert_with(|| id.clone());
                let values = self.flow.singleton_value(origin);
                self.merge_binding(id, values, &mut changed);
            }
            FlowEvent::ClassDeclaration {
                id,
                origin,
                source_unit,
            } => {
                self.flow
                    .canonical
                    .entry(origin)
                    .or_insert_with(|| id.clone());
                self.flow
                    .callable_source_unit
                    .entry(origin)
                    .or_insert(source_unit);
                let values = self.flow.singleton_value(origin);
                self.merge_binding(id, values, &mut changed);
            }
            FlowEvent::Variable {
                id,
                init,
                canonical,
            } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&init, &mut dependencies);
                if let Some(origin) = canonical
                    && values.contains(&origin)
                {
                    self.flow
                        .canonical
                        .entry(origin)
                        .or_insert_with(|| id.clone());
                }
                self.merge_binding(id, values, &mut changed);
            }
            FlowEvent::AssignBinding { id, right } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&right, &mut dependencies);
                self.merge_binding(id, values, &mut changed);
            }
            FlowEvent::AssignProperty {
                object,
                property,
                right,
            } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&right, &mut dependencies);
                let objects = self
                    .flow
                    .values_of_value_expr_tracking(&object, &mut dependencies);
                for origin in objects {
                    if let ValueOrigin::Object(object) = origin {
                        self.merge_property(object, property.clone(), values.clone(), &mut changed);
                    }
                }
            }
            FlowEvent::WritePropertyExpr {
                object,
                property,
                value,
            } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&value, &mut dependencies);
                self.merge_property(object, property, values, &mut changed);
            }
            FlowEvent::WritePropertyBinding {
                object,
                property,
                binding,
            } => {
                let values = self
                    .flow
                    .values_of_binding_tracking(&binding, &mut dependencies);
                self.merge_property(object, property, values, &mut changed);
            }
            FlowEvent::WritePropertyValue {
                object,
                property,
                value,
            } => {
                let values = self.flow.singleton_value(value);
                self.merge_property(object, property, values, &mut changed);
            }
            FlowEvent::FunctionMetadata {
                function,
                params,
                source_unit,
            } => {
                if let std::collections::btree_map::Entry::Vacant(entry) =
                    self.flow.params.entry(function)
                {
                    entry.insert(params);
                    self.round_zero_new_parameters.insert(function);
                }
                self.flow
                    .callable_source_unit
                    .entry(ValueOrigin::Function(function))
                    .or_insert(source_unit);
            }
            FlowEvent::Return { function, value } => {
                let values = self
                    .flow
                    .values_of_value_expr_tracking(&value, &mut dependencies);
                self.merge_return(function, values, &mut changed);
            }
            FlowEvent::Invoke { callee, args } => {
                let targets = self
                    .flow
                    .values_of_value_expr_tracking(&callee, &mut dependencies);
                let mut cached_args: Vec<Option<CachedArgument>> = vec![None; args.len()];

                for target in targets {
                    let ValueOrigin::Function(function) = target else {
                        continue;
                    };
                    let Some(params) = self.flow.params.get(&function).cloned() else {
                        dependencies.insert(FlowDependency::Parameters(function));
                        continue;
                    };
                    for (arg_index, (param, arg)) in params.into_iter().zip(&args).enumerate() {
                        if let Some(param) = param {
                            let values = if let Some(cached) = &cached_args[arg_index] {
                                cached.values.clone()
                            } else {
                                let mut arg_dependencies = BTreeSet::new();
                                #[cfg(test)]
                                {
                                    self.stats.argument_evaluations += 1;
                                }
                                let values = self
                                    .flow
                                    .values_of_value_expr_tracking(arg, &mut arg_dependencies);
                                let binding_dependencies = arg_dependencies
                                    .iter()
                                    .filter_map(|dependency| match dependency {
                                        FlowDependency::Binding(id) => Some(id.clone()),
                                        _ => None,
                                    })
                                    .collect();
                                dependencies.extend(arg_dependencies);
                                cached_args[arg_index] = Some(CachedArgument {
                                    values: values.clone(),
                                    binding_dependencies,
                                });
                                values
                            };
                            // Invoke only changes parameter bindings. Cached argument queries
                            // remain exact until a binding they read actually grows; property,
                            // return, and parameter metadata facts are immutable during this
                            // event. Invalidate after each real merge so later arguments still
                            // observe the legacy left-to-right parameter updates.
                            if self.merge_binding(param.clone(), values, &mut changed) {
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
        if initial_sweep && !changed.is_empty() {
            self.round_zero_had_fact_change = true;
        }
        self.record_event_dependencies(index, dependencies);
        self.schedule_changed_events(index, round, initial_sweep, changed);
    }

    fn merge_binding(
        &mut self,
        id: Id,
        values: Values,
        changed: &mut BTreeSet<FlowDependency>,
    ) -> bool {
        let merged = self.flow.merge_binding(id.clone(), values);
        if merged {
            changed.insert(FlowDependency::Binding(id));
        }
        merged
    }

    fn merge_property(
        &mut self,
        object: u32,
        property: String,
        values: Values,
        changed: &mut BTreeSet<FlowDependency>,
    ) {
        if self.flow.merge_property(object, property.clone(), values) {
            changed.insert(FlowDependency::Property(object, property));
            changed.insert(FlowDependency::Properties(object));
        }
    }

    fn merge_return(
        &mut self,
        function: u32,
        values: Values,
        changed: &mut BTreeSet<FlowDependency>,
    ) {
        if self.flow.merge_return(function, values) {
            changed.insert(FlowDependency::Return(function));
        }
    }

    fn record_event_dependencies(&mut self, index: usize, dependencies: BTreeSet<FlowDependency>) {
        for dependency in dependencies {
            // The per-event set admits each watcher once, so a Vec is a compact
            // lookup-only reverse index with the same uniqueness semantics.
            if self.dependencies[index].insert(dependency.clone()) {
                self.watchers.entry(dependency).or_default().push(index);
            }
        }
    }

    fn schedule_changed_events(
        &mut self,
        index: usize,
        round: usize,
        initial_sweep: bool,
        changed: BTreeSet<FlowDependency>,
    ) {
        // Preserve sweep timing: only later source-order events can run this round.
        for dependency in changed {
            let Some(watchers) = self.watchers.get(&dependency) else {
                continue;
            };
            for &watcher in watchers {
                if initial_sweep && watcher > index {
                    continue;
                }
                let next_round = if watcher > index {
                    round
                } else {
                    round.checked_add(1).expect("value-flow round overflow")
                };
                // Watcher visitation order is immaterial: the pending set keeps
                // source-index order and schedule_event retains the earliest round.
                schedule_event(
                    &mut self.pending,
                    &mut self.scheduled_round,
                    watcher,
                    next_round,
                );
            }
        }
    }
}

fn schedule_event(
    pending: &mut BTreeSet<(usize, usize)>,
    scheduled_round: &mut [Option<usize>],
    index: usize,
    round: usize,
) {
    if scheduled_round[index].is_some_and(|scheduled| scheduled <= round) {
        return;
    }
    if let Some(scheduled) = scheduled_round[index].replace(round) {
        pending.remove(&(scheduled, index));
    }
    pending.insert((round, index));
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
