//! Emit-side imports, exports, and local-name disambiguation in
//! modules produced by `materialize_logical_modules`.

use debundle_e2e_support::*;
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
fn after_entry_probe_imports_the_fixture_chunk_entry() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const value = "custom";
console.log("entry");
export { value };
"#,
            vec![logical_module("feature/value", &[Member::new("value")])],
        )
        .with_chunk_id("static/custom"),
    );
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const { value } = await import("./static/custom/modules/feature/value.js");
console.log(value);
"#,
        "custom\n",
    );
}

#[test]
fn folds_unclaimed_assigner_with_extracted_mutable_binding() {
    // This is the minimal shape behind the an upstream boot-progress
    // `Assignment to constant variable` failure: the spec peels a
    // mutable binding, while a still-residual function assigns to
    // that binding at runtime. Emitting `import { a } ...` into the
    // residual entry and leaving `a = 1` there makes Node reject the
    // assignment because imported ESM bindings are read-only in the
    // importing module.
    //
    // Rebind-only atomic-unit folding extends the explicit `state`
    // destination to cover the unclaimed assigner `b`. That keeps the
    // assignment local to the module that owns `a`; the residual entry
    // imports and calls `b` instead of assigning an imported binding.
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
    let fixture = run_fixture(opts);

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/state.js",
        &["a", "b"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/state.js",
        &["let a = 0;", "function b()", "a = 1;"],
        &[],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &["import { a, b }"],
        &["a = 1;"],
    );
    assert_entry_output(&fixture, "1\n");
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
        &fixture,
        r#"const { Text } = await import("./static/app/modules/shared/ui/text.js");
console.log(Text({ id: "probe" }).props.id);
"#,
        "probe\n",
    );
}

#[test]
fn jsx_names_do_not_import_residual_binding() {
    // the upstream Text component has this shape after minification:
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
        &fixture,
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
    // Two YAMLs both claim the same input-bundle top-level binding. Previously
    // the emitter put the body in one module and emitted `export { X };` with
    // no backing decl in the other — invalid JS that fails `import()` with
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
        r#"class runtimeProcessor {
  static isEnabled() { return false; }
}
console.log(runtimeProcessor);
export { runtimeProcessor };
"#,
        vec![
            logical_module(
                "mod_a",
                &[Member::renamed_with_kind(
                    "RuntimeProcessor",
                    "runtimeProcessor",
                    Some("class_declaration"),
                )],
            ),
            logical_module(
                "mod_b",
                &[Member::renamed("RuntimeProcessorAlias", "runtimeProcessor")],
            ),
        ],
    );
    expect_rejection_containing_all(
        opts,
        &[
            "Duplicate binding claim",
            "\"runtimeProcessor\"",
            "mod_a",
            "as `RuntimeProcessor`",
            "members[].selector.binding as `RuntimeProcessor`",
            "mod_b",
            "as `RuntimeProcessorAlias`",
        ],
    );
}

#[test]
fn duplicate_top_level_decl_claims_are_reported_together() {
    let opts = FixtureOpts::new(
        r#"const runtimeService = "service";
const runtimeCache = "cache";
console.log(runtimeService, runtimeCache);
export { runtimeService, runtimeCache };
"#,
        vec![
            logical_module(
                "owners/service",
                &[Member::renamed("service", "runtimeService")],
            ),
            logical_module(
                "duplicates/service",
                &[Member::renamed("serviceAgain", "runtimeService")],
            ),
            logical_module("owners/cache", &[Member::renamed("cache", "runtimeCache")]),
            logical_module(
                "duplicates/cache",
                &[Member::renamed("cacheAgain", "runtimeCache")],
            ),
        ],
    );

    let rejected = run_keep_going_dry_run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    for required in [
        "Duplicate binding claim report: 2 duplicate claim(s) found",
        "\"runtimeService\"",
        "owners/service",
        "as `service`",
        "duplicates/service",
        "as `serviceAgain`",
        "\"runtimeCache\"",
        "owners/cache",
        "as `cache`",
        "duplicates/cache",
        "as `cacheAgain`",
    ] {
        assert!(
            stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{stderr}",
        );
    }
}

