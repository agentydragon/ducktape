use std::collections::BTreeSet;

use binding_targets::{
    TargetAccessRecorder, binding_names, record_assign_target, record_update_target, strip_parens,
};
use swc_ecma_ast::*;

use super::{TopLevelItemView, collect_declared_names, var_decl_of_item};
use crate::analysis_hints::LocalEffectPolicy;
use crate::purity::{ChunkCodeGraph, classify_expr_purity};

#[derive(Debug, Default)]
pub(crate) struct LocalEffectContext {
    /// Chunk-top declared binding names. Populated under both
    /// non-default policies: `VendorPrune` uses it to scope its
    /// recognizers' write targets, `LocalPropertyWrites` to require
    /// the written-through root to be chunk-declared (a property
    /// write through an *import* mutates another module's value and
    /// stays a globally-ordered effect).
    declared_bindings: BTreeSet<Id>,
    vendor_prune_commonjs_module_bindings: BTreeSet<Id>,
    vendor_prune_intrinsic_local_effect_callees: BTreeSet<Id>,
}

impl LocalEffectContext {
    pub(crate) fn for_body(
        body: &[TopLevelItemView<'_>],
        local_effect_policy: LocalEffectPolicy,
    ) -> Self {
        let mut context = Self::default();
        if local_effect_policy == LocalEffectPolicy::KnownEffectsOnly {
            return context;
        }
        for item in body {
            context
                .declared_bindings
                .extend(collect_declared_names(item.as_module_item()));
        }
        if local_effect_policy != LocalEffectPolicy::VendorPrune {
            return context;
        }
        for item in body {
            context
                .vendor_prune_commonjs_module_bindings
                .extend(vendor_prune_commonjs_module_bindings(item.as_module_item()));
            context.vendor_prune_intrinsic_local_effect_callees.extend(
                vendor_prune_intrinsic_local_effect_aliases(item.as_module_item()),
            );
        }
        for item in body {
            context.vendor_prune_intrinsic_local_effect_callees.extend(
                vendor_prune_target_first_local_effect_wrappers(item.as_module_item(), &context),
            );
        }
        context
    }

    pub(crate) fn local_effect_targets(&self, item: &ModuleItem) -> BTreeSet<Id> {
        vendor_prune_local_effect_targets(item, self)
    }

    /// Recognize a whole-statement local property write under
    /// `LocalEffectPolicy::LocalPropertyWrites`: an expression
    /// statement that is one `X.prop = <pure-rhs>` assignment (or a
    /// comma-sequence of such), every member-path segment a static
    /// non-`__proto__` name, every root `X` a chunk-top declared
    /// binding, every RHS classifier-Pure. Returns the set of written
    /// roots (the statement's local-effect targets), or empty when any
    /// part of the statement falls outside that shape — partial
    /// recognition would under-account the unrecognized remainder's
    /// effects, so it's all or nothing (same contract as the
    /// VendorPrune recognizers).
    ///
    /// The RHS check uses the strict expression classifier rather than
    /// the vendor recognizers' structural `namespace_iife_local_init_is_pure`
    /// — a getter-bearing RHS read (`X.a = Y.b`) must disqualify, and
    /// the classifier is the component that proves that.
    pub(crate) fn local_property_write_targets(
        &self,
        item: &ModuleItem,
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        graph: &ChunkCodeGraph,
    ) -> BTreeSet<Id> {
        let ModuleItem::Stmt(Stmt::Expr(expr_stmt)) = item else {
            return BTreeSet::new();
        };
        let mut targets = BTreeSet::new();
        if self.collect_local_property_writes(
            &expr_stmt.expr,
            shadowed,
            declared_pure,
            graph,
            &mut targets,
        ) && !targets.is_empty()
        {
            targets
        } else {
            BTreeSet::new()
        }
    }

    fn collect_local_property_writes(
        &self,
        expr: &Expr,
        shadowed: &BTreeSet<&'static str>,
        declared_pure: &BTreeSet<String>,
        graph: &ChunkCodeGraph,
        targets: &mut BTreeSet<Id>,
    ) -> bool {
        match strip_parens(expr) {
            Expr::Seq(seq) => seq.exprs.iter().all(|expr| {
                self.collect_local_property_writes(expr, shadowed, declared_pure, graph, targets)
            }),
            // Plain `=` only: a compound assignment (`+=` etc.) also
            // READS the property, and a read on a written-through (so
            // non-PlainData) object may fire a getter.
            Expr::Assign(assign) if assign.op == AssignOp::Assign => {
                let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left else {
                    return false;
                };
                let Some(root) = static_member_write_root(member) else {
                    return false;
                };
                if !self.declared_bindings.contains(&root) {
                    return false;
                }
                if !classify_expr_purity(
                    &assign.right,
                    shadowed,
                    &BTreeSet::new(),
                    declared_pure,
                    graph,
                )
                .is_pure()
                {
                    return false;
                }
                targets.insert(root);
                true
            }
            _ => false,
        }
    }
}

/// The root identifier of a member-write target whose every path
/// segment (including the final written property) is a static name
/// and none is `__proto__` (a `__proto__` write rewires the prototype
/// chain — not a plain data-property effect on the root). `None` for
/// computed non-literal segments or a non-`Ident` root.
fn static_member_write_root(member: &MemberExpr) -> Option<Id> {
    if static_member_name(&member.prop)
        .as_deref()
        .is_none_or(|prop| prop == "__proto__")
    {
        return None;
    }
    match strip_parens(&member.obj) {
        Expr::Ident(root) => Some(root.to_id()),
        Expr::Member(inner) => static_member_write_root(inner),
        _ => None,
    }
}

fn vendor_prune_intrinsic_local_effect_aliases(item: &ModuleItem) -> BTreeSet<Id> {
    let Some(var) = var_decl_of_item(item) else {
        return BTreeSet::new();
    };
    var.decls
        .iter()
        .filter_map(|decl| {
            let Pat::Ident(local) = &decl.name else {
                return None;
            };
            let init = decl.init.as_deref()?;
            if vendor_prune_intrinsic_local_effect_member(init) {
                Some(local.to_id())
            } else {
                None
            }
        })
        .collect()
}

fn vendor_prune_intrinsic_local_effect_member(expr: &Expr) -> bool {
    let Expr::Member(member) = strip_parens(expr) else {
        return false;
    };
    let Expr::Ident(object) = strip_parens(&member.obj) else {
        return false;
    };
    object.sym.as_ref() == "Object"
        && static_member_name(&member.prop)
            .as_deref()
            .is_some_and(vendor_prune_object_local_effect_method)
}

fn vendor_prune_commonjs_module_bindings(item: &ModuleItem) -> BTreeSet<Id> {
    let Some(var) = var_decl_of_item(item) else {
        return BTreeSet::new();
    };
    var.decls
        .iter()
        .filter_map(|decl| {
            let Pat::Ident(local) = &decl.name else {
                return None;
            };
            let init = decl.init.as_deref()?;
            if vendor_prune_commonjs_module_init(init) {
                Some(local.to_id())
            } else {
                None
            }
        })
        .collect()
}

fn vendor_prune_commonjs_module_init(expr: &Expr) -> bool {
    let Expr::Object(obj) = strip_parens(expr) else {
        return false;
    };
    obj.props.iter().any(|prop| {
        let PropOrSpread::Prop(prop) = prop else {
            return false;
        };
        let Prop::KeyValue(kv) = &**prop else {
            return false;
        };
        static_prop_name(&kv.key).as_deref() == Some("exports")
            && matches!(
                strip_parens(&kv.value),
                Expr::Object(exports) if exports.props.is_empty()
            )
    })
}

fn vendor_prune_target_first_local_effect_wrappers(
    item: &ModuleItem,
    local_effect_context: &LocalEffectContext,
) -> BTreeSet<Id> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function)))
        | ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Fn(function),
            ..
        })) if vendor_prune_target_first_wrapper_function(
            &function.function,
            local_effect_context,
        ) =>
        {
            BTreeSet::from([function.ident.to_id()])
        }
        _ => {
            let Some(var) = var_decl_of_item(item) else {
                return BTreeSet::new();
            };
            var.decls
                .iter()
                .filter_map(|decl| {
                    let Pat::Ident(local) = &decl.name else {
                        return None;
                    };
                    let init = decl.init.as_deref()?;
                    if vendor_prune_target_first_wrapper_expr(init, local_effect_context) {
                        Some(local.to_id())
                    } else {
                        None
                    }
                })
                .collect()
        }
    }
}

