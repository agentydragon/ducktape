//! Monotone value identities for calls and object aliases.
//!
//! The sets deliberately over-approximate: an unsupported expression yields
//! `Unknown`, and every assignment adds possible values rather than replacing
//! them. The same facts feed at-init call promotion and PlainData's escape
//! check, so an alias/return cannot be understood by one pass and invisible to
//! the other. `program` qualifies source identities and joins these same rules
//! across imports. The relation is context-insensitive and supplies no proof
//! of call effects, initialization availability or normal completion.

mod call_effects;
mod initialization;
mod program;
mod read_only_callbacks;
use call_effects::{CallEffectBody, CallEffectRules, KnownCellAccesses};
pub(crate) use initialization::CallableDeclaration;
use initialization::{InitializationCensus, InitializationSites};
pub use program::ProgramValueFlows;
pub(crate) use program::{ProgramFlowModule, program_value_flows};
use read_only_callbacks::ReadOnlyCallback;

use source_identity::SourceUnitOrdinal;
#[cfg(test)]
use std::collections::VecDeque;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;
#[cfg(test)]
use std::{cell::Cell, rc::Rc};

use roaring::{
    RoaringBitmap,
    bitmap::{IntoIter as RoaringIntoIter, Iter as RoaringIter},
};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use super::abstract_domain::value::{Kind, Value};
use crate::inference::{FactContext, IterativeInference, IterativeRules};

use super::{
    LazyBoundary, PositionBucketed, StatementFacts, TopLevelItemView, lazy_visit_arrow_expr,
    lazy_visit_bin_expr, lazy_visit_class, lazy_visit_class_member, lazy_visit_cond_expr,
    lazy_visit_function, lazy_visit_getter_prop, lazy_visit_if_stmt, lazy_visit_may_skip,
    lazy_visit_method_prop, lazy_visit_setter_prop,
};

