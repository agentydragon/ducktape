//! Translation validation (migration step 1) against the realizability gate
//! on the real pipeline. Every e2e test also runs with `DEBUNDLE_TV_ORACLE`,
//! which fails the run when the two verdicts disagree; these fixtures pin the
//! evidence each side reports.

use debundle_e2e_support::*;
use serde_json::Value;

fn edges(report: &Value, key: &str) -> usize {
    report[key]["total"]
        .as_u64()
        .unwrap_or_else(|| panic!("{key} must have an exact total: {report:#}")) as usize
}

#[test]
fn backward_reads_in_source_order_swap_nothing() {
    let fixture = run_fixture(FixtureOpts::new(
        "const a = 1;\nconst b = a + 1;\nconsole.log(b);\nexport { a, b };\n",
        vec![logical_module("mod_a", &[Member::new("a")])],
    ));
    assert_preserved_entry_output(&fixture, "2\n");
    let report = fixture.translation_validation();
    assert_eq!(report["gate_accepts"], true, "{report:#}");
    assert_eq!(report["accepts"], true, "{report:#}");
    assert_eq!(report["swapped_pairs"], 0, "{report:#}");
}

#[test]
fn constraining_module_cycle_is_a_swapped_conflict() {
    let report = run_rejection_fixture(FixtureOpts::new(
        "const a = 1;\nconst b = a + 1;\nconst c = b + 1;\nconsole.log(c);\nexport { a, b, c };\n",
        vec![
            logical_module("mod_x", &[Member::new("a"), Member::new("c")]),
            logical_module("mod_y", &[Member::new("b")]),
        ],
    ))
    .translation_validation();
    assert_eq!(report["gate_accepts"], false, "{report:#}");
    assert_eq!(report["accepts"], false, "{report:#}");
    let conflicts = report["conflicts"]["examples"]
        .as_array()
        .expect("conflicts");
    assert!(
        conflicts
            .iter()
            .any(|conflict| conflict["kind"] == "eager_use"
                && conflict["runs_first"]["unit"].as_u64()
                    > conflict["runs_later"]["unit"].as_u64()),
        "{report:#}"
    );
}

/// A read of a later `var` sees `undefined` in the source. The read is a
/// forward edge and the declaration's timeline edge points back at it, so
/// both verdicts keep the pair in one module.
#[test]
fn forward_var_read_split_is_rejected() {
    for source in [
        "const seen = String(later);\nvar later = \"set\";\nconsole.log(seen);\nexport { seen, later };\n",
        "const seen = readLater();\nfunction readLater() { return String(later); }\nvar later = \"set\";\nconsole.log(seen);\nexport { seen, readLater, later };\n",
    ] {
        let report = run_rejection_fixture(FixtureOpts::new(
            source,
            vec![
                logical_module("reader", &[Member::new("seen")]),
                logical_module("values", &[Member::new("later")]),
            ],
        ))
        .translation_validation();
        assert_eq!(report["gate_accepts"], false, "{report:#}");
        assert_eq!(report["accepts"], false, "{report:#}");
        assert_eq!(edges(&report, "placement_splits"), 0, "{report:#}");
    }
}

/// The at-init call reads `value` before its declaration in the source too.
/// The emit keeps that order, so no pair swaps; the forward edge counts as
/// violated for the gate and for step 1, though swapped-pair semantics would
/// accept this order.
#[test]
fn forward_at_init_read_kept_in_source_order_is_reported() {
    let report = run_rejection_fixture(FixtureOpts::new(
        "const seed = 7;\nconst result = readValue(seed);\nfunction readValue(x) { return value + x; }\nconst value = 42;\nconsole.log(result);\nexport { seed, result, readValue, value };\n",
        vec![
            logical_module("readers", &[Member::new("readValue")]),
            logical_module("values", &[Member::new("value")]),
        ],
    ))
    .translation_validation();
    assert_eq!(report["gate_accepts"], false, "{report:#}");
    assert_eq!(report["accepts"], false, "{report:#}");
    assert_eq!(edges(&report, "conflicts"), 0, "{report:#}");
    assert_eq!(edges(&report, "forward_edges_kept"), 1, "{report:#}");
}

