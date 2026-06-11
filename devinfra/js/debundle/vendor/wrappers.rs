use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde_json::Value;
use swc_common::{DUMMY_SP, SyntaxContext};
use swc_ecma_ast::*;

use artifact::path_from_module_path;
use binding_targets::module_export_name;
use js_ast::{ParsedJsModule, emit_js_module};
use spec::BundledPartialSwapPackage;

pub(super) fn generate_named_from_default_wrapper(
    upstream_ast: &ParsedJsModule,
    named_exports: &BTreeSet<String>,
) -> Result<String> {
    let mut body = Vec::new();
    // The synthetic local hoisting upstream's default must not collide
    // with any identifier the upstream body already uses — a duplicate
    // top-level `const` is a module-level SyntaxError.
    let mut used_idents = super::module_used_idents(&upstream_ast.module);
    let default_local_name = super::unique_synthetic_ident("_d", &mut used_idents);
    let default_local = Ident::new_no_ctxt(default_local_name.as_str().into(), DUMMY_SP);
    for item in &upstream_ast.module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) => {
                body.push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(VarDecl {
                    span: DUMMY_SP,
                    ctxt: SyntaxContext::empty(),
                    kind: VarDeclKind::Const,
                    declare: false,
                    decls: vec![VarDeclarator {
                        span: DUMMY_SP,
                        name: Pat::Ident(BindingIdent {
                            id: default_local.clone(),
                            type_ann: None,
                        }),
                        init: Some(default_expr.expr.clone()),
                        definite: false,
                    }],
                })))));
            }
            _ => body.push(item.clone()),
        }
    }
    body.push(export_default_ident(&default_local_name));
    for name in named_exports {
        body.push(export_const_member(name, &default_local_name, name));
    }
    emit_js_module(
        &ParsedJsModule {
            cm: upstream_ast.cm.clone(),
            module: Module {
                span: DUMMY_SP,
                body,
                shebang: None,
            },
            unresolved_mark: upstream_ast.unresolved_mark,
            top_level_mark: upstream_ast.top_level_mark,
        },
        &[],
    )
}

pub(super) fn generate_named_from_json_default_wrapper(
    upstream_json: &Value,
    named_exports: &BTreeSet<String>,
) -> Result<String> {
    // `_d` cannot collide here: the wrapper body is pure JSON data, so
    // no upstream identifier exists to clash with.
    let body = serde_json::to_string_pretty(upstream_json)?;
    let named = named_exports
        .iter()
        .map(|name| format!("export const {name} = _d.{name};"))
        .collect::<Vec<_>>()
        .join("\n");
    Ok(format!("const _d = {body};\nexport default _d;\n{named}\n"))
}

