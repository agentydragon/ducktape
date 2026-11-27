//! Per-scope target checks [`crate::RenameLedger::seal`] runs on the winning intents.

use crate::{
    ChunkRenameViolation, ModuleRenameViolation, RenamePriority, RenameScope, SealError,
    merge_module_renames,
};
use js_ast::is_binding_identifier;
use std::collections::{BTreeMap, BTreeSet};
use swc_atoms::Atom;
use swc_ecma_ast::Id;

/// Target validation for `Chunk` / `Module` / `EntryPublicExports`
/// scopes. Explicit intents replay the pre-ledger application-site
/// checks (hard errors, same messages); import-induced intents assert
/// the mint invariant; heuristic intents (free-source aliases) get the
/// [`merge_module_renames`] target-collision rule — their occupancy is
/// deliberately NOT checked here, matching pre-ledger behavior (they
/// apply function-locally; the subtree check runs on their
/// `Function`-scope copies).
pub(crate) fn validate_body_scope(
    scope: &RenameScope,
    label: &str,
    root: &BTreeSet<String>,
    nested: &BTreeSet<String>,
    captured: &BTreeSet<(String, String)>,
    mut winners: BTreeMap<Id, (Atom, RenamePriority)>,
) -> Result<BTreeMap<Id, (Atom, RenamePriority)>, SealError> {
    let chunk_style = matches!(scope, RenameScope::Chunk);
    // Sources of explicit renames vacate their root-level slot: a target
    // equal to another explicit rename's source is allowed past the
    // root-collision check (the binding is being renamed away) — but
    // only at the root level; a same-named nested binding stays
    // occupied. Chunk-style validation still reports such targets as
    // duplicates (the pre-ledger loop's growing occupied set already
    // held every root name), while module-style validation has no
    // duplicate-against-root rule, so chain/swap renames are allowed.
    let vacated: BTreeSet<String> = winners
        .iter()
        .filter(|(_, (_, priority))| *priority == RenamePriority::Explicit)
        .map(|(from, _)| from.0.to_string())
        .collect();
    let mut chunk_violations = Vec::new();
    let mut module_violations = Vec::new();
    // Chunk-style: grows with accepted targets so an earlier accepted
    // target occupies the name for later entries (sorted-by-source
    // order, matching the pre-ledger loop). Module-style: tracks rename
    // targets only, so two explicit renames sharing one target surface
    // (plan building already rejects duplicate export names; this is the
    // seal-side guarantee).
    let mut taken: BTreeSet<String> = if chunk_style {
        root.iter().cloned().collect()
    } else {
        BTreeSet::new()
    };
    for (from, (to, priority)) in &winners {
        if *priority != RenamePriority::Explicit {
            continue;
        }
        let from = from.0.to_string();
        let to = to.to_string();
        if chunk_style && !is_binding_identifier(&to) {
            chunk_violations.push(ChunkRenameViolation::NotAnIdentifier { from, to });
            continue;
        }
        let occupied_for_collision = if chunk_style { &taken } else { root };
        if to != from && occupied_for_collision.contains(&to) && !vacated.contains(&to) {
            if chunk_style {
                chunk_violations.push(ChunkRenameViolation::CollidesWithLocal { from, to });
            } else {
                module_violations.push(ModuleRenameViolation::CollidesWithBinding { from, to });
            }
            continue;
        }
        if !taken.insert(to.clone()) && to != from {
            if chunk_style {
                chunk_violations.push(ChunkRenameViolation::DuplicatesTarget { from, to });
            } else {
                module_violations.push(ModuleRenameViolation::DuplicatesTarget { from, to });
            }
        }
    }
    if !chunk_violations.is_empty() {
        return Err(SealError::InvalidChunkRenames(chunk_violations));
    }
    if !module_violations.is_empty() {
        return Err(SealError::InvalidModuleRenames {
            label: label.to_string(),
            violations: module_violations,
        });
    }
    // Capture facts are reference-precise. Explicit and import-induced
    // renames retain their hard-error contract, while heuristic renames are
    // cosmetic and are dropped when a target would capture a reference.
    let captured_non_heuristic: BTreeSet<(String, String)> = winners
        .iter()
        .filter(|(_, (_, priority))| *priority != RenamePriority::Heuristic)
        .filter(|&(from, (to, _))| captured.contains(&(from.0.to_string(), to.to_string())))
        .map(|(from, (to, _))| (from.0.to_string(), to.to_string()))
        .collect();
    if !captured_non_heuristic.is_empty() {
        let label = label.to_string();
        return Err(if chunk_style {
            SealError::ChunkRenamesCaptured {
                chunk: label,
                captured: captured_non_heuristic,
            }
        } else {
            SealError::ModuleRenamesCaptured {
                label,
                captured: captured_non_heuristic,
            }
        });
    }
    winners.retain(|from, (to, priority)| {
        *priority != RenamePriority::Heuristic
            || !captured.contains(&(from.0.to_string(), to.to_string()))
    });
    // A heuristic is optional readability. Unlike explicit renames, it
    // silently falls back when the target name is already occupied in the
    // body; no suffix is minted because that would invent a new name.
    winners.retain(|from, (to, priority)| {
        *priority != RenamePriority::Heuristic || to == &from.0 || !root.contains(to.as_ref())
    });
    for (from, (to, priority)) in &winners {
        if *priority == RenamePriority::ImportInduced
            && (root.contains(to.as_ref()) || nested.contains(to.as_ref()))
        {
            return Err(SealError::MintedTargetOccupied {
                scope: *scope,
                from: from.0.to_string(),
                to: to.to_string(),
            });
        }
    }
    // Heuristic (free-source) target collisions: drop losers silently.
    let explicit_by_name: BTreeMap<String, String> = winners
        .iter()
        .filter(|(_, (_, priority))| *priority != RenamePriority::Heuristic)
        .map(|(from, (to, _))| (from.0.to_string(), to.to_string()))
        .collect();
    let heuristic_by_name: BTreeMap<String, String> = winners
        .iter()
        .filter(|(_, (_, priority))| *priority == RenamePriority::Heuristic)
        .map(|(from, (to, _))| (from.0.to_string(), to.to_string()))
        .collect();
    if !heuristic_by_name.is_empty() {
        let merged = merge_module_renames(explicit_by_name, heuristic_by_name);
        winners.retain(|from, (to, priority)| {
            *priority != RenamePriority::Heuristic
                || merged.get(from.0.as_ref()).map(String::as_str) == Some(to.as_ref())
        });
    }
    Ok(winners)
}

