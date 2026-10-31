//! Primitive types of operator results, of the expressions built from them,
//! and of chunk-top bindings.
//!
//! Every result is the set of types the value can have when evaluation
//! completes; `None` means it may be an object. Throwing is not modeled here:
//! an operation that always throws on its operands yields the empty set, and
//! callers keep classifying the evaluation's own effects and throws.

use std::collections::{BTreeMap, BTreeSet};

use super::builtins::{self, ArgSel, Invocation, Return, Row};
use super::value::PrimKinds;
use crate::facts::{TopLevelItemView, top_level_initializers, var_decl_of_item};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

/// Where `evaluate` reads identifiers.
pub trait PrimScope {
    /// The types of the binding `ident` resolves to, when they are tracked.
    fn binding(&self, ident: &Ident) -> Option<PrimKinds>;

    /// Whether `global` (with `member`, `global.member`) names the intact
    /// intrinsic `row` summarizes.
    fn intact_intrinsic(&self, row: &Row, global: &Ident, member: Option<&str>) -> bool;
}

/// A scope that tracks no binding and no intrinsic.
pub struct NoBindings;

impl PrimScope for NoBindings {
    fn binding(&self, _: &Ident) -> Option<PrimKinds> {
        None
    }

    fn intact_intrinsic(&self, _: &Row, _: &Ident, _: Option<&str>) -> bool {
        false
    }
}

const NOT_NUMERIC: PrimKinds = PrimKinds::ANY
    .without(PrimKinds::BIGINT)
    .without(PrimKinds::SYMBOL);

/// `ToNumeric` (ECMA-262 § 7.1.3) after `ToPrimitive`: an object operand can
/// produce any primitive.
fn to_numeric(operand: Option<PrimKinds>) -> PrimKinds {
    let operand = operand.unwrap_or(PrimKinds::ANY);
    let mut result = PrimKinds::NONE;
    if operand.intersects(NOT_NUMERIC) {
        result = result.union(PrimKinds::NUMBER);
    }
    if operand.intersects(PrimKinds::BIGINT) {
        result = result.union(PrimKinds::BIGINT);
    }
    result
}

/// ECMA-262 § 13.5 unary operators.
fn unary(op: UnaryOp, operand: Option<PrimKinds>) -> PrimKinds {
    match op {
        UnaryOp::TypeOf => PrimKinds::STRING,
        UnaryOp::Void => PrimKinds::UNDEFINED,
        UnaryOp::Bang | UnaryOp::Delete => PrimKinds::BOOLEAN,
        // `ToNumber` throws on BigInt and Symbol.
        UnaryOp::Plus => {
            if operand.unwrap_or(PrimKinds::ANY).intersects(NOT_NUMERIC) {
                PrimKinds::NUMBER
            } else {
                PrimKinds::NONE
            }
        }
        UnaryOp::Minus | UnaryOp::Tilde => to_numeric(operand),
    }
}

/// ECMA-262 § 13.4 update expressions.
fn update(operand: Option<PrimKinds>) -> PrimKinds {
    to_numeric(operand)
}

