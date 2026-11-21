mod async_fallback;
mod class_reachability;
mod future_availability;
mod provider_initialization;
mod publication_order;
mod read_only_summary;

use debundle_e2e_support::*;

/// Runs `source` with every binding left in the entry and expects `expected_stdout`.
fn assert_unmoved_output(source: &str, peers: &[(&str, &str)], expected_stdout: &str) {
    let unmoved = run_fixture(
        FixtureOpts::new(source, Vec::new())
            .with_extra_chunks(peers)
            .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&unmoved, expected_stdout);
}

/// Expects moving only `binding` into `module` to be rejected as an atomic-group conflict.
fn expect_single_binding_rejection(
    source: &str,
    peers: &[(&str, &str)],
    module: &str,
    binding: &'static str,
) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![logical_module(module, &[Member::new(binding)])],
        )
        .with_extra_chunks(peers)
        .with_unassigned_mode(unassigned_mode_inline()),
        &[module],
        &[],
    );
}
