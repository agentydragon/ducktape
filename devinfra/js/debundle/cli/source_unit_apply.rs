//! Apply exact source-unit assignments through the shared module edit gate.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use analysis::{DependencyGraphReport, ModuleDestinationKind, StatementKind};
use anonymous_resolution::resolve_source_file;
use anyhow::{Context, Result, anyhow, bail};
use peel::assignment::{AssignmentProblem, SourceUnitAssignment, accepted};
use spec::{LogicalModule, ModulePath, is_residual_namespace_path};
use spec_modules::{coalesce_source_match_claims, module_path_from_file};
use spec_tree::ModuleTreeScope;

use crate::binding::{
    BindingIdentity, BindingLabel, ModuleDocs, apply_doc_changes, binding_effective_name,
    binding_matches_in_doc, canonical_module_path, insert_annotation, load_module_docs,
    member_effective_name, source_match_readable, take_indexed, take_source_match_bindings,
};
use crate::candidate_assessment::ResolvedCandidateEffectAssessment;
use crate::edit_gate::{
    Gate, LocalBindingResolution, SourceUnitClaimProjection, SourceUnitModuleAssignments,
    post_edit_spec_from_docs, resolve_edit_claims, source_unit_module_assignments_from_projection,
};
use crate::outcome::{GateOutcome, MutationAction, MutationOutcome, MutationVerb};

#[derive(Debug, Clone, serde::Serialize)]
pub struct SourceUnitApplyOutcome {
    #[serde(flatten)]
    pub outcome: MutationOutcome,
    pub source_units_reassigned: usize,
    pub residual_source_units_claimed: usize,
    pub synthesized_claims: BTreeMap<String, LogicalModule>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub effect_relocation_assessment: Option<gate::CandidateRelocationAssessmentReport>,
}

pub struct SourceUnitApplyOptions<'a> {
    pub tree_config: Option<&'a Path>,
    /// Plan destinations are relative to the chunk's roots in a full module tree.
    pub chunk_logical_destinations: bool,
    pub assignments: Vec<SourceUnitAssignment>,
    pub import_order: Option<Vec<ModulePath>>,
    pub readable_names: BTreeMap<String, String>,
    pub selector_overrides: BTreeMap<String, BTreeMap<String, SelectorOverride>>,
    pub dry_run: bool,
    pub effect_assessment_context: Option<&'a ResolvedCandidateEffectAssessment<'a>>,
}

#[derive(Debug, Clone, serde::Deserialize)]
#[serde(tag = "kind", content = "selector", rename_all = "snake_case")]
pub enum SelectorOverride {
    SourceMatch(spec::SourceMatchClaim),
    Relational(Box<spec::RawMemberSelector>),
}

fn resolve_plan_destinations(
    changes: &mut [SourceUnitAssignment],
    docs: &ModuleDocs,
    scope: &ModuleTreeScope,
) -> Result<()> {
    let mut authored_paths = BTreeMap::<_, Vec<_>>::new();
    for physical in docs.keys() {
        let logical = ModulePath::parse(scope.logical_destination(physical)?, "")?;
        authored_paths.entry(logical).or_default().push(physical);
    }
    for change in changes {
        let logical = ModulePath::parse(&change.module, "")?;
        let existing = authored_paths
            .get(&logical)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        anyhow::ensure!(
            existing.len() <= 1,
            "logical destination {logical} has multiple authored files"
        );
        change.module = if let Some(existing) = existing.first() {
            (*existing).clone()
        } else {
            scope
                .roots()
                .first()
                .context("tree config has no module root for plan destination")?
                .join(logical.as_str())
                .to_str()
                .context("module destination is not UTF-8")?
                .to_string()
        };
    }
    Ok(())
}

