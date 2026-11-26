use super::*;

#[macro_export]
macro_rules! impl_lazy_callable_visits {
    () => {
        fn visit_function(&mut self, node: &Function) {
            $crate::lazy_boundary::lazy_visit_function(self, node);
        }
        fn visit_arrow_expr(&mut self, node: &ArrowExpr) {
            $crate::lazy_boundary::lazy_visit_arrow_expr(self, node);
        }
        fn visit_method_prop(&mut self, node: &MethodProp) {
            $crate::lazy_boundary::lazy_visit_method_prop(self, node);
        }
        fn visit_getter_prop(&mut self, node: &GetterProp) {
            $crate::lazy_boundary::lazy_visit_getter_prop(self, node);
        }
        fn visit_setter_prop(&mut self, node: &SetterProp) {
            $crate::lazy_boundary::lazy_visit_setter_prop(self, node);
        }
    };
}

#[macro_export]
macro_rules! impl_lazy_class_visits {
    () => {
        fn visit_class(&mut self, node: &Class) {
            $crate::lazy_boundary::lazy_visit_class(self, node);
        }
        fn visit_class_member(&mut self, member: &ClassMember) {
            $crate::lazy_boundary::lazy_visit_class_member(self, member);
        }
    };
}

#[macro_export]
macro_rules! impl_lazy_optional_path_visits {
    () => {
        fn visit_if_stmt(&mut self, node: &IfStmt) {
            $crate::lazy_boundary::lazy_visit_if_stmt(self, node);
        }
        fn visit_cond_expr(&mut self, node: &CondExpr) {
            $crate::lazy_boundary::lazy_visit_cond_expr(self, node);
        }
        fn visit_bin_expr(&mut self, node: &BinExpr) {
            $crate::lazy_boundary::lazy_visit_bin_expr(self, node);
        }
        fn visit_opt_chain_expr(&mut self, node: &OptChainExpr) {
            $crate::lazy_boundary::lazy_visit_may_skip(self, node);
        }
        fn visit_while_stmt(&mut self, node: &WhileStmt) {
            $crate::lazy_boundary::lazy_visit_may_skip(self, node);
        }
        fn visit_do_while_stmt(&mut self, node: &DoWhileStmt) {
            $crate::lazy_boundary::lazy_visit_may_skip(self, node);
        }
        fn visit_for_stmt(&mut self, node: &ForStmt) {
            $crate::lazy_boundary::lazy_visit_may_skip(self, node);
        }
        fn visit_try_stmt(&mut self, node: &TryStmt) {
            $crate::lazy_boundary::lazy_visit_may_skip(self, node);
        }
        fn visit_switch_stmt(&mut self, node: &SwitchStmt) {
            $crate::lazy_boundary::lazy_visit_may_skip(self, node);
        }
    };
}

/// Depth zero is eager. A synchronous call also runs the immediate function
/// body (depth one) up to its first unavoidable suspension. Nested bodies
/// and code after that suspension still contribute to the general lazy sets.
#[derive(Debug, Clone, Copy, Default)]
pub struct LazyPosition {
    pub depth: u32,
    pub past_await: bool,
    pub may_skip_await_depth: u32,
}

impl LazyPosition {
    pub fn can_run_at_init(self) -> bool {
        self.depth == 0 || (self.depth == 1 && !self.past_await)
    }
}

pub trait LazyBoundary: Visit {
    fn position(&mut self) -> &mut LazyPosition;

    fn descend_lazy<F: FnOnce(&mut Self)>(&mut self, f: F) {
        let outer = *self.position();
        *self.position() = LazyPosition {
            depth: outer.depth + 1,
            ..Default::default()
        };
        f(self);
        *self.position() = outer;
    }
}

