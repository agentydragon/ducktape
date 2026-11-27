//! Gate evaluation and per-chunk report emission for a finalized assignment.

use super::ChunkContext;
use super::plan_builder::ChunkPlanBuilder;
use crate::io::{ProgramGraphOutput, chunk_report_path, write_chunk_report_json};
use crate::plans::ModulePlan;
use crate::{
    BindingSourceUnitAssociation, ExportReentryReport, FinalModuleContent, GateRejectionPolicy,
    GraphReportSelection, ReportEmission,
};
use analysis::facts::ChunkFactAnalysis;
use analysis::reports::spec_lint::{ChunkSpecLint, LintFinding, LoaderKeyUses, PurityHintOwner};
use analysis::{BindingId, ModuleId, ModuleRole, top_level_id};
use analysis_model::DepKind;
use analysis_projection::AnalysisProjection;
use anyhow::{Result, bail, ensure};
use artifact::paths::join_module_path;
use artifact::reports::DirectoryDependencyFact;
use frontend_elaboration::ResolvedMemberSite;
use gate::{
    AssignmentAnalysis, ChunkGateRejection, GateVerdict, RejectedAssignment, ValidatedAssignment,
    render_atomic_group_conflict_summary, render_cycle_summary,
};
use graph::AtomicGroupConflict;
use graph::report::source_unit_key;
use output_layout::{
    ATOMIC_GROUP_CONFLICTS_REPORT, CANDIDATE_RELOCATION_ASSESSMENT_REPORT, CYCLES_REPORT,
    DEPENDENCY_GRAPH_REPORT, EFFECT_TRANSLATION_VALIDATION_REPORT, EXPORT_REENTRY_REPORT,
    SELECTOR_DIAGNOSTICS_REPORT, SPEC_LINT_REPORT, TRANSLATION_VALIDATION_REPORT,
};
use ownership_subset::SelectedModule;
use report_io::JsonlReport;
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet};

pub(super) fn write_spec_lint_report(
    context: ChunkContext<'_>,
    unused_commuting_statements: &[analysis::reports::spec_lint::CommutingStatementsUnused],
    redundant_admission_overrides: &[spec::AdmissionCheck],
    facts: &ChunkFactAnalysis,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
    projection: &AnalysisProjection<'_>,
) -> Result<()> {
    let ChunkContext {
        chunk_id,
        report_emission,
        export_reentry_reports,
        ..
    } = context;
    if let Some(report_out_dir) = report_emission.rejection_dir() {
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            SPEC_LINT_REPORT,
            &ChunkSpecLint {
                findings: spec_lint_findings(
                    chunk_id,
                    unused_commuting_statements,
                    redundant_admission_overrides,
                    &facts.redundant_purity_hints,
                    &facts.unconsulted_purity_hints,
                    resolved_member_sites,
                )
                .into_iter()
                .chain(
                    projection
                        .chunk_analysis_inputs
                        .get(chunk_id)
                        .is_some_and(|inputs| inputs.trusted_dataflow_summaries)
                        .then(|| {
                            LintFinding::trusted_dataflow_summaries(
                                chunk_id,
                                facts.consumed_trusted_dataflow_summaries(),
                            )
                        }),
                )
                .collect(),
                consumed_reentry_free_operations: facts.consumed_reentry_free_operations(),
                loader_key_uses: export_reentry_reports
                    .values()
                    .map(|report| loader_key_uses(&report.loader))
                    .fold(LoaderKeyUses::default(), LoaderKeyUses::join),
            },
        )?;
    }
    Ok(())
}

