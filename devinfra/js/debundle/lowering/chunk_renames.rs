//! Validate + collect chunk-level `chunk_renames` from the spec,
//! including the body-level checks driven through `LoweringPlan`
//! (Phase 5 of the plan-pipeline migration). Phases 7a-c also
//! routed every import-disambiguation call site through Plan
//! submission, retiring the legacy `mint_fresh_local_name`
//! helper.
//!
//! The AST mutation itself still runs through `IdentifierRenamer`
//! in `lower.rs` — the executor migration lands later, once all
//! rename contributors are on the plan and a single Plan-aware
//! visitor can replace `IdentifierRenamer` (which today
//! special-cases import/export specifiers, prop names, and
//! member props in ways the Phase 4a executor doesn't yet).

use std::collections::HashSet;

use swc_atoms::Atom;
use swc_common::Mark;

use super::lowering_plan::{
    LoweringOp, LoweringPlan, NamePolicy, Priority, Scope, SubmitOutcome, SubmitPolicy,
};
use super::util::collect_occupied_local_names;
use super::*;

pub(super) fn collect_chunk_renames(
    chunk_renames: &ChunkRenames,
) -> Result<HashMap<String, String>> {
    let mut renames = HashMap::<String, String>::new();
    let id = chunk_renames.id.as_deref().unwrap_or("chunk_renames");
    for member in &chunk_renames.members {
        let binding = member.selector.binding.name.clone();
        let export_name = member.name.clone().unwrap_or_else(|| binding.clone());
        if let Some(existing) = renames.get(&binding) {
            if existing != &export_name {
                bail!(
                    "chunk_renames {id}: binding {binding} already renamed to \
                     {existing}; refusing to overwrite with {export_name}"
                );
            }
        } else {
            renames.insert(binding, export_name);
        }
    }
    Ok(renames)
}

/// Build a fresh chunk-level `LoweringPlan` for `lower_chunk`,
/// seeded with the chunk body's existing top-level names so that
/// rename submissions don't collide with what's already in the
/// AST. Callers extend the plan's name pool later with nested
/// binding names via `LoweringPlan::extend_occupied` before the
/// import-disambiguation phase.
pub(super) fn new_chunk_plan(entry_body: &[ModuleItem]) -> LoweringPlan {
    let body_locals: HashSet<Atom> = collect_occupied_local_names(entry_body)
        .into_iter()
        .map(|s| Atom::from(s.as_str()))
        .collect();
    let mut occupied_by_scope = HashMap::new();
    occupied_by_scope.insert(Scope::Chunk, body_locals);
    // `Scope::Chunk` renames don't consult `plan.modules()` or
    // `plan.residual()`, so a placeholder residual + empty modules
    // list is fine — neither submit, the chunk-coherence rule, nor
    // execute reads them for chunk-scope renames.
    LoweringPlan::new(ModuleId::logical(0), Vec::new(), occupied_by_scope)
}

/// Phase 5 of the plan pipeline: validate `chunk_renames` against
/// the shared chunk plan and return the body-level rename map.
/// Replaces the ad-hoc validation that previously lived inline in
/// `lower.rs:167-238`.
///
/// Bindings owned by a logical module are silently dropped — the
/// `disambiguate_import_locals` pass picks them up via the
/// logical-module plan's `bindings` map. Rename-to-self entries
/// are also dropped (legacy accepted these as no-ops; the plan
/// would reject because the binding's own name is in the occupied
/// pool).
pub(super) fn submit_chunk_renames(
    plan: &mut LoweringPlan,
    chunk_renames: &HashMap<String, String>,
    binding_assignment: &HashMap<Id, usize>,
    chunk_top_level_mark: Mark,
) -> Result<BTreeMap<String, String>> {
    let mut sorted_renames: Vec<(&String, &String)> = chunk_renames.iter().collect();
    sorted_renames.sort_by(|a, b| a.0.cmp(b.0));

    let mut errors = Vec::<String>::new();
    let mut accepted = BTreeMap::<String, String>::new();
    for (binding, export_name) in sorted_renames {
        if binding == export_name {
            continue;
        }
        let original = top_level_id(binding, chunk_top_level_mark);
        if binding_assignment.contains_key(&original) {
            continue;
        }
        match plan.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original,
                name: NamePolicy::Required(Atom::from(export_name.as_str())),
                reason: "chunk_renames",
                priority: Priority::Explicit,
            },
            SubmitPolicy::Fail,
        ) {
            Ok(_) => {
                accepted.insert(binding.clone(), export_name.clone());
            }
            Err(e) => errors.push(format!("binding {binding} → {export_name}: {e}")),
        }
    }
    if !errors.is_empty() {
        bail!("invalid chunk_renames spec:\n  - {}", errors.join("\n  - "));
    }
    Ok(accepted)
}

