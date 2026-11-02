//! Whole-source-unit coverage for a small, non-invoking initializer subset.

use super::value_model::{AbstractValue, ValueExpr, Values};
use crate::facts::OwnerInitializationCoverageRefusalReason;
use crate::facts::abstract_domain::value::{Kind, PrimKinds, Value};
use crate::facts::item_views::{SplitDeclaratorProvenance, TopLevelItemView};
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;

#[derive(Clone)]
enum Leaf {
    Primitive(PrimKinds),
    Function(u32),
    Binding(Id),
    Allocation {
        allocation: u32,
        reads: BTreeSet<Id>,
    },
}

/// Collected from the original owner before ValueExpr can erase syntax.
/// This says nothing about executing a function's lazy body.
#[derive(Clone)]
pub(super) struct OriginalInitializer {
    owner: SourceUnitOrdinal,
    binding: Id,
    initializer_span: Span,
    leaf: Leaf,
}

pub(super) struct EvaluatedInitializer {
    owner: SourceUnitOrdinal,
    initializer_span: Span,
}

/// Terminal evidence for this source owner only. Never composed through a
/// child ledger, generic Evaluate result, callee outcome or invocation memo.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct CompletedInitialization {
    owner: SourceUnitOrdinal,
}

impl CompletedInitialization {
    pub(crate) fn covers(&self, owner: SourceUnitOrdinal) -> bool {
        self.owner == owner
    }
}

fn refusal_for_non_variable_owner(item: &ModuleItem) -> OwnerInitializationCoverageRefusalReason {
    if matches!(
        item,
        ModuleItem::ModuleDecl(
            ModuleDecl::ExportDecl(_)
                | ModuleDecl::ExportNamed(_)
                | ModuleDecl::ExportDefaultDecl(_)
                | ModuleDecl::ExportDefaultExpr(_)
                | ModuleDecl::ExportAll(_)
                | ModuleDecl::TsExportAssignment(_)
                | ModuleDecl::TsNamespaceExport(_)
        )
    ) {
        OwnerInitializationCoverageRefusalReason::ExportedOwner
    } else {
        OwnerInitializationCoverageRefusalReason::UnsupportedOwnerKind
    }
}

fn same_simple_binding(left: &Pat, right: &Pat) -> bool {
    let (Pat::Ident(left), Pat::Ident(right)) = (left, right) else {
        return false;
    };
    left.id.to_id() == right.id.to_id()
        && left.id.span == right.id.span
        && left.id.optional == right.id.optional
        && left.type_ann.as_ref().map(|annotation| annotation.span())
            == right.type_ann.as_ref().map(|annotation| annotation.span())
}

fn original_declarator<'source>(
    item: &TopLevelItemView<'source>,
) -> Result<(&'source VarDecl, &'source VarDeclarator), OwnerInitializationCoverageRefusalReason> {
    match item {
        TopLevelItemView::Borrowed(item) => {
            let ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration))) = item else {
                return Err(refusal_for_non_variable_owner(item));
            };
            let [declarator] = declaration.decls.as_slice() else {
                return Err(OwnerInitializationCoverageRefusalReason::SplitDeclaration);
            };
            Ok((declaration, declarator))
        }
        TopLevelItemView::Owned { item, provenance } => original_split_declarator(item, provenance),
    }
}

fn original_split_declarator<'source>(
    view: &ModuleItem,
    provenance: &SplitDeclaratorProvenance<'source>,
) -> Result<(&'source VarDecl, &'source VarDeclarator), OwnerInitializationCoverageRefusalReason> {
    use OwnerInitializationCoverageRefusalReason::SplitDeclaration;

    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(original))) = provenance.parent_item else {
        return Err(SplitDeclaration);
    };
    if original.decls.len() <= 1 {
        return Err(SplitDeclaration);
    }
    let Some(declarator) = original.decls.get(provenance.declarator_index) else {
        return Err(SplitDeclaration);
    };
    let ModuleItem::Stmt(Stmt::Decl(Decl::Var(single))) = view else {
        return Err(SplitDeclaration);
    };
    let [single_declarator] = single.decls.as_slice() else {
        return Err(SplitDeclaration);
    };
    let same_initializer_span = single_declarator.init.as_ref().map(|init| init.span())
        == declarator.init.as_ref().map(|init| init.span());
    if single.kind != original.kind
        || single.ctxt != original.ctxt
        || single.declare != original.declare
        || single_declarator.span != declarator.span
        || !same_simple_binding(&single_declarator.name, &declarator.name)
        || !same_initializer_span
    {
        return Err(SplitDeclaration);
    }
    Ok((original, declarator))
}

