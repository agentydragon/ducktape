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
//! the object/var candidate menus): the index ranks each candidate
//! feature by selective × stable, so the chosen anchors are sparse and
//! rebuild-robust, and the production matcher proves the rendered selector
//! resolves uniquely (gate 1). When read-off cannot single out a target, the
//! matcher-proven exact declaration is relaxed through supported holes before
//! trying relational use sites or neighbors.
//!
//! Multi-target var binding groups read off per slot (`try_var_group_read_off`):
//! each target declarator slot reads its minimal anchor off the shape index
//! (restricted to the slot) plus a slot-aware greedy (`slot_minimal_anchors`),
//! the per-slot kept spans union, and the binding-group matcher proves the tuple.
//! Slots need not resolve independently: partial slot covers feed the tuple
//! proof, which adds ranked anchors if needed. All var paths share padded
//! initializer holing; the legacy keep-shallow renderer and recursive expression
//! dispatcher are gone. Unindexed literals remain proof-checked fallback pins.

mod class;
mod function;
mod group;
mod object;
mod use_site;
mod var;

pub(crate) use class::minimize_class_selector_candidates;
pub(crate) use function::minimize_function_selector_candidates;
pub(crate) use group::{minimize_var_group_selector, minimize_var_group_selector_candidates};
pub(crate) use use_site::named_object_use_bindings;

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result};
use readoff_render::kept_spans_for_anchor_set;
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, ARRAY_ELEMENTS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, SEQ_EXPRS_HOLE_KEYWORD, STMT_LIST_HOLE_KEYWORD,
    STRING_LITERAL_REGEX_PREDICATE, hole_keyword, is_hole_keyword,
};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

use crate::match_selector::relax_progressively;
use crate::regex_anchor::RegexAnchorSubstitution;
use crate::render::{
    AnchorSpan, declarator_hole, emit_selector, hole_expr, hole_function, hole_object_padded,
    hole_stmt, holes_present, named_pat, object_props_pat_prop,
};
use crate::{
    ChunkSelectorIndex, IndexedDeclaration, SpecializedSelector, SynthesizedTargetBinding,
    declarator_hole_name, matched_body_indices, prove_synthesized_selector, single_ident_pat_name,
};

// Keep whole-declaration proof work bounded even when its AST is enormous.
const MAX_EXACT_RELAXATION_PROBES: usize = 128;

/// Last-resort own-declaration search. The exact target is the most specific
/// member of the supported hole lattice: if a relaxation uniquely identifies
/// this declaration, the exact form does too, provided the matcher accepts the
/// declaration itself. Starting here covers discriminators absent from the
/// feature index (for example a binary operator). Each accepted edit is proven
/// by the production resolver; repeated emitted candidates share one proof.
/// The exact witness preserves completeness if the trial budget is exhausted.
pub(crate) fn relax_exact_declaration(
    index: &ChunkSelectorIndex<'_>,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    let Some(mut exact_item) = exact_declaration_item(item, targets) else {
        return Ok(None);
    };
    sanitize_exact_identifiers(&mut exact_item);
    let source = emit_selector(exact_item)?;
    if prove_synthesized_selector(index, decl, targets, &source).is_err() {
        return Ok(None);
    }
    let mut current = js_ast::parse_js_module_consuming("<exact selector>", source)?.module;
    js_ast::strip_parens(&mut current);
    let mut seen = BTreeSet::from([js_ast::emit_module_source(&current)?]);
    current = relax_progressively(
        current,
        targets.first().map(|target| target.export_name.as_str()),
        MAX_EXACT_RELAXATION_PROBES,
        |candidate| {
            let source = js_ast::emit_module_source(candidate)?;
            Ok(seen.insert(source.clone())
                && prove_synthesized_selector(index, decl, targets, &source).is_ok())
        },
    )?;
    // Adjacent run holes express the same language as one run hole. Collapse
    // them after greedy editing so a long body remains readable.
    let collapsed_source = collapse_statement_hole_runs(&js_ast::emit_module_source(&current)?)?;
    let match_source =
        if prove_synthesized_selector(index, decl, targets, &collapsed_source).is_ok() {
            collapsed_source
        } else {
            js_ast::emit_module_source(&current)?
        };
    Ok(Some(SpecializedSelector {
        rewritten_holes: holes_present(&match_source)?,
        match_source,
    }))
}

