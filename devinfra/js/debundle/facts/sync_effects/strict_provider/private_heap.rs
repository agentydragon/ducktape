//! Invocation-private heap debt for the shared provider/native transfer.
//! Raw provider addresses never become caller footprints. A complete graph
//! either remains private or exports formal slots and substituted input edges
//! with its exact result; the latter keeps allocation and property use opaque.

use super::super::super::object_surface::{
    CurrentOwnPropertyDescriptor, CurrentOwnPropertyProjection,
};
use super::super::super::property_inference::CapturedPropertyDescriptor;
use super::*;
use crate::analysis_inputs::{
    ProviderGraphEdge, ProviderGraphInput, ProviderGraphNode, ProviderGraphNodeKind,
    ProviderGraphRole, ProviderGraphValue, ProviderRetainedGraph,
};
use crate::facts::{EffectCell, HeapContentCells};
use native_operation::NativePrivateBirth;

#[derive(Clone, Copy, PartialEq, Eq)]
enum PrivateKind {
    Record,
    Array,
    DescriptorMap,
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum PrivateHeapRefusal {
    UnsupportedContext,
    UnrepresentedCell,
    UnprovedBirth,
    BorrowedOrForeign,
    UnrepresentedValue,
    NativeSlots,
    EscapingPrivateGraph,
    IncompleteBody,
}

#[derive(Clone, Default)]
pub(super) struct PrivateHeapEvidence {
    owned: BTreeMap<HeapAddress, PrivateKind>,
    touched: BTreeSet<HeapAddress>,
    footprint: HeapContentCells,
}

/// Construction is private to sealing below. Keeping the token in the
/// execution product prevents an unsealed footprint from being exported.
pub(super) struct SealedPrivateHeap {
    _footprint: HeapContentCells,
    pub(super) retained_graph: Option<ProviderRetainedGraph>,
    source_slots: BTreeMap<GraphKey, usize>,
}

impl SealedPrivateHeap {
    pub(super) fn verified_slots(
        &self,
        context: &ProviderContext<'_>,
        callable: u32,
        selected: &SelectedValue,
        program: &super::super::provider_program::ProviderProgram,
    ) -> Option<Vec<VerifiedProviderSlot>> {
        let graph = self.retained_graph.as_ref()?;
        let mut slots = BTreeMap::new();
        for (key, slot) in &self.source_slots {
            let verified = match key {
                GraphKey::Cell(binding) => {
                    if program.module_cells.contains_key(binding) {
                        VerifiedProviderSlot::ModuleCell
                    } else {
                        let body = context
                            .index
                            .callables
                            .get(&callable)?
                            .body_facts
                            .ordered_plan()?;
                        let source_origin = *context.index.lexical_cell_origins.get(binding)?;
                        program.source.source_offset(source_origin)?;
                        VerifiedProviderSlot::LocalCell {
                            kind: *body.bindings.get(binding)?,
                            source_origin,
                        }
                    }
                }
                GraphKey::Heap(address) => {
                    let origin = context.index.heap_resolver.source_origin(*address)?;
                    let source_origin = program.source.source_offset(origin)?;
                    if matches!(
                        graph.nodes[*slot].kind,
                        ProviderGraphNodeKind::Closure { .. }
                    ) {
                        if program.callable_position(source_origin) != Some(origin) {
                            return None;
                        }
                        let bindings = context
                            .closure_local_reads
                            .get(&origin)?
                            .iter()
                            .chain(context.closure_provider_reads.get(&origin)?)
                            .map(|binding| {
                                Some((
                                    binding.clone(),
                                    *self.source_slots.get(&GraphKey::Cell(binding.clone()))?,
                                ))
                            })
                            .collect::<Option<BTreeMap<_, _>>>()?;
                        VerifiedProviderSlot::Closure {
                            source_origin,
                            bindings,
                        }
                    } else {
                        let properties = context
                            .surface
                            .current_own_property_projection(*address)
                            .or_else(|| {
                                let proof = selected.postcondition.as_deref()?;
                                if selected.values
                                    != Values::from([AbstractValue::Value(Value::Alloc(
                                        Kind::Object,
                                        *address,
                                    ))])
                                {
                                    return None;
                                }
                                proof.current_projection(context, &selected.values)
                            })
                            .map(|projection| {
                                Box::new(VerifiedProviderProperties {
                                    layout: projection.layout,
                                    dependencies: projection.dependencies,
                                })
                            });
                        VerifiedProviderSlot::Heap {
                            source_origin,
                            kind: address.kind()?,
                            properties,
                        }
                    }
                }
            };
            slots.insert(*slot, verified);
        }
        (slots.len() == graph.nodes.len() && slots.keys().copied().eq(0..graph.nodes.len()))
            .then(|| slots.into_values().collect())
    }
}

impl PrivateHeapEvidence {
    pub(super) fn observe_frame(
        &mut self,
        frame: &LocalFrame,
        resolver: SourceHeapResolver,
    ) -> Result<(), PrivateHeapRefusal> {
        for address in &frame.fresh_records {
            if frame.borrowed_records.contains(address)
                || resolver.source_origin(*address).is_none()
                || address.kind() != Some(Kind::Object)
            {
                return Err(PrivateHeapRefusal::BorrowedOrForeign);
            }
            if self
                .owned
                .get(address)
                .is_some_and(|kind| *kind != PrivateKind::Record)
            {
                return Err(PrivateHeapRefusal::UnprovedBirth);
            }
            self.owned.insert(*address, PrivateKind::Record);
        }
        if self
            .owned
            .keys()
            .any(|address| frame.borrowed_records.contains(address))
        {
            return Err(PrivateHeapRefusal::BorrowedOrForeign);
        }
        Ok(())
    }

