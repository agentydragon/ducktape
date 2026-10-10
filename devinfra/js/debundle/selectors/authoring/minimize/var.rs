//! Single-target non-object `var` selector minimization (read-off).

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_sets;
use source_match_holes::AST_CONTAINS_STRING_PREDICATE;
use swc_common::{DUMMY_SP, Spanned, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

use super::{anchor_quality, render_var_slots};
use crate::regex_anchor::{accepted_regex_anchors, collect_regex_anchor_candidates};
use crate::render::{
    AnchorSpan, declarator_hole, emit_selector, holes_present, ident_node, named_pat,
    node_holds_anchor, span_key,
};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    declarator_hole_name, prove_synthesized_selector,
};

/// Find a single retained string literal underneath enough call wrappers that
/// an ordinary path selector would pin incidental nesting. This is an exact
/// AST literal lookup, not substring or source-text matching.
fn deep_string_anchor(init: &Expr, kept: &BTreeSet<AnchorSpan>) -> Option<String> {
    struct Find<'a> {
        kept: &'a BTreeSet<AnchorSpan>,
        call_depth: usize,
        found: Vec<(String, usize)>,
    }
    impl Visit for Find<'_> {
        fn visit_call_expr(&mut self, call: &CallExpr) {
            self.call_depth += 1;
            call.visit_children_with(self);
            self.call_depth -= 1;
        }

        fn visit_expr(&mut self, expr: &Expr) {
            if let Expr::Lit(Lit::Str(value)) = expr
                && self.kept.contains(&span_key(value.span))
            {
                self.found
                    .push((value.value.to_string_lossy().into_owned(), self.call_depth));
            } else {
                expr.visit_children_with(self);
            }
        }
    }
    let mut find = Find {
        kept,
        call_depth: 0,
        found: Vec::new(),
    };
    init.visit_with(&mut find);
    let [(value, depth)] = find.found.as_slice() else {
        return None;
    };
    (*depth >= 6).then(|| value.clone())
}

fn descendant_string_selector(
    var: &VarDecl,
    target_slot: usize,
    target: &SynthesizedTargetBinding,
    value: &str,
) -> Result<String> {
    let mut selected = var.decls[target_slot].clone();
    selected.name = named_pat(&target.selector_local);
    selected.init = Some(Box::new(Expr::Call(CallExpr {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        callee: Callee::Expr(Box::new(Expr::Ident(ident_node(
            AST_CONTAINS_STRING_PREDICATE,
        )))),
        args: vec![ExprOrSpread {
            spread: None,
            expr: Box::new(Expr::Lit(Lit::Str(Str {
                span: DUMMY_SP,
                value: value.into(),
                raw: None,
            }))),
        }],
        type_args: None,
    })));
    let mut decls = Vec::new();
    if target_slot > 0 {
        decls.push(declarator_hole(declarator_hole_name()));
    }
    decls.push(selected);
    if target_slot + 1 < var.decls.len() {
        decls.push(declarator_hole(declarator_hole_name()));
    }
    let mut holed_var = var.clone();
    holed_var.declare = false;
    holed_var.decls = decls;
    emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(holed_var)))))
}

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
        |name: &str| (name == target.runtime_binding).then(|| target.selector_local.clone());

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
    let anchor_sets: Vec<_> = index
        .shape_index
        .minimal_anchor_set(decl.body_idx)
        .into_iter()
        .chain(
            index
                .shape_index
                .unique_value_anchor_candidates(decl.body_idx),
        )
        .chain(index.shape_index.unique_value_anchor_cover(decl.body_idx))
        .collect();
    let mut candidates: Vec<BTreeSet<AnchorSpan>> = kept_spans_for_anchor_sets(item, &anchor_sets)
        .into_iter()
        .map(|spans| {
            spans
                .into_iter()
                .filter(|span| node_holds_anchor(target_decl_span, *span))
                .collect()
        })
        .collect();
    candidates.sort_by_key(|kept| (anchor_quality(index, kept), kept.len()));
    let mut out: Vec<SpecializedSelector> = Vec::new();
    for kept in candidates {
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
        let mut source = render_with(&kept, &regex_anchors)?;
        if regex_anchors.is_empty()
            && let Some(value) = deep_string_anchor(init, &kept)
        {
            let compact = descendant_string_selector(var, target_slot, target, &value)?;
            if prove_synthesized_selector(index, decl, targets, &compact).is_ok() {
                source = compact;
            }
        }
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
