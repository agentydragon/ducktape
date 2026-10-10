//! Object-valued `var` selector minimization: chunk-wide read-off then a
//! slot-aware key-set cover (`ANYTHING` run holes around the discriminating keys).

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_set;
use swc_common::Spanned;
use swc_ecma_ast::*;

use super::{extend_anchor_cover, finish_minimized_selector, render_var_slots};
use crate::render::{AnchorSpan, node_holds_anchor, span_key};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    match_single_member_selector, prove_synthesized_selector,
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
/// anchors are exhausted (then `None`, and the caller tries the tuple/context
/// form).
fn cover_object_slot(
    index: &ChunkSelectorIndex<'_>,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    ranked: &[AnchorSpan],
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
) -> Result<Option<SpecializedSelector>> {
    let targets = std::slice::from_ref(target);
    let kept = extend_anchor_cover(
        BTreeSet::new(),
        ranked,
        |kept| Ok(prove_synthesized_selector(index, decl, targets, &render_with(kept)?).is_ok()),
        |trial| {
            let source = render_with(trial)?;
            // A read-off candidate may put a source identifier named like a
            // run hole outside a list. Reject that candidate and leave the
            // exact-declaration fallback reachable.
            let Ok(matches) = match_single_member_selector(index, &target.selector_local, &source)
            else {
                return Ok((true, usize::MAX));
            };
            // Object ranking distinguishes the declaration as well as the binding.
            let target_unresolved = !matches.iter().any(|m| {
                m.body_idx == decl.body_idx && m.binding.binding_name == target.runtime_binding
            });
            Ok((target_unresolved, matches.len()))
        },
    )?;
    finish_minimized_selector(index, decl, target, render_with(&kept)?)
}

/// Up to `limit` proven single-target object selectors, using the minimal
/// chunk-wide anchor set before trying alternative value anchors and slot cover.
/// Unresolved targets fall through to the shared tuple/context read-off.
pub(crate) fn try_object_read_off_candidates(
    index: &ChunkSelectorIndex<'_>,
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
        |name: &str| (name == target.runtime_binding).then(|| target.selector_local.clone());
    let render_with = |kept: &BTreeSet<AnchorSpan>| -> Result<String> {
        render_var_slots(var, &only_target, &export_for, kept, &no_regex)
    };

    let item = index
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