pub(super) fn generate_named_from_module_default_wrapper(
    upstream_ast: &ParsedJsModule,
    vendor_exports: &BTreeSet<String>,
    chunk_path: &str,
) -> Result<String> {
    // Avoid duplicate-declaration SyntaxErrors when the upstream body
    // already binds the synthetic default-local name.
    let mut used_idents = super::module_used_idents(&upstream_ast.module);
    let default_local_name = &super::unique_synthetic_ident("__vendor_default__", &mut used_idents);
    let mut found_default = false;
    let mut deferred_default_alias: Option<String> = None;
    let mut body = Vec::new();
    for item in &upstream_ast.module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) => {
                found_default = true;
                body.push(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(VarDecl {
                    span: DUMMY_SP,
                    ctxt: SyntaxContext::empty(),
                    kind: VarDeclKind::Const,
                    declare: false,
                    decls: vec![VarDeclarator {
                        span: DUMMY_SP,
                        name: Pat::Ident(BindingIdent {
                            id: Ident::new_no_ctxt(default_local_name.as_str().into(), DUMMY_SP),
                            type_ann: None,
                        }),
                        init: Some(default_expr.expr.clone()),
                        definite: false,
                    }],
                })))));
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) => {
                found_default = true;
                match &default_decl.decl {
                    DefaultDecl::Fn(function) => {
                        if let Some(ident) = &function.ident {
                            body.push(ModuleItem::Stmt(Stmt::Decl(Decl::Fn(FnDecl {
                                ident: ident.clone(),
                                declare: false,
                                function: function.function.clone(),
                            }))));
                            body.push(const_alias(default_local_name, ident.sym.as_ref()));
                        } else {
                            // Anonymous `export default function () { ... }`
                            // collapses to `const __vendor_default__ = function () { ... };`.
                            body.push(const_init_with_expr(
                                default_local_name,
                                Expr::Fn(FnExpr {
                                    ident: None,
                                    function: function.function.clone(),
                                }),
                            ));
                        }
                    }
                    DefaultDecl::Class(class) => {
                        if let Some(ident) = &class.ident {
                            body.push(ModuleItem::Stmt(Stmt::Decl(Decl::Class(ClassDecl {
                                ident: ident.clone(),
                                declare: false,
                                class: class.class.clone(),
                            }))));
                            body.push(const_alias(default_local_name, ident.sym.as_ref()));
                        } else {
                            // Anonymous `export default class { ... }` collapses
                            // to `const __vendor_default__ = class { ... };`.
                            body.push(const_init_with_expr(
                                default_local_name,
                                Expr::Class(ClassExpr {
                                    ident: None,
                                    class: class.class.clone(),
                                }),
                            ));
                        }
                    }
                    DefaultDecl::TsInterfaceDecl(_) => {}
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named_decl)) => {
                if named_decl.src.is_some() {
                    body.push(item.clone());
                    continue;
                }
                let mut remaining = Vec::with_capacity(named_decl.specifiers.len());
                for specifier in &named_decl.specifiers {
                    let ExportSpecifier::Named(named_specifier) = specifier else {
                        remaining.push(specifier.clone());
                        continue;
                    };
                    let exported_name = named_specifier
                        .exported
                        .as_ref()
                        .map(module_export_name)
                        .unwrap_or_else(|| module_export_name(&named_specifier.orig));
                    if exported_name != "default" {
                        remaining.push(specifier.clone());
                        continue;
                    }
                    let ModuleExportName::Ident(local) = &named_specifier.orig else {
                        bail!(
                            "swap_vendor_chunks vendor entry {chunk_path} named-from-module-default: \"export {{ ... as default }}\" must alias a local identifier"
                        );
                    };
                    if deferred_default_alias.is_some() {
                        bail!(
                            "swap_vendor_chunks vendor entry {chunk_path} named-from-module-default: upstream declares more than one default export"
                        );
                    }
                    found_default = true;
                    // Defer the `const __vendor_default__ = <local>;` emission
                    // to the end of the body. ESM allows `export { lib as default };
                    // const lib = ...;`, so emitting the alias at the original
                    // export position would TDZ on `lib` if the export sits before
                    // the local declaration.
                    deferred_default_alias = Some(local.sym.to_string());
                }
                if !remaining.is_empty() {
                    let mut kept = named_decl.clone();
                    kept.specifiers = remaining;
                    body.push(ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(kept)));
                }
            }
            _ => body.push(item.clone()),
        }
    }
    if let Some(local) = deferred_default_alias {
        body.push(const_alias(default_local_name, &local));
    }
    if !found_default {
        bail!(
            "swap_vendor_chunks vendor entry {chunk_path} named-from-module-default: upstream has no default export"
        );
    }
    body.push(export_default_ident(default_local_name));
    for name in vendor_exports {
        if name == "default" {
            continue;
        }
        body.push(export_const_ident(name, default_local_name));
    }
    emit_js_module(
        &ParsedJsModule {
            cm: upstream_ast.cm.clone(),
            module: Module {
                span: DUMMY_SP,
                body,
                shebang: None,
            },
            unresolved_mark: upstream_ast.unresolved_mark,
            top_level_mark: upstream_ast.top_level_mark,
        },
        &[],
    )
}

pub(super) fn write_wrapper_if_requested(
    write: bool,
    output_wrapper_dir: Option<&Path>,
    chunk_id: &str,
    entry_file: &str,
    source: &str,
) -> Result<Option<PathBuf>> {
    let Some(output_wrapper_dir) = output_wrapper_dir else {
        return Ok(None);
    };
    let wrapper_abs_path = output_wrapper_dir
        .join(path_from_module_path(chunk_id))
        .join(path_from_module_path(entry_file));
    if write {
        if let Some(parent) = wrapper_abs_path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&wrapper_abs_path, source)?;
    }
    Ok(Some(wrapper_abs_path))
}

pub(super) struct BundledGeneratedAssets {
    pub(super) bundle_abs_path: PathBuf,
    pub(super) facades: BTreeMap<String, BundledGeneratedFacade>,
}

pub(super) struct BundledGeneratedFacade {
    pub(super) abs_path: PathBuf,
    pub(super) app_path: String,
}

