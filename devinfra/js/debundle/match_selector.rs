//! `debundle spec match-selector`: resolve a candidate `source_match` against a
//! chunk and report what it binds — and, when it pins a unique target, how much
//! further it could be holed.
//!
//! The interactive prove-gate probe behind the selector-authoring loop
//! (`plans/selector_authoring_agent.md`): the agent forms an anchor hypothesis,
//! writes a candidate `match`, and asks "does this resolve to the singleton I
//! mean, and the right one — and did I over-pin it?" before committing the
//! selector to YAML. Matching and slack share the same parse + baseline resolve,
//! so they are answered together.
//!
//! **Slack** is the mechanical half of "report over-narrow selectors as debt even
//! when they match": each entry is a strictly looser variant of the selector —
//! one kept thing holed — that still pins the same unique target. It is a
//! _heuristic_ for which pins to revisit, never a verdict: a zero-slack selector
//! can still be anchored on an incidental key, and the agent still judges whether
//! the surviving anchors are the right ones.
//!
//! Slack tries one relaxation at a time, of these kinds: hole a value expression
//! (literal / argument / property value) to `ANYTHING`; drop an object-literal
//! property, an object-pattern (destructure) property, a class member, a block
//! statement, or a call/`new` argument via the matching run-hole; or remove a
//! top-level context statement outright (a module-level `STMT_LIST` is not
//! honored on the member-binding resolution path). A relaxation that would delete
//! the `target_binding`'s own declaration is never tried — the matcher rejects a
//! selector that no longer declares its target.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, CLASS_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD, OBJECT_PROPS_HOLE_KEYWORD, STMT_HOLE_KEYWORD,
    STMT_LIST_HOLE_KEYWORD,
};
use spec::{SourceMatch, SourceMatchIdentifierMode};
use swc_common::DUMMY_SP;
use swc_ecma_ast::{
    AssignPatProp, BindingIdent, BlockStmt, CallExpr, Class, ClassMember, ClassProp, Decl, Expr,
    ExprOrSpread, ExprStmt, IdentName, Module, ModuleItem, NewExpr, ObjectLit, ObjectPat,
    ObjectPatProp, Pat, Prop, PropName, PropOrSpread, Stmt,
};
use swc_ecma_visit::{VisitMut, VisitMutWith};

use crate::render::{anything_expr, ident_node};