pub(crate) fn collapse_statement_hole_runs(source: &str) -> Result<String> {
    let mut module =
        js_ast::parse_js_module_consuming("<selector hole runs>", source.to_string())?.module;
    module.visit_mut_with(&mut CollapseStatementHoleRuns);
    js_ast::emit_module_source(&module)
}

fn selector_keyword_collision(name: &str) -> bool {
    is_hole_keyword(name) || name == STRING_LITERAL_REGEX_PREDICATE
}

fn selector_run_keyword_collision(name: &str) -> bool {
    hole_keyword(name).is_some_and(|keyword| {
        [
            ARGS_HOLE_KEYWORD,
            ARRAY_ELEMENTS_HOLE_KEYWORD,
            CASE_REST_HOLE_KEYWORD,
            DECLARATORS_HOLE_KEYWORD,
            SEQ_EXPRS_HOLE_KEYWORD,
            STMT_LIST_HOLE_KEYWORD,
        ]
        .contains(&keyword)
    }) || name == STRING_LITERAL_REGEX_PREDICATE
}

fn selector_prop_name_collision(name: &PropName) -> bool {
    match name {
        PropName::Ident(ident) => selector_run_keyword_collision(ident.sym.as_ref()),
        PropName::Str(string) => selector_run_keyword_collision(&js_ast::str_value(string)),
        _ => false,
    }
}

/// Real source identifiers can have the same spelling as selector hole tokens.
/// In `AlphaAll` mode their spelling is immaterial, so rename those identifiers
/// consistently before constructing the exact witness. A source property name
/// that collides with selector syntax is holed at its enclosing expression,
/// object property, or class member; it cannot be represented as an exact pin.
fn sanitize_exact_identifiers(item: &mut ModuleItem) {
    #[derive(Default)]
    struct Names(BTreeSet<String>);
    impl Visit for Names {
        fn visit_ident(&mut self, ident: &Ident) {
            self.0.insert(ident.sym.to_string());
        }
    }
    struct Renamer {
        used: BTreeSet<String>,
        replacements: BTreeMap<String, String>,
        next: usize,
    }
    impl VisitMut for Renamer {
        fn visit_mut_expr(&mut self, expr: &mut Expr) {
            if let Expr::Member(member) = expr
                && let MemberProp::Ident(prop) = &member.prop
                && selector_run_keyword_collision(prop.sym.as_ref())
            {
                *expr = crate::render::anything_expr();
                return;
            }
            expr.visit_mut_children_with(self);
        }

        fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
            object.visit_mut_children_with(self);
            for prop in &mut object.props {
                let collision = match prop {
                    PropOrSpread::Prop(boxed) => {
                        let key = match boxed.as_ref() {
                            Prop::KeyValue(value) => Some(&value.key),
                            Prop::Getter(value) => Some(&value.key),
                            Prop::Setter(value) => Some(&value.key),
                            Prop::Method(value) => Some(&value.key),
                            _ => None,
                        };
                        key.is_some_and(selector_prop_name_collision)
                    }
                    _ => false,
                };
                if collision {
                    *prop = PropOrSpread::Prop(Box::new(Prop::Shorthand(
                        crate::render::ident_node(ANYTHING_HOLE_KEYWORD),
                    )));
                }
            }
        }

        fn visit_mut_object_pat(&mut self, object: &mut ObjectPat) {
            for prop in &mut object.props {
                let collision = match prop {
                    ObjectPatProp::KeyValue(value) => selector_prop_name_collision(&value.key),
                    ObjectPatProp::Assign(value) => {
                        selector_keyword_collision(value.key.id.sym.as_ref())
                    }
                    ObjectPatProp::Rest(_) => false,
                };
                if collision {
                    *prop = object_props_pat_prop();
                } else {
                    prop.visit_mut_children_with(self);
                }
            }
        }

        fn visit_mut_prop(&mut self, prop: &mut Prop) {
            let shorthand_collision = match prop {
                Prop::Shorthand(ident) => selector_keyword_collision(ident.sym.as_ref()),
                Prop::Assign(assign) => selector_keyword_collision(assign.key.sym.as_ref()),
                _ => false,
            };
            if shorthand_collision {
                *prop = Prop::Shorthand(crate::render::ident_node(ANYTHING_HOLE_KEYWORD));
                return;
            }
            prop.visit_mut_children_with(self);
        }

        fn visit_mut_class(&mut self, class: &mut Class) {
            class.visit_mut_children_with(self);
            for member in &mut class.body {
                let key = match member {
                    ClassMember::Method(value) => Some(&value.key),
                    ClassMember::ClassProp(value) => Some(&value.key),
                    _ => None,
                };
                if key.is_some_and(selector_prop_name_collision) {
                    *member = crate::render::class_member_hole();
                }
            }
        }

        fn visit_mut_ident(&mut self, ident: &mut Ident) {
            let original = ident.sym.as_ref();
            if !selector_keyword_collision(original) {
                return;
            }
            let key = original.to_string();
            let replacement = self.replacements.entry(key).or_insert_with(|| {
                loop {
                    let candidate = format!("__debundle_exact_identifier_{}", self.next);
                    self.next += 1;
                    if self.used.insert(candidate.clone()) {
                        break candidate;
                    }
                }
            });
            ident.sym = replacement.as_str().into();
        }
    }
    let mut names = Names::default();
    item.visit_with(&mut names);
    item.visit_mut_with(&mut Renamer {
        used: names.0,
        replacements: BTreeMap::new(),
        next: 0,
    });
}

