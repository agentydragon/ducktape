//! E2e for `debundle gate {list,describe,cut}` against REAL pipeline
//! artifacts. A spec with a cross-module at-init cycle is rejected by
//! `debundle run`, which writes `owner_graph.json` + `cycles.json`
//! under the report root; the gate CLI is then exercised against
//! those files.
//!
//! Driving the real materializer (instead of a hand-written JSON
//! pair) pins the cross-file join contract: `gate describe`'s
//! evidence recompute resolves each owner's destination `ModuleKey`
//! through the owner graph's module table to the same canonical
//! `ModulePath` the `cycles.json` `modules` entries carry. A
//! vocabulary drift between the two files (e.g. the historical
//! `"<chunk_id>::<path>"` spelling leaking into `cycles.json`) makes
//! the join come up empty — and fails the non-empty-evidence
//! assertions here.

use std::collections::BTreeSet;
use std::fs;
use std::path::PathBuf;

use debundle_e2e_support::*;

/// A two-module at-init cycle the gate must reject:
/// `mod_x = {A, D}` where unresolved `wrap(C)` reads `C` from `mod_y`,
/// and `mod_y = {B, C}` where unresolved `wrap(A)` reads `A` from
/// `mod_x`. Since B is before D, D's impure sequencing edge points to B
/// in the cut's `mod_x -> mod_y` pair alongside D's at-init read.
fn cycle_fixture_opts() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"const A = "a";
const B = wrap(A);
const C = "c";
const D = wrap(C);
const local = A;
function lazy() { return C; }
const outside = 7;
function extra() { return outside; }
console.log(B.ref, D.ref);
export { A, B, C, D };
"#,
        vec![
            logical_module(
                "mod_x",
                &[
                    Member::new("A"),
                    Member::new("D"),
                    Member::new("local"),
                    Member::new("lazy"),
                    Member::new("extra"),
                ],
            ),
            logical_module("mod_y", &[Member::new("B"), Member::new("C")]),
            logical_module("mod_outside", &[Member::new("outside")]),
        ],
    )
}

fn rejected_cycle_fixture() -> RejectedFixture {
    run_rejection_fixture(cycle_fixture_opts())
}

fn graph_path(rejected: &RejectedFixture) -> PathBuf {
    rejected.report_root.join("static/app/owner_graph.json")
}

