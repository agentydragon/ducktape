//! Source-qualified preparation and chunk views of the existing flow rules.
//!
//! This is a may-origin relation. Imports retain an opaque alternative; it
//! supplies neither foreign-call effects nor initialization/completion proofs.

use super::*;
use crate::program_bindings::{ProgramBindings, SourceModuleKey};
use roaring::RoaringBitmap;

pub(crate) struct ProgramFlowModule<'a> {
    pub module: &'a str,
    pub body: &'a [TopLevelItemView<'a>],
    pub native_operation_inputs: &'a [crate::analysis_inputs::ResolvedNativeOperationInput],
    pub inferred_sets: &'a BTreeMap<u32, crate::analysis_inputs::InferredSetConstruction>,
}

/// Transient completed source flow. Saved analysis persists the derived chunk
/// facts instead; loading those facts never starts another flow solve.
#[derive(Default)]
pub struct ProgramValueFlows {
    flows: BTreeMap<SourceModuleKey, Arc<ChunkValueFlow>>,
    cell_effects: BTreeMap<FlowKey, KnownCellAccesses>,
    module_bindings: BTreeMap<(SourceModuleKey, String), BindingSlot>,
}

impl ProgramValueFlows {
    pub(crate) fn get(&self, module: &str) -> Option<&Arc<ChunkValueFlow>> {
        self.flows.get(module)
    }

    /// Positive obligations for the existing cyclic-export fallback. Returned
    /// and other deferred descendants remain possible, never observed effects.
    pub(crate) fn take_importer_cell_reads(
        &mut self,
        bindings: &ProgramBindings,
    ) -> BTreeMap<SourceModuleKey, BTreeMap<String, BTreeSet<String>>> {
        let Some(flow) = self.flows.values().next() else {
            return BTreeMap::new();
        };
        let shared = &flow.program.as_ref().expect("program flow view").shared;
        // This is the last access-summary consumer. Retire the summaries and
        // source-name index here, before per-chunk analysis starts.
        let cell_effects = std::mem::take(&mut self.cell_effects);
        let module_bindings = std::mem::take(&mut self.module_bindings);
        let slot = |binding| module_bindings.get(bindings.binding_name(binding)).copied();
        let mut aliases = HashMap::new();
        let mut users: BTreeMap<u32, BTreeMap<String, BTreeSet<String>>> = BTreeMap::new();
        for module in bindings.modules() {
            for (import, _) in bindings.imports(module) {
                let Some(import_slot) = slot(import) else {
                    continue;
                };
                if let Some(source) = bindings.source_cell(import).and_then(slot) {
                    aliases.insert(import_slot.0, source.0);
                }
                if let Some(values) = shared.values.get(&FlowKey::Binding(import_slot)) {
                    for value in values {
                        if let Value::Alloc(Kind::Function, function) = value {
                            users
                                .entry(function)
                                .or_default()
                                .entry(module.clone())
                                .or_default()
                                .insert(bindings.binding_name(import).1.clone());
                        }
                    }
                }
            }
        }
        let mut exported: BTreeMap<String, BTreeMap<u32, BTreeSet<String>>> = BTreeMap::new();
        for export in bindings.exports() {
            let Some(local) = bindings.local_export(export) else {
                continue;
            };
            let Some(cell) = bindings.source_cell(local).and_then(slot) else {
                continue;
            };
            let (module, name) = bindings.binding_name(local);
            exported
                .entry(module.clone())
                .or_default()
                .entry(cell.0)
                .or_default()
                .insert(name.clone());
        }
        let masks: BTreeMap<_, RoaringBitmap> = exported
            .iter()
            .map(|(module, cells)| (module, cells.keys().copied().collect()))
            .collect();
        let mut output: BTreeMap<String, BTreeMap<String, BTreeSet<String>>> = BTreeMap::new();
        for (function, consumers) in users {
            let Some(accesses) = cell_effects.get(&FlowKey::RetainedCells(function)) else {
                continue;
            };
            // Normalize a shared summary once, regardless of how many imports
            // alias this function. Imported cells retain source-cell identity.
            let cells: RoaringBitmap = accesses
                .reads
                .iter()
                .chain(accesses.writes.iter())
                .map(|slot| aliases.get(&slot).copied().unwrap_or(slot))
                .collect();
            for (module, callees) in consumers {
                let Some(exports) = exported.get(&module) else {
                    continue;
                };
                let mut names = BTreeSet::new();
                for_each_bitmap_intersection(&cells, &masks[&module], |cell| {
                    names.extend(exports[&cell].iter().cloned());
                });
                if !names.is_empty() {
                    for callee in callees {
                        output
                            .entry(module.clone())
                            .or_default()
                            .entry(callee)
                            .or_default()
                            .extend(names.iter().cloned());
                    }
                }
            }
        }
        output
    }

