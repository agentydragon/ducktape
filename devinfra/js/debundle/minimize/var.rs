//! Single-target non-object `var` selector minimization (read-off).

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_set;
use swc_common::Spanned;
use swc_ecma_ast::*;

use super::{hole_var_init_padded, render_var_slots};
use crate::regex_anchor::{accepted_regex_anchors, collect_regex_anchor_candidates};
use crate::render::{AnchorSpan, holes_present, node_holds_anchor};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    prove_synthesized_selector,
};

/// Read off a minimal selector for a single-target `var`/`let`/`const` whose
/// target declarator value is **not** an object literal (objects route through
/// [`try_object_read_off`]). Mirrors `read_off_candidates` (function/class) but
/// slot-aware: holes non-target declarators to `DECLARATORS_*`, holes the target
/// init with [`hole_expr`] around the read-off anchors, and restricts the kept
/// spans to the target declarator so a group's chunk-wide anchor set never pins a
/// span carried only by a sibling this selector holes away.
///
/// Deviation from the function/class read-off: there is no empty-kept structural
/// fast path. A var's maximally-holed scaffold is the *degenerate* `const X =
/// ANYTHING`, which pins nothing meaningful and would match any wrapped-const a
/// rebuild adds (criterion 2). A var selector must keep a discriminating value
/// anchor, so an empty anchor set yields `None` and the caller falls back to the
/// keep-shallow group path.
///
/// Returns `None` — caller falls back — when the target is an object declarator,
/// the read-off has no anchor set, every anchor lies outside the target slot, or
/// the rendered selector fails the matcher gate.
pub(crate) fn try_var_read_off(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    target_slot: usize,
) -> Result<Option<SpecializedSelector>> {
    Ok(
        try_var_read_off_candidates(index, var, decl, target, target_slot, 1)?
            .into_iter()
            .next(),
    )
}

/// Up to `limit` ranked read-off selectors for a single-target non-object var, in
/// the same priority order [`try_var_read_off`] picks from (minimal anchor →
/// individually-discriminating value anchors → multi-feature cover), each proven
/// uniquely and deduped by source. `limit == 1` reproduces [`try_var_read_off`]
/// exactly; `limit > 1` powers the `synthesize-selectors --candidates N` menu.
pub(crate) fn try_var_read_off_candidates(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    target_slot: usize,
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    let declarator = &var.decls[target_slot];
    let Some(init) = declarator.init.as_deref() else {
        return Ok(Vec::new());
    };
    // Objects are `try_object_read_off`'s domain (padded `ANYTHING` holes + the
    // slot-aware key-set cover); this owns every other initializer shape.
    if matches!(init, Expr::Object(_)) {
        return Ok(Vec::new());
    }
    let target_decl_span = declarator.span();
    let targets = std::slice::from_ref(target);
    let only_target = BTreeSet::from([target_slot]);
    let export_for =
        |name: &str| (name == target.runtime_binding).then(|| target.export_name.clone());

    // Shared var-slot render: `DECLARATORS_*` holes for the non-target declarators
    // (none when the target stands alone), the target's non-object init holed via
    // `hole_var_init_padded` (the `other` arm, i.e. `hole_expr`).
    let render_with = |kept: &BTreeSet<AnchorSpan>,
                       regex_anchors: &BTreeMap<AnchorSpan, String>|
     -> Result<String> {
        render_var_slots(
            var,
            &only_target,
            &export_for,
            kept,
            regex_anchors,
            &hole_var_init_padded,
        )
    };

    let item = index
        .parsed
        .module
        .body
        .get(decl.body_idx)
        .context("read-off body index no longer in module")?;
    let no_regex = BTreeMap::new();

    // Try the single minimal anchor set first, then — robustness-anchor fallback —
    // the target's individually-discriminating value anchors best-first. A deep
    // value anchor whose minimal pick does not prove (it lands in a statement the
    // holer keeps verbatim, leaving raw subtrees the matcher rejects) is recovered
    // here instead of collapsing to the degenerate `const X = ANYTHING` scaffold;
    // this drills a whole-body component initializer down to one anchored leaf.
    // Finally the multi-feature interior cover (#2289): when no single value anchor
    // is unique, a greedy cover over value-bearing features keeps the *set* of deep
    // leaves that jointly single the slot out.
    let anchor_sets = index
        .shape_index
        .minimal_anchor_set(decl.body_idx)
        .into_iter()
        .chain(
            index
                .shape_index
                .unique_value_anchor_candidates(decl.body_idx),
        )
        .chain(index.shape_index.unique_value_anchor_cover(decl.body_idx));
    let mut out: Vec<SpecializedSelector> = Vec::new();
    for anchor_set in anchor_sets {
        let kept: BTreeSet<AnchorSpan> = kept_spans_for_anchor_set(item, &anchor_set)
            .into_iter()
            .filter(|span| node_holds_anchor(target_decl_span, *span))
            .collect();
        if kept.is_empty() {
            continue;
        }
        // Prove the read-off resolves uniquely before offering the regex upgrade.
        if prove_synthesized_selector(index, decl, targets, &render_with(&kept, &no_regex)?)
            .is_err()
        {
            continue;
        }
        // Preserve the keep-shallow path's regex-literal upgrade: among kept string
        // literals, swap a volatile-suffix value for a `STR_LITERAL_MATCHING_RE`
        // anchor when the upgraded selector still resolves uniquely.
        let regex_anchors = accepted_regex_anchors(
            index,
            decl,
            targets,
            &collect_regex_anchor_candidates(init),
            &kept,
            &render_with,
        )?;
        let source = render_with(&kept, &regex_anchors)?;
        if out.iter().any(|kept| kept.match_source == source) {
            continue;
        }
        let rewritten_holes = holes_present(&source);
        out.push(SpecializedSelector {
            match_source: source,
            rewritten_holes,
        });
        if out.len() >= limit {
            break;
        }
    }
    Ok(out)
}