pub struct MatchSelectorConfig {
    pub source_file: Option<PathBuf>,
    pub source_root: Option<PathBuf>,
    pub chunk: Option<PathBuf>,
    pub match_source: String,
    pub identifiers: SourceMatchIdentifierMode,
    pub target_binding: Option<String>,
    /// Also compute holing slack when the selector pins a unique target.
    pub check_slack: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct MatchSelectorMatch {
    /// Top-level statement index the selector matched in the chunk.
    pub body_index: usize,
    /// Runtime (minified) name of the binding the selector would claim.
    pub binding_name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SlackRelaxation {
    /// A strictly looser selector — the input with one kept thing holed — that
    /// still resolves to the same unique target.
    pub relaxed_match: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct MatchSelectorReport {
    /// The headline verdict: exactly one item matched, so the selector is a
    /// valid (unique) pin. Zero or several matches both make it unusable.
    pub unique: bool,
    pub matches: Vec<MatchSelectorMatch>,
    /// Looser variants that still pin the same unique target — a non-empty list
    /// flags a likely over-pin. `None` when the selector is not unique (slack is
    /// undefined) or `--no-slack` skipped it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub slack: Option<Vec<SlackRelaxation>>,
}

/// Resolve the chunk path from the `--source-file` / `--source-root` + `--chunk`
/// flag combination shared by the source-aware selector commands.
pub(crate) fn resolve_chunk_source_file(
    source_file: Option<&Path>,
    source_root: Option<&Path>,
    chunk: Option<&Path>,
) -> Result<PathBuf> {
    match (source_file, source_root, chunk) {
        (Some(source_file), _, None) => Ok(source_file.to_path_buf()),
        (None, Some(source_root), Some(chunk)) => Ok(source_root.join(chunk)),
        (Some(_), _, Some(_)) => {
            bail!("use either --source-file or --source-root with --chunk, not both")
        }
        _ => bail!("a source chunk is required: pass --source-file or --source-root + --chunk"),
    }
}

pub fn run_match_selector(config: &MatchSelectorConfig) -> Result<MatchSelectorReport> {
    js_ast::with_swc_globals(|| run_match_selector_impl(config))
}

fn run_match_selector_impl(config: &MatchSelectorConfig) -> Result<MatchSelectorReport> {
    let source_file = resolve_chunk_source_file(
        config.source_file.as_deref(),
        config.source_root.as_deref(),
        config.chunk.as_deref(),
    )?;
    let source = std::fs::read_to_string(&source_file)
        .with_context(|| format!("reading source file {}", source_file.display()))?;
    let parsed = js_ast::parse_js_module_consuming(&source_file.display().to_string(), source)
        .with_context(|| format!("parsing source file {}", source_file.display()))?;

    let resolve = |match_source: String| -> Result<Vec<source_match::MemberBindingMatch>> {
        let selector = SourceMatch {
            match_source,
            identifiers: config.identifiers,
            target_binding: config.target_binding.clone(),
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        }
        .selector();
        source_match::member_binding_candidate_matches(
            &parsed.module,
            "<match-selector>",
            &selector,
        )
    };

    let baseline = resolve(config.match_source.clone())?;
    let mut matches: Vec<MatchSelectorMatch> = baseline
        .iter()
        .map(|matched| MatchSelectorMatch {
            body_index: matched.body_idx,
            binding_name: matched.binding.binding_name.clone(),
        })
        .collect();
    matches.sort_by_key(|matched| matched.body_index);

    let unique = matches.len() == 1;
    let slack = match (unique, config.check_slack) {
        (true, true) => Some(compute_slack(
            &config.match_source,
            baseline[0].body_idx,
            &baseline[0].binding.binding_name,
            config.target_binding.as_deref(),
            &resolve,
        )?),
        _ => None,
    };

    Ok(MatchSelectorReport {
        unique,
        matches,
        slack,
    })
}

/// Try every single-edit relaxation of the selector; keep the ones that still
/// resolve to the same unique `(body_idx, binding_name)` target.
fn compute_slack(
    match_source: &str,
    target_body_idx: usize,
    target_binding_name: &str,
    selector_target_binding: Option<&str>,
    resolve: &impl Fn(String) -> Result<Vec<source_match::MemberBindingMatch>>,
) -> Result<Vec<SlackRelaxation>> {
    let mut selector_module =
        js_ast::parse_js_module_consuming("<match-selector slack>", match_source.to_string())
            .with_context(|| "parsing the candidate selector for slack analysis")?
            .module;
    js_ast::strip_parens(&mut selector_module);
    let baseline_emit = js_ast::emit_module_source(&selector_module)?;

    let mut seen = BTreeSet::new();
    let mut slack = Vec::new();
    for relaxed in enumerate_relaxations(&selector_module, selector_target_binding) {
        let relaxed_match = js_ast::emit_module_source(&relaxed)?;
        if relaxed_match == baseline_emit || !seen.insert(relaxed_match.clone()) {
            continue;
        }
        if let [only] = resolve(relaxed_match.clone())?.as_slice() {
            if only.body_idx == target_body_idx && only.binding.binding_name == target_binding_name
            {
                slack.push(SlackRelaxation { relaxed_match });
            }
        }
    }
    Ok(slack)
}

/// The single-edit relaxation kinds. Each holes one kept element so the resulting
/// selector is a strict superset of the original.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Relaxation {
    /// Hole a kept value expression to `ANYTHING`.
    HoleExpr,
    /// Drop an object-literal property (absorbed by an `ANYTHING` run-hole).
    DropObjectProp,
    /// Drop an object-**pattern** (destructure) property, absorbed by the
    /// `ANYTHING`/`OBJECT_PROPS` pattern run-hole — the destructure analogue of
    /// [`Relaxation::DropObjectProp`].
    DropPatternProp,
    /// Drop a class member (absorbed by a `CLASS_REST` field).
    DropClassMember,
    /// Drop a statement inside a block body (absorbed by `STMT_LIST`).
    DropStatement,
    /// Drop a top-level context statement outright — a member selector's
    /// surrounding-window pins. Unlike a block statement, a module-level
    /// `STMT_LIST` is not honored on the member-binding resolution path (it
    /// matches a contiguous window), so the statement is removed, not holed.
    DropContextStatement,
    /// Drop a call/`new` argument (absorbed by `ARGS`).
    DropCallArg,
}

const RELAXATIONS: [Relaxation; 7] = [
    Relaxation::HoleExpr,
    Relaxation::DropObjectProp,
    Relaxation::DropPatternProp,
    Relaxation::DropClassMember,
    Relaxation::DropStatement,
    Relaxation::DropContextStatement,
    Relaxation::DropCallArg,
];

/// Produce every selector with exactly one element holed, across all relaxation
/// kinds. Each kind is counted (a dry run with an out-of-range target) and then
/// applied once per index.
fn enumerate_relaxations(selector: &Module, target_binding: Option<&str>) -> Vec<Module> {
    let mut out = Vec::new();
    for kind in RELAXATIONS {
        let total = apply_relaxation(&mut selector.clone(), kind, usize::MAX, target_binding);
        for index in 0..total {
            let mut relaxed = selector.clone();
            apply_relaxation(&mut relaxed, kind, index, target_binding);
            out.push(relaxed);
        }
    }
    out
}

/// Apply the `target`-th relaxation of `kind` (pre-order) to `module`, returning
/// the number of relaxation sites of that kind. With `target == usize::MAX` it
/// edits nothing and just counts. `target_binding` (the selector-local target
/// name) is the one declaration the binding-dropping kinds must never delete.
fn apply_relaxation(
    module: &mut Module,
    kind: Relaxation,
    target: usize,
    target_binding: Option<&str>,
) -> usize {
    let mut relaxer = Relaxer {
        kind,
        target,
        target_binding,
        seen: 0,
        done: false,
    };
    module.visit_mut_with(&mut relaxer);
    relaxer.seen
}

struct Relaxer<'a> {
    kind: Relaxation,
    target: usize,
    target_binding: Option<&'a str>,
    seen: usize,
    done: bool,
}

impl Relaxer<'_> {
    /// Count this site; replace it (returning true) when it is the target index.
    fn take(&mut self, droppable: bool) -> bool {
        if self.done || !droppable {
            return false;
        }
        if self.seen == self.target {
            self.done = true;
            return true;
        }
        self.seen += 1;
        false
    }
}