    /// Finish a source census with the same program flow used by capability
    /// and purity preparation. Missing flow is not a local-analysis fallback.
    pub fn complete_chunk<'a>(
        &self,
        module: &str,
        source: crate::facts::StructuralChunkSourceFacts<'a>,
        arguments: &[crate::analysis_inputs::ResolvedArgumentInput],
    ) -> Option<crate::facts::StructuralChunkAnalysis<'a>> {
        let flow = self.flows.get(module)?.clone();
        Some(super::super::analyze::complete_chunk_structural_with_flow(
            source, arguments, flow,
        ))
    }
}

/// One source occurrence, interned before solving. Local byte offsets and SWC
/// binding IDs are never compared across source modules.
#[derive(Debug, Default)]
#[cfg_attr(test, derive(PartialEq, Eq))]
struct Sites {
    sources: Vec<(usize, u32)>,
}

#[derive(Debug)]
#[cfg_attr(test, derive(PartialEq, Eq))]
struct ProgramFlowFacts {
    origins: Arc<OriginTable>,
    sites: Sites,
    values: BTreeMap<FlowKey, Values>,
    reachability: BTreeMap<OriginId, Arc<RoaringBitmap>>,
    property_slots: HashMap<String, PropertySlot>,
    declared: Vec<bool>,
}

/// Identity of an immutable completed value-set node, including its separate
/// opaque alternative. The shared completed store keeps every keyed bitmap
/// alive; transient query results are never inserted into this translation map.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct CompletedSet(usize, bool);

impl CompletedSet {
    fn of(values: &Values) -> Self {
        Self(Arc::as_ptr(&values.bitmap) as usize, values.unknown)
    }
}

/// A source's view retains the shared completed store for expression queries.
/// Foreign values become Top at the local API boundary, with any reachable
/// local candidate roots retained. They never masquerade as local byte offsets.
#[derive(Debug, Clone)]
#[cfg_attr(test, derive(PartialEq, Eq))]
pub(super) struct ChunkProgramFlow {
    shared: Arc<ProgramFlowFacts>,
    module: usize,
    bindings: HashMap<Id, BindingSlot>,
    sites: BTreeMap<u32, u32>,
    candidates: BTreeMap<u32, u32>,
    projected: HashMap<CompletedSet, Values>,
}

impl ChunkProgramFlow {
    fn candidate_bits(&self, bits: &RoaringBitmap) -> RoaringBitmap {
        if bits.len() <= self.candidates.len() as u64 {
            bits.iter()
                .filter_map(|global| self.candidates.get(&global).copied())
                .collect()
        } else {
            self.candidates
                .iter()
                .filter_map(|(global, local)| bits.contains(*global).then_some(*local))
                .collect()
        }
    }

    fn project(&self, values: &Values, flow: &ChunkValueFlow) -> Values {
        self.projected
            .get(&CompletedSet::of(values))
            .cloned()
            .unwrap_or_else(|| self.translate(values, flow))
    }

    /// Each completed set is translated on its first local fact consumer, once
    /// per source view. Retain that translation exactly as long as the view;
    /// later expression queries can reuse the same immutable node.
    fn prepare_projection(&mut self, values: &Values, flow: &ChunkValueFlow) -> Values {
        let key = CompletedSet::of(values);
        if let Some(projected) = self.projected.get(&key) {
            return projected.clone();
        }
        let projected = self.translate(values, flow);
        self.projected.insert(key, projected.clone());
        projected
    }

