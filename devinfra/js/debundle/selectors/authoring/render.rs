//! AST-holing engine for selector rendering.
//!
//! A selector is the target rendered with a *retention set* ([`AnchorSpan`]s):
//! the byte spans of the concrete tokens (literals, member/property names,
//! callees, object keys) the selector pins. A node renders concretely iff a kept
//! span lies inside it; every other position is holed — `ANYTHING` for a bare
//! expression and for the object-property / class-member run holes (whose
//! detector predicates carry an `ANYTHING` fallback), and the load-bearing
//! run holes `STMT_LIST` / `ARGS` / `CASE_REST` for dropped statement / argument
//! / switch-case runs (where `ANYTHING` would collapse to an arity-exact
//! single-node hole, so the keyword stays). These primitives are form-agnostic;
//! the per-form minimizers (`crate::minimize`) drive them.

use std::collections::BTreeSet;

use anyhow::Result;
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, SEQ_EXPRS_HOLE_KEYWORD,
    STMT_LIST_HOLE_KEYWORD, hole_keyword, hole_name_for,
};
use swc_common::{DUMMY_SP, Span, Spanned, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

#[derive(Default)]
struct RetainedIdentifiers(BTreeSet<String>);

impl Visit for RetainedIdentifiers {
    fn visit_ident(&mut self, ident: &Ident) {
        if hole_keyword(&ident.sym).is_none() {
            self.0.insert(ident.sym.to_string());
        }
    }
}

/// `(lo, hi)` byte offsets of a retained concrete token.
pub(crate) type AnchorSpan = (u32, u32);

pub(crate) const MAX_MINIMIZER_ANCHORS: usize = 64;

pub(crate) fn span_key(span: Span) -> AnchorSpan {
    (span.lo.0, span.hi.0)
}

pub(crate) fn node_holds_anchor(node: Span, anchor: AnchorSpan) -> bool {
    node.lo.0 <= anchor.0 && anchor.1 <= node.hi.0
}

pub(crate) fn node_retains_any(node: Span, kept: &BTreeSet<AnchorSpan>) -> bool {
    kept.iter().any(|anchor| node_holds_anchor(node, *anchor))
}

pub(crate) fn ident_node(name: &str) -> Ident {
    Ident::new_no_ctxt(name.into(), DUMMY_SP)
}

pub(crate) fn anything_expr() -> Expr {
    Expr::Ident(ident_node(ANYTHING_HOLE_KEYWORD))
}

fn stmt_list_stmt() -> Stmt {
    Stmt::Expr(ExprStmt {
        span: DUMMY_SP,
        expr: Box::new(Expr::Ident(ident_node(STMT_LIST_HOLE_KEYWORD))),
    })
}

/// An object-property run-absorber hole, emitted as a bare `ANYTHING` shorthand
/// property — the only spelling the matcher accepts in this position.
fn object_props_prop() -> PropOrSpread {
    PropOrSpread::Prop(Box::new(Prop::Shorthand(ident_node(ANYTHING_HOLE_KEYWORD))))
}

/// An `ARGS` argument-list hole that absorbs a run of dropped (non-anchor)
/// call/`new` arguments (the argument analog of [`object_props_prop`]).
fn args_hole() -> ExprOrSpread {
    ExprOrSpread {
        spread: None,
        expr: Box::new(Expr::Ident(ident_node(ARGS_HOLE_KEYWORD))),
    }
}

/// A `case CASE_REST:` switch-case hole that absorbs a run of dropped
/// `case`/`default` clauses (the switch analog of the `ANYTHING;` class field).
fn case_rest_case() -> SwitchCase {
    SwitchCase {
        span: DUMMY_SP,
        test: Some(Box::new(Expr::Ident(ident_node(CASE_REST_HOLE_KEYWORD)))),
        cons: vec![],
    }
}

fn anything_pat() -> Pat {
    Pat::Ident(BindingIdent {
        id: ident_node(ANYTHING_HOLE_KEYWORD),
        type_ann: None,
    })
}

pub(crate) fn holed_block(block: &BlockStmt, kept: &BTreeSet<AnchorSpan>) -> BlockStmt {
    let mut holed = block.clone();
    holed.stmts = hole_stmts(&block.stmts, kept);
    holed
}

pub(crate) fn holed_function_body(
    body: &FunctionBody,
    kept: &BTreeSet<AnchorSpan>,
) -> FunctionBody {
    let mut holed = body.clone();
    holed.stmts = hole_stmts(&body.stmts, kept);
    holed
}

/// Hole a function for selector form, preserving parameters referenced by the
/// retained body. Used both for top-level function selectors and for
/// function-valued subexpressions reached through [`hole_expr`].
pub(crate) fn hole_function(function: &Function, kept: &BTreeSet<AnchorSpan>) -> Function {
    let mut holed = function.clone();
    if let Some(body) = &function.body {
        holed.body = Some(holed_function_body(body, kept));
    }
    let retained = retained_body_identifiers(holed.body.as_ref());
    holed.params = function
        .params
        .iter()
        .map(|param| retain_or_hole_param(param, &retained, kept))
        .collect();
    holed
}

pub(crate) fn retained_body_identifiers(body: Option<&FunctionBody>) -> BTreeSet<String> {
    let mut retained = RetainedIdentifiers::default();
    if let Some(body) = body {
        body.visit_with(&mut retained);
    }
    retained.0
}

pub(crate) fn retain_or_hole_param(
    param: &Param,
    retained: &BTreeSet<String>,
    kept: &BTreeSet<AnchorSpan>,
) -> Param {
    if !pattern_referenced_by_retained_body(&param.pat, retained)
        && !(matches!(param.pat, Pat::Object(_)) && node_retains_any(param.pat.span(), kept))
    {
        return anything_param();
    }
    let mut holed = param.clone();
    holed.pat = retain_or_hole_param_pat(&param.pat, retained, kept);
    holed
}

fn retain_or_hole_param_pat(
    pat: &Pat,
    retained: &BTreeSet<String>,
    kept: &BTreeSet<AnchorSpan>,
) -> Pat {
    if pattern_referenced_by_retained_body(pat, retained) {
        pat.clone()
    } else if matches!(pat, Pat::Object(_)) && node_retains_any(pat.span(), kept) {
        // A parameter's own property key can be the chosen anchor even when
        // no retained body expression refers to its local binding.
        hole_pat(pat, kept)
    } else {
        anything_pat()
    }
}

fn pattern_referenced_by_retained_body(pat: &Pat, retained: &BTreeSet<String>) -> bool {
    let mut identifiers = RetainedIdentifiers::default();
    pat.visit_with(&mut identifiers);
    !identifiers.0.is_disjoint(retained)
}

/// Prune an expression into selector form: keep concrete tokens whose span is in
/// `kept`, and replace every subtree off a kept token's path with an `ANYTHING`
/// hole node. The result is an ordinary `swc` AST emitted by codegen, not a
/// hand-built string.
pub(crate) fn hole_expr(expr: &Expr, kept: &BTreeSet<AnchorSpan>) -> Expr {
    if !node_retains_any(expr.span(), kept) {
        return anything_expr();
    }
    match expr {
        // Preserve grouping around arrow bodies and callees. Stripping these
        // parentheses can move a comma expression outside the arrow on emit.
        Expr::Paren(paren) => Expr::Paren(ParenExpr {
            expr: Box::new(hole_expr(&paren.expr, kept)),
            ..paren.clone()
        }),
        Expr::Lit(_) | Expr::Ident(_) | Expr::Tpl(_) => expr.clone(),
        Expr::Member(member) => {
            let mut holed = member.clone();
            holed.obj = Box::new(hole_expr(&member.obj, kept));
            Expr::Member(holed)
        }
        Expr::Call(call) => {
            let mut holed = call.clone();
            holed.callee = hole_callee(&call.callee, kept);
            holed.args = hole_args(&call.args, kept);
            Expr::Call(holed)
        }
        Expr::New(new_expr) => {
            let mut holed = new_expr.clone();
            holed.callee = Box::new(hole_callee_expr(&new_expr.callee, kept));
            holed.args = new_expr.args.as_ref().map(|args| hole_args(args, kept));
            Expr::New(holed)
        }
        Expr::Object(object) => Expr::Object(hole_object(object, kept)),
        Expr::Array(array) => Expr::Array(hole_array(array, kept)),
        // Keep sequence elements carrying an anchor and absorb each run of the
        // others with `SEQ_EXPRS`. A per-element `ANYTHING` would pin the
        // sequence length, which can change when unrelated assignments move.
        Expr::Seq(seq) => {
            let mut holed = seq.clone();
            holed.exprs = collapse_omitted_runs(
                seq.exprs.iter().map(|element| {
                    node_retains_any(element.span(), kept)
                        .then(|| Box::new(hole_expr(element, kept)))
                }),
                || Box::new(Expr::Ident(ident_node(SEQ_EXPRS_HOLE_KEYWORD))),
            );
            Expr::Seq(holed)
        }
        Expr::Await(await_expr) => {
            let mut holed = await_expr.clone();
            holed.arg = Box::new(hole_expr(&await_expr.arg, kept));
            Expr::Await(holed)
        }
        Expr::Unary(unary) => {
            let mut holed = unary.clone();
            holed.arg = Box::new(hole_expr(&unary.arg, kept));
            Expr::Unary(holed)
        }
        Expr::Bin(bin) => {
            let mut holed = bin.clone();
            holed.left = Box::new(hole_expr(&bin.left, kept));
            holed.right = Box::new(hole_expr(&bin.right, kept));
            Expr::Bin(holed)
        }
        // Conditional (ternary) `test ? cons : alt`: hole each branch so a
        // discriminating leaf in one branch is kept and the rest collapses to
        // `ANYTHING`, instead of pinning the whole ternary verbatim (which keeps raw
        // sibling subtrees). Reached e.g. when a kept anchor sits inside a
        // `x ? new Y(x) : void 0` initializer in a holed sequence element.
        Expr::Cond(cond) => {
            let mut holed = cond.clone();
            holed.test = Box::new(hole_expr(&cond.test, kept));
            holed.cons = Box::new(hole_expr(&cond.cons, kept));
            holed.alt = Box::new(hole_expr(&cond.alt, kept));
            Expr::Cond(holed)
        }
        // Assignment expression (`state.delta = "value"`): the dominant statement
        // shape in sequential write-block bodies. Hole the LHS target's receiver
        // (`state` → `ANYTHING`) while keeping the stable property name, and recurse
        // into the RHS so a discriminating literal there is kept and everything else
        // holed. The member property is preserved verbatim, mirroring `Expr::Member`.
        Expr::Assign(assign) => {
            let mut holed = assign.clone();
            holed.left = hole_assign_target(&assign.left, kept);
            holed.right = Box::new(hole_expr(&assign.right, kept));
            Expr::Assign(holed)
        }
        // Function/arrow-valued subexpressions (e.g. a `wrap(function(){…})` or
        // `useCallback((e) => {…})` initializer) carrying a kept anchor: hole
        // unreferenced params and the body to `STMT_LIST` around the anchor, the
        // same interior holing the top-level function selector does. A callback
        // with no kept anchor never reaches here — the leading `node_retains_any`
        // guard already collapsed it to `ANYTHING`.
        Expr::Fn(fn_expr) => {
            let mut holed = fn_expr.clone();
            holed.function = Box::new(hole_function(&fn_expr.function, kept));
            Expr::Fn(holed)
        }
        Expr::Arrow(arrow) => {
            let mut holed = arrow.clone();
            holed.body = Box::new(match arrow.body.as_ref() {
                ArrowFunctionBody::FunctionBody(body) => {
                    ArrowFunctionBody::FunctionBody(holed_function_body(body, kept))
                }
                ArrowFunctionBody::Expr(expr) => {
                    ArrowFunctionBody::Expr(Box::new(hole_expr(expr, kept)))
                }
            });
            // A retained body reference must still bind to its parameter. A
            // parameter hole would let `n[(n.KEY = 0)]` match a write through an
            // unrelated receiver, even though `n` itself matches alpha-renames.
            let mut retained = RetainedIdentifiers::default();
            holed.body.visit_with(&mut retained);
            holed.params = arrow
                .params
                .iter()
                .map(|param| retain_or_hole_param_pat(param, &retained.0, kept))
                .collect();
            Expr::Arrow(holed)
        }
        // Unmodeled shapes carrying a kept anchor: keep verbatim rather than
        // risk an unsound hole. Over-pinning here is a future refinement.
        _ => expr.clone(),
    }
}

/// Hole an assignment target. A member target (`receiver.prop`) holes its
/// receiver expression (a minified binding such as a function parameter) while
/// keeping the stable property name, mirroring [`hole_expr`]'s `Expr::Member`
/// arm. Bare-ident and pattern targets are kept verbatim — an ident assign
/// target is itself a (usually minified) name with nothing to hole, and
/// destructuring patterns are left intact rather than risk an unsound hole.
fn hole_assign_target(target: &AssignTarget, kept: &BTreeSet<AnchorSpan>) -> AssignTarget {
    match target {
        AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
            let mut holed = member.clone();
            holed.obj = Box::new(hole_expr(&member.obj, kept));
            AssignTarget::Simple(SimpleAssignTarget::Member(holed))
        }
        _ => target.clone(),
    }
}

