//! Named projections of source-derived callable body facts.

use super::local_operations::LocalBody;
use super::value_model::{CallSite, ValueExpr};
use swc_common::Spanned;
use swc_ecma_ast::Expr;

#[derive(Debug, Clone, Default)]
pub(super) struct CallableBodyFacts {
    value_flow: ValueFlowProjection,
    direct_return_candidate: Option<CallSite>,
    /// Present only when ordered lowering covers every live source operation.
    /// Consumers may impose stricter admission, such as the application's
    /// keyed-collection restriction, without narrowing the value-flow facts.
    ordered_plan: Option<LocalBody>,
}

#[derive(Debug, Clone, Default)]
pub(super) struct ValueFlowProjection {
    /// Broad, intentionally lossy returned-value facts, retained independently
    /// of whether the ordered plan can represent the complete body.
    returns: Vec<ValueExpr>,
    direct_allocation_return: Option<CoveredAllocationReturn>,
}

/// Constructed only when both exact return-site coverage and value-expression
/// syntax coverage hold. It does not certify ordered effects or application
/// execution.
#[derive(Debug, Clone)]
struct CoveredAllocationReturn(ValueExpr);

impl ValueFlowProjection {
    pub(super) fn returns(&self) -> &[ValueExpr] {
        &self.returns
    }

    pub(super) fn direct_allocation_return(&self) -> Option<&ValueExpr> {
        self.direct_allocation_return
            .as_ref()
            .map(|returned| &returned.0)
    }
}

impl CallableBodyFacts {
    pub(super) fn value_flow(&self) -> &ValueFlowProjection {
        &self.value_flow
    }

    pub(super) fn ordered_plan(&self) -> Option<&LocalBody> {
        self.ordered_plan.as_ref()
    }

    pub(super) fn set_ordered_plan(&mut self, plan: Option<LocalBody>) {
        self.ordered_plan = plan;
    }

    pub(super) fn take_ordered_plan(&mut self) -> Option<LocalBody> {
        self.ordered_plan.take()
    }

    /// The collector admits only a whole-body direct-return shape here. That
    /// source-shape fact gates the narrow value projection, independently of
    /// ordered-plan lowering.
    pub(super) fn set_direct_return_candidate(&mut self, candidate: Option<CallSite>) {
        self.direct_return_candidate = candidate;
    }

    pub(super) fn record_return(&mut self, expression: &Expr) -> ValueExpr {
        let (value, covered) = ValueExpr::from_expr_with_coverage(expression);
        self.value_flow.returns.push(value.clone());
        let exact_direct_return = self
            .direct_return_candidate
            .take()
            .is_some_and(|candidate| candidate == CallSite::from(expression.span()));
        if exact_direct_return && covered {
            self.value_flow.direct_allocation_return = Some(CoveredAllocationReturn(value.clone()));
        }
        value
    }

    pub(super) fn record_undefined_return(&mut self) {
        self.value_flow.returns.push(ValueExpr::Undefined);
    }
}