fn ordinary_parameter(parameter: &Pat) -> bool {
    matches!(parameter, Pat::Ident(binding)
        if binding.type_ann.is_none() && !binding.id.optional)
}

/// Static data fields and dense array elements use CreateDataProperty, without
/// invoking receiver accessors. Retain every declarative-cell read, including
/// overwritten duplicate fields. No value-kind or heap-locality premise is needed.
fn allocation_inputs(expression: &Expr, reads: &mut BTreeSet<Id>) -> Option<()> {
    match expression.unwrap_parens() {
        Expr::Ident(binding) => {
            reads.insert(binding.to_id());
        }
        Expr::Lit(Lit::Str(_) | Lit::Num(_) | Lit::Bool(_) | Lit::Null(_) | Lit::BigInt(_)) => {}
        Expr::Object(record) => {
            for property in &record.props {
                let PropOrSpread::Prop(property) = property else {
                    return None;
                };
                match property.as_ref() {
                    Prop::Shorthand(binding) => {
                        reads.insert(binding.to_id());
                    }
                    Prop::KeyValue(property) => {
                        let name = match &property.key {
                            PropName::Ident(name) => name.sym.as_ref(),
                            PropName::Str(name) => name.value.as_str()?,
                            _ => return None,
                        };
                        // A colon-form __proto__ field changes the prototype
                        // rather than creating an own data property.
                        if name == "__proto__" {
                            return None;
                        }
                        allocation_inputs(&property.value, reads)?;
                    }
                    _ => return None,
                }
            }
        }
        Expr::Array(array) => {
            for element in &array.elems {
                let element = element.as_ref()?;
                if element.spread.is_some() {
                    return None;
                }
                allocation_inputs(&element.expr, reads)?;
            }
        }
        _ => return None,
    }
    Some(())
}