fn vendor_prune_target_first_wrapper_expr(
    expr: &Expr,
    local_effect_context: &LocalEffectContext,
) -> bool {
    match strip_parens(expr) {
        Expr::Fn(function) => {
            vendor_prune_target_first_wrapper_function(&function.function, local_effect_context)
        }
        Expr::Arrow(arrow) => {
            let Some(param) = arrow.params.first() else {
                return false;
            };
            let Pat::Ident(param) = param else {
                return false;
            };
            match &*arrow.body {
                BlockStmtOrExpr::BlockStmt(block) => vendor_prune_target_first_wrapper_block(
                    block,
                    &param.to_id(),
                    local_effect_context,
                ),
                BlockStmtOrExpr::Expr(expr) => {
                    let mut saw_effect = false;
                    vendor_prune_target_first_wrapper_effect_expr(
                        expr,
                        &param.to_id(),
                        local_effect_context,
                        &mut saw_effect,
                    ) && saw_effect
                }
            }
        }
        _ => false,
    }
}

fn vendor_prune_target_first_wrapper_function(
    function: &Function,
    local_effect_context: &LocalEffectContext,
) -> bool {
    let Some(param) = function.params.first() else {
        return false;
    };
    let Pat::Ident(param) = &param.pat else {
        return false;
    };
    let Some(body) = function.body.as_ref() else {
        return false;
    };
    vendor_prune_target_first_wrapper_block(body, &param.to_id(), local_effect_context)
}