pub(super) fn evaluate_and_gate_chunk<'a>(
    context: ChunkContext<'_>,
    ownership_subset: Option<&BTreeSet<SelectedModule>>,
    preflight: gate::CandidatePreflight<'a>,
    facts: &[analysis::facts::StatementFacts],
    program_graph: &ProgramGraphOutput<'_>,
    chunk_id_interned: ChunkId,
) -> Result<GateOutcome<'a>> {
    let ChunkContext {
        chunk_id,
        report_emission,
        graph_reports,
        effect_conflicts_jsonl,
        effect_conflicts_chunks,
        full_reports,
        full_report_chunks,
        gate_rejection: gate_rejection_policy,
        export_reentry_reports,
        ..
    } = context;
    // Select only full-detail sinks. Facts, graph construction, emitted order
    // and every chunk's bounded summary use the unchanged full input.
    let effect_conflicts_jsonl = effect_conflicts_jsonl
        && (effect_conflicts_chunks.is_empty() || effect_conflicts_chunks.contains(chunk_id));
    let full_report = |kind| {
        full_reports.contains(&kind)
            && (full_report_chunks.is_empty() || full_report_chunks.contains(chunk_id))
    };
    let report_context = ChunkReportContext {
        ownership_subset,
        chunk_id,
        report_emission,
        graph_reports,
        policy: gate_rejection_policy,
        effect_conflicts_jsonl,
        full_export_reentry: full_report(crate::FullReportKind::ExportReentry),
        full_translation_validation: full_report(crate::FullReportKind::TranslationValidation),
        full_residual_atomic_groups: full_report(crate::FullReportKind::ResidualAtomicGroups),
    };
    let (evaluation, report_out_dir) = evaluate_chunk_candidate(report_context, preflight, facts)?;
    emit_reports_and_resolve_gate(
        report_context,
        program_graph,
        evaluation,
        report_out_dir,
        &export_reentry_reports[&chunk_id_interned],
    )
}

/// The gate's judgement of one chunk's assignment, with the evidence reports
/// already written.
pub(super) enum GateOutcome<'a> {
    Accepted(ValidatedAssignment<'a>),
    /// Only under [`GateRejectionPolicy::LowerUnrunnable`]; `Refuse` fails
    /// with the rendered diagnostic instead.
    Rejected {
        assignment: RejectedAssignment<'a>,
        rejection: ChunkGateRejection,
    },
}

/// Write per-chunk validation reports (dependency graph, atomic-group
/// conflicts, cycles) and, when the spec is unrealizable, either bail with a
/// human-readable summary or hand the rejected assignment back for the
/// reading-only lowering of `debundle run --emit-on-rejection`.
///
/// Under [`ReportEmission::OnRejection`] (the `debundle run
/// --dry-run` mode) the accept path writes nothing, but a rejection
/// still writes rejection evidence (and explicitly selected full graphs) so
/// the documented `debundle gate list/describe` follow-up works.
/// The chunk's `spec lint` findings whose signal the run computes anyway:
/// unused `commuting_statements` entries, admission overrides that suppress
/// nothing, `purity: pure` hints no statement reads by name, and those the
/// classifier infers without the hint.
fn spec_lint_findings(
    chunk_id: &str,
    unused_commuting_statements: &[analysis::reports::spec_lint::CommutingStatementsUnused],
    redundant_admission_overrides: &[spec::AdmissionCheck],
    redundant_purity_hints: &[purity::RedundantPurityHint],
    unconsulted_purity_hints: &BTreeSet<String>,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
) -> Vec<LintFinding> {
    let purity_owner = |binding: &str| {
        resolved_member_sites
            .values()
            .flatten()
            .find(|site| {
                site.provider_chunk == chunk_id
                    && site
                        .declaration
                        .as_ref()
                        .is_some_and(|declaration| declaration.binding == binding)
            })
            .map_or_else(
                || PurityHintOwner::InputBinding {
                    binding: binding.to_owned(),
                },
                |site| {
                    PurityHintOwner::LogicalMember(spec::ResolvedMemberSelector {
                        module: site.module.clone(),
                        name: site.name.clone(),
                    })
                },
            )
    };
    unused_commuting_statements
        .iter()
        .map(|unused| LintFinding::unused_commuting_statements(chunk_id, unused))
        .chain(
            redundant_admission_overrides
                .iter()
                .map(|check| LintFinding::redundant_admission_override(chunk_id, *check)),
        )
        .chain(
            unconsulted_purity_hints.iter().map(|binding| {
                LintFinding::unconsulted_purity_hint(chunk_id, purity_owner(binding))
            }),
        )
        .chain(
            redundant_purity_hints
                .iter()
                .filter(|hint| !unconsulted_purity_hints.contains(&hint.binding_name))
                .map(|hint| {
                    LintFinding::redundant_purity_hint(
                        chunk_id,
                        hint,
                        purity_owner(&hint.binding_name),
                    )
                }),
        )
        .collect()
}

