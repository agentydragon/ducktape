use super::*;
use serde::Serialize;
use source_identity::SourceUnitOrdinal;

/// Controls only whether the source module's export surface contributes
/// synthetic execution roots to unknown-call fallback.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportReentryPolicy {
    #[default]
    Conservative,
    NoStaticImporterReentry,
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
/// [`analyze_chunk_with_policy`].
///
/// **Sharing**: materialization computes this layer once and feeds it
/// to selector resolution and then to [`analyze_chunk_with_policy`]
/// (via `chunk_analysis::compute_chunk_analysis`). Semantic passes may revisit
/// the retained item views. `vendor::strip` calls [`analyze_vendor_prune_chunk`]
/// on transformed output syntax, for which the materializer's original facts
/// do not apply.
pub fn analyze_chunk_structural<'a, F>(
    module: &'a Module,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> StructuralChunkAnalysis<'a>
where
    F: FnMut(Span) -> Option<(usize, usize, usize)>,
{
    let body = top_level_item_views(&module.body);
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
            let constructor = class_of_item(item).map(|class| {
                collect_constructor_facts(
                    class,
                    &collector,
                    global_object_names.clone(),
                    async_direct_function_bindings.clone(),
                    !shadowed.contains("Promise"),
                )
            });
            let source_location = source_path.and_then(|source_path| {
                line_range_for_span(item.span()).map(|(start_line, end_line, start_column)| {
                    SourceLocation {
                        source_path: source_path.to_string(),
                        start_line,
                        end_line,
                        start_column: Some(start_column),
                    }
                })
            });
            StructuralStatementFacts {
                ordinal: SourceUnitOrdinal(ordinal),
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
                local_bindings: collector
                    .binding_idents
                    .difference(&declared)
                    .cloned()
                    .collect(),
                declared,
                reads: collector.reads,
                rebinds: collector.rebinds,
                member_write_roots: collector.member_write_roots,
                calls: collector.calls,
                constructor,
                at_init_unresolved_sources: collector.at_init_unresolved_sources,
                at_init_unresolved_inline_fn: collector.at_init_unresolved_inline_fn,
                first_order_unresolved_sources: collector.first_order_unresolved_sources,
                first_order_unresolved_inline_fn: collector.first_order_unresolved_inline_fn,
                declares_direct_function: declares_direct_function(item),
                global_writes: collector.global_writes,
                global_reads: collector.global_reads,
                cell_writes_summarizable: collector.cell_writes_summarizable,
                dataflow_summarizable: collector.dataflow_summarizable,
            }
        })
        .collect();
    apply_global_escape_taint(&body, &global_object_names, &mut per_statement);
    let value_flow = ChunkValueFlow::analyze(&body);
    let plain_data_bindings =
        crate::purity::collect_plain_data_bindings(&body, &shadowed, &value_flow);
    let dynamic_scope = DynamicScopeFact::analyze(&body);
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
        imported_bindings,
        value_flow,
        set_iterable_shapes: set_iterable_proof,
        local_class_shapes: local_class_construction_proof,
        plain_data_bindings,
        shadowed,
        per_statement,
        top_level_await,
    }
}

/// Compute the policy-dependent half of the chunk analysis from the
/// pre-computed structural layer and a set of `AnalysisInputs`. Builds
/// the chunk code graph (hint-gated purity propagation), the
/// local-effect context (gated by `hints.local_effect_policy`), the
/// per-statement purity classification, and the redundant-hint
/// diagnostics. Folds the policy-independent facts in to produce the
/// full [`ChunkFactAnalysis`].
pub fn analyze_chunk_with_policy<F>(
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisInputs,
    export_reentry_policy: ExportReentryPolicy,
    source_path: Option<&str>,
    line_range_for_span: F,
) -> ChunkFactAnalysis
where
    F: FnMut(Span) -> Option<(usize, usize, usize)>,
{
    analyze_chunk_with_policy_and_sync(
        structural,
        hints,
        export_reentry_policy,
        source_path,
        line_range_for_span,
        SynchronousCallAnalysis::Include,
    )
}

