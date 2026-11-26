//! Source-qualified preparation and chunk views of the existing flow rules.
//!
//! This is a may-origin relation. Imports retain an opaque alternative; it
//! supplies neither foreign-call effects nor initialization/completion proofs.

use super::*;
use crate::program_bindings::{ProgramBindings, SourceModuleKey};
use roaring::RoaringBitmap;

pub struct ProgramFlowModule<'a> {
    pub module: &'a str,
    pub body: &'a [TopLevelItemView<'a>],
    pub fresh_factory_imports: &'a BTreeSet<String>,
    pub fresh_factory_members: &'a BTreeMap<String, BTreeSet<String>>,
    pub native_operation_inputs: &'a [analysis_vocabulary::inputs::ResolvedNativeOperationInput],
    pub inferred_sets: &'a BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
}

/// Transient completed source flow. Saved analysis persists the derived chunk
/// facts instead; loading those facts never starts another flow solve.
#[derive(Default)]
pub struct ProgramValueFlows {
    flows: BTreeMap<SourceModuleKey, Arc<ChunkValueFlow>>,
    cell_effects: BTreeMap<FlowKey, KnownCellAccesses>,
    module_bindings: BTreeMap<(SourceModuleKey, String), BindingSlot>,
    #[cfg(test)]
    flow_work_stats: super::FlowWorkStats,
}

impl ProgramValueFlows {
    pub fn get(&self, module: &str) -> Option<&Arc<ChunkValueFlow>> {
        self.flows.get(module)
    }

    /// Positive obligations for the existing cyclic-export fallback. Returned
    /// and other deferred descendants remain possible, never observed effects.
    pub fn take_importer_cell_reads(
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
        arguments: &[analysis_vocabulary::inputs::ResolvedArgumentInput],
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
    values: CompletedValueStore,
    #[cfg(test)]
    ordered_value_oracle: BTreeMap<FlowKey, Values>,
    reachability: BTreeMap<OriginId, Arc<RoaringBitmap>>,
    property_slots: HashMap<String, PropertySlot>,
    declared: Vec<bool>,
    return_summaries: Arc<BTreeMap<u32, ReturnSummary>>,
    importer_allocations: Arc<Vec<RoaringBitmap>>,
}

/// Read index for the immutable value facts left after the shared solve.
/// Binding and site IDs are dense; static property rows stay sparse and sorted.
/// Values move here once, so the original bitmap Arcs remain the sole payloads.
#[derive(Debug, Default)]
#[cfg_attr(test, derive(PartialEq, Eq))]
struct CompletedValueStore {
    bindings: Vec<Option<Values>>,
    sites: Vec<CompletedSiteValues>,
    property_ranges: Vec<PropertyRange>,
    property_keys: Vec<PropertySlot>,
    property_values: Vec<Values>,
    last_property_key: Option<(u32, PropertySlot)>,
}

#[derive(Debug, Default, Clone, Copy)]
#[cfg_attr(test, derive(PartialEq, Eq))]
struct PropertyRange {
    start: usize,
    end: usize,
}

#[derive(Debug, Default)]
#[cfg_attr(test, derive(PartialEq, Eq))]
struct CompletedSiteValues {
    all_properties: Option<Values>,
    numeric_properties: Option<Values>,
    returns: Option<Values>,
}

impl CompletedValueStore {
    fn with_domains(binding_count: usize, site_count: usize) -> Self {
        Self {
            bindings: std::iter::repeat_with(|| None)
                .take(binding_count)
                .collect(),
            sites: (0..site_count)
                .map(|_| CompletedSiteValues::default())
                .collect(),
            property_ranges: vec![PropertyRange::default(); site_count],
            property_keys: Vec::new(),
            property_values: Vec::new(),
            last_property_key: None,
        }
    }

