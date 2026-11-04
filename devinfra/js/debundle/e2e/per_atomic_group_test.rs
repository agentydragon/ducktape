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
    let subset = write_subset(r#"{"modules":[{"chunk":"static/app","module":"features/public"}]}"#);
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
    .with_unassigned_mode(unassigned_mode_per_atomic_group());
    let rejected = run_rejection_fixture_without_graphs_with_args(
        invalid_selector,
        &["--ownership-subset", subset_path],
        1,
    );
    assert!(rejected.stderr.contains("selector"), "{}", rejected.stderr);

    let duplicate_claim = FixtureOpts::new(
        SUBSET_SOURCE,
        vec![
            logical_module("features/public", &[Member::new("originalName")]),
            logical_module("features/duplicate", &[Member::new("originalName")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_per_atomic_group());
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
    .with_unassigned_mode(unassigned_mode_per_atomic_group())
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

#[test]
fn ownership_subset_still_rejects_catchall_file() {
    let subset = write_subset(r#"{"modules":[{"chunk":"static/app","module":"features/public"}]}"#);
    let subset_path = subset.path().to_str().unwrap();
    let rejected = run_rejection_fixture_without_graphs_with_args(
        subset_spec().with_unassigned_mode(unassigned_mode_catchall_file(None)),
        &["--ownership-subset", subset_path],
        1,
    );
    assert!(
        rejected
            .stderr
            .contains("inline_in_entry or per_atomic_group"),
        "{}",
        rejected.stderr
    );
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