/// Target validation for `Function` scopes, replaying the pre-ledger
/// per-scope heuristic rules. An intent whose source the subtree binds is
/// a bound-source rename (`validated_bound` rules: target unique among
/// the scope's bound-source targets, unreserved, absent from the
/// subtree's mentions, not another bound source). An intent whose source
/// the subtree does NOT bind is a free-source alias copy: its target must
/// not be bound in the subtree and its `(from, to)` pair must have
/// survived Module-scope validation. Failures drop silently
/// (over-suppression is acceptable for heuristics).
pub(crate) fn validate_function_scope(
    bound: &BTreeSet<String>,
    mentions: &BTreeSet<String>,
    reserved: &BTreeSet<String>,
    surviving_module_heuristics: &BTreeSet<(String, String)>,
    mut winners: BTreeMap<Id, (Atom, RenamePriority)>,
) -> BTreeMap<Id, (Atom, RenamePriority)> {
    let bound_sources: BTreeSet<String> = winners
        .keys()
        .map(|from| from.0.to_string())
        .filter(|sym| bound.contains(sym))
        .collect();
    let mut bound_target_counts = BTreeMap::<String, usize>::new();
    for (from, (to, _)) in &winners {
        if bound.contains(from.0.as_ref()) {
            *bound_target_counts.entry(to.to_string()).or_default() += 1;
        }
    }
    winners.retain(|from, (to, priority)| {
        if *priority != RenamePriority::Heuristic {
            return true;
        }
        let to = to.as_ref();
        if bound.contains(from.0.as_ref()) {
            bound_target_counts.get(to).copied() == Some(1)
                && !reserved.contains(to)
                && !mentions.contains(to)
                && !bound_sources.contains(to)
        } else {
            !bound.contains(to)
                && surviving_module_heuristics.contains(&(from.0.to_string(), to.to_string()))
        }
    });
    winners
}
