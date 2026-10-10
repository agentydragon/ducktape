//! Tuple-aware `var` binding-group read-off and shared candidate dispatch.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::{
    kept_spans_for_anchor_set, kept_spans_for_anchor_sets, unindexed_literal_spans,
};
use swc_common::Spanned;
use swc_ecma_ast::*;

use super::object::{object_anchor_ranking, try_object_read_off_candidates};
use super::var::try_var_read_off_candidates;
use super::{relax_exact_declaration, render_var_slots, render_via_neighbor_context};
use crate::regex_anchor::{accepted_regex_anchors, collect_regex_anchor_candidates};
use crate::render::{AnchorSpan, MAX_MINIMIZER_ANCHORS, holes_present, node_holds_anchor};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    prove_synthesized_selector, single_ident_pat_name,
};

/// Probe prefixes of the shape index's ranking in exponentially growing
/// batches, find the shortest proving prefix, then remove redundant blocks.
/// The bounded pruning pass keeps the selector sparse without scoring every
/// possible next anchor at every step. Callers may keep a small prefix after a
/// failed search as a tuple seed.
fn extend_ranked_prefix(
    seed: &BTreeSet<AnchorSpan>,
    ranked: &[AnchorSpan],
    limit: usize,
    proves: impl Fn(&BTreeSet<AnchorSpan>) -> Result<bool>,
) -> Result<(BTreeSet<AnchorSpan>, bool)> {
    if proves(seed)? {
        return Ok((seed.clone(), true));
    }
    let mut seen = seed.clone();
    let additions: Vec<_> = ranked
        .iter()
        .copied()
        .filter(|span| seen.insert(*span))
        .take(limit)
        .collect();
    let mut kept = seed.clone();
    let mut previous_end = 0;
    let mut end = 1;
    while previous_end < additions.len() {
        end = end.min(additions.len());
        kept.extend(additions[previous_end..end].iter().copied());
        if proves(&kept)? {
            let mut low = previous_end + 1;
            let mut high = end;
            while low < high {
                let mid = (low + high) / 2;
                let trial = seed
                    .iter()
                    .copied()
                    .chain(additions[..mid].iter().copied())
                    .collect();
                if proves(&trial)? {
                    high = mid;
                } else {
                    low = mid + 1;
                }
            }
            let mut minimal: BTreeSet<_> = seed
                .iter()
                .copied()
                .chain(additions[..low].iter().copied())
                .collect();
            let mut width = (low.next_power_of_two() / 2).max(1);
            let mut pruned = 0;
            while width > 0 && pruned < 24 {
                for block in additions[..low].chunks(width) {
                    if pruned == 24 {
                        break;
                    }
                    let mut trial = minimal.clone();
                    for anchor in block {
                        trial.remove(anchor);
                    }
                    if trial.len() == minimal.len() {
                        continue;
                    }
                    pruned += 1;
                    if proves(&trial)? {
                        minimal = trial;
                    }
                }
                width /= 2;
            }
            return Ok((minimal, true));
        }
        previous_end = end;
        end = end.saturating_mul(2);
    }
    Ok((kept, false))
}

/// Non-object slots reuse the shape index's ranked features instead of a second
/// expression walk. Object slots keep the shared value-before-key policy.
fn slot_anchor_ranking(declarator: &VarDeclarator, ranked_spans: &[AnchorSpan]) -> Vec<AnchorSpan> {
    if let Some(Expr::Object(object)) = declarator.init.as_deref() {
        return object_anchor_ranking(object);
    }
    ranked_spans
        .iter()
        .copied()
        .filter(|span| node_holds_anchor(declarator.span(), *span))
        .collect()
}

/// Prefer anchors that resolve each slot independently, but retain a small
/// prefix when only the complete tuple can disambiguate the binding group.
fn slot_minimal_anchors(
    index: &ChunkSelectorIndex<'_>,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    slot: usize,
    target: &SynthesizedTargetBinding,
    seed: &BTreeSet<AnchorSpan>,
    ranked: &[AnchorSpan],
) -> Result<BTreeSet<AnchorSpan>> {
    let runtime = target.runtime_binding.as_str();
    // Single-target view of this slot: the slot is the lone target, every other
    // declarator holes to a `DECLARATORS_*` run.
    let only_this = BTreeSet::from([slot]);
    let export_for = |name: &str| (name == runtime).then(|| target.selector_local.clone());
    let no_regex = BTreeMap::new();
    let render_slot = |kept: &BTreeSet<AnchorSpan>| -> Result<String> {
        render_var_slots(var, &only_this, &export_for, kept, &no_regex)
    };
    // The slot resolves when its single-target view proves.
    let slot_resolves = |kept: &BTreeSet<AnchorSpan>| -> Result<bool> {
        Ok(prove_synthesized_selector(
            index,
            decl,
            std::slice::from_ref(target),
            &render_slot(kept)?,
        )
        .is_ok())
    };
    let (kept, resolved) =
        extend_ranked_prefix(seed, ranked, MAX_MINIMIZER_ANCHORS, slot_resolves)?;
    if resolved {
        return Ok(kept);
    }
    let mut partial = seed.clone();
    for &span in ranked {
        if kept.contains(&span) && partial.insert(span) && partial.len() == seed.len() + 2 {
            break;
        }
    }
    Ok(partial)
}

