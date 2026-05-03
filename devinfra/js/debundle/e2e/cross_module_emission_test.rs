//! Emit-side imports, exports, and local-name disambiguation in
//! modules produced by `materialize_logical_modules`.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;

// --- Cross-module dependency wiring --------------------------------------

#[test]
fn extracted_module_imports_unowned_helper_from_residual() {
    // Spec claims only `b`. Its helper `a` is unclaimed, so `a`
    // stays in residual; mod_x imports it (no implicit closure
    // pull).
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a = x => "h:" + x;
const b = x => a(x);
console.log(b("y"));
export { b };
"#,
        vec![logical_module("x", &[Member::new("b")])],
    ));
    assert_module_exports(&fixture.out_root, "static/app/modules/x.js", &["b"], &[]);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/x.js",
        &["import { a }"],
        &["a = "],
    );
    assert_entry_output(&fixture, "h:y\n");
}

#[test]
fn explicit_modules_share_a_residual_helper_via_imports() {
    // Helper `r` is unclaimed; both `inner` (owns `s`) and
    // `outer` (owns `t`) import it from residual.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const q = "a";
function r() { return q; }
function s() { return "b" + r(); }
function t() { return s() + r(); }
console.log(t());
export { t, s };
"#,
        vec![
            logical_module("inner", &[Member::new("s")]),
            logical_module("outer", &[Member::new("t")]),
        ],
    ));
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/inner.js",
        &["s"],
        &["r", "t"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/outer.js",
        &["t"],
        &["r", "s"],
    );
    assert_entry_output(&fixture, "baa\n");
}

#[test]
fn imports_renamed_dependencies_across_split_declarators() {
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const q = o => o.a, r = o => o.b;
const s = o => q(o) ?? r(o);
console.log(s({ a: null, b: "c" }));
export { s };
"#,
        vec![
            logical_module(
                "provider",
                &[Member::renamed("u", "q"), Member::renamed("v", "r")],
            ),
            logical_module("consumer", &[Member::renamed("w", "s")]),
        ],
    ));
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/provider.js",
        &["u", "v"],
        &[],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/consumer.js",
        &["w"],
        &["u"],
    );
    assert_generated_module_after_entry_script(
        &fixture.out_root,
        r#"const { w } = await import("./static/app/modules/consumer.js");
console.log(w({ a: null, b: "d" }));
"#,
        "d\n",
    );
    assert_entry_output(&fixture, "c\n");
}

// --- Consumer-side import-local disambiguation ---------------------------