struct CollapseStatementHoleRuns;

impl VisitMut for CollapseStatementHoleRuns {
    fn visit_mut_function_body(&mut self, body: &mut FunctionBody) {
        body.visit_mut_children_with(self);
        collapse_stmt_run(&mut body.stmts);
    }

    fn visit_mut_block_stmt(&mut self, block: &mut BlockStmt) {
        block.visit_mut_children_with(self);
        collapse_stmt_run(&mut block.stmts);
    }
}

fn collapse_stmt_run(stmts: &mut Vec<Stmt>) {
    stmts.dedup_by(|a, b| is_stmt_list_hole(a) && is_stmt_list_hole(b));
}

fn is_stmt_list_hole(stmt: &Stmt) -> bool {
    matches!(stmt, Stmt::Expr(expr)
        if matches!(expr.expr.as_ref(), Expr::Ident(ident)
            if ident.sym.as_ref() == STMT_LIST_HOLE_KEYWORD))
}

fn exact_declaration_item(
    item: &ModuleItem,
    targets: &[SynthesizedTargetBinding],
) -> Option<ModuleItem> {
    let decl = js_ast::item_decl(item)?;
    let renamed = match decl {
        Decl::Fn(function)
            if targets.len() == 1
                && function.ident.sym.as_ref() == targets[0].runtime_binding.as_str() =>
        {
            let mut function = function.clone();
            function.ident = crate::render::ident_node(&targets[0].export_name);
            Decl::Fn(function)
        }
        Decl::Class(class)
            if targets.len() == 1
                && class.ident.sym.as_ref() == targets[0].runtime_binding.as_str() =>
        {
            let mut class = class.clone();
            class.ident = crate::render::ident_node(&targets[0].export_name);
            Decl::Class(class)
        }
        Decl::Var(var) => {
            let mut var = var.clone();
            let mut found = BTreeSet::new();
            for declarator in &mut var.decls {
                rename_pattern_targets(&mut declarator.name, targets, &mut found);
            }
            if found.len() != targets.len() {
                return None;
            }
            Decl::Var(var)
        }
        _ => return None,
    };
    Some(ModuleItem::Stmt(Stmt::Decl(renamed)))
}

/// Rename only binding positions. An object-pattern shorthand is converted to
/// `key: Export` so the stable property key is not accidentally renamed too.
fn rename_pattern_targets(
    pat: &mut Pat,
    targets: &[SynthesizedTargetBinding],
    found: &mut BTreeSet<String>,
) {
    match pat {
        Pat::Ident(binding) => {
            if let Some(target) = targets
                .iter()
                .find(|target| binding.id.sym.as_ref() == target.runtime_binding.as_str())
            {
                found.insert(target.runtime_binding.clone());
                binding.id = crate::render::ident_node(&target.export_name);
            }
        }
        Pat::Array(array) => {
            for element in array.elems.iter_mut().flatten() {
                rename_pattern_targets(element, targets, found);
            }
        }
        Pat::Object(object) => {
            for prop in &mut object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => {
                        rename_pattern_targets(&mut key_value.value, targets, found);
                    }
                    ObjectPatProp::Assign(assign) => {
                        if let Some(target) = targets.iter().find(|target| {
                            assign.key.id.sym.as_ref() == target.runtime_binding.as_str()
                        }) {
                            found.insert(target.runtime_binding.clone());
                            let key = PropName::Ident(IdentName::new(
                                assign.key.id.sym.clone(),
                                assign.key.id.span,
                            ));
                            let binding = Pat::Ident(BindingIdent {
                                id: crate::render::ident_node(&target.export_name),
                                type_ann: None,
                            });
                            let value = match assign.value.take() {
                                Some(default) => Pat::Assign(AssignPat {
                                    span: assign.span,
                                    left: Box::new(binding),
                                    right: default,
                                }),
                                None => binding,
                            };
                            *prop = ObjectPatProp::KeyValue(KeyValuePatProp {
                                key,
                                value: Box::new(value),
                            });
                        }
                    }
                    ObjectPatProp::Rest(rest) => {
                        rename_pattern_targets(&mut rest.arg, targets, found);
                    }
                }
            }
        }
        Pat::Assign(assign) => rename_pattern_targets(&mut assign.left, targets, found),
        Pat::Rest(rest) => rename_pattern_targets(&mut rest.arg, targets, found),
        _ => {}
    }
}