/// Resolve the complete declarator tuple: independently useful slot anchors
/// are seeds, not a requirement that every slot be unique in isolation. Extend
/// the union with ranked anchors until the production matcher proves the tuple.
/// Both candidate policies use the same renderer, tuple proof and regex upgrade.
fn try_var_group_read_off(
    index: &ChunkSelectorIndex<'_>,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    target_slots: &BTreeSet<usize>,
    value_first: bool,
) -> Result<Option<(SpecializedSelector, bool)>> {
    let export_for = |runtime: &str| {
        targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .map(|target| target.selector_local.clone())
    };
    let item = index
        .module
        .body
        .get(decl.body_idx)
        .context("read-off body index no longer in module")?;
    let ranked_anchor_sets: Vec<_> = index
        .shape_index
        .scored_features(decl.body_idx)
        .into_iter()
        .map(|feature| shape_index::AnchorSet {
            body_idx: decl.body_idx,
            anchors: vec![feature],
            opt_one: false,
        })
        .collect();
    let mut ranked_spans = Vec::new();
    let mut seen_spans = BTreeSet::new();
    for spans in kept_spans_for_anchor_sets(item, &ranked_anchor_sets) {
        for span in spans {
            if seen_spans.insert(span) {
                ranked_spans.push(span);
            }
        }
    }
    for span in unindexed_literal_spans(item) {
        if seen_spans.insert(span) {
            ranked_spans.push(span);
        }
    }
    let chunk_kept = index
        .shape_index
        .minimal_anchor_set(decl.body_idx)
        .map(|set| kept_spans_for_anchor_set(item, &set))
        .unwrap_or_default();
    let mut tuple_ranked = Vec::new();
    let mut union: BTreeSet<AnchorSpan> = BTreeSet::new();
    for &slot in target_slots {
        let runtime = single_ident_pat_name(&var.decls[slot].name)
            .expect("target declarator is a plain identifier");
        let target = targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .expect("target slot has a target");
        let slot_decl_span = var.decls[slot].span();
        let mut seed: BTreeSet<AnchorSpan> = chunk_kept
            .iter()
            .copied()
            .filter(|span| node_holds_anchor(slot_decl_span, *span))
            .collect();
        let ranked = slot_anchor_ranking(&var.decls[slot], &ranked_spans);
        if value_first {
            seed = ranked.first().copied().into_iter().collect();
        }
        union.extend(slot_minimal_anchors(
            index, var, decl, slot, target, &seed, &ranked,
        )?);
        tuple_ranked.extend(ranked.into_iter().take(MAX_MINIMIZER_ANCHORS));
    }

    // The object slot policy prefers direct values/keys, but a joint tuple may
    // need a deeper anchor that cannot distinguish either slot on its own.
    let tuple_fallback: Vec<_> = ranked_spans
        .into_iter()
        .filter(|anchor| {
            target_slots
                .iter()
                .any(|&slot| node_holds_anchor(var.decls[slot].span(), *anchor))
        })
        .collect();
    tuple_ranked.extend(tuple_fallback.iter().copied().take(MAX_MINIMIZER_ANCHORS));

    let no_regex = BTreeMap::new();
    let render_with = |kept: &BTreeSet<AnchorSpan>,
                       regex_anchors: &BTreeMap<AnchorSpan, String>|
     -> Result<String> {
        render_var_slots(var, target_slots, &export_for, kept, regex_anchors)
    };
    let resolves = |kept: &BTreeSet<AnchorSpan>| -> Result<bool> {
        Ok(
            prove_synthesized_selector(index, decl, targets, &render_with(kept, &no_regex)?)
                .is_ok(),
        )
    };
    let (extended, mut resolved) =
        extend_ranked_prefix(&union, &tuple_ranked, tuple_ranked.len(), resolves)?;
    union = extended;
    if !resolved {
        // Bound greedy search, not completeness: one full-anchor proof handles
        // a discriminator beyond the search budget without a second renderer.
        union.extend(tuple_fallback);
        resolved = resolves(&union)?;
    }
    if !resolved {
        // The tuple read-off has exhausted its indexed anchors. An operator or
        // other unindexed AST detail may still distinguish the declaration;
        // relax that exact witness before borrowing an adjacent statement.
        if let Some(selector) = relax_exact_declaration(index, item, decl, targets)? {
            return Ok(Some((selector, true)));
        }
        if let [target] = targets {
            let scaffold = render_with(&BTreeSet::new(), &no_regex)?;
            return Ok(render_via_neighbor_context(index, decl, target, &scaffold)?
                .map(|selector| (selector, false)));
        }
        return Ok(None);
    }

    let mut regex_candidates = BTreeMap::new();
    for &slot in target_slots {
        if let Some(init) = &var.decls[slot].init {
            regex_candidates.extend(collect_regex_anchor_candidates(init));
        }
    }
    let regex_anchors = accepted_regex_anchors(
        index,
        decl,
        targets,
        &regex_candidates,
        &union,
        &render_with,
    )?;

    let source = render_with(&union, &regex_anchors)?;
    let rewritten_holes = holes_present(&source)?;
    let all_slots_anchored = target_slots.iter().all(|&slot| {
        var.decls[slot].init.as_ref().is_some_and(|init| {
            union
                .iter()
                .any(|span| node_holds_anchor(init.span(), *span))
        })
    });
    Ok(Some((
        SpecializedSelector {
            match_source: source,
            rewritten_holes,
        },
        all_slots_anchored,
    )))
}

