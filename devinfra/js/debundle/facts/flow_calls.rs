//! Flow-derived call targets folded into the syntactic statement facts.

use super::*;

/// Add call targets discovered through parameters, aliases, returns and
/// static object properties to the ordinary syntactic call buckets.
pub fn augment_calls(
    flow: &ChunkValueFlow,
    body: &[TopLevelItemView<'_>],
    facts: &mut [StatementFacts],
) {
    for (item, fact) in body.iter().zip(facts) {
        let mut collector = FlowCallCollector {
            flow,
            calls: PositionBucketed::default(),
            complete_ident_calls: PositionBucketed::default(),
            incomplete_ident_calls: PositionBucketed::default(),
            position: LazyPosition::default(),
        };
        item.as_module_item().visit_with(&mut collector);
        for id in collector
            .complete_ident_calls
            .eager
            .difference(&collector.incomplete_ident_calls.eager)
        {
            fact.calls.eager.remove(id);
        }
        for id in collector
            .complete_ident_calls
            .lazy
            .difference(&collector.incomplete_ident_calls.lazy)
        {
            fact.calls.lazy.remove(id);
        }
        for id in collector
            .complete_ident_calls
            .first_order_lazy
            .difference(&collector.incomplete_ident_calls.first_order_lazy)
        {
            fact.calls.first_order_lazy.remove(id);
        }
        fact.calls.extend(collector.calls);
        if let (Some(class), Some(constructor)) = (
            analysis_domain::item_views::class_of_item(item.as_module_item()),
            fact.constructor.as_mut(),
        ) {
            let mut ctor_calls = FlowCallCollector {
                flow,
                calls: PositionBucketed::default(),
                complete_ident_calls: PositionBucketed::default(),
                incomplete_ident_calls: PositionBucketed::default(),
                position: LazyPosition::default(),
            };
            for member in &class.body {
                if matches!(member, ClassMember::Constructor(_))
                    || matches!(member, ClassMember::ClassProp(prop) if !prop.is_static)
                    || matches!(member, ClassMember::PrivateProp(prop) if !prop.is_static)
                    || matches!(member, ClassMember::AutoAccessor(accessor) if !accessor.is_static)
                {
                    member.visit_with(&mut ctor_calls);
                }
            }
            for id in ctor_calls
                .complete_ident_calls
                .first_order_lazy
                .difference(&ctor_calls.incomplete_ident_calls.first_order_lazy)
            {
                constructor.calls.remove(id);
            }
            constructor.calls.extend(ctor_calls.calls.first_order_lazy);
        }
    }
}

struct FlowCallCollector<'a> {
    flow: &'a ChunkValueFlow,
    calls: PositionBucketed<BTreeSet<Id>>,
    complete_ident_calls: PositionBucketed<BTreeSet<Id>>,
    incomplete_ident_calls: PositionBucketed<BTreeSet<Id>>,
    position: LazyPosition,
}

impl LazyBoundary for FlowCallCollector<'_> {
    fn position(&mut self) -> &mut LazyPosition {
        &mut self.position
    }
}

impl FlowCallCollector<'_> {
    fn record(&mut self, callee: &Expr) {
        let targets = self.flow.call_targets(callee);
        if let Expr::Ident(ident) = callee.unwrap_parens() {
            let id = ident.to_id();
            let bucket = if targets.complete {
                &mut self.complete_ident_calls
            } else {
                &mut self.incomplete_ident_calls
            };
            bucket.record(&id, self.position);
        }
        for binding in targets.bindings {
            self.calls.record(&binding, self.position);
        }
    }
}

impl Visit for FlowCallCollector<'_> {
    impl_lazy_optional_path_visits!();

    fn visit_assign_expr(&mut self, node: &AssignExpr) {
        if node.op.may_short_circuit() {
            lazy_visit_may_skip(self, node);
        } else {
            node.visit_children_with(self);
        }
    }
    fn visit_for_in_stmt(&mut self, node: &ForInStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_for_of_stmt(&mut self, node: &ForOfStmt) {
        lazy_visit_may_skip(self, node);
    }
    fn visit_call_expr(&mut self, node: &CallExpr) {
        if let Callee::Expr(callee) = &node.callee {
            self.record(callee);
        }
        node.visit_children_with(self);
    }

    fn visit_new_expr(&mut self, node: &NewExpr) {
        self.record(&node.callee);
        node.visit_children_with(self);
    }

    impl_lazy_callable_visits!();
    impl_lazy_class_visits!();

    fn visit_await_expr(&mut self, node: &AwaitExpr) {
        node.arg.visit_with(self);
        if self.position.depth > 0 && self.position.may_skip_await_depth == 0 {
            self.position.past_await = true;
        }
    }
}
