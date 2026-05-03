//! Disambiguation of consumer-side import locals that would collide with
//! another import already bound under the same scrambled name.
//!
//! Two `define_logical_module` requests that both list the same scrambled
//! `binding` produce two `import { ... as <local> } from ...` lines into
//! the chunk-entry body. Without disambiguation the second emit shadows
//! the first and the file fails to parse. This regression locks down the
//! fresh-suffix behavior introduced for that case.

use debundle_e2e_support::*;
use serde_json::json;
use std::collections::BTreeSet;
use std::fs;
use swc_common::FileName;
use swc_common::sync::Lrc;
use swc_ecma_ast::{
    BindingIdent, BlockStmtOrExpr, Decl, ExportSpecifier, Expr, FnDecl, Function, ImportSpecifier,
    Module, ModuleDecl, ModuleExportName, ModuleItem, ObjectPatProp, Pat, Stmt, VarDeclKind,
    VarDeclarator,
};
use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};

#[test]
fn renames_consumer_import_local_when_a_second_plan_claims_the_same_scrambled_binding() {
    let opts = FixtureOpts::new(
        r#"const aH = 42;
console.log(aH);
export { aH };
"#,
        vec![
            json!({
                "id": "logical__mod_a",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_a" },
                "members": [{
                    "id": "member__readableA",
                    "name": "readableA",
                    "selector": { "binding": { "name": "aH" } },
                }],
            }),
            json!({
                "id": "logical__mod_b",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_b" },
                "members": [{
                    "id": "member__plainAh",
                    "name": "plainAh",
                    "selector": { "binding": { "name": "aH" } },
                }],
            }),
        ],
    );
    let fixture = run_logical_modules_e2e_fixture(opts);

    let entry = fs::read_to_string(&fixture.entry_path).expect("read entry.js");

    // The first emit keeps the scrambled local `aH`; the second emit must
    // mint a fresh `aH$1` so the two `import` declarations don't collide.
    assert!(
        entry.contains(r#"import { readableA as aH } from "./modules/mod_a.js""#),
        "expected unrenamed-local mod_a import; entry was:\n{entry}",
    );
    assert!(
        entry.contains(r#"import { plainAh as aH$1 } from "./modules/mod_b.js""#),
        "expected fresh-suffix mod_b import; entry was:\n{entry}",
    );

    // Body refs that resolve to the moved decl (now imported under the
    // fresh local) must follow the rename. The decl moved to mod_b
    // because the second plan was last to claim the binding, so refs
    // point at `aH$1`.
    assert!(
        entry.contains("console.log(aH$1)"),
        "expected console.log to be rewritten to aH$1; entry was:\n{entry}",
    );

    // Public re-export name `aH` must survive even though the local is
    // now `aH$1`: the re-export becomes `export { aH$1 as aH }` rather
    // than `export { aH$1 }` (which would silently change the public
    // name downstream consumers see). A substring check would also accept
    // the broken `aH$1 as aH$1` shape (it contains `aH$1 as aH`), so
    // this asserts on the parsed specifier instead.
    assert_export_named_specifier(&entry, "aH$1", Some("aH"));

    // SWC parses the result without a duplicate-decl error: every named
    // import specifier in the file binds a distinct local.
    assert_unique_import_locals(&entry);
}

#[test]
fn keeps_unrelated_consumer_import_locals_unchanged() {
    // Single plan, no collision: the emitted import keeps the scrambled
    // local verbatim and body refs are not rewritten. Sanity check that
    // the disambiguation pass is no-op when there's nothing to fix.
    let opts = FixtureOpts::new(
        r#"const aH = 7;
console.log(aH);
export { aH };
"#,
        vec![json!({
            "id": "logical__mod_a",
            "operation": "define_logical_module",
            "selector": { "chunkId": "static/app" },
            "target": { "path": "mod_a" },
            "members": [{
                "id": "member__readableA",
                "name": "readableA",
                "selector": { "binding": { "name": "aH" } },
            }],
        })],
    );
    let fixture = run_logical_modules_e2e_fixture(opts);
    let entry = fs::read_to_string(&fixture.entry_path).expect("read entry.js");

    assert!(
        entry.contains(r#"import { readableA as aH } from "./modules/mod_a.js""#),
        "expected unrenamed mod_a import; entry was:\n{entry}",
    );
    assert!(
        !entry.contains("aH$1"),
        "no fresh-suffix should appear when there is no collision; entry was:\n{entry}",
    );
    assert_entry_output(&fixture, "7\n");
}

#[test]
fn does_not_collapse_two_distinct_locals_onto_the_same_readable_name() {
    // Two readable-rename rules in one logical module pick the same
    // target name from different inputs. The destructured-pattern rule
    // sees `{ readable: o }` and queues `o -> readable`; the
    // return-object-alias rule sees `return { readable: u }` and queues
    // `u -> readable`. Both rewrites land in the module-wide rename map.
    // The naive renamer then rewrites every `o` and every `u` to
    // `readable`. Any function that happens to bind both `o` and `u`
    // (param + local) ends up with two `readable` decls in the same
    // scope and Node refuses to load it with `Identifier 'readable'
    // has already been declared`.
    let opts = FixtureOpts::new(
        r#"function destructure({ readable: o }) {
  return o;
}
function alias() {
  const u = "y";
  return { readable: u };
}
function consumer({ readable: o }) {
  const u = String(o) + "x";
  return u;
}
console.log(destructure({ readable: 0 }), alias().readable, consumer({ readable: "v" }));
export { destructure, alias, consumer };
"#,
        vec![logical_module(
            "mod_x",
            &[
                Member::new("destructure"),
                Member::new("alias"),
                Member::new("consumer"),
            ],
        )],
    );
    let fixture = run_logical_modules_e2e_fixture(opts);
    let module_path = fixture.out_root.join("static/app/modules/mod_x.js");
    let module_src = fs::read_to_string(&module_path).expect("read modules/mod_x.js");

    // Module body must parse — colliding `readable` decls in the same
    // scope would surface as a duplicate-decl SyntaxError.
    parse_module(&module_src);
    // Each function-body scope binds `readable` at most once. This
    // mirrors the lexical-binding constraint Node enforces at module
    // load time.
    assert_unique_lexical_decls_per_scope(&module_src, "readable");

    // Module behaviour preserved: `consumer` still produces "vx".
    assert_entry_output(&fixture, "0 y vx\n");
}

/// Parse `source` and assert that every named import specifier binds a
/// distinct local symbol. Mirrors the duplicate-declaration check Node
/// would perform at module-load time.
fn assert_unique_import_locals(source: &str) {
    let module = parse_module(source);
    let mut seen = BTreeSet::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        for specifier in &import.specifiers {
            let local = match specifier {
                ImportSpecifier::Named(named) => named.local.sym.to_string(),
                ImportSpecifier::Default(default) => default.local.sym.to_string(),
                ImportSpecifier::Namespace(namespace) => namespace.local.sym.to_string(),
            };
            assert!(
                seen.insert(local.clone()),
                "duplicate import local `{local}` in:\n{source}",
            );
        }
    }
}

/// Parse `source` and assert exactly one `export { ... }` specifier has
/// `orig.sym == expected_orig`, with its `exported` either absent (when
/// `expected_exported_as` is `None`) or `Ident { sym: expected_exported_as }`.
/// Walks the parsed specifier tree so a corrupted `export { aH$1 as aH$1 }`
/// fails — a substring check on `aH$1 as aH` would accept both shapes.
fn assert_export_named_specifier(
    source: &str,
    expected_orig: &str,
    expected_exported_as: Option<&str>,
) {
    let module = parse_module(source);
    let matched: Vec<_> = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => Some(named),
            _ => None,
        })
        .flat_map(|named| named.specifiers.iter())
        .filter_map(|spec| match spec {
            ExportSpecifier::Named(named) => Some(named),
            _ => None,
        })
        .filter(|spec| {
            let ModuleExportName::Ident(ident) = &spec.orig else {
                return false;
            };
            ident.sym.as_ref() == expected_orig
        })
        .collect();
    assert_eq!(
        matched.len(),
        1,
        "expected exactly one `export {{ {expected_orig} ... }}` specifier; got {} in:\n{source}",
        matched.len(),
    );
    let actual = match &matched[0].exported {
        Some(ModuleExportName::Ident(ident)) => Some(ident.sym.to_string()),
        Some(ModuleExportName::Str(_)) => panic!("unexpected string export in:\n{source}"),
        None => None,
    };
    assert_eq!(
        actual.as_deref(),
        expected_exported_as,
        "export {{ {expected_orig} ... }} `as` clause mismatch in:\n{source}",
    );
}

