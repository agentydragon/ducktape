use super::*;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;

/// Controls only which of the source module's exports contribute synthetic
/// execution roots and exposed capabilities to unknown-call fallback.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportReentryPolicy {
    /// Every export.
    #[default]
    Conservative,
    /// None: no code can obtain the module's bindings while it evaluates.
    NoStaticImporterReentry,
    /// Only these export names (`default` for the default export): the only
    /// code that can obtain the module's bindings while it evaluates imports
    /// exactly these.
    ReentryThrough(BTreeSet<String>),
}

/// Finds an `await` outside lazy scopes: function, arrow, method and
/// accessor bodies and class instance-field initializers may contain
/// `await` without making the module a top-level-await module.
#[derive(Default)]
struct TopLevelAwaitFinder {
    found: bool,
}

pub fn module_has_top_level_await(module: &Module) -> bool {
    module.body.iter().any(|item| {
        let mut finder = TopLevelAwaitFinder::default();
        item.visit_with(&mut finder);
        finder.found
    })
}

impl Visit for TopLevelAwaitFinder {
    fn visit_await_expr(&mut self, _node: &AwaitExpr) {
        self.found = true;
    }

    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        self.found |= node.is_await;
        node.visit_children_with(self);
    }

    fn visit_using_decl(&mut self, node: &UsingDecl) {
        self.found |= node.is_await;
        node.visit_children_with(self);
    }

    // Lazy boundaries — `await` inside any of these is the body's
    // own concern (and only legal if the body is itself `async`).
    fn visit_function(&mut self, _node: &Function) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_method_prop(&mut self, node: &MethodProp) {
        node.key.visit_with(self);
    }
    fn visit_getter_prop(&mut self, node: &GetterProp) {
        node.key.visit_with(self);
    }
    fn visit_setter_prop(&mut self, node: &SetterProp) {
        node.key.visit_with(self);
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        visit_eager_member_parts(self, member);
    }
}

/// Compute the policy-independent layer of the chunk analysis: item
/// views, shadowed globals, per-statement static facts, and the
/// top-level-await scan. None of these depend on `AnalysisInputs`
/// (declared-pure hints, known effects, or local-effect policy).
///
/// The policy-dependent half — [`ChunkCodeGraph::build_full`],
/// local-effect detection, per-statement purity classification, and
/// the redundant-hint diagnostics — runs in
/// `analyze::analyze_chunk_with_policy`.
///
/// **Sharing**: materialization computes this layer once and feeds it
/// to selector resolution and then to `analyze::analyze_chunk_with_policy`
/// (via `chunk_analysis::compute_chunk_facts`). Semantic passes may revisit
/// the retained item views. `vendor::strip` calls `analyze::analyze_vendor_prune_chunk`
/// on transformed output syntax, for which the materializer's original facts
/// do not apply.
pub fn analyze_chunk_structural<'a>(
    module: &'a Module,
    locator: Option<SourceLocator<'_>>,
) -> StructuralChunkAnalysis<'a> {
    complete_chunk_structural(collect_chunk_source_facts(module, locator))
}

/// Collect the source census once, before exact-site inputs are resolved.
pub fn collect_chunk_source_facts<'a>(
    module: &'a Module,
    locator: Option<SourceLocator<'_>>,
) -> StructuralChunkSourceFacts<'a> {
    collect_chunk_source_facts_from_items(&module.body, locator)
}

