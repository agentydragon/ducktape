//! Per-chunk materialization: take an `OwnerGraphAndUnits` + spec plan, run the
//! chunk through `lower_chunk`, and emit a `MaterializedLogicalChunk` whose
//! files/applied/report are spliced into the artifact by
//! `apply_materialized_logical_chunks`.

mod apply;
mod plan_builder;

pub(super) use apply::apply_materialized_logical_chunks;
use plan_builder::{ChunkPlan, ChunkPlanBuilder, ExplicitRequestContext};

use super::io::write_chunk_report_json;
use super::util::{render_atomic_unit_cause_guidance, target_file_for_request};
use super::*;
use crate::time_phase;
use js_ast::statement_ordinal_for_body_index;
use output_layout::{ATOMIC_UNIT_CONFLICTS_REPORT, CYCLES_REPORT, OWNER_GRAPH_REPORT};

/// Per-chunk inputs that identify the chunk and where its outputs
/// should land. Bundled into `MaterializeLogicalChunkInputs::context`.
pub(super) struct ChunkContext<'a> {
    pub(super) artifact: &'a ChunkBundle,
    pub(super) artifact_indexes: &'a ArtifactIndexes,
    pub(super) chunk_id: &'a str,
    pub(super) file: Option<&'a str>,
    pub(super) target_dir: &'a str,
    pub(super) report_emission: &'a ReportEmission,
    /// Program-level cross-module purity output; this chunk's entries land
    /// in `AnalysisHints::imported_purities` / `declared_pure_members`.
    pub(super) cross_module_purities: &'a super::cross_module::CrossModulePurities,
    /// Vendor-plan consultation for runtime re-import construction
    /// (vendor_into_emission §2.4); `None` when no vendor mark could
    /// affect construction.
    pub(super) vendor_import_oracle: Option<&'a VendorReimportOracle<'a>>,
}

/// Spec-derived per-chunk inputs: logical-module layout, chunk
/// renames, unassigned-mode, and analysis-options. Bundled into
/// `MaterializeLogicalChunkInputs::spec`.
pub(super) struct ChunkSpec<'a> {
    pub(super) logical_modules: &'a BTreeMap<String, BTreeMap<String, LogicalModule>>,
    pub(super) chunk_renames: &'a BTreeMap<String, ChunkRenames>,
    pub(super) unassigned_mode: &'a BTreeMap<String, UnassignedMode>,
    pub(super) chunk_analysis_options: &'a BTreeMap<String, OwnerGraphOptions>,
}

pub(super) struct MaterializeLogicalChunkInputs<'a> {
    pub(super) context: ChunkContext<'a>,
    pub(super) spec: ChunkSpec<'a>,
}

pub(super) struct MaterializedLogicalChunk {
    pub(super) chunk_id: ChunkId,
    pub(super) target_file: String,
    pub(super) source_path: String,
    pub(super) files: Vec<JsFile>,
    pub(super) file_records: Vec<(String, FileRole)>,
    pub(super) applied: Vec<SelectedModuleLowering>,
    pub(super) directory_dependency_facts: Vec<DirectoryDependencyFact>,
    pub(super) validation: ChunkValidationSummary,
    pub(super) report: ChunkModulesReport,
    /// Per-symbol vendor-swap rewrite counts applied at construction
    /// time in this chunk's materialized module bodies; rolled up by
    /// `materialize_logical_modules` for the pipeline's manifest fold.
    pub(super) vendor_reference_rewrites: BTreeMap<(ChunkId, String), usize>,
    /// Spec member claims that named a binding for which no
    /// top-level declaration exists in this chunk. Materialization
    /// continues — the binding silently falls through to the
    /// residual sweep — but `materialize_logical_modules` rolls
    /// these up across every chunk and fails the pipeline at the
    /// end with the full list.
    pub(super) unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
}

