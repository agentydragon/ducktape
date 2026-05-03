//! Cross-module emission shape: imports, exports, and local-name
//! disambiguation in modules produced by
//! `materialize_logical_modules`.
//!
//! Each test feeds a fixture spec through the pipeline and
//! asserts on the **emitted source** of the resulting modules
//! (export sets, import directives, local-name disambiguation,
//! source-chunk re-imports for moved bodies). Behaviour
//! preservation under Node is asserted alongside the shape
//! checks where relevant.
//!
//! Sections:
//!
//! - **Cross-module dependency wiring** — extracted modules
//!   import unowned helpers from the residual entry; multiple
//!   plans share a residual helper; rename targets propagate
//!   through cross-module imports across split declarators.
//! - **Consumer-side import-local disambiguation** — two plans
//!   claim the same scrambled local; the second emit gets a
//!   fresh `$N` suffix so the imports don't shadow.
//! - **`ImportSpecifier`-bound members** — a plan member whose
//!   binding is an import specifier from another chunk; the
//!   destination re-imports the source-chunk binding.
//! - **`BindingKind::Imported.re_exported_by`** — two logical
//!   modules can re-export the same imported binding under
//!   different public names.
//! - **Source-chunk re-imports for moved bodies** — moved code
//!   that references a source-chunk import-specifier local
//!   carries a re-import in the destination module so Node can
//!   resolve the free variable.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;

// --- Cross-module dependency wiring --------------------------------------

#[test]
fn extracted_module_imports_unowned_helper_from_residual() {
    // Spec claims only `b` for mod_x. Its helper `a` is unclaimed,
    // so `a` stays in the residual entry; mod_x imports it.
    // Nothing is implicit — the spec is the only routing.
    let fixture = run_logical_modules_e2e_fixture(FixtureOpts::new(
        r#"const a = x => "h:" + x;
const b = x => a(x);
console.log(b("y"));
export { b };
"#,
        vec![logical_module("x", &[Member::new("b")])],
    ));
    assert_module_exports(&fixture.out_root, "static/app/modules/x.js", &["b"], &[]);
    // mod_x imports `a` from residual rather than carrying its
    // declaration locally — the explicit spec is the only routing,
    // closure no longer pulls helpers along.
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
    // Without closure, helper `r` (unclaimed by either explicit
    // module) stays in residual. Both `inner` (owns `s`, which
    // calls `r`) and `outer` (owns `t`, which calls `s` and `r`)
    // import `r` from residual.
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
    // Two `define_logical_module` requests both list the same
    // scrambled `binding`, producing two
    // `import { ... as <local> } from ...` lines into the entry
    // body. Without disambiguation the second emit shadows the
    // first and the file fails to parse. The materializer
    // should mint a fresh `$1` suffix on the second emit.
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

// --- ImportSpecifier-bound members --------------------------------------

#[test]
fn import_specifier_member_emits_reimport_in_destination() {
    // The chunk imports `x` as local `a` from `./vendor.js`. The spec
    // claims that import as a member of mod_x with rename `Readable`.
    // Materialized mod_x.js must end up with a re-import like
    // `import { x as Readable } from "../vendor.js"` plus
    // `export { Readable };` so Node can resolve the export.
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
    // The chunk imports `j` from a vendor under local `a`, then
    // uses `a` once. Two logical modules each re-export `a`
    // under different public names — `jsxRuntime` and `__jsx`.
    // Both emit successfully with their own re-import paths;
    // the entries share one `BindingKind::Imported` whose
    // `re_exported_by` map carries each module's chosen public
    // name.
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

    // Each module exports the binding under its own chosen name.
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
    // When `materialize_logical_modules` moves a top-level decl
    // whose body references a name that was an
    // `import { foo as gge }` in the source chunk, the
    // destination module must carry along a re-import for `gge`.
    // Without it the moved code references a free variable and
    // Node throws `ReferenceError: gge is not defined`.
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
    // The destination still references `gge` — confirms the moved body
    // wasn't rewritten away.
    assert!(
        mod_x.contains("gge.decode"),
        "mod_x.js body must still reference `gge.decode`; got:\n{mod_x}",
    );
}
