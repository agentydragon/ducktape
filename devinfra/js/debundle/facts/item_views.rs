use super::*;

pub enum TopLevelItemView<'a> {
    Borrowed(&'a ModuleItem),
    Owned(ModuleItem),
}

impl TopLevelItemView<'_> {
    pub fn as_module_item(&self) -> &ModuleItem {
        match self {
            Self::Borrowed(item) => item,
            Self::Owned(item) => item,
        }
    }
}

/// View the top-level body as analysis statements. Multi-declarator
/// top-level `var/let/const` statements are split into N
/// single-declarator statements preserving source order; unchanged
/// statements stay borrowed so the analyzer does not clone the whole
/// app chunk just to get per-declarator ownership.
pub fn top_level_item_views(body: &[ModuleItem]) -> Vec<TopLevelItemView<'_>> {
    let mut out = Vec::with_capacity(body.len());
    for item in body {
        match item {
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.decls.len() > 1 => {
                for decl in &var.decls {
                    let single = VarDecl {
                        span: decl.span,
                        ctxt: var.ctxt,
                        kind: var.kind,
                        declare: var.declare,
                        decls: vec![decl.clone()],
                    };
                    out.push(TopLevelItemView::Owned(ModuleItem::Stmt(Stmt::Decl(
                        Decl::Var(Box::new(single)),
                    ))));
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                match &export_decl.decl {
                    Decl::Var(var) if var.decls.len() > 1 => {
                        for decl in &var.decls {
                            let single = VarDecl {
                                span: decl.span,
                                ctxt: var.ctxt,
                                kind: var.kind,
                                declare: var.declare,
                                decls: vec![decl.clone()],
                            };
                            out.push(TopLevelItemView::Owned(ModuleItem::ModuleDecl(
                                ModuleDecl::ExportDecl(ExportDecl {
                                    span: decl.span,
                                    decl: Decl::Var(Box::new(single)),
                                }),
                            )));
                        }
                    }
                    _ => out.push(TopLevelItemView::Borrowed(item)),
                }
            }
            _ => out.push(TopLevelItemView::Borrowed(item)),
        }
    }
    out
}

/// Walk `body` and collect the subset of `SHADOW_TRACKED_GLOBALS`
/// (the union of every global name any purity-whitelist table keys
/// on — receivers like `Math`/`Object`, global callables like
/// `Boolean`/`Symbol`, pure-new builtins like `Map`/`Set`) that are
/// declared at the chunk's top-level scope (`var/let/const`,
/// `function`, `class`, exported decls) or bound by an import
/// specifier (default / namespace / named). The classifier consults
/// this set to skip the whitelist for any name the chunk shadows —
/// `const Math = …` and `import { Math } from "./userland"` both
/// make `Math.PI` an Unknown read, and `const Map = class { … }`
/// makes `new Map()` an Unknown construction, not the built-in.
/// See docs/design.md A8.
pub(crate) fn compute_shadowed_globals(body: &[TopLevelItemView<'_>]) -> BTreeSet<&'static str> {
    let mut shadowed = BTreeSet::new();
    let try_shadow = |name: &str, into: &mut BTreeSet<&'static str>| {
        if let Some(global) = SHADOW_TRACKED_GLOBALS.get(name) {
            into.insert(*global);
        }
    };
    for item in body {
        let item = item.as_module_item();
        for id in collect_declared_names(item) {
            try_shadow(id.0.as_ref(), &mut shadowed);
        }
        if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item {
            for spec in &import.specifiers {
                let local = match spec {
                    ImportSpecifier::Named(named) => named.local.sym.as_ref(),
                    ImportSpecifier::Default(default) => default.local.sym.as_ref(),
                    ImportSpecifier::Namespace(namespace) => namespace.local.sym.as_ref(),
                };
                try_shadow(local, &mut shadowed);
            }
        }
    }
    shadowed
}

pub(crate) fn var_decl_of_item(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Var(var) => Some(var),
            _ => None,
        },
        _ => None,
    }
}

pub(crate) fn class_of_item(item: &ModuleItem) -> Option<&Class> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(cls))) => Some(&cls.class),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Class(cls) => Some(&cls.class),
            _ => None,
        },
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl)) => match &decl.decl {
            DefaultDecl::Class(cls) => Some(&cls.class),
            _ => None,
        },
        _ => None,
    }
}