    /// Preflight is pure. Its returned debt is installed only after the shared
    /// native executor has accepted the revision and committed exactly once.
    pub(super) fn admit(
        &self,
        context: &ProviderContext<'_>,
        frame: &LocalFrame,
        plan: &CheckedNativePlan,
    ) -> Result<Self, PrivateHeapRefusal> {
        let resolver = context.index.heap_resolver;
        let birth = plan
            .private_birth(resolver)
            .map_err(|()| PrivateHeapRefusal::UnprovedBirth)?;
        let has_heap = !plan.heap.allocations.is_empty()
            || !plan.heap.reads.is_empty()
            || !plan.heap.writes.is_empty();
        if has_heap && (!context.body_effect_mode || context.class_mode) {
            return Err(PrivateHeapRefusal::UnsupportedContext);
        }
        if plan.heap.opaque || plan.heap.observes_external_heap {
            return Err(PrivateHeapRefusal::UnrepresentedCell);
        }
        let mut next = self.clone();
        next.observe_frame(frame, resolver)?;
        if let Some(birth) = birth {
            let (address, kind) = match birth {
                NativePrivateBirth::DescriptorMap(address) => (address, PrivateKind::DescriptorMap),
                NativePrivateBirth::DenseArray(address) => (address, PrivateKind::Array),
            };
            if resolver.source_origin(address).is_none()
                || address.kind() != Some(Kind::Object)
                || frame.borrowed_records.contains(&address)
                || next.owned.contains_key(&address)
                || context.surface.has_surface_state(address)
                || context.descriptor_maps.contains_key(&address)
                || context.arrays.contains_key(&address)
                || context.native_retained_references.contains_key(&address)
            {
                return Err(PrivateHeapRefusal::UnprovedBirth);
            }
            next.owned.insert(address, kind);
        }
        for cell in plan
            .heap
            .allocations
            .iter()
            .chain(&plan.heap.reads)
            .chain(&plan.heap.writes)
        {
            let address = next.classify_cell(cell)?;
            next.touched.insert(address);
        }
        next.footprint.extend(&plan.heap);
        Ok(next)
    }

