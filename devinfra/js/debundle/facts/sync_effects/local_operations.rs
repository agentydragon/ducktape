//! Source-covered local operations collected before value lowering erases writes.

use super::value_model::{ValueExpr, object_data_property_name, static_property_name};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::Span;
use swc_ecma_ast::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum LocalBinding {
    Parameter,
    Var,
    Let,
    Const,
}

#[derive(Debug, Clone)]
pub(super) enum LocalCallee {
    Binding(Id),
    Member {
        receiver: Id,
        property: String,
        span: Span,
    },
}

#[derive(Debug, Clone)]
pub(super) enum LocalExpression {
    Value(ValueExpr),
    /// The fresh receiver supplied by a checked constructor query.
    This,
    Sequence(Arc<[Self]>),
    Record {
        allocation: u32,
        properties: Arc<[(String, Self)]>,
    },
    Assign {
        binding: Id,
        value: Arc<Self>,
        span: Span,
    },
    Closure(u32),
    Call {
        callee: LocalCallee,
        arguments: Arc<[Self]>,
        span: Span,
    },
    ReadField {
        receiver: Id,
        property: String,
        span: Span,
    },
    AssignField {
        receiver: Id,
        property: String,
        value: Arc<Self>,
        span: Span,
    },
    /// Class field definition and declared private-field assignment both
    /// contribute weak containment to the fresh-instance retention projection.
    /// This does not model public/private descriptor identity or authorize a
    /// later field read.
    DefineThisField {
        property: String,
        value: Arc<Self>,
    },
}

#[derive(Debug, Clone)]
pub(super) enum LocalOperation {
    Initialize(Id, Option<LocalExpression>),
    Evaluate(LocalExpression),
    Return(Option<LocalExpression>),
    Throw(LocalExpression),
}

#[derive(Debug, Clone)]
pub(super) struct LocalBody {
    pub(super) bindings: BTreeMap<Id, LocalBinding>,
    pub(super) operations: Arc<[LocalOperation]>,
    /// Sorted source reads outside this body's parameters and declarations.
    /// Shared across body clones and reused by every invocation/provider query.
    free_reads: Option<Arc<[Id]>>,
    captured_writes: Option<Arc<[Id]>>,
    allocation_only_syntax: bool,
}

fn simple_binding(pattern: &Pat) -> Option<Id> {
    match pattern {
        Pat::Ident(binding) if binding.type_ann.is_none() && !binding.id.optional => {
            Some(binding.id.to_id())
        }
        _ => None,
    }
}

