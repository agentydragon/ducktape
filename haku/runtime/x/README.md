# haku/runtime/x — experimental runtime pieces

In-flux runtime code with no stable API, in the sense <../../../README.md> § `x/` gives the
convention. Design still under discussion in <../../plans/agent_sdk_sandbox_runtime.md>.

`agent_sdk_transport` is the sandbox side of the Claude chat experiment: the wire protocol
between console and sandbox, the `ClaudeAgentOptions`/launch construction, the WebSocket
transport, and the `runner_bin` that runs inside the sandbox image. It moved here with
`haku/console/x/claude_chat.py`, which is its only production consumer — the two halves of
one experiment, on both sides of the WebSocket.

**Gotcha:** the published image name is `haku-claude-runner`, set explicitly in
`.github/workflows/push-images.yml` rather than derived from this path, so moving the
package did not rename the image and nothing that pulls it had to change.
