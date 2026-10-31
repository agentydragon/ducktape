//! Budget control for `spec propose-commutations`: thousands of inertable
//! pairs beside a large unrelated edge set, without `--modules`, must finish
//! well inside the budget. Scanning every graph edge per pair, or proving a
//! selector for every endpoint instead of the emitted ones, exceeds it.

use debundle_e2e_support::*;
use serde_json::Value;
use spec::{BindingSelector, Member as SpecMember, MemberSelector};
use std::fmt::Write as _;
use std::time::{Duration, Instant};

const STEPS: usize = 4096;
const READERS: usize = 96;
/// About 3x a fastbuild run over this fixture, and a third of the run that
/// scanned every edge per pair.
const BUDGET: Duration = Duration::from_secs(30);

#[test]
fn many_pairs_beside_many_unrelated_edges_fit_the_budget() {
    // `globalThis.record` touches no chunk binding, so consecutive steps are
    // ordered only by opaque-call strict-chain adjacency, which a premise
    // inerts. Alternating modules makes every adjacent pair a cross-module
    // candidate; the readers add `READERS * STEPS` lazy edges no pair joins.
    let mut source =
        String::from("globalThis.record = (key) => { console.log(\"step\", key); };\n");
    let mut modules = [logical_module("even", &[]), logical_module("odd", &[])];
    for step in 0..STEPS {
        writeln!(
            source,
            "const step{step} = (globalThis.record({step}), {step});"
        )
        .unwrap();
        let name = format!("step{step}");
        modules[step % 2].1.members.push(SpecMember {
            name: Some(name.clone()),
            selector: MemberSelector {
                binding: Some(BindingSelector { name, kind: None }),
                ..Default::default()
            },
        });
    }
    for reader in 0..READERS {
        write!(source, "function reader{reader}() {{ return [").unwrap();
        for step in 0..STEPS {
            write!(source, "step{step},").unwrap();
        }
        source.push_str("]; }\n");
    }
    source.push_str("console.log(step0, reader0);\n");
    let rejected = run_rejection_fixture(
        FixtureOpts::new(&source, modules.into()).with_unassigned_mode(unassigned_mode_inline()),
    );
    let graph = rejected.report_path("dependency_graph.json");
    let started = Instant::now();
    let out = run_debundle(&[
        "spec",
        "propose-commutations",
        "--graph",
        graph.to_str().unwrap(),
        "--source-root",
        rejected.source_root.to_str().unwrap(),
        "--format",
        "json",
    ]);
    let elapsed = started.elapsed();
    eprintln!("propose-commutations took {elapsed:?}");
    assert!(
        out.status.success(),
        "propose-commutations failed\nstderr:\n{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        elapsed < BUDGET,
        "{elapsed:?} exceeds the {BUDGET:?} budget"
    );
    let report: Value = parse_stdout_json(&out);
    assert!(
        report["totals"]["proposals"]
            .as_u64()
            .is_some_and(|total| total >= STEPS as u64 - 2),
        "{}",
        report["totals"]
    );
}
