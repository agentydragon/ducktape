# Testing MCP server

Use the existing `tzolov/mcp-everything-server:v3` Docker image, pinned to the published
multi-platform digest in the testing Deployment
(`cluster/k8s/agentplane-testing/actions/mcp-everything-deployment.yaml`). The image packages the upstream
Everything reference server with streamable HTTP support. Its
[source and Dockerfile](https://github.com/tzolov/mcp-everything-server-docker-image/tree/18d3cedb9f3685fff86b9e18dad413c9ad99506c)
are maintained outside this repository. There is no Agentplane MCP server
implementation, custom image build, or image publication pipeline for this test.

The Action Service connects over streamable HTTP. Its opt-in decision provider permits
only the `echo` Action in the reviewed `everything` group with one bounded string
`message` argument. Other tools receive no automatic allow. The server has no workload
token, mounted credentials, writable root, public ingress, or outbound network access.
The broader upstream tool catalog is not a reason to maintain our own replacement server.

The existing live acceptance suite sends real Claude/Codex agents the Action API task
and checks their JSON reports. The remote Docker-backed runtime test exercises the same
pinned upstream image and provider composition without claiming to be the live testing deployment.

The official `mcp/everything` latest image was also checked: its published May 2025
digest lacks the streamable-HTTP entry point and failed the remote container test.
The externally built v3 image avoids adding our own image or transport wrapper.

## Testing MCP OAuth linkage

`everything` has no auth, so it cannot exercise the operator-managed OAuth linkage flow
(`x/agentplane/action_service/mcp_linkage.py`). For that,
`x/agentplane/action_service/test_fixtures/oauth_mcp_server.py` builds a small, self-contained
server: `fastmcp`'s `InMemoryOAuthProvider` (an in-memory OAuth 2.1 authorization server, built
for exactly this — simulating the flow with no external calls) fronting one `echo` tool, in one
process. A single client is pre-registered at startup with a fixed `client_id`, since dynamic
registration would mint an unpredictable one and Agentplane's `McpOAuthServer` configuration
needs to name it. This is our own code (unlike `everything`), so it is built and published like
any other in-cluster image (`agentplane-oauth-fixture` in `devinfra/ci/image_targets.json`) and
deployed under `cluster/k8s/agentplane-testing/actions/`, cluster-internal only (no public route),
same as `everything`. The OAuth authorize step is normally browser-facing, but the acceptance
suite runs from the `public-coder-devbox` KubeVirt VM
(`cluster/k8s/agents/public-coder-agent/devbox/`) -- a genuine pod-network endpoint, not an
external host -- so it reaches the fixture's `/authorize` directly the same way the Action
Service reaches it for discovery/token exchange, no port-forward or public exposure needed.
