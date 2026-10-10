//! `debundle spec match-selector`: resolve a candidate `source_match` against a
//! chunk and report what it binds — and, when it pins a unique target, how much
//! further it could be holed.
//!
//! The interactive prove-gate probe behind selector authoring: the agent forms
//! an anchor hypothesis, writes a candidate `match`, and asks "does this resolve
//! to the singleton I mean, and the right one — and did I over-pin it?" before
//! committing the selector to YAML. Matching and slack share the same parse +
//! baseline resolve, so they are answered together.
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
//! statement, a call/`new` argument, or a comma-sequence element via the matching
//! run-hole; or remove a top-level context statement outright (a module-level
//! `STMT_LIST` is not honored on the member-binding resolution path). A
//! relaxation that would delete the `target_binding`'s own declaration is never
//! tried — the matcher rejects a selector that no longer declares its target.

use std::collections::BTreeSet;
use std::path::PathBuf;

use anyhow::{Context, Result};
use binding_targets::{binding_name_strings, declaration_name_strings};
use selector_outcome::{Outcome, SelectorOutcome, SelectorOutcomeReport};
use selector_resolve::{Member, MemberSelector, SpecModule};
use serde::Serialize;
use source_match::ParsedSourceMatchSelector;
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, SEQ_EXPRS_HOLE_KEYWORD, STMT_LIST_HOLE_KEYWORD,
    is_hole_keyword,
};
use spec::{AnonymousStatementSelector, SourceMatchIdentifierMode};
use swc_common::DUMMY_SP;
use swc_ecma_ast::{
    ArrowExpr, ArrowFunctionBody, BlockStmt, CallExpr, Class, ClassMember, Constructor, Expr,
    ExprOrSpread, ExprStmt, Function, Module, ModuleItem, NewExpr, ObjectLit, ObjectPat,
    ObjectPatProp, Pat, Prop, PropName, PropOrSpread, SeqExpr, Stmt,
};
use swc_ecma_visit::{VisitMut, VisitMutWith};

use crate::render::{anything_expr, class_member_hole, ident_node, object_props_pat_prop};