    /// Exhaustive domain classification: qualified module cells and symbolic
    /// parameter reads have other transports; no other cell is silently local.
    fn classify_cell(&self, cell: &EffectCell) -> Result<HeapAddress, PrivateHeapRefusal> {
        let (address, admitted) = match cell {
            EffectCell::ObjectShape(address)
            | EffectCell::OwnProperty {
                origin: address, ..
            } => (
                *address,
                matches!(
                    self.owned.get(address),
                    Some(PrivateKind::Record | PrivateKind::Array)
                ),
            ),
            EffectCell::ArrayContents(address) => (
                *address,
                self.owned.get(address) == Some(&PrivateKind::Array),
            ),
            EffectCell::DescriptorMapContents(address) => (
                *address,
                self.owned.get(address) == Some(&PrivateKind::DescriptorMap),
            ),
            EffectCell::SetContents(_)
            | EffectCell::PrivateSlot { .. }
            | EffectCell::LexicalCell { .. }
            | EffectCell::Binding(_)
            | EffectCell::ModuleCell(_)
            | EffectCell::GlobalProp(_)
            | EffectCell::ExecutionProgress
            | EffectCell::GlobalSymbolRegistry => {
                return Err(PrivateHeapRefusal::UnrepresentedCell);
            }
        };
        admitted
            .then_some(address)
            .ok_or(PrivateHeapRefusal::BorrowedOrForeign)
    }

    pub(super) fn seal(
        &self,
        context: &ProviderContext<'_>,
        ledger: &TransferLedger<SelectedValue, Box<local_transfer::KnownThrow>>,
        capture_count: usize,
    ) -> Result<SealedPrivateHeap, PrivateHeapRefusal> {
        if self.touched.is_empty() {
            return Ok(SealedPrivateHeap {
                _footprint: self.footprint.clone(),
                retained_graph: None,
                source_slots: BTreeMap::new(),
            });
        }
        let selected = match &ledger.completion {
            crate::facts::sync_effects::Completion::Normal(selected) => selected,
            crate::facts::sync_effects::Completion::Abrupt(thrown) => &thrown.value,
            _ => return Err(PrivateHeapRefusal::IncompleteBody),
        };
        // Check private contents even when they are discarded. Unknown or
        // native-retained references cannot disappear behind a private target.
        let contents = self
            .owned
            .iter()
            .map(|(address, kind)| match kind {
                PrivateKind::Record | PrivateKind::Array => {
                    AbstractValue::Value(Value::Alloc(Kind::Object, *address))
                }
                PrivateKind::DescriptorMap => AbstractValue::OwnDescriptorMap(*address),
            })
            .collect();
        self.trace(context, contents, capture_count, false)?;
        let returned = &selected.values;
        let escaping = ledger.is_exact_abrupt();
        self.trace(context, returned.clone(), capture_count, escaping)?;
        let mut source_slots = BTreeMap::new();
        let retained_graph = if let Some(shape) = &context.input_shape {
            let (graph, touches_private, slots) =
                RetainedGraphBuilder::new(context, selected).build(returned, &self.touched)?;
            if touches_private {
                if !graph.is_well_formed(shape, &context.parameter_reads) {
                    return Err(PrivateHeapRefusal::UnrepresentedValue);
                }
                source_slots = slots;
                Some(graph)
            } else {
                None
            }
        } else {
            self.trace(context, returned.clone(), capture_count, true)?;
            None
        };
        Ok(SealedPrivateHeap {
            _footprint: self.footprint.clone(),
            retained_graph,
            source_slots,
        })
    }

