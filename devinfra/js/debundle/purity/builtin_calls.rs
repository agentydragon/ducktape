use super::*;

/// Borrow the `(recv_ident_sym, prop_sym)` pair for a static-ident
/// member access spelled as either `recv.prop` (`Expr::Member`) or
/// `recv?.prop` (`Expr::OptChain { base: OptChainBase::Member }`).
/// Returns `None` for any other shape (chained member access,
/// computed access, non-Ident receivers, private names, OptCall
/// bases). The returned references borrow from the AST node and
/// outlive only the surrounding match — short-lived by design,
/// because the `pure_members` admission only needs the strings to
/// look up the per-binding declared-pure set.
pub(crate) fn static_member_obj_prop(expr: &Expr) -> Option<(&str, &str)> {
    let member = match expr {
        Expr::Member(member) => member,
        Expr::OptChain(opt) => match opt.base.as_ref() {
            OptChainBase::Member(member) => member,
            OptChainBase::Call(_) => return None,
        },
        _ => return None,
    };
    let Expr::Ident(recv) = member.obj.as_ref() else {
        return None;
    };
    let MemberProp::Ident(prop) = &member.prop else {
        return None;
    };
    Some((recv.sym.as_ref(), prop.sym.as_ref()))
}

/// `(receiver_ident, prop_name)` for `Receiver.prop` where
/// `Receiver` is a plain `Ident` and `prop` is a static name.
/// Returns `None` for computed access (`obj[k]`), private fields,
/// or non-Ident receivers.
pub(crate) fn static_member_pair(member: &MemberExpr) -> Option<(&'static str, &'static str)> {
    let recv_sym = match member.obj.as_ref() {
        Expr::Ident(ident) => ident.sym.as_ref(),
        _ => return None,
    };
    let prop_sym = match &member.prop {
        MemberProp::Ident(ident) => ident.sym.as_ref(),
        _ => return None,
    };
    let recv = WHITELIST_RECEIVERS
        .iter()
        .copied()
        .find(|r| *r == recv_sym)?;
    // `prop_sym` may be borrowed from the AST; intern via the
    // whitelist tables so we return `&'static str` for downstream
    // `contains` checks.
    let prop = PURE_STATIC_PROPS
        .iter()
        .chain(PURE_STATIC_FUNCTION_REFS.iter())
        .chain(PURE_STATIC_CALLS.iter())
        .find_map(|(r, p)| (*r == recv && *p == prop_sym).then_some(*p))?;
    Some((recv, prop))
}

