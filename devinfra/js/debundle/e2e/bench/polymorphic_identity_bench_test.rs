//! Repeated independent identity calls expose shared-return projection fanout.

use debundle_e2e_support::*;
use std::fmt::Write as _;
use std::path::PathBuf;

fn source(calls: usize, shared_identity: bool) -> String {
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
            let fixture = run_fixture(
                FixtureOpts::new(&source, vec![])
                    .with_unassigned_mode(unassigned_mode_inline())
                    .with_extra_args(&["--metrics-file", metrics.to_str().unwrap()]),
            );
            assert_preserved_entry_output(&fixture, "false\n");
        }
    }
}
