//! Flow-program construction: symbols, events, facts and the collector that
//! lowers source into them.

use super::*;

#[derive(Default)]
#[cfg(test)]
pub(super) struct DeclaredBindingCollector {
    pub(super) ids: BTreeSet<Id>,
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
pub(super) struct BindingSlot(pub(super) u32);

/// A static property name written or read anywhere in one flow program.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(super) struct PropertySlot(pub(super) u32);

/// Source-validated factory identities for one importer. Namespace members
/// additionally require the completed whole-source privacy receipt.
#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct FreshFactoryImports {
    pub(super) importer: usize,
    pub(super) named: BTreeSet<Id>,
    pub(super) members: BTreeMap<Id, BTreeSet<String>>,
}

#[derive(Default)]
pub(super) struct FlowSymbols {
    pub(super) member_read_certificates: Arc<MemberReadCertificates>,
    pub(super) bindings: Vec<Id>,
    pub(super) binding_slots: HashMap<Id, BindingSlot>,
    pub(super) properties: Vec<String>,
    pub(super) property_slots: HashMap<String, PropertySlot>,
    pub(super) numeric_properties: Vec<bool>,
    pub(super) call_arguments: Vec<FlowValueExpr>,
    pub(super) call_argument_slots: HashMap<(u32, u32), usize>,
    pub(super) fresh_factory_imports: Arc<FreshFactoryImports>,
}