pub(super) fn materialize_logical_chunk(
    inputs: MaterializeLogicalChunkInputs<'_>,
) -> Result<MaterializedLogicalChunk> {
    let MaterializeLogicalChunkInputs { context, spec } = inputs;
    let ChunkContext {
        artifact,
        artifact_indexes,
        chunk_id,
        file,
        target_dir,
        report_emission,
        cross_module_purities,
        vendor_import_oracle,
    } = context;
    let ChunkSpec {
        logical_modules,
        chunk_renames,
        unassigned_mode,
        chunk_analysis_options,
    } = spec;
    // The spec validator (`validate_transform_spec`) enforces that
    // every materialised chunk has an `unassigned_mode` entry, so
    // this lookup must not miss. Missing here is a bug in the
    // validator, not a recoverable spec error.
    let chunk_unassigned_mode = unassigned_mode.get(chunk_id).cloned().with_context(|| {
        format!("materialize_logical_modules missing unassigned_mode for chunk: {chunk_id}")
    })?;
    let chunk_id_interned = artifact
        .chunk_table
        .get(chunk_id)
        .with_context(|| format!("materialize_logical_modules unknown chunk: {chunk_id}"))?;
    let chunk_started = Instant::now();
    let mut timings = PhaseTimings::default();
    let target_file = time_phase!(timings, "resolve_entry", {
        file.map(normalize_module_path)
            .transpose()?
            .or_else(|| get_chunk_entry_path(artifact, chunk_id_interned))
            .with_context(|| {
                format!(
                    "materialize_logical_modules could not determine entry file for chunk: {chunk_id}"
                )
            })
    })?;
    let runtime_file = artifact
        .js_chunk(chunk_id_interned)?
        .get_file(&target_file)
        .with_context(|| {
            format!("materialize_logical_modules missing entry file for chunk: {chunk_id}")
        })?;
    let runtime_ast = runtime_file.ast().with_context(|| {
        format!("materialize_logical_modules missing entry AST for chunk: {chunk_id}")
    })?;
    // Chunk-wide `top_level_mark` for resolving spec-derived String
    // binding names to hygiene-aware `Id`s via `top_level_id`.
    let chunk_top_level_mark = runtime_ast.top_level_mark;
    let header_lines = runtime_file.header_lines.clone();
    let source_path = runtime_file.metadata.source_path.clone();
    let chunk_ast_analysis = time_phase!(timings, "analyze_chunk_ast", {
        analyze_chunk_ast(&runtime_ast.module)
    });
    let ChunkAstAnalysis {
        runtime_import_facts,
        declarations,
        declaration_by_name,
        destructure_siblings,
        pre_existing_entry_exports,
        pre_existing_public_export_names,
    } = chunk_ast_analysis;
    let requests = time_phase!(timings, "build_requests", {
        logical_requests_for_chunk(
            logical_modules.get(chunk_id),
            &chunk_unassigned_mode,
            chunk_renames.contains_key(chunk_id),
            chunk_id,
            target_dir,
        )
    })?;
    let mut explicit_requests = requests
        .iter()
        .filter(|request| !request.residual)
        .cloned()
        .collect::<Vec<_>>();
    let residual_request = requests.iter().find(|request| request.residual).cloned();

    let build_module_plans_started = Instant::now();
    let mut builder = ChunkPlanBuilder::new();
    let mut imported_binding_resolver =
        ArtifactSourceImportResolutionCache::new(artifact, artifact_indexes);
    let mut imported_from_by_src = BTreeMap::<String, String>::new();
    let explicit_request_ctx = ExplicitRequestContext {
        runtime_module: &runtime_ast.module,
        declaration_by_name: &declaration_by_name,
        chunk_top_level_mark,
        target_dir,
        chunk_id,
        target_file: &target_file,
        runtime_import_facts: &runtime_import_facts,
    };
    for (index, request) in explicit_requests.iter_mut().enumerate() {
        builder.add_explicit_request(
            index,
            request,
            &explicit_request_ctx,
            &mut imported_binding_resolver,
            &mut imported_from_by_src,
        )?;
    }
    drop(imported_binding_resolver);
    builder.drop_explicit_request_scratch();
    builder.pull_destructure_siblings(&destructure_siblings, chunk_top_level_mark)?;
    builder.adopt_bindings_of_claimed_anonymous_statements(&declarations);
    builder.add_residual_sweep(
        residual_request.as_ref(),
        chunk_unassigned_mode.catchall_file_target(),
        &declarations,
        target_dir,
    )?;
    timings.add("build_module_plans", build_module_plans_started.elapsed());

    // Fetched before hints assembly: `local_property_effects` selects
    // the facts pass's local-effect policy, which travels in the hints.
    let owner_graph_options = chunk_analysis_options
        .get(chunk_id)
        .copied()
        .unwrap_or_default();
    let analysis_hints: AnalysisHints = time_phase!(timings, "collect_analysis_hints", {
        let mut hints = collect_analysis_hints(&explicit_requests, chunk_renames.get(chunk_id));
        hints.imported_purities = cross_module_purities
            .bindings
            .get(chunk_id)
            .cloned()
            .unwrap_or_default();
        // Definition-side `pure_members` assertions projected onto this
        // chunk's local import bindings; merged (not overwritten) so spec
        // member annotations and cross-module assertions coexist.
        if let Some(member_sets) = cross_module_purities.members.get(chunk_id) {
            for (binding, members) in member_sets {
                hints
                    .declared_pure_members
                    .entry(binding.clone())
                    .or_default()
                    .extend(members.iter().cloned());
            }
        }
        // Definition-side `fluent_exports` assertions projected onto this
        // chunk's local import bindings (deep-purity chain roots).
        if let Some(fluent) = cross_module_purities.fluent.get(chunk_id) {
            hints.fluent_bindings.extend(fluent.iter().cloned());
        }
        if owner_graph_options.local_property_effects {
            hints.local_effect_policy = LocalEffectPolicy::LocalPropertyWrites;
        }
        hints.trusted_dataflow_summaries = owner_graph_options.trusted_dataflow_summaries;
        hints
    });
    let line_index = time_phase!(timings, "build_source_line_index", {
        runtime_ast.line_index()
    });
    // Stage A: spec-independent analysis (facts + owner graph +
    // structural atomic units). See `stage_one/mod.rs` for the composer.
    // A3 admission resolver: where does a dynamic-import specifier in
    // this chunk's entry land? Same artifact resolution the specifier
    // rewriter uses; `SameChunk` marks a debundled internal module.
    let resolve_dynamic_import = |specifier: &str| match artifact_indexes
        .resolve_runtime_import_reference(
            specifier,
            chunk_id_interned,
            &target_file,
            &artifact.chunk_table,
        ) {
        Some(resolved) if resolved.target_chunk_id == chunk_id_interned => {
            DynamicImportTarget::SameChunk
        }
        Some(_) => DynamicImportTarget::OtherChunk,
        None => DynamicImportTarget::External,
    };
    let stage_one = time_phase!(timings, "compute_stage_one_analysis", {
        compute_stage_one_analysis(
            chunk_id,
            &runtime_ast.module,
            &analysis_hints,
            Some(&source_path),
            |span| line_index.line_range_for_span(span),
            owner_graph_options,
            &resolve_dynamic_import,
        )?
    });
    let StageOneAnalysis {
        fact_analysis: analysis,
        owner_graph_and_units: precomputed,
    } = stage_one;
    time_phase!(timings, "fold_rebind_atomic_units", {
        apply_stage_one_a5(&mut builder, &precomputed);
    });
    if matches!(chunk_unassigned_mode, UnassignedMode::MiniFactors) {
        time_phase!(timings, "synthesize_mini_factor_plans", {
            builder.synthesize_mini_factors(&precomputed, &runtime_ast.module.body, target_dir)
        })?;
    }
    // Plan construction is finished — consume the builder and use
    // owned state from here on.
    let ChunkPlan {
        module_plans,
        binding_assignment,
        bindings_catalogue,
        anonymous_ordinal_assignment,
        unmatched_spec_claims,
    } = builder.finalize();
    // Plan structure is final — collect the explicit rename contributors
    // into the chunk's rename ledger and seal it. Seal hard-errors when
    // same-priority intents disagree on one binding's target; the sealed
    // output feeds the same application sites the pre-ledger maps fed
    // (Chunk scope → `chunk_renames_map` below, Module scope → the
    // per-plan naturalize pass in `lower_chunk`). No occupancy facts are
    // passed: the post-split bodies these renames must not collide with
    // don't exist yet, so target occupancy is validated downstream — the
    // entry ledger in `lower_chunk` re-collects the Chunk-scope intents
    // and the per-module naturalize ledgers re-collect the Module-scope
    // ones, each sealing against its body's occupancy.
    let sealed_renames = time_phase!(timings, "seal_rename_ledger", {
        let mut ledger = RenameLedger::default();
        if let Some(renames) = chunk_renames.get(chunk_id) {
            collect_chunk_renames(renames, chunk_top_level_mark, &mut ledger)?;
        }
        collect_plan_export_rename_intents(&module_plans, chunk_top_level_mark, &mut ledger);
        ledger.seal(&SealValidation::default())
    })?;
    let chunk_renames_map = sealed_renames.chunk_renames_by_name();
    let (logical_modules, default_destination) =
        time_phase!(timings, "project_factorization_modules", {
            project_factorization_modules_with_sentinel(
                &module_plans,
                &runtime_ast.module.body,
                chunk_top_level_mark,
                chunk_id,
                target_dir,
                &target_file,
                chunk_unassigned_mode.catchall_file_target(),
            )
        })?;
    let redundant_purity_hints = analysis.redundant_purity_hints;
    let factorization_chunk_renames: HashMap<Id, swc_atoms::Atom> = chunk_renames_map
        .iter()
        .map(|(local, exported)| {
            (
                top_level_id(local, chunk_top_level_mark),
                exported.as_str().into(),
            )
        })
        .collect();
    let factorization = time_phase!(timings, "build_factorization", {
        ChunkFactorization::build_with(
            chunk_id.to_string(),
            precomputed,
            bindings_catalogue,
            logical_modules,
            factorization_chunk_renames,
            default_destination,
        )
    });
    let factorization_report = time_phase!(timings, "validate_factorization", {
        factorization.validate()
    });
    validate_and_emit_reports(
        chunk_id,
        report_emission,
        &factorization,
        &factorization_report,
        &mut timings,
    )?;

    let lowered = time_phase!(timings, "lower_chunk_total", {
        lower_chunk(LowerChunkInputs {
            context: LowerChunkContext {
                artifact,
                artifact_indexes,
                chunk_id,
                chunk_id_interned,
                source_path: &source_path,
                entry_file: &target_file,
                header_lines: &header_lines,
                vendor_import_oracle,
            },
            ast: LowerChunkAst {
                runtime_ast,
                declarations: &declarations,
                declaration_by_name: &declaration_by_name,
                chunk_top_level_mark,
            },
            plan: LowerChunkPlan {
                module_plans: &module_plans,
                binding_assignment: &binding_assignment,
                anonymous_ordinal_assignment: &anonymous_ordinal_assignment,
                factorization: &factorization,
            },
            spec_facts: LowerChunkSpecFacts {
                runtime_import_facts: &runtime_import_facts,
                sealed_renames: &sealed_renames,
                chunk_renames: &chunk_renames_map,
                pre_existing_entry_exports: &pre_existing_entry_exports,
                pre_existing_public_export_names: &pre_existing_public_export_names,
            },
        })
    })?;
    let LoweredChunk {
        files,
        file_records,
        applied,
        vendor_reference_rewrites,
        timings: lower_timings,
    } = lowered;
    timings.extend_prefixed("lower", lower_timings);

    let final_modules = time_phase!(timings, "build_final_module_report", {
        build_final_module_report(&module_plans, &factorization, chunk_top_level_mark)
    });
    let directory_dependency_facts = time_phase!(timings, "build_directory_dependency_facts", {
        build_directory_dependency_facts(chunk_id, &factorization)
    });
    let validation = ChunkValidationSummary {
        status: "ok",
        linker_order: factorization_report.linker_order.clone(),
    };
    let timings = timings.into_durations(chunk_started.elapsed());
    let report = ChunkModulesReport {
        chunk_id: chunk_id.to_string(),
        counts: ChunkModulesCounts {
            applied: applied.len(),
            selected_owners: binding_assignment.len(),
        },
        final_module_contents: final_modules,
        requested_logical_modules: requests
            .iter()
            .map(|request| RequestedLogicalModule {
                target_path: spec::ModulePath::parse(&request.target_path, "")
                    .expect("request target_path is a canonical module path"),
                residual: request.residual,
            })
            .collect(),
        redundant_purity_hints,
        timings,
    };
    Ok(MaterializedLogicalChunk {
        chunk_id: chunk_id_interned,
        unmatched_spec_claims,
        target_file,
        source_path,
        files,
        file_records,
        applied,
        directory_dependency_facts,
        validation,
        report,
        vendor_reference_rewrites,
    })
}

