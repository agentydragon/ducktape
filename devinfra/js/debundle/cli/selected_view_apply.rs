//! Append one selector-backed owner to a captured selected materialization.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use analysis::DependencyGraphInput;
use anyhow::{Context, Result, bail, ensure};
use graph_provenance::VerifiedGraphSourceContext;
use lowering::ownership_subset::OwnershipSubset;
use peel::assignment::{AssignmentProblem, accepted};
use selector_codemod::{SourceUnitSelectorRequest, SourceUnitSelectorSynthesis};
use spec::{LogicalModule, ModulePath, SourceMatchBinding, SourceMatchBindingDetail};

use crate::outcome::{GateOutcome, MutationAction, MutationOutcome, MutationVerb};
use crate::source_unit_apply::{SelectorOverride, SourceUnitApplyOptions, SourceUnitApplyOutcome};

pub(super) fn selected_provenance(
    context: &VerifiedGraphSourceContext,
    graph_path: &Path,
    graph: &DependencyGraphInput,
) -> Result<graph_provenance::GraphProvenance> {
    let report = graph.report();
    ensure!(
        report.chunk_id == context.chunk_id()
            && report.ownership_subset.as_deref() == context.selected_ownership(),
        "selected graph does not match its verified chunk and ownership"
    );
    graph_provenance::read_program_provenance(
        graph_path,
        &report.program_graph,
        &report.chunk_id,
        report.ownership_subset.as_deref(),
    )?
    .context("selected-view editing requires graph provenance")
}

pub(super) fn run(
    stderr: &mut (dyn std::io::Write + Send),
    graph_path: &Path,
    context: &VerifiedGraphSourceContext,
    graph: DependencyGraphInput,
    options: SourceUnitApplyOptions<'_>,
) -> Result<SourceUnitApplyOutcome> {
    let change = prepare_change(context, graph, options)?;
    let provenance = selected_provenance(context, graph_path, &change.graph)?;
    let request = crate::graph_transform_request(context)?;
    let program = pipeline::SelectedOwnershipProgram::prepare(&request, context, &provenance)?;
    let targets = BTreeSet::from([context.chunk_id().to_owned()]);
    command_diagnostics::run(stderr, |diagnostics| {
        program.with_preflight_session(targets, diagnostics, |session| {
            realize_change(change, session)
        })
    })
}

/// Corpus discovery supplies one verified shared frontend. All cheap assignment
/// restrictions and the complete realized-assignment comparison stay identical
/// to ordinary check/preview/apply.
pub(super) fn run_prepared(
    context: &VerifiedGraphSourceContext,
    graph: DependencyGraphInput,
    options: SourceUnitApplyOptions<'_>,
    session: &mut lowering::SelectedOwnershipPreflightSession<'_>,
) -> Result<SourceUnitApplyOutcome> {
    realize_change(prepare_change(context, graph, options)?, session)
}

struct SelectedViewChange<'a> {
    chunk_id: &'a str,
    config_path: &'a Path,
    config_bytes: &'a [u8],
    view_path: &'a Path,
    graph: DependencyGraphInput,
    view: OwnershipSubset,
    destination: ModulePath,
    import_order: Vec<ModulePath>,
    expected: BTreeMap<String, Option<ModulePath>>,
    unchanged_exports: BTreeMap<String, BTreeMap<String, String>>,
    bindings: Vec<(String, String, Option<SelectorOverride>)>,
    anonymous_ordinals: Vec<source_identity::SourceUnitOrdinal>,
    dry_run: bool,
    residual_source_units_claimed: usize,
}