/// Assert that no function-body scope in `source` declares `target_name`
/// more than once (counting destructured params and `let`/`const` decls;
/// `var` is excluded because it allows redeclaration in the same scope).
/// Mirrors Node's lexical-binding duplicate check.
fn assert_unique_lexical_decls_per_scope(source: &str, target_name: &str) {
    fn pat_binds(pat: &Pat, target: &str) -> bool {
        match pat {
            Pat::Ident(BindingIdent { id, .. }) => id.sym.as_ref() == target,
            Pat::Object(object) => object.props.iter().any(|prop| match prop {
                ObjectPatProp::KeyValue(kv) => pat_binds(&kv.value, target),
                ObjectPatProp::Assign(assign) => assign.key.id.sym.as_ref() == target,
                ObjectPatProp::Rest(rest) => pat_binds(&rest.arg, target),
            }),
            Pat::Array(array) => array
                .elems
                .iter()
                .flatten()
                .any(|elem| pat_binds(elem, target)),
            Pat::Assign(assign) => pat_binds(&assign.left, target),
            Pat::Rest(rest) => pat_binds(&rest.arg, target),
            _ => false,
        }
    }

    fn check_function(function: &Function, target: &str, source: &str) {
        let Some(body) = &function.body else {
            return;
        };
        let mut count = 0;
        for param in &function.params {
            if pat_binds(&param.pat, target) {
                count += 1;
            }
        }
        for stmt in &body.stmts {
            // `var` allows redeclaration in the same scope (and `function f(a){var a;}`
            // is legal); only `let`/`const`/`class`/`function` are subject to the
            // "Identifier 'X' has already been declared" lexical check.
            if let Stmt::Decl(Decl::Var(var)) = stmt
                && matches!(var.kind, VarDeclKind::Let | VarDeclKind::Const)
            {
                for declarator in &var.decls {
                    if pat_binds(&declarator.name, target) {
                        count += 1;
                    }
                }
            }
        }
        assert!(
            count <= 1,
            "scope binds `{target}` {count} times in:\n{source}",
        );
        for stmt in &body.stmts {
            descend_stmt(stmt, target, source);
        }
    }

    fn descend_stmt(stmt: &Stmt, target: &str, source: &str) {
        match stmt {
            Stmt::Decl(Decl::Fn(FnDecl { function, .. })) => {
                check_function(function, target, source)
            }
            Stmt::Decl(Decl::Var(var)) => {
                for VarDeclarator { init, .. } in &var.decls {
                    if let Some(init) = init {
                        descend_expr(init, target, source);
                    }
                }
            }
            Stmt::Block(block) => {
                for stmt in &block.stmts {
                    descend_stmt(stmt, target, source);
                }
            }
            _ => {}
        }
    }

    fn descend_expr(expr: &Expr, target: &str, source: &str) {
        match expr {
            Expr::Fn(fn_expr) => check_function(&fn_expr.function, target, source),
            Expr::Arrow(arrow) => {
                let mut count = 0;
                for param in &arrow.params {
                    if pat_binds(param, target) {
                        count += 1;
                    }
                }
                if let BlockStmtOrExpr::BlockStmt(block) = &*arrow.body {
                    for stmt in &block.stmts {
                        if let Stmt::Decl(Decl::Var(var)) = stmt
                            && matches!(var.kind, VarDeclKind::Let | VarDeclKind::Const)
                        {
                            for declarator in &var.decls {
                                if pat_binds(&declarator.name, target) {
                                    count += 1;
                                }
                            }
                        }
                    }
                }
                assert!(
                    count <= 1,
                    "arrow scope binds `{target}` {count} times in:\n{source}",
                );
            }
            _ => {}
        }
    }

    let module = parse_module(source);
    for item in &module.body {
        if let ModuleItem::Stmt(stmt) = item {
            descend_stmt(stmt, target_name, source);
        }
    }
}

fn parse_module(source: &str) -> Module {
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let fm = cm.new_source_file(
        FileName::Custom("entry.js".into()).into(),
        source.to_string(),
    );
    let lexer = Lexer::new(
        Syntax::Typescript(TsSyntax {
            tsx: true,
            decorators: true,
            no_early_errors: true,
            ..Default::default()
        }),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    Parser::new_from(lexer)
        .parse_module()
        .unwrap_or_else(|err| panic!("entry must parse, got {err:?}; source:\n{source}"))
}