    fn insert(&mut self, key: FlowKey, values: Values) {
        match key {
            FlowKey::Binding(binding) => {
                let slot = &mut self.bindings[binding.0 as usize];
                assert!(slot.is_none(), "duplicate completed binding value");
                *slot = Some(values);
            }
            FlowKey::Property(site, property) => {
                // The solver returns facts in FlowKey order, so these sparse
                // rows arrive sorted by (site, property). Append directly to
                // the flat arrays instead of staging a second row store.
                let key = (site, property);
                if let Some(previous) = self.last_property_key {
                    assert_ne!(previous, key, "duplicate completed property value");
                    assert!(
                        previous < key,
                        "completed property values must be inserted in key order"
                    );
                }
                let index = self.property_values.len();
                let row = &mut self.property_ranges[site as usize];
                if row.start == row.end {
                    row.start = index;
                    row.end = index;
                }
                assert_eq!(
                    row.end, index,
                    "completed property rows for a site must be contiguous"
                );
                self.property_keys.push(property);
                self.property_values.push(values);
                row.end = index + 1;
                self.last_property_key = Some(key);
            }
            FlowKey::Properties(site) => {
                let summary = &mut self.sites[site as usize].all_properties;
                assert!(summary.is_none(), "duplicate completed property summary");
                *summary = Some(values);
            }
            FlowKey::NumericProperties(site) => {
                let summary = &mut self.sites[site as usize].numeric_properties;
                assert!(
                    summary.is_none(),
                    "duplicate completed numeric property summary"
                );
                *summary = Some(values);
            }
            FlowKey::Return(site) => {
                let summary = &mut self.sites[site as usize].returns;
                assert!(summary.is_none(), "duplicate completed return summary");
                *summary = Some(values);
            }
            _ => panic!("non-value flow key in completed value store: {key:?}"),
        }
    }

    fn seal(&mut self) {
        assert_eq!(self.property_keys.len(), self.property_values.len());
        let mut next_property = 0;
        for range in &self.property_ranges {
            assert!(range.start <= range.end);
            assert!(range.end <= self.property_keys.len());
            if range.start == range.end {
                continue;
            }
            assert_eq!(range.start, next_property);
            let keys = &self.property_keys[range.start..range.end];
            assert!(
                keys.windows(2).all(|pair| pair[0] < pair[1]),
                "completed property slots must be unique and sorted per site"
            );
            next_property = range.end;
        }
        assert_eq!(next_property, self.property_keys.len());
    }

    fn get(&self, key: &FlowKey) -> Option<&Values> {
        match key {
            FlowKey::Binding(binding) => self.bindings.get(binding.0 as usize)?.as_ref(),
            FlowKey::Property(site, property) => {
                let row = self.property_ranges.get(*site as usize)?;
                let keys = self.property_keys.get(row.start..row.end)?;
                let index = keys.binary_search(property).ok()?;
                self.property_values.get(row.start + index)
            }
            FlowKey::Properties(site) => self.sites.get(*site as usize)?.all_properties.as_ref(),
            FlowKey::NumericProperties(site) => {
                self.sites.get(*site as usize)?.numeric_properties.as_ref()
            }
            FlowKey::Return(site) => self.sites.get(*site as usize)?.returns.as_ref(),
            _ => None,
        }
    }

    fn properties_at(&self, site: u32) -> impl Iterator<Item = (PropertySlot, &Values)> + '_ {
        let range = self
            .property_ranges
            .get(site as usize)
            .map_or(0..0, |site| site.start..site.end);
        let keys = self.property_keys.get(range.clone()).unwrap_or(&[]);
        let values = self.property_values.get(range).unwrap_or(&[]);
        keys.iter().copied().zip(values)
    }

    #[cfg(test)]
    fn iter_values(&self) -> impl Iterator<Item = &Values> + '_ {
        self.bindings
            .iter()
            .filter_map(Option::as_ref)
            .chain(self.property_values.iter())
            .chain(self.sites.iter().flat_map(|site| {
                site.all_properties
                    .iter()
                    .chain(site.numeric_properties.iter())
                    .chain(site.returns.iter())
            }))
    }
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
    fresh_factory_imports: Arc<FreshFactoryImports>,
    bindings: HashMap<Id, BindingSlot>,
    sites: BTreeMap<u32, u32>,
    candidates: BTreeMap<u32, u32>,
    projected: HashMap<CompletedSet, Values>,
    /// Projections are keyed only by rows retained in `shared.reachability`.
    /// Those source Arcs outlive this view, so pointer identity stays valid.
    projected_reachability: HashMap<usize, Arc<RoaringBitmap>>,
    #[cfg(test)]
    projection_work: ProjectionWork,
}

