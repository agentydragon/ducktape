//! Emit-side import consolidation in the materializer.
//!
//! When a moved module body references multiple bindings that originated
//! as ImportSpecifier-bound locals from a single source in the
//! source chunk, the materializer must emit ONE `import { ... } from
//! "<src>"` statement with all the specifiers — not one statement per
//! binding. The consolidation happens natively in the emitter; there is
//! no post-hoc coalescing pass.
//!
//! Two emit sites are exercised here:
//!
//! - `source_chunk_imports_for_moved_body` (re-imports for moved
//!   bodies that reference source-chunk runtime specifiers).
//! - The `BindingKind::Imported` reexport loop (one
//!   `import { ... } from "<src>"` per re-exported imported binding,
//!   grouped by source when the same module re-exports multiple
//!   bindings from the same import-from).
//!
//! ESM grammar constraint: a Namespace specifier
//! (`import * as ns from "src"`) cannot share an ImportClause with
//! NamedImports. Namespace specifiers therefore stay on their own
//! statement even when other same-source specifiers are present.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;
use swc_ecma_ast::{ImportSpecifier, ModuleDecl, ModuleExportName, ModuleItem};

/// Count separate top-level `import ... from "<src>";` ModuleItems in
/// `source` whose specifier source equals `expected_src`. Side-effect
/// imports (no specifiers) count separately and are also returned.
fn count_imports_from(source: &str, expected_src: &str) -> (usize, usize) {
    let module = parse_module(source);
    let mut named_or_default = 0usize;
    let mut side_effect_only = 0usize;
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        if import.src.value.to_string_lossy() != expected_src {
            continue;
        }
        if import.specifiers.is_empty() {
            side_effect_only += 1;
        } else {
            named_or_default += 1;
        }
    }
    (named_or_default, side_effect_only)
}

/// Set of (local, imported_or_default) tuples extracted from every
/// `import ... from "<expected_src>";` in `source`. Used to assert the
/// consolidated output preserves every binding from the split inputs.
fn collect_import_bindings(source: &str, expected_src: &str) -> Vec<(String, String)> {
    let module = parse_module(source);
    let mut bindings = Vec::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        if import.src.value.to_string_lossy() != expected_src {
            continue;
        }
        for specifier in &import.specifiers {
            match specifier {
                ImportSpecifier::Named(named) => {
                    let local = named.local.sym.to_string();
                    let imported = match &named.imported {
                        Some(ModuleExportName::Ident(ident)) => ident.sym.to_string(),
                        Some(ModuleExportName::Str(s)) => s.value.to_string_lossy().to_string(),
                        None => local.clone(),
                    };
                    bindings.push((local, imported));
                }
                ImportSpecifier::Default(default) => {
                    bindings.push((default.local.sym.to_string(), "default".to_string()));
                }
                ImportSpecifier::Namespace(namespace) => {
                    bindings.push((namespace.local.sym.to_string(), "*".to_string()));
                }
            }
        }
    }
    bindings.sort();
    bindings
}

#[test]
fn moved_body_reimports_three_same_source_specifiers_in_one_statement() {
    // Source chunk has three named imports from `./vendor.js`
    // (esbuild's per-binding import emit shape, common after vendor
    // chunk-splitting). A moved function body references all three
    // locals, so the materializer must emit re-imports in the
    // destination module — one ImportDecl per binding would mean
    // three separate statements; the emitter consolidates them into
    // ONE `import { a as x, b as y, c as z } from ".././vendor.js"`.
    let mut opts = FixtureOpts::new(
        r#"import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
import { c as z } from "./vendor.js";
function bridge() {
  return x + y + z;
}
console.log(bridge());
export { bridge };
"#,
        vec![(
            "mod_bridge".to_string(),
            json!({
                "members": [{ "name": "bridge", "selector": { "binding": { "name": "bridge" } } }],
            }),
        )],
    );
    opts.extra_files = &[(
        "static/app/vendor.js",
        "export const a = 1;\nexport const b = 2;\nexport const c = 3;\n",
    )];
    let fixture = run_fixture(opts);

    let mod_bridge = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_bridge.js"))
        .expect("read mod_bridge.js");

    let (named, _side_effect) = count_imports_from(&mod_bridge, ".././vendor.js");
    assert_eq!(
        named, 1,
        "expected one consolidated `import {{ ... }} from \".././vendor.js\";` statement, got {named}; mod_bridge was:\n{mod_bridge}",
    );

    let bindings = collect_import_bindings(&mod_bridge, ".././vendor.js");
    assert_eq!(
        bindings,
        vec![
            ("x".to_string(), "a".to_string()),
            ("y".to_string(), "b".to_string()),
            ("z".to_string(), "c".to_string()),
        ],
        "consolidated statement must preserve every original (local, imported) pair; mod_bridge was:\n{mod_bridge}",
    );

    // Behaviour preservation: every binding is still bound; the
    // moved body evaluates against the same vendor exports.
    assert_entry_output(&fixture, "6\n");
}

