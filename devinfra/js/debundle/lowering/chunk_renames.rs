//! Validate + collect chunk-level `chunk_renames` from the spec,
//! including the body-level checks driven through `LoweringPlan`
//! (Phase 5 of the plan-pipeline migration). Phase 7a adds a
//! Plan-based wrapper for the entry-body import-disambiguation
//! call site in `lower.rs`. The other import-disambiguation call
//! sites (`imports_cross.rs`) still use the legacy
//! `mint_fresh_local_name` path — migrating them is a follow-up.
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

/// Phase 5 of the plan pipeline: validate `chunk_renames` against
/// the chunk's body via a `LoweringPlan` and return the body-level
/// rename map to seed `body_renames` in `lower.rs`. Replaces the
/// ad-hoc validation that previously lived inline in
/// `lower.rs:167-238`.
///
/// Bindings owned by a logical module are silently dropped — the
/// `disambiguate_import_locals` pass picks them up via the
/// logical-module plan's `bindings` map. Rename-to-self entries
/// are also dropped (the legacy code accepted them as no-ops; the
/// plan would otherwise flag the binding's own name as
/// already-taken).
///
/// On success returns `binding → export_name` for every accepted
/// rename. The caller adds the values to its `occupied` pool
/// before running import disambiguation so newly-minted import
/// locals can't collide with chunk-rename targets.
pub(super) fn validate_chunk_renames_via_plan(
    entry_body: &[ModuleItem],
    chunk_renames: &HashMap<String, String>,
    binding_assignment: &HashMap<Id, usize>,
    chunk_top_level_mark: Mark,
) -> Result<BTreeMap<String, String>> {
    // Seed the plan's name pool with every name currently bound in
    // the entry body. We do NOT subtract chunk_rename sources here:
    // the legacy validation rejected any target that collided with
    // a body local regardless of whether the colliding local was
    // itself being renamed away (the "duplicates an earlier rename
    // target" branch in `lower.rs:223` short-circuited the
    // "collides with body local" branch's `renamed_away` exception).
    // Preserve that behavior — rename-swaps were never supported in
    // spec validation and adding support is out of scope for the
    // migration.
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
    let mut plan = LoweringPlan::new(ModuleId::logical(0), Vec::new(), occupied_by_scope);

    let mut sorted_renames: Vec<(&String, &String)> = chunk_renames.iter().collect();
    sorted_renames.sort_by(|a, b| a.0.cmp(b.0));

    let mut errors = Vec::<String>::new();
    let mut accepted = BTreeMap::<String, String>::new();
    for (binding, export_name) in sorted_renames {
        if binding == export_name {
            // Rename-to-self: legacy accepted as no-op; plan would
            // otherwise reject because the binding's own name is in
            // the occupied pool. Skipping matches spec author intent.
            continue;
        }
        let original = top_level_id(binding, chunk_top_level_mark);
        if binding_assignment.contains_key(&original) {
            // Owned by a logical module — the logical-module
            // disambiguation pass handles this binding's rename.
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
            Err(e) => {
                errors.push(format!("binding {binding} → {export_name}: {e}"));
            }
        }
    }
    if !errors.is_empty() {
        bail!("invalid chunk_renames spec:\n  - {}", errors.join("\n  - "));
    }
    // Seal proves the plan is internally consistent (no rename/move
    // incoherence). For Phase 5 we don't carry it past this point —
    // `body_renames` consumption stays in `lower.rs`.
    plan.seal()?;
    Ok(accepted)
}

/// Phase 7a: import-disambiguation for the entry-body call site
/// in `lower.rs`, routed through `LoweringPlan::submit` with
/// `NamePolicy::MintOrSuffix` at `Priority::ImportInduced`. Drop-in
/// replacement for the legacy `disambiguate_import_locals` —
/// same `(occupied, renames) -> resolved` signature, but the
/// fresh-name minting happens through the plan's unified `_N`
/// suffixer with identifier validity baked in.
///
/// The plan is ephemeral: built per call from the current
/// `occupied` set, dropped after submission. Future phases will
/// thread a single chunk-wide plan through the lowering pipeline
/// (Phase 9 retires the defensive bridge in
/// `plan_module_reference_needs` once that thread is in place).
pub(super) fn disambiguate_import_locals_via_plan(
    live_bindings: &BTreeMap<String, String>,
    occupied: &mut BTreeSet<String>,
    emit_renames: &mut BTreeMap<String, String>,
    chunk_top_level_mark: Mark,
) -> Result<BTreeMap<String, String>> {
    let mut occupied_by_scope = HashMap::new();
    occupied_by_scope.insert(
        Scope::Chunk,
        occupied
            .iter()
            .map(|s| Atom::from(s.as_str()))
            .collect::<HashSet<_>>(),
    );
    let mut plan = LoweringPlan::new(ModuleId::logical(0), Vec::new(), occupied_by_scope);

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