#[test]
fn duplicate_source_match_class_claims_report_exports_and_selector_origins() {
    let class_selector = r#"class K {
  ANYTHING;
  open() {
    return "open";
  }
  ANYTHING;
  close() {
    return "closed";
  }
  ANYTHING;
}"#;
    let opts = FixtureOpts::new(
        r#"class RuntimeCatalog {
  constructor() {
    this.count = 0;
  }
  open() {
    return "open";
  }
  close() {
    return "closed";
  }
}
console.log(new RuntimeCatalog().close());
export { RuntimeCatalog };
"#,
        vec![
            logical_module(
                "catalog/primary",
                &[Member::source_alpha("PrimaryCatalog", class_selector)],
            ),
            logical_module(
                "catalog/duplicate",
                &[Member::source_alpha_target(
                    "DuplicateCatalog",
                    "K",
                    class_selector,
                )],
            ),
        ],
    );

    expect_rejection_containing_all(
        opts,
        &[
            "valid global selector assignment",
            "catalog/primary",
            "as `PrimaryCatalog`",
            "source_matches[].bindings[`K`]",
            "catalog/duplicate",
            "as `DuplicateCatalog`",
            "source_matches[].bindings[`K`]",
        ],
    );
}

#[test]
fn repeated_source_match_selectors_use_native_solver_across_modules() {
    let class_selector = r#"class K {
  ANYTHING;
  label() {
    return "catalog";
  }
  ANYTHING;
}"#;
    let opts = FixtureOpts::new(
        r#"class RuntimeCatalog {
  label() {
    return "catalog";
  }
}
console.log(new RuntimeCatalog().label());
export { RuntimeCatalog };
"#,
        vec![
            logical_module(
                "catalog/primary",
                &[Member::source_alpha_target(
                    "PrimaryCatalog",
                    "K",
                    class_selector,
                )],
            ),
            logical_module(
                "catalog/duplicate",
                &[Member::source_alpha_target(
                    "DuplicateCatalog",
                    "K",
                    class_selector,
                )],
            ),
        ],
    );

    let rejected = run_keep_going_dry_run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    for required in [
        "Source-match selector diagnostic report: 2 unresolved selector(s) found",
        "valid global selector assignment",
        "catalog/primary",
        "catalog/duplicate",
    ] {
        assert!(
            stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{stderr}",
        );
    }
}

#[test]
fn duplicate_source_match_members_in_one_module_report_member_origins() {
    let class_selector = r#"class K {
  ANYTHING;
  label() {
    return "catalog";
  }
  ANYTHING;
}"#;
    let opts = FixtureOpts::new(
        r#"class RuntimeCatalog {
  label() {
    return "catalog";
  }
}
console.log(new RuntimeCatalog().label());
export { RuntimeCatalog };
"#,
        vec![logical_module(
            "catalog/combined",
            &[
                Member::source_alpha("PrimaryCatalog", class_selector),
                Member::source_alpha_target("DuplicateCatalog", "K", class_selector),
            ],
        )],
    );

    let rejected = run_keep_going_dry_run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    for required in [
        "Source-match selector diagnostic report: 2 unresolved selector(s) found",
        "valid global selector assignment",
        "export `PrimaryCatalog`",
        "source_matches[].bindings[`K`]",
        "export `DuplicateCatalog`",
        "source_matches[].bindings[`K`]",
    ] {
        assert!(
            stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{stderr}",
        );
    }
    assert!(
        !stderr.contains("duplicate source bindings:"),
        "old blank duplicate list must not be emitted:\n{stderr}",
    );
}