    fn translate(&self, values: &Values, flow: &ChunkValueFlow) -> Values {
        let mut result = Values::empty(flow.origin_table.clone());
        for value in values {
            match value {
                Value::Alloc(kind, site) => {
                    let (owner, offset) = self.shared.sites.sources[site as usize];
                    if owner == self.module {
                        result.insert(Value::Alloc(kind, offset));
                    } else {
                        result.insert(Value::Top);
                        // A foreign carrier may contain a local object. Losing
                        // that reach here would hide escape/publication paths.
                        if let Some(bits) =
                            self.shared.reachability.get(&self.shared.origins.id(value))
                        {
                            for index in self.candidate_bits(bits) {
                                result.insert(flow.candidate_objects[index as usize]);
                            }
                        }
                    }
                }
                other => {
                    result.insert(other);
                }
            }
        }
        result
    }

    pub(super) fn values_of_expr(&self, expr: &Expr, flow: &ChunkValueFlow) -> Values {
        if let Expr::New(construction) = expr.unwrap_parens()
            && flow
                .native_set_allocations
                .contains_key(&construction.span.lo.0)
        {
            return flow.singleton_value(Value::Alloc(Kind::Set, construction.span.lo.0));
        }
        let mut symbols = FlowSymbols::default();
        let mut expr = FlowValueExpr::from_ast(expr, &mut symbols);
        let mapping = Rekey {
            bindings: symbols
                .bindings
                .iter()
                .map(|id| self.bindings.get(id).copied())
                .collect(),
            properties: symbols
                .properties
                .iter()
                .map(|name| self.shared.property_slots.get(name).copied())
                .collect(),
            sites: &self.sites,
        };
        mapping.expression(&mut expr);
        let mut result = ValuesBuilder::new(self.shared.origins.clone());
        append_value_expr(&expr, &mut result, &self.shared.declared, &mut |key| {
            self.shared.values.get(&key).cloned()
        });
        self.project(&result.finish(), flow)
    }

    pub(super) fn values_of_property(&self, object: u32, property: &str) -> Option<&Values> {
        let site = *self.sites.get(&object)?;
        let property = *self.shared.property_slots.get(property)?;
        let values = self.shared.values.get(&FlowKey::Property(site, property))?;
        self.projected.get(&CompletedSet::of(values))
    }
}

/// Coordinate conversion only: every semantic event/transfer remains owned by
/// FlowProgramCollector, FlowRules and CandidateRules.
struct Rekey<'a> {
    bindings: Vec<Option<BindingSlot>>,
    properties: Vec<Option<PropertySlot>>,
    sites: &'a BTreeMap<u32, u32>,
}

