//! Selector minimizer (read-off based), split by form.
//!
//! A selector is the target rendered with a *retention set*: the byte spans of
//! the concrete tokens (literals, member/property names, callees, object keys)
//! the selector pins. A node renders concretely iff a kept span lies inside it;
//! every other position is holed — `ANYTHING` for a bare expression and for the
//! object-property / class-member run holes (emitted as `ANYTHING`, the
//! run-absorber form their detector predicates fall back to), and the
//! load-bearing run holes `STMT_LIST` / `ARGS` / `CASE_REST` for dropped
//! statement / argument / switch-case runs (where `ANYTHING` would collapse to
//! an arity-exact single-node hole).
//!
//! Single targets (function, class, object, and non-object var) read their
//! minimal anchor set off the chunk-wide shape index (`read_off_candidates` /
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

pub(crate) use class::minimize_class_selector_candidates;
pub(crate) use function::minimize_function_selector_candidates;
pub(crate) use group::{minimize_var_group_selector, minimize_var_group_selector_candidates};

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_set;
use swc_ecma_ast::*;
use swc_ecma_visit::VisitMutWith;

use crate::regex_anchor::RegexAnchorSubstitution;
use crate::render::{
    AnchorSpan, declarator_hole, emit_selector, hole_expr, hole_object_padded, hole_stmt,
    holes_present, named_pat,
};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    declarator_hole_name, matched_body_indices, prove_synthesized_selector, single_ident_pat_name,
};