#[derive(Clone, Copy)]
enum SynchronousCallAnalysis {
    Include,
    Omit,
}

fn analyze_chunk_with_policy_and_sync<F>(
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisInputs,
    export_reentry_policy: ExportReentryPolicy,
    source_path: Option<&str>,
    mut line_range_for_span: F,
    synchronous_calls: SynchronousCallAnalysis,
) -> ChunkFactAnalysis
where
    F: FnMut(Span) -> Option<(usize, usize, usize)>,
{
    let StructuralChunkAnalysis {
        body,
        imported_bindings,
        value_flow,
        set_iterable_shapes,
        local_class_shapes,
        plain_data_bindings,
        shadowed,
        per_statement,
        top_level_await,
    } = structural;
    let plain_array_bindings = crate::purity::collect_plain_array_bindings(&body, &shadowed);
    let mut prepared_sync_index =
        super::sync_effects::execution_index::interpreter::prepare_synchronous_call_index(
            &body,
            &value_flow,
            hints,
            &plain_array_bindings,
            &local_class_shapes,
            set_iterable_shapes,
            &shadowed,
        );
    prepared_sync_index.locate_native_integrity_witnesses(|span| {
        let source_path = source_path?;
        let (start_line, end_line, start_column) = line_range_for_span(span)?;
        Some(SourceLocation {
            source_path: source_path.to_owned(),
            start_line,
            end_line,
            start_column: Some(start_column),
        })
    });
    let local_class_construction_proof =
        prepared_sync_index.local_class_construction_proof().clone();
    let set_iterable_proof = prepared_sync_index.set_iterable_proof().clone();
    let graph = {
        let mut locate = |span| {
            let source_path = source_path?;
            let (start_line, end_line, start_column) = line_range_for_span(span)?;
            Some(SourceLocation {
                source_path: source_path.to_string(),
                start_line,
                end_line,
                start_column: Some(start_column),
            })
        };
        ChunkCodeGraph::build_full_with_prepared_plain_data(
            &body,
            &shadowed,
            &hints.assumptions.declared_pure,
            &hints.assumptions.declared_pure_new,
            &hints.assumptions.declared_pure_members,
            &hints.imported_purities,
            &hints.assumptions.fluent_bindings,
            &set_iterable_proof,
            &local_class_construction_proof,
            &plain_data_bindings,
            &plain_array_bindings,
            Some(&mut locate),
        )
    };
    let local_effect_context = local_effects::LocalEffectContext::for_body(
        &body,
        hints.local_effect_policy,
        &hints.assumptions.known_effects,
        &value_flow,
        &per_statement,
        &imported_bindings,
    );
    let redundant_purity_hints =
        detect_redundant_purity_hints(&body, &shadowed, &hints.assumptions.declared_pure);
    let redundant_pure_member_hints =
        detect_redundant_pure_member_hints(&hints.assumptions.declared_pure_members, &shadowed);
    let mut seen_vars = BTreeSet::new();
    let mut shared_vars = BTreeSet::new();
    for stmt in &per_statement {
        for binding in &stmt.var_declared {
            if !seen_vars.insert(binding.clone()) {
                shared_vars.insert(binding.clone());
            }
        }
    }
    let mut facts: Vec<StatementFacts> = body
        .iter()
        .zip(per_statement)
        .map(|(item_view, structural_facts)| {
            let item = item_view.as_module_item();
            let mut fact = assemble_statement_facts(
                item,
                structural_facts,
                &shadowed,
                hints,
                &graph,
                &local_effect_context,
                &shared_vars,
            );
            // Resolve any reason spans on `fact.purity` to
            // SourceLocations using the same per-chunk line index.
            // Done after fact construction because the classifier
            // doesn't have access to the line resolver.
            if let Some(source_path) = source_path
                && let Purity::NotPure { reasons } = &mut fact.purity
            {
                for reason in reasons.iter_mut() {
                    reason.source_location = line_range_for_span(reason.span).map(
                        |(start_line, end_line, start_column)| SourceLocation {
                            source_path: source_path.to_string(),
                            start_line,
                            end_line,
                            start_column: Some(start_column),
                        },
                    );
                }
            }
            fact
        })
        .collect();
    value_flow.augment_calls(&body, &mut facts);
    let local_class_heap_proof = prepared_sync_index.local_class_heap_proof();
    super::array_content_effects::annotate_array_content_effects(
        &body,
        &value_flow,
        &local_class_heap_proof,
        &mut facts,
    );
    if matches!(synchronous_calls, SynchronousCallAnalysis::Include) {
        super::sync_effects::execution_index::interpreter::annotate_synchronous_calls(
            &body,
            &value_flow,
            super::sync_effects::execution_index::interpreter::SynchronousCallProofs {
                set_iterable: &set_iterable_proof,
                graph: &graph,
                index: prepared_sync_index,
            },
            &shadowed,
            hints,
            export_reentry_policy,
            &mut facts,
        );
        discharge_checked_local_record_factory_calls(&mut facts);
    }
    if let Some(source_path) = source_path {
        let source_locations: Vec<_> = facts
            .iter()
            .map(|fact| fact.source_location.clone())
            .collect();
        for fact in &mut facts {
            if let Some(calls) = &mut fact.synchronous_calls {
                for loss in &mut calls.precision_losses {
                    loss.source_location = if loss.span.is_dummy() {
                        source_locations[loss.statement_ordinal.0].clone()
                    } else {
                        line_range_for_span(loss.span).map(
                            |(start_line, end_line, start_column)| SourceLocation {
                                source_path: source_path.to_string(),
                                start_line,
                                end_line,
                                start_column: Some(start_column),
                            },
                        )
                    };
                }
                for proof_use in &mut calls.callback_factory_uses {
                    proof_use.source_location = line_range_for_span(proof_use.span).map(
                        |(start_line, end_line, start_column)| SourceLocation {
                            source_path: source_path.to_string(),
                            start_line,
                            end_line,
                            start_column: Some(start_column),
                        },
                    );
                }
                for proof_use in &mut calls.returned_record_uses {
                    proof_use.source_location = line_range_for_span(proof_use.span).map(
                        |(start_line, end_line, start_column)| SourceLocation {
                            source_path: source_path.to_string(),
                            start_line,
                            end_line,
                            start_column: Some(start_column),
                        },
                    );
                }
                for proof_use in &mut calls.local_record_factory_uses {
                    proof_use.source_location = line_range_for_span(proof_use.span).map(
                        |(start_line, end_line, start_column)| SourceLocation {
                            source_path: source_path.to_string(),
                            start_line,
                            end_line,
                            start_column: Some(start_column),
                        },
                    );
                }
                for proof_use in &mut calls.author_host_contract_uses {
                    proof_use.source_location = line_range_for_span(proof_use.span).map(
                        |(start_line, end_line, start_column)| SourceLocation {
                            source_path: source_path.to_string(),
                            start_line,
                            end_line,
                            start_column: Some(start_column),
                        },
                    );
                }
            }
        }
    }
    ChunkFactAnalysis {
        facts,
        top_level_await,
        redundant_purity_hints,
        redundant_pure_member_hints,
    }
}