pub(crate) fn classify_item(item: &ModuleItem) -> StatementKind {
    match item {
        ModuleItem::ModuleDecl(ModuleDecl::Import(_)) => StatementKind::Import,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Var(_) => StatementKind::VarDecl,
            Decl::Fn(_) => StatementKind::FnDecl,
            Decl::Class(_) => StatementKind::ClassDecl,
            _ => StatementKind::Export,
        },
        ModuleItem::ModuleDecl(_) => StatementKind::Export,
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(_))) => StatementKind::VarDecl,
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => StatementKind::FnDecl,
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(_))) => StatementKind::ClassDecl,
        _ => StatementKind::SideEffect,
    }
}

pub(crate) fn collect_declared_names(item: &ModuleItem) -> BTreeSet<Id> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declaration_names(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => declaration_names(&decl.decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl)) => match &decl.decl {
            DefaultDecl::Fn(fn_expr) => fn_expr
                .ident
                .as_ref()
                .map(|id| BTreeSet::from([id.to_id()]))
                .unwrap_or_default(),
            DefaultDecl::Class(class_expr) => class_expr
                .ident
                .as_ref()
                .map(|id| BTreeSet::from([id.to_id()]))
                .unwrap_or_default(),
            _ => BTreeSet::new(),
        },
        // `var` declarations hoist to module scope out of blocks
        // (`try { var impl = ...; } catch { var impl = ...; }`,
        // `if`, loop bodies). The enclosing top-level statement is
        // the binding's owner.
        ModuleItem::Stmt(stmt) => hoisted_var_ids(stmt).into_iter().collect(),
        _ => BTreeSet::new(),
    }
}

fn declaration_names(decl: &Decl) -> BTreeSet<Id> {
    declaration_ids(decl).into_iter().collect()
}

/// `true` when the statement's declared binding is directly a
/// function value. See [`StatementFacts::declares_direct_function`].
pub(crate) fn declares_direct_function(item: &ModuleItem) -> bool {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(_))) => true,
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl))
            if matches!(decl.decl, Decl::Fn(_)) =>
        {
            true
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl))
            if matches!(decl.decl, DefaultDecl::Fn(_)) =>
        {
            true
        }
        _ => var_decl_of_item(item).is_some_and(|var| {
            var.decls.len() == 1
                && matches!(&var.decls[0].name, Pat::Ident(_))
                && var.decls[0]
                    .init
                    .as_deref()
                    .map(strip_parens)
                    .is_some_and(|init| matches!(init, Expr::Fn(_) | Expr::Arrow(_)))
        }),
    }
}

pub(crate) fn collect_async_direct_function_bindings(
    body: &[TopLevelItemView<'_>],
) -> BTreeSet<Id> {
    body.iter()
        .filter_map(|item| async_direct_function_binding(item.as_module_item()))
        .collect()
}

fn async_direct_function_binding(item: &ModuleItem) -> Option<Id> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Fn(fn_decl))) if fn_decl.function.is_async => {
            Some(fn_decl.ident.to_id())
        }
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(decl)) => match &decl.decl {
            Decl::Fn(fn_decl) if fn_decl.function.is_async => Some(fn_decl.ident.to_id()),
            _ => var_decl_of_item(item).and_then(async_var_function_binding),
        },
        ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(decl)) => match &decl.decl {
            DefaultDecl::Fn(fn_expr) if fn_expr.function.is_async => {
                fn_expr.ident.as_ref().map(|ident| ident.to_id())
            }
            _ => None,
        },
        _ => var_decl_of_item(item).and_then(async_var_function_binding),
    }
}

fn async_var_function_binding(var: &VarDecl) -> Option<Id> {
    if var.decls.len() != 1 {
        return None;
    }
    let Pat::Ident(binding) = &var.decls[0].name else {
        return None;
    };
    let init = var.decls[0].init.as_deref().map(strip_parens)?;
    let is_async = match init {
        Expr::Fn(fn_expr) => fn_expr.function.is_async,
        Expr::Arrow(arrow) => arrow.is_async,
        _ => false,
    };
    is_async.then(|| binding.id.to_id())
}
