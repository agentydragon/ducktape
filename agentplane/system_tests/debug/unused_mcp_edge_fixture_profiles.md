# Unused MCP Edge in system-test stacks

The application fixtures keep a Connection Service URL for the standalone MCP Edge. The required Pod scenarios, VM suites and Firecracker suite do not exercise external MCP Connections; they use Actions' command-box flow or ordinary Sandbox behavior. Those stacks were still rendering the MCP Edge Deployment and importing its image.

The original implementation used fixture profiles. Suite-owned Python deployment recipes now
call the additions directly: Pod, deletion, VM and Firecracker recipes omit MCP edge; probe,
real-model, History and MCP recipes add it. The App keeps its unused Connection routes and reports
the omitted service unavailable. Image manifests declare service image inputs separately. Actions
and its command-box executor remain in every fixture.

The renderer tests assert that the Pod, VM and Firecracker outputs omit the edge and its database,
and that the probe and MCP outputs contain the real edge deployment/configuration. Determinism
tests execute each recipe independently twice. These checks do not exercise a running stack or
measure wall-time savings. The change preserves scenario bodies and test frequency.

The architecture branch also exercised the unchanged Pod, full VM and both Firecracker cases at source `91f72591a1`, and a [synthetic selector probe](https://app.buildbuddy.io/invocation/c51d318c-90ea-4fe0-a892-6110260728ef) selected 15 rather than 18 tests for one isolated MCP-edge source change: only those three unrelated suites disappeared. MCP acceptance remained selected. That is source-specific graph evidence; the backport requires its own exact-head CI, and no representative workload frequency or wall-time saving is inferred.