#[test]
fn renames_consumer_import_local_when_a_second_plan_claims_the_same_scrambled_binding() {
    // Two plans claim the same scrambled `binding`. Without
    // disambiguation the second emit's import shadows the first
    // and the file fails to parse. The materializer mints a
    // `$N` suffix on the second.
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

    assert!(
        entry.contains(r#"import { readableA as aH } from "./modules/mod_a.js""#),
        "expected unrenamed-local mod_a import; entry was:\n{entry}",
    );
    assert!(
        entry.contains(r#"import { plainAh as aH$1 } from "./modules/mod_b.js""#),
        "expected fresh-suffix mod_b import; entry was:\n{entry}",
    );
    // Body refs to the second-claimed binding follow the rename.
    assert!(
        entry.contains("console.log(aH$1)"),
        "expected console.log to be rewritten to aH$1; entry was:\n{entry}",
    );
    // Public re-export name survives: `export { aH$1 as aH }`,
    // not `export { aH$1 }`. AST walk so a corrupt
    // `aH$1 as aH$1` doesn't slip past a substring match.
    assert_export_named_specifier(&entry, "aH$1", Some("aH"));
    assert_unique_import_locals(&entry);
}

#[test]
fn keeps_unrelated_consumer_import_locals_unchanged() {
    // Single-plan sanity: disambiguation is a no-op with no
    // collision.
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
    // Two heuristic readable-rename rules pick the same target
    // for different inputs (`o → readable` from destructuring,
    // `u → readable` from a return-object alias). A naive
    // rewriter renames both, so any scope binding both `o` and
    // `u` ends up with duplicate `readable` decls and Node
    // refuses to load it.
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

    parse_module(&module_src);
    assert_unique_lexical_decls_per_scope(&module_src, "readable");
    assert_entry_output(&fixture, "0 y vx\n");
}

// --- ImportSpecifier-bound members --------------------------------------

#[test]
fn import_specifier_member_emits_reimport_in_destination() {
    // Chunk imports `x` as local `a`. Spec claims that import
    // as a mod_x member renamed `Readable`. Destination must
    // emit a re-import + export so Node can resolve the export.
    let mut opts = FixtureOpts::new(
        r#"import { x as a } from "./vendor.js";
console.log(a);
export { a };
"#,
        vec![json!({
            "id": "logical__mod_x",
            "operation": "define_logical_module",
            "selector": { "chunkId": "static/app" },
            "target": { "path": "mod_x" },
            "members": [{
                "id": "m_a",
                "name": "Readable",
                "selector": {
                    "binding": { "name": "a", "kind": "ImportSpecifier" },
                },
            }],
        })],
    );
    opts.extra_files = &[(
        "static/vendor.js",
        "export const x = 42;\nexport default x;\n",
    )];
    let fixture = run_logical_modules_e2e_fixture(opts);

    let mod_x = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_x.js"))
        .expect("read mod_x.js");

    assert!(
        mod_x.contains("import {") && mod_x.contains("Readable") && mod_x.contains("vendor"),
        "mod_x.js must re-import the vendor binding under the readable name; got:\n{mod_x}",
    );
    assert!(
        mod_x.contains("export {") && mod_x.contains("Readable"),
        "mod_x.js must export Readable; got:\n{mod_x}",
    );
}

// --- Multi-module re-export of a shared imported binding ----------------

#[test]
fn imported_binding_re_exported_under_two_different_names() {
    // Two logical modules each re-export the same imported
    // binding under different public names. They share one
    // `BindingKind::Imported` whose `re_exported_by` map carries
    // each module's chosen public name.
    let mut opts = FixtureOpts::new(
        r#"import { j as a } from "./vendor.js";
console.log(a());
export { a };
"#,
        vec![
            json!({
                "id": "logical__mod_jsx_runtime",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_jsx_runtime" },
                "members": [{
                    "id": "m_jsx_runtime",
                    "name": "jsxRuntime",
                    "selector": { "binding": { "name": "a", "kind": "ImportSpecifier" } },
                }],
            }),
            json!({
                "id": "logical__mod_dunder_jsx",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_dunder_jsx" },
                "members": [{
                    "id": "m_dunder_jsx",
                    "name": "__jsx",
                    "selector": { "binding": { "name": "a", "kind": "ImportSpecifier" } },
                }],
            }),
        ],
    );
    opts.extra_files = &[("static/vendor.js", "export const j = () => 42;\n")];
    let fixture = run_logical_modules_e2e_fixture(opts);

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_jsx_runtime.js",
        &["jsxRuntime"],
        &["__jsx"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/mod_dunder_jsx.js",
        &["__jsx"],
        &["jsxRuntime"],
    );
}

// --- Source-chunk re-imports for moved bodies ---------------------------

#[test]
fn moved_body_re_imports_runtime_specifier_local() {
    // A moved top-level decl whose body references a
    // source-chunk import-specifier local needs a re-import in
    // the destination module — otherwise the moved code has a
    // free variable and Node throws `ReferenceError`.
    let mut opts = FixtureOpts::new(
        r#"import { mu as gge } from "./vendor.js";
function bridge() {
  return gge.decode;
}
console.log(bridge()());
export { bridge };
"#,
        vec![json!({
            "id": "logical__mod_x",
            "operation": "define_logical_module",
            "selector": { "chunkId": "static/app" },
            "target": { "path": "mod_x" },
            "members": [{
                "id": "m_bridge",
                "name": "bridge",
                "selector": { "binding": { "name": "bridge" } },
            }],
        })],
    );
    opts.extra_files = &[(
        "static/vendor.js",
        "export const mu = { decode: () => \"ok\" };\n",
    )];
    let fixture = run_logical_modules_e2e_fixture(opts);

    let mod_x = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_x.js"))
        .expect("read mod_x.js");
    assert!(
        mod_x.contains("gge") && mod_x.contains("import"),
        "mod_x.js must re-import the source-chunk specifier; got:\n{mod_x}",
    );
    assert!(
        mod_x.contains("gge.decode"),
        "mod_x.js body must still reference `gge.decode`; got:\n{mod_x}",
    );
}

#[test]
fn moved_body_re_imports_specifier_re_exported_by_another_plan() {
    // mod_a re-exports a vendor binding (`a` → public `Re`),
    // making it `BindingKind::Imported` in `Schedule.bindings`.
    // mod_b moves a body that references the same local `a`.
    // The source-chunk re-import filter must NOT skip `a` just
    // because it's in `Schedule.bindings` — `cross_module_imports_for_body`
    // can't satisfy it (Imported bindings have `owner_of() == None`),
    // so without the source-chunk re-import mod_b has a free
    // variable and `ReferenceError: a is not defined` at runtime.
    //
    // The source chunk lives at `static/app/`; place vendor.js
    // there too so the entry's `./vendor.js` resolves (and
    // mod_b's emitted re-import resolves through the same
    // chunk-relative path rewrite).
    let mut opts = FixtureOpts::new(
        r#"import { x as a } from "./vendor.js";
function bridge() {
  return a;
}
console.log(bridge());
export { bridge };
"#,
        vec![
            json!({
                "id": "logical__mod_a",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_a" },
                "members": [{
                    "id": "m_re",
                    "name": "Re",
                    "selector": { "binding": { "name": "a", "kind": "ImportSpecifier" } },
                }],
            }),
            json!({
                "id": "logical__mod_b",
                "operation": "define_logical_module",
                "selector": { "chunkId": "static/app" },
                "target": { "path": "mod_b" },
                "members": [{
                    "id": "m_bridge",
                    "name": "bridge",
                    "selector": { "binding": { "name": "bridge" } },
                }],
            }),
        ],
    );
    opts.extra_files = &[("static/app/vendor.js", "export const x = 42;\n")];
    let fixture = run_logical_modules_e2e_fixture(opts);

    let mod_b = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_b.js"))
        .expect("read mod_b.js");
    // mod_b's bridge body references `a`; mod_b must carry a
    // source-chunk re-import for `a` (the vendor binding) so the
    // moved body resolves at link time.
    assert!(
        mod_b.contains("import") && mod_b.contains("vendor"),
        "mod_b.js must re-import the vendor specifier; got:\n{mod_b}",
    );
    // Behaviour preservation: `bridge()` returns the imported
    // vendor value. Without the filter narrowing this would
    // `ReferenceError` at module-load time.
    assert_entry_output(&fixture, "42\n");
}