/// Up to `limit` distinct, proven selectors. Single-target forms retain their
/// specialized menus; groups try selective-first and value-first tuple seeds.
pub(crate) fn minimize_var_group_selector_candidates(
    index: &ChunkSelectorIndex<'_>,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    let export_for = |runtime: &str| {
        targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .map(|target| target.selector_local.clone())
    };
    let target_slots: BTreeSet<usize> = var
        .decls
        .iter()
        .enumerate()
        .filter_map(|(idx, declarator)| {
            let name = single_ident_pat_name(&declarator.name)?;
            export_for(name).map(|_| idx)
        })
        .collect();
    if target_slots.len() != targets.len() {
        return Ok(Vec::new());
    }

    let object = try_object_read_off_candidates(index, var, decl, targets, &target_slots, limit)?;
    if !object.is_empty() {
        return Ok(object);
    }
    if let [target] = targets
        && target_slots.len() == 1
    {
        let slot = *target_slots.iter().next().expect("one target slot");
        let var_candidates = try_var_read_off_candidates(index, var, decl, target, slot, limit)?;
        if !var_candidates.is_empty() {
            return Ok(var_candidates);
        }
    }
    let mut out: Vec<(SpecializedSelector, bool)> = Vec::new();
    // A tuple can prove solely from slot positions while leaving a target's
    // initializer as `ANYTHING`. Prefer the value-first alternative only when
    // it anchors every slot and the selective-first choice does not.
    for value_first in [false, true] {
        if limit == 0 || (limit == 1 && out.first().is_some_and(|(_, anchored)| *anchored)) {
            break;
        }
        if let Some((selector, all_slots_anchored)) =
            try_var_group_read_off(index, var, decl, targets, &target_slots, value_first)?
            && !out
                .iter()
                .any(|(kept, _)| kept.match_source == selector.match_source)
        {
            out.push((selector, all_slots_anchored));
        }
    }
    out.sort_by_key(|(_, anchored)| !anchored);
    Ok(out
        .into_iter()
        .take(limit)
        .map(|(selector, _)| selector)
        .collect())
}

/// The default pick is exactly the first candidate, so dispatch cannot drift
/// between ordinary synthesis and the interactive candidate menu.
pub(crate) fn minimize_var_group_selector(
    index: &ChunkSelectorIndex<'_>,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    Ok(
        minimize_var_group_selector_candidates(index, var, decl, targets, 1)?
            .into_iter()
            .next(),
    )
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use super::*;

    #[test]
    fn ranked_prefix_finds_earliest_proving_anchor_with_logarithmic_probes() {
        let ranked: Vec<AnchorSpan> = (0..64).map(|n| (n, n + 1)).collect();
        let probes = Cell::new(0);
        let (kept, resolved) =
            extend_ranked_prefix(&BTreeSet::new(), &ranked, ranked.len(), |trial| {
                probes.set(probes.get() + 1);
                Ok(trial.contains(&(31, 32)))
            })
            .unwrap();
        assert!(resolved);
        assert_eq!(kept, BTreeSet::from([(31, 32)]));
        assert!(probes.get() <= 36, "{} proofs", probes.get());
    }
}