pub struct MatchSelectorConfig {
    pub source_file: Option<PathBuf>,
    pub source_root: Option<PathBuf>,
    pub chunk: Option<PathBuf>,
    pub match_source: String,
    pub target_binding: Option<String>,
    /// Also compute holing slack when the selector pins a unique target.
    pub check_slack: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct SlackRelaxation {
    /// A strictly looser selector — the input with one kept thing holed — that
    /// still resolves to the same unique target.
    pub relaxed_match: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct MatchSelectorReport {
    /// The probe's one outcome; `resolved` means the selector is a valid pin.
    #[serde(flatten)]
    pub outcomes: SelectorOutcomeReport,
    /// Looser variants that still pin the same unique target — a non-empty list
    /// flags a likely over-pin. `None` when the selector is not unique (slack is
    /// undefined) or `--no-slack` skipped it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub slack: Option<Vec<SlackRelaxation>>,
}

use crate::source_input::resolve_chunk_source_file;

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
    let chunk = selector_resolve::Chunk::analyze("<match-selector>", &parsed.module);
    // The probe is a one-entity spec: its outcome is the resolve's.
    let resolve = |match_source: String| -> Result<Outcome> {
        let selector = AnonymousStatementSelector {
            match_source,
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: config.target_binding.clone(),
        };
        let probe = SpecModule {
            path: "<match-selector>".to_string(),
            members: vec![Member {
                export_name: "<match-selector>".to_string(),
                selector: MemberSelector::SourceMatch(ParsedSourceMatchSelector::parse(
                    "<match-selector>",
                    "<source_match needle in <match-selector>>".to_string(),
                    &selector,
                    "source_match",
                )?),
            }],
            anonymous_statements: Vec::new(),
        };
        let [resolved] = <[_; 1]>::try_from(chunk.resolve(&[probe])?.outcomes)
            .map_err(|outcomes| anyhow::anyhow!("a one-entity resolve returned {outcomes:?}"))?;
        Ok(resolved.outcome.outcome)
    };

    let outcome = resolve(config.match_source.clone())?;
    let slack = match (&outcome, config.check_slack) {
        (Outcome::Resolved { .. }, true) => Some(compute_slack(
            &config.match_source,
            &outcome,
            config.target_binding.as_deref(),
            &resolve,
        )?),
        _ => None,
    };

    Ok(MatchSelectorReport {
        outcomes: SelectorOutcomeReport {
            outcomes: vec![SelectorOutcome {
                chunk: config
                    .chunk
                    .as_deref()
                    .unwrap_or(&source_file)
                    .display()
                    .to_string(),
                placement: None,
                target_binding: config.target_binding.clone(),
                selector_preview: Some(source_match::source_match_preview(&config.match_source)),
                outcome,
            }],
            templates: Vec::new(),
        },
        slack,
    })
}

/// Try every single-edit relaxation of the selector; keep the ones that still
/// resolve to the same `resolved` outcome.
fn compute_slack(
    match_source: &str,
    resolved: &Outcome,
    selector_target_binding: Option<&str>,
    resolve: &impl Fn(String) -> Result<Outcome>,
) -> Result<Vec<SlackRelaxation>> {
    let mut selector_module =
        js_ast::parse_js_module_consuming("<match-selector slack>", match_source.to_string())
            .with_context(|| "parsing the candidate selector for slack analysis")?
            .module;
    js_ast::strip_parens(&mut selector_module);
    let baseline_emit = js_ast::emit_module_source(&selector_module)?;

    let mut seen = BTreeSet::new();
    let mut slack = Vec::new();
    for_each_relaxation(&selector_module, selector_target_binding, |relaxed| {
        let relaxed_match = js_ast::emit_module_source(&relaxed)?;
        if relaxed_match == baseline_emit || !seen.insert(relaxed_match.clone()) {
            return Ok(false);
        }
        if resolve(relaxed_match.clone())? == *resolved {
            slack.push(SlackRelaxation { relaxed_match });
        }
        Ok(false)
    })?;
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
    /// `ANYTHING` pattern run-hole — the destructure analogue of
    /// [`Relaxation::DropObjectProp`].
    DropPatternProp,
    /// Drop a class member (absorbed by an `ANYTHING;` class field).
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
    /// Drop one element of a comma-sequence expression (absorbed by `SEQ_EXPRS`).
    DropSequenceElement,
}

const RELAXATIONS: [Relaxation; 8] = [
    // Try large removable units first. This matters when the same walk is
    // repeated after each accepted minimizer edit: discarding a statement or
    // member before its nested expressions avoids many redundant proof calls.
    Relaxation::DropStatement,
    Relaxation::DropClassMember,
    Relaxation::DropObjectProp,
    Relaxation::DropPatternProp,
    Relaxation::DropCallArg,
    Relaxation::DropSequenceElement,
    Relaxation::HoleExpr,
    Relaxation::DropContextStatement,
];

/// Visit every one-edit relaxation without retaining the whole candidate set.
/// The callback returns true to stop after a useful edit. Slack and the exact
/// declaration fallback use the same hole vocabulary and traversal order.
pub(crate) fn for_each_relaxation(
    selector: &Module,
    target_binding: Option<&str>,
    mut inspect: impl FnMut(Module) -> Result<bool>,
) -> Result<()> {
    for kind in RELAXATIONS {
        let total = apply_relaxation(&mut selector.clone(), kind, usize::MAX, target_binding).0;
        for index in 0..total {
            let mut relaxed = selector.clone();
            apply_relaxation(&mut relaxed, kind, index, target_binding);
            if inspect(relaxed)? {
                return Ok(());
            }
        }
    }
    Ok(())
}

/// Greedily edit a selector with a forward cursor over each relaxation kind.
/// Keeping the cursor avoids restarting the full walk after every accepted
/// edit. Rejected sites are not retried, so this pass favors bounded work over
/// exhausting every order of greedy edits.
/// `max_probes` also bounds AST clones and resolver calls for a huge exact
/// declaration; the original selector remains a proven witness throughout.
pub(crate) fn relax_progressively(
    mut selector: Module,
    target_binding: Option<&str>,
    max_probes: usize,
    mut inspect: impl FnMut(&Module) -> Result<bool>,
) -> Result<Module> {
    let mut probes = 0;
    'kinds: for kind in RELAXATIONS {
        let mut cursor = 0;
        loop {
            if probes == max_probes {
                break 'kinds;
            }
            let mut candidate = selector.clone();
            let (_, applied) = apply_relaxation(&mut candidate, kind, cursor, target_binding);
            if !applied {
                break;
            }
            probes += 1;
            if inspect(&candidate)? {
                selector = candidate;
                // The edited site disappeared from this kind's candidate list;
                // its successor now occupies the same cursor.
            } else {
                cursor += 1;
            }
        }
    }
    Ok(selector)
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
) -> (usize, bool) {
    let mut relaxer = Relaxer {
        kind,
        target,
        target_binding,
        seen: 0,
        done: false,
    };
    module.visit_mut_with(&mut relaxer);
    (relaxer.seen, relaxer.done)
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
    fn visit_mut_function(&mut self, function: &mut Function) {
        if self.kind == Relaxation::DropStatement
            && let Some(body) = function.body.as_mut()
        {
            for stmt in &mut body.stmts {
                if self.take(is_droppable_stmt(stmt)) {
                    *stmt = stmt_list_hole();
                    break;
                }
            }
        }
        function.visit_mut_children_with(self);
    }

    fn visit_mut_arrow_expr(&mut self, arrow: &mut ArrowExpr) {
        if self.kind == Relaxation::DropStatement
            && let ArrowFunctionBody::FunctionBody(body) = arrow.body.as_mut()
        {
            for stmt in &mut body.stmts {
                if self.take(is_droppable_stmt(stmt)) {
                    *stmt = stmt_list_hole();
                    break;
                }
            }
        }
        arrow.visit_mut_children_with(self);
    }

    fn visit_mut_constructor(&mut self, constructor: &mut Constructor) {
        if self.kind == Relaxation::DropStatement
            && let Some(body) = constructor.body.as_mut()
        {
            for stmt in &mut body.stmts {
                if self.take(is_droppable_stmt(stmt)) {
                    *stmt = stmt_list_hole();
                    break;
                }
            }
        }
        constructor.visit_mut_children_with(self);
    }

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
                    *prop = object_props_pat_prop();
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
        if self.kind == Relaxation::DropCallArg
            && let Some(args) = new_expr.args.as_mut()
        {
            for arg in args.iter_mut() {
                if self.take(is_droppable_arg(arg)) {
                    *arg = args_hole();
                    break;
                }
            }
        }
        new_expr.visit_mut_children_with(self);
    }

    fn visit_mut_seq_expr(&mut self, seq: &mut SeqExpr) {
        if self.kind == Relaxation::DropSequenceElement {
            for element in seq.exprs.iter_mut() {
                if self.take(is_droppable_seq_element(element)) {
                    **element = seq_exprs_hole();
                    break;
                }
            }
        }
        seq.visit_mut_children_with(self);
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

/// A comma-sequence element droppable by `DropSequenceElement`: not already a
/// hole, so the relaxed selector is strictly looser instead of unchanged.
fn is_droppable_seq_element(element: &Expr) -> bool {
    !matches!(element, Expr::Ident(ident) if is_hole_keyword(&ident.sym))
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
    js_ast::item_decl(item).is_some_and(|decl| {
        declaration_name_strings(decl)
            .iter()
            .any(|bound| bound == name)
    })
}

/// Whether a destructure-pattern property introduces `name` anywhere.
fn object_pat_prop_binds_name(prop: &ObjectPatProp, name: &str) -> bool {
    let pat_binds_name = |pat: &Pat| binding_name_strings(pat).iter().any(|bound| bound == name);
    match prop {
        ObjectPatProp::KeyValue(key_value) => pat_binds_name(&key_value.value),
        ObjectPatProp::Assign(assign) => assign.key.id.sym == *name,
        ObjectPatProp::Rest(rest) => pat_binds_name(&rest.arg),
    }
}

fn object_props_hole() -> PropOrSpread {
    PropOrSpread::Prop(Box::new(Prop::Shorthand(ident_node(ANYTHING_HOLE_KEYWORD))))
}

fn args_hole() -> ExprOrSpread {
    ExprOrSpread {
        spread: None,
        expr: Box::new(Expr::Ident(ident_node(ARGS_HOLE_KEYWORD))),
    }
}

/// The comma-sequence run hole, emitted as a bare identifier element — the only
/// spelling the matcher accepts in sequence-element position.
fn seq_exprs_hole() -> Expr {
    Expr::Ident(ident_node(SEQ_EXPRS_HOLE_KEYWORD))
}

fn stmt_list_hole() -> Stmt {
    Stmt::Expr(ExprStmt {
        span: DUMMY_SP,
        expr: Box::new(Expr::Ident(ident_node(STMT_LIST_HOLE_KEYWORD))),
    })
}

pub fn render_match_selector_text(report: &MatchSelectorReport, out: &mut String) {
    use std::fmt::Write;
    for outcome in &report.outcomes.outcomes {
        let _ = writeln!(out, "{}", outcome.render_line());
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