/// ECMA-262 § 13.15.3 `ApplyStringOrNumericBinaryOperator` and the relational
/// and equality operators. `None` for the short-circuit operators, whose
/// result is one of their operands.
fn binary(op: BinaryOp, left: Option<PrimKinds>, right: Option<PrimKinds>) -> Option<PrimKinds> {
    let (left, right) = (
        left.unwrap_or(PrimKinds::ANY),
        right.unwrap_or(PrimKinds::ANY),
    );
    let numeric = |unsigned_shift: bool| {
        let mut result = PrimKinds::NONE;
        if left.intersects(NOT_NUMERIC) && right.intersects(NOT_NUMERIC) {
            result = result.union(PrimKinds::NUMBER);
        }
        // BigInt has no unsigned right shift; mixing BigInt with Number throws.
        if !unsigned_shift
            && left.intersects(PrimKinds::BIGINT)
            && right.intersects(PrimKinds::BIGINT)
        {
            result = result.union(PrimKinds::BIGINT);
        }
        result
    };
    Some(match op {
        BinaryOp::LogicalAnd | BinaryOp::LogicalOr | BinaryOp::NullishCoalescing => return None,
        BinaryOp::EqEq
        | BinaryOp::NotEq
        | BinaryOp::EqEqEq
        | BinaryOp::NotEqEq
        | BinaryOp::Lt
        | BinaryOp::LtEq
        | BinaryOp::Gt
        | BinaryOp::GtEq
        | BinaryOp::In
        | BinaryOp::InstanceOf => PrimKinds::BOOLEAN,
        BinaryOp::Add => {
            // A String operand converts the other with `ToString`, which
            // throws only on Symbol.
            let not_symbol = PrimKinds::ANY.without(PrimKinds::SYMBOL);
            let string = (left.intersects(PrimKinds::STRING) && right.intersects(not_symbol))
                || (right.intersects(PrimKinds::STRING) && left.intersects(not_symbol));
            let non_string = PrimKinds::ANY.without(PrimKinds::STRING);
            let mut result = if left.intersects(non_string) && right.intersects(non_string) {
                numeric(false)
            } else {
                PrimKinds::NONE
            };
            if string {
                result = result.union(PrimKinds::STRING);
            }
            result
        }
        BinaryOp::ZeroFillRShift => numeric(true),
        BinaryOp::Sub
        | BinaryOp::Mul
        | BinaryOp::Div
        | BinaryOp::Mod
        | BinaryOp::Exp
        | BinaryOp::BitOr
        | BinaryOp::BitXor
        | BinaryOp::BitAnd
        | BinaryOp::LShift
        | BinaryOp::RShift => numeric(false),
    })
}

/// The primitive types `expr` can complete with, or `None` when it may be an
/// object or its shape is not modeled.
pub fn evaluate(expr: &Expr, scope: &impl PrimScope) -> Option<PrimKinds> {
    match expr.unwrap_parens() {
        Expr::Lit(literal) => match literal {
            Lit::Str(_) => Some(PrimKinds::STRING),
            Lit::Num(_) => Some(PrimKinds::NUMBER),
            Lit::Bool(_) => Some(PrimKinds::BOOLEAN),
            Lit::Null(_) => Some(PrimKinds::NULL),
            Lit::BigInt(_) => Some(PrimKinds::BIGINT),
            Lit::Regex(_) | Lit::JSXText(_) => None,
        },
        Expr::Tpl(_) => Some(PrimKinds::STRING),
        Expr::Ident(ident) => scope.binding(ident),
        Expr::Unary(unary_expr) => Some(unary(unary_expr.op, evaluate(&unary_expr.arg, scope))),
        Expr::Update(update_expr) => Some(update(evaluate(&update_expr.arg, scope))),
        Expr::Bin(binary_expr) => {
            let left = evaluate(&binary_expr.left, scope);
            let right = evaluate(&binary_expr.right, scope);
            binary(binary_expr.op, left, right).or_else(|| Some(left?.union(right?)))
        }
        Expr::Cond(cond) => Some(evaluate(&cond.cons, scope)?.union(evaluate(&cond.alt, scope)?)),
        Expr::Seq(sequence) => evaluate(sequence.exprs.last()?, scope),
        Expr::Call(call) => intrinsic_call(call, scope),
        _ => None,
    }
}