impl Rekey<'_> {
    fn binding(&self, slot: BindingSlot) -> BindingSlot {
        self.bindings[slot.0 as usize].expect("prepared binding slot")
    }

    fn property(&self, slot: PropertySlot) -> PropertySlot {
        self.properties[slot.0 as usize].expect("prepared property slot")
    }

    fn site(&self, site: u32) -> u32 {
        self.sites[&site]
    }

    fn value(&self, value: Value) -> Value {
        match value {
            Value::Alloc(kind, site) => Value::Alloc(kind, self.site(site)),
            other => other,
        }
    }

    fn expression(&self, expression: &mut FlowValueExpr) {
        let mut pending = vec![expression];
        while let Some(expr) = pending.pop() {
            match expr {
                FlowValueExpr::Binding(slot) => {
                    *expr = self.bindings[slot.0 as usize]
                        .map_or(FlowValueExpr::Unknown, FlowValueExpr::Binding);
                }
                FlowValueExpr::Value(value) => {
                    *expr = match *value {
                        Value::Alloc(kind, site) => self
                            .sites
                            .get(&site)
                            .map_or(FlowValueExpr::Unknown, |site| {
                                FlowValueExpr::Value(Value::Alloc(kind, *site))
                            }),
                        other => FlowValueExpr::Value(other),
                    };
                }
                FlowValueExpr::Member { object, property } => {
                    *property = property.and_then(|slot| self.properties[slot.0 as usize]);
                    pending.push(object);
                }
                FlowValueExpr::Call(callee) => pending.push(callee),
                FlowValueExpr::Union(left, right) => {
                    pending.push(left);
                    pending.push(right);
                }
                FlowValueExpr::Empty | FlowValueExpr::Unknown => {}
            }
        }
    }

    fn event(&self, event: &mut FlowEvent) {
        match event {
            FlowEvent::Initialization { id, sites } => {
                *id = self.binding(*id);
                sites.rekey(|site| self.site(site));
            }
            FlowEvent::FunctionDeclaration { id, origin }
            | FlowEvent::ClassDeclaration { id, origin } => {
                *id = self.binding(*id);
                *origin = self.value(*origin);
            }
            FlowEvent::Variable { id, init } | FlowEvent::AssignBinding { id, right: init } => {
                *id = self.binding(*id);
                self.expression(init);
            }
            FlowEvent::AssignProperty {
                object,
                property,
                right,
            } => {
                self.expression(object);
                *property = self.property(*property);
                self.expression(right);
            }
            FlowEvent::WritePropertyExpr {
                object,
                property,
                value,
            } => {
                *object = self.site(*object);
                *property = self.property(*property);
                self.expression(value);
            }
            FlowEvent::WritePropertyBinding {
                object,
                property,
                binding,
            } => {
                *object = self.site(*object);
                *property = self.property(*property);
                *binding = self.binding(*binding);
            }
            FlowEvent::WritePropertyValue {
                object,
                property,
                value,
            } => {
                *object = self.site(*object);
                *property = self.property(*property);
                *value = self.value(*value);
            }
            FlowEvent::FunctionMetadata { function, params } => {
                *function = self.site(*function);
                for param in params.iter_mut().flatten() {
                    *param = self.binding(*param);
                }
            }
            FlowEvent::Return { function, value } => {
                *function = self.site(*function);
                self.expression(value);
            }
            FlowEvent::Invoke { callee, args } => {
                self.expression(callee);
                for arg in args {
                    self.expression(arg);
                }
            }
        }
    }
}