impl VisitMut for Relaxer<'_> {
    fn visit_mut_module(&mut self, module: &mut Module) {
        if self.kind == Relaxation::DropContextStatement {
            // Remove (don't hole) the context statement: a top-level `STMT_LIST`
            // is not honored on the member-binding resolution path (it matches a
            // contiguous window of fixed statements), so dropping the statement
            // outright is the strictly-looser sub-window the matcher supports.
            let protect = self.target_binding;
            let drop_at = module
                .body
                .iter()
                .position(|item| self.take(module_item_droppable(item, protect)));
            if let Some(idx) = drop_at {
                module.body.remove(idx);
            }
        }
        module.visit_mut_children_with(self);
    }

    fn visit_mut_expr(&mut self, expr: &mut Expr) {
        if self.kind == Relaxation::HoleExpr && self.take(is_holeable_expr(expr)) {
            *expr = anything_expr();
            return;
        }
        expr.visit_mut_children_with(self);
    }

    fn visit_mut_object_lit(&mut self, object: &mut ObjectLit) {
        if self.kind == Relaxation::DropObjectProp {
            for prop in object.props.iter_mut() {
                if self.take(is_droppable_prop(prop)) {
                    *prop = object_props_hole();
                    break;
                }
            }
        }
        object.visit_mut_children_with(self);
    }

    fn visit_mut_object_pat(&mut self, pat: &mut ObjectPat) {
        if self.kind == Relaxation::DropPatternProp {
            for prop in pat.props.iter_mut() {
                if self.take(pat_prop_droppable(prop, self.target_binding)) {
                    *prop = object_pat_props_hole();
                    break;
                }
            }
        }
        pat.visit_mut_children_with(self);
    }

    fn visit_mut_class(&mut self, class: &mut Class) {
        if self.kind == Relaxation::DropClassMember {
            for member in class.body.iter_mut() {
                if self.take(is_droppable_member(member)) {
                    *member = class_member_hole();
                    break;
                }
            }
        }
        class.visit_mut_children_with(self);
    }

    fn visit_mut_block_stmt(&mut self, block: &mut BlockStmt) {
        if self.kind == Relaxation::DropStatement {
            for stmt in block.stmts.iter_mut() {
                if self.take(is_droppable_stmt(stmt)) {
                    *stmt = stmt_list_hole();
                    break;
                }
            }
        }
        block.visit_mut_children_with(self);
    }

    fn visit_mut_call_expr(&mut self, call: &mut CallExpr) {
        if self.kind == Relaxation::DropCallArg {
            for arg in call.args.iter_mut() {
                if self.take(is_droppable_arg(arg)) {
                    *arg = args_hole();
                    break;
                }
            }
        }
        call.visit_mut_children_with(self);
    }

    fn visit_mut_new_expr(&mut self, new_expr: &mut NewExpr) {
        if self.kind == Relaxation::DropCallArg {
            if let Some(args) = new_expr.args.as_mut() {
                for arg in args.iter_mut() {
                    if self.take(is_droppable_arg(arg)) {
                        *arg = args_hole();
                        break;
                    }
                }
            }
        }
        new_expr.visit_mut_children_with(self);
    }
}