#[test]
fn moved_body_reimports_default_plus_named_in_one_statement() {
    // Default + named imports from the same source coalesce into a
    // single `import D, { a as x, b as y } from "<src>"` statement
    // (ESM grammar allows mixing a DefaultBinding with NamedImports
    // in one ImportClause; the emitter sorts default before named so
    // the resulting syntax is valid).
    let mut opts = FixtureOpts::new(
        r#"import D from "./vendor.js";
import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
function bridge() {
  return D + x + y;
}
console.log(bridge());
export { bridge };
"#,
        vec![(
            "mod_bridge".to_string(),
            json!({
                "members": [{ "name": "bridge", "selector": { "binding": { "name": "bridge" } } }],
            }),
        )],
    );
    opts.extra_files = &[(
        "static/app/vendor.js",
        "export default 10;\nexport const a = 1;\nexport const b = 2;\n",
    )];
    let fixture = run_fixture(opts);

    let mod_bridge = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_bridge.js"))
        .expect("read mod_bridge.js");

    let (named, _) = count_imports_from(&mod_bridge, ".././vendor.js");
    assert_eq!(
        named, 1,
        "expected default + named imports to consolidate into one statement; got {named} in:\n{mod_bridge}",
    );
    let bindings = collect_import_bindings(&mod_bridge, ".././vendor.js");
    assert_eq!(
        bindings,
        vec![
            ("D".to_string(), "default".to_string()),
            ("x".to_string(), "a".to_string()),
            ("y".to_string(), "b".to_string()),
        ],
        "consolidated statement must keep the default + every named binding; mod_bridge was:\n{mod_bridge}",
    );

    assert_entry_output(&fixture, "13\n");
}

#[test]
fn moved_body_emits_namespace_specifier_on_its_own_statement() {
    // ESM grammar forbids mixing NameSpaceImport with NamedImports
    // in one ImportClause. When a moved body needs both a namespace
    // re-import AND named re-imports from the same source, the
    // emitter emits two ImportDecls: a namespace-only statement and
    // a named-only statement.
    let mut opts = FixtureOpts::new(
        r#"import * as ns from "./vendor.js";
import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
function bridge() {
  return ns.a + x + y;
}
console.log(bridge());
export { bridge };
"#,
        vec![(
            "mod_bridge".to_string(),
            json!({
                "members": [{ "name": "bridge", "selector": { "binding": { "name": "bridge" } } }],
            }),
        )],
    );
    opts.extra_files = &[(
        "static/app/vendor.js",
        "export const a = 1;\nexport const b = 2;\n",
    )];
    let fixture = run_fixture(opts);

    let mod_bridge = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_bridge.js"))
        .expect("read mod_bridge.js");

    let (named, _) = count_imports_from(&mod_bridge, ".././vendor.js");
    assert_eq!(
        named, 2,
        "expected one namespace-only ImportDecl plus one consolidated named ImportDecl; got {named} in:\n{mod_bridge}",
    );

    let bindings = collect_import_bindings(&mod_bridge, ".././vendor.js");
    assert_eq!(
        bindings,
        vec![
            ("ns".to_string(), "*".to_string()),
            ("x".to_string(), "a".to_string()),
            ("y".to_string(), "b".to_string()),
        ],
        "every original (local, imported) pair must survive across the two statements; mod_bridge was:\n{mod_bridge}",
    );

    assert_entry_output(&fixture, "4\n");
}

#[test]
fn imported_binding_reexports_from_same_source_consolidate_in_one_statement() {
    // The BindingKind::Imported reexport loop emits one
    // `import { <imported> as <local> } from "<src>"` per re-exported
    // binding. When a single logical module re-exports multiple
    // bindings from the same source, the emitter groups them by
    // source so all specifiers land in one consolidated ImportDecl.
    let mut opts = FixtureOpts::new(
        r#"import { x as a, y as b } from "./vendor.js";
console.log(a, b);
export { a, b };
"#,
        vec![(
            "mod_re".to_string(),
            json!({
                "members": [
                    {
                        "name": "Re_a",
                        "selector": { "binding": { "name": "a", "kind": "import_specifier" } },
                    },
                    {
                        "name": "Re_b",
                        "selector": { "binding": { "name": "b", "kind": "import_specifier" } },
                    },
                ],
            }),
        )],
    );
    opts.extra_files = &[(
        "static/app/vendor.js",
        "export const x = 1;\nexport const y = 2;\n",
    )];
    let fixture = run_fixture(opts);

    let mod_re = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_re.js"))
        .expect("read mod_re.js");

    let (named, _) = count_imports_from(&mod_re, "../../vendor.js");
    assert_eq!(
        named, 1,
        "expected one consolidated reexport ImportDecl for both bindings; got {named} in:\n{mod_re}",
    );
    let bindings = collect_import_bindings(&mod_re, "../../vendor.js");
    assert_eq!(
        bindings,
        vec![
            ("a".to_string(), "x".to_string()),
            ("b".to_string(), "y".to_string()),
        ],
        "consolidated reexport statement must carry both bindings; mod_re was:\n{mod_re}",
    );
}
