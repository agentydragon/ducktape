//! `coarse_atomic_groups` merges wholly-unclaimed atomic groups that share
//! strict constraining reachability to existing owners within fine evaluation
//! intervals when their public export names have compatible origins. The
//! normal gate and Node execution still decide whether the final placement is
//! valid.

use debundle_e2e_support::*;
use std::path::{Path, PathBuf};

const ORDER_SOURCE: &str = r#"globalThis.trace = [];
export const left = (globalThis.trace.push("left"), 1);
globalThis.trace.push("anonymous");
export const right = (globalThis.trace.push("right"), 2);
export const anchor = (globalThis.trace.push("anchor"), 3);
console.log(globalThis.trace.join("|"));
"#;

fn ordering_fixture(mode: spec::UnassignedMode) -> Fixture {
    run_fixture(
        FixtureOpts::new(
            ORDER_SOURCE,
            vec![logical_module("features/anchor", &[Member::new("anchor")])],
        )
        .with_unassigned_mode(mode),
    )
}

fn js_files_under(out_root: &Path, directory: &str) -> Vec<PathBuf> {
    let mut files: Vec<_> = std::fs::read_dir(out_root.join(directory))
        .unwrap_or_else(|error| panic!("read {directory}: {error}"))
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|extension| extension == "js"))
        .collect();
    files.sort();
    files
}

fn module_containing(out_root: &Path, directory: &str, needle: &str) -> PathBuf {
    let matches: Vec<_> = js_files_under(out_root, directory)
        .into_iter()
        .filter(|path| std::fs::read_to_string(path).unwrap().contains(needle))
        .collect();
    assert_eq!(
        matches.len(),
        1,
        "expected one module under {directory} containing {needle:?}; found {matches:?}"
    );
    matches.into_iter().next().unwrap()
}

#[test]
fn coarse_residuals_reduce_files_and_preserve_named_anonymous_placement_and_order() {
    let fine = ordering_fixture(unassigned_mode_per_atomic_group());
    let coarse = ordering_fixture(unassigned_mode_coarse_atomic_groups());
    let fine_files = js_files_under(&fine.out_root, "static/app/modules/__auto/atomic_groups");
    let coarse_files = js_files_under(
        &coarse.out_root,
        "static/app/modules/__auto/residual_groups",
    );
    assert!(
        coarse_files.len() < fine_files.len(),
        "coarse routing should reduce residual files: fine={fine_files:?}, coarse={coarse_files:?}"
    );

    assert_preserved_entry_output(&fine, "left|anonymous|right|anchor\n");
    assert_preserved_entry_output(&coarse, "left|anonymous|right|anchor\n");
    let fine_authored =
        std::fs::read_to_string(fine.out_root.join("static/app/modules/features/anchor.js"))
            .expect("read fine authored anchor module");
    let authored = std::fs::read_to_string(
        coarse
            .out_root
            .join("static/app/modules/features/anchor.js"),
    )
    .expect("read authored anchor module");
    assert_export_named_specifiers(&fine_authored, "anchor", &[None]);
    assert_export_named_specifiers(&fine_authored, "left", &[]);
    assert_export_named_specifiers(&fine_authored, "right", &[]);
    assert_export_named_specifiers(&authored, "anchor", &[None]);
    assert_export_named_specifiers(&authored, "left", &[]);
    assert_export_named_specifiers(&authored, "right", &[]);

    let fine_left_module = module_containing(
        &fine.out_root,
        "static/app/modules/__auto/atomic_groups",
        "const left",
    );
    let fine_right_module = module_containing(
        &fine.out_root,
        "static/app/modules/__auto/atomic_groups",
        "const right",
    );
    assert_export_named_specifiers(
        &std::fs::read_to_string(fine_left_module).unwrap(),
        "left",
        &[None],
    );
    assert_export_named_specifiers(
        &std::fs::read_to_string(fine_right_module).unwrap(),
        "right",
        &[None],
    );

    let left_module = module_containing(
        &coarse.out_root,
        "static/app/modules/__auto/residual_groups",
        "const left",
    );
    let right_module = module_containing(
        &coarse.out_root,
        "static/app/modules/__auto/residual_groups",
        "const right",
    );
    let left_source = std::fs::read_to_string(&left_module).unwrap();
    let right_source = std::fs::read_to_string(&right_module).unwrap();
    assert_export_named_specifiers(&left_source, "left", &[None]);
    assert_export_named_specifiers(&right_source, "right", &[None]);
    assert_export_named_specifiers(&left_source, "anchor", &[]);
    assert_export_named_specifiers(&right_source, "anchor", &[]);
    assert_eq!(
        left_module, right_module,
        "equal strict-owner signatures share one generated residual group"
    );
    let anonymous_module = module_containing(
        &coarse.out_root,
        "static/app/modules/__auto/residual_groups",
        "trace.push(\"anonymous\")",
    );
    assert!(
        coarse_files.contains(&anonymous_module),
        "the anonymous statement must be placed in a generated residual module"
    );
}