fn prepare_change<'a>(
    context: &'a VerifiedGraphSourceContext,
    graph: DependencyGraphInput,
    options: SourceUnitApplyOptions<'_>,
) -> Result<SelectedViewChange<'a>> {
    let SourceUnitApplyOptions {
        tree_config,
        chunk_logical_destinations: _,
        assignments,
        import_order,
        mut readable_names,
        mut selector_overrides,
        dry_run,
        effect_assessment_context,
    } = options;
    ensure!(
        effect_assessment_context.is_none(),
        "selected-view effect relocation assessment is not supported; omit --analysis"
    );
    ensure!(
        !assignments.is_empty(),
        "source unit assignment batch is empty"
    );
    let requested_config = tree_config.context("selected-view editing requires --tree-config")?;
    let config_path = context
        .tree_config_path()
        .context("graph lacks a captured tree configuration path")?;
    ensure!(
        config_path == graph_provenance::absolute(requested_config)?.as_path(),
        "selected-view editing must update the verified graph's tree configuration"
    );
    let config_bytes = context
        .tree_config()
        .context("graph lacks a captured tree configuration")?;
    let (view_path, view_bytes) = context
        .ownership_subset_input()
        .context("graph lacks a captured selected-view input")?;
    let view: OwnershipSubset = serde_json::from_slice(view_bytes)?;
    // A selected view has chunk-local owner keys rather than physical YAML
    // destinations. Preserve the same coordinates used by its graph and plan.
    for assignment in &assignments {
        crate::source_unit_apply::validate_assignment_module_id(&assignment.module)?;
    }
    let destinations: BTreeSet<_> = assignments
        .iter()
        .map(|assignment| ModulePath::parse(&assignment.module, ""))
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        destinations.len() == 1,
        "selected-view editing supports one fresh residual destination per plan"
    );
    let destination = destinations.into_iter().next().expect("one destination");
    ensure!(
        graph
            .report()
            .quotient
            .nodes
            .iter()
            .all(|module| module.path != destination),
        "selected-view editing requires a fresh destination; existing owners stay fixed"
    );
    let problem = AssignmentProblem::new(&graph)?;
    let baseline = problem.candidate(&[])?;
    let candidate = problem.candidate(&assignments)?;
    for assignment in &assignments {
        let unit = graph
            .source_unit_id(&assignment.unit_id)
            .expect("candidate checked unit IDs");
        ensure!(
            baseline[unit.0].is_none(),
            "selected-view editing can peel only entry residual; existing owners stay fixed"
        );
    }
    let mut appended_priority = graph.report().import_order.clone();
    appended_priority.push(destination.clone());
    let import_order =
        import_order.context("selected-view editing requires the final import priority")?;
    ensure!(
        import_order == appended_priority,
        "selected-view editing must preserve the baseline priority and append the fresh owner last"
    );
    let validation = problem.validate_with_priority(&candidate, &import_order)?;
    ensure!(
        accepted(&validation),
        "complete assignment gate rejected the batch: {:?}",
        validation.report
    );
    let expected: BTreeMap<_, _> = graph
        .report()
        .nodes
        .iter()
        .enumerate()
        .map(|(row, node)| (node.id.clone(), candidate[row].clone()))
        .collect();
    let unchanged_exports: BTreeMap<_, BTreeMap<_, _>> = graph
        .report()
        .nodes
        .iter()
        .enumerate()
        .filter(|(row, _)| candidate[*row] == baseline[*row])
        .map(|(_, node)| {
            (
                node.id.clone(),
                node.declared_bindings
                    .iter()
                    .map(|binding| (binding.binding.to_string(), binding.export_name.to_string()))
                    .collect(),
            )
        })
        .collect();

    let mut bindings = Vec::new();
    let mut anonymous_ordinals = Vec::new();
    let mut selected_names = BTreeSet::new();
    for assignment in &assignments {
        let unit = graph
            .source_unit_id(&assignment.unit_id)
            .expect("checked unit");
        let node = graph.source_unit_report(unit);
        if node.declared_bindings.is_empty() {
            anonymous_ordinals.push(node.statement_ordinal);
        }
        for binding in &node.declared_bindings {
            let runtime = binding.binding.to_string();
            if !selected_names.insert(runtime.clone()) {
                continue; // Shared var sites are addressed by one binding claim.
            }
            let readable = readable_names
                .remove(&runtime)
                .unwrap_or_else(|| binding.export_name.to_string());
            ensure!(
                !readable.is_empty(),
                "readable name for {runtime:?} is empty"
            );
            let selector = selector_overrides
                .get_mut(&assignment.unit_id)
                .and_then(|selectors| selectors.remove(&runtime));
            bindings.push((runtime, readable, selector));
        }
    }
    ensure!(
        readable_names.is_empty(),
        "readable_names must name only newly claimed bindings"
    );
    selector_overrides.retain(|_, selectors| !selectors.is_empty());
    ensure!(
        selector_overrides.is_empty(),
        "selector_overrides must name newly claimed bindings by selected source-unit ID"
    );
    drop(problem);

    Ok(SelectedViewChange {
        chunk_id: context.chunk_id(),
        config_path,
        config_bytes,
        view_path,
        graph,
        view,
        destination,
        import_order,
        expected,
        unchanged_exports,
        bindings,
        anonymous_ordinals,
        dry_run,
        residual_source_units_claimed: assignments.len(),
    })
}

