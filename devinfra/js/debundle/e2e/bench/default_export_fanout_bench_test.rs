//! A shared default-export wrapper mixes otherwise independent call chains.
//! Keep raw metrics for comparing a future call-site return specialization.

use debundle_e2e_support::*;
use std::fmt::Write as _;
use std::path::PathBuf;

const WRAPPER: &str = include_str!("../fixtures/default_export_wrapper.js");

fn assert_fanout(width: usize) {
    let mut source = String::from(WRAPPER);
    for index in 0..width {
        writeln!(
            source,
            "const cell{index} = {index};\n\
             function callback{index}() {{ return cell{index}; }}\n\
             function read{index}(value) {{ return value.current; }}\n\
             const record{index} = {{ current: callback{index} }};\n\
             const selected{index} = unwrapDefault(read{index});"
        )
        .unwrap();
    }
    source.push_str("let sum = 0;\n");
    for index in 0..width {
        writeln!(
            source,
            "const forwarded{index} = selected{index}(record{index});\n\
             sum += forwarded{index}();"
        )
        .unwrap();
    }
    source.push_str("console.log(sum);\n");
    let outputs = PathBuf::from(
        std::env::var_os("TEST_UNDECLARED_OUTPUTS_DIR")
            .expect("Bazel undeclared outputs directory"),
    );
    let source_artifact = outputs.join(format!("default-export-fanout-{width}.js"));
    std::fs::write(&source_artifact, &source).unwrap();
    let metrics = outputs.join(format!("default-export-fanout-{width}.jsonl"));
    let fixture = run_fixture(
        FixtureOpts::new(&source, vec![])
            .with_unassigned_mode(unassigned_mode_inline())
            .with_extra_args(&["--metrics-file", metrics.to_str().unwrap()]),
    );
    assert_preserved_entry_output(&fixture, &format!("{}\n", width * (width - 1) / 2));
    assert!(
        source_artifact.is_file(),
        "raw benchmark JavaScript must be retained"
    );
    assert!(metrics.is_file(), "raw work metrics must be retained");
}

#[test]
fn independent_default_export_calls_128() {
    assert_fanout(128);
}

#[test]
fn independent_default_export_calls_256() {
    assert_fanout(256);
}