fn vendor_prune_target_first_wrapper_block(
    block: &BlockStmt,
    target_param: &Id,
    local_effect_context: &LocalEffectContext,
) -> bool {
    let mut saw_effect = false;
    for stmt in &block.stmts {
        let ok = match stmt {
            Stmt::Decl(Decl::Var(var)) => var.decls.iter().all(vendor_prune_callback_local_var),
            Stmt::Expr(expr) => vendor_prune_target_first_wrapper_effect_expr(
                &expr.expr,
                target_param,
                local_effect_context,
                &mut saw_effect,
            ),
            Stmt::Return(ret) => ret.arg.as_ref().is_none_or(|arg| {
                vendor_prune_target_first_wrapper_effect_expr(
                    arg,
                    target_param,
                    local_effect_context,
                    &mut saw_effect,
                )
            }),
            _ => false,
        };
        if !ok {
            return false;
        }
    }
    saw_effect
}

fn vendor_prune_target_first_wrapper_effect_expr(
    expr: &Expr,
    target_param: &Id,
    local_effect_context: &LocalEffectContext,
    saw_effect: &mut bool,
) -> bool {
    match strip_parens(expr) {
        Expr::Assign(_) | Expr::Update(_) => {
            let targets = vendor_prune_expr_local_effect_targets(expr, local_effect_context);
            if !targets.is_empty() && targets.iter().all(|target| target == target_param) {
                *saw_effect = true;
                true
            } else {
                false
            }
        }
        Expr::Call(call) => {
            if vendor_prune_direct_call_local_effect_target(call, local_effect_context).as_ref()
                == Some(target_param)
            {
                *saw_effect = true;
                true
            } else {
                false
            }
        }
        Expr::Seq(seq) => seq.exprs.iter().all(|expr| {
            vendor_prune_target_first_wrapper_effect_expr(
                expr,
                target_param,
                local_effect_context,
                saw_effect,
            )
        }),
        Expr::Bin(bin) if matches!(bin.op, BinaryOp::LogicalAnd | BinaryOp::LogicalOr) => {
            vendor_prune_target_first_wrapper_branch(
                &bin.left,
                target_param,
                local_effect_context,
                saw_effect,
            ) && vendor_prune_target_first_wrapper_branch(
                &bin.right,
                target_param,
                local_effect_context,
                saw_effect,
            )
        }
        Expr::Cond(cond) if namespace_iife_local_init_is_pure(&cond.test) => {
            vendor_prune_target_first_wrapper_branch(
                &cond.cons,
                target_param,
                local_effect_context,
                saw_effect,
            ) && vendor_prune_target_first_wrapper_branch(
                &cond.alt,
                target_param,
                local_effect_context,
                saw_effect,
            )
        }
        expr => namespace_iife_local_init_is_pure(expr),
    }
}

fn vendor_prune_target_first_wrapper_branch(
    expr: &Expr,
    target_param: &Id,
    local_effect_context: &LocalEffectContext,
    saw_effect: &mut bool,
) -> bool {
    if namespace_iife_local_init_is_pure(expr) {
        true
    } else {
        vendor_prune_target_first_wrapper_effect_expr(
            expr,
            target_param,
            local_effect_context,
            saw_effect,
        )
    }
}

