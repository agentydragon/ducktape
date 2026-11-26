use crate::{
    ClassSurface, MemberKey, NamespaceValueFacts, ValueExpr, object_data_property_name,
    static_prop, static_property_name,
};
use analysis_model::PrimKinds;
use analysis_vocabulary::value::Kind;
use std::collections::BTreeSet;
use std::sync::Arc;
use swc_ecma_ast::*;

fn iterator_prototype_receiver(call: &CallExpr) -> Option<&Expr> {
    let Callee::Expr(outer_callee) = &call.callee else {
        return None;
    };
    let Expr::Member(outer_member) = outer_callee.unwrap_parens() else {
        return None;
    };
    if !matches!(outer_member.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Object")
        || !matches!(&outer_member.prop, MemberProp::Ident(id) if id.sym == "getPrototypeOf")
    {
        return None;
    }
    let [argument] = call.args.as_slice() else {
        return None;
    };
    if argument.spread.is_some() {
        return None;
    }
    let Expr::Call(iterator_call) = argument.expr.unwrap_parens() else {
        return None;
    };
    if !iterator_call.args.is_empty() {
        return None;
    }
    let Callee::Expr(iterator_callee) = &iterator_call.callee else {
        return None;
    };
    let Expr::Member(iterator_member) = iterator_callee.unwrap_parens() else {
        return None;
    };
    if !matches!(&iterator_member.prop, MemberProp::Computed(key)
        if matches!(key.expr.unwrap_parens(), Expr::Member(symbol)
            if matches!(symbol.obj.unwrap_parens(), Expr::Ident(id) if id.sym == "Symbol")
                && matches!(&symbol.prop, MemberProp::Ident(id) if id.sym == "iterator")))
    {
        return None;
    }
    Some(iterator_member.obj.unwrap_parens())
}

fn array_iterator_prototype_expression(call: &CallExpr) -> bool {
    let Some(Expr::Array(array)) = iterator_prototype_receiver(call) else {
        return false;
    };
    array.elems.is_empty()
}

fn set_iterator_prototype_expression(call: &CallExpr) -> bool {
    let Some(Expr::New(set)) = iterator_prototype_receiver(call) else {
        return false;
    };
    matches!(set.callee.unwrap_parens(), Expr::Ident(id) if id.sym == "Set")
        && set.args.as_deref().is_none_or(|args| args.is_empty())
}

impl ValueExpr {
    /// Possible returned values; the conditional test is evaluated separately.
    pub fn result_alternatives(&self) -> Option<&[Self]> {
        match self {
            Self::Alternatives(values) => Some(values),
            Self::Sequence(values) => Some(&values[values.len().saturating_sub(1)..]),
            Self::Conditional { alternatives, .. } => Some(alternatives.as_slice()),
            _ => None,
        }
    }

    pub fn from_expr(expr: &Expr) -> Self {
        Self::from_expr_with_coverage(expr).0
    }

    /// Preserve coverage in the same recursive lowering that produces values.
    /// This is syntax coverage only; input access and runtime transfer evidence
    /// are required before any allocation-only judgment may be issued.
    pub fn from_expr_with_coverage(expr: &Expr) -> (Self, bool) {
        let mut covered = true;
        let value = Self::lower(expr, &mut covered);
        (value, covered)
    }

    fn lower(expr: &Expr, covered: &mut bool) -> Self {
        if !matches!(
            expr.unwrap_parens(),
            Expr::Ident(_)
                | Expr::Object(_)
                | Expr::Array(_)
                | Expr::Cond(_)
                | Expr::Seq(_)
                | Expr::Lit(
                    Lit::Str(_)
                        | Lit::Num(_)
                        | Lit::Bool(_)
                        | Lit::Null(_)
                        | Lit::BigInt(_)
                        | Lit::Regex(_)
                )
        ) && !matches!(expr.unwrap_parens(), Expr::Bin(bin)
            if matches!(bin.op, BinaryOp::EqEqEq | BinaryOp::NotEqEq))
            && !matches!(expr.unwrap_parens(), Expr::Unary(unary)
                if matches!(unary.op, UnaryOp::Void | UnaryOp::Bang))
        {
            *covered = false;
        }
        match expr.unwrap_parens() {
            Expr::Ident(ident) => Self::Binding(ident.to_id()),
            Expr::Fn(function) => Self::Alloc(Kind::Function, function.function.span.lo.0),
            Expr::Arrow(arrow) => Self::Alloc(Kind::Function, arrow.span.lo.0),
            Expr::Class(class) => Self::Alloc(Kind::Class, class.class.span.lo.0),
            Expr::Object(object) => Self::ObjectLiteral(
                object.span.lo.0,
                object
                    .props
                    .iter()
                    .map(|property| match property {
                        PropOrSpread::Spread(spread) => {
                            *covered = false;
                            (None, Self::lower(&spread.expr, covered))
                        }
                        PropOrSpread::Prop(property) => match property.as_ref() {
                            Prop::KeyValue(pair) => {
                                if !matches!(
                                    pair.key,
                                    PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                                ) || static_property_name(&pair.key).as_deref()
                                    == Some("__proto__")
                                {
                                    *covered = false;
                                }
                                (
                                    object_data_property_name(&pair.key),
                                    Self::lower(&pair.value, covered),
                                )
                            }
                            Prop::Shorthand(ident) => {
                                (Some(ident.sym.to_string()), Self::Binding(ident.to_id()))
                            }
                            Prop::Assign(assign) => {
                                *covered = false;
                                (
                                    Some(assign.key.sym.to_string()),
                                    Self::lower(&assign.value, covered),
                                )
                            }
                            Prop::Method(method) => {
                                *covered = false;
                                (
                                    static_property_name(&method.key),
                                    Self::Alloc(Kind::Function, method.function.span.lo.0),
                                )
                            }
                            Prop::Getter(getter) => {
                                *covered = false;
                                (
                                    static_property_name(&getter.key),
                                    Self::Alloc(Kind::Function, getter.function.span.lo.0),
                                )
                            }
                            Prop::Setter(setter) => {
                                *covered = false;
                                (
                                    static_property_name(&setter.key),
                                    Self::Alloc(Kind::Function, setter.function.span.lo.0),
                                )
                            }
                        },
                    })
                    .collect(),
            ),
            Expr::Array(array) => Self::ObjectLiteral(
                array.span.lo.0,
                array
                    .elems
                    .iter()
                    .filter_map(|element| {
                        let Some(element) = element else {
                            *covered = false;
                            return None;
                        };
                        if element.spread.is_some() {
                            *covered = false;
                        }
                        Some((None, Self::lower(&element.expr, covered)))
                    })
                    .collect(),
            ),
            Expr::Member(member) => Self::Member(
                Arc::new(Self::lower(&member.obj, covered)),
                static_prop(&member.prop),
                MemberKey::of(&member.prop),
                member.span.into(),
            ),
            Expr::OptChain(chain) => match chain.base.as_ref() {
                OptChainBase::Call(call) if call.args.iter().all(|arg| arg.spread.is_none()) => {
                    // Keep a known local call's returned callable reachable.
                    // The undefined alternative overapproximates optional
                    // short-circuiting without dropping the invocation path.
                    Self::Alternatives(
                        [
                            Self::Undefined,
                            // Preserve the prior unknown publication capability;
                            // timing precision must not admit namespace contracts.
                            Self::Unknown,
                            Self::Call(
                                Arc::new(Self::lower(&call.callee, covered)),
                                call.args
                                    .iter()
                                    .map(|arg| Self::lower(&arg.expr, covered))
                                    .collect(),
                                call.span.into(),
                            ),
                        ]
                        .into(),
                    )
                }
                OptChainBase::Member(member) => {
                    let mut options = vec![
                        Self::Undefined,
                        Self::Member(
                            Arc::new(Self::lower(&member.obj, covered)),
                            static_prop(&member.prop),
                            MemberKey::of(&member.prop),
                            member.span.into(),
                        ),
                    ];
                    if !matches!(member.obj.unwrap_parens(), Expr::Ident(_))
                        || !matches!(member.prop, MemberProp::Ident(_))
                    {
                        options.push(Self::Unknown);
                    }
                    Self::Alternatives(options.into())
                }
                // Spread calls have no modeled result shape.
                _ => Self::Unknown,
            },
            Expr::Call(call) if array_iterator_prototype_expression(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    unreachable!("recognized iterator-prototype expression is an ordinary call")
                };
                Self::ArrayIteratorPrototype(Arc::new(Self::Call(
                    Arc::new(Self::lower(callee, covered)),
                    call.args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, covered))
                        .collect(),
                    call.span.into(),
                )))
            }
            Expr::Call(call) if set_iterator_prototype_expression(call) => {
                let Callee::Expr(callee) = &call.callee else {
                    unreachable!("recognized iterator-prototype expression is an ordinary call")
                };
                Self::SetIteratorPrototype(Arc::new(Self::Call(
                    Arc::new(Self::lower(callee, covered)),
                    call.args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, covered))
                        .collect(),
                    call.span.into(),
                )))
            }
            Expr::Call(call) if call.args.iter().all(|arg| arg.spread.is_none()) => {
                match &call.callee {
                    Callee::Expr(callee) => Self::Call(
                        Arc::new(Self::lower(callee, covered)),
                        call.args
                            .iter()
                            .map(|arg| Self::lower(&arg.expr, covered))
                            .collect(),
                        call.span.into(),
                    ),
                    Callee::Import(_) | Callee::Super(_) => Self::Unknown,
                }
            }
            Expr::New(new_expr)
                if new_expr
                    .args
                    .as_deref()
                    .unwrap_or_default()
                    .iter()
                    .all(|arg| arg.spread.is_none()) =>
            {
                Self::New(
                    Arc::new(Self::lower(&new_expr.callee, covered)),
                    new_expr
                        .args
                        .as_deref()
                        .unwrap_or_default()
                        .iter()
                        .map(|arg| Self::lower(&arg.expr, covered))
                        .collect(),
                    new_expr.span.into(),
                )
            }
            Expr::Cond(cond) => Self::Conditional {
                test: Arc::new(Self::lower(&cond.test, covered)),
                alternatives: Arc::new([
                    Self::lower(&cond.cons, covered),
                    Self::lower(&cond.alt, covered),
                ]),
            },
            Expr::Bin(bin) if matches!(bin.op, BinaryOp::EqEqEq | BinaryOp::NotEqEq) => {
                Self::StrictEquality(
                    Arc::new(Self::lower(&bin.left, covered)),
                    Arc::new(Self::lower(&bin.right, covered)),
                )
            }
            Expr::Bin(bin) if bin.op.may_short_circuit() => Self::Alternatives(
                [
                    Self::lower(&bin.left, covered),
                    Self::lower(&bin.right, covered),
                ]
                .into(),
            ),
            Expr::Bin(bin) => Self::Primitive(
                [
                    Self::lower(&bin.left, covered),
                    Self::lower(&bin.right, covered),
                ]
                .into(),
            ),
            Expr::Unary(unary) if unary.op == UnaryOp::Void => {
                Self::Void(Arc::new(Self::lower(&unary.arg, covered)))
            }
            Expr::Unary(unary) if unary.op == UnaryOp::Bang => {
                Self::LogicalNot(Arc::new(Self::lower(&unary.arg, covered)))
            }
            Expr::Unary(unary) => Self::Primitive([Self::lower(&unary.arg, covered)].into()),
            Expr::Update(update) => Self::Primitive([Self::lower(&update.arg, covered)].into()),
            Expr::Seq(seq) => {
                let mut sequence_covered = true;
                let expressions: Vec<_> = seq
                    .exprs
                    .iter()
                    .map(|expression| Self::lower(expression, &mut sequence_covered))
                    .collect();
                *covered &= sequence_covered;
                if sequence_covered {
                    Self::Sequence(expressions.into())
                } else {
                    // Uncovered calls/writes are executed by the indexed
                    // timing path. Do not replay them to reconstruct a value.
                    expressions.into_iter().last().unwrap_or(Self::Unknown)
                }
            }
            Expr::Assign(assign) => Self::lower(&assign.right, covered),
            Expr::Lit(Lit::Regex(regex)) => Self::Alloc(Kind::RegExp, regex.span.lo.0),
            Expr::Lit(literal) => Self::NonCallable(match literal {
                Lit::Str(_) => PrimKinds::STRING,
                Lit::Num(_) => PrimKinds::NUMBER,
                Lit::Bool(_) => PrimKinds::BOOLEAN,
                Lit::Null(_) => PrimKinds::NULL,
                Lit::BigInt(_) => PrimKinds::BIGINT,
                Lit::Regex(_) | Lit::JSXText(_) => PrimKinds::ANY,
            }),
            Expr::Tpl(_) => Self::NonCallable(PrimKinds::STRING),
            _ => Self::Unknown,
        }
    }

    pub fn bindings(&self, result: &mut BTreeSet<Id>) {
        match self {
            Self::Binding(id) => {
                result.insert(id.clone());
            }
            Self::ArrayIteratorPrototype(expression)
            | Self::SetIteratorPrototype(expression)
            | Self::LogicalNot(expression)
            | Self::Void(expression) => expression.bindings(result),
            Self::StrictEquality(left, right) => {
                left.bindings(result);
                right.bindings(result);
            }
            Self::Conditional { test, alternatives } => {
                test.bindings(result);
                for alternative in alternatives.iter() {
                    alternative.bindings(result);
                }
            }
            Self::Member(object, _, _, _) => object.bindings(result),
            Self::Call(callee, args, _) | Self::New(callee, args, _) => {
                callee.bindings(result);
                for arg in args.iter() {
                    arg.bindings(result);
                }
            }
            Self::Alternatives(options) | Self::Sequence(options) => {
                for option in options.iter() {
                    option.bindings(result);
                }
            }
            Self::Primitive(operands) => {
                for operand in operands.iter() {
                    operand.bindings(result);
                }
            }
            Self::ObjectLiteral(_, properties) => {
                for (_, value) in properties.iter() {
                    value.bindings(result);
                }
            }
            Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) | Self::Unknown => {}
        }
    }

    /// Whether lowering may have omitted inputs used to produce this value.
    /// A function or class allocation can capture locals without naming them
    /// in the value expression; computed keys and unknown syntax can also
    /// hide their operands.
    pub fn bindings_may_be_incomplete(&self) -> bool {
        match self {
            Self::Unknown | Self::Alloc(Kind::Function | Kind::Class, _) => true,
            Self::Binding(_) | Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) => false,
            Self::ArrayIteratorPrototype(value)
            | Self::SetIteratorPrototype(value)
            | Self::LogicalNot(value)
            | Self::Void(value) => value.bindings_may_be_incomplete(),
            Self::StrictEquality(left, right) => {
                left.bindings_may_be_incomplete() || right.bindings_may_be_incomplete()
            }
            Self::Conditional { test, alternatives } => {
                test.bindings_may_be_incomplete()
                    || alternatives.iter().any(Self::bindings_may_be_incomplete)
            }
            Self::Member(object, _, key, _) => {
                !matches!(key, MemberKey::Static) || object.bindings_may_be_incomplete()
            }
            Self::Call(callee, args, _) | Self::New(callee, args, _) => {
                callee.bindings_may_be_incomplete()
                    || args.iter().any(Self::bindings_may_be_incomplete)
            }
            Self::Alternatives(values) | Self::Sequence(values) | Self::Primitive(values) => {
                values.iter().any(Self::bindings_may_be_incomplete)
            }
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(name, value)| name.is_none() || value.bindings_may_be_incomplete()),
        }
    }

    /// Whether this value has an explicit namespace path through its syntax or
    /// a binding already proven to carry that path. Unrelated call/constructor
    /// results are covered by the namespace contract's author audit, while
    /// their use as mutation receivers is handled separately below.
    pub fn carries_namespace(&self, id: &Id, aliases: &BTreeSet<Id>) -> bool {
        match self {
            Self::Binding(binding) => binding == id || aliases.contains(binding),
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.carries_namespace(id, aliases)
            }
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.carries_namespace(id, aliases)),
            Self::Primitive(_) | Self::StrictEquality(..) | Self::LogicalNot(_) | Self::Void(_) => {
                false
            }
            Self::Alternatives(_) | Self::Conditional { .. } | Self::Sequence(_) => self
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| value.carries_namespace(id, aliases)),
            Self::Member(object, _, _, _) => {
                matches!(object.as_ref(), Self::Call(callee, _, _)
                    if Self::is_direct_namespace_member_callee(callee, id))
                    || object.carries_namespace(id, aliases)
            }
            Self::Call(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || (!Self::is_direct_namespace_member_callee(callee, id)
                        && callee.carries_namespace(id, aliases))
            }
            Self::New(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || callee.carries_namespace(id, aliases)
            }
            // Unknown syntax is not evidence that a value aliases the
            // namespace. Sinks still handle unknown receivers and
            // publications conservatively below.
            Self::Unknown => false,
            Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) => false,
        }
    }

    fn is_direct_namespace_member_callee(callee: &Self, id: &Id) -> bool {
        matches!(callee, Self::Member(object, Some(_), MemberKey::Static, _)
            if matches!(object.as_ref(), Self::Binding(namespace) if namespace == id))
    }

    /// Whether a value being published may expose the namespace. Unsupported
    /// syntax and unsupported projections remain possible namespace values;
    /// aliases to those values are propagated by `infer_namespace_aliases` so
    /// the uncertainty reaches a later sink.
    pub fn may_publish_namespace(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        self.may_publish_namespace_inner(
            id,
            aliases,
            facts,
            &mut BTreeSet::new(),
            &mut BTreeSet::new(),
        )
    }

    /// Equality with the namespace differs from publishing an object that
    /// contains it. A fresh literal is not the namespace, while a read of one
    /// of its ordinary own data properties can recover the namespace.
    pub(crate) fn may_equal_namespace(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
        visiting: &mut BTreeSet<Id>,
    ) -> bool {
        match self {
            Self::ObjectLiteral(_, _) | Self::Alloc(Kind::Function, _) => false,
            // Class metadata may contain a namespace path, including a
            // self-referential static initializer. Keep the existing
            // conservative class-surface fallback.
            Self::Alloc(Kind::Class, _) => self.may_publish_namespace(id, aliases, facts),
            Self::Binding(binding) => {
                if binding == id
                    || aliases.contains(binding)
                    || facts.unstable_class_bindings.contains(binding)
                {
                    return true;
                }
                if !visiting.insert(binding.clone()) {
                    return false;
                }
                let possible = facts
                    .binding_assignments
                    .get(binding)
                    .is_some_and(|values| {
                        values
                            .iter()
                            .any(|value| value.may_equal_namespace(id, aliases, facts, visiting))
                    });
                visiting.remove(binding);
                possible
            }
            Self::Alternatives(_) | Self::Conditional { .. } | Self::Sequence(_) => self
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| value.may_equal_namespace(id, aliases, facts, visiting)),
            Self::Member(object, Some(property), MemberKey::Static, _) => {
                if let Some(value) = facts.own_data_member(object, property, id, aliases) {
                    value.may_equal_namespace(id, aliases, facts, visiting)
                } else {
                    self.may_publish_namespace(id, aliases, facts)
                }
            }
            _ => self.may_publish_namespace(id, aliases, facts),
        }
    }

    pub(crate) fn may_publish_namespace_inner(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
        visiting_bindings: &mut BTreeSet<Id>,
        visiting_classes: &mut BTreeSet<(u32, ClassSurface)>,
    ) -> bool {
        match self {
            Self::Unknown => true,
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }
            Self::Primitive(_) | Self::StrictEquality(..) | Self::LogicalNot(_) | Self::Void(_) => {
                false
            }
            Self::ObjectLiteral(_, properties) => properties.iter().any(|(_, value)| {
                value.may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }),
            Self::Alternatives(_) | Self::Conditional { .. } | Self::Sequence(_) => self
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| {
                    value.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    )
                }),
            Self::Member(object, property, is_static, _) => {
                let projected = property
                    .as_deref()
                    .filter(|_| is_static.is_static())
                    .and_then(|property| facts.own_data_member(object, property, id, aliases));
                projected.unwrap_or(object).may_publish_namespace_inner(
                    id,
                    aliases,
                    facts,
                    visiting_bindings,
                    visiting_classes,
                )
            }
            Self::Call(callee, args, _) => {
                args.iter().any(|value| {
                    value.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    )
                }) || (!Self::is_direct_namespace_member_callee(callee, id)
                    && callee.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    ))
            }
            Self::New(callee, args, _) => {
                let local_class = facts
                    .resolve_class(callee, &mut BTreeSet::new())
                    .is_some_and(|(_, surface)| surface == ClassSurface::Constructor);
                args.iter().any(|value| {
                    value.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    )
                }) || (!local_class
                    && callee.may_publish_namespace_inner(
                        id,
                        aliases,
                        facts,
                        visiting_bindings,
                        visiting_classes,
                    ))
                    || facts.class_may_publish(
                        self,
                        id,
                        aliases,
                        visiting_bindings,
                        visiting_classes,
                    )
            }
            Self::Binding(binding) => {
                if binding == id
                    || aliases.contains(binding)
                    || facts.unstable_class_bindings.contains(binding)
                {
                    true
                } else if let Some(values) = facts.binding_assignments.get(binding) {
                    if !visiting_bindings.insert(binding.clone()) {
                        // A binding cycle contributes no namespace provenance
                        // without a namespace or unknown-value seed. The
                        // enclosing traversal still checks every alternative.
                        false
                    } else {
                        let result = values.iter().any(|value| {
                            value.may_publish_namespace_inner(
                                id,
                                aliases,
                                facts,
                                visiting_bindings,
                                visiting_classes,
                            )
                        });
                        visiting_bindings.remove(binding);
                        result
                    }
                } else {
                    false
                }
            }
            Self::Alloc(Kind::Class, _) => {
                facts.class_may_publish(self, id, aliases, visiting_bindings, visiting_classes)
            }
            Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) => false,
        }
    }

    /// Whether this value could be the namespace object used as a property
    /// write receiver. Unknown call and constructor results remain possible
    /// receivers here, even though merely storing or returning those unrelated
    /// results does not establish namespace provenance.
    pub fn may_be_namespace_mutation_receiver(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        receiver_aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        self.may_be_namespace_mutation_receiver_with_alias_query(
            id,
            aliases,
            &mut |binding| receiver_aliases.contains(binding),
            facts,
        )
    }

    pub fn may_be_namespace_mutation_receiver_with_alias_query(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        receiver_alias: &mut impl FnMut(&Id) -> bool,
        facts: &NamespaceValueFacts,
    ) -> bool {
        match self {
            Self::Binding(binding) => {
                binding == id || aliases.contains(binding) || receiver_alias(binding)
            }
            Self::ArrayIteratorPrototype(expression) | Self::SetIteratorPrototype(expression) => {
                expression.may_be_namespace_mutation_receiver_with_alias_query(
                    id,
                    aliases,
                    receiver_alias,
                    facts,
                )
            }
            Self::Primitive(_) | Self::StrictEquality(..) | Self::LogicalNot(_) | Self::Void(_) => {
                false
            }
            Self::ObjectLiteral(_, properties) => properties
                .iter()
                .any(|(_, value)| value.carries_namespace(id, aliases)),
            Self::Alternatives(_) | Self::Conditional { .. } | Self::Sequence(_) => self
                .result_alternatives()
                .expect("alternative expression")
                .iter()
                .any(|value| {
                    value.may_be_namespace_mutation_receiver_with_alias_query(
                        id,
                        aliases,
                        receiver_alias,
                        facts,
                    )
                }),
            Self::Member(object, property, is_static, _) => {
                if let Some(value) = property
                    .as_deref()
                    .filter(|_| is_static.is_static())
                    .and_then(|property| facts.own_data_member(object, property, id, aliases))
                {
                    return value.may_be_namespace_mutation_receiver_with_alias_query(
                        id,
                        aliases,
                        receiver_alias,
                        facts,
                    );
                }
                if facts.unique_literal(object).is_some() {
                    // A getter, computed key, spread, or prototype setter can
                    // return the namespace even when the literal is fresh.
                    return true;
                }
                matches!(object.as_ref(), Self::Call(callee, _, _)
                    if Self::is_direct_namespace_member_callee(callee, id))
                    || matches!(object.as_ref(), Self::Unknown | Self::New(..))
                    || object.may_be_namespace_mutation_receiver_with_alias_query(
                        id,
                        aliases,
                        receiver_alias,
                        facts,
                    )
            }
            Self::Call(callee, args, _) => {
                args.iter()
                    .any(|value| value.carries_namespace(id, aliases))
                    || !Self::is_direct_namespace_member_callee(callee, id)
            }
            Self::New(..) | Self::Unknown => true,
            Self::Alloc(..) | Self::Undefined | Self::NonCallable(_) => false,
        }
    }

    /// A callback result may contain one-hop namespace member reads as
    /// ordinary data-property values. That exception applies only to the
    /// direct returned object graph; aliases and other unknown shapes remain
    /// subject to the ordinary publication check.
    pub fn may_publish_namespace_in_factory_result(
        &self,
        id: &Id,
        aliases: &BTreeSet<Id>,
        facts: &NamespaceValueFacts,
    ) -> bool {
        match self {
            Self::ObjectLiteral(_, properties) => properties.iter().any(|(name, value)| {
                let direct_member_value = name.is_some()
                    && matches!(value, Self::Member(object, Some(_), MemberKey::Static, _)
                        if matches!(object.as_ref(), Self::Binding(namespace) if namespace == id));
                !direct_member_value
                    && value.may_publish_namespace_in_factory_result(id, aliases, facts)
            }),
            _ => self.may_publish_namespace(id, aliases, facts),
        }
    }
}
