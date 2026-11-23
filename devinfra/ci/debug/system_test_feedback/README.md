# Agentplane system-test CI feedback

Start with the [four-minute system-test graph investigation](four_minute_system_test_graph.md) for the full-gate objective, observed limiting paths and the [initial Pod split patch](four_minute_system_test_graph.pod_split.patch).

- [Fixture image overlap](fixture_overlap_three_suite_graph_2026_11_12.md) compares deferred image imports on the selected Pod, MCP Actions and VM graph.
- [Controlled Pod TLS-tail split](pod_split_controlled_experiment.md) covers case selection, fixture isolation and the physical comparison. See the [manual-target patch](pod_split_controlled_experiment.patch) and [results](pod_split_controlled_experiment.results.json).
- [Balanced two-stack Pod partition](pod_balanced_split_experiment.md) and its [results](pod_balanced_split_experiment.results.json) compare the two-stack and unchanged Pod paths in the selected graph.
- [Three-stack Pod partition](pod_three_stack_partition_2026_11_12.md) records a different partition of Pod cases using the [manual-target patch](pod_three_stack_partition.patch).
- [Firecracker cleanup overlap and cold K3s image seeding](kvm_pilots_2026_11_22.md) records the measured timings and why both pilots were retired.