/// Hole a call's callee, keeping only an alpha-stable invoked identity. A
/// member-method name (`.then`, `.bar`) is a stable pin and stays; a
/// bare-function reference (`make`, `wrapOuter`) is a minified name that churns
/// every rebuild — the matcher alpha-wildcards it, so it discriminates nothing
/// and the shape index never proposes it as an anchor — so it holes to
/// `ANYTHING`.
fn hole_callee(callee: &Callee, kept: &BTreeSet<AnchorSpan>) -> Callee {
    match callee {
        Callee::Expr(expr) => Callee::Expr(Box::new(hole_callee_expr(expr, kept))),
        Callee::Super(_) | Callee::Import(_) => callee.clone(),
    }
}

fn hole_callee_expr(expr: &Expr, kept: &BTreeSet<AnchorSpan>) -> Expr {
    match expr {
        // Member-method callee (`a.then(...)`): keep the stable property name even
        // when no anchor lands on it, holing only the receiver. Routing through
        // `hole_expr` would instead collapse the whole member to `ANYTHING` when its
        // subtree carries no kept anchor, dropping the discriminating method name.
        Expr::Member(member) => {
            let mut holed = member.clone();
            holed.obj = Box::new(hole_expr(&member.obj, kept));
            Expr::Member(holed)
        }
        Expr::Paren(paren) => Expr::Paren(ParenExpr {
            expr: Box::new(hole_callee_expr(&paren.expr, kept)),
            ..paren.clone()
        }),
        // Bare-identifier callee (and any other callee expression): hole through the
        // normal path. A bare-function name is alpha-wildcarded by the matcher and is
        // never a chosen anchor, so `hole_expr` holes it to `ANYTHING`.
        _ => hole_expr(expr, kept),
    }
}