impl OriginalInitializer {
    pub(super) fn collect(
        item: &TopLevelItemView<'_>,
        owner: SourceUnitOrdinal,
    ) -> Result<Self, OwnerInitializationCoverageRefusalReason> {
        // For split views, recover only the exact original child named by the
        // canonical view's parent/index provenance. The child's own evaluation
        // remains one source-unit owner; sibling initializers are separate
        // owners and keep separate effect records.
        let (declaration, declarator) = original_declarator(item)?;
        let Pat::Ident(binding) = &declarator.name else {
            return Err(OwnerInitializationCoverageRefusalReason::UnsupportedBindingShape);
        };
        if declaration.declare || declarator.definite || !ordinary_parameter(&declarator.name) {
            return Err(OwnerInitializationCoverageRefusalReason::UnsupportedBindingShape);
        }
        let Some(initializer) = declarator.init.as_deref() else {
            return Err(OwnerInitializationCoverageRefusalReason::MissingInitializer);
        };
        let leaf = match initializer.unwrap_parens() {
            Expr::Ident(binding) => Leaf::Binding(binding.to_id()),
            Expr::Object(_) | Expr::Array(_) => {
                let mut reads = BTreeSet::new();
                allocation_inputs(initializer, &mut reads)
                    .ok_or(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer)?;
                Leaf::Allocation {
                    allocation: initializer.unwrap_parens().span().lo.0,
                    reads,
                }
            }
            Expr::Lit(literal) => Leaf::Primitive(match literal {
                Lit::Str(_) => PrimKinds::STRING,
                Lit::Num(_) => PrimKinds::NUMBER,
                Lit::Bool(_) => PrimKinds::BOOLEAN,
                Lit::Null(_) => PrimKinds::NULL,
                Lit::BigInt(_) => PrimKinds::BIGINT,
                _ => {
                    return Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer);
                }
            }),
            Expr::Fn(expression)
                if !expression.function.is_async
                    && !expression.function.is_generator
                    && expression.function.body.is_some()
                    && expression.function.type_params.is_none()
                    && expression.function.return_type.is_none()
                    && expression.function.decorators.is_empty()
                    && expression.function.params.iter().all(|parameter| {
                        parameter.decorators.is_empty() && ordinary_parameter(&parameter.pat)
                    }) =>
            {
                Leaf::Function(expression.function.span.lo.0)
            }
            Expr::Arrow(expression)
                if !expression.is_async
                    && expression.type_params.is_none()
                    && expression.return_type.is_none()
                    && expression.params.iter().all(ordinary_parameter) =>
            {
                Leaf::Function(expression.span.lo.0)
            }
            _ => return Err(OwnerInitializationCoverageRefusalReason::UnsupportedInitializer),
        };
        Ok(Self {
            owner,
            binding: binding.id.to_id(),
            initializer_span: initializer.span(),
            leaf,
        })
    }

    pub(super) fn matches_binding(&self, owner: SourceUnitOrdinal, binding: &Id) -> bool {
        self.owner == owner && self.binding == *binding
    }

    pub(super) fn reads_are_initialized(&self, mut initialized: impl FnMut(&Id) -> bool) -> bool {
        match &self.leaf {
            Leaf::Binding(binding) => initialized(binding),
            Leaf::Allocation { reads, .. } => reads.iter().all(initialized),
            Leaf::Primitive(_) | Leaf::Function(_) => true,
        }
    }

    /// The dedicated root Evaluate transfer must actually finish with its
    /// expected value. Every source cell read must be initialized before that
    /// transfer; imported and global reads cannot supply this proof.
    /// Generic lowering or an empty effect ledger is no proof.
    pub(super) fn evaluated(
        &self,
        expression: &ValueExpr,
        values: &Values,
        initialized_reads: bool,
    ) -> Option<EvaluatedInitializer> {
        if values.is_empty() || values.contains(&AbstractValue::Value(Value::Unavailable)) {
            return None;
        }
        let matches = match (&self.leaf, expression, values.first()?) {
            (Leaf::Binding(expected), ValueExpr::Binding(lowered), _) => {
                expected == lowered && initialized_reads
            }
            (
                Leaf::Allocation { allocation, .. },
                ValueExpr::ObjectLiteral(lowered, _),
                AbstractValue::Value(Value::Alloc(Kind::Object, evaluated)),
            ) => {
                values.len() == 1
                    && allocation == lowered
                    && allocation == evaluated
                    && initialized_reads
            }
            (
                Leaf::Primitive(expected),
                ValueExpr::NonCallable(lowered),
                AbstractValue::Value(Value::Prim(evaluated)),
            ) => values.len() == 1 && expected == lowered && expected == evaluated,
            (
                Leaf::Function(expected),
                ValueExpr::Alloc(Kind::Function, lowered),
                AbstractValue::Closure(evaluated, _),
            ) => values.len() == 1 && expected == lowered && expected == evaluated,
            _ => false,
        };
        matches.then_some(EvaluatedInitializer {
            owner: self.owner,
            initializer_span: self.initializer_span,
        })
    }

    pub(super) fn complete(
        &self,
        evaluated: EvaluatedInitializer,
        owner: SourceUnitOrdinal,
        initialized: &BTreeSet<Id>,
    ) -> Option<CompletedInitialization> {
        (self.owner == owner
            && evaluated.owner == owner
            && evaluated.initializer_span == self.initializer_span
            && initialized.contains(&self.binding))
        .then_some(CompletedInitialization { owner })
    }
}
