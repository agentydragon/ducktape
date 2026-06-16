use super::*;

/// Shared trait for visitors that track lazy nesting depth and the
/// per-body "past first await" boundary.
///
/// Depth semantics:
/// - `0` — eager (outside any function body).
/// - `1` — first-order lazy (inside the immediate body of a function).
/// - `≥2` — nested lazy (inside a function nested in another function body).
///
/// `past_await` is a per-body flag: while visiting an async function /
/// arrow / method body, it flips `true` once an `AwaitExpr` has been
/// seen, and resets back to `false` when control exits that body.
/// Code past the first await runs in a microtask after the at-init
/// caller has finished, so it doesn't behave as "synchronously fires
/// when the function is invoked" — at-init call promotion treats it
/// like a nested closure.
///
/// At-init call promotion only inherits reads/rebinds/calls from the
/// **first-order, pre-await** part of a callee's body
/// (`lazy_depth == 1 && !past_await`), because a synchronous invocation
/// of the callee runs only that portion of the body. Statements
/// lexically inside nested function/arrow definitions or past an
/// `await` in an async body are not executed until something else
/// (the nested closure being called, or the microtask scheduler)
/// fires them. The general `lazy_*` sets stay coarse (any depth ≥1,
/// regardless of `past_await`) because, from the chunk's top-level
/// POV, any rebind inside any function body remains "lazy".
pub(crate) trait LazyBoundary: Visit {
    fn lazy_depth_mut(&mut self) -> &mut u32;
    fn past_await_mut(&mut self) -> &mut bool;

    fn descend_lazy<F: FnOnce(&mut Self)>(&mut self, f: F) {
        // Each body has its own `past_await` scope: a nested function
        // starts pre-await regardless of the enclosing body's state.
        let saved_past_await = std::mem::replace(self.past_await_mut(), false);
        *self.lazy_depth_mut() += 1;
        f(self);
        *self.lazy_depth_mut() -= 1;
        *self.past_await_mut() = saved_past_await;
    }
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
        if let Some(body) = &node.body {
            body.visit_with(s);
        }
    });
}

pub(crate) fn lazy_visit_setter_prop<V: LazyBoundary>(v: &mut V, node: &SetterProp) {
    node.key.visit_with(v);
    node.param.visit_with(v);
    v.descend_lazy(|s| {
        if let Some(body) = &node.body {
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
        // `rebinds.first_order_lazy` and the owner-graph emits the
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

pub(crate) fn visit_eager_member_parts<V: Visit>(v: &mut V, member: &ClassMember) {
    match member {
        ClassMember::Method(method) => {
            visit_computed_prop_name(v, &method.key);
        }
        ClassMember::PrivateMethod(_) | ClassMember::Constructor(_) => {}
        ClassMember::ClassProp(prop) => {
            visit_computed_prop_name(v, &prop.key);
            if prop.is_static {
                if let Some(value) = &prop.value {
                    value.visit_with(v);
                }
            }
        }
        ClassMember::PrivateProp(prop) => {
            if prop.is_static {
                if let Some(value) = &prop.value {
                    value.visit_with(v);
                }
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
            if accessor.is_static {
                if let Some(value) = &accessor.value {
                    value.visit_with(v);
                }
            }
        }
    }
}
