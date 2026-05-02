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
use std::fs;

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

/// Parse `source` and assert that every named import specifier binds a
/// distinct local symbol. Mirrors the duplicate-declaration check Node
/// would perform at module-load time.
fn assert_unique_import_locals(source: &str) {
    use std::collections::BTreeSet;
    use swc_ecma_ast::{ImportSpecifier, ModuleDecl, ModuleItem};

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
    use swc_ecma_ast::{ExportSpecifier, ModuleDecl, ModuleExportName, ModuleItem};

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

fn parse_module(source: &str) -> swc_ecma_ast::Module {
    use swc_common::FileName;
    use swc_common::sync::Lrc;
    use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};

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