pub(super) fn validate_assignment_module_id(module: &str) -> Result<()> {
    let normalized = module.to_ascii_lowercase();
    for extension in [".yaml", ".yml"] {
        if normalized.ends_with(extension) {
            let logical_id = &module[..module.len() - extension.len()];
            bail!(
                "module destination {module:?} looks like a YAML filename; use extensionless logical module ID {logical_id:?}"
            );
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Hash)]
enum SourceUnitClaimSlot {
    Member(usize),
    SourceMatch(usize, usize),
    Anonymous(usize),
}

#[derive(Debug, Clone)]
struct SourceUnitClaimLocation {
    source_unit_id: String,
    /// Exact key used by the in-memory document map and filesystem path.
    physical_module: String,
    /// Canonical logical module identity used by the complete source unit map.
    logical_module: String,
    slot: SourceUnitClaimSlot,
}

#[derive(Debug, Clone)]
struct PlannedSourceUnitMove {
    source_unit_ids: BTreeSet<String>,
    source_module: String,
    destination: String,
    slot: SourceUnitClaimSlot,
}

/// Apply an exact whole-graph source-unit mapping. Existing selector locations
/// come from the canonical joint resolver; moves preserve authored metadata.
pub fn run_source_unit_apply(
    stderr: &mut (dyn std::io::Write + Send),
    modules_root: &Path,
    dependency_graph_path: &Path,
    graph_input: analysis::DependencyGraphInput,
    source_root: &Path,
    source_context: Option<&graph_provenance::VerifiedGraphSourceContext>,
    options: SourceUnitApplyOptions<'_>,
) -> Result<SourceUnitApplyOutcome> {
    if graph_input.report().ownership_subset.is_some() {
        return crate::selected_view_apply::run(
            stderr,
            dependency_graph_path,
            source_context.context("selected-view editing requires verified graph inputs")?,
            graph_input,
            options,
        );
    }
    let SourceUnitApplyOptions {
        tree_config,
        mut assignments,
        chunk_logical_destinations,
        import_order,
        mut readable_names,
        mut selector_overrides,
        dry_run,
        effect_assessment_context,
    } = options;
    // --modules may name the verified exact chunk subtree. Configured roots
    // are relative to the full tree, so document lookup and writes must use
    // that same verified root before applying the chunk scope.
    let modules_root = tree_config
        .and(source_context)
        .and_then(graph_provenance::VerifiedGraphSourceContext::modules_root)
        .unwrap_or(modules_root);
    if assignments.is_empty() {
        bail!("source unit assignment batch is empty");
    }

    let graph_input = std::sync::Arc::new(graph_input);
    let dependency_graph = graph_input.report();
    let scope = tree_config
        .map(|path| ModuleTreeScope::from_config(path, &dependency_graph.chunk_id))
        .transpose()?;
    let mut original_docs = load_module_docs(modules_root)?;
    if let Some(scope) = &scope {
        original_docs.retain(|module, _| scope.contains(Path::new(module)));
        if chunk_logical_destinations {
            resolve_plan_destinations(&mut assignments, &original_docs, scope)?;
        }
    }
    for assignment in &assignments {
        validate_assignment_module_id(&assignment.module)?;
    }
    let mut docs = original_docs.clone();
    let initial_spec = post_edit_spec_from_docs(&original_docs, &BTreeSet::new())?;
    let claim_projection = resolve_edit_claims(
        stderr,
        &graph_input,
        dependency_graph_path,
        modules_root,
        Some(source_root),
        source_context,
        &initial_spec,
    )?;
    let current_source_unit_modules = source_unit_module_assignments_from_projection(
        dependency_graph,
        modules_root,
        &initial_spec,
        &claim_projection,
        scope.as_ref(),
    )?;
    check_graph_source_unit_destinations(
        dependency_graph,
        &current_source_unit_modules,
        scope.as_ref(),
    )?;

    let locations = source_unit_claim_locations(
        dependency_graph,
        modules_root,
        &original_docs,
        &initial_spec,
        &claim_projection,
        scope.as_ref(),
    )?;
    let locations_by_source_unit =
        claim_locations_by_source_unit(locations, &current_source_unit_modules)?;

    let requested_destinations =
        requested_source_unit_destinations(&graph_input, assignments, scope.as_ref())?;

    let changes = requested_destinations
        .iter()
        .map(|(unit_id, destination)| {
            Ok(SourceUnitAssignment {
                unit_id: unit_id.clone(),
                module: scope
                    .as_ref()
                    .map_or(Ok(destination.as_str()), |scope| {
                        scope.logical_destination(destination)
                    })?
                    .to_string(),
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let problem = AssignmentProblem::prepare(&graph_input)?;
    let candidate = problem.candidate(&changes)?;
    let import_order = import_order.unwrap_or_else(|| dependency_graph.import_order.clone());
    let validation = problem.validate_with_priority(&candidate, &import_order)?;
    anyhow::ensure!(
        accepted(&validation),
        "complete assignment gate rejected the batch: {:?}",
        validation.report
    );

    let staged_tree_config =
        staged_import_order_config(tree_config, dependency_graph, &import_order)?;

    let mut expected_source_unit_modules = current_source_unit_modules.clone();
    let mut planned_moves = Vec::<PlannedSourceUnitMove>::new();
    let mut planned_move_by_slot = BTreeMap::<(String, SourceUnitClaimSlot), usize>::new();
    let mut source_units_reassigned = 0;
    let mut residual_source_units_claimed = 0;
    let mut residual_claims = ResidualClaimPlan::default();
    let existing_destinations = physical_modules_by_logical_destination(&docs, scope.as_ref())?;
    for (source_unit_id, destination) in &requested_destinations {
        let Some(source_logical_module) = current_source_unit_modules[source_unit_id].as_ref()
        else {
            let unit = graph_input
                .source_unit_id(source_unit_id)
                .expect("checked source unit");
            let node = graph_input.source_unit_report(unit);
            let location = node
                .source_location
                .as_ref()
                .context("residual source unit has no source location")?;
            let source = resolve_source_file(
                &location.source_path,
                Some(source_root),
                dependency_graph_path,
                modules_root,
                source_context,
            )?;
            residual_claims.claim(
                node,
                source,
                destination,
                source_unit_id,
                &mut readable_names,
                &mut selector_overrides,
            )?;
            expected_source_unit_modules.insert(source_unit_id.clone(), Some(destination.clone()));
            residual_source_units_claimed += 1;
            continue;
        };
        let source_logical_module = source_logical_module.as_str().to_string();
        if destination == &source_logical_module {
            continue;
        }
        source_units_reassigned += 1;
        expected_source_unit_modules.insert(source_unit_id.clone(), Some(destination.clone()));
        plan_claim_moves(
            &mut planned_moves,
            &mut planned_move_by_slot,
            &locations_by_source_unit,
            &existing_destinations,
            source_unit_id,
            destination,
            &source_logical_module,
        )?;
    }

    anyhow::ensure!(
        readable_names.is_empty(),
        "readable_names must name only newly claimed bindings"
    );
    selector_overrides.retain(|_, selectors| !selectors.is_empty());
    anyhow::ensure!(
        selector_overrides.is_empty(),
        "selector_overrides must name newly claimed bindings by selected source-unit ID"
    );
    let synthesized_claims = residual_claims.realize(&mut docs, modules_root)?;

    let moved_names_by_destination = apply_planned_moves(&mut docs, modules_root, &planned_moves)?;

    check_moved_name_collisions(&docs, modules_root, &moved_names_by_destination)?;

    // Normalize only the reviewed destinations, including an unchanged-owner
    // request. Resolve this exact document through the complete identity gate.
    let requested_modules: BTreeSet<_> = requested_destinations
        .values()
        .flat_map(|destination| {
            existing_destinations
                .get(destination)
                .cloned()
                .unwrap_or_else(|| vec![destination.clone()])
        })
        .collect();
    for destination in requested_modules {
        if let Some((_, module)) = docs.get_mut(&destination) {
            coalesce_source_match_claims(module);
        }
    }

    // Applying assignments keeps empty authored shells; use `modules delete`
    // when their removal is intended.
    let to_delete = BTreeSet::new();
    let post_spec = post_edit_spec_from_docs(&docs, &to_delete)?;
    // Resolve the entire scoped spec, prove the expected source-unit map, then
    // apply the same complete graph check used for the reviewed assignment.
    Gate::Run {
        graph_path: dependency_graph_path,
        input: std::sync::Arc::clone(&graph_input),
        source_root: Some(source_root),
        source_context,
        import_order: Some(import_order.clone()),
    }
    .check_source_units(
        stderr,
        modules_root,
        &post_spec,
        &expected_source_unit_modules,
        scope.as_ref(),
        &import_order,
    )?;
    let effect_relocation_assessment = if let Some(context) = effect_assessment_context {
        Some(context.assess(stderr, modules_root, &docs, &to_delete, &import_order)?)
    } else {
        None
    };
    if let Some(assessment) = &effect_relocation_assessment {
        pipeline::ensure_candidate_emittable_for_write(assessment, dry_run)?;
    }

    let (mut files_written, files_deleted) =
        apply_doc_changes(&original_docs, &docs, &to_delete, dry_run)?;
    if let Some((config_path, config_bytes)) = staged_tree_config {
        if !dry_run {
            yaml_edit::write_bytes_atomic(config_path, &config_bytes)?;
        }
        files_written.push(config_path.display().to_string());
    }
    let action = if files_written.is_empty() && files_deleted.is_empty() {
        MutationAction::Unchanged
    } else if dry_run {
        MutationAction::DryRun
    } else {
        MutationAction::Applied
    };
    Ok(SourceUnitApplyOutcome {
        outcome: MutationOutcome::ownership_edit(
            MutationVerb::Apply,
            action,
            GateOutcome::ClaimsChecked,
            effect_relocation_assessment.as_ref(),
            files_written,
            files_deleted,
        ),
        source_units_reassigned,
        residual_source_units_claimed,
        synthesized_claims,
        effect_relocation_assessment,
    })
}

/// Groups claim locations by source unit, checking each against the module
/// the complete spec resolves it to.
fn claim_locations_by_source_unit(
    locations: Vec<SourceUnitClaimLocation>,
    current_source_unit_modules: &SourceUnitModuleAssignments,
) -> Result<BTreeMap<String, Vec<SourceUnitClaimLocation>>> {
    let mut locations_by_source_unit = BTreeMap::<String, Vec<SourceUnitClaimLocation>>::new();
    for location in locations {
        let expected_module = current_source_unit_modules
            .get(&location.source_unit_id)
            .and_then(Option::as_ref)
            .with_context(|| {
                format!(
                    "source unit {:?} has a module claim location but resolves to residual",
                    location.source_unit_id
                )
            })?;
        anyhow::ensure!(
            expected_module.as_str() == location.logical_module,
            "source unit {:?} selector is stored in {}, but the complete spec resolves it to {}",
            location.source_unit_id,
            location.logical_module,
            expected_module
        );
        locations_by_source_unit
            .entry(location.source_unit_id.clone())
            .or_default()
            .push(location);
    }
    Ok(locations_by_source_unit)
}

/// The batch's canonical destination per source unit; rejects repeats,
/// import/export scaffolding and reserved residual paths.
fn requested_source_unit_destinations(
    graph_input: &analysis::DependencyGraphInput,
    assignments: Vec<SourceUnitAssignment>,
    scope: Option<&ModuleTreeScope>,
) -> Result<BTreeMap<String, String>> {
    let mut requested_destinations = BTreeMap::<String, String>::new();
    for assignment in assignments {
        let source_unit = graph_input
            .source_unit_id(&assignment.unit_id)
            .with_context(|| {
                format!(
                    "source unit ID {:?} is absent from dependency_graph.json.gz",
                    assignment.unit_id
                )
            })?;
        anyhow::ensure!(
            !requested_destinations.contains_key(&assignment.unit_id),
            "source unit assignment batch repeats {:?}",
            assignment.unit_id
        );
        let node = graph_input.source_unit_report(source_unit);
        anyhow::ensure!(
            !matches!(
                node.statement_kind,
                StatementKind::Import | StatementKind::Export
            ),
            "source unit {:?} is import/export scaffolding and cannot be moved by modules apply",
            assignment.unit_id
        );
        let destination = if let Some(scope) = scope {
            scope.canonical_destination(&assignment.module)?
        } else {
            canonical_module_path(&assignment.module)?
        };
        anyhow::ensure!(
            !is_residual_namespace_path(&destination),
            "destination {:?} is a reserved residual path",
            destination
        );
        requested_destinations.insert(assignment.unit_id, destination);
    }
    Ok(requested_destinations)
}

/// The tree config rewritten with a changed global import priority, if any.
fn staged_import_order_config<'a>(
    tree_config: Option<&'a Path>,
    dependency_graph: &DependencyGraphReport,
    import_order: &[ModulePath],
) -> Result<Option<(&'a Path, Vec<u8>)>> {
    Ok(if import_order != dependency_graph.import_order {
        let config_path = tree_config.context(
            "changing the global import priority requires --tree-config so the checked order can be persisted",
        )?;
        let config_bytes = std::fs::read(config_path)
            .with_context(|| format!("reading tree config {}", config_path.display()))?;
        let updated = spec_tree::update_chunk_import_order(
            &config_bytes,
            &dependency_graph.chunk_id,
            import_order.to_vec(),
        )?;
        Some((config_path, updated))
    } else {
        None
    })
}

/// Residual source units the batch claims fresh: bindings and anonymous
/// statements whose selectors are synthesized, and author-provided selectors.
#[derive(Default)]
struct ResidualClaimPlan {
    selected_binding_names: BTreeSet<String>,
    fresh_bindings: BTreeMap<std::path::PathBuf, BTreeMap<String, BTreeMap<String, String>>>,
    fresh_anonymous:
        BTreeMap<std::path::PathBuf, BTreeMap<String, Vec<source_identity::SourceUnitOrdinal>>>,
    provided_selectors: Vec<(String, String, String, SelectorOverride)>,
}

impl ResidualClaimPlan {
    fn claim(
        &mut self,
        node: &analysis::SourceUnitReport,
        source: std::path::PathBuf,
        destination: &str,
        source_unit_id: &str,
        readable_names: &mut BTreeMap<String, String>,
        selector_overrides: &mut BTreeMap<String, BTreeMap<String, SelectorOverride>>,
    ) -> Result<()> {
        if node.declared_bindings.is_empty() {
            self.fresh_anonymous
                .entry(source)
                .or_default()
                .entry(destination.to_string())
                .or_default()
                .push(node.statement_ordinal);
        } else {
            for binding in &node.declared_bindings {
                let runtime = binding.binding.to_string();
                anyhow::ensure!(
                    self.selected_binding_names.insert(runtime.clone()),
                    "binding {runtime:?} is declared by several selected source units"
                );
                let readable = readable_names
                    .remove(&runtime)
                    .unwrap_or_else(|| binding.export_name.to_string());
                anyhow::ensure!(
                    !readable.is_empty(),
                    "readable name for {runtime:?} is empty"
                );
                if let Some(selector) = selector_overrides
                    .get_mut(source_unit_id)
                    .and_then(|selectors| selectors.remove(&runtime))
                {
                    self.provided_selectors.push((
                        destination.to_string(),
                        runtime,
                        readable,
                        selector,
                    ));
                } else {
                    anyhow::ensure!(
                        self.fresh_bindings
                            .entry(source.clone())
                            .or_default()
                            .entry(destination.to_string())
                            .or_default()
                            .insert(runtime.clone(), readable)
                            .is_none(),
                        "binding {runtime:?} has several declaration units; selector realization requires an explicit spec claim"
                    );
                }
            }
        }
        Ok(())
    }

    fn realize(
        self,
        docs: &mut ModuleDocs,
        modules_root: &Path,
    ) -> Result<BTreeMap<String, LogicalModule>> {
        let mut synthesized_claims = BTreeMap::<String, LogicalModule>::new();
        for (source, modules) in self.fresh_bindings {
            for (destination, claims) in
                selector_codemod::source_unit::synthesize_residual_claims_for_modules(
                    &source, &modules,
                )?
            {
                synthesized_claims
                    .entry(destination.clone())
                    .or_default()
                    .source_matches
                    .extend(claims.iter().cloned());
                ensure_source_unit_destination(docs, modules_root, &destination)
                    .source_matches
                    .extend(claims);
            }
        }
        for (source, modules) in self.fresh_anonymous {
            for (destination, statements) in
                selector_codemod::source_unit::synthesize_anonymous_claims_for_modules(
                    &source, &modules,
                )?
            {
                synthesized_claims
                    .entry(destination.clone())
                    .or_default()
                    .anonymous_statements
                    .extend(statements.iter().cloned());
                ensure_source_unit_destination(docs, modules_root, &destination)
                    .anonymous_statements
                    .extend(statements);
            }
        }
        for (destination, runtime, readable, selector) in self.provided_selectors {
            let doc = ensure_source_unit_destination(docs, modules_root, &destination);
            match selector {
                SelectorOverride::SourceMatch(claim) => {
                    let [binding] = claim.bindings.as_slice() else {
                        bail!(
                            "source_match override for {runtime:?} must claim exactly one binding"
                        );
                    };
                    anyhow::ensure!(
                        binding.name() == readable,
                        "source_match override for {runtime:?} exports {:?}, expected {readable:?}",
                        binding.name()
                    );
                    anyhow::ensure!(
                        binding.local() != runtime || readable == runtime,
                        "source_match override for {runtime:?} retains its capture local; use a neutral selector-local name"
                    );
                    doc.source_matches.push(claim);
                }
                SelectorOverride::Relational(selector) => {
                    if matches!(selector.validate()?, spec::MemberSelectorSpec::Binding(_)) {
                        bail!("relational override for {runtime:?} cannot use a binding name pin");
                    }
                    doc.members.push(spec::Member {
                        name: Some(readable),
                        selector: *selector,
                    });
                }
            }
        }
        Ok(synthesized_claims)
    }
}

/// Moves each planned claim slot and its annotation into its destination;
/// returns the binding names moved into each destination.
fn apply_planned_moves(
    docs: &mut ModuleDocs,
    modules_root: &Path,
    planned_moves: &[PlannedSourceUnitMove],
) -> Result<BTreeMap<String, BTreeSet<String>>> {
    let moved_members = planned_moves
        .iter()
        .filter_map(|movement| match movement.slot {
            SourceUnitClaimSlot::Member(index) => Some((movement.source_module.clone(), index)),
            _ => None,
        });
    let mut pulled_members = take_indexed(docs, moved_members, |doc| &mut doc.members);
    let moved_source_match_bindings =
        planned_moves
            .iter()
            .filter_map(|movement| match movement.slot {
                SourceUnitClaimSlot::SourceMatch(claim, binding) => {
                    Some((movement.source_module.clone(), claim, binding))
                }
                _ => None,
            });
    let mut pulled_source_match_bindings =
        take_source_match_bindings(docs, moved_source_match_bindings);
    let moved_anonymous = planned_moves
        .iter()
        .filter_map(|movement| match movement.slot {
            SourceUnitClaimSlot::Anonymous(index) => Some((movement.source_module.clone(), index)),
            _ => None,
        });
    let mut pulled_anonymous =
        take_indexed(docs, moved_anonymous, |doc| &mut doc.anonymous_statements);

    let mut destination_claims = BTreeMap::<(String, String, usize), usize>::new();
    let mut moved_names_by_destination = BTreeMap::<String, BTreeSet<String>>::new();
    for movement in planned_moves {
        match movement.slot {
            SourceUnitClaimSlot::Member(index) => {
                let member = pulled_members
                    .remove(&(movement.source_module.clone(), index))
                    .expect("planned member was extracted");
                let name = member_effective_name(&member).with_context(|| {
                    format!(
                        "source unit(s) {:?} member has no binding name",
                        movement.source_unit_ids
                    )
                })?;
                moved_names_by_destination
                    .entry(movement.destination.clone())
                    .or_default()
                    .insert(name.clone());
                let annotation = docs
                    .get_mut(&movement.source_module)
                    .expect("source module remains loaded")
                    .1
                    .annotations
                    .remove(&name);
                let destination =
                    ensure_source_unit_destination(docs, modules_root, &movement.destination);
                destination.members.push(member);
                insert_annotation(destination, &name, annotation)?;
            }
            SourceUnitClaimSlot::SourceMatch(claim_index, binding_index) => {
                let mut claim = pulled_source_match_bindings
                    .remove(&(movement.source_module.clone(), claim_index, binding_index))
                    .expect("planned source-match binding was extracted");
                anyhow::ensure!(
                    claim.bindings.len() == 1,
                    "source-match split did not isolate one binding"
                );
                let binding = &claim.bindings[0];
                let label = BindingLabel::new(
                    BindingIdentity::SourceMatchCapture {
                        local: binding.local().to_string(),
                    },
                    source_match_readable(binding),
                );
                let effective_name = binding_effective_name(&label).to_string();
                moved_names_by_destination
                    .entry(movement.destination.clone())
                    .or_default()
                    .insert(effective_name.clone());
                let annotation = docs
                    .get_mut(&movement.source_module)
                    .expect("source module remains loaded")
                    .1
                    .annotations
                    .remove(&effective_name);
                let destination =
                    ensure_source_unit_destination(docs, modules_root, &movement.destination);
                let key = (
                    movement.destination.clone(),
                    movement.source_module.clone(),
                    claim_index,
                );
                if let Some(index) = destination_claims.get(&key) {
                    destination.source_matches[*index]
                        .bindings
                        .append(&mut claim.bindings);
                } else {
                    let index = destination.source_matches.len();
                    destination.source_matches.push(claim);
                    destination_claims.insert(key, index);
                }
                insert_annotation(destination, &effective_name, annotation)?;
            }
            SourceUnitClaimSlot::Anonymous(index) => {
                let statement = pulled_anonymous
                    .remove(&(movement.source_module.clone(), index))
                    .expect("planned anonymous selector was extracted");
                ensure_source_unit_destination(docs, modules_root, &movement.destination)
                    .anonymous_statements
                    .push(statement);
            }
        }
    }
    Ok(moved_names_by_destination)
}

fn check_moved_name_collisions(
    docs: &ModuleDocs,
    modules_root: &Path,
    moved_names_by_destination: &BTreeMap<String, BTreeSet<String>>,
) -> Result<()> {
    for (destination, moved_names) in moved_names_by_destination {
        let (_, module) = docs
            .get(destination)
            .with_context(|| format!("destination module {destination:?} is missing"))?;
        let mut counts = BTreeMap::<String, usize>::new();
        for binding in binding_matches_in_doc(
            &modules_root.join(format!("{destination}.yaml")),
            destination,
            module,
        ) {
            *counts
                .entry(binding_effective_name(&binding.label).to_string())
                .or_default() += 1;
        }
        for name in moved_names {
            let count = counts.get(name).copied().unwrap_or_default();
            anyhow::ensure!(
                count == 1,
                "moving a claim named {name:?} into {destination} would create a binding-name collision ({count} entries)"
            );
        }
    }
    Ok(())
}

/// Plans moving every claim slot of an assigned source unit from its one
/// physical source module to the destination, merging slots shared with
/// source units already planned.
fn plan_claim_moves(
    planned_moves: &mut Vec<PlannedSourceUnitMove>,
    planned_move_by_slot: &mut BTreeMap<(String, SourceUnitClaimSlot), usize>,
    locations_by_source_unit: &BTreeMap<String, Vec<SourceUnitClaimLocation>>,
    existing_destinations: &BTreeMap<String, Vec<String>>,
    source_unit_id: &str,
    destination: &str,
    source_logical_module: &str,
) -> Result<()> {
    let source_unit_locations = locations_by_source_unit.get(source_unit_id).with_context(|| {
        format!("source unit {source_unit_id:?} has no movable member or anonymous selector in the spec")
    })?;
    let mut physical_sources = BTreeSet::new();
    for location in source_unit_locations {
        anyhow::ensure!(
            location.logical_module == source_logical_module,
            "source unit {source_unit_id:?} has claims in more than one source module"
        );
        physical_sources.insert(location.physical_module.clone());
    }
    anyhow::ensure!(
        physical_sources.len() == 1,
        "source unit {source_unit_id:?} resolves to multiple physical module files with the same logical identity"
    );
    let source_module = physical_sources
        .into_iter()
        .next()
        .expect("movable source unit has at least one location");
    let destination_module = match existing_destinations.get(destination) {
        Some(paths) if paths.len() == 1 => paths[0].clone(),
        Some(paths) => anyhow::bail!(
            "logical destination {destination:?} matches multiple module files: {}",
            paths.join(", ")
        ),
        None => destination.to_string(),
    };
    for location in source_unit_locations {
        let key = (source_module.clone(), location.slot.clone());
        if let Some(&move_index) = planned_move_by_slot.get(&key) {
            let movement = &mut planned_moves[move_index];
            anyhow::ensure!(
                movement.destination == destination_module,
                "one spec claim resolves to multiple requested destinations for source units {:?} and {:?}",
                movement.source_unit_ids,
                source_unit_id
            );
            movement.source_unit_ids.insert(source_unit_id.to_string());
        } else {
            let move_index = planned_moves.len();
            planned_move_by_slot.insert(key, move_index);
            planned_moves.push(PlannedSourceUnitMove {
                source_unit_ids: BTreeSet::from([source_unit_id.to_string()]),
                source_module: source_module.clone(),
                destination: destination_module.clone(),
                slot: location.slot.clone(),
            });
        }
    }
    Ok(())
}

fn source_unit_claim_locations(
    dependency_graph: &DependencyGraphReport,
    modules_root: &Path,
    docs: &ModuleDocs,
    post_spec: &crate::edit_gate::PostEditSpec,
    projection: &SourceUnitClaimProjection,
    scope: Option<&ModuleTreeScope>,
) -> Result<Vec<SourceUnitClaimLocation>> {
    anyhow::ensure!(
        post_spec.modules.len() == projection.modules.len(),
        "projected selector results do not match the current module list"
    );
    let mut locations = Vec::new();
    for (module, projected) in post_spec.modules.iter().zip(&projection.modules) {
        let module_path = module_path_from_file(&module.path, modules_root);
        let logical_module = logical_module_path(&module_path, scope)?;
        let (_, doc) = docs.get(&module_path).with_context(|| {
            format!(
                "module {} is absent from the scoped spec",
                module.path.display()
            )
        })?;
        anyhow::ensure!(
            doc.members.len() == projected.local_name_by_member.len(),
            "projected member claims do not match module {}",
            module.path.display()
        );
        let mut record_local = |binding_name: &str, slot: SourceUnitClaimSlot| {
            if let Some(LocalBindingResolution::Resolved(source_units)) =
                projected.local_bindings.get(binding_name)
            {
                for source_unit in source_units {
                    locations.push(SourceUnitClaimLocation {
                        source_unit_id: dependency_graph.nodes[source_unit.0].id.clone(),
                        physical_module: module_path.clone(),
                        logical_module: logical_module.clone(),
                        slot: slot.clone(),
                    });
                }
            }
        };
        for (member_index, binding) in projected.local_name_by_member.iter().enumerate() {
            if let Some(binding) = binding {
                record_local(binding, SourceUnitClaimSlot::Member(member_index));
            }
        }
        for (claim_index, claim) in doc.source_matches.iter().enumerate() {
            for (binding_index, binding) in claim.bindings.iter().enumerate() {
                let resolved_binding = projected
                    .resolved_member_bindings
                    .get(binding.name())
                    .with_context(|| {
                        format!(
                            "source_matches[{claim_index}] binding {:?} in {module_path} did not resolve",
                            binding.local()
                        )
                    })?;
                // `local` is the selector-local name used by the source
                // pattern. Under alpha-equivalent matching, the actual source
                // declaration can have a different name; the canonical
                // resolver's outcome is the authoritative binding identity.
                record_local(
                    resolved_binding,
                    SourceUnitClaimSlot::SourceMatch(claim_index, binding_index),
                );
            }
        }
        let mut selectors = BTreeSet::new();
        for (statement_index, statement) in doc.anonymous_statements.iter().enumerate() {
            let selector = statement.selector()?;
            anyhow::ensure!(
                selectors.insert(selector.clone()),
                "module {module_path} repeats an anonymous selector, so its metadata cannot be assigned unambiguously"
            );
            let source_unit = projected
                .anonymous_selector_source_units
                .get(&selector)
                .with_context(|| {
                    format!(
                        "anonymous_statements[{statement_index}] in {module_path} did not resolve to an source unit"
                    )
                })?;
            locations.push(SourceUnitClaimLocation {
                source_unit_id: dependency_graph
                    .nodes
                    .get(source_unit.0)
                    .with_context(|| {
                        format!("anonymous source unit index {} is absent", source_unit.0)
                    })?
                    .id
                    .clone(),
                physical_module: module_path.clone(),
                logical_module: logical_module.clone(),
                slot: SourceUnitClaimSlot::Anonymous(statement_index),
            });
        }
    }
    Ok(locations)
}

fn logical_module_path(raw_path: &str, scope: Option<&ModuleTreeScope>) -> Result<String> {
    if let Some(scope) = scope {
        scope.canonical_destination(raw_path)
    } else {
        canonical_module_path(raw_path)
    }
}

fn physical_modules_by_logical_destination(
    docs: &ModuleDocs,
    scope: Option<&ModuleTreeScope>,
) -> Result<BTreeMap<String, Vec<String>>> {
    let mut by_logical = BTreeMap::<String, Vec<String>>::new();
    for physical in docs.keys() {
        by_logical
            .entry(logical_module_path(physical, scope)?)
            .or_default()
            .push(physical.clone());
    }
    Ok(by_logical)
}

fn check_graph_source_unit_destinations(
    dependency_graph: &DependencyGraphReport,
    spec_source_unit_modules: &SourceUnitModuleAssignments,
    scope: Option<&ModuleTreeScope>,
) -> Result<()> {
    for node in &dependency_graph.nodes {
        let graph_destination = dependency_graph
            .module(&node.destination)
            .with_context(|| {
                format!("source unit {:?} has an unknown graph destination", node.id)
            })?;
        let expected = if matches!(
            graph_destination.kind,
            ModuleDestinationKind::Entry | ModuleDestinationKind::Unassigned
        ) {
            None
        } else {
            Some(graph_destination.path.as_str().to_string())
        };
        let actual = spec_source_unit_modules
            .get(&node.id)
            .with_context(|| {
                format!(
                    "source unit {:?} is absent from the current spec mapping",
                    node.id
                )
            })?
            .as_ref()
            .map(|path| {
                let logical = if let Some(scope) = scope {
                    scope.logical_destination(path)?
                } else {
                    path.as_str()
                };
                ModulePath::parse(logical, "")
                    .map(|path| path.as_str().to_string())
                    .map_err(|err| {
                        anyhow!("invalid logical module path in source unit spec: {err}")
                    })
            })
            .transpose()?;
        anyhow::ensure!(
            actual == expected,
            "dependency graph is stale for {:?}: graph destination {:?}, current spec destination {:?}",
            node.id,
            expected,
            actual
        );
    }
    Ok(())
}

fn ensure_source_unit_destination<'a>(
    docs: &'a mut ModuleDocs,
    modules_root: &Path,
    destination: &str,
) -> &'a mut LogicalModule {
    &mut docs
        .entry(destination.to_string())
        .or_insert_with(|| {
            (
                modules_root.join(format!("{destination}.yaml")),
                LogicalModule::default(),
            )
        })
        .1
}