    fn trace(
        &self,
        context: &ProviderContext<'_>,
        roots: Values,
        capture_count: usize,
        escaping: bool,
    ) -> Result<(), PrivateHeapRefusal> {
        let mut pending: Vec<_> = roots.into_iter().collect();
        let mut visited = BTreeSet::new();
        let environment = context
            .final_environment
            .as_ref()
            .ok_or(PrivateHeapRefusal::IncompleteBody)?;
        while let Some(value) = pending.pop() {
            match value {
                AbstractValue::Value(Value::Prim(kinds)) if !kinds.is_empty() => {}
                AbstractValue::SymbolicInput(input) if input.scope == context.scope => {
                    let valid = match input.input {
                        ProviderInput::Argument(_) => context
                            .argument_values
                            .contains(&AbstractValue::SymbolicInput(input)),
                        ProviderInput::Capture(index) => index < capture_count,
                        role @ (ProviderInput::Property { argument, property }
                        | ProviderInput::Getter { argument, property }
                        | ProviderInput::Setter { argument, property }) => {
                            argument == 0
                                && context
                                    .input_shape
                                    .as_ref()
                                    .and_then(|shape| shape.properties.values().nth(property))
                                    .is_some_and(|kind| {
                                        matches!(
                                            (role, kind),
                                            (
                                                ProviderInput::Property { .. },
                                                RecordInputProperty::Data
                                            ) | (
                                                ProviderInput::Getter { .. },
                                                RecordInputProperty::Accessor { getter: true, .. }
                                            ) | (
                                                ProviderInput::Setter { .. },
                                                RecordInputProperty::Accessor { setter: true, .. }
                                            )
                                        )
                                    })
                        }
                    };
                    if !valid {
                        return Err(PrivateHeapRefusal::UnrepresentedValue);
                    }
                }
                AbstractValue::Value(Value::Alloc(Kind::Object, address)) => {
                    if escaping && self.touched.contains(&address) {
                        return Err(PrivateHeapRefusal::EscapingPrivateGraph);
                    }
                    if !matches!(
                        self.owned.get(&address),
                        Some(PrivateKind::Record | PrivateKind::Array)
                    ) {
                        return Err(PrivateHeapRefusal::BorrowedOrForeign);
                    }
                    if !visited.insert((0, address)) {
                        continue;
                    }
                    if context.private_properties.contains_key(&address)
                        || context.native_retained_references.contains_key(&address)
                    {
                        return Err(PrivateHeapRefusal::NativeSlots);
                    }
                    pending.extend(
                        context
                            .retained_object_values(address)
                            .ok_or(PrivateHeapRefusal::UnrepresentedValue)?,
                    );
                    if let Some(array) = context.arrays.get(&address) {
                        pending.extend(array.slots.iter().flatten().cloned());
                    }
                }
                AbstractValue::OwnDescriptorMap(address) => {
                    if escaping && self.touched.contains(&address) {
                        return Err(PrivateHeapRefusal::EscapingPrivateGraph);
                    }
                    if self.owned.get(&address) != Some(&PrivateKind::DescriptorMap) {
                        return Err(PrivateHeapRefusal::BorrowedOrForeign);
                    }
                    if !visited.insert((1, address)) {
                        continue;
                    }
                    let map = context
                        .descriptor_maps
                        .get(&address)
                        .filter(|map| map.valid)
                        .ok_or(PrivateHeapRefusal::UnrepresentedValue)?;
                    pending.extend(map.retained_values.iter().cloned());
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, address)) => {
                    if !visited.insert((2, address)) {
                        continue;
                    }
                    let key = context
                        .index
                        .heap_resolver
                        .source_origin(address)
                        .ok_or(PrivateHeapRefusal::BorrowedOrForeign)?;
                    if context.unsupported_closures.contains(&key) {
                        return Err(PrivateHeapRefusal::UnrepresentedValue);
                    }
                    pending.extend(
                        context
                            .closure_captures
                            .get(&key)
                            .ok_or(PrivateHeapRefusal::UnrepresentedValue)?
                            .iter()
                            .cloned(),
                    );
                    // These maps include descendant callable reads, not just
                    // values present when the closure expression was created.
                    let locals = context
                        .closure_local_reads
                        .get(&key)
                        .ok_or(PrivateHeapRefusal::UnrepresentedValue)?;
                    let captures = context
                        .closure_provider_reads
                        .get(&key)
                        .ok_or(PrivateHeapRefusal::UnrepresentedValue)?;
                    let callable = context
                        .index
                        .callables
                        .get(&key)
                        .ok_or(PrivateHeapRefusal::UnrepresentedValue)?;
                    let reads = context
                        .index
                        .prototype_callable_reads
                        .get(&key)
                        .unwrap_or(&callable.reads);
                    if reads.iter().any(|binding| {
                        !locals.contains(binding)
                            && !captures.contains(binding)
                            && !callable.declared_locals.contains(binding)
                            && !callable.pattern_locals.contains(binding)
                            && !callable.local_functions.contains_key(binding)
                            && callable.self_binding.as_ref() != Some(binding)
                            && !callable
                                .params
                                .iter()
                                .any(|parameter| parameter.bindings.contains(binding))
                    }) {
                        return Err(PrivateHeapRefusal::UnrepresentedValue);
                    }
                    for binding in locals.iter().chain(captures) {
                        pending.extend(
                            environment
                                .get(binding)
                                .ok_or(PrivateHeapRefusal::UnrepresentedValue)?
                                .iter()
                                .cloned(),
                        );
                    }
                }
                AbstractValue::Value(
                    Value::Prim(_) | Value::Top | Value::Unavailable | Value::Alloc(_, _),
                )
                | AbstractValue::SymbolicInput(_)
                | AbstractValue::Closure(_, _)
                | AbstractValue::ClassMembers(_)
                | AbstractValue::Instance(..)
                | AbstractValue::CallbackFactory
                | AbstractValue::InlineCallbackFactory
                | AbstractValue::DeferredCallbacks(_)
                | AbstractValue::ProviderPrivateGraph(_)
                | AbstractValue::ProviderClosure(_, _)
                | AbstractValue::AuditedRetainedResult(..)
                | AbstractValue::ForeignDeferredRecord
                | AbstractValue::ObjectAssign
                | AbstractValue::ArrayIntrinsic
                | AbstractValue::GlobalObject => {
                    return Err(PrivateHeapRefusal::UnrepresentedValue);
                }
            }
        }
        Ok(())
    }
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
enum GraphKey {
    Heap(HeapAddress),
    Cell(Id),
}

/// A projection of the already executed heap, not an evaluator. Each key gets
/// one formal slot, including final lexical cells shared by multiple closures.
struct RetainedGraphBuilder<'a, 'index> {
    context: &'a ProviderContext<'index>,
    slots: BTreeMap<GraphKey, usize>,
    pending: Vec<GraphKey>,
    nodes: Vec<ProviderGraphNode>,
    current_generation: Option<(HeapAddress, CurrentOwnPropertyProjection)>,
}

impl<'a, 'index> RetainedGraphBuilder<'a, 'index> {
    fn new(context: &'a ProviderContext<'index>, selected: &SelectedValue) -> Self {
        let current_generation = selected.postcondition.as_ref().and_then(|proof| {
            let projection = proof.current_projection(context, &selected.values)?;
            let AbstractValue::Value(Value::Alloc(Kind::Object, address)) =
                selected.values.first()?
            else {
                return None;
            };
            Some((*address, projection))
        });
        Self {
            context,
            slots: BTreeMap::new(),
            pending: Vec::new(),
            nodes: Vec::new(),
            current_generation,
        }
    }

