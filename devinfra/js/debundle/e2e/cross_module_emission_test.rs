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
    let fixture = run_fixture(FixtureOpts::new(
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
fn rejects_extracted_binding_assigned_by_residual_owner() {
    // This is the minimal shape behind the Tana boot-progress
    // `Assignment to constant variable` failure: the spec peels a
    // mutable binding, while a still-residual function assigns to
    // that binding at runtime. Emitting `import { a } ...` into the
    // residual entry and leaving `a = 1` there makes Node reject the
    // assignment because imported ESM bindings are read-only in the
    // importing module.
    //
    // The realizability/schedule validation phase must reject this
    // spec before emission. A binding with a cross-destination
    // assignment cannot be safely peeled unless every top-level
    // owner that may assign it is peeled into the same destination,
    // or the emitter grows a sound live-mutation bridge for that
    // binding.
    let mut opts = FixtureOpts::new(
        r#"let a = 0;
function b() {
  a = 1;
}
b();
console.log(a);
export { a };
"#,
        vec![logical_module("state", &[Member::new("a")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    expect_rejection_containing_all(
        opts,
        &["assignment", "assigner", "mutable", "cross-destination"],
    );
}

#[test]
fn object_pattern_and_static_property_keys_do_not_import_residual_binding() {
    let mut opts = FixtureOpts::new(
        r#"const id = "residual";
function Text({ id: h }) {
  return { type: "span", props: { id: h } };
}
export { Text };
"#,
        vec![logical_module("shared/ui/text", &[Member::new("Text")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shared/ui/text.js",
        &["function Text", "props:"],
        &["import { id }"],
    );
    assert_generated_module_after_entry_script(
        &fixture.out_root,
        r#"const { Text } = await import("./static/app/modules/shared/ui/text.js");
console.log(Text({ id: "probe" }).props.id);
"#,
        "probe\n",
    );
}

#[test]
fn jsx_names_do_not_import_residual_binding() {
    // Tana's Text component has this shape after minification:
    // `function Text({ id: h }) { return <span id={h} />; }`.
    // The JSX `span`/`id` tokens are syntax names, not references to
    // residual top-level bindings.
    let mut opts = FixtureOpts::new(
        r#"const id = "residual";
const span = "residual element";
function Text({ id: h }) {
  return <span id={h} />;
}
export { Text };
"#,
        vec![logical_module("shared/ui/text", &[Member::new("Text")])],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/shared/ui/text.js",
        &["function Text", "id="],
        &["import { id }", "import { span }"],
    );
    parse_module(
        &fs::read_to_string(
            fixture
                .out_root
                .join("static/app/modules/shared/ui/text.js"),
        )
        .expect("read shared/ui/text module"),
    );
}

#[test]
fn explicit_modules_share_a_residual_helper_via_imports() {
    // Helper `r` is unclaimed; both `inner` (owns `s`) and
    // `outer` (owns `t`) import it from residual.
    let fixture = run_fixture(FixtureOpts::new(
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
    let fixture = run_fixture(FixtureOpts::new(
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
fn duplicate_top_level_decl_claims_are_rejected() {
    // Regression for the gaffer Tana case: two YAMLs both claim the
    // same input-bundle top-level binding (e.g. both `runtime.yaml`
    // and `ai_conversation_node_accessor.yaml` export
    // `AIConversationNodeAccessor` from binding `ho`). Previously the
    // emitter put the body in one module and emitted
    // `export { AIConversationNodeAccessor };` with no backing decl
    // in the other — invalid JS that fails `import()` with
    // "SyntaxError: Export 'X' is not defined in module".
    //
    // The spec author's options are: put both renames in one module,
    // or pick one module to own the declaration. Two modules
    // re-exporting the same source binding under different readable
    // names is not a supported pattern for top-level decls.
    // (Multi-re-export of an `import_specifier` binding via
    // `re_exported_by` remains supported — see
    // `imported_binding_re_exported_under_two_different_names`.)
    //
    // Different selector forms targeting the same declaration
    // (`{name: ho}` vs `{name: ho, kind: class_declaration}`) all
    // collapse to one `binding_assignment` entry, so the rejection
    // catches any way to spell the duplicate.
    let opts = FixtureOpts::new(
        r#"class ho {
  static isAIConversation() { return false; }
}
console.log(ho);
export { ho };
"#,
        vec![
            (
                "mod_a".to_string(),
                json!({
                    "members": [{
                        "name": "AIConversationNodeAccessor",
                        "selector": { "binding": { "name": "ho", "kind": "class_declaration" } },
                    }],
                }),
            ),
            (
                "mod_b".to_string(),
                json!({
                    "members": [{
                        "name": "AIConversationNodeAccessor",
                        "selector": { "binding": { "name": "ho" } },
                    }],
                }),
            ),
        ],
    );
    expect_rejection_containing_all(
        opts,
        &["Duplicate binding claim", "\"ho\"", "mod_a", "mod_b"],
    );
}

#[test]
fn readable_import_without_collision() {
    // Single-plan sanity: a spec rename becomes the consumer-side import
    // local too; no input-bundle alias is needed when there is no collision.
    let opts = FixtureOpts::new(
        r#"const aH = 7;
console.log(aH);
export { aH };
"#,
        vec![(
            "mod_a".to_string(),
            json!({
                "members": [{ "name": "readableA", "selector": { "binding": { "name": "aH" } } }],
            }),
        )],
    );
    let fixture = run_fixture(opts);
    let entry = fs::read_to_string(&fixture.entry_path).expect("read entry.js");

    assert!(
        entry.contains(r#"import { readableA } from "./modules/mod_a.js""#),
        "expected readable-local mod_a import; entry was:\n{entry}",
    );
    assert!(
        entry.contains("console.log(readableA)"),
        "expected entry body to use readableA; entry was:\n{entry}",
    );
    assert_export_named_specifier(&entry, "readableA", Some("aH"));
    assert!(
        !entry.contains("aH$1"),
        "no fresh-suffix should appear when there is no collision; entry was:\n{entry}",
    );
    assert_entry_output(&fixture, "7\n");
}

#[test]
fn readable_import_avoids_nested_collision() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const aH = 1;
function bC(readableA) {
  return aH + readableA;
}
console.log(bC(2));
export { aH, bC };
"#,
        vec![logical_module(
            "mod_a",
            &[Member::renamed("readableA", "aH")],
        )],
    ));
    let residual = fs::read_to_string(
        fixture
            .out_root
            .join("static/app/modules/residual/unhandled.js"),
    )
    .expect("read residual module");

    assert!(
        residual.contains(r#"import { readableA as readableA$1 } from "../mod_a.js""#),
        "expected import local to avoid nested readableA binding; residual was:\n{residual}",
    );
    assert!(
        residual.contains("return readableA$1 + readableA"),
        "expected imported binding refs to use fresh local and parameter refs to stay local; residual was:\n{residual}",
    );
    assert_entry_output(&fixture, "3\n");
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
    let fixture = run_fixture(opts);
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
        vec![(
            "mod_x".to_string(),
            json!({
                "members": [{
                    "name": "Readable",
                    "selector": {
                        "binding": { "name": "a", "kind": "import_specifier" },
                    },
                }],
            }),
        )],
    );
    opts.extra_files = &[(
        "static/vendor.js",
        "export const x = 42;\nexport default x;\n",
    )];
    let fixture = run_fixture(opts);

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

// --- Duplicate import-specifier claims are rejected ---------------------

#[test]
fn duplicate_import_specifier_claims_are_rejected() {
    // Same rule as `duplicate_top_level_decl_claims_are_rejected`,
    // but for `kind: import_specifier`. Every binding — imported or
    // top-level — must have exactly one home in the spec. If a
    // consumer wants to refer to a vendor symbol under a different
    // local name, it can do so at its own import site; the spec
    // doesn't need a separate re-export module.
    let mut opts = FixtureOpts::new(
        r#"import { j as a } from "./vendor.js";
console.log(a());
export { a };
"#,
        vec![
            (
                "mod_jsx_runtime".to_string(),
                json!({
                    "members": [{
                        "name": "jsxRuntime",
                        "selector": { "binding": { "name": "a", "kind": "import_specifier" } },
                    }],
                }),
            ),
            (
                "mod_dunder_jsx".to_string(),
                json!({
                    "members": [{
                        "name": "__jsx",
                        "selector": { "binding": { "name": "a", "kind": "import_specifier" } },
                    }],
                }),
            ),
        ],
    );
    opts.extra_files = &[("static/vendor.js", "export const j = () => 42;\n")];
    expect_rejection_containing_all(
        opts,
        &[
            "Duplicate binding claim",
            "\"a\"",
            "mod_jsx_runtime",
            "mod_dunder_jsx",
        ],
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
        vec![(
            "mod_x".to_string(),
            json!({
                "members": [{ "name": "bridge", "selector": { "binding": { "name": "bridge" } } }],
            }),
        )],
    );
    opts.extra_files = &[(
        "static/vendor.js",
        "export const mu = { decode: () => \"ok\" };\n",
    )];
    let fixture = run_fixture(opts);

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
            (
                "mod_a".to_string(),
                json!({
                    "members": [{
                        "name": "Re",
                        "selector": { "binding": { "name": "a", "kind": "import_specifier" } },
                    }],
                }),
            ),
            (
                "mod_b".to_string(),
                json!({
                    "members": [{ "name": "bridge", "selector": { "binding": { "name": "bridge" } } }],
                }),
            ),
        ],
    );
    opts.extra_files = &[("static/app/vendor.js", "export const x = 42;\n")];
    let fixture = run_fixture(opts);

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

// --- Dead source-chunk specifier trim ------------------------------------

#[test]
fn drops_specifier_for_imported_binding_claimed_and_unused_in_residual() {
    // Source chunk imports `dep` from vendor and uses it only to
    // build `composed`. Spec claims `composed` for mod_x AND
    // claims `dep` (as an ImportSpecifier-bound member) for
    // mod_dep. After the move, the residual entry never
    // references `dep` anywhere — `composed` lives in mod_x
    // (with its own re-import of `dep`), and the residual's
    // re-export references `composed`. The trim drops the dead
    // `dep` specifier from the residual's vendor import; the
    // directive is preserved as a side-effect-only
    // `import "./vendor.js";` so vendor's evaluation is not
    // skipped (mod_dep, an ImportSpecifier-only logical module
    // with no `Owned` bindings, isn't imported by the residual
    // at runtime, so vendor evaluation must come from the
    // residual itself).
    let mut opts = FixtureOpts::new(
        r#"import { dep } from "./vendor.js";
const composed = dep + 1;
console.log(composed);
export { composed };
"#,
        vec![
            (
                "mod_x".to_string(),
                json!({
                    "members": [{
                        "name": "Composed",
                        "selector": { "binding": { "name": "composed" } },
                    }],
                }),
            ),
            (
                "mod_dep".to_string(),
                json!({
                    "members": [{
                        "name": "Dep",
                        "selector": { "binding": { "name": "dep", "kind": "import_specifier" } },
                    }],
                }),
            ),
        ],
    );
    opts.extra_files = &[("static/app/vendor.js", "export const dep = 41;\n")];
    let fixture = run_fixture(opts);

    let entry = fs::read_to_string(fixture.out_root.join("static/app/entry.js"))
        .expect("read residual entry");
    assert!(
        !entry.contains("{ dep }") && !entry.contains("{dep}"),
        "residual must drop the dead `dep` named specifier; got:\n{entry}",
    );
    assert!(
        entry.contains("./vendor.js"),
        "residual must keep `./vendor.js` as a side-effect-only import to preserve evaluation; got:\n{entry}",
    );

    // Behaviour preservation: vendor.js is still evaluated via
    // the side-effect-only import, so `composed = dep + 1 = 42`
    // and the re-export keeps the original public surface.
    assert_entry_output(&fixture, "42\n");
}
