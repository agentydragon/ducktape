//! Contraction of mutually-constraining generated residual plans.

use crate::exports::PublicExportSurface;
use crate::plans::ModulePlan;
use analysis::{BindingCatalogue, BindingKind, ModuleId, ModuleRole, top_level_id};
use anyhow::{Result, ensure};
use frontend_elaboration::chunk_ast::SourceExportIdentity;
use gate::SccRejection;
use source_identity::StatementId;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use swc_common::Mark;

/// Return only complete Pass 1 SCCs whose module IDs have explicit provenance
/// as coarse-generated residual plans. This consumes the gate's typed in-memory
/// diagnosis; the rendered cycle report and its cut are intentionally ignored.
pub(super) fn generated_residual_mutual_sccs(
    diagnoses: &[gate::SccDiagnosis],
    generated_residual_plans: &BTreeSet<usize>,
    module_count: usize,
    module_plans: &[ModulePlan],
    source_exports_by_statement: &HashMap<StatementId, Vec<SourceExportIdentity>>,
    chunk_top_level_mark: Mark,
) -> Vec<Vec<usize>> {
    diagnoses
        .iter()
        .filter(|diagnosis| diagnosis.rejection == SccRejection::MutualConstrainingCycle)
        .filter_map(|diagnosis| {
            let modules = &diagnosis.core.modules;
            if modules.len() < 2
                || modules.iter().any(|module| {
                    module.0 >= module_count || !generated_residual_plans.contains(&module.0)
                })
            {
                return None;
            }
            let mut merged_surface = PublicExportSurface::default();
            for module in modules {
                let plan = &module_plans[module.0];
                let mut plan_surface = PublicExportSurface::default();
                for (local, public) in &plan.bindings {
                    // `top_level_id` reconstructs the chunk-level Id used by
                    // the resolved AST and dependency graph. Source export
                    // aliases therefore compare against the same identity as
                    // generated residual exports.
                    plan_surface
                        .add_binding(public.clone(), top_level_id(local, chunk_top_level_mark));
                }
                for statement in &plan.anonymous_statements {
                    if let Some(exports) = source_exports_by_statement.get(statement) {
                        plan_surface.add_source_identities(exports);
                    }
                }
                if !merged_surface.is_compatible_with(&plan_surface) {
                    // Keep this SCC separate; the original gate diagnosis
                    // remains blocking and is handled by the ordinary path.
                    return None;
                }
                merged_surface.merge(&plan_surface);
            }
            Some(modules.iter().map(|module| module.0).collect())
        })
        .collect()
}