/// Purity of a `new X(args…)` expression. Matches:
///   * No-arg form against `PURE_BUILTIN_NEW_NO_ARGS`.
///   * 1-arg Array-literal-iterable form against
///     `PURE_BUILTIN_NEW_ARRAY_ITERABLE` (`Set` / `Map`).
///   * Spec-declared `purity: pure_new` bindings, with all args pure.
///
/// Everything else (non-Ident callees, shadowed names, tagged
/// templates, other arg shapes) falls through to `Unknown`.
pub(crate) fn classify_new_expr_purity(
    new_expr: &NewExpr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Expr::Ident(callee) = new_expr.callee.as_ref() else {
        return Purity::from_reason_with_detail(
            PurityRule::UnknownNew,
            new_expr.span,
            "non-ident callee".to_string(),
        );
    };
    let arg_count = new_expr.args.as_ref().map_or(0, Vec::len);
    if let Some(name) = PURE_BUILTIN_NEW_NO_ARGS
        .iter()
        .copied()
        .find(|n| *n == callee.sym.as_ref())
        && !shadowed.contains(name)
        && !local_shadowed.contains(name)
        && arg_count == 0
    {
        return Purity::Pure;
    }
    // `new X("literal")` against PURE_BUILTIN_NEW_STRING_LITERAL_ARG.
    // The argument must be a string LITERAL (no spread): the
    // admission arguments on the table are about parsing a string —
    // a non-literal expression would additionally need value-class
    // tracking to prove it can't be an object whose ToString fires
    // user code.
    if let Some(name) = PURE_BUILTIN_NEW_STRING_LITERAL_ARG
        .iter()
        .copied()
        .find(|n| *n == callee.sym.as_ref())
        && !shadowed.contains(name)
        && !local_shadowed.contains(name)
        && let Some(args) = new_expr.args.as_ref()
        && args.len() == 1
        && args[0].spread.is_none()
        && matches!(args[0].expr.as_ref(), Expr::Lit(Lit::Str(_)))
    {
        return Purity::Pure;
    }
    if let Some(name) = PURE_BUILTIN_NEW_ARRAY_ITERABLE
        .iter()
        .copied()
        .find(|n| *n == callee.sym.as_ref())
        && !shadowed.contains(name)
        && !local_shadowed.contains(name)
        && let Some(args) = new_expr.args.as_ref()
        && args.len() == 1
        && args[0].spread.is_none()
    {
        // Recurse through the array literal; if every element
        // classifies as Pure, the `new <Container>([...])` is
        // Pure. If not, return the failing elements' reasons
        // alongside an UnknownNew umbrella reason so the
        // diagnostic reports both the rule that gated the
        // constructor *and* which sub-expression(s) failed.
        let inner = classify_array_literal_for_iterable(
            &args[0].expr,
            name,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        );
        if inner.is_pure() {
            return Purity::Pure;
        }
        return Purity::from_reason_with_detail(
            PurityRule::UnknownNew,
            new_expr.span,
            format!("new {name}([...]) iterable arg has impure element(s)"),
        )
        .worst(inner);
    }
    // `pure_new` is an author trust contract on the chunk-top
    // binding; a body-local binding of the same name is a different
    // value the contract doesn't cover.
    if !local_shadowed.contains(callee.sym.as_ref())
        && graph.is_declared_pure_new(callee.sym.as_ref())
    {
        let args = new_expr.args.as_deref().unwrap_or(&[]);
        let arg_purity = all_args_pure(args, shadowed, local_shadowed, declared_pure, graph);
        if arg_purity.is_pure() {
            return Purity::Pure;
        }
        return Purity::from_reason_with_detail(
            PurityRule::UnknownNew,
            new_expr.span,
            format!(
                "new {}(...) has impure argument(s) despite pure_new annotation",
                callee.sym
            ),
        )
        .worst(arg_purity);
    }
    Purity::from_reason_with_detail(
        PurityRule::UnknownNew,
        new_expr.span,
        callee.sym.to_string(),
    )
}

/// Classify the iterable arg of `new Set([...])` / `new Map([[k,v],...])`.
/// Returns `Purity::Pure` only when the arg is an Array literal with
/// every element a Pure expression (no holes; spreads only when the
/// source is a fresh Array literal, optionally behind a pure
/// conditional; for `Map`, every expanded element is a 2-element
/// Array literal of Pure entries). Map's iterator path Get's [0]/[1]
/// on each entry — fresh literal entries guarantee those reads are
/// own-data-property hits, not user-getter hits on a 2-tuple-shaped
/// object.
///
/// On failure returns a `NotPure` carrying the offending sub-expression's
/// reason(s) so the caller can attach them to the surrounding
/// `UnknownNew` verdict.
fn classify_array_literal_for_iterable(
    expr: &Expr,
    callee: &str,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Expr::Array(arr) = expr else {
        return Purity::from_reason_with_detail(
            PurityRule::Other,
            expr.span(),
            format!("new {callee} arg is not an Array literal"),
        );
    };
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
}

pub(crate) fn classify_iterable_element(
    elem: Option<&ExprOrSpread>,
    arr_span: Span,
    callee: &str,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Some(elem) = elem else {
        // Hole: `[1, , 3]`. Set treats hole as undefined (still a
        // value); Map's `Get(undefined, "0")` throws. Reject for both.
        return Purity::from_reason_with_detail(
            PurityRule::Other,
            arr_span,
            format!("new {callee} array-literal arg has hole"),
        );
    };
    if let Some(sp) = elem.spread {
        return classify_fresh_array_spread_source(
            &elem.expr,
            Some(callee),
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )
        .unwrap_or_else(|| Purity::from_reason(PurityRule::ArraySpread, sp));
    }
    match callee {
        "Set" => classify_expr_purity(&elem.expr, shadowed, local_shadowed, declared_pure, graph),
        "Map" => classify_map_entry(&elem.expr, shadowed, local_shadowed, declared_pure, graph),
        _ => Purity::from_reason_with_detail(
            PurityRule::Other,
            elem.expr.span(),
            format!("unsupported callee {callee} for array-iterable rule"),
        ),
    }
}

