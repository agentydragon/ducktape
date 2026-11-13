# Bazel test graph shadow

The checked-in snapshot records the labels used by the actual selected-graph A/B
probe. Its proposed replacement is comparison-only: no CI workflow consumes it,
and no required test was removed. It is neither a coverage certification nor a
complete diff-selected PR graph. Future replay must start from persisted actual
CI selection labels, preserve every unmapped target, and keep unknown mappings
on the current gate.

The pinned graph probe uses source `220e6a5a732656f6359234b99d92ae8635f77a6b` and base `6c5f5362e1d261f93c8a1c22a254c3f1c3e2c0ca`. A read-only Bazel query resolved the current MCP/Pod/VM targets and both manual `test_services` slices in [BuildBuddy invocation `bdd13729-c69a-4197-888a-9e5ab1fb3d0a`](https://app.buildbuddy.io/invocation/bdd13729-c69a-4197-888a-9e5ab1fb3d0a). The baseline control is [invocation `3a80aeab-436c-4722-b3b7-5c1f88e2470d`](https://app.buildbuddy.io/invocation/3a80aeab-436c-4722-b3b7-5c1f88e2470d). This is a pinned graph probe, not the complete diff-selected PR gate; [PR CI invocation `ab7094c9-df08-54b7-b87a-2db5c64fdc8d`](https://app.buildbuddy.io/invocation/ab7094c9-df08-54b7-b87a-2db5c64fdc8d) has the full selection, whose labels remain to be extracted for the historical coverage audit.

For the pinned cohort, the current set contains `//agentplane/system_tests/suites:test_pod`, `//agentplane/system_tests/suites:test_vm`, and `//agentplane/system_tests/suites/mcp:test_services`. The candidate set retains the Pod and VM targets and substitutes the MCP target with `//agentplane/system_tests/suites/mcp:test_services_mcp_candidate` and `//agentplane/system_tests/suites/mcp:test_services_central_candidate`. The snapshot records the actual source/base revisions and Bazel query invocation. Its current target list is scoped to this graph probe, not inferred from path names or claimed as the PR's full required set.
