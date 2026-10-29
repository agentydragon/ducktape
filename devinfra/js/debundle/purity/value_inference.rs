//! Finite positive closures for value classes used by purity rules.
//!
//! These rules only publish facts that have been proven by existing syntax
//! matchers. Whole-chunk write, escape, and method scans remain admission gates
//! in `plain_data`.

use super::*;
use crate::inference::{FactContext, IterativeInference, IterativeRules};

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum ValueKey {
    Fluent(String),
    PlainArray(String),
    Const(ConstBindingKey),
    VarNumber(ConstBindingKey),
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum ValueFact {
    Known,
    ConstClasses {
        primitive: bool,
        symbol: bool,
        number: bool,
        bigint: bool,
    },
    InitializedVarNumber {
        number: bool,
    },
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct ConstBindingKey {
    source_index: usize,
    name: String,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct ConstProducer {
    target: ConstBindingKey,
    previous_binding: Option<ConstBindingKey>,
    referenced_predecessors: Vec<ConstBindingKey>,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct VarNumberProducer {
    target: ConstBindingKey,
    referenced_predecessors: Vec<ConstBindingKey>,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum ValueRule {
    Fluent {
        target: String,
        root: String,
    },
    PlainArray {
        target: String,
        source: Option<String>,
    },
    Const(ConstProducer),
    VarNumber(VarNumberProducer),
}

struct ValueRules<'a> {
    const_inits: BTreeMap<ConstBindingKey, &'a Expr>,
    var_number_inits: BTreeMap<ConstBindingKey, &'a Expr>,
    shadowed: Option<&'a BTreeSet<&'static str>>,
}

impl IterativeRules for ValueRules<'_> {
    type Key = ValueKey;
    type Fact = ValueFact;
    type Rule = ValueRule;

    fn evaluate(
        &self,
        rule: &Self::Rule,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
    ) {
        match rule {
            ValueRule::Fluent { target, root } => {
                if context.read(ValueKey::Fluent(root.clone())).is_some() {
                    context.propose(ValueKey::Fluent(target.clone()), ValueFact::Known);
                }
            }
            ValueRule::PlainArray { target, source } => match source {
                None => {
                    context.propose(ValueKey::PlainArray(target.clone()), ValueFact::Known);
                }
                Some(source) => {
                    if context.read(ValueKey::PlainArray(source.clone())).is_some() {
                        context.propose(ValueKey::PlainArray(target.clone()), ValueFact::Known);
                    }
                }
            },
            ValueRule::Const(producer) => self.evaluate_const(producer, context),
            ValueRule::VarNumber(producer) => self.evaluate_var_number(producer, context),
        }
    }

    fn merge(key: &Self::Key, current: &mut Self::Fact, contribution: Self::Fact) -> bool {
        match (key, current, contribution) {
            (ValueKey::Fluent(_) | ValueKey::PlainArray(_), ValueFact::Known, ValueFact::Known) => {
                false
            }
            (
                ValueKey::Const(_),
                ValueFact::ConstClasses {
                    primitive: current_primitive,
                    symbol: current_symbol,
                    number: current_number,
                    bigint: current_bigint,
                },
                ValueFact::ConstClasses {
                    primitive: next_primitive,
                    symbol: next_symbol,
                    number: next_number,
                    bigint: next_bigint,
                },
            ) => {
                let changed = (!*current_primitive && next_primitive)
                    || (!*current_symbol && next_symbol)
                    || (!*current_number && next_number)
                    || (!*current_bigint && next_bigint);
                *current_primitive |= next_primitive;
                *current_symbol |= next_symbol;
                *current_number |= next_number;
                *current_bigint |= next_bigint;
                changed
            }
            (
                ValueKey::VarNumber(_),
                ValueFact::InitializedVarNumber {
                    number: current_number,
                },
                ValueFact::InitializedVarNumber {
                    number: next_number,
                },
            ) => {
                let changed = !*current_number && next_number;
                *current_number |= next_number;
                changed
            }
            _ => unreachable!("value rule proposed a fact for the wrong key kind"),
        }
    }
}

impl ValueRules<'_> {
    fn evaluate_const(
        &self,
        producer: &ConstProducer,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
    ) {
        // Source-order const classification is position-sensitive. Wait for
        // every earlier declaration that the initializer references, including
        // unsupported consts whose completed fact is explicitly false. Never
        // publish a temporary negative result while one of these is absent.
        let mut complete = true;
        let mut previous_primitives = BTreeSet::new();
        let mut previous_symbols = BTreeSet::new();
        let mut previous_numbers = BTreeSet::new();
        let mut previous_bigints = BTreeSet::new();
        let previous_binding = match &producer.previous_binding {
            Some(key) => match context.read(ValueKey::Const(key.clone())) {
                Some(fact) => Some(fact.clone()),
                None => {
                    complete = false;
                    None
                }
            },
            None => None,
        };
        for predecessor in &producer.referenced_predecessors {
            match context.read(ValueKey::Const(predecessor.clone())) {
                Some(ValueFact::ConstClasses {
                    primitive,
                    symbol,
                    number,
                    bigint,
                }) => {
                    if *primitive {
                        previous_primitives.insert(predecessor.name.clone());
                    }
                    if *symbol {
                        previous_symbols.insert(predecessor.name.clone());
                    }
                    if *number {
                        previous_numbers.insert(predecessor.name.clone());
                    }
                    if *bigint {
                        previous_bigints.insert(predecessor.name.clone());
                    }
                }
                None => complete = false,
                Some(ValueFact::Known | ValueFact::InitializedVarNumber { .. }) => {
                    unreachable!("const predecessor has a different value fact")
                }
            }
        }
        if !complete {
            return;
        }

        let Some(init) = self.const_inits.get(&producer.target) else {
            unreachable!("const producer has no initializer");
        };
        let primitive = is_result_primitive(init, &previous_primitives, &BTreeSet::new());
        let number = is_number_expression(init, &previous_numbers, &BTreeSet::new());
        let bigint = is_bigint_expression(init, &previous_bigints, &BTreeSet::new());
        let symbol = !primitive
            && is_symbol_const_init(
                init,
                &previous_symbols,
                self.shadowed
                    .expect("const inference has global shadow facts"),
            );

        // Duplicate declarations are invalid JavaScript, but retaining the
        // original set-accumulation behavior keeps malformed-input handling
        // conservative and source ordered.
        let (primitive, symbol, number, bigint) = match previous_binding {
            Some(ValueFact::ConstClasses {
                primitive: previous_primitive,
                symbol: previous_symbol,
                number: previous_number,
                bigint: previous_bigint,
            }) => (
                primitive || previous_primitive,
                symbol || previous_symbol,
                number || previous_number,
                bigint || previous_bigint,
            ),
            Some(ValueFact::Known | ValueFact::InitializedVarNumber { .. }) => {
                unreachable!("const predecessor has a different value fact")
            }
            None => (primitive, symbol, number, bigint),
        };
        context.propose(
            ValueKey::Const(producer.target.clone()),
            ValueFact::ConstClasses {
                primitive,
                symbol,
                number,
                bigint,
            },
        );
    }

    fn evaluate_var_number(
        &self,
        producer: &VarNumberProducer,
        context: &mut impl FactContext<<Self as IterativeRules>::Key, <Self as IterativeRules>::Fact>,
    ) {
        let mut complete = true;
        let mut previous_numbers = BTreeSet::new();
        for predecessor in &producer.referenced_predecessors {
            match context.read(ValueKey::VarNumber(predecessor.clone())) {
                Some(ValueFact::InitializedVarNumber { number }) => {
                    if *number {
                        previous_numbers.insert(predecessor.name.clone());
                    }
                }
                None => complete = false,
                Some(ValueFact::Known | ValueFact::ConstClasses { .. }) => {
                    unreachable!("var-number predecessor has a different value fact")
                }
            }
        }
        if !complete {
            return;
        }

        let Some(init) = self.var_number_inits.get(&producer.target) else {
            unreachable!("var-number producer has no initializer");
        };
        context.propose(
            ValueKey::VarNumber(producer.target.clone()),
            ValueFact::InitializedVarNumber {
                number: is_number_expression(init, &previous_numbers, &BTreeSet::new()),
            },
        );
    }
}

pub(super) fn infer_fluent_const_bindings(
    body: &[TopLevelItemView<'_>],
    seed: &BTreeSet<String>,
) -> BTreeSet<String> {
    if seed.is_empty() {
        return BTreeSet::new();
    }
    let mut inference = IterativeInference::new(ValueRules {
        const_inits: BTreeMap::new(),
        var_number_inits: BTreeMap::new(),
        shadowed: None,
    });
    for root in seed {
        inference.seed(ValueKey::Fluent(root.clone()), ValueFact::Known);
    }
    let producers = body.iter().flat_map(|item| {
        plain_data_var_candidates(item.as_module_item())
            .into_iter()
            .filter_map(|(target, init, kind)| {
                (kind == VarDeclKind::Const)
                    .then(|| fluent_chain_root(init))
                    .flatten()
                    .map(|root| ValueRule::Fluent {
                        target,
                        root: root.to_owned(),
                    })
            })
    });
    inference
        .solve_values(producers)
        .into_keys()
        .filter_map(|key| match key {
            ValueKey::Fluent(name) => Some(name),
            ValueKey::PlainArray(_) | ValueKey::Const(_) | ValueKey::VarNumber(_) => None,
        })
        .collect()
}

pub(super) fn infer_plain_array_candidates(body: &[TopLevelItemView<'_>]) -> BTreeSet<String> {
    let inference = IterativeInference::new(ValueRules {
        const_inits: BTreeMap::new(),
        var_number_inits: BTreeMap::new(),
        shadowed: None,
    });
    let producers = body.iter().flat_map(|item| {
        plain_data_var_candidates(item.as_module_item())
            .into_iter()
            .filter_map(|(target, init, kind)| {
                if kind != VarDeclKind::Const {
                    return None;
                }
                plain_array_source(init).map(|source| ValueRule::PlainArray { target, source })
            })
    });
    inference
        .solve_values(producers)
        .into_keys()
        .filter_map(|key| match key {
            ValueKey::PlainArray(name) => Some(name),
            ValueKey::Fluent(_) | ValueKey::Const(_) | ValueKey::VarNumber(_) => None,
        })
        .collect()
}

fn plain_array_source(expr: &Expr) -> Option<Option<String>> {
    match expr.unwrap_parens() {
        Expr::Array(_) => Some(None),
        Expr::Ident(ident) => Some(Some(ident.sym.to_string())),
        Expr::Call(call) => {
            let Callee::Expr(callee) = &call.callee else {
                return None;
            };
            let Expr::Member(member) = callee.unwrap_parens() else {
                return None;
            };
            if !matches!(
                &member.prop,
                MemberProp::Ident(prop) if ["filter", "flatMap", "map", "slice"].contains(&prop.sym.as_ref())
            ) {
                return None;
            }
            plain_array_source(member.obj.as_ref())
        }
        _ => None,
    }
}

pub(super) fn infer_const_value_classes(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
    stable_var_number_candidates: &BTreeSet<Id>,
) -> BindingValueClasses {
    let mut const_inits = BTreeMap::new();
    let mut var_number_inits = BTreeMap::new();
    let mut previous_by_name = BTreeMap::new();
    let mut producers = Vec::<ValueRule>::new();
    for (source_index, item) in body.iter().enumerate() {
        for (name, init, kind) in plain_data_var_candidates(item.as_module_item()) {
            if kind != VarDeclKind::Const {
                continue;
            }
            let target = ConstBindingKey { source_index, name };
            const_inits.insert(target.clone(), init);
            let previous_binding = previous_by_name.get(&target.name).cloned();

            let mut identifiers = IdentifierCollector::default();
            init.visit_with(&mut identifiers);
            let referenced_predecessors = identifiers
                .0
                .into_iter()
                .filter_map(|name| previous_by_name.get(&name).cloned())
                .collect();
            producers.push(ValueRule::Const(ConstProducer {
                target: target.clone(),
                previous_binding,
                referenced_predecessors,
            }));
            previous_by_name.insert(target.name.clone(), target);
        }
    }

    let mut previous_number_by_name = BTreeMap::new();
    for (source_index, item) in body.iter().enumerate() {
        let Some(var) = crate::facts::var_decl_of_item(item.as_module_item()) else {
            continue;
        };
        if var.kind != VarDeclKind::Var {
            continue;
        }
        for decl in &var.decls {
            let (Pat::Ident(binding), Some(init)) = (&decl.name, decl.init.as_deref()) else {
                continue;
            };
            if !stable_var_number_candidates.contains(&binding.id.to_id()) {
                continue;
            }
            let target = ConstBindingKey {
                source_index,
                name: binding.id.sym.to_string(),
            };
            var_number_inits.insert(target.clone(), init);
            let mut identifiers = IdentifierCollector::default();
            init.visit_with(&mut identifiers);
            let referenced_predecessors = identifiers
                .0
                .into_iter()
                .filter_map(|name| previous_number_by_name.get(&name).cloned())
                .collect();
            producers.push(ValueRule::VarNumber(VarNumberProducer {
                target: target.clone(),
                referenced_predecessors,
            }));
            previous_number_by_name.insert(target.name.clone(), target);
        }
    }

    let facts = IterativeInference::new(ValueRules {
        const_inits,
        var_number_inits,
        shadowed: Some(shadowed),
    })
    .solve_values(producers);
    let mut classes = BindingValueClasses::default();
    for (key, fact) in facts {
        match (key, fact) {
            (
                ValueKey::Const(binding),
                ValueFact::ConstClasses {
                    primitive,
                    symbol,
                    number,
                    bigint,
                },
            ) => {
                if primitive {
                    classes.primitive_bindings.insert(binding.name.clone());
                }
                if symbol {
                    classes.symbol_const_bindings.insert(binding.name.clone());
                }
                if number {
                    classes.number_bindings.insert(binding.name.clone());
                }
                if bigint {
                    classes.bigint_bindings.insert(binding.name);
                }
            }
            (ValueKey::VarNumber(binding), ValueFact::InitializedVarNumber { number }) => {
                if number {
                    classes.primitive_bindings.insert(binding.name.clone());
                    // At its initializer this var becomes Number. At earlier
                    // module positions it is still `undefined`, so only the
                    // wider Number-or-Undefined fact is exported to general
                    // expression classification.
                    classes.number_or_undefined_bindings.insert(binding.name);
                }
            }
            (ValueKey::Fluent(_) | ValueKey::PlainArray(_), ValueFact::Known) => {}
            _ => unreachable!("value key has a fact of the wrong kind"),
        }
    }
    classes
}

/// This narrower result class is used only for stable `var` bindings. It
/// proves non-throwing Number arithmetic, not merely primitive output, so a
/// mixed BigInt/Number initializer never gains a binding fact.
pub(super) fn is_number_expression(
    expr: &Expr,
    numbers: &BTreeSet<String>,
    local_shadowed: &BTreeSet<String>,
) -> bool {
    match expr.unwrap_parens() {
        Expr::Lit(Lit::Num(_)) => true,
        Expr::Ident(ident) => {
            numbers.contains(ident.sym.as_ref()) && !local_shadowed.contains(ident.sym.as_ref())
        }
        Expr::Unary(unary)
            if matches!(unary.op, UnaryOp::Plus | UnaryOp::Minus | UnaryOp::Tilde) =>
        {
            is_number_expression(&unary.arg, numbers, local_shadowed)
        }
        Expr::Bin(binary)
            if matches!(
                binary.op,
                BinaryOp::Add
                    | BinaryOp::Sub
                    | BinaryOp::Mul
                    | BinaryOp::Div
                    | BinaryOp::Mod
                    | BinaryOp::Exp
                    | BinaryOp::BitOr
                    | BinaryOp::BitXor
                    | BinaryOp::BitAnd
                    | BinaryOp::LShift
                    | BinaryOp::RShift
                    | BinaryOp::ZeroFillRShift
            ) =>
        {
            is_number_expression(&binary.left, numbers, local_shadowed)
                && is_number_expression(&binary.right, numbers, local_shadowed)
        }
        _ => false,
    }
}

pub(super) fn is_bigint_expression(
    expr: &Expr,
    bigints: &BTreeSet<String>,
    local_shadowed: &BTreeSet<String>,
) -> bool {
    match expr.unwrap_parens() {
        Expr::Lit(Lit::BigInt(_)) => true,
        Expr::Ident(ident) => {
            bigints.contains(ident.sym.as_ref()) && !local_shadowed.contains(ident.sym.as_ref())
        }
        Expr::Unary(unary) if matches!(unary.op, UnaryOp::Minus | UnaryOp::Tilde) => {
            is_bigint_expression(&unary.arg, bigints, local_shadowed)
        }
        Expr::Bin(binary)
            if matches!(
                binary.op,
                BinaryOp::Add
                    | BinaryOp::Sub
                    | BinaryOp::Mul
                    | BinaryOp::Div
                    | BinaryOp::Mod
                    | BinaryOp::Exp
                    | BinaryOp::BitOr
                    | BinaryOp::BitXor
                    | BinaryOp::BitAnd
                    | BinaryOp::LShift
                    | BinaryOp::RShift
            ) =>
        {
            is_bigint_expression(&binary.left, bigints, local_shadowed)
                && is_bigint_expression(&binary.right, bigints, local_shadowed)
        }
        Expr::Cond(cond) => {
            is_bigint_expression(&cond.cons, bigints, local_shadowed)
                && is_bigint_expression(&cond.alt, bigints, local_shadowed)
        }
        Expr::Seq(sequence) => sequence
            .exprs
            .last()
            .is_some_and(|last| is_bigint_expression(last, bigints, local_shadowed)),
        _ => false,
    }
}

pub(super) fn is_definitely_string_expression(expr: &Expr) -> bool {
    match expr.unwrap_parens() {
        Expr::Lit(Lit::Str(_)) | Expr::Tpl(_) => true,
        Expr::Unary(unary) if unary.op == UnaryOp::TypeOf => true,
        Expr::Bin(binary) if binary.op == BinaryOp::Add => {
            is_definitely_string_expression(&binary.left)
                || is_definitely_string_expression(&binary.right)
        }
        Expr::Cond(cond) => {
            is_definitely_string_expression(&cond.cons)
                && is_definitely_string_expression(&cond.alt)
        }
        Expr::Seq(sequence) => sequence
            .exprs
            .last()
            .is_some_and(|last| is_definitely_string_expression(last)),
        _ => false,
    }
}

pub(super) fn is_definitely_non_bigint_expression(
    expr: &Expr,
    numbers: &BTreeSet<String>,
    number_or_undefined: &BTreeSet<String>,
    local_shadowed: &BTreeSet<String>,
) -> bool {
    match expr.unwrap_parens() {
        Expr::Lit(Lit::Num(_) | Lit::Str(_) | Lit::Bool(_) | Lit::Null(_)) | Expr::Tpl(_) => true,
        Expr::Ident(ident) => {
            !local_shadowed.contains(ident.sym.as_ref())
                && (numbers.contains(ident.sym.as_ref())
                    || number_or_undefined.contains(ident.sym.as_ref()))
        }
        Expr::Unary(unary)
            if matches!(
                unary.op,
                UnaryOp::Bang | UnaryOp::TypeOf | UnaryOp::Void | UnaryOp::Delete
            ) =>
        {
            true
        }
        Expr::Unary(unary)
            if matches!(unary.op, UnaryOp::Plus | UnaryOp::Minus | UnaryOp::Tilde) =>
        {
            is_definitely_non_bigint_expression(
                &unary.arg,
                numbers,
                number_or_undefined,
                local_shadowed,
            )
        }
        Expr::Bin(binary)
            if matches!(
                binary.op,
                BinaryOp::EqEq
                    | BinaryOp::NotEq
                    | BinaryOp::EqEqEq
                    | BinaryOp::NotEqEq
                    | BinaryOp::Lt
                    | BinaryOp::LtEq
                    | BinaryOp::Gt
                    | BinaryOp::GtEq
                    | BinaryOp::In
                    | BinaryOp::InstanceOf
            ) =>
        {
            true
        }
        Expr::Bin(binary) if binary.op == BinaryOp::Add => {
            // Addition returns a string if either primitive operand converts
            // to string; otherwise two proven non-BigInt primitives produce a
            // Number. Unknown objects stay unknown because conversion can
            // invoke user code, and unproven BigInts can still make addition
            // throw unless the string case above is guaranteed.
            is_definitely_string_expression(expr)
                || (is_definitely_non_bigint_expression(
                    &binary.left,
                    numbers,
                    number_or_undefined,
                    local_shadowed,
                ) && is_definitely_non_bigint_expression(
                    &binary.right,
                    numbers,
                    number_or_undefined,
                    local_shadowed,
                ))
        }
        Expr::Bin(binary)
            if matches!(
                binary.op,
                BinaryOp::Sub
                    | BinaryOp::Mul
                    | BinaryOp::Div
                    | BinaryOp::Mod
                    | BinaryOp::Exp
                    | BinaryOp::BitOr
                    | BinaryOp::BitXor
                    | BinaryOp::BitAnd
                    | BinaryOp::LShift
                    | BinaryOp::RShift
                    | BinaryOp::ZeroFillRShift
            ) =>
        {
            is_definitely_non_bigint_expression(
                &binary.left,
                numbers,
                number_or_undefined,
                local_shadowed,
            ) && is_definitely_non_bigint_expression(
                &binary.right,
                numbers,
                number_or_undefined,
                local_shadowed,
            )
        }
        Expr::Cond(cond) => {
            is_definitely_non_bigint_expression(
                &cond.cons,
                numbers,
                number_or_undefined,
                local_shadowed,
            ) && is_definitely_non_bigint_expression(
                &cond.alt,
                numbers,
                number_or_undefined,
                local_shadowed,
            )
        }
        Expr::Seq(sequence) => sequence.exprs.last().is_some_and(|last| {
            is_definitely_non_bigint_expression(last, numbers, number_or_undefined, local_shadowed)
        }),
        _ => false,
    }
}

#[derive(Default)]
struct IdentifierCollector(BTreeSet<String>);

impl Visit for IdentifierCollector {
    fn visit_ident(&mut self, ident: &Ident) {
        self.0.insert(ident.sym.to_string());
    }
}
