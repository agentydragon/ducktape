//! Report-only effect translation validation: conditional (`?:`) and logical
//! (`&&`, `||`, `??`) operators through the kernel's fork, joined by
//! `alternatives/` when every path of the body has completed.

mod branch_receipts;
mod class_captures;
mod constructions;
mod eval;
mod generators;
mod iterator_installs;
mod summarized_locals;
mod unknown_values;

use debundle_e2e_support::*;

/// Runs `run` on the fixture both helpers split: `observed` (a read of
/// `counter`) in its own module after `result`, so the split runs `result`
/// first.
fn with_reader_split<T>(
    definitions: &str,
    extra: &[&'static str],
    run: impl FnOnce(FixtureOpts<'_>) -> T,
) -> T {
    let source = format!(
        "let counter = 0;\nfunction first() {{ return 0; }}\nfunction second() {{ counter = 1; return 1; }}\nfunction noop() {{ return 0; }}\n{definitions}\nconst observed = counter;\nvar result = run();\nconsole.log(observed, counter);\n"
    );
    let members: Vec<Member> = [
        "counter", "first", "second", "noop", "flag", "run", "result",
    ]
    .iter()
    .chain(extra)
    .map(|name| Member::new(name))
    .collect();
    run(FixtureOpts::new(
        &source,
        vec![
            logical_module("reader", &[Member::new("observed")]),
            logical_module("run", &members),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline()))
}

/// When `flag()` can hold a value the analysis omits, `second` runs and
/// writes `counter`: the source prints `0 1`, the split `1 1`. The gate keeps
/// `result` sequenced after the reader.
fn assert_reader_split_diverges_with(definitions: &str, extra: &[&'static str]) {
    let rejected = with_reader_split(definitions, extra, run_emit_on_rejection_fixture);
    let report = rejected.translation_validation();
    assert_eq!(report["gate_accepts"], false, "{definitions}: {report:#}");
    assert_eq!(report["accepts"], false, "{definitions}: {report:#}");
    assert_eq!(
        report["counts"]["conflicts_by_kind"],
        serde_json::json!({ "sequenced": 1 }),
        "{definitions}: {report:#}"
    );
    let conflict = &report["conflicts"]["examples"][0];
    assert_eq!(conflict["runs_first"]["module"], "run", "{report:#}");
    assert_eq!(conflict["runs_later"]["module"], "reader", "{report:#}");
    assert_node_output(
        &rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id)),
        "0 1\n",
        "",
    );
    assert_node_output(&rejected.entry_path(), "1 1\n", "");
}

fn assert_reader_split_diverges(definitions: &str) {
    assert_reader_split_diverges_with(definitions, &[]);
}

/// Positive anchor: the same split where `flag()` reaches only effect-free
/// functions is accepted, and the moved tree prints the source's `0 0`.
fn assert_reader_split_commutes(definitions: &str) {
    let fixture = with_reader_split(definitions, &[], run_fixture);
    assert_preserved_entry_output(&fixture, "0 0\n");
}

const CALL_FLAG: &str = "function run() { return flag()(); }";
