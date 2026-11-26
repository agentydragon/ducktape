//! Monotone value identities for calls and object aliases.
//!
//! The sets deliberately over-approximate: an unsupported expression yields
//! `Unknown`, and every assignment adds possible values rather than replacing
//! them. The same facts feed at-init call promotion and PlainData's escape
//! check, so an alias/return cannot be understood by one pass and invisible to
//! the other. `program` qualifies source identities and joins these same rules
//! across imports. The relation is context-insensitive and supplies no proof
//! of call effects, initialization availability or normal completion.

use work_metrics::SourceUnknownCause;

mod call_effects;
mod candidate_reachability;
mod guarded_member_reads;
mod initialization;
mod member_read_certificates;
mod program;
mod read_only_callbacks;
mod return_summary;
mod scalar_sequence;
use call_effects::{CallEffectBody, CallEffectRules, KnownCellAccesses};
use candidate_reachability::CandidateReachabilityClosure;
pub use initialization::CallableDeclaration;
use initialization::{InitializationCensus, InitializationSites};
use member_read_certificates::MemberReadCertificates;
pub use program::{ProgramFlowModule, ProgramValueFlows, program_value_flows};
use read_only_callbacks::ReadOnlyCallback;
use return_summary::ReturnSummary;

use rustc_hash::FxHashSet;
use source_identity::SourceUnitOrdinal;
use std::cell::RefCell;
#[cfg(test)]
use std::collections::VecDeque;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::{Arc, Weak};
#[cfg(test)]
use std::{cell::Cell, rc::Rc};

use roaring::{
    RoaringBitmap,
    bitmap::{IntoIter as RoaringIntoIter, Iter as RoaringIter},
};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use super::abstract_domain::primitive::{NoBindings, evaluate};
use analysis_model::PrimKinds;
use analysis_vocabulary::value::{Kind, Value};
use inference::{FactContext, IterativeInference, IterativeRules};

use super::{
    LazyBoundary, LazyPosition, PositionBucketed, StatementFacts, TopLevelItemView,
    lazy_visit_arrow_expr, lazy_visit_bin_expr, lazy_visit_class, lazy_visit_class_member,
    lazy_visit_cond_expr, lazy_visit_function, lazy_visit_getter_prop, lazy_visit_if_stmt,
    lazy_visit_may_skip, lazy_visit_method_prop, lazy_visit_setter_prop,
};

pub(crate) fn native_set_allocation_roots(
    inputs: &[analysis_vocabulary::inputs::ResolvedNativeOperationInput],
    inferred: &BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
) -> BTreeMap<u32, analysis_vocabulary::inputs::ProvenSetAllocation> {
    let mut allocations: BTreeMap<_, _> = inferred
        .iter()
        .map(|(site, proof)| {
            (
                *site,
                analysis_vocabulary::inputs::ProvenSetAllocation::Inferred(Box::new(proof.clone())),
            )
        })
        .collect();
    allocations.extend(
        inputs
            .iter()
            .filter(|input| {
                input.evidence.operation
                    == analysis_vocabulary::inputs::NativeInputKind::Operation(
                        spec::NativeOperationKind::SetFromPrimitiveArray,
                    )
            })
            .map(|input| {
                (
                    input.call_span.lo.0,
                    analysis_vocabulary::inputs::ProvenSetAllocation::AuthorInput(
                        input.evidence.reference.clone(),
                    ),
                )
            }),
    );
    allocations
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
/// positions. The vector order preserves `Value::Ord` when decoding.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OriginTable {
    origins: Vec<Value>,
    ids: HashMap<Value, OriginId>,
    function_class_ids: RoaringBitmap,
}

impl OriginTable {
    pub(super) fn from_origins(origins: impl IntoIterator<Item = Value>) -> Self {
        let mut origins: Vec<_> = origins.into_iter().chain([Value::Top]).collect();
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
        let function_class_ids = origins
            .iter()
            .enumerate()
            .filter(|(_, value)| matches!(value, Value::Alloc(Kind::Function | Kind::Class, _)))
            .map(|(index, _)| u32::try_from(index).expect("origin count exceeds u32"))
            .collect();
        Self {
            origins,
            ids,
            function_class_ids,
        }
    }

    fn id(&self, origin: Value) -> OriginId {
        *self
            .ids
            .get(&origin)
            .expect("Value must belong to this chunk")
    }

    fn origin(&self, id: OriginId) -> Value {
        self.origins[id.0 as usize]
    }

    #[cfg(test)]
    pub(super) fn for_expr(expr: &Expr) -> Arc<Self> {
        let mut collector = AllocationCollector::default();
        expr.visit_with(&mut collector);
        Arc::new(Self::from_origins(collector.origins))
    }
}

#[cfg(test)]
#[derive(Default)]
struct AllocationCollector {
    origins: BTreeSet<Value>,
}

#[cfg(test)]
impl Visit for AllocationCollector {
    fn visit_function(&mut self, node: &Function) {
        self.origins
            .insert(Value::Alloc(Kind::Function, node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        self.origins
            .insert(Value::Alloc(Kind::Function, node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_class(&mut self, node: &Class) {
        self.origins
            .insert(Value::Alloc(Kind::Class, node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        self.origins
            .insert(Value::Alloc(Kind::Object, node.span.lo.0));
        node.visit_children_with(self);
    }

    fn visit_array_lit(&mut self, node: &ArrayLit) {
        self.origins
            .insert(Value::Alloc(Kind::Object, node.span.lo.0));
        node.visit_children_with(self);
    }
}

impl Default for OriginTable {
    fn default() -> Self {
        Self::from_origins([])
    }
}

/// A set of semantic origins. All sets in one flow analysis share its immutable
/// origin table, so Roaring ids have identical meaning.
#[derive(Clone)]
pub struct Values {
    bitmap: Arc<RoaringBitmap>,
    unknown: bool,
    origins: Arc<OriginTable>,
}

impl PartialEq for Values {
    fn eq(&self, other: &Self) -> bool {
        self.bitmap == other.bitmap
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
            bitmap: Arc::new(RoaringBitmap::new()),
            unknown: false,
            origins,
        }
    }

    pub(super) fn singleton(origins: Arc<OriginTable>, value: Value) -> Self {
        let mut values = Self::empty(origins);
        values.insert(value);
        values
    }

    pub fn is_empty(&self) -> bool {
        self.bitmap.is_empty() && !self.unknown
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.bitmap.len() as usize + usize::from(self.unknown)
    }

    #[cfg(test)]
    pub(super) fn bitmap_payload_bytes(&self) -> u64 {
        let statistics = self.bitmap.statistics();
        statistics.n_bytes_array_containers
            + statistics.n_bytes_bitset_containers
            + statistics.n_bytes_run_containers
    }

    #[cfg(test)]
    pub(super) fn shares_bitmap(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.bitmap, &other.bitmap)
    }

    #[cfg(test)]
    pub(super) fn contains(&self, value: &Value) -> bool {
        if *value == Value::Top {
            return self.unknown;
        }
        let Some(id) = self.origins.ids.get(value) else {
            return false;
        };
        self.bitmap.contains(id.0)
    }

    pub(super) fn insert(&mut self, value: Value) -> bool {
        if value == Value::Top {
            return !std::mem::replace(&mut self.unknown, true);
        }
        let id = self.origins.id(value).0;
        Arc::make_mut(&mut self.bitmap).insert(id)
    }

    pub fn iter(&self) -> ValuesIter<'_> {
        ValuesIter {
            ids: self.bitmap.iter(),
            origins: &self.origins,
            unknown: self.unknown,
            yielded_unknown: false,
        }
    }

    /// Merge another value set, returning whether it contributed new origins.
    pub(super) fn union_with(&mut self, other: &Self) -> bool {
        assert!(Arc::ptr_eq(&self.origins, &other.origins));
        let mut changed = union_bitmap(&mut self.bitmap, &other.bitmap);
        if other.unknown && !self.unknown {
            self.unknown = true;
            changed = true;
        }
        changed
    }

    fn without_function_class(&self) -> Self {
        if self.bitmap.is_disjoint(&self.origins.function_class_ids) {
            return self.clone();
        }
        Self {
            bitmap: Arc::new(self.bitmap.as_ref() - &self.origins.function_class_ids),
            unknown: self.unknown,
            origins: self.origins.clone(),
        }
    }

    fn difference(&self, delivered: &Self) -> Self {
        assert!(Arc::ptr_eq(&self.origins, &delivered.origins));
        Self {
            bitmap: Arc::new(self.bitmap.as_ref() - delivered.bitmap.as_ref()),
            unknown: self.unknown && !delivered.unknown,
            origins: self.origins.clone(),
        }
    }

    fn union(mut self, other: Self) -> Self {
        self.union_with(&other);
        self
    }
}

fn union_bitmap(into: &mut Arc<RoaringBitmap>, from: &Arc<RoaringBitmap>) -> bool {
    union_bitmap_observed(into, from, |_| {})
}

fn union_bitmap_observed(
    into: &mut Arc<RoaringBitmap>,
    from: &Arc<RoaringBitmap>,
    observe: impl FnOnce(work_metrics::Operation),
) -> bool {
    use work_metrics::Operation;
    if Arc::ptr_eq(into, from) {
        observe(Operation::BitmapPointerSkip);
        return false;
    }
    if from.is_subset(into) {
        observe(Operation::BitmapSubsetSkip);
        return false;
    }
    if into.is_subset(from) {
        observe(Operation::BitmapSupersetAdoption);
        *into = from.clone();
        return true;
    }
    observe(Operation::BitmapCopyOnWriteUnion);
    *Arc::make_mut(into) |= from.as_ref();
    true
}

fn merge_values_with_scratch(
    current: &mut Values,
    contribution: Values,
    scratch: &mut FlowMergeScratch,
    observe: impl FnMut(work_metrics::Operation),
) -> bool {
    assert!(Arc::ptr_eq(&current.origins, &contribution.origins));
    let bitmap_changed = scratch.merge_bitmap(&mut current.bitmap, &contribution.bitmap, observe);
    let unknown_changed = contribution.unknown && !current.unknown;
    current.unknown |= contribution.unknown;
    bitmap_changed || unknown_changed
}

/// Per-rule-activation memo for the exact ordered bitmap joins that can recur
/// between a source write's receiver-specific and object-wide destinations.
/// Weak input witnesses avoid retaining old bitmap payloads; only at most eight
/// computed union outputs can be held strongly until this activation ends.
const FLOW_JOIN_MEMO_CAPACITY: usize = 8;

#[derive(Clone)]
enum BitmapJoinResult {
    SubsetSkip,
    SupersetAdoption,
    Union(Arc<RoaringBitmap>),
}

struct BitmapJoinMemoEntry {
    into_address: usize,
    from_address: usize,
    into: Weak<RoaringBitmap>,
    from: Weak<RoaringBitmap>,
    result: BitmapJoinResult,
}

struct FlowMergeScratch {
    entries: [Option<BitmapJoinMemoEntry>; FLOW_JOIN_MEMO_CAPACITY],
    next: usize,
    #[cfg(test)]
    work: Option<Rc<RefCell<FlowWorkStats>>>,
}

impl Default for FlowMergeScratch {
    fn default() -> Self {
        Self {
            entries: std::array::from_fn(|_| None),
            next: 0,
            #[cfg(test)]
            work: None,
        }
    }
}

impl FlowMergeScratch {
    #[cfg(test)]
    fn with_work(work: Rc<RefCell<FlowWorkStats>>) -> Self {
        Self {
            work: Some(work),
            ..Self::default()
        }
    }

    fn merge_bitmap(
        &mut self,
        into: &mut Arc<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
        mut observe: impl FnMut(work_metrics::Operation),
    ) -> bool {
        use work_metrics::Operation;
        if Arc::ptr_eq(into, from) {
            observe(Operation::BitmapPointerSkip);
            return false;
        }
        if let Some(result) = self.lookup(into, from) {
            #[cfg(test)]
            self.record_work(|work| work.bitmap_join_memo_hits += 1);
            observe(Operation::BitmapJoinMemoHit);
            return Self::apply(into, from, result);
        }

        #[cfg(test)]
        self.record_work(|work| work.bitmap_join_memo_misses += 1);
        observe(Operation::BitmapJoinMemoMiss);
        #[cfg(test)]
        self.record_work(|work| work.bitmap_subset_checks += 1);
        if from.is_subset(into) {
            observe(Operation::BitmapSubsetSkip);
            self.remember(into, from, BitmapJoinResult::SubsetSkip);
            return false;
        }

        #[cfg(test)]
        self.record_work(|work| work.bitmap_subset_checks += 1);
        if into.is_subset(from) {
            observe(Operation::BitmapSupersetAdoption);
            self.remember(into, from, BitmapJoinResult::SupersetAdoption);
            *into = from.clone();
            return true;
        }

        // Arc::make_mut clones a shared bitmap and dissociates Weak witnesses
        // before mutating a uniquely-owned allocation. Cache the old pair only
        // when another strong owner guarantees the witness remains stable.
        let cache_old_pair = Arc::strong_count(into) > 1;
        let into_witness =
            cache_old_pair.then(|| (Arc::as_ptr(into) as usize, Arc::downgrade(into)));
        #[cfg(test)]
        self.record_work(|work| work.bitmap_copy_on_write_unions += 1);
        *Arc::make_mut(into) |= from.as_ref();
        if let Some((into_address, into_witness)) = into_witness {
            self.remember_witnessed(
                into_address,
                into_witness,
                from,
                BitmapJoinResult::Union(into.clone()),
            );
        }
        observe(Operation::BitmapCopyOnWriteUnion);
        true
    }

    fn lookup(
        &self,
        into: &Arc<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
    ) -> Option<BitmapJoinResult> {
        let into_address = Arc::as_ptr(into) as usize;
        let from_address = Arc::as_ptr(from) as usize;
        self.entries.iter().flatten().find_map(|entry| {
            if entry.into_address != into_address || entry.from_address != from_address {
                return None;
            }
            let witnessed_into = entry.into.upgrade()?;
            let witnessed_from = entry.from.upgrade()?;
            (Arc::ptr_eq(&witnessed_into, into) && Arc::ptr_eq(&witnessed_from, from))
                .then(|| entry.result.clone())
        })
    }

    fn remember(
        &mut self,
        into: &Arc<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
        result: BitmapJoinResult,
    ) {
        self.remember_witnessed(
            Arc::as_ptr(into) as usize,
            Arc::downgrade(into),
            from,
            result,
        );
    }

    fn remember_witnessed(
        &mut self,
        into_address: usize,
        into: Weak<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
        result: BitmapJoinResult,
    ) {
        self.entries[self.next] = Some(BitmapJoinMemoEntry {
            into_address,
            from_address: Arc::as_ptr(from) as usize,
            into,
            from: Arc::downgrade(from),
            result,
        });
        self.next = (self.next + 1) % FLOW_JOIN_MEMO_CAPACITY;
    }

    fn apply(
        into: &mut Arc<RoaringBitmap>,
        from: &Arc<RoaringBitmap>,
        result: BitmapJoinResult,
    ) -> bool {
        match result {
            BitmapJoinResult::SubsetSkip => false,
            BitmapJoinResult::SupersetAdoption => {
                *into = from.clone();
                true
            }
            BitmapJoinResult::Union(union) => {
                *into = union;
                true
            }
        }
    }

    #[cfg(test)]
    fn record_work(&self, update: impl FnOnce(&mut FlowWorkStats)) {
        if let Some(work) = &self.work {
            update(&mut work.borrow_mut());
        }
    }
}

impl Extend<Value> for Values {
    fn extend<T: IntoIterator<Item = Value>>(&mut self, iter: T) {
        for origin in iter {
            self.insert(origin);
        }
    }
}

pub struct ValuesIter<'a> {
    ids: RoaringIter<'a>,
    origins: &'a OriginTable,
    unknown: bool,
    yielded_unknown: bool,
}

impl Iterator for ValuesIter<'_> {
    type Item = Value;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(id) = self.ids.next() {
            return Some(self.origins.origin(OriginId(id)));
        }
        if self.unknown && !self.yielded_unknown {
            self.yielded_unknown = true;
            return Some(Value::Top);
        }
        None
    }
}

pub struct ValuesIntoIter {
    ids: RoaringIntoIter,
    origins: Arc<OriginTable>,
    unknown: bool,
    yielded_unknown: bool,
}

impl Iterator for ValuesIntoIter {
    type Item = Value;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(id) = self.ids.next() {
            return Some(self.origins.origin(OriginId(id)));
        }
        if self.unknown && !self.yielded_unknown {
            self.yielded_unknown = true;
            return Some(Value::Top);
        }
        None
    }
}

