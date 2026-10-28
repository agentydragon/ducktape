//! Missing conflict reports must retain the earlier CLI rejection's context.

use debundle_e2e_support::{mixed_selector_failure_fixture, run_rejection_fixture};
use std::panic::catch_unwind;

#[test]
fn missing_conflict_report_includes_case_path_and_cli_stderr() {
    let fixture = run_rejection_fixture(mixed_selector_failure_fixture());
    assert!(!fixture.stderr.is_empty());
    for filename in ["cycles.json", "atomic_group_conflicts.json"] {
        let path = fixture.report_root.join(&fixture.chunk_id).join(filename);
        assert!(
            !path.exists(),
            "selector rejection must precede conflict reports"
        );
        let panic = catch_unwind(|| match filename {
            "cycles.json" => fixture.cycles(),
            _ => fixture.atomic_group_conflicts(),
        })
        .expect_err("reading the missing report must fail");
        let message = panic.downcast_ref::<String>().expect("diagnostic string");
        assert!(message.contains(path.to_str().unwrap()), "{message}");
        assert!(message.contains(&fixture.stderr), "{message}");
    }
}