pub(crate) fn program_value_flows<'a>(
    bindings: &ProgramBindings,
    modules: impl IntoIterator<Item = ProgramFlowModule<'a>>,
) -> ProgramValueFlows {
    let mut programs = Vec::new();
    let mut names = Vec::new();
    for module in modules {
        programs.push(FlowProgram::structural(
            module.body,
            module.native_operation_inputs,
            module.inferred_sets,
        ));
        names.push(module.module.to_owned());
    }
    let mut sites = Sites::default();
    let mut site_maps = Vec::new();
    let mut binding_maps = Vec::new();
    let mut declared = Vec::new();
    let mut property_names = FlowSymbols::default();
    let mut origins = Vec::new();
    let mut candidates = Vec::new();
    let mut candidate_maps = Vec::new();
    let mut native_sets = BTreeSet::new();
    let mut events = Vec::new();
    let mut linkage = BTreeMap::new();
    for (module, program) in programs.iter_mut().enumerate() {
        let mut source_sites = BTreeMap::new();
        for origin in &program.origin_table.origins {
            if let Value::Alloc(_, offset) = origin {
                source_sites.entry(*offset).or_insert_with(|| {
                    let id = u32::try_from(sites.sources.len())
                        .expect("program allocation sites exceed u32");
                    sites.sources.push((module, *offset));
                    id
                });
            }
        }
        let mut local_bindings = Vec::new();
        for id in &program.symbols.bindings {
            let slot = BindingSlot(
                u32::try_from(declared.len()).expect("program binding slots exceed u32"),
            );
            local_bindings.push(Some(slot));
            declared.push(program.declared_ids.contains(id));
        }
        let properties = program
            .symbols
            .properties
            .iter()
            .map(|name| Some(property_names.property(name.clone())))
            .collect();
        let mapping = Rekey {
            bindings: local_bindings,
            properties,
            sites: &source_sites,
        };
        origins.extend(
            program
                .origin_table
                .origins
                .iter()
                .map(|value| mapping.value(*value)),
        );
        let mut local_candidates = BTreeMap::new();
        for (local, value) in program.candidate_objects.iter().enumerate() {
            local_candidates.insert(
                u32::try_from(candidates.len()).expect("program candidate count exceeds u32"),
                u32::try_from(local).expect("source candidate count exceeds u32"),
            );
            candidates.push(mapping.value(*value));
        }
        native_sets.extend(
            program
                .native_set_allocations
                .keys()
                .map(|site| mapping.site(*site)),
        );
        for (local, slot) in &program.module_bindings {
            linkage.insert(
                (names[module].clone(), local.clone()),
                mapping.binding(*slot),
            );
        }
        for event in &mut program.events {
            mapping.event(event);
        }
        let local_effects = std::mem::take(&mut program.cell_effects);
        program.cell_effects = local_effects
            .into_iter()
            .map(|(function, mut body)| {
                body.reads = body
                    .reads
                    .into_iter()
                    .map(|slot| mapping.binding(slot))
                    .collect();
                body.read_only_returns = body.read_only_returns.map(|returned| {
                    returned
                        .into_iter()
                        .map(|slot| mapping.binding(slot))
                        .collect()
                });
                body.writes = body
                    .writes
                    .into_iter()
                    .map(|slot| mapping.binding(slot))
                    .collect();
                body.descendants = body
                    .descendants
                    .into_iter()
                    .map(|site| mapping.site(site))
                    .collect();
                for call in &mut body.calls {
                    *call += events.len();
                }
                (mapping.site(function), body)
            })
            .collect();
        events.append(&mut program.events);
        // Preserve the previous opaque-import alternative even after a source
        // identity arrives. May-flow is not a foreign execution certificate.
        events.extend(program.imports.iter().map(|slot| FlowEvent::AssignBinding {
            id: mapping.binding(*slot),
            right: FlowValueExpr::Unknown,
        }));
        binding_maps.push(mapping.bindings);
        site_maps.push(source_sites);
        candidate_maps.push(local_candidates);
    }
    for module in bindings.modules() {
        for (import, export) in bindings.imports(module) {
            let Some(provider) = bindings.local_export(export) else {
                continue;
            };
            if let (Some(import), Some(provider)) = (
                linkage.get(bindings.binding_name(import)),
                linkage.get(bindings.binding_name(provider)),
            ) {
                events.push(FlowEvent::AssignBinding {
                    id: *import,
                    right: FlowValueExpr::Binding(*provider),
                });
            }
        }
    }
    let origins = Arc::new(OriginTable::from_origins(origins));
    let cell_effects = programs
        .iter_mut()
        .flat_map(|program| std::mem::take(&mut program.cell_effects))
        .collect();
    let (facts, _) = solve_flow(
        events,
        declared.clone(),
        origins.clone(),
        &candidates,
        native_sets,
        cell_effects,
    );
    let mut values = BTreeMap::new();
    let mut reachability = BTreeMap::new();
    let mut candidate_bindings = BTreeMap::new();
    let mut parameters = BTreeMap::new();
    let mut cell_effects = BTreeMap::new();
    let mut initializations = BTreeMap::new();
    let mut read_only_callbacks = BTreeMap::new();
    // Move completed facts into their read indexes. Do not keep a second full
    // value-key map alive while constructing source projections.
    for (key, fact) in facts {
        match (key, fact) {
            (key, FlowFact::Values(value)) => {
                values.insert(key, value);
            }
            (FlowKey::CandidateReachable(origin), FlowFact::CandidateReachability(bits)) => {
                reachability.insert(origin, bits);
            }
            (FlowKey::CandidateBinding(binding), FlowFact::CandidateReachability(bits)) => {
                candidate_bindings.insert(binding, bits);
            }
            (FlowKey::Parameters(function), FlowFact::Parameters(params)) => {
                parameters.insert(function, params);
            }
            (key, FlowFact::Cells(accesses)) => {
                cell_effects.insert(key, accesses);
            }
            (FlowKey::ReadOnlyCallback(function), FlowFact::ReadOnlyCallback(summary)) => {
                read_only_callbacks.insert(function, summary);
            }
            (FlowKey::Initialization(binding), FlowFact::Initialization(sites)) => {
                initializations.insert(binding, sites);
            }
            _ => {}
        }
    }
    let shared = Arc::new(ProgramFlowFacts {
        origins,
        sites,
        values,
        reachability,
        property_slots: property_names.property_slots,
        declared,
    });
    let mut outputs = BTreeMap::new();
    for (module, program) in programs.into_iter().enumerate() {
        let mut view = ChunkProgramFlow {
            projected: HashMap::new(),
            shared: shared.clone(),
            module,
            bindings: program
                .symbols
                .bindings
                .iter()
                .zip(&binding_maps[module])
                .map(|(id, slot)| (id.clone(), slot.expect("prepared source binding")))
                .collect(),
            sites: std::mem::take(&mut site_maps[module]),
            candidates: std::mem::take(&mut candidate_maps[module]),
        };
        let mut flow = ChunkValueFlow {
            origin_table: program.origin_table,
            declared_ids: program.declared_ids,
            canonical: program.canonical,
            callable_source_unit: program.callable_source_unit,
            native_set_allocations: program.native_set_allocations,
            fresh_returned_record_keys: program.fresh_returned_record_keys,
            candidate_objects: program.candidate_objects,
            ..ChunkValueFlow::default()
        };
        for (local, global) in program.symbols.bindings.iter().zip(&binding_maps[module]) {
            let global = global.expect("prepared source binding");
            if let Some(values) = shared.values.get(&FlowKey::Binding(global)) {
                flow.bindings
                    .insert(local.clone(), view.prepare_projection(values, &flow));
            }
            if let Some(bits) = candidate_bindings.get(&global) {
                flow.candidate_bindings
                    .insert(local.clone(), Arc::new(view.candidate_bits(bits)));
            }
            if let Some(sites) = initializations.remove(&global) {
                let mut sites = sites;
                sites.rekey(|site| shared.sites.sources[site as usize].1);
                flow.initializations.insert(local.clone(), sites);
            }
        }
        let inverse: BTreeMap<_, _> = view
            .bindings
            .iter()
            .map(|(id, slot)| (*slot, id.clone()))
            .collect();
        let local_sites = view.sites.clone();
        for (offset, site) in &local_sites {
            if let Some(summary) = read_only_callbacks.remove(site) {
                flow.read_only_callbacks
                    .insert(*offset, summary.map(|slot| inverse[slot].clone()));
            }
            if let Some(values) = shared.values.get(&FlowKey::Properties(*site)) {
                flow.property_unions
                    .insert(*offset, view.prepare_projection(values, &flow));
            }
            if let Some(values) = shared.values.get(&FlowKey::Return(*site)) {
                flow.returns
                    .insert(*offset, view.prepare_projection(values, &flow));
            }
            if let Some(params) = parameters.get(site) {
                flow.params.insert(
                    *offset,
                    params
                        .iter()
                        .map(|param| param.and_then(|param| inverse.get(&param).cloned()))
                        .collect(),
                );
            }
        }
        for site in local_sites.values() {
            // Ordered range lookup projects existing property facts only;
            // there is no source-count x program-fact scan or sites x names table.
            for (key, values) in shared.values.range(
                FlowKey::Property(*site, PropertySlot(0))
                    ..=FlowKey::Property(*site, PropertySlot(u32::MAX)),
            ) {
                let FlowKey::Property(_, _) = key else {
                    unreachable!("property range");
                };
                // Keep the translated value in the shared projection cache;
                // property reads resolve their slot through the source view
                // instead of duplicating every property name/value row in a
                // per-chunk HashMap.
                let _ = view.prepare_projection(values, &flow);
            }
        }
        for origin in &flow.origin_table.origins {
            let global = match *origin {
                Value::Alloc(kind, offset) => Value::Alloc(kind, view.sites[&offset]),
                other => other,
            };
            if let Some(bits) = shared.reachability.get(&shared.origins.id(global)) {
                flow.candidate_reachability
                    .insert(*origin, Arc::new(view.candidate_bits(bits)));
            }
        }
        flow.program = Some(view);
        outputs.insert(names[module].clone(), Arc::new(flow));
    }
    ProgramValueFlows {
        flows: outputs,
        cell_effects,
        module_bindings: linkage,
    }
}

#[cfg(test)]
#[path = "program_tests.rs"]
mod tests;
