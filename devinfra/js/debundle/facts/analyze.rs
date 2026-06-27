use super::*;

/// Walk the chunk's module body and produce one `StatementFacts`
/// entry per top-level statement, in source order.
///
/// Multi-declarator `var/let/const` statements are split into
/// per-declarator entries before analysis, so each row declares
/// a single name and owner-graph destination attribution
/// returns an unambiguous owner. Without the split, a chunk like
/// `const A = 1, B = readsX;` with `{A → mod_a, B → mod_b}`
/// would attribute `B`'s read of `X` to `mod_a` (the first
/// declared name's owner), inventing or hiding cycles. The
/// emitter splits the same comma-lists separately at lower-time
/// (`split_var_decl` in `lowering/util.rs`); this pre-split
/// just teaches the analyzer the same view.
/// Locate the first top-level `await` expression in `module`'s
/// body, if any. Returns the source-order ordinal of the offending
/// statement (in the post-comma-list-split view that
/// `analyze_chunk` uses, so reports align with statement indices
/// in `reports/tree/<chunk_id>/owner_graph.json`).
///
/// "Top-level" excludes function/method/arrow/getter/setter
/// bodies and class instance-field initializers — those are lazy
/// scopes that may legitimately contain `await` without making
/// the module a top-level-await module.
pub fn find_top_level_await(module: &Module) -> Option<StatementOrdinal> {
    let body = top_level_item_views(&module.body);
    for (ordinal, item) in body.iter().enumerate() {
        let mut finder = TopLevelAwaitFinder::default();
        item.as_module_item().visit_with(&mut finder);
        if finder.found {
            return Some(StatementOrdinal(ordinal));
        }
    }
    None
}

#[derive(Default)]
struct TopLevelAwaitFinder {
    found: bool,
}

impl Visit for TopLevelAwaitFinder {
    fn visit_await_expr(&mut self, _node: &AwaitExpr) {
        self.found = true;
    }

    // Lazy boundaries — `await` inside any of these is the body's
    // own concern (and only legal if the body is itself `async`).
    fn visit_function(&mut self, _node: &Function) {}
    fn visit_arrow_expr(&mut self, _node: &ArrowExpr) {}
    fn visit_method_prop(&mut self, _node: &MethodProp) {}
    fn visit_getter_prop(&mut self, _node: &GetterProp) {}
    fn visit_setter_prop(&mut self, _node: &SetterProp) {}

    fn visit_class_member(&mut self, member: &ClassMember) {
        visit_eager_member_parts(self, member);
    }
}

/// Compute the policy-independent layer of the chunk analysis: item
/// views, shadowed globals, per-statement static facts, and the
/// top-level-await scan. None of these depend on `AnalysisHints`
/// (declared-pure hints, known effects, or local-effect policy).
///
/// The policy-dependent half — [`ChunkCodeGraph::build_full`],
/// local-effect detection, per-statement purity classification, and
/// the redundant-hint diagnostics — runs in
/// [`analyze_chunk_with_policy`].
///
/// **Cross-pass sharing**: this layer is NOT shareable across the two
/// `analyze_chunk` call sites (chunk-analysis composer vs `vendor::strip`)
/// because they analyze different `Module` values. Vendor strip
/// reparses the emitted chunk file from disk and then mutates it
/// (`split_top_level_var_decls`, `strip_export_specifiers`) before
/// analyzing; chunk analysis analyzes the in-memory lowered runtime AST.
/// Even ignoring the reparse, `strip_export_specifiers` rewrites
/// `ExportNamed` items and folds `ExportDecl` into `Stmt::Decl`, which
/// changes the body view that `top_level_item_views` produces. So this
/// split exists for code-shape clarity, not as a perf optimization.
pub(crate) fn analyze_chunk_structural<'a, F>(
    module: &'a Module,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> StructuralChunkAnalysis<'a>