/// Hole a call/`new` argument list for selector form: a run of dropped
/// (no-anchor) arguments collapses to a single `ARGS` run hole, while each
/// argument carrying an anchor is kept and recursively holed.
///
/// The `ARGS` hole matches as an ordered subsequence with gaps
/// (`match_expr_or_spread_slice`), so the selector survives a rebuild that adds
/// or removes a non-anchor argument — unlike a per-argument `ANYTHING`, which is
/// an arity-exact single-node hole. Mirrors [`hole_object`]'s property-run
/// collapsing. An anchored spread is kept verbatim (its expr left intact);
/// a non-anchor spread is absorbed into the run like any other dropped argument.
/// Replace each nonempty run of omitted elements with one list hole.
/// Callers own retention/rendering and any hole required for an empty list.
pub(crate) fn collapse_omitted_runs<T>(
    items: impl IntoIterator<Item = Option<T>>,
    hole: impl Fn() -> T,
) -> Vec<T> {
    let mut out = Vec::new();
    let mut omitted = false;
    for item in items {
        if let Some(item) = item {
            if omitted {
                out.push(hole());
                omitted = false;
            }
            out.push(item);
        } else {
            omitted = true;
        }
    }
    if omitted {
        out.push(hole());
    }
    out
}

fn hole_args(args: &[ExprOrSpread], kept: &BTreeSet<AnchorSpan>) -> Vec<ExprOrSpread> {
    collapse_omitted_runs(
        args.iter().map(|arg| {
            node_retains_any(arg.expr.span(), kept).then(|| {
                let mut arg = arg.clone();
                if arg.spread.is_none() {
                    arg.expr = Box::new(hole_expr(&arg.expr, kept));
                }
                arg
            })
        }),
        args_hole,
    )
}