/// The central allocation transfer has no conditional-path provenance. Keep
/// branches outside this ordered subset until their effects can be tagged.
fn unconditional_value(value: &ValueExpr) -> bool {
    match value {
        ValueExpr::Binding(_) | ValueExpr::NonCallable(_) | ValueExpr::Undefined => true,
        ValueExpr::New(callee, arguments, _)
            if arguments.is_empty()
                && matches!(callee.as_ref(), ValueExpr::Binding(binding)
                    if matches!(binding.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet")) =>
        {
            true
        }
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .all(|(_, value)| unconditional_value(value)),
        ValueExpr::StrictEquality(left, right) => {
            unconditional_value(left) && unconditional_value(right)
        }
        ValueExpr::Void(value) | ValueExpr::LogicalNot(value) => unconditional_value(value),
        ValueExpr::Sequence(values) => values.iter().all(unconditional_value),
        _ => false,
    }
}

impl LocalExpression {
    fn optional(expression: Option<&Expr>, class_template: bool) -> Option<Option<Self>> {
        match expression {
            Some(expression) => Some(Some(Self::lower(expression, class_template)?)),
            None => Some(None),
        }
    }

    fn lower(expression: &Expr, class_template: bool) -> Option<Self> {
        match expression.unwrap_parens() {
            Expr::Object(object) => {
                let properties = object
                    .props
                    .iter()
                    .map(|property| {
                        let PropOrSpread::Prop(property) = property else {
                            return None;
                        };
                        match property.as_ref() {
                            Prop::Shorthand(binding) => Some((
                                binding.sym.to_string(),
                                Self::Value(ValueExpr::Binding(binding.to_id())),
                            )),
                            Prop::KeyValue(pair)
                                if matches!(
                                    pair.key,
                                    PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                                ) =>
                            {
                                Some((
                                    object_data_property_name(&pair.key)?,
                                    Self::lower(&pair.value, class_template)?,
                                ))
                            }
                            Prop::Method(method)
                                if class_template
                                    && method.function.decorators.is_empty()
                                    && object_data_property_name(&method.key).is_some() =>
                            {
                                Some((
                                    object_data_property_name(&method.key)?,
                                    Self::Closure(method.function.span.lo.0),
                                ))
                            }
                            Prop::Getter(getter)
                                if class_template
                                    && object_data_property_name(&getter.key).is_some() =>
                            {
                                Some((
                                    object_data_property_name(&getter.key)?,
                                    Self::Closure(getter.function.span.lo.0),
                                ))
                            }
                            Prop::Setter(setter)
                                if class_template
                                    && object_data_property_name(&setter.key).is_some() =>
                            {
                                Some((
                                    object_data_property_name(&setter.key)?,
                                    Self::Closure(setter.span.lo.0),
                                ))
                            }
                            _ => None,
                        }
                    })
                    .collect::<Option<Arc<[_]>>>()?;
                Some(Self::Record {
                    allocation: object.span.lo.0,
                    properties,
                })
            }
            Expr::Fn(function)
                if function.ident.is_none()
                    && function.function.decorators.is_empty()
                    && function
                        .function
                        .params
                        .iter()
                        .all(|parameter| parameter.decorators.is_empty()) =>
            {
                Some(Self::Closure(function.function.span.lo.0))
            }
            Expr::Arrow(arrow) => Some(Self::Closure(arrow.span.lo.0)),
            Expr::Seq(sequence) => Some(Self::Sequence(
                sequence
                    .exprs
                    .iter()
                    .map(|expression| Self::lower(expression, class_template))
                    .collect::<Option<Arc<[_]>>>()?,
            )),
            Expr::Call(call) if !call.args.iter().any(|argument| argument.spread.is_some()) => {
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                let callee = match callee.unwrap_parens() {
                    // A direct eval reference can enter dynamic scope.
                    Expr::Ident(binding) if binding.sym != "eval" => {
                        LocalCallee::Binding(binding.to_id())
                    }
                    Expr::Member(member) => {
                        let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                            return None;
                        };
                        let MemberProp::Ident(property) = &member.prop else {
                            return None;
                        };
                        LocalCallee::Member {
                            receiver: receiver.to_id(),
                            property: property.sym.to_string(),
                            span: member.span,
                        }
                    }
                    _ => return None,
                };
                Some(Self::Call {
                    callee,
                    arguments: call
                        .args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr, class_template))
                        .collect::<Option<Arc<[_]>>>()?,
                    span: call.span,
                })
            }
            Expr::Member(member) => {
                let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                    return None;
                };
                let MemberProp::Ident(property) = &member.prop else {
                    return None;
                };
                Some(Self::ReadField {
                    receiver: receiver.to_id(),
                    property: property.sym.to_string(),
                    span: member.span,
                })
            }
            Expr::Assign(assignment) if assignment.op == AssignOp::Assign => {
                let value = Arc::new(Self::lower(&assignment.right, class_template)?);
                match &assignment.left {
                    AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                        Some(Self::Assign {
                            binding: binding.id.to_id(),
                            value,
                            span: assignment.span,
                        })
                    }
                    AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
                        if class_template
                            && matches!(member.obj.unwrap_parens(), Expr::This(_))
                            && let MemberProp::PrivateName(private) = &member.prop
                        {
                            return Some(Self::DefineThisField {
                                property: private.name.to_string(),
                                value,
                            });
                        }
                        let Expr::Ident(receiver) = member.obj.unwrap_parens() else {
                            return None;
                        };
                        let MemberProp::Ident(property) = &member.prop else {
                            return None;
                        };
                        Some(Self::AssignField {
                            receiver: receiver.to_id(),
                            property: property.sym.to_string(),
                            value,
                            span: assignment.span,
                        })
                    }
                    _ => None,
                }
            }
            Expr::This(_) if class_template => Some(Self::This),
            _ => {
                let (value, covered) = ValueExpr::from_expr_with_coverage(expression);
                let class_native_constructor = class_template
                    && matches!(
                        &value,
                        ValueExpr::New(callee, arguments, _)
                            if arguments.is_empty()
                                && matches!(callee.as_ref(), ValueExpr::Binding(binding)
                                    if matches!(binding.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet"))
                    );
                (covered || class_native_constructor)
                    .then(|| unconditional_value(&value).then_some(Self::Value(value)))
                    .flatten()
            }
        }
    }

    fn local_targets(&self, bindings: &BTreeMap<Id, LocalBinding>) -> bool {
        match self {
            Self::Value(_) | Self::This | Self::ReadField { .. } | Self::Closure(_) => true,
            Self::Record { properties, .. } => properties
                .iter()
                .all(|(_, value)| value.local_targets(bindings)),
            Self::Sequence(expressions) => expressions
                .iter()
                .all(|expression| expression.local_targets(bindings)),
            Self::Assign { value, .. } => value.local_targets(bindings),
            Self::Call { arguments, .. } => arguments
                .iter()
                .all(|argument| argument.local_targets(bindings)),
            Self::AssignField {
                receiver, value, ..
            } => bindings.contains_key(receiver) && value.local_targets(bindings),
            Self::DefineThisField { value, .. } => value.local_targets(bindings),
        }
    }

    fn reads(&self, reads: &mut BTreeSet<Id>) {
        match self {
            Self::Value(value) => value.bindings(reads),
            Self::Record { properties, .. } => {
                for (_, value) in properties.iter() {
                    value.reads(reads);
                }
            }
            Self::Sequence(expressions) => {
                for expression in expressions.iter() {
                    expression.reads(reads);
                }
            }
            Self::Assign { binding, value, .. } => {
                reads.insert(binding.clone());
                value.reads(reads);
            }
            Self::Closure(_) | Self::This => {}
            Self::Call {
                callee, arguments, ..
            } => {
                let (LocalCallee::Binding(binding)
                | LocalCallee::Member {
                    receiver: binding, ..
                }) = callee;
                reads.insert(binding.clone());
                for argument in arguments.iter() {
                    argument.reads(reads);
                }
            }
            Self::ReadField { receiver, .. } => {
                reads.insert(receiver.clone());
            }
            Self::AssignField {
                receiver, value, ..
            } => {
                reads.insert(receiver.clone());
                value.reads(reads);
            }
            Self::DefineThisField { value, .. } => value.reads(reads),
        }
    }

    fn allocation_only_syntax(&self, bindings: &BTreeMap<Id, LocalBinding>) -> bool {
        match self {
            Self::Call { .. } | Self::Closure(_) => false,
            Self::Record { properties, .. } => properties
                .iter()
                .all(|(_, value)| value.allocation_only_syntax(bindings)),
            Self::Sequence(expressions) => expressions
                .iter()
                .all(|expression| expression.allocation_only_syntax(bindings)),
            Self::Assign { binding, value, .. } => {
                bindings.contains_key(binding) && value.allocation_only_syntax(bindings)
            }
            Self::AssignField { value, .. } | Self::DefineThisField { value, .. } => {
                value.allocation_only_syntax(bindings)
            }
            Self::Value(_) | Self::This | Self::ReadField { .. } => true,
        }
    }

    fn writes(&self, writes: &mut BTreeSet<Id>) {
        match self {
            Self::Record { properties, .. } => {
                for (_, value) in properties.iter() {
                    value.writes(writes);
                }
            }
            Self::Assign { binding, value, .. } => {
                writes.insert(binding.clone());
                value.writes(writes);
            }
            Self::Sequence(expressions) => {
                for expression in expressions.iter() {
                    expression.writes(writes);
                }
            }
            Self::Call { arguments, .. } => {
                for argument in arguments.iter() {
                    argument.writes(writes);
                }
            }
            Self::AssignField { value, .. } | Self::DefineThisField { value, .. } => {
                value.writes(writes)
            }
            Self::Value(_) | Self::This | Self::ReadField { .. } | Self::Closure(_) => {}
        }
    }
}