/// The entry's import of `vendor.js` loses its only specifier once `cx()`
/// moves to `b_module`, which imports `vendor.js` itself. The entry requests
/// `vendor.js` before `z_module`, so dropping that request would log `z`
/// before `vendor`; the pruned import stays bare and the order holds.
#[test]
fn pruned_import_keeps_its_load_order() {
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { f as cx } from \"./vendor.js\";\nconst z = (console.log(\"z\"), 1);\nconst b = cx();\nconsole.log(z + b);\nexport { z, b };\n",
            vec![
                logical_module("b_module", &[Member::new("b")]),
                logical_module("z_module", &[Member::new("z")]),
            ],
        )
        .with_extra_files(&[(
            "static/app/vendor.js",
            "console.log(\"vendor\");\nexport function f() { return 1; }\n",
        )]),
    );
    assert_entry_output(&fixture, "vendor\nz\n2\n");
}

#[test]
fn bounded_conflict_examples_preserve_all_checks_and_complete_export() {
    let mut source = String::from("const origin = 1;\n");
    let names = [
        "item0", "item1", "item2", "item3", "item4", "item5", "item6", "item7", "item8", "item9",
        "item10", "item11", "item12", "item13", "item14", "item15", "item16", "item17", "item18",
        "item19", "item20", "item21", "item22", "item23", "item24", "item25", "item26", "item27",
        "item28", "item29", "item30", "item31",
    ];
    let mut modules = vec![logical_module(
        "outer",
        &[Member::new("origin"), Member::new("tail")],
    )];
    for (index, name) in names.iter().enumerate() {
        source.push_str(&format!("const {name} = origin + {index};\n"));
        modules.push(logical_module(name, &[Member::new(name)]));
    }
    source.push_str(&format!(
        "const tail = {};\nconsole.log(tail);\n",
        names.join(" + ")
    ));
    let default = run_rejection_fixture(FixtureOpts::new(&source, modules.clone()));
    let summary = default.translation_validation();
    assert_eq!(summary["accepts"], false);
    assert_eq!(summary["gate_accepts"], false);
    assert!(
        summary["conflicts"]
            .as_array()
            .is_none_or(|rows| rows.len() <= 20),
        "default report retained more than20 full conflict rows"
    );
    assert!(edges(&summary, "conflicts") > 20, "{summary:#}");
    assert_eq!(
        summary["conflicts"]["examples"].as_array().unwrap().len(),
        20
    );
    assert_eq!(summary["conflicts"]["truncated"], true);
    assert!(
        !default
            .report_path("translation_validation.full.jsonl")
            .exists()
    );
    let full = run_rejection_fixture_with_args(
        FixtureOpts::new(&source, modules),
        &["--full-report", "translation-validation"],
        1,
    );
    assert_eq!(summary, full.translation_validation());
    let rows = read_jsonl(&full.report_path("translation_validation.full.jsonl"));
    let mut pairs = std::collections::BTreeSet::new();
    let mut kinds = std::collections::BTreeMap::<String, usize>::new();
    for (category, field) in [
        ("conflict", "conflicts"),
        ("forward_edge_kept", "forward_edges_kept"),
        ("forward_edge_swapped", "forward_edges_swapped"),
        ("placement_split", "placement_splits"),
    ] {
        let selected: Vec<_> = rows
            .iter()
            .filter(|row| row["category"] == category)
            .collect();
        assert_eq!(selected.len(), edges(&summary, field));
        assert_eq!(
            summary["counts"][field].as_u64().unwrap() as usize,
            selected.len()
        );
        for (example, row) in summary[field]["examples"]
            .as_array()
            .unwrap()
            .iter()
            .zip(&selected)
        {
            if category == "conflict" {
                let mut row = (*row).clone();
                row.as_object_mut().unwrap().remove("category");
                assert_eq!(*example, row);
            } else {
                assert_eq!(*example, row["dependency_edge"]);
            }
        }
        if category == "conflict" {
            for row in selected {
                pairs.insert((
                    row["runs_first"]["unit"].as_u64().unwrap(),
                    row["runs_later"]["unit"].as_u64().unwrap(),
                ));
                *kinds
                    .entry(row["kind"].as_str().unwrap().to_string())
                    .or_default() += 1;
            }
        }
    }
    assert_eq!(
        summary["counts"]["conflicts_by_kind"],
        serde_json::to_value(kinds).unwrap()
    );
    // This comparison must include conflicts after the first20 examples.
    let effects = full.effect_translation_validation();
    assert_eq!(
        pairs.len() as u64,
        effects["counts"]["both_pairs"].as_u64().unwrap()
            + effects["counts"]["legacy_edge_only_pairs"]
                .as_u64()
                .unwrap()
    );
}

