use super::*;

/// Recognize the TypeScript-emit "enum" IIFE shape that initializes a
/// `var`-bound binding to a plain object built by parameter-mutation:
///
///     var X = ((p) => (p.A = "a", p.B = "b", p))(X || {});
///     var X = ((p) => (p["A"] = "a", p["B"] = "b", p))(X || (X = {}));
///     var X = ((p) => p)({});
///
/// Returns `true` iff every constraint below holds; the caller treats a
/// `true` result the same as any other `is_plain_data_init` admission
/// (the binding admits as `PlainData` if no chunk-wide member writes /
/// hostile builtin calls etc. follow). The scanner's scope-tracking
/// for function/arrow params handles the inner `p.K = …` writes when
/// `p` shadows the binding name (the canonical TS shape uses the same
/// name for the param).
///
/// **Soundness contract:**
///
/// At runtime, the binding holds the object returned by the IIFE.
/// We require that object to have only data properties:
///
/// 1. **Inline IIFE callable.** The callee is a syntactic `Expr::Arrow`
///    or `Expr::Fn`. Excludes any case where the callable could be
///    replaced at runtime with a different function.
/// 2. **Single Ident parameter.** Pattern parameters (destructuring,
///    rest, default) are out of scope — the parameter is the alias for
///    the mutating writes, and treating non-Ident patterns would
///    require deeper escape analysis.
/// 3. **Single non-spread positional argument.** Either an empty object
///    literal, `X || {}`, or `X || (X = {})` where `X` is the binding
///    being initialized. This excludes passing arbitrary existing
///    objects into the mutating body.
/// 4. **Body mutates only the parameter and returns it.** Body is either:
///    * `Expr::Ident(p)` alone — degenerate "return param unchanged"
///      shape; the IIFE returns the arg as-is, which is plain by (3).
///    * `Expr::Seq` whose last element is `Expr::Ident(p)` and every
///      preceding element is `p.K = primLit` or `p[strLit] = primLit`
///      or `p[numLit] = primLit` — a parameter-mutation followed by
///      return. Function-expression IIFEs may use expression
///      statements for the writes followed by `return p`.
///
/// **Why the result is plain:**
///
/// The argument starts as a plain object (by 3). The body's writes are
/// all `param.K = primitiveLiteral` data-property assignments — these
/// invoke `[[Set]]` on the param, which on a target with no existing
/// accessor for `K` runs `CreateDataProperty` and produces a data
/// descriptor. None of the writes can install an accessor, change the
/// prototype, or call user-defined code. The returned object is the
/// same object passed in, now carrying additional data properties.
/// Reads `X.K` on the binding after the IIFE fires no user code.
///
/// **What is NOT admitted (and why):**
///
/// * **Multi-statement bodies** that do anything other than parameter
///   mutation — `console.log(p)`, `Object.defineProperty(p, …)`,
///   nested IIFEs, etc.
///
/// **Numeric-enum reverse-mapping** `param[(param.X = 0)] = "X"` IS
/// admitted via the nested-assignment-as-computed-key branch in
/// `is_ts_enum_iife_property_write` — the inner assignment is itself
/// a `param.X = primLit` data-property write, the outer is a
/// data-property write keyed by the primitive that inner evaluates
/// to. Both writes are sound.
pub(crate) fn is_ts_enum_iife_init_for_binding(expr: &Expr, binding: &str) -> bool {
    let mut cur = expr;
    while let Expr::Paren(p) = cur {
        cur = &p.expr;
    }
    let Expr::Call(call) = cur else {
        return false;
    };
    is_ts_enum_iife_call_for_binding(call, binding)
}

/// `CallExpr`-level form of `is_ts_enum_iife_init_for_binding`, used
/// both from the var-init admission and by `PlainDataWriteScanner` to
/// exempt the vetted `X || (X = {})` argument occurrence of the enum
/// binding from the escape scan.
pub(crate) fn is_ts_enum_iife_call_for_binding(call: &CallExpr, binding: &str) -> bool {
    let Callee::Expr(callee_expr) = &call.callee else {
        return false;
    };
    let mut callee = callee_expr.as_ref();
    while let Expr::Paren(p) = callee {
        callee = &p.expr;
    }
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return false;
    }
    if !is_ts_enum_iife_arg(&call.args[0].expr, binding) {
        return false;
    }
    match callee {
        Expr::Arrow(arrow) => {
            if arrow.params.len() != 1 {
                return false;
            }
            let Pat::Ident(param_ident) = &arrow.params[0] else {
                return false;
            };
            let param_name = param_ident.id.sym.as_ref();
            match arrow.body.as_ref() {
                BlockStmtOrExpr::Expr(body_expr) => {
                    is_ts_enum_iife_body_expr(strip_parens(body_expr.as_ref()), param_name)
                }
                BlockStmtOrExpr::BlockStmt(block) => is_ts_enum_iife_body_block(block, param_name),
            }
        }
        Expr::Fn(function) => {
            if function.function.params.len() != 1 {
                return false;
            }
            let Pat::Ident(param_ident) = &function.function.params[0].pat else {
                return false;
            };
            let Some(body) = &function.function.body else {
                return false;
            };
            is_ts_enum_iife_body_block(body, param_ident.id.sym.as_ref())
        }
        _ => false,
    }
}