/// An `await` reached only through an optional path does not prove that
/// subsequent code runs after suspension. Preserve the pre-branch state while
/// still classifying reads inside the branch after its own await as deferred.
pub fn lazy_visit_if_stmt<V: LazyBoundary>(v: &mut V, node: &IfStmt) {
    node.test.visit_with(v);
    let before_branch = v.position().past_await;
    node.cons.visit_with(v);
    v.position().past_await = before_branch;
    if let Some(alt) = &node.alt {
        alt.visit_with(v);
        v.position().past_await = before_branch;
    }
}

pub fn lazy_visit_cond_expr<V: LazyBoundary>(v: &mut V, node: &CondExpr) {
    node.test.visit_with(v);
    let before_branch = v.position().past_await;
    node.cons.visit_with(v);
    v.position().past_await = before_branch;
    node.alt.visit_with(v);
    v.position().past_await = before_branch;
}

pub fn lazy_visit_bin_expr<V: LazyBoundary>(v: &mut V, node: &BinExpr) {
    node.left.visit_with(v);
    let before_right = v.position().past_await;
    node.right.visit_with(v);
    if node.op.may_short_circuit() {
        v.position().past_await = before_right;
    }
}

pub fn lazy_visit_may_skip<V: LazyBoundary, N: VisitWith<V>>(v: &mut V, node: &N) {
    let before = v.position().past_await;
    v.position().may_skip_await_depth += 1;
    node.visit_children_with(v);
    v.position().may_skip_await_depth -= 1;
    v.position().past_await = before;
}

pub fn lazy_visit_function<V: LazyBoundary>(v: &mut V, node: &Function) {
    v.descend_lazy(|s| node.visit_children_with(s));
}

pub fn lazy_visit_arrow_expr<V: LazyBoundary>(v: &mut V, node: &ArrowExpr) {
    v.descend_lazy(|s| node.visit_children_with(s));
}

pub fn lazy_visit_method_prop<V: LazyBoundary>(v: &mut V, node: &MethodProp) {
    node.key.visit_with(v);
    // `node.function.visit_with` dispatches to `visit_function`, which
    // already calls `descend_lazy` via `lazy_visit_function`. No outer
    // descend here — the method body must land at lazy_depth 1, the
    // same as a bare `function f() { ... }`.
    node.function.visit_with(v);
}

pub fn lazy_visit_getter_prop<V: LazyBoundary>(v: &mut V, node: &GetterProp) {
    node.key.visit_with(v);
    v.descend_lazy(|s| {
        if let Some(body) = &node.function.body {
            body.visit_with(s);
        }
    });
}

pub fn lazy_visit_setter_prop<V: LazyBoundary>(v: &mut V, node: &SetterProp) {
    node.key.visit_with(v);
    for param in &node.function.params {
        param.pat.visit_with(v);
    }
    v.descend_lazy(|s| {
        if let Some(body) = &node.function.body {
            body.visit_with(s);
        }
    });
}

pub fn lazy_visit_class<V: LazyBoundary>(v: &mut V, node: &Class) {
    visit_class_decl(v, node, |v, m| v.visit_class_member(m));
}

pub fn lazy_visit_class_member<V: LazyBoundary>(v: &mut V, member: &ClassMember) {
    visit_eager_member_parts(v, member);
    match member {
        // `method.function.visit_with` dispatches to `visit_function`,
        // which already calls `descend_lazy` via `lazy_visit_function`.
        // No outer descend here — a method body must land at
        // lazy_depth 1, the same as a bare `function f() { ... }`,
        // so rebinds in the immediate body show up in
        // `rebinds.first_order_lazy` and the dependency graph emits the
        // constraining `LazyRebind` edge that catches cross-module
        // writes to imported bindings (ESM rejects at runtime).
        ClassMember::Method(method) => {
            method.function.visit_with(v);
        }
        ClassMember::PrivateMethod(method) => {
            method.function.visit_with(v);
        }
        ClassMember::Constructor(ctor) => {
            v.descend_lazy(|s| ctor.visit_children_with(s));
        }
        ClassMember::ClassProp(prop) if !prop.is_static => {
            if let Some(value) = &prop.value {
                v.descend_lazy(|s| value.visit_with(s));
            }
        }
        ClassMember::PrivateProp(prop) if !prop.is_static => {
            if let Some(value) = &prop.value {
                v.descend_lazy(|s| value.visit_with(s));
            }
        }
        ClassMember::AutoAccessor(accessor) if !accessor.is_static => {
            if let Some(value) = &accessor.value {
                v.descend_lazy(|s| value.visit_with(s));
            }
        }
        _ => {}
    }
}