#[test]
fn keep_going_drops_failed_source_match_members_in_same_module() {
    let opts = FixtureOpts::new(
        r#"function existingHelper(value) {
  return value.trim();
}
console.log(existingHelper(" ok "));
export { existingHelper };
"#,
        vec![logical_module(
            "diagnostics/source_match",
            &[
                Member::source_alpha(
                    "MissingFormatter",
                    r#"function missingFormatter(value) {
  return value.toLowerCase();
}"#,
                ),
                Member::source_alpha(
                    "MissingParser",
                    r#"function missingParser(value) {
  return Number(value);
}"#,
                ),
            ],
        )],
    );

    let rejected = run_keep_going_dry_run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    for required in [
        "Source-match selector diagnostic report: 2 unresolved selector(s) found",
        "diagnostics/source_match",
        "as `MissingFormatter`",
        "as `MissingParser`",
        "valid global selector assignment",
        "missingFormatter",
        "missingParser",
    ] {
        assert!(
            stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{stderr}",
        );
    }
    assert!(
        !stderr.contains("duplicate source binding"),
        "failed source_match members must not become empty binding claims:\n{stderr}",
    );
}

fn source_match_and_duplicate_claims_fixture() -> FixtureOpts<'static> {
    let missing_selector = r#"function selectedFormatter(value) {
  return value.toLowerCase();
}"#;
    let ambiguous_selector = r#"function repeatedHelper() {
  return "shared";
}"#;
    FixtureOpts::new(
        r#"function renderCard(value) {
  return value.trim();
}
function decoratePrimary() {
  return "shared";
}
function decorateSecondary() {
  return "shared";
}
console.log(renderCard(" ok "), decoratePrimary(), decorateSecondary());
export { renderCard, decoratePrimary, decorateSecondary };
"#,
        vec![
            logical_module(
                "diagnostics/missing",
                &[Member::source_alpha("MissingFormatter", missing_selector)],
            ),
            logical_module("owners/card", &[Member::new("renderCard")]),
            logical_module(
                "duplicates/card",
                &[Member::renamed("renderCardAgain", "renderCard")],
            ),
            logical_module(
                "diagnostics/ambiguous",
                &[Member::source_alpha("AmbiguousHelper", ambiguous_selector)],
            ),
        ],
    )
}

#[test]
fn dry_run_defaults_to_collecting_source_match_failures_and_duplicate_claims_together() {
    let rejected = run_dry_run_rejection_fixture(source_match_and_duplicate_claims_fixture());
    let stderr = rejected.stderr;
    for required in [
        "Source-match selector diagnostic report: 2 unresolved selector(s) found",
        "diagnostics/missing",
        "as `MissingFormatter`",
        "valid global selector assignment",
        "diagnostics/ambiguous",
        "as `AmbiguousHelper`",
        "Duplicate binding claim report: 1 duplicate claim(s) found",
        "\"renderCard\"",
        "owners/card",
        "duplicates/card",
        "as `renderCardAgain`",
    ] {
        assert!(
            stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{stderr}",
        );
    }
}