/// A builtin row's primitive return, when the callee is its intact intrinsic
/// and every operand the row coerces is primitive.
fn intrinsic_call(call: &CallExpr, scope: &impl PrimScope) -> Option<PrimKinds> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let (global, member) = match callee.unwrap_parens() {
        Expr::Ident(global) => (global, None),
        Expr::Member(member) => match (member.obj.unwrap_parens(), &member.prop) {
            (Expr::Ident(global), MemberProp::Ident(name)) => (global, Some(name.sym.as_ref())),
            _ => return None,
        },
        _ => return None,
    };
    let row = builtins::lookup(global.sym.as_ref(), member, Invocation::Call)?;
    if !scope.intact_intrinsic(row, global, member)
        || !call.args.iter().all(|argument| argument.spread.is_none())
    {
        return None;
    }
    let Return::Prim(kinds) = row.returns else {
        return None;
    };
    let admitted = row.arity.contains(&call.args.len())
        && row.coerces.iter().all(|(operand, _)| {
            let operands = match *operand {
                ArgSel::Arg(index) => call.args.get(index..=index).unwrap_or_default(),
                ArgSel::Rest(start) => call.args.get(start..).unwrap_or_default(),
            };
            operands.iter().all(|argument| {
                evaluate(&argument.expr, scope).is_some_and(|kinds| !kinds.is_empty())
            })
        });
    admitted.then_some(kinds)
}

/// Primitive types of chunk-top bindings (`collect_binding_prims`) and the
/// global names whose intrinsic the chunk may replace.
#[derive(Debug, Default, Clone)]
pub(crate) struct BindingPrims {
    pub(crate) kinds: BTreeMap<String, PrimKinds>,
    /// Shadowed globals plus every `G` and `G.m` a visible write may replace.
    replaced_globals: BTreeSet<String>,
}

impl BindingPrims {
    /// The bindings visible where `local_shadowed` names are rebound.
    pub(crate) fn scope<'a>(&'a self, local_shadowed: &'a BTreeSet<String>) -> ScopedPrims<'a> {
        ScopedPrims {
            prims: self,
            local_shadowed,
        }
    }

    /// Bindings whose primitive excludes Symbol: `ToString` and `ToNumber`
    /// on them run no user code.
    pub(crate) fn non_symbol_bindings(&self) -> BTreeSet<String> {
        self.bindings_within(PrimKinds::ANY.without(PrimKinds::SYMBOL))
    }

    /// Symbol bindings: safe for `ToPropertyKey`, while `ToString` throws.
    pub(crate) fn symbol_bindings(&self) -> BTreeSet<String> {
        self.bindings_within(PrimKinds::SYMBOL)
    }

    fn bindings_within(&self, kinds: PrimKinds) -> BTreeSet<String> {
        self.kinds
            .iter()
            .filter(|(_, binding)| binding.is_only(kinds))
            .map(|(name, _)| name.clone())
            .collect()
    }
}

pub(crate) struct ScopedPrims<'a> {
    prims: &'a BindingPrims,
    local_shadowed: &'a BTreeSet<String>,
}

impl PrimScope for ScopedPrims<'_> {
    fn binding(&self, ident: &Ident) -> Option<PrimKinds> {
        let name = ident.sym.as_ref();
        if self.local_shadowed.contains(name) {
            return None;
        }
        self.prims.kinds.get(name).copied()
    }

    /// Purity trusts a callee only where its row allows identity from A11 and
    /// an unshadowed, unwritten spelling.
    fn intact_intrinsic(&self, row: &Row, global: &Ident, member: Option<&str>) -> bool {
        let name = global.sym.as_ref();
        row.assumed_intact
            && !self.local_shadowed.contains(name)
            && !self.prims.replaced_globals.contains(name)
            && member.is_none_or(|member| {
                !self
                    .prims
                    .replaced_globals
                    .contains(&format!("{name}.{member}"))
            })
    }
}