/// Generic function-body purity cannot compose the already-checked private
/// constructor graph at one call site. Consume only the typed certificate
/// produced by successful ordinary synchronous execution, and remove only its
/// matching `impure_function_call` reason. Argument, callee, read, and any
/// other expression reasons remain classified normally; function-body purity
/// caches are never changed.
fn discharge_checked_local_record_factory_calls(facts: &mut [StatementFacts]) {
    for fact in facts {
        let Some(certificate) = fact
            .synchronous_calls
            .as_ref()
            .and_then(|calls| calls.local_record_factory_purity.as_ref())
        else {
            continue;
        };
        if certificate.checked_owners.is_empty() {
            continue;
        }
        let Purity::NotPure { reasons } = &mut fact.purity else {
            continue;
        };
        let matching = reasons
            .iter()
            .filter(|reason| {
                reason.rule == PurityRule::ImpureFunctionCall
                    && reason.span == certificate.call_span
            })
            .count();
        if matching != 1 {
            continue;
        }
        reasons.retain(|reason| {
            !(reason.rule == PurityRule::ImpureFunctionCall && reason.span == certificate.call_span)
        });
        if reasons.is_empty() {
            fact.purity = Purity::Pure {
                dependencies: certificate.dependencies.clone(),
            };
        }
    }
}

