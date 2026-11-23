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
use std::fs;
use swc_ecma_ast::{ImportSpecifier, ModuleDecl, ModuleExportName, ModuleItem};

/// Count separate top-level `import ... from "<src>";` ModuleItems in
/// `source` whose specifier source equals `expected_src`. Three buckets
/// returned: `(with_named_or_default, namespace_only, side_effect_only)`.
/// Namespace imports get their own bucket because — per ESM grammar —
/// they cannot share an ImportClause with NamedImports; the emitter
/// emits them as their own statements and tests assert on them
/// separately.
fn count_imports_from(source: &str, expected_src: &str) -> (usize, usize, usize) {
    let module = parse_module(source);
    let mut with_named_or_default = 0usize;
    let mut namespace_only = 0usize;
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
        } else if import
            .specifiers
            .iter()
            .all(|s| matches!(s, ImportSpecifier::Namespace(_)))
        {
            namespace_only += 1;
        } else {
            with_named_or_default += 1;
        }
    }
    (with_named_or_default, namespace_only, side_effect_only)
}

/// Vec of (local, imported_or_default) tuples extracted from every
/// `import ... from "<expected_src>";` in `source`, in **first-seen
/// traversal order** (no sort). Callers asserting first-seen specifier
/// order compare directly against an ordered expected vec.
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
    bindings
}

/// Emit `source` with `members` moved into `module`, beside a `vendor.js` holding
/// `vendor`. The module must import from it in `named` statements with named or
/// default specifiers and `namespace` namespace-only statements, carrying exactly
/// `bindings` as `(local, imported)` pairs in first-seen order. The entry must
/// still print `stdout`.
fn assert_consolidated_vendor_imports(
    source: &str,
    vendor: &str,
    module: &str,
    members: &[Member],
    (named, namespace): (usize, usize),
    bindings: &[(&str, &str)],
    stdout: &str,
) {
    let extra_files = [("static/app/vendor.js", vendor)];
    let mut opts = FixtureOpts::new(source, vec![logical_module(module, members)]);
    opts.extra_files = &extra_files;
    let fixture = run_fixture(opts);

    let path = fixture
        .out_root
        .join(format!("static/app/modules/{module}.js"));
    let emitted = fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));

    let (actual_named, actual_namespace, _side_effect) =
        count_imports_from(&emitted, "../vendor.js");
    assert_eq!(
        (actual_named, actual_namespace),
        (named, namespace),
        "expected {named} named/default and {namespace} namespace-only `import ... from \"../vendor.js\";` statements; {module} was:\n{emitted}",
    );

    let actual_bindings = collect_import_bindings(&emitted, "../vendor.js");
    let actual_bindings: Vec<_> = actual_bindings
        .iter()
        .map(|(local, imported)| (local.as_str(), imported.as_str()))
        .collect();
    assert_eq!(
        actual_bindings, bindings,
        "every original (local, imported) pair must survive consolidation; {module} was:\n{emitted}",
    );

    assert_entry_output(&fixture, stdout);
}

#[test]
fn moved_body_reimports_three_same_source_specifiers_in_one_statement() {
    // esbuild's one-import-per-binding shape: three statements from one source become one re-import.
    assert_consolidated_vendor_imports(
        r#"import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
import { c as z } from "./vendor.js";
function bridge() {
  return x + y + z;
}
console.log(bridge());
export { bridge };
"#,
        "export const a = 1;\nexport const b = 2;\nexport const c = 3;\n",
        "mod_bridge",
        &[Member::new("bridge")],
        (1, 0),
        &[("x", "a"), ("y", "b"), ("z", "c")],
        "6\n",
    );
}

#[test]
fn moved_body_reimports_default_plus_named_in_one_statement() {
    // A default binding and named bindings share one clause, default first.
    assert_consolidated_vendor_imports(
        r#"import D from "./vendor.js";
import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
function bridge() {
  return D + x + y;
}
console.log(bridge());
export { bridge };
"#,
        "export default 10;\nexport const a = 1;\nexport const b = 2;\n",
        "mod_bridge",
        &[Member::new("bridge")],
        (1, 0),
        &[("D", "default"), ("x", "a"), ("y", "b")],
        "13\n",
    );
}

#[test]
fn moved_body_emits_namespace_specifier_on_its_own_statement() {
    // The namespace import stays its own statement beside one named statement.
    assert_consolidated_vendor_imports(
        r#"import * as ns from "./vendor.js";
import { a as x } from "./vendor.js";
import { b as y } from "./vendor.js";
function bridge() {
  return ns.a + x + y;
}
console.log(bridge());
export { bridge };
"#,
        "export const a = 1;\nexport const b = 2;\n",
        "mod_bridge",
        &[Member::new("bridge")],
        (1, 1),
        &[("ns", "*"), ("x", "a"), ("y", "b")],
        "4\n",
    );
}

#[test]
fn imported_binding_reexports_from_same_source_consolidate_in_one_statement() {
    // The `BindingKind::Imported` reexport loop groups bindings from one source into one statement.
    assert_consolidated_vendor_imports(
        r#"import { x as a, y as b } from "./vendor.js";
console.log(a, b);
export { a, b };
"#,
        "export const x = 1;\nexport const y = 2;\n",
        "mod_re",
        &[
            Member::renamed_with_kind("Re_a", "a", Some("import_specifier")),
            Member::renamed_with_kind("Re_b", "b", Some("import_specifier")),
        ],
        (1, 0),
        &[("a", "x"), ("b", "y")],
        "1 2\n",
    );
}