/// Primitive types of chunk-top `const` bindings and stable top-level `var`
/// bindings, evaluated in source order: an initializer reads only bindings
/// declared before it, the set visible without hitting the temporal dead
/// zone. Function and arrow initializers never reach here
/// (`top_level_initializers` filters them).
///
/// A stable `var` has one direct initialization, retained whole-chunk
/// declaration and rebind facts and no dynamic scope; it is tracked only for
/// a Number initializer. Before that initializer runs it is `undefined`, so
/// its module-wide type always includes Undefined. Initializer effects remain
/// classified at the declaration site.
pub(crate) fn collect_binding_prims(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
    stable_vars: &BTreeSet<Id>,
) -> BindingPrims {
    let mut writes = GlobalWriteFinder::default();
    for item in body {
        item.as_module_item().visit_with(&mut writes);
    }
    let mut prims = BindingPrims {
        kinds: BTreeMap::new(),
        replaced_globals: writes
            .written
            .into_iter()
            .chain(shadowed.iter().map(|name| (*name).to_owned()))
            .collect(),
    };
    let top_level = BTreeSet::new();
    for item in body {
        let item = item.as_module_item();
        let mut declared = Vec::new();
        for (name, init, kind) in top_level_initializers(item) {
            if kind == VarDeclKind::Const
                && let Some(kinds) = evaluate(init, &prims.scope(&top_level))
                && !kinds.is_empty()
            {
                declared.push((name, kinds));
            }
        }
        if let Some(var) = var_decl_of_item(item)
            && var.kind == VarDeclKind::Var
        {
            for decl in &var.decls {
                if let (Pat::Ident(binding), Some(init)) = (&decl.name, decl.init.as_deref())
                    && stable_vars.contains(&binding.id.to_id())
                    && let Some(kinds) = evaluate(init, &prims.scope(&top_level))
                    && kinds.is_only(PrimKinds::NUMBER.union(PrimKinds::UNDEFINED))
                {
                    declared.push((
                        binding.id.sym.to_string(),
                        kinds.union(PrimKinds::UNDEFINED),
                    ));
                }
            }
        }
        for (name, kinds) in declared {
            // A duplicate declaration is a SyntaxError in a module; joining
            // keeps malformed input deterministic.
            prims
                .kinds
                .entry(name)
                .and_modify(|current| *current = current.union(kinds))
                .or_insert(kinds);
        }
    }
    prims
}

/// Builtin globals a chunk may write through assignment, update,
/// `delete` or a reflective define: `G` for a binding or property named `G`,
/// and `G.m` for property `m` of binding `G`. A visible replacement can make an
/// intrinsic return an object, so its builtin row stops applying.
#[derive(Default)]
struct GlobalWriteFinder {
    written: BTreeSet<String>,
}

impl GlobalWriteFinder {
    fn record(&mut self, name: &str) {
        if builtins::GLOBALS.contains(name) {
            self.written.insert(name.to_owned());
        }
    }

    fn record_property(&mut self, object: &Expr, key: &str) {
        if let Expr::Ident(object) = object.unwrap_parens()
            && builtins::GLOBALS.contains(object.sym.as_ref())
        {
            self.written.insert(format!("{}.{key}", object.sym));
        }
        self.record(key);
    }

    fn record_member(&mut self, member: &MemberExpr) {
        if let Some(key) = static_member_key(member) {
            self.record_property(&member.obj, key);
        }
    }
}

impl Visit for GlobalWriteFinder {
    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        match &node.left {
            AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                self.record_member(member);
            }
            other => other.visit_with(&mut AssignTargetNames(self)),
        }
        node.visit_children_with(self);
    }

    fn visit_update_expr(&mut self, node: &UpdateExpr) {
        match node.arg.unwrap_parens() {
            Expr::Ident(ident) => self.record(ident.sym.as_ref()),
            Expr::Member(member) => self.record_member(member),
            _ => {}
        }
        node.visit_children_with(self);
    }

    fn visit_unary_expr(&mut self, node: &UnaryExpr) {
        if node.op == UnaryOp::Delete
            && let Expr::Member(member) = node.arg.unwrap_parens()
        {
            self.record_member(member);
        }
        node.visit_children_with(self);
    }

    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee
            && let Expr::Member(member) = callee.unwrap_parens()
            && let Expr::Ident(receiver) = member.obj.unwrap_parens()
            && let MemberProp::Ident(property) = &member.prop
            && matches!(
                (receiver.sym.as_ref(), property.sym.as_ref()),
                ("Object", "defineProperty" | "defineProperties" | "assign")
                    | ("Reflect", "defineProperty" | "set")
            )
            && let [target, keys, ..] = node.args.as_slice()
        {
            let keys: Vec<&str> = match keys.expr.unwrap_parens() {
                Expr::Lit(Lit::Str(key)) => key.value.as_str().into_iter().collect(),
                Expr::Object(object) => object
                    .props
                    .iter()
                    .filter_map(|prop| match prop {
                        PropOrSpread::Prop(prop) => match prop.as_ref() {
                            Prop::KeyValue(KeyValueProp {
                                key: PropName::Ident(key),
                                ..
                            }) => Some(key.sym.as_ref()),
                            _ => None,
                        },
                        PropOrSpread::Spread(_) => None,
                    })
                    .collect(),
                _ => Vec::new(),
            };
            for key in keys {
                self.record_property(&target.expr, key);
            }
        }
        node.visit_children_with(self);
    }
}