impl IntoIterator for Values {
    type Item = Value;
    type IntoIter = ValuesIntoIter;

    fn into_iter(self) -> Self::IntoIter {
        ValuesIntoIter {
            ids: Arc::unwrap_or_clone(self.bitmap).into_iter(),
            origins: self.origins,
            unknown: self.unknown,
            yielded_unknown: false,
        }
    }
}

impl<'a> IntoIterator for &'a Values {
    type Item = Value;
    type IntoIter = ValuesIter<'a>;

    fn into_iter(self) -> Self::IntoIter {
        ValuesIter {
            ids: self.bitmap.iter(),
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

    fn push(&mut self, value: Value) {
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
        self.values.union_with(values);
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
    /// Requested candidate positions, stored without a chunk-wide zero prefix.
    mask: RoaringBitmap,
    #[cfg(test)]
    summary_intersections: Cell<usize>,
}

fn for_each_bitmap_intersection(
    left: &RoaringBitmap,
    right: &RoaringBitmap,
    mut visit: impl FnMut(u32),
) {
    let (iterate, contains) = if left.len() <= right.len() {
        (left, right)
    } else {
        (right, left)
    };
    for value in iterate.iter() {
        if contains.contains(value) {
            visit(value);
        }
    }
}

impl CandidateObjectIndex<'_> {
    fn visit_bits(&self, bits: &RoaringBitmap, mut visit: impl FnMut(Value)) {
        for_each_bitmap_intersection(bits, &self.mask, |index| {
            visit(self.flow.candidate_objects[index as usize]);
        });
    }

    pub(crate) fn for_binding(&self, id: &Id, visit: impl FnMut(Value)) {
        if let Some(bits) = self.flow.candidate_bindings.get(id) {
            self.visit_bits(bits, visit);
        }
    }

    /// Visit the union of reachable requested roots, once per root. Expanding
    /// each origin separately repeats every shared descendant at the sink.
    pub(crate) fn for_values(&self, values: &Values, mut visit: impl FnMut(Value)) {
        #[cfg(test)]
        self.summary_intersections.set(0);
        if self.mask.is_empty() {
            return;
        }

        let mut reached = RoaringBitmap::new();
        // CandidateReachabilityClosure shares one retained Arc across every
        // origin in an SCC. The flow immutably owns those Arcs for this query,
        // so their allocation addresses are stable identities until `self`
        // drops. Keep only one transient address per distinct summary and
        // intersect each one at most once.
        let mut seen_summaries = HashSet::new();
        for value in values {
            // Every union is intersected with the mask, so equal cardinality
            // means the requested roots have all been reached.
            if reached.len() == self.mask.len() {
                break;
            }
            let Some(bits) = self.flow.candidate_reachability.get(&value) else {
                continue;
            };
            if !seen_summaries.insert(Arc::as_ptr(bits)) {
                continue;
            }
            #[cfg(test)]
            self.summary_intersections
                .set(self.summary_intersections.get() + 1);
            let intersection = bits.as_ref() & &self.mask;
            reached |= &intersection;
        }
        for index in reached {
            visit(self.flow.candidate_objects[index as usize]);
        }
    }

    #[cfg(test)]
    pub(crate) fn summary_intersections_for_test(&self) -> usize {
        self.summary_intersections.get()
    }
}

#[derive(Debug, Clone, Default)]
#[cfg_attr(test, derive(PartialEq, Eq))]
pub struct ChunkValueFlow {
    origin_table: Arc<OriginTable>,
    /// Source-site numeric key certificates survive later expression queries.
    member_read_certificates: Arc<MemberReadCertificates>,
    program: Option<program::ChunkProgramFlow>,
    declared_ids: BTreeSet<Id>,
    bindings: BTreeMap<Id, Values>,
    properties: BTreeMap<u32, HashMap<String, Values>>,
    /// Property names indexed by the source-value slots retained in summaries.
    property_names_by_slot: Arc<Vec<String>>,
    binding_ids_by_slot: Arc<Vec<Id>>,
    /// Cached union of all known properties, used by dynamic property reads.
    property_unions: BTreeMap<u32, Values>,
    /// Cached union of statically known keys that may result from numeric ToPropertyKey.
    numeric_property_unions: BTreeMap<u32, Values>,
    returns: BTreeMap<u32, Values>,
    return_summaries: Arc<BTreeMap<u32, ReturnSummary>>,
    params: BTreeMap<u32, Vec<Option<Id>>>,
    initializations: BTreeMap<Id, InitializationSites>,
    /// Candidate roots and their transitive reachability are ordinary facts in
    /// the same store as bindings, properties, and returns.
    candidate_objects: Vec<Value>,
    candidate_reachability: BTreeMap<Value, Arc<RoaringBitmap>>,
    candidate_bindings: BTreeMap<Id, Arc<RoaringBitmap>>,
    /// Callable identity to its chunk-top declaration binding, if any.
    canonical: BTreeMap<Value, Id>,
    /// Structural successful-return shapes, kept separate from the
    /// over-approximating return-origin fixed point. A missing entry says
    /// nothing about the value returned by the function.
    fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
    read_only_callbacks: BTreeMap<u32, ReadOnlyCallback<Id>>,
    pub callable_source_unit: BTreeMap<Value, SourceUnitOrdinal>,
    /// Native Set construction sites backed by a validated author input or
    /// checked pre-flow source proof. Other constructor results stay unknown.
    native_set_allocations: BTreeMap<u32, analysis_vocabulary::inputs::ProvenSetAllocation>,
}

impl ChunkValueFlow {
    pub fn read_only_callback(&self, function: u32) -> Option<&ReadOnlyCallback<Id>> {
        self.read_only_callbacks.get(&function)
    }

    pub fn lexical_read_guards(&self) -> impl Iterator<Item = (&Id, SourceUnitOrdinal)> {
        self.initializations
            .iter()
            .filter_map(|(binding, sites)| sites.lexical_read_guard().map(|owner| (binding, owner)))
    }

    pub fn callable_declarations(&self) -> impl Iterator<Item = (&Id, CallableDeclaration)> {
        self.initializations
            .iter()
            .flat_map(|(binding, sites)| sites.callables().map(move |callable| (binding, callable)))
    }

    #[cfg(test)]
    pub(super) fn evaluate_value_expr_for_test(&self, expr: &Expr) -> (Values, usize) {
        let mut symbols = FlowSymbols {
            member_read_certificates: self.member_read_certificates.clone(),
            ..FlowSymbols::default()
        };
        let value_expr = FlowValueExpr::from_ast(expr, &mut symbols);
        let default_property = Some(symbols.property("default".into()));
        let summaries = self
            .return_summaries
            .iter()
            .map(|(function, summary)| {
                let summary = match summary {
                    ReturnSummary::ArgumentProperty(property) => {
                        let name = self.property_names_by_slot[property.0 as usize].clone();
                        ReturnSummary::ArgumentProperty(symbols.property(name))
                    }
                    ReturnSummary::ForwardChoices(helpers) => {
                        ReturnSummary::ForwardChoices(helpers.map(|helper| {
                            symbols.binding(self.binding_ids_by_slot[helper.0 as usize].clone())
                        }))
                    }
                    other => *other,
                };
                (*function, summary)
            })
            .collect();
        let work = Rc::new(Cell::new(0));
        let mut flow = self.clone();
        if flow.bindings.is_empty() && flow.properties.is_empty() && flow.returns.is_empty() {
            flow.origin_table = OriginTable::for_expr(expr);
        }
        let mut values = ValuesBuilder::with_work(flow.origin_table.clone(), work.clone());
        let declared = symbols.declared(&flow.declared_ids);
        append_value_expr(
            &value_expr,
            &mut values,
            &declared,
            &ReturnInputs {
                arguments: std::mem::take(&mut symbols.call_arguments),
                summaries: Arc::new(summaries),
                default_property,
                importer_allocations: Arc::default(),
            },
            &mut |key| flow.values_for_key(&symbols, key),
            &mut |_| {},
        );
        (values.finish(), work.get())
    }

    fn summary_values(&self, summary: ReturnSummary, actual: &Values) -> Values {
        let mut values = ValuesBuilder::new(self.origin_table.clone());
        let default_property = if summary == ReturnSummary::ArgumentOrDefault {
            self.property_names_by_slot
                .iter()
                .position(|name| name == "default")
                .map(|slot| PropertySlot(u32::try_from(slot).expect("property slot")))
        } else {
            None
        };
        append_summary_result(
            summary,
            actual,
            &mut values,
            &|slot| {
                self.declared_ids
                    .contains(&self.binding_ids_by_slot[slot.0 as usize])
            },
            &self.return_summaries,
            default_property,
            &mut |key| match key {
                FlowKey::Binding(slot) => self
                    .bindings
                    .get(&self.binding_ids_by_slot[slot.0 as usize])
                    .cloned(),
                FlowKey::Return(function) => self.returns.get(&function).cloned(),
                FlowKey::Property(object, property) => self
                    .properties
                    .get(&object)
                    .and_then(|properties| {
                        properties.get(&self.property_names_by_slot[property.0 as usize])
                    })
                    .cloned(),
                FlowKey::Properties(object) => self.property_unions.get(&object).cloned(),
                _ => unreachable!("return summaries read only value facts"),
            },
            &mut |_| {},
        );
        values.finish()
    }

    pub(crate) fn candidate_object_index(
        &self,
        candidates: &BTreeSet<Value>,
    ) -> CandidateObjectIndex<'_> {
        let mut mask = RoaringBitmap::new();
        for candidate in candidates {
            let index = self
                .candidate_objects
                .binary_search(candidate)
                .expect("candidate roots must be registered before value-flow solving");
            mask.insert(u32::try_from(index).expect("candidate object count exceeds u32"));
        }
        CandidateObjectIndex {
            flow: self,
            mask,
            #[cfg(test)]
            summary_intersections: Cell::new(0),
        }
    }

    #[cfg(test)]
    pub(crate) fn candidate_summaries_share_for_test(&self, left: Value, right: Value) -> bool {
        match (
            self.candidate_reachability.get(&left),
            self.candidate_reachability.get(&right),
        ) {
            (Some(left), Some(right)) => Arc::ptr_eq(left, right),
            _ => false,
        }
    }

    /// Return the old ordered-map traversal as semantic maps for differential
    /// tests. This deliberately remains separate from the indexed production
    /// implementation so the test can catch mistakes in its ID layout.
    #[cfg(test)]
    pub(crate) fn candidate_object_index_reference_for_test(
        &self,
        candidates: &BTreeSet<Value>,
    ) -> (
        BTreeMap<Value, BTreeSet<Value>>,
        BTreeMap<Id, BTreeSet<Value>>,
    ) {
        let objects: Vec<_> = candidates.iter().copied().collect();
        let mut reverse: BTreeMap<Value, BTreeSet<Value>> = BTreeMap::new();
        for (function, values) in &self.returns {
            for value in values {
                reverse
                    .entry(value)
                    .or_default()
                    .insert(Value::Alloc(Kind::Function, *function));
            }
        }
        for (object, properties) in &self.properties {
            let container = if self.native_set_allocations.contains_key(object) {
                Value::Alloc(Kind::Set, *object)
            } else {
                Value::Alloc(Kind::Object, *object)
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
        candidates: &BTreeSet<Value>,
    ) -> (
        BTreeMap<Value, BTreeSet<Value>>,
        BTreeMap<Id, BTreeSet<Value>>,
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

    #[cfg(test)]
    pub(super) fn candidate_reachability_storage_for_test(&self) -> (usize, u64, u64) {
        let mut cardinality = 0;
        let mut payload_bytes = 0;
        for bitmap in self.candidate_reachability.values() {
            let statistics = bitmap.statistics();
            cardinality += statistics.cardinality;
            payload_bytes += statistics.n_bytes_array_containers
                + statistics.n_bytes_bitset_containers
                + statistics.n_bytes_run_containers;
        }
        (
            self.candidate_reachability.len(),
            cardinality,
            payload_bytes,
        )
    }

    pub fn analyze(body: &[TopLevelItemView<'_>]) -> Self {
        FlowProgram::collect(body, &BTreeSet::new()).solve().0
    }

    pub fn analyze_with_candidates(
        body: &[TopLevelItemView<'_>],
        candidates: &BTreeSet<Value>,
    ) -> Self {
        FlowProgram::collect(body, candidates).solve().0
    }

    pub(crate) fn analyze_structural(
        body: &[TopLevelItemView<'_>],
        inputs: &[analysis_vocabulary::inputs::ResolvedNativeOperationInput],
        inferred: &BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
    ) -> Self {
        FlowProgram::structural(body, inputs, inferred).solve().0
    }

    pub(crate) fn native_set_allocations(
        &self,
    ) -> &BTreeMap<u32, analysis_vocabulary::inputs::ProvenSetAllocation> {
        &self.native_set_allocations
    }

    #[cfg(test)]
    pub(crate) fn analyze_with_stats(body: &[TopLevelItemView<'_>]) -> (Self, FlowWorkStats) {
        Self::analyze_with_candidates_and_stats(body, &BTreeSet::new())
    }

    #[cfg(test)]
    pub(crate) fn analyze_with_candidates_and_stats(
        body: &[TopLevelItemView<'_>],
        candidates: &BTreeSet<Value>,
    ) -> (Self, FlowWorkStats) {
        FlowProgram::collect(body, candidates).solve()
    }

    #[cfg(test)]
    pub(crate) fn analyze_reference(body: &[TopLevelItemView<'_>]) -> Self {
        let program = FlowProgram::collect(body, &BTreeSet::new());
        let property_names_by_slot = Arc::new(program.symbols.properties.clone());
        let initializations = program
            .events
            .iter()
            .filter_map(|event| {
                let FlowEvent::Initialization { id, sites } = event else {
                    return None;
                };
                Some((program.symbols.id(*id).clone(), sites.clone()))
            })
            .collect();
        let read_only_callbacks = program
            .cell_effects
            .iter()
            .filter_map(|(function, body)| {
                Some((
                    *function,
                    ReadOnlyCallback {
                        reads: body.reads.clone(),
                        returned: body.read_only_returns.clone()?,
                    }
                    .map(|slot| program.symbols.id(*slot).clone()),
                ))
            })
            .collect();
        let mut flow = Self {
            property_names_by_slot,
            binding_ids_by_slot: Arc::new(program.symbols.bindings.clone()),
            read_only_callbacks,
            origin_table: program.origin_table,
            member_read_certificates: program.symbols.member_read_certificates.clone(),
            return_summaries: Arc::new(program.return_summaries),
            initializations,
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
        // Keep the test oracle independent of the worklist solver's numeric
        // aggregate. Reads below filter the reference property table directly;
        // this cache is materialized only for result parity with production.
        for (&object, properties) in &flow.properties {
            let mut numeric_values = flow.empty_values();
            for (name, values) in properties {
                if may_be_numeric_property_key(name) {
                    numeric_values.union_with(values);
                }
            }
            if !numeric_values.is_empty() {
                flow.numeric_property_unions.insert(object, numeric_values);
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
                    entry.insert(values.clone());
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

    pub fn values_of_binding(&self, id: &Id) -> Values {
        self.bindings.get(id).cloned().unwrap_or_else(|| {
            if self.declared_ids.contains(id) {
                Values::empty(self.origin_table.clone())
            } else {
                self.unknown_values()
            }
        })
    }

    pub fn is_declared(&self, id: &Id) -> bool {
        self.declared_ids.contains(id)
    }

    fn values_of_member(&self, objects: Values, prop: Option<&str>, numeric: bool) -> Values {
        let mut out = self.empty_values();
        for origin in objects {
            if let Value::Alloc(Kind::Object, object) | Value::Alloc(Kind::Set, object) = origin {
                if let Some(prop) = prop {
                    if let Some(values) = self
                        .properties
                        .get(&object)
                        .and_then(|properties| properties.get(prop))
                    {
                        out.union_with(values);
                    } else {
                        out.insert(Value::Top);
                    }
                } else {
                    if numeric {
                        if let Some(properties) = self.properties.get(&object) {
                            // The reference oracle independently filters the
                            // property table instead of consulting the tracked
                            // solver's numeric aggregate cache.
                            for (name, values) in properties {
                                if may_be_numeric_property_key(name) {
                                    out.union_with(values);
                                }
                            }
                        }
                    } else if let Some(properties) = self.properties.get(&object) {
                        // This path remains independent of the tracked solver's cached union.
                        // Set union is commutative, so HashMap iteration cannot affect facts.
                        for values in properties.values() {
                            out.union_with(values);
                        }
                    }
                    out.insert(Value::Top);
                }
            } else {
                out.insert(Value::Top);
            }
        }
        out
    }

    pub(crate) fn values_of_expr(&self, expr: &Expr) -> Values {
        if let Some(program) = &self.program {
            return program.values_of_expr(expr, self);
        }
        match expr.unwrap_parens() {
            Expr::Ident(ident) => {
                let values = if let Some((receiver, property)) =
                    self.member_read_certificates.guarded_projection(ident.span)
                {
                    self.values_of_member(self.values_of_binding(receiver), Some(property), false)
                } else {
                    self.values_of_binding(&ident.to_id())
                };
                if self
                    .member_read_certificates
                    .excludes_function_class(ident.span, &ident.to_id())
                {
                    values.without_function_class()
                } else {
                    values
                }
            }
            Expr::Fn(function) => {
                self.singleton_value(Value::Alloc(Kind::Function, function.function.span.lo.0))
            }
            Expr::Arrow(arrow) => {
                self.singleton_value(Value::Alloc(Kind::Function, arrow.span.lo.0))
            }
            Expr::Class(class) => {
                self.singleton_value(Value::Alloc(Kind::Class, class.class.span.lo.0))
            }
            Expr::Object(object) => {
                self.singleton_value(Value::Alloc(Kind::Object, object.span.lo.0))
            }
            Expr::Array(array) => self.singleton_value(Value::Alloc(Kind::Object, array.span.lo.0)),
            Expr::New(construction)
                if self
                    .native_set_allocations
                    .contains_key(&construction.span.lo.0) =>
            {
                self.singleton_value(Value::Alloc(Kind::Set, construction.span.lo.0))
            }
            Expr::Member(member) => {
                let prop = static_prop(&member.prop);
                let numeric = computed_numeric_property(&member.prop)
                    || self.member_read_certificates.contains(member.span);
                self.values_of_member(self.values_of_expr(&member.obj), prop.as_deref(), numeric)
            }
            Expr::Call(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    return self.unknown_values();
                };
                let mut out = self.empty_values();
                for origin in self.values_of_expr(callee) {
                    if let Value::Alloc(Kind::Function, function) = origin {
                        if let Some(first) = call.args.first()
                            && call.args.iter().all(|argument| argument.spread.is_none())
                            && let Some(summary) = self.return_summaries.get(&function)
                        {
                            let actual = self.values_of_expr(&first.expr);
                            out.union_with(&self.summary_values(*summary, &actual));
                        } else if let Some(values) = self.returns.get(&function) {
                            out.union_with(values);
                        }
                    } else {
                        out.insert(Value::Top);
                    }
                }
                out
            }
            Expr::Cond(cond) => self
                .values_of_expr(&cond.cons)
                .union(self.values_of_expr(&cond.alt)),
            Expr::Bin(bin) if bin.op.may_short_circuit() => self
                .values_of_expr(&bin.left)
                .union(self.values_of_expr(&bin.right)),
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

    fn singleton_value(&self, value: Value) -> Values {
        Values::singleton(self.origin_table.clone(), value)
    }

    fn unknown_values(&self) -> Values {
        self.singleton_value(Value::Top)
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
            FlowKey::NumericProperties(object) => {
                self.numeric_property_unions.get(&object).cloned()
            }
            FlowKey::Return(function) => self.returns.get(&function).cloned(),
            FlowKey::ValueFlowActive
            | FlowKey::CandidateBinding(_)
            | FlowKey::CandidateReachable(_)
            | FlowKey::Parameters(_)
            | FlowKey::ReadyParameters(_)
            | FlowKey::InvokedCells(_)
            | FlowKey::RetainedCells(_)
            | FlowKey::SourceCells(_)
            | FlowKey::Initialization(_)
            | FlowKey::ReadOnlyCallback(_) => None,
        }
    }

    pub(crate) fn canonical_callable(&self, origin: Value) -> Option<&Id> {
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
        let Value::Alloc(Kind::Function, function) = origins.next()? else {
            return None;
        };
        if origins.next().is_some()
            || self.canonical_callable(Value::Alloc(Kind::Function, function))
                != Some(&ident.to_id())
        {
            return None;
        }
        self.fresh_returned_record_keys.get(&function)
    }

    pub fn values_of_property(&self, object: u32, property: &str) -> Option<&Values> {
        if let Some(program) = &self.program {
            return program.values_of_property(object, property);
        }
        self.properties.get(&object)?.get(property)
    }

    pub(crate) fn aliases_by_object(
        &self,
        objects: &BTreeSet<Value>,
    ) -> BTreeMap<Value, BTreeSet<String>> {
        let mut aliases: BTreeMap<Value, BTreeSet<String>> = BTreeMap::new();
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
    pub fn augment_calls(&self, body: &[TopLevelItemView<'_>], facts: &mut [StatementFacts]) {
        for (item, fact) in body.iter().zip(facts) {
            let mut collector = FlowCallCollector {
                flow: self,
                calls: PositionBucketed::default(),
                complete_ident_calls: PositionBucketed::default(),
                incomplete_ident_calls: PositionBucketed::default(),
                position: LazyPosition::default(),
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
                    position: LazyPosition::default(),
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

/// Per-call operands share an arena. The source-proved result relations have
/// constant size and are keyed by resolved function allocation identity.
struct ReturnInputs {
    arguments: Vec<FlowValueExpr>,
    summaries: Arc<BTreeMap<u32, ReturnSummary>>,
    default_property: Option<PropertySlot>,
    importer_allocations: Arc<Vec<RoaringBitmap>>,
}

fn append_value_expr(
    expr: &FlowValueExpr,
    values: &mut ValuesBuilder,
    declared: &[bool],
    returns: &ReturnInputs,
    read_values: &mut impl FnMut(FlowKey) -> Option<Values>,
    unknown: &mut impl FnMut(SourceUnknownCause),
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
                    unknown(SourceUnknownCause::UndeclaredBinding);
                    values.push(Value::Top);
                }
            }
            FlowValueExpr::NonFunction(inner) => {
                let mut result = values.child();
                append_value_expr(inner, &mut result, declared, returns, read_values, unknown);
                values.extend(&result.finish().without_function_class());
            }
            FlowValueExpr::Value(value) => values.push(*value),
            FlowValueExpr::Member { object, property } => {
                let mut objects = values.child();
                append_value_expr(
                    object,
                    &mut objects,
                    declared,
                    returns,
                    read_values,
                    unknown,
                );
                append_property_values(&objects.finish(), *property, values, read_values, unknown);
            }
            FlowValueExpr::NumericMember { object } => {
                let mut objects = values.child();
                append_value_expr(
                    object,
                    &mut objects,
                    declared,
                    returns,
                    read_values,
                    unknown,
                );
                append_numeric_property_values(&objects.finish(), values, read_values, unknown);
            }
            FlowValueExpr::Call {
                callee,
                argument,
                factory_importer,
            } => {
                if let Some(module) = factory_importer {
                    let mut result = values.child();
                    append_call_result(
                        callee,
                        *argument,
                        &mut result,
                        declared,
                        returns,
                        read_values,
                        unknown,
                    );
                    let mut result = result.finish();
                    // Only direct result identities are refined. Top is separate
                    // from this bitmap and all ordinary invocation/retained facts
                    // survive; no purity or successful-return proof follows.
                    *Arc::make_mut(&mut result.bitmap) -= &returns.importer_allocations[*module];
                    values.extend(&result);
                } else {
                    append_call_result(
                        callee,
                        *argument,
                        values,
                        declared,
                        returns,
                        read_values,
                        unknown,
                    );
                }
            }
            FlowValueExpr::Union(_, _) => unreachable!("unions are handled by the work stack"),
            FlowValueExpr::Empty => {}
            FlowValueExpr::Unknown(cause) => {
                unknown(*cause);
                values.push(Value::Top);
            }
        }
    }
}

/// The ordinary call-result transfer, shared by constrained and unconstrained
/// calls. Constraints wrap this transfer; they do not change its reads or calls.
fn append_call_result(
    callee: &FlowValueExpr,
    argument: Option<usize>,
    values: &mut ValuesBuilder,
    declared: &[bool],
    returns: &ReturnInputs,
    read_values: &mut impl FnMut(FlowKey) -> Option<Values>,
    unknown: &mut impl FnMut(SourceUnknownCause),
) {
    let mut targets = values.child();
    append_value_expr(
        callee,
        &mut targets,
        declared,
        returns,
        read_values,
        unknown,
    );
    let mut actual = None;
    for origin in targets.finish() {
        if let Value::Alloc(Kind::Function, function) = origin {
            if let Some(argument) = argument
                && let Some(summary) = returns.summaries.get(&function)
            {
                let argument = actual.get_or_insert_with(|| {
                    let mut result = values.child();
                    append_value_expr(
                        &returns.arguments[argument],
                        &mut result,
                        declared,
                        returns,
                        read_values,
                        unknown,
                    );
                    result.finish()
                });
                append_summary_result(
                    *summary,
                    argument,
                    values,
                    &|slot| declared[slot.0 as usize],
                    &returns.summaries,
                    returns.default_property,
                    read_values,
                    unknown,
                );
            } else if let Some(found) = read_values(FlowKey::Return(function)) {
                values.extend(&found);
            }
        } else {
            unknown(SourceUnknownCause::NonFunctionCallee);
            values.push(Value::Top);
        }
    }
}

/// A constant-size relation. Forwarding expands exactly one level; another
/// forwarder or an unsupported helper retains its ordinary global Return.
#[allow(clippy::too_many_arguments)]
fn append_summary_result(
    summary: ReturnSummary,
    actual: &Values,
    values: &mut ValuesBuilder,
    declared: &impl Fn(BindingSlot) -> bool,
    summaries: &BTreeMap<u32, ReturnSummary>,
    default_property: Option<PropertySlot>,
    read_values: &mut impl FnMut(FlowKey) -> Option<Values>,
    unknown: &mut impl FnMut(SourceUnknownCause),
) {
    if append_leaf_summary(
        summary,
        actual,
        values,
        default_property,
        read_values,
        unknown,
    ) {
        return;
    }
    let ReturnSummary::ForwardChoices(helpers) = summary else {
        unreachable!()
    };
    for helper in helpers {
        let Some(targets) = read_values(FlowKey::Binding(helper)) else {
            if !declared(helper) {
                unknown(SourceUnknownCause::UndeclaredBinding);
                values.push(Value::Top);
            }
            continue;
        };
        for target in targets {
            if let Value::Alloc(Kind::Function, function) = target {
                match summaries.get(&function) {
                    Some(leaf @ (ReturnSummary::Argument | ReturnSummary::ArgumentProperty(_))) => {
                        append_leaf_summary(
                            *leaf,
                            actual,
                            values,
                            default_property,
                            read_values,
                            unknown,
                        );
                    }
                    _ => {
                        if let Some(found) = read_values(FlowKey::Return(function)) {
                            values.extend(&found);
                        }
                    }
                }
            } else {
                unknown(SourceUnknownCause::NonFunctionCallee);
                values.push(Value::Top);
            }
        }
    }
}

fn append_leaf_summary(
    summary: ReturnSummary,
    actual: &Values,
    values: &mut ValuesBuilder,
    default_property: Option<PropertySlot>,
    read_values: &mut impl FnMut(FlowKey) -> Option<Values>,
    unknown: &mut impl FnMut(SourceUnknownCause),
) -> bool {
    match summary {
        ReturnSummary::Argument => values.extend(actual),
        ReturnSummary::ArgumentProperty(property) => {
            append_property_values(actual, Some(property), values, read_values, unknown);
        }
        ReturnSummary::ArgumentOrDefault => {
            values.extend(actual);
            append_property_values(actual, default_property, values, read_values, unknown);
        }
        ReturnSummary::ForwardChoices(_) => return false,
    }
    true
}

fn append_property_values(
    objects: &Values,
    property: Option<PropertySlot>,
    values: &mut ValuesBuilder,
    read_values: &mut impl FnMut(FlowKey) -> Option<Values>,
    unknown: &mut impl FnMut(SourceUnknownCause),
) {
    for origin in objects {
        if let Value::Alloc(Kind::Object | Kind::Set, object) = origin {
            if let Some(property) = property {
                // Unknown is durable even when a later write adds a value.
                if let Some(found) = read_values(FlowKey::Property(object, property)) {
                    values.extend(&found);
                } else {
                    unknown(SourceUnknownCause::MissingProperty);
                    values.push(Value::Top);
                }
            } else {
                if let Some(found) = read_values(FlowKey::Properties(object)) {
                    values.extend(&found);
                }
                unknown(SourceUnknownCause::DynamicProperty);
                values.push(Value::Top);
            }
        } else {
            unknown(SourceUnknownCause::NonObjectReceiver);
            values.push(Value::Top);
        }
    }
}

fn append_numeric_property_values(
    objects: &Values,
    values: &mut ValuesBuilder,
    read_values: &mut impl FnMut(FlowKey) -> Option<Values>,
    unknown: &mut impl FnMut(SourceUnknownCause),
) {
    for origin in objects {
        if let Value::Alloc(Kind::Object | Kind::Set, object) = origin {
            if let Some(found) = read_values(FlowKey::NumericProperties(object)) {
                values.extend(&found);
            }
            unknown(SourceUnknownCause::DynamicProperty);
            values.push(Value::Top);
        } else {
            unknown(SourceUnknownCause::NonObjectReceiver);
            values.push(Value::Top);
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

/// Source-validated factory identities for one importer. Namespace members
/// additionally require the completed whole-source privacy receipt.
#[derive(Debug, Default, PartialEq, Eq)]
struct FreshFactoryImports {
    importer: usize,
    named: BTreeSet<Id>,
    members: BTreeMap<Id, BTreeSet<String>>,
}

#[derive(Default)]
struct FlowSymbols {
    member_read_certificates: Arc<MemberReadCertificates>,
    bindings: Vec<Id>,
    binding_slots: HashMap<Id, BindingSlot>,
    properties: Vec<String>,
    property_slots: HashMap<String, PropertySlot>,
    numeric_properties: Vec<bool>,
    call_arguments: Vec<FlowValueExpr>,
    call_argument_slots: HashMap<(u32, u32), usize>,
    fresh_factory_imports: Arc<FreshFactoryImports>,
}

impl FlowSymbols {
    fn factory_importer(&self, call: &CallExpr, argument: Option<usize>) -> Option<usize> {
        let [input] = call.args.as_slice() else {
            return None;
        };
        if input.spread.is_some() {
            return None;
        }
        // Immediate composition has no intervening use that could mutate or
        // expose the inner fresh graph. Reuse its already lowered receipt;
        // aliases, projections, sequences and named callbacks stay conservative.
        let fresh_input = match input.expr.unwrap_parens() {
            Expr::Fn(_) | Expr::Arrow(_) => true,
            Expr::Call(_) => matches!(
                self.call_arguments.get(argument?),
                Some(FlowValueExpr::Call {
                    factory_importer: Some(_),
                    ..
                })
            ),
            _ => false,
        };
        if !fresh_input {
            return None;
        }
        let Callee::Expr(callee) = &call.callee else {
            return None;
        };
        let imports = &self.fresh_factory_imports;
        let eligible = match callee.unwrap_parens() {
            Expr::Ident(id) => imports.named.contains(&id.to_id()),
            Expr::Member(member) => match (member.obj.unwrap_parens(), &member.prop) {
                (Expr::Ident(root), MemberProp::Ident(property)) => imports
                    .members
                    .get(&root.to_id())
                    .is_some_and(|members| members.contains(property.sym.as_ref())),
                _ => false,
            },
            _ => false,
        };
        eligible.then_some(imports.importer)
    }

    /// Lower each actual first argument once. Nested calls refer to this arena
    /// instead of duplicating their argument trees in every expression user.
    fn call_argument(&mut self, call: &CallExpr) -> Option<usize> {
        if call.args.iter().any(|argument| argument.spread.is_some()) {
            return None;
        }
        let first = call.args.first()?;
        // Nested calls such as relay(relay)(value) share a start offset.
        let key = (call.span.lo.0, call.span.hi.0);
        if let Some(slot) = self.call_argument_slots.get(&key) {
            return Some(*slot);
        }
        let argument = FlowValueExpr::from_ast(&first.expr, self);
        let slot = self.call_arguments.len();
        self.call_arguments.push(argument);
        self.call_argument_slots.insert(key, slot);
        Some(slot)
    }

    fn binding(&mut self, id: Id) -> BindingSlot {
        *self.binding_slots.entry(id).or_insert_with_key(|id| {
            self.bindings.push(id.clone());
            BindingSlot(u32::try_from(self.bindings.len() - 1).expect("binding slot overflow"))
        })
    }

    fn property(&mut self, name: String) -> PropertySlot {
        *self.property_slots.entry(name).or_insert_with_key(|name| {
            self.properties.push(name.clone());
            self.numeric_properties
                .push(may_be_numeric_property_key(name));
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
    NonFunction(Box<FlowValueExpr>),
    Value(Value),
    Member {
        object: Box<FlowValueExpr>,
        property: Option<PropertySlot>,
    },
    NumericMember {
        object: Box<FlowValueExpr>,
    },
    Call {
        callee: Box<FlowValueExpr>,
        argument: Option<usize>,
        /// A declared fresh result cannot originate at this importer's AST sites.
        /// This refines direct identity only, never retained callback reach.
        factory_importer: Option<usize>,
    },
    Union(Box<FlowValueExpr>, Box<FlowValueExpr>),
    Empty,
    Unknown(SourceUnknownCause),
}

impl FlowValueExpr {
    fn reads_values(&self) -> bool {
        let mut pending = vec![self];
        while let Some(expr) = pending.pop() {
            match expr {
                Self::Binding(_)
                | Self::Member { .. }
                | Self::NumericMember { .. }
                | Self::Call { .. } => return true,
                Self::NonFunction(inner) => pending.push(inner),
                Self::Union(left, right) => {
                    pending.push(left);
                    pending.push(right);
                }
                Self::Empty | Self::Unknown(_) | Self::Value(_) => {}
            }
        }
        false
    }

    fn from_ast(expr: &Expr, symbols: &mut FlowSymbols) -> Self {
        match expr.unwrap_parens() {
            Expr::Cond(cond) => Self::from_union_branches(&cond.cons, &cond.alt, symbols),
            Expr::Bin(bin) if bin.op.may_short_circuit() => {
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
                Expr::Bin(bin) if bin.op.may_short_circuit() => {
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
            Expr::Ident(ident) => {
                let binding = ident.to_id();
                let excludes_function_class = symbols
                    .member_read_certificates
                    .excludes_function_class(ident.span, &binding);
                let value = if let Some((receiver, property)) = symbols
                    .member_read_certificates
                    .guarded_projection(ident.span)
                    .cloned()
                {
                    Self::Member {
                        object: Box::new(Self::Binding(symbols.binding(receiver))),
                        property: Some(symbols.property(property)),
                    }
                } else {
                    Self::Binding(symbols.binding(binding))
                };
                if excludes_function_class {
                    Self::NonFunction(Box::new(value))
                } else {
                    value
                }
            }
            Expr::Fn(function) => {
                Self::Value(Value::Alloc(Kind::Function, function.function.span.lo.0))
            }
            Expr::Arrow(arrow) => Self::Value(Value::Alloc(Kind::Function, arrow.span.lo.0)),
            Expr::Class(class) => Self::Value(Value::Alloc(Kind::Class, class.class.span.lo.0)),
            Expr::Object(object) => Self::Value(Value::Alloc(Kind::Object, object.span.lo.0)),
            Expr::Array(array) => Self::Value(Value::Alloc(Kind::Object, array.span.lo.0)),
            Expr::Member(member) => {
                let object = Box::new(Self::from_ast(&member.obj, symbols));
                if let Some(property) = static_prop(&member.prop) {
                    Self::Member {
                        object,
                        property: Some(symbols.property(property)),
                    }
                } else if computed_numeric_property(&member.prop)
                    || symbols.member_read_certificates.contains(member.span)
                {
                    Self::NumericMember { object }
                } else {
                    Self::Member {
                        object,
                        property: None,
                    }
                }
            }
            Expr::Call(call) => match &call.callee {
                Callee::Expr(callee) => {
                    let callee = Box::new(Self::from_ast(callee, symbols));
                    let argument = symbols.call_argument(call);
                    Self::Call {
                        callee,
                        argument,
                        factory_importer: symbols.factory_importer(call, argument),
                    }
                }
                _ => Self::Unknown(SourceUnknownCause::UnsupportedExpression),
            },
            Expr::Seq(seq) => seq.exprs.last().map_or(
                Self::Unknown(SourceUnknownCause::UnsupportedExpression),
                |last| Self::from_ast(last, symbols),
            ),
            Expr::Assign(assign) => Self::from_ast(&assign.right, symbols),
            Expr::Lit(_) | Expr::This(_) | Expr::Tpl(_) => Self::Empty,
            _ => Self::Unknown(SourceUnknownCause::UnsupportedExpression),
        }
    }
}

#[derive(Debug, Clone)]
enum FlowEvent {
    Initialization {
        id: BindingSlot,
        sites: InitializationSites,
    },
    FunctionDeclaration {
        id: BindingSlot,
        origin: Value,
    },
    ClassDeclaration {
        id: BindingSlot,
        origin: Value,
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
        value: Value,
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PropertyProducer {
    DynamicReceiver(PropertySlot),
    FixedReceiver(u32, PropertySlot),
}

impl FlowEvent {
    /// Exact property-fact producers in the source flow relation. Keep this
    /// match exhaustive so a new event variant must be reviewed for writes.
    fn property_producer(&self) -> Option<PropertyProducer> {
        match self {
            Self::AssignProperty { property, .. } => {
                Some(PropertyProducer::DynamicReceiver(*property))
            }
            Self::WritePropertyExpr {
                object, property, ..
            }
            | Self::WritePropertyBinding {
                object, property, ..
            }
            | Self::WritePropertyValue {
                object, property, ..
            } => Some(PropertyProducer::FixedReceiver(*object, *property)),
            Self::Initialization { .. }
            | Self::FunctionDeclaration { .. }
            | Self::ClassDeclaration { .. }
            | Self::Variable { .. }
            | Self::AssignBinding { .. }
            | Self::FunctionMetadata { .. }
            | Self::Return { .. }
            | Self::Invoke { .. } => None,
        }
    }
}

#[derive(Default)]
struct PropertyProducerCensus {
    fixed_receivers: HashSet<(u32, PropertySlot)>,
    dynamic_receiver_properties: Vec<bool>,
}

impl PropertyProducerCensus {
    fn from_events(events: &[FlowEvent]) -> Self {
        let mut census = Self::default();
        for event in events {
            match event.property_producer() {
                Some(PropertyProducer::DynamicReceiver(property)) => {
                    let property_index = property.0 as usize;
                    if census.dynamic_receiver_properties.len() <= property_index {
                        census
                            .dynamic_receiver_properties
                            .resize(property_index + 1, false);
                    }
                    census.dynamic_receiver_properties[property_index] = true;
                }
                Some(PropertyProducer::FixedReceiver(object, property)) => {
                    census.fixed_receivers.insert((object, property));
                }
                None => {}
            }
        }
        census
    }

    fn may_produce(&self, object: u32, property: PropertySlot) -> bool {
        self.dynamic_receiver_properties
            .get(property.0 as usize)
            .copied()
            .unwrap_or(false)
            || self.fixed_receivers.contains(&(object, property))
    }
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
    NumericProperties(u32),
    Return(u32),
    Parameters(u32),
    /// Calls watch readiness so metadata-only input preserves legacy behavior.
    ReadyParameters(u32),
    InvokedCells(u32),
    RetainedCells(u32),
    SourceCells(u32),
    Initialization(BindingSlot),
    ReadOnlyCallback(u32),
}

#[derive(Clone)]
enum FlowFact {
    ValueFlowActive,
    Values(Values),
    CandidateReachability(Arc<RoaringBitmap>),
    Parameters(Vec<Option<BindingSlot>>),
    ReadyParameters(Vec<Option<BindingSlot>>),
    Cells(KnownCellAccesses),
    Initialization(InitializationSites),
    ReadOnlyCallback(ReadOnlyCallback<BindingSlot>),
}

struct FlowProgram {
    module_bindings: BTreeMap<String, BindingSlot>,
    imports: Vec<BindingSlot>,
    declared_ids: BTreeSet<Id>,
    symbols: FlowSymbols,
    events: Vec<FlowEvent>,
    cell_effects: BTreeMap<u32, CallEffectBody>,
    origin_table: Arc<OriginTable>,
    candidate_objects: Vec<Value>,
    canonical: BTreeMap<Value, Id>,
    callable_source_unit: BTreeMap<Value, SourceUnitOrdinal>,
    native_set_allocations: BTreeMap<u32, analysis_vocabulary::inputs::ProvenSetAllocation>,
    fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
    return_summaries: BTreeMap<u32, ReturnSummary>,
}

impl FlowProgram {
    fn structural(
        body: &[TopLevelItemView<'_>],
        inputs: &[analysis_vocabulary::inputs::ResolvedNativeOperationInput],
        inferred: &BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
    ) -> Self {
        Self::structural_with_factory_imports(body, inputs, inferred, Arc::default())
    }

    fn structural_with_factory_imports(
        body: &[TopLevelItemView<'_>],
        inputs: &[analysis_vocabulary::inputs::ResolvedNativeOperationInput],
        inferred: &BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
        fresh_factory_imports: Arc<FreshFactoryImports>,
    ) -> Self {
        let mut candidates = super::abstract_domain::heap::tracked_allocation_roots(body);
        candidates.extend(super::heap_contents::array_candidate_roots(body));
        let allocations = native_set_allocation_roots(inputs, inferred);
        candidates.extend(
            allocations
                .keys()
                .map(|site| Value::Alloc(Kind::Set, *site)),
        );
        Self::collect_with_native_set_allocations(
            body,
            &candidates,
            &allocations,
            fresh_factory_imports,
        )
    }

    fn collect(body: &[TopLevelItemView<'_>], candidate_objects: &BTreeSet<Value>) -> Self {
        Self::collect_with_native_set_allocations(
            body,
            candidate_objects,
            &BTreeMap::new(),
            Arc::default(),
        )
    }

    fn collect_with_native_set_allocations(
        body: &[TopLevelItemView<'_>],
        candidate_objects: &BTreeSet<Value>,
        native_set_allocations: &BTreeMap<u32, analysis_vocabulary::inputs::ProvenSetAllocation>,
        fresh_factory_imports: Arc<FreshFactoryImports>,
    ) -> Self {
        let member_read_certificates = Arc::new(MemberReadCertificates::collect(body));
        let mut collector = FlowProgramCollector {
            declared_ids: BTreeSet::new(),
            symbols: FlowSymbols {
                fresh_factory_imports,
                member_read_certificates,
                ..FlowSymbols::default()
            },
            events: Vec::new(),
            cell_effects: BTreeMap::new(),
            origins: BTreeSet::from([Value::Top]),
            source_unit: SourceUnitOrdinal(0),
            current_function: None,
            collect_effect_calls: true,
            canonical: BTreeMap::new(),
            callable_source_unit: BTreeMap::new(),
            native_set_allocations: native_set_allocations.clone(),
            fresh_returned_record_keys: BTreeMap::new(),
            return_summaries: BTreeMap::new(),
            return_candidates: BTreeMap::new(),
            summary_writes: BTreeSet::new(),
            summary_returns: BTreeMap::new(),
            summary_dynamic: false,
        };
        let mut module_bindings = BTreeMap::new();
        let mut imports = Vec::new();
        let mut initialization = InitializationCensus::default();
        for (ordinal, item) in body.iter().enumerate() {
            let declared = super::item_views::collect_declared_names(item.as_module_item());
            initialization.record(
                item.as_module_item(),
                &declared,
                SourceUnitOrdinal(ordinal),
                &mut collector.symbols,
            );
            for id in declared {
                module_bindings.insert(id.0.to_string(), collector.symbols.binding(id));
            }
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item.as_module_item() {
                for specifier in &import.specifiers {
                    let local = specifier.local().to_id();
                    let slot = collector.symbols.binding(local.clone());
                    module_bindings.insert(local.0.to_string(), slot);
                    imports.push(slot);
                }
            }
            collector.source_unit = SourceUnitOrdinal(ordinal);
            item.as_module_item().visit_with(&mut collector);
        }
        // A relation is admitted only after the same lowering walk has seen
        // captured writes and every return. This census changes no cell effect.
        if !collector.summary_dynamic {
            for (function, candidate) in &collector.return_candidates {
                if !collector.summary_writes.contains(&candidate.parameter)
                    && collector
                        .summary_returns
                        .get(function)
                        .is_some_and(|returns| returns.as_slice() == [candidate.return_start])
                {
                    collector
                        .return_summaries
                        .insert(*function, candidate.summary);
                }
            }
        }
        collector.events.extend(initialization.finish());
        collector.origins.extend(candidate_objects.iter().copied());
        collector.origins.extend(
            native_set_allocations
                .keys()
                .copied()
                .map(|site| Value::Alloc(Kind::Set, site)),
        );
        Self {
            module_bindings,
            imports,
            declared_ids: collector.declared_ids,
            symbols: collector.symbols,
            events: collector.events,
            cell_effects: collector.cell_effects,
            origin_table: Arc::new(OriginTable::from_origins(collector.origins)),
            candidate_objects: candidate_objects.iter().copied().collect(),
            canonical: collector.canonical,
            callable_source_unit: collector.callable_source_unit,
            native_set_allocations: native_set_allocations.clone(),
            fresh_returned_record_keys: collector.fresh_returned_record_keys,
            return_summaries: collector.return_summaries,
        }
    }

    fn solve(self) -> (ChunkValueFlow, FlowWorkStats) {
        let Self {
            module_bindings: _,
            imports: _,
            declared_ids,
            mut symbols,
            events,
            cell_effects,
            origin_table,
            candidate_objects,
            canonical,
            callable_source_unit,
            native_set_allocations,
            fresh_returned_record_keys,
            return_summaries,
        } = self;
        let member_read_certificates = symbols.member_read_certificates.clone();
        let return_summaries = Arc::new(return_summaries);
        let numeric_properties = std::mem::take(&mut symbols.numeric_properties);
        let declared = symbols.declared(&declared_ids);
        let (facts, stats) = solve_flow(
            FlowConstraints {
                events,
                returns: ReturnInputs {
                    arguments: std::mem::take(&mut symbols.call_arguments),
                    summaries: return_summaries.clone(),
                    default_property: symbols.property_slots.get("default").copied(),
                    importer_allocations: Arc::default(),
                },
                declared,
                numeric_properties,
            },
            origin_table.clone(),
            &candidate_objects,
            native_set_allocations.keys().copied().collect(),
            cell_effects
                .into_iter()
                .filter(|(_, body)| body.read_only_returns.is_some())
                .collect(),
        );

        let mut flow = ChunkValueFlow {
            property_names_by_slot: Arc::new(symbols.properties.clone()),
            binding_ids_by_slot: Arc::new(symbols.bindings.clone()),
            candidate_objects,
            origin_table,
            member_read_certificates,
            declared_ids,
            canonical,
            callable_source_unit,
            native_set_allocations,
            fresh_returned_record_keys,
            return_summaries,
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
                (FlowKey::ReadOnlyCallback(function), FlowFact::ReadOnlyCallback(summary)) => {
                    flow.read_only_callbacks
                        .insert(function, summary.map(|slot| symbols.id(*slot).clone()));
                }
                (_, FlowFact::Cells(_)) => {}
                (FlowKey::Initialization(slot), FlowFact::Initialization(sites)) => {
                    flow.initializations.insert(symbols.id(slot).clone(), sites);
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
                (FlowKey::NumericProperties(object), FlowFact::Values(values)) => {
                    flow.numeric_property_unions.insert(object, values);
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
        (flow, stats)
    }
}

/// The shared value and completed-candidate strata, independent of the source
/// coordinate system used to prepare their constraints.
fn solve_flow(
    constraints: FlowConstraints,
    origin_table: Arc<OriginTable>,
    candidate_objects: &[Value],
    native_set_allocations: BTreeSet<u32>,
    cell_effects: BTreeMap<u32, CallEffectBody>,
) -> (BTreeMap<FlowKey, FlowFact>, FlowWorkStats) {
    solve_flow_observed(
        constraints,
        origin_table,
        candidate_objects,
        native_set_allocations,
        cell_effects,
        &crate::diagnostics::Diagnostics::default(),
    )
}

struct FlowConstraints {
    events: Vec<FlowEvent>,
    returns: ReturnInputs,
    declared: Vec<bool>,
    numeric_properties: Vec<bool>,
}

fn solve_flow_observed(
    constraints: FlowConstraints,
    origin_table: Arc<OriginTable>,
    candidate_objects: &[Value],
    native_set_allocations: BTreeSet<u32>,
    cell_effects: BTreeMap<u32, CallEffectBody>,
    diagnostics: &crate::diagnostics::Diagnostics,
) -> (BTreeMap<FlowKey, FlowFact>, FlowWorkStats) {
    let FlowConstraints {
        events,
        returns,
        declared,
        numeric_properties,
    } = constraints;
    let returns = Arc::new(returns);
    let producer_count = events.len();
    let property_producers = PropertyProducerCensus::from_events(&events);
    #[cfg(test)]
    let work = Rc::new(RefCell::new(FlowWorkStats::default()));
    let events = Arc::new(events);
    let rules = FlowRules {
        metrics: diagnostics.metrics().clone(),
        invocations: RefCell::new(
            events
                .iter()
                .map(|event| match event {
                    FlowEvent::Invoke { args, .. } => Some(Box::new(Invocation::new(args.len()))),
                    _ => None,
                })
                .collect(),
        ),
        property_writes: RefCell::new(
            events
                .iter()
                .map(|event| match event {
                    FlowEvent::AssignProperty {
                        right: FlowValueExpr::Empty,
                        ..
                    } => None,
                    FlowEvent::AssignProperty { .. } => Some(Box::new(PropertyWrite {
                        receiver: Some(Box::new(PropertyReceivers::default())),
                        ..PropertyWrite::default()
                    })),
                    FlowEvent::WritePropertyExpr { value, .. } if value.reads_values() => {
                        Some(Box::new(PropertyWrite::default()))
                    }
                    FlowEvent::WritePropertyBinding { .. } => {
                        Some(Box::new(PropertyWrite::default()))
                    }
                    _ => None,
                })
                .collect(),
        ),
        events: events.clone(),
        returns: returns.clone(),
        property_producers,
        numeric_properties,
        declared: declared.clone(),
        origin_table: origin_table.clone(),
        #[cfg(test)]
        work: work.clone(),
    };
    use crate::diagnostics::progress::{Phase, State};
    diagnostics.progress("*", Phase::SolveProgramValueFlow, State::Start);
    let values = IterativeInference::new(rules)
        .observed(diagnostics.metrics())
        .solve_values_resumable(0..producer_count);
    diagnostics.progress("*", Phase::SolveProgramValueFlow, State::End);
    diagnostics.progress("*", Phase::CloseProgramValueFlow, State::Start);
    #[cfg(test)]
    {
        work.borrow_mut().origin_deliveries = values.delta_deliveries();
    }
    let call_effects = if cell_effects.is_empty() {
        BTreeMap::new()
    } else {
        CallEffectRules::new(
            cell_effects,
            &events,
            &values,
            &declared,
            &origin_table,
            &returns,
        )
        .solve()
    };
    drop(events);
    let mut facts = if candidate_objects.is_empty() {
        values.into_values()
    } else {
        let candidate_facts = CandidateReachabilityClosure::new(
            origin_table,
            native_set_allocations,
            candidate_objects,
        )
        .solve(&values)
        .into_iter()
        .map(|(key, fact)| {
            // Candidate proof dependencies stay typed through the completed
            // closure, then share the same value-only projection boundary as
            // the previous `solve_values` stratum.
            let candidate_reachability::CandidateReachabilityFact { bits, dependencies } = fact;
            // The final `ValueFlow` projection keeps the same value-only
            // boundary as the previous `solve_values` closure.
            drop(dependencies);
            (key, FlowFact::CandidateReachability(bits))
        })
        .collect::<BTreeMap<_, _>>();
        let mut completed = values.into_values();
        completed.extend(candidate_facts);
        completed
    };
    facts.extend(call_effects);
    #[cfg(test)]
    let stats = *work.borrow();
    #[cfg(not(test))]
    let stats = ();
    diagnostics.progress("*", Phase::CloseProgramValueFlow, State::End);
    (facts, stats)
}

struct FlowProgramCollector {
    declared_ids: BTreeSet<Id>,
    symbols: FlowSymbols,
    events: Vec<FlowEvent>,
    cell_effects: BTreeMap<u32, CallEffectBody>,
    origins: BTreeSet<Value>,
    source_unit: SourceUnitOrdinal,
    current_function: Option<u32>,
    collect_effect_calls: bool,
    canonical: BTreeMap<Value, Id>,
    callable_source_unit: BTreeMap<Value, SourceUnitOrdinal>,
    native_set_allocations: BTreeMap<u32, analysis_vocabulary::inputs::ProvenSetAllocation>,
    fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
    return_summaries: BTreeMap<u32, ReturnSummary>,
    return_candidates: BTreeMap<u32, return_summary::Candidate>,
    summary_writes: BTreeSet<BindingSlot>,
    summary_returns: BTreeMap<u32, Vec<u32>>,
    summary_dynamic: bool,
}

impl FlowProgramCollector {
    fn enter_function(&mut self, function: u32) -> Option<u32> {
        if let Some(parent) = self.current_function {
            self.cell_effects
                .entry(parent)
                .or_default()
                .descendants
                .insert(function);
        }
        self.cell_effects.entry(function).or_default();
        self.current_function.replace(function)
    }

    fn record_invoke(&mut self, callee: &Expr, args: &[ExprOrSpread]) {
        if self.collect_effect_calls
            && let Some(function) = self.current_function
        {
            self.cell_effects
                .entry(function)
                .or_default()
                .calls
                .push(self.events.len());
        }
        self.events.push(FlowEvent::Invoke {
            callee: FlowValueExpr::from_ast(callee, &mut self.symbols),
            args: args
                .iter()
                .map(|arg| FlowValueExpr::from_ast(&arg.expr, &mut self.symbols))
                .collect(),
        });
    }

    /// The access projection retains these source references, but does not
    /// infer execution through an unmodeled lazy/class evaluation boundary.
    fn without_effect_calls(&mut self, visit: impl FnOnce(&mut Self)) {
        let previous = std::mem::replace(&mut self.collect_effect_calls, false);
        visit(self);
        self.collect_effect_calls = previous;
    }
}

impl binding_targets::TargetAccessRecorder for FlowProgramCollector {
    fn record_binding_write(&mut self, id: &Id) {
        self.summary_writes.insert(self.symbols.binding(id.clone()));
        if let Some(function) = self.current_function {
            let slot = self.symbols.binding(id.clone());
            self.cell_effects
                .entry(function)
                .or_default()
                .writes
                .insert(slot);
        }
    }
}

impl Visit for FlowProgramCollector {
    fn visit_ident(&mut self, node: &Ident) {
        if let Some(function) = self.current_function {
            let slot = self.symbols.binding(node.to_id());
            self.cell_effects
                .entry(function)
                .or_default()
                .reads
                .insert(slot);
        }
    }

    fn visit_binding_ident(&mut self, node: &BindingIdent) {
        self.declared_ids.insert(node.id.to_id());
    }

    fn visit_fn_decl(&mut self, node: &FnDecl) {
        let id = node.ident.to_id();
        self.summary_writes.insert(self.symbols.binding(id.clone()));
        let origin = Value::Alloc(Kind::Function, node.function.span.lo.0);
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
        let origin = Value::Alloc(Kind::Class, node.class.span.lo.0);
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
        if node.init.is_some() {
            for id in binding_targets::binding_names(&node.name) {
                self.summary_writes.insert(self.symbols.binding(id));
            }
        }
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
                        FlowValueExpr::Value(Value::Alloc(Kind::Set, construction.span.lo.0))
                    }
                    other => FlowValueExpr::from_ast(other, &mut self.symbols),
                },
            });
        }
        node.visit_children_with(self);
    }

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        binding_targets::record_assign_target(&node.left, self);
        match binding_targets::simple_write_target(&node.left) {
            Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                self.events.push(FlowEvent::AssignBinding {
                    id: self.symbols.binding(binding),
                    right: FlowValueExpr::from_ast(&node.right, &mut self.symbols),
                });
            }
            Some(binding_targets::SimpleWriteTarget::Member(member)) => {
                if let Some(property) = static_prop(&member.prop) {
                    self.events.push(FlowEvent::AssignProperty {
                        object: FlowValueExpr::from_ast(&member.obj, &mut self.symbols),
                        property: self.symbols.property(property),
                        right: FlowValueExpr::from_ast(&node.right, &mut self.symbols),
                    });
                }
            }
            None => {}
        }
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        binding_targets::record_update_target(&node.arg, self);
        node.visit_children_with(self);
    }

    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        if let ForHead::VarDecl(declaration) = &node.left {
            for declaration in &declaration.decls {
                for id in binding_targets::binding_names(&declaration.name) {
                    self.summary_writes.insert(self.symbols.binding(id));
                }
            }
        }
        if let ForHead::Pat(pattern) = &node.left {
            binding_targets::record_pat_write(pattern, self);
        }
        node.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        if let ForHead::VarDecl(declaration) = &node.left {
            for declaration in &declaration.decls {
                for id in binding_targets::binding_names(&declaration.name) {
                    self.summary_writes.insert(self.symbols.binding(id));
                }
            }
        }
        if let ForHead::Pat(pattern) = &node.left {
            binding_targets::record_pat_write(pattern, self);
        }
        node.visit_children_with(self);
    }

    fn visit_object_lit(&mut self, node: &ObjectLit) {
        let object = node.span.lo.0;
        self.origins.insert(Value::Alloc(Kind::Object, object));
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
                                value: Value::Alloc(Kind::Function, method.function.span.lo.0),
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
        self.origins.insert(Value::Alloc(Kind::Object, object));
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
        if let Some(candidate) = return_summary::candidate(node, &mut self.symbols) {
            self.return_candidates.insert(function, candidate);
        }
        if let Some(summary) = return_summary::function(node, &mut self.symbols) {
            self.return_summaries.insert(function, summary);
        }
        if !node.is_async
            && !node.is_generator
            && let Some(body) = &node.body
            && let Some(keys) = fresh_returned_record_keys(body)
        {
            self.fresh_returned_record_keys.insert(function, keys);
        }
        self.origins.insert(Value::Alloc(Kind::Function, function));
        self.callable_source_unit
            .entry(Value::Alloc(Kind::Function, function))
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
        let previous = self.enter_function(function);
        if !node.is_async && !node.is_generator && node.params.is_empty() {
            self.cell_effects
                .get_mut(&function)
                .unwrap()
                .read_only_returns = node
                .body
                .as_ref()
                .and_then(|body| read_only_callbacks::block(&body.stmts, &mut self.symbols));
        }
        let previous_calls = std::mem::replace(
            &mut self.collect_effect_calls,
            !node.is_async && !node.is_generator,
        );
        node.visit_children_with(self);
        self.collect_effect_calls = previous_calls;
        self.current_function = previous;
    }

    fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
        let function = node.span.lo.0;
        if let Some(summary) = return_summary::arrow(node, &mut self.symbols) {
            self.return_summaries.insert(function, summary);
        }
        if !node.is_async
            && let ArrowFunctionBody::FunctionBody(body) = node.body.as_ref()
            && let Some(keys) = fresh_returned_record_keys(body)
        {
            self.fresh_returned_record_keys.insert(function, keys);
        }
        self.origins.insert(Value::Alloc(Kind::Function, function));
        self.callable_source_unit
            .entry(Value::Alloc(Kind::Function, function))
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
        let previous = self.enter_function(function);
        if !node.is_async && node.params.is_empty() {
            self.cell_effects
                .get_mut(&function)
                .unwrap()
                .read_only_returns = match node.body.as_ref() {
                ArrowFunctionBody::Expr(expr) => {
                    read_only_callbacks::expression(expr, &mut self.symbols)
                }
                ArrowFunctionBody::FunctionBody(body) => {
                    read_only_callbacks::block(&body.stmts, &mut self.symbols)
                }
            };
        }
        let previous_calls = std::mem::replace(&mut self.collect_effect_calls, !node.is_async);
        if let ArrowFunctionBody::Expr(expr) = node.body.as_ref() {
            self.events.push(FlowEvent::Return {
                function,
                value: FlowValueExpr::from_ast(expr, &mut self.symbols),
            });
        }
        node.visit_children_with(self);
        self.collect_effect_calls = previous_calls;
        self.current_function = previous;
    }

    fn visit_class(&mut self, node: &Class) {
        self.origins
            .insert(Value::Alloc(Kind::Class, node.span.lo.0));
        self.without_effect_calls(|this| node.visit_children_with(this));
    }

    fn visit_getter_prop(&mut self, node: &GetterProp) {
        self.without_effect_calls(|this| node.visit_children_with(this));
    }

    fn visit_setter_prop(&mut self, node: &SetterProp) {
        self.without_effect_calls(|this| node.visit_children_with(this));
    }

    fn visit_return_stmt(&mut self, node: &ReturnStmt) {
        if let Some(function) = self.current_function {
            self.summary_returns
                .entry(function)
                .or_default()
                .push(node.span.lo.0);
        }
        if let (Some(function), Some(value)) = (self.current_function, node.arg.as_deref()) {
            self.events.push(FlowEvent::Return {
                function,
                value: FlowValueExpr::from_ast(value, &mut self.symbols),
            });
        }
        node.visit_children_with(self);
    }

    fn visit_with_stmt(&mut self, node: &WithStmt) {
        self.summary_dynamic = true;
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if matches!(&node.callee, Callee::Expr(callee)
            if matches!(callee.unwrap_parens(), Expr::Ident(id) if id.sym == *"eval"))
        {
            self.summary_dynamic = true;
        }
        if let Callee::Expr(callee) = &node.callee {
            self.record_invoke(callee, &node.args);
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        self.record_invoke(&node.callee, node.args.as_deref().unwrap_or_default());
        node.visit_children_with(self);
    }
}

fn direct_callable_origin(expr: &Expr) -> Option<Value> {
    match expr.unwrap_parens() {
        Expr::Fn(function) => Some(Value::Alloc(Kind::Function, function.function.span.lo.0)),
        Expr::Arrow(arrow) => Some(Value::Alloc(Kind::Function, arrow.span.lo.0)),
        Expr::Class(class) => Some(Value::Alloc(Kind::Class, class.class.span.lo.0)),
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
    pub(crate) expression_dependency_read_occurrences: usize,
    pub(crate) expression_dependency_unique_keys: usize,
    pub(crate) callee_evaluations: usize,
    pub(crate) target_discoveries: usize,
    pub(crate) activated_edges: usize,
    pub(crate) edge_visits: usize,
    pub(crate) origin_deliveries: usize,
    /// Size of exact sets submitted to subtraction, including recurring origins.
    pub(crate) difference_inputs: usize,
    pub(crate) property_rhs_evaluations: usize,
    pub(crate) property_receiver_evaluations: usize,
    pub(crate) property_comparison_inputs: usize,
    pub(crate) property_subtractions: usize,
    pub(crate) property_subset_checks: usize,
    pub(crate) property_direct_evaluations: usize,
    pub(crate) property_receiver_scans: usize,
    pub(crate) property_receiver_activations: usize,
    pub(crate) property_receiver_visits: usize,
    pub(crate) property_proposals: usize,
    pub(crate) numeric_property_proposals: usize,
    pub(crate) numeric_property_reads: usize,
    pub(crate) property_join_inputs: usize,
    pub(crate) property_retained_receivers: usize,
    pub(crate) property_retained_origins: usize,
    pub(crate) property_retained_reads: usize,
    pub(crate) property_dynamic_records: usize,
    pub(crate) property_fixed_records: usize,
    pub(crate) property_state_capacity: usize,
    pub(crate) property_impossible_reads_skipped: usize,
    pub(crate) property_possible_reads_tracked: usize,
    pub(crate) bitmap_join_memo_hits: usize,
    pub(crate) bitmap_join_memo_misses: usize,
    pub(crate) bitmap_subset_checks: usize,
    pub(crate) bitmap_copy_on_write_unions: usize,
}

#[cfg(not(test))]
type FlowWorkStats = ();

#[derive(Clone)]
struct CachedExpression {
    values: Values,
    dependencies: BTreeSet<FlowKey>,
}

/// Event-local target/argument coordinates preserve the old transfer order.
/// These are bounded source identities, never execution or call-path identities.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
struct ArgumentEdge {
    function: u32,
    argument: usize,
}

struct FormalEdge {
    parameter: BindingSlot,
    delivered: Values,
}

struct Invocation {
    callee: Option<CachedExpression>,
    callee_dirty: bool,
    arguments: Vec<Option<CachedExpression>>,
    dirty: BTreeSet<usize>,
    /// Exact currently consumed reads, including absence. Solver watchers also
    /// retain historical reads, which may wake the event without dirtying an arg.
    readers: BTreeMap<FlowKey, BTreeSet<usize>>,
    targets: BTreeSet<u32>,
    waiting: BTreeSet<u32>,
    ready: BTreeSet<u32>,
    edges: BTreeMap<ArgumentEdge, FormalEdge>,
    consumers: Vec<BTreeSet<ArgumentEdge>>,
}

impl Invocation {
    fn new(arguments: usize) -> Self {
        Self {
            callee: None,
            callee_dirty: true,
            arguments: vec![None; arguments],
            dirty: BTreeSet::new(),
            readers: BTreeMap::new(),
            targets: BTreeSet::new(),
            waiting: BTreeSet::new(),
            ready: BTreeSet::new(),
            edges: BTreeMap::new(),
            consumers: vec![BTreeSet::new(); arguments],
        }
    }

    fn changed(&mut self, changed: &BTreeSet<FlowKey>) {
        if self
            .callee
            .as_ref()
            .is_some_and(|callee| !callee.dependencies.is_disjoint(changed))
        {
            self.callee_dirty = true;
        }
        for key in changed {
            if let Some(readers) = self.readers.get(key) {
                self.dirty.extend(readers);
            }
            if let FlowKey::ReadyParameters(function) = key
                && self.waiting.remove(function)
            {
                self.ready.insert(*function);
            }
        }
    }
}

/// One source write activates new receivers without replaying old receivers.
/// RHS growth fans out the shared full result to preserve bitmap Arc adoption.
/// Fixed receivers need only an RHS slot; immutable fields need no state.
#[derive(Default)]
struct PropertyWrite {
    receiver: Option<Box<PropertyReceivers>>,
    right: Option<CachedExpression>,
    right_dirty: bool,
}

#[derive(Default)]
struct PropertyReceivers {
    expression: Option<CachedExpression>,
    dirty: bool,
    receivers: BTreeSet<u32>,
}

enum PropertyTarget<'a> {
    Fixed(u32),
    Expression(&'a FlowValueExpr),
}

impl PropertyWrite {
    fn changed(&mut self, changed: &BTreeSet<FlowKey>) {
        if let Some(receiver) = self.receiver.as_mut()
            && receiver
                .expression
                .as_ref()
                .is_some_and(|expr| !expr.dependencies.is_disjoint(changed))
        {
            receiver.dirty = true;
        }
        if self
            .right
            .as_ref()
            .is_some_and(|expr| !expr.dependencies.is_disjoint(changed))
        {
            self.right_dirty = true;
        }
    }
}

struct FlowRules {
    metrics: work_metrics::WorkMetrics,
    /// Exact expression results and activated source edges, retired together at
    /// completion. No result is published before its first ordered use.
    invocations: RefCell<Vec<Option<Box<Invocation>>>>,
    property_writes: RefCell<Vec<Option<Box<PropertyWrite>>>>,
    events: Arc<Vec<FlowEvent>>,
    returns: Arc<ReturnInputs>,
    property_producers: PropertyProducerCensus,
    numeric_properties: Vec<bool>,
    declared: Vec<bool>,
    origin_table: Arc<OriginTable>,
    #[cfg(test)]
    work: Rc<RefCell<FlowWorkStats>>,
}

#[cfg(test)]
impl Drop for FlowRules {
    fn drop(&mut self) {
        let mut work = self.work.borrow_mut();
        work.property_state_capacity = self.property_writes.get_mut().capacity();
        for write in self.property_writes.get_mut().iter().flatten() {
            if let Some(receiver) = &write.receiver {
                work.property_dynamic_records += 1;
                work.property_retained_receivers += receiver.receivers.len();
                if let Some(expr) = &receiver.expression {
                    work.property_retained_origins += expr.values.len();
                    work.property_retained_reads += expr.dependencies.len();
                }
            } else {
                work.property_fixed_records += 1;
            }
            if let Some(expr) = &write.right {
                work.property_retained_origins += expr.values.len();
                work.property_retained_reads += expr.dependencies.len();
            }
        }
    }
}

impl FlowRules {
    fn invoke(
        &self,
        index: usize,
        callee: &FlowValueExpr,
        args: &[FlowValueExpr],
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        let mut invocations = self.invocations.borrow_mut();
        let invocation = invocations[index].as_mut().expect("invocation event state");
        // Snapshot all new targets before any formal write. A write can dirty
        // the callee, but must not activate a target until the next event run.
        if invocation.callee_dirty {
            let cached = self.cached_expression(callee, context);
            #[cfg(test)]
            {
                self.work.borrow_mut().callee_evaluations += 1;
            }
            let added = invocation.callee.as_ref().map_or_else(
                || cached.values.clone(),
                |previous| cached.values.difference(&previous.values),
            );
            for value in &added {
                if let Value::Alloc(Kind::Function, function) = value
                    && invocation.targets.insert(function)
                {
                    invocation.ready.insert(function);
                    #[cfg(test)]
                    {
                        self.work.borrow_mut().target_discoveries += 1;
                    }
                }
            }
            invocation.callee = Some(cached);
            invocation.callee_dirty = false;
        }
        let mut pending = BTreeSet::new();
        for argument in &invocation.dirty {
            pending.extend(&invocation.consumers[*argument]);
        }
        for function in std::mem::take(&mut invocation.ready) {
            let params = match context.read(FlowKey::ReadyParameters(function)).cloned() {
                Some(FlowFact::ReadyParameters(params)) => params,
                Some(_) => unreachable!("ready parameter key contains metadata"),
                None => {
                    invocation.waiting.insert(function);
                    continue;
                }
            };
            for (argument, param) in params.into_iter().take(args.len()).enumerate() {
                let Some(parameter) = param else {
                    continue;
                };
                let edge = ArgumentEdge { function, argument };
                invocation.edges.insert(
                    edge,
                    FormalEdge {
                        parameter,
                        delivered: Values::empty(self.origin_table.clone()),
                    },
                );
                invocation.consumers[argument].insert(edge);
                pending.insert(edge);
                #[cfg(test)]
                {
                    self.work.borrow_mut().activated_edges += 1;
                }
            }
        }
        while let Some(edge) = pending.pop_first() {
            let argument = edge.argument;
            if invocation.arguments[argument].is_none() || invocation.dirty.remove(&argument) {
                let cached = self.cached_expression(&args[argument], context);
                #[cfg(test)]
                {
                    self.work.borrow_mut().argument_evaluations += 1;
                }
                if let Some(previous) = &invocation.arguments[argument] {
                    for dependency in previous.dependencies.difference(&cached.dependencies) {
                        let readers = invocation
                            .readers
                            .get_mut(dependency)
                            .expect("indexed argument read");
                        readers.remove(&argument);
                        if readers.is_empty() {
                            invocation.readers.remove(dependency);
                        }
                    }
                }
                for dependency in &cached.dependencies {
                    invocation
                        .readers
                        .entry(*dependency)
                        .or_default()
                        .insert(argument);
                }
                invocation.arguments[argument] = Some(cached);
            }
            let values = &invocation.arguments[argument]
                .as_ref()
                .expect("ordered argument evaluated")
                .values;
            let formal = invocation.edges.get_mut(&edge).expect("activated edge");
            #[cfg(test)]
            {
                let mut work = self.work.borrow_mut();
                work.edge_visits += 1;
                work.difference_inputs += values.len();
            }
            let parameter = formal.parameter;
            // Keep only the compact Values frontier in the edge index; the
            // generic family fact wrapper is needed just during delivery.
            let mut delivered = FlowFact::Values(formal.delivered.clone());
            let change = context.propose_delta(
                FlowKey::Binding(parameter),
                FlowFact::Values(values.clone()),
                &mut delivered,
            );
            let FlowFact::Values(delivered) = delivered else {
                unreachable!("value delta frontier")
            };
            formal.delivered = delivered;
            if !values.is_empty() {
                context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
            }
            if change.value {
                let key = FlowKey::Binding(parameter);
                if invocation
                    .callee
                    .as_ref()
                    .is_some_and(|callee| callee.dependencies.contains(&key))
                {
                    invocation.callee_dirty = true;
                }
                if let Some(readers) = invocation.readers.get(&key) {
                    for argument in readers {
                        invocation.dirty.insert(*argument);
                        // Already-passed edges retain the next-round wakeup;
                        // only later targets/arguments observe this write now.
                        pending.extend(
                            invocation.consumers[*argument].range((
                                std::ops::Bound::Excluded(edge),
                                std::ops::Bound::Unbounded,
                            )),
                        );
                    }
                }
            }
        }
    }

    fn values_of_expr_tracking(
        &self,
        expr: &FlowValueExpr,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
        dependencies: Option<&mut FxHashSet<FlowKey>>,
    ) -> Values {
        let mut unknowns = self
            .metrics
            .enabled()
            .then(work_metrics::SourceUnknownCounts::default);
        let mut dependencies = dependencies;
        let mut values = ValuesBuilder::new(self.origin_table.clone());
        append_value_expr(
            expr,
            &mut values,
            &self.declared,
            &self.returns,
            &mut |key| {
                if let FlowKey::Property(object, property) = key {
                    if !self.property_producers.may_produce(object, property) {
                        #[cfg(test)]
                        {
                            self.work.borrow_mut().property_impossible_reads_skipped += 1;
                        }
                        // Keep the existing Top result for missing properties,
                        // but avoid a watcher and cached dependency edge that no
                        // event in this completed source census can ever change.
                        return None;
                    }
                    #[cfg(test)]
                    {
                        self.work.borrow_mut().property_possible_reads_tracked += 1;
                    }
                }
                #[cfg(test)]
                if matches!(key, FlowKey::NumericProperties(_)) {
                    self.work.borrow_mut().numeric_property_reads += 1;
                }
                if let Some(dependencies) = dependencies.as_deref_mut() {
                    let inserted = dependencies.insert(key);
                    #[cfg(test)]
                    {
                        let mut work = self.work.borrow_mut();
                        work.expression_dependency_read_occurrences += 1;
                        if inserted {
                            work.expression_dependency_unique_keys += 1;
                        }
                    }
                    #[cfg(not(test))]
                    let _ = inserted;
                }
                match context.read(key) {
                    Some(FlowFact::Values(values)) => Some(values.clone()),
                    Some(_) => {
                        unreachable!("value expressions cannot read parameter metadata")
                    }
                    None => None,
                }
            },
            &mut |cause| {
                if let Some(unknowns) = &mut unknowns {
                    unknowns.record(cause);
                }
            },
        );
        if let Some(unknowns) = &unknowns {
            self.metrics.source_unknowns(unknowns);
        }
        values.finish()
    }

    fn cached_expression(
        &self,
        expr: &FlowValueExpr,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) -> CachedExpression {
        let mut dependencies = FxHashSet::default();
        let values = self.values_of_expr_tracking(expr, context, Some(&mut dependencies));
        CachedExpression {
            values,
            dependencies: dependencies.into_iter().collect(),
        }
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
        changed.value
    }

    fn write_property(
        &self,
        index: usize,
        object: PropertyTarget<'_>,
        property: PropertySlot,
        right: &FlowValueExpr,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        // Primitive-only RHS values carry no may-origin and can never grow.
        if matches!(right, FlowValueExpr::Empty) {
            return;
        }
        let mut writes = self.property_writes.borrow_mut();
        let Some(write) = writes[index].as_mut() else {
            let PropertyTarget::Fixed(object) = object else {
                unreachable!("dynamic write has state")
            };
            let values = self.values_of_expr_tracking(right, context, None);
            #[cfg(test)]
            {
                self.work.borrow_mut().property_direct_evaluations += 1;
            }
            if !values.is_empty() {
                self.propose_property(context, object, property, &values);
                context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_join_inputs +=
                        self.property_fact_count(property) * values.len();
                }
            }
            return;
        };
        // Preserve RHS-before-receiver evaluation and evaluate both before any
        // write. Self-dependent expressions therefore refresh on the next run.
        let right_grew = if write.right.is_none() || std::mem::take(&mut write.right_dirty) {
            let cached = self.cached_expression(right, context);
            #[cfg(test)]
            {
                self.work.borrow_mut().property_rhs_evaluations += 1;
            }
            let grew = write.right.as_ref().map_or_else(
                || !cached.values.is_empty(),
                |previous| {
                    let unknown_added = cached.values.unknown && !previous.values.unknown;
                    if unknown_added || Arc::ptr_eq(&cached.values.bitmap, &previous.values.bitmap)
                    {
                        return unknown_added;
                    }
                    #[cfg(test)]
                    {
                        let mut work = self.work.borrow_mut();
                        work.property_subset_checks += 1;
                        work.property_comparison_inputs +=
                            cached.values.len() + previous.values.len();
                    }
                    !cached.values.bitmap.is_subset(&previous.values.bitmap)
                },
            );
            write.right = Some(cached);
            grew
        } else {
            false
        };
        let current = &write.right.as_ref().expect("ordered RHS evaluated").values;
        #[cfg(test)]
        let current_count = current.len();
        let PropertyTarget::Expression(object) = object else {
            let PropertyTarget::Fixed(object) = object else {
                unreachable!()
            };
            if right_grew {
                self.propose_property(context, object, property, current);
                context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_join_inputs +=
                        self.property_fact_count(property) * current_count;
                }
            }
            return;
        };
        let receivers = write.receiver.as_mut().expect("dynamic receiver state");
        let mut new_receivers = Vec::new();
        if receivers.expression.is_none() || std::mem::take(&mut receivers.dirty) {
            let cached = self.cached_expression(object, context);
            #[cfg(test)]
            {
                let mut work = self.work.borrow_mut();
                work.property_receiver_evaluations += 1;
                if let Some(previous) = &receivers.expression {
                    work.property_subtractions += 1;
                    work.property_comparison_inputs += cached.values.len() + previous.values.len();
                }
            }
            let added = receivers.expression.as_ref().map_or_else(
                || cached.values.clone(),
                |previous| cached.values.difference(&previous.values),
            );
            for origin in &added {
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_receiver_scans += 1;
                }
                if let Value::Alloc(Kind::Object, receiver) | Value::Alloc(Kind::Set, receiver) =
                    origin
                    && !receivers.receivers.contains(&receiver)
                {
                    new_receivers.push(receiver);
                }
            }
            receivers.expression = Some(cached);
        }
        // Full-current proposals let every subset destination adopt the same
        // immutable bitmap. Delta joins would COW-clone the old shared bitmap
        // separately for every receiver, retaining receiver-by-origin payloads.
        if right_grew {
            for receiver in &receivers.receivers {
                self.propose_property(context, *receiver, property, current);
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_join_inputs +=
                        self.property_fact_count(property) * current_count;
                }
            }
        }
        for receiver in new_receivers {
            receivers.receivers.insert(receiver);
            #[cfg(test)]
            {
                self.work.borrow_mut().property_receiver_activations += 1;
            }
            if !current.is_empty() {
                self.propose_property(context, receiver, property, current);
                #[cfg(test)]
                {
                    self.work.borrow_mut().property_join_inputs +=
                        self.property_fact_count(property) * current_count;
                }
            }
        }
        // Watchers are cumulative and SOURCE_ONLY carries no proof premises:
        // cached reads need neither replay nor per-receiver proof frontiers.
        if !receivers.receivers.is_empty() && !current.is_empty() {
            context.propose(FlowKey::ValueFlowActive, FlowFact::ValueFlowActive);
        }
    }

    fn propose_property(
        &self,
        context: &mut impl FactContext<FlowKey, FlowFact>,
        object: u32,
        property: PropertySlot,
        values: &Values,
    ) {
        #[cfg(test)]
        {
            let mut work = self.work.borrow_mut();
            work.property_receiver_visits += 1;
            work.property_proposals += self.property_fact_count(property);
            work.numeric_property_proposals +=
                usize::from(self.numeric_properties[property.0 as usize]);
        }
        context.propose(
            FlowKey::Property(object, property),
            FlowFact::Values(values.clone()),
        );
        context.propose(
            FlowKey::Properties(object),
            FlowFact::Values(values.clone()),
        );
        if self.numeric_properties[property.0 as usize] {
            context.propose(
                FlowKey::NumericProperties(object),
                FlowFact::Values(values.clone()),
            );
        }
    }

    #[cfg(test)]
    fn property_fact_count(&self, property: PropertySlot) -> usize {
        2 + usize::from(self.numeric_properties[property.0 as usize])
    }

    fn singleton_value(&self, value: Value) -> Values {
        Values::singleton(self.origin_table.clone(), value)
    }
}

impl IterativeRules for FlowRules {
    type Key = FlowKey;
    type Fact = FlowFact;
    type Rule = usize;
    type MergeScratch = FlowMergeScratch;
    const SOURCE_ONLY: bool = true;
    const EVALUATES_CHANGES: bool = true;

    fn new_merge_scratch(&self) -> Self::MergeScratch {
        #[cfg(test)]
        {
            FlowMergeScratch::with_work(self.work.clone())
        }
        #[cfg(not(test))]
        {
            FlowMergeScratch::default()
        }
    }

    fn metric_key_family(key: &FlowKey) -> work_metrics::KeyFamily {
        use work_metrics::KeyFamily;
        match key {
            FlowKey::ValueFlowActive => KeyFamily::Active,
            FlowKey::Binding(_) => KeyFamily::Binding,
            FlowKey::CandidateBinding(_) => KeyFamily::CandidateBinding,
            FlowKey::CandidateReachable(_) => KeyFamily::CandidateReachable,
            FlowKey::Property(_, _) => KeyFamily::Property,
            FlowKey::Properties(_) | FlowKey::NumericProperties(_) => KeyFamily::Properties,
            FlowKey::Return(_) => KeyFamily::Return,
            FlowKey::Parameters(_) => KeyFamily::Parameters,
            FlowKey::ReadyParameters(_) => KeyFamily::ReadyParameters,
            FlowKey::InvokedCells(_) => KeyFamily::InvokedCells,
            FlowKey::RetainedCells(_) => KeyFamily::RetainedCells,
            FlowKey::SourceCells(_) => KeyFamily::SourceCells,
            FlowKey::Initialization(_) => KeyFamily::Initialization,
            FlowKey::ReadOnlyCallback(_) => KeyFamily::ReadOnlyCallback,
        }
    }

    fn metric_event_kind(&self, rule: &usize) -> work_metrics::EventKind {
        use work_metrics::EventKind;
        match &self.events[*rule] {
            FlowEvent::Initialization { .. } => EventKind::Initialization,
            FlowEvent::FunctionDeclaration { .. } => EventKind::FunctionDeclaration,
            FlowEvent::ClassDeclaration { .. } => EventKind::ClassDeclaration,
            FlowEvent::Variable { .. } => EventKind::Variable,
            FlowEvent::AssignBinding { .. } => EventKind::AssignBinding,
            FlowEvent::AssignProperty { .. } => EventKind::AssignProperty,
            FlowEvent::WritePropertyExpr { .. } => EventKind::WritePropertyExpr,
            FlowEvent::WritePropertyBinding { .. } => EventKind::WritePropertyBinding,
            FlowEvent::WritePropertyValue { .. } => EventKind::WritePropertyValue,
            FlowEvent::FunctionMetadata { .. } => EventKind::FunctionMetadata,
            FlowEvent::Return { .. } => EventKind::Return,
            FlowEvent::Invoke { .. } => EventKind::Invoke,
        }
    }

    fn merge_observed(
        key: &FlowKey,
        current: &mut FlowFact,
        contribution: FlowFact,
        metrics: &mut work_metrics::Batch,
    ) -> bool {
        match (current, contribution) {
            (FlowFact::Values(current), FlowFact::Values(contribution))
                if matches!(
                    key,
                    FlowKey::Binding(_)
                        | FlowKey::Property(_, _)
                        | FlowKey::Properties(_)
                        | FlowKey::NumericProperties(_)
                        | FlowKey::Return(_)
                ) =>
            {
                assert!(Arc::ptr_eq(&current.origins, &contribution.origins));
                let bitmap_changed =
                    union_bitmap_observed(&mut current.bitmap, &contribution.bitmap, |operation| {
                        metrics.add(operation, 1)
                    });
                let unknown_changed = contribution.unknown && !current.unknown;
                current.unknown |= contribution.unknown;
                bitmap_changed || unknown_changed
            }
            (current, contribution) => Self::merge(key, current, contribution),
        }
    }

    fn merge_with_scratch(
        key: &FlowKey,
        current: &mut FlowFact,
        contribution: FlowFact,
        scratch: &mut Self::MergeScratch,
    ) -> bool {
        match (current, contribution) {
            (FlowFact::Values(current), FlowFact::Values(contribution))
                if matches!(key, FlowKey::Property(_, _) | FlowKey::Properties(_)) =>
            {
                merge_values_with_scratch(current, contribution, scratch, |_| {})
            }
            (current, contribution) => merge_flow_fact(key, current, contribution),
        }
    }

    fn merge_observed_with_scratch(
        key: &FlowKey,
        current: &mut FlowFact,
        contribution: FlowFact,
        metrics: &mut work_metrics::Batch,
        scratch: &mut Self::MergeScratch,
    ) -> bool {
        match (current, contribution) {
            (FlowFact::Values(current), FlowFact::Values(contribution))
                if matches!(key, FlowKey::Property(_, _) | FlowKey::Properties(_)) =>
            {
                merge_values_with_scratch(current, contribution, scratch, |operation| {
                    metrics.add(operation, 1)
                })
            }
            (current, contribution) => Self::merge_observed(key, current, contribution, metrics),
        }
    }

    fn evaluate_changed(
        &self,
        index: &usize,
        changed: &BTreeSet<FlowKey>,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        if matches!(self.events[*index], FlowEvent::Invoke { .. }) {
            self.invocations.borrow_mut()[*index]
                .as_mut()
                .expect("invocation event state")
                .changed(changed);
        }
        if let Some(write) = self.property_writes.borrow_mut()[*index].as_mut() {
            write.changed(changed);
        }
        self.evaluate(index, context);
    }

    fn evaluate(
        &self,
        index: &usize,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
    ) {
        #[cfg(test)]
        {
            self.work.borrow_mut().event_executions += 1;
        }
        let event = &self.events[*index];
        match event {
            FlowEvent::Initialization { id, sites } => {
                context.propose(
                    FlowKey::Initialization(*id),
                    FlowFact::Initialization(sites.clone()),
                );
            }
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
                self.write_property(
                    *index,
                    PropertyTarget::Expression(object),
                    *property,
                    right,
                    context,
                );
            }
            FlowEvent::WritePropertyExpr {
                object,
                property,
                value,
            } => {
                self.write_property(
                    *index,
                    PropertyTarget::Fixed(*object),
                    *property,
                    value,
                    context,
                );
            }
            FlowEvent::WritePropertyBinding {
                object,
                property,
                binding,
            } => {
                self.write_property(
                    *index,
                    PropertyTarget::Fixed(*object),
                    *property,
                    &FlowValueExpr::Binding(*binding),
                    context,
                );
            }
            FlowEvent::WritePropertyValue {
                object,
                property,
                value,
            } => {
                self.write_property(
                    *index,
                    PropertyTarget::Fixed(*object),
                    *property,
                    &FlowValueExpr::Value(*value),
                    context,
                );
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
                self.invoke(*index, callee, args, context);
            }
        }
    }

    fn difference(
        key: &Self::Key,
        contribution: &Self::Fact,
        delivered: &Self::Fact,
    ) -> Option<Self::Fact> {
        let (FlowKey::Binding(_), FlowFact::Values(values), FlowFact::Values(delivered)) =
            (key, contribution, delivered)
        else {
            unreachable!("invocation deltas deliver values to formals");
        };
        let delta = values.difference(delivered);
        (!delta.is_empty()).then_some(FlowFact::Values(delta))
    }

    #[cfg(test)]
    fn delta_atoms(delta: &Self::Fact) -> usize {
        let FlowFact::Values(values) = delta else {
            unreachable!("origin delta")
        };
        values.len()
    }

    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        merge_flow_fact(key, current, contribution)
    }
}

fn merge_flow_fact(key: &FlowKey, current: &mut FlowFact, contribution: FlowFact) -> bool {
    match (key, current, contribution) {
        (
            FlowKey::ReadOnlyCallback(_),
            FlowFact::ReadOnlyCallback(current),
            FlowFact::ReadOnlyCallback(contribution),
        ) => {
            assert_eq!(
                *current, contribution,
                "immutable callback source certificate"
            );
            false
        }
        (
            FlowKey::Initialization(_),
            FlowFact::Initialization(current),
            FlowFact::Initialization(contribution),
        ) => current.union_with(contribution),
        (
            FlowKey::InvokedCells(_) | FlowKey::RetainedCells(_) | FlowKey::SourceCells(_),
            FlowFact::Cells(current),
            FlowFact::Cells(contribution),
        ) => current.union_with(&contribution),
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
        ) => union_bitmap(current, &contribution),
        (
            FlowKey::Binding(_)
            | FlowKey::Property(_, _)
            | FlowKey::Properties(_)
            | FlowKey::NumericProperties(_)
            | FlowKey::Return(_),
            FlowFact::Values(current),
            FlowFact::Values(contribution),
        ) => current.union_with(&contribution),
        _ => unreachable!("flow key and fact variants must agree"),
    }
}

fn numeric_property_expr(expr: &Expr) -> bool {
    evaluate(expr, &NoBindings)
        .is_some_and(|kinds| kinds.is_only(PrimKinds::NUMBER.union(PrimKinds::BIGINT)))
}

fn computed_numeric_property(prop: &MemberProp) -> bool {
    matches!(prop, MemberProp::Computed(computed) if numeric_property_expr(&computed.expr))
}

/// A conservative superset of strings a Number or BigInt can produce through ToPropertyKey.
/// Prefix checks avoid scanning names on reads; NaN and Infinity are the named Number cases.
pub(super) fn may_be_numeric_property_key(name: &str) -> bool {
    matches!(
        name.as_bytes().first(),
        Some(b'0'..=b'9' | b'+' | b'-' | b'.')
    ) || matches!(name, "NaN" | "Infinity")
}

pub fn static_prop(prop: &MemberProp) -> Option<String> {
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
            let Value::Alloc(Kind::Function, function) = target else {
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
            .entry(Value::Alloc(Kind::Function, function))
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
        let value = Value::Alloc(Kind::Function, key);
        self.flow
            .canonical
            .entry(value)
            .or_insert_with(|| node.ident.to_id());
        let values = self.flow.singleton_value(value);
        self.merge_binding(node.ident.to_id(), values);
        node.function.visit_with(self);
    }

    fn visit_class_decl(&mut self, node: &ClassDecl) {
        let value = Value::Alloc(Kind::Class, node.class.span.lo.0);
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
                    if matches!(
                        value,
                        Value::Alloc(Kind::Function, _) | Value::Alloc(Kind::Class, _)
                    ) && matches!(
                        init.unwrap_parens(),
                        Expr::Fn(_) | Expr::Arrow(_) | Expr::Class(_)
                    ) {
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
        match binding_targets::simple_write_target(&node.left) {
            Some(binding_targets::SimpleWriteTarget::Binding(binding)) => {
                self.merge_binding(binding, values);
            }
            Some(binding_targets::SimpleWriteTarget::Member(member)) => {
                if let Some(prop) = static_prop(&member.prop) {
                    for origin in self.flow.values_of_expr(&member.obj) {
                        if let Value::Alloc(Kind::Object, object) = origin {
                            *self.changed |=
                                self.flow
                                    .merge_property(object, prop.clone(), values.clone());
                        }
                    }
                }
            }
            None => {}
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
                            let values = self.flow.singleton_value(Value::Alloc(
                                Kind::Function,
                                method.function.span.lo.0,
                            ));
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
    position: LazyPosition,
}

impl LazyBoundary for FlowCallCollector<'_> {
    fn position(&mut self) -> &mut LazyPosition {
        &mut self.position
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
            bucket.record(&id, self.position);
        }
        for binding in targets.bindings {
            self.calls.record(&binding, self.position);
        }
    }
}

impl Visit for FlowCallCollector<'_> {
    impl_lazy_optional_path_visits!();

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        if node.op.may_short_circuit() {
            lazy_visit_may_skip(self, node);
        } else {
            node.visit_children_with(self);
        }
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
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

    impl_lazy_callable_visits!();
    impl_lazy_class_visits!();

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        node.arg.visit_with(self);
        if self.position.depth > 0 && self.position.may_skip_await_depth == 0 {
            self.position.past_await = true;
        }
    }
}

#[cfg(test)]
mod bitmap_join_tests {
    use super::*;

    fn bitmap(values: impl IntoIterator<Item = u32>) -> Arc<RoaringBitmap> {
        Arc::new(values.into_iter().collect())
    }

    fn values(bitmap: Arc<RoaringBitmap>, unknown: bool, origins: &Arc<OriginTable>) -> Values {
        Values {
            bitmap,
            unknown,
            origins: origins.clone(),
        }
    }

    fn mask_bitmap(mask: u32, width: u32) -> Arc<RoaringBitmap> {
        bitmap((0..width).filter(|bit| mask & (1 << bit) != 0))
    }

    #[test]
    fn guarded_non_function_projection_preserves_unknown_and_proxy_values() {
        let function = Value::Alloc(Kind::Function, 1);
        let class = Value::Alloc(Kind::Class, 2);
        let proxy = Value::Alloc(Kind::Proxy, 3);
        let object = Value::Alloc(Kind::Object, 4);
        let origins = Arc::new(OriginTable::from_origins([function, class, proxy, object]));
        let mut values = Values::empty(origins);
        for value in [function, class, proxy, object, Value::Top] {
            values.insert(value);
        }

        let filtered = values.without_function_class();
        assert!(!filtered.contains(&function));
        assert!(!filtered.contains(&class));
        assert!(filtered.contains(&proxy));
        assert!(filtered.contains(&object));
        assert!(filtered.contains(&Value::Top));

        let unchanged = Values::singleton(values.origins.clone(), object);
        let original_bitmap = unchanged.bitmap.clone();
        let filtered_unchanged = unchanged.without_function_class();
        assert!(
            Arc::ptr_eq(&filtered_unchanged.bitmap, &original_bitmap),
            "a disjoint filter should preserve bitmap sharing",
        );
    }

    #[test]
    fn cached_joins_match_uncached_bitmap_and_unknown_algebra_exhaustively() {
        let origins = Arc::new(OriginTable::from_origins([]));
        for width in 0..=4 {
            let masks = 0..(1 << width);
            for left_mask in masks.clone() {
                for right_mask in masks.clone() {
                    for left_unknown in [false, true] {
                        for right_unknown in [false, true] {
                            for second_unknown in [false, true] {
                                let left_bitmap = mask_bitmap(left_mask, width);
                                let right_bitmap = mask_bitmap(right_mask, width);
                                let left = values(left_bitmap.clone(), left_unknown, &origins);
                                let second_left =
                                    values(left_bitmap.clone(), second_unknown, &origins);
                                let right = values(right_bitmap, right_unknown, &origins);

                                let mut expected = left.clone();
                                let expected_changed = expected.union_with(&right);
                                let mut second_expected = second_left.clone();
                                let second_expected_changed = second_expected.union_with(&right);

                                let mut actual = left;
                                let mut second_actual = second_left;
                                let mut scratch = FlowMergeScratch::default();
                                let actual_changed = merge_values_with_scratch(
                                    &mut actual,
                                    right.clone(),
                                    &mut scratch,
                                    |_| {},
                                );
                                let second_changed = merge_values_with_scratch(
                                    &mut second_actual,
                                    right.clone(),
                                    &mut scratch,
                                    |_| {},
                                );
                                assert_eq!(actual_changed, expected_changed);
                                assert_eq!(actual, expected);
                                assert_eq!(second_changed, second_expected_changed);
                                assert_eq!(second_actual, second_expected);
                                assert!(actual.shares_bitmap(&second_actual));

                                // Metrics-on uses the same cached join path;
                                // observation cannot alter values or changes.
                                let mut observed =
                                    values(left_bitmap.clone(), left_unknown, &origins);
                                let mut second_observed =
                                    values(left_bitmap, second_unknown, &origins);
                                let mut observed_scratch = FlowMergeScratch::default();
                                let mut operations = Vec::new();
                                let observed_changed = merge_values_with_scratch(
                                    &mut observed,
                                    right.clone(),
                                    &mut observed_scratch,
                                    |operation| operations.push(operation),
                                );
                                let second_observed_changed = merge_values_with_scratch(
                                    &mut second_observed,
                                    right,
                                    &mut observed_scratch,
                                    |operation| operations.push(operation),
                                );
                                assert!(operations.iter().any(|operation| matches!(
                                    operation,
                                    work_metrics::Operation::BitmapJoinMemoHit
                                )));
                                assert_eq!(observed_changed, expected_changed);
                                assert_eq!(observed, expected);
                                assert_eq!(second_observed_changed, second_expected_changed);
                                assert_eq!(second_observed, second_expected);
                                assert!(observed.shares_bitmap(&second_observed));
                            }
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn join_outcomes_preserve_pointer_sharing_and_reuse_shared_union_results() {
        let origins = Arc::new(OriginTable::from_origins([]));

        let shared = bitmap([1, 2]);
        let mut same = values(shared.clone(), false, &origins);
        assert!(!merge_values_with_scratch(
            &mut same,
            values(shared.clone(), false, &origins),
            &mut FlowMergeScratch::default(),
            |_| {},
        ));
        assert!(same.shares_bitmap(&values(shared.clone(), false, &origins)));

        let old = bitmap([1, 2]);
        let mut included = values(old.clone(), false, &origins);
        let incoming_subset = values(bitmap([2]), false, &origins);
        assert!(!merge_values_with_scratch(
            &mut included,
            incoming_subset,
            &mut FlowMergeScratch::default(),
            |_| {},
        ));
        assert!(Arc::ptr_eq(&included.bitmap, &old));

        let incoming = values(bitmap([1, 2, 3]), false, &origins);
        let incoming_bitmap = incoming.bitmap.clone();
        let mut adopted = values(bitmap([1]), false, &origins);
        assert!(merge_values_with_scratch(
            &mut adopted,
            incoming,
            &mut FlowMergeScratch::default(),
            |_| {},
        ));
        assert!(Arc::ptr_eq(&adopted.bitmap, &incoming_bitmap));

        let old_bitmap = bitmap([1]);
        let old_witness = Arc::downgrade(&old_bitmap);
        let mut first = values(old_bitmap.clone(), false, &origins);
        let mut second = values(old_bitmap.clone(), false, &origins);
        drop(old_bitmap);
        let right = values(bitmap([2]), true, &origins);
        let work = Rc::new(RefCell::new(FlowWorkStats::default()));
        let mut scratch = FlowMergeScratch::with_work(work.clone());
        let mut operations = Vec::new();
        assert!(merge_values_with_scratch(
            &mut first,
            right.clone(),
            &mut scratch,
            |operation| operations.push(operation),
        ));
        assert!(merge_values_with_scratch(
            &mut second,
            right,
            &mut scratch,
            |operation| operations.push(operation),
        ));
        assert!(first.shares_bitmap(&second));
        assert!(first.unknown && second.unknown);
        assert_eq!(work.borrow().bitmap_join_memo_hits, 1);
        assert_eq!(work.borrow().bitmap_subset_checks, 2);
        assert_eq!(work.borrow().bitmap_copy_on_write_unions, 1);
        assert_eq!(
            operations
                .iter()
                .filter(|operation| matches!(operation, work_metrics::Operation::BitmapJoinMemoHit))
                .count(),
            1
        );
        assert_eq!(
            operations
                .iter()
                .filter(|operation| matches!(
                    operation,
                    work_metrics::Operation::BitmapCopyOnWriteUnion
                ))
                .count(),
            1,
            "cached union reuse is not counted as a second COW union"
        );
        drop(second);
        assert!(
            old_witness.upgrade().is_none(),
            "memo must not pin old bitmap payloads"
        );
    }

    #[test]
    fn in_place_cow_invalidates_weak_pair_witnesses_before_bitmap_mutation() {
        let origins = Arc::new(OriginTable::from_origins([]));
        let old_bitmap = bitmap([1]);
        let mut first = values(old_bitmap.clone(), false, &origins);
        let mut second = values(old_bitmap.clone(), false, &origins);
        drop(old_bitmap);
        let right = values(bitmap([2]), false, &origins);
        let other = values(bitmap([4]), false, &origins);
        let work = Rc::new(RefCell::new(FlowWorkStats::default()));
        let mut scratch = FlowMergeScratch::with_work(work.clone());

        assert!(merge_values_with_scratch(
            &mut first,
            right.clone(),
            &mut scratch,
            |_| {},
        ));
        drop(first);
        // The old bitmap now has one strong owner. A different join mutates it
        // in place; Arc::make_mut must dissociate the prior weak witness.
        assert!(merge_values_with_scratch(
            &mut second,
            other,
            &mut scratch,
            |_| {},
        ));
        assert!(merge_values_with_scratch(
            &mut second,
            right,
            &mut scratch,
            |_| {},
        ));
        assert_eq!(second.bitmap.iter().collect::<Vec<_>>(), [1, 2, 4]);
        assert_eq!(work.borrow().bitmap_join_memo_hits, 0);
    }

    #[test]
    fn dead_weak_witness_rejects_an_address_only_aba_match() {
        let mut scratch = FlowMergeScratch::default();
        let old = bitmap([1]);
        let stale_witness = Arc::downgrade(&old);
        drop(old);
        let current = bitmap([1]);
        let from = bitmap([2]);
        scratch.entries[0] = Some(BitmapJoinMemoEntry {
            // Simulate allocator address reuse: the address matches, but the
            // weak witness belongs to the dead prior allocation.
            into_address: Arc::as_ptr(&current) as usize,
            from_address: Arc::as_ptr(&from) as usize,
            into: stale_witness,
            from: Arc::downgrade(&from),
            result: BitmapJoinResult::SubsetSkip,
        });
        assert!(scratch.lookup(&current, &from).is_none());
    }

    #[test]
    fn parsed_property_write_fanout_reuses_shared_bitmap_join_pairs() {
        let mut source = String::from("function left() {}\nfunction right() {}\n");
        for index in 0..12 {
            source.push_str(&format!(
                "const object{index} = {{}};\nobject{index}.p = left;\nobject{index}.p = right;\n"
            ));
        }
        let parsed = raw_js_test_support::parse(&source);
        let views = crate::facts::top_level_item_views(&parsed.body);
        let (flow, work) = ChunkValueFlow::analyze_with_stats(&views);
        assert_eq!(work.bitmap_join_memo_hits, 12);
        assert_eq!(work.bitmap_join_memo_misses, 12);
        assert_eq!(work.bitmap_subset_checks, 24);
        assert_eq!(work.bitmap_copy_on_write_unions, 12);

        let id = |name: &str| (name.into(), swc_common::SyntaxContext::empty());
        let left = flow.values_of_binding(&id("left"));
        let right = flow.values_of_binding(&id("right"));
        for index in 0..12 {
            let object = flow.values_of_binding(&id(&format!("object{index}")));
            let [Value::Alloc(Kind::Object, object)] = object.iter().collect::<Vec<_>>()[..] else {
                panic!("object allocation should be singleton");
            };
            let property = flow
                .values_of_property(object, "p")
                .expect("property write result");
            assert!(left.iter().all(|value| property.contains(&value)));
            assert!(right.iter().all(|value| property.contains(&value)));
        }
    }
}