fn classify_map_entry(
    entry_expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> Purity {
    let Expr::Array(entry) = entry_expr else {
        return Purity::from_reason_with_detail(
            PurityRule::Other,
            entry_expr.span(),
            "new Map entry is not an Array literal".to_string(),
        );
    };
    if entry.elems.len() != 2 {
        return Purity::from_reason_with_detail(
            PurityRule::Other,
            entry.span,
            "new Map entry is not a 2-element Array".to_string(),
        );
    }
    entry
        .elems
        .iter()
        .map(|e| match e {
            None => Purity::from_reason_with_detail(
                PurityRule::Other,
                entry.span,
                "new Map entry has hole".to_string(),
            ),
            Some(e) if e.spread.is_some() => {
                Purity::from_reason(PurityRule::ArraySpread, e.spread.unwrap())
            }
            Some(e) => {
                classify_expr_purity(&e.expr, shadowed, local_shadowed, declared_pure, graph)
            }
        })
        .fold(Purity::Pure, Purity::worst)
}

pub(crate) fn is_pure_object_define_property_on_fresh_namespace(
    callee_expr: &Expr,
    args: &[ExprOrSpread],
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let Expr::Member(member) = strip_parens(callee_expr) else {
        return false;
    };
    if !matches!(
        (member.obj.as_ref(), &member.prop),
        (Expr::Ident(obj), MemberProp::Ident(prop))
            if obj.sym.as_ref() == "Object" && prop.sym.as_ref() == "defineProperty"
    ) || shadowed.contains("Object")
        || local_shadowed.contains("Object")
        || args.len() != 3
        || args.iter().any(|arg| arg.spread.is_some())
    {
        return false;
    }
    is_fresh_namespace_object_literal(
        &args[0].expr,
        shadowed,
        local_shadowed,
        declared_pure,
        graph,
    ) && is_symbol_to_string_tag(&args[1].expr, shadowed, local_shadowed)
        && is_data_descriptor_literal(
            &args[2].expr,
            shadowed,
            local_shadowed,
            declared_pure,
            graph,
        )
}

fn is_fresh_namespace_object_literal(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let Expr::Object(obj) = strip_parens(expr) else {
        return false;
    };
    obj.props.iter().all(|prop| match prop {
        PropOrSpread::Spread(_) => false,
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::Shorthand(_) => true,
            Prop::KeyValue(kv) => {
                if prop_name_is(&kv.key, "__proto__") {
                    return matches!(strip_parens(&kv.value), Expr::Lit(Lit::Null(_)));
                }
                prop_name_is_static_data_key(&kv.key)
                    && classify_expr_purity(
                        &kv.value,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    )
                    .is_pure()
            }
            Prop::Getter(_) | Prop::Setter(_) | Prop::Method(_) | Prop::Assign(_) => false,
        },
    })
}

fn is_symbol_to_string_tag(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
) -> bool {
    let Expr::Member(member) = strip_parens(expr) else {
        return false;
    };
    matches!(
        (member.obj.as_ref(), &member.prop),
        (Expr::Ident(obj), MemberProp::Ident(prop))
            if obj.sym.as_ref() == "Symbol"
                && prop.sym.as_ref() == "toStringTag"
                && !shadowed.contains("Symbol")
                && !local_shadowed.contains("Symbol")
    )
}

fn is_data_descriptor_literal(
    expr: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let Expr::Object(obj) = strip_parens(expr) else {
        return false;
    };
    obj.props.iter().all(|prop| match prop {
        PropOrSpread::Spread(_) => false,
        PropOrSpread::Prop(prop) => match prop.as_ref() {
            Prop::KeyValue(kv) => {
                prop_name_is_static_data_key(&kv.key)
                    && !prop_name_is(&kv.key, "get")
                    && !prop_name_is(&kv.key, "set")
                    && classify_expr_purity(
                        &kv.value,
                        shadowed,
                        local_shadowed,
                        declared_pure,
                        graph,
                    )
                    .is_pure()
            }
            Prop::Shorthand(_)
            | Prop::Getter(_)
            | Prop::Setter(_)
            | Prop::Method(_)
            | Prop::Assign(_) => false,
        },
    })
}

fn prop_name_is_static_data_key(name: &PropName) -> bool {
    matches!(
        name,
        PropName::Ident(_) | PropName::Str(_) | PropName::Num(_) | PropName::BigInt(_)
    )
}

fn prop_name_is(name: &PropName, expected: &str) -> bool {
    match name {
        PropName::Ident(ident) => ident.sym.as_ref() == expected,
        PropName::Str(s) => s.value.to_string_lossy() == expected,
        PropName::Num(_) | PropName::BigInt(_) | PropName::Computed(_) => false,
    }
}

