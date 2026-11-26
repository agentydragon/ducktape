use super::*;

/// Depth zero is eager. A synchronous call also runs the immediate function
/// body (depth one) up to its first unavoidable suspension. Nested bodies
/// and code after that suspension still contribute to the general lazy sets.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct LazyPosition {
    pub(super) depth: u32,
    pub(super) past_await: bool,
    pub(super) may_skip_await_depth: u32,
}

impl LazyPosition {
    pub(super) fn can_run_at_init(self) -> bool {
        self.depth == 0 || (self.depth == 1 && !self.past_await)
    }
}

pub(crate) trait LazyBoundary: Visit {
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
pub(crate) fn lazy_visit_if_stmt<V: LazyBoundary>(v: &mut V, node: &IfStmt) {
    node.test.visit_with(v);
    let before_branch = v.position().past_await;
    node.cons.visit_with(v);
    v.position().past_await = before_branch;
    if let Some(alt) = &node.alt {
        alt.visit_with(v);
        v.position().past_await = before_branch;
    }
}

pub(crate) fn lazy_visit_cond_expr<V: LazyBoundary>(v: &mut V, node: &CondExpr) {
    node.test.visit_with(v);
    let before_branch = v.position().past_await;
    node.cons.visit_with(v);
    v.position().past_await = before_branch;
    node.alt.visit_with(v);
    v.position().past_await = before_branch;
}

pub(crate) fn lazy_visit_bin_expr<V: LazyBoundary>(v: &mut V, node: &BinExpr) {
    node.left.visit_with(v);
    let before_right = v.position().past_await;
    node.right.visit_with(v);
    if node.op.may_short_circuit() {
        v.position().past_await = before_right;
    }
}

pub(crate) fn lazy_visit_may_skip<V: LazyBoundary, N: VisitWith<V>>(v: &mut V, node: &N) {
    let before = v.position().past_await;
    v.position().may_skip_await_depth += 1;
    node.visit_children_with(v);
    v.position().may_skip_await_depth -= 1;
    v.position().past_await = before;
}

pub(crate) fn lazy_visit_function<V: LazyBoundary>(v: &mut V, node: &Function) {
    v.descend_lazy(|s| node.visit_children_with(s));
}

pub(crate) fn lazy_visit_arrow_expr<V: LazyBoundary>(v: &mut V, node: &ArrowExpr) {
    v.descend_lazy(|s| node.visit_children_with(s));
}

pub(crate) fn lazy_visit_method_prop<V: LazyBoundary>(v: &mut V, node: &MethodProp) {
    node.key.visit_with(v);
    // `node.function.visit_with` dispatches to `visit_function`, which
    // already calls `descend_lazy` via `lazy_visit_function`. No outer
    // descend here — the method body must land at lazy_depth 1, the
    // same as a bare `function f() { ... }`.
    node.function.visit_with(v);
}

pub(crate) fn lazy_visit_getter_prop<V: LazyBoundary>(v: &mut V, node: &GetterProp) {
    node.key.visit_with(v);
    v.descend_lazy(|s| {
        if let Some(body) = &node.function.body {
            body.visit_with(s);
        }
    });
}

pub(crate) fn lazy_visit_setter_prop<V: LazyBoundary>(v: &mut V, node: &SetterProp) {
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

pub(crate) fn lazy_visit_class<V: LazyBoundary>(v: &mut V, node: &Class) {
    visit_class_decl(v, node, |v, m| v.visit_class_member(m));
}

pub(crate) fn lazy_visit_class_member<V: LazyBoundary>(v: &mut V, member: &ClassMember) {
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