#[test]
fn coarse_residuals_split_conflicting_origins_for_one_public_export_name() {
    let source = r#"const a = 1;
export { a as Q };
const Q = 2;
console.log(Q);
"#;
    let fixture = |mode| run_fixture(FixtureOpts::new(source, vec![]).with_unassigned_mode(mode));
    let fine = fixture(unassigned_mode_per_atomic_group());
    let coarse = fixture(unassigned_mode_coarse_atomic_groups());
    assert_preserved_entry_output(&fine, "2\n");
    assert_preserved_entry_output(&coarse, "2\n");

    let directory = "static/app/modules/__auto/residual_groups";
    let alias_module = module_containing(&coarse.out_root, directory, "export { a as Q }");
    let binding_module = module_containing(&coarse.out_root, directory, "const Q = 2;");
    assert_ne!(
        alias_module, binding_module,
        "coarse merging must keep distinct origins for public export `Q` apart"
    );
    assert_export_named_specifiers(
        &std::fs::read_to_string(alias_module).unwrap(),
        "a",
        &[Some("Q"), None],
    );
    assert_export_named_specifiers(
        &std::fs::read_to_string(binding_module).unwrap(),
        "Q",
        &[None],
    );
    assert_module_exports(&coarse.out_root, "static/app/entry.js", &["Q"], &[]);
    assert_generated_module_after_entry_script(
        &coarse,
        "const mod = await import('./static/app/entry.js');\nconsole.log(mod.Q);\n",
        "1\n",
    );

    let coarse_files = js_files_under(&coarse.out_root, directory);
    let fine_files = js_files_under(&fine.out_root, "static/app/modules/__auto/atomic_groups");
    assert!(
        coarse_files.len() < fine_files.len(),
        "coarse routing should still merge export-compatible groups: coarse={coarse_files:?}, fine={fine_files:?}"
    );
}

const ASYMMETRIC_SOURCE: &str = r#"const readNamed = () => namedReader;
const namedReader = readNamed;
const residualAlias = namedReader;
console.log(residualAlias() === readNamed);
export { readNamed, namedReader, residualAlias };
"#;

#[test]
fn coarse_residuals_keep_fixed_evaluations_between_groups() {
    let source = r#"export const before = 1;
export const anchor = before + 1;
export const after = () => anchor;
console.log([before, anchor, after()].join("|"));
"#;
    let fixture = |mode| {
        run_fixture(
            FixtureOpts::new(
                source,
                vec![logical_module("features/anchor", &[Member::new("anchor")])],
            )
            .with_unassigned_mode(mode),
        )
    };
    let fine = fixture(unassigned_mode_per_atomic_group());
    let coarse = fixture(unassigned_mode_coarse_atomic_groups());
    assert_preserved_entry_output(&fine, "1|2|2\n");
    assert_preserved_entry_output(&coarse, "1|2|2\n");
    let directory = "static/app/modules/__auto/residual_groups";
    let before = module_containing(&coarse.out_root, directory, "const before");
    let after = module_containing(&coarse.out_root, directory, "const after");
    assert_ne!(
        before, after,
        "the fixed anchor evaluates between these equal-signature residual groups"
    );
}

fn asymmetric_fixture(mode: spec::UnassignedMode) -> FixtureOpts<'static> {
    FixtureOpts::new(
        ASYMMETRIC_SOURCE,
        vec![logical_module(
            "features/named_reader",
            &[Member::new("namedReader")],
        )],
    )
    .with_unassigned_mode(mode)
}