fn realize_change(
    change: SelectedViewChange<'_>,
    session: &mut lowering::SelectedOwnershipPreflightSession<'_>,
) -> Result<SourceUnitApplyOutcome> {
    let SelectedViewChange {
        chunk_id,
        config_path,
        config_bytes,
        view_path,
        graph,
        mut view,
        destination,
        import_order,
        expected,
        unchanged_exports,
        bindings,
        anonymous_ordinals,
        dry_run,
        residual_source_units_claimed,
    } = change;
    let source = session.source(chunk_id)?;
    let body_statements =
        js_ast::statements_for_source_unit_ordinals(&source.module.body, &anonymous_ordinals);
    let mut module = selector_codemod::with_source_unit_selector_synthesizer(
        &source.module,
        |synthesizer| {
            let mut module = LogicalModule::default();
            for (runtime, readable, selector) in bindings {
                match selector {
                    Some(SelectorOverride::SourceMatch(claim)) => {
                        let [binding] = claim.bindings.as_slice() else {
                            bail!(
                                "source_match override for {runtime:?} must claim exactly one binding"
                            );
                        };
                        ensure!(
                            binding.name() == readable,
                            "source_match override for {runtime:?} has a different export name"
                        );
                        ensure!(
                            binding.local() != runtime || readable == runtime,
                            "source_match override must use a neutral selector-local name"
                        );
                        module.source_matches.push(claim);
                    }
                    Some(SelectorOverride::Relational(selector)) => {
                        ensure!(
                            !matches!(selector.validate()?, spec::MemberSelectorSpec::Binding(_)),
                            "relational override cannot use a binding name pin"
                        );
                        module.members.push(spec::Member {
                            name: Some(readable),
                            selector: *selector,
                        });
                    }
                    None => {
                        let SourceUnitSelectorSynthesis::Synthesized(mut selector) = synthesizer
                            .synthesize(&SourceUnitSelectorRequest::DeclaredBinding {
                                binding: runtime.clone(),
                            })?
                        else {
                            bail!(
                                "newly claimed binding {runtime:?} has no uniquely proven structural selector"
                            );
                        };
                        let [binding] = selector.claim.bindings.as_mut_slice() else {
                            bail!(
                                "binding selector for {runtime:?} did not claim exactly one binding"
                            );
                        };
                        *binding = SourceMatchBinding::Detailed(SourceMatchBindingDetail {
                            local: binding.local().to_owned(),
                            name: Some(readable),
                        });
                        module.source_matches.push(selector.claim);
                    }
                }
            }
            for (ordinal, statement) in anonymous_ordinals.iter().zip(body_statements) {
                let statement = statement.with_context(|| {
                    format!("anonymous source ordinal {ordinal} is absent from the parsed source")
                })?;
                let SourceUnitSelectorSynthesis::Synthesized(selector) =
                    synthesizer.synthesize(&SourceUnitSelectorRequest::AnonymousStatement {
                        body_index: statement.0,
                    })?
                else {
                    bail!(
                        "anonymous source ordinal {ordinal} has no uniquely proven structural selector"
                    );
                };
                module.anonymous_statements.push(spec::AnonymousStatement {
                    match_source: None,
                    source_match: Some(spec::SourceMatch {
                        identifiers: selector.claim.identifiers,
                        match_source: selector.claim.match_source,
                        target_binding: None,
                    }),
                    no_reentry_call: None,
                    note: None,
                    comment: None,
                });
            }
            Ok(module)
        },
    )?;
    spec_modules::coalesce_source_match_claims(&mut module);
    let placements = view.placements.entry(chunk_id.to_owned()).or_default();
    ensure!(
        !placements.contains_key(destination.as_str()),
        "selected-view placement destination already exists"
    );
    placements.insert(destination.to_string(), module.clone());
    let realized = session.check(chunk_id, &view, &import_order, graph)?;
    ensure!(
        realized.source_unit_modules == expected,
        "selected-view selectors realize a different complete assignment from the checked plan"
    );
    ensure!(
        realized.emitted_owner_paths == import_order.iter().cloned().collect(),
        "selected-view selectors realize a different emitted owner universe from the checked priority"
    );
    for (unit, expected_exports) in unchanged_exports {
        let actual_exports = realized
            .source_unit_binding_exports
            .get(&unit)
            .with_context(|| format!("selected-view preflight omitted source unit {unit:?}"))?;
        ensure!(
            actual_exports == &expected_exports,
            "selected-view selectors changed retained binding exports in source unit {unit:?}"
        );
    }

    // Both complete documents are serialized and verified before the first
    // persistent write. Semantic failures leave every authoring input intact.
    let mut next_view = serde_json::to_vec_pretty(&view)?;
    next_view.push(b'\n');
    let next_config = spec_tree::update_chunk_import_order(config_bytes, chunk_id, import_order)?;
    ensure!(
        view_path != config_path,
        "selected view and tree configuration must be different files"
    );
    if !dry_run {
        yaml_edit::write_bytes_atomic(view_path, &next_view)?;
        yaml_edit::write_bytes_atomic(config_path, &next_config)?;
    }
    Ok(SourceUnitApplyOutcome {
        outcome: MutationOutcome::ownership_edit(
            MutationVerb::Apply,
            if dry_run {
                MutationAction::DryRun
            } else {
                MutationAction::Applied
            },
            GateOutcome::ClaimsChecked,
            None,
            vec![
                view_path.display().to_string(),
                config_path.display().to_string(),
            ],
            Vec::new(),
        ),
        source_units_reassigned: 0,
        residual_source_units_claimed,
        synthesized_claims: BTreeMap::from([(destination.to_string(), module)]),
        effect_relocation_assessment: None,
    })
}