/// Every name a destructuring or other non-member assignment target writes.
struct AssignTargetNames<'a>(&'a mut GlobalWriteFinder);

impl Visit for AssignTargetNames<'_> {
    fn visit_ident(&mut self, ident: &Ident) {
        self.0.record(ident.sym.as_ref());
    }

    fn visit_member_expr(&mut self, member: &MemberExpr) {
        self.0.record_member(member);
        member.visit_children_with(self);
    }
}

fn static_member_key(member: &MemberExpr) -> Option<&str> {
    match &member.prop {
        MemberProp::Ident(id) => Some(id.sym.as_ref()),
        MemberProp::Computed(key) => match key.expr.unwrap_parens() {
            Expr::Lit(Lit::Str(value)) => value.value.as_str(),
            _ => None,
        },
        MemberProp::PrivateName(_) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Tracks `n` as Number, `b` as BigInt and `s` as String; `Symbol` and
    /// `Math` are intact.
    struct Bindings;

    impl PrimScope for Bindings {
        fn binding(&self, ident: &Ident) -> Option<PrimKinds> {
            match ident.sym.as_ref() {
                "n" => Some(PrimKinds::NUMBER),
                "b" => Some(PrimKinds::BIGINT),
                "s" => Some(PrimKinds::STRING),
                _ => None,
            }
        }

        fn intact_intrinsic(&self, _: &Row, global: &Ident, _: Option<&str>) -> bool {
            matches!(global.sym.as_ref(), "Symbol" | "Math")
        }
    }

    #[test]
    fn expression_types_follow_ecmascript_operator_rules() {
        let number_or_string = PrimKinds::NUMBER.union(PrimKinds::STRING);
        let numeric = PrimKinds::NUMBER.union(PrimKinds::BIGINT);
        for (source, expected) in [
            ("n + 1", Some(PrimKinds::NUMBER)),
            ("b * 2n", Some(PrimKinds::BIGINT)),
            ("n + b", Some(PrimKinds::NONE)),
            ("s + b", Some(PrimKinds::STRING)),
            ("object + 1", Some(number_or_string)),
            ("-object", Some(numeric)),
            ("+b", Some(PrimKinds::NONE)),
            ("b >>> 1n", Some(PrimKinds::NONE)),
            ("object < n", Some(PrimKinds::BOOLEAN)),
            ("n || s", Some(number_or_string)),
            ("n || object", None),
            ("Symbol('k')", Some(PrimKinds::SYMBOL)),
            ("Math.round(n)", Some(PrimKinds::NUMBER)),
            ("Math.round(object)", None),
            ("Math.max(n)", None),
        ] {
            let module = raw_js_test_support::parse(&format!("({source});"));
            let [ModuleItem::Stmt(Stmt::Expr(statement))] = module.body.as_slice() else {
                panic!("{source} parses as one expression statement");
            };
            assert_eq!(evaluate(&statement.expr, &Bindings), expected, "{source}");
        }
    }
}