fn vendor_prune_object_local_effect_method(method: &str) -> bool {
    matches!(
        method,
        "assign"
            | "defineProperties"
            | "defineProperty"
            | "freeze"
            | "preventExtensions"
            | "seal"
            | "setPrototypeOf"
    )
}

fn vendor_prune_local_effect_targets(
    item: &ModuleItem,
    local_effect_context: &LocalEffectContext,
) -> BTreeSet<Id> {
    if let ModuleItem::Stmt(Stmt::Expr(expr_stmt)) = item {
        return vendor_prune_expr_local_effect_targets(&expr_stmt.expr, local_effect_context);
    }
    let Some(var) = var_decl_of_item(item) else {
        return BTreeSet::new();
    };
    var.decls
        .iter()
        .filter_map(|decl| decl.init.as_deref())
        .flat_map(|init| vendor_prune_expr_local_effect_targets(init, local_effect_context))
        .collect()
}

fn vendor_prune_expr_local_effect_targets(
    expr: &Expr,
    local_effect_context: &LocalEffectContext,
) -> BTreeSet<Id> {
    match strip_parens(expr) {
        Expr::Assign(assign) => {
            let mut recorder = LocalEffectRecorder::new(&local_effect_context.declared_bindings);
            record_assign_target(&assign.left, &mut recorder);
            recorder.local_effects
        }
        Expr::Update(update) => {
            let mut recorder = LocalEffectRecorder::new(&local_effect_context.declared_bindings);
            record_update_target(&update.arg, &mut recorder);
            recorder.local_effects
        }
        Expr::Call(call) => vendor_prune_call_local_effect_target(call, local_effect_context)
            .into_iter()
            .collect(),
        Expr::Seq(seq) => seq
            .exprs
            .iter()
            .flat_map(|expr| vendor_prune_expr_local_effect_targets(expr, local_effect_context))
            .collect(),
        _ => BTreeSet::new(),
    }
}

struct LocalEffectRecorder<'a> {
    local_bindings: &'a BTreeSet<Id>,
    local_effects: BTreeSet<Id>,
}

impl<'a> LocalEffectRecorder<'a> {
    fn new(local_bindings: &'a BTreeSet<Id>) -> Self {
        Self {
            local_bindings,
            local_effects: BTreeSet::new(),
        }
    }
}

impl TargetAccessRecorder for LocalEffectRecorder<'_> {
    fn record_binding_write(&mut self, id: &Id) {
        if self.local_bindings.contains(id) {
            self.local_effects.insert(id.clone());
        }
    }

    fn record_member_write(&mut self, id: &Id) {
        self.local_effects.insert(id.clone());
    }
}

fn vendor_prune_call_local_effect_target(
    call: &CallExpr,
    local_effect_context: &LocalEffectContext,
) -> Option<Id> {
    if let Some(target) = local_namespace_iife_target(call) {
        return Some(target);
    }
    if let Some(target) = vendor_prune_inline_namespace_iife_target(call, local_effect_context) {
        return Some(target);
    }
    if let Some(target) = local_commonjs_module_iife_target(call, local_effect_context) {
        return Some(target);
    }
    if let Some(target) = vendor_prune_for_each_local_effect_target(call, local_effect_context) {
        return Some(target);
    }
    vendor_prune_direct_call_local_effect_target(call, local_effect_context)
}