#[test]
fn fail_fast_dry_run_stops_before_later_duplicate_claim_diagnostics() {
    let rejected =
        run_fail_fast_dry_run_rejection_fixture(source_match_and_duplicate_claims_fixture());
    let stderr = rejected.stderr;
    for required in [
        "diagnostics/ambiguous",
        "export `AmbiguousHelper`",
        "source_matches[].bindings[`repeatedHelper`]",
        "valid global selector assignment",
    ] {
        assert!(
            stderr.contains(required),
            "stderr missing {required:?}\nstderr:\n{stderr}",
        );
    }
    for absent in [
        "Source-match selector diagnostic report:",
        "Duplicate binding claim report:",
        "duplicates/card",
        "as `renderCardAgain`",
    ] {
        assert!(
            !stderr.contains(absent),
            "fail-fast stderr unexpectedly contained {absent:?}\nstderr:\n{stderr}",
        );
    }
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
        vec![logical_module(
            "mod_a",
            &[Member::renamed("readableA", "aH")],
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
fn readable_import_avoids_top_level_function_decl_collision() {
    // Collision shape:
    //
    //   provider exports a as sharedReadableName
    //   consumer owns b, also exported as sharedReadableName, and calls a.
    //
    // The consumer module must alias the imported provider binding; otherwise the
    // emitted module declares the same lexical name twice and Chromium rejects
    // it with "Identifier 'sharedReadableName' has already been declared".
    let fixture = run_fixture(FixtureOpts::new(
        r#"function a() {
  return "provider";
}
function b() {
  return "local";
}
function c() {
  return a() + ":" + b();
}
console.log(c());
export { a, b, c };
"#,
        vec![
            logical_module("provider", &[Member::renamed("sharedReadableName", "a")]),
            logical_module(
                "consumer",
                &[
                    Member::renamed("sharedReadableName", "b"),
                    Member::renamed("run", "c"),
                ],
            ),
        ],
    ));
    let consumer = fs::read_to_string(fixture.out_root.join("static/app/modules/consumer.js"))
        .expect("read consumer module");

    parse_module(&consumer);
    assert_unique_lexical_decls_per_scope(&consumer, "sharedReadableName");
    assert!(
        consumer.contains("sharedReadableName as sharedReadableName$1"),
        "expected imported readable name to be aliased away from local function decl; got:\n{consumer}",
    );
    assert!(
        consumer.contains("sharedReadableName$1() + \":\" + sharedReadableName()"),
        "expected provider refs to use import alias and local refs to stay local; got:\n{consumer}",
    );
    assert_entry_output(&fixture, "provider:local\n");
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
        vec![logical_module(
            "mod_x",
            &[Member::renamed_with_kind(
                "Readable",
                "a",
                Some("import_specifier"),
            )],
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
            logical_module(
                "mod_jsx_runtime",
                &[Member::renamed_with_kind(
                    "jsxRuntime",
                    "a",
                    Some("import_specifier"),
                )],
            ),
            logical_module(
                "mod_dunder_jsx",
                &[Member::renamed_with_kind(
                    "__jsx",
                    "a",
                    Some("import_specifier"),
                )],
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
        vec![logical_module("mod_x", &[Member::new("bridge")])],
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
    // making it `BindingKind::Imported` in `ChunkFactorization.bindings`.
    // mod_b moves a body that references the same local `a`.
    // The source-chunk re-import filter must NOT skip `a` just
    // because it's in `ChunkFactorization.bindings` — `cross_module_imports_for_body`
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
            logical_module(
                "mod_a",
                &[Member::renamed_with_kind(
                    "Re",
                    "a",
                    Some("import_specifier"),
                )],
            ),
            logical_module("mod_b", &[Member::new("bridge")]),
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

#[test]
fn residual_public_export_name_does_not_capture_unrelated_chunk_renamed_import() {
    // Collision shape:
    //
    //   import { o as B } from "./vendor.js";  // source local B = vendor helper
    //   const St = ...;                        // residual entry helper
    //   export { St as B };                    // public entry export B
    //
    // The two `B`s live in different namespaces. The moved body needs
    // `St` from entry and the vendor import local `B`; chunk_renames
    // then naturalizes them to `entryHelper` and `vendorHelper`.
    // The entry import must therefore be `B as entryHelper`, not
    // `B as vendorHelper`.
    let opts = FixtureOpts {
        local_property_effects: false,
        trusted_dataflow_summaries: false,
        chunk_export_purity: &[],
        extra_chunks: &[],
        source: r#"import { o as B } from "./vendor.js";
const St = value => "row:" + value;
function Ite() {
  return St(B("ok"));
}
export { St as B, Ite };
"#,
        logical_modules: vec![logical_module(
            "feature/consumer",
            &[Member::renamed("runConsumer", "Ite")],
        )],
        chunk_renames: Some(chunk_renames(&[
            ChunkRenameEntry::new("vendorHelper", "B").with_kind("import_specifier"),
            ChunkRenameEntry::new("entryHelper", "St"),
        ])),
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_inline(),
        dataflow_aware_s_chain: false,
        admission_overrides: &[],
        extra_files: &[(
            "static/app/vendor.js",
            r#"export function o(value) {
  return "wrapped:" + value;
}
"#,
        )],
    };
    let fixture = run_fixture(opts);

    let moved = fs::read_to_string(
        fixture
            .out_root
            .join("static/app/modules/feature/consumer.js"),
    )
    .expect("read moved consumer module");
    parse_module(&moved);
    assert_unique_import_locals(&moved);

    assert!(
        moved.contains("B as entryHelper"),
        "entry's public export B must be imported under its entry-local name; got:\n{moved}",
    );
    assert!(
        moved.contains("o as vendorHelper"),
        "vendor import local B must keep the vendor-helper readable name; got:\n{moved}",
    );
    assert!(
        !moved.contains("B as vendorHelper"),
        "entry export B must not be mistaken for the vendor binding; got:\n{moved}",
    );
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const { runConsumer } = await import("./static/app/modules/feature/consumer.js");
console.log(runConsumer());
"#,
        "row:wrapped:ok\n",
    );
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
            logical_module("mod_x", &[Member::renamed("Composed", "composed")]),
            logical_module(
                "mod_dep",
                &[Member::renamed_with_kind(
                    "Dep",
                    "dep",
                    Some("import_specifier"),
                )],
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

#[test]
fn keeps_specifier_for_imported_binding_still_used_in_residual() {
    let mut opts = FixtureOpts::new(
        r#"import { dep } from "./vendor.js";
const composed = dep + 1;
console.log(dep);
export { composed };
"#,
        vec![
            logical_module("mod_x", &[Member::renamed("Composed", "composed")]),
            logical_module(
                "mod_dep",
                &[Member::renamed_with_kind(
                    "Dep",
                    "dep",
                    Some("import_specifier"),
                )],
            ),
        ],
    );
    opts.extra_files = &[("static/app/vendor.js", "export const dep = 41;\n")];
    let fixture = run_fixture(opts);

    let entry = fs::read_to_string(fixture.out_root.join("static/app/entry.js"))
        .expect("read residual entry");
    assert!(
        entry.contains("{ dep }") || entry.contains("{dep}"),
        "residual must keep the live `dep` named specifier; got:\n{entry}",
    );
    assert_entry_output(&fixture, "41\n");
}

// Regression test (originally RED) for the phantom-first import
// divergence: the emitter used to place phantom side-effect imports
// FIRST in every moved module as a separate run before the
// cross-module binding imports, while the realizability gate's
// evaluation simulator ordered ALL of a module's import targets in
// one `linker_position` list. A phantom provider with a HIGHER
// linker position than a binding-import provider therefore made the
// emitted DFS order diverge from the simulated one. Both sides now
// consume the single shared ordering (`EsmImportOrder`); this pins
// the emitted shape: imports interleave by linker position, so the
// binding import of `mod_p1` (the deeper dependency) precedes the
// phantom side-effect import of `mod_p2`.
//
// Shape: `mod_m`'s body references `p1_v` directly (binding import
// of `mod_p1`) and calls the residual helper `read_helper` at init,
// whose body reads `p2_v` — an at-init-promoted constraining edge to
// `mod_p2` with no direct binding reference, i.e. a phantom
// side-effect import. `p2_v`'s initializer reads `p1_v`, forcing
// `linker_position(mod_p1) < linker_position(mod_p2)`.
#[test]
fn phantom_side_effect_import_interleaves_with_binding_imports_by_linker_position() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const p1_v = "x";
const p2_v = p1_v + "y";
function read_helper() { return p2_v; }
const m_v = p1_v + read_helper();
console.log(m_v);
export { p1_v, p2_v, m_v };
"#,
        vec![
            logical_module("mod_p1", &[Member::new("p1_v")]),
            logical_module("mod_p2", &[Member::new("p2_v")]),
            logical_module("mod_m", &[Member::new("m_v")]),
        ],
    ));

    let m_src = fs::read_to_string(fixture.out_root.join("static/app/modules/mod_m.js"))
        .expect("read mod_m.js");
    let binding_import_pos = m_src
        .find("import { p1_v }")
        .unwrap_or_else(|| panic!("mod_m.js missing binding import of p1_v:\n{m_src}"));
    let phantom_import_pos = m_src.find(r#"import "./mod_p2.js""#).unwrap_or_else(|| {
        panic!("mod_m.js missing phantom side-effect import of mod_p2:\n{m_src}")
    });
    assert!(
        binding_import_pos < phantom_import_pos,
        "phantom import of mod_p2 (linker position 1) must follow the \
         binding import of mod_p1 (linker position 0) — shared-order \
         interleaving, not phantom-first:\n{m_src}",
    );
    assert_entry_output(&fixture, "xxy\n");
}