pub fn collect_chunk_source_facts_from_items<'a>(
    items: &'a [ModuleItem],
    locator: Option<SourceLocator<'_>>,
) -> StructuralChunkSourceFacts<'a> {
    let body = top_level_item_views(items);
    let stable_array_spread_shapes = StableArraySpreadShapes::collect(items, &body);
    let shadowed = compute_shadowed_globals(&body);
    let global_object_names = unshadowed_global_object_aliases(&body);
    let async_direct_function_bindings = collect_async_direct_function_bindings(&body);
    let mut top_level_await = None;
    let mut imported_bindings = BTreeSet::new();
    let mut per_statement: Vec<StructuralStatementFacts> = body
        .iter()
        .enumerate()
        .map(|(ordinal, item)| {
            let item = item.as_module_item();
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
                imported_bindings.extend(import.specifiers.iter().map(|s| s.local().to_id()));
            }
            if top_level_await.is_none() {
                let mut finder = TopLevelAwaitFinder::default();
                item.visit_with(&mut finder);
                if finder.found {
                    top_level_await = Some(SourceUnitOrdinal(ordinal));
                }
            }
            let kind = classify_item(item);
            let declared = collect_declared_names(item);
            let mut collector = StatementFactsCollector::new(
                global_object_names.clone(),
                async_direct_function_bindings.clone(),
                !shadowed.contains("Promise"),
            );
            item.visit_with(&mut collector);
            let top_level_var_initializer_candidates = var_decl_of_item(item)
                .filter(|var| var.kind == VarDeclKind::Var)
                .into_iter()
                .flat_map(|var| &var.decls)
                .filter_map(|decl| match (&decl.name, decl.init.as_deref()) {
                    (Pat::Ident(binding), Some(_)) => Some(binding.id.to_id()),
                    _ => None,
                })
                .collect();
            let constructor = class_of_item(item).map(|class| {
                collect_constructor_facts(
                    class,
                    &collector,
                    global_object_names.clone(),
                    async_direct_function_bindings.clone(),
                    !shadowed.contains("Promise"),
                )
            });
            let source_location = locator.and_then(|locator| locator.span(item.span()));
            StructuralStatementFacts {
                ordinal: SourceUnitOrdinal(ordinal),
                export_linkage: match item {
                    ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(export)) => {
                        Some(if export.src.is_none() {
                            ExportLinkage::Local
                        } else {
                            ExportLinkage::Dependency
                        })
                    }
                    ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => {
                        Some(ExportLinkage::Dependency)
                    }
                    _ => None,
                },
                source_location,
                kind,
                var_declared: collector
                    .var_declared
                    .intersection(&declared)
                    .cloned()
                    .collect(),
                var_initializers: collector
                    .var_initializers
                    .intersection(&declared)
                    .cloned()
                    .collect(),
                var_declaration_counts: collector.var_declaration_counts,
                var_initializer_counts: collector.var_initializer_counts,
                top_level_var_initializer_candidates,
                local_bindings: collector
                    .binding_idents
                    .difference(&declared)
                    .cloned()
                    .collect(),
                declared,
                reads: collector.reads,
                rebinds: collector.rebinds,
                member_write_roots: collector.member_write_roots,
                argument_value_escapes: collector.argument_value_escapes,
                calls: collector.calls,
                constructor,
                at_init_unresolved_sources: collector.at_init_unresolved_sources,
                at_init_unresolved_inline_fn: collector.at_init_unresolved_inline_fn,
                first_order_unresolved_sources: collector.first_order_unresolved_sources,
                first_order_unresolved_inline_fn: collector.first_order_unresolved_inline_fn,
                declares_direct_function: declares_direct_function(item),
                global_writes: collector.global_writes,
                global_reads: collector.global_reads,
                havoc: collector.havoc,
            }
        })
        .collect();
    apply_global_escape_taint(&body, &global_object_names, &mut per_statement);
    StructuralChunkSourceFacts {
        body,
        stable_array_spread_shapes,
        imported_bindings,
        shadowed,
        per_statement,
        top_level_await,
    }
}

/// Finish the retained source census with one canonical value-flow solve.
pub fn complete_chunk_structural(
    source: StructuralChunkSourceFacts<'_>,
) -> StructuralChunkAnalysis<'_> {
    complete_chunk_structural_with_inputs(source, &[], &BTreeMap::new(), &[])
}

/// Complete the source census with selected native-operation and argument
/// inputs before deriving plain-data, Set, and synchronous-call facts.
/// The argument analysis and native allocation roots share one canonical flow.
pub fn complete_chunk_structural_with_inputs<'a>(
    source: StructuralChunkSourceFacts<'a>,
    native_operation_inputs: &[analysis_vocabulary::inputs::ResolvedNativeOperationInput],
    inferred_set_constructions: &BTreeMap<
        u32,
        analysis_vocabulary::inputs::InferredSetConstruction,
    >,
    argument_inputs: &[analysis_vocabulary::inputs::ResolvedArgumentInput],
) -> StructuralChunkAnalysis<'a> {
    let value_flow = ChunkValueFlow::analyze_structural(
        &source.body,
        native_operation_inputs,
        inferred_set_constructions,
    );
    complete_chunk_structural_with_flow(source, argument_inputs, std::sync::Arc::new(value_flow))
}