/// Prune the elements of an array literal, holing each non-anchor element to
/// `ANYTHING` (an arity-exact `EXPR` hole) and recursing into the ones that
/// carry an anchor. This pass holes element-wise (arity-exact), so the holed
/// array keeps the same length; elisions and spreads are preserved verbatim.
/// (The matcher does support an `ARRAY_ELEMENTS` run hole that would absorb a
/// variable-length element run; emitting it from the minimizer instead of the
/// arity-exact form is a separate, not-yet-implemented step.)
fn hole_array(array: &ArrayLit, kept: &BTreeSet<AnchorSpan>) -> ArrayLit {
    let mut holed = array.clone();
    holed.elems = array
        .elems
        .iter()
        .map(|elem| {
            elem.as_ref().map(|element| {
                let mut holed_element = element.clone();
                if element.spread.is_none() {
                    holed_element.expr = Box::new(hole_expr(&element.expr, kept));
                }
                holed_element
            })
        })
        .collect();
    holed
}

/// A destructure-pattern run-absorber hole — a shorthand binding whose name is
/// `ANYTHING` — absorbing a run of dropped destructured properties. The pattern
/// analog of [`object_props_prop`].
pub(crate) fn object_props_pat_prop() -> ObjectPatProp {
    ObjectPatProp::Assign(AssignPatProp {
        span: DUMMY_SP,
        key: BindingIdent {
            id: ident_node(ANYTHING_HOLE_KEYWORD),
            type_ann: None,
        },
        value: None,
    })
}

