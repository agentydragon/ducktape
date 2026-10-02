//! Single-target non-object `var` selector minimization (read-off).

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_set;
use swc_common::Spanned;
use swc_ecma_ast::*;

use super::render_var_slots;
use crate::regex_anchor::{accepted_regex_anchors, collect_regex_anchor_candidates};
use crate::render::{AnchorSpan, holes_present, node_holds_anchor};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    prove_synthesized_selector,
};

/// Up to `limit` proven single-target var selectors, using the minimal
/// chunk-wide anchor set before trying alternative value anchors and slot cover.
/// Unresolved targets fall through to the shared tuple/context read-off.
pub(crate) fn try_var_read_off_candidates(
    index: &ChunkSelectorIndex<'_>,
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
    // Objects are the object read-off's domain (padded `ANYTHING` holes + the
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
    // shared class-member or expression holing.
    let render_with = |kept: &BTreeSet<AnchorSpan>,
                       regex_anchors: &BTreeMap<AnchorSpan, String>|
     -> Result<String> {
        render_var_slots(var, &only_target, &export_for, kept, regex_anchors)
    };

    let item = index
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
        // Apply the shared regex-literal upgrade: among kept string
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
        let rewritten_holes = holes_present(&source)?;
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