fn vendor_prune_inline_namespace_iife_target(
    call: &CallExpr,
    local_effect_context: &LocalEffectContext,
) -> Option<Id> {
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let target = local_namespace_iife_arg_target(&call.args[0].expr)?;
    if !local_effect_context.declared_bindings.contains(&target) {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Fn(function) = strip_parens(callee) else {
        return None;
    };
    if function.function.params.len() == 1 {
        Some(target)
    } else {
        None
    }
}

fn vendor_prune_direct_call_local_effect_target(
    call: &CallExpr,
    local_effect_context: &LocalEffectContext,
) -> Option<Id> {
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    if let Expr::Ident(callee) = strip_parens(callee)
        && local_effect_context
            .vendor_prune_intrinsic_local_effect_callees
            .contains(&callee.to_id())
    {
        let first_arg = call.args.first()?;
        return local_member_owner(&first_arg.expr);
    }
    let Expr::Member(member) = strip_parens(callee) else {
        return None;
    };
    let Expr::Ident(object) = strip_parens(&member.obj) else {
        return None;
    };
    if object.sym.as_ref() != "Object" {
        return None;
    }
    let method = static_member_name(&member.prop)?;
    if !vendor_prune_object_local_effect_method(method.as_str()) {
        return None;
    }
    let first_arg = call.args.first()?;
    local_member_owner(&first_arg.expr)
}

fn vendor_prune_for_each_local_effect_target(
    call: &CallExpr,
    local_effect_context: &LocalEffectContext,
) -> Option<Id> {
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Member(member) = strip_parens(callee) else {
        return None;
    };
    if static_member_name(&member.prop).as_deref() != Some("forEach") {
        return None;
    }
    if !vendor_prune_static_object_iteration(&member.obj) {
        return None;
    }
    single_local_effect_target(vendor_prune_for_each_callback_targets(
        &call.args[0].expr,
        local_effect_context,
    )?)
}

fn vendor_prune_static_object_iteration(expr: &Expr) -> bool {
    let Expr::Call(call) = strip_parens(expr) else {
        return false;
    };
    if call
        .args
        .iter()
        .any(|arg| arg.spread.is_some() || !namespace_iife_local_init_is_pure(&arg.expr))
    {
        return false;
    }
    let Callee::Expr(callee) = &call.callee else {
        return false;
    };
    let Expr::Member(member) = strip_parens(callee) else {
        return false;
    };
    let Expr::Ident(object) = strip_parens(&member.obj) else {
        return false;
    };
    object.sym.as_ref() == "Object"
        && matches!(
            static_member_name(&member.prop).as_deref(),
            Some("entries" | "keys" | "values")
        )
}

fn vendor_prune_for_each_callback_targets(
    expr: &Expr,
    local_effect_context: &LocalEffectContext,
) -> Option<BTreeSet<Id>> {
    match strip_parens(expr) {
        Expr::Fn(function) => vendor_prune_for_each_callback_block_targets(
            function.function.body.as_ref()?,
            local_effect_context,
        ),
        Expr::Arrow(arrow) => match &*arrow.body {
            BlockStmtOrExpr::BlockStmt(block) => {
                vendor_prune_for_each_callback_block_targets(block, local_effect_context)
            }
            BlockStmtOrExpr::Expr(expr) => {
                vendor_prune_for_each_callback_expr_targets(expr, local_effect_context)
            }
        },
        _ => None,
    }
}

fn vendor_prune_for_each_callback_block_targets(
    block: &BlockStmt,
    local_effect_context: &LocalEffectContext,
) -> Option<BTreeSet<Id>> {
    let mut targets = BTreeSet::new();
    for stmt in &block.stmts {
        match stmt {
            Stmt::Decl(Decl::Fn(_)) => {}
            Stmt::Decl(Decl::Var(var)) if var.decls.iter().all(vendor_prune_callback_local_var) => {
            }
            Stmt::Expr(expr) => {
                targets.extend(vendor_prune_for_each_callback_expr_targets(
                    &expr.expr,
                    local_effect_context,
                )?);
            }
            Stmt::Return(ret) => {
                if let Some(arg) = &ret.arg {
                    targets.extend(vendor_prune_for_each_callback_expr_targets(
                        arg,
                        local_effect_context,
                    )?);
                }
            }
            _ => return None,
        }
    }
    Some(targets)
}

fn vendor_prune_callback_local_var(decl: &VarDeclarator) -> bool {
    matches!(decl.name, Pat::Ident(_))
        && decl
            .init
            .as_deref()
            .is_none_or(namespace_iife_local_init_is_pure)
}

fn vendor_prune_for_each_callback_expr_targets(
    expr: &Expr,
    local_effect_context: &LocalEffectContext,
) -> Option<BTreeSet<Id>> {
    match strip_parens(expr) {
        Expr::Assign(_) | Expr::Update(_) => {
            let targets = vendor_prune_expr_local_effect_targets(expr, local_effect_context);
            if targets.is_empty() {
                None
            } else {
                Some(targets)
            }
        }
        Expr::Call(call) => vendor_prune_call_local_effect_target(call, local_effect_context)
            .map(|target| BTreeSet::from([target])),
        Expr::Seq(seq) => {
            let mut targets = BTreeSet::new();
            for expr in &seq.exprs {
                targets.extend(vendor_prune_for_each_callback_expr_targets(
                    expr,
                    local_effect_context,
                )?);
            }
            Some(targets)
        }
        Expr::Bin(bin) if matches!(bin.op, BinaryOp::LogicalAnd | BinaryOp::LogicalOr) => {
            let mut targets =
                vendor_prune_callback_branch_targets(&bin.left, local_effect_context)?;
            targets.extend(vendor_prune_callback_branch_targets(
                &bin.right,
                local_effect_context,
            )?);
            Some(targets)
        }
        Expr::Cond(cond) if namespace_iife_local_init_is_pure(&cond.test) => {
            let mut targets =
                vendor_prune_callback_branch_targets(&cond.cons, local_effect_context)?;
            targets.extend(vendor_prune_callback_branch_targets(
                &cond.alt,
                local_effect_context,
            )?);
            Some(targets)
        }
        expr if namespace_iife_local_init_is_pure(expr) => Some(BTreeSet::new()),
        _ => None,
    }
}

fn vendor_prune_callback_branch_targets(
    expr: &Expr,
    local_effect_context: &LocalEffectContext,
) -> Option<BTreeSet<Id>> {
    if namespace_iife_local_init_is_pure(expr) {
        Some(BTreeSet::new())
    } else {
        vendor_prune_for_each_callback_expr_targets(expr, local_effect_context)
    }
}

fn single_local_effect_target(targets: BTreeSet<Id>) -> Option<Id> {
    let mut iter = targets.into_iter();
    let target = iter.next()?;
    if iter.next().is_none() {
        Some(target)
    } else {
        None
    }
}

pub fn local_namespace_iife_target(call: &CallExpr) -> Option<Id> {
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let target = local_namespace_iife_arg_target(&call.args[0].expr)?;
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Fn(function) = strip_parens(callee) else {
        return None;
    };
    if function.function.params.len() != 1 {
        return None;
    }
    let Pat::Ident(param) = &function.function.params[0].pat else {
        return None;
    };
    let body = function.function.body.as_ref()?;
    if namespace_iife_body_mutates_only_param(body, param.id.sym.as_ref()) {
        Some(target)
    } else {
        None
    }
}

fn local_namespace_iife_arg_target(expr: &Expr) -> Option<Id> {
    match strip_parens(expr) {
        Expr::Ident(ident) => Some(ident.to_id()),
        Expr::Assign(assign) if assign.op == AssignOp::Assign => {
            let AssignTarget::Simple(SimpleAssignTarget::Ident(target)) = &assign.left else {
                return None;
            };
            if matches!(
                strip_parens(&assign.right),
                Expr::Object(obj) if obj.props.is_empty()
            ) {
                Some(target.to_id())
            } else {
                None
            }
        }
        Expr::Bin(bin) if bin.op == BinaryOp::LogicalOr => {
            let Expr::Ident(target) = strip_parens(&bin.left) else {
                return None;
            };
            if is_namespace_iife_arg_fallback_for(&bin.right, &target.to_id()) {
                Some(target.to_id())
            } else {
                None
            }
        }
        _ => None,
    }
}

fn is_namespace_iife_arg_fallback_for(expr: &Expr, target: &Id) -> bool {
    match strip_parens(expr) {
        Expr::Object(obj) => obj.props.is_empty(),
        Expr::Assign(assign) if assign.op == AssignOp::Assign => {
            matches!(
                &assign.left,
                AssignTarget::Simple(SimpleAssignTarget::Ident(ident)) if ident.to_id() == *target
            ) && matches!(
                strip_parens(&assign.right),
                Expr::Object(obj) if obj.props.is_empty()
            )
        }
        Expr::Bin(bin) if bin.op == BinaryOp::LogicalOr => {
            matches!(
                strip_parens(&bin.left),
                Expr::Ident(ident) if ident.to_id() == *target
            ) && is_namespace_iife_arg_fallback_for(&bin.right, target)
        }
        _ => false,
    }
}

fn local_commonjs_module_iife_target(
    call: &CallExpr,
    local_effect_context: &LocalEffectContext,
) -> Option<Id> {
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let target = local_namespace_iife_arg_target(&call.args[0].expr)?;
    if !local_effect_context
        .vendor_prune_commonjs_module_bindings
        .contains(&target)
    {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Fn(function) = strip_parens(callee) else {
        return None;
    };
    if function.function.params.len() != 1 {
        return None;
    }
    let Pat::Ident(param) = &function.function.params[0].pat else {
        return None;
    };
    let body = function.function.body.as_ref()?;
    if commonjs_iife_body_mutates_only_module_param(body, param.id.sym.as_ref()) {
        Some(target)
    } else {
        None
    }
}

fn commonjs_iife_body_mutates_only_module_param(block: &BlockStmt, param: &str) -> bool {
    let local_bindings = commonjs_iife_local_bindings(block);
    let mut saw_write = false;
    for stmt in &block.stmts {
        let ok = match stmt {
            Stmt::Decl(Decl::Fn(_)) => true,
            Stmt::Decl(Decl::Var(var)) => var.decls.iter().all(|decl| {
                matches!(&decl.name, Pat::Ident(_))
                    && decl
                        .init
                        .as_deref()
                        .is_none_or(namespace_iife_local_init_is_pure)
            }),
            Stmt::Expr(expr) => commonjs_iife_param_effect_expr(
                strip_parens(&expr.expr),
                param,
                &local_bindings,
                &mut saw_write,
            ),
            _ => false,
        };
        if !ok {
            return false;
        }
    }
    saw_write
}

fn commonjs_iife_local_bindings(block: &BlockStmt) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for stmt in &block.stmts {
        match stmt {
            Stmt::Decl(Decl::Fn(function)) => {
                out.insert(function.ident.sym.to_string());
            }
            Stmt::Decl(Decl::Var(var)) => {
                out.extend(
                    var.decls
                        .iter()
                        .flat_map(|decl| binding_names(&decl.name))
                        .map(|id| id.0.to_string()),
                );
            }
            _ => {}
        }
    }
    out
}

fn commonjs_iife_param_effect_expr(
    expr: &Expr,
    param: &str,
    local_bindings: &BTreeSet<String>,
    saw_write: &mut bool,
) -> bool {
    match expr {
        Expr::Paren(paren) => {
            commonjs_iife_param_effect_expr(&paren.expr, param, local_bindings, saw_write)
        }
        Expr::Seq(seq) => seq
            .exprs
            .iter()
            .all(|expr| commonjs_iife_param_effect_expr(expr, param, local_bindings, saw_write)),
        Expr::Assign(assign) if assign.op == AssignOp::Assign => {
            let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left else {
                return false;
            };
            let Some(root) = local_member_owner(&member.obj) else {
                return false;
            };
            if static_member_name(&member.prop).as_deref() == Some("__proto__") {
                return false;
            }
            let root_name = root.0.as_ref();
            if root_name == param {
                *saw_write = true;
            } else if !local_bindings.contains(root_name) {
                return false;
            }
            namespace_iife_local_init_is_pure(&assign.right)
        }
        Expr::Call(call) => match commonjs_nested_iife_mutates_param(call, param) {
            Some(true) => {
                *saw_write = true;
                true
            }
            Some(false) => true,
            None => false,
        },
        Expr::Cond(cond) if commonjs_truthy_exports_test(&cond.test, param) => {
            commonjs_iife_param_effect_expr(&cond.cons, param, local_bindings, saw_write)
        }
        expr => namespace_iife_local_init_is_pure(expr),
    }
}

fn commonjs_nested_iife_mutates_param(call: &CallExpr, param: &str) -> Option<bool> {
    if !call.args.is_empty() {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Fn(function) = strip_parens(callee) else {
        return None;
    };
    if !function.function.params.is_empty() {
        return None;
    }
    Some(commonjs_iife_body_mutates_only_module_param(
        function.function.body.as_ref()?,
        param,
    ))
}

fn commonjs_truthy_exports_test(expr: &Expr, param: &str) -> bool {
    let Expr::Member(member) = strip_parens(expr) else {
        return false;
    };
    matches!(
        strip_parens(&member.obj),
        Expr::Ident(ident) if ident.sym.as_ref() == param
    ) && static_member_name(&member.prop).as_deref() == Some("exports")
}

fn namespace_iife_body_mutates_only_param(block: &BlockStmt, param: &str) -> bool {
    let mut saw_write = false;
    for stmt in &block.stmts {
        let ok = match stmt {
            Stmt::Decl(Decl::Fn(_)) => true,
            Stmt::Decl(Decl::Var(var)) => var.decls.iter().all(|decl| {
                matches!(&decl.name, Pat::Ident(_))
                    && decl
                        .init
                        .as_deref()
                        .is_none_or(namespace_iife_local_init_is_pure)
            }),
            Stmt::Expr(expr) => {
                namespace_iife_param_write_expr(strip_parens(&expr.expr), param, &mut saw_write)
            }
            _ => false,
        };
        if !ok {
            return false;
        }
    }
    saw_write
}

fn namespace_iife_local_init_is_pure(expr: &Expr) -> bool {
    match strip_parens(expr) {
        Expr::Lit(_)
        | Expr::Ident(_)
        | Expr::This(_)
        | Expr::Fn(_)
        | Expr::Arrow(_)
        | Expr::Class(_)
        | Expr::Tpl(_)
        | Expr::PrivateName(_) => true,
        Expr::Unary(u) => {
            matches!(u.op, UnaryOp::Void | UnaryOp::TypeOf)
                || namespace_iife_local_init_is_pure(&u.arg)
        }
        Expr::Array(arr) => arr
            .elems
            .iter()
            .flatten()
            .all(|elem| elem.spread.is_none() && namespace_iife_local_init_is_pure(&elem.expr)),
        Expr::Object(obj) => obj.props.iter().all(|prop| match prop {
            PropOrSpread::Spread(_) => false,
            PropOrSpread::Prop(prop) => namespace_iife_prop_is_pure(prop),
        }),
        Expr::Member(member) => namespace_iife_local_init_is_pure(&member.obj),
        Expr::Cond(cond) => {
            namespace_iife_local_init_is_pure(&cond.test)
                && namespace_iife_local_init_is_pure(&cond.cons)
                && namespace_iife_local_init_is_pure(&cond.alt)
        }
        Expr::Bin(bin) => {
            namespace_iife_local_init_is_pure(&bin.left)
                && namespace_iife_local_init_is_pure(&bin.right)
        }
        Expr::Seq(seq) => seq
            .exprs
            .iter()
            .all(|expr| namespace_iife_local_init_is_pure(expr)),
        _ => false,
    }
}

fn namespace_iife_prop_is_pure(prop: &Prop) -> bool {
    match prop {
        Prop::Shorthand(_) => true,
        Prop::KeyValue(kv) => namespace_iife_local_init_is_pure(&kv.value),
        Prop::Method(_) | Prop::Getter(_) | Prop::Setter(_) => true,
        Prop::Assign(assign) => namespace_iife_local_init_is_pure(&assign.value),
    }
}

fn namespace_iife_param_write_expr(expr: &Expr, param: &str, saw_write: &mut bool) -> bool {
    match expr {
        Expr::Paren(paren) => namespace_iife_param_write_expr(&paren.expr, param, saw_write),
        Expr::Seq(seq) => seq
            .exprs
            .iter()
            .all(|expr| namespace_iife_param_write_expr(expr, param, saw_write)),
        Expr::Assign(assign) if assign.op == AssignOp::Assign => {
            let AssignTarget::Simple(SimpleAssignTarget::Member(member)) = &assign.left else {
                return false;
            };
            if !matches!(
                strip_parens(&member.obj),
                Expr::Ident(ident) if ident.sym.as_ref() == param
            ) {
                return false;
            }
            if static_member_name(&member.prop).as_deref() == Some("__proto__") {
                return false;
            }
            *saw_write = true;
            true
        }
        _ => false,
    }
}

fn local_member_owner(expr: &Expr) -> Option<Id> {
    match strip_parens(expr) {
        Expr::Ident(ident) => Some(ident.to_id()),
        Expr::Member(member) => local_member_owner(&member.obj),
        _ => None,
    }
}

fn static_member_name(prop: &MemberProp) -> Option<String> {
    match prop {
        MemberProp::Ident(ident) => Some(ident.sym.to_string()),
        MemberProp::PrivateName(name) => Some(name.name.to_string()),
        MemberProp::Computed(computed) => match strip_parens(&computed.expr) {
            Expr::Lit(Lit::Str(value)) => Some(value.value.to_string_lossy().into_owned()),
            _ => None,
        },
    }
}

fn static_prop_name(prop: &PropName) -> Option<String> {
    match prop {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(value) => Some(value.value.to_string_lossy().into_owned()),
        PropName::Num(value) => Some(value.value.to_string()),
        PropName::BigInt(value) => Some(value.value.to_string()),
        PropName::Computed(computed) => match strip_parens(&computed.expr) {
            Expr::Lit(Lit::Str(value)) => Some(value.value.to_string_lossy().into_owned()),
            _ => None,
        },
    }
}
