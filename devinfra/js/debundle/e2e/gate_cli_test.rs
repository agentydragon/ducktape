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
use std::process::Command;

use debundle_e2e_support::*;

/// A two-module at-init cycle the gate must reject:
/// `mod_x = {A, D}` where `D = wrap(C)` reads `C` from `mod_y`, and
/// `mod_y = {B, C}` where `B = wrap(A)` reads `A` from `mod_x`.
fn cycle_fixture_opts() -> FixtureOpts<'static> {
    FixtureOpts::new(
        r#"function wrap(x) { return { ref: x }; }
const A = "a";
const B = wrap(A);
const C = "c";
const D = wrap(C);
console.log(B.ref, D.ref);
export { A, B, C, D };
"#,
        vec![
            logical_module("mod_x", &[Member::new("A"), Member::new("D")]),
            logical_module("mod_y", &[Member::new("B"), Member::new("C")]),
        ],
    )
}

fn rejected_cycle_fixture() -> RejectedFixture {
    run_rejection_fixture(cycle_fixture_opts())
}

fn graph_path(rejected: &RejectedFixture) -> PathBuf {
    rejected.report_root.join("static/app/owner_graph.json")
}

fn run_gate(args: &[&str]) -> std::process::Output {
    Command::new(debundler_path())
        .args(args)
        .output()
        .expect("spawn debundle")
}

fn gate_json(args: &[&str]) -> serde_json::Value {
    let out = run_gate(args);
    assert!(
        out.status.success(),
        "gate {:?} exit: stderr={}",
        args,
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("gate output is JSON")
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
}

#[test]
fn gate_describe_binding_filter_narrows_evidence_to_one_symbol() {
    let rejected = rejected_cycle_fixture();
    let parsed = gate_json(&[
        "gate",
        "describe",
        "0",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--binding",
        "A",
        "--format",
        "json",
    ]);
    let evidence = parsed["evidence"].as_array().unwrap();
    assert!(!evidence.is_empty(), "{parsed}");
    for e in evidence {
        assert!(
            e["binding"] == "A" || e["from_binding"] == "A",
            "binding filter kept an unrelated row: {e}"
        );
    }
}

#[test]
fn gate_cut_returns_the_actionable_edges() {
    let rejected = rejected_cycle_fixture();
    let parsed = gate_json(&[
        "gate",
        "cut",
        "0",
        "--graph",
        graph_path(&rejected).to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(parsed["id"].as_u64(), Some(0));
    let cut = parsed["cut"].as_array().unwrap();
    assert!(!cut.is_empty(), "{parsed}");
    for e in cut {
        for endpoint in [&e["from"], &e["to"]] {
            let path = endpoint.as_str().unwrap();
            assert!(
                path == "mod_x" || path == "mod_y",
                "cut endpoint {path} outside the SCC: {e}"
            );
        }
    }
}

#[test]
fn gate_unknown_id_fails_cleanly() {
    let rejected = rejected_cycle_fixture();
    let out = run_gate(&[
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