/// Stage A.5 composer: fold rebind-only atomic units into their
/// explicit destination. Bridges the pure
/// `stage_one::compute_rebind_folds` decision over the chunk's
/// post-seed partition (managed by `ChunkPlanBuilder`) into the
/// builder's plan-list/catalogue state.
///
/// Stage A.5 runs after the seed phases of partition construction
/// (explicit requests, destructure pull, residual sweep) and before
/// `synthesize_mini_factors`. The decision step is spec-independent —
/// it only inspects Stage A's owner graph + atomic units, the
/// builder's binding→plan assignment, and the residual landing-site
/// index — so it lives in the `analysis` crate; this thin composer
/// owns the application of those decisions to the builder's
/// `ModulePlan` list.
///
/// See `ARCHITECTURE_BACKLOG.md` § "`compute_stage_one_analysis` —
/// only rebind-folding still leaks into the materializer" for the
/// original separation rationale.
fn apply_stage_one_a5(builder: &mut ChunkPlanBuilder, precomputed: &OwnerGraphAndUnits) {
    let folds = compute_rebind_folds(
        precomputed,
        builder.binding_assignment(),
        builder.residual_plan_index(),
    );
    builder.apply_rebind_folds(folds);
}

/// Build the `FactorizationLogicalModule` list the factorizer
/// consumes from the per-chunk `ModulePlan`s, then append the
/// "anon residual" sentinel that holds the partition's default
/// destination.
///
/// Commit 1 transitional behavior: the partition's "default
/// destination" — the module owners with no claim fall back to — is a
/// factorization-only sentinel logical module appended past
/// `module_plans.len()`. The emit loop iterates `module_plans`, so
/// the sentinel never gets emitted as a file. Anonymous statements
/// without an explicit logical-module `anonymous_statements` match
/// thus stay in the sentinel, preserving the pre-refactor split
/// where anon-fallback was a distinct destination from the residual
/// logical module (which only held named-unclaimed bindings). Commit
/// 2 collapses this sentinel back into the residual module via
/// explicit `anonymous_statement_ordinals` routing.
fn project_factorization_modules_with_sentinel(
    module_plans: &[ModulePlan],
    body: &[ModuleItem],
    chunk_top_level_mark: swc_common::Mark,
    chunk_id: &str,
    target_dir: &str,
    target_file: &str,
    catchall_target_for_overflow: Option<&str>,
) -> Result<(Vec<FactorizationLogicalModule>, ModuleId)> {
    let mut logical_modules: Vec<FactorizationLogicalModule> = module_plans
        .iter()
        .map(|plan| FactorizationLogicalModule {
            id: plan.id.clone(),
            target_file: plan.target_file.clone(),
            residual: !plan.explicit,
            rename_map: plan
                .bindings
                .iter()
                .map(|(local, exported)| {
                    (
                        top_level_id(local, chunk_top_level_mark),
                        exported.as_str().into(),
                    )
                })
                .collect(),
            // ChunkFactorization's owner graph uses post-comma-list-split
            // `StatementOrdinal`s; convert body indices here so the
            // destination override targets the right owner node (an
            // anon body item is always a single post-split position,
            // but earlier comma-list var-decls in the chunk shift the
            // count).
            anonymous_statement_ordinals: plan
                .anonymous_statement_ordinals
                .iter()
                .map(|body_idx| statement_ordinal_for_body_index(body, *body_idx))
                .collect(),
        })
        .collect();
    let sentinel_residual_target = catchall_target_for_overflow
        .map(|t| target_file_for_request(target_dir, t))
        .transpose()?
        .unwrap_or_else(|| target_file.to_string());
    let sentinel_idx = logical_modules.len();
    logical_modules.push(FactorizationLogicalModule {
        id: format!("{chunk_id}::anon_residual_sentinel"),
        target_file: sentinel_residual_target,
        residual: true,
        rename_map: HashMap::new(),
        anonymous_statement_ordinals: Vec::new(),
    });
    Ok((logical_modules, ModuleId(LogicalModuleIndex(sentinel_idx))))
}