/// Prune a binding pattern into selector form. An object destructuring pattern
/// (`const { …, x } = e`) holes down to the props carrying a kept anchor (a
/// discriminating destructured key) via [`hole_object_pat`]; every other
/// pattern is kept verbatim — a bare minified binding name is already
/// alpha-wildcarded by the matcher, so there is nothing to hole.
fn hole_pat(pat: &Pat, kept: &BTreeSet<AnchorSpan>) -> Pat {
    match pat {
        Pat::Object(object) => Pat::Object(hole_object_pat(object, kept)),
        _ => pat.clone(),
    }
}

/// Hole a destructuring object pattern: keep only the props carrying a kept
/// anchor (the discriminating destructured key), dropping every other run into
/// an `ANYTHING` hole. The pattern analog of [`hole_object`]; a kept prop is
/// retained verbatim (its bound local is alpha-wildcarded by the matcher).
fn hole_object_pat(object: &ObjectPat, kept: &BTreeSet<AnchorSpan>) -> ObjectPat {
    let mut holed = object.clone();
    holed.props = collapse_omitted_runs(
        object
            .props
            .iter()
            .map(|prop| node_retains_any(prop.span(), kept).then(|| prop.clone())),
        object_props_pat_prop,
    );
    holed
}

fn hole_object(object: &ObjectLit, kept: &BTreeSet<AnchorSpan>) -> ObjectLit {
    let mut holed = object.clone();
    holed.props = collapse_omitted_runs(
        object
            .props
            .iter()
            .map(|prop| node_retains_any(prop.span(), kept).then(|| hole_prop(prop, kept))),
        object_props_prop,
    );
    holed
}