impl LocalBody {
    /// Called by the canonical function collector. Unsupported syntax discards
    /// the entire candidate; the ordinary conservative lanes remain available.
    pub(super) fn lower<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
        body: &FunctionBody,
    ) -> Option<Self> {
        let mut bindings = Self::parameter_bindings(parameters)?;
        let mut operations = Vec::new();
        let mut terminal = false;
        for statement in &body.stmts {
            if terminal {
                return None;
            }
            match statement {
                Stmt::Empty(_) => {}
                Stmt::Decl(Decl::Var(declaration)) if !declaration.declare => {
                    let kind = match declaration.kind {
                        VarDeclKind::Var => LocalBinding::Var,
                        VarDeclKind::Let => LocalBinding::Let,
                        VarDeclKind::Const => LocalBinding::Const,
                    };
                    for declarator in &declaration.decls {
                        if declarator.definite {
                            return None;
                        }
                        let binding = simple_binding(&declarator.name)?;
                        match bindings.get(&binding) {
                            Some(LocalBinding::Var | LocalBinding::Parameter)
                                if kind == LocalBinding::Var => {}
                            Some(_) => return None,
                            None => {
                                bindings.insert(binding.clone(), kind);
                            }
                        }
                        let value = LocalExpression::optional(declarator.init.as_deref(), false)?;
                        if kind == LocalBinding::Const && value.is_none() {
                            return None;
                        }
                        operations.push(LocalOperation::Initialize(binding, value));
                    }
                }
                Stmt::Expr(expression) => operations.push(LocalOperation::Evaluate(
                    LocalExpression::lower(&expression.expr, false)?,
                )),
                Stmt::Return(statement) => {
                    operations.push(LocalOperation::Return(LocalExpression::optional(
                        statement.arg.as_deref(),
                        false,
                    )?));
                    terminal = true;
                }
                Stmt::Throw(statement) => {
                    operations.push(LocalOperation::Throw(LocalExpression::lower(
                        &statement.arg,
                        false,
                    )?));
                    terminal = true;
                }
                _ => return None,
            }
        }
        Self::from_operations(bindings, operations)
    }

    /// A concise arrow has the same ordered return semantics as a block body.
    /// Retain that operation directly without manufacturing or revisiting AST.
    pub(super) fn lower_expression<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
        expression: &Expr,
    ) -> Option<Self> {
        Self::from_operations(
            Self::parameter_bindings(parameters)?,
            vec![LocalOperation::Return(Some(LocalExpression::lower(
                expression, false,
            )?))],
        )
    }

    /// Canonically lower the allocation effects of a base class constructor.
    /// The caller still proves binding stability and native identity; this
    /// collector retains every constructor-time field operation in order.
    pub(super) fn lower_class_constructor(class: &Class) -> Option<(Self, Vec<Id>)> {
        if class.super_class.is_some() || !class.decorators.is_empty() {
            return None;
        }

        let mut constructor = None;
        let mut private_fields = BTreeSet::new();
        let mut field_operations = Vec::new();
        for member in &class.body {
            match member {
                ClassMember::Constructor(definition) if constructor.is_none() => {
                    constructor = Some(definition);
                }
                ClassMember::Constructor(_) => return None,
                ClassMember::PrivateProp(field) => {
                    if !field.decorators.is_empty() {
                        return None;
                    }
                    if !field.is_static {
                        let property = field.key.name.to_string();
                        private_fields.insert(property.clone());
                        if let Some(value) = field.value.as_deref() {
                            field_operations.push(LocalOperation::Evaluate(
                                LocalExpression::DefineThisField {
                                    property,
                                    value: Arc::new(LocalExpression::lower(value, true)?),
                                },
                            ));
                        }
                    }
                }
                ClassMember::ClassProp(field) => {
                    if !field.decorators.is_empty() {
                        return None;
                    }
                    if !field.is_static {
                        if !matches!(
                            field.key,
                            PropName::Ident(_) | PropName::Str(_) | PropName::Num(_)
                        ) {
                            return None;
                        }
                        if let Some(value) = field.value.as_deref() {
                            field_operations.push(LocalOperation::Evaluate(
                                LocalExpression::DefineThisField {
                                    property: static_property_name(&field.key)?,
                                    value: Arc::new(LocalExpression::lower(value, true)?),
                                },
                            ));
                        }
                    }
                }
                ClassMember::AutoAccessor(accessor) if !accessor.is_static => return None,
                ClassMember::Method(method) if !method.function.decorators.is_empty() => {
                    return None;
                }
                ClassMember::PrivateMethod(method) if !method.function.decorators.is_empty() => {
                    return None;
                }
                _ => {}
            }
        }

        let mut bindings = BTreeMap::new();
        let mut parameters = Vec::new();
        let mut constructor_operations = Vec::new();
        if let Some(constructor) = constructor {
            for parameter in &constructor.params {
                let ParamOrTsParamProp::Param(parameter) = parameter else {
                    return None;
                };
                if !parameter.decorators.is_empty() {
                    return None;
                }
                let Pat::Ident(binding) = &parameter.pat else {
                    return None;
                };
                if bindings
                    .insert(binding.id.to_id(), LocalBinding::Parameter)
                    .is_some()
                {
                    return None;
                }
                parameters.push(binding.id.to_id());
            }

            for statement in &constructor.body.as_ref()?.stmts {
                match statement {
                    Stmt::Empty(_) => {}
                    Stmt::Decl(Decl::Var(declaration)) if !declaration.declare => {
                        let kind = match declaration.kind {
                            VarDeclKind::Var => LocalBinding::Var,
                            VarDeclKind::Let => LocalBinding::Let,
                            VarDeclKind::Const => LocalBinding::Const,
                        };
                        for declarator in &declaration.decls {
                            if declarator.definite {
                                return None;
                            }
                            let binding = simple_binding(&declarator.name)?;
                            match bindings.get(&binding) {
                                Some(LocalBinding::Var | LocalBinding::Parameter)
                                    if kind == LocalBinding::Var => {}
                                Some(_) => return None,
                                None => {
                                    bindings.insert(binding.clone(), kind);
                                }
                            }
                            let value =
                                LocalExpression::optional(declarator.init.as_deref(), true)?;
                            if kind == LocalBinding::Const && value.is_none() {
                                return None;
                            }
                            constructor_operations.push(LocalOperation::Initialize(binding, value));
                        }
                    }
                    Stmt::Expr(expression) => {
                        let operation = LocalExpression::lower(&expression.expr, true)?;
                        let LocalExpression::DefineThisField { property, .. } = &operation else {
                            return None;
                        };
                        if !private_fields.contains(property) {
                            return None;
                        }
                        constructor_operations.push(LocalOperation::Evaluate(operation));
                    }
                    Stmt::Return(statement) => {
                        constructor_operations.push(LocalOperation::Return(
                            LocalExpression::optional(statement.arg.as_deref(), true)?,
                        ));
                        break;
                    }
                    _ => return None,
                }
            }
        }

        field_operations.extend(constructor_operations);
        Some((
            Self::from_operations(bindings, field_operations)?,
            parameters,
        ))
    }

    fn parameter_bindings<'a>(
        parameters: impl Iterator<Item = &'a Pat>,
    ) -> Option<BTreeMap<Id, LocalBinding>> {
        let mut bindings = BTreeMap::new();
        for parameter in parameters {
            if bindings
                .insert(simple_binding(parameter)?, LocalBinding::Parameter)
                .is_some()
            {
                return None;
            }
        }
        Some(bindings)
    }

    fn from_operations(
        bindings: BTreeMap<Id, LocalBinding>,
        operations: Vec<LocalOperation>,
    ) -> Option<Self> {
        if !Self::expressions(&operations).all(|expression| expression.local_targets(&bindings)) {
            return None;
        }
        let mut reads = BTreeSet::new();
        for expression in Self::expressions(&operations) {
            expression.reads(&mut reads);
        }
        reads.retain(|binding| !bindings.contains_key(binding));
        let mut writes = BTreeSet::new();
        for expression in Self::expressions(&operations) {
            expression.writes(&mut writes);
        }
        writes.retain(|binding| !bindings.contains_key(binding));
        let allocation_only_syntax = Self::expressions(&operations)
            .all(|expression| expression.allocation_only_syntax(&bindings));
        Some(Self {
            bindings,
            operations: operations.into(),
            free_reads: (!reads.is_empty()).then(|| reads.into_iter().collect()),
            captured_writes: (!writes.is_empty()).then(|| writes.into_iter().collect()),
            allocation_only_syntax,
        })
    }

    pub(super) fn free_reads(&self) -> &[Id] {
        self.free_reads.as_deref().unwrap_or_default()
    }

    pub(super) fn captured_writes(&self) -> &[Id] {
        self.captured_writes.as_deref().unwrap_or_default()
    }

    /// Calls, closures and captured writes have not migrated to the
    /// allocation-only provider proof. Classify before selecting that
    /// proof; a failed migrated query still never retries another evaluator.
    pub(super) fn allocation_only_syntax(&self) -> bool {
        self.allocation_only_syntax
    }

    fn expressions(operations: &[LocalOperation]) -> impl Iterator<Item = &LocalExpression> {
        operations.iter().filter_map(|operation| match operation {
            LocalOperation::Initialize(_, value) | LocalOperation::Return(value) => value.as_ref(),
            LocalOperation::Evaluate(value) | LocalOperation::Throw(value) => Some(value),
        })
    }
}
