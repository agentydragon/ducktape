# Unused MCP Edge in system-test stacks

The application fixtures keep a Connection Service URL for the standalone MCP Edge. The required Pod scenarios, VM suites and Firecracker suite do not exercise external MCP Connections; they use Actions' command-box flow or ordinary Sandbox behavior. Those stacks were still rendering the MCP Edge Deployment and importing its image.

`FixtureProfile.NO_MCP_EDGE` removes the edge construct for the `pod_scenarios`, VM and Firecracker fixtures. The app keeps its unused Connection routes and reports the service unavailable. Full `pod`, deletion, real-model and MCP fixture profiles retain the edge. The image manifest selects common service images separately from the MCP Edge image, so the omitted construct also removes the configured OCI image input. Actions and its command-box executor remain in every fixture.

The renderer tests compare the Pod fixture with the full fixture and require every retained object to be identical, with only edge-named resources absent. They also compare VM and Firecracker output against full renderings, and assert that the full default and MCP fixtures still contain the real edge deployment/configuration. These tests are the scoped rendering proof; they do not exercise a running stack or measure wall-time savings. This backport was checked with repository hooks only pending root review and CI.