/// The loader keys one target's loader closure consumed. A closure whose only
/// failure is reach-back still sets a `reentry_through` policy, but records
/// nothing (it assumes its dependencies only when closed).
fn loader_key_uses(
    loader: &staged_analysis::loader_surface::ExportReentryLoaderDerivation,
) -> LoaderKeyUses {
    use staged_analysis::loader_surface::ExportReentryLoaderDerivation;
    match loader {
        ExportReentryLoaderDerivation::LoaderClosure {
            reentry_through: Some(_),
            ..
        } => LoaderKeyUses::Unattributed,
        ExportReentryLoaderDerivation::LoaderClosure {
            host_keys,
            premise_keys,
            ..
        } => LoaderKeyUses::Recorded {
            host_keys: host_keys.clone(),
            premise_keys: premise_keys.clone(),
        },
        ExportReentryLoaderDerivation::NoSynchronousLoader
        | ExportReentryLoaderDerivation::NoHostLoaderSites => LoaderKeyUses::default(),
    }
}

/// The chunk's `selector_diagnostics.json`, where `spec validate` collects
/// per-chunk reports; nothing to write while no outcome was recorded.
pub(super) fn write_selector_diagnostics(
    builder: &ChunkPlanBuilder,
    report_out_dir: &std::path::Path,
    chunk_id: &str,
) -> Result<()> {
    if let Some(report) = builder.selector_outcome_report() {
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            SELECTOR_DIAGNOSTICS_REPORT,
            &report.to_report(),
        )?;
    }
    Ok(())
}

/// The selected chunk's output policy, independent of its analysis facts.
#[derive(Clone, Copy)]
struct ChunkReportContext<'a> {
    ownership_subset: Option<&'a BTreeSet<SelectedModule>>,
    chunk_id: &'a str,
    report_emission: &'a ReportEmission,
    graph_reports: GraphReportSelection,
    policy: GateRejectionPolicy,
    effect_conflicts_jsonl: bool,
    full_translation_validation: bool,
    full_export_reentry: bool,
    full_residual_atomic_groups: bool,
}

/// Exact counts over the canonical in-memory graph, with no node/edge table.
#[derive(serde::Serialize)]
struct GraphSummary<'a> {
    chunk_id: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    ownership_subset: Option<&'a BTreeSet<SelectedModule>>,
    source_units: usize,
    edges: usize,
    edge_kinds: std::collections::BTreeMap<analysis_model::DepKind, usize>,
    atomic_groups: usize,
    atomic_group_conflicts: usize,
    cycles: usize,
    gate_accepts: bool,
    translation_validation_accepts: bool,
    full_graphs: bool,
}