/// Contract disjoint, complete SCCs of generated residual plans and reproject
/// every positional owner. Authored plans, source-unit identities, and module
/// contents survive; the survivor is the earliest plan in original plan order.
pub(super) fn contract_generated_residual_sccs(
    module_plans: &mut Vec<ModulePlan>,
    bindings_catalogue: &mut BindingCatalogue,
    whole_statement_assignment: &mut BTreeMap<source_identity::StatementId, usize>,
    generated_residual_plans: &mut BTreeSet<usize>,
    sccs: &[Vec<usize>],
) -> Result<()> {
    let old_plan_count = module_plans.len();
    ensure!(
        generated_residual_plans
            .iter()
            .all(|module| *module < old_plan_count),
        "generated residual provenance contains an invalid module ID"
    );
    let mut absorbed_by = vec![None; old_plan_count];
    let mut seen_members = BTreeSet::new();

    for scc in sccs {
        let mut members = scc.clone();
        members.sort_unstable();
        members.dedup();
        ensure!(members.len() >= 2, "cannot contract a singleton SCC");
        let survivor = members[0];
        ensure!(
            survivor < old_plan_count,
            "SCC contains an invalid module ID"
        );
        ensure!(
            members.iter().all(|member| {
                *member < old_plan_count && generated_residual_plans.contains(member)
            }),
            "SCC contraction received a module without generated-residual provenance"
        );
        for member in &members {
            ensure!(
                seen_members.insert(*member),
                "SCC module appears more than once"
            );
        }

        let mut merged = module_plans[survivor].clone();
        for member in members.iter().copied().skip(1) {
            merge_generated_residual_plan(&mut merged, &module_plans[member])?;
            absorbed_by[member] = Some(survivor);
        }
        module_plans[survivor] = merged;
    }

    let mut old_to_new = vec![ModuleId(usize::MAX); old_plan_count + 1];
    let mut compacted_plans = Vec::with_capacity(old_plan_count - seen_members.len() + sccs.len());
    for (old_id, plan) in std::mem::take(module_plans).into_iter().enumerate() {
        if let Some(survivor) = absorbed_by[old_id] {
            ensure!(
                survivor < old_id,
                "SCC survivor must preserve original order"
            );
            old_to_new[old_id] = old_to_new[survivor];
        } else {
            old_to_new[old_id] = ModuleId(compacted_plans.len());
            compacted_plans.push(plan);
        }
    }
    old_to_new[old_plan_count] = ModuleId(compacted_plans.len());

    let chunk_id = bindings_catalogue.chunk_id();
    let mut remapped_bindings = bindings_catalogue.local_view(chunk_id)?.as_map().clone();
    for binding in remapped_bindings.values_mut() {
        match binding {
            BindingKind::Owned { module } => {
                *module = remap_planned_or_entry_module(*module, &old_to_new, old_plan_count)?;
            }
            BindingKind::Imported { re_exporter, .. } => {
                *re_exporter =
                    remap_planned_or_entry_module(*re_exporter, &old_to_new, old_plan_count)?;
            }
        }
    }
    *bindings_catalogue = BindingCatalogue::new(chunk_id, remapped_bindings);

    for module in whole_statement_assignment.values_mut() {
        ensure!(
            *module < old_plan_count,
            "anonymous statement has invalid module ID"
        );
        *module = old_to_new[*module].0;
    }
    *generated_residual_plans = generated_residual_plans
        .iter()
        .map(|module| old_to_new[*module].0)
        .collect();
    *module_plans = compacted_plans;
    Ok(())
}

fn remap_planned_or_entry_module(
    module: ModuleId,
    old_to_new: &[ModuleId],
    old_plan_count: usize,
) -> Result<ModuleId> {
    ensure!(
        module.0 <= old_plan_count,
        "binding catalogue has invalid module ID"
    );
    Ok(old_to_new[module.0])
}

fn merge_generated_residual_plan(target: &mut ModulePlan, source: &ModulePlan) -> Result<()> {
    ensure!(
        target.role == ModuleRole::Unassigned && source.role == ModuleRole::Unassigned,
        "generated residual plans must retain their unassigned role"
    );
    for (name, export) in &source.bindings {
        if let Some(existing) = target.bindings.get(name) {
            ensure!(
                existing == export,
                "generated residual plans disagree on export for {name:?}"
            );
        } else {
            target.bindings.insert(name.clone(), export.clone());
        }
    }
    target
        .spec_named_bindings
        .extend(source.spec_named_bindings.iter().cloned());
    target
        .anonymous_statements
        .extend(source.anonymous_statements.iter().copied());
    target.anonymous_statements.sort_unstable();
    target.anonymous_statements.dedup();
    merge_equal_keyed_values(
        &mut target.anonymous_statement_comments,
        &source.anonymous_statement_comments,
    )?;
    merge_equal_keyed_values(&mut target.binding_comments, &source.binding_comments)?;
    merge_equal_keyed_values(
        &mut target.binding_claim_origins,
        &source.binding_claim_origins,
    )?;
    match (&target.comment, &source.comment) {
        (Some(target), Some(source)) => ensure!(target == source, "residual comments disagree"),
        (None, Some(source)) => target.comment = Some(source.clone()),
        _ => {}
    }
    Ok(())
}

fn merge_equal_keyed_values<K, V>(
    target: &mut BTreeMap<K, V>,
    source: &BTreeMap<K, V>,
) -> Result<()>
where
    K: Ord + Clone + std::fmt::Debug,
    V: Clone + PartialEq,
{
    for (key, value) in source {
        if let Some(existing) = target.get(key) {
            ensure!(
                existing == value,
                "merged residual metadata conflicts at {key:?}"
            );
        } else {
            target.insert(key.clone(), value.clone());
        }
    }
    Ok(())
}
