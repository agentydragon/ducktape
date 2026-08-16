# haku/runtime/x — experimental runtime pieces

In-flux runtime code with no stable API, in the sense <../../../README.md> § `x/` gives the
convention. Design still under discussion in <../../plans/agent_sdk_sandbox_runtime.md>.

`claude_bridge` is the sandbox side of the Claude chat experiment: the wire protocol between
console and sandbox, the CLI protocol client, the launch construction, the WebSocket transport, and
the `runner_bin` that runs inside the sandbox image. It moved here with
`haku/console/x/session_runtime.py`, which is its only production consumer — the two halves of one
experiment, on both sides of the WebSocket. It was `agent_sdk_transport` until the console took the
CLI protocol over and the name stopped being true.

**Gotcha:** the published image name is `haku-claude-runner`, set explicitly in
`.github/workflows/push-images.yml` rather than derived from this path, so moving the
package did not rename the image and nothing that pulls it had to change.
