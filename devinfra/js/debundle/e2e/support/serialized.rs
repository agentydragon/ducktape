//! Serialized fixtures use case-relative paths from their complete spec.

use super::{CaseWorkspace, Fixture, PreparedFixture, RejectedFixture};
use std::path::Path;

fn prepare(root: CaseWorkspace, spec: &Path) -> PreparedFixture {
    let spec_path = root.path().join(spec);
    let spec: spec::TransformSpec =
        serde_yaml::from_slice(&std::fs::read(&spec_path).expect("read serialized spec"))
            .expect("parse serialized spec");
    PreparedFixture {
        spec_path,
        out_root: root.path().join(
            spec.write_js_tree
                .expect("serialized fixture writes its JS tree")
                .out_dir,
        ),
        report_root: root.path().join(
            spec.materialize_logical_modules
                .report_out_dir
                .expect("serialized fixture writes reports"),
        ),
        snapshot_root: root.path().join(spec.inputs.input_root),
        modules_root: root
            .path()
            .join(spec.materialize_logical_modules.target_dir),
        js_list_path: root.path().join(spec.inputs.js_list_path),
        browser_harness: None,
        root,
    }
}

pub fn run_serialized_fixture(
    root: CaseWorkspace,
    spec: &Path,
    chunk_id: &str,
    runtime_files: &[(&str, &str)],
) -> Fixture {
    super::run_prepared_fixture(prepare(root, spec), chunk_id, &[], false, runtime_files)
}

pub fn run_serialized_rejection_fixture(
    root: CaseWorkspace,
    spec: &Path,
    chunk_id: &str,
) -> RejectedFixture {
    super::run_prepared_rejection_fixture(
        prepare(root, spec),
        chunk_id,
        &[],
        1,
        false,
        "expected spec to be rejected",
        &[],
    )
}
