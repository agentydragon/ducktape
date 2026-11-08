//! `unassigned_mode: per_atomic_group` synthesizes one logical module
//! per unclaimed atomic factor group instead of funneling everything
//! into the residual catch-all. Pin the contract:
//!
//! * Default mode (`catchall`): unclaimed bindings live together in
//!   the residual module.
//! * `per_atomic_group`: each unclaimed atomic group becomes its own
//!   synthetic `__auto/atomic_groups/{idx:04}` module; the residual catch-all
//!   collapses to whatever truly couldn't be peeled (here: nothing).

use debundle_e2e_support::*;
use std::path::Path;

const FIXTURE_SOURCE: &str = r#"export const a = 1;
export const b = 2;
"#;

#[test]
fn catchall_keeps_unclaimed_bindings_in_residual() {
    let fixture = run_fixture(FixtureOpts::new(FIXTURE_SOURCE, vec![]));
    // Default catchall: both `a` and `b` go into the residual
    // module; no synthetic `__auto/atomic_groups/...` files exist.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["a", "b"],
        &[],
    );
    assert!(
        !atomic_group_dir_exists(&fixture.out_root),
        "expected no __auto/atomic_groups/ tree under catchall mode",
    );
}

#[test]
fn per_atomic_group_synthesizes_one_module_per_unclaimed_atomic_group() {
    let fixture = run_fixture(
        FixtureOpts::new(FIXTURE_SOURCE, vec![])
            .with_unassigned_mode(unassigned_mode_per_atomic_group()),
    );
    // Two top-level const bindings with no constraining edges
    // between them form two separate atomic groups. Under
    // `per_atomic_group` each atomic group becomes its own synthetic module.
    let group_dir = fixture
        .out_root
        .join("static/app/modules/__auto/atomic_groups");
    assert!(
        group_dir.exists(),
        "expected synthetic atomic-group module directory at {}",
        group_dir.display(),
    );
    // Collect synthesized files and union their exports.
    let mut entries: Vec<String> = std::fs::read_dir(&group_dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".js"))
        .collect();
    entries.sort();
    assert!(
        entries.len() >= 2,
        "expected at least two synthesized atomic-group modules; got {entries:?}",
    );
    let mut all_exports = std::collections::BTreeSet::<String>::new();
    for entry in &entries {
        let exports = list_module_exports(
            &fixture.out_root,
            &format!("static/app/modules/__auto/atomic_groups/{entry}"),
        );
        all_exports.extend(exports);
    }
    assert!(
        all_exports.contains("a") && all_exports.contains("b"),
        "expected synthesized modules to collectively export `a` and `b`; got {all_exports:?}",
    );
    // The residual catch-all collapses: nothing left to put there,
    // so the empty residual is dropped entirely.
    let residual = fixture
        .out_root
        .join("static/app/modules/residual/unhandled.js");
    if residual.exists() {
        let residual_exports = list_module_exports(
            &fixture.out_root,
            "static/app/modules/residual/unhandled.js",
        );
        assert!(
            !residual_exports.iter().any(|e| e == "a" || e == "b"),
            "expected residual to no longer carry `a`/`b` under per_atomic_group; got {residual_exports:?}",
        );
    }
}

fn atomic_group_dir_exists(out_root: &Path) -> bool {
    out_root
        .join("static/app/modules/__auto/atomic_groups")
        .exists()
}

#[test]
fn per_atomic_group_peels_entry_with_leading_import() {
    let mut opts = FixtureOpts::new("import { a } from './dep.js'; console.log(a);", vec![]);
    opts.unassigned_mode = unassigned_mode_per_atomic_group();
    opts.extra_chunks = &[("static/dep", "export const a = 1;")];
    let fixture = run_fixture(opts);
    assert_entry_output(&fixture, "1\n");
}

const SUBSET_SOURCE: &str = r#"globalThis.events = ["before"];
export const originalName = "selected";
export const omitted = "omitted";
export let carried;
carried = (globalThis.events.push("residual"), "carried");
globalThis.events.push("after");
console.log(globalThis.events.join(","));
"#;