/// Evaluate both diagnostic projections for one prepared candidate whose
/// gate verdict the preflight already carries. Report policy selects only
/// output locations and full-detail evidence sinks.
fn evaluate_chunk_candidate<'a, 'b>(
    context: ChunkReportContext<'b>,
    preflight: gate::CandidatePreflight<'a>,
    facts: &[analysis::facts::StatementFacts],
) -> Result<(gate::CandidateEvaluation<'a>, Option<&'b std::path::Path>)> {
    let ChunkReportContext {
        chunk_id,
        report_emission,
        full_translation_validation,
        effect_conflicts_jsonl,
        graph_reports,
        full_export_reentry,
        full_residual_atomic_groups,
        ..
    } = context;
    let report_out_dir = if preflight.is_rejected() {
        report_emission.rejection_dir()
    } else {
        report_emission.full_dir()
    };
    ensure!(
        !effect_conflicts_jsonl || report_out_dir.is_some(),
        "--effect-conflicts-jsonl requested for chunk {chunk_id}, but the current report policy suppresses output; run without --dry-run",
    );
    ensure!(
        !graph_reports.enabled() || report_out_dir.is_some(),
        "--graphs requested for chunk {chunk_id}, but the current report policy suppresses output; run without --dry-run",
    );
    ensure!(
        !(full_translation_validation || full_export_reentry || full_residual_atomic_groups)
            || report_out_dir.is_some(),
        "--full-report requested for chunk {chunk_id}, but the current report policy suppresses output; run without --dry-run",
    );

    let full_tv = if full_translation_validation {
        Some(JsonlReport::create(chunk_report_path(
            report_out_dir.expect("full-report output was checked"),
            chunk_id,
            output_layout::TRANSLATION_VALIDATION_FULL_REPORT,
        ))?)
    } else {
        None
    };
    let jsonl = if effect_conflicts_jsonl {
        Some(JsonlReport::create(chunk_report_path(
            report_out_dir.expect("effect-conflict output was checked"),
            chunk_id,
            output_layout::EFFECT_CONFLICTS_JSONL,
        ))?)
    } else {
        None
    };
    Ok((
        evaluate_with_sinks(preflight, facts, full_tv, jsonl)?,
        report_out_dir,
    ))
}

pub(super) fn evaluate_with_sinks<'a>(
    preflight: gate::CandidatePreflight<'a>,
    facts: &[analysis::facts::StatementFacts],
    mut full_tv: Option<JsonlReport>,
    mut jsonl: Option<JsonlReport>,
) -> Result<gate::CandidateEvaluation<'a>> {
    let evaluation = {
        let mut tv = full_tv
            .as_mut()
            .map(|writer| move |row: &gate::TranslationValidationEvidenceReport| writer.write(row));
        let mut effect = jsonl
            .as_mut()
            .map(|writer| move |row: &gate::EffectConflictDiagnosticRow<'_>| writer.write(row));
        preflight.evaluate(facts, tv.as_mut(), effect.as_mut())?
    };
    if let Some(writer) = full_tv {
        writer.finish()?;
    }
    if let Some(writer) = jsonl {
        writer.finish()?;
    }
    Ok(evaluation)
}

fn graph_export_guidance(
    dir: &std::path::Path,
    chunk_id: &str,
    selection: GraphReportSelection,
    ownership_subset: bool,
) -> String {
    if ownership_subset {
        if selection.enabled() {
            return format!(
                " Selected-ownership dependency graph at {}. For an accepted selected view, use `modules propose/check/apply` to append a selector-backed residual owner.",
                chunk_report_path(dir, chunk_id, DEPENDENCY_GRAPH_REPORT).display(),
            );
        }
        return " Bounded reports describe this ownership subset; rerun with `--graphs` to export its provenance-bound selected-ownership graph. For an accepted selected view, use `modules propose/check/apply` to append a selector-backed residual owner.".to_string();
    }
    if selection.enabled() {
        format!(
            " Dependency graph at {}.",
            chunk_report_path(dir, chunk_id, DEPENDENCY_GRAPH_REPORT).display()
        )
    } else {
        " Full graph queries require rerunning with --graphs or building the dependency_graphs Bazel output group.".to_string()
    }
}

