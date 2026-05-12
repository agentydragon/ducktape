//! Emit-side import coalescing.
//!
//! When the input chunk has multiple separate `import { x } from "<src>"`
//! statements with the same source (esbuild's per-binding import emit
//! shape, common after vendor specifier rewriting), the debundler must
//! coalesce them into one `import { ... } from "<src>"` in the emitted
//! output.

use debundle_e2e_support::*;
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
/// coalesced output preserves every binding from the split inputs.
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
fn same_source_named_imports_coalesce_into_one_statement() {
    // Mirrors the gaffer-private bug: the input chunk has three separate
    // `import { ... } from "./vendor.js"` lines (esbuild emits one per
    // binding when chunk-splitting interacts with vendor swaps). After
    // emission, the entry should carry exactly ONE `import { ... } from
    // "./vendor.js"` line with all three bindings consolidated.
    let mut opts = FixtureOpts::new(
        r#"import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
import { c as z } from "./vendor.js";
console.log(x, y, z);
"#,
        vec![],
    );
    opts.extra_files = &[(
        "static/app/vendor.js",
        "export const a = 1;\nexport const b = 2;\nexport const c = 3;\n",
    )];
    opts.include_residual = false;
    let fixture = run_fixture(opts);

    let entry =
        fs::read_to_string(fixture.out_root.join("static/app/entry.js")).expect("read entry.js");

    let (named, _side_effect) = count_imports_from(&entry, "./vendor.js");
    assert_eq!(
        named, 1,
        "expected one consolidated `import {{ ... }} from \"./vendor.js\";` statement, got {named}; entry was:\n{entry}",
    );

    let bindings = collect_import_bindings(&entry, "./vendor.js");
    assert_eq!(
        bindings,
        vec![
            ("x".to_string(), "a".to_string()),
            ("y".to_string(), "b".to_string()),
            ("z".to_string(), "c".to_string()),
        ],
        "coalesced statement must preserve every original (local, imported) pair; entry was:\n{entry}",
    );

    // Behaviour preservation: every binding is still bound; emit can run.
    assert_entry_output(&fixture, "1 2 3\n");
}

#[test]
fn side_effect_only_imports_are_not_merged_with_named_imports() {
    // `import "./vendor.js";` is semantically distinct from a named
    // import — it forces evaluation without binding anything. Coalescing
    // must NOT fold a side-effect-only import into a named-import
    // statement from the same source (or vice versa).
    let mut opts = FixtureOpts::new(
        r#"import "./vendor.js";
import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
console.log(x, y);
"#,
        vec![],
    );
    opts.extra_files = &[(
        "static/app/vendor.js",
        "console.log(\"side-effect\");\nexport const a = 1;\nexport const b = 2;\n",
    )];
    opts.include_residual = false;
    let fixture = run_fixture(opts);

    let entry =
        fs::read_to_string(fixture.out_root.join("static/app/entry.js")).expect("read entry.js");
    let (named, side_effect_only) = count_imports_from(&entry, "./vendor.js");
    assert_eq!(
        named, 1,
        "expected one consolidated named-import statement; got {named} in:\n{entry}",
    );
    assert_eq!(
        side_effect_only, 1,
        "expected the side-effect-only `import \"./vendor.js\";` line to survive intact; got {side_effect_only} in:\n{entry}",
    );

    assert_entry_output(&fixture, "side-effect\n1 2\n");
}

#[test]
fn default_and_named_imports_from_same_source_coalesce() {
    // `import D from "src"` and `import { x } from "src"` can be
    // combined into a single `import D, { x } from "src"` statement.
    let mut opts = FixtureOpts::new(
        r#"import D from "./vendor.js";
import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
console.log(D, x, y);
"#,
        vec![],
    );
    opts.extra_files = &[(
        "static/app/vendor.js",
        "export default \"D\";\nexport const a = 1;\nexport const b = 2;\n",
    )];
    opts.include_residual = false;
    let fixture = run_fixture(opts);

    let entry =
        fs::read_to_string(fixture.out_root.join("static/app/entry.js")).expect("read entry.js");
    let (named, _) = count_imports_from(&entry, "./vendor.js");
    assert_eq!(
        named, 1,
        "expected default + named imports to coalesce into one statement; got {named} in:\n{entry}",
    );
    let bindings = collect_import_bindings(&entry, "./vendor.js");
    assert_eq!(
        bindings,
        vec![
            ("D".to_string(), "default".to_string()),
            ("x".to_string(), "a".to_string()),
            ("y".to_string(), "b".to_string()),
        ],
        "coalesced statement must keep the default + every named binding; entry was:\n{entry}",
    );

    assert_entry_output(&fixture, "D 1 2\n");
}