/// Whether `arg` is a sound argument shape for the
/// `PURE_OBJECT_CALLS_ON_PLAIN_DATA` admission rule at the given
/// `Object.<prop>` callsite. Two admissible shapes:
///
/// * **Fresh plain-data literal.** An `Expr::Object` with only
///   `Prop::KeyValue` / `Prop::Shorthand` (no `__proto__`, computed
///   keys, methods, getters, setters, or `Prop::Assign`), or an
///   `Expr::Array` (literal). The literal itself must classify
///   pure — that catches impure value sub-expressions and spreads
///   of non-plain-data sources (the existing
///   `classify_array_spread_source` gate). For `Object.fromEntries`
///   the Array form is required AND each element must itself be a
///   2-element Array literal with pure values, paralleling the
///   `new Map([[k, v], …])` gate.
///
/// * **`PlainData` chunk-top binding.** A bare `Expr::Ident` whose
///   `name` is registered as `ChunkBinding::PlainData` in the
///   chunk graph. `collect_plain_data_bindings` only registers
///   bindings whose initializers pass `is_plain_data_init` (same
///   plain-data shape predicate) AND whose chunk-wide write scan
///   ruled out accessor installation post-init. So a PlainData
///   binding carries the same "no accessor channels at any
///   program point" invariant as a fresh literal.
///
/// Returning `false` falls back to `Unknown` — the soundness-first
/// default.
pub(crate) fn is_pure_plain_data_arg_for(
    prop: &str,
    arg: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let arg = strip_parens(arg);
    if prop == "fromEntries" {
        // `Object.fromEntries(I)` iterates I. Restrict I to a fresh
        // Array literal whose every element is a 2-element Array
        // literal with pure values — same admission shape as
        // `new Map([[k, v], …])` (PURE_BUILTIN_NEW_ARRAY_ITERABLE).
        return matches!(arg, Expr::Array(_))
            && is_fresh_entry_array_for_from_entries(
                arg,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            );
    }
    match arg {
        // PlainData chunk-top binding read: provably plain-data
        // shape, no accessor channels anywhere in the chunk — unless a
        // local binding of the same name lexically shadows it, in which
        // case `ident` is the local (possibly getter-bearing) value.
        Expr::Ident(ident) => {
            graph.is_plain_data(ident.sym.as_ref()) && !local_shadowed.contains(ident.sym.as_ref())
        }
        // Fresh object literal with no accessor channels.
        Expr::Object(obj) => {
            obj.props.iter().all(is_plain_data_prop)
                && classify_expr_purity(arg, shadowed, local_shadowed, declared_pure, graph)
                    .is_pure()
        }
        // Fresh array literal; element purity (including spread
        // sources) is handled by the standard literal classifier.
        Expr::Array(_) => {
            classify_expr_purity(arg, shadowed, local_shadowed, declared_pure, graph).is_pure()
        }
        Expr::Call(call) if prop == "freeze" => {
            let Callee::Expr(callee) = &call.callee else {
                return false;
            };
            is_pure_object_define_property_on_fresh_namespace(
                callee,
                &call.args,
                shadowed,
                local_shadowed,
                declared_pure,
                graph,
            )
        }
        _ => false,
    }
}

/// `Object.fromEntries([[k1, v1], [k2, v2], …])` admission shape.
/// Every outer element must be a 2-element Array literal whose
/// element expressions classify pure (key + value). No spreads at
/// either level: a spread fires the iterable's `[Symbol.iterator]`
/// which can call user code outside the literal-only domain we
/// claim sound here. Matches the
/// `PURE_BUILTIN_NEW_ARRAY_ITERABLE`-style entry test for `Map`.
fn is_fresh_entry_array_for_from_entries(
    arg: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    declared_pure: &BTreeSet<String>,
    graph: &ChunkCodeGraph,
) -> bool {
    let Expr::Array(outer) = arg else {
        return false;
    };
    outer.elems.iter().all(|elem| {
        let Some(elem) = elem else {
            return false;
        };
        if elem.spread.is_some() {
            return false;
        }
        let Expr::Array(entry) = strip_parens(&elem.expr) else {
            return false;
        };
        if entry.elems.len() != 2 {
            return false;
        }
        entry.elems.iter().flatten().all(|kv| {
            kv.spread.is_none()
                && classify_expr_purity(&kv.expr, shadowed, local_shadowed, declared_pure, graph)
                    .is_pure()
        })
    })
}

