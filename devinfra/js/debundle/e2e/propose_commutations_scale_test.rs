//! `spec propose-commutations` must not scan every graph edge per candidate
//! pair: thousands of inertable pairs beside a large unrelated edge set.

use debundle_e2e_support::*;
use serde_json::Value;
use std::fmt::Write as _;

const STEPS: usize = 8192;
const READERS: usize = 128;

#[test]
fn many_pairs_beside_many_unrelated_edges() {
    // `globalThis.record` touches no chunk binding, so consecutive steps are
    // ordered only by opaque-call strict-chain adjacency, which a premise
    // inerts. Alternating modules makes every adjacent pair a cross-module
    // candidate; the readers add `READERS * STEPS` lazy edges no pair joins.
    // Every endpoint is a tree member, so no selector is synthesized.
    let mut source =
        String::from("globalThis.record = (key) => { console.log(\"step\", key); };\n");
    let mut modules = [String::from("members:\n"), String::from("members:\n")];
    for step in 0..STEPS {
        writeln!(
            source,
            "const step{step} = (globalThis.record({step}), {step});"
        )
        .unwrap();
        writeln!(
            modules[step % 2],
            "  - name: step{step}\n    selector:\n      binding: {{ name: step{step} }}"
        )
        .unwrap();
    }
    for reader in 0..READERS {
        write!(source, "function reader{reader}() {{ return [").unwrap();
        for step in 0..STEPS {
            write!(source, "step{step},").unwrap();
        }
        source.push_str("]; }\n");
    }
    source.push_str("console.log(step0, reader0);\n");
    let run = run_tree_fixture(
        &TreeFixture {
            chunks: &[("app", &source)],
            module_roots: &[("tree", "app")],
            modules: &[
                ("tree/Even.yaml", &modules[0]),
                ("tree/Odd.yaml", &modules[1]),
            ],
        },
        &[],
    );
    assert_eq!(
        run.result.status.code(),
        Some(1),
        "the split must be rejected\nstderr:\n{}",
        run.result.stderr
    );
    let graph = run.report_root.join("app/dependency_graph.json");
    let modules_root = run.root().join("modules/tree");
    let source_root = run.root().join("snapshot");
    let out = run_debundle(&[
        "spec",
        "propose-commutations",
        "--graph",
        graph.to_str().unwrap(),
        "--source-root",
        source_root.to_str().unwrap(),
        "--modules",
        modules_root.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert!(
        out.status.success(),
        "propose-commutations failed\nstderr:\n{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let report: Value = parse_stdout_json(&out);
    let proposals = report["proposals"].as_array().expect("proposals");
    assert!(proposals.len() >= STEPS - 2, "{}", proposals.len());
}