/// Phase 7: import-disambiguation routed through the shared
/// chunk `LoweringPlan` with `NamePolicy::MintOrSuffix` at
/// `Priority::ImportInduced`. The plan's `is_name_taken` queries
/// see every prior submission (chunk_renames, other
/// import-disambiguation calls in the same chunk), so the minted
/// `_N` suffix steers clear of all of them by construction.
///
/// The extern `occupied` BTreeSet and `emit_renames` BTreeMap are
/// still mutated for the legacy `IdentifierRenamer` pass — those
/// drop in a future iteration once the executor is plan-aware.
pub(super) fn disambiguate_import_locals_via_plan(
    plan: &mut LoweringPlan,
    live_bindings: &BTreeMap<String, String>,
    occupied: &mut BTreeSet<String>,
    emit_renames: &mut BTreeMap<String, String>,
    chunk_top_level_mark: Mark,
) -> Result<BTreeMap<String, String>> {
    let mut resolved = BTreeMap::new();
    for (original, exported) in live_bindings {
        let preferred = if exported != original {
            exported.as_str()
        } else {
            original.as_str()
        };
        let outcome = plan.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original: top_level_id(original, chunk_top_level_mark),
                name: NamePolicy::MintOrSuffix(Atom::from(preferred)),
                reason: "import_disambiguation",
                priority: Priority::ImportInduced,
            },
            SubmitPolicy::Fail,
        )?;
        let actual = match outcome {
            SubmitOutcome::Accepted {
                final_op:
                    LoweringOp::Rename {
                        name: NamePolicy::Required(atom),
                        ..
                    },
            } => atom.to_string(),
            other => bail!(
                "unexpected submit outcome for import disambiguation \
                 (binding {original} → preferred {preferred}): {other:?}"
            ),
        };
        occupied.insert(actual.clone());
        if actual != *original {
            emit_renames.insert(original.clone(), actual.clone());
        }
        resolved.insert(actual, exported.clone());
    }
    Ok(resolved)
}

/// Phase 7c counterpart of [`disambiguate_import_locals_via_plan`]
/// for residual-entry imports keyed on `EntryExport` (different
/// shape — preferred local is the entry's actual local name, not
/// the spec-exported alias). Returns `(actual_local → exported)`.
/// Shares the per-chunk `LoweringPlan` with all other rename
/// contributors.
pub(super) fn disambiguate_residual_entry_import_locals_via_plan(
    plan: &mut LoweringPlan,
    imports: &BTreeMap<String, super::plan_references::EntryExport>,
    occupied: &mut BTreeSet<String>,
    emit_renames: &mut BTreeMap<String, String>,
    chunk_top_level_mark: Mark,
) -> Result<BTreeMap<String, String>> {
    let mut resolved = BTreeMap::new();
    for (original, entry_export) in imports {
        let preferred = entry_export.local_name.as_str();
        let outcome = plan.submit(
            LoweringOp::Rename {
                scope: Scope::Chunk,
                original: top_level_id(original, chunk_top_level_mark),
                name: NamePolicy::MintOrSuffix(Atom::from(preferred)),
                reason: "residual_entry_import_disambiguation",
                priority: Priority::ImportInduced,
            },
            SubmitPolicy::Fail,
        )?;
        let actual = match outcome {
            SubmitOutcome::Accepted {
                final_op:
                    LoweringOp::Rename {
                        name: NamePolicy::Required(atom),
                        ..
                    },
            } => atom.to_string(),
            other => bail!(
                "unexpected submit outcome for residual-entry import disambiguation \
                 (binding {original} → preferred {preferred}): {other:?}"
            ),
        };
        occupied.insert(actual.clone());
        if actual != *original {
            emit_renames.insert(original.clone(), actual.clone());
        }
        resolved.insert(actual, entry_export.exported_name.clone());
    }
    Ok(resolved)
}
