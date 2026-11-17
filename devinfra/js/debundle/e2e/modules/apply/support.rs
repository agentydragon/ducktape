//! Shared mechanics for the `modules apply --assignments` E2E cases.

use debundle_e2e_graph_support::GraphFixture;
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};

pub fn import_order_file(fixture: &GraphFixture, assignments_path: &Path) -> PathBuf {
    let changes: Value = serde_json::from_slice(&fs::read(assignments_path).unwrap()).unwrap();
    fixture.write_assignment_import_order("import-order.json", &changes)
}

pub fn apply_args(fixture: &GraphFixture, assignments_path: &Path) -> Vec<String> {
    let import_order = import_order_file(fixture, assignments_path);
    apply_args_with_order(fixture, assignments_path, &import_order)
}

pub fn apply_args_with_order(
    fixture: &GraphFixture,
    assignments_path: &Path,
    import_order: &Path,
) -> Vec<String> {
    let changes: Value = serde_json::from_slice(&fs::read(assignments_path).unwrap()).unwrap();
    let physical = fixture.write_physical_assignment_batch("physical-assignments.json", &changes);
    vec![
        "modules".to_owned(),
        "apply".to_owned(),
        "--assignments".to_owned(),
        physical.to_string_lossy().into_owned(),
        "--modules".to_owned(),
        fixture
            .root()
            .join("modules")
            .to_string_lossy()
            .into_owned(),
        "--import-order".to_owned(),
        import_order.to_string_lossy().into_owned(),
        "--tree-config".to_owned(),
        fixture
            .root()
            .join("spec_config.yaml")
            .to_string_lossy()
            .into_owned(),
    ]
}