#[test]
fn accepted_full_report_is_explicit_and_does_not_change_runtime() {
    let fixture = run_fixture_with_args(
        FixtureOpts::new("export const value = 7;\nconsole.log(value);\n", vec![]),
        &[
            "--full-report",
            "translation-validation",
            "--full-report-chunk",
            "static/app",
        ],
    );
    assert_entry_output(&fixture, "7\n");
    assert_eq!(fixture.translation_validation()["accepts"], true);
    assert!(
        read_jsonl(
            &fixture
                .report_root
                .join("static/app/translation_validation.full.jsonl")
        )
        .is_empty()
    );
}

#[test]
fn full_report_selection_errors_remain_failures() {
    let fixture =
        write_validate_fixture_spec(FixtureOpts::new("export const value = 7;\n", vec![]));
    for (flags, diagnostic) in [
        (vec!["--full-report-chunk", "static/app"], "--full-report"),
        (vec!["--full-report", "unknown"], "unknown full report"),
        (
            vec![
                "--full-report",
                "translation-validation",
                "--full-report-chunk",
                "static/typo",
            ],
            "unknown --full-report-chunk",
        ),
        (
            vec![
                "--full-report",
                "translation-validation",
                "--full-report-chunk",
                "../app",
            ],
            "Invalid relative path",
        ),
        (
            vec!["--full-report", "translation-validation", "--dry-run"],
            "report policy suppresses output",
        ),
    ] {
        let mut args = vec!["run", "--spec", fixture.spec_path.to_str().unwrap()];
        args.extend(flags);
        let result = run_debundle(&args);
        assert!(!result.status.success(), "{result:?}");
        assert!(
            String::from_utf8_lossy(&result.stderr).contains(diagnostic),
            "{result:?}"
        );
    }
}

#[test]
fn invalid_spec_still_fails_with_reading_only_and_full_report_flags() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new(
        "export const value = 7;\n",
        vec![logical_module("missing", &[Member::new("absent")])],
    ));
    let result = run_debundle(&[
        "run",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
        "--emit-on-rejection",
        "--full-report",
        "translation-validation",
    ]);
    assert_eq!(result.status.code(), Some(1), "{result:?}");
    assert!(
        !fixture
            .spec_path
            .parent()
            .unwrap()
            .join("out/GATE_REJECTED")
            .exists()
    );
}

#[test]
fn invocation_record_retains_full_report_sink_selection_without_claiming_execution() {
    let fixture =
        write_validate_fixture_spec(FixtureOpts::new("export const value = 7;\n", vec![]));
    let result = run_debundle(&[
        "--print-invocation",
        "run",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
        "--full-report",
        "translation-validation",
        "--full-report-chunk",
        "static/app",
    ]);
    assert!(result.status.success(), "{result:?}");
    let record: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(record["will_execute"], false);
    let argv = record["argv"].as_array().unwrap();
    for pair in [
        ["--full-report", "translation-validation"],
        ["--full-report-chunk", "static/app"],
    ] {
        assert!(
            argv.windows(2)
                .any(|items| items[0] == pair[0] && items[1] == pair[1]),
            "{record:#}"
        );
    }
}
