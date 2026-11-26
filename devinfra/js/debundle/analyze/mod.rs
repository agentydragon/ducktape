//! Chunk fact analysis: completes the structural layer from
//! `analysis::facts` with synchronous-execution facts from `sync_effects`,
//! purity classification and local effects into a `ChunkFactAnalysis`.
//! Sits above both crates; `analyze_chunk` is the entry point.

use analysis::facts::analyze::ExportReentryPolicy;
use analysis::facts::analyze::analyze_chunk_structural;
use analysis::facts::at_init_fallback::has_opaque_at_init_call;
use analysis::facts::at_init_fallback::has_untrusted_at_init_unresolved_inline_fn_call;
use analysis::facts::at_init_fallback::no_sync_member_argument_fallback_sources;
use analysis::facts::at_init_fallback::safe_plain_array_at_init_fallback_sources;
use analysis::facts::global_object::resolve_global_alias_escape;
use analysis::facts::global_object::unshadowed_global_object_aliases;
use analysis::facts::local_effect_targets::collect_local_effects;
use analysis::facts::purity_classification::CompletedPurity;
use analysis::facts::purity_classification::ItemPurityProofs;
use analysis::facts::purity_classification::StatementPurityContext;
use analysis::facts::purity_classification::has_trusted_initializer_purity;
use analysis::facts::statement_facts::ChunkFactAnalysis;
use analysis::facts::statement_facts::DataflowOpacityCause;
use analysis::facts::statement_facts::StatementFacts;
use analysis::facts::statement_facts::StructuralChunkAnalysis;
use analysis::facts::statement_facts::StructuralStatementFacts;
use analysis::purity::code_graph::ChunkCodeGraph;
use analysis::purity::redundant_hints::detect_redundant_pure_member_hints;
use analysis::purity::redundant_hints::detect_redundant_purity_hints;
use analysis_vocabulary::execution_evidence::SourceLocator;
use analysis_vocabulary::inputs::AnalysisInputs;
use analysis_vocabulary::inputs::LocalEffectPolicy;
use analysis_vocabulary::inputs::ValidatedNativeOperationFacts;
use analysis_vocabulary::purity::Purity;
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use swc_ecma_ast::{Id, Module, ModuleItem};
#[cfg(test)]
mod at_init_fallback_tests;
#[cfg(test)]
mod classifier_tests;
#[cfg(test)]
mod diagnostics_tests;
#[cfg(test)]
mod fact_analysis_tests;
#[cfg(test)]
mod graph_purity_tests;
#[cfg(test)]
mod initializer_overlay_tests;
#[cfg(test)]
mod redundant_hints_tests;
#[cfg(test)]
mod test_support;

/// Compute the policy-dependent half of the chunk analysis from the
/// pre-computed structural layer and a set of `AnalysisInputs`. Builds
/// the chunk code graph (hint-gated purity propagation), the
/// local-effect context (gated by `hints.local_effect_policy`), the
/// per-statement purity classification, and the redundant-hint facts. Folds
/// the policy-independent facts in to produce the
/// full [`ChunkFactAnalysis`].
pub fn analyze_chunk_with_policy(
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisInputs,
    export_reentry_policy: &ExportReentryPolicy,
    locator: Option<SourceLocator<'_>>,
    metrics: &work_metrics::WorkMetrics,
) -> ChunkFactAnalysis {
    analyze_chunk_with_policy_and_sync(
        structural,
        hints,
        export_reentry_policy,
        locator,
        SynchronousCallAnalysis::Include,
        metrics,
    )
}

#[derive(Clone, Copy)]
enum SynchronousCallAnalysis {
    Include,
    Omit,
}