/// Collect `AnalysisHints` from spec member annotations (purity,
/// pure_members, effect). Spec annotations carried on any member
/// form (logical-module member, chunk_renames member) propagate the
/// same way: collect them by local binding name and feed them into
/// fact analysis. They are semantic trust assertions, not ownership
/// claims; binding patches routed through chunk_renames still do not
/// force factorizer grouping.
fn collect_analysis_hints(
    explicit_requests: &[LogicalRequest],
    chunk_renames: Option<&ChunkRenames>,
) -> AnalysisHints {
    let mut hints = AnalysisHints::default();
    for req in explicit_requests {
        for m in &req.members {
            m.collect_hints(&mut hints);
        }
    }
    if let Some(cr) = chunk_renames {
        for m in super::plans::build_members(&cr.members, &[], "<chunk_renames>")
            .expect("chunk_renames members must use binding selectors")
        {
            m.collect_hints(&mut hints);
        }
    }
    hints
}

/// Write per-chunk validation reports (owner graph, atomic-unit
/// conflicts, cycles) and bail with a human-readable summary when
/// the spec is unrealizable.
///
/// Under [`ReportEmission::OnRejection`] (the `debundle run
/// --dry-run` mode) the accept path writes nothing, but a rejection
/// still writes `owner_graph.json` plus the rejection evidence so
/// the documented `debundle gate list/describe` follow-up works.
fn validate_and_emit_reports(
    chunk_id: &str,
    report_emission: &ReportEmission,
    factorization: &ChunkFactorization,
    factorization_report: &::gate::FactorizationReport,
    timings: &mut PhaseTimings,
) -> Result<()> {
    let rejected = !factorization_report.atomic_unit_conflicts.is_empty()
        || !factorization_report.cycles.is_empty();
    // Full mode writes the owner graph unconditionally; OnRejection
    // writes it only alongside rejection evidence (`gate describe`
    // recomputes per-edge blame from it).
    let report_out_dir = if rejected {
        report_emission.rejection_dir()
    } else {
        report_emission.full_dir()
    };
    if let Some(report_out_dir) = report_out_dir {
        let owner_graph_report = time_phase!(timings, "build_owner_graph_report", {
            factorization.owner_graph_report()
        });
        time_phase!(timings, "write_owner_graph_report", {
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                OWNER_GRAPH_REPORT,
                &owner_graph_report,
            )
        })?;
    }
    if !factorization_report.atomic_unit_conflicts.is_empty() {
        if let Some(report_out_dir) = report_out_dir {
            // Project onto the wire shape: owners as `"owner:N"`,
            // modules as canonical `ModulePath` — the same entity
            // keys `owner_graph.json` uses, so the file joins
            // against the owner graph without string surgery.
            let wire = ::analysis::AtomicUnitConflictReport::from_conflicts(
                &factorization_report.atomic_unit_conflicts,
                &|id| factorization.analysis.module_path(id),
            );
            time_phase!(timings, "write_atomic_unit_conflicts_report", {
                write_chunk_report_json(
                    report_out_dir,
                    chunk_id,
                    ATOMIC_UNIT_CONFLICTS_REPORT,
                    &wire,
                )
            })?;
        }
        let summary = render_atomic_unit_conflict_summary(
            &factorization_report.atomic_unit_conflicts,
            &|id| factorization.analysis.module_path(id),
        );
        let causes = render_atomic_unit_cause_guidance(&factorization_report.atomic_unit_conflicts);
        bail!(
            "materialize_logical_modules: chunk {chunk_id} has {n} atomic-factor-unit conflict(s) — the spec assigns members of one atomic factor unit to different destination modules, forming a cycle in the module dep graph that the constraining-edge SCC analysis says is unrealizable. Atomic factor units come from `G_atomic` SCC over the owner graph (docs/design.md §\"Two classes of atom\"); every member must co-locate. {causes}Resolve by reconciling each unit's claims into a single destination. Full evidence written to reports/tree/{chunk_id}/atomic_unit_conflicts.json; owner graph written to reports/tree/{chunk_id}/owner_graph.json. Summary:\n{summary}",
            n = factorization_report.atomic_unit_conflicts.len(),
        );
    }
    if !factorization_report.cycles.is_empty() {
        if let Some(report_out_dir) = report_out_dir {
            // Trim the wire shape before writing: keep `id` (array
            // index), `modules`, `cut`. Per-edge evidence is
            // recomputable from `owner_graph.json` + this entry's
            // `modules` set via `debundle gate describe <id>`. See
            // `validation.rs` `BlockingSccEntry` for the schema and
            // `docs/cli.md` § "Gate queries" for the CLI surface.
            let wire = ::gate::BlockingSccEntry::from_cycle_reports(&factorization_report.cycles);
            time_phase!(timings, "write_cycles_report", {
                write_chunk_report_json(report_out_dir, chunk_id, CYCLES_REPORT, &wire)
            })?;
        }
        let summary = render_cycle_summary(&factorization_report.cycles);
        bail!(
            "materialize_logical_modules: chunk {chunk_id} — spec is unrealizable: {n} module-quotient SCC(s) with at-init / side-effect edges between members. Each SCC names the binding pairs whose split forced the cycle; co-locate them or break a back-edge. Full per-cycle evidence at reports/tree/{chunk_id}/cycles.json; owner graph at reports/tree/{chunk_id}/owner_graph.json. Summary:\n{summary}",
            n = factorization_report.cycles.len(),
        );
    }
    // Defense-in-depth on the accept path: a cross-destination
    // rebinding write (clause 2) always co-locates with its target in
    // one atomic factor unit, so a spec splitting them must have
    // produced an `atomic_unit_conflicts` bail above. Reaching this
    // point with a non-empty clause-2 verdict means the atomic-unit
    // glue and the realizability gate disagree — refuse to emit
    // rather than materialize a bundle that reassigns an ESM import.
    if !factorization_report.cross_rebinds.is_empty() {
        bail!(
            "materialize_logical_modules: chunk {chunk_id} — realizability verdict carries {n} cross-destination rebind(s) but no atomic-unit conflict or blocking SCC was reported; this should be unreachable (rebinding writes co-locate with their target via atomic factor units). Rebinds:\n  {rebinds}",
            n = factorization_report.cross_rebinds.len(),
            rebinds = factorization_report.cross_rebinds.join("\n  "),
        );
    }
    Ok(())
}

