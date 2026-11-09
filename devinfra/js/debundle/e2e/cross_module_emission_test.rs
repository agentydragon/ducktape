//! Emit-side imports, exports, and local-name disambiguation in
//! modules produced by `materialize_logical_modules`.

use debundle_e2e_support::*;
use serde_json::json;
use std::fs;
use swc_ecma_ast::{ImportSpecifier, ModuleDecl, ModuleExportName, ModuleItem};

fn has_named_import(source: &str, expected: &str) -> bool {
    parse_module(source).body.iter().any(|item| {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            return false;
        };
        import.specifiers.iter().any(|specifier| {
            let ImportSpecifier::Named(named) = specifier else {
                return false;
            };
            match &named.imported {
                Some(ModuleExportName::Ident(name)) => name.sym.as_ref() == expected,
                Some(ModuleExportName::Str(name)) => name.value.to_string_lossy() == expected,
                None => named.local.sym.as_ref() == expected,
            }
        })
    })
}

#[test]
fn assignment_only_import_references_preserve_read_only_binding_failures() {
    for statement in [
        "target = 1;",
        "target += 1;",
        "[target] = [1];",
        "({ value: target } = { value: 1 });",
        "({ target } = { target: 1 });",
        "for (target of [1]) {}",
        "for (target in { value: 1 }) {}",
    ] {
        for extracted in [false, true] {
            let source = if extracted {
                format!(
                    "import {{ value as target }} from './peer.js';\nfunction write() {{ {statement} }}\nwrite();\n"
                )
            } else {
                format!("import {{ value as target }} from './peer.js';\n{statement}\n")
            };
            let modules = if extracted {
                vec![logical_module("writer", &[Member::new("write")])]
            } else {
                vec![]
            };
            let fixture = run_fixture(FixtureOpts::new(&source, modules).with_extra_chunks(&[(
                "static/peer",
                "console.log('peer'); export const value = 0;\n",
            )]));
            assert_preserved_entry_error(&fixture, "TypeError");
        }
    }
}

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
fn explicitly_claimed_assigner_stays_with_extracted_mutable_binding() {
    // Both the mutable cell and its writer are explicit members. The entry
    // imports the writer instead of assigning through a read-only ESM import.
    let mut opts = FixtureOpts::new(
        r#"let a = 0;
function b() {
  a = 1;
}
b();
console.log(a);
export { a };
"#,
        vec![logical_module(
            "state",
            &[Member::new("a"), Member::new("b")],
        )],
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
    assert_preserved_entry_output(&fixture, "1\n");
}

const UNCLAIMED_WRITER_SOURCE: &str =
    "let mutable = 0;\nfunction write() { mutable = 1; }\nwrite();\nconsole.log(mutable);\n";

#[test]
fn partial_rebind_claim_does_not_pull_an_unclaimed_writer_into_a_named_module() {
    // Claiming only the mutable binding leaves its writer to the residual
    // policy. The ordinary gate rejects the split as a cross-destination
    // rebind; the emitted writer would assign through a read-only import.
    for mode in [
        unassigned_mode_inline(),
        unassigned_mode_catchall_file(None),
        unassigned_mode_per_atomic_group(),
        unassigned_mode_coarse_atomic_groups(),
    ] {
        let rejected = run_emit_on_rejection_fixture(
            FixtureOpts::new(
                UNCLAIMED_WRITER_SOURCE,
                vec![logical_module("state", &[Member::new("mutable")])],
            )
            .with_unassigned_mode(mode.clone()),
        );

        let conflicts = rejected.atomic_group_conflicts();
        assert_eq!(conflicts.len(), 1, "{mode:?}: {conflicts:#?}");
        assert!(
            conflicts[0]["causes"]
                .as_array()
                .expect("atomic group causes")
                .iter()
                .any(|cause| cause == "lazy_rebind"),
            "{mode:?}: the gate rejection must retain the rebinding cause: {conflicts:#?}"
        );
        let candidate = rejected.candidate_relocation_assessment();
        assert_eq!(candidate["legacy_gate_accepts"], false, "{candidate:#}");
        assert_eq!(candidate["placement"]["status"], "blocked", "{candidate:#}");
        assert!(
            !candidate["placement"]["cross_destination_rebinds"]
                .as_array()
                .expect("typed cross-destination rebind obligations")
                .is_empty(),
            "{mode:?}: {candidate:#}"
        );

        assert_node_output(
            &rejected
                .source_root
                .join(format!("{}.js", rejected.chunk_id)),
            "1\n",
            "",
        );
        assert_node_error(&rejected.entry_path(), "Assignment to constant variable");
    }
}

#[test]
fn wholly_unclaimed_rebind_group_keeps_its_configured_fallback_and_runtime() {
    for (mode, module_path) in [
        (unassigned_mode_inline(), None),
        (
            unassigned_mode_catchall_file(None),
            Some("static/app/modules/residual/unhandled.js"),
        ),
        (
            unassigned_mode_per_atomic_group(),
            Some("static/app/modules/__auto/atomic_groups/0000.js"),
        ),
        (
            unassigned_mode_coarse_atomic_groups(),
            Some("static/app/modules/__auto/residual_groups/0000.js"),
        ),
    ] {
        let fixture = run_fixture(
            FixtureOpts::new(UNCLAIMED_WRITER_SOURCE, vec![]).with_unassigned_mode(mode),
        );
        assert_preserved_entry_output(&fixture, "1\n");
        if let Some(module_path) = module_path {
            assert_module_source(
                &fixture.out_root,
                module_path,
                &["let mutable = 0;", "function write()", "mutable = 1;"],
                &[],
            );
        }
    }
}

#[test]
fn cross_destination_rebind_is_rejected_when_effect_order_does_not_change() {
    // Keep the declaration and deferred writer in separate explicit
    // destinations. The ESM statement order still matches source order, but
    // the writer module would assign through an imported, read-only binding.
    let rejected = run_emit_on_rejection_fixture(FixtureOpts::new(
        r#"let mutable = 0;
function write() {
  mutable = 1;
  return mutable;
}
write();
console.log(mutable);
export { mutable, write };
"#,
        vec![
            logical_module("state", &[Member::new("mutable")]),
            logical_module("writer", &[Member::new("write")]),
        ],
    ));

    let effects = rejected.effect_translation_validation();
    assert_eq!(effects["swapped_pairs"], 0, "{effects:#}");
    assert_eq!(effects["decision"]["status"], "accepted", "{effects:#}");
    assert_eq!(effects["decision"]["scope"], "no_reordering", "{effects:#}");
    assert_eq!(effects["legacy_gate_accepts"], false, "{effects:#}");

    let candidate = rejected.candidate_relocation_assessment();
    assert_eq!(candidate["status"], "blocked_by_placement", "{candidate:#}");
    assert_eq!(candidate["emitted_order"], effects["emitted_order"]);
    assert_eq!(candidate["legacy_gate_accepts"], false, "{candidate:#}");
    assert_eq!(
        candidate["legacy_gate_ordering_veto"], false,
        "{candidate:#}"
    );
    assert_eq!(candidate["decision"]["status"], "accepted", "{candidate:#}");
    assert_eq!(candidate["swapped_pairs"], 0, "{candidate:#}");
    assert_eq!(
        candidate["placement"]["coverage"]["unresolved_checks"],
        json!([]),
        "{candidate:#}"
    );
    assert_eq!(candidate["placement"]["status"], "blocked");
    assert_eq!(
        candidate["placement"]["cross_destination_rebinds"]
            .as_array()
            .expect("typed cross-destination rebind obligations")
            .len(),
        1,
        "{candidate:#}"
    );
    assert_eq!(
        candidate["placement"]["atomic_group_obligations"][0]["classification"],
        "structural_constraint",
        "{candidate:#}"
    );

    let conflicts = rejected.atomic_group_conflicts();
    assert_eq!(conflicts.len(), 1, "{conflicts:#?}");
    assert!(
        conflicts[0]["causes"]
            .as_array()
            .expect("atomic group causes")
            .iter()
            .any(|cause| cause == "lazy_rebind"),
        "the gate rejection must retain the rebinding cause: {conflicts:#?}"
    );
    assert_module_source(
        &rejected.app_root(),
        "static/app/modules/writer.js",
        &["import { mutable as mutable$1 }", "mutable$1 = 1;"],
        &[],
    );
    assert_node_error(&rejected.entry_path(), "Assignment to constant variable");
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

#[test]
fn duplicate_top_level_decl_claims_are_rejected() {
    // Two YAMLs both claim the same input-bundle top-level binding; emitting
    // both would leave `export { X };` with no backing decl in one module
    // (`SyntaxError: Export 'X' is not defined in module`).
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
    let outcomes = run_rejection_fixture(opts).selector_outcomes();
    let claim = find_outcome(&outcomes, "duplicate_claim", "RuntimeProcessorAlias");
    assert_eq!(claim["placement"]["logical_module"], "mod_b");
    assert_eq!(claim["outcome"]["binding"], "runtimeProcessor");
    assert_eq!(claim["outcome"]["declaration"]["kind"], "class");
    let claimed_by = &claim["outcome"]["claimed_by"];
    assert_eq!(claimed_by["logical_module"], "mod_a");
    assert_eq!(claimed_by["entity"]["export"], "RuntimeProcessor");
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
                "source_units/service",
                &[Member::renamed("service", "runtimeService")],
            ),
            logical_module(
                "duplicates/service",
                &[Member::renamed("serviceAgain", "runtimeService")],
            ),
            logical_module(
                "source_units/cache",
                &[Member::renamed("cache", "runtimeCache")],
            ),
            logical_module(
                "duplicates/cache",
                &[Member::renamed("cacheAgain", "runtimeCache")],
            ),
        ],
    );

    let rejected = run_dry_run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    for required in [
        "2 selector outcome(s): duplicate_claim=2",
        "\"runtimeService\" (`const` at body[0])",
        "source_units/service",
        "as `service`",
        "duplicates/service",
        "as `serviceAgain`",
        "\"runtimeCache\" (`const` at body[1])",
        "source_units/cache",
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

    let outcomes = run_rejection_fixture(opts).selector_outcomes();
    for (export, module) in [
        ("PrimaryCatalog", "catalog/primary"),
        ("DuplicateCatalog", "catalog/duplicate"),
    ] {
        // Each claim is one side of the same ownership conflict.
        let claim = find_outcome(&outcomes, "unsatisfiable", export);
        assert_eq!(claim["placement"]["logical_module"], module);
        assert_eq!(claim["placement"]["selector_kind"], "source_matches");
        assert_eq!(claim["target_binding"], "K");
        assert_eq!(
            claim["outcome"]["witness"]["selectors"]
                .as_array()
                .unwrap()
                .len(),
            2,
            "{claim:#}"
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

    let rejected = run_dry_run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    for required in [
        "2 selector outcome(s): unsatisfiable=2",
        "participates in ownership conflict",
        "as `PrimaryCatalog`",
        "source_matches[].bindings[`K`]",
        "as `DuplicateCatalog`",
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

    let rejected = run_dry_run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    for required in [
        "2 selector outcome(s): no_match=2",
        "diagnostics/source_match",
        "as `MissingFormatter`",
        "as `MissingParser`",
        "did not match any top-level declaration",
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

/// The name pins' duplicate claim is found while requests are built, before
/// any selector is matched, so fail-fast stops there.
#[test]
fn fail_fast_dry_run_stops_at_the_duplicate_claim_found_while_building_requests() {
    let line =
        assert_fail_fast_stops_at_first_outcome(mixed_selector_failure_fixture, "duplicate_claim");
    assert!(line.contains("\"renderCard\""), "{line}");
}

/// Without the duplicate pins, the unmatched selector is rejected before the
/// solve that finds the ambiguous one.
#[test]
fn fail_fast_dry_run_stops_at_the_first_no_match() {
    let line = assert_fail_fast_stops_at_first_outcome(
        || {
            let mut opts = mixed_selector_failure_fixture();
            opts.logical_modules
                .retain(|(path, _)| !path.ends_with("/card"));
            opts
        },
        "no_match",
    );
    assert!(line.contains("as `MissingFormatter`"), "{line}");
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
    assert_export_named_specifiers(&entry, "readableA", &[Some("aH"), None]);
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
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/consumer.js",
        &[
            "sharedReadableName as sharedReadableName$1",
            "sharedReadableName$1() + \":\" + sharedReadableName()",
        ],
        &[],
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
    assert_entry_output(&run_fixture(opts), "0 y vx\n");
}

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

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_x.js",
        &["import {", "Readable", "vendor", "export {", "Readable"],
        &[],
    );
}

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
    let outcomes = run_rejection_fixture(opts).selector_outcomes();
    let claim = find_outcome(&outcomes, "duplicate_claim", "jsxRuntime");
    assert_eq!(claim["placement"]["logical_module"], "mod_jsx_runtime");
    assert_eq!(claim["outcome"]["binding"], "a");
    assert_eq!(claim["outcome"]["declaration"]["kind"], "import");
    assert_eq!(
        claim["outcome"]["claimed_by"]["logical_module"],
        "mod_dunder_jsx"
    );
}

#[test]
fn moved_body_re_imports_specifier_re_exported_by_another_plan() {
    // mod_a re-exports a vendor binding (`a` → public `Re`),
    // making it `BindingKind::Imported` in `AssignmentAnalysis.bindings`.
    // mod_b moves a body that references the same local `a`.
    // The source-chunk re-import filter must NOT skip `a` just
    // because it's in `AssignmentAnalysis.bindings` — `cross_module_imports_for_body`
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

    // mod_b's bridge body references `a`; mod_b must carry a
    // source-chunk re-import for `a` (the vendor binding) so the
    // moved body resolves at link time.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_b.js",
        &["import", "vendor"],
        &[],
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
    let opts = FixtureOpts::new(
        r#"import { o as B } from "./vendor.js";
const St = value => "row:" + value;
function Ite() {
  return St(B("ok"));
}
export { St as B, Ite };
"#,
        vec![logical_module(
            "feature/consumer",
            &[Member::renamed("runConsumer", "Ite")],
        )],
    )
    .with_chunk_renames(chunk_renames(&[
        ChunkRenameEntry::new("vendorHelper", "B").with_kind("import_specifier"),
        ChunkRenameEntry::new("entryHelper", "St"),
    ]))
    .with_unassigned_mode(unassigned_mode_inline())
    .with_extra_files(&[(
        "static/app/vendor.js",
        r#"export function o(value) {
  return "wrapped:" + value;
}
"#,
    )]);
    let fixture = run_fixture(opts);

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/feature/consumer.js",
        &["B as entryHelper", "o as vendorHelper"],
        &["B as vendorHelper"],
    );
    assert_generated_module_after_entry_script(
        &fixture,
        r#"const { runConsumer } = await import("./static/app/modules/feature/consumer.js");
console.log(runConsumer());
"#,
        "row:wrapped:ok\n",
    );
}

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
        !has_named_import(&entry, "dep"),
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
        has_named_import(&entry, "dep"),
        "residual must keep the live `dep` named specifier; got:\n{entry}",
    );
    assert_entry_output(&fixture, "41\n");
}

// Phantom side-effect imports and cross-module binding imports share one
// ordering (`EsmImportOrder`) with the realizability gate's evaluation
// simulator, so imports interleave by linker position: the binding import
// of `mod_p1` (the deeper dependency) precedes the phantom side-effect
// import of `mod_p2`.
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