/// Hole an object literal for the read-off object form: keep only props carrying
/// a kept anchor, with an `ANYTHING` run hole before the first kept prop,
/// after the last, and **between every pair** of kept props.
///
/// Unlike [`hole_object`] (which only emits a list hole where a run of props was
/// actually dropped), this pads both edges and interleaves unconditionally.
/// Object properties are unordered enum/lookup entries: a kept key can move on a
/// rebuild, so anchoring one to the object's edge (`anchored_right` in the
/// matcher) or assuming two kept keys stay adjacent is fragile. Surrounding every
/// kept prop with `ANYTHING` matches each as an independent interior
/// subsequence element, so a minimal *key set* survives key reorder and arbitrary
/// gaps. With no kept prop this is a bare `{ ANYTHING }`; with one it is the
/// padded single-key form (unchanged from the edge-padding behavior).
pub(crate) fn hole_object_padded(object: &ObjectLit, kept: &BTreeSet<AnchorSpan>) -> ObjectLit {
    let mut props = vec![object_props_prop()];
    for prop in &object.props {
        if node_retains_any(prop.span(), kept) {
            props.push(hole_prop(prop, kept));
            props.push(object_props_prop());
        }
    }
    let mut holed = object.clone();
    holed.props = props;
    holed
}

fn hole_prop(prop: &PropOrSpread, kept: &BTreeSet<AnchorSpan>) -> PropOrSpread {
    let PropOrSpread::Prop(inner) = prop else {
        return prop.clone();
    };
    if let Prop::KeyValue(key_value) = inner.as_ref() {
        let mut holed = key_value.clone();
        holed.value = Box::new(hole_expr(&key_value.value, kept));
        PropOrSpread::Prop(Box::new(Prop::KeyValue(holed)))
    } else {
        prop.clone()
    }
}

/// Hole a statement list, collapsing runs of dropped statements into a single
/// `STMT_LIST;` hole statement.
fn hole_stmts(stmts: &[Stmt], kept: &BTreeSet<AnchorSpan>) -> Vec<Stmt> {
    let mut out = collapse_omitted_runs(
        stmts
            .iter()
            .map(|item| node_retains_any(item.span(), kept).then(|| hole_stmt(item, kept))),
        stmt_list_stmt,
    );
    if out.is_empty() {
        out.push(stmt_list_stmt());
    }
    out
}

pub(crate) fn hole_stmt(stmt: &Stmt, kept: &BTreeSet<AnchorSpan>) -> Stmt {
    match stmt {
        Stmt::Expr(expr_stmt) => {
            let mut holed = expr_stmt.clone();
            holed.expr = Box::new(hole_expr(&expr_stmt.expr, kept));
            Stmt::Expr(holed)
        }
        Stmt::Return(ret) => {
            let mut holed = ret.clone();
            holed.arg = ret.arg.as_ref().map(|arg| Box::new(hole_expr(arg, kept)));
            Stmt::Return(holed)
        }
        Stmt::Throw(throw) => {
            let mut holed = throw.clone();
            holed.arg = Box::new(hole_expr(&throw.arg, kept));
            Stmt::Throw(holed)
        }
        Stmt::Decl(Decl::Var(var)) => {
            let mut holed = (**var).clone();
            for declarator in &mut holed.decls {
                declarator.name = hole_pat(&declarator.name, kept);
                if let Some(init) = &declarator.init {
                    declarator.init = Some(Box::new(hole_expr(init, kept)));
                }
            }
            Stmt::Decl(Decl::Var(Box::new(holed)))
        }
        Stmt::If(if_stmt) => {
            let mut holed = if_stmt.clone();
            holed.test = Box::new(hole_expr(&if_stmt.test, kept));
            let cons_stmts = match if_stmt.cons.as_ref() {
                Stmt::Block(block) => hole_stmts(&block.stmts, kept),
                other => hole_stmts(std::slice::from_ref(other), kept),
            };
            holed.cons = Box::new(Stmt::Block(BlockStmt {
                span: DUMMY_SP,
                ctxt: SyntaxContext::empty(),
                stmts: cons_stmts,
            }));
            holed.alt = None;
            Stmt::If(holed)
        }
        Stmt::Try(try_stmt) => {
            let mut holed = try_stmt.clone();
            holed.block = holed_block(&try_stmt.block, kept);
            if let Some(handler) = &mut holed.handler {
                if handler.param.is_some() {
                    handler.param = Some(anything_pat());
                }
                handler.body = holed_block(&handler.body, kept);
            }
            if let Some(finalizer) = &mut holed.finalizer {
                *finalizer = holed_block(finalizer, kept);
            }
            Stmt::Try(holed)
        }
        Stmt::Block(block) => Stmt::Block(holed_block(block, kept)),
        Stmt::Switch(switch) => {
            let mut holed = switch.clone();
            holed.discriminant = Box::new(hole_expr(&switch.discriminant, kept));
            holed.cases = hole_switch_cases(&switch.cases, kept);
            Stmt::Switch(holed)
        }
        // Unmodeled statement shapes carrying a kept anchor: keep verbatim.
        _ => stmt.clone(),
    }
}

