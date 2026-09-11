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