pub(crate) fn native_set_allocation_roots(
    inputs: &[crate::analysis_inputs::ResolvedNativeOperationInput],
    inferred: &BTreeMap<u32, crate::analysis_inputs::InferredSetConstruction>,
) -> BTreeMap<u32, crate::analysis_inputs::ProvenSetAllocation> {
    let mut allocations: BTreeMap<_, _> = inferred
        .iter()
        .map(|(site, proof)| {
            (
                *site,
                crate::analysis_inputs::ProvenSetAllocation::Inferred(Box::new(proof.clone())),
            )
        })
        .collect();
    allocations.extend(
        inputs
            .iter()
            .filter(|input| {
                input.evidence.operation
                    == crate::analysis_inputs::NativeInputKind::Operation(
                        spec::NativeOperationKind::SetFromPrimitiveArray,
                    )
            })
            .map(|input| {
                (
                    input.call_span.lo.0,
                    crate::analysis_inputs::ProvenSetAllocation::AuthorInput(
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
        Self { origins, ids }
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
pub(crate) struct Values {
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

    pub(crate) fn is_empty(&self) -> bool {
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

    /// Ids of this set's origins also present in `mask`, without decoding
    /// origins that are not eligible for candidate reachability.
    fn ids_within<'a>(&'a self, mask: &'a RoaringBitmap) -> impl Iterator<Item = OriginId> + 'a {
        let unknown = self.origins.id(Value::Top);
        let unknown = (self.unknown && mask.contains(unknown.0)).then_some(unknown);
        self.bitmap
            .iter()
            .filter(move |id| mask.contains(*id))
            .map(OriginId)
            .chain(unknown)
    }

    pub(super) fn iter(&self) -> ValuesIter<'_> {
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

    fn union(mut self, other: Self) -> Self {
        self.union_with(&other);
        self
    }
}

fn union_bitmap(into: &mut Arc<RoaringBitmap>, from: &Arc<RoaringBitmap>) -> bool {
    if Arc::ptr_eq(into, from) || from.is_subset(into) {
        return false;
    }
    if into.is_subset(from) {
        *into = from.clone();
        return true;
    }
    *Arc::make_mut(into) |= from.as_ref();
    true
}

impl Extend<Value> for Values {
    fn extend<T: IntoIterator<Item = Value>>(&mut self, iter: T) {
        for origin in iter {
            self.insert(origin);
        }
    }
}

pub(crate) struct ValuesIter<'a> {
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

pub(crate) struct ValuesIntoIter {
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
        let mut reached = RoaringBitmap::new();
        for value in values {
            if let Some(bits) = self.flow.candidate_reachability.get(&value) {
                for_each_bitmap_intersection(bits, &self.mask, |index| {
                    reached.insert(index);
                });
            }
        }
        for index in reached {
            visit(self.flow.candidate_objects[index as usize]);
        }
    }
}

#[derive(Debug, Clone, Default)]
#[cfg_attr(test, derive(PartialEq, Eq))]
pub(crate) struct ChunkValueFlow {
    origin_table: Arc<OriginTable>,
    program: Option<program::ChunkProgramFlow>,
    declared_ids: BTreeSet<Id>,
    bindings: BTreeMap<Id, Values>,
    properties: BTreeMap<u32, HashMap<String, Values>>,
    /// Cached union of all known properties, used by dynamic property reads.
    property_unions: BTreeMap<u32, Values>,
    returns: BTreeMap<u32, Values>,
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
    pub(crate) callable_source_unit: BTreeMap<Value, SourceUnitOrdinal>,
    /// Native Set construction sites backed by a validated author input or
    /// checked pre-flow source proof. Other constructor results stay unknown.
    native_set_allocations: BTreeMap<u32, crate::analysis_inputs::ProvenSetAllocation>,
}

impl ChunkValueFlow {
    pub(crate) fn read_only_callback(&self, function: u32) -> Option<&ReadOnlyCallback<Id>> {
        self.read_only_callbacks.get(&function)
    }

    pub(crate) fn lexical_read_guards(&self) -> impl Iterator<Item = (&Id, SourceUnitOrdinal)> {
        self.initializations
            .iter()
            .filter_map(|(binding, sites)| sites.lexical_read_guard().map(|owner| (binding, owner)))
    }

    pub(crate) fn callable_declarations(&self) -> impl Iterator<Item = (&Id, CallableDeclaration)> {
        self.initializations
            .iter()
            .flat_map(|(binding, sites)| sites.callables().map(move |callable| (binding, callable)))
    }

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
        CandidateObjectIndex { flow: self, mask }
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

    pub(crate) fn analyze(body: &[TopLevelItemView<'_>]) -> Self {
        FlowProgram::collect(body, &BTreeSet::new()).solve().0
    }

    pub(crate) fn analyze_with_candidates(
        body: &[TopLevelItemView<'_>],
        candidates: &BTreeSet<Value>,
    ) -> Self {
        FlowProgram::collect(body, candidates).solve().0
    }

    pub(crate) fn analyze_structural(
        body: &[TopLevelItemView<'_>],
        inputs: &[crate::analysis_inputs::ResolvedNativeOperationInput],
        inferred: &BTreeMap<u32, crate::analysis_inputs::InferredSetConstruction>,
    ) -> Self {
        FlowProgram::structural(body, inputs, inferred).solve().0
    }

    pub(crate) fn native_set_allocations(
        &self,
    ) -> &BTreeMap<u32, crate::analysis_inputs::ProvenSetAllocation> {
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
            read_only_callbacks,
            origin_table: program.origin_table,
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
        if let Some(program) = &self.program {
            return program.values_of_expr(expr, self);
        }
        match expr.unwrap_parens() {
            Expr::Ident(ident) => self.values_of_binding(&ident.to_id()),
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
                let mut out = self.empty_values();
                for origin in self.values_of_expr(&member.obj) {
                    if let Value::Alloc(Kind::Object, object) | Value::Alloc(Kind::Set, object) =
                        origin
                    {
                        if let Some(prop) = &prop {
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
                            if let Some(properties) = self.properties.get(&object) {
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
            Expr::Call(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    return self.unknown_values();
                };
                let mut out = self.empty_values();
                for origin in self.values_of_expr(callee) {
                    if let Value::Alloc(Kind::Function, function) = origin {
                        if let Some(values) = self.returns.get(&function) {
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

    pub(crate) fn values_of_property(&self, object: u32, property: &str) -> Option<&Values> {
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
                    values.push(Value::Top);
                }
            }
            FlowValueExpr::Value(value) => values.push(*value),
            FlowValueExpr::Member { object, property } => {
                let mut objects = values.child();
                append_value_expr(object, &mut objects, declared, read_values);
                for origin in objects.finish() {
                    if let Value::Alloc(Kind::Object, object) | Value::Alloc(Kind::Set, object) =
                        origin
                    {
                        if let Some(property) = property {
                            // Unknown is durable even when a later write adds a value.
                            if let Some(found) = read_values(FlowKey::Property(object, *property)) {
                                values.extend(&found);
                            } else {
                                values.push(Value::Top);
                            }
                        } else {
                            if let Some(found) = read_values(FlowKey::Properties(object)) {
                                values.extend(&found);
                            }
                            values.push(Value::Top);
                        }
                    } else {
                        values.push(Value::Top);
                    }
                }
            }
            FlowValueExpr::Call(callee) => {
                let mut targets = values.child();
                append_value_expr(callee, &mut targets, declared, read_values);
                for origin in targets.finish() {
                    if let Value::Alloc(Kind::Function, function) = origin {
                        if let Some(found) = read_values(FlowKey::Return(function)) {
                            values.extend(&found);
                        }
                    } else {
                        values.push(Value::Top);
                    }
                }
            }
            FlowValueExpr::Union(_, _) => unreachable!("unions are handled by the work stack"),
            FlowValueExpr::Empty => {}
            FlowValueExpr::Unknown => values.push(Value::Top),
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
    Value(Value),
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
            Expr::Fn(function) => {
                Self::Value(Value::Alloc(Kind::Function, function.function.span.lo.0))
            }
            Expr::Arrow(arrow) => Self::Value(Value::Alloc(Kind::Function, arrow.span.lo.0)),
            Expr::Class(class) => Self::Value(Value::Alloc(Kind::Class, class.class.span.lo.0)),
            Expr::Object(object) => Self::Value(Value::Alloc(Kind::Object, object.span.lo.0)),
            Expr::Array(array) => Self::Value(Value::Alloc(Kind::Object, array.span.lo.0)),
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

/// Candidate reachability is a second stratum over the completed value facts:
/// no event rule reads a candidate key, so it never wakes AST events.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum CandidateRule {
    Reachability(OriginId),
    Binding(BindingSlot),
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
    native_set_allocations: BTreeMap<u32, crate::analysis_inputs::ProvenSetAllocation>,
    fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
}

impl FlowProgram {
    fn structural(
        body: &[TopLevelItemView<'_>],
        inputs: &[crate::analysis_inputs::ResolvedNativeOperationInput],
        inferred: &BTreeMap<u32, crate::analysis_inputs::InferredSetConstruction>,
    ) -> Self {
        let mut candidates = super::abstract_domain::heap::tracked_allocation_roots(body);
        candidates.extend(super::heap_contents::array_candidate_roots(body));
        let allocations = native_set_allocation_roots(inputs, inferred);
        candidates.extend(
            allocations
                .keys()
                .map(|site| Value::Alloc(Kind::Set, *site)),
        );
        Self::collect_with_native_set_allocations(body, &candidates, &allocations)
    }

    fn collect(body: &[TopLevelItemView<'_>], candidate_objects: &BTreeSet<Value>) -> Self {
        Self::collect_with_native_set_allocations(body, candidate_objects, &BTreeMap::new())
    }

    fn collect_with_native_set_allocations(
        body: &[TopLevelItemView<'_>],
        candidate_objects: &BTreeSet<Value>,
        native_set_allocations: &BTreeMap<u32, crate::analysis_inputs::ProvenSetAllocation>,
    ) -> Self {
        let mut collector = FlowProgramCollector {
            declared_ids: BTreeSet::new(),
            symbols: FlowSymbols::default(),
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
        }
    }

    fn solve(self) -> (ChunkValueFlow, usize, usize) {
        let Self {
            module_bindings: _,
            imports: _,
            declared_ids,
            symbols,
            events,
            cell_effects,
            origin_table,
            candidate_objects,
            canonical,
            callable_source_unit,
            native_set_allocations,
            fresh_returned_record_keys,
        } = self;
        let (facts, evaluations, argument_evaluations) = solve_flow(
            events,
            symbols.declared(&declared_ids),
            origin_table.clone(),
            &candidate_objects,
            native_set_allocations.keys().copied().collect(),
            cell_effects
                .into_iter()
                .filter(|(_, body)| body.read_only_returns.is_some())
                .collect(),
        );

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
        (flow, evaluations, argument_evaluations)
    }
}

/// The shared value and completed-candidate strata, independent of the source
/// coordinate system used to prepare their constraints.
fn solve_flow(
    events: Vec<FlowEvent>,
    declared: Vec<bool>,
    origin_table: Arc<OriginTable>,
    candidate_objects: &[Value],
    native_set_allocations: BTreeSet<u32>,
    cell_effects: BTreeMap<u32, CallEffectBody>,
) -> (BTreeMap<FlowKey, FlowFact>, usize, usize) {
    let producer_count = events.len();
    #[cfg(test)]
    let argument_evaluations = Rc::new(Cell::new(0));
    #[cfg(test)]
    let event_executions = Rc::new(Cell::new(0));
    let events = Arc::new(events);
    let rules = FlowRules {
        events: events.clone(),
        declared: declared.clone(),
        origin_table: origin_table.clone(),
        #[cfg(test)]
        argument_evaluations: argument_evaluations.clone(),
        #[cfg(test)]
        event_executions: event_executions.clone(),
    };
    let values = IterativeInference::new(rules).solve_values_resumable(0..producer_count);
    let values = if cell_effects.is_empty() {
        values
    } else {
        let rules = CallEffectRules::new(cell_effects, &events, &values, &declared, &origin_table);
        let producers = rules.producers();
        IterativeInference::from_completed(rules, values).solve_values_resumable(producers)
    };
    drop(events);
    let facts = if candidate_objects.is_empty() {
        values.into_values()
    } else {
        let rules = CandidateRules::new(
            origin_table.clone(),
            native_set_allocations,
            values.keys(),
            candidate_objects,
        );
        let mut inference = IterativeInference::from_completed(rules, values);
        for (index, object) in candidate_objects.iter().copied().enumerate() {
            let mut bits = RoaringBitmap::new();
            bits.insert(u32::try_from(index).expect("candidate object count exceeds u32"));
            inference.seed(
                FlowKey::CandidateReachable(origin_table.id(object)),
                FlowFact::CandidateReachability(Arc::new(bits)),
            );
        }
        inference.solve_values([])
    };
    #[cfg(test)]
    let counts = (event_executions.get(), argument_evaluations.get());
    #[cfg(not(test))]
    let counts = (0, 0);
    (facts, counts.0, counts.1)
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
    native_set_allocations: BTreeMap<u32, crate::analysis_inputs::ProvenSetAllocation>,
    fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
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
        if let ForHead::Pat(pattern) = &node.left {
            binding_targets::record_pat_write(pattern, self);
        }
        node.visit_children_with(self);
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
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
}

#[derive(Clone)]
struct CachedArgument {
    values: Values,
    binding_dependencies: BTreeSet<BindingSlot>,
}

struct FlowRules {
    events: Arc<Vec<FlowEvent>>,
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

    fn singleton_value(&self, value: Value) -> Values {
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
                let values = self.values_of_expr_tracking(right, context, None);
                let objects = self.values_of_expr_tracking(object, context, None);
                for origin in objects {
                    if let Value::Alloc(Kind::Object, object) | Value::Alloc(Kind::Set, object) =
                        origin
                    {
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
                    let Value::Alloc(Kind::Function, function) = target else {
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
    may_reach: RoaringBitmap,
}

impl CandidateRules {
    fn new<'a>(
        origin_table: Arc<OriginTable>,
        native_set_allocations: BTreeSet<u32>,
        completed_keys: impl IntoIterator<Item = &'a FlowKey>,
        candidates: &[Value],
    ) -> Self {
        let mut rules = Self {
            may_reach: RoaringBitmap::new(),
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
            rules.may_reach.insert(id);
        }
        rules
    }

    /// The origin whose reachability a completed property-union or return fact carries.
    fn carrier_origin(&self, key: &FlowKey) -> Option<Value> {
        match key {
            FlowKey::Properties(object) if self.native_set_allocations.contains(object) => {
                Some(Value::Alloc(Kind::Set, *object))
            }
            FlowKey::Properties(object) => Some(Value::Alloc(Kind::Object, *object)),
            FlowKey::Return(function) => Some(Value::Alloc(Kind::Function, *function)),
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
        Self::propose_reachability(
            context,
            target,
            values
                .ids_within(&self.may_reach)
                .map(FlowKey::CandidateReachable),
        );
    }

    /// Union the reachability facts at `keys`, then propose it once.
    fn propose_reachability(
        context: &mut impl FactContext<FlowKey, FlowFact>,
        target: FlowKey,
        keys: impl IntoIterator<Item = FlowKey>,
    ) {
        let mut reachable: Option<Arc<RoaringBitmap>> = None;
        for key in keys {
            let Some(fact) = context.read(key) else {
                continue;
            };
            let FlowFact::CandidateReachability(bits) = fact else {
                unreachable!("candidate reachability keys must contain candidate facts")
            };
            match &mut reachable {
                None => reachable = Some(bits.clone()),
                Some(into) => {
                    union_bitmap(into, bits);
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

    /// A rule re-runs only when a reachability fact it read grew, and its
    /// carrier is final, so the changed reachability facts are all it needs to
    /// join.
    const EVALUATES_CHANGES: bool = true;

    fn evaluate_changed(
        &self,
        rule: &CandidateRule,
        changed: &BTreeSet<FlowKey>,
        context: &mut impl FactContext<FlowKey, FlowFact>,
    ) {
        let target = match rule {
            CandidateRule::Binding(binding) => FlowKey::CandidateBinding(*binding),
            CandidateRule::Reachability(origin) => FlowKey::CandidateReachable(*origin),
        };
        Self::propose_reachability(context, target, changed.iter().copied());
    }

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
                    Value::Alloc(Kind::Object, object) | Value::Alloc(Kind::Set, object) => {
                        FlowKey::Properties(object)
                    }
                    Value::Alloc(Kind::Function, function) => FlowKey::Return(function),
                    Value::Alloc(Kind::Class, _) | Value::Top => return,
                    other => unreachable!("value flow tracks no {other:?}"),
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
    impl_lazy_optional_path_visits!();

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
        if self.lazy_depth > 0 && self.may_skip_await_depth == 0 {
            self.past_await = true;
        }
    }
}