/// Prune a `switch`'s case list: drop runs of non-discriminating
/// `case`/`default` clauses into `case CASE_REST:` holes, keeping only the
/// clauses that retain an anchor (their test literal or a body statement).
/// Mirrors [`hole_class_members`].
fn hole_switch_cases(cases: &[SwitchCase], kept: &BTreeSet<AnchorSpan>) -> Vec<SwitchCase> {
    let mut out = collapse_omitted_runs(
        cases
            .iter()
            .map(|item| node_retains_any(item.span(), kept).then(|| hole_switch_case(item, kept))),
        case_rest_case,
    );
    if out.is_empty() {
        out.push(case_rest_case());
    }
    out
}

fn hole_switch_case(case: &SwitchCase, kept: &BTreeSet<AnchorSpan>) -> SwitchCase {
    let mut holed = case.clone();
    holed.test = case
        .test
        .as_ref()
        .map(|test| Box::new(hole_expr(test, kept)));
    holed.cons = hole_stmts(&case.cons, kept);
    holed
}

/// A `DECLARATORS_<pos> = null` declarator hole absorbing a run of non-target
/// declarators in a binding-group selector.
pub(crate) fn declarator_hole(name: &str) -> VarDeclarator {
    VarDeclarator {
        span: DUMMY_SP,
        name: named_pat(name),
        init: Some(Box::new(Expr::Lit(Lit::Null(Null { span: DUMMY_SP })))),
        definite: false,
    }
}

pub(crate) fn anything_param() -> Param {
    Param {
        span: DUMMY_SP,
        decorators: vec![],
        pat: anything_pat(),
    }
}

pub(crate) fn named_pat(name: &str) -> Pat {
    Pat::Ident(BindingIdent {
        id: ident_node(name),
        type_ann: None,
    })
}

/// Emit a synthesized selector item (a holed declaration) to source via the
/// shared codegen — the only AST→string step, and the matcher's parse inverts it.
pub(crate) fn emit_selector(item: ModuleItem) -> Result<String> {
    js_ast::emit_module_source(&Module {
        span: DUMMY_SP,
        body: vec![item],
        shebang: None,
    })
}

/// Report keyword identifiers in generated selector syntax, not occurrences in
/// strings, comments, or concrete property names. Parse only the final generated
/// selector; input-chunk analysis still comes from the shared chunk indexes.
pub(crate) fn holes_present(source: &str) -> Result<BTreeSet<String>> {
    #[derive(Default)]
    struct Holes(BTreeSet<String>);

    impl Visit for Holes {
        fn visit_ident(&mut self, ident: &Ident) {
            if let Some(keyword) = hole_keyword(&ident.sym) {
                self.0.insert(keyword.to_string());
            }
        }

        fn visit_class_prop(&mut self, prop: &ClassProp) {
            // Class-member runs use an uninitialized ANYTHING field. Its key
            // is an IdentName, unlike the Ident in expression/pattern holes.
            if prop.value.is_none()
                && let PropName::Ident(key) = &prop.key
                && hole_name_for(&key.sym, ANYTHING_HOLE_KEYWORD).is_some()
            {
                self.0.insert(ANYTHING_HOLE_KEYWORD.to_string());
            }
            prop.visit_children_with(self);
        }
    }

    let module = js_ast::parse_js_module_ast("<generated selector holes>", source)?;
    let mut holes = Holes::default();
    module.visit_with(&mut holes);
    Ok(holes.0)
}