/// Build the per-plan `FinalModuleContent` rows for `ChunkModulesReport`.
/// Each plan's binding map is iterated in name-sorted order so the
/// resulting JSON is deterministic across runs.
fn build_final_module_report(
    module_plans: &[ModulePlan],
    factorization: &ChunkFactorization,
    chunk_top_level_mark: swc_common::Mark,
) -> Vec<FinalModuleContent> {
    module_plans
        .iter()
        .map(|plan| {
            let mut sorted: Vec<(&String, &String)> = plan.bindings.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(b.0));
            let binding_names: Vec<String> = sorted.iter().map(|(k, _)| (*k).clone()).collect();
            let member_names: Vec<String> = sorted.iter().map(|(_, v)| (*v).clone()).collect();
            let binding_ids: Vec<Id> = binding_names
                .iter()
                .map(|name| top_level_id(name, chunk_top_level_mark))
                .collect();
            let owner_ids = factorization
                .analysis
                .owner_report_ids_for_bindings(binding_ids.iter());
            FinalModuleContent {
                binding_names,
                file: plan.target_file.clone(),
                member_names,
                path: spec::ModulePath::parse(&plan.target_path, "")
                    .expect("plan target_path is a canonical module path"),
                owner_ids,
                residual: !plan.explicit,
            }
        })
        .collect()
}