/// Collect up to `limit` read-off selectors for the target, best-first — minimal
/// anchor set → individually-discriminating value anchors → multi-feature value
/// cover → bare scaffold → exact-declaration relaxation → use site →
/// enclosing-context neighbor — each rendered + proven
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
/// fails to prove. An exact declaration is tried when the indexed routes fail;
/// it may expose a discriminator that was never indexed.
fn read_off_candidates(
    index: &ChunkSelectorIndex<'_>,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    let item = index
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
    // returns `None` for tuple/context read-off to handle. When the scaffold
    // uniquely matches the read-off is done — it never falls through to neighbor
    // context (mirroring the original early return), so stop here regardless of
    // `limit`.
    let empty = BTreeSet::new();
    let scaffold = render_with(&empty)?;
    if matched_body_indices(index, &target.export_name, &scaffold)
        .is_ok_and(|matched| matched == BTreeSet::from([decl.body_idx]))
    {
        if let Some(selector) = finish_minimized_selector(index, decl, target, scaffold)? {
            collect(selector, &mut out);
        }
        return Ok(out);
    }

    // The feature index is deliberately incomplete: operators and other AST
    // structure can distinguish declarations even when indexed leaves cannot.
    // Search the target's own hole lattice before borrowing context.
    if let Some(selector) =
        relax_exact_declaration(index, item, decl, std::slice::from_ref(target))?
        && collect(selector, &mut out)
    {
        return Ok(out);
    }

    // A stable named property in a use site can identify a declaration whose own
    // body is indistinguishable from a sibling. Prefer that relationship over
    // an incidental adjacent declaration.
    if let Some(selector) = use_site::render_via_named_object_use_site(index, decl, target)?
        && collect(selector, &mut out)
    {
        return Ok(out);
    }

    // Enclosing-context anchoring (#2315): the target's own value anchors and the
    // bare scaffold all fail to single it out among same-shape siblings. Fall to
    // its stable neighbors — a 2-statement window pinning an adjacent declaration's
    // unique anchor + the target scaffold. `None` here leaves the target as residual
    // debt after exact-declaration relaxation also fails.
    if let Some(selector) = render_via_neighbor_context(index, decl, target, &scaffold)? {
        collect(selector, &mut out);
    }
    Ok(out)
}