#[cfg(test)]
#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct ProjectionWork {
    retained_summary_remaps: usize,
    prepared_summary_unions: HashMap<CompletedSet, usize>,
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

    fn retained_candidate_bits(&mut self, origin: OriginId) -> Option<Arc<RoaringBitmap>> {
        let bits = self.shared.reachability.get(&origin)?.clone();
        let key = Arc::as_ptr(&bits) as usize;
        if let Some(projected) = self.projected_reachability.get(&key) {
            return Some(projected.clone());
        }
        let projected = Arc::new(self.candidate_bits(&bits));
        self.projected_reachability.insert(key, projected.clone());
        #[cfg(test)]
        {
            self.projection_work.retained_summary_remaps += 1;
        }
        Some(projected)
    }

    fn prepare_retained_candidate_bits(&mut self, values: &Values) {
        for value in values {
            if let Value::Alloc(_, site) = value
                && self.shared.sites.sources[site as usize].0 != self.module
            {
                let origin = self.shared.origins.id(value);
                let Some(bits) = self.shared.reachability.get(&origin) else {
                    continue;
                };
                let key = Arc::as_ptr(bits) as usize;
                if !self.projected_reachability.contains_key(&key) {
                    let _ = self.retained_candidate_bits(origin);
                }
            }
        }
    }

    fn project(&self, values: &Values, flow: &ChunkValueFlow) -> Values {
        self.projected
            .get(&CompletedSet::of(values))
            .cloned()
            .unwrap_or_else(|| self.translate(values, flow).0)
    }

    /// Each completed set is translated on its first local fact consumer, once
    /// per source view. Retain that translation exactly as long as the view;
    /// later expression queries can reuse the same immutable node.
    fn prepare_projection(&mut self, values: &Values, flow: &ChunkValueFlow) -> Values {
        let key = CompletedSet::of(values);
        if let Some(projected) = self.projected.get(&key) {
            return projected.clone();
        }
        self.prepare_retained_candidate_bits(values);
        let (projected, summary_unions) = self.translate(values, flow);
        self.projected.insert(key, projected.clone());
        #[cfg(test)]
        self.projection_work
            .prepared_summary_unions
            .insert(key, summary_unions);
        #[cfg(not(test))]
        let _ = summary_unions;
        projected
    }

    /// Translate one value set while unioning each distinct source summary
    /// once. Candidate indices remain separate from the origin IDs stored by
    /// `Values` until the final conversion.
    fn translate(&self, values: &Values, flow: &ChunkValueFlow) -> (Values, usize) {
        let mut origins = RoaringBitmap::new();
        let mut candidates = RoaringBitmap::new();
        let mut unknown = false;
        let mut translated_summaries = HashSet::new();
        for value in values {
            match value {
                Value::Alloc(kind, site) => {
                    let (owner, offset) = self.shared.sites.sources[site as usize];
                    if owner == self.module {
                        origins.insert(flow.origin_table.id(Value::Alloc(kind, offset)).0);
                    } else {
                        unknown = true;
                        // A foreign carrier may contain a local object. Losing
                        // that reach here would hide escape/publication paths.
                        if let Some(bits) =
                            self.shared.reachability.get(&self.shared.origins.id(value))
                        {
                            let key = Arc::as_ptr(bits) as usize;
                            if translated_summaries.insert(key) {
                                if let Some(projected) = self.projected_reachability.get(&key) {
                                    candidates |= projected.as_ref();
                                } else {
                                    // Query-only summaries are scoped to this
                                    // translation and do not enter the retained
                                    // view cache.
                                    candidates |= &self.candidate_bits(bits);
                                }
                            }
                        }
                    }
                }
                Value::Top => unknown = true,
                other => {
                    origins.insert(flow.origin_table.id(other).0);
                }
            }
        }
        for index in candidates {
            let candidate = flow.candidate_objects[index as usize];
            if candidate == Value::Top {
                unknown = true;
            } else {
                origins.insert(flow.origin_table.id(candidate).0);
            }
        }
        (
            Values {
                bitmap: Arc::new(origins),
                unknown,
                origins: flow.origin_table.clone(),
            },
            translated_summaries.len(),
        )
    }

    pub(super) fn values_of_expr(&self, expr: &Expr, flow: &ChunkValueFlow) -> Values {
        if let Expr::New(construction) = expr.unwrap_parens()
            && flow
                .native_set_allocations
                .contains_key(&construction.span.lo.0)
        {
            return flow.singleton_value(Value::Alloc(Kind::Set, construction.span.lo.0));
        }
        let mut symbols = FlowSymbols {
            fresh_factory_imports: self.fresh_factory_imports.clone(),
            member_read_certificates: flow.member_read_certificates.clone(),
            ..FlowSymbols::default()
        };
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
            argument_offset: 0,
        };
        mapping.expression(&mut expr);
        for argument in &mut symbols.call_arguments {
            mapping.expression(argument);
        }
        let mut result = ValuesBuilder::new(self.shared.origins.clone());
        append_value_expr(
            &expr,
            &mut result,
            &self.shared.declared,
            &ReturnInputs {
                arguments: symbols.call_arguments,
                summaries: self.shared.return_summaries.clone(),
                default_property: self.shared.property_slots.get("default").copied(),
                importer_allocations: self.shared.importer_allocations.clone(),
            },
            &mut |key| self.shared.values.get(&key).cloned(),
            &mut |_| {},
        );
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
    argument_offset: usize,
}