#[test]
fn evaluation_intervals_preserve_asymmetric_lazy_eager_execution() {
    let fine = run_fixture(asymmetric_fixture(unassigned_mode_per_atomic_group()));
    let coarse = run_fixture(asymmetric_fixture(unassigned_mode_coarse_atomic_groups()));
    assert_preserved_entry_output(&fine, "true\n");
    assert_preserved_entry_output(&coarse, "true\n");
    let directory = "static/app/modules/__auto/residual_groups";
    let read_named = module_containing(&coarse.out_root, directory, "const readNamed");
    let residual_alias = module_containing(&coarse.out_root, directory, "const residualAlias");
    assert_ne!(read_named, residual_alias);
}

#[test]
fn coarse_routing_keeps_the_normal_gate_for_incompatible_named_claims() {
    let source = r#"export const early = 1;
export const middle = early + 1;
export const late = middle + 1;
console.log(late);
"#;
    let rejected = run_emit_on_rejection_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "features/__auto/residual_groups/endpoints",
                &[Member::new("early"), Member::new("late")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_coarse_atomic_groups()),
    );
    let cycles = rejected.cycles();
    assert!(
        cycles
            .iter()
            .any(|cycle| cycle["rejection"] == "mutual_constraining_cycle"),
        "the final normal gate must reject the incompatible named boundary: {cycles:#?}"
    );
    let mixed_authored_and_generated_scc = cycles.iter().any(|cycle| {
        let Some(modules) = cycle["modules"].as_array() else {
            return false;
        };
        let has_path_lookalike_authored_module = modules.iter().any(|module| {
            module
                .as_str()
                .is_some_and(|path| path == "features/__auto/residual_groups/endpoints")
        });
        let has_generated_residual = modules.iter().any(|module| {
            module
                .as_str()
                .is_some_and(|path| path == "__auto/residual_groups/0000")
        });
        has_path_lookalike_authored_module && has_generated_residual
    });
    assert!(
        mixed_authored_and_generated_scc,
        "the authored path that resembles generated output must stay a distinct SCC member: {cycles:#?}"
    );
    assert_gate_rejected_banner(&rejected.entry_path(), &rejected.report_path("cycles.json"));
}

#[test]
fn coarse_generated_scc_contraction_repairs_interleaved_evaluation_intervals() {
    // The generated reader/trigger chain makes the fine ESM DFS enter
    // reader -> first -> anchor -> middle -> first. Middle and its anonymous
    // predecessor evaluate before anchor; first and last evaluate after it.
    // The effectful residuals share strict-owner signature {anchor}, but their
    // two evaluation intervals interleave along the source S-chain. Their coarse
    // quotient therefore needs an actual generated-only SCC merge. Reader and
    // trigger have empty strict signatures and remain outside that contraction.
    let source = r#"const reader = () => first;
const trigger = reader;
const anchor = () => middle;
const first = (console.log("first"), anchor);
console.log("between");
const middle = (console.log("middle"), 1);
const last = (console.log("last"), 2);
console.log([trigger() === first, first(), middle, last].join("|"));
"#;
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("features/anchor", &[Member::new("anchor")])],
        )
        .with_unassigned_mode(unassigned_mode_coarse_atomic_groups()),
    );
    assert_preserved_entry_output(&fixture, "first\nbetween\nmiddle\nlast\ntrue|1|1|2\n");
    let residual = module_containing(
        &fixture.out_root,
        "static/app/modules/__auto/residual_groups",
        "const first",
    );
    let residual_relative = residual
        .strip_prefix(&fixture.out_root)
        .expect("generated residual module remains under the app root")
        .to_string_lossy()
        .into_owned();
    assert_eq!(
        residual,
        module_containing(
            &fixture.out_root,
            "static/app/modules/__auto/residual_groups",
            "console.log(\"between\")",
        ),
        "the anonymous statement from the residual SCC must remain with its merged members"
    );
    assert_module_source(
        &fixture.out_root,
        &residual_relative,
        &[
            "const first",
            "console.log(\"between\")",
            "const middle",
            "const last",
        ],
        &[],
    );
    let residual_source = std::fs::read_to_string(residual).unwrap();
    for name in ["first", "middle"] {
        assert_export_named_specifiers(&residual_source, name, &[None]);
    }
    for name in ["last", "anchor"] {
        assert_export_named_specifiers(&residual_source, name, &[]);
    }
}