/// A value expression worth holing. Bare identifiers are excluded: under
/// `alpha_all` they are already alpha-wildcards (and a hole keyword is itself an
/// identifier), so holing one removes no real anchor.
fn is_holeable_expr(expr: &Expr) -> bool {
    !matches!(expr, Expr::Ident(_) | Expr::Invalid(_))
}

fn is_droppable_prop(prop: &PropOrSpread) -> bool {
    !matches!(prop, PropOrSpread::Prop(boxed)
        if matches!(boxed.as_ref(), Prop::Shorthand(ident) if is_hole_keyword(&ident.sym)))
}

fn is_droppable_member(member: &ClassMember) -> bool {
    !matches!(member, ClassMember::ClassProp(prop)
        if prop.value.is_none()
            && matches!(&prop.key, PropName::Ident(key) if is_hole_keyword(&key.sym)))
}

fn is_droppable_stmt(stmt: &Stmt) -> bool {
    !matches!(stmt, Stmt::Expr(expr_stmt)
        if matches!(expr_stmt.expr.as_ref(), Expr::Ident(ident) if is_hole_keyword(&ident.sym)))
}

fn is_droppable_arg(arg: &ExprOrSpread) -> bool {
    !matches!(arg.expr.as_ref(), Expr::Ident(ident) if is_hole_keyword(&ident.sym))
}

/// A top-level module item droppable by `DropContextStatement`: a statement
/// (module declarations cannot be holed — the hole syntax is an expression
/// statement) that is not already a statement hole, and whose deletion would not
/// remove `protect` — the selector's `target_binding`, whose own declaration the
/// matcher requires to be present.
fn module_item_droppable(item: &ModuleItem, protect: Option<&str>) -> bool {
    let ModuleItem::Stmt(stmt) = item else {
        return false;
    };
    is_droppable_stmt(stmt) && !protect.is_some_and(|name| module_item_declares(item, name))
}

/// A destructure-pattern property droppable by `DropPatternProp`: not already a
/// pattern run-hole, and not the property binding `protect` (the
/// `target_binding`, which must stay declared).
fn pat_prop_droppable(prop: &ObjectPatProp, protect: Option<&str>) -> bool {
    is_droppable_pat_prop(prop)
        && !protect.is_some_and(|name| object_pat_prop_binds_name(prop, name))
}

fn is_droppable_pat_prop(prop: &ObjectPatProp) -> bool {
    !matches!(prop, ObjectPatProp::Assign(assign)
        if assign.value.is_none() && is_hole_keyword(&assign.key.id.sym))
}

/// Whether a top-level statement declares `name` (a `var`/`function`/`class`
/// binding), so `DropContextStatement` can leave the target's own declaration in
/// place.
fn module_item_declares(item: &ModuleItem, name: &str) -> bool {
    let ModuleItem::Stmt(Stmt::Decl(decl)) = item else {
        return false;
    };
    match decl {
        Decl::Var(var) => var.decls.iter().any(|d| pat_binds_name(&d.name, name)),
        Decl::Fn(fn_decl) => fn_decl.ident.sym == *name,
        Decl::Class(class_decl) => class_decl.ident.sym == *name,
        _ => false,
    }
}