where
    F: FnMut(Span) -> Option<(usize, usize)>,
{
    let body = top_level_item_views(&module.body);
    let shadowed = compute_shadowed_globals(&body);
    let global_object_names = unshadowed_global_object_aliases(&body);
    let async_direct_function_bindings = collect_async_direct_function_bindings(&body);
    let mut top_level_await = None;
    let mut per_statement: Vec<StructuralStatementFacts> = body
        .iter()
        .enumerate()
        .map(|(ordinal, item)| {
            let item = item.as_module_item();
            if top_level_await.is_none() {
                let mut finder = TopLevelAwaitFinder::default();
                item.visit_with(&mut finder);
                if finder.found {
                    top_level_await = Some(StatementOrdinal(ordinal));
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
            let source_location = source_path.and_then(|source_path| {
                line_range_for_span(item.span()).map(|(start_line, end_line)| SourceLocation {
                    source_path: source_path.to_string(),
                    start_line,
                    end_line,
                })
            });
            StructuralStatementFacts {
                ordinal: StatementOrdinal(ordinal),
                source_location,
                kind,
                declared,
                reads: collector.reads,
                rebinds: collector.rebinds,
                calls: collector.calls,
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
    StructuralChunkAnalysis {
        body,
        shadowed,
        per_statement,
        top_level_await,
    }
}

/// Compute the policy-dependent half of the chunk analysis from the
/// pre-computed structural layer and a set of `AnalysisHints`. Builds
/// the chunk code graph (hint-gated purity propagation), the
/// local-effect context (gated by `hints.local_effect_policy`), the
/// per-statement purity classification, and the redundant-hint
/// diagnostics. Folds the policy-independent facts in to produce the
/// full [`ChunkFactAnalysis`].
pub(crate) fn analyze_chunk_with_policy<F>(
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisHints,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> ChunkFactAnalysis
where
    F: FnMut(Span) -> Option<(usize, usize)>,
{
    let StructuralChunkAnalysis {
        body,
        shadowed,
        per_statement,
        top_level_await,
    } = structural;
    let graph = ChunkCodeGraph::build_full(
        &body,
        &shadowed,
        &hints.declared_pure,
        &hints.declared_pure_new,
        &hints.declared_pure_members,
        &hints.imported_purities,
        &hints.fluent_bindings,
    );
    let local_effect_context =
        local_effects::LocalEffectContext::for_body(&body, hints.local_effect_policy);
    let redundant_purity_hints =
        detect_redundant_purity_hints(&body, &shadowed, &hints.declared_pure);
    let redundant_pure_member_hints =
        detect_redundant_pure_member_hints(&hints.declared_pure_members);
    let facts = body
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
            );
            // Resolve any reason spans on `fact.purity` to
            // SourceLocations using the same per-chunk line index.
            // Done after fact construction because the classifier
            // doesn't have access to the line resolver.
            if let Some(source_path) = source_path
                && let Purity::NotPure { reasons } = &mut fact.purity
            {
                for reason in reasons.iter_mut() {
                    reason.source_location =
                        line_range_for_span(reason.span).map(|(start_line, end_line)| {
                            SourceLocation {
                                source_path: source_path.to_string(),
                                start_line,
                                end_line,
                            }
                        });
                }
            }
            fact
        })
        .collect();
    ChunkFactAnalysis {
        facts,
        top_level_await,
        redundant_purity_hints,
        redundant_pure_member_hints,
    }
}

pub fn analyze_chunk<F>(
    module: &Module,
    hints: &AnalysisHints,
    source_path: Option<&str>,
    mut line_range_for_span: F,
) -> ChunkFactAnalysis
where
    F: FnMut(Span) -> Option<(usize, usize)>,
{
    let structural = analyze_chunk_structural(module, source_path, &mut line_range_for_span);
    analyze_chunk_with_policy(structural, hints, source_path, line_range_for_span)
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
    hints: &AnalysisHints,
    graph: &ChunkCodeGraph,
    local_effect_context: &local_effects::LocalEffectContext,
) -> StatementFacts {
    let StructuralStatementFacts {
        ordinal,
        source_location,
        kind,
        declared,
        reads,
        rebinds,
        calls,
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
    let purity = item_purity(
        item,
        kind,
        shadowed,
        hints,
        graph,
        !local_effects.is_empty(),
    );
    // Opaque at-init calls: a call/new the purity classifier can't
    // prove Pure may touch any cell (I/O like `console.log`, global
    // props written inside callee bodies, indirect eval) — the
    // statement must fall back to the strict S-chain. Classifier-Pure
    // calls are exempt: Pure guarantees no observable writes and no
    // global-prop reads, and binding-cell ordering is enforced by
    // binding edges + rebind co-location rather than the S-chain.
    let trusted_summary = hints.trusted_dataflow_summaries && cell_writes_summarizable;
    let dataflow_summarizable = trusted_summary
        || dataflow_summarizable && !has_opaque_at_init_call(item, shadowed, hints, graph);
    // A pure top-level statement may still eagerly read ordinary
    // argument bindings (`pureWrap(B)`), and those reads stay in
    // `reads.eager`. What purity rules out is the unresolved-call
    // fallback's stronger assumption that opaque member calls or
    // wrapper calls may synchronously invoke function-valued
    // arguments / object-held functions at module init. Dropping only
    // the at-init fallback roots preserves direct R edges while
    // avoiding promoted callback-body reads for audited pure factories
    // such as React `forwardRef`.
    let no_sync_arg_sources = no_sync_member_argument_fallback_sources(item, hints);
    let mut first_order_unresolved_sources = first_order_unresolved_sources;
    for id in &no_sync_arg_sources.first_order_lazy {
        first_order_unresolved_sources.remove(id);
    }
    let (at_init_unresolved_sources, at_init_unresolved_inline_fn, first_order_unresolved_sources) =
        if purity.is_pure() {
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
        declared,
        reads,
        rebinds,
        local_effects,
        calls,
        at_init_unresolved_sources,
        at_init_unresolved_inline_fn,
        first_order_unresolved_sources,
        first_order_unresolved_inline_fn,
        declares_direct_function,
        global_writes,
        global_reads,
        cell_writes_summarizable,
        dataflow_summarizable,
        purity,
        kind,
    }
}