/// Collect up to `limit` read-off selectors for the target, best-first — minimal
/// anchor set → individually-discriminating value anchors → multi-feature value
/// cover → bare scaffold → enclosing-context neighbor — each rendered + proven
/// uniquely through the supplied `render_with` (the same prune + codegen — no
/// second serializer) and deduped by source. `limit == 1` is the single pick
/// (stops at the first proving selector — the form dispatchers' single-selector
/// path); `limit > 1` powers `synthesize-selectors --candidates N`, the ranked
/// menu. Returns fewer than `limit` (or empty) when the target has no more
/// proving anchors.
///
/// **Robustness-anchor policy.** A holed-down *value* anchor is preferred over
/// the bare structural scaffold even when the scaffold alone would resolve. A
/// scaffold that pins only declaration kind + arity (`class X { ANYTHING; }`,
/// `function f(ANYTHING) { STMT_LIST }`, `const X = ANYTHING`) is a degenerate
/// selector: it pins nothing rebuild-stable and matches any same-shape sibling a
/// rebuild adds. So the read-off keeps its chosen value anchor when it has one,
/// and falls back to the bare scaffold only when the target has no renderable
/// value anchor — `minimal_anchor_set` chose a purely structural skeleton (empty
/// kept spans) because nothing but shape discriminates — or the value anchor
/// fails to prove. Yields nothing when neither route singles the target out (a
/// genuine alpha-duplicate); the caller reports it as debt, never a full-AST pin.
fn read_off_candidates(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    let item = index
        .parsed
        .module
        .body
        .get(decl.body_idx)
        .context("read-off body index no longer in module")?;

    // Push a proven candidate unless its source duplicates one already kept;
    // return true once `limit` is reached so the caller stops walking.
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

    // Primary read-off: the minimal anchor set. Keep it when its holed selector
    // proves uniquely — except the degenerate case of a single (OPT=1) feature that
    // occurs *several times* in the target (e.g. the literal `0`, which also hides
    // inside `void 0`): pinning it keeps every occurrence and drags in unrelated
    // members, so a multi-occurrence single anchor defers to the value-anchor walk
    // below, which prefers a single-occurrence leaf. A genuine multi-feature cover
    // (`!opt_one`) legitimately keeps several spans and is taken here.
    if let Some(anchor_set) = index.shape_index.minimal_anchor_set(decl.body_idx) {
        let kept = kept_spans_for_anchor_set(item, &anchor_set);
        if !kept.is_empty()
            && (!anchor_set.opt_one || kept.len() == 1)
            && let Some(selector) =
                finish_minimized_selector(index, decl, target, render_with(&kept)?)?
            && collect(selector, &mut out)
        {
            return Ok(out);
        }
    }

    // Robustness-anchor fallback: walk the target's individually-discriminating
    // value anchors, preferring those that pin the *fewest* source spans (a
    // single-occurrence leaf over a literal repeated across the body), then by the
    // shape index's rank (stable sort keeps the rank order within a span count).
    // Emit the first whose holed selector proves uniquely. The minimal anchor's
    // value may not survive holing (a deep literal whose only home is a large
    // statement the holer keeps verbatim leaves raw subtrees the matcher rejects),
    // so this is also where a whole-body class/component drills down to one anchored
    // member (e.g. `applyChange(ANYTHING) { STMT_LIST; ANYTHING.set("running"); }`)
    // instead of `class X { ANYTHING; }`.
    let mut value_kept: Vec<BTreeSet<AnchorSpan>> = index
        .shape_index
        .unique_value_anchor_candidates(decl.body_idx)
        .into_iter()
        .map(|anchor_set| kept_spans_for_anchor_set(item, &anchor_set))
        .filter(|kept| !kept.is_empty())
        .collect();
    value_kept.sort_by_key(BTreeSet::len);
    for kept in value_kept {
        if let Some(selector) = finish_minimized_selector(index, decl, target, render_with(&kept)?)?
            && collect(selector, &mut out)
        {
            return Ok(out);
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
            && collect(selector, &mut out)
        {
            return Ok(out);
        }
    }

    // Bare structural scaffold, used only when it resolves uniquely (a purely
    // structural discriminator — arity/shape — with no value anchor to keep). The
    // scaffold is degenerate for `var`, so the var path skips this entirely and
    // returns `None` for the keep-shallow path to handle. When the scaffold
    // uniquely matches the read-off is done — it never falls through to neighbor
    // context (mirroring the original early return), so stop here regardless of
    // `limit`.
    let empty = BTreeSet::new();
    let scaffold = render_with(&empty)?;
    if matched_body_indices(index, &target.export_name, &scaffold)?
        == BTreeSet::from([decl.body_idx])
    {
        if let Some(selector) = finish_minimized_selector(index, decl, target, scaffold)? {
            collect(selector, &mut out);
        }
        return Ok(out);
    }

    // Enclosing-context anchoring (#2315): the target's own value anchors and the
    // bare scaffold all fail to single it out among same-shape siblings. Fall to
    // its stable neighbors — a 2-statement window pinning an adjacent declaration's
    // unique anchor + the target scaffold. `None` here leaves the target as residual
    // debt (never a full-AST pin).
    if let Some(selector) = render_via_neighbor_context(index, decl, target, &scaffold)? {
        collect(selector, &mut out);
    }
    Ok(out)
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

/// Enclosing-context anchoring (#2315). The last resort before residual debt:
/// when a target's own value anchors cannot separate it from same-shape siblings
/// — alpha-only constructs (`new ()` with no stable args) or near-duplicate
/// emitted helpers (the `__decorate` family) — pin a **stable adjacent
/// declaration** as context. Emit a 2-statement window selector pairing the
/// target's holed `target_scaffold` with an immediate neighbor holed to a
/// globally-unique value anchor, ordered by their chunk adjacency, with
/// `target_binding` picking the target out of the window. The matcher's
/// contiguous member-window path resolves it and the prove-gate confirms
/// uniqueness.
///
/// Returns `None` when no adjacent declaration carries a unique value anchor, or
/// none of the windows prove — the target then stays name-pinned as residual
/// debt, never a full-AST pin.
fn render_via_neighbor_context(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    target_scaffold: &str,
) -> Result<Option<SpecializedSelector>> {
    for candidate in index
        .shape_index
        .context_neighbor_anchor_candidates(decl.body_idx)
    {
        let Some(neighbor_item) = index.parsed.module.body.get(candidate.neighbor_body_idx) else {
            continue;
        };
        let neighbor_kept = kept_spans_for_anchor_set(neighbor_item, &candidate.anchor_set);
        if neighbor_kept.is_empty() {
            continue;
        }
        let Some(neighbor_source) = render_context_neighbor(neighbor_item, &neighbor_kept)? else {
            continue;
        };
        // Compose the window in chunk order so the matcher's contiguous member
        // window aligns the neighbor and target to their real adjacency.
        let (first, second) = if candidate.neighbor_body_idx < decl.body_idx {
            (neighbor_source.as_str(), target_scaffold)
        } else {
            (target_scaffold, neighbor_source.as_str())
        };
        let window = format!("{}\n{}", first.trim_end(), second.trim_end());
        if let Some(selector) = finish_minimized_selector(index, decl, target, window)? {
            return Ok(Some(selector));
        }
    }
    Ok(None)
}

/// Render an adjacent declaration as holed selector *context*: keep only the
/// spans in `kept` (the neighbor's unique value anchor) and hole everything else,
/// so the neighbor contributes a stable pin without dumping its full AST. Only
/// plain statements are holed (the common adjacent-helper / config / call-site
/// shape `hole_stmt` covers); a module declaration (import/export) yields `None`
/// and the caller tries the other neighbor.
fn render_context_neighbor(
    item: &ModuleItem,
    kept: &BTreeSet<AnchorSpan>,
) -> Result<Option<String>> {
    let ModuleItem::Stmt(stmt) = item else {
        return Ok(None);
    };
    Ok(Some(emit_selector(ModuleItem::Stmt(hole_stmt(
        stmt, kept,
    )))?))
}

/// Per-slot initializer holing for the read-off var paths: an object init holes
/// to its key-set form ([`hole_object_padded`], interleaved `ANYTHING`),
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
    for (idx, declarator) in var.decls.iter().enumerate() {
        if !target_slots.contains(&idx) {
            skipped_run = true;
            continue;
        }
        if skipped_run {
            decls.push(declarator_hole(declarator_hole_name()));
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
    }
    if skipped_run {
        decls.push(declarator_hole(declarator_hole_name()));
    }
    let mut holed_var = var.clone();
    holed_var.declare = false;
    holed_var.decls = decls;
    emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(holed_var)))))
}
