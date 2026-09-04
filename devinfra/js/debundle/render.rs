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
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, DECLARATORS_HOLE_KEYWORD,
    STMT_LIST_HOLE_KEYWORD,
};
use swc_common::{DUMMY_SP, Span, Spanned, SyntaxContext};
use swc_ecma_ast::*;

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

/// Hole a function for selector form: every parameter to an `ANYTHING` pattern
/// (pinning arity, not names) and the body's statements to `STMT_LIST` runs
/// around the kept anchors. Used both for top-level function selectors and for
/// function-valued subexpressions reached through [`hole_expr`].
pub(crate) fn hole_function(function: &Function, kept: &BTreeSet<AnchorSpan>) -> Function {
    let mut holed = function.clone();
    holed.params = function.params.iter().map(|_| anything_param()).collect();
    if let Some(body) = &function.body {
        holed.body = Some(holed_block(body, kept));
    }
    holed
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
        Expr::Paren(paren) => hole_expr(&paren.expr, kept),
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
        // Sequence (comma) expression (`(super(a), this.x = b, this.label = "tok")`):
        // hole each element the way [`hole_array`] holes array elements — a non-anchor
        // element collapses to `ANYTHING` through the leading guard, the anchored one
        // recurses. A discriminating leaf buried in a comma-sequence (e.g. an error
        // subclass whose entire constructor body is one sequence statement) is holed in
        // place rather than kept verbatim; keeping it verbatim leaves raw sibling
        // subtrees the matcher rejects, forcing the read-off all the way to
        // enclosing-context anchoring. Arity-exact (no run hole), mirroring the array
        // path.
        Expr::Seq(seq) => {
            let mut holed = seq.clone();
            holed.exprs = seq
                .exprs
                .iter()
                .map(|element| Box::new(hole_expr(element, kept)))
                .collect();
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
        // `useCallback((e) => {…})` initializer) carrying a kept anchor: hole the
        // params to `ANYTHING` and the body to `STMT_LIST` around the anchor, the
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
            holed.params = arrow.params.iter().map(|_| anything_pat()).collect();
            holed.body = Box::new(match arrow.body.as_ref() {
                BlockStmtOrExpr::BlockStmt(block) => {
                    BlockStmtOrExpr::BlockStmt(holed_block(block, kept))
                }
                BlockStmtOrExpr::Expr(expr) => {
                    BlockStmtOrExpr::Expr(Box::new(hole_expr(expr, kept)))
                }
            });
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
        Expr::Paren(paren) => hole_callee_expr(&paren.expr, kept),
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
fn hole_args(args: &[ExprOrSpread], kept: &BTreeSet<AnchorSpan>) -> Vec<ExprOrSpread> {
    let mut holed = Vec::new();
    let mut dropped_run = false;
    for arg in args {
        if node_retains_any(arg.expr.span(), kept) {
            if dropped_run {
                holed.push(args_hole());
                dropped_run = false;
            }
            let mut kept_arg = arg.clone();
            if arg.spread.is_none() {
                kept_arg.expr = Box::new(hole_expr(&arg.expr, kept));
            }
            holed.push(kept_arg);
        } else {
            dropped_run = true;
        }
    }
    if dropped_run {
        holed.push(args_hole());
    }
    holed
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
fn object_props_pat_prop() -> ObjectPatProp {
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
    let mut props = Vec::new();
    let mut dropped_run = false;
    for prop in &object.props {
        if node_retains_any(prop.span(), kept) {
            if dropped_run {
                props.push(object_props_pat_prop());
                dropped_run = false;
            }
            props.push(prop.clone());
        } else {
            dropped_run = true;
        }
    }
    if dropped_run {
        props.push(object_props_pat_prop());
    }
    let mut holed = object.clone();
    holed.props = props;
    holed
}

fn hole_object(object: &ObjectLit, kept: &BTreeSet<AnchorSpan>) -> ObjectLit {
    let mut props = Vec::new();
    let mut dropped_run = false;
    for prop in &object.props {
        if node_retains_any(prop.span(), kept) {
            if dropped_run {
                props.push(object_props_prop());
                dropped_run = false;
            }
            props.push(hole_prop(prop, kept));
        } else {
            dropped_run = true;
        }
    }
    if dropped_run {
        props.push(object_props_prop());
    }
    let mut holed = object.clone();
    holed.props = props;
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
    let mut out = Vec::new();
    let mut dropped_run = false;
    for stmt in stmts {
        if node_retains_any(stmt.span(), kept) {
            if dropped_run {
                out.push(stmt_list_stmt());
                dropped_run = false;
            }
            out.push(hole_stmt(stmt, kept));
        } else {
            dropped_run = true;
        }
    }
    if dropped_run || out.is_empty() {
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
    let mut out = Vec::new();
    let mut dropped_run = false;
    for case in cases {
        if node_retains_any(case.span(), kept) {
            if dropped_run {
                out.push(case_rest_case());
                dropped_run = false;
            }
            out.push(hole_switch_case(case, kept));
        } else {
            dropped_run = true;
        }
    }
    if dropped_run || out.is_empty() {
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

pub(crate) fn holes_present(source: &str) -> BTreeSet<String> {
    let mut holes = BTreeSet::new();
    for keyword in [
        ANYTHING_HOLE_KEYWORD,
        STMT_LIST_HOLE_KEYWORD,
        DECLARATORS_HOLE_KEYWORD,
    ] {
        if source.contains(keyword) {
            holes.insert(keyword.to_string());
        }
    }
    holes
}

#[cfg(test)]
mod interior_holing_tests {
    use super::*;
    use swc_ecma_visit::{Visit, VisitWith};

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
    fn holes_nested_object_inside_a_kept_array_argument() {
        // The object nested in a kept `ANYTHING.run([{…}])` keeps only the anchor
        // property, holing the rest to the object-property run hole (emitted as
        // `ANYTHING`, the run-absorber form in object-property position).
        // Regression guard for the `Expr::Array` recursion: the array used to fall
        // through `hole_expr`'s verbatim catch-all, so every property was pinned.
        let holed = hole_statement_expr(
            r#"ctx.engine.run([{ source: ctx.node, mode: "keepMe", silent: true }]);"#,
            "keepMe",
        );
        assert_eq!(
            normalize(&holed),
            normalize(r#"ANYTHING.run([{ ANYTHING, mode: "keepMe", ANYTHING }]);"#),
        );
    }

    #[test]
    fn holes_non_anchor_array_elements_to_anything() {
        // Array elements carrying no anchor hole to ANYTHING (arity-exact, since
        // the matcher matches array elements element-wise); only the element
        // holding the anchor is recursed into. The lone-prop object keeps its one
        // anchor prop with no run-hole padding (nothing was dropped). The bare
        // `render` callee holes to ANYTHING (a minified name the matcher
        // alpha-wildcards), unlike the member-method `.run` in the sibling case.
        let holed = hole_statement_expr(
            r#"render([first(), { mode: "keepMe" }, third()]);"#,
            "keepMe",
        );
        assert_eq!(
            normalize(&holed),
            normalize(r#"ANYTHING([ANYTHING, { mode: "keepMe" }, ANYTHING]);"#),
        );
    }

    /// The object-property run hole is emitted as the bare `ANYTHING` keyword —
    /// the only run-absorber spelling in object-property position. The padded
    /// key-set form interleaves the hole around the kept discriminating key.
    #[test]
    fn object_property_run_holes_emit_anything() {
        js_ast::with_swc_globals(|| {
            let module = js_ast::parse_js_module_ast(
                "<object-prop-hole>",
                r#"const x = { drop_a: 1, keepMe: "v", drop_b: 2 };"#,
            )
            .unwrap();
            let [ModuleItem::Stmt(Stmt::Decl(Decl::Var(var)))] = module.body.as_slice() else {
                panic!("expected a single var declaration");
            };
            let Some(Expr::Object(object)) = var.decls[0].init.as_deref() else {
                panic!("expected an object initializer");
            };
            let key_span = {
                let PropOrSpread::Prop(prop) = &object.props[1] else {
                    panic!("expected a key-value prop");
                };
                let Prop::KeyValue(kv) = prop.as_ref() else {
                    panic!("expected a key-value prop");
                };
                kv.key.span()
            };
            let kept = BTreeSet::from([span_key(key_span)]);
            let mut holed_decl = (**var).clone();
            holed_decl.decls[0].init =
                Some(Box::new(Expr::Object(hole_object_padded(object, &kept))));
            let holed = emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(
                holed_decl,
            )))))
            .unwrap();
            // The kept anchor is the `keepMe` key token; its value holes to
            // `ANYTHING`, and the dropped sibling-prop runs on both sides become
            // the object-property run hole, also emitted as `ANYTHING`.
            assert_eq!(
                normalize(&holed),
                normalize(r#"const x = { ANYTHING, keepMe: ANYTHING, ANYTHING };"#),
            );
        });
    }
}
