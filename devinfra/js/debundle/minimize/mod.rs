//! Selector minimizer (read-off based), split by form.
//!
//! A selector is the target rendered with a *retention set*: the byte spans of
//! the concrete tokens (literals, member/property names, callees, object keys)
//! the selector pins. A node renders concretely iff a kept span lies inside it;
//! every other position is holed — `ANYTHING` for a bare expression, and the
//! run holes `STMT_LIST` / `OBJECT_PROPS` / `CLASS_REST` for dropped statement /
//! object-property / class-member runs.
//!
//! Single targets (function, class, object, and non-object var) read their
//! minimal anchor set off the chunk-wide shape index (`render_via_read_off` /
//! `try_object_read_off` / `try_var_read_off`): the index ranks each candidate
//! feature by selective × stable, so the chosen anchors are sparse and
//! rebuild-robust, and the production matcher proves the rendered selector
//! resolves uniquely (gate 1). A target the read-off cannot single out returns
//! `None` and is reported as debt — never a full-AST pin.
//!
//! Multi-target var binding groups read off per slot (`try_var_group_read_off`):
//! each target declarator slot reads its minimal anchor off the shape index
//! (restricted to the slot) plus a slot-aware greedy (`slot_minimal_anchors`),
//! the per-slot kept spans union, and the binding-group matcher proves the tuple.
//! A keep-shallow path (`minimize_var_group_selector`) remains as the fallback for
//! groups whose per-slot single-binding view cannot single a slot out (a value
//! shared across sibling statements that resolves only as a tuple): it keeps each
//! slot's direct shallow literals, escalating to structural/deeper anchors only
//! until the group resolves, proven through the binding-group matcher as a
//! resolves-uniquely oracle; it may over-pin rather than run an exact-minimum
//! cover (near-minimal is the accepted target).

mod class;
mod function;
mod group;
mod object;
mod var;

pub(crate) use class::minimize_class_selector;
pub(crate) use function::minimize_function_selector;
pub(crate) use group::minimize_var_group_selector;

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_set;
use swc_ecma_ast::*;
use swc_ecma_visit::VisitMutWith;

use crate::regex_anchor::RegexAnchorSubstitution;
use crate::render::{
    AnchorSpan, declarator_hole, emit_selector, hole_expr, hole_object_padded, holes_present,
    named_pat,
};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    declarator_hole_name, matched_body_indices, prove_synthesized_selector, single_ident_pat_name,
};

/// Render a single-target selector via the read-off API.
///
/// Reads the minimal [`AnchorSet`] off the shape index, maps its value anchors
/// to kept byte spans over the target item, and renders + proves through the
/// supplied `render_with` (the same prune + codegen — no second serializer).
///
/// **Robustness-anchor policy.** A holed-down *value* anchor is preferred over
/// the bare structural scaffold even when the scaffold alone would resolve. A
/// scaffold that pins only declaration kind + arity (`class X { CLASS_REST }`,
/// `function f(ANYTHING) { STMT_LIST }`, `const X = ANYTHING`) is a degenerate
/// selector: it pins nothing rebuild-stable and matches any same-shape sibling a
/// rebuild adds. So the read-off keeps its chosen value anchor when it has one,
/// and falls back to the bare scaffold only when the target has no renderable
/// value anchor — `minimal_anchor_set` chose a purely structural skeleton (empty
/// kept spans) because nothing but shape discriminates — or the value anchor
/// fails to prove. Returns `None` when neither route singles the target out (a
/// genuine alpha-duplicate); the caller reports it as debt, never a full-AST pin.
fn render_via_read_off(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
) -> Result<Option<SpecializedSelector>> {
    let item = index
        .parsed
        .module
        .body
        .get(decl.body_idx)
        .context("read-off body index no longer in module")?;

    // Primary read-off: the single minimal anchor set. When its value anchor
    // renders to a holed selector that proves uniquely, keep it.
    if let Some(anchor_set) = index.shape_index.minimal_anchor_set(decl.body_idx) {
        let kept = kept_spans_for_anchor_set(item, &anchor_set);
        if !kept.is_empty()
            && let Some(selector) =
                finish_minimized_selector(index, decl, target, render_with(&kept)?)?
        {
            return Ok(Some(selector));
        }
    }

    // Robustness-anchor fallback: the minimal anchor's *value* may not survive
    // holing — a deep literal whose only occurrence sits inside a large statement
    // the holer keeps verbatim leaves raw subtrees the matcher rejects, and a
    // structural-only minimal set keeps nothing at all. Rather than collapse to
    // the degenerate scaffold, walk the target's individually-discriminating value
    // anchors best-first and emit the first whose holed selector proves uniquely.
    // This is what drills a whole-body class/component down to one anchored member
    // (e.g. `applyChange(ANYTHING) { STMT_LIST; ANYTHING.set("running"); }`,
    // anchoring on the `"running"` literal) instead of `class X { CLASS_REST }`.
    for anchor_set in index
        .shape_index
        .unique_value_anchor_candidates(decl.body_idx)
    {
        let kept = kept_spans_for_anchor_set(item, &anchor_set);
        if !kept.is_empty()
            && let Some(selector) =
                finish_minimized_selector(index, decl, target, render_with(&kept)?)?
        {
            return Ok(Some(selector));
        }
    }

    // Multi-feature interior cover (#2289): no single value anchor singled the
    // target out (every value anchor still shares same-shape siblings), but a
    // *combination* of value anchors may — each individually shared with a
    // different sibling. Greedy-cover over value-bearing features only, so each
    // chosen anchor still renders a kept span, and emit the holed selector if it
    // proves uniquely. This drills bodies whose discriminator is a *set* of deep
    // leaves rather than one.
    if let Some(anchor_set) = index.shape_index.unique_value_anchor_cover(decl.body_idx) {
        let kept = kept_spans_for_anchor_set(item, &anchor_set);
        if !kept.is_empty()
            && let Some(selector) =
                finish_minimized_selector(index, decl, target, render_with(&kept)?)?
        {
            return Ok(Some(selector));
        }
    }

    // Last resort: the bare structural scaffold, used only when it resolves
    // uniquely (a purely structural discriminator — arity/shape — with no value
    // anchor to keep). The scaffold is degenerate for `var`, so the var path skips
    // this entirely and returns `None` for the keep-shallow path to handle.
    let empty = BTreeSet::new();
    if matched_body_indices(index, &target.export_name, &render_with(&empty)?)?
        == BTreeSet::from([decl.body_idx])
    {
        return finish_minimized_selector(index, decl, target, render_with(&empty)?);
    }
    Ok(None)
}