fn subset_spec() -> FixtureOpts<'static> {
    FixtureOpts::new(
        SUBSET_SOURCE,
        vec![
            logical_module(
                "features/public",
                &[Member::renamed("publicName", "originalName")],
            ),
            logical_module("features/omitted", &[Member::new("omitted")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_per_atomic_group())
}

fn write_subset(json: &str) -> tempfile::NamedTempFile {
    let subset = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(subset.path(), json).unwrap();
    subset
}

fn atomic_group_with_source(fixture: &Fixture, source_fragment: &str) -> String {
    let group_dir = fixture
        .out_root
        .join("static/app/modules/__auto/atomic_groups");
    for entry in std::fs::read_dir(group_dir).unwrap() {
        let entry = entry.unwrap();
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.ends_with(".js") {
            continue;
        }
        let source = std::fs::read_to_string(entry.path()).unwrap();
        if source.contains(source_fragment) {
            return format!("static/app/modules/__auto/atomic_groups/{name}");
        }
    }
    panic!("no generated atomic-group module contains {source_fragment:?}");
}

#[test]
fn ownership_subset_with_per_atomic_group_keeps_selected_rename_and_groups_residual_statement() {
    let subset = write_subset(r#"{"modules":[{"chunk":"static/app","module":"features/public"}]}"#);
    let subset_path = subset.path().to_str().unwrap();
    let fixture =
        run_fixture_without_graphs_with_args(subset_spec(), &["--ownership-subset", subset_path]);

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/features/public.js",
        &["publicName"],
        &[],
    );
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/features/omitted.js")
            .exists()
    );
    let residual_group = atomic_group_with_source(&fixture, "let carried");
    assert_module_source(
        &fixture.out_root,
        &residual_group,
        &[
            "let carried",
            "carried =",
            "globalThis.events.push(\"residual\")",
        ],
        &["publicName"],
    );
    assert_entry_output(&fixture, "before,residual,after\n");
}

#[test]
fn empty_ownership_subset_routes_all_claims_to_generated_atomic_groups() {
    let subset = write_subset(r#"{"modules":[]}"#);
    let subset_path = subset.path().to_str().unwrap();
    let fixture =
        run_fixture_without_graphs_with_args(subset_spec(), &["--ownership-subset", subset_path]);

    assert!(
        !fixture
            .out_root
            .join("static/app/modules/features/public.js")
            .exists()
    );
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/features/omitted.js")
            .exists()
    );
    let generated_public = atomic_group_with_source(&fixture, "originalName = \"selected\"");
    assert_module_source(
        &fixture.out_root,
        &generated_public,
        &["originalName"],
        &["publicName"],
    );
    let residual_group = atomic_group_with_source(&fixture, "let carried");
    assert_module_source(
        &fixture.out_root,
        &residual_group,
        &[
            "let carried",
            "carried =",
            "globalThis.events.push(\"residual\")",
        ],
        &[],
    );
    assert_entry_output(&fixture, "before,residual,after\n");
}

#[test]
fn ownership_subset_keeps_full_spec_selector_duplicate_and_premise_guards() {
    for mode in subset_modes() {
        let subset =
            write_subset(r#"{"modules":[{"chunk":"static/app","module":"features/public"}]}"#);
        let subset_path = subset.path().to_str().unwrap();

        let invalid_selector = FixtureOpts::new(
            SUBSET_SOURCE,
            vec![
                logical_module(
                    "features/public",
                    &[Member::renamed("publicName", "originalName")],
                ),
                logical_module(
                    "features/omitted",
                    &[Member::source_alpha("missing", "const missing = 9;")],
                ),
            ],
        )
        .with_unassigned_mode(mode.clone());
        let rejected = run_rejection_fixture_without_graphs_with_args(
            invalid_selector,
            &["--ownership-subset", subset_path],
            1,
        );
        assert!(rejected.stderr.contains("no_match"), "{}", rejected.stderr);

        let duplicate_claim = FixtureOpts::new(
            SUBSET_SOURCE,
            vec![
                logical_module("features/public", &[Member::new("originalName")]),
                logical_module("features/duplicate", &[Member::new("originalName")]),
            ],
        )
        .with_unassigned_mode(mode.clone());
        let rejected = run_rejection_fixture_without_graphs_with_args(
            duplicate_claim,
            &["--ownership-subset", subset_path],
            1,
        );
        assert!(rejected.stderr.contains("duplicate"), "{}", rejected.stderr);

        let premise =
            serde_yaml::from_str("intrinsics_never_written:\n  families: [Map]\n  note: audited\n")
                .unwrap();
        let contradiction = FixtureOpts::new(
        "function patch(value) { Map.prototype.extra = value; }\nexport const originalName = 1;\n",
        vec![logical_module(
            "features/public",
            &[Member::renamed("publicName", "originalName")],
        )],
    )
    .with_unassigned_mode(mode.clone())
    .with_program_premises(premise);
        let rejected = run_rejection_fixture_without_graphs_with_args(
            contradiction,
            &["--ownership-subset", subset_path],
            1,
        );
        assert!(
            rejected.stderr.contains("intrinsics_never_written")
                && rejected.stderr.contains("exact objects"),
            "{}",
            rejected.stderr
        );
    }
}

fn subset_modes() -> [spec::UnassignedMode; 4] {
    [
        unassigned_mode_inline(),
        unassigned_mode_catchall_file(None),
        unassigned_mode_per_atomic_group(),
        unassigned_mode_coarse_atomic_groups(),
    ]
}

#[test]
fn empty_and_retained_subsets_use_every_normal_residual_policy() {
    for mode in subset_modes() {
        for selected in [false, true] {
            let subset = write_subset(if selected {
                r#"{"modules":[{"chunk":"static/app","module":"features/public"}]}"#
            } else {
                r#"{"modules":[]}"#
            });
            let fixture = run_fixture_without_graphs_with_args(
                subset_spec().with_unassigned_mode(mode.clone()),
                &["--ownership-subset", subset.path().to_str().unwrap()],
            );
            assert_preserved_entry_output(&fixture, "before,residual,after\n");
            assert!(
                !fixture
                    .out_root
                    .join("static/app/modules/features/omitted.js")
                    .exists()
            );
            if selected {
                assert_module_exports(
                    &fixture.out_root,
                    "static/app/modules/features/public.js",
                    &["publicName"],
                    &["originalName"],
                );
            } else {
                assert!(
                    !fixture
                        .out_root
                        .join("static/app/modules/features/public.js")
                        .exists()
                );
            }
            if matches!(mode, spec::UnassignedMode::CatchallFile { .. }) {
                assert_module_source(
                    &fixture.out_root,
                    "static/app/modules/residual/unhandled.js",
                    &["const omitted", "let carried"],
                    &[],
                );
                assert_module_source(
                    &fixture.out_root,
                    "static/app/modules/residual/unhandled.js",
                    &["let carried", "carried ="],
                    &[],
                );
            }
        }
    }
}

#[test]
fn omitting_an_explicit_catchall_claim_recreates_only_residual_ownership() {
    for selected in ["features/public", "residual/unhandled", ""] {
        let subset = write_subset(&if selected.is_empty() {
            r#"{"modules":[]}"#.to_string()
        } else {
            format!(r#"{{"modules":[{{"chunk":"static/app","module":"{selected}"}}]}}"#)
        });
        let fixture = run_fixture_without_graphs_with_args(
            FixtureOpts::new(
                "export const first = 1;\nexport const omitted = 2;\nconsole.log(first, omitted);\n",
                vec![logical_module("features/public", &[Member::new("first")]), logical_module("residual/unhandled", &[Member::renamed("renamed", "omitted")])],
            ).with_unassigned_mode(unassigned_mode_catchall_file(None)),
            &["--ownership-subset", subset.path().to_str().unwrap()],
        );
        assert_preserved_entry_output(&fixture, "1 2\n");
        let (expected, excluded) = if selected == "residual/unhandled" {
            ("renamed", "omitted")
        } else {
            ("omitted", "renamed")
        };
        assert_module_source(
            &fixture.out_root,
            "static/app/modules/residual/unhandled.js",
            &[&format!("const {expected}")],
            &[&format!("const {excluded}")],
        );
    }
}

#[test]
fn subset_retains_full_context_cross_claim_anchors_imports_and_anonymous_claims() {
    for mode in subset_modes() {
        for retain_import in [false, true] {
            let subset = write_subset(if retain_import {
                r#"{"modules":[{"chunk":"static/app","module":"features/run"},{"chunk":"static/app","module":"features/provider"}]}"#
            } else {
                r#"{"modules":[{"chunk":"static/app","module":"features/run"}]}"#
            });
            let fixture = run_fixture_without_graphs_with_args(
            FixtureOpts::new(
                "import { seed } from './peer.js';\nfunction base(value) { return value + seed; }\nfunction wrapper(value) { return base(value); }\nconsole.log(wrapper(1));\nexport { wrapper };\n",
                vec![
                    logical_module("features/anchor", &[Member::source_alpha("anchor", "function $fn($value) { return $value + seed; }")]),
                    logical_module("features/provider", &[Member::renamed_with_kind("providerSeed", "seed", Some("import_specifier"))]),
                    logical_module_with_anon("features/run", &[Member::cross_ref_references("run", "anchor", Some("function_declaration"))], &["console.log(wrapper(1));"]),
                ],
            ).with_extra_chunks(&[("static/peer", "export const seed = 41;\n")]).with_unassigned_mode(mode.clone()),
            &["--ownership-subset", subset.path().to_str().unwrap()],
        );
            assert_preserved_entry_output(&fixture, "42\n");
            assert_module_source(
                &fixture.out_root,
                "static/app/modules/features/run.js",
                &["function run", "console.log"],
                &["function wrapper"],
            );
            assert!(
                !fixture
                    .out_root
                    .join("static/app/modules/features/anchor.js")
                    .exists()
            );
            if retain_import {
                assert_module_exports(
                    &fixture.out_root,
                    "static/app/modules/features/provider.js",
                    &["providerSeed"],
                    &["seed"],
                );
            } else {
                assert!(
                    !fixture
                        .out_root
                        .join("static/app/modules/features/provider.js")
                        .exists()
                );
            }
        }
    }
}

#[test]
fn catchall_subset_still_rejects_a_real_nonmonotone_assignment_cycle() {
    let options = || {
        FixtureOpts::new(
        "const first = console.log('first');\nconst middle = console.log('middle');\nconst last = console.log('last');\n",
        vec![logical_module("first", &[Member::new("first")]), logical_module("middle", &[Member::new("middle")]), logical_module("last", &[Member::new("last")])],
    ).with_unassigned_mode(unassigned_mode_catchall_file(None))
    };
    let full = run_fixture(options());
    assert_preserved_entry_output(&full, "first\nmiddle\nlast\n");
    let subset = write_subset(r#"{"modules":[{"chunk":"static/app","module":"middle"}]}"#);
    let rejected = run_rejection_fixture_without_graphs_with_args(
        options(),
        &["--ownership-subset", subset.path().to_str().unwrap()],
        1,
    );
    assert!(
        rejected.stderr.contains("unrealizable")
            || rejected.stderr.contains("atomic-group conflict"),
        "{}",
        rejected.stderr
    );
}

#[test]
fn ownership_subset_selects_anonymous_claims_by_authored_index() {
    let options = || {
        FixtureOpts::new(
            r#"const first = console.log("first");
console.log("claimed early");
const middle = console.log("middle");
console.log("claimed late");
export const styles = (console.log("styles"), {});
"#,
            vec![logical_module_with_anon(
                "features/styles",
                &[Member::new("styles")],
                &[
                    "console.log(\"claimed early\");",
                    "console.log(\"claimed late\");",
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_catchall_file(None))
    };

    let retain_all =
        write_subset(r#"{"modules":[{"chunk":"static/app","module":"features/styles"}]}"#);
    let rejected = run_rejection_fixture_without_graphs_with_args(
        options(),
        &["--ownership-subset", retain_all.path().to_str().unwrap()],
        1,
    );
    assert!(
        rejected.stderr.contains("unrealizable")
            || rejected.stderr.contains("atomic-group conflict"),
        "{}",
        rejected.stderr
    );

    let retain_late = write_subset(
        r#"{"modules":[{"chunk":"static/app","module":"features/styles","anonymous_statements":[1]}]}"#,
    );
    let fixture = run_fixture_without_graphs_with_args(
        options(),
        &["--ownership-subset", retain_late.path().to_str().unwrap()],
    );
    assert_entry_output(
        &fixture,
        "first\nclaimed early\nmiddle\nclaimed late\nstyles\n",
    );
    let report: serde_json::Value = read_json(&fixture.report_root.join("static/app/modules.json"));
    assert_eq!(
        report["ownership_subset"][0]["anonymous_statements"],
        serde_json::json!([1])
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/features/styles.js",
        &["claimed late", "const styles"],
        &["claimed early", "const middle"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/residual/unhandled.js",
        &["const first", "claimed early", "const middle"],
        &["claimed late", "const styles"],
    );
}

#[test]
fn ownership_subset_anonymous_index_controls_adopted_declarations() {
    let options = || {
        FixtureOpts::new(
            "if (true) { var adopted = 41; }\nexport const selected = \"selected\";\nconsole.log(selected, adopted);\n",
            vec![logical_module_with_anon(
                "features/owner",
                &[Member::new("selected")],
                &["if (true) { var adopted = 41; }"]
            )],
        )
        .with_unassigned_mode(unassigned_mode_catchall_file(None))
    };

    let retain_statement = write_subset(
        r#"{"modules":[{"chunk":"static/app","module":"features/owner","bindings":["selected"],"anonymous_statements":[0]}]}"#,
    );
    let retained = run_fixture_without_graphs_with_args(
        options(),
        &[
            "--ownership-subset",
            retain_statement.path().to_str().unwrap(),
        ],
    );
    assert_entry_output(&retained, "selected 41\n");
    assert_module_exports(
        &retained.out_root,
        "static/app/modules/features/owner.js",
        &["selected", "adopted"],
        &[],
    );
    assert_module_source(
        &retained.out_root,
        "static/app/modules/features/owner.js",
        &["if (true)", "var adopted"],
        &[],
    );

    let release_statement = write_subset(
        r#"{"modules":[{"chunk":"static/app","module":"features/owner","bindings":["selected"],"anonymous_statements":[]}]}"#,
    );
    let released = run_fixture_without_graphs_with_args(
        options(),
        &[
            "--ownership-subset",
            release_statement.path().to_str().unwrap(),
        ],
    );
    assert_entry_output(&released, "selected 41\n");
    assert_module_exports(
        &released.out_root,
        "static/app/modules/features/owner.js",
        &["selected"],
        &["adopted"],
    );
    assert_module_source(
        &released.out_root,
        "static/app/modules/features/owner.js",
        &["selected"],
        &["if (true)", "adopted"],
    );
    let report: serde_json::Value =
        read_json(&released.report_root.join("static/app/modules.json"));
    assert_eq!(
        report["ownership_subset"][0]["anonymous_statements"],
        serde_json::json!([])
    );
    assert_module_exports(
        &released.out_root,
        "static/app/modules/residual/unhandled.js",
        &["adopted"],
        &["selected"],
    );
    assert_module_source(
        &released.out_root,
        "static/app/modules/residual/unhandled.js",
        &["if (true)", "var adopted"],
        &["const selected"],
    );
}

#[test]
fn ownership_subset_rejects_invalid_anonymous_indices_without_hiding_full_selectors() {
    let options = || {
        FixtureOpts::new(
            "console.log(\"present\");\nexport const selected = 1;\n",
            vec![logical_module_with_anon(
                "features/owner",
                &[Member::new("selected")],
                &["console.log(\"present\");"],
            )],
        )
        .with_unassigned_mode(unassigned_mode_catchall_file(None))
    };

    for (indices, expected_error) in [("[0,0]", "repeats"), ("[1]", "unknown")] {
        let subset = write_subset(&format!(
            r#"{{"modules":[{{"chunk":"static/app","module":"features/owner","anonymous_statements":{indices}}}]}}"#
        ));
        let rejected = run_rejection_fixture_without_graphs_with_args(
            options(),
            &["--ownership-subset", subset.path().to_str().unwrap()],
            1,
        );
        assert!(
            rejected.stderr.contains(expected_error),
            "expected {expected_error:?} in:\n{}",
            rejected.stderr
        );
    }

    let invalid_full_spec = FixtureOpts::new(
        "console.log(\"present\");\nexport const selected = 1;\n",
        vec![logical_module_with_anon(
            "features/owner",
            &[Member::new("selected")],
            &["console.log(\"present\");", "console.log(\"missing\");"],
        )],
    )
    .with_unassigned_mode(unassigned_mode_catchall_file(None));
    let subset = write_subset(
        r#"{"modules":[{"chunk":"static/app","module":"features/owner","anonymous_statements":[0]}]}"#,
    );
    let rejected = run_rejection_fixture_without_graphs_with_args(
        invalid_full_spec,
        &["--ownership-subset", subset.path().to_str().unwrap()],
        1,
    );
    assert!(rejected.stderr.contains("selector"), "{}", rejected.stderr);
}

#[test]
fn ownership_subset_still_rejects_full_graph_export() {
    let subset = write_subset(r#"{"modules":[]}"#);
    let subset_path = subset.path().to_str().unwrap();
    let rejected =
        run_rejection_fixture_with_args(subset_spec(), &["--ownership-subset", subset_path], 2);
    assert!(
        rejected.stderr.contains("cannot be used with") && rejected.stderr.contains("--graphs"),
        "{}",
        rejected.stderr
    );
}
