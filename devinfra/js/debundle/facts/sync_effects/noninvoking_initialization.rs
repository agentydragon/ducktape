//! Whole-original-unit coverage for a small, non-invoking initializer subset.

use super::value_model::{AbstractValue, ValueExpr, Values};
use crate::facts::abstract_domain::value::{Kind, PrimKinds, Value};
use crate::facts::item_views::TopLevelItemView;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use swc_common::{Span, Spanned};
use swc_ecma_ast::*;

#[derive(Clone)]
enum Leaf {
    Primitive(PrimKinds),
    Function(u32),
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

fn ordinary_parameter(parameter: &Pat) -> bool {
    matches!(parameter, Pat::Ident(binding)
        if binding.type_ann.is_none() && !binding.id.optional)
}

impl OriginalInitializer {
    pub(super) fn collect(item: &TopLevelItemView<'_>, owner: SourceUnitOrdinal) -> Option<Self> {
        // Owned views can be split comma declarations: they cannot prove the
        // original statement's complete eager evaluation envelope.
        let TopLevelItemView::Borrowed(ModuleItem::Stmt(Stmt::Decl(Decl::Var(declaration)))) = item
        else {
            return None;
        };
        let [declarator] = declaration.decls.as_slice() else {
            return None;
        };
        let Pat::Ident(binding) = &declarator.name else {
            return None;
        };
        if declaration.declare || declarator.definite || !ordinary_parameter(&declarator.name) {
            return None;
        }
        let initializer = declarator.init.as_deref()?;
        let leaf = match initializer.unwrap_parens() {
            Expr::Lit(literal) => Leaf::Primitive(match literal {
                Lit::Str(_) => PrimKinds::STRING,
                Lit::Num(_) => PrimKinds::NUMBER,
                Lit::Bool(_) => PrimKinds::BOOLEAN,
                Lit::Null(_) => PrimKinds::NULL,
                Lit::BigInt(_) => PrimKinds::BIGINT,
                _ => return None,
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
            _ => return None,
        };
        Some(Self {
            owner,
            binding: binding.id.to_id(),
            initializer_span: initializer.span(),
            leaf,
        })
    }

    pub(super) fn matches_binding(&self, owner: SourceUnitOrdinal, binding: &Id) -> bool {
        self.owner == owner && self.binding == *binding
    }

    /// The dedicated root Evaluate transfer must actually finish with its
    /// expected value. Generic lowering or an empty effect ledger is no proof.
    pub(super) fn evaluated(
        &self,
        expression: &ValueExpr,
        values: &Values,
    ) -> Option<EvaluatedInitializer> {
        if values.len() != 1 {
            return None;
        }
        let matches = match (&self.leaf, expression, values.first()?) {
            (
                Leaf::Primitive(expected),
                ValueExpr::NonCallable(lowered),
                AbstractValue::Value(Value::Prim(evaluated)),
            ) => expected == lowered && expected == evaluated,
            (
                Leaf::Function(expected),
                ValueExpr::Alloc(Kind::Function, lowered),
                AbstractValue::Closure(evaluated, _),
            ) => expected == lowered && expected == evaluated,
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