fn emit_reports_and_resolve_gate<'a>(
    context: ChunkReportContext<'_>,
    program_graph: &ProgramGraphOutput<'_>,
    evaluation: gate::CandidateEvaluation<'a>,
    report_out_dir: Option<&std::path::Path>,
    export_reentry_report: &ExportReentryReport,
) -> Result<GateOutcome<'a>> {
    let ChunkReportContext {
        chunk_id,
        ownership_subset,
        graph_reports,
        policy,
        full_export_reentry,
        full_residual_atomic_groups,
        ..
    } = context;
    let assignment_analysis = evaluation.assignment_analysis();
    let assignment_report = evaluation.report();
    let rejected = evaluation.is_rejected();
    let translation_validation = evaluation.translation_validation();
    // Report timing and full graph serialization are separate output choices.
    // Gate, translation checks and candidate assessment are complete,
    // independently of whether this caller writes their diagnostic reports.
    if let Some(report_out_dir) = report_out_dir {
        if full_residual_atomic_groups {
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                output_layout::RESIDUAL_ATOMIC_GROUPS_REPORT,
                &super::residual_atomic_report::build(
                    assignment_analysis,
                    ownership_subset,
                    !rejected,
                ),
            )?;
        }
        let graph = assignment_analysis.inputs().dependency_graph();
        let mut edge_kinds = std::collections::BTreeMap::new();
        for edge in graph.iter_edges() {
            *edge_kinds.entry(edge.reason.kind()).or_insert(0usize) += 1;
        }
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            output_layout::GRAPH_SUMMARY_REPORT,
            &GraphSummary {
                chunk_id,
                ownership_subset,
                source_units: graph.num_nodes(),
                edges: graph.num_edges(),
                edge_kinds,
                atomic_groups: assignment_analysis.atomic_groups().len(),
                atomic_group_conflicts: assignment_report.atomic_group_conflicts.len(),
                cycles: assignment_report.cycles.len(),
                gate_accepts: !rejected,
                translation_validation_accepts: translation_validation.report.accepts,
                full_graphs: graph_reports.enabled(),
            },
        )?;
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            EFFECT_TRANSLATION_VALIDATION_REPORT,
            evaluation.effect_validation(),
        )?;
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            CANDIDATE_RELOCATION_ASSESSMENT_REPORT,
            evaluation.candidate_assessment(),
        )?;
        if graph_reports.enabled() {
            let mut dependency_graph_report = assignment_analysis.dependency_graph_report(
                program_graph.reference_for(report_out_dir, chunk_id)?,
                program_graph.author_inputs(),
            );
            dependency_graph_report.ownership_subset = ownership_subset.map(|selected| {
                selected
                    .iter()
                    .map(|module| graph_provenance::SelectedOwnershipModule {
                        module: module.module.to_string(),
                        bindings: module.bindings.clone(),
                        anonymous_statements: module.anonymous_statements.clone(),
                    })
                    .collect()
            });
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                DEPENDENCY_GRAPH_REPORT,
                &dependency_graph_report,
            )?;
        }
        if full_export_reentry {
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                output_layout::EXPORT_REENTRY_FULL_REPORT,
                export_reentry_report,
            )?;
        }
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            EXPORT_REENTRY_REPORT,
            &crate::reentry_report::ExportReentrySummary::from(export_reentry_report),
        )?;
        write_chunk_report_json(
            report_out_dir,
            chunk_id,
            TRANSLATION_VALIDATION_REPORT,
            &translation_validation.report,
        )?;
    }
    // Atomic-group conflicts take precedence over cycles: a split atom
    // already forces the cycle, so its evidence remains the primary
    // diagnostic. Keep any independently computed cycle report beside it.
    let verdict = if !assignment_report.atomic_group_conflicts.is_empty() {
        if let Some(report_out_dir) = report_out_dir {
            // Project onto the wire shape: source units as `"unit:N"`,
            // modules as canonical `ModulePath` — the same entity
            // keys `dependency_graph.json.gz` uses, so the file joins
            // against the dependency graph without string surgery.
            let wire = ::graph::AtomicGroupConflictReport::from_conflicts(
                &assignment_report.atomic_group_conflicts,
                &|id| assignment_analysis.module_path(id),
                assignment_analysis.inputs().dependency_graph(),
                &|id| {
                    ::graph::DependencyEdgeReport::from_edge(
                        assignment_analysis.inputs().dependency_graph().edge(id),
                    )
                },
            );
            write_chunk_report_json(
                report_out_dir,
                chunk_id,
                ATOMIC_GROUP_CONFLICTS_REPORT,
                &wire,
            )?;
            if !assignment_report.cycles.is_empty() {
                let wire = ::gate::BlockingSccEntry::from_cycle_reports(&assignment_report.cycles);
                write_chunk_report_json(report_out_dir, chunk_id, CYCLES_REPORT, &wire)?;
            }
        }
        let summary = render_atomic_group_conflict_summary(
            &assignment_report.atomic_group_conflicts,
            &|id| assignment_analysis.module_path(id),
            assignment_analysis.inputs().dependency_graph(),
        );
        let causes = render_atomic_group_cause_guidance(&assignment_report.atomic_group_conflicts);
        let evidence = report_out_dir.map_or_else(String::new, |dir| {
            format!(
                " Full evidence written to {}.{}",
                chunk_report_path(dir, chunk_id, ATOMIC_GROUP_CONFLICTS_REPORT).display(),
                graph_export_guidance(dir, chunk_id, graph_reports, ownership_subset.is_some()),
            )
        });
        Some((
            GateVerdict::AtomicGroupConflicts {
                count: assignment_report.atomic_group_conflicts.len(),
            },
            ATOMIC_GROUP_CONFLICTS_REPORT,
            format!(
                "materialize_logical_modules: chunk {chunk_id} has {n} atomic-group conflict(s) — the spec assigns members of one atomic group to different destination modules, forming a cycle in the module dependency graph that the constraining-edge SCC analysis says is unrealizable. Atomic groups come from `G_atomic` SCCs over the dependency graph (docs/design.md §\"Two classes of atom\"); every member must co-locate. {causes}Resolve by reconciling each group's claims into one destination.{evidence} Summary:\n{summary}",
                n = assignment_report.atomic_group_conflicts.len(),
            ),
        ))
    } else if !assignment_report.cycles.is_empty() {
        if let Some(report_out_dir) = report_out_dir {
            // Trim the wire shape before writing: keep `id` (array
            // index), `modules`, `cut`, and the bounded Pass-1 walk. Other per-edge evidence is
            // recomputable from `dependency_graph.json.gz` + this entry's
            // `modules` set via `debundle gate describe <id>`. See
            // `validation.rs` `BlockingSccEntry` for the schema and
            // `docs/cli.md` § "Gate queries" for the CLI surface.
            let wire = ::gate::BlockingSccEntry::from_cycle_reports(&assignment_report.cycles);
            write_chunk_report_json(report_out_dir, chunk_id, CYCLES_REPORT, &wire)?;
        }
        let summary = render_cycle_summary(&assignment_report.cycles);
        let evidence = report_out_dir.map_or_else(String::new, |dir| {
            format!(
                " Full per-cycle evidence at {}.{}",
                chunk_report_path(dir, chunk_id, CYCLES_REPORT).display(),
                graph_export_guidance(dir, chunk_id, graph_reports, ownership_subset.is_some()),
            )
        });
        Some((
            GateVerdict::UnrealizableCycles {
                count: assignment_report.cycles.len(),
            },
            CYCLES_REPORT,
            format!(
                "materialize_logical_modules: chunk {chunk_id} — spec is unrealizable: {n} module-quotient SCC(s) with at-init / side-effect edges between members. Each SCC names the binding pairs whose split forced the cycle; co-locate them or break a back-edge.{evidence} Summary:\n{summary}",
                n = assignment_report.cycles.len(),
            ),
        ))
    } else {
        None
    };
    if let Some((verdict, evidence_report, diagnostic)) = verdict {
        return match (policy, evaluation.into_validation()) {
            (GateRejectionPolicy::LowerUnrunnable, Err(assignment)) => Ok(GateOutcome::Rejected {
                assignment,
                rejection: ChunkGateRejection {
                    chunk_id: chunk_id.to_string(),
                    verdict,
                    evidence_report: report_out_dir
                        .map(|dir| chunk_report_path(dir, chunk_id, evidence_report)),
                    dependency_graph_report: report_out_dir
                        .filter(|_| graph_reports.enabled())
                        .map(|dir| chunk_report_path(dir, chunk_id, DEPENDENCY_GRAPH_REPORT)),
                    diagnostic,
                },
            }),
            _ => Err(anyhow::anyhow!(diagnostic)),
        };
    }
    match evaluation.into_validation() {
        // Defense-in-depth on the accept path: a cross-destination
        // rebinding write (clause 2) always co-locates with its target in
        // one atomic factor group, so a spec splitting them must have
        // produced an `atomic_group_conflicts` rejection above. Reaching this
        // point with a non-empty clause-2 verdict means the atomic-group
        // glue and the realizability gate disagree — refuse to emit
        // rather than materialize a bundle that reassigns an ESM import.
        Ok(validated) if !validated.cross_destination_rebinds().is_empty() => bail!(
            "materialize_logical_modules: chunk {chunk_id} — realizability verdict carries {n} cross-destination rebind(s) but no atomic-group conflict or blocking SCC was reported; this should be unreachable (rebinding writes co-locate with their target via atomic factor groups). Rebinds:\n  {rebinds}",
            n = validated.cross_destination_rebinds().len(),
            rebinds = validated.report().cross_rebinds.join("\n  "),
        ),
        Ok(validated) => Ok(GateOutcome::Accepted(validated)),
        Err(_) => {
            bail!("chunk {chunk_id}: assignment validation rejected without a rendered diagnostic")
        }
    }
}