/// IIFE argument must be a plain object literal, a self-assigning
/// short-circuit (`X || (X = {})`), or a plain short-circuit
/// (`X || {}`). Walks through `Paren` wrappers and accepts the
/// short-circuit form recursively on the right side.
fn is_ts_enum_iife_arg(expr: &Expr, binding: &str) -> bool {
    match expr {
        Expr::Paren(p) => is_ts_enum_iife_arg(&p.expr, binding),
        Expr::Object(obj) => obj.props.is_empty(),
        Expr::Bin(b) if b.op == BinaryOp::LogicalOr => {
            matches!(strip_parens(b.left.as_ref()), Expr::Ident(id) if id.sym.as_ref() == binding)
                && is_ts_enum_iife_arg(&b.right, binding)
        }
        Expr::Assign(a) if a.op == AssignOp::Assign => {
            matches!(
                &a.left,
                AssignTarget::Simple(SimpleAssignTarget::Ident(id)) if id.id.sym.as_ref() == binding
            ) && matches!(strip_parens(a.right.as_ref()), Expr::Object(obj) if obj.props.is_empty())
        }
        _ => false,
    }
}

/// Arrow body must be either `p` (return param unchanged) or a
/// sequence expression `(p.K1 = lit, p.K2 = lit, …, p)` with the
/// trailing `p` as the return value.
fn is_ts_enum_iife_body_expr(expr: &Expr, param: &str) -> bool {
    if matches!(expr, Expr::Ident(id) if id.sym.as_ref() == param) {
        return true;
    }
    let Expr::Seq(seq) = expr else {
        return false;
    };
    if seq.exprs.is_empty() {
        return false;
    }
    let (last, rest) = seq.exprs.split_last().expect("non-empty checked above");
    let last_inner = strip_parens(last.as_ref());
    if !matches!(last_inner, Expr::Ident(id) if id.sym.as_ref() == param) {
        return false;
    }
    rest.iter()
        .all(|e| is_ts_enum_iife_property_write(strip_parens(e.as_ref()), param))
}

fn is_ts_enum_iife_body_block(block: &BlockStmt, param: &str) -> bool {
    let Some((last, rest)) = block.stmts.split_last() else {
        return false;
    };
    if !rest.iter().all(|stmt| match stmt {
        Stmt::Expr(expr) => is_ts_enum_iife_property_write(strip_parens(expr.expr.as_ref()), param),
        _ => false,
    }) {
        return false;
    }
    let Stmt::Return(ret) = last else {
        return false;
    };
    let Some(arg) = ret.arg.as_deref() else {
        return false;
    };
    is_ts_enum_iife_body_expr(strip_parens(arg), param)
}

/// One step of the IIFE body:
///
/// * `param.K = primLit` — forward map (static key).
/// * `param[strLit] = primLit` / `param[numLit] = primLit` —
///   forward map (literal computed key).
/// * `param[(param.K = primLit)] = primLit` /
///   `param[(param["K"] = primLit)] = primLit` — TypeScript's
///   numeric-enum reverse-mapping shape. The inner assignment is
///   itself a `param.X = primLit` data-property write that
///   evaluates to the assigned primitive, which then becomes the
///   outer Member's computed key. Both writes are data-property
///   writes on the param; reads on the post-IIFE binding see only
///   data properties.
///
/// Only primitive-literal RHS is accepted at both nesting levels;
/// non-literal RHS could theoretically return an accessor-bearing
/// object — the property write would still install it as a data
/// descriptor on the param, but the stricter primitive-literal RHS
/// keeps the soundness story uniform with `is_plain_data_prop`.
fn is_ts_enum_iife_property_write(expr: &Expr, param: &str) -> bool {
    let Expr::Assign(assign) = expr else {
        return false;
    };
    if assign.op != AssignOp::Assign {
        return false;
    }
    let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left else {
        return false;
    };
    if !matches!(member.obj.as_ref(), Expr::Ident(id) if id.sym.as_ref() == param) {
        return false;
    }
    let key_ok = match &member.prop {
        MemberProp::Ident(ident) => ident.sym.as_ref() != "__proto__",
        MemberProp::Computed(c) => {
            let key = strip_parens(c.expr.as_ref());
            matches!(key, Expr::Lit(Lit::Str(s)) if s.value.to_string_lossy() != "__proto__")
                || matches!(key, Expr::Lit(Lit::Num(_)))
                // TS numeric-enum reverse-mapping: the computed key
                // is itself a `param.X = primLit` data-property write
                // that evaluates to a primitive used as the outer
                // key. Both the inner and outer writes are sound
                // data-property writes; recurse to verify the inner
                // matches the same shape this function admits.
                || is_ts_enum_iife_property_write(key, param)
        }
        MemberProp::PrivateName(_) => false,
    };
    if !key_ok {
        return false;
    }
    matches!(
        assign.right.as_ref(),
        Expr::Lit(Lit::Str(_))
            | Expr::Lit(Lit::Num(_))
            | Expr::Lit(Lit::Bool(_))
            | Expr::Lit(Lit::Null(_))
            | Expr::Lit(Lit::BigInt(_))
    )
}