fn build_directory_dependency_facts(
    chunk_id: &str,
    factorization: &ChunkFactorization,
) -> Vec<DirectoryDependencyFact> {
    let mut facts = Vec::new();
    for edge in factorization.analysis.owner_graph().iter_edges() {
        let source_module = factorization.partition.of(edge.from);
        let target_module = factorization.partition.of(edge.to);
        if source_module == target_module {
            continue;
        }
        let Some(source_file) = module_output_file(chunk_id, factorization, source_module) else {
            continue;
        };
        let Some(target_file) = module_output_file(chunk_id, factorization, target_module) else {
            continue;
        };
        let symbol = edge.reason.binding().map(|id| {
            format!(
                "{}#{}",
                target_file,
                factorization.analysis.export_name_for(id)
            )
        });
        facts.push(DirectoryDependencyFact {
            source_file,
            target_file,
            edge_kind: edge.reason.kind(),
            symbol,
        });
    }
    facts.sort_by(|left, right| {
        left.source_file
            .cmp(&right.source_file)
            .then_with(|| left.target_file.cmp(&right.target_file))
            .then_with(|| left.edge_kind.cmp(&right.edge_kind))
            .then_with(|| left.symbol.cmp(&right.symbol))
    });
    facts
}

fn module_output_file(
    chunk_id: &str,
    factorization: &ChunkFactorization,
    module: ModuleId,
) -> Option<String> {
    factorization
        .analysis
        .logical_module(module.0)
        .map(|logical| join_module_path(&[chunk_id, &logical.target_file]))
}