/// Build the per-plan `FinalModuleContent` rows for `ChunkModulesReport`.
/// Each plan's binding map is iterated in name-sorted order so the
/// resulting JSON is deterministic across runs.
pub(super) fn build_final_module_report(
    module_plans: &[ModulePlan],
    assignment_analysis: &AssignmentAnalysis,
    chunk_identity: ChunkId,
    chunk_top_level_mark: swc_common::Mark,
) -> Vec<FinalModuleContent> {
    module_plans
        .iter()
        .map(|plan| {
            let mut sorted: Vec<(&String, &String)> = plan.bindings.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(b.0));
            let binding_names: Vec<String> = sorted.iter().map(|(k, _)| (*k).clone()).collect();
            let member_names: Vec<String> = sorted.iter().map(|(_, v)| (*v).clone()).collect();
            let binding_ids: Vec<BindingId> = binding_names
                .iter()
                .map(|name| {
                    BindingId::new(chunk_identity, top_level_id(name, chunk_top_level_mark))
                })
                .collect();
            let source_unit_ids = assignment_analysis
                .inputs()
                .source_unit_report_ids_for_bindings(binding_ids.iter())
                .expect("module report bindings belong to the analysis chunk");
            let binding_source_units = sorted
                .iter()
                .map(|(binding_name, _)| {
                    let binding_id = BindingId::new(
                        chunk_identity,
                        top_level_id(binding_name, chunk_top_level_mark),
                    );
                    let source_unit_ids = assignment_analysis
                        .inputs()
                        .source_unit_ids_for_binding(&binding_id)
                        .expect("module report bindings belong to the analysis chunk")
                        .iter()
                        .copied()
                        .map(source_unit_key)
                        .collect();
                    BindingSourceUnitAssociation {
                        binding_name: (*binding_name).clone(),
                        source_unit_ids,
                    }
                })
                .collect();
            FinalModuleContent {
                binding_names,
                binding_source_units,
                file: plan.target_file.clone(),
                member_names,
                path: spec::ModulePath::from_pipeline_target(&plan.target_path),
                source_unit_ids,
                residual: plan.role == ModuleRole::Unassigned,
            }
        })
        .collect()
}