impl FlowSymbols {
    pub(super) fn factory_importer(
        &self,
        call: &CallExpr,
        argument: Option<usize>,
    ) -> Option<usize> {
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
    pub(super) fn call_argument(&mut self, call: &CallExpr) -> Option<usize> {
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

    pub(super) fn binding(&mut self, id: Id) -> BindingSlot {
        *self.binding_slots.entry(id).or_insert_with_key(|id| {
            self.bindings.push(id.clone());
            BindingSlot(u32::try_from(self.bindings.len() - 1).expect("binding slot overflow"))
        })
    }

    pub(super) fn property(&mut self, name: String) -> PropertySlot {
        *self.property_slots.entry(name).or_insert_with_key(|name| {
            self.properties.push(name.clone());
            self.numeric_properties
                .push(may_be_numeric_property_key(name));
            PropertySlot(u32::try_from(self.properties.len() - 1).expect("property slot overflow"))
        })
    }

    pub(super) fn id(&self, slot: BindingSlot) -> &Id {
        &self.bindings[slot.0 as usize]
    }

    pub(super) fn name(&self, slot: PropertySlot) -> &str {
        &self.properties[slot.0 as usize]
    }

    /// Whether each slot names a binding declared in the chunk, indexed by slot.
    pub(super) fn declared(&self, declared_ids: &BTreeSet<Id>) -> Vec<bool> {
        self.bindings
            .iter()
            .map(|id| declared_ids.contains(id))
            .collect()
    }
}

#[derive(Debug, Clone)]
pub(super) enum FlowValueExpr {
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
    pub(super) fn reads_values(&self) -> bool {
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

    pub(super) fn from_ast(expr: &Expr, symbols: &mut FlowSymbols) -> Self {
        match expr.unwrap_parens() {
            Expr::Cond(cond) => Self::from_union_branches(&cond.cons, &cond.alt, symbols),
            Expr::Bin(bin) if bin.op.may_short_circuit() => {
                Self::from_union_branches(&bin.left, &bin.right, symbols)
            }
            expr => Self::from_ast_leaf(expr, symbols),
        }
    }

    pub(super) fn from_union_branches(
        left: &Expr,
        right: &Expr,
        symbols: &mut FlowSymbols,
    ) -> Self {
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

    pub(super) fn from_union_level(mut leaves: Vec<Self>) -> Self {
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

    pub(super) fn from_ast_leaf(expr: &Expr, symbols: &mut FlowSymbols) -> Self {
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
pub(super) enum FlowEvent {
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
pub(super) enum PropertyProducer {
    DynamicReceiver(PropertySlot),
    FixedReceiver(u32, PropertySlot),
}

impl FlowEvent {
    /// Exact property-fact producers in the source flow relation. Keep this
    /// match exhaustive so a new event variant must be reviewed for writes.
    pub(super) fn property_producer(&self) -> Option<PropertyProducer> {
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
pub(super) struct PropertyProducerCensus {
    pub(super) fixed_receivers: HashSet<(u32, PropertySlot)>,
    pub(super) dynamic_receiver_properties: Vec<bool>,
}

impl PropertyProducerCensus {
    pub(super) fn from_events(events: &[FlowEvent]) -> Self {
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

    pub(super) fn may_produce(&self, object: u32, property: PropertySlot) -> bool {
        self.dynamic_receiver_properties
            .get(property.0 as usize)
            .copied()
            .unwrap_or(false)
            || self.fixed_receivers.contains(&(object, property))
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub(super) enum FlowKey {
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
pub(super) enum FlowFact {
    ValueFlowActive,
    Values(Values),
    CandidateReachability(Arc<RoaringBitmap>),
    Parameters(Vec<Option<BindingSlot>>),
    ReadyParameters(Vec<Option<BindingSlot>>),
    Cells(KnownCellAccesses),
    Initialization(InitializationSites),
    ReadOnlyCallback(ReadOnlyCallback<BindingSlot>),
}

pub(super) struct FlowProgram {
    pub(super) module_bindings: BTreeMap<String, BindingSlot>,
    pub(super) imports: Vec<BindingSlot>,
    pub(super) declared_ids: BTreeSet<Id>,
    pub(super) symbols: FlowSymbols,
    pub(super) events: Vec<FlowEvent>,
    pub(super) cell_effects: BTreeMap<u32, CallEffectBody>,
    pub(super) origin_table: Arc<OriginTable>,
    pub(super) candidate_objects: Vec<Value>,
    pub(super) canonical: BTreeMap<Value, Id>,
    pub(super) callable_source_unit: BTreeMap<Value, SourceUnitOrdinal>,
    pub(super) native_set_allocations:
        BTreeMap<u32, analysis_vocabulary::inputs::ProvenSetAllocation>,
    pub(super) fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
    pub(super) return_summaries: BTreeMap<u32, ReturnSummary>,
}

impl FlowProgram {
    pub(super) fn structural(
        body: &[TopLevelItemView<'_>],
        inputs: &[analysis_vocabulary::inputs::ResolvedNativeOperationInput],
        inferred: &BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
    ) -> Self {
        Self::structural_with_factory_imports(body, inputs, inferred, Arc::default())
    }

    pub(super) fn structural_with_factory_imports(
        body: &[TopLevelItemView<'_>],
        inputs: &[analysis_vocabulary::inputs::ResolvedNativeOperationInput],
        inferred: &BTreeMap<u32, analysis_vocabulary::inputs::InferredSetConstruction>,
        fresh_factory_imports: Arc<FreshFactoryImports>,
    ) -> Self {
        let mut candidates = analysis_domain::allocation_roots::tracked_allocation_roots(body);
        candidates.extend(analysis_domain::allocation_roots::array_candidate_roots(
            body,
        ));
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

    pub(super) fn collect(
        body: &[TopLevelItemView<'_>],
        candidate_objects: &BTreeSet<Value>,
    ) -> Self {
        Self::collect_with_native_set_allocations(
            body,
            candidate_objects,
            &BTreeMap::new(),
            Arc::default(),
        )
    }

    pub(super) fn collect_with_native_set_allocations(
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
            let declared =
                analysis_domain::item_views::collect_declared_names(item.as_module_item());
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

    pub(super) fn solve(self) -> (ChunkValueFlow, FlowWorkStats) {
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
pub(super) fn solve_flow(
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
        &work_metrics::WorkMetrics::default(),
        None,
    )
}

pub(super) struct FlowConstraints {
    pub(super) events: Vec<FlowEvent>,
    pub(super) returns: ReturnInputs,
    pub(super) declared: Vec<bool>,
    pub(super) numeric_properties: Vec<bool>,
}

pub(super) fn solve_flow_observed(
    constraints: FlowConstraints,
    origin_table: Arc<OriginTable>,
    candidate_objects: &[Value],
    native_set_allocations: BTreeSet<u32>,
    cell_effects: BTreeMap<u32, CallEffectBody>,
    metrics: &work_metrics::WorkMetrics,
    observer: Option<&dyn progress::Observer>,
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
        metrics: metrics.clone(),
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
    use progress::{Phase, State, report};
    report(observer, "*", Phase::SolveProgramValueFlow, State::Start);
    let values = IterativeInference::new(rules)
        .observed(metrics)
        .solve_values_resumable(0..producer_count);
    report(observer, "*", Phase::SolveProgramValueFlow, State::End);
    report(observer, "*", Phase::CloseProgramValueFlow, State::Start);
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
    report(observer, "*", Phase::CloseProgramValueFlow, State::End);
    (facts, stats)
}

pub(super) struct FlowProgramCollector {
    pub(super) declared_ids: BTreeSet<Id>,
    pub(super) symbols: FlowSymbols,
    pub(super) events: Vec<FlowEvent>,
    pub(super) cell_effects: BTreeMap<u32, CallEffectBody>,
    pub(super) origins: BTreeSet<Value>,
    pub(super) source_unit: SourceUnitOrdinal,
    pub(super) current_function: Option<u32>,
    pub(super) collect_effect_calls: bool,
    pub(super) canonical: BTreeMap<Value, Id>,
    pub(super) callable_source_unit: BTreeMap<Value, SourceUnitOrdinal>,
    pub(super) native_set_allocations:
        BTreeMap<u32, analysis_vocabulary::inputs::ProvenSetAllocation>,
    pub(super) fresh_returned_record_keys: BTreeMap<u32, BTreeSet<String>>,
    pub(super) return_summaries: BTreeMap<u32, ReturnSummary>,
    pub(super) return_candidates: BTreeMap<u32, return_summary::Candidate>,
    pub(super) summary_writes: BTreeSet<BindingSlot>,
    pub(super) summary_returns: BTreeMap<u32, Vec<u32>>,
    pub(super) summary_dynamic: bool,
}

impl FlowProgramCollector {
    pub(super) fn enter_function(&mut self, function: u32) -> Option<u32> {
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

    pub(super) fn record_invoke(&mut self, callee: &Expr, args: &[ExprOrSpread]) {
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
    pub(super) fn without_effect_calls(&mut self, visit: impl FnOnce(&mut Self)) {
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

    /// The walk reaches a loop head for both loop forms, so one override covers
    /// both: each declarator is assigned by the loop, and a destructuring head
    /// writes through its pattern.
    fn visit_for_head(&mut self, head: &ForHead) {
        if let ForHead::VarDecl(declaration) = head {
            for declarator in &declaration.decls {
                for id in binding_targets::binding_names(&declarator.name) {
                    self.summary_writes.insert(self.symbols.binding(id));
                }
            }
        }
        if let ForHead::Pat(pattern) = head {
            binding_targets::record_pat_write(pattern, self);
        }
        head.visit_children_with(self);
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

pub(super) fn direct_callable_origin(expr: &Expr) -> Option<Value> {
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
pub(super) fn fresh_returned_record_keys(body: &FunctionBody) -> Option<BTreeSet<String>> {
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