/// Whether a binding pattern introduces `name` anywhere, descending through
/// array/object destructuring, rest, and default-value patterns.
fn pat_binds_name(pat: &Pat, name: &str) -> bool {
    match pat {
        Pat::Ident(ident) => ident.id.sym == *name,
        Pat::Array(array) => array
            .elems
            .iter()
            .flatten()
            .any(|elem| pat_binds_name(elem, name)),
        Pat::Object(object) => object
            .props
            .iter()
            .any(|prop| object_pat_prop_binds_name(prop, name)),
        Pat::Rest(rest) => pat_binds_name(&rest.arg, name),
        Pat::Assign(assign) => pat_binds_name(&assign.left, name),
        Pat::Expr(_) | Pat::Invalid(_) => false,
    }
}

fn object_pat_prop_binds_name(prop: &ObjectPatProp, name: &str) -> bool {
    match prop {
        ObjectPatProp::KeyValue(key_value) => pat_binds_name(&key_value.value, name),
        ObjectPatProp::Assign(assign) => assign.key.id.sym == *name,
        ObjectPatProp::Rest(rest) => pat_binds_name(&rest.arg, name),
    }
}

/// Whether an identifier name is one of the selector hole keywords, so we never
/// "drop" a hole the relaxer (or the input) already wrote.
fn is_hole_keyword(name: &str) -> bool {
    name == ANYTHING_HOLE_KEYWORD
        || name == EXPR_HOLE_KEYWORD
        || name == STMT_HOLE_KEYWORD
        || name == STMT_LIST_HOLE_KEYWORD
        || name == ARGS_HOLE_KEYWORD
        || name == OBJECT_PROPS_HOLE_KEYWORD
        || name == CLASS_REST_HOLE_KEYWORD
        || name == CASE_REST_HOLE_KEYWORD
        || name == DECLARATORS_HOLE_KEYWORD
}

fn object_props_hole() -> PropOrSpread {
    PropOrSpread::Prop(Box::new(Prop::Shorthand(ident_node(ANYTHING_HOLE_KEYWORD))))
}

/// The destructure-pattern analogue of [`object_props_hole`]: a shorthand
/// binding named `ANYTHING` (the sugar form `object_pat_prop_list_hole_name`
/// also accepts for `OBJECT_PROPS`) absorbing a run of dropped pattern props.
fn object_pat_props_hole() -> ObjectPatProp {
    ObjectPatProp::Assign(AssignPatProp {
        span: DUMMY_SP,
        key: BindingIdent {
            id: ident_node(ANYTHING_HOLE_KEYWORD),
            type_ann: None,
        },
        value: None,
    })
}

fn args_hole() -> ExprOrSpread {
    ExprOrSpread {
        spread: None,
        expr: Box::new(Expr::Ident(ident_node(ARGS_HOLE_KEYWORD))),
    }
}

fn stmt_list_hole() -> Stmt {
    Stmt::Expr(ExprStmt {
        span: DUMMY_SP,
        expr: Box::new(Expr::Ident(ident_node(STMT_LIST_HOLE_KEYWORD))),
    })
}

fn class_member_hole() -> ClassMember {
    ClassMember::ClassProp(ClassProp {
        span: DUMMY_SP,
        key: PropName::Ident(IdentName::new(ANYTHING_HOLE_KEYWORD.into(), DUMMY_SP)),
        value: None,
        type_ann: None,
        is_static: false,
        decorators: vec![],
        accessibility: None,
        is_abstract: false,
        is_optional: false,
        is_override: false,
        readonly: false,
        declare: false,
        definite: false,
    })
}

pub fn render_match_selector_text(report: &MatchSelectorReport, out: &mut String) {
    use std::fmt::Write;
    let verdict = match report.matches.len() {
        1 => "unique",
        0 => "no-match",
        _ => "ambiguous",
    };
    let _ = writeln!(out, "{verdict} ({} match(es))", report.matches.len());
    for matched in &report.matches {
        let _ = writeln!(
            out,
            "  body[{}] -> {}",
            matched.body_index, matched.binding_name
        );
    }
    match &report.slack {
        None => {}
        Some(slack) if slack.is_empty() => {
            let _ = writeln!(out, "slack: none (nothing further is holeable)");
        }
        Some(slack) => {
            let _ = writeln!(
                out,
                "slack: {} looser variant(s) still unique — likely over-pin",
                slack.len()
            );
            for (i, relaxation) in slack.iter().enumerate() {
                let _ = writeln!(out, "  [{i}]");
                for line in relaxation.relaxed_match.lines() {
                    let _ = writeln!(out, "        {line}");
                }
            }
        }
    }
}