pub(super) fn build_directory_dependency_facts(
    chunk_id: &str,
    entry_destination_report_target_file: &str,
    assignment_analysis: &AssignmentAnalysis,
) -> Vec<DirectoryDependencyFact> {
    let mut facts = Vec::new();
    for edge in assignment_analysis.inputs().dependency_graph().iter_edges() {
        let source_module = assignment_analysis.assignment().of(edge.from);
        let target_module = assignment_analysis.assignment().of(edge.to);
        if source_module == target_module {
            continue;
        }
        let Some(source_file) = module_output_file(
            chunk_id,
            entry_destination_report_target_file,
            assignment_analysis,
            source_module,
        ) else {
            continue;
        };
        let Some(target_file) = module_output_file(
            chunk_id,
            entry_destination_report_target_file,
            assignment_analysis,
            target_module,
        ) else {
            continue;
        };
        let symbol = edge.reason.binding().map(|id| {
            format!(
                "{}#{}",
                target_file,
                assignment_analysis
                    .inputs()
                    .export_name_for(&BindingId::new(
                        assignment_analysis.inputs().chunk_identity(),
                        id.clone(),
                    ))
                    .expect("dependency graph edge bindings belong to the analysis chunk")
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
    entry_destination_report_target_file: &str,
    assignment_analysis: &AssignmentAnalysis,
    module: ModuleId,
) -> Option<String> {
    if module
        == assignment_analysis
            .assignment()
            .entry_destination()
            .module_id()
    {
        return Some(join_module_path(&[
            chunk_id,
            entry_destination_report_target_file,
        ]));
    }
    assignment_analysis
        .inputs()
        .planned_module(module)
        .map(|planned| join_module_path(&[chunk_id, &planned.target_file]))
}

/// Per-cause guidance for the atomic-group-conflict bail message —
/// gives the spec author vocabulary to search for (`cycle`,
/// `side-effect`, `mutable`, `assignment`, `cross-destination`).
fn render_atomic_group_cause_guidance(conflicts: &[AtomicGroupConflict]) -> String {
    // `AtomicGroup::causes` is already a `BTreeSet<DepKind>` — gather
    // per-conflict causes into one `BTreeSet` so iteration stays
    // `DepKind`-`Ord`-stable without a post-collection sort.
    let causes: BTreeSet<DepKind> = conflicts
        .iter()
        .flat_map(|c| c.causes.iter().copied())
        .collect();
    let mut out = String::new();
    for cause in &causes {
        out.push_str(match cause {
            DepKind::EagerUse => {
                "EagerUse cycle: a top-level statement reads a binding at-init; \
                 splitting reader and declarer across modules forms an evaluation-order cycle. "
            }
            DepKind::EagerRebind | DepKind::LazyRebind | DepKind::DeferredRebind => {
                "Rebind: a function or top-level statement performs an assignment \
                 to a mutable binding owned by a different module — the resulting ESM \
                 import would be read-only, so this cross-destination assignment is invalid. \
                 The assigner and the binding declarer must materialize together. "
            }
            DepKind::Sequenced => {
                "Sequenced side-effect chain: two top-level side-effect statements are \
                 forced into a fixed source order; splitting them across modules \
                 inverts the run order. "
            }
            DepKind::LocalEffect => {
                "Local effect: a trusted helper call mutates a target binding \
                 (for example a TypeScript decorator application on a class prototype); \
                 the mutating statement and target binding must materialize together. "
            }
            DepKind::CoDeclaration => {
                "Shared var declaration: all source statements that declare the same `var` \
                 binding must materialize in one module. "
            }
            DepKind::CoLocation => {
                "Protected iterable interval: its producers and consumer must \
                 materialize in one module to retain their source interval. "
            }
            DepKind::LazyUse | DepKind::HoistedUse => continue,
        });
    }
    out
}
