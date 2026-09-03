//! Object-valued `var` selector minimization: chunk-wide read-off then a
//! slot-aware key-set cover (`ANYTHING` run holes around the discriminating keys).

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_set;
use swc_common::Spanned;
use swc_ecma_ast::*;

use super::{finish_minimized_selector, hole_var_init_padded, render_var_slots};
use crate::render::{AnchorSpan, MAX_MINIMIZER_ANCHORS, node_holds_anchor, span_key};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    prove_synthesized_selector, solve_single_member_selector,
};

/// Ranked anchor spans for an object literal's key-set cover, best-first: each
/// direct literal/template **value** (a value-level discriminator, tried first)
/// then each **key** (the key-set discriminator). Value member accesses and
/// nested expressions are intentionally excluded — a minified `.prop` is
/// rebuild-volatile, so the cover pins the stable key and holes the value to
/// `ANYTHING` rather than anchoring on a churning property name.
///
/// Within each class the order is source order; the cover's slot-resolution
/// greedy ([`cover_object_slot`]) then picks the most discriminating anchor, so
/// this ordering only breaks ties — preferring a unique value (`accent:
/// "…Accent"`) over its equally-unique key (`accent: ANYTHING`) when both single
/// out the slot.
pub(crate) fn object_anchor_ranking(object: &ObjectLit) -> Vec<AnchorSpan> {
    let key_value_props = || {
        object.props.iter().filter_map(|prop| match prop {
            PropOrSpread::Prop(prop) => match prop.as_ref() {
                Prop::KeyValue(key_value) => Some(key_value),
                _ => None,
            },
            PropOrSpread::Spread(_) => None,
        })
    };
    let values = key_value_props()
        .filter(|kv| matches!(kv.value.as_ref(), Expr::Lit(_) | Expr::Tpl(_)))
        .map(|kv| span_key(kv.value.span()));
    let keys = key_value_props().map(|kv| span_key(kv.key.span()));
    values.chain(keys).collect()
}

/// Slot-aware minimal cover for a single-target object inside a `var` group: a
/// greedy key-set set-cover that, at each step, adds the `ranked` anchor that best
/// steers the selector toward resolving to the target binding's own declarator
/// slot, until it proves unique. The chunk-wide read-off resolves by distinct
/// body index and so cannot see two sibling declarators of the *same* statement;
/// this scores by whether the **target binding** is the one the selector
/// resolves, then by the match count.
///
/// The matcher reports one alignment per body (the leftmost declarator the holed
/// pattern fits), so a key shared with an *earlier* sibling slot
/// (`blue: "#00f"`, also in `firstPalette`) resolves there, not to the target —
/// hence the score's first key is "did the target slot resolve at all", which a
/// key/value unique to the target slot (`accent`) flips true. Keeps adding the
/// best anchor until the matcher's uniqueness proof passes, or the target's own
/// anchors are exhausted (then `None`, and the caller keeps its keep-shallow
/// form).
fn cover_object_slot(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    ranked: &[AnchorSpan],
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
) -> Result<Option<SpecializedSelector>> {
    let targets = std::slice::from_ref(target);
    let mut kept: BTreeSet<AnchorSpan> = BTreeSet::new();
    while prove_synthesized_selector(index, decl, targets, &render_with(&kept)?).is_err() {
        // Score a trial by `(target slot not yet resolved, total matches)`; a
        // smaller score is better, so an anchor that makes the target binding the
        // resolved one and rules out the most competitors wins.
        let mut best: Option<((bool, usize), AnchorSpan)> = None;
        for &anchor in ranked.iter().take(MAX_MINIMIZER_ANCHORS) {
            if kept.contains(&anchor) {
                continue;
            }
            let mut trial = kept.clone();
            trial.insert(anchor);
            let matches =
                solve_single_member_selector(index, &target.export_name, &render_with(&trial)?)?;
            let target_unresolved = !matches.iter().any(|m| {
                m.body_idx == decl.body_idx && m.binding.binding_name == target.runtime_binding
            });
            let score = (target_unresolved, matches.len());
            if best.is_none_or(|(best_score, _)| score < best_score) {
                best = Some((score, anchor));
            }
        }
        // No remaining anchor to add: the target's own keys/values cannot single
        // out its slot. Defer to the caller.
        let Some((_, anchor)) = best else {
            return Ok(None);
        };
        kept.insert(anchor);
    }
    finish_minimized_selector(index, decl, target, render_with(&kept)?)
}