fn gate_json(args: &[&str]) -> serde_json::Value {
    let out = run_debundle(args);
    assert!(
        out.status.success(),
        "gate {:?} exit: stderr={}",
        args,
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("gate output is JSON")
}

#[test]
fn sequenced_initializer_rejection_names_owner_location_rule_and_escape_hatch() {
    let rejected = rejected_cycle_fixture();
    let (binding, line) = if rejected.stderr.contains("`D` at static/app.js:4:11") {
        ("D", 4)
    } else if rejected.stderr.contains("`B` at static/app.js:2:11") {
        ("B", 2)
    } else {
        panic!(
            "rejection omitted sequenced initializer owner/location:\n{}",
            rejected.stderr
        );
    };
    for required in [
        "unknown_call",
        "(wrap)",
        "member-level `purity: pure` annotation",
    ] {
        assert!(
            rejected.stderr.contains(required),
            "missing {required:?} in rejection:\n{}",
            rejected.stderr
        );
    }
    let cycles: serde_json::Value = read_json(&rejected.report_root.join("static/app/cycles.json"));
    let cause = cycles[0]["cut"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|edge| edge.get("sequenced_owner"))
        .find(|cause| !cause.is_null() && cause["binding_names"][0] == binding)
        .expect("sequenced cycle edge carries purity owner cause");
    let reason = &cause["purity"]["reasons"][0];
    assert_eq!(reason["rule"], "unknown_call");
    assert_eq!(reason["detail"], "wrap");
    assert_eq!(reason["source_location"]["source_path"], "static/app.js");
    assert_eq!(reason["source_location"]["start_line"], line);
    assert_eq!(reason["source_location"]["start_column"], 11);
    assert!(
        reason["author_guidance"]
            .as_str()
            .unwrap()
            .contains("purity: pure")
    );

    let text = run_debundle(&[
        "gate",
        "describe",
        "0",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--format",
        "text",
    ]);
    let text = String::from_utf8_lossy(&text.stdout);
    assert!(
        text.contains(&format!("`{binding}` at static/app.js:{line}:11")),
        "{text}"
    );
    assert!(text.contains("unknown_call (wrap)"), "{text}");
}

#[test]
fn gate_list_reports_each_blocking_scc() {
    let rejected = rejected_cycle_fixture();
    let parsed = gate_json(&[
        "gate",
        "list",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--format",
        "json",
    ]);
    let entries = parsed["blocking_sccs"].as_array().unwrap();
    assert_eq!(entries.len(), 1, "{parsed}");
    assert_eq!(entries[0]["id"].as_u64(), Some(0));
    assert_eq!(entries[0]["module_count"].as_u64(), Some(2));
    assert!(entries[0]["cut_count"].as_u64().unwrap() >= 1, "{parsed}");
}

#[test]
fn gate_describe_recomputes_nonempty_evidence_from_real_artifacts() {
    let rejected = rejected_cycle_fixture();
    let parsed = gate_json(&[
        "gate",
        "describe",
        "0",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(parsed["id"].as_u64(), Some(0));
    let modules: BTreeSet<&str> = parsed["modules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m.as_str().unwrap())
        .collect();
    assert_eq!(
        modules,
        BTreeSet::from(["mod_x", "mod_y"]),
        "SCC modules are canonical ModulePaths: {parsed}"
    );
    // The unified-identity contract: cycles.json modules and the
    // recomputed evidence both use clean canonical paths — no
    // interned `logical:N` keys, no `<chunk_id>::<path>` spelling.
    let evidence = parsed["evidence"].as_array().unwrap();
    assert!(
        !evidence.is_empty(),
        "evidence recompute joined zero owner-graph edges against the SCC modules — \
         the two files speak different module vocabularies: {parsed}"
    );
    for e in evidence {
        assert_ne!(
            e["from"], e["to"],
            "intra-module edge leaked into evidence: {e}"
        );
        assert!(
            e["from_binding"].is_string(),
            "source binding label missing: {e}"
        );
        for endpoint in [&e["from"], &e["to"]] {
            let path = endpoint.as_str().unwrap();
            assert!(
                modules.contains(path),
                "evidence endpoint {path} outside SCC modules {modules:?}: {e}"
            );
            assert!(
                !path.contains("::") && !path.starts_with("logical:"),
                "evidence endpoint {path} is not a canonical ModulePath: {e}"
            );
        }
    }
    // Stored cut provenance and reconstructed evidence agree field-for-field,
    // including binding labels, sequencing causes, and source locations. The
    // evidence is deliberately broader: lazy/context edges are not cut edges.
    let cut = gate_json(&[
        "gate",
        "cut",
        "0",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(parsed["cut"], cut["cut"]);
    let cut_edges = cut["cut"].as_array().unwrap();
    assert!(!cut_edges.is_empty(), "{cut}");
    for edge in cut_edges {
        assert!(
            evidence.contains(edge),
            "cut row absent from evidence: {edge}"
        );
    }
    assert!(
        cut_edges
            .iter()
            .any(|edge| edge["sequenced_owner"].is_object()),
        "fixture must exercise sequencing provenance: {cut}"
    );
    // Both directions of the at-init cycle appear, naming the
    // bindings whose reads forced it.
    assert!(
        evidence
            .iter()
            .any(|e| e["from"] == "mod_y" && e["to"] == "mod_x" && e["binding"] == "A"),
        "evidence missing mod_y -> mod_x via A: {parsed}"
    );
    assert!(
        evidence
            .iter()
            .any(|e| e["from"] == "mod_x" && e["to"] == "mod_y" && e["binding"] == "C"),
        "evidence missing mod_x -> mod_y via C: {parsed}"
    );
    assert!(
        evidence.iter().any(|e| {
            e["from_binding"] == "lazy" && e["binding"] == "C" && e["kind"] == "lazy_use"
        }),
        "describe must include lazy edges, not just the cut: {parsed}"
    );
}

#[test]
fn gate_describe_binding_filter_narrows_evidence_to_one_symbol() {
    let rejected = rejected_cycle_fixture();
    // A occurs as the target binding, D as the source binding. Neither half
    // of the filter may disappear, and an absent binding must select nothing.
    for (binding, endpoint) in [
        ("A", "binding"),
        ("D", "from_binding"),
        ("absent", "binding"),
    ] {
        let parsed = gate_json(&[
            "gate",
            "describe",
            "0",
            "--graph",
            graph_path(&rejected).to_str().unwrap(),
            "--binding",
            binding,
            "--format",
            "json",
        ]);
        let evidence = parsed["evidence"].as_array().unwrap();
        if binding == "absent" {
            assert!(evidence.is_empty(), "{parsed}");
        } else {
            assert!(evidence.iter().any(|e| e[endpoint] == binding), "{parsed}");
        }
        for e in evidence {
            assert!(
                e["binding"] == binding || e["from_binding"] == binding,
                "binding filter kept an unrelated row: {e}"
            );
        }
    }
}

#[test]
fn gate_unknown_id_fails_cleanly() {
    let rejected = rejected_cycle_fixture();
    let out = run_debundle(&[
        "gate",
        "describe",
        "99",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
    ]);
    assert!(!out.status.success(), "describe 99 should fail");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("no blocking SCC with id 99"),
        "stderr: {stderr}"
    );
}

#[test]
fn dry_run_rejection_materializes_gate_artifacts() {
    // `debundle run --dry-run` keeps the no-output contract on the
    // accept path, but a gate rejection must still write
    // owner_graph.json + cycles.json at the standard report location
    // so the documented `gate list/describe` follow-up works on the
    // rejection that was just reported.
    let rejected = run_dry_run_rejection_fixture(cycle_fixture_opts());
    assert!(
        !rejected.out_root.join("app").exists(),
        "dry-run must not emit the JS tree"
    );

    let parsed = gate_json(&[
        "gate",
        "list",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(parsed["blocking_sccs"].as_array().unwrap().len(), 1);

    let parsed = gate_json(&[
        "gate",
        "describe",
        "0",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--format",
        "json",
    ]);
    let modules: BTreeSet<&str> = parsed["modules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m.as_str().unwrap())
        .collect();
    assert_eq!(modules, BTreeSet::from(["mod_x", "mod_y"]), "{parsed}");
    assert!(
        !parsed["evidence"].as_array().unwrap().is_empty(),
        "describe must recompute evidence from the dry-run owner graph: {parsed}"
    );
}

#[test]
fn gate_cycles_override_picks_up_custom_path() {
    // Move the real cycles.json away from the graph's sibling and
    // make sure `--cycles` finds it.
    let rejected = rejected_cycle_fixture();
    let sibling = rejected.report_root.join("static/app/cycles.json");
    let moved = rejected.report_root.join("elsewhere/cycles.json");
    fs::create_dir_all(moved.parent().unwrap()).unwrap();
    fs::rename(&sibling, &moved).unwrap();

    let parsed = gate_json(&[
        "gate",
        "list",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--cycles",
        moved.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(parsed["blocking_sccs"].as_array().unwrap().len(), 1);

    // With cycles.json gone from the default location and no
    // `--cycles`, the gate reads the clean state: zero blocking SCCs.
    let parsed = gate_json(&[
        "gate",
        "list",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(parsed["blocking_sccs"].as_array().unwrap().len(), 0);
}

#[test]
fn gate_list_and_cut_accept_cycles_without_graph() {
    let fixture = rejected_cycle_fixture();
    let cycles = fixture.report_root.join("static/app/cycles.json");
    let graph = graph_path(&fixture);
    for command in [vec!["list"], vec!["cut", "0"]] {
        let mut args = vec!["gate"];
        args.extend(command);
        args.extend(["--format", "json"]);
        let mut from_graph = args.clone();
        from_graph.extend(["--graph", graph.to_str().unwrap()]);
        args.extend(["--cycles", cycles.to_str().unwrap()]);
        assert_eq!(gate_json(&args), gate_json(&from_graph));
    }
}

#[test]
fn gate_describe_still_requires_graph_with_explicit_cycles() {
    let output = run_debundle(&["gate", "describe", "0", "--cycles", "missing.json"]);
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("--graph"), "{stderr}");
}

#[test]
fn scc_rejects_conflicting_size_filters_before_reading_files() {
    let output = run_debundle(&[
        "scc",
        "--graph",
        "missing.json",
        "--modules",
        "missing",
        "--cycles-only",
        "--singletons-only",
    ]);
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("cannot be used with"), "{stderr}");
    assert!(stderr.contains("--cycles-only"), "{stderr}");
    assert!(stderr.contains("--singletons-only"), "{stderr}");
}