fn visit_computed_prop_name<V: Visit>(v: &mut V, name: &PropName) {
    if let PropName::Computed(computed) = name {
        computed.expr.visit_with(v);
    }
}

fn visit_class_decl<V: Visit>(
    v: &mut V,
    node: &Class,
    mut visit_member: impl FnMut(&mut V, &ClassMember),
) {
    for decorator in &node.decorators {
        decorator.visit_with(v);
    }
    if let Some(super_class) = &node.super_class {
        super_class.visit_with(v);
    }
    for member in &node.body {
        visit_member(v, member);
    }
}

pub fn visit_eager_member_parts<V: Visit>(v: &mut V, member: &ClassMember) {
    match member {
        ClassMember::Method(method) => {
            visit_computed_prop_name(v, &method.key);
        }
        ClassMember::PrivateMethod(_) | ClassMember::Constructor(_) => {}
        ClassMember::ClassProp(prop) => {
            visit_computed_prop_name(v, &prop.key);
            if prop.is_static
                && let Some(value) = &prop.value
            {
                value.visit_with(v);
            }
        }
        ClassMember::PrivateProp(prop) => {
            if prop.is_static
                && let Some(value) = &prop.value
            {
                value.visit_with(v);
            }
        }
        ClassMember::StaticBlock(block) => {
            block.visit_with(v);
        }
        ClassMember::TsIndexSignature(_) | ClassMember::Empty(_) => {}
        ClassMember::AutoAccessor(accessor) => {
            if let Key::Public(name) = &accessor.key {
                visit_computed_prop_name(v, name);
            }
            if accessor.is_static
                && let Some(value) = &accessor.value
            {
                value.visit_with(v);
            }
        }
    }
}

/// One value per syntactic position bucket. Collapses the repeated
/// eager / lazy / first-order-lazy triples (reads, rebinds, calls)
/// into a single shape so the "first-order ⊆ lazy" subset invariant
/// lives in one place ([`Self::record`]) instead of at every
/// construction site.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PositionBucketed<T> {
    /// At-init position: outside any function/arrow/method/getter/
    /// setter body, constructor body, or instance class-field
    /// initializer.
    pub eager: T,
    /// Inside a lazy syntactic position, at any nesting depth. May
    /// overlap with `eager` if the same name appears in both eager
    /// and lazy positions of the statement.
    pub lazy: T,
    /// Subset of `lazy` whose sites sit in a function's
    /// **first-order, pre-await** body (depth 1 from this statement).
    /// Used by at-init call promotion: a synchronous call to the
    /// function only runs its immediate pre-await body, so sites
    /// inside nested function/arrow definitions or past an `await`
    /// don't promote to the caller.
    pub first_order_lazy: T,
}

impl PositionBucketed<BTreeSet<Id>> {
    /// Record `id` in the bucket the cursor position selects: `eager`
    /// at depth 0; `lazy` at depth ≥ 1, additionally `first_order_lazy`
    /// at depth 1 before the body's first `await`. Maintains the
    /// `first_order_lazy ⊆ lazy` invariant structurally.
    pub fn record(&mut self, id: &Id, position: LazyPosition) {
        if position.depth == 0 {
            self.eager.insert(id.clone());
            return;
        }
        self.lazy.insert(id.clone());
        if position.can_run_at_init() {
            self.first_order_lazy.insert(id.clone());
        }
    }

    pub fn extend(&mut self, other: Self) {
        self.eager.extend(other.eager);
        self.lazy.extend(other.lazy);
        self.first_order_lazy.extend(other.first_order_lazy);
    }
}
