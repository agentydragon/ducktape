//! Serialized fixtures use case-relative inputs and the standard `out/` layout.

use super::{CaseWorkspace, Fixture, PreparedFixture, RejectedFixture};
use std::path::Path;

fn prepare(root: CaseWorkspace, spec: &Path) -> PreparedFixture {
    PreparedFixture {
        spec_path: root.path().join(spec),
        out_root: root.path().join("out"),
        report_root: root.path().join("out/reports/tree"),
        snapshot_root: root.path().to_path_buf(),
        modules_root: root.path().join("modules"),
        js_list_path: root.path().join(".fixture/js-files.txt"),
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
