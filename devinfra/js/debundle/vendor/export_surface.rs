//! Inspect export surfaces, preserving the distinction between external names and local identities.
use anyhow::{Result, bail};
use binding_targets::{declaration_name_strings, public_export_name};
use js_ast::{is_binding_identifier, str_value};
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::*;

/// Collect the boundary-rename mapping (vendor-LOCAL binding name → the
/// distinct, valid export name it is published under) and validate it against
/// the entry's genuine exports — both in a single pass over `module.body`.
///
/// Validation bails when a mapping key (a vendor-LOCAL binding name) is itself a
/// genuine export name of the vendor entry bound to a *different* local. The
/// caller-side rewrite treats `import { k }` as "the caller spelled export
/// `<mapping[k]>` by its vendor-local name" — but when the vendor really exports
/// the name `k` (from another local), that import is legitimate and rewriting it
/// would silently rebind the caller to the wrong value.
pub(super) fn collect_and_validate_boundary_mapping(
    module: &Module,
    chunk_path: &str,
) -> Result<BTreeMap<String, String>> {
    let mut mapping: BTreeMap<String, String> = BTreeMap::new();
    // export name -> Some(local sym) when the export aliases a plain local
    // binding; None when its local identity is not a local ident (forwarded
    // `export … from`, string-literal orig).
    let mut export_locals: BTreeMap<String, Option<String>> = BTreeMap::new();
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
                for specifier in &named.specifiers {
                    let ExportSpecifier::Named(named_spec) = specifier else {
                        continue;
                    };
                    let exported = public_export_name(named_spec);
                    let local = match (&named.src, &named_spec.orig) {
                        (None, ModuleExportName::Ident(local)) => Some(local.sym.to_string()),
                        _ => None,
                    };
                    // A boundary rename: a non-forwarded local binding published
                    // under a different, valid identifier.
                    if let Some(local_sym) = &local
                        && exported != *local_sym
                        && is_binding_identifier(&exported)
                    {
                        mapping.insert(local_sym.clone(), exported.clone());
                    }
                    export_locals.insert(exported, local);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for name in declaration_name_strings(&export_decl.decl) {
                    export_locals.insert(name.clone(), Some(name));
                }
            }
            _ => {}
        }
    }
    for local_name in mapping.keys() {
        let Some(identity) = export_locals.get(local_name) else {
            continue;
        };
        if identity.as_deref() != Some(local_name.as_str()) {
            let bound_to = identity
                .as_deref()
                .map(|local| format!("local `{local}`"))
                .unwrap_or_else(|| "a non-local origin".to_string());
            bail!(
                "vendor boundary mapping for {chunk_path}: key `{local_name}` collides with a genuine export named `{local_name}` bound to {bound_to}; rewriting caller imports of `{local_name}` would silently rebind them to the wrong value",
            );
        }
    }
    Ok(mapping)
}

pub(super) fn module_has_export_star(module: &Module) -> bool {
    module.body.iter().any(|item| match item {
        // `export * from "./other.js";`
        ModuleItem::ModuleDecl(ModuleDecl::ExportAll(_)) => true,
        // `export * as ns from "./other.js";` — still re-exports the
        // whole namespace, just under one name.
        ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
            named.src.is_some()
                && named
                    .specifiers
                    .iter()
                    .any(|s| matches!(s, ExportSpecifier::Namespace(_)))
        }
        _ => false,
    })
}

/// Export names of `module`. The `default` name is included whether it
/// comes from an `export default …` declaration or the named form
/// `export { x as default }` — the two spellings are equivalent on the
/// module's export surface.
pub(super) fn collect_exported_names(module: &Module) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(_))
            | ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(_)) => {
                names.insert("default".to_string());
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                for name in declaration_name_strings(&export_decl.decl) {
                    names.insert(name);
                }
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => {
                for specifier in &named.specifiers {
                    if let ExportSpecifier::Named(named_specifier) = specifier {
                        names.insert(public_export_name(named_specifier));
                    }
                }
            }
            _ => {}
        }
    }
    names
}

