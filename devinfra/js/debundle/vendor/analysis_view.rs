//! The provider body that program analysis may trust for a partially swapped
//! chunk. The emitted program runs replacement code for every swapped symbol,
//! including residual calls the bundled self-rewrite retargets, so a fact
//! derived from the original implementation does not describe it.

use std::collections::BTreeMap;

use binding_targets::{binding_names, declaration_ids};
use swc_common::{DUMMY_SP, Span, Spanned, SyntaxContext};
use swc_ecma_ast::*;

use spec::PartialSwapSymbol;

use super::export_surface::collect_local_idents_by_export_name;
use super::{collect_top_level_binding_ids, module_used_idents, unique_synthetic_ident};

/// Clone `module` with each swapped symbol's implementation replaced by an
/// import from its package, which analysis cannot resolve and so treats as
/// unknown code. A declaration item becomes that import in place; a swapped
/// declarator of a multi-declarator or destructuring `var` instead takes its
/// value from a fresh import appended to the body, making every binding of
/// that declarator opaque. Item positions and post-split statement ordinals
/// match `module`, so author inputs and resolved member sites still address
/// the same statements.
pub fn module_with_opaque_swapped_symbols(
    module: &Module,
    symbols: &BTreeMap<String, PartialSwapSymbol>,
) -> Module {
    let exported_locals = collect_local_idents_by_export_name(module);
    let top_level_bindings = collect_top_level_binding_ids(module);
    let swapped: BTreeMap<Id, &str> = symbols
        .iter()
        .filter_map(|(export, symbol)| {
            let local = match &symbol.local {
                Some(local) => top_level_bindings.get(local),
                // `export function f` / `export const f` binds its export name.
                None => exported_locals
                    .get(export)
                    .or_else(|| top_level_bindings.get(export)),
            }?;
            Some((local.clone(), symbol.package.as_str()))
        })
        .collect();
    let mut used_idents = module_used_idents(module);
    let mut appended_imports = Vec::new();
    let body = module
        .body
        .iter()
        .map(|item| {
            let decl = match item {
                ModuleItem::Stmt(Stmt::Decl(decl)) => decl,
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => &export_decl.decl,
                _ => return item.clone(),
            };
            let Decl::Var(var) = decl else {
                return match declaration_ids(decl)
                    .into_iter()
                    .find(|id| swapped.contains_key(id))
                {
                    Some(id) => {
                        let package = swapped[&id];
                        opaque_import(item.span(), id, package)
                    }
                    None => item.clone(),
                };
            };
            if let [
                VarDeclarator {
                    name: Pat::Ident(binding),
                    ..
                },
            ] = var.decls.as_slice()
                && let Some(package) = swapped.get(&binding.to_id())
            {
                return opaque_import(item.span(), binding.to_id(), package);
            }
            let mut var = var.clone();
            for declarator in &mut var.decls {
                let Some(package) =
                    binding_names(&declarator.name).find_map(|id| swapped.get(&id).copied())
                else {
                    continue;
                };
                let local = unique_synthetic_ident("__debundle_swapped", &mut used_idents);
                declarator.init = Some(Box::new(Expr::Ident(Ident::new_no_ctxt(
                    local.clone().into(),
                    DUMMY_SP,
                ))));
                appended_imports.push(opaque_import(
                    DUMMY_SP,
                    (local.into(), SyntaxContext::empty()),
                    package,
                ));
            }
            match item {
                ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export_decl)) => {
                    ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
                        span: export_decl.span,
                        decl: Decl::Var(var),
                    }))
                }
                _ => ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))),
            }
        })
        .collect::<Vec<_>>();
    Module {
        span: module.span,
        body: body.into_iter().chain(appended_imports).collect(),
        shebang: module.shebang.clone(),
    }
}

fn opaque_import(span: Span, (sym, ctxt): Id, package: &str) -> ModuleItem {
    ModuleItem::ModuleDecl(ModuleDecl::Import(ImportDecl {
        span,
        specifiers: vec![ImportSpecifier::Named(ImportNamedSpecifier {
            span: DUMMY_SP,
            local: Ident::new(sym, DUMMY_SP, ctxt),
            imported: None,
            is_type_only: false,
        })],
        src: Box::new(Str::from(package)),
        type_only: false,
        with: None,
        phase: ImportPhase::Evaluation,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use js_ast::{emit_js_module, parse_js_module};
    use spec::PartialSwapKind;

    #[test]
    fn swapped_bindings_become_imports_at_the_same_statement_ordinals() {
        js_ast::with_swc_globals(|| {
            let mut parsed = parse_js_module(
                "vendor.js",
                "const kept = 1, sw = () => kept;\n\
                 export const { sibling, sw2 } = globalThis.source;\n\
                 function sw3() { return kept; }\n\
                 export function sw4() {}\n\
                 export { sw as a, sw2 as b, sw3 as c };\n",
            )
            .unwrap();
            let symbol = |local: Option<&str>| PartialSwapSymbol {
                package: "pkg".into(),
                kind: PartialSwapKind::Named,
                upstream_export: Some("x".into()),
                local: local.map(str::to_owned),
            };
            let symbols = BTreeMap::from([
                ("a".to_string(), symbol(None)),
                ("b".to_string(), symbol(None)),
                ("helper".to_string(), symbol(Some("sw3"))),
                ("sw4".to_string(), symbol(None)),
            ]);
            // Each original item keeps its position and declarator count;
            // fresh imports only follow the last one.
            parsed.module = module_with_opaque_swapped_symbols(&parsed.module, &symbols);
            assert_eq!(
                emit_js_module(&parsed, &[]).unwrap(),
                "const kept = 1, sw = __debundle_swapped;\n\
                 export const { sibling, sw2 } = __debundle_swapped_2;\n\
                 import { sw3 } from \"pkg\";\n\
                 import { sw4 } from \"pkg\";\n\
                 export { sw as a, sw2 as b, sw3 as c };\n\
                 import { __debundle_swapped } from \"pkg\";\n\
                 import { __debundle_swapped_2 } from \"pkg\";\n",
            );
        });
    }
}
