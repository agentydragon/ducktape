use super::*;

pub(crate) fn classify_var_decl_purity(
    var: &VarDecl,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    var.decls
        .iter()
        .filter_map(|decl| {
            let init = decl.init.as_deref()?;
            let Pat::Ident(binding) = &decl.name else {
                // Destructuring declarators (`const {a} = o`,
                // `const [x] = o`) fire getters / the iterator
                // protocol on the initializer value — user code the
                // init expression's own classification doesn't see.
                // Simplest sound rule: any non-Ident pattern makes
                // the statement not pure, regardless of init shape.
                // (A fresh-plain-data-literal RHS would be sound to
                // admit; deliberately not done — the shape is rare
                // at chunk top and the uniform rule is easier to
                // audit.)
                let pattern_purity = param_destructuring_purity(&decl.name)
                    .unwrap_or_else(|| Purity::from_reason(PurityRule::Other, decl.span));
                return Some(pattern_purity.worst(classify_expr_purity(
                    init,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                )));
            };
            let name = binding.id.sym.as_ref();
            if var.kind == VarDeclKind::Var
                && graph.is_plain_data(name)
                && is_ts_enum_iife_init_for_binding(init, name)
            {
                Some(Purity::Pure)
            } else {
                Some(classify_expr_purity(
                    init,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ))
            }
        })
        .fold(Purity::Pure, Purity::worst)
}