fn finish_minimized_selector(
    index: &ChunkSelectorIndex<'_>,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    source: String,
) -> Result<Option<SpecializedSelector>> {
    let targets = std::slice::from_ref(target);
    if prove_synthesized_selector(index, decl, targets, &source).is_err() {
        return Ok(None);
    }
    let rewritten_holes = holes_present(&source)?;
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
/// debt if even the exact declaration cannot distinguish it.
fn render_via_neighbor_context(
    index: &ChunkSelectorIndex<'_>,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    target_scaffold: &str,
) -> Result<Option<SpecializedSelector>> {
    for candidate in index
        .shape_index
        .context_neighbor_anchor_candidates(decl.body_idx)
    {
        let Some(neighbor_item) = index.module.body.get(candidate.neighbor_body_idx) else {
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
    // Declaration names already match modulo alpha-renaming. ANYTHING is not
    // a supported declaration-name hole; retain the name and prune the body.
    let holed = match stmt {
        Stmt::Decl(Decl::Fn(function)) => Stmt::Decl(Decl::Fn(FnDecl {
            function: Box::new(hole_function(&function.function, kept)),
            ..function.clone()
        })),
        Stmt::Decl(Decl::Class(decl)) => Stmt::Decl(Decl::Class(ClassDecl {
            class: Box::new(class::hole_class(&decl.class, kept)),
            ..decl.clone()
        })),
        other => hole_stmt(other, kept),
    };
    Ok(Some(emit_selector(ModuleItem::Stmt(holed))?))
}

/// Shared initializer holing: padded object keys, sparse class members, and
/// expression holing for other forms.
fn hole_var_init_padded(init: &Expr, kept: &BTreeSet<AnchorSpan>) -> Expr {
    match init {
        Expr::Object(object) => Expr::Object(hole_object_padded(object, kept)),
        Expr::Class(expr) => Expr::Class(ClassExpr {
            class: Box::new(class::hole_class(&expr.class, kept)),
            ..expr.clone()
        }),
        other => hole_expr(other, kept),
    }
}

/// Render a `var` binding-group selector: keep the target declarator slots (each
/// renamed to its export via `export_for`, init holed by `hole_var_init_padded` then the
/// optional `regex_anchors` `STR_LITERAL_MATCHING_RE` post-pass), with
/// `DECLARATORS_*` holes absorbing the runs of non-target slots. The single-target
/// var and object read-offs are the N=1 case (one target slot, no `DECLARATORS_*`
/// gaps).
///
fn render_var_slots(
    var: &VarDecl,
    target_slots: &BTreeSet<usize>,
    export_for: &impl Fn(&str) -> Option<String>,
    kept: &BTreeSet<AnchorSpan>,
    regex_anchors: &BTreeMap<AnchorSpan, String>,
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
            let mut holed_init = hole_var_init_padded(init, kept);
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

/// Greedily extend a retention set using caller-specific proof and scoring.
/// At most two rounds of the indexed anchor window are scored; otherwise a
/// large ambiguous declaration can trigger a quadratic number of matcher
/// calls. Exhaustion returns partial progress: object callers must prove the
/// result, while groups can combine partial slot covers before proving the
/// whole tuple or trying the exact-declaration fallback.
fn extend_anchor_cover(
    mut kept: BTreeSet<AnchorSpan>,
    ranked: &[AnchorSpan],
    resolves: impl Fn(&BTreeSet<AnchorSpan>) -> Result<bool>,
    score: impl Fn(&BTreeSet<AnchorSpan>) -> Result<(bool, usize)>,
) -> Result<BTreeSet<AnchorSpan>> {
    let mut scored = 0;
    while !resolves(&kept)? {
        let mut best = None;
        for &anchor in ranked.iter().take(crate::render::MAX_MINIMIZER_ANCHORS) {
            if kept.contains(&anchor) {
                continue;
            }
            if scored == 2 * crate::render::MAX_MINIMIZER_ANCHORS {
                if let Some((_, best_anchor)) = best {
                    kept.insert(best_anchor);
                }
                return Ok(kept);
            }
            let mut trial = kept.clone();
            trial.insert(anchor);
            let candidate_score = score(&trial)?;
            scored += 1;
            if best.is_none_or(|(best_score, _)| candidate_score < best_score) {
                best = Some((candidate_score, anchor));
            }
        }
        let Some((_, anchor)) = best else {
            break;
        };
        kept.insert(anchor);
    }
    Ok(kept)
}

#[cfg(test)]
mod run_hole_tests {
    #[test]
    fn progressive_relaxation_skips_rejected_site_and_obeys_probe_budget() {
        js_ast::with_swc_globals(|| {
            let module = js_ast::parse_js_module_consuming(
                "<selector>",
                "function Selected(x) { x += 1; x += 2; x += 3; return x * 4; }".to_string(),
            )
            .unwrap()
            .module;
            let mut probes = 0;
            let relaxed =
                crate::match_selector::relax_progressively(module, Some("Selected"), 3, |_| {
                    probes += 1;
                    Ok(probes > 1)
                })
                .unwrap();
            let source = js_ast::emit_module_source(&relaxed).unwrap();
            assert_eq!(probes, 3);
            assert_eq!(source.matches("STMT_LIST").count(), 2, "{source}");
            assert!(source.contains("x += 1"), "{source}");
        });
    }

    #[test]
    fn collapses_adjacent_statement_run_holes() {
        js_ast::with_swc_globals(|| {
            let source =
                "function Selected(x) { STMT_LIST; STMT_LIST; STMT_LIST; return x * ANYTHING; }";
            let collapsed = super::collapse_statement_hole_runs(source).unwrap();
            assert_eq!(collapsed.matches("STMT_LIST").count(), 1, "{collapsed}");
        });
    }
}
