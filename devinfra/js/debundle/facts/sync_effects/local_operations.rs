//! Source-covered local operations collected before value lowering erases writes.

use super::value_model::ValueExpr;
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
pub(super) enum LocalExpression {
    Value(ValueExpr),
    Sequence(Arc<[Self]>),
    Assign {
        binding: Id,
        value: Arc<Self>,
        span: Span,
    },
    Closure(u32),
    Call {
        callee: Id,
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
        ValueExpr::ObjectLiteral(_, properties) => properties
            .iter()
            .all(|(_, value)| unconditional_value(value)),
        ValueExpr::StrictEquality(left, right) => {
            unconditional_value(left) && unconditional_value(right)
        }
        ValueExpr::Void(value) => unconditional_value(value),
        _ => false,
    }
}

impl LocalExpression {
    fn optional(expression: Option<&Expr>) -> Option<Option<Self>> {
        match expression {
            Some(expression) => Some(Some(Self::lower(expression)?)),
            None => Some(None),
        }
    }

    fn lower(expression: &Expr) -> Option<Self> {
        match expression.unwrap_parens() {
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
                    .map(|expression| Self::lower(expression))
                    .collect::<Option<Arc<[_]>>>()?,
            )),
            Expr::Call(call) if !call.args.iter().any(|argument| argument.spread.is_some()) => {
                let Callee::Expr(callee) = &call.callee else {
                    return None;
                };
                let Expr::Ident(callee) = callee.unwrap_parens() else {
                    return None;
                };
                // A reference to eval can enter dynamic scope. Its binding
                // resolution remains on the existing conservative path.
                if callee.sym == "eval" {
                    return None;
                }
                Some(Self::Call {
                    callee: callee.to_id(),
                    arguments: call
                        .args
                        .iter()
                        .map(|argument| Self::lower(&argument.expr))
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
                let value = Arc::new(Self::lower(&assignment.right)?);
                match &assignment.left {
                    AssignTarget::Simple(SimpleAssignTarget::Ident(binding)) => {
                        Some(Self::Assign {
                            binding: binding.id.to_id(),
                            value,
                            span: assignment.span,
                        })
                    }
                    AssignTarget::Simple(SimpleAssignTarget::Member(member)) => {
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
            _ => {
                let (value, covered) = ValueExpr::from_expr_with_coverage(expression);
                (covered && unconditional_value(&value)).then_some(Self::Value(value))
            }
        }
    }

    fn local_targets(&self, bindings: &BTreeMap<Id, LocalBinding>) -> bool {
        match self {
            Self::Value(_) | Self::ReadField { .. } | Self::Closure(_) => true,
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
        }
    }

    fn reads(&self, reads: &mut BTreeSet<Id>) {
        match self {
            Self::Value(value) => value.bindings(reads),
            Self::Sequence(expressions) => {
                for expression in expressions.iter() {
                    expression.reads(reads);
                }
            }
            Self::Assign { binding, value, .. } => {
                reads.insert(binding.clone());
                value.reads(reads);
            }
            Self::Closure(_) => {}
            Self::Call {
                callee, arguments, ..
            } => {
                reads.insert(callee.clone());
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
        }
    }

    fn allocation_only_syntax(&self, bindings: &BTreeMap<Id, LocalBinding>) -> bool {
        match self {
            Self::Call { .. } | Self::Closure(_) => false,
            Self::Sequence(expressions) => expressions
                .iter()
                .all(|expression| expression.allocation_only_syntax(bindings)),
            Self::Assign { binding, value, .. } => {
                bindings.contains_key(binding) && value.allocation_only_syntax(bindings)
            }
            Self::AssignField { value, .. } => value.allocation_only_syntax(bindings),
            Self::Value(_) | Self::ReadField { .. } => true,
        }
    }

    fn writes(&self, writes: &mut BTreeSet<Id>) {
        match self {
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
            Self::AssignField { value, .. } => value.writes(writes),
            Self::Value(_) | Self::ReadField { .. } | Self::Closure(_) => {}
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
        let mut bindings = BTreeMap::new();
        for parameter in parameters {
            if bindings
                .insert(simple_binding(parameter)?, LocalBinding::Parameter)
                .is_some()
            {
                return None;
            }
        }
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
                        let value = LocalExpression::optional(declarator.init.as_deref())?;
                        if kind == LocalBinding::Const && value.is_none() {
                            return None;
                        }
                        operations.push(LocalOperation::Initialize(binding, value));
                    }
                }
                Stmt::Expr(expression) => operations.push(LocalOperation::Evaluate(
                    LocalExpression::lower(&expression.expr)?,
                )),
                Stmt::Return(statement) => {
                    operations.push(LocalOperation::Return(LocalExpression::optional(
                        statement.arg.as_deref(),
                    )?));
                    terminal = true;
                }
                Stmt::Throw(statement) => {
                    operations.push(LocalOperation::Throw(LocalExpression::lower(
                        &statement.arg,
                    )?));
                    terminal = true;
                }
                _ => return None,
            }
        }
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