/// Facts needed by vendor pruning after its own AST mutations. Vendor pruning
/// consumes the structural, value-flow, purity and local-effect fields, but
/// builds no dependency graph and never reads `StatementFacts::synchronous_calls`.
/// Keep value-flow call augmentation; it is part of vendor reachability.
pub fn analyze_vendor_prune_chunk(module: &Module) -> ChunkFactAnalysis {
    let hints = AnalysisInputs {
        local_effect_policy: LocalEffectPolicy::VendorPrune,
        ..AnalysisInputs::default()
    };
    let structural = analyze_chunk_structural(module, None, |_| None);
    analyze_chunk_with_policy_and_sync(
        structural,
        &hints,
        ExportReentryPolicy::Conservative,
        None,
        |_| None,
        SynchronousCallAnalysis::Omit,
    )
}

/// Walk the chunk's module body and produce one `StatementFacts`
/// entry per top-level statement, in source order.
///
/// Multi-declarator `var/let/const` statements are split into
/// per-declarator entries before analysis, so each row declares
/// a single name and dependency graph destination attribution
/// returns an unambiguous source_unit. Without the split, a chunk like
/// `const A = 1, B = readsX;` with `{A → mod_a, B → mod_b}`
/// would attribute `B`'s read of `X` to `mod_a` (the first
/// declared name's source_unit), inventing or hiding cycles. The
/// emitter splits the same comma-lists separately at lower-time
/// (`split_var_decl` in `lowering/util.rs`); this pre-split
/// just teaches the analyzer the same view.
pub fn analyze_chunk<F>(
    module: &Module,
    hints: &AnalysisInputs,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> ChunkFactAnalysis
where
    F: FnMut(Span) -> Option<(usize, usize, usize)>,
{
    let structural = analyze_chunk_structural(module, source_path, &mut line_range_for_span);
    analyze_chunk_with_policy(
        structural,
        hints,
        ExportReentryPolicy::Conservative,
        source_path,
        line_range_for_span,
    )
}

/// Fold the policy-independent per-statement facts together with the
/// policy-dependent local-effect and purity classifications into a
/// complete [`StatementFacts`] record. Called once per statement by
/// [`analyze_chunk_with_policy`] after the policy-independent layer
/// has already walked the AST.
fn assemble_statement_facts(
    item: &ModuleItem,
    structural: StructuralStatementFacts,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    graph: &ChunkCodeGraph,
    local_effect_context: &local_effects::LocalEffectContext,
    shared_vars: &BTreeSet<Id>,
) -> StatementFacts {
    let StructuralStatementFacts {
        ordinal,
        source_location,
        kind,
        declared,
        local_bindings,
        var_declared,
        var_initializers,
        reads,
        rebinds,
        member_write_roots,
        calls,
        constructor,
        at_init_unresolved_sources,
        at_init_unresolved_inline_fn,
        first_order_unresolved_sources,
        first_order_unresolved_inline_fn,
        declares_direct_function,
        global_writes,
        global_reads,
        cell_writes_summarizable,
        dataflow_summarizable,
    } = structural;
    let local_effects = collect_local_effects(item, shadowed, hints, graph, local_effect_context);
    let mut purity = item_purity(
        item,
        kind,
        shadowed,
        hints,
        graph,
        !local_effects.is_empty(),
    );
    if !var_initializers.is_disjoint(shared_vars) {
        purity = purity.worst(Purity::from_reason_with_detail(
            PurityRule::AssignOrUpdate,
            item.span(),
            "initializer writes a shared var binding".to_string(),
        ));
    }
    // Opaque at-init calls: a call/new the purity classifier can't
    // prove Pure may touch any cell (I/O like `console.log`, global
    // props written inside callee bodies, indirect eval) — the
    // statement must fall back to the strict S-chain. Classifier-Pure
    // calls are exempt: Pure guarantees no observable writes and no
    // global-prop reads, and binding-cell ordering is enforced by
    // binding edges + rebind co-location rather than the S-chain.
    let trusted_summary = hints.assumptions.trusted_dataflow_summaries && cell_writes_summarizable;
    let dataflow_summarizable = trusted_summary
        || dataflow_summarizable && !has_opaque_at_init_call(item, shadowed, hints, graph);
    // Ordinary eager reads (including `pureWrap(B)`) always remain in
    // `reads.eager`. For classifier/callee-pure statements, clearing the
    // unresolved-call fallback also asserts that opaque calls cannot
    // synchronously invoke callback bodies at module init. An
    // initializer-only trust annotation certifies the initializer's
    // evaluation effects but deliberately does not make that callback
    // assertion: retain its fallback roots so callback-body binding reads
    // still constrain module order.
    let no_sync_arg_sources = no_sync_member_argument_fallback_sources(item, hints);
    let mut first_order_unresolved_sources = first_order_unresolved_sources;
    for id in &no_sync_arg_sources.first_order_lazy {
        first_order_unresolved_sources.remove(id);
    }
    let (at_init_unresolved_sources, at_init_unresolved_inline_fn, first_order_unresolved_sources) =
        if purity.is_pure() && !has_trusted_initializer_purity(item, hints) {
            (BTreeSet::new(), false, first_order_unresolved_sources)
        } else {
            let safe_array_sources =
                safe_plain_array_at_init_fallback_sources(item, shadowed, graph);
            let mut at_init_unresolved_sources = at_init_unresolved_sources;
            for id in &no_sync_arg_sources.eager {
                at_init_unresolved_sources.remove(id);
            }

            if safe_array_sources.is_empty() {
                let at_init_unresolved_inline_fn = at_init_unresolved_inline_fn
                    && has_untrusted_at_init_unresolved_inline_fn_call(item, hints);
                (
                    at_init_unresolved_sources,
                    at_init_unresolved_inline_fn,
                    first_order_unresolved_sources,
                )
            } else {
                (
                    at_init_unresolved_sources
                        .difference(&safe_array_sources)
                        .cloned()
                        .collect(),
                    at_init_unresolved_inline_fn
                        && has_untrusted_at_init_unresolved_inline_fn_call(item, hints),
                    first_order_unresolved_sources,
                )
            }
        };

    StatementFacts {
        ordinal,
        source_location,
        callback_factory_barriers: Vec::new(),
        declared,
        local_bindings,
        var_declared,
        var_initializers,
        reads,
        rebinds,
        member_write_roots,
        local_effects,
        calls,
        constructor,
        synchronous_calls: None,
        at_init_unresolved_sources,
        at_init_unresolved_inline_fn,
        first_order_unresolved_sources,
        first_order_unresolved_inline_fn,
        declares_direct_function,
        global_writes,
        global_reads,
        cell_writes_summarizable,
        dataflow_summarizable,
        array_contents: super::ArrayContentEffects::default(),
        heap_native_integrity_uses: Vec::new(),
        purity,
        kind,
    }
}
