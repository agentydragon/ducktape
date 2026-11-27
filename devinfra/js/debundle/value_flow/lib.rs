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

#[cfg(test)]
mod bitmap_join_tests;
mod call_effects;
mod candidate_reachability;
mod flow_program;
mod flow_rules;
mod guarded_member_reads;
mod initialization;
mod member_read_certificates;
mod program;
pub mod program_bindings;
mod read_only_callbacks;
mod return_summary;
mod scalar_sequence;
pub mod values;
use call_effects::{CallEffectBody, CallEffectRules, KnownCellAccesses};
use candidate_reachability::CandidateReachabilityClosure;
#[cfg(test)]
use flow_program::DeclaredBindingCollector;
use flow_program::{
    BindingSlot, FlowConstraints, FlowEvent, FlowFact, FlowKey, FlowProgram, FlowSymbols,
    FlowValueExpr, FreshFactoryImports, PropertyProducerCensus, PropertySlot, solve_flow_observed,
};
#[cfg(test)]
use flow_rules::FlowPass;
use flow_rules::{FlowRules, Invocation, PropertyReceivers, PropertyWrite};
pub use initialization::CallableDeclaration;
use initialization::{InitializationCensus, InitializationSites};
use member_read_certificates::MemberReadCertificates;
pub use program::{ProgramFlowModule, ProgramValueFlows, program_value_flows};
use read_only_callbacks::ReadOnlyCallback;
use return_summary::ReturnSummary;
use values::{
    FlowMergeScratch, OriginId, OriginTable, Values, ValuesBuilder, merge_values_with_scratch,
    union_bitmap, union_bitmap_observed,
};

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

use analysis_domain::primitive::{NoBindings, evaluate};
use analysis_model::PrimKinds;
use analysis_vocabulary::value::{Kind, Value};
use inference::{FactContext, IterativeInference, IterativeRules};

use analysis_domain::item_views::TopLevelItemView;

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

pub struct CallTargets {
    pub bindings: BTreeSet<Id>,
    /// Every possible value has a canonical callable binding. Otherwise a
    /// direct identifier's syntactic target must retain its opaque fallback.
    pub complete: bool,
}

/// A filtered view over candidate-reachability facts in the completed flow
/// store. Reachability itself is solved with value flow; this view only limits
/// results to the roots requested by one consumer.
pub struct CandidateObjectIndex<'a> {
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

    pub fn for_binding(&self, id: &Id, visit: impl FnMut(Value)) {
        if let Some(bits) = self.flow.candidate_bindings.get(id) {
            self.visit_bits(bits, visit);
        }
    }

    /// Visit the union of reachable requested roots, once per root. Expanding
    /// each origin separately repeats every shared descendant at the sink.
    pub fn for_values(&self, values: &Values, mut visit: impl FnMut(Value)) {
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
    pub(crate) fn evaluate_value_expr_for_test(&self, expr: &Expr) -> (Values, usize) {
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

    pub fn candidate_object_index(&self, candidates: &BTreeSet<Value>) -> CandidateObjectIndex<'_> {
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
    pub(crate) fn candidate_reachability_storage_for_test(&self) -> (usize, u64, u64) {
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

    pub fn analyze_structural(
        body: &[TopLevelItemView<'_>],
        inputs: &[analysis_vocabulary::inputs::ResolvedNativeOperationInput],
        inferred: &BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
    ) -> Self {
        FlowProgram::structural(body, inputs, inferred).solve().0
    }

    pub fn native_set_allocations(
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

    pub fn values_of_expr(&self, expr: &Expr) -> Values {
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
    pub fn fresh_returned_record_keys(&self, call: &CallExpr) -> Option<&BTreeSet<String>> {
        let Some(Expr::Ident(ident)) = js_ast::callee_expr(call) else {
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

    pub fn aliases_by_object(
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

    pub fn call_targets(&self, callee: &Expr) -> CallTargets {
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

/// Exact own data fields created by one fresh ordinary object literal.
/// Spread, computed names, accessors, methods and `__proto__` retain the
/// conservative getter/proxy classification at the consuming pattern.
pub fn own_data_keys(object: &ObjectLit) -> Option<BTreeSet<String>> {
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

fn numeric_property_expr(expr: &Expr) -> bool {
    evaluate(expr, &NoBindings)
        .is_some_and(|kinds| kinds.is_only(PrimKinds::NUMBER.union(PrimKinds::BIGINT)))
}

fn computed_numeric_property(prop: &MemberProp) -> bool {
    matches!(prop, MemberProp::Computed(computed) if numeric_property_expr(&computed.expr))
}

/// A conservative superset of strings a Number or BigInt can produce through ToPropertyKey.
/// Prefix checks avoid scanning names on reads; NaN and Infinity are the named Number cases.
pub(crate) fn may_be_numeric_property_key(name: &str) -> bool {
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
mod value_flow_tests;
#[cfg(test)]
mod value_set_tests;