impl Rekey<'_> {
    fn binding(&self, slot: BindingSlot) -> BindingSlot {
        self.bindings[slot.0 as usize].expect("prepared binding slot")
    }

    fn property(&self, slot: PropertySlot) -> PropertySlot {
        self.properties[slot.0 as usize].expect("prepared property slot")
    }

    fn return_summary(&self, summary: ReturnSummary) -> ReturnSummary {
        match summary {
            ReturnSummary::ArgumentProperty(property) => {
                ReturnSummary::ArgumentProperty(self.property(property))
            }
            ReturnSummary::ForwardChoices(helpers) => {
                ReturnSummary::ForwardChoices(helpers.map(|helper| self.binding(helper)))
            }
            other => other,
        }
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
                    *expr = self.bindings[slot.0 as usize].map_or(
                        FlowValueExpr::Unknown(SourceUnknownCause::UnavailableProjection),
                        FlowValueExpr::Binding,
                    );
                }
                FlowValueExpr::NonFunction(inner) => pending.push(inner),
                FlowValueExpr::Value(value) => {
                    *expr = match *value {
                        Value::Alloc(kind, site) => self.sites.get(&site).map_or(
                            FlowValueExpr::Unknown(SourceUnknownCause::UnavailableProjection),
                            |site| FlowValueExpr::Value(Value::Alloc(kind, *site)),
                        ),
                        other => FlowValueExpr::Value(other),
                    };
                }
                FlowValueExpr::Member { object, property } => {
                    *property = property.and_then(|slot| self.properties[slot.0 as usize]);
                    pending.push(object);
                }
                FlowValueExpr::NumericMember { object } => pending.push(object),
                FlowValueExpr::Call {
                    callee, argument, ..
                } => {
                    if let Some(argument) = argument {
                        *argument += self.argument_offset;
                    }
                    pending.push(callee);
                }
                FlowValueExpr::Union(left, right) => {
                    pending.push(left);
                    pending.push(right);
                }
                FlowValueExpr::Empty | FlowValueExpr::Unknown(_) => {}
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