pub(super) fn write_bundled_partial_swap_assets_if_requested(
    write: bool,
    output_wrapper_dir: Option<&Path>,
    chunk_id: &str,
    bundle_source_path: &Path,
    bundle_source: &str,
    packages: &BTreeMap<String, BundledPartialSwapPackage>,
) -> Result<BundledGeneratedAssets> {
    let output_wrapper_dir = output_wrapper_dir.with_context(|| {
        format!(
            "bundled_partial_swap for chunk {chunk_id} requires swap_vendor_chunks.output_wrapper_dir"
        )
    })?;
    let chunk_path = path_from_module_path(chunk_id);
    let abs_dir = output_wrapper_dir.join(&chunk_path);
    let app_dir = PathBuf::from("vendors/generated").join(&chunk_path);
    let bundle_abs_path = abs_dir.join("bundle.js");
    if write {
        if let Some(parent) = bundle_abs_path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&bundle_abs_path, bundle_source).with_context(|| {
            format!(
                "copying bundled_partial_swap bundle {} to {}",
                bundle_source_path.display(),
                bundle_abs_path.display()
            )
        })?;
    }

    let mut facades = BTreeMap::new();
    let mut slugs = BTreeMap::<String, String>::new();
    for (package_name, package) in packages {
        let slug = package_slug(package_name);
        if let Some(prior) = slugs.insert(slug.clone(), package_name.clone()) {
            bail!(
                "bundled_partial_swap packages `{prior}` and `{package_name}` both map to generated facade name `{slug}.js`"
            );
        }
        let file_name = format!("{slug}.js");
        let abs_path = abs_dir.join(&file_name);
        let app_path = app_dir.join(&file_name);
        let facade_source = generate_bundled_partial_swap_facade(&package.bundle_export);
        if write {
            if let Some(parent) = abs_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(&abs_path, facade_source)
                .with_context(|| format!("writing {}", abs_path.display()))?;
        }
        facades.insert(
            package_name.clone(),
            BundledGeneratedFacade {
                abs_path,
                app_path: path_to_module_string(&app_path),
            },
        );
    }

    Ok(BundledGeneratedAssets {
        bundle_abs_path,
        facades,
    })
}

fn generate_bundled_partial_swap_facade(bundle_export: &str) -> String {
    if bundle_export == "default" {
        return "import __debundle_bundle_export__ from \"./bundle.js\";\nexport default __debundle_bundle_export__;\n"
            .to_string();
    }
    format!(
        "import {{ {bundle_export} as __debundle_bundle_export__ }} from \"./bundle.js\";\nexport default __debundle_bundle_export__;\n"
    )
}

fn package_slug(package_name: &str) -> String {
    package_name
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect()
}

fn path_to_module_string(path: &Path) -> String {
    path.components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/")
}

pub(super) fn set_diff(left: &BTreeSet<String>, right: &BTreeSet<String>) -> BTreeSet<String> {
    left.difference(right).cloned().collect()
}

fn export_default_ident(name: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(ExportDefaultExpr {
        span: DUMMY_SP,
        expr: Box::new(Expr::Ident(Ident::new_no_ctxt(name.into(), DUMMY_SP))),
    }))
}

fn export_const_member(export_name: &str, object_name: &str, property_name: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
        span: DUMMY_SP,
        decl: Decl::Var(Box::new(VarDecl {
            span: DUMMY_SP,
            ctxt: SyntaxContext::empty(),
            kind: VarDeclKind::Const,
            declare: false,
            decls: vec![VarDeclarator {
                span: DUMMY_SP,
                name: Pat::Ident(BindingIdent {
                    id: Ident::new_no_ctxt(export_name.into(), DUMMY_SP),
                    type_ann: None,
                }),
                init: Some(Box::new(Expr::Member(MemberExpr {
                    span: DUMMY_SP,
                    obj: Box::new(Expr::Ident(Ident::new_no_ctxt(
                        object_name.into(),
                        DUMMY_SP,
                    ))),
                    prop: MemberProp::Ident(IdentName::new(property_name.into(), DUMMY_SP)),
                }))),
                definite: false,
            }],
        })),
    }))
}

fn export_const_ident(export_name: &str, local_name: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
        span: DUMMY_SP,
        decl: Decl::Var(Box::new(VarDecl {
            span: DUMMY_SP,
            ctxt: SyntaxContext::empty(),
            kind: VarDeclKind::Const,
            declare: false,
            decls: vec![VarDeclarator {
                span: DUMMY_SP,
                name: Pat::Ident(BindingIdent {
                    id: Ident::new_no_ctxt(export_name.into(), DUMMY_SP),
                    type_ann: None,
                }),
                init: Some(Box::new(Expr::Ident(Ident::new_no_ctxt(
                    local_name.into(),
                    DUMMY_SP,
                )))),
                definite: false,
            }],
        })),
    }))
}

fn const_alias(alias: &str, target: &str) -> ModuleItem {
    const_init_with_expr(
        alias,
        Expr::Ident(Ident::new_no_ctxt(target.into(), DUMMY_SP)),
    )
}

fn const_init_with_expr(alias: &str, init: Expr) -> ModuleItem {
    ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(VarDecl {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        kind: VarDeclKind::Const,
        declare: false,
        decls: vec![VarDeclarator {
            span: DUMMY_SP,
            name: Pat::Ident(BindingIdent {
                id: Ident::new_no_ctxt(alias.into(), DUMMY_SP),
                type_ann: None,
            }),
            init: Some(Box::new(init)),
            definite: false,
        }],
    }))))
}