/// A class-member run hole, represented by an `ANYTHING;` field.
pub(crate) fn class_member_hole() -> ClassMember {
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

#[cfg(test)]
mod interior_holing_tests {
    use super::*;

    #[test]
    fn omitted_runs_preserve_retained_order_and_empty_input() {
        for (input, expected) in [
            (vec![], vec![]),
            (vec![None, None], vec![0]),
            (vec![Some(1), Some(2)], vec![1, 2]),
            (
                vec![None, Some(1), None, None, Some(2), None],
                vec![0, 1, 0, 2, 0],
            ),
        ] {
            assert_eq!(collapse_omitted_runs(input, || 0), expected);
        }
    }

    #[test]
    fn hole_inventory_reads_syntax_not_text() {
        js_ast::with_swc_globals(|| {
            for (source, keyword) in [
                ("f(ARGS_value);", "ARGS"),
                ("class C { ANYTHING_members; }", "ANYTHING"),
            ] {
                assert_eq!(
                    holes_present(source).unwrap(),
                    BTreeSet::from([keyword.to_string()])
                );
            }
            assert!(
                holes_present(
                    r#"// ANYTHING
const x = "STMT_LIST";
const y = { DECLARATORS: 1 };
x.ARGS;
class C { CASE_REST = 1; }"#,
                )
                .unwrap()
                .is_empty()
            );
        });
    }

    /// Round-trip `source` through swc parse + codegen so equality is on AST
    /// shape, not incidental formatting.
    fn normalize(source: &str) -> String {
        js_ast::with_swc_globals(|| {
            let module = js_ast::parse_js_module_ast("<interior-holing>", source).unwrap();
            js_ast::emit_module_source(&module).unwrap()
        })
    }

    /// Span of the first string literal whose value is `needle`.
    fn string_literal_span(expr: &Expr, needle: &str) -> Span {
        struct Find<'a> {
            needle: &'a str,
            found: Option<Span>,
        }
        impl Visit for Find<'_> {
            fn visit_str(&mut self, str_: &Str) {
                if self.found.is_none() && str_.value.to_string_lossy() == self.needle {
                    self.found = Some(str_.span);
                }
            }
        }
        let mut find = Find {
            needle,
            found: None,
        };
        expr.visit_with(&mut find);
        find.found.expect("needle literal present in expression")
    }

    /// Hole the single expression-statement in `source`, pinning the lone
    /// `anchor` string literal as the kept span.
    fn hole_statement_expr(source: &str, anchor: &str) -> String {
        js_ast::with_swc_globals(|| {
            let module = js_ast::parse_js_module_ast("<interior-holing>", source).unwrap();
            let [ModuleItem::Stmt(Stmt::Expr(stmt))] = module.body.as_slice() else {
                panic!("expected a single expression statement");
            };
            let kept = BTreeSet::from([span_key(string_literal_span(&stmt.expr, anchor))]);
            emit_selector(ModuleItem::Stmt(Stmt::Expr(ExprStmt {
                span: DUMMY_SP,
                expr: Box::new(hole_expr(&stmt.expr, &kept)),
            })))
            .unwrap()
        })
    }

    #[test]
    fn holes_non_anchor_array_elements_to_anything() {
        // Array elements carrying no anchor hole to ANYTHING (arity-exact, since
        // the matcher matches array elements element-wise); only the element
        // holding the anchor is recursed into. The lone-prop object keeps its one
        // anchor prop with no run-hole padding (nothing was dropped). The bare
        // `render` callee holes to ANYTHING (a minified name the matcher
        // alpha-wildcards).
        let holed = hole_statement_expr(
            r#"render([first(), { mode: "keepMe" }, third()]);"#,
            "keepMe",
        );
        assert_eq!(
            normalize(&holed),
            normalize(r#"ANYTHING([ANYTHING, { mode: "keepMe" }, ANYTHING]);"#),
        );
    }
}