pub fn program_value_flows<'a>(
    bindings: &ProgramBindings,
    modules: impl IntoIterator<Item = ProgramFlowModule<'a>>,
    diagnostics: &crate::diagnostics::Diagnostics,
) -> ProgramValueFlows {
    let mut programs = Vec::new();
    let mut names = Vec::new();
    for module in modules {
        let importer = names.len();
        let mut imports = FreshFactoryImports {
            importer,
            ..Default::default()
        };
        for specifier in module
            .body
            .iter()
            .flat_map(|item| match item.as_module_item() {
                ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import.specifiers.iter(),
                _ => [].iter(),
            })
        {
            let ImportSpecifier::Named(named) = specifier else {
                continue;
            };
            let name = named.local.sym.as_ref();
            if module.fresh_factory_imports.contains(name) {
                imports.named.insert(named.local.to_id());
            }
            if let Some(members) = module.fresh_factory_members.get(name) {
                imports.members.insert(named.local.to_id(), members.clone());
            }
        }
        programs.push(FlowProgram::structural_with_factory_imports(
            module.body,
            module.native_operation_inputs,
            module.inferred_sets,
            Arc::new(imports),
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
    let mut arguments = Vec::new();
    let mut return_summaries = BTreeMap::new();
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
            argument_offset: arguments.len(),
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
        for argument in &mut program.symbols.call_arguments {
            mapping.expression(argument);
        }
        arguments.append(&mut program.symbols.call_arguments);
        return_summaries.extend(program.return_summaries.iter().map(|(function, summary)| {
            (mapping.site(*function), mapping.return_summary(*summary))
        }));
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
            right: FlowValueExpr::Unknown(SourceUnknownCause::OpaqueImport),
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
    let mut importer_allocations = vec![RoaringBitmap::new(); names.len()];
    for (index, value) in origins.origins.iter().enumerate() {
        if let Value::Alloc(_, site) = value {
            importer_allocations[sites.sources[*site as usize].0].insert(index as u32);
        }
    }
    let importer_allocations = Arc::new(importer_allocations);
    let cell_effects = programs
        .iter_mut()
        .flat_map(|program| std::mem::take(&mut program.cell_effects))
        .collect();
    let return_summaries = Arc::new(return_summaries);
    let default_property = property_names.property_slots.get("default").copied();
    let numeric_properties = std::mem::take(&mut property_names.numeric_properties);
    let (facts, _flow_work_stats) = solve_flow_observed(
        FlowConstraints {
            events,
            returns: ReturnInputs {
                arguments,
                summaries: return_summaries.clone(),
                default_property,
                importer_allocations: importer_allocations.clone(),
            },
            declared: declared.clone(),
            numeric_properties,
        },
        origins.clone(),
        &candidates,
        native_sets,
        cell_effects,
        diagnostics,
    );
    let mut values = CompletedValueStore::with_domains(declared.len(), sites.sources.len());
    #[cfg(test)]
    let mut ordered_value_oracle = BTreeMap::new();
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
                #[cfg(test)]
                assert!(ordered_value_oracle.insert(key, value.clone()).is_none());
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
    values.seal();
    let shared = Arc::new(ProgramFlowFacts {
        origins,
        sites,
        values,
        #[cfg(test)]
        ordered_value_oracle,
        reachability,
        property_slots: property_names.property_slots,
        declared,
        return_summaries,
        importer_allocations,
    });
    let mut outputs = BTreeMap::new();
    for (module, program) in programs.into_iter().enumerate() {
        let mut view = ChunkProgramFlow {
            projected: HashMap::new(),
            projected_reachability: HashMap::new(),
            #[cfg(test)]
            projection_work: ProjectionWork::default(),
            shared: shared.clone(),
            module,
            fresh_factory_imports: program.symbols.fresh_factory_imports.clone(),
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
            property_names_by_slot: Arc::new(program.symbols.properties.clone()),
            binding_ids_by_slot: Arc::new(program.symbols.bindings.clone()),
            member_read_certificates: program.symbols.member_read_certificates.clone(),
            origin_table: program.origin_table,
            declared_ids: program.declared_ids,
            canonical: program.canonical,
            callable_source_unit: program.callable_source_unit,
            native_set_allocations: program.native_set_allocations,
            fresh_returned_record_keys: program.fresh_returned_record_keys,
            return_summaries: Arc::new(program.return_summaries),
            candidate_objects: program.candidate_objects,
            ..ChunkValueFlow::default()
        };
        // Candidate-binding summaries are construction-local. Intern their
        // projections only while this source map is alive, then discard the
        // pointer keys and cache at the end of this module's assembly.
        let mut projected_bindings = HashMap::new();
        for (local, global) in program.symbols.bindings.iter().zip(&binding_maps[module]) {
            let global = global.expect("prepared source binding");
            if let Some(values) = shared.values.get(&FlowKey::Binding(global)) {
                flow.bindings
                    .insert(local.clone(), view.prepare_projection(values, &flow));
            }
            if let Some(bits) = candidate_bindings.get(&global) {
                let key = Arc::as_ptr(bits) as usize;
                let projected = projected_bindings
                    .entry(key)
                    .or_insert_with(|| Arc::new(view.candidate_bits(bits)))
                    .clone();
                if !projected.is_empty() {
                    flow.candidate_bindings.insert(local.clone(), projected);
                }
            }
            if let Some(sites) = initializations.remove(&global) {
                let mut sites = sites;
                sites.rekey(|site| shared.sites.sources[site as usize].1);
                flow.initializations.insert(local.clone(), sites);
            }
        }
        drop(projected_bindings);
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
            if let Some(values) = shared.values.get(&FlowKey::NumericProperties(*site)) {
                flow.numeric_property_unions
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
            // Visit only populated properties for this site. Sorted sparse rows
            // keep deterministic enumeration without a source-count x fact scan
            // or a site-count x property-name table.
            for (_, values) in shared.values.properties_at(*site) {
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
            if let Some(projected) = view
                .retained_candidate_bits(shared.origins.id(global))
                .filter(|bits| !bits.is_empty())
            {
                flow.candidate_reachability.insert(*origin, projected);
            }
        }
        flow.program = Some(view);
        outputs.insert(names[module].clone(), Arc::new(flow));
    }
    ProgramValueFlows {
        flows: outputs,
        cell_effects,
        module_bindings: linkage,
        #[cfg(test)]
        flow_work_stats: _flow_work_stats,
    }
}

#[cfg(test)]
#[path = "program_tests.rs"]
mod tests;