/// Read off a minimal selector for a single-target `var`/`let`/`const` whose
/// target declarator value is an object literal (W3 + key-set minimization).
///
/// Handles the single-target object whether it stands alone or sits inside a
/// multi-declarator group (one target slot, `DECLARATORS_*` holes for the rest).
/// Two passes, both rendering through one slot-aware `render_with` that holes the
/// object with [`hole_object_padded`] (interleaved `ANYTHING`) so the kept key
/// subset survives key reorder:
///
///   1. **Read-off.** The chunk-wide shape index ranks the statement's own
///      features by selective × stable, so a globally-rare discriminating
///      key/value wins. Its kept spans are restricted to the target declarator (a
///      group's anchor set may name a key carried only by a *sibling* declarator
///      this selector holes away), and the matcher proves the restricted pin.
///   2. **Cover fallback.** A matcher-driven minimal cover over the target
///      object's own keys (and direct literal values) — the key-set analogue of
///      greedy set-cover: anchor the rarest discriminating keys, hole the
///      common ones. This is what singles out a target object inside a
///      multi-declarator group, where the chunk-wide read-off cannot see the
///      per-slot key sets.
///
/// Returns `None` — so the caller falls back to the keep-shallow group path —
/// when the target is not a single object declarator or neither pass resolves it.
pub(crate) fn try_object_read_off(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    target_slots: &BTreeSet<usize>,
) -> Result<Option<SpecializedSelector>> {
    Ok(
        try_object_read_off_candidates(index, var, decl, targets, target_slots, 1)?
            .into_iter()
            .next(),
    )
}

/// Up to `limit` ranked read-off selectors for a single-target object declarator —
/// the `synthesize-selectors --candidates N` menu. `limit == 1` reproduces
/// [`try_object_read_off`] exactly (minimal anchor set, else the slot key-set
/// cover). For `limit > 1`, once a primary read-off exists the menu is extended
/// with the slot's individually-discriminating value anchors; an object that has
/// no minimal/cover read-off offers no menu (matching the single-pick `None`).
pub(crate) fn try_object_read_off_candidates(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    target_slots: &BTreeSet<usize>,
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    // Only the single-target case (one binding). A multi-target group needs the
    // tuple-resolving binding-group path; this owns the single object slot.
    let [target] = targets else {
        return Ok(Vec::new());
    };
    if target_slots.len() != 1 {
        return Ok(Vec::new());
    }
    let target_slot = *target_slots.iter().next().expect("one target slot");
    let declarator = &var.decls[target_slot];
    let Some(Expr::Object(object)) = declarator.init.as_deref() else {
        return Ok(Vec::new());
    };
    let target_decl_span = declarator.span();

    // Slot-aware render via the shared var-slot renderer: `DECLARATORS_*` holes
    // for the non-target declarators (none when the target stands alone), the
    // target's object holed to its kept keys padded + interleaved with
    // `ANYTHING` (`hole_var_init_padded`'s object arm). The object path never
    // upgrades to a regex anchor, so the regex map is always empty.
    let only_target = BTreeSet::from([target_slot]);
    let no_regex: BTreeMap<AnchorSpan, String> = BTreeMap::new();
    let export_for =
        |name: &str| (name == target.runtime_binding).then(|| target.export_name.clone());
    let render_with = |kept: &BTreeSet<AnchorSpan>| -> Result<String> {
        render_var_slots(
            var,
            &only_target,
            &export_for,
            kept,
            &no_regex,
            &hole_var_init_padded,
        )
    };

    let item = index
        .parsed
        .module
        .body
        .get(decl.body_idx)
        .context("read-off body index no longer in module")?;
    let kept_in_slot = |anchor_set: &shape_index::AnchorSet| -> BTreeSet<AnchorSpan> {
        kept_spans_for_anchor_set(item, anchor_set)
            .into_iter()
            .filter(|span| node_holds_anchor(target_decl_span, *span))
            .collect()
    };
    let collect = |selector: SpecializedSelector, out: &mut Vec<SpecializedSelector>| -> bool {
        if !out
            .iter()
            .any(|kept| kept.match_source == selector.match_source)
        {
            out.push(selector);
        }
        out.len() >= limit
    };
    let mut out: Vec<SpecializedSelector> = Vec::new();

    // Single pick (limit == 1): the minimal anchor set, else the slot key-set cover.
    if let Some(anchor_set) = index.shape_index.minimal_anchor_set(decl.body_idx) {
        let kept = kept_in_slot(&anchor_set);
        if !kept.is_empty()
            && let Some(selector) =
                finish_minimized_selector(index, decl, target, render_with(&kept)?)?
            && collect(selector, &mut out)
        {
            return Ok(out);
        }
    }
    if let Some(selector) = cover_object_slot(
        index,
        decl,
        target,
        &object_anchor_ranking(object),
        &render_with,
    )? && collect(selector, &mut out)
    {
        return Ok(out);
    }

    // No minimal/cover read-off ⇒ no menu (the single pick is `None`).
    if out.is_empty() {
        return Ok(out);
    }

    // Menu extras: the slot's individually-discriminating value anchors.
    for anchor_set in index
        .shape_index
        .unique_value_anchor_candidates(decl.body_idx)
    {
        let kept = kept_in_slot(&anchor_set);
        if kept.is_empty() {
            continue;
        }
        if let Some(selector) = finish_minimized_selector(index, decl, target, render_with(&kept)?)?
            && collect(selector, &mut out)
        {
            return Ok(out);
        }
    }
    Ok(out)
}