fn finish_minimized_selector(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    source: String,
) -> Result<Option<SpecializedSelector>> {
    let targets = std::slice::from_ref(target);
    if prove_synthesized_selector(index, decl, targets, &source).is_err() {
        return Ok(None);
    }
    let rewritten_holes = holes_present(&source);
    Ok(Some(SpecializedSelector {
        match_source: source,
        rewritten_holes,
    }))
}

/// Per-slot initializer holing for the read-off var paths: an object init holes
/// to its key-set form ([`hole_object_padded`], interleaved `OBJECT_PROPS`),
/// every other init via [`hole_expr`]. The keep-shallow group path passes plain
/// [`hole_expr`] instead (objects hole via [`hole_object`], not padded).
fn hole_var_init_padded(init: &Expr, kept: &BTreeSet<AnchorSpan>) -> Expr {
    match init {
        Expr::Object(object) => Expr::Object(hole_object_padded(object, kept)),
        other => hole_expr(other, kept),
    }
}

/// Render a `var` binding-group selector: keep the target declarator slots (each
/// renamed to its export via `export_for`, init holed by `hole_init` then the
/// optional `regex_anchors` `STR_LITERAL_MATCHING_RE` post-pass), with
/// `DECLARATORS_*` holes absorbing the runs of non-target slots. The single-target
/// var and object read-offs are the N=1 case (one target slot, no `DECLARATORS_*`
/// gaps).
///
/// The per-slot initializer holing is the one axis the call sites differ on, so
/// it is a parameter: the read-off paths pass [`hole_var_init_padded`] (object →
/// padded key-set holing); the keep-shallow group path passes [`hole_expr`].
/// Factored from the near-identical `render_with` closures the object, var, and
/// var-group minimizers each used to build inline.
fn render_var_slots(
    var: &VarDecl,
    target_slots: &BTreeSet<usize>,
    export_for: &impl Fn(&str) -> Option<String>,
    kept: &BTreeSet<AnchorSpan>,
    regex_anchors: &BTreeMap<AnchorSpan, String>,
    hole_init: &impl Fn(&Expr, &BTreeSet<AnchorSpan>) -> Expr,
) -> Result<String> {
    let mut decls: Vec<VarDeclarator> = Vec::new();
    let mut skipped_run = false;
    let mut target_seen = 0usize;
    for (idx, declarator) in var.decls.iter().enumerate() {
        if !target_slots.contains(&idx) {
            skipped_run = true;
            continue;
        }
        if skipped_run {
            decls.push(declarator_hole(declarator_hole_name(
                target_seen,
                target_slots.len(),
            )));
            skipped_run = false;
        }
        let name = single_ident_pat_name(&declarator.name)
            .expect("target declarator is a plain identifier");
        let mut holed = declarator.clone();
        holed.name = named_pat(&export_for(name).expect("target declarator has an export"));
        holed.init = declarator.init.as_ref().map(|init| {
            let mut holed_init = hole_init(init, kept);
            if !regex_anchors.is_empty() {
                holed_init.visit_mut_with(&mut RegexAnchorSubstitution {
                    patterns: regex_anchors,
                });
            }
            Box::new(holed_init)
        });
        decls.push(holed);
        target_seen += 1;
    }
    if skipped_run {
        decls.push(declarator_hole(declarator_hole_name(
            target_seen,
            target_slots.len(),
        )));
    }
    let mut holed_var = var.clone();
    holed_var.declare = false;
    holed_var.decls = decls;
    emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(holed_var)))))
}