/// True for AST nodes whose evaluation produces a primitive
/// value with no user-code side effects: string/number/boolean/
/// null/bigint literals only. `Lit::Regex` is excluded because
/// it produces a fresh `RegExp` *object* (not a primitive), and
/// would fail the "no user code on coercion" admission contract
/// of `PURE_GLOBAL_CALLS_WITH_PRIMITIVE_ARGS`. `Expr::Tpl` is
/// excluded even when it has zero interpolations because it's
/// not a `Lit::Str` AST shape — a future refinement could add
/// it.
pub(crate) fn is_primitive_literal(expr: &Expr) -> bool {
    matches!(
        expr,
        Expr::Lit(Lit::Str(_))
            | Expr::Lit(Lit::Bool(_))
            | Expr::Lit(Lit::Null(_))
            | Expr::Lit(Lit::Num(_))
            | Expr::Lit(Lit::BigInt(_)),
    )
}

/// Whether evaluating `expr` is statically guaranteed to produce a
/// **primitive** value, so a parent operator's ToPrimitive /
/// ToString / ToNumber / ToPropertyKey coercion of the RESULT
/// cannot fire user code. This predicate only answers "is the
/// resulting value primitive?" — the operand's own evaluation
/// effects are classified separately by the regular purity
/// recursion.
///
/// Admitted result-primitive shapes:
/// * primitive literals (string / number / boolean / null / bigint;
///   regex literals are objects and excluded);
/// * template literals (always evaluate to a string);
/// * any unary operator except `delete` (`typeof` → string, `void`
///   → undefined, `!` → boolean, `+`/`-`/`~` → number/bigint);
/// * any binary operator except the short-circuit logicals
///   (`&&` / `||` / `??` return one of their operands verbatim;
///   everything else — arithmetic, relational, equality, bitwise,
///   `in`, `instanceof` — returns a number/string/bigint/boolean);
/// * conditionals whose both branches are result-primitive;
/// * a reference to a chunk-top `const` provably bound to a
///   primitive value (`primitives`, from
///   `collect_primitive_const_bindings`), when the name is not
///   locally shadowed.
///
/// The global `undefined` is deliberately NOT admitted: it is an
/// ordinary (shadowable) global binding, not a keyword, and the
/// shadowed-globals pass doesn't track it. Over-restriction is the
/// acceptable failure mode.
///
/// No admitted shape can produce a Symbol, so ToString on a
/// result-primitive value never hits the symbol TypeError path.
pub(crate) fn is_result_primitive(
    expr: &Expr,
    primitives: &BTreeSet<String>,
    local_shadowed: &BTreeSet<String>,
) -> bool {
    match strip_parens(expr) {
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_)) => true,
        Expr::Tpl(_) => true,
        // A chunk-top `const` provably bound to a primitive value: the
        // binding is immutable and the value carries no user accessors,
        // so coercing it fires no user code — unless a local binding
        // shadows the name in the current scope.
        Expr::Ident(id) => {
            primitives.contains(id.sym.as_ref()) && !local_shadowed.contains(id.sym.as_ref())
        }
        Expr::Unary(u) => u.op != UnaryOp::Delete,
        Expr::Bin(b) => !matches!(
            b.op,
            BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing
        ),
        Expr::Cond(c) => {
            is_result_primitive(&c.cons, primitives, local_shadowed)
                && is_result_primitive(&c.alt, primitives, local_shadowed)
        }
        _ => false,
    }
}

/// Whether `key` is a safe computed property key: ToPropertyKey on
/// its value fires no user code. True for result-primitive
/// expressions (string/number coercion of a primitive is
/// engine-only) and for whitelisted well-known-symbol reads
/// (`Symbol.iterator` etc. — Symbols are valid property keys with
/// no coercion path), provided neither the global `Symbol` nor a
/// local binding shadows the receiver.
pub(crate) fn is_safe_property_key(
    key: &Expr,
    shadowed: &BTreeSet<&'static str>,
    local_shadowed: &BTreeSet<String>,
    primitives: &BTreeSet<String>,
) -> bool {
    if is_result_primitive(key, primitives, local_shadowed) {
        return true;
    }
    if let Expr::Member(member) = strip_parens(key)
        && let Some((recv, prop)) = static_member_pair(member)
        && recv == "Symbol"
        && !shadowed.contains(recv)
        && !local_shadowed.contains(recv)
        && PURE_STATIC_PROPS.contains(&(recv, prop))
    {
        return true;
    }
    false
}