/// Export names of `module` that are verified aliases of its default
/// export — bound to the same local binding as the default. Empty when
/// the default's local identity cannot be established.
pub(super) fn verified_default_alias_export_names(module: &Module) -> BTreeSet<String> {
    let by_export = collect_local_idents_by_export_name(module);
    let default_id = by_export
        .get("default")
        .cloned()
        .or_else(|| default_export_decl_local_id(module));
    let Some(default_id) = default_id else {
        return BTreeSet::new();
    };
    by_export
        .iter()
        .filter(|(name, id)| name.as_str() != "default" && **id == default_id)
        .map(|(name, _)| name.clone())
        .collect()
}

/// Local binding `Id` of the module's `export default …` declaration,
/// when the default is a plain local identifier (or a named fn/class).
fn default_export_decl_local_id(module: &Module) -> Option<Id> {
    for item in &module.body {
        match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) => {
                let Expr::Ident(ident) = &*default_expr.expr else {
                    return None;
                };
                return Some(ident.to_id());
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultDecl(default_decl)) => {
                return match &default_decl.decl {
                    DefaultDecl::Fn(function) => function.ident.as_ref().map(Ident::to_id),
                    DefaultDecl::Class(class) => class.ident.as_ref().map(Ident::to_id),
                    DefaultDecl::TsInterfaceDecl(_) => None,
                };
            }
            _ => {}
        }
    }
    None
}

pub(super) fn collect_default_export_object_keys(
    module: &Module,
    chunk_path: &str,
) -> Result<BTreeSet<String>> {
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportDefaultExpr(default_expr)) = item else {
            continue;
        };
        let Expr::Object(object) = &*default_expr.expr else {
            bail!(
                "swap_vendor_chunks vendor entry {chunk_path} named-from-default: upstream default export is not an object literal"
            );
        };
        let mut keys = BTreeSet::new();
        for prop in &object.props {
            let PropOrSpread::Prop(prop) = prop else {
                continue;
            };
            // Two accepted prop shapes — both produce the same wrapper
            // (`export const K = _d.K;`):
            // * `KeyValue` with `Ident` or `Str` key (`{ ping: fn, "pong": fn }`).
            // * `Shorthand` (`{ ping, pong }`) where the local binding name
            //   is the property name; `_d.ping` re-exports the same value
            //   because object-literal shorthand assigns the binding's
            //   value as a data property under the binding's name.
            // Real-world vendor `index.mjs` files use shorthand commonly;
            // both shapes are emit-equivalent for the wrapper's purposes.
            let key = match &**prop {
                Prop::KeyValue(key_value) => prop_name(&key_value.key),
                Prop::Shorthand(ident) => Some(ident.sym.to_string()),
                _ => None,
            };
            if let Some(key) = key {
                keys.insert(key);
            }
        }
        return Ok(keys);
    }
    bail!(
        "swap_vendor_chunks vendor entry {chunk_path} named-from-default: upstream has no export default declaration"
    );
}

fn prop_name(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(string) => Some(str_value(string)),
        _ => None,
    }
}

/// Map each chunk-local named export to the hygiene-preserving `Id`
/// (`(atom, SyntaxContext)`) of the binding it re-exports. The `orig`
/// identifier carries the resolver-assigned `SyntaxContext`, so the
/// returned `Id` is the canonical binding identity used to key the
/// self-rewrite `bindings` map.
pub(super) fn collect_local_idents_by_export_name(module: &Module) -> BTreeMap<String, Id> {
    let mut out = BTreeMap::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) = item else {
            continue;
        };
        if named.src.is_some() {
            continue;
        }
        for spec in &named.specifiers {
            let ExportSpecifier::Named(named_spec) = spec else {
                continue;
            };
            let ModuleExportName::Ident(orig) = &named_spec.orig else {
                continue;
            };
            out.insert(public_export_name(named_spec), orig.to_id());
        }
    }
    out
}