/// `complete_chunk_structural_with_inputs` reusing a flow that
/// the program value flow already solved for the same body and inputs.
pub fn complete_chunk_structural_with_flow<'a>(
    source: StructuralChunkSourceFacts<'a>,
    argument_inputs: &[analysis_vocabulary::inputs::ResolvedArgumentInput],
    value_flow: std::sync::Arc<ChunkValueFlow>,
) -> StructuralChunkAnalysis<'a> {
    let StructuralChunkSourceFacts {
        body,
        stable_array_spread_shapes,
        imported_bindings,
        shadowed,
        per_statement,
        top_level_await,
    } = source;
    let dynamic_scope = DynamicScopeFact::analyze(&body);
    let stable_var_number_candidates =
        stable_top_level_var_candidates(&per_statement, dynamic_scope);
    let argument_facts = (!argument_inputs.is_empty()).then(|| {
        super::semantic_inference::ArgumentNoEscapeFacts::from_source(
            &value_flow,
            &per_statement,
            argument_inputs,
            dynamic_scope.present,
        )
    });
    let super::abstract_domain::heap::PlainDataBindings {
        bindings: plain_data_bindings,
        dependencies: plain_data_dependencies,
        inference_uses: plain_data_inference_uses,
    } = super::abstract_domain::heap::ChunkHeap::analyze(
        &body,
        &shadowed,
        &value_flow,
        argument_facts.as_ref(),
    )
    .plain_data_bindings();
    let source_semantic_facts =
        argument_facts.map(|facts| std::sync::Arc::new(facts.into_semantic_facts()));
    let set_iterable_proof = SetIterableShapes::analyze(
        &body,
        &shadowed,
        &value_flow,
        &plain_data_bindings,
        dynamic_scope,
    );
    let local_class_construction_proof =
        LocalClassConstructionShapes::analyze(&body, &shadowed, &value_flow, dynamic_scope);
    StructuralChunkAnalysis {
        body,
        stable_array_spread_shapes,
        imported_bindings,
        value_flow,
        dynamic_scope,
        set_iterable_shapes: set_iterable_proof,
        local_class_shapes: local_class_construction_proof,
        plain_data_bindings,
        stable_var_number_candidates,
        plain_data_dependencies,
        plain_data_inference_uses,
        source_semantic_facts,
        shadowed,
        per_statement,
        top_level_await,
    }
}

/// Derive stable top-level `var` candidates from facts retained by the normal
/// per-statement collector. This does not walk syntax or run another census:
/// declaration multiplicity and eager/lazy rebinds came from the same visitor
/// pass that produced each statement's other structural facts.
fn stable_top_level_var_candidates(
    statements: &[StructuralStatementFacts],
    dynamic_scope: DynamicScopeFact,
) -> BTreeSet<Id> {
    if dynamic_scope.present {
        return BTreeSet::new();
    }

    let mut candidates = BTreeSet::new();
    let mut declaration_counts = BTreeMap::<Id, usize>::new();
    let mut initializer_counts = BTreeMap::<Id, usize>::new();
    let mut declared_counts = BTreeMap::<Id, usize>::new();
    let mut nested_bindings = BTreeSet::new();
    let mut rebinds = BTreeSet::new();
    for statement in statements {
        candidates.extend(
            statement
                .top_level_var_initializer_candidates
                .iter()
                .cloned(),
        );
        for (id, count) in &statement.var_declaration_counts {
            *declaration_counts.entry(id.clone()).or_default() += count;
        }
        for (id, count) in &statement.var_initializer_counts {
            *initializer_counts.entry(id.clone()).or_default() += count;
        }
        for id in &statement.declared {
            *declared_counts.entry(id.clone()).or_default() += 1;
        }
        nested_bindings.extend(statement.local_bindings.iter().cloned());
        rebinds.extend(statement.rebinds.eager.iter().cloned());
        rebinds.extend(statement.rebinds.lazy.iter().cloned());
    }
    candidates.retain(|id| {
        declaration_counts.get(id) == Some(&1)
            && initializer_counts.get(id) == Some(&1)
            && declared_counts.get(id) == Some(&1)
            && !nested_bindings.contains(id)
            && !rebinds.contains(id)
    });
    candidates
}