    fn slot(
        &mut self,
        key: GraphKey,
        kind: ProviderGraphNodeKind,
    ) -> Result<ProviderGraphValue, PrivateHeapRefusal> {
        let slot = if let Some(slot) = self.slots.get(&key) {
            *slot
        } else {
            if self.nodes.len() >= 512 {
                return Err(PrivateHeapRefusal::UnrepresentedValue);
            }
            let slot = self.nodes.len();
            self.nodes.push(ProviderGraphNode {
                kind,
                edges: BTreeSet::new(),
            });
            self.slots.insert(key.clone(), slot);
            self.pending.push(key);
            slot
        };
        Ok(ProviderGraphValue::Slot { slot })
    }

    fn value(&mut self, value: &AbstractValue) -> Result<ProviderGraphValue, PrivateHeapRefusal> {
        Ok(match value {
            AbstractValue::Value(Value::Prim(kinds)) => {
                ProviderGraphValue::Primitive { kinds: *kinds }
            }
            AbstractValue::SymbolicInput(input) if input.scope == self.context.scope => {
                let projected = match input.input {
                    ProviderInput::Argument(index) => ProviderGraphInput::Argument { index },
                    role @ (ProviderInput::Property { argument, property }
                    | ProviderInput::Getter { argument, property }
                    | ProviderInput::Setter { argument, property }) => {
                        let property = self
                            .context
                            .input_shape
                            .as_ref()
                            .and_then(|shape| shape.properties.keys().nth(property))
                            .ok_or(PrivateHeapRefusal::UnrepresentedValue)?
                            .clone();
                        match role {
                            ProviderInput::Property { .. } => {
                                ProviderGraphInput::Property { argument, property }
                            }
                            ProviderInput::Getter { .. } => {
                                ProviderGraphInput::Getter { argument, property }
                            }
                            ProviderInput::Setter { .. } => {
                                ProviderGraphInput::Setter { argument, property }
                            }
                            _ => unreachable!(),
                        }
                    }
                    ProviderInput::Capture(_) => {
                        return Err(PrivateHeapRefusal::UnrepresentedValue);
                    }
                };
                ProviderGraphValue::Input { input: projected }
            }
            AbstractValue::Value(Value::Alloc(Kind::Object, address)) => {
                let kind = if self.context.arrays.contains_key(address) {
                    ProviderGraphNodeKind::Array
                } else {
                    ProviderGraphNodeKind::Record
                };
                self.slot(GraphKey::Heap(*address), kind)?
            }
            AbstractValue::OwnDescriptorMap(address) => self.slot(
                GraphKey::Heap(*address),
                ProviderGraphNodeKind::DescriptorMap,
            )?,
            AbstractValue::Value(Value::Alloc(Kind::Function, address)) => self.slot(
                GraphKey::Heap(*address),
                ProviderGraphNodeKind::Closure {
                    source_key: self
                        .context
                        .index
                        .heap_resolver
                        .source_origin(*address)
                        .ok_or(PrivateHeapRefusal::BorrowedOrForeign)?,
                },
            )?,
            _ => return Err(PrivateHeapRefusal::UnrepresentedValue),
        })
    }

