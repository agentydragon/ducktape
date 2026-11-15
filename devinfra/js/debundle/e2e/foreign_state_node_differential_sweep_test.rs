use debundle_e2e_differential_sweep_cases::{SweepFamily, assert_case_registry, run_family};

#[test]
fn accepted_splits_of_stateful_provider_calls_match_node_binding_cells() {
    run_family(SweepFamily::ProviderBindingCells);
}

#[test]
fn accepted_splits_of_stateful_provider_calls_match_node_collections() {
    run_family(SweepFamily::ProviderCollections);
}

#[test]
fn accepted_splits_of_stateful_provider_calls_match_node_object_state() {
    run_family(SweepFamily::ProviderObjectState);
}

#[test]
fn accepted_splits_of_stateful_provider_calls_match_node_indirect_state() {
    run_family(SweepFamily::ProviderIndirectState);
}

#[test]
fn accepted_splits_of_direct_foreign_reads_match_node() {
    run_family(SweepFamily::DirectReads);
}

#[test]
fn accepted_splits_of_stateless_provider_calls_match_node() {
    run_family(SweepFamily::StatelessProviderCalls);
}

#[test]
fn case_registry_covers_the_original_matrix_once() {
    assert_case_registry();
}