fn analyze_chunk_with_policy_and_sync(
    structural: StructuralChunkAnalysis<'_>,
    hints: &AnalysisInputs,
    export_reentry_policy: &ExportReentryPolicy,
    locator: Option<SourceLocator<'_>>,
    synchronous_calls: SynchronousCallAnalysis,
    metrics: &work_metrics::WorkMetrics,
) -> ChunkFactAnalysis {
    let StructuralChunkAnalysis {
        body,
        stable_array_spread_shapes,
        imported_bindings,
        value_flow,
        dynamic_scope,
        set_iterable_shapes,
        local_class_shapes,
        plain_data_bindings,
        stable_var_number_candidates,
        plain_data_dependencies,
        plain_data_inference_uses,
        source_semantic_facts,
        shadowed,
        per_statement,
        top_level_await,
    } = structural;
    let plain_array_bindings =
        analysis::facts::abstract_domain::heap::plain_array_bindings(&body, &shadowed);
    let mut prepared_sync_index =
        sync_effects::interpreter::synchronous_calls::prepare_synchronous_call_index_with_source_facts(
            &body,
            &value_flow,
            hints,
            &plain_array_bindings,
            sync_effects::interpreter::synchronous_calls::PreparedSyncShapeInputs {
                local_class_construction: &local_class_shapes,
                set_iterable_shapes,
                stable_array_spread_shapes,
                shadowed: &shadowed,
                source_facts: source_semantic_facts.as_ref(),
            },
        );
    let native_facts =
        ValidatedNativeOperationFacts::new(prepared_sync_index.resolved_native_operation_inputs());
    prepared_sync_index.locate_native_integrity_witnesses(|span| locator?.span(span));
    let local_class_construction_proof =
        prepared_sync_index.local_class_construction_proof().clone();
    let set_iterable_proof = prepared_sync_index.set_iterable_proof().clone();
    let graph = ChunkCodeGraph::build_full_with_prepared_plain_data(
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
        &plain_data_dependencies,
        &plain_data_inference_uses,
        &plain_array_bindings,
        &stable_var_number_candidates,
        &hints.stable_imports,
        locator,
    );
    let local_effect_context = analysis::facts::local_effects::LocalEffectContext::for_body(
        &body,
        hints.local_effect_policy,
        &hints.assumptions.known_effects,
        &value_flow,
        &per_statement,
        &imported_bindings,
    );
    let redundant_purity_hints = detect_redundant_purity_hints(
        &body,
        &shadowed,
        &hints.assumptions.declared_pure,
        &sync_effects::interpreter::synchronous_calls::SynchronousShapeRefinement,
    );
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
    let rebindings: BTreeSet<Id> = per_statement
        .iter()
        .flat_map(|statement| {
            statement
                .rebinds
                .lazy
                .iter()
                .chain(&statement.rebinds.eager)
        })
        .cloned()
        .collect();
    let fresh_record_destructuring = analysis::purity::FreshRecordDestructuringContext {
        flow: &value_flow,
        dynamic_scope,
        rebindings: &rebindings,
    };
    let native_own_data_reads = per_statement
        .iter()
        .map(|statement| {
            (
                statement.ordinal,
                prepared_sync_index.native_own_data_read_diagnostics(statement.ordinal),
            )
        })
        .collect();
    let mut facts: Vec<StatementFacts> = body
        .iter()
        .zip(per_statement)
        .map(|(item_view, structural_facts)| {
            assemble_statement_facts(
                item_view.as_module_item(),
                structural_facts,
                &shadowed,
                hints,
                &native_facts,
                &graph,
                &local_effect_context,
            )
        })
        .collect();
    for fact in &mut facts {
        if let Some(producers) = set_iterable_proof.stable_iterable_producers(fact.ordinal) {
            fact.stable_iterable_producers = producers.clone();
        }
    }
    // Protect the same source interval in the canonical graph, so every
    // placement validator (including staged edit commands) sees this proof's
    // co-location obligation before emission.
    let binding_owners: BTreeMap<_, _> = facts
        .iter()
        .flat_map(|fact| {
            fact.declared
                .iter()
                .map(move |id| (id.clone(), fact.ordinal))
        })
        .collect();
    for interval in set_iterable_proof.protected_intervals() {
        let Some(consumer) = interval
            .last()
            .and_then(|id| binding_owners.get(id))
            .copied()
        else {
            continue;
        };
        for binding in interval {
            if let Some(producer) = binding_owners
                .get(binding)
                .filter(|owner| **owner != consumer)
            {
                facts[consumer.0]
                    .stable_iterable_producers
                    .insert(*producer);
            }
        }
    }
    value_flow.augment_calls(&body, &mut facts);
    let local_class_heap_proof = prepared_sync_index.local_class_heap_proof();
    let fresh_freeze_heap_proof = prepared_sync_index.fresh_record_freeze_heap_proof();
    let heap_contents = analysis::facts::heap_contents::annotate_heap_contents(
        &body,
        &value_flow,
        analysis::facts::heap_contents::HeapContentProofs {
            local_class: &local_class_heap_proof,
            set_iterable: &set_iterable_proof,
            fresh_freeze: &fresh_freeze_heap_proof,
            native_reads: &prepared_sync_index,
            native_facts: &native_facts,
            shadowed: &shadowed,
        },
        &mut facts,
    );
    let purity_context = StatementPurityContext {
        body: &body,
        shadowed: &shadowed,
        hints,
        graph: &graph,
        proofs: ItemPurityProofs {
            native_operations: &native_facts,
            fresh_record_destructuring: &fresh_record_destructuring,
        },
        shared_vars: &shared_vars,
        native_own_data_reads: &native_own_data_reads,
    };
    // Current-owner proof preparation consumes the unchanged classifier.
    // Completed purity is published after evaluation and exact certificate
    // discharge. Vendor pruning finalizes the same classifier without execution.
    let purities = match synchronous_calls {
        SynchronousCallAnalysis::Include => {
            sync_effects::interpreter::synchronous_calls::annotate_synchronous_calls(
                &value_flow,
                sync_effects::interpreter::synchronous_calls::SynchronousCallProofs {
                    set_iterable: &set_iterable_proof,
                    graph: &graph,
                    purity: &purity_context,
                    callable_heap_contents: &heap_contents.callables,
                    index: prepared_sync_index,
                },
                &shadowed,
                hints,
                export_reentry_policy,
                &mut facts,
                metrics,
            )
        }
        SynchronousCallAnalysis::Omit => facts
            .iter()
            .map(|fact| CompletedPurity::unrefined(purity_context.statement_effects(fact)))
            .collect(),
    };
    for fact in &mut facts {
        if let Some(span) = fact
            .synchronous_calls
            .as_ref()
            .and_then(|calls| calls.local_record_factory_heap_call)
            && let Some(arguments) = heap_contents.checked_factory_arguments.get(&span.lo.0)
        {
            fact.heap_contents = arguments.clone();
        }
    }
    let global_object_names = unshadowed_global_object_aliases(&body);
    for ((fact, purity), item_view) in facts.iter_mut().zip(purities).zip(&body) {
        settle_statement_purity(
            fact,
            purity,
            item_view.as_module_item(),
            &shadowed,
            hints,
            &graph,
            &global_object_names,
        );
    }
    for fact in &mut facts {
        // Resolve any reason spans on `fact.purity` to SourceLocations using
        // the same per-chunk line index; the classifier has no line resolver.
        if let Some(locator) = locator
            && let Purity::NotPure { reasons } = &mut fact.purity
        {
            for reason in reasons.iter_mut() {
                reason.source_location = locator.span(reason.span);
            }
        }
    }
    for (fact, (inferences, inputs)) in facts.iter_mut().zip(heap_contents.native_read_uses) {
        if let Some(calls) = &mut fact.synchronous_calls {
            for use_ in inferences {
                if !calls.inference_uses.contains(&use_) {
                    calls.inference_uses.push(use_);
                }
            }
            for use_ in inputs {
                if !calls.native_operation_input_uses.contains(&use_) {
                    calls.native_operation_input_uses.push(use_);
                }
            }
        }
    }
    if let Some(locator) = locator {
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
                        locator.span(loss.span)
                    };
                }
                for proof_use in &mut calls.callback_factory_uses {
                    proof_use.source_location = locator.span(proof_use.span);
                }
                for proof_use in &mut calls.returned_record_uses {
                    proof_use.source_location = locator.span(proof_use.span);
                }
                for proof_use in &mut calls.local_record_factory_uses {
                    proof_use.source_location = locator.span(proof_use.span);
                }
                for rejection in &mut calls.local_record_factory_rejections {
                    rejection.source_location = locator.span(rejection.span);
                }
                for proof_use in &mut calls.author_host_contract_uses {
                    proof_use.source_location = locator.span(proof_use.span);
                }
                for proof_use in &mut calls.native_operation_input_uses {
                    proof_use.source_location = locator.span(proof_use.span);
                }
            }
        }
    }
    let read_names: BTreeSet<&str> = facts
        .iter()
        .flat_map(|fact| fact.reads.eager.iter().chain(&fact.reads.lazy))
        .map(|id| id.0.as_ref())
        .collect();
    let unconsulted_purity_hints = hints
        .assumptions
        .declared_pure
        .iter()
        .filter(|name| !read_names.contains(name.as_str()))
        .cloned()
        .collect();
    ChunkFactAnalysis {
        facts,
        top_level_await,
        redundant_purity_hints,
        redundant_pure_member_hints,
        unconsulted_purity_hints,
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
    let structural = analyze_chunk_structural(module, None);
    analyze_chunk_with_policy_and_sync(
        structural,
        &hints,
        &ExportReentryPolicy::Conservative,
        None,
        SynchronousCallAnalysis::Omit,
        &work_metrics::WorkMetrics::default(),
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
pub fn analyze_chunk(
    module: &Module,
    hints: &AnalysisInputs,
    locator: Option<SourceLocator<'_>>,
) -> ChunkFactAnalysis {
    let structural = analyze_chunk_structural(module, locator);
    analyze_chunk_with_policy(
        structural,
        hints,
        &ExportReentryPolicy::Conservative,
        locator,
        &work_metrics::WorkMetrics::default(),
    )
}

/// Fold the policy-independent per-statement facts together with the
/// policy-dependent local-effect classification into a [`StatementFacts`]
/// record. Called once per statement by [`analyze_chunk_with_policy`] after
/// the policy-independent layer has already walked the AST. Purity and the
/// fallback pruning it gates are settled later by
/// [`settle_statement_purity`].
fn assemble_statement_facts(
    item: &ModuleItem,
    structural: StructuralStatementFacts,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    native_facts: &ValidatedNativeOperationFacts,
    graph: &ChunkCodeGraph,
    local_effect_context: &analysis::facts::local_effects::LocalEffectContext,
) -> StatementFacts {
    let StructuralStatementFacts {
        ordinal,
        export_linkage,
        source_location,
        kind,
        declared,
        local_bindings,
        var_declared,
        var_initializers,
        var_declaration_counts: _,
        var_initializer_counts: _,
        top_level_var_initializer_candidates: _,
        reads,
        rebinds,
        member_write_roots,
        calls,
        constructor,
        mut at_init_unresolved_sources,
        at_init_unresolved_inline_fn,
        mut first_order_unresolved_sources,
        first_order_unresolved_inline_fn,
        declares_direct_function,
        global_writes,
        global_reads,
        mut havoc,
        argument_value_escapes: _,
    } = structural;
    let local_effects = collect_local_effects(item, shadowed, hints, graph, local_effect_context);
    // Opaque at-init calls: a call/new the purity classifier can't
    // prove Pure may touch any cell (I/O like `console.log`, global
    // props written inside callee bodies, indirect eval) — the
    // statement must fall back to the strict S-chain. Classifier-Pure
    // calls are exempt: Pure guarantees no observable writes and no
    // global-prop reads, and binding-cell ordering is enforced by
    // binding edges + rebind co-location rather than the S-chain.
    // The trusted syntactic summary stands in for every havoc but a
    // dynamic cell write, which no summary can name.
    // `trusted_summary_discharged` records every cause the summary removed, so
    // an empty set means `trusted_dataflow_summaries` changed nothing here.
    let mut trusted_summary_discharged = BTreeSet::new();
    if hints.assumptions.trusted_dataflow_summaries
        && !havoc.contains(&DataflowOpacityCause::DynamicCellWrites)
    {
        trusted_summary_discharged = std::mem::take(&mut havoc);
        if has_opaque_at_init_call(item, shadowed, hints, native_facts, graph) {
            trusted_summary_discharged.insert(DataflowOpacityCause::OpaqueAtInitCall);
        }
    } else if has_opaque_at_init_call(item, shadowed, hints, native_facts, graph) {
        havoc.insert(DataflowOpacityCause::OpaqueAtInitCall);
    }
    let no_sync_arg_sources = no_sync_member_argument_fallback_sources(item, hints);
    for id in &no_sync_arg_sources.first_order_lazy {
        first_order_unresolved_sources.remove(id);
    }
    for id in &no_sync_arg_sources.eager {
        at_init_unresolved_sources.remove(id);
    }

    StatementFacts {
        ordinal,
        export_linkage,
        stable_iterable_producers: BTreeSet::new(),
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
        synchronous_prefix_effects: None,
        at_init_unresolved_sources,
        at_init_unresolved_inline_fn,
        first_order_unresolved_sources,
        first_order_unresolved_inline_fn,
        declares_direct_function,
        global_writes,
        global_reads,
        havoc,
        trusted_summary_discharged,
        heap_contents: analysis::facts::HeapContentCells::default(),
        heap_one_shot_allocations: BTreeSet::new(),
        deferred_heap_allocations: BTreeSet::new(),
        heap_native_integrity_uses: Vec::new(),
        // Conservative until `settle_statement_purity` records the verdict.
        purity: Purity::NotPure {
            reasons: Vec::new(),
        },
        kind,
    }
}

/// Record a statement's purity and prune the at-init fallback it gates.
///
/// Ordinary eager reads (including `pureWrap(B)`) always remain in
/// `reads.eager`. For completed-pure statements, clearing the
/// unresolved-call fallback also asserts that opaque calls cannot
/// synchronously invoke callback bodies at module init. An
/// initializer-only trust annotation certifies the initializer's
/// evaluation effects but deliberately does not make that callback
/// assertion: retain its fallback roots so callback-body binding reads
/// still constrain module order.
fn settle_statement_purity(
    fact: &mut StatementFacts,
    purity: CompletedPurity,
    item: &ModuleItem,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    graph: &ChunkCodeGraph,
    global_object_names: &BTreeSet<&'static str>,
) {
    if let Some(aliases) = fact
        .synchronous_calls
        .as_ref()
        .and_then(|calls| calls.global_object_aliases.clone())
    {
        resolve_global_alias_escape(item, global_object_names, &aliases, fact);
    }
    if purity.is_pure() && !has_trusted_initializer_purity(item, hints) {
        fact.at_init_unresolved_sources.clear();
        fact.at_init_unresolved_inline_fn = false;
    } else {
        let safe_array_sources = safe_plain_array_at_init_fallback_sources(item, shadowed, graph);
        for id in &safe_array_sources {
            fact.at_init_unresolved_sources.remove(id);
        }
        fact.at_init_unresolved_inline_fn = fact.at_init_unresolved_inline_fn
            && has_untrusted_at_init_unresolved_inline_fn_call(item, hints);
    }
    fact.purity = purity.verdict;
}