pub(crate) fn classify_expr_purity(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match expr {
        Expr::Lit(_) => Purity::Pure,
        Expr::Ident(_) => Purity::Pure,
        Expr::This(_) | Expr::MetaProp(_) => Purity::Pure,
        // Template interpolation runs ToString on each interpolated
        // value — an object operand fires user `toString` /
        // `[Symbol.toPrimitive]`. Each interpolation must therefore
        // be statically primitive-valued in addition to being pure
        // to evaluate.
        Expr::Tpl(tpl) => tpl
            .exprs
            .iter()
            .map(|e| {
                let p = classify_expr_purity(e, shadowed, local_shadowed, declared_pure, graph);
                if is_result_primitive(e, &graph.primitive_const_bindings, local_shadowed) {
                    p
                } else {
                    p.worst(Purity::from_reason_with_detail(
                        PurityRule::CoercingOperator,
                        e.span(),
                        "template interpolation runs ToString on a possibly-object value"
                            .to_string(),
                    ))
                }
            })
            .fold(Purity::Pure, Purity::worst),
        Expr::Fn(_) | Expr::Arrow(_) => Purity::Pure,
        Expr::Class(class_expr) => {
            if class_has_static_observable(
                &class_expr.class,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            ) {
                Purity::from_reason(PurityRule::ClassStaticObservable, class_expr.class.span)
            } else {
                Purity::Pure
            }
        }
        Expr::Paren(p) => {
            classify_expr_purity(&p.expr, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::Unary(u) => {
            let arg_purity =
                classify_expr_purity(&u.arg, shadowed, local_shadowed, declared_pure, graph);
            match u.op {
                UnaryOp::Delete => Purity::from_reason(PurityRule::DeleteOperator, u.span),
                // `typeof` has no coercion path; `void` discards;
                // `!` runs ToBoolean, which is type-cased and fires
                // no user code on any value (ECMA-262 §7.1.2).
                // Pure iff the operand is.
                UnaryOp::TypeOf | UnaryOp::Void | UnaryOp::Bang => arg_purity,
                // Unary `+` / `-` / `~` run ToNumber / ToNumeric —
                // on an object operand that fires user `valueOf` /
                // `[Symbol.toPrimitive]`. Pure only when the
                // operand is statically primitive-valued.
                UnaryOp::Plus | UnaryOp::Minus | UnaryOp::Tilde => {
                    if is_result_primitive(&u.arg, &graph.primitive_const_bindings, local_shadowed)
                    {
                        arg_purity
                    } else {
                        arg_purity.worst(Purity::from_reason_with_detail(
                            PurityRule::CoercingOperator,
                            u.span,
                            format!("unary {} runs ToNumber on a possibly-object operand", u.op),
                        ))
                    }
                }
            }
        }
        Expr::Bin(b) => {
            let operands =
                classify_expr_purity(&b.left, shadowed, local_shadowed, declared_pure, graph)
                    .worst(classify_expr_purity(
                        &b.right,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    ));
            match b.op {
                // No-coercion operators: short-circuit logicals
                // evaluate operands as-is; strict (in)equality is
                // type-cased with no ToPrimitive path (ECMA-262
                // §7.2.16 IsStrictlyEqual).
                BinaryOp::LogicalAnd
                | BinaryOp::LogicalOr
                | BinaryOp::NullishCoalescing
                | BinaryOp::EqEqEq
                | BinaryOp::NotEqEq => operands,
                // `in` runs ToPropertyKey on the LHS and HasProperty
                // on the RHS (proxy `has` trap); `instanceof` runs
                // GetMethod(RHS, @@hasInstance) and reads
                // `RHS.prototype` (proxy `get` trap). The
                // interesting RHS is always an object, so no
                // primitive-operand gate can admit these.
                BinaryOp::In | BinaryOp::InstanceOf => {
                    operands.worst(Purity::from_reason_with_detail(
                        PurityRule::CoercingOperator,
                        b.span,
                        format!(
                            "`{}` can fire proxy traps / @@hasInstance on its operand",
                            b.op
                        ),
                    ))
                }
                // Every remaining operator (arithmetic, relational,
                // loose equality, bitwise, shifts, `+`) coerces its
                // operands via ToPrimitive / ToNumeric, which fires
                // user `valueOf` / `toString` / `[Symbol.toPrimitive]`
                // on object operands. Pure only when both operands
                // are statically primitive-valued.
                _ => {
                    if is_result_primitive(&b.left, &graph.primitive_const_bindings, local_shadowed)
                        && is_result_primitive(
                            &b.right,
                            &graph.primitive_const_bindings,
                            local_shadowed,
                        )
                    {
                        operands
                    } else {
                        operands.worst(Purity::from_reason_with_detail(
                            PurityRule::CoercingOperator,
                            b.span,
                            format!(
                                "binary {} runs ToPrimitive on a possibly-object operand",
                                b.op
                            ),
                        ))
                    }
                }
            }
        }
        Expr::Cond(c) => {
            classify_expr_purity(&c.test, shadowed, local_shadowed, declared_pure, graph)
                .worst(classify_expr_purity(
                    &c.cons,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ))
                .worst(classify_expr_purity(
                    &c.alt,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ))
        }
        Expr::Seq(s) => s
            .exprs
            .iter()
            .map(|e| classify_expr_purity(e, shadowed, local_shadowed, declared_pure, graph))
            .fold(Purity::Pure, Purity::worst),
        Expr::Array(arr) => {
            classify_array_literal_purity(arr, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::Object(obj) => obj
            .props
            .iter()
            .map(|prop| classify_prop_purity(prop, shadowed, local_shadowed, declared_pure, graph))
            .fold(Purity::Pure, Purity::worst),
        Expr::Member(member) => {
            classify_member_purity(member, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::SuperProp(s) => Purity::from_reason(PurityRule::SuperProp, s.span),
        // Optional chaining (`recv?.prop`, `recv?.()`) only adds a
        // null/undefined short-circuit on top of plain member /
        // call evaluation; it doesn't introduce side effects of
        // its own. Recurse through the OptChainBase so an
        // OptChain that expands to a whitelisted static-property
        // read or a whitelisted call returns the same `Pure` /
        // `Unknown` answer the non-optional shape would. R1 in
        // docs/design.md "Open design questions / OptChain purity".
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(member) => {
                classify_member_purity(member, shadowed, local_shadowed, declared_pure, graph)
            }
            OptChainBase::Call(opt_call) => classify_callee_call(
                &opt_call.callee,
                &opt_call.args,
                opt.span,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            ),
        },
        Expr::Call(call) => {
            classify_call_purity(call, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::New(new_expr) => {
            classify_new_expr_purity(new_expr, shadowed, local_shadowed, declared_pure, graph)
        }
        Expr::TaggedTpl(t) => Purity::from_reason(PurityRule::TaggedTpl, t.span),
        Expr::Assign(a) => Purity::from_reason(PurityRule::AssignOrUpdate, a.span),
        Expr::Update(u) => Purity::from_reason(PurityRule::AssignOrUpdate, u.span),
        Expr::Await(a) => Purity::from_reason(PurityRule::AwaitOrYield, a.span),
        Expr::Yield(y) => Purity::from_reason(PurityRule::AwaitOrYield, y.span),
        // Anything we didn't enumerate falls into the Unknown
        // bucket — soundness-first.
        other => Purity::from_reason(PurityRule::Other, other.span()),
    }
}

fn classify_array_literal_purity(
    arr: &ArrayLit,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    arr.elems
        .iter()
        .flatten()
        .map(|elem| {
            classify_array_literal_element_purity(
                elem,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
        })
        .fold(Purity::Pure, Purity::worst)
}

fn classify_array_literal_element_purity(
    elem: &ExprOrSpread,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    if let Some(sp) = elem.spread {
        return classify_fresh_array_spread_source(
            &elem.expr,
            None,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )
        .unwrap_or_else(|| Purity::from_reason(PurityRule::ArraySpread, sp));
    }
    classify_expr_purity(&elem.expr, shadowed, local_shadowed, declared_pure, graph)
}

/// Array spread is only side-effect-free when the source is known to
/// evaluate to a fresh ordinary Array whose own element expressions
/// are pure. This admits literal and conditional-literal shapes like
/// `...[1, 2]` and `...(flag ? ["a"] : [])` while preserving the
/// conservative `array_spread` verdict for arbitrary iterables.
///
/// When `for_iterable` is `Some(callee)`, the Array arm classifies
/// elements for `new Set(...)` / `new Map(...)` iterable semantics
/// instead of generic array-literal purity.
pub(crate) fn classify_fresh_array_spread_source(
    expr: &Expr,
    for_iterable: Option<&str>,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Option<Purity> {
    match expr {
        Expr::Paren(p) => classify_fresh_array_spread_source(
            &p.expr,
            for_iterable,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ),
        Expr::Array(arr) => Some(if let Some(callee) = for_iterable {
            arr.elems
                .iter()
                .map(|elem| {
                    classify_iterable_element(
                        elem.as_ref(),
                        arr.span,
                        callee,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    )
                })
                .fold(Purity::Pure, Purity::worst)
        } else {
            classify_array_literal_purity(arr, shadowed, local_shadowed, declared_pure, graph)
        }),
        Expr::Cond(cond) => Some(
            classify_expr_purity(&cond.test, shadowed, local_shadowed, declared_pure, graph)
                .worst(classify_fresh_array_spread_source(
                    &cond.cons,
                    for_iterable,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                )?)
                .worst(classify_fresh_array_spread_source(
                    &cond.alt,
                    for_iterable,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                )?),
        ),
        _ => None,
    }
}

/// Purity of `member` taken as an r-value member access
/// (`recv.prop` or `recv?.prop`). Pure iff one of:
///
/// * The receiver+property pair is whitelisted on a non-shadowed
///   global (e.g. `Math.PI`, `Object.freeze`).
/// * The receiver is a chunk-local `Ident` bound to a confirmed
///   `ChunkBinding::PlainData` shape (see `ChunkCodeGraph::build` for
///   the soundness argument). For computed access, the key
///   sub-expression must itself be pure; non-computed (`X.k`) and
///   private-name (`X.#k`) reads are unconditionally pure.
///
/// Otherwise `Unknown` — `obj.prop` on an arbitrary object can fire
/// a getter, which we can't rule out statically.
fn classify_member_purity(
    member: &MemberExpr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    if let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && !local_shadowed.contains(recv)
        && (PURE_STATIC_PROPS.contains(&(recv, prop))
            || PURE_STATIC_FUNCTION_REFS.contains(&(recv, prop)))
    {
        return Purity::Pure;
    }
    if let Expr::Ident(recv) = member.obj.as_ref()
        && graph.is_plain_data(recv.sym.as_ref())
        // A function param / local binding of the same name lexically
        // shadows the chunk-top PlainData const, so `recv` here is the
        // local (possibly a getter-bearing object), not the plain-data
        // shape. Admitting it as pure would be a soundness hole.
        && !local_shadowed.contains(recv.sym.as_ref())
    {
        return match &member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => Purity::Pure,
            // Computed access runs ToPropertyKey on the key VALUE
            // before the (accessor-free) lookup on the PlainData
            // receiver — an object key fires user `toString` /
            // `[Symbol.toPrimitive]`. The key must therefore be
            // statically primitive-valued (or a well-known Symbol)
            // in addition to being pure to evaluate.
            MemberProp::Computed(computed) => {
                let key_purity = classify_expr_purity(
                    &computed.expr,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                );
                if is_safe_property_key(
                    &computed.expr,
                    shadowed,
                    local_shadowed,
                    &graph.primitive_const_bindings,
                ) {
                    key_purity
                } else {
                    key_purity.worst(Purity::from_reason_with_detail(
                        PurityRule::ToPropertyKeyCoercion,
                        computed.span,
                        "computed key runs ToPropertyKey on a possibly-object value".to_string(),
                    ))
                }
            }
        };
    }
    // Member read on a fluent chain (`k.string().description`,
    // including the bare-root `k.object` read inside a larger chain
    // when classified standalone): the author's `fluent_exports`
    // assertion covers member reads on the root and on every derived
    // value. Static (non-computed) properties only — a computed read
    // would additionally need a ToPropertyKey-safe key, so it falls
    // through to the conservative verdict. The chain verdict carries
    // any inner call-argument impurity.
    if let MemberProp::Ident(_) | MemberProp::PrivateName(_) = &member.prop
        && let Some(chain) =
            classify_fluent_chain(&member.obj, shadowed, local_shadowed, declared_pure, graph)
    {
        return chain;
    }
    let detail = match (member.obj.as_ref(), &member.prop) {
        (Expr::Ident(o), MemberProp::Ident(p)) => Some(format!("{}.{}", o.sym, p.sym)),
        _ => None,
    };
    Purity::NotPure {
        reasons: vec![PurityReason {
            rule: PurityRule::UnknownMember,
            span: member.span,
            source_location: None,
            detail,
        }],
    }
}

pub(crate) fn classify_call_purity(
    call: &CallExpr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Callee::Expr(callee_expr) = &call.callee else {
        return Purity::from_reason_with_detail(
            PurityRule::UnknownCall,
            call.span,
            "non-Expr callee (Super/Import)".to_string(),
        );
    };
    classify_callee_call(
        callee_expr,
        &call.args,
        call.span,
        shadowed,
        local_shadowed,
        declared_pure,
        graph,
    )
}

/// Classify `expr` as a fluent chain: `Some(purity)` when the
/// expression is a chain of static member reads / calls rooted in a
/// fluent-trusted binding (`ChunkCodeGraph::fluent_bindings`), `None`
/// when it isn't a fluent chain at all (caller falls through to the
/// regular arms).
///
/// The returned purity is the combined verdict of **every call
/// argument throughout the chain** — the author's assertion covers
/// the API's own functions and their results, not the evaluation of
/// caller-supplied arguments, so
/// `k.object(sideEffect()).describe("x")` must surface
/// `sideEffect()`'s impurity even though the chain shape is trusted.
/// Trust propagation mirrors `fluent_chain_root` exactly: static
/// members and calls only — a computed member or `new` breaks the
/// chain (falls back to the conservative classifier).
///
/// A body-local binding shadowing the root makes the called value a
/// different value than the one the author annotated, so the chain is
/// not trusted then (same rule as every other author-trust arm).
pub(crate) fn classify_fluent_chain(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Option<Purity> {
    match strip_parens(expr) {
        Expr::Ident(ident)
            if graph.is_fluent_binding(ident.sym.as_ref())
                && !local_shadowed.contains(ident.sym.as_ref()) =>
        {
            Some(Purity::Pure)
        }
        Expr::Member(member) => match &member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => {
                classify_fluent_chain(&member.obj, shadowed, local_shadowed, declared_pure, graph)
            }
            MemberProp::Computed(_) => None,
        },
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            classify_fluent_chain(callee, shadowed, local_shadowed, declared_pure, graph).map(
                |chain| {
                    chain.worst(all_args_pure(
                        &call.args,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    ))
                },
            )
        }
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(member) => match &member.prop {
                MemberProp::Ident(_) | MemberProp::PrivateName(_) => classify_fluent_chain(
                    &member.obj,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ),
                MemberProp::Computed(_) => None,
            },
            OptChainBase::Call(opt_call) => classify_fluent_chain(
                &opt_call.callee,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
            .map(|chain| {
                chain.worst(all_args_pure(
                    &opt_call.args,
                    shadowed,
                    local_shadowed,
                    declared_pure,
                    graph,
                ))
            }),
        },
        _ => None,
    }
}

/// Common backbone for `Expr::Call` and `OptChainBase::Call` —
/// classify a `callee_expr(args…)` invocation. Walks the same
/// whitelists and chunk-local function-body purity cache the
/// regular call classifier consults; the only difference between
/// `Expr::Call` and `OptChainBase::Call` is the null-coalesce
/// short-circuit on the optional form, which is irrelevant for
/// side-effect classification.
fn classify_callee_call(
    callee_expr: &Expr,
    args: &[ExprOrSpread],
    call_span: Span,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    // Author-declared pure binding: a chunk-local function whose
    // spec member carries `purity: "pure"`. The annotation is an
    // explicit override and wins over both the whitelist and the
    // chunk-top (A8) shadowing check — the spec author asserts that
    // THIS bound value is pure regardless of what its body does or
    // whether an import shadows the name. It does NOT win over a
    // body-local binding of the same name (`local_shadowed`): a
    // function param or local var is a *different value* than the
    // chunk-top binding the author annotated, so the trust contract
    // doesn't cover it. See AGENTS.md "Declared purity".
    if let Expr::Ident(ident) = callee_expr
        && declared_pure.contains(ident.sym.as_ref())
        && !local_shadowed.contains(ident.sym.as_ref())
    {
        return all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
    }
    // Author-declared pure member call: a binding whose spec member
    // carries `pure_members: [<prop>, …]`. Admits
    // `<binding>.<prop>(args)` as pure with args still classified
    // independently. Static identifier-property only — computed
    // access (`<binding>[expr](...)`) and private fields fall back
    // to the regular classifier path. Both the call-then-opt form
    // (`b?.forwardRef(args)`, callee = `Expr::OptChain(Member)`)
    // and the opt-call form (`b.forwardRef?.(args)`, callee =
    // `Expr::Member`) qualify under the same admission rule via
    // `static_member_obj_prop`. As with `purity: pure` on a
    // direct-Ident call, the annotation overrides chunk-top (A8)
    // shadowing — the spec author asserts THIS bound value is pure
    // regardless of where it came from — but NOT a body-local
    // binding of the same name (a param/local is a different value
    // than the annotated chunk-top binding). See AGENTS.md
    // "Declared purity".
    if let Some((recv, prop)) = static_member_obj_prop(callee_expr)
        && !local_shadowed.contains(recv)
        && graph.is_declared_pure_member(recv, prop)
    {
        return all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
    }
    // Fluent chain rooted in an author-asserted `fluent_exports`
    // binding: the callee may be arbitrarily deep
    // (`k.object({...}).optional().describe`), where every
    // intermediate receiver is a call *result* — unreachable by any
    // binding-keyed arm above. `classify_fluent_chain` validated and
    // carried every inner call's argument purity; this call's own
    // args are still classified normally.
    if let Some(chain) =
        classify_fluent_chain(callee_expr, shadowed, local_shadowed, declared_pure, graph)
    {
        return chain.worst(all_args_pure(
            args,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ));
    }
    // Vite/Rollup namespace facade:
    // `Object.defineProperty({ __proto__: null, ...exports },
    // Symbol.toStringTag, { value: "Module" })`. The mutation targets a
    // fresh object and installs a data descriptor only, so no user code
    // fires. The generic `Object.defineProperty(t, ...)` form remains
    // unknown below.
    if is_pure_object_define_property_on_fresh_namespace(
        callee_expr,
        args,
        shadowed,
        local_shadowed,
        declared_pure,
        graph,
    ) {
        return Purity::Pure;
    }
    // Chunk-local function declaration: consult the per-chunk
    // function-body purity cache. `Pure` callee + Pure args → Pure;
    // non-Pure callee inherits its reasons (so the chain points
    // back through to the unhandled construct in the function body).
    // A body-local binding of the same name shadows the chunk-top
    // function — the called value is unknown then.
    if let Expr::Ident(ident) = callee_expr
        && !local_shadowed.contains(ident.sym.as_ref())
        && let Some(callee_purity) = graph.function_purity(ident.sym.as_ref())
    {
        return callee_purity.worst(all_args_pure(
            args,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ));
    }
    // Imported function with a cross-module purity verdict from the
    // program-level oracle (`imported_purities`). Same shape as the
    // chunk-local arm: a `Pure` import + Pure args → Pure; an impure
    // import inherits its reasons. A body-local binding of the same name
    // shadows the import, so the called value is unknown then.
    if let Expr::Ident(ident) = callee_expr
        && !local_shadowed.contains(ident.sym.as_ref())
        && let Some(callee_purity) = graph.imported_purity(ident.sym.as_ref())
    {
        return callee_purity.worst(all_args_pure(
            args,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        ));
    }
    // `Recv.method(args)` against PURE_STATIC_CALLS.
    if let Expr::Member(member) = callee_expr
        && let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && !local_shadowed.contains(recv)
        && PURE_STATIC_CALLS.contains(&(recv, prop))
    {
        return all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
    }
    // `Object.{entries,keys,values,freeze}(<plain-data arg>)` /
    // `Object.fromEntries(<entry-array literal>)` — admitted when
    // the argument is structurally a plain ordinary literal (no
    // accessors / methods / `__proto__` / computed keys / spread)
    // OR a chunk-top `PlainData` binding whose accessor-free shape
    // is enforced by `collect_plain_data_bindings` /
    // `PlainDataWriteScanner`. See `PURE_OBJECT_CALLS_ON_PLAIN_DATA`
    // for the per-entry soundness argument.
    if let Expr::Member(member) = callee_expr
        && let Some((recv, prop)) = static_member_pair(member)
        && !shadowed.contains(recv)
        && !local_shadowed.contains(recv)
        && PURE_OBJECT_CALLS_ON_PLAIN_DATA.contains(&(recv, prop))
        && args.len() == 1
        && args[0].spread.is_none()
        && is_pure_plain_data_arg_for(
            prop,
            &args[0].expr,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )
    {
        return Purity::Pure;
    }
    // `globalCallable(args)` against PURE_GLOBAL_CALLS.
    if let Expr::Ident(ident) = callee_expr
        && let Some(name) = PURE_GLOBAL_CALLS
            .iter()
            .copied()
            .find(|n| *n == ident.sym.as_ref())
        && !shadowed.contains(name)
        && !local_shadowed.contains(name)
    {
        return all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
    }
    // `globalCallable(prim_lit, …)` against
    // PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS. Every argument
    // must be a `Lit::Str/Num/Bool/Null/BigInt` (no spread, no
    // computed sub-expression). On a match the call has no
    // observable side effects (per the per-entry soundness
    // notes on the constant); if any arg is non-literal the
    // rule doesn't fire and we fall through to `Unknown`.
    if let Expr::Ident(ident) = callee_expr
        && let Some(name) = PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS
            .iter()
            .copied()
            .find(|n| *n == ident.sym.as_ref())
        && !shadowed.contains(name)
        && !local_shadowed.contains(name)
        && args
            .iter()
            .all(|arg| arg.spread.is_none() && is_primitive_literal(&arg.expr))
    {
        return Purity::Pure;
    }
    let detail = callee_summary(callee_expr);
    Purity::NotPure {
        reasons: vec![PurityReason {
            rule: PurityRule::UnknownCall,
            span: call_span,
            source_location: None,
            detail,
        }],
    }
}

fn callee_summary(callee_expr: &Expr) -> Option<String> {
    match callee_expr {
        Expr::Ident(ident) => Some(ident.sym.to_string()),
        Expr::Member(m) => match (m.obj.as_ref(), &m.prop) {
            (Expr::Ident(o), MemberProp::Ident(p)) => Some(format!("{}.{}", o.sym, p.sym)),
            _ => None,
        },
        _ => None,
    }
}

pub(crate) fn all_args_pure(
    args: &[ExprOrSpread],
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    args.iter()
        .map(|arg| {
            // Spread arg's iterator could fire side effects.
            let spread = arg
                .spread
                .map(|sp| Purity::from_reason(PurityRule::ArraySpread, sp));
            let body =
                classify_expr_purity(&arg.expr, shadowed, local_shadowed, declared_pure, graph);
            spread.unwrap_or(Purity::Pure).worst(body)
        })
        .fold(Purity::Pure, Purity::worst)
}

fn classify_prop_purity(
    prop: &PropOrSpread,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match prop {
        PropOrSpread::Spread(spread) => {
            // Spreading an arbitrary expression invokes its
            // iterator (array spread) or property iteration
            // (object spread). Either can fire a getter or a
            // user-defined `[Symbol.iterator]`.
            classify_expr_purity(&spread.expr, shadowed, local_shadowed, declared_pure, graph)
                .worst(Purity::from_reason(
                    PurityRule::ObjectSpread,
                    spread.expr.span(),
                ))
        }
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::Shorthand(_) => Purity::Pure,
            Prop::KeyValue(kv) => {
                classify_propname_purity(&kv.key, shadowed, local_shadowed, declared_pure, graph)
                    .worst(classify_expr_purity(
                        &kv.value,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    ))
            }
            Prop::Assign(a) => Purity::from_reason(PurityRule::ObjectAssignProp, a.key.span),
            // `{ get x() {}, set x(v) {}, m() {} }` — defining a
            // method or accessor is pure; invoking it is not, and
            // we don't invoke it during init.
            Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) => Purity::Pure,
        },
    }
}

fn classify_propname_purity(
    name: &PropName,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    match name {
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::BigInt(_) => {
            Purity::Pure
        }
        // Defining a property under a computed key runs
        // ToPropertyKey on the key value — an object key fires user
        // `toString` / `[Symbol.toPrimitive]`. The key must be
        // statically primitive-valued (or a well-known Symbol) in
        // addition to being pure to evaluate.
        PropName::Computed(c) => {
            let key_purity =
                classify_expr_purity(&c.expr, shadowed, local_shadowed, declared_pure, graph);
            if is_safe_property_key(
                &c.expr,
                shadowed,
                local_shadowed,
                &graph.primitive_const_bindings,
            ) {
                key_purity
            } else {
                key_purity.worst(Purity::from_reason_with_detail(
                    PurityRule::ToPropertyKeyCoercion,
                    c.span,
                    "computed key runs ToPropertyKey on a possibly-object value".to_string(),
                ))
            }
        }
    }
}

/// Whether a class declaration runs observable code at class-decl
/// time. Eagerly-evaluated class parts (aligned with the fact
/// collector's `visit_eager_member_parts` / `visit_class_decl`):
///
/// * **decorators** (class-level or member-level) — decorator
///   application CALLS the decorator function at class-definition
///   time; any decorator present is observable.
/// * **`extends <expr>`** — the superclass expression evaluates at
///   class-definition time; an impure expression (e.g.
///   `extends f()`) is observable. A pure-classified expression
///   (Ident etc.) is admitted: reading `.prototype` off a plain
///   constructor fires no user code (function `prototype` is an
///   own data property; the Proxy-superclass case falls under
///   assumption A11, intrinsic/exotic-object integrity).
/// * **computed member keys** (any member kind, static or not) —
///   key expressions evaluate eagerly AND their values undergo
///   ToPropertyKey (user `toString` on object keys), so the key
///   must be pure and statically primitive-valued (or a well-known
///   `Symbol.*`).
/// * **static field / static auto-accessor initializers** — run at
///   class-definition time; impure initializer is observable.
///
/// Static blocks always run. Instance field/accessor initializers,
/// constructor and method bodies are lazy and not consulted.
pub(crate) fn class_has_static_observable(
    class: &Class,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let value_impure = |v: &Expr| {
        !classify_expr_purity(v, shadowed, local_shadowed, declared_pure, graph).is_pure()
    };
    let key_observable = |key: &PropName| match key {
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::BigInt(_) => false,
        PropName::Computed(c) => {
            value_impure(&c.expr)
                || !is_safe_property_key(
                    &c.expr,
                    shadowed,
                    local_shadowed,
                    &graph.primitive_const_bindings,
                )
        }
    };
    if !class.decorators.is_empty() {
        return true;
    }
    if let Some(super_class) = class.super_class.as_deref()
        && value_impure(super_class)
    {
        return true;
    }
    class.body.iter().any(|member| match member {
        ClassMember::StaticBlock(_) => true,
        ClassMember::Method(method) => {
            !method.function.decorators.is_empty() || key_observable(&method.key)
        }
        ClassMember::PrivateMethod(method) => !method.function.decorators.is_empty(),
        ClassMember::Constructor(_) => false,
        ClassMember::ClassProp(prop) => {
            !prop.decorators.is_empty()
                || key_observable(&prop.key)
                || (prop.is_static && prop.value.as_deref().is_some_and(value_impure))
        }
        ClassMember::PrivateProp(prop) => {
            !prop.decorators.is_empty()
                || (prop.is_static && prop.value.as_deref().is_some_and(value_impure))
        }
        ClassMember::AutoAccessor(accessor) => {
            !accessor.decorators.is_empty()
                || (match &accessor.key {
                    Key::Public(name) => key_observable(name),
                    Key::Private(_) => false,
                })
                || (accessor.is_static && accessor.value.as_deref().is_some_and(value_impure))
        }
        ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => false,
    })
}