    fn edges(
        &mut self,
        slot: usize,
        role: ProviderGraphRole,
        values: &Values,
    ) -> Result<(), PrivateHeapRefusal> {
        for value in values {
            let value = self.value(value)?;
            self.nodes[slot].edges.insert(ProviderGraphEdge {
                role: role.clone(),
                value,
            });
        }
        Ok(())
    }

    fn descriptor(
        &mut self,
        slot: usize,
        property: &str,
        descriptor: &CapturedPropertyDescriptor,
    ) -> Result<(), PrivateHeapRefusal> {
        match descriptor {
            CapturedPropertyDescriptor::Data(values) => self.edges(
                slot,
                ProviderGraphRole::OwnData {
                    property: property.to_owned(),
                },
                values,
            ),
            CapturedPropertyDescriptor::Accessor { getter, setter } => {
                self.edges(
                    slot,
                    ProviderGraphRole::Getter {
                        property: property.to_owned(),
                    },
                    getter,
                )?;
                self.edges(
                    slot,
                    ProviderGraphRole::Setter {
                        property: property.to_owned(),
                    },
                    setter,
                )
            }
        }
    }

    fn build(
        mut self,
        values: &Values,
        touched: &BTreeSet<HeapAddress>,
    ) -> Result<(ProviderRetainedGraph, bool, BTreeMap<GraphKey, usize>), PrivateHeapRefusal> {
        let roots = values
            .iter()
            .map(|value| self.value(value))
            .collect::<Result<_, _>>()?;
        let context = self.context;
        while let Some(key) = self.pending.pop() {
            let slot = self.slots[&key];
            match key {
                GraphKey::Cell(binding) => self.edges(
                    slot,
                    ProviderGraphRole::CellValue,
                    context
                        .final_environment
                        .as_ref()
                        .and_then(|environment| environment.get(&binding))
                        .ok_or(PrivateHeapRefusal::UnrepresentedValue)?,
                )?,
                GraphKey::Heap(address) => match &self.nodes[slot].kind.clone() {
                    ProviderGraphNodeKind::Record | ProviderGraphNodeKind::Array => {
                        if let Some((_, projection)) = self
                            .current_generation
                            .as_ref()
                            .filter(|(target, _)| *target == address)
                        {
                            let descriptors = projection.descriptors.clone();
                            for (property, descriptor) in descriptors {
                                match descriptor {
                                    CurrentOwnPropertyDescriptor::Data { values, .. } => self
                                        .edges(
                                            slot,
                                            ProviderGraphRole::OwnData { property },
                                            &values,
                                        )?,
                                    CurrentOwnPropertyDescriptor::Accessor {
                                        getter,
                                        setter,
                                        ..
                                    } => {
                                        self.edges(
                                            slot,
                                            ProviderGraphRole::Getter {
                                                property: property.clone(),
                                            },
                                            &getter,
                                        )?;
                                        self.edges(
                                            slot,
                                            ProviderGraphRole::Setter { property },
                                            &setter,
                                        )?;
                                    }
                                }
                            }
                            continue;
                        }
                        if let Some(properties) = context.surface.own_properties(address) {
                            for (property, values) in properties {
                                let role = match property {
                                    Some(property) => ProviderGraphRole::OwnData {
                                        property: property.clone(),
                                    },
                                    None => ProviderGraphRole::Retained,
                                };
                                self.edges(slot, role, values)?;
                            }
                        }
                        if let Some(accessors) = context.surface.applied_accessors.get(&address) {
                            for (property, accessor) in accessors {
                                self.edges(
                                    slot,
                                    ProviderGraphRole::Getter {
                                        property: property.clone(),
                                    },
                                    &accessor.getter,
                                )?;
                                self.edges(
                                    slot,
                                    ProviderGraphRole::Setter {
                                        property: property.clone(),
                                    },
                                    &accessor.setter,
                                )?;
                            }
                        }
                        if let Some(array) = context.arrays.get(&address) {
                            for (index, values) in array.slots.iter().enumerate() {
                                self.edges(
                                    slot,
                                    ProviderGraphRole::ArrayElement { index },
                                    values,
                                )?;
                            }
                        }
                    }
                    ProviderGraphNodeKind::DescriptorMap => {
                        let map = context
                            .descriptor_maps
                            .get(&address)
                            .ok_or(PrivateHeapRefusal::UnrepresentedValue)?;
                        for (property, descriptor) in &map.properties {
                            self.descriptor(slot, property, descriptor)?;
                        }
                        self.edges(slot, ProviderGraphRole::Retained, &map.retained_values)?;
                    }
                    ProviderGraphNodeKind::Closure { source_key } => {
                        self.edges(
                            slot,
                            ProviderGraphRole::Retained,
                            context
                                .closure_captures
                                .get(source_key)
                                .ok_or(PrivateHeapRefusal::UnrepresentedValue)?,
                        )?;
                        let locals = context
                            .closure_local_reads
                            .get(source_key)
                            .ok_or(PrivateHeapRefusal::UnrepresentedValue)?;
                        let captures = context
                            .closure_provider_reads
                            .get(source_key)
                            .ok_or(PrivateHeapRefusal::UnrepresentedValue)?;
                        for (index, binding) in locals.iter().chain(captures).enumerate() {
                            let value = self.slot(
                                GraphKey::Cell(binding.clone()),
                                ProviderGraphNodeKind::LexicalCell,
                            )?;
                            self.nodes[slot].edges.insert(ProviderGraphEdge {
                                role: ProviderGraphRole::ClosureCell { index },
                                value,
                            });
                        }
                    }
                    ProviderGraphNodeKind::LexicalCell => {
                        return Err(PrivateHeapRefusal::UnrepresentedValue);
                    }
                },
            }
        }
        let touches_private = self
            .slots
            .keys()
            .any(|key| matches!(key, GraphKey::Heap(address) if touched.contains(address)));
        Ok((
            ProviderRetainedGraph {
                roots,
                nodes: self.nodes,
            },
            touches_private,
            self.slots,
        ))
    }
}
