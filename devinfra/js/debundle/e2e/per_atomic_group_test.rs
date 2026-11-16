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
    let fixture =
        run_fixture(FixtureOpts::new(FIXTURE_SOURCE, vec![]).with_import_order(&["residual"]));
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
            .with_unassigned_mode(unassigned_mode_per_atomic_group())
            .with_import_order(&["__auto/atomic_groups/0000", "__auto/atomic_groups/0001"]),
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
    let mut opts = FixtureOpts::new("import { a } from './dep.js'; console.log(a);", vec![])
        .with_unassigned_mode(unassigned_mode_per_atomic_group())
        .with_import_order(&["__auto/atomic_groups/0000", "__auto/atomic_groups/0001"]);
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
    let fixture = run_fixture_with_args(
        subset_spec().with_import_order(&[
            "__auto/atomic_groups/0000",
            "__auto/atomic_groups/0001",
            "__auto/atomic_groups/0002",
            "__auto/atomic_groups/0003",
            "__auto/atomic_groups/0004",
            "features/public",
        ]),
        &["--ownership-subset", subset_path],
    );

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
    let fixture = run_fixture_with_args(
        subset_spec().with_import_order(&[
            "__auto/atomic_groups/0000",
            "__auto/atomic_groups/0001",
            "__auto/atomic_groups/0002",
            "__auto/atomic_groups/0003",
            "__auto/atomic_groups/0004",
            "__auto/atomic_groups/0005",
        ]),
        &["--ownership-subset", subset_path],
    );

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
        let rejected = run_rejection_fixture_with_args(
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
        let rejected = run_rejection_fixture_with_args(
            duplicate_claim,
            &["--ownership-subset", subset_path],
            1,
        );
        assert!(rejected.stderr.contains("duplicate"), "{}", rejected.stderr);

        let premise =
            serde_yaml::from_str("intrinsics_never_written:\n  families: [map]\n  note: audited\n")
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
        let rejected =
            run_rejection_fixture_with_args(contradiction, &["--ownership-subset", subset_path], 1);
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
            let priority: &[&str] = match (&mode, selected) {
                (spec::UnassignedMode::InlineInEntry, false) => &[],
                (spec::UnassignedMode::InlineInEntry, true) => &["features/public"],
                (spec::UnassignedMode::CatchallFile { .. }, false) => &["residual"],
                (spec::UnassignedMode::CatchallFile { .. }, true) => {
                    &["features/public", "residual"]
                }
                (spec::UnassignedMode::PerAtomicGroup, false) => &[
                    "__auto/atomic_groups/0000",
                    "__auto/atomic_groups/0001",
                    "__auto/atomic_groups/0002",
                    "__auto/atomic_groups/0003",
                    "__auto/atomic_groups/0004",
                    "__auto/atomic_groups/0005",
                ],
                (spec::UnassignedMode::PerAtomicGroup, true) => &[
                    "__auto/atomic_groups/0000",
                    "__auto/atomic_groups/0001",
                    "__auto/atomic_groups/0002",
                    "__auto/atomic_groups/0003",
                    "__auto/atomic_groups/0004",
                    "features/public",
                ],
                (spec::UnassignedMode::CoarseAtomicGroups { .. }, false) => {
                    &["__auto/residual_groups/0000"]
                }
                (spec::UnassignedMode::CoarseAtomicGroups { .. }, true) => &[
                    "__auto/residual_groups/0000",
                    "__auto/residual_groups/0001",
                    "features/public",
                ],
            };
            let fixture = run_fixture_with_args(
                subset_spec()
                    .with_unassigned_mode(mode.clone())
                    .with_import_order(priority),
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
        let priority: &[&str] = match selected {
            "features/public" => &["features/public", "residual/unhandled"],
            "residual/unhandled" | "" => &["residual/unhandled"],
            _ => unreachable!(),
        };
        let fixture = run_fixture_with_args(
            FixtureOpts::new(
                "export const first = 1;\nexport const omitted = 2;\nconsole.log(first, omitted);\n",
                vec![logical_module("features/public", &[Member::new("first")]), logical_module("residual/unhandled", &[Member::renamed("renamed", "omitted")])],
            )
            .with_unassigned_mode(unassigned_mode_catchall_file(None))
            .with_import_order(priority),
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
            let opts = FixtureOpts::new(
                "import { seed } from './peer.js';\nfunction base(value) { return value + seed; }\nfunction wrapper(value) { return base(value); }\nconsole.log(wrapper(1));\nexport { wrapper };\n",
                vec![
                    logical_module("features/anchor", &[Member::source_alpha("anchor", "function $fn($value) { return $value + seed; }")]),
                    logical_module("features/provider", &[Member::renamed_with_kind("providerSeed", "seed", Some("import_specifier"))]),
                    logical_module_with_anon("features/run", &[Member::cross_ref_references("run", "anchor", Some("function_declaration"))], &["console.log(wrapper(1));"]),
                ],
            )
            .with_extra_chunks(&[("static/peer", "export const seed = 41;\n")])
            .with_unassigned_mode(mode.clone())
            .with_import_order(match (&mode, retain_import) {
                (spec::UnassignedMode::InlineInEntry, false) => &["features/run"],
                (spec::UnassignedMode::InlineInEntry, true) => {
                    &["features/provider", "features/run"]
                }
                (spec::UnassignedMode::CatchallFile { .. }, false) => {
                    &["features/run", "residual"]
                }
                (spec::UnassignedMode::CatchallFile { .. }, true) => {
                    &["features/provider", "features/run", "residual"]
                }
                (
                    spec::UnassignedMode::PerAtomicGroup,
                    false,
                ) => &[
                    "__auto/atomic_groups/0000",
                    "__auto/atomic_groups/0001",
                    "__auto/atomic_groups/0002",
                    "features/run",
                ],
                (spec::UnassignedMode::PerAtomicGroup, true) => &[
                    "__auto/atomic_groups/0000",
                    "__auto/atomic_groups/0001",
                    "__auto/atomic_groups/0002",
                    "features/provider",
                    "features/run",
                ],
                (spec::UnassignedMode::CoarseAtomicGroups { .. }, false) => &[
                    "__auto/residual_groups/0000",
                    "__auto/residual_groups/0001",
                    "features/run",
                ],
                (spec::UnassignedMode::CoarseAtomicGroups { .. }, true) => &[
                    "__auto/residual_groups/0000",
                    "features/provider",
                    "features/run",
                    "__auto/residual_groups/0001",
                ],
            });
            let fixture = run_fixture_with_args(
                opts,
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
    let rejected = run_rejection_fixture_with_args(
        options().with_import_order(&["middle", "residual"]),
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
    let rejected = run_rejection_fixture_with_args(
        options().with_import_order(&["features/styles", "residual"]),
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
    let fixture = run_fixture_with_args(
        options().with_import_order(&["features/styles", "residual"]),
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

fn named_subset_spec() -> FixtureOpts<'static> {
    FixtureOpts::new(
        "const keep = 1;\nconst drop = 2;\nconsole.log(keep, drop);\nexport { keep, drop };\n",
        vec![logical_module_with_anon(
            "features/values",
            &[
                Member::source_alpha_target("readableKeep", "keep", "const keep = 1;"),
                Member::source_alpha_target("readableDrop", "drop", "const drop = 2;"),
            ],
            &["console.log(keep, drop);"],
        )],
    )
    .with_unassigned_mode(unassigned_mode_catchall_file(None))
}

#[test]
fn ownership_subset_keeps_only_requested_readable_named_member() {
    let subset = write_subset(
        r#"{"modules":[{"chunk":"static/app","module":"features/values","bindings":["readableKeep"],"anonymous_statements":[]}]}"#,
    );
    let fixture = run_fixture_with_args(
        named_subset_spec().with_unassigned_mode(unassigned_mode_inline()),
        &["--ownership-subset", subset.path().to_str().unwrap()],
    );
    assert_preserved_entry_output(&fixture, "1 2\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/features/values.js",
        &["readableKeep"],
        &["readableDrop", "keep", "drop"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/features/values.js",
        &["const readableKeep"],
        &["readableDrop", "drop", "console.log"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &["const drop", "console.log"],
        &["const keep", "const readableKeep", "readableDrop"],
    );
    assert!(
        !fixture
            .out_root
            .join("static/app/modules/residual")
            .exists(),
        "inline residual bindings must remain in entry.js",
    );
}

#[test]
fn ownership_subset_empty_bindings_keeps_anonymous_claim_but_no_named_claims() {
    for bindings in ["", r#", "bindings": []"#] {
        let subset = write_subset(&format!(
            r#"{{"modules":[{{"chunk":"static/app","module":"features/values","anonymous_statements":[0]{bindings}}}]}}"#,
        ));
        let opts = if bindings.is_empty() {
            named_subset_spec().with_import_order(&["features/values"])
        } else {
            named_subset_spec().with_import_order(&["features/values", "residual"])
        };
        let fixture = run_fixture_with_args(
            opts,
            &["--ownership-subset", subset.path().to_str().unwrap()],
        );
        assert_preserved_entry_output(&fixture, "1 2\n");
        if bindings.is_empty() {
            assert_module_source(
                &fixture.out_root,
                "static/app/modules/features/values.js",
                &["const readableKeep", "const readableDrop", "console.log"],
                &["const keep", "const drop"],
            );
            assert!(
                !fixture
                    .out_root
                    .join("static/app/modules/residual")
                    .exists(),
                "omitting bindings must retain all named claims",
            );
        } else {
            assert_module_source(
                &fixture.out_root,
                "static/app/modules/features/values.js",
                &["console.log"],
                &["const keep", "const drop", "readableKeep", "readableDrop"],
            );
            assert_module_exports(
                &fixture.out_root,
                "static/app/modules/residual/unhandled.js",
                &["keep", "drop"],
                &["readableKeep", "readableDrop"],
            );
            assert_module_source(
                &fixture.out_root,
                "static/app/modules/residual/unhandled.js",
                &["const keep", "const drop"],
                &["console.log", "readableKeep", "readableDrop"],
            );
        }
    }
}

#[test]
fn ownership_subset_rejects_invalid_named_selections_and_records() {
    for (modules, diagnostic) in [
        (
            r#"[{"chunk":"static/app","module":"features/values","bindings":["missing"]}]"#,
            "names unknown binding",
        ),
        (
            r#"[{"chunk":"static/app","module":"features/values","bindings":["keep"]}]"#,
            "names unknown binding",
        ),
        (
            r#"[{"chunk":"static/app","module":"features/values","bindings":["readableKeep","readableKeep"]}]"#,
            "repeats binding",
        ),
        (
            r#"[{"chunk":"static/app","module":"features/missing"}]"#,
            "names unknown module",
        ),
        (
            r#"[{"chunk":"static/missing","module":"features/values"}]"#,
            "names unknown materialized chunk",
        ),
        (
            r#"[{"chunk":"static/app","module":"features/values"},{"chunk":"static/app","module":"features/values"}]"#,
            "repeats module",
        ),
        (
            r#"[{"chunk":"static/app","module":"features/values","binding":["readableKeep"]}]"#,
            "unknown field",
        ),
    ] {
        let subset = write_subset(&format!(r#"{{"modules":{modules}}}"#));
        let rejected = run_rejection_fixture_with_args(
            named_subset_spec(),
            &["--ownership-subset", subset.path().to_str().unwrap()],
            1,
        );
        assert!(
            rejected.stderr.contains(diagnostic),
            "expected {diagnostic:?} for {modules}, got:\n{}",
            rejected.stderr,
        );
        assert!(
            !rejected.out_root.join("app/static/app/entry.js").exists(),
            "invalid ownership selection must not emit a runnable entry",
        );
    }
}

#[test]
fn ownership_subset_named_selection_keeps_destructuring_companions_together() {
    let subset = write_subset(
        r#"{"modules":[{"chunk":"static/app","module":"values","bindings":["readableLeft"]}]}"#,
    );
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            r#"const { left, right } = (() => {
  globalThis.evaluations = (globalThis.evaluations || 0) + 1;
  return { left: 1, right: 2 };
})();
console.log(left, right, globalThis.evaluations);
export { left, right };
"#,
            vec![logical_module(
                "values",
                &[
                    Member::renamed("readableLeft", "left"),
                    Member::renamed("readableRight", "right"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--ownership-subset", subset.path().to_str().unwrap()],
    );
    assert_preserved_entry_output(&fixture, "1 2 1\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values.js",
        &["readableLeft", "right"],
        &["left", "readableRight"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &["console.log"],
        &["const {", "evaluations =", "readableRight"],
    );
}

#[test]
fn ownership_subset_named_selection_keeps_every_shared_var_declaration_site() {
    let subset = write_subset(
        r#"{"modules":[{"chunk":"static/app","module":"values","bindings":["readableShared"]}]}"#,
    );
    let fixture = run_fixture_with_args(
        FixtureOpts::new(
            r#"var shared = (console.log("first"), 1);
var shared = (console.log("second"), 2);
const other = 3;
console.log(shared, other);
export { shared, other };
"#,
            vec![logical_module(
                "values",
                &[
                    Member::renamed("readableShared", "shared"),
                    Member::renamed("readableOther", "other"),
                ],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["--ownership-subset", subset.path().to_str().unwrap()],
    );
    assert_preserved_entry_output(&fixture, "first\nsecond\n2 3\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/values.js",
        &[
            "var readableShared",
            "console.log(\"first\")",
            "console.log(\"second\")",
        ],
        &["const other", "readableOther"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &["const other"],
        &["var shared", "var readableShared", "readableOther"],
    );
    assert_generated_module_after_entry_script(
        &fixture,
        "const { readableShared } = await import('./static/app/modules/values.js');\nconsole.log(readableShared);\n",
        "2\n",
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
    let retained = run_fixture_with_args(
        options().with_import_order(&["features/owner"]),
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
    let released = run_fixture_with_args(
        options().with_import_order(&["features/owner", "residual"]),
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
        let rejected = run_rejection_fixture_with_args(
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
    let rejected = run_rejection_fixture_with_args(
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
    let rejected = run_rejection_fixture_with_graphs_and_args(
        subset_spec(),
        &["--ownership-subset", subset_path],
        2,
    );
    assert!(
        rejected.stderr.contains("cannot be used with") && rejected.stderr.contains("--graphs"),
        "{}",
        rejected.stderr
    );
}
