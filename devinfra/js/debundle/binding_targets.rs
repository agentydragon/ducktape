use swc_atoms::Atom;
use swc_ecma_ast::*;

pub trait TargetAccessRecorder {
    /// Called for update expressions like `a++`, where the target binding is
    /// both read and written. Validators that only care about writes can leave
    /// the default no-op in place.
    fn record_binding_read(&mut self, _id: &Id) {}
    fn record_binding_write(&mut self, id: &Id);
    /// Called for mutations through a binding, e.g. `obj.x = 1`. The `id` is
    /// the leftmost identifier of the member access (`obj` here). This is not
    /// a rebinding write to `obj`; validator collectors can ignore it when
    /// they only need to reject assignments to imported binding cells.
    fn record_member_write(&mut self, _id: &Id) {}
}

/// Yield the hygiene-preserving `Id` of every binding the pattern
/// declares. `swc_ecma_utils::find_pat_ids::<Pat, Id>` does the
/// same; this handwritten walker is kept to avoid an extra crate
/// dep and to surface order deterministically.
pub fn binding_names(pattern: &Pat) -> impl Iterator<Item = Id> + '_ {
    enum Work<'a> {
        Pat(&'a Pat),
        BindIdent(&'a BindingIdent),
    }
    let mut stack = vec![Work::Pat(pattern)];
    std::iter::from_fn(move || {
        loop {
            match stack.pop()? {
                Work::BindIdent(id) => return Some(id.to_id()),
                Work::Pat(pat) => match pat {
                    Pat::Ident(id) => return Some(id.to_id()),
                    Pat::Array(arr) => {
                        for elem in arr.elems.iter().flatten().rev() {
                            stack.push(Work::Pat(elem));
                        }
                    }
                    Pat::Object(obj) => {
                        for prop in obj.props.iter().rev() {
                            match prop {
                                ObjectPatProp::KeyValue(kv) => stack.push(Work::Pat(&kv.value)),
                                ObjectPatProp::Assign(a) => stack.push(Work::BindIdent(&a.key)),
                                ObjectPatProp::Rest(rest) => stack.push(Work::Pat(&rest.arg)),
                            }
                        }
                    }
                    Pat::Rest(rest) => stack.push(Work::Pat(&rest.arg)),
                    Pat::Assign(assign) => stack.push(Work::Pat(&assign.left)),
                    _ => {}
                },
            }
        }
    })
}

pub fn record_assign_target(target: &AssignTarget, recorder: &mut impl TargetAccessRecorder) {
    match target {
        AssignTarget::Simple(simple) => record_simple_assign_target(simple, recorder),
        AssignTarget::Pat(pattern) => record_assign_target_pat(pattern, recorder),
    }
}

fn record_simple_assign_target(
    target: &SimpleAssignTarget,
    recorder: &mut impl TargetAccessRecorder,
) {
    match target {
        SimpleAssignTarget::Ident(ident) => {
            recorder.record_binding_write(&ident.to_id());
        }
        SimpleAssignTarget::Member(member) => {
            record_member_target(member, recorder);
        }
        SimpleAssignTarget::Paren(paren) => {
            record_assign_expr_target(&paren.expr, recorder);
        }
        SimpleAssignTarget::OptChain(opt_chain) => {
            if let Some(id) = opt_chain_base_id(opt_chain) {
                recorder.record_member_write(&id);
            }
        }
        _ => {}
    }
}

fn record_assign_expr_target(target: &Expr, recorder: &mut impl TargetAccessRecorder) {
    match target {
        Expr::Ident(ident) => recorder.record_binding_write(&ident.to_id()),
        Expr::Member(member) => record_member_target(member, recorder),
        Expr::Paren(paren) => record_assign_expr_target(&paren.expr, recorder),
        Expr::OptChain(opt_chain) => {
            if let Some(id) = opt_chain_base_id(opt_chain) {
                recorder.record_member_write(&id);
            }
        }
        _ => {}
    }
}

fn record_assign_target_pat(target: &AssignTargetPat, recorder: &mut impl TargetAccessRecorder) {
    match target {
        AssignTargetPat::Array(array) => {
            for element in array.elems.iter().flatten() {
                record_pat_write(element, recorder);
            }
        }
        AssignTargetPat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => {
                        record_pat_write(&key_value.value, recorder);
                    }
                    ObjectPatProp::Assign(assign) => {
                        recorder.record_binding_write(&assign.key.to_id());
                    }
                    ObjectPatProp::Rest(rest) => record_pat_write(&rest.arg, recorder),
                }
            }
        }
        AssignTargetPat::Invalid(_) => {}
    }
}

pub fn record_pat_write(pattern: &Pat, recorder: &mut impl TargetAccessRecorder) {
    for id in binding_names(pattern) {
        recorder.record_binding_write(&id);
    }
}

pub fn record_member_target(member: &MemberExpr, recorder: &mut impl TargetAccessRecorder) {
    if let Some(id) = member_root_id(&member.obj) {
        recorder.record_member_write(&id);
    }
}

pub fn record_update_target(target: &Expr, recorder: &mut impl TargetAccessRecorder) {
    match target {
        Expr::Ident(ident) => {
            let id = ident.to_id();
            recorder.record_binding_read(&id);
            recorder.record_binding_write(&id);
        }
        Expr::Member(member) => {
            if let Some(id) = member_root_id(&member.obj) {
                recorder.record_binding_read(&id);
                recorder.record_member_write(&id);
            }
        }
        Expr::Paren(paren) => record_update_target(&paren.expr, recorder),
        Expr::OptChain(opt_chain) => {
            if let Some(id) = opt_chain_base_id(opt_chain) {
                recorder.record_binding_read(&id);
                recorder.record_member_write(&id);
            }
        }
        _ => {}
    }
}

/// Hygiene-preserving counterpart of [`member_root_sym`]. Returns the
/// leftmost identifier of `expr`'s member-access chain as an `Id`
/// (atom + syntax context), or `None` if the head isn't a plain
/// identifier (numeric literal, call expression, etc.).
pub fn member_root_id(expr: &Expr) -> Option<Id> {
    match expr {
        Expr::Ident(ident) => Some(ident.to_id()),
        Expr::Member(member) => member_root_id(&member.obj),
        Expr::OptChain(opt_chain) => match &*opt_chain.base {
            OptChainBase::Member(member) => member_root_id(&member.obj),
            OptChainBase::Call(call) => member_root_id(&call.callee),
        },
        Expr::Paren(paren) => member_root_id(&paren.expr),
        _ => None,
    }
}

/// Returns the leftmost identifier's atom (i.e. just the source-level
/// textual name, without `SyntaxContext`). Use when the caller only
/// compares against fixed literal names — e.g. detecting
/// `window.foo` / `document.foo`. For binding-cell identity (graph
/// edges, validator membership checks), use [`member_root_id`].
pub fn member_root_sym(expr: &Expr) -> Option<&Atom> {
    match expr {
        Expr::Ident(ident) => Some(&ident.sym),
        Expr::Member(member) => member_root_sym(&member.obj),
        Expr::OptChain(opt_chain) => match &*opt_chain.base {
            OptChainBase::Member(member) => member_root_sym(&member.obj),
            OptChainBase::Call(call) => member_root_sym(&call.callee),
        },
        Expr::Paren(paren) => member_root_sym(&paren.expr),
        _ => None,
    }
}

fn opt_chain_base_id(opt_chain: &OptChainExpr) -> Option<Id> {
    match &*opt_chain.base {
        OptChainBase::Member(member) => member_root_id(&member.obj),
        OptChainBase::Call(call) => member_root_id(&call.callee),
    }
}
