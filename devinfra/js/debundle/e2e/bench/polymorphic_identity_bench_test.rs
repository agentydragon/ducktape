//! Repeated independent identity calls expose shared-return projection fanout.
//! Bodies the call-return recognizer does not admit (docs/inference.md
//! "Bounded call-return relationships") still read one pooled return cell, so N
//! independent projections repeat the receiver walk (N^2); the recognizer keeps
//! this family linear.

use debundle_e2e_support::*;
use std::fmt::Write as _;
use std::path::PathBuf;

fn source(calls: usize, shared_identity: bool) -> String {
    // One declaration per call, each with its own object literal: a JavaScript
    // loop would reuse one allocation site and not reproduce the relation. The
    // object-valued field is deliberate: primitive fields keep the N^2
    // subscriptions but lose the large result bitmaps, and no fields leave only
    // N^2 missing-property decisions.
    let mut source = String::from("function identity(value) { return value; }\n");
    for index in 0..calls {
        let receiver = if shared_identity {
            "identity({ value: {} })"
        } else {
            "({ value: {} })"
        };
        writeln!(source, "const result{index} = {receiver}.value;").unwrap();
    }
    writeln!(source, "console.log(result0 === result{});", calls - 1).unwrap();
    source
}

#[test]
fn independent_calls_preserve_distinct_objects_and_export_work_counts() {
    let outputs = PathBuf::from(
        std::env::var_os("TEST_UNDECLARED_OUTPUTS_DIR")
            .expect("Bazel undeclared outputs directory"),
    );
    for calls in [128, 512, 2048] {
        for shared_identity in [true, false] {
            let variant = if shared_identity {
                "identity"
            } else {
                "direct"
            };
            let source = source(calls, shared_identity);
            let metrics = outputs.join(format!("{variant}-{calls}.jsonl"));
            std::fs::write(outputs.join(format!("{variant}-{calls}.js")), &source).unwrap();
            let fixture = run_fixture_with_args(
                FixtureOpts::new(&source, vec![]).with_unassigned_mode(unassigned_mode_inline()),
                &["--metrics-file", metrics.to_str().unwrap()],
            );
            assert_preserved_entry_output(&fixture, "false\n");
        }
    }
}
